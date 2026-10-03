import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from hermes_codex_router.claude_permission_protocol import ProtectedPayload
from hermes_codex_router.tlive_permissions import (
    ProtectedTliveClient,
    TlivePermissionError,
    load_tlive_permission_config,
)


class TliveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "private.json"
        self.sock = str(Path(self.tmp.name) / "tlive.sock")
        self.cfg = {
            "version": 1,
            "socket_path": self.sock,
            "owner_id": "123",
            "chat_id": "456",
            "request_key": "11" * 32,
            "result_key": "22" * 32,
        }
        self.write_config()

    def write_config(self):
        self.path.write_text(json.dumps(self.cfg), encoding="utf-8")
        self.path.chmod(0o600)

    def test_private_file_checks(self):
        self.assertEqual(load_tlive_permission_config(self.path).chat_id, "456")
        self.path.chmod(0o400)
        with self.assertRaises(TlivePermissionError):
            load_tlive_permission_config(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(TlivePermissionError):
            load_tlive_permission_config(self.path)
        self.path.unlink()
        self.path.symlink_to(Path(self.tmp.name) / "other")
        with self.assertRaises(TlivePermissionError):
            load_tlive_permission_config(self.path)

    def test_handshake_and_authenticated_human_allow(self):
        epoch = str(uuid4())
        import time

        payload = ProtectedPayload(
            request_nonce=str(uuid4()),
            job_id="job_1",
            session_id=str(uuid4()),
            generation=1,
            root_digest="a" * 64,
            lease_id=str(uuid4()),
            launch_epoch=str(uuid4()),
            tool_name="Read",
            tool_input={},
            expires_at=int((time.time() + 60) * 1000),
        ).to_json()

        def mac(key, fields):
            data = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()
            return hmac.new(bytes.fromhex(key), data, hashlib.sha256).hexdigest()

        class FakeSocket:
            reply = b""
            tamper = False

            def __init__(self, *_args):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def settimeout(self, _value):
                pass

            def connect(self, _path):
                pass

            def sendall(self, wire):
                request = json.loads(wire)
                if request["kind"] == "hub.permission.hello":
                    result = {
                        "kind": "hub.permission.capability",
                        "version": 1,
                        "epoch": epoch,
                        "tag": mac(
                            self.cfg["result_key"],
                            ["capability", request["challenge"], epoch, 1],
                        ),
                    }
                else:
                    actor = {
                        "callbackId": "callback",
                        "userId": "123",
                        "isBot": False,
                        "chatId": "456",
                        "chatType": "private",
                        "messageId": "7",
                        "data": "hp:" + json.loads(payload)["requestNonce"] + ":a",
                    }
                    if self.tamper:
                        actor["userId"] = "999"
                    result = {
                        "kind": "hub.permission.result",
                        "decision": "allow",
                        "epoch": epoch,
                        "payload": payload,
                        "actor": actor,
                        "tag": mac(
                            self.cfg["result_key"],
                            ["result", epoch, payload, "allow", actor],
                        ),
                    }
                self.reply = json.dumps(result).encode() + b"\n"

            def recv(self, _size):
                reply, self.reply = self.reply, b""
                return reply

            cfg = self.cfg

        from hermes_codex_router import tlive_permissions

        with (
            patch.object(tlive_permissions, "_safe_socket_path"),
            patch.object(tlive_permissions.socket, "socket", FakeSocket),
            patch(
                "hermes_codex_router.unix_peer._peer_credentials",
                return_value=(os.getpid(), os.geteuid(), os.getegid()),
            ),
        ):
            client = ProtectedTliveClient(load_tlive_permission_config(self.path))
            capability = client.hello()
            self.assertEqual(capability.epoch, epoch)
            self.assertEqual(client.request(payload, capability).decision, "allow")
            FakeSocket.tamper = True
            with self.assertRaises(TlivePermissionError):
                client.request(payload, capability)

    def test_untrusted_peer_closes_without_sending_hello(self):
        from hermes_codex_router import tlive_permissions

        instances = []

        class FakeSocket:
            sent = False
            closed = False

            def __init__(self, *_args):
                instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

            def settimeout(self, _value):
                pass

            def connect(self, _path):
                pass

            def sendall(self, _wire):
                self.sent = True

            def close(self):
                self.closed = True

        for result in ((1, os.geteuid() + 1, 0), OSError("unavailable")):
            with self.subTest(result=result):
                options = (
                    {"return_value": result}
                    if isinstance(result, tuple)
                    else {"side_effect": result}
                )
                with (
                    patch.object(tlive_permissions, "_safe_socket_path"),
                    patch.object(tlive_permissions.socket, "socket", FakeSocket),
                    patch("hermes_codex_router.unix_peer._peer_credentials", **options),
                ):
                    with self.assertRaisesRegex(TlivePermissionError, "protected peer unavailable"):
                        ProtectedTliveClient(load_tlive_permission_config(self.path)).hello()
                self.assertFalse(instances[-1].sent)
                self.assertTrue(instances[-1].closed)
        self.assertEqual(len(instances), 2)


if __name__ == "__main__":
    unittest.main()
