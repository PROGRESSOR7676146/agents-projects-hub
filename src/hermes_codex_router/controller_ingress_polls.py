"""Startup captures its first successful epoch snapshot; never refreshes CAS/token."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from .diagnostic_log import survived
from .state_errors import StateError
from .telegram_ingress_ledger import (
    IngressOwnershipLost,
    IngressPollOwner,
    TelegramIngressLedger,
)


class ControllerIngressPolls:
    def __init__(self, ledger: TelegramIngressLedger, identity: str) -> None:
        self.ledger = ledger
        self.owner: IngressPollOwner | None = None
        self.sequence = 0
        self.identity = identity
        self.instance_token = uuid.uuid4().hex
        self.previous_epoch: int | None = None
        self.registration_pending = True
        self._register_startup()

    def _register_startup(self) -> None:
        try:
            if self.previous_epoch is None:
                self.previous_epoch = self.ledger.current_epoch(self.identity)
            self.owner = self.ledger.register(
                self.identity,
                instance_token=self.instance_token,
                previous_epoch=self.previous_epoch,
                now=datetime.now(timezone.utc),
            )
            self.registration_pending = False
        except sqlite3.Error as error:
            survived("controller.ingress_registration", error)
        except IngressOwnershipLost as error:
            self.registration_pending = False
            survived("controller.ingress_retired", error)
        except StateError as error:
            survived("controller.ingress_registration", error)

    def record(self, *, succeeded: bool, observed_at: datetime) -> None:
        self.sequence += 1
        if self.registration_pending:
            self._register_startup()
        if self.owner is None:
            return
        try:
            if not self.ledger.record_poll(
                self.owner, sequence=self.sequence, succeeded=succeeded, observed_at=observed_at
            ):
                self.owner = None
                survived("controller.ingress_retired", StateError("poll evidence fence refused"))
        except sqlite3.Error as error:
            # A skipped or ambiguously committed sample may create a gap. The
            # ledger breaks failure consecutiveness rather than inventing proof.
            survived("controller.ingress_poll_write", error)
        except StateError as error:
            survived("controller.ingress_sample_refused", error)
