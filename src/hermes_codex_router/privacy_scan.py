from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import tempfile
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
_GITHUB_WEB_FLOW_FINGERPRINTS = frozenset(
    {
        "5DE3E0509C47EA3CF04A42D34AEE18F83AFDEB23",
        "968479A1AFF927E37D1A566BB5690EEEBB952194",
    }
)
_PUBLIC_AUTHOR_EMAIL_FILE_ENV = "HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE"
_MAX_PUBLIC_AUTHOR_EMAIL_FILE_BYTES = 320
_AUTHOR_HEADER_RE = re.compile(
    r"(?m)^author (?P<name>[^<>\n]*) <(?P<email>[^<>\n]+)> "
    r"(?P<timestamp>[0-9]+) (?P<timezone>[+-][0-9]{4})$"
)
_GITHUB_COMMITTER_RE = re.compile(
    r"(?m)^committer GitHub <noreply@github\.com> "
    r"(?P<timestamp>[0-9]+) (?P<timezone>[+-][0-9]{4})$"
)


@dataclass(frozen=True, slots=True)
class PrivacyFinding:
    path: Path
    line: int
    rule: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


@dataclass(frozen=True, slots=True)
class _PrivacyMatch:
    finding: PrivacyFinding
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _SpanExemption:
    start: int
    end: int
    rules: frozenset[str]


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _byte_span(text: str, start: int, end: int) -> tuple[int, int]:
    return len(text[:start].encode()), len(text[:end].encode())


def _match_finding(
    path: Path,
    text: str,
    start: int,
    end: int,
    rule: str,
) -> _PrivacyMatch:
    byte_start, byte_end = _byte_span(text, start, end)
    return _PrivacyMatch(
        PrivacyFinding(path, _line_number(text, start), rule),
        byte_start,
        byte_end,
    )


def _scan_text_matches(path: Path, text: str) -> list[_PrivacyMatch]:
    findings: list[_PrivacyMatch] = []

    def add(match: re.Match[str], rule: str) -> None:
        findings.append(_match_finding(path, text, match.start(), match.end(), rule))

    for match in _EMAIL_RE.finditer(text):
        domain = match.group(2).casefold()
        address = match.group(0).casefold()
        if (
            domain not in {"example.com", "example.org", "example.net"}
            and not domain.endswith(".service")
            and address != "noreply@github.com"
            and not domain.endswith(".users.noreply.github.com")
        ):
            add(match, "non-example email address")

    for match in _HOME_RE.finditer(text):
        if match.group(1).casefold() not in {"example", "user"}:
            add(match, "owner-specific home path")

    pattern_rules = (
        (_WINDOWS_PATH_RE, "absolute Windows path"),
        (_INVITE_RE, "private Telegram invite link"),
        (_BOT_TOKEN_RE, "Telegram bot token"),
        (_SECRET_RE, "credential-like high-entropy value"),
    )
    for pattern, rule in pattern_rules:
        for match in pattern.finditer(text):
            add(match, rule)

    for match in _CHAT_ID_RE.finditer(text):
        if match.group(0) not in _PLACEHOLDER_CHAT_IDS:
            add(match, "non-placeholder Telegram chat ID")

    for match in _BOT_USERNAME_RE.finditer(text):
        username = match.group(0).casefold()
        if not username.startswith(("@example_", "@project_")):
            add(match, "non-example Telegram bot username")

    placeholder_uuids = {
        "00000000-0000-4000-8000-000000000001",
        "019abcde-1234-7fff-8fff-0123456789ab",
    }
    for match in _UUID_RE.finditer(text):
        if match.group(0).casefold() not in placeholder_uuids:
            add(match, "non-placeholder session UUID")

    for pattern in _RAW_SESSION_RES:
        for match in pattern.finditer(text):
            add(match, "raw agent/session transcript marker")

    for match in _TOKEN_RE.finditer(text):
        digest = hashlib.sha256(match.group(0).casefold().encode()).hexdigest()
        if digest in _PRIVATE_FINGERPRINTS:
            add(match, "private deployment fingerprint")

    return findings


