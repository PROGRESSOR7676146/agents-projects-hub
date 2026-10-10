"""Explicit opt-in native CLI compatibility; no provider/account inference."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import tempfile
from pathlib import Path
from typing import Any, NoReturn

from hermes_codex_router import claude_stream
from tests.claude_native_request_contract import CAPSULE_BYTES, PROMPT, SYSTEM_PROMPT
from tests.claude_native_transport_actor import (
    CASES,
    FAILURE_CATEGORIES,
    MODEL,
    NATIVE_SESSION_ID,
    STAGES,
)
from tests.native_process_capture import NativeCaptureError, capture_owned_process
from tests.native_runtime_mounts import native_runtime_mounts


class NativeTransportFixtureError(RuntimeError):
    """A bounded fixed-code compatibility failure, never native raw output."""


def _copy_native_binary(source: Path, destination: Path) -> str:
    source = source.resolve(strict=True)
    # Refuse special files before open: even a nonblocking device read may have
    # side effects. Descriptor and named-file checks still own the copy race.
    if not stat.S_ISREG(source.stat().st_mode):
        raise NativeTransportFixtureError("explicit_standalone_native_binary_required")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not before.st_mode & 0o111
            or not 4 <= before.st_size <= 512 * 1024 * 1024
            or os.read(descriptor, 4) != b"\x7fELF"
        ):
            raise NativeTransportFixtureError("explicit_standalone_native_binary_required")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        copied = 0
        with destination.open("xb") as output:
            while chunk := os.read(descriptor, 1024 * 1024):
                copied += len(chunk)
                if copied > before.st_size:
                    raise NativeTransportFixtureError("native_binary_changed_during_copy")
                output.write(chunk)
                digest.update(chunk)
        after = os.fstat(descriptor)
        named = source.stat()
        fields = ("st_dev", "st_ino", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns")
        if copied != before.st_size or any(
            getattr(before, field) != getattr(candidate, field)
            for candidate in (after, named)
            for field in fields
        ):
            raise NativeTransportFixtureError("native_binary_changed_during_copy")
        destination.chmod(0o500)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def build_native_fixture_argv(cwd: Path) -> tuple[str, ...]:
    from hermes_codex_router.external_runtime import ExternalCliAdapter

    argv = list(
        ExternalCliAdapter("claude", executable="/opt/example/claude").build_argv(
            cwd=cwd,
            prompt=PROMPT,
            model=MODEL,
            effort="high",
            new_session_id=NATIVE_SESSION_ID,
        )
    )
    settings_index = argv.index("--settings") + 1
    settings = json.loads(argv[settings_index])
    # Explicit fixture-only bounds. These are not production retry or billing
    # guarantees; every production text-only flag/settings value is inherited.
    settings.update(switchModelsOnFlag=False, fallbackModel=[])
    argv[settings_index] = json.dumps(settings, separators=(",", ":"))
    separator = argv.index("--")
    argv[separator:separator] = [
        "--no-session-persistence",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--setting-sources",
        "",
        "--max-turns",
        "1",
        "--system-prompt",
        SYSTEM_PROMPT,
    ]
    return tuple(argv)


def validate_native_identity(
    digest: str, version: object, expected_sha256: str | None, expected_version: str | None
) -> None:
    if (
        not re.fullmatch(r"[0-9a-f]{64}", digest)
        or not isinstance(version, str)
        or len(version) > 64
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)", version)
    ):
        raise NativeTransportFixtureError("native_identity_shape_invalid")
    if (expected_sha256 is not None and digest != expected_sha256) or (
        expected_version is not None and version != expected_version
    ):
        raise NativeTransportFixtureError("native_identity_mismatch")


def validate_transport_evidence(report: object, case: str) -> dict[str, Any]:
    if case not in CASES or not isinstance(report, dict):
        raise NativeTransportFixtureError("native_evidence_shape_invalid")
    if report.get("case") != case or not all(
        report.get(key) is True
        for key in ("host_files_hidden", "host_loopback_blocked", "ports_distinct")
    ):
        raise NativeTransportFixtureError("native_evidence_isolation_unproven")
    if (
        type(report.get("posts")) is not int
        or report["posts"] != 1
        or type(report.get("messages_served")) is not int
        or report["messages_served"] != 1
        or type(report.get("heads")) is not int
        or not 0 <= report["heads"] <= 1
        or type(report.get("requests")) is not int
        or report["requests"] != report["posts"] + report["heads"]
        or type(report.get("violations")) is not int
        or report["violations"] != 0
        or type(report.get("connections")) is not int
        or not 1 <= report["connections"] <= report["requests"]
        or type(report.get("timeouts")) is not int
        or report["timeouts"] != 0
    ):
        raise NativeTransportFixtureError("native_request_surface_unproven")
    contract = report.get("request_contract")
    if (
        not isinstance(contract, dict)
        or set(contract) != {"validated_requests", "selected_capsule_sha256"}
        or type(contract.get("validated_requests")) is not int
        or contract["validated_requests"] != 1
        or contract.get("selected_capsule_sha256") != hashlib.sha256(CAPSULE_BYTES).hexdigest()
    ):
        raise NativeTransportFixtureError("native_request_contract_unproven")
    rejected = case.endswith("reject")
    if type(report.get("exit_code")) is not int or any(
        type(report.get(key)) is not bool for key in ("terminal_success", "terminal_failure")
    ):
        raise NativeTransportFixtureError("native_terminal_shape_invalid")
    shape = report.get("terminal_shape")
    python_version = report.get("parser_python_version")
    if (
        not isinstance(shape, dict)
        or set(shape)
        != {
            "subtype",
            "is_error",
            "error_is_boolean",
            "api_error_status",
            "result_is_text",
            "assistant_error_present",
            "latest_assistant_error",
            "errors_is_list",
        }
        or type(shape.get("errors_is_list")) is not bool
        or (
            shape.get("latest_assistant_error") is not None
            and (
                not isinstance(shape["latest_assistant_error"], str)
                or shape["latest_assistant_error"]
                not in {
                    "overloaded",
                    "unknown",
                    "rate_limit",
                    "authentication_failed",
                    "billing_error",
                    "model_not_found",
                    "other",
                }
            )
        )
        or not isinstance(python_version, list)
        or len(python_version) != 2
        or any(type(part) is not int for part in python_version)
        or python_version[0] != 3
        or not 11 <= python_version[1] <= 99
        or shape.get("subtype") != "success"
        or shape.get("error_is_boolean") is not True
        or shape.get("result_is_text") is not True
        or shape.get("is_error") is not rejected
        or shape.get("assistant_error_present") is not rejected
        or shape.get("assistant_error_present")
        is not (shape.get("latest_assistant_error") is not None)
        or type(report.get("visible_messages")) is not int
        or report["visible_messages"] != (0 if rejected else 1)
        or (
            rejected
            and (type(shape.get("api_error_status")) is not int or shape["api_error_status"] != 529)
        )
        or (not rejected and shape.get("api_error_status") is not None)
    ):
        raise NativeTransportFixtureError("native_terminal_shape_unproven")
    if rejected:
        valid = (
            report["exit_code"] != 0
            and report["terminal_failure"]
            and not report["terminal_success"]
            and report.get("failure_code") == "claude_provider_overloaded"
        )
    else:
        valid = (
            report["exit_code"] == 0
            and report["terminal_success"]
            and not report["terminal_failure"]
            and report.get("failure_code") is None
        )
    if not valid:
        raise NativeTransportFixtureError("native_terminal_outcome_unproven")
    return report


def run_native_transport_case(
    executable: Path,
    case: str,
    *,
    expected_sha256: str | None = None,
    expected_version: str | None = None,
) -> dict[str, Any]:
    return _run_native_case(
        executable, case, expected_sha256=expected_sha256, expected_version=expected_version
    )


def run_native_selection_case(
    executable: Path, case: str, *, expected_sha256: str, expected_version: str
) -> dict[str, Any]:
    validate_native_identity(expected_sha256, expected_version, None, None)
    if expected_version != "2.1.285 (Claude Code)":
        raise NativeTransportFixtureError("native_identity_mismatch")
    return _run_native_case(
        executable,
        case,
        expected_sha256=expected_sha256,
        expected_version=expected_version,
        selection_case=True,
    )


def _run_native_case(
    executable: Path,
    case: str,
    *,
    expected_sha256: str | None,
    expected_version: str | None,
    selection_case: bool = False,
) -> dict[str, Any]:
    allowed_cases = CASES
    if selection_case:
        from tests.claude_selection_contract import CASES as SELECTION_CASES

        allowed_cases = set(SELECTION_CASES)
    if case not in allowed_cases:
        raise NativeTransportFixtureError("native_case_invalid")
    bwrap = Path("/usr/bin/bwrap")
    if not bwrap.is_file():
        raise NativeTransportFixtureError("native_namespace_unavailable")
    with tempfile.TemporaryDirectory(prefix="example-native-claude-") as directory:
        base = Path(directory)
        binary = base / "example-claude"
        digest = _copy_native_binary(executable, binary)
        if expected_sha256 is not None and digest != expected_sha256:
            raise NativeTransportFixtureError("native_identity_mismatch")
        sentinel = base / "fictional-authority"
        sentinel.write_text("example-only", encoding="utf-8")
        sentinel.chmod(0o600)
        if sentinel.read_text(encoding="utf-8") != "example-only":
            raise NativeTransportFixtureError("host_file_positive_control_failed")
        empty = base / "empty.py"
        empty.write_bytes(b"")
        actor = Path(__file__).with_name(
            "claude_selection_actor.py" if selection_case else "claude_native_transport_actor.py"
        )
        capture = Path(__file__).with_name("native_process_capture.py")
        contract = Path(__file__).with_name("claude_native_request_contract.py")
        parser = Path(claude_stream.__file__)
        with socket.socket() as listener:
            listener.settimeout(2)
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port), timeout=2) as control:
                connection, _address = listener.accept()
                with connection:
                    connection.settimeout(2)
                    control.sendall(b"example")
                    if connection.recv(7) != b"example":
                        raise NativeTransportFixtureError("host_network_positive_control_failed")
                    connection.sendall(b"ok")
                    if control.recv(2) != b"ok":
                        raise NativeTransportFixtureError("host_network_positive_control_failed")
            argv = [
                str(bwrap),
                "--die-with-parent",
                "--new-session",
                "--unshare-net",
                "--unshare-pid",
                "--unshare-ipc",
                *native_runtime_mounts(),
                "--tmpfs",
                "/tmp",
                "--dir",
                "/home/example/.claude",
                "--dir",
                "/home/example/.config",
                "--dir",
                "/home/example/.cache",
                "--dir",
                "/home/example/.local/share",
                "--dir",
                "/home/example/.local/state",
                "--dir",
                "/workspace/example",
                "--ro-bind",
                str(binary),
                "/opt/example/claude",
                "--ro-bind",
                str(actor),
                "/opt/example/actor.py",
                "--ro-bind",
                str(capture),
                "/opt/example/tests/native_process_capture.py",
                "--ro-bind",
                str(contract),
                "/opt/example/tests/claude_native_request_contract.py",
                "--ro-bind",
                str(empty),
                "/opt/example/tests/__init__.py",
                "--ro-bind",
                str(parser),
                "/opt/example/hermes_codex_router/claude_stream.py",
                "--ro-bind",
                str(empty),
                "/opt/example/hermes_codex_router/__init__.py",
            ]
            if selection_case:
                for name in (
                    "claude_native_transport_actor",
                    "claude_selection_contract",
                    "claude_image_request_contract",
                ):
                    argv.extend(
                        (
                            "--ro-bind",
                            str(Path(__file__).with_name(name + ".py")),
                            "/opt/example/tests/" + name + ".py",
                        )
                    )
            argv.extend(
                [
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                    "--chdir",
                    "/workspace/example",
                    "--",
                    "/usr/bin/python3",
                    "-I",
                    "/opt/example/actor.py",
                    str(port),
                    str(sentinel),
                    case,
                    json.dumps(
                        build_selection_fixture_argv(base, case)
                        if selection_case
                        else build_native_fixture_argv(base)
                    ),
                ]
            )
            try:
                code, output = capture_owned_process(
                    argv,
                    {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                    timeout=130 if selection_case else 105,
                    stdout_limit=8192,
                    stderr_limit=65536,
                )
            except NativeCaptureError as error:
                raise NativeTransportFixtureError("native_namespace_capture_failed") from error
            if code != 0:
                try:
                    failure = json.loads(output)
                except (ValueError, RecursionError):
                    failure = None
                if (
                    isinstance(failure, dict)
                    and failure.get("fixture_failed") is True
                    and failure.get("stage") in STAGES
                    and failure.get("category") in FAILURE_CATEGORIES
                ):
                    raise NativeTransportFixtureError(
                        "native_namespace_execution_failed:"
                        + failure["stage"]
                        + ":"
                        + failure["category"]
                    )
                if selection_case:
                    from tests.claude_selection_actor import STAGES as SELECTION_STAGES

                    if (
                        isinstance(failure, dict)
                        and failure.get("fixture_failed") is True
                        and failure.get("stage") in SELECTION_STAGES
                    ):
                        raise NativeTransportFixtureError(
                            "native_selection_execution_failed:" + failure["stage"]
                        )
                raise NativeTransportFixtureError("native_namespace_execution_failed")
            try:
                report = json.loads(output)
            except (ValueError, RecursionError) as error:
                raise NativeTransportFixtureError("native_evidence_shape_invalid") from error
            validated = (
                validate_selection_evidence(report, case)
                if selection_case
                else validate_transport_evidence(report, case)
            )
            validate_native_identity(
                digest, validated.get("native_version"), expected_sha256, expected_version
            )
            validated["native_binary_sha256"] = digest
            return validated


def build_selection_fixture_argv(cwd: Path, case: str) -> tuple[tuple[str, ...], ...]:
    from hermes_codex_router.external_runtime import ExternalCliAdapter
    from tests.claude_image_request_contract import MISSING_SESSION_ID
    from tests.claude_selection_contract import PROMPTS, selection

    invocations = []
    for phase in (0, 1, 2):
        model, effort = selection(case, min(phase, 1))
        argv = list(
            ExternalCliAdapter("claude", executable="/opt/example/claude").build_argv(
                cwd=cwd,
                prompt=PROMPTS[min(phase, 1)],
                model=model,
                effort=effort,
                new_session_id=NATIVE_SESSION_ID if phase == 0 else None,
                session_id=(NATIVE_SESSION_ID if phase == 1 else MISSING_SESSION_ID)
                if phase
                else None,
            )
        )
        index = argv.index("--settings") + 1
        settings = json.loads(argv[index])
        settings.update(switchModelsOnFlag=False, fallbackModel=[])
        argv[index] = json.dumps(settings, separators=(",", ":"))
        separator = argv.index("--")
        argv[separator:separator] = [
            "--mcp-config",
            '{"mcpServers":{}}',
            "--setting-sources",
            "",
            "--max-turns",
            "1",
            "--system-prompt",
            SYSTEM_PROMPT,
        ]
        invocations.append(tuple(argv))
    return tuple(invocations)


def validate_selection_evidence(report: object, case: str) -> dict[str, Any]:
    from tests.claude_selection_actor import COUNTERS
    from tests.claude_selection_contract import CASES as SELECTION_CASES
    from tests.claude_selection_contract import selection

    def invalid() -> NoReturn:
        raise NativeTransportFixtureError("native_selection_evidence_unproven")

    if (
        case not in SELECTION_CASES
        or not isinstance(report, dict)
        or set(report)
        != {
            "case",
            "native_version",
            "host_files_hidden",
            "host_loopback_blocked",
            "ports_distinct",
            "phases",
            "missing",
        }
    ):
        invalid()
    assert isinstance(report, dict)
    if (
        report["case"] != case
        or report["native_version"] != "2.1.285 (Claude Code)"
        or any(
            report[key] is not True
            for key in ("host_files_hidden", "host_loopback_blocked", "ports_distinct")
        )
    ):
        invalid()
    phases = report["phases"]
    if not isinstance(phases, list) or len(phases) != 2:
        invalid()
    for phase, entry in enumerate(phases):
        model, effort = selection(case, phase)
        if (
            not isinstance(entry, dict)
            or set(entry)
            != set(COUNTERS)
            | {"phase", "model", "effort", "session_id", "completed", "store_exact"}
            or any(type(entry[key]) is not int for key in (*COUNTERS, "phase"))
        ):
            invalid()
        if (
            entry["phase"] != phase
            or entry["model"] != model
            or entry["effort"] != effort
            or entry["session_id"] != NATIVE_SESSION_ID
            or entry["completed"] is not True
            or entry["store_exact"] is not True
        ):
            invalid()
        if (
            any(entry[key] != 1 for key in ("posts", "validated_requests", "messages_served"))
            or not 0 <= entry["heads"] <= 1
            or entry["requests"] != 1 + entry["heads"]
            or not 1 <= entry["connections"] <= entry["requests"]
            or any(entry[key] != 0 for key in ("violations", "timeouts"))
        ):
            invalid()
    missing = report["missing"]
    if (
        not isinstance(missing, dict)
        or set(missing) != set(COUNTERS) | {"refusal_validated", "store_unchanged", "exit_code"}
        or any(type(missing[key]) is not int for key in (*COUNTERS, "exit_code"))
    ):
        invalid()
    if (
        missing["refusal_validated"] is not True
        or missing["store_unchanged"] is not True
        or not 0 <= missing["exit_code"] <= 255
        or any(
            missing[key] != 0
            for key in ("posts", "validated_requests", "messages_served", "violations", "timeouts")
        )
        or not 0 <= missing["heads"] <= 1
        or missing["requests"] != missing["heads"]
        or missing["connections"] != missing["requests"]
    ):
        invalid()
    return report
