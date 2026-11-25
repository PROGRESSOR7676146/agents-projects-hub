"""Offline tests for the Claude CLI store and nightly promotion (ADR 0113)."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hermes_codex_router import claude_cli_gate
from scripts import hub_claude_store as store_mod
from scripts import hub_deploy

HUB = "a" * 40
OTHER_HUB = "b" * 40
DAY = 24 * 3600


def binary(version: str) -> bytes:
    return ("#!/bin/sh\necho '%s (Claude Code)'\n" % version).encode()


class StoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.store = self.base / "store"
        self.store.mkdir(mode=0o700)
        store_mod.open_store(self.store)
        self.host = self.base / "host"
        self.host.mkdir()
        self.now = 1_800_000_000.0
        self.gate_calls: list[tuple[str, str | None]] = []
        self.gate_failures: set[str] = set()
        self.post_failures: set[str] = set()

    def _cleanup(self) -> None:
        for item in (self.base, *self.base.rglob("*")):
            if item.is_dir() and not item.is_symlink():
                os.chmod(item, 0o700)
        self.temporary.cleanup()

    def host_file(self, version: str) -> Path:
        path = self.host / version
        path.write_bytes(binary(version))
        path.chmod(0o755)
        return path

    def fetch_for(self, *, tamper: bool = False):
        def fetch(url: str) -> bytes:
            version = url.split("/")[-2]
            data = binary(version)
            digest = hashlib.sha256(data + (b"x" if tamper else b"")).hexdigest()
            entry = {"checksum": digest, "size": len(data)}
            return json.dumps({"version": version, "platforms": {"linux-x64": entry}}).encode()

        return fetch

    def gate(self, candidate: Path, version: str, previous: Path | None) -> dict[str, Any]:
        self.gate_calls.append((version, None if previous is None else previous.parent.name))
        passed = version not in self.gate_failures
        return {
            "gate_version": 1,
            "passed": passed,
            "checks": [],
            "failure": None if passed else "x",
        }

    def post_check(self, path: Path, version: str) -> bool:
        return version not in self.post_failures

    def admit(self, version: str, at: float) -> dict[str, Any]:
        return store_mod.admit(
            self.store, self.host_file(version), version, now=at, fetch=self.fetch_for()
        )

    def seed(self, version: str = "2.1.316") -> dict[str, Any]:
        receipt = store_mod.seed(
            self.store, self.host_file(version), version, "c" * 40, now=self.now - 30 * DAY
        )
        return store_mod.first_selection(
            self.store,
            receipt,
            hub_git_sha=HUB,
            post_check=self.post_check,
            now=self.now - 30 * DAY,
        )

    def promote(self, **kwargs: Any) -> dict[str, Any]:
        return store_mod.promote(
            self.store,
            hub_git_sha=kwargs.get("hub", HUB),
            gate=self.gate,
            post_check=self.post_check,
            now=kwargs.get("now", self.now),
        )


class AdmissionTests(StoreFixture):
    def test_matching_candidate_is_stored_once_read_only(self) -> None:
        receipt = self.admit("2.1.330", self.now)
        again = self.admit("2.1.330", self.now + 100)
        self.assertEqual(receipt, again)  # the admission time does not restart
        target = store_mod.object_path(self.store, receipt["sha256"])
        self.assertEqual(target.read_bytes(), binary("2.1.330"))
        self.assertEqual(target.stat().st_mode & 0o777, 0o555)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o555)
        self.assertEqual(receipt["source"], "publisher-manifest")

    def test_digest_mismatch_refuses_and_stores_nothing(self) -> None:
        with self.assertRaisesRegex(store_mod.StoreRefused, "does not match manifest"):
            store_mod.admit(
                self.store,
                self.host_file("2.1.330"),
                "2.1.330",
                now=self.now,
                fetch=self.fetch_for(tamper=True),
            )
        self.assertEqual(list((self.store / "objects").iterdir()), [])
        self.assertEqual(list((self.store / "candidates").iterdir()), [])

    def test_unavailable_manifest_refuses(self) -> None:
        def fail(url: str) -> bytes:
            raise OSError("offline")

        with self.assertRaisesRegex(store_mod.StoreRefused, "manifest unavailable"):
            store_mod.admit(self.store, self.host_file("2.1.330"), "2.1.330", now=1, fetch=fail)

    def test_other_origins_are_refused(self) -> None:
        with self.assertRaisesRegex(store_mod.StoreRefused, "origin"):
            store_mod.fetch_url("https://example.com/claude-code-releases/1.0.0/manifest.json")


class PromotionTests(StoreFixture):
    def test_candidate_waits_one_day_with_jitter_allowance(self) -> None:
        self.seed()
        self.admit("2.1.330", self.now)
        result = self.promote(now=self.now + DAY - 2 * 3600)
        self.assertEqual(result["outcome"], "skipped")
        self.assertEqual(result["next_version"], "2.1.330")
        self.assertEqual(self.gate_calls, [])
        # Admitted last night a few minutes later than tonight's run: still eligible.
        result = self.promote(now=self.now + DAY - 10 * 60)
        self.assertEqual(result["outcome"], "promoted")
        self.assertEqual(result["active"]["version"], "2.1.330")
        self.assertEqual(result["previous"]["version"], "2.1.316")

    def test_newest_soaked_candidate_wins_over_fresher_ones(self) -> None:
        self.seed()
        self.admit("2.1.329", self.now - 3 * DAY)
        self.admit("2.1.330", self.now - 2 * DAY)
        self.admit("2.1.331", self.now - 3600)  # tonight's: still soaking
        result = self.promote()
        self.assertEqual(result["outcome"], "promoted")
        self.assertEqual(result["active"]["version"], "2.1.330")
        # The gate resumed a session written by the previously active CLI.
        self.assertEqual(self.gate_calls[0][0], "2.1.330")
        self.assertIsNotNone(self.gate_calls[0][1])
        self.assertTrue(store_mod.has_pair(self.store, HUB, result["active"]["sha256"]))
        self.assertEqual(
            os.path.realpath(store_mod.stable_path(self.store)),
            str(store_mod.object_path(self.store, result["active"]["sha256"])),
        )

    def test_gate_failure_quarantines_and_falls_back_to_older_candidate(self) -> None:
        self.seed()
        self.admit("2.1.329", self.now - 3 * DAY)
        bad = self.admit("2.1.330", self.now - 2 * DAY)
        self.gate_failures.add("2.1.330")
        result = self.promote()
        self.assertEqual(result["outcome"], "promoted")
        self.assertEqual(result["active"]["version"], "2.1.329")
        self.assertEqual(result["rejected"], [{"version": "2.1.330", "failure": "x"}])
        self.assertTrue(store_mod.quarantined(self.store, HUB, bad["sha256"]))
        self.gate_calls.clear()
        self.assertEqual(self.promote()["outcome"], "current")
        self.assertEqual(self.gate_calls, [])  # a failed pair is not retried nightly
        # Another Hub release gets its own verdict.
        self.assertFalse(store_mod.quarantined(self.store, OTHER_HUB, bad["sha256"]))

    def test_only_failing_candidate_reports_gate_failure_without_switch(self) -> None:
        active = self.seed()
        self.admit("2.1.330", self.now - 2 * DAY)
        self.gate_failures.add("2.1.330")
        result = self.promote()
        self.assertEqual(result["outcome"], "gate_failed")
        self.assertEqual(store_mod.EXIT[result["outcome"]], 1)
        self.assertEqual(store_mod.current_id(self.store), active["id"])

    def test_failed_post_check_restores_and_rechecks_previous(self) -> None:
        active = self.seed()
        candidate = self.admit("2.1.330", self.now - 2 * DAY)
        self.post_failures.add("2.1.330")
        result = self.promote()
        self.assertEqual(result["outcome"], "failed_rolled_back")
        self.assertEqual(store_mod.current_id(self.store), active["id"])
        self.assertFalse((self.store / "pending.json").exists())
        self.assertTrue(store_mod.quarantined(self.store, HUB, candidate["sha256"]))

    def test_both_sides_failing_needs_inspection_and_keeps_pending(self) -> None:
        self.seed()
        self.admit("2.1.330", self.now - 2 * DAY)
        self.post_failures.update({"2.1.330", "2.1.316"})
        with self.assertRaises(store_mod.NeedsInspection):
            self.promote()
        self.assertTrue((self.store / "pending.json").exists())

    def test_no_newer_candidate_is_current(self) -> None:
        self.seed("2.1.330")
        self.admit("2.1.329", self.now - 5 * DAY)
        self.assertEqual(self.promote()["outcome"], "current")

    def test_unseeded_store_refuses(self) -> None:
        with self.assertRaisesRegex(store_mod.StoreRefused, "no active selection"):
            self.promote()


class RecoveryAndRollbackTests(StoreFixture):
    def test_unfinished_switch_is_completed_when_target_checks(self) -> None:
        active = self.seed()
        candidate = self.admit("2.1.330", self.now - 2 * DAY)
        identifier = store_mod.write_selection(
            self.store,
            candidate,
            previous=active["id"],
            reason="promote",
            hub_git_sha=HUB,
            now=self.now,
        )
        store_mod.switch(self.store, identifier, now=self.now)  # crash before the post-check
        self.assertEqual(store_mod.recover(self.store, self.post_check), "switch_completed")
        self.assertEqual(store_mod.current_id(self.store), identifier)
        self.assertFalse((self.store / "pending.json").exists())

    def test_unfinished_switch_is_reverted_when_target_fails(self) -> None:
        active = self.seed()
        candidate = self.admit("2.1.330", self.now - 2 * DAY)
        identifier = store_mod.write_selection(
            self.store,
            candidate,
            previous=active["id"],
            reason="promote",
            hub_git_sha=HUB,
            now=self.now,
        )
        store_mod.switch(self.store, identifier, now=self.now)
        self.post_failures.add("2.1.330")
        self.assertEqual(store_mod.recover(self.store, self.post_check), "switch_reverted")
        self.assertEqual(store_mod.current_id(self.store), active["id"])

    def test_rollback_returns_previous_and_blocks_re_promotion(self) -> None:
        self.seed()
        self.admit("2.1.330", self.now - 2 * DAY)
        promoted = self.promote()["active"]
        result = store_mod.rollback(
            self.store,
            hub_git_sha=HUB,
            gate=self.gate,
            post_check=self.post_check,
            now=self.now + 60,
        )
        self.assertEqual(result["outcome"], "rolled_back")
        self.assertEqual(result["active"]["version"], "2.1.316")
        self.assertTrue(store_mod.quarantined(self.store, HUB, promoted["sha256"]))
        self.assertEqual(self.promote(now=self.now + DAY)["outcome"], "current")

    def test_rollback_without_previous_refuses(self) -> None:
        self.seed()
        with self.assertRaisesRegex(store_mod.StoreRefused, "no previous"):
            store_mod.rollback(
                self.store,
                hub_git_sha=HUB,
                gate=self.gate,
                post_check=self.post_check,
                now=self.now,
            )

    def test_rollback_gates_previous_for_a_release_without_evidence(self) -> None:
        self.seed()
        self.admit("2.1.330", self.now - 2 * DAY)
        self.promote()
        store_mod.ensure_pair(self.store, self.gate, OTHER_HUB, now=self.now)
        self.gate_failures.add("2.1.316")
        with self.assertRaisesRegex(store_mod.StoreRefused, "fails the gate"):
            store_mod.rollback(
                self.store,
                hub_git_sha=OTHER_HUB,
                gate=self.gate,
                post_check=self.post_check,
                now=self.now,
            )


class PairAndPruneTests(StoreFixture):
    def test_ensure_pair_runs_gate_once_per_release(self) -> None:
        self.seed()
        store_mod.ensure_pair(self.store, self.gate, OTHER_HUB, now=self.now)
        store_mod.ensure_pair(self.store, self.gate, OTHER_HUB, now=self.now)
        self.assertEqual(len(self.gate_calls), 1)
        self.gate_failures.add("2.1.316")
        with self.assertRaisesRegex(store_mod.StoreRefused, "failed the gate"):
            store_mod.ensure_pair(self.store, self.gate, "d" * 40, now=self.now)

    def test_prune_keeps_active_previous_and_fresh_candidates(self) -> None:
        self.seed("2.1.300")
        versions = ["2.1.301", "2.1.302", "2.1.303", "2.1.304", "2.1.305", "2.1.306"]
        clock = self.now - 20 * DAY
        for version in versions:
            self.admit(version, clock - 2 * DAY)
            self.promote(now=clock)
            clock += DAY
        fresh = self.admit("2.1.307", clock - 3600)
        store_mod.prune(self.store, now=clock)
        stored = {
            json.loads(p.read_text())["version"] for p in (self.store / "candidates").glob("*.json")
        }
        self.assertIn("2.1.306", stored)  # active
        self.assertIn("2.1.305", stored)  # previous
        self.assertIn("2.1.307", stored)  # still soaking
        self.assertNotIn("2.1.300", stored)
        self.assertNotIn("2.1.301", stored)
        self.assertTrue(store_mod.object_path(self.store, fresh["sha256"]).is_file())
        status = store_mod.status(self.store, hub_git_sha=HUB, now=clock)
        self.assertEqual(status["active"]["version"], "2.1.306")
        self.assertEqual(status["previous"]["version"], "2.1.305")
        self.assertTrue(status["pair_verified"])
        self.assertEqual(
            {c["version"]: c["state"] for c in status["candidates"]}["2.1.307"], "soaking"
        )


class WindowTests(unittest.TestCase):
    def test_windows_including_midnight(self) -> None:
        def at(hour: int, minute: int) -> float:
            return time.mktime((2026, 11, 25, hour, minute, 0, 0, 0, -1))

        self.assertTrue(store_mod.in_window("02:00-05:30", at(3, 0)))
        self.assertFalse(store_mod.in_window("02:00-05:30", at(5, 30)))
        self.assertTrue(store_mod.in_window("23:00-02:00", at(0, 30)))
        self.assertFalse(store_mod.in_window("23:00-02:00", at(12, 0)))
        with self.assertRaises(store_mod.StoreRefused):
            store_mod.in_window("25:00-02:00", at(0, 0))


class DeployIntegrationTests(StoreFixture):
    def test_stable_path_replaces_the_release_executable(self) -> None:
        config = {
            "agents": [{"runtime": "claude", "executable": "/home/example/old/bin/claude"}],
            "claude_file_permissions": {
                "profile": "host",
                "provider_home": "/home/example/claude-host-sessions",
            },
        }
        stable = str(store_mod.stable_path(self.store))
        result = hub_deploy.rewrite_claude_config(
            config, "/home/example/old", "/home/example/new", "e" * 64, claude_executable=stable
        )
        self.assertEqual(result["agents"][0]["executable"], stable)

    def test_installation_requires_one_complete_claude_mode(self) -> None:
        fields = hub_deploy._INPUT_FIELDS
        store_fields = hub_deploy._STORE_INPUT_FIELDS
        self.assertNotIn("native_claude", store_fields)
        self.assertEqual(store_fields - fields, {"claude_store", "claude_window"})

    def test_seed_from_active_release_and_gate_new_release(self) -> None:
        old_root = self.base / "releases" / "old"
        (old_root / "bin").mkdir(parents=True)
        embedded = old_root / "bin/claude"
        embedded.write_bytes(binary("2.1.316"))
        embedded.chmod(0o755)
        installation = {"claude_store": str(self.store)}
        old = {"root": str(old_root), "git_sha": "c" * 40, "claude_executable": str(embedded)}
        new = {"root": str(self.base / "releases/new"), "git_sha": HUB}
        with (
            patch.object(store_mod, "release_gate", return_value=self.gate),
            patch.object(store_mod, "release_post_check", return_value=self.post_check),
        ):
            stable = hub_deploy.prepare_claude_store(installation, old, new, None)
            again = hub_deploy.prepare_claude_store(installation, old, new, None)
            self.gate_failures.add("2.1.316")
            with self.assertRaisesRegex(hub_deploy.Refused, "failed the gate"):
                hub_deploy.prepare_claude_store(
                    installation, old, dict(new, git_sha="d" * 40), None
                )
        self.assertEqual(stable, again)
        self.assertEqual(Path(stable).read_bytes(), binary("2.1.316"))
        # Seeded pair once, nothing for the repeated deploy, once for the other release.
        self.assertEqual(len(self.gate_calls), 2)
        record = store_mod.current(self.store)
        assert record is not None
        self.assertEqual((record["reason"], record["version"]), ("seed", "2.1.316"))

    def test_seed_refuses_a_release_without_embedded_cli(self) -> None:
        installation = {"claude_store": str(self.store)}
        old = {
            "root": "/home/example/old",
            "git_sha": "c" * 40,
            "claude_executable": "/usr/bin/claude",
        }
        with (
            patch.object(store_mod, "release_gate", return_value=self.gate),
            patch.object(store_mod, "release_post_check", return_value=self.post_check),
        ):
            with self.assertRaisesRegex(hub_deploy.Refused, "embedded Claude CLI"):
                hub_deploy.prepare_claude_store(
                    installation, old, {"root": "/home/example/new", "git_sha": HUB}, None
                )

    def test_promotion_refuses_outside_window_and_maps_exit_codes(self) -> None:
        self.seed()
        installation = {"claude_store": str(self.store), "claude_window": "02:00-05:30"}
        active = {
            "root": "/home/example/release",
            "git_sha": HUB,
            "claude_executable": str(store_mod.stable_path(self.store)),
        }
        noon = time.mktime((2026, 11, 25, 12, 0, 0, 0, 0, -1))
        with (
            patch.object(store_mod, "release_gate", return_value=self.gate),
            patch.object(store_mod, "release_post_check", return_value=self.post_check),
        ):
            with self.assertRaisesRegex(hub_deploy.Refused, "outside the nightly"):
                hub_deploy.claude_action(installation, "claude-promote", active, now=lambda: noon)
            result = hub_deploy.claude_action(
                installation, "claude-promote", active, now=lambda: noon, outside_window=True
            )
            self.assertEqual(result["outcome"], "current")
            legacy = dict(active, claude_executable="/home/example/release/bin/claude")
            with self.assertRaisesRegex(hub_deploy.Refused, "does not run the Claude CLI store"):
                hub_deploy.claude_action(installation, "claude-rollback", legacy)
        self.assertEqual(
            {
                k: store_mod.EXIT[k]
                for k in ("promoted", "failed_rolled_back", "needs_inspection", "refused")
            },
            {"promoted": 0, "failed_rolled_back": 1, "needs_inspection": 2, "refused": 3},
        )

    def test_command_refuses_legacy_installation_with_exit_three(self) -> None:
        legacy = self.base / "installation.json"
        legacy.write_text(json.dumps({"schema_version": 1}))
        legacy.chmod(0o600)
        output = io.StringIO()
        with redirect_stdout(output):
            code = hub_deploy.main(["claude-status", "--installation", str(legacy)])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["outcome"], "refused")


class GateArgvTests(unittest.TestCase):
    def test_namespace_hides_home_and_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "claude"
            candidate.write_bytes(b"#!/bin/sh\n")
            candidate.chmod(0o755)
            with patch.object(claude_cli_gate, "_bwrap", return_value=Path("/usr/bin/bwrap")):
                argv = claude_cli_gate.gate_argv(candidate, None, "2.1.330")
        self.assertIn("--unshare-all", argv)
        self.assertIn("--clearenv", argv)
        home = str(Path.home())
        self.assertEqual(argv[argv.index("--tmpfs", argv.index("/tmp")) + 1], home)
        self.assertIn(claude_cli_gate.CANDIDATE, argv)
        self.assertNotIn("--with-previous", argv)

    def test_invalid_version_is_refused_before_launch(self) -> None:
        with self.assertRaises(claude_cli_gate.ClaudeCliGateError):
            claude_cli_gate.run_gate(Path("/nonexistent"), "latest")


@unittest.skipUnless(
    os.environ.get("HUB_NATIVE_CLAUDE") and os.environ.get("HUB_NATIVE_CLAUDE_VERSION"),
    "set HUB_NATIVE_CLAUDE and HUB_NATIVE_CLAUDE_VERSION to run the real offline gate",
)
class NativeGateTests(unittest.TestCase):
    def test_real_cli_passes_the_offline_gate(self) -> None:
        report = claude_cli_gate.run_gate(
            Path(os.environ["HUB_NATIVE_CLAUDE"]), os.environ["HUB_NATIVE_CLAUDE_VERSION"]
        )
        self.assertTrue(report["passed"], copy.deepcopy(report))


if __name__ == "__main__":
    unittest.main()
