"""Native urllib attempt boundaries with fake sockets; no network or credentials."""

from __future__ import annotations

import io
import os
import socket
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_codex_router.delivery_retry import delivery_failure_phase, retryable_delivery_failure
from hermes_codex_router.telegram import TelegramBotApi, TelegramError
from hermes_codex_router.telegram_transport import _InitialConnectNotSent, urlopen_with_send_proof


def response_socket(body: bytes = b'{"ok":true,"result":{"message_id":71}}') -> Mock:
    sock = Mock()
    sock.makefile.return_value = io.BytesIO(
        b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    )
    return sock


class NativeNotSentTests(unittest.TestCase):
    def test_initial_connection_failures_retry_text_and_document(self) -> None:
        for failure in (
            socket.gaierror(socket.EAI_AGAIN, "private DNS detail"),
            ConnectionRefusedError("private connection detail"),
            TimeoutError("private connect detail"),
        ):
            for document in (False, True):
                with (
                    self.subTest(failure=type(failure).__name__, document=document),
                    patch.dict(os.environ, {}, clear=True),
                    patch("socket.create_connection", side_effect=failure) as connect,
                    tempfile.TemporaryDirectory() as directory,
                ):
                    bot = TelegramBotApi("123456:example")
                    path = Path(directory) / "example.md"
                    path.write_bytes(b"Example attachment")
                    with self.assertRaises(TelegramError) as caught:
                        if document:
                            bot.send_document(-1001234567890, 77, path)
                        else:
                            bot.send_html(-1001234567890, 77, "Example")
                    connect.assert_called_once()
                    self.assertTrue(retryable_delivery_failure(caught.exception))
                    self.assertEqual(delivery_failure_phase(caught.exception), "proven_not_sent")
                    self.assertNotIn("private", str(caught.exception))

    def test_custom_opener_errors_have_no_connection_phase_proof(self) -> None:
        for failure in (socket.gaierror("DNS"), ConnectionRefusedError(), TimeoutError()):
            with self.subTest(failure=type(failure).__name__):
                bot = TelegramBotApi(
                    "123456:example", opener=Mock(side_effect=urllib.error.URLError(failure))
                )
                with self.assertRaises(TelegramError) as caught:
                    bot.send_html(-1001234567890, 77, "Example")
                self.assertFalse(retryable_delivery_failure(caught.exception))

    def test_custom_opener_cannot_supply_native_error_label(self) -> None:
        for document in (False, True):
            with self.subTest(document=document), tempfile.TemporaryDirectory() as directory:
                bot = TelegramBotApi(
                    "123456:example",
                    opener=Mock(
                        side_effect=urllib.error.URLError(_InitialConnectNotSent(socket.gaierror()))
                    ),
                )
                path = Path(directory) / "example.md"
                path.write_text("Example")
                with self.assertRaises(TelegramError) as caught:
                    if document:
                        bot.send_document(-1001234567890, 77, path)
                    else:
                        bot.send_html(-1001234567890, 77, "Example")
                self.assertEqual(delivery_failure_phase(caught.exception), "unknown")

    def test_custom_retry_wrapper_cannot_prove_unsent_original_request(self) -> None:
        def retrying_opener(request, *, timeout):
            try:
                return urlopen_with_send_proof(request, timeout=timeout)
            except OSError:
                return urlopen_with_send_proof(request, timeout=timeout)

        for document in (False, True):
            sock = response_socket()
            sock.makefile.side_effect = TimeoutError("read after sending")
            with (
                self.subTest(document=document),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(os.environ, {}, clear=True),
                patch("socket.create_connection", side_effect=[sock, socket.gaierror()]) as connect,
                patch("ssl.SSLContext.wrap_socket", return_value=sock),
            ):
                bot = TelegramBotApi("123456:example", opener=retrying_opener)
                path = Path(directory) / "example.md"
                path.write_text("Example")
                with self.assertRaises(TelegramError) as caught:
                    if document:
                        bot.send_document(-1001234567890, 77, path)
                    else:
                        bot.send_html(-1001234567890, 77, "Example")
                self.assertEqual(connect.call_count, 2)
                self.assertGreater(sock.sendall.call_count, 0)
                self.assertEqual(delivery_failure_phase(caught.exception), "unknown")

    def test_after_connection_or_transmission_errors_remain_unknown(self) -> None:
        for failure in (socket.gaierror("DNS"), ConnectionRefusedError(), TimeoutError()):
            for seam in ("send", "read", "tls"):
                with (
                    self.subTest(failure=type(failure).__name__, seam=seam),
                    patch.dict(os.environ, {}, clear=True),
                ):
                    sock = response_socket()
                    if seam == "send":
                        sock.sendall.side_effect = failure
                    elif seam == "read":
                        sock.makefile.side_effect = failure
                    with (
                        patch("socket.create_connection", return_value=sock),
                        patch(
                            "ssl.SSLContext.wrap_socket",
                            side_effect=failure if seam == "tls" else None,
                            return_value=sock,
                        ),
                    ):
                        bot = TelegramBotApi("123456:example")
                        with self.assertRaises(TelegramError) as caught:
                            bot.send_html(-1001234567890, 77, "Example")
                    self.assertFalse(retryable_delivery_failure(caught.exception))

    def test_redirect_then_dns_failure_cannot_retry_original_send(self) -> None:
        sock = response_socket()
        sock.makefile.return_value = io.BytesIO(
            b"HTTP/1.1 302 Found\r\nLocation: https://example.com/next\r\nContent-Length: 0\r\n\r\n"
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("socket.create_connection", side_effect=[sock, socket.gaierror("DNS")]),
            patch("ssl.SSLContext.wrap_socket", return_value=sock),
        ):
            with self.assertRaises(TelegramError) as caught:
                TelegramBotApi("123456:example").send_html(-1001234567890, 77, "Example")
        self.assertGreater(sock.sendall.call_count, 0)
        self.assertFalse(retryable_delivery_failure(caught.exception))

    def test_https_proxy_connect_failure_after_transmission_is_unknown(self) -> None:
        sock = response_socket()
        sock.makefile.side_effect = socket.gaierror("DNS after proxy CONNECT")
        with (
            patch.dict(os.environ, {"https_proxy": "http://example.com:3128"}, clear=True),
            patch("socket.create_connection", return_value=sock),
            patch("ssl.SSLContext.wrap_socket", return_value=sock),
        ):
            with self.assertRaises(TelegramError) as caught:
                TelegramBotApi("123456:example").send_html(-1001234567890, 77, "Example")
        self.assertTrue(sock.sendall.call_args.args[0].startswith(b"CONNECT "))
        self.assertFalse(retryable_delivery_failure(caught.exception))

    def test_success_keeps_native_https_verification_and_receipt(self) -> None:
        sock = response_socket()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("socket.create_connection", return_value=sock),
            patch("ssl.SSLContext.wrap_socket", autospec=True, return_value=sock) as tls,
        ):
            receipt = TelegramBotApi("123456:example").send_html(-1001234567890, 77, "Example")
        self.assertEqual(receipt, 71)
        context = tls.call_args.args[0]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(tls.call_args.kwargs["server_hostname"], "api.telegram.org")

    def test_each_call_has_its_own_witness_even_with_a_configured_proxy(self) -> None:
        sock = response_socket()
        sock.makefile.side_effect = [
            io.BytesIO(b"HTTP/1.1 200 Connection Established\r\n\r\n"),
            io.BytesIO(
                b"HTTP/1.1 200 OK\r\nContent-Length: 38\r\n\r\n"
                b'{"ok":true,"result":{"message_id":71}}'
            ),
        ]
        bot = TelegramBotApi("123456:example")
        with (
            patch.dict(os.environ, {"https_proxy": "http://example.com:3128"}, clear=True),
            patch("socket.create_connection", side_effect=[sock, socket.gaierror("DNS")]),
            patch("ssl.SSLContext.wrap_socket", return_value=sock),
        ):
            self.assertEqual(bot.send_html(-1001234567890, 77, "Example"), 71)
            with self.assertRaises(TelegramError) as caught:
                bot.send_html(-1001234567890, 77, "Example follow-up")
        self.assertEqual(delivery_failure_phase(caught.exception), "proven_not_sent")
