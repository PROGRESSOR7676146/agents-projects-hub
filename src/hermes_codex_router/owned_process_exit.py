"""Observe an owned child's exit without releasing its PID reservation."""

from __future__ import annotations

import os
import subprocess


def peek_exit_code(process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> int | None:
    """Keep the leader waitable until its process group has been cleaned up."""
    if process.returncode is not None:
        return process.returncode
    outcome = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    if outcome is None:
        return None
    return outcome.si_status if outcome.si_code == os.CLD_EXITED else -outcome.si_status
