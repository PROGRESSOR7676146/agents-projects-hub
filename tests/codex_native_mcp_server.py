"""One fixed fictional MCP probe; mount this standalone script read-only."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SERVER = "example-custody"
SERVER_PATH = "/opt/example-native/mcp_server.py"
NONCE_PATTERN = r"example-nonce-[0-9]{1,8}"
PROBE_KEYS = {
    "project_read",
    "project_write",
    "authority_read",
    "authority_symlink_read",
    "authority_write",
    "git_write",
}
PROBE_SOURCE = """
import json,pathlib
p=pathlib.Path.cwd(); key=p.parent/'example-authority.key'; result={}
for name,path in [('project_read',p/'visible'),('authority_read',key),
                  ('authority_symlink_read',p/'private-link')]:
    try: path.read_bytes(); result[name]=True
    except OSError: result[name]=False
for name,path in [('project_write',p/'example-write'),('authority_write',key),
                  ('git_write',p/'.git'/'HEAD')]:
    try: path.write_text('example mutation'); result[name]=True
    except OSError: result[name]=False
print(json.dumps(result))
"""


def valid_nonce(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(NONCE_PATTERN, value) is not None


def decode_json(value: str):
    def unique_object(pairs):
        result = {}
        for key, field in pairs:
            if key in result:
                raise ValueError("duplicate fictional JSON key")
            result[key] = field
        return result

    return json.loads(value, object_pairs_hook=unique_object)


def trusted_root() -> Path:
    declared = os.environ.get("EXAMPLE_MCP_PROJECT", "")
    root = Path(declared)
    if (
        not root.is_absolute()
        or root.name != "example-project"
        or root.resolve(strict=True) != root
        or Path.cwd() != root
        or not (root / ".git").is_dir()
        or not (root / "visible").is_file()
        or not (root.parent / "example-authority.key").is_file()
        or (root / "private-link").resolve(strict=True) != root.parent / "example-authority.key"
    ):
        raise RuntimeError("fictional MCP root is invalid")
    return root


def _direct_probe(root: Path) -> dict[str, bool]:
    result = {}
    authority = root.parent / "example-authority.key"
    for name, path in (
        ("project_read", root / "visible"),
        ("authority_read", authority),
        ("authority_symlink_read", root / "private-link"),
    ):
        try:
            path.read_bytes()
            result[name] = True
        except OSError:
            result[name] = False
    for name, path in (
        ("project_write", root / "example-write"),
        ("authority_write", authority),
        ("git_write", root / ".git" / "HEAD"),
    ):
        try:
            path.write_text("example mutation")
            result[name] = True
        except OSError:
            result[name] = False
    return result


def probe(root: Path, nonce: str) -> dict:
    if not valid_nonce(nonce):
        raise ValueError("invalid fictional nonce")
    direct = _direct_probe(root)
    child = subprocess.run(
        ["/usr/bin/python3", "-I", "-c", PROBE_SOURCE],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    result = json.loads(child.stdout)
    if not isinstance(result, dict) or set(result) != PROBE_KEYS:
        raise RuntimeError("fictional child probe is invalid")
    if not all(type(value) is bool for value in result.values()):
        raise RuntimeError("fictional child probe is invalid")
    return {"version": 1, "nonce": nonce, "direct": direct, "child": result}


def dispatch(message: dict, root: Path | None) -> dict | None:
    identifier, method = message.get("id"), message.get("method")
    if identifier is None and method == "notifications/initialized":
        return None
    reply = {"jsonrpc": "2.0", "id": identifier}
    if method == "initialize":
        version = message.get("params", {}).get("protocolVersion")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18"):
            return {**reply, "error": {"code": -32602, "message": "unsupported protocol"}}
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER, "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "probe",
                    "description": "Fixed fictional filesystem custody diagnostic.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"nonce": {"type": "string", "pattern": NONCE_PATTERN}},
                        "required": ["nonce"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
    elif method in ("resources/list", "resources/templates/list"):
        result = {"resources" if method == "resources/list" else "resourceTemplates": []}
    elif method == "tools/call":
        params = message.get("params", {})
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if (
            not isinstance(params, dict)
            or params.get("name") != "probe"
            or not isinstance(arguments, dict)
            or set(arguments) != {"nonce"}
            or not valid_nonce(arguments["nonce"])
        ):
            return {**reply, "error": {"code": -32602, "message": "invalid fixed probe call"}}
        if root is None:
            raise RuntimeError("fictional MCP root is unavailable")
        value = probe(root, arguments["nonce"])
        result = {
            "content": [{"type": "text", "text": json.dumps(value)}],
            "structuredContent": value,
            "isError": False,
        }
    else:
        return {**reply, "error": {"code": -32601, "message": "unsupported fixture method"}}
    return {**reply, "result": result}


def main() -> None:
    root = trusted_root()
    while line := sys.stdin.readline(65537):
        if len(line) > 65536:
            raise RuntimeError("fictional MCP input exceeds bound")
        message = decode_json(line)
        if not isinstance(message, dict):
            raise RuntimeError("fictional MCP input is invalid")
        response = dispatch(message, root)
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
