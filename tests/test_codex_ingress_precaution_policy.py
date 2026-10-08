"""Pure policy fixtures; no controller, provider, Telegram or native authority."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from hermes_codex_router.codex_ingress_precaution_policy import (
    IngressPollEvidence,
    assess_ingress,
    retain_earlier_deadline,
)


class IngressPrecautionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.accepted = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.now = self.accepted + timedelta(seconds=100)
        self.evidence = IngressPollEvidence(
            identity="hub",
            epoch=1,
            registered_at=self.accepted,
            heartbeat_at=self.now,
            last_poll_at=self.now,
            last_success_at=self.now,
            failure_streak=0,
            failure_threshold_at=None,
        )

    def assess(self, evidence, *, now=None, prior=None, last_confirmed_poll_at=None):
        return assess_ingress(
            evidence,
            expected_identity="hub",
            accepted_at=self.accepted,
            now=now or self.now,
            prior=prior,
            last_confirmed_poll_at=last_confirmed_poll_at,
        )

    def test_healthy_long_turn_then_restart_retains_confirmation_without_episode(self):
        hour = self.accepted + timedelta(hours=1)
        healthy = replace(self.evidence, heartbeat_at=hour, last_poll_at=hour, last_success_at=hour)
        confirmed = self.assess(healthy, now=hour)
        self.assertIsNone(confirmed.episode)
        self.assertEqual(confirmed.last_confirmed_poll_at, hour)
        restart_at = hour + timedelta(seconds=1)
        restarted = replace(
            healthy,
            epoch=2,
            registered_at=restart_at,
            heartbeat_at=restart_at,
            last_poll_at=None,
            last_success_at=None,
        )
        for evidence in (None, restarted, replace(healthy, identity="codex")):
            with self.subTest(evidence=evidence):
                result = self.assess(
                    evidence,
                    now=restart_at,
                    last_confirmed_poll_at=confirmed.last_confirmed_poll_at,
                )
                assert result.episode is not None
                self.assertFalse(result.recent_poll_confirmed)
                self.assertEqual(result.episode.deadline, hour + timedelta(seconds=180))
                self.assertEqual(result.last_confirmed_poll_at, hour)

    def test_successful_empty_poll_is_healthy_without_topic_control_claim(self):
        result = self.assess(self.evidence)
        self.assertTrue(result.recent_poll_confirmed)
        self.assertIsNone(result.episode)

    def test_new_epoch_success_clears_restart_episode_before_future_deadline_anchor(self):
        hour = self.accepted + timedelta(hours=1)
        restarted_at = hour + timedelta(seconds=1)
        restart = self.assess(None, now=restarted_at, last_confirmed_poll_at=hour)
        assert restart.episode is not None
        recovered_at = hour + timedelta(seconds=2)
        evidence = replace(
            self.evidence,
            epoch=2,
            registered_at=restarted_at,
            heartbeat_at=recovered_at,
            last_poll_at=recovered_at,
            last_success_at=recovered_at,
        )
        recovered = self.assess(
            evidence,
            now=recovered_at,
            prior=restart.episode,
            last_confirmed_poll_at=restart.last_confirmed_poll_at,
        )
        self.assertTrue(recovered.recent_poll_confirmed)
        self.assertIsNone(recovered.episode)
        self.assertEqual(recovered.last_confirmed_poll_at, recovered_at)

    def test_invalid_retained_state_refuses_even_with_healthy_poll(self):
        episode = self.assess(None).episode
        assert episode is not None
        for prior in (
            replace(episode, since=episode.since.replace(tzinfo=None)),
            replace(episode, deadline=episode.since - timedelta(seconds=1)),
            replace(episode, recovery_after=episode.deadline + timedelta(seconds=1)),
            replace(episode, recovery_after=episode.recovery_after.replace(tzinfo=None)),
        ):
            with self.subTest(prior=prior), self.assertRaises(ValueError):
                self.assess(self.evidence, prior=prior)
        for stamp in (self.now.replace(tzinfo=None), self.now + timedelta(seconds=6)):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.assess(self.evidence, last_confirmed_poll_at=stamp)

    def test_unusable_evidence_cannot_advance_continuity_or_postpone_deadline(self):
        hour = self.accepted + timedelta(hours=1)
        now = hour + timedelta(seconds=1)
        prior = self.assess(None, now=now, last_confirmed_poll_at=hour)
        later = hour + timedelta(seconds=20)
        for evidence in (
            None,
            replace(
                self.evidence,
                identity="codex",
                heartbeat_at=later,
                last_poll_at=later,
                last_success_at=later,
            ),
            replace(
                self.evidence,
                heartbeat_at=later + timedelta(seconds=6),
                last_poll_at=later + timedelta(seconds=6),
                last_success_at=later + timedelta(seconds=6),
            ),
            replace(
                self.evidence,
                epoch=10,
                registered_at=later,
                heartbeat_at=later,
                last_poll_at=None,
                last_success_at=None,
            ),
        ):
            with self.subTest(evidence=evidence):
                result = self.assess(
                    evidence,
                    now=later,
                    prior=prior.episode,
                    last_confirmed_poll_at=prior.last_confirmed_poll_at,
                )
                self.assertEqual(result.last_confirmed_poll_at, hour)
                self.assertEqual(result.episode, prior.episode)

    def test_old_recent_success_does_not_clear_new_failure_episode(self):
        failed = replace(
            self.evidence,
            last_success_at=self.accepted + timedelta(seconds=70),
            failure_streak=3,
            failure_threshold_at=self.accepted + timedelta(seconds=80),
        )
        prior = self.assess(failed).episode
        result = self.assess(
            replace(failed, failure_streak=0, failure_threshold_at=None), prior=prior
        )
        self.assertEqual(result.episode, prior)
        self.assertFalse(result.recent_poll_confirmed)

    def test_first_observation_after_deadline_retains_due_episode(self):
        now = self.accepted + timedelta(hours=1)
        result = self.assess(None, now=now)
        assert result.episode is not None
        self.assertEqual(result.episode.deadline, self.accepted + timedelta(seconds=120))
        self.assertEqual(result.episode.recovery_after, now)

    def test_existing_failure_deadline_can_precede_new_target_acceptance(self):
        now = self.accepted + timedelta(seconds=116)
        failed = replace(
            self.evidence,
            heartbeat_at=now,
            last_poll_at=now,
            last_success_at=self.accepted + timedelta(seconds=70),
            failure_streak=3,
            failure_threshold_at=self.accepted + timedelta(seconds=80),
        )
        result = assess_ingress(failed, expected_identity="hub", accepted_at=now, now=now)
        assert result.episode is not None
        self.assertEqual(result.episode.deadline, self.accepted + timedelta(seconds=110))
        self.assertLess(result.episode.deadline, now)

    def test_fresh_recovery_clears_due_but_unsent_episode(self):
        pending = self.assess(None).episode
        assert pending is not None
        now = pending.deadline + timedelta(seconds=10)
        fresh = replace(self.evidence, heartbeat_at=now, last_poll_at=now, last_success_at=now)
        result = self.assess(fresh, now=now, prior=pending)
        self.assertTrue(result.recent_poll_confirmed)
        self.assertIsNone(result.episode)

    def test_missing_or_other_ingress_uses_acceptance_without_restart_extension(self):
        deadline = self.accepted + timedelta(seconds=120)
        for evidence in (None, replace(self.evidence, identity="codex")):
            with self.subTest(evidence=evidence):
                result = self.assess(evidence)
                self.assertFalse(result.recent_poll_confirmed)
                assert result.episode is not None
                self.assertEqual(result.episode.deadline, deadline)
                restarted = replace(
                    self.evidence,
                    epoch=2,
                    registered_at=self.now,
                    last_poll_at=None,
                    last_success_at=None,
                )
                recovery = self.assess(restarted, prior=result.episode)
                assert recovery.episode is not None
                self.assertEqual(recovery.episode.deadline, deadline)

    def test_stale_success_gets_sixty_plus_one_hundred_twenty_seconds(self):
        last_success = self.accepted + timedelta(seconds=10)
        result = self.assess(
            replace(self.evidence, last_success_at=last_success, last_poll_at=last_success)
        )
        assert result.episode is not None
        self.assertEqual(result.episode.deadline, last_success + timedelta(seconds=180))
        self.assertEqual(result.episode.since, last_success + timedelta(seconds=60))

    def test_preexisting_stale_signal_does_not_remove_new_target_grace(self):
        evidence = replace(
            self.evidence,
            registered_at=self.accepted - timedelta(hours=1),
            last_poll_at=self.accepted - timedelta(minutes=20),
            last_success_at=self.accepted - timedelta(minutes=20),
        )
        result = self.assess(evidence)
        assert result.episode is not None
        self.assertEqual(result.episode.deadline, self.accepted + timedelta(seconds=120))

    def test_three_fresh_failures_use_original_threshold_and_thirty_second_grace(self):
        threshold = self.accepted + timedelta(seconds=80)
        failed = replace(
            self.evidence,
            last_success_at=self.accepted + timedelta(seconds=70),
            failure_streak=3,
            failure_threshold_at=threshold,
        )
        result = self.assess(failed)
        assert result.episode is not None
        self.assertEqual(result.episode.deadline, threshold + timedelta(seconds=30))
        repeated = self.assess(
            replace(failed, failure_streak=20),
            now=self.now + timedelta(seconds=5),
            prior=result.episode,
        )
        assert repeated.episode is not None
        self.assertEqual(repeated.episode.deadline, result.episode.deadline)

    def test_one_or_two_failures_with_recent_success_do_not_invent_outage(self):
        for count in (1, 2):
            result = self.assess(replace(self.evidence, failure_streak=count))
            self.assertTrue(result.recent_poll_confirmed)
            self.assertIsNone(result.episode)

    def test_suspect_becoming_missing_cannot_extend_original_deadline(self):
        failed = replace(
            self.evidence,
            last_success_at=self.accepted + timedelta(seconds=70),
            failure_streak=3,
            failure_threshold_at=self.accepted + timedelta(seconds=80),
        )
        prior = self.assess(failed).episode
        assert prior is not None
        result = self.assess(None, prior=prior)
        assert result.episode is not None
        self.assertEqual(result.episode.deadline, prior.deadline)
        self.assertEqual(retain_earlier_deadline(prior, result.episode), prior)

    def test_heartbeat_or_new_epoch_without_poll_success_does_not_clear_episode(self):
        prior = self.assess(None).episode
        evidence = replace(self.evidence, epoch=2, last_success_at=None, last_poll_at=None)
        self.assertIsNotNone(self.assess(evidence, prior=prior).episode)

    def test_only_matching_fresh_poll_success_clears_prospective_episode(self):
        prior = self.assess(None).episode
        self.assertIsNone(self.assess(self.evidence, prior=prior).episode)
        self.assertIsNotNone(
            self.assess(replace(self.evidence, identity="codex"), prior=prior).episode
        )

    def test_invalid_timestamp_order_future_and_counter_degrade_to_missing(self):
        for evidence in (
            replace(self.evidence, epoch=True),
            replace(self.evidence, failure_streak=True),
            replace(self.evidence, failure_streak=-1),
            replace(self.evidence, heartbeat_at=self.now.replace(tzinfo=None)),
            replace(self.evidence, last_poll_at=self.now + timedelta(seconds=6)),
            replace(self.evidence, registered_at=self.now + timedelta(seconds=1)),
            replace(self.evidence, last_success_at=self.now + timedelta(seconds=1)),
            replace(self.evidence, failure_streak=3, failure_threshold_at=None),
        ):
            with self.subTest(evidence=evidence):
                result = self.assess(evidence)
                self.assertFalse(result.recent_poll_confirmed)
                assert result.episode is not None
                self.assertEqual(result.episode.deadline, self.accepted + timedelta(seconds=120))

    def test_future_tolerance_allows_coherent_clock_skew_of_five_seconds(self):
        skew = self.now + timedelta(seconds=5)
        evidence = replace(
            self.evidence, heartbeat_at=skew, last_poll_at=skew, last_success_at=skew
        )
        self.assertTrue(self.assess(evidence).recent_poll_confirmed)

    def test_invalid_target_clock_refuses_policy_instead_of_guessing_identity(self):
        with self.assertRaises(ValueError):
            assess_ingress(
                None,
                expected_identity="hub",
                accepted_at=self.accepted.replace(tzinfo=None),
                now=self.now,
            )


if __name__ == "__main__":
    unittest.main()