def scan_text(path: Path, text: str) -> list[PrivacyFinding]:
    return [match.finding for match in _scan_text_matches(path, text)]


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


def _valid_git_timezone(value: str) -> bool:
    if not re.fullmatch(r"[+-][0-9]{4}", value):
        return False
    hours = int(value[1:3])
    minutes = int(value[3:5])
    return hours < 14 and minutes < 60 or hours == 14 and minutes == 0


def _valid_github_owner(value: str) -> bool:
    return bool(
        re.fullmatch(r"[A-Za-z0-9-]{1,39}", value)
        and not value.startswith("-")
        and not value.endswith("-")
        and "--" not in value
    )


def _metadata_privacy_exemptions(
    metadata: bytes,
    *,
    declared_author_email: bytes | None,
    github_signature_verified: bool,
    github_owner: str | None,
) -> tuple[_SpanExemption, ...]:
    if not github_signature_verified:
        return ()
    try:
        text = metadata.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    headers, separator, message = text.partition("\n\n")
    if not separator or "\x00" in text or "\r" in text:
        return ()
    tree_headers = re.findall(r"(?m)^tree ([0-9a-f]{40})$", headers)
    parents = re.findall(r"(?m)^parent ([0-9a-f]{40})$", headers)
    author_headers = tuple(_AUTHOR_HEADER_RE.finditer(headers))
    committer_headers = tuple(_GITHUB_COMMITTER_RE.finditer(headers))
    if (
        len(tree_headers) != 1
        or len(parents) != 2
        or len(re.findall(r"(?m)^tree ", headers)) != 1
        or len(re.findall(r"(?m)^parent ", headers)) != 2
        or len(author_headers) != 1
        or len(committer_headers) != 1
        or len(re.findall(r"(?m)^author ", headers)) != 1
        or len(re.findall(r"(?m)^committer ", headers)) != 1
    ):
        return ()
    author = author_headers[0]
    committer = committer_headers[0]
    if not (
        _valid_git_timezone(author.group("timezone"))
        and _valid_git_timezone(committer.group("timezone"))
    ):
        return ()

    message_offset = len(headers) + len(separator)
    hosted = re.match(
        r"^(Merge pull request #[0-9]+ from )"
        r"(?P<owner>[A-Za-z0-9-]{1,39})/(?P<branch>[^\n]+)(?:\n|$)",
        message,
    )
    synthetic = re.fullmatch(
        r"Merge (?P<head>[0-9a-f]{40}) into (?P<base>[0-9a-f]{40})\n?",
        message,
    )
    owner_char_span: tuple[int, int] | None = None
    if hosted:
        source_owner = hosted.group("owner")
        if (
            not github_owner
            or not _valid_github_owner(source_owner)
            or source_owner != github_owner
        ):
            return ()
        owner_char_span = (
            message_offset + hosted.start("owner"),
            message_offset + hosted.end("owner"),
        )
    elif synthetic:
        if synthetic.group("base") != parents[0] or synthetic.group("head") != parents[1]:
            return ()
    else:
        return ()

    exemptions: list[_SpanExemption] = []
    email_char_span = author.span("email")
    email_start, email_end = _byte_span(text, *email_char_span)
    if (
        declared_author_email is not None
        and metadata[email_start:email_end] == declared_author_email
    ):
        exemptions.append(
            _SpanExemption(
                email_start,
                email_end,
                frozenset({"non-example email address", "private deployment fingerprint"}),
            )
        )
    name_char_span = author.span("name")
    name_start, name_end = _byte_span(text, *name_char_span)
    if (
        github_owner is not None
        and _valid_github_owner(github_owner)
        and metadata[name_start:name_end] == github_owner.encode("ascii")
    ):
        exemptions.append(
            _SpanExemption(
                name_start,
                name_end,
                frozenset({"private deployment fingerprint"}),
            )
        )
    if owner_char_span is not None:
        owner_start, owner_end = _byte_span(text, *owner_char_span)
        exemptions.append(
            _SpanExemption(
                owner_start,
                owner_end,
                frozenset({"private deployment fingerprint"}),
            )
        )
    return tuple(exemptions)


