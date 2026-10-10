"""Production input owner and false-success guard in a disposable native boundary."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, "/opt/example")

from hermes_codex_router.claude_image_input import VerifiedClaudeImage, encode_claude_image_input
from hermes_codex_router.claude_image_receipt import ClaudeImageReceipt
from hermes_codex_router.claude_stream import ClaudeStreamError, parse_claude_stream
from hermes_codex_router.external_runtime import ExternalCliAdapter
from tests import claude_image_request_contract as contract
from tests import claude_image_session_actor as actor
from tests import claude_native_request_contract as native_contract
from tests.claude_native_request_contract import (
    DUMMY,
    MODEL,
    NATIVE_SESSION_ID,
    PROMPT,
    ExpectedNativeRequest,
    NativeRequestContractError,
    _strict_json,
    environment_text,
    validate_request_body,
)
from tests.native_process_capture import NativeCaptureError

original_capture = actor.capture_owned_process
original_expected_messages = contract.expected_messages
original_receipt_observe = ClaudeImageReceipt.observe
negative_images: tuple[VerifiedClaudeImage, ...] = ()
maximum_input = False
PROCESSED_PNG = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD43ooor9IPgj//2Q=="
PROCESSED_JPEG = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAQDAwMDAgQDAwMEBAQFBgoGBgUFBgwICQcKDgwPDg4MDQ0PERYTDxAVEQ0NExoTFRcYGRkZDxIbHRsYHRYYGRj/2wBDAQQEBAYFBgsGBgsYEA0QGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBj/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD5pooor7Y+TP/Z"
PROCESSED_IMAGES = (PROCESSED_PNG, PROCESSED_JPEG)


def validate_receipt(self, event):
    retain = original_receipt_observe(self, event)
    if event.get("type") == "user" and not negative_images:
        blocks = event["message"]["content"]
        phase = 0 if blocks[0]["text"] == "Example PNG image." else 1
        expected = content(phase)
        if maximum_input:
            for position, image_index in ((2, phase), (4, 1 - phase)):
                expected[position]["source"] = {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": PROCESSED_IMAGES[image_index],
                }
        if blocks != expected:
            raise NativeCaptureError("native_image_processed_receipt_unproven")
    return retain


def content(phase: int) -> list[dict]:
    selected = [
        {"type": "text", "text": "Example " + ("PNG" if phase == 0 else "JPEG") + " image."},
        {"type": "text", "text": "MATERIAL 1 IMAGE"},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png" if phase == 0 else "image/jpeg",
                "data": contract.IMAGE_BASE64[phase],
            },
        },
    ]
    if maximum_input:
        selected.extend(
            (
                {"type": "text", "text": "MATERIAL 2 IMAGE"},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg" if phase == 0 else "image/png",
                        "data": contract.IMAGE_BASE64[1 - phase],
                    },
                },
            )
        )
    return selected


def image(data: bytes, mime: str, position: int = 1) -> VerifiedClaudeImage:
    return VerifiedClaudeImage(position, mime, data, hashlib.sha256(data).hexdigest())


def encoded(phase: int) -> bytes:
    return encode_claude_image_input(
        content(phase)[0]["text"],
        tuple(
            image(
                contract.IMAGE_BYTES[index], "image/png" if index == 0 else "image/jpeg", position
            )
            for position, index in enumerate((phase, 1 - phase) if maximum_input else (phase,), 1)
        ),
        NATIVE_SESSION_ID,
    )


def expected_messages(expected: ExpectedNativeRequest, phase: int, *, uid: int) -> list[dict]:
    messages = original_expected_messages(expected, phase, uid=uid)
    if maximum_input:
        for index, message in enumerate(item for item in messages if item["role"] == "user"):
            for position, image_index in ((2, index), (4, 1 - index)):
                message["content"][position]["source"] = {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": PROCESSED_IMAGES[image_index],
                }
            message["content"].pop()
            message["content"].extend(
                {
                    "type": "text",
                    "text": f"[Image: source: /tmp/claude-{uid}/-workspace-example/{NATIVE_SESSION_ID}/images/{number}.{'png' if image_index == 0 else 'jpg'}]",
                }
                for number, image_index in ((index * 2 + 1, index), (index * 2 + 2, 1 - index))
            )
    return messages


def validate_request(raw: bytes, expected: ExpectedNativeRequest, phase: int, *, uid: int) -> None:
    body = _strict_json(raw)
    if not isinstance(body, dict):
        raise NativeRequestContractError("native_image_request_invalid")
    if not negative_images:
        if body.get("messages") != contract.expected_messages(expected, phase, uid=uid):
            raise NativeRequestContractError("native_image_request_invalid")
        for index, message in enumerate(
            item for item in body["messages"] if item["role"] == "user"
        ):
            source = message["content"][2]["source"]
            if (
                not maximum_input
                and base64.b64decode(source["data"], validate=True) != contract.IMAGE_BYTES[index]
            ):
                raise NativeRequestContractError("native_image_bytes_invalid")
    else:
        # Deliberately return success after a proved downgrade. A rejecting
        # endpoint cannot establish that production rejects false success.
        messages = body.get("messages")
        if not isinstance(messages, list) or len(messages) != 2:
            raise NativeRequestContractError("native_image_request_invalid")
        blocks = messages[0].get("content")
        if (
            messages[0].get("role") != "user"
            or not isinstance(blocks, list)
            or blocks[0] != {"type": "text", "text": "Example corrupt image."}
        ):
            raise NativeRequestContractError("native_image_request_invalid")
        sources = [block["source"] for block in blocks if block.get("type") == "image"]
        supplied = [
            (part.media_type, base64.b64encode(part.data).decode()) for part in negative_images
        ]
        actual = [(source.get("media_type"), source.get("data")) for source in sources]
        if actual == supplied or any(item not in supplied for item in actual):
            raise NativeRequestContractError("native_image_downgrade_unproven")
        if messages[1] != contract.expected_messages(expected, 0, uid=uid)[1]:
            raise NativeRequestContractError("native_image_request_invalid")
    # Full model/system/tools/metadata contract remains independently enforced.
    body["messages"] = [
        {"role": "user", "content": PROMPT},
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": expected.environment,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "output_config": {"effort": "high"},
        },
    ]
    validate_request_body(json.dumps(body).encode(), expected)


def owned_capture(argv, environment, *, stdin_data=None, on_stdout=None, **options):
    if stdin_data is None:
        return original_capture(argv, environment, on_stdout=on_stdout, **options)
    flag = "--resume" if "--resume" in argv else "--session-id"
    adapter = ExternalCliAdapter(
        "claude",
        executable="/opt/example/claude",
        opencode_log_path=Path("/tmp/example-unused-log"),
    )
    argv = (
        (*argv, "--replay-user-messages") if "--replay-user-messages" not in argv else tuple(argv)
    )
    result = adapter._run_owned_claude_turn(
        argv,
        cwd=Path("/workspace/example"),
        environment=environment,
        timeout=options["timeout"],
        expected_session_id=argv[argv.index(flag) + 1],
        on_visible_assistant=None,
        on_process_started=None,
        sandbox=None,
        input_data=stdin_data,
    )
    if on_stdout is not None:
        on_stdout(result.stdout.encode())
    return result.returncode, result.stdout.encode()


def environment_for(home: Path) -> dict[str, str]:
    home.mkdir()
    return {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "ANTHROPIC_API_KEY": DUMMY,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1024",
    }


def corrupt_case(argv: list[str], expected: ExpectedNativeRequest, index: int) -> str:
    frame = encode_claude_image_input("Example corrupt image.", negative_images, NATIVE_SESSION_ID)
    for guarded in (False, True):
        env = environment_for(
            Path(f"/home/example/corrupt-{index}-" + ("guard" if guarded else "reference"))
        )
        with actor.image_endpoint(int(sys.argv[1]), 0) as server:
            server.request_contract = expected
            env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:" + str(server.server_port)
            if guarded:
                try:
                    owned_capture(argv, env, stdin_data=frame, timeout=40)
                except ClaudeStreamError as error:
                    if str(error) != "claude image input was replaced or omitted":
                        raise NativeCaptureError("native_corrupt_image_guard_unproven") from None
                else:
                    raise NativeCaptureError("native_corrupt_image_guard_unproven")
            else:
                code, output = original_capture(
                    argv,
                    env,
                    stdin_data=frame,
                    timeout=40,
                    stdout_limit=4 * 1024 * 1024,
                    stderr_limit=65536,
                )
                events = [json.loads(line) for line in output.splitlines() if line.strip()]
                acks = [event for event in events if event.get("type") == "user"]
                if (
                    len(acks) != 1
                    or acks[0].get("isReplay") is not True
                    or acks[0].get("uuid") != json.loads(frame)["uuid"]
                    or acks[0].get("message", {}).get("content")
                    == json.loads(frame)["message"]["content"]
                ):
                    raise NativeCaptureError("native_corrupt_image_receipt_unproven")
                retained = "\n".join(
                    json.dumps(event)
                    for event in events
                    if event.get("type") not in {"user", "command_lifecycle"}
                )
                result = parse_claude_stream(
                    retained,
                    expected_session_id=NATIVE_SESSION_ID,
                    requested_model=MODEL,
                    returncode=code,
                )
                if (
                    result.text != contract.MARKERS[0]
                    or server.posts != server.validated_requests
                    or server.posts != 1
                    or server.messages_served != 1
                ):
                    raise NativeCaptureError("native_corrupt_image_false_success_unproven")
        if server.violations != 0 or server.timeouts != 0:
            raise NativeCaptureError("native_corrupt_image_unproven")
    return "guarded_downgrade"


def maximum_case(argv: list[str], expected: ExpectedNativeRequest) -> None:
    global maximum_input
    original_bytes, original_base64 = contract.IMAGE_BYTES, contract.IMAGE_BASE64
    contract.IMAGE_BYTES = tuple(
        data + b"\x00" * (2 * 1024 * 1024 - len(data)) for data in original_bytes
    )
    contract.IMAGE_BASE64 = tuple(base64.b64encode(data).decode() for data in contract.IMAGE_BYTES)
    native_contract.MAX_REQUEST_BYTES = 16 * 1024 * 1024
    native_contract.MAX_JSON_NODES = 512
    cache = Path(f"/tmp/claude-{os.getuid()}/-workspace-example/{NATIVE_SESSION_ID}/images")
    if cache.exists():
        shutil.rmtree(cache)
    maximum_input = True
    env = environment_for(Path("/home/example/maximum"))
    for phase in (0, 1):
        args = list(argv)
        if phase:
            start = args.index("--session-id")
            args[start : start + 2] = ["--resume", NATIVE_SESSION_ID]
        with actor.image_endpoint(int(sys.argv[1]), phase) as server:
            server.request_contract = expected
            env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:" + str(server.server_port)
            code, raw = owned_capture(args, env, stdin_data=encoded(phase), timeout=40)
            result = parse_claude_stream(
                raw.decode(),
                expected_session_id=NATIVE_SESSION_ID,
                requested_model=MODEL,
                returncode=code,
            )
            if result.text != contract.MARKERS[phase]:
                raise NativeCaptureError("native_maximum_image_unproven")
        if (
            server.posts != 1
            or server.validated_requests != 1
            or server.messages_served != 1
            or server.violations != 0
            or server.timeouts != 0
        ):
            raise NativeCaptureError("native_maximum_image_unproven")
    maximum_input = False
    contract.IMAGE_BYTES, contract.IMAGE_BASE64 = original_bytes, original_base64


def main() -> None:
    global negative_images
    actor.input_message = encoded
    actor.validate_image_request = validate_request
    contract.selected_content = content
    contract.expected_messages = expected_messages
    actor.capture_owned_process = owned_capture
    ClaudeImageReceipt.observe = validate_receipt
    captured = io.StringIO()
    with redirect_stdout(captured):
        actor.main()
    base = json.loads(captured.getvalue())
    argv = json.loads(sys.argv[3]) + ["--replay-user-messages"]
    expected = ExpectedNativeRequest(
        "2.1.285",
        environment_text(
            "Linux " + os.uname().release, datetime.now(timezone.utc).date().isoformat()
        ),
    )
    maximum_case(argv, expected)
    cases = (
        (image(b"\x89PNG\r\n\x1a\n", "image/png"),),
        (image(b"\xff\xd8\xff", "image/jpeg"),),
        (image(contract.IMAGE_BYTES[0], "image/png"), image(b"\xff\xd8\xff", "image/jpeg", 2)),
    )
    outcomes = []
    for index, parts in enumerate(cases):
        negative_images = parts
        outcomes.append(corrupt_case(argv, expected, index))
    print(
        json.dumps(
            {
                "base": base,
                "production_input": True,
                "maximum_input": True,
                "corrupt_outcomes": outcomes,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "stage": actor.stage if actor.stage in actor.STAGES else "complete",
                    "error": type(error).__name__,
                    "code": str(error)
                    if isinstance(error, (ClaudeStreamError, NativeCaptureError))
                    else None,
                }
            ),
            flush=True,
        )
        sys.exit(1)
