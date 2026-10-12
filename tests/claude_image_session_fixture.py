"""Explicit pinned native image/resume proof; no live route or approval evidence."""

from __future__ import annotations

import json
import socket
import tempfile
from pathlib import Path
from typing import Any

from hermes_codex_router import claude_stream
from tests.claude_native_transport_fixture import (
    NativeTransportFixtureError,
    _copy_native_binary,
    build_native_fixture_argv,
    validate_native_identity,
)
from tests.native_process_capture import capture_owned_process
from tests.native_runtime_mounts import native_runtime_mounts


def build_image_fixture_argv(cwd: Path) -> tuple[str, ...]:
    argv = list(build_native_fixture_argv(cwd))
    argv.remove("--no-session-persistence")
    return tuple(argv[: argv.index("--")] + ["--input-format", "stream-json"])


def validate_image_evidence(report: object) -> dict[str, Any]:
    counts = {
        "connections",
        "timeouts",
        "requests",
        "heads",
        "posts",
        "messages_served",
        "violations",
        "validated_requests",
        "missing_exit_code",
        "missing_requests",
        "missing_heads",
    }
    flags = {
        "host_files_hidden",
        "host_loopback_blocked",
        "ports_distinct",
        "missing_no_messages",
        "missing_refusal_validated",
        "missing_store_unchanged",
    }
    if (
        not isinstance(report, dict)
        or set(report) != counts | flags | {"native_version", "completed"}
        or any(type(report[key]) is not int for key in counts)
        or any(report[key] is not True for key in flags)
        or not isinstance(report["completed"], list)
        or len(report["completed"]) != 2
        or any(item is not True for item in report["completed"])
        or report["native_version"] != "2.1.285 (Claude Code)"
        or report["posts"] != 2
        or report["validated_requests"] != 2
        or report["messages_served"] != 2
        or not 0 <= report["heads"] <= 3
        or not 0 <= report["missing_heads"] <= 1
        or report["missing_requests"] != report["missing_heads"]
        or not 0 <= report["heads"] - report["missing_heads"] <= 2
        or report["requests"] != 2 + report["heads"]
        or not 2 + report["missing_heads"] <= report["connections"] <= report["requests"]
        or report["timeouts"] != 0
        or report["violations"] != 0
        or not 0 <= report["missing_exit_code"] <= 255
    ):
        if isinstance(report, dict) and set(report) == counts | flags | {
            "native_version",
            "completed",
        }:
            failed_flags = sorted(key for key in flags if report[key] is not True)
            if failed_flags:
                raise NativeTransportFixtureError(
                    "native_image_evidence_unproven:" + failed_flags[0]
                )
        raise NativeTransportFixtureError("native_image_evidence_unproven")
    return report


def validate_worker_image_evidence(report: object) -> dict[str, Any]:
    if (
        not isinstance(report, dict)
        or set(report) != {"base", "production_input", "maximum_input", "corrupt_outcomes"}
        or report["production_input"] is not True
        or report["maximum_input"] is not True
        or not isinstance(report["corrupt_outcomes"], list)
        or len(report["corrupt_outcomes"]) != 3
        or any(
            not isinstance(item, str) or item != "guarded_downgrade"
            for item in report["corrupt_outcomes"]
        )
    ):
        raise NativeTransportFixtureError("native_image_evidence_unproven")
    return {
        **validate_image_evidence(report["base"]),
        "production_input": True,
        "maximum_input": True,
        "corrupt_outcomes": report["corrupt_outcomes"],
    }


def run_image_session_fixture(
    executable: Path, *, expected_sha256: str, expected_version: str, worker_input: bool = False
) -> dict[str, Any]:
    if type(worker_input) is not bool:
        raise NativeTransportFixtureError("native_image_evidence_unproven")
    validate_native_identity(expected_sha256, expected_version, None, None)
    if expected_version != "2.1.285 (Claude Code)":
        raise NativeTransportFixtureError("native_identity_mismatch")
    with tempfile.TemporaryDirectory(prefix="example-claude-image-") as directory:
        base = Path(directory)
        binary = base / "claude"
        digest = _copy_native_binary(executable, binary)
        if digest != expected_sha256:
            raise NativeTransportFixtureError("native_identity_mismatch")
        sentinel = base / "fictional-authority"
        sentinel.write_text("example-only", encoding="utf-8")
        sentinel.chmod(0o600)
        if sentinel.read_text(encoding="utf-8") != "example-only":
            raise NativeTransportFixtureError("host_file_positive_control_failed")
        empty = base / "empty.py"
        empty.write_bytes(b"")
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
                "/usr/bin/bwrap",
                "--die-with-parent",
                "--new-session",
                "--unshare-net",
                "--unshare-pid",
                "--unshare-ipc",
                *native_runtime_mounts(),
                "--tmpfs",
                "/tmp",
            ]
            for path in (
                "/home/example/.claude",
                "/home/example/.config",
                "/home/example/.cache",
                "/home/example/.local/share",
                "/home/example/.local/state",
                "/workspace/example",
            ):
                argv.extend(("--dir", path))
            argv.extend(("--ro-bind", str(binary), "/opt/example/claude"))
            for name in (
                "claude_image_session_actor",
                "claude_image_request_contract",
                "claude_native_request_contract",
                "claude_native_transport_actor",
                "native_process_capture",
                *(("claude_image_worker_actor",) if worker_input else ()),
            ):
                argv.extend(
                    (
                        "--ro-bind",
                        str(Path(__file__).with_name(name + ".py")),
                        "/opt/example/tests/" + name + ".py",
                    )
                )
            for source, destination in (
                (empty, "/opt/example/tests/__init__.py"),
                (empty, "/opt/example/hermes_codex_router/__init__.py"),
                (Path(claude_stream.__file__), "/opt/example/hermes_codex_router/claude_stream.py"),
            ):
                argv.extend(("--ro-bind", str(source), destination))
            if worker_input:
                argv.extend(
                    (
                        "--ro-bind",
                        str(Path(claude_stream.__file__).parent),
                        "/opt/example/hermes_codex_router",
                    )
                )
            argv.extend(
                (
                    "--proc",
                    "/proc",
                    "--dev",
                    "/dev",
                    "--chdir",
                    "/workspace/example",
                    "--",
                    "/usr/bin/python3",
                    "-I",
                    "/opt/example/tests/"
                    + (
                        "claude_image_worker_actor.py"
                        if worker_input
                        else "claude_image_session_actor.py"
                    ),
                    str(port),
                    str(sentinel),
                    json.dumps(build_image_fixture_argv(base)),
                )
            )
            code, output = capture_owned_process(
                argv,
                {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                timeout=410 if worker_input else 130,
                stdout_limit=8192,
                stderr_limit=65536,
            )
        try:
            report = json.loads(output)
        except (ValueError, RecursionError):
            raise NativeTransportFixtureError("native_image_evidence_unproven") from None
        if code != 0:
            stage = report.get("stage") if isinstance(report, dict) else None
            from tests.claude_image_session_actor import STAGES

            suffix = ":" + stage if isinstance(stage, str) and stage in STAGES else ""
            raise NativeTransportFixtureError("native_image_execution_failed" + suffix)
        if worker_input:
            validated = validate_worker_image_evidence(report)
        else:
            validated = validate_image_evidence(report)
        validate_native_identity(
            digest, validated["native_version"], expected_sha256, expected_version
        )
        return {
            **validated,
            "native_binary_sha256": digest,
        }