def _scan_metadata_for_privacy(
    path: Path,
    metadata: bytes,
    *,
    declared_author_email: bytes | None,
    github_signature_verified: bool,
    github_owner: str | None,
) -> list[PrivacyFinding]:
    try:
        text = metadata.decode("utf-8")
    except UnicodeDecodeError:
        return [PrivacyFinding(path, 1, "non-UTF-8 Git metadata")]
    exemptions = _metadata_privacy_exemptions(
        metadata,
        declared_author_email=declared_author_email,
        github_signature_verified=github_signature_verified,
        github_owner=github_owner,
    )
    findings: list[PrivacyFinding] = []
    for match in _scan_text_matches(path, text):
        if any(
            match.start == exemption.start
            and match.end == exemption.end
            and match.finding.rule in exemption.rules
            for exemption in exemptions
        ):
            continue
        findings.append(match.finding)
    return findings


def _metadata_for_privacy_scan(
    metadata: str,
    *,
    github_signature_verified: bool = False,
    github_owner: str | None = None,
) -> str:
    """Compatibility helper: metadata must remain byte-for-byte visible to scanners."""
    return metadata


def _read_public_author_email(root: Path) -> bytes | None:
    raw_path = os.environ.get(_PUBLIC_AUTHOR_EMAIL_FILE_ENV)
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        return None
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_uid != os.getuid()
            or details.st_nlink != 1
            or details.st_size > _MAX_PUBLIC_AUTHOR_EMAIL_FILE_BYTES
        ):
            return None
        descriptor_link = Path(f"/proc/self/fd/{descriptor}")
        opened_path = descriptor_link.resolve(strict=True)
        checkout_path = root.resolve(strict=True)
        try:
            opened_path.relative_to(checkout_path)
        except ValueError:
            pass
        else:
            return None
        chunks: list[bytes] = []
        remaining = _MAX_PUBLIC_AUTHOR_EMAIL_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        final_details = os.fstat(descriptor)
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(raw) > _MAX_PUBLIC_AUTHOR_EMAIL_FILE_BYTES
            or len(raw) != details.st_size
            or any(
                getattr(details, field) != getattr(final_details, field) for field in stable_fields
            )
        ):
            return None
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if not raw or b"\n" in raw or b"\r" in raw or b"\x00" in raw:
            return None
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return None
        if _EMAIL_RE.fullmatch(text) is None:
            return None
        return raw
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _isolated_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    for key in tuple(environment):
        if key in {
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CEILING_DIRECTORIES",
        } or re.fullmatch(r"GIT_CONFIG_(?:KEY|VALUE)_[0-9]+", key):
            environment.pop(key, None)
    return environment


def _read_git_object(root: Path, object_id: str) -> bytes:
    return subprocess.run(
        [str(Path("/usr/bin/git")), "cat-file", "-p", object_id],
        cwd=root,
        env=_isolated_git_environment(),
        check=True,
        capture_output=True,
    ).stdout


def _git_object_bytes_match_oid(object_type: str, object_id: str, data: bytes) -> bool:
    if not re.fullmatch(r"[0-9a-f]{40}", object_id):
        return False
    framed = f"{object_type} {len(data)}\0".encode() + data
    return hashlib.sha1(framed, usedforsecurity=False).hexdigest() == object_id


