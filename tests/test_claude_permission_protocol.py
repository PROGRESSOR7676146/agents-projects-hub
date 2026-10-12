import json
import unittest
from uuid import uuid4

from hermes_codex_router.claude_permission_protocol import (
    PermissionProtocolError,
    ProtectedPayload,
    canonical_json,
    parse_json_strict,
)


class ProtocolTests(unittest.TestCase):
    def test_payload_roundtrip(self):
        value = ProtectedPayload(
            request_nonce=str(uuid4()),
            job_id="job_1",
            session_id=str(uuid4()),
            generation=1,
            root_digest="a" * 64,
            lease_id=str(uuid4()),
            launch_epoch=str(uuid4()),
            tool_name="Edit",
            tool_input={"file_path": "example.py"},
            expires_at=1234567890000,
        )
        self.assertEqual(ProtectedPayload.parse(value.to_json()), value)
        self.assertEqual(json.loads(value.to_json())["writer"], "telegram")

    def test_strict_json_rejects_duplicate_and_nan(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}', '"\\ud800"'):
            with self.subTest(raw=raw), self.assertRaises(PermissionProtocolError):
                parse_json_strict(raw)

    def test_canonical_json_rejects_non_json(self):
        with self.assertRaises(PermissionProtocolError):
            canonical_json({"a": float("nan")})


if __name__ == "__main__":
    unittest.main()
