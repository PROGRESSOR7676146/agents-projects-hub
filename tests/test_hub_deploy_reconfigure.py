"""Hub config amendments on an accepted deployment window (`hub_deploy reconfigure`)."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts import hub_deploy
from tests.test_hub_deploy import WindowFixture


class Interrupted(BaseException):
    """A process loss between two journaled effects."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReconfigureTests(WindowFixture):
    def setUp(self):
        super().setUp()
        self.original = self.hub.read_bytes()
        # Amendments keep the mode the window recorded, whatever the umask made it.
        self.mode = self.hub.stat().st_mode & 0o777
        self.candidate = self.base / "private" / "candidate.json"
        self.candidate.parent.mkdir(mode=0o700)

    def accept(self) -> str:
        result = self.execute()
        self.assertEqual(result["outcome"], "ACCEPTED", result)
        return result["id"]

    def write_candidate(self, **changes: Any) -> Path:
        config = json.loads(self.hub.read_bytes())
        config.update(changes)
        self.candidate.write_text(json.dumps(config, indent=2))
        self.candidate.chmod(0o600)
        return self.candidate

    def reconfigure(self, candidate: Path | None, fault=None, runner=None) -> dict[str, Any]:
        return hub_deploy.reconfigure(
            self.install,
            self.manager,
            runner or self.window_runner,
            self.sleeps.append,
            candidate=candidate,
            load=lambda: 0,
            fault=fault,
        )

    def rollback(self, window: str, **kwargs: Any) -> dict[str, Any]:
        return hub_deploy.rollback_window(
            self.install, window, self.manager, self.window_runner, self.sleeps.append, **kwargs
        )

    def stops(self) -> int:
        return sum(1 for call in self.manager.calls if call[0] == "stop")

    def amendment(self, window: str, index: int = 1) -> Path:
        return self.ops / window / f"amendment-{index:03d}"

    def test_change_restarts_services_and_binds_a_new_manifest(self):
        window = self.accept()
        candidate = self.write_candidate(topic_panel={"enabled": True})
        stops = self.stops()
        result = self.reconfigure(candidate)
        self.assertEqual(result["outcome"], "ACCEPTED", result)
        self.assertEqual(self.hub.read_bytes(), candidate.read_bytes())
        self.assertEqual(self.hub.stat().st_mode & 0o777, self.mode)
        self.assertEqual(result["changed_keys"], ["topic_panel"])
        self.assertFalse(result["adopted"])
        self.assertEqual(result["config_sha256"], digest(candidate.read_bytes()))
        manifest = self.amendment(window) / "release-manifest.json"
        self.assertEqual(result["manifest"], str(manifest))
        self.assertEqual(result["manifest_sha256"], digest(manifest.read_bytes()))
        # The window manifest stays; the new one binds the same wheels and backup.
        self.assertTrue((self.ops / window / "release-manifest.json").exists())
        create = next(argv for argv in self.calls if "create" in argv and str(manifest) in argv)
        plan = hub_deploy.verify_saved_plan(self.plan, check_live=False)
        self.assertEqual(create[create.index("--active-artifact") + 1], plan["new_wheel"])
        self.assertEqual(create[create.index("--rollback-artifact") + 1], plan["old_wheel"])
        self.assertEqual(create[create.index("--config") + 1], str(self.hub))
        self.assertEqual(create[create.index("--backup") + 1], str(self.ops / window / "state.db"))
        verify = next(argv for argv in self.calls if "verify" in argv and str(manifest) in argv)
        self.assertEqual(verify[-2:], ["--state", str(self.db)])
        # Every restart-order service was stopped and started again on the same runtime.
        self.assertEqual(self.stops() - stops, 4)
        starts = [call[1] for call in self.manager.calls if call[0] == "start"]
        self.assertEqual(
            starts[-4:],
            ["worker.service", "controller.service", "monitor.service", "monitor.timer"],
        )
        status = self.require_status()
        self.assertEqual(
            status["amendments"],
            [{"id": "amendment-001", "state": "ACCEPTED", "changed_keys": ["topic_panel"]}],
        )
        self.assertEqual(status["manifest"], str(manifest))
        # The window plan and its own evidence are untouched.
        hub_deploy.verify_saved_plan(self.plan, check_live=False)
        self.assertEqual(self.require_window()["state"], "ACCEPTED")

    def test_chained_amendments_and_status_without_amendments(self):
        window = self.accept()
        status = self.require_status()
        self.assertEqual(status["amendments"], [])
        self.assertEqual(status["manifest"], str(self.ops / window / "release-manifest.json"))
        self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        result = self.reconfigure(self.write_candidate(message_batch_quiet_ms=900))
        self.assertEqual(result["outcome"], "ACCEPTED", result)
        self.assertEqual(result["amendment"], "amendment-002")
        self.assertEqual(json.loads(self.hub.read_bytes())["message_batch_quiet_ms"], 900)
        self.assertEqual(json.loads(self.hub.read_bytes())["topic_panel"], {"enabled": True})
        self.assertEqual(self.require_status()["manifest"], result["manifest"])

    def test_keys_outside_the_allowlist_are_refused_before_any_effect(self):
        self.accept()
        stops = self.stops()
        config = json.loads(self.hub.read_bytes())
        agents = config["agents"] + [{"runtime": "codex", "agent_id": "codex"}]
        for changes in (
            {"owner_user_ids": [1]},
            {"agents": agents},
            {"claude_stream_input": True},
            {"topic_panel": {"enabled": True}, "codex_worker_count": 2},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(hub_deploy.Refused, "not reconfigurable"):
                    self.reconfigure(self.write_candidate(**changes))
        self.assertEqual(self.stops(), stops)
        self.assertEqual(self.hub.read_bytes(), self.original)
        self.assertEqual(list((self.ops / self.require_window()["id"]).glob("*amendment*")), [])

    def test_controller_and_sender_settings_are_reconfigurable(self):
        # Workers read these from the database projection, never their own copy.
        self.assertLessEqual({"agent_mail", "session_handoff"}, hub_deploy.RECONFIGURABLE_KEYS)

    def test_unsafe_or_empty_candidates_are_refused(self):
        self.accept()
        with self.assertRaisesRegex(hub_deploy.Refused, "no Hub config change"):
            self.reconfigure(self.write_candidate())
        candidate = self.write_candidate(topic_panel={"enabled": True})
        candidate.chmod(0o644)
        with self.assertRaisesRegex(hub_deploy.Refused, "unsafe private"):
            self.reconfigure(candidate)
        candidate.write_text('{"topic_panel": {}, "topic_panel": {"enabled": true}}')
        candidate.chmod(0o600)
        with self.assertRaisesRegex(hub_deploy.Refused, "duplicate Hub config key"):
            self.reconfigure(candidate)
        with self.assertRaisesRegex(hub_deploy.Refused, "must be absolute"):
            self.reconfigure(Path("candidate.json"))

        def invalid(argv, **kwargs):
            if "validate-hub" in argv:
                return SimpleNamespace(stdout=json.dumps({"ok": False}))
            return self.window_runner(argv, **kwargs)

        with self.assertRaisesRegex(hub_deploy.Refused, "validation failed"):
            self.reconfigure(self.write_candidate(topic_panel={"enabled": True}), runner=invalid)
        self.assertEqual(self.hub.read_bytes(), self.original)
        self.assertEqual(list((self.ops / self.require_window()["id"]).glob("*amendment*")), [])

    def test_busy_installation_is_refused_before_stopping(self):
        self.accept()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("INSERT INTO provider_jobs VALUES ('codex', 'queued')")
            db.commit()
        stops = self.stops()
        with self.assertRaisesRegex(hub_deploy.Refused, "provider jobs busy"):
            self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.assertEqual(self.stops(), stops)

    def test_config_change_values_compare_exactly(self):
        self.assertEqual(
            hub_deploy.config_changes(b'{"topic_panel": 1}', b'{"topic_panel": true}'),
            ["topic_panel"],
        )
        self.assertEqual(
            hub_deploy.config_changes(b"{}", b'{"topic_panel": {"enabled": true}}'),
            ["topic_panel"],
        )
        with self.assertRaisesRegex(hub_deploy.Refused, "not reconfigurable: registry_path"):
            hub_deploy.config_changes(b'{"registry_path": "/a"}', b"{}")
        with self.assertRaisesRegex(hub_deploy.Refused, "no Hub config change"):
            hub_deploy.config_changes(b'{"a": [1, 2]}', b'{\n  "a": [1, 2]\n}')
        with self.assertRaisesRegex(hub_deploy.Refused, "must be an object"):
            hub_deploy.config_changes(b"[]", b"{}")
        # Digits beyond float precision and int/float types are real changes.
        changes = (
            b'{"topic_panel": true, "x": 1.00000000000000001}',
            b'{"topic_panel": false, "x": 1.00000000000000002}',
        )
        with self.assertRaisesRegex(hub_deploy.Refused, "not reconfigurable: x"):
            hub_deploy.config_changes(*changes)
        with self.assertRaisesRegex(hub_deploy.Refused, "not reconfigurable: x"):
            hub_deploy.config_changes(b'{"x": 1}', b'{"x": 1.0}')
        with self.assertRaisesRegex(hub_deploy.Refused, "non-JSON number"):
            hub_deploy.config_changes(b"{}", b'{"topic_panel": NaN}')
        with self.assertRaises(hub_deploy.Refused):
            hub_deploy.config_changes(b"{}", b'{"operational_alerts": {"project_id": "x"}}')

    def test_requires_an_accepted_newest_window(self):
        with self.assertRaisesRegex(hub_deploy.Refused, "not accepted"):
            self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        window = self.accept()
        self.assertEqual(self.rollback(window)["outcome"], "ROLLED_BACK")
        with self.assertRaisesRegex(hub_deploy.Refused, "not accepted"):
            self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))

    def test_out_of_band_edit_is_refused_then_adopted(self):
        window = self.accept()
        config = json.loads(self.original)
        config["topic_panel"] = {"enabled": True}
        edited = json.dumps(config).encode()
        self.hub.write_bytes(edited)
        with self.assertRaisesRegex(hub_deploy.Refused, "--adopt"):
            self.reconfigure(self.write_candidate(message_batch_quiet_ms=900))
        result = self.reconfigure(None)
        self.assertEqual(result["outcome"], "ACCEPTED", result)
        self.assertTrue(result["adopted"])
        self.assertEqual(result["changed_keys"], ["topic_panel"])
        self.assertEqual(self.hub.read_bytes(), edited)
        rows = hub_deploy._journal_rows(self.amendment(window) / "journal.jsonl")
        # The edit is already in place: the amendment restarts but writes nothing.
        self.assertFalse(any(str(row.get("effect", "")).startswith("config:") for row in rows))
        self.assertTrue(any(row.get("effect") == "manifest-create" for row in rows))
        with self.assertRaisesRegex(hub_deploy.Refused, "no Hub config edit to adopt"):
            self.reconfigure(None)
        # The adopted bytes are now the chain head.
        self.assertEqual(
            self.reconfigure(self.write_candidate(message_batch_quiet_ms=900))["outcome"],
            "ACCEPTED",
        )

    def test_adopting_an_edit_of_a_protected_key_is_refused(self):
        self.accept()
        config = json.loads(self.original)
        config["owner_user_ids"] = [1]
        self.hub.write_bytes(json.dumps(config).encode())
        with self.assertRaisesRegex(hub_deploy.Refused, "not reconfigurable: owner_user_ids"):
            self.reconfigure(None)

    def test_failed_convergence_reverts_the_config_and_restarts(self):
        window = self.accept()
        self.manager.fail_start = True
        result = self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.assertEqual(result["outcome"], "REVERTED", result)
        self.assertEqual(self.hub.read_bytes(), self.original)
        self.assertEqual(self.manager.states["controller.service"], "active")
        rows = hub_deploy._journal_rows(self.amendment(window) / "journal.jsonl")
        phases = [row["phase"] for row in rows]
        self.assertIn("amendment_failed", phases)
        self.assertEqual(phases[-1], "REVERTED")
        # The reverted amendment is finished and leaves the head unchanged.
        status = self.require_status()
        self.assertEqual(status["manifest"], str(self.ops / window / "release-manifest.json"))
        self.assertEqual(
            self.reconfigure(self.write_candidate(topic_panel={}))["outcome"], "ACCEPTED"
        )
        # A reverted amendment needs no acknowledgement; the accepted one does.
        with self.assertRaisesRegex(hub_deploy.Refused, "amendment-002; pass --drop"):
            self.rollback(window)

    def test_unverified_revert_requires_recovery(self):
        self.accept()
        self.manager.fail_start = True
        original = self.window_runner

        def failing(argv, **kwargs):
            if "status" in argv and self.manager.fail_start is False:
                raise hub_deploy.Refused("convergence")
            return original(argv, **kwargs)

        result = self.reconfigure(
            self.write_candidate(topic_panel={"enabled": True}), runner=failing
        )
        self.assertEqual(result["outcome"], "RECOVERY_REQUIRED", result)
        with self.assertRaisesRegex(hub_deploy.Refused, "reconfigure --recover"):
            self.execute()
        self.assertEqual(
            hub_deploy.recover_amendment(
                self.install, self.manager, self.window_runner, self.sleeps.append
            )["outcome"],
            "REVERTED",
        )
        self.assertEqual(self.hub.read_bytes(), self.original)

    def test_release_rollback_needs_acknowledgement_and_restores_pre_deploy_config(self):
        window = self.accept()
        self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        stops = self.stops()
        with self.assertRaisesRegex(hub_deploy.Refused, "drops its amendments amendment-001"):
            self.rollback(window)
        self.assertEqual(self.stops(), stops)
        result = self.rollback(window, drop_amendments=True)
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        self.assertEqual(
            result["dropped_amendments"],
            [{"id": "amendment-001", "state": "ACCEPTED", "changed_keys": ["topic_panel"]}],
        )
        self.assertEqual(self.hub.read_bytes(), self.original)
        self.assertIn(str(self.old), (self.units / "worker.service.d/override.conf").read_text())
        rows = hub_deploy._journal_rows(self.ops / window / "journal.jsonl")
        dropped = next(row for row in rows if row["phase"] == "amendments_drop_acknowledged")
        self.assertEqual(dropped["amendments"], ["amendment-001"])
        hub_deploy._unfinished(self.install)

    def test_acknowledgement_without_amendments_is_refused(self):
        window = self.accept()
        with self.assertRaisesRegex(hub_deploy.Refused, "no Hub config amendments to drop"):
            self.rollback(window, drop_amendments=True)

    def test_forward_window_rollback_restores_pre_deploy_config(self):
        old = self.prepare_forward()
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, rollback_release="v3.0.0"
        )
        shutil.rmtree(self.plan)
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        original = self.manager.start

        def migrate(unit):
            if unit == "worker.service":
                with closing(sqlite3.connect(self.db)) as db:
                    db.execute("PRAGMA user_version=8")
            original(unit)

        self.manager.start = migrate
        window = self.accept()
        self.manager.start = original
        result = self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.assertEqual(result["outcome"], "ACCEPTED", result)
        create = next(
            argv for argv in self.calls if "create" in argv and result["manifest"] in argv
        )
        plan = hub_deploy.verify_saved_plan(self.plan, check_live=False)
        self.assertEqual(create[create.index("--rollback-artifact") + 1], plan["rollback_wheel"])
        result = self.rollback(window, drop_amendments=True)
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        self.assertEqual(self.hub.read_bytes(), self.original)
        self.assertIn("d" * 40, (self.units / "worker.service.d/override.conf").read_text())

    def test_interruption_at_every_amendment_boundary(self):
        boundaries: list[str] = []
        self.accept()
        self.reconfigure(
            self.write_candidate(topic_panel={"enabled": True}), fault=boundaries.append
        )
        self.assertTrue(any(b.startswith("effect:config:") for b in boundaries))
        for target in range(len(boundaries)):
            with self.subTest(boundary=boundaries[target]):
                fixture = ReconfigureTests(
                    methodName="test_change_restarts_services_and_binds_a_new_manifest"
                )
                fixture.setUp()
                try:
                    fixture.accept()
                    counter = [0]

                    def interrupt(_):
                        counter[0] += 1
                        if counter[0] == target + 1:
                            raise Interrupted()

                    candidate = fixture.write_candidate(topic_panel={"enabled": True})
                    with self.assertRaises(Interrupted):
                        fixture.reconfigure(candidate, fault=interrupt)
                    with self.assertRaisesRegex(hub_deploy.Refused, "reconfigure --recover"):
                        fixture.reconfigure(candidate)
                    with self.assertRaisesRegex(hub_deploy.Refused, "reconfigure --recover"):
                        fixture.execute()
                    status = fixture.require_status()
                    self.assertNotIn("manifest", status)
                    result = hub_deploy.recover_amendment(
                        fixture.install,
                        fixture.manager,
                        fixture.window_runner,
                        fixture.sleeps.append,
                    )
                    self.assertEqual(result["outcome"], "REVERTED", result)
                    self.assertEqual(fixture.hub.read_bytes(), fixture.original)
                    self.assertEqual(fixture.reconfigure(candidate)["outcome"], "ACCEPTED")
                finally:
                    fixture.doCleanups()

    def test_release_rollback_drops_an_interrupted_amendment(self):
        window = self.accept()

        def interrupt(boundary):
            if boundary == "after:config:after":
                raise Interrupted()

        with self.assertRaises(Interrupted):
            self.reconfigure(self.write_candidate(topic_panel={"enabled": True}), fault=interrupt)
        self.assertNotEqual(self.hub.read_bytes(), self.original)
        with self.assertRaisesRegex(hub_deploy.Refused, "pass --drop-amendments"):
            self.rollback(window)
        result = self.rollback(window, drop_amendments=True)
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        self.assertEqual(result["dropped_amendments"][0]["state"], "after")
        self.assertEqual(self.hub.read_bytes(), self.original)
        hub_deploy._unfinished(self.install)
        with self.assertRaisesRegex(
            hub_deploy.Refused, "no unfinished config amendment|not accepted"
        ):
            hub_deploy.recover_amendment(
                self.install, self.manager, self.window_runner, self.sleeps.append
            )

    def test_tampered_amendment_record_blocks_the_chain(self):
        window = self.accept()
        self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        copy = self.amendment(window) / "config-after"
        copy.write_bytes(b"{}")
        with self.assertRaisesRegex(hub_deploy.Refused, "amendment copy changed"):
            self.reconfigure(self.write_candidate(message_batch_quiet_ms=900))

    def reboot(self):
        """Every unit gets a new invocation, as after a host restart."""
        identity = self.manager.identity
        self.manager.identity = lambda unit: {**identity(unit), "InvocationID": "after-reboot"}

    def test_release_rollback_after_a_reboot_uses_a_fresh_baseline(self):
        window = self.accept()
        self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.reboot()
        result = self.rollback(window, drop_amendments=True)
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        rows = hub_deploy._journal_rows(self.ops / window / "journal.jsonl")
        baseline = next(row for row in rows if row["phase"] == "recovery_baseline")
        self.assertEqual(
            baseline["untouched"]["units"]["tlive.service"]["InvocationID"], "after-reboot"
        )
        # A retry on the rolled-back window has nothing left to drop.
        with self.assertRaisesRegex(hub_deploy.Refused, "no Hub config amendments to drop"):
            self.rollback(window, drop_amendments=True)
        self.assertEqual(self.rollback(window)["outcome"], "ROLLED_BACK")

    def test_recover_after_a_reboot_uses_a_fresh_baseline_and_refuses_busy_work(self):
        window = self.accept()

        def interrupt(boundary):
            if boundary == "after:config:after":
                raise Interrupted()

        with self.assertRaises(Interrupted):
            self.reconfigure(self.write_candidate(topic_panel={"enabled": True}), fault=interrupt)
        self.reboot()
        for unit in self.install["restart_order"]:
            self.manager.states[unit] = "active"
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("INSERT INTO provider_jobs VALUES ('codex', 'executing')")
            db.commit()
        stops = self.stops()
        with self.assertRaisesRegex(hub_deploy.Refused, "recover when idle"):
            hub_deploy.recover_amendment(
                self.install, self.manager, self.window_runner, self.sleeps.append
            )
        self.assertEqual(self.stops(), stops)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("DELETE FROM provider_jobs")
            db.commit()
        result = hub_deploy.recover_amendment(
            self.install, self.manager, self.window_runner, self.sleeps.append
        )
        self.assertEqual(result["outcome"], "REVERTED", result)
        self.assertEqual(self.hub.read_bytes(), self.original)
        rows = hub_deploy._journal_rows(self.amendment(window) / "journal.jsonl")
        self.assertIn("recovery_baseline", [row["phase"] for row in rows])

    def test_revert_without_a_fresh_monitor_run_requires_recovery(self):
        self.accept()
        self.manager.fail_start = True
        original = self.manager.start

        def start(unit):
            # The candidate controller fails, then both monitor runs of the revert fail.
            if self.manager.fail_start is False and unit == "monitor.service":
                raise hub_deploy.Refused("monitor failure")
            original(unit)

        self.manager.start = start
        result = self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.assertEqual(result["outcome"], "RECOVERY_REQUIRED", result)
        self.assertIn("monitor", result["error"])
        self.assertEqual(self.hub.read_bytes(), self.original)
        self.manager.start = original
        self.assertEqual(
            hub_deploy.recover_amendment(
                self.install, self.manager, self.window_runner, self.sleeps.append
            )["outcome"],
            "REVERTED",
        )

    def test_swapped_window_backup_is_refused_before_stopping(self):
        window = self.accept()
        backup = self.ops / window / "state.db"
        with closing(sqlite3.connect(backup)) as db:
            db.execute("CREATE TABLE swapped (x)")
            db.commit()
        stops = self.stops()
        with self.assertRaisesRegex(hub_deploy.Refused, "differs from the window manifest"):
            self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.assertEqual(self.stops(), stops)

    def test_new_manifest_must_bind_only_the_config_change(self):
        self.accept()
        original = self.window_runner

        def other_wheel(argv, **kwargs):
            if "create" in argv and "release-manifest" in argv:
                argv = list(map(str, argv))
                argv[argv.index("--rollback-artifact") + 1] = argv[
                    argv.index("--active-artifact") + 1
                ]
            return original(argv, **kwargs)

        result = self.reconfigure(
            self.write_candidate(topic_panel={"enabled": True}), runner=other_wheel
        )
        self.assertEqual(result["outcome"], "REVERTED", result)
        self.assertEqual(self.hub.read_bytes(), self.original)

    def test_changed_window_file_is_refused_before_stopping(self):
        self.accept()
        dropin = self.units / "worker.service.d/override.conf"
        entry = next(
            e
            for e in hub_deploy.verify_saved_plan(self.plan, check_live=False)["files"]
            if e["path"] == str(dropin)
        )
        # Restoring the pre-deploy bytes is valid for a release rollback, not here.
        dropin.write_bytes((self.plan / entry["before"]["copy"]).read_bytes())
        stops = self.stops()
        with self.assertRaisesRegex(hub_deploy.Refused, "differs from the window"):
            self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.assertEqual(self.stops(), stops)

    def test_status_reports_drift_instead_of_a_stale_manifest(self):
        self.accept()
        config = json.loads(self.original)
        config["topic_panel"] = {"enabled": True}
        self.hub.write_bytes(json.dumps(config).encode())
        status = self.require_status()
        self.assertTrue(status["config_drift"])
        self.assertNotIn("manifest", status)
        self.reconfigure(None)
        status = self.require_status()
        self.assertNotIn("config_drift", status)
        self.assertTrue(status["manifest"].endswith("amendment-001/release-manifest.json"))

    def test_rollback_refuses_bytes_of_a_reverted_candidate(self):
        window = self.accept()
        self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.manager.fail_start = True
        reverted = self.write_candidate(message_batch_quiet_ms=900).read_bytes()
        self.assertEqual(self.reconfigure(self.candidate)["outcome"], "REVERTED")
        self.hub.write_bytes(reverted)
        with self.assertRaisesRegex(hub_deploy.Refused, "unrelated file edit"):
            self.rollback(window, drop_amendments=True)

    def test_unfinished_rollback_suggests_the_acknowledgement(self):
        window = self.accept()
        self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.fail_verify_old = True
        result = self.rollback(window, drop_amendments=True)
        self.assertEqual(result["outcome"], "RECOVERY_REQUIRED", result)
        self.assertNotIn("dropped_amendments", result)
        with self.assertRaisesRegex(hub_deploy.Refused, "--drop-amendments"):
            self.execute()

    def test_abandoned_preparation_is_removed_under_the_lock(self):
        window = self.accept()
        abandoned = self.ops / window / ".amendment-preparing-example"
        abandoned.mkdir(mode=0o700)
        (abandoned / "config-after").write_bytes(b"{}")
        self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.assertFalse(abandoned.exists())

    def test_release_rollback_probes_the_socket_with_the_recovery_runtime(self):
        self.manager.states["codex.service"] = "active"
        self.manifest.write_text(
            json.dumps({"hub_git_sha": self.old_sha, "codex_binary": {"path": "/fixture/codex"}})
        )
        old = hub_deploy.discover_installation(
            self.install, self.runner, lambda *_: True, lambda _: 0
        )
        new = hub_deploy.prepare_stage(
            self.install, old, "v2.0.0", self.runner, python=Path("/usr/bin/python3")
        )
        shutil.rmtree(self.plan)
        self.plan = hub_deploy.create_plan(self.install, old, new, self.runner)
        window = self.accept()
        self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        original = self.window_runner
        probes = []

        def broken_release(argv, **kwargs):
            if "doctor" in argv:
                probes.append(argv[0])
                if str(self.old) not in argv[0]:
                    raise hub_deploy.Refused("the release being rolled back is broken")
                if b"topic_panel" in self.hub.read_bytes():
                    raise hub_deploy.Refused("the previous release cannot read the amendment")
            return original(argv, **kwargs)

        result = hub_deploy.rollback_window(
            self.install,
            window,
            self.manager,
            broken_release,
            self.sleeps.append,
            drop_amendments=True,
        )
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        self.assertTrue(probes)
        self.assertTrue(all(str(self.old) in probe for probe in probes))

    def test_window_manifest_must_be_the_one_the_window_verified(self):
        window = self.accept()
        rows = hub_deploy._journal_rows(self.ops / window / "journal.jsonl")
        bound = next(row for row in rows if row["phase"] == "manifest_bound")
        manifest = self.ops / window / "release-manifest.json"
        self.assertEqual(bound["sha256"], digest(manifest.read_bytes()))
        # Replace the backup and its matching manifest together.
        backup = self.ops / window / "state.db"
        with closing(sqlite3.connect(backup)) as db:
            db.execute("CREATE TABLE swapped (x)")
            db.commit()
        document = json.loads(manifest.read_bytes())
        document["state_backup"]["sha256"] = digest(backup.read_bytes())
        manifest.write_text(json.dumps(document))
        stops = self.stops()
        with self.assertRaisesRegex(hub_deploy.Refused, "differs from the one the window verified"):
            self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.assertEqual(self.stops(), stops)

    def test_manifest_replaced_during_window_verification_is_never_bound(self):
        original = self.window_runner
        replaced = []

        def replace_during_verify(argv, **kwargs):
            result = original(argv, **kwargs)
            if "verify" in argv and "release-manifest" in argv and not replaced:
                manifest = Path(argv[argv.index("verify") + 1])
                document = json.loads(manifest.read_bytes())
                document["created_at"] = "replaced"
                manifest.write_text(json.dumps(document))
                replaced.append(manifest)
            return result

        result = hub_deploy.apply_window(
            self.install,
            self.plan,
            self.manager,
            replace_during_verify,
            self.sleeps.append,
            load=lambda: 0,
        )
        self.assertEqual(result["outcome"], "ROLLED_BACK", result)
        rows = hub_deploy._journal_rows(self.ops / result["id"] / "journal.jsonl")
        self.assertNotIn("manifest_bound", [row["phase"] for row in rows])
        failure = next(row for row in rows if row["phase"] == "apply_failed")
        self.assertIn("changed during verification", failure["error"])

    def test_window_without_a_recorded_manifest_digest_is_trusted_as_found(self):
        window = self.accept()
        journal = self.ops / window / "journal.jsonl"
        lines = journal.read_bytes().splitlines(keepends=True)
        journal.write_bytes(b"".join(line for line in lines if b"manifest_bound" not in line))
        result = self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        self.assertEqual(result["outcome"], "ACCEPTED", result)

    def test_manifest_replaced_after_its_check_is_not_reported(self):
        window = self.accept()
        original = self.window_runner
        manifest = self.amendment(window) / "release-manifest.json"

        def replace_during_convergence(argv, **kwargs):
            if "status" in argv and manifest.exists() and not self.sleeps:
                document = json.loads(manifest.read_bytes())
                document["state_backup"]["sha256"] = "0" * 64
                manifest.write_text(json.dumps(document))
                self.sleeps.append("replaced")
            return original(argv, **kwargs)

        result = self.reconfigure(
            self.write_candidate(topic_panel={"enabled": True}),
            runner=replace_during_convergence,
        )
        self.assertEqual(result["outcome"], "REVERTED", result)
        self.assertNotIn("manifest_sha256", result)
        rows = hub_deploy._journal_rows(self.amendment(window) / "journal.jsonl")
        failure = next(row for row in rows if row["phase"] == "amendment_failed")
        self.assertIn("changed after its check", failure["error"])

    def test_accepted_amendment_journals_its_manifest_digest(self):
        window = self.accept()
        result = self.reconfigure(self.write_candidate(topic_panel={"enabled": True}))
        rows = hub_deploy._journal_rows(self.amendment(window) / "journal.jsonl")
        self.assertEqual(
            rows[-1], {"phase": "ACCEPTED", "manifest_sha256": result["manifest_sha256"]}
        )

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = hub_deploy.main(
                [argv[0], "--installation", str(self.input), *argv[1:]],
                runner=self.window_runner,
                manager=self.manager,
                sleep=self.sleeps.append,
                load=lambda: 0,
            )
        return code, output.getvalue(), errors.getvalue()

    def test_cli_options_and_exit_codes(self):
        window = self.accept()
        candidate = str(self.write_candidate(topic_panel={"enabled": True}))
        for argv in (
            ("reconfigure",),
            ("reconfigure", "--config", candidate, "--adopt"),
            ("reconfigure", "--recover", "--window", window),
            ("status", "--adopt"),
            ("status", "--drop-amendments"),
            ("reconfigure", "--config", candidate, "--drop-amendments"),
            ("reconfigure", "--config", "", "--adopt"),
            ("status", "--config", ""),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(*argv)[0], 1)
        code, output, _ = self.run_cli("reconfigure", "--config", candidate)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["outcome"], "ACCEPTED")
        code, _, errors = self.run_cli("reconfigure", "--recover")
        self.assertEqual(code, 1)
        self.assertIn("no unfinished config amendment", errors)
        self.manager.fail_start = True
        code, output, _ = self.run_cli(
            "reconfigure", "--config", str(self.write_candidate(topic_panel={}))
        )
        self.assertEqual((code, json.loads(output)["outcome"]), (2, "REVERTED"))
        code, output, _ = self.run_cli("rollback", "--window", window, "--drop-amendments")
        self.assertEqual((code, json.loads(output)["outcome"]), (2, "ROLLED_BACK"))


if __name__ == "__main__":
    unittest.main()
