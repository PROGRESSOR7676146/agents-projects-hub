"""Classify same-UID processes that could lease Codex jobs (ADR 0105).

The first boot start of the pinned server may leave queued Codex work in place
only when no Hub process that can consume it is observed. Only the external
worker of the ``codex`` agent and the embedded consumer of the Controller and
the Telegram bot lease Codex jobs. This classifier is deliberately narrow: a
process is harmless only when it parses as a known command that cannot lease a
Codex job. Anything else that mentions the Hub counts as a consumer, so an
unknown or ambiguous form keeps the queued work blocking, as before.
"""

from __future__ import annotations

import os
import re

PACKAGE = b"hermes_codex_router"
HUB_SCRIPTS = frozenset({b"agents-projects-hub", b"hcr"})
# Console-script helpers that never touch the job queue.
HUB_HELPERS = frozenset(
    {b"agents-projects-hub-prepare-codex-socket-dir", b"agents-projects-hub-wait-socket"}
)
CLI_MODULES = frozenset({PACKAGE, PACKAGE + b".cli"})
# The start gate itself, and its readiness step that runs beside it.
SERVER_MODULE = PACKAGE + b".codex_server_service"
# Commands whose units are not ordered after the server and that lease no
# Codex job. Each is a contract: a command that gains a consumer leaves here.
SAFE_COMMANDS = frozenset({b"sender", b"monitor", b"project-provisioner"})
CODEX_AGENT = b"codex"
_PYTHON = re.compile(rb"python[0-9.]*")


def _hub_related(argv: list[bytes]) -> bool:
    for item in argv:
        name = os.path.basename(item)
        if PACKAGE in item or name.startswith(b"agents-projects-hub") or name == b"hcr":
            return True
    return False


def _worker_agent(arguments: list[bytes]) -> bytes | None:
    """The one explicit ``--agent`` value of a worker, or None when ambiguous.

    The worker defaults to the Codex agent, argparse accepts abbreviations and
    the last repetition wins, so only one unabbreviated value is accepted.
    """
    if b"--" in arguments:
        return None
    values: list[bytes] = []
    for index, item in enumerate(arguments):
        if item == b"--agent":
            values.append(arguments[index + 1] if index + 1 < len(arguments) else b"")
        elif item.startswith(b"--agent="):
            values.append(item[len(b"--agent=") :])
        elif item.startswith(b"--a"):
            return None
    if len(values) != 1 or not values[0] or values[0].startswith(b"-"):
        return None
    return values[0]


def _safe_command(arguments: list[bytes]) -> bool:
    if not arguments:
        return False
    command, rest = arguments[0], arguments[1:]
    if command in SAFE_COMMANDS:
        return True
    if command == b"worker":
        agent = _worker_agent(rest)
        return agent is not None and agent != CODEX_AGENT
    return False


def _python(item: bytes) -> bool:
    return _PYTHON.fullmatch(os.path.basename(item)) is not None


def _entry(argv: list[bytes]) -> tuple[bytes, bytes, list[bytes]] | None:
    """(kind, name, arguments) of a Hub entry at a proven program position.

    Only three forms qualify: the program itself, an interpreter running the
    program as its only option (the shebang form), or ``python -m module``.
    An interpreter option or wrapper in front of it is not parsed.
    """
    if argv and not _python(argv[0]):
        return b"program", os.path.basename(argv[0]), argv[1:]
    if len(argv) >= 2 and not argv[1].startswith(b"-"):
        return b"program", os.path.basename(argv[1]), argv[2:]
    if len(argv) >= 3 and argv[1] == b"-m":
        return b"module", argv[2], argv[3:]
    return None


def may_consume_codex_jobs(argv: list[bytes]) -> bool:
    """True unless the process is unrelated to the Hub or a known non-consumer.

    A Hub mention without a recognised entry at a proven program position
    counts as a consumer.
    """
    if not _hub_related(argv):
        return False
    entry = _entry(argv)
    if entry is None:
        return True
    kind, name, arguments = entry
    if kind == b"module":
        if name == SERVER_MODULE:
            return False
        return name not in CLI_MODULES or not _safe_command(arguments)
    if name in HUB_HELPERS:
        return False
    return name not in HUB_SCRIPTS or not _safe_command(arguments)
