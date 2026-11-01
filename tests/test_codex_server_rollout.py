"""An owner-closed exact native completion permits start without archiving."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from hermes_codex_router import codex_rollout_completion as rollout_reader
from hermes_codex_router import codex_server_service as service
from tests import test_codex_turn_controls as fixtures


class CodexServerRolloutTests(unittest.TestCase):
    def setUp(self):
        self.f = self.uncertain_fixture("externally_completed")
        self.home = self.f.root.parent / "native-home"
        self.home.mkdir(mode=0o700)
        directory = self.home / "sessions" / "2026" / "01" / "01"
        directory.mkdir(parents=True)
        self.rollout = directory / "rollout-example-example-thread.jsonl"
        self.records = [
            self.record("session_meta", id="example-thread", cwd=str(self.f.root)),
            self.record("event_msg", type="task_started", turn_id="example-turn"),
            self.record("turn_context", turn_id="example-turn", cwd=str(self.f.root)),
            self.record("response_item", type="reasoning", content="EXAMPLE_PRIVATE_REASONING"),
            self.record("event_msg", type="task_complete", turn_id="example-turn"),
        ]
        self.write_rollout()

    def uncertain_fixture(self, resolution):
        f = fixtures.CodexTurnControlJournalTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.journal.record_turn(f.job_id, f.token, "example-turn")
        f.state.terminate_provider_job_with_notice(
            f.job_id,
            f.token,
            status="indeterminate",
            error_class="ambiguous_execution",
            error_code="example_unknown",
            sender_agent_id="codex",
            telegram_html="Unknown outcome",
        )
        if resolution:
            f.state.resolve_indeterminate_job(f.job_id, resolution)
        return f

    @staticmethod
    def record(kind, **payload):
        return {"type": kind, "payload": payload}

    def write_rollout(self):
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in self.records))
        self.rollout.chmod(0o600)

    def check(self):
        service.require_idle_state(self.f.fixture.config.state_path, codex_home=self.home)

    def test_exact_completion_keeps_active_session_and_database_unchanged(self):
        before = self.f.fixture.config.state_path.read_bytes()
        self.check()
        self.assertEqual(self.f.fixture.config.state_path.read_bytes(), before)
        self.assertEqual(self.f.state.get_provider_job(self.f.job_id).status, "indeterminate")
        session = self.f.state.get_session(self.f.state.get_provider_job(self.f.job_id).session_id)
        self.assertEqual(session.status, "active")

    def test_missing_or_other_turn_root_thread_context_and_terminal_refuse(self):
        original = list(self.records)
        for index, field, value in (
            (0, "id", "other-thread"),
            (0, "cwd", "/home/example/other-project"),
            (2, "cwd", "/home/example/other-project"),
            (2, "turn_id", "other-turn"),
            (4, "turn_id", "other-turn"),
            (4, "type", "turn_aborted"),
        ):
            with self.subTest(index=index, field=field, value=value):
                self.records = json.loads(json.dumps(original))
                self.records[index]["payload"][field] = value
                self.write_rollout()
                with self.assertRaisesRegex(ValueError, "active or unknown"):
                    self.check()
        self.rollout.unlink()
        with self.assertRaisesRegex(ValueError, "active or unknown"):
            self.check()

    def test_owner_resolution_alone_or_other_resolution_never_substitutes_for_proof(self):
        with self.assertRaisesRegex(ValueError, "active or unknown"):
            service.require_idle_state(self.f.fixture.config.state_path)
        for resolution in ("acknowledged", "superseded", None):
            with self.subTest(resolution=resolution):
                f = self.uncertain_fixture(resolution)
                with patch.object(service, "completed_rollout_turns") as read:
                    with self.assertRaisesRegex(ValueError, "active or unknown"):
                        service.require_idle_state(
                            f.fixture.config.state_path, codex_home=self.home
                        )
                read.assert_not_called()

    def test_native_completion_never_waives_reader_sender_or_local_writer(self):
        for update in (
            "UPDATE codex_turn_controls SET read_claim_token='example-reader',"
            "read_claim_owner='example-worker',read_claim_expires_at='example-expiry'",
            "UPDATE codex_turn_controls SET send_started_at='example-time',"
            "send_owner_token_hash='example-owner',interrupt_source='protective'",
            "UPDATE agent_sessions SET writer_mode='local'",
        ):
            with self.subTest(update=update):
                f = self.uncertain_fixture("externally_completed")
                with f.state._connection as db:
                    db.execute(update)
                with patch.object(service, "completed_rollout_turns") as read:
                    with self.assertRaisesRegex(ValueError, "active or unknown"):
                        service.require_idle_state(
                            f.fixture.config.state_path, codex_home=self.home
                        )
                read.assert_not_called()

    def test_incomplete_ambiguous_changed_or_unsafe_history_refuses(self):
        for tail in ("{", "{}", json.dumps(self.records[-1])):
            with self.subTest(tail=tail):
                self.write_rollout()
                with self.rollout.open("a") as stream:
                    stream.write(tail)
                with self.assertRaisesRegex(ValueError, "active or unknown"):
                    self.check()
        self.write_rollout()
        self.records.append(self.record("event_msg", type="task_started", turn_id="later-turn"))
        self.write_rollout()
        with self.assertRaisesRegex(ValueError, "active or unknown"):
            self.check()
        self.records.pop()
        self.write_rollout()
        duplicate = self.home / "archived_sessions"
        duplicate.mkdir()
        other = duplicate / self.rollout.name
        other.write_bytes(self.rollout.read_bytes())
        with self.assertRaisesRegex(ValueError, "active or unknown"):
            self.check()
        other.unlink()
        self.rollout.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "active or unknown"):
            self.check()

    def test_bounds_and_symlink_refuse_without_reading_arbitrary_history(self):
        for name, value in (("MAX_ROLLOUT_BYTES", 1), ("MAX_RECORD_BYTES", 8), ("MAX_TURNS", 0)):
            with self.subTest(bound=name), patch.object(rollout_reader, name, value):
                with self.assertRaisesRegex(ValueError, "active or unknown"):
                    self.check()
        self.rollout.rename(self.rollout.with_suffix(".saved"))
        self.rollout.symlink_to(self.rollout.with_suffix(".saved"))
        with self.assertRaisesRegex(ValueError, "active or unknown"):
            self.check()

    def test_concurrent_append_and_repeated_turn_lifecycle_refuse(self):
        real_fstat = rollout_reader.os.fstat
        calls = 0

        def append_before_final_stat(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.rollout.open("a") as stream:
                    stream.write(json.dumps(self.record("event_msg", type="example-event")) + "\n")
            return real_fstat(fd)

        with patch.object(rollout_reader.os, "fstat", side_effect=append_before_final_stat):
            with self.assertRaisesRegex(ValueError, "active or unknown"):
                self.check()
        self.records.extend(self.records[1:])
        self.write_rollout()
        with self.assertRaisesRegex(ValueError, "active or unknown"):
            self.check()

    def test_archived_native_history_and_later_completed_or_aborted_turn_are_supported(self):
        self.records.extend(
            [
                self.record("event_msg", type="task_started", turn_id="later-turn"),
                self.record("event_msg", type="turn_aborted", turn_id="later-turn"),
            ]
        )
        self.write_rollout()
        archived = self.home / "archived_sessions"
        archived.mkdir()
        self.rollout.rename(archived / self.rollout.name)
        self.check()

    def test_prepare_passes_only_manifest_codex_home_to_read_only_gate(self):
        manifest = {
            "state_path": str(self.f.fixture.config.state_path),
            "codex_home": str(self.home),
        }
        with (
            patch.object(service, "verify_pins"),
            patch.object(service, "refuse_existing_servers"),
            patch.object(service, "require_idle_state") as check,
        ):
            service.prepare(manifest, clients_closed=True)
        check.assert_called_once_with(self.f.fixture.config.state_path, codex_home=self.home)
