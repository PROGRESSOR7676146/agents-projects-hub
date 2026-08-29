from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_PRIVATE_FINGERPRINTS = frozenset(
    {
        "117a221e6475ac8305cfc58c3cfdc427acd2469f30677314074fac386a30e137",
        "12132cd6767ee325d35883d25c0b7f5e1d142d60d33c563c39cea29984dcea57",
        "1adb4d0daf119b8bdeccf6a0aaac88f5f33f6d50a0cf7b5408eeae5970af9f1a",
        "2208160c7714587b08250b5ee7b6ade3f2fd22fa29e5d1f81088918b797ceb77",
        "2799b9f3ec30d9608c13ce203bba78d9a1b8b590db36da3898e0f670ee5fb02e",
        "2982f730073b8f76952ccf1b7265f1a5ae8ec99fa88e360027ec408010cf19a8",
        "46092676d014e923beb0de574dd930f937cc129cfc504a6425e8d7e6bd8bc88f",
        "4c754d5e0794a13718a3638259b02e17b12be51d88ea808e8289518801ee49ef",
        "5fca82adbdb6eeb5bbbabe1e880e7eaf93f4c366b55cc26a6bcf4c0c71741d9a",
        "7e92f3fecc9aa3fb3238ba89fabaf521a6d077832c5d4810bead794181d06777",
        "8eb9e90347b4ed5b1a356a5086cc56a1759b5ac98ad048123788d8fb81b56aaf",
        "c5b9897106bcc4c3edfaf00bbe1d03827dcea21fc766e93dd31a75693235a89a",
        "ceb3870733bb6affa15d4460afaeca27dac0b0888e120faca2b1f28148d7d0c2",
        "d45e337a0119d87b0df36278b87396b1e7ef037d02d26428e79fd8713796f99f",
        "dff299d07135e30e6983372d1cc1a58c24525d345159929cda4b00adf6fa4a69",
    }
)

_PLACEHOLDER_CHAT_IDS = frozenset(
    {
        "-1000000000001",
        "-1001111111111",
        "-1001234567890",
        "-1002222222222",
        "-1009999999999",
    }
)

_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])([\w.+-]+)@([\w.-]+\.[a-z]{2,})")
_HOME_RE = re.compile(r"/(?:home|Users)/([^/\s'\"`]+)")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?<![a-z])[a-z]:[\\/]")
_INVITE_RE = re.compile(r"(?i)(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)")
_BOT_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")
_SECRET_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|AIza[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9_-]{20,}|"
    r"\d{8,10}[A-Z]{2,}[A-Za-z0-9_-]{20,})\b"
)
_CHAT_ID_RE = re.compile(r"(?<!\d)-100\d{7,}(?!\d)")
_BOT_USERNAME_RE = re.compile(r"(?i)@[a-z0-9_]{5,}bot\b")
_UUID_RE = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_TOKEN_RE = re.compile(r"[\w@.+-]{3,}", re.UNICODE)
_RAW_SESSION_RES = (
    re.compile("<environment" + "_context>"),
    re.compile("<user" + "_action>"),
    re.compile("<permissions " + "instructions>"),
    re.compile(r"(?m)^## \d+\. (?:User|Assistant)\s+·"),
)


@dataclass(frozen=True, slots=True)
class PrivacyFinding:
    path: Path
    line: int
    rule: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _finding(path: Path, text: str, offset: int, rule: str) -> PrivacyFinding:
    return PrivacyFinding(path, _line_number(text, offset), rule)


