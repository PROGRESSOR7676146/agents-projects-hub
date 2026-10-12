"""Controlling-terminal ownership without inspecting terminal contents."""

from __future__ import annotations

import errno
import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tests.native_pty_capture import NativePtyError, NativePtyInput, capture_owned_pty


class NativePtyCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="example-native-pty-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {"PATH": "/usr/bin:/bin", "TERM": "xterm-256color"}

    def argv(self, code: str) -> tuple[str, ...]:
        return (sys.executable, "-I", "-c", code)

    def capture(self, code: str, **options):
        return capture_owned_pty(
            self.argv(code),
            self.environment,
            timeout=options.pop("timeout", 3),
            output_limit=options.pop("output_limit", 65536),
            **options,
        )

    def assert_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = Path(f"/proc/{pid}/stat")
            try:
                state = status.read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                # A process may disappear after opening its proc entry and
                # before read; Linux can report ESRCH as well as ENOENT.
                return
            if state == "Z":
                return
            time.sleep(0.02)
        self.fail("owned fixture process survived cleanup")

    def test_cleanup_observation_accepts_process_disappearing_during_read(self) -> None:
        for error in (
            FileNotFoundError(errno.ENOENT, "example-process-gone"),
            ProcessLookupError(errno.ESRCH, "example-process-gone"),
        ):
            with (
                self.subTest(error_type=type(error).__name__),
                patch.object(Path, "exists", return_value=True),
                patch.object(Path, "read_text", side_effect=error),
            ):
                self.assert_gone(os.getpid())

    def test_cleanup_observation_preserves_other_read_failures(self) -> None:
        for error in (
            PermissionError(errno.EACCES, "example-denied"),
            OSError(errno.EIO, "example-io-error"),
        ):
            with (
                self.subTest(error_type=type(error).__name__),
                patch.object(Path, "exists", return_value=True),
                patch.object(Path, "read_text", side_effect=error),
                self.assertRaises(OSError),
            ):
                self.assert_gone(os.getpid())

    def test_real_controlling_terminal_literal_input_and_discarded_output(self) -> None:
        proof = self.root / "proof.json"
        ready = self.root / "ready"
        first = self.root / "first"
        code = (
            "import os,sys,json,termios,fcntl,struct; from pathlib import Path; "
            f"proof=Path({str(proof)!r}); ready=Path({str(ready)!r}); first=Path({str(first)!r}); "
            "ready.touch(); a=sys.stdin.readline(); first.touch(); b=sys.stdin.readline(); "
            "facts={'tty':all(os.isatty(fd) for fd in (0,1,2)),"
            "'same':len({os.fstat(fd).st_rdev for fd in (0,1,2)})==1,"
            "'session':os.getsid(0)==os.getpid(),'group':os.getpgrp()==os.getpid(),"
            "'foreground':os.tcgetpgrp(0)==os.getpid(),"
            "'size':list(struct.unpack('HHHH',fcntl.ioctl(0,termios.TIOCGWINSZ,b'\\0'*8))[:2]),"
            "'input':[a,b]}; proof.write_text(json.dumps(facts)); "
            "os.write(1,b'fictional terminal output\\n'); os.write(2,b'fictional stderr\\n')"
        )
        result = self.capture(
            code,
            inputs=(
                NativePtyInput(b"literal prompt\r", ready.exists),
                NativePtyInput(b"/exit\r", first.exists),
            ),
        )
        facts = json.loads(proof.read_text())
        self.assertTrue(all(facts[k] for k in ("tty", "same", "session", "group", "foreground")))
        self.assertEqual(facts["size"], [24, 80])
        self.assertEqual(facts["input"], ["literal prompt\n", "/exit\n"])
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.normal_exit)
        self.assertTrue(result.tty_verified)
        self.assertEqual(result.input_bytes, len(b"literal prompt\r/exit\r"))
        self.assertGreater(result.output_bytes, 0)
        self.assertFalse(hasattr(result, "output"))
        self.assertNotIn("literal prompt", repr(result))

    def test_signal_exit_is_distinct_from_normal_native_exit(self) -> None:
        result = self.capture("import os,signal; os.kill(os.getpid(),signal.SIGTERM)")
        self.assertFalse(result.normal_exit)
        self.assertEqual(result.exit_code, -signal.SIGTERM)

    def test_normal_nonzero_exit_is_not_rewritten_to_success(self) -> None:
        result = self.capture("raise SystemExit(7)")
        self.assertTrue(result.normal_exit)
        self.assertEqual(result.exit_code, 7)

    def test_timeout_kills_owned_leader_and_descendant(self) -> None:
        proof = self.root / "pids.json"
        code = (
            "import os,json,time; from pathlib import Path; child=os.fork(); "
            "\nif child == 0: time.sleep(30)"
            f"\nPath({str(proof)!r}).write_text(json.dumps([os.getpid(),child])); time.sleep(30)"
        )
        with self.assertRaisesRegex(NativePtyError, "native_pty_timeout"):
            self.capture(code, timeout=0.5)
        for pid in json.loads(proof.read_text()):
            self.assert_gone(pid)

    def test_exited_leader_is_not_reaped_before_descendant_cleanup(self) -> None:
        proof = self.root / "pids.json"
        code = (
            "import os,json,time; from pathlib import Path; child=os.fork(); "
            "\nif child == 0: time.sleep(30)"
            f"\nPath({str(proof)!r}).write_text(json.dumps([os.getpid(),child])); os._exit(0)"
        )
        result = self.capture(code)
        self.assertTrue(result.normal_exit)
        self.assertEqual(result.exit_code, 0)
        for pid in json.loads(proof.read_text()):
            self.assert_gone(pid)

    def test_output_flood_fails_bounded_and_cleans_process(self) -> None:
        proof = self.root / "pid"
        code = (
            "import os; from pathlib import Path; "
            f"Path({str(proof)!r}).write_text(str(os.getpid())); "
            "\nwhile True: os.write(1,b'x'*8192)"
        )
        with self.assertRaisesRegex(NativePtyError, "native_pty_output_bound"):
            self.capture(code, output_limit=1024)
        self.assert_gone(int(proof.read_text()))

    def test_terminal_eof_with_living_leader_waits_for_deadline_and_reaps(self) -> None:
        proof = self.root / "pid"
        closed = self.root / "closed"
        code = (
            "import os,time; from pathlib import Path; "
            f"Path({str(proof)!r}).write_text(str(os.getpid())); "
            "os.close(0); os.close(1); os.close(2); "
            f"Path({str(closed)!r}).touch(); time.sleep(30)"
        )
        with self.assertRaisesRegex(NativePtyError, "native_pty_timeout"):
            self.capture(code, timeout=0.75)
        self.assertTrue(closed.exists(), "fixture must close the terminal before timing out")
        self.assertFalse(Path(f"/proc/{int(proof.read_text())}").exists())

    def test_exit_before_all_input_requires_visible_incomplete_failure(self) -> None:
        with self.assertRaisesRegex(NativePtyError, "native_pty_input_incomplete"):
            self.capture("pass", inputs=(NativePtyInput(b"not sent\r", lambda: False),))

    def test_partial_writes_transfer_each_byte_once(self) -> None:
        proof, ready = self.root / "input", self.root / "ready"
        payload = b"fictional" * 1024
        code = (
            "import os,tty; from pathlib import Path; tty.setraw(0); "
            f"Path({str(ready)!r}).touch(); data=b''; "
            f"\nwhile len(data)<{len(payload)}: data+=os.read(0,{len(payload)}-len(data))"
            f"\nPath({str(proof)!r}).write_bytes(data)"
        )
        real_write = os.write
        with patch(
            "tests.native_pty_capture.os.write",
            side_effect=lambda fd, data: real_write(fd, data[:17]),
        ):
            result = self.capture(code, inputs=(NativePtyInput(payload, ready.exists),))
        self.assertEqual(proof.read_bytes(), payload)
        self.assertEqual(result.input_bytes, len(payload))
        self.assertTrue(result.normal_exit)

    def test_readiness_fault_closes_process_without_resending_input(self) -> None:
        proof = self.root / "pid"
        code = (
            "import os,time; from pathlib import Path; "
            f"Path({str(proof)!r}).write_text(str(os.getpid())); time.sleep(30)"
        )

        def faulty_gate() -> bool:
            if not proof.exists():
                return False
            raise RuntimeError("fictional external evidence fault")

        with self.assertRaisesRegex(NativePtyError, "native_pty_input_gate_failed"):
            self.capture(code, inputs=(NativePtyInput(b"literal\r", faulty_gate),))
        self.assert_gone(int(proof.read_text()))

    def test_invalid_bounds_refuse_before_any_process_or_terminal(self) -> None:
        for options in (
            {"timeout": 0},
            {"timeout": True},
            {"timeout": float("inf")},
            {"output_limit": 0},
            {"output_limit": True},
            {"inputs": (NativePtyInput(b"x" * 65537, lambda: True),)},
            {"inputs": (NativePtyInput(cast(bytes, "text"), lambda: True),)},
        ):
            with (
                self.subTest(options=options),
                patch("tests.native_pty_capture.subprocess.Popen") as spawn,
            ):
                with self.assertRaises(NativePtyError):
                    self.capture("pass", **options)
                spawn.assert_not_called()

    def test_delayed_independent_gate_cannot_write_after_deadline(self) -> None:
        proof, received = self.root / "ready", self.root / "received"
        code = (
            "import sys; from pathlib import Path; "
            f"Path({str(proof)!r}).touch(); data=sys.stdin.readline(); "
            f"Path({str(received)!r}).write_text(data)"
        )

        def delayed_gate() -> bool:
            if not proof.exists():
                return False
            time.sleep(0.3)
            return True

        with self.assertRaisesRegex(NativePtyError, "native_pty_timeout"):
            self.capture(code, timeout=0.15, inputs=(NativePtyInput(b"too late\r", delayed_gate),))
        self.assertFalse(received.exists())

    def test_exec_failure_keeps_nonzero_exit_and_preexec_proof_separate(self) -> None:
        result = capture_owned_pty(
            (str(self.root / "missing-native-program"),),
            self.environment,
            timeout=3,
            output_limit=1024,
        )
        self.assertTrue(result.tty_verified)
        self.assertTrue(result.normal_exit)
        self.assertEqual(result.exit_code, 126)

    def test_unrelated_inheritable_descriptor_does_not_reach_child(self) -> None:
        sentinel, proof = self.root / "fictional-private-file", self.root / "fds.json"
        sentinel.write_text("example-only")
        descriptor = os.open(sentinel, os.O_RDONLY)
        try:
            os.set_inheritable(descriptor, True)
            code = (
                "import os,json; from pathlib import Path; paths=[]; "
                "\nfor entry in Path('/proc/self/fd').iterdir():"
                "\n try: paths.append(os.readlink(entry))"
                "\n except OSError: pass"
                f"\nPath({str(proof)!r}).write_text(json.dumps(paths))"
            )
            self.capture(code)
            self.assertNotIn(str(sentinel), json.loads(proof.read_text()))
        finally:
            os.close(descriptor)

    def test_repeated_success_and_launch_failure_leave_no_parent_descriptors(self) -> None:
        before = len(tuple(Path("/proc/self/fd").iterdir()))
        for _ in range(3):
            self.capture("pass")
        with patch(
            "tests.native_pty_capture.subprocess.Popen",
            side_effect=OSError("fictional spawn fault"),
        ):
            with self.assertRaisesRegex(NativePtyError, "native_pty_launch_failed"):
                self.capture("pass")
        self.assertEqual(len(tuple(Path("/proc/self/fd").iterdir())), before)
