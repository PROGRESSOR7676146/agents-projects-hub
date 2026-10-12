from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex_router.claude_cli_capabilities import (
    ClaudeCliCapabilities,
    ClaudeCliCapabilityError,
)

OPTIONS = (
    "--print",
    "--output-format",
    "--verbose",
    "--restricted",
    "--safe-mode",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--settings",
    "--permission-mode",
    "--permission-prompts",
    "--tools",
    "--session-id",
    "--resume",
    "--model",
    "--effort",
)
HELP = "Usage: claude [options]\nOptions:\n" + "".join(
    f"  {option}  example option\n" for option in OPTIONS
).replace(
    "  --permission-mode  example option\n",
    "  --permission-mode <mode>  Permission mode\n      Values: default, dontAsk, plan\n",
).replace(
    "  --permission-prompts  example option\n",
    "  --permission-prompts <mode>  Prompt host mode\n      Values: host, none\n",
)


class LeaderExitClock:
    """Expire after atomic PID publication and observed leader exit, with a watchdog."""

    def __init__(self, marker: Path) -> None:
        self.marker = marker
        self.started = time.monotonic()
        self.observed_leader_exit = False

    def monotonic(self) -> float:
        if self.marker.exists():
            leader = int(self.marker.read_text().split()[1])
            try:
                self.observed_leader_exit = (
                    Path(f"/proc/{leader}/stat").read_text().split()[2] == "Z"
                )
            except (FileNotFoundError, ProcessLookupError):
                self.observed_leader_exit = True
        return 1.0 if self.observed_leader_exit or time.monotonic() - self.started >= 10 else 0.0


class ClaudeCliCapabilitiesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = self.root / "fictional-claude"
        self.environment = {"PATH": str(self.root)}
        self.capabilities = ClaudeCliCapabilities()

    def make_cli(self, source: str) -> None:
        self.cli.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        self.cli.chmod(0o700)

    def help_cli(self, help_text: str = HELP, *, exit_code: int = 0) -> None:
        self.make_cli(
            "import os, sys\n"
            "assert sys.argv == [sys.argv[0], '--help']\n"
            f"os.write(1, {help_text.encode()!r})\n"
            f"sys.exit({exit_code})\n"
        )

    def require(self, executable: str | None = None) -> str:
        return self.capabilities.require(
            executable or str(self.cli),
            cwd=self.root,
            environment=self.environment,
            interrupted=threading.Event(),
        )

    def test_accepts_full_multiline_help_and_resolves_path_from_supplied_environment(self) -> None:
        self.help_cli()
        self.assertEqual(self.require(self.cli.name), str(self.cli))

    def test_relative_executable_and_path_never_run_project_code_for_help(self) -> None:
        self.help_cli()
        for executable in ("./fictional-claude", "fictional-claude"):
            with self.subTest(executable=executable):
                self.environment["PATH"] = "."
                with (
                    patch("subprocess.Popen") as child,
                    self.assertRaises(ClaudeCliCapabilityError),
                ):
                    self.require(executable)
                child.assert_not_called()

    def test_missing_option_and_prose_mention_fail(self) -> None:
        for option in OPTIONS:
            with self.subTest(option=option):
                help_text = HELP.replace(f"  {option}  example option\n", "")
                if option == "--permission-mode":
                    help_text = HELP.replace(
                        "  --permission-mode <mode>  Permission mode\n"
                        "      Values: default, dontAsk, plan\n",
                        "",
                    )
                if option == "--permission-prompts":
                    help_text = HELP.replace(
                        "  --permission-prompts <mode>  Prompt host mode\n"
                        "      Values: host, none\n",
                        "",
                    )
                self.help_cli(help_text + f"The {option} switch may exist elsewhere.\n")
                with self.assertRaises(ClaudeCliCapabilityError):
                    self.require()

    def test_indented_prose_mention_is_not_an_option_row(self) -> None:
        self.help_cli(
            HELP.replace("  --tools  example option\n", "")
            + "Notes:\n    --tools  may be available in another mode.\n"
        )
        with self.assertRaises(ClaudeCliCapabilityError):
            self.require()

    def test_none_and_dontask_must_be_in_their_own_option_stanzas(self) -> None:
        for old, new in (("host, none", "host"), ("default, dontAsk, plan", "default, plan")):
            with self.subTest(old=old):
                self.help_cli(HELP.replace(old, new) + f"Some other option has {old}.\n")
                with self.assertRaises(ClaudeCliCapabilityError):
                    self.require()

    def test_failed_process_and_missing_executable_have_safe_error(self) -> None:
        self.help_cli(exit_code=7)
        with self.assertRaises(ClaudeCliCapabilityError) as raised:
            self.require()
        self.assertEqual(str(raised.exception), "Claude CLI capabilities could not be verified.")
        self.cli.unlink()
        with self.assertRaises(ClaudeCliCapabilityError):
            self.require()

    def test_stdout_and_stderr_flood_fail_before_eof_and_reap(self) -> None:
        for stream in (1, 2):
            with self.subTest(stream=stream):
                self.make_cli(
                    "import os, time\n"
                    f"for _ in range(100): os.write({stream}, b'private' * 4096)\n"
                    "time.sleep(30)\n"
                )
                spawned: list[subprocess.Popen[str]] = []
                real_popen = subprocess.Popen

                def spawn(*args: object, **kwargs: object) -> subprocess.Popen[str]:
                    child = real_popen(*args, **kwargs)  # type: ignore[arg-type]
                    spawned.append(child)
                    return child

                with patch("subprocess.Popen", side_effect=spawn):
                    with self.assertRaises(ClaudeCliCapabilityError) as raised:
                        self.require()
                self.assertNotIn("private", str(raised.exception))
                self.assertEqual(len(spawned), 1)
                self.assertIsNotNone(spawned[0].poll())
                self.assertTrue(spawned[0].stdout and spawned[0].stdout.closed)
                self.assertTrue(spawned[0].stderr and spawned[0].stderr.closed)

    def test_timeout_is_bounded(self) -> None:
        self.make_cli("import time\ntime.sleep(30)\n")
        with patch("hermes_codex_router.claude_cli_capabilities._PROBE_TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(ClaudeCliCapabilityError):
                self.require()

    def test_preexisting_and_mid_probe_interrupt(self) -> None:
        self.help_cli()
        interrupted = threading.Event()
        interrupted.set()
        with patch("subprocess.Popen", side_effect=AssertionError("must not spawn")):
            with self.assertRaises(ClaudeCliCapabilityError):
                self.capabilities.require(
                    str(self.cli),
                    cwd=self.root,
                    environment=self.environment,
                    interrupted=interrupted,
                )
        marker = self.root / "started"
        self.make_cli(
            f"import pathlib, time\npathlib.Path({str(marker)!r}).touch()\ntime.sleep(30)\n"
        )

        def interrupt_when_started() -> None:
            until = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < until:
                time.sleep(0.005)
            interrupted.set()

        interrupted.clear()
        thread = threading.Thread(target=interrupt_when_started)
        thread.start()
        try:
            with self.assertRaises(ClaudeCliCapabilityError):
                self.capabilities.require(
                    str(self.cli),
                    cwd=self.root,
                    environment=self.environment,
                    interrupted=interrupted,
                )
        finally:
            thread.join(timeout=10)
        self.assertTrue(marker.exists())

    def test_timeout_kills_descendant_even_after_leader_exits(self) -> None:
        pid_file = self.root / "descendant-pid"
        self.make_cli(
            "import os, pathlib, subprocess, sys\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"marker = pathlib.Path({str(pid_file)!r})\n"
            "ready = marker.with_suffix('.ready')\n"
            "ready.write_text(f'{child.pid} {os.getpid()}')\n"
            "ready.replace(marker)\n"
        )
        clock = LeaderExitClock(pid_file)
        with (
            patch("hermes_codex_router.claude_cli_capabilities._PROBE_TIMEOUT_SECONDS", 0.2),
            patch(
                "hermes_codex_router.claude_cli_capabilities.time",
                SimpleNamespace(monotonic=clock.monotonic),
            ),
        ):
            with self.assertRaises(ClaudeCliCapabilityError):
                self.require()
        self.assertTrue(clock.observed_leader_exit)
        pid = int(pid_file.read_text().split()[0])
        until = time.monotonic() + 1
        while time.monotonic() < until:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split()[2]
            except (FileNotFoundError, ProcessLookupError):
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            os.kill(pid, 9)
            self.fail("descendant survived preflight cleanup")

    def test_cache_success_then_invalidate_after_file_replacement(self) -> None:
        count = self.root / "count"
        source = (
            "import pathlib, sys\n"
            f"p = pathlib.Path({str(count)!r})\n"
            "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
            f"sys.stdout.write({HELP!r})\n"
        )
        self.make_cli(source)
        self.require()
        self.require()
        self.assertEqual(count.read_text(), "x")
        replacement = self.root / "replacement"
        replacement.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        replacement.chmod(0o700)
        replacement.replace(self.cli)
        self.require()
        self.assertEqual(count.read_text(), "xx")

    def test_executable_change_during_probe_is_rejected_and_not_cached(self) -> None:
        self.make_cli(
            "import pathlib, sys\n"
            "p = pathlib.Path(__file__)\n"
            "p.write_text(p.read_text() + '#changed\\n')\n"
            f"sys.stdout.write({HELP!r})\n"
        )
        with self.assertRaises(ClaudeCliCapabilityError):
            self.require()
        self.help_cli()
        self.assertEqual(self.require(), str(self.cli))


if __name__ == "__main__":
    unittest.main()