def scan_text(path: Path, text: str) -> list[PrivacyFinding]:
    findings: list[PrivacyFinding] = []

    for match in _EMAIL_RE.finditer(text):
        domain = match.group(2).casefold()
        if domain not in {"example.com", "example.org", "example.net"} and not domain.endswith(
            ".service"
        ):
            findings.append(_finding(path, text, match.start(), "non-example email address"))

    for match in _HOME_RE.finditer(text):
        if match.group(1).casefold() not in {"example", "user"}:
            findings.append(_finding(path, text, match.start(), "owner-specific home path"))

    pattern_rules = (
        (_WINDOWS_PATH_RE, "absolute Windows path"),
        (_INVITE_RE, "private Telegram invite link"),
        (_BOT_TOKEN_RE, "Telegram bot token"),
        (_SECRET_RE, "credential-like high-entropy value"),
    )
    for pattern, rule in pattern_rules:
        findings.extend(
            _finding(path, text, match.start(), rule) for match in pattern.finditer(text)
        )

    for match in _CHAT_ID_RE.finditer(text):
        if match.group(0) not in _PLACEHOLDER_CHAT_IDS:
            findings.append(_finding(path, text, match.start(), "non-placeholder Telegram chat ID"))

    for match in _BOT_USERNAME_RE.finditer(text):
        username = match.group(0).casefold()
        if not username.startswith(("@example_", "@project_")):
            findings.append(
                _finding(path, text, match.start(), "non-example Telegram bot username")
            )

    placeholder_uuids = {
        "00000000-0000-4000-8000-000000000001",
        "019abcde-1234-7fff-8fff-0123456789ab",
    }
    for match in _UUID_RE.finditer(text):
        if match.group(0).casefold() not in placeholder_uuids:
            findings.append(_finding(path, text, match.start(), "non-placeholder session UUID"))

    for pattern in _RAW_SESSION_RES:
        findings.extend(
            _finding(path, text, match.start(), "raw agent/session transcript marker")
            for match in pattern.finditer(text)
        )

    for match in _TOKEN_RE.finditer(text):
        digest = hashlib.sha256(match.group(0).casefold().encode()).hexdigest()
        if digest in _PRIVATE_FINGERPRINTS:
            findings.append(_finding(path, text, match.start(), "private deployment fingerprint"))

    return findings


def _candidate_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / item.decode() for item in result.stdout.split(b"\0") if item]


def _scan_blob(path: Path, data: bytes) -> list[PrivacyFinding]:
    forbidden_files = {
        Path("config/environment"),
        Path("config/hub.json"),
        Path("config/projects.json"),
    }
    forbidden_names = {".env", "auth.json", "credentials.json"}
    if path.parts[:2] in {("docs", "handoffs"), ("docs", "history")}:
        return [PrivacyFinding(path, 1, "internal history/handoff file is forbidden")]
    if (
        path in forbidden_files
        or path.name.casefold() in forbidden_names
        or "secrets" in {part.casefold() for part in path.parts}
        or path.suffix.casefold()
        in {".db", ".key", ".log", ".pem", ".session", ".sock", ".sqlite", ".sqlite3"}
    ):
        return [PrivacyFinding(path, 1, "local runtime or secret-bearing file is forbidden")]
    if len(data) > 2_000_000:
        return [PrivacyFinding(path, 1, "unexpected large tracked file requires review")]
    if path.parts[:1] == ("docs",) and len(data) > 50_000:
        return [PrivacyFinding(path, 1, "oversized documentation requires review")]
    if b"\0" in data:
        return [PrivacyFinding(path, 1, "binary tracked file requires explicit review")]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [PrivacyFinding(path, 1, "non-UTF-8 tracked file requires review")]
    return scan_text(path, text)


def scan_repository(root: Path) -> list[PrivacyFinding]:
    findings: list[PrivacyFinding] = []
    for absolute_path in _candidate_paths(root):
        if absolute_path.is_file():
            findings.extend(_scan_blob(absolute_path.relative_to(root), absolute_path.read_bytes()))
    return findings


def scan_history(root: Path) -> list[PrivacyFinding]:
    result = subprocess.run(
        ["git", "rev-list", "--objects", "--all", "--filter=object:type=blob"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    findings: list[PrivacyFinding] = []
    inspected: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split(" ", 1)
        object_id = fields[0]
        if object_id in inspected:
            continue
        inspected.add(object_id)
        object_type = subprocess.run(
            ["git", "cat-file", "-t", object_id],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if object_type in {"commit", "tag"}:
            metadata = subprocess.run(
                ["git", "cat-file", "-p", object_id],
                cwd=root,
                check=True,
                capture_output=True,
            ).stdout.decode("utf-8", errors="replace")
            findings.extend(
                PrivacyFinding(
                    Path(".git-metadata") / object_id[:12],
                    item.line,
                    f"{item.rule} in Git {object_type} metadata",
                )
                for item in scan_text(Path(".git-metadata"), metadata)
            )
            continue
        if object_type != "blob" or len(fields) != 2:
            continue
        raw_path = fields[1]
        blob = subprocess.run(
            ["git", "cat-file", "-p", object_id],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        historical = _scan_blob(Path(raw_path), blob)
        findings.extend(
            PrivacyFinding(item.path, item.line, f"{item.rule} in historical blob {object_id[:12]}")
            for item in historical
        )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reject private deployment data from Git")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--history", action="store_true", help="also scan every reachable Git blob")
    args = parser.parse_args(argv)
    findings = scan_repository(args.root.resolve())
    if args.history:
        findings.extend(scan_history(args.root.resolve()))
    if findings:
        print("Privacy scan failed:")
        for finding in findings:
            print(f"- {finding.render()}")
        return 1
    print("Privacy scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