def _github_signature_verified(root: Path, object_id: str) -> bool:
    key_path = Path(__file__).with_name("github-web-flow.asc")
    git_path = Path("/usr/bin/git")
    gpg_path = Path("/usr/bin/gpg")
    if not key_path.is_file() or not git_path.is_file() or not gpg_path.is_file():
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="hub-gpg-") as temporary:
            os.chmod(temporary, 0o700)
            environment = _isolated_git_environment()
            environment["GNUPGHOME"] = temporary
            imported = subprocess.run(
                [
                    str(gpg_path),
                    "--batch",
                    "--no-autostart",
                    "--quiet",
                    "--import",
                    str(key_path),
                ],
                cwd=root,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            if imported.returncode != 0:
                return False
            verified = subprocess.run(
                [
                    str(git_path),
                    "-c",
                    "gpg.format=openpgp",
                    "-c",
                    f"gpg.program={gpg_path}",
                    "-c",
                    f"gpg.openpgp.program={gpg_path}",
                    "verify-commit",
                    "--raw",
                    object_id,
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            valid_signatures = re.findall(
                r"(?m)^\[GNUPG:\] VALIDSIG ([0-9A-F]{40}) ", verified.stderr
            )
            return (
                verified.returncode == 0
                and len(valid_signatures) == 1
                and valid_signatures[0] in _GITHUB_WEB_FLOW_FINGERPRINTS
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _is_github_merge_candidate(metadata: str) -> bool:
    headers, separator, message = metadata.partition("\n\n")
    if not separator:
        return False
    parents = re.findall(r"(?m)^parent ([0-9a-f]{40})$", headers)
    if len(parents) != 2 or _GITHUB_COMMITTER_RE.search(headers) is None:
        return False
    hosted = re.match(
        r"^Merge pull request #[0-9]+ from [A-Za-z0-9-]{1,39}/[^\n]+(?:\n|$)",
        message,
    )
    synthetic = re.fullmatch(
        r"Merge (?P<head>[0-9a-f]{40}) into (?P<base>[0-9a-f]{40})\n?",
        message,
    )
    return bool(
        hosted
        or synthetic
        and synthetic.group("base") == parents[0]
        and synthetic.group("head") == parents[1]
    )


def _github_remote_owner(root: Path) -> str | None:
    environment = _isolated_git_environment()
    try:
        result = subprocess.run(
            [str(Path("/usr/bin/git")), "config", "--local", "--get", "remote.origin.url"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.fullmatch(
        r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
        r"([A-Za-z0-9-]{1,39})/[^/\s]+?(?:\.git)?\n?",
        result.stdout,
    )
    if not match:
        return None
    owner = match.group(1)
    if owner.startswith("-") or owner.endswith("-") or "--" in owner:
        return None
    return owner


def scan_history(root: Path) -> list[PrivacyFinding]:
    git_path = str(Path("/usr/bin/git"))
    git_environment = _isolated_git_environment()
    result = subprocess.run(
        [git_path, "rev-list", "--objects", "--all", "--filter=object:type=blob"],
        cwd=root,
        env=git_environment,
        check=True,
        capture_output=True,
        text=True,
    )
    findings: list[PrivacyFinding] = []
    github_owner = _github_remote_owner(root)
    declared_author_email = _read_public_author_email(root)
    inspected: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split(" ", 1)
        object_id = fields[0]
        if object_id in inspected:
            continue
        inspected.add(object_id)
        object_type = subprocess.run(
            [git_path, "cat-file", "-t", object_id],
            cwd=root,
            env=git_environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if object_type in {"commit", "tag"}:
            metadata = _read_git_object(root, object_id)
            if not _git_object_bytes_match_oid(object_type, object_id, metadata):
                findings.append(
                    PrivacyFinding(
                        Path(".git-metadata") / object_id[:12],
                        1,
                        "Git object bytes do not match object ID",
                    )
                )
                continue
            try:
                metadata_text = metadata.decode("utf-8")
            except UnicodeDecodeError:
                metadata_text = ""
            github_signature_verified = _is_github_merge_candidate(
                metadata_text
            ) and _github_signature_verified(root, object_id)
            findings.extend(
                PrivacyFinding(
                    Path(".git-metadata") / object_id[:12],
                    item.line,
                    f"{item.rule} in Git {object_type} metadata",
                )
                for item in _scan_metadata_for_privacy(
                    Path(".git-metadata"),
                    metadata,
                    declared_author_email=declared_author_email,
                    github_signature_verified=github_signature_verified,
                    github_owner=github_owner,
                )
            )
            continue
        if object_type != "blob" or len(fields) != 2:
            continue
        raw_path = fields[1]
        blob = _read_git_object(root, object_id)
        if not _git_object_bytes_match_oid(object_type, object_id, blob):
            findings.append(
                PrivacyFinding(
                    Path(raw_path),
                    1,
                    "Git object bytes do not match object ID",
                )
            )
            continue
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
