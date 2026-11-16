"""ADR 0105: which same-UID processes keep queued Codex work blocking at boot."""

from __future__ import annotations

import unittest

from hermes_codex_router.codex_boot_consumers import may_consume_codex_jobs

VENV = "/home/example/.local/share/agents-projects-hub/venv/bin"
CONFIG = "/home/example/.config/agents-projects-hub/hub.json"


def argv(*items: str) -> list[bytes]:
    return [item.encode() for item in items]


def script(*arguments: str) -> list[bytes]:
    """A console script as the kernel records it after the shebang."""
    return argv(f"{VENV}/python", f"{VENV}/agents-projects-hub", *arguments)


class CodexBootConsumerTests(unittest.TestCase):
    def assert_consumer(self, *commands: list[bytes]) -> None:
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(may_consume_codex_jobs(command))

    def assert_harmless(self, *commands: list[bytes]) -> None:
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(may_consume_codex_jobs(command))

    def test_codex_consumers_of_every_supported_launch_form(self):
        self.assert_consumer(
            script("worker", CONFIG, "--agent", "codex", "--slot", "1"),
            script("worker", CONFIG, "--agent=codex"),
            script("worker", CONFIG),  # The worker defaults to the Codex agent.
            script("controller", CONFIG),
            script("serve", CONFIG, "--agent", "codex"),
            script("serve", CONFIG, "--agent", "claude"),
            argv(f"{VENV}/hcr", "worker", CONFIG, "--agent", "codex"),
            argv(f"{VENV}/agents-projects-hub", "controller", CONFIG),
            argv(f"{VENV}/python", "-m", "hermes_codex_router", "worker", CONFIG),
            argv(f"{VENV}/python", "-m", "hermes_codex_router.cli", "controller", CONFIG),
            argv("/usr/bin/nice", f"{VENV}/agents-projects-hub", "worker", CONFIG),
        )

    def test_ambiguous_or_unknown_hub_processes_count_as_consumers(self):
        self.assert_consumer(
            script("worker", CONFIG, "--ag", "claude"),
            script("worker", CONFIG, "--agent", "claude", "--agent", "codex"),
            script("worker", CONFIG, "--agent"),
            script("worker", CONFIG, "--agent", "--slot"),
            script("worker", CONFIG, "--agent=", "--slot", "1"),
            script("worker", CONFIG, "--", "--agent", "claude"),
            script("worker-control", "inspect", "/home/example/socket"),
            script("doctor", CONFIG),
            script(),
            argv(f"{VENV}/python", "-m", "hermes_codex_router.service"),
            argv(f"{VENV}/python", "-mhermes_codex_router.cli", "worker"),
            argv(f"{VENV}/python", "-c", "import hermes_codex_router"),
            argv("/usr/bin/git", "-C", "/home/example/src/agents-projects-hub", "status"),
        )

    def test_known_non_consumers_and_unrelated_processes_are_harmless(self):
        self.assert_harmless(
            script("sender", CONFIG),
            script("monitor", CONFIG, "--notify", "--repair"),
            script("project-provisioner", CONFIG),
            script("worker", CONFIG, "--agent", "claude", "--slot", "2"),
            script("worker", CONFIG, "--slot", "1", "--agent=opencode"),
            argv(f"{VENV}/python", f"{VENV}/agents-projects-hub-prepare-codex-socket-dir"),
            argv(f"{VENV}/python", f"{VENV}/agents-projects-hub-wait-socket"),
            argv(
                f"{VENV}/python",
                "-m",
                "hermes_codex_router.codex_server_service",
                "ready",
                "--manifest",
                "/home/example/.config/agents-projects-hub/codex-server.json",
            ),
            argv("/usr/bin/bash"),
            argv("/usr/lib/systemd/systemd", "--user"),
            argv("/usr/bin/python3", "-m", "http.server"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
