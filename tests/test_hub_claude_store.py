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
        at = kwargs.get("now", self.now)
        return store_mod.promote(
            self.store,
            hub_git_sha=kwargs.get("hub", HUB),
            gate=self.gate,
            post_check=self.post_check,
            now=lambda: at,
            window_open=kwargs.get("window_open", lambda: True),
        )

    def rollback(self, hub: str = HUB, at: float | None = None) -> dict[str, Any]:
        return store_mod.rollback(
            self.store,
            hub_git_sha=hub,
            gate=self.gate,
            post_check=self.post_check,
            now=self.now if at is None else at,
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
        # Crash before the post-check.
        store_mod.switch(
            self.store, identifier, hub_git_sha=HUB, now=self.now, on_revert=(candidate["sha256"],)
        )
        self.assertEqual(
            store_mod.recover(self.store, self.post_check, now=self.now), "switch_completed"
        )
        self.assertFalse(store_mod.quarantined(self.store, HUB, candidate["sha256"]))
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
        store_mod.switch(
            self.store, identifier, hub_git_sha=HUB, now=self.now, on_revert=(candidate["sha256"],)
        )
        self.post_failures.add("2.1.330")
        self.assertEqual(
            store_mod.recover(self.store, self.post_check, now=self.now), "switch_reverted"
        )
        self.assertEqual(store_mod.current_id(self.store), active["id"])
        # The journaled revert effect survives the crash.
        self.assertTrue(store_mod.quarantined(self.store, HUB, candidate["sha256"]))

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


class ReviewRegressionTests(StoreFixture):
    """Failure scenarios from the independent review of ADR 0113."""

    def test_failed_revert_stays_inspectable_on_retry(self) -> None:
        self.seed()
        self.admit("2.1.330", self.now - 2 * DAY)
        self.post_failures.update({"2.1.330", "2.1.316"})
        with self.assertRaises(store_mod.NeedsInspection):
            self.promote()
        # A retry must not treat the reverted pointer as a switch that never started.
        with self.assertRaises(store_mod.NeedsInspection):
            store_mod.recover(self.store, self.post_check, now=self.now)
        self.post_failures.clear()
        self.assertEqual(
            store_mod.recover(self.store, self.post_check, now=self.now), "switch_reverted"
        )

    def test_crash_after_rollback_rename_still_quarantines_abandoned_build(self) -> None:
        self.seed()
        promoted = self.admit("2.1.330", self.now - 2 * DAY)
        self.promote()
        original = store_mod._finish
        with patch.object(store_mod, "_finish", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.rollback(at=self.now + 60)
        self.assertFalse(store_mod.quarantined(self.store, HUB, promoted["sha256"]))
        self.assertIs(store_mod._finish, original)
        self.assertEqual(
            store_mod.recover(self.store, self.post_check, now=self.now), "switch_completed"
        )
        self.assertTrue(store_mod.quarantined(self.store, HUB, promoted["sha256"]))
        self.assertEqual(self.promote(now=self.now + DAY)["outcome"], "current")

    def test_repeated_rollback_walks_back_instead_of_oscillating(self) -> None:
        self.seed("2.1.316")
        self.admit("2.1.329", self.now - 4 * DAY)
        self.promote(now=self.now - 2 * DAY)
        self.admit("2.1.330", self.now - 2 * DAY)
        self.promote()
        self.assertEqual(self.rollback()["active"]["version"], "2.1.329")
        self.assertEqual(self.rollback()["active"]["version"], "2.1.316")
        with self.assertRaisesRegex(store_mod.StoreRefused, "no previous"):
            self.rollback()

    def test_restored_record_without_object_is_refused(self) -> None:
        active = self.seed()
        directory = store_mod.object_path(self.store, active["sha256"]).parent
        os.chmod(directory, 0o700)
        store_mod.object_path(self.store, active["sha256"]).unlink()
        with self.assertRaisesRegex(store_mod.StoreRefused, "missing"):
            store_mod.ensure_pair(self.store, self.gate, HUB, now=self.now)

    def test_corrupt_object_is_never_gated(self) -> None:
        self.seed()
        candidate = self.admit("2.1.330", self.now - 2 * DAY)
        path = store_mod.object_path(self.store, candidate["sha256"])
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o700)
        path.write_bytes(binary("6.6.6"))
        os.chmod(path, 0o555)
        os.chmod(path.parent, 0o555)
        with self.assertRaisesRegex(store_mod.StoreRefused, "corrupt"):
            self.promote()
        self.assertEqual(self.gate_calls, [])

    def test_window_closing_after_gate_skips_the_switch(self) -> None:
        active = self.seed()
        self.admit("2.1.330", self.now - 2 * DAY)
        result = self.promote(window_open=lambda: False)
        self.assertEqual((result["outcome"], result["reason"]), ("skipped", "window_closed"))
        self.assertEqual(store_mod.current_id(self.store), active["id"])

    def test_unrelated_directory_is_not_a_store(self) -> None:
        other = self.base / "other"
        (other / "objects" / ("f" * 64)).mkdir(parents=True)
        with self.assertRaisesRegex(store_mod.StoreRefused, "not a Claude CLI store"):
            store_mod.open_store(other)
        self.assertTrue((other / "objects" / ("f" * 64)).is_dir())

    def test_writable_evidence_directory_is_refused(self) -> None:
        os.chmod(self.store / "pairs", 0o777)
        with self.assertRaisesRegex(store_mod.StoreRefused, "unsafe store directory"):
            store_mod.check_store(self.store)

    def test_cached_evidence_for_another_writer_is_regated(self) -> None:
        self.seed("2.1.316")
        candidate = self.admit("2.1.330", self.now - 2 * DAY)
        other_writer = self.admit("2.1.300", self.now - 9 * DAY)
        store_mod.run_pair_gate(self.store, self.gate, HUB, candidate, other_writer, now=self.now)
        self.gate_calls.clear()
        self.promote()
        self.assertEqual(len(self.gate_calls), 1)  # resume from the actual predecessor

    def test_gate_that_cannot_run_records_no_verdict(self) -> None:
        self.seed()
        candidate = self.admit("2.1.330", self.now - 2 * DAY)

        def broken(*args: Any) -> dict[str, Any]:
            raise store_mod.StoreRefused("release gate could not run")

        with self.assertRaisesRegex(store_mod.StoreRefused, "could not run"):
            store_mod.run_pair_gate(self.store, broken, HUB, candidate, None, now=self.now)
        self.assertFalse(store_mod.quarantined(self.store, HUB, candidate["sha256"]))

    def test_redirects_are_refused_before_following(self) -> None:
        with self.assertRaisesRegex(store_mod.StoreRefused, "redirected"):
            store_mod._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com")

    def test_quarantined_candidates_age_out_of_storage(self) -> None:
        self.seed()
        bad = self.admit("2.1.330", self.now - 5 * DAY)
        self.gate_failures.add("2.1.330")
        self.promote()
        self.assertFalse(store_mod.object_path(self.store, bad["sha256"]).exists())
        self.assertTrue(store_mod.quarantined(self.store, HUB, bad["sha256"]))

    def test_status_never_initializes_a_directory(self) -> None:
        empty = self.base / "empty"
        empty.mkdir(mode=0o700)
        result = store_mod.status(empty, hub_git_sha=HUB, now=self.now)
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(list(empty.iterdir()), [])


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
            stable = hub_deploy.prepare_claude_store(installation, old, new)
            again = hub_deploy.prepare_claude_store(installation, old, new)
            self.gate_failures.add("2.1.316")
            with self.assertRaisesRegex(hub_deploy.Refused, "failed the gate"):
                hub_deploy.prepare_claude_store(installation, old, dict(new, git_sha="d" * 40))
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
                    installation, old, {"root": "/home/example/new", "git_sha": HUB}
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
            night = time.mktime((2026, 11, 25, 3, 0, 0, 0, 0, -1))
            result = hub_deploy.claude_action(
                installation, "claude-promote", active, now=lambda: night
            )
            self.assertEqual(result["outcome"], "current")
            # Rollback is recovery and runs at any time.
            with self.assertRaisesRegex(store_mod.StoreRefused, "no previous"):
                hub_deploy.claude_action(installation, "claude-rollback", active, now=lambda: noon)
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

    def test_apply_regates_a_release_for_the_cli_promoted_after_its_plan(self) -> None:
        self.seed()
        stable = str(store_mod.stable_path(self.store))
        plan_dir = self.base / "plan"
        plan_dir.mkdir(mode=0o700)
        configs = {
            "000-before": {"agents": [{"runtime": "claude", "executable": "/home/example/bin"}]},
            "000-after": {"agents": [{"runtime": "claude", "executable": stable}]},
        }
        for name, config in configs.items():
            (plan_dir / name).write_text(json.dumps(config))
            (plan_dir / name).chmod(0o600)
        plan = {
            "files": [
                {
                    "kind": "claude_config",
                    "before": {"copy": "000-before"},
                    "after": {"copy": "000-after"},
                }
            ],
            "old_runtime": {"root": "/home/example/old", "git_sha": "c" * 40},
            "new_runtime": {"root": "/home/example/new", "git_sha": OTHER_HUB},
        }
        installation = {"claude_store": str(self.store)}
        roles = ("new_runtime", "old_runtime", "rollback_runtime")
        with patch.object(store_mod, "release_gate", return_value=self.gate):
            hub_deploy._check_store_pairs(installation, plan_dir, plan, roles)
            self.assertEqual(len(self.gate_calls), 1)  # the embedded old release is skipped
            self.assertTrue(store_mod.has_pair(self.store, OTHER_HUB, self.seed_digest()))
            # A promotion between plan and apply: the new release has no evidence yet.
            plan["new_runtime"]["git_sha"] = "d" * 40
            self.gate_failures.add("2.1.316")
            with self.assertRaisesRegex(hub_deploy.Refused, "failed the gate"):
                hub_deploy._check_store_pairs(installation, plan_dir, plan, roles)

    def seed_digest(self) -> str:
        record = store_mod.current(self.store)
        assert record is not None
        return str(record["sha256"])

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
        binds = [argv[i + 1] for i, part in enumerate(argv) if part == "--ro-bind"]
        self.assertNotIn("/", binds)  # an allowlist, never the whole host
        self.assertFalse(any(b == "/mnt" or b.startswith(("/mnt/", "/run", "/var")) for b in binds))
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
