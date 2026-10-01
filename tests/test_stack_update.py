from __future__ import annotations

import ast
import datetime as dt
import fcntl
import hashlib
import http.client
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.request
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from hermes_codex_router import stack_update
from hermes_codex_router.stack_update import (
    MARKER_NAME,
    Environment,
    StackUpdateBusy,
    StackUpdateError,
    apply_plan,
    build_plan,
    install_copy,
    installed_version,
    link_state,
    load_manifest,
    parse_manifest,
    plan_digest,
    rollback_switch,
    stack_status,
)

CODEX_PACKAGE = "@example/codex"
PROXY_URL = "https://example.com/proxy/v{version}/proxy_{version}_linux_amd64.tar.gz"
CHECKSUMS_URL = "https://example.com/proxy/v{version}/checksums.txt"
HEALTH_URL = "http://127.0.0.1:8317/v1/models"


def _tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeStack:
    """A two-component stack in a temporary root with scripted side effects."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []
        self.failing_units: set[str] = set()
        self.inactive_units: set[str] = set()
        self.failing_argv: set[str] = set()
        self.urls: dict[str, bytes] = {}
        self.fetched: list[str] = []
        self.broken_urls: set[str] = set()
        self.integrity = {"0.3.0": "sha512-newer", "0.2.0": "sha512-new", "0.1.0": "sha512-old"}
        self.lock_integrity: dict[str, str] = {}
        self.plan_integrity: dict[str, str] = {}
        self.tampered_dependency = False
        self.rebuilt: list[Path] = []
        self.npm_version = "10.9.0"
        self.clock = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.UTC)
        self.ticks = 0.0
        self.proxy_versions = root / "proxy" / "releases"
        self.codex_versions = root / "codex" / "releases"
        self.proxy_link = root / "proxy" / "current"
        self.codex_link = root / "codex" / "current"
        self.manifest_path = root / "stack-manifest.json"
        self.state_dir = root / "state"
        self._installed(self.proxy_versions, "proxy", "1.0.0")
        self._installed(self.codex_versions, "codex", "0.1.0")
        os.symlink(self.proxy_versions / "1.0.0", self.proxy_link)
        os.symlink(self.codex_versions / "0.1.0", self.codex_link)
        self.write_manifest(self.manifest())
        archive = _tarball({"proxy": b"#!/bin/sh\n"})
        self.proxy_archive = archive
        self.urls[PROXY_URL.format(version="1.1.0")] = archive
        digest = hashlib.sha256(archive).hexdigest()
        self.urls[CHECKSUMS_URL.format(version="1.1.0")] = (
            f"{digest}  proxy_1.1.0_linux_amd64.tar.gz\n".encode()
        )
        self.urls[HEALTH_URL] = b'{"data": []}'
        for version, integrity in self.integrity.items():
            self.urls[f"https://registry.npmjs.org/@example%2Fcodex/{version}"] = json.dumps(
                {"dist": {"integrity": integrity}}
            ).encode()
        self.urls["https://registry.npmjs.org/@example%2Fcodex/latest"] = b'{"version": "0.2.0"}'

    def _installed(self, versions: Path, component: str, version: str) -> None:
        directory = versions / version
        directory.mkdir(parents=True)
        (directory / MARKER_NAME).write_text(
            json.dumps({"component": component, "version": version, "artifact": {}}),
            encoding="utf-8",
        )

    def manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "state_dir": str(self.state_dir),
            "npm": "/opt/example/node/bin/npm",
            "components": [
                {
                    "id": "codex",
                    "order": 20,
                    "link": str(self.codex_link),
                    "versions_dir": str(self.codex_versions),
                    "version": "0.1.0",
                    "source": {"kind": "npm", "package": CODEX_PACKAGE},
                    "version_argv": ["{dir}/node_modules/.bin/codex", "--version"],
                    "units": ["codex-daemon.service"],
                    "checks": [{"phase": "staged", "argv": ["{dir}/probe", "initialize"]}],
                },
                {
                    "id": "proxy",
                    "order": 10,
                    "link": str(self.proxy_link),
                    "versions_dir": str(self.proxy_versions),
                    "version": "1.0.0",
                    "source": {
                        "kind": "archive",
                        "url": PROXY_URL,
                        "checksums_url": CHECKSUMS_URL,
                        "github_repo": "example/proxy",
                    },
                    "version_argv": ["{dir}/proxy", "--version"],
                    "units": ["proxy.service"],
                    "checks": [
                        {"phase": "live", "url": HEALTH_URL, "expect": "data", "timeout_seconds": 3}
                    ],
                },
            ],
        }

    def write_manifest(self, data: dict[str, Any]) -> None:
        self.manifest_path.write_text(json.dumps(data), encoding="utf-8")
        self.manifest_path.chmod(0o600)

    def load(self) -> stack_update.StackManifest:
        return load_manifest(self.manifest_path)

    def env(self) -> Environment:
        return Environment(
            run=self.run,
            fetch=self.fetch,
            sleep=self.sleep,
            monotonic=lambda: self.ticks,
            now=self.now,
        )

    def now(self) -> dt.datetime:
        self.clock += dt.timedelta(seconds=1)
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.ticks += seconds

    def fetch(self, url: str, limit: int) -> bytes:
        self.fetched.append(url)
        if url in self.broken_urls:
            raise http.client.IncompleteRead(b"")
        if url not in self.urls:
            raise OSError("not found")
        return self.urls[url]

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "restart"]:
            return self._result(argv, 1 if argv[3] in self.failing_units else 0)
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return self._result(argv, 3 if argv[-1] in self.inactive_units else 0)
        if argv[0].endswith("/npm"):
            return self._npm(argv)
        if any(argv[0].endswith(name) for name in self.failing_argv):
            return self._result(argv, 1)
        return self._result(argv, 0, stdout=f"tool {self._version_of(Path(argv[0]))}\n")

    def _npm(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Model npm: lock-only resolution, ci of an exact lockfile, and rebuild."""
        if argv[1:] == ["--version"]:
            return self._result(argv, 0, stdout=f"{self.npm_version}\n")
        prefix = Path(argv[argv.index("--prefix") + 1])
        root = f"node_modules/{CODEX_PACKAGE}"
        if "--package-lock-only" in argv:
            assert "--ignore-scripts" in argv
            spec = next(arg for arg in argv if arg.startswith(f"{CODEX_PACKAGE}@"))
            version = spec.rsplit("@", 1)[1]
            integrity = self.plan_integrity.get(version, self.integrity.get(version, ""))
            # Like npm: the lockfile takes its name from an existing package.json,
            # otherwise from the directory name.
            existing = prefix / "package.json"
            name = json.loads(existing.read_text())["name"] if existing.exists() else prefix.name
            existing.write_text(
                json.dumps({"name": name, "dependencies": {CODEX_PACKAGE: version}})
            )
            lock = {
                "name": name,
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": name, "dependencies": {CODEX_PACKAGE: version}},
                    root: {"version": version, "integrity": integrity},
                    "node_modules/example-dep": {"version": "1.0.0", "integrity": "sha512-dep"},
                },
            }
            (prefix / "package-lock.json").write_text(json.dumps(lock))
            return self._result(argv, 0)
        if "ci" in argv:
            assert "--ignore-scripts" in argv
            lock = json.loads((prefix / "package-lock.json").read_text())
            packages = {name: dict(entry) for name, entry in lock["packages"].items() if name}
            version = packages[root]["version"]
            if version in self.lock_integrity:
                packages[root]["integrity"] = self.lock_integrity[version]
            if self.tampered_dependency:
                packages["node_modules/example-dep"]["integrity"] = "sha512-evil"
            (prefix / "node_modules" / ".bin").mkdir(parents=True)
            (prefix / "node_modules" / ".bin" / "codex").write_text("#!/bin/sh\n")
            (prefix / "node_modules" / ".package-lock.json").write_text(
                json.dumps({"packages": packages})
            )
            return self._result(argv, 0)
        if "rebuild" in argv:
            self.rebuilt.append(prefix)
            return self._result(argv, 0)
        return self._result(argv, 1)

    @staticmethod
    def _version_of(path: Path) -> str:
        for parent in path.parents:
            marker = parent / MARKER_NAME
            if marker.exists():
                return json.loads(marker.read_text())["version"]
        return "unknown"

    @staticmethod
    def _result(argv: list[str], code: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr="")

    def restarts(self) -> list[str]:
        return [call[3] for call in self.calls if call[:3] == ["systemctl", "--user", "restart"]]


class StackUpdateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.stack = FakeStack(Path(self._tempdir.name))

    def plan(self, **targets: str) -> dict[str, Any]:
        return build_plan(self.stack.load(), targets, self.stack.env())


class ManifestTests(StackUpdateTestCase):
    def test_loads_components_in_update_order(self) -> None:
        manifest = self.stack.load()
        self.assertEqual([c.component_id for c in manifest.components], ["proxy", "codex"])
        self.assertEqual(manifest.component("proxy").source.github_repo, "example/proxy")

    def test_rejects_invalid_manifests(self) -> None:
        cases: dict[str, Any] = {
            "unknown keys": lambda d: d.update(extra=1),
            "absolute path": lambda d: d["components"][0].update(link="relative/current"),
            "must not contain": lambda d: d["components"][0].update(link="/a/../b"),
            "loopback": lambda d: d["components"][1]["checks"][0].update(url="http://example.com/"),
            "phase live": lambda d: d["components"][1]["checks"][0].update(phase="staged"),
            "stage 4": lambda d: d["components"][0].update(watchdog=True),
            "ids must be unique": lambda d: d["components"][1].update(id="codex"),
            "orders must be unique": lambda d: d["components"][1].update(order=20),
            "manifest.npm is required": lambda d: d.pop("npm"),
            "{dir}": lambda d: d["components"][0].update(version_argv=["{dir}/x", "{home}"]),
            "https URL": lambda d: d["components"][1]["source"].update(url="http://example.com/x"),
            "must not contain each other": lambda d: d["components"][0].update(
                link=d["components"][0]["versions_dir"] + "/current"
            ),
            "not contain each other": lambda d: d["components"][0].update(
                link=str(Path(d["components"][0]["versions_dir"]).parent)
            ),
            ".service": lambda d: d["components"][0].update(units=["x; rm -rf /"]),
            "lifecycle": lambda d: d["components"][1]["source"].update(lifecycle_scripts=True),
        }
        for message, mutate in cases.items():
            with self.subTest(message):
                data = self.stack.manifest()
                mutate(data)
                with self.assertRaisesRegex(StackUpdateError, message.replace("{", r"\{")):
                    parse_manifest(json.dumps(data).encode(), path=self.stack.manifest_path)

    def test_refuses_a_group_writable_manifest(self) -> None:
        self.stack.manifest_path.chmod(0o620)
        with self.assertRaisesRegex(StackUpdateError, "group- or world-writable"):
            self.stack.load()

    def test_refuses_a_symlinked_manifest(self) -> None:
        link = self.stack.root / "linked-manifest.json"
        os.symlink(self.stack.manifest_path, link)
        with self.assertRaisesRegex(StackUpdateError, "not a symbolic link"):
            load_manifest(link)

    def test_reads_relative_and_foreign_links(self) -> None:
        manifest = self.stack.load()
        proxy = manifest.component("proxy")
        for relative in ("releases/1.0.0", "../proxy/releases/1.0.0"):
            self.stack.proxy_link.unlink()
            os.symlink(relative, self.stack.proxy_link)
            self.assertEqual(installed_version(proxy), "1.0.0")
        self.stack.proxy_link.unlink()
        os.symlink("/usr/bin", self.stack.proxy_link)
        self.assertIsNone(installed_version(proxy))
        self.stack.proxy_link.unlink()
        self.stack.proxy_link.write_text("file")
        self.assertEqual(link_state(proxy), stack_update.NOT_A_LINK)


class PlanTests(StackUpdateTestCase):
    def test_plan_binds_versions_published_digests_and_links(self) -> None:
        plan = self.plan(proxy="1.1.0", codex="0.2.0")
        self.assertEqual([s["component"] for s in plan["steps"]], ["proxy", "codex"])
        proxy, codex = plan["steps"]
        self.assertEqual((proxy["from"], proxy["to"]), ("1.0.0", "1.1.0"))
        self.assertEqual(
            proxy["published"], {"sha256": hashlib.sha256(self.stack.proxy_archive).hexdigest()}
        )
        self.assertEqual(codex["published"]["integrity"], "sha512-new")
        self.assertEqual(
            codex["published"]["lock_sha256"],
            hashlib.sha256(stack_update._canonical(codex["lock"])).hexdigest(),
        )
        self.assertIn("node_modules/example-dep", codex["lock"]["package-lock.json"]["packages"])
        self.assertEqual(plan["links"]["codex"], str(self.stack.codex_versions / "0.1.0"))
        self.assertEqual(plan["digest"], plan_digest(plan))

    def test_plan_skips_current_versions_and_refuses_nothing_to_do(self) -> None:
        plan = self.plan(proxy="1.0.0", codex="0.2.0")
        self.assertEqual([s["component"] for s in plan["steps"]], ["codex"])
        with self.assertRaisesRegex(StackUpdateError, "already runs"):
            self.plan(proxy="1.0.0")

    def test_plan_refuses_unknown_components_invalid_versions_and_unmanaged_links(self) -> None:
        with self.assertRaisesRegex(StackUpdateError, "unknown components: ghost"):
            self.plan(proxy="1.1.0", ghost="1")
        self.assertEqual(self.stack.fetched, [])
        with self.assertRaisesRegex(StackUpdateError, "invalid version"):
            self.plan(proxy="../1")
        self.stack.proxy_link.unlink()
        os.symlink("/usr/bin", self.stack.proxy_link)
        with self.assertRaisesRegex(StackUpdateError, "not managed yet"):
            self.plan(proxy="1.1.0")

    def test_plan_requires_a_published_checksum_when_configured(self) -> None:
        self.stack.urls[CHECKSUMS_URL.format(version="1.1.0")] = b"0" * 64 + b"  other.tar.gz\n"
        with self.assertRaisesRegex(StackUpdateError, "no published checksum"):
            self.plan(proxy="1.1.0")

    def test_plan_binds_downloaded_bytes_when_nothing_is_published(self) -> None:
        data = self.stack.manifest()
        del data["components"][1]["source"]["checksums_url"]
        self.stack.write_manifest(data)
        plan = self.plan(proxy="1.1.0")
        digest = hashlib.sha256(self.stack.proxy_archive).hexdigest()
        self.assertEqual(plan["steps"][0]["published"], {"sha256": digest})
        self.stack.urls[PROXY_URL.format(version="1.1.0")] = _tarball({"proxy": b"changed"})
        with self.assertRaisesRegex(StackUpdateError, "published checksum"):
            apply_plan(
                self.stack.load(), plan, expected_digest=plan["digest"], env=self.stack.env()
            )

    def test_parses_gnu_and_bsd_checksum_lines(self) -> None:
        digest = "ab" * 32
        name = "proxy.tar.gz"
        for line in (
            f"{digest}  {name}",
            f"{digest} *{name}",
            f"{digest}  ./{name}",
            f"{digest.upper()}  dist/{name}",
            f"SHA256 ({name}) = {digest}",
        ):
            with self.subTest(line):
                self.assertEqual(stack_update._checksum_for(line, name), digest)
        for line in (f"{digest}  other.tar.gz", f"{digest[:-1]}  {name}", name, ""):
            with self.subTest(line):
                self.assertIsNone(stack_update._checksum_for(line, name))

    def test_resolves_latest_versions_read_only(self) -> None:
        manifest = self.stack.load()
        self.stack.urls["https://api.github.com/repos/example/proxy/releases/latest"] = (
            b'{"tag_name": "v1.1.0"}'
        )
        env = self.stack.env()
        self.assertEqual(stack_update.resolve_latest(manifest.component("codex"), env), "0.2.0")
        self.assertEqual(stack_update.resolve_latest(manifest.component("proxy"), env), "1.1.0")
        self.assertEqual(self.stack.calls, [])


class ApplyTests(StackUpdateTestCase):
    def apply(self, plan: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return apply_plan(
            self.stack.load(), plan, expected_digest=plan["digest"], env=self.stack.env(), **kwargs
        )

    def test_applies_in_dependency_order_and_pins_versions(self) -> None:
        record = self.apply(self.plan(proxy="1.1.0", codex="0.2.0"))
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["flipped"], ["proxy", "codex"])
        manifest = self.stack.load()
        self.assertEqual(installed_version(manifest.component("proxy")), "1.1.0")
        self.assertEqual(installed_version(manifest.component("codex")), "0.2.0")
        self.assertEqual([c.version for c in manifest.components], ["1.1.0", "0.2.0"])
        self.assertEqual(self.stack.restarts(), ["proxy.service", "codex-daemon.service"])
        self.assertTrue((self.stack.proxy_versions / "1.1.0" / "proxy").exists())
        staged = json.loads((self.stack.codex_versions / "0.2.0" / MARKER_NAME).read_text())
        self.assertEqual(staged["artifact"]["integrity"], "sha512-new")
        self.assertEqual(len(staged["artifact"]["lock_sha256"]), 64)
        npm = [call for call in self.stack.calls if call[0].endswith("/npm")]
        self.assertEqual([call[1] for call in npm], ["install", "ci"])
        self.assertIn("--package-lock-only", npm[0])
        self.assertIn("--ignore-scripts", npm[1])
        self.assertEqual(self.stack.rebuilt, [])
        leftovers = [p.name for p in self.stack.proxy_versions.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])
        saved = stack_update.read_record(manifest, record["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(oct(self.stack.manifest_path.stat().st_mode & 0o777), "0o600")

    def test_a_plan_is_single_use(self) -> None:
        plan = self.plan(proxy="1.1.0")
        self.apply(plan)
        with self.assertRaisesRegex(StackUpdateError, "manifest changed"):
            self.apply(plan)

    def test_refuses_mismatched_or_tampered_digests(self) -> None:
        plan = self.plan(proxy="1.1.0")
        self.stack.calls.clear()
        with self.assertRaisesRegex(StackUpdateError, "approved digest"):
            apply_plan(self.stack.load(), plan, expected_digest="0" * 64, env=self.stack.env())
        tampered = {**plan, "steps": [{**plan["steps"][0], "to": "9.9.9"}]}
        with self.assertRaisesRegex(StackUpdateError, "does not match its content"):
            self.apply(tampered)
        self.assertEqual(self.stack.calls, [])

    def test_revalidates_plan_steps_whatever_their_digest(self) -> None:
        plan = self.plan(proxy="1.1.0", codex="0.2.0")
        self.stack.calls.clear()
        cases = {
            "invalid version": lambda p: p["steps"][0].update(to="../../x"),
            "malformed published": lambda p: p["steps"][1].update(published={}),
            "published digest": lambda p: p["steps"][0].update(published={"sha256": ""}),
            "dependency order": lambda p: p["steps"].reverse(),
            "twice": lambda p: p["steps"].append(dict(p["steps"][0])),
        }
        for message, mutate in cases.items():
            with self.subTest(message):
                crafted = json.loads(json.dumps(plan))
                mutate(crafted)
                crafted["digest"] = plan_digest(crafted)
                with self.assertRaisesRegex(StackUpdateError, message):
                    self.apply(crafted)
        self.assertEqual(self.stack.calls, [])

    def test_a_broken_health_response_fails_the_gate_instead_of_the_tool(self) -> None:
        self.stack.broken_urls.add(HEALTH_URL)
        record = self.apply(self.plan(proxy="1.1.0"))
        self.assertEqual(record["status"], "restored")

    def test_refuses_when_links_changed_since_the_plan(self) -> None:
        plan = self.plan(proxy="1.1.0")
        self.stack._installed(self.stack.codex_versions, "codex", "0.1.5")
        self.stack.codex_link.unlink()
        os.symlink(self.stack.codex_versions / "0.1.5", self.stack.codex_link)
        with self.assertRaisesRegex(StackUpdateError, "links changed"):
            self.apply(plan)
        self.assertEqual(self.stack.calls, [])

    def test_refuses_while_another_apply_holds_the_lock(self) -> None:
        plan = self.plan(proxy="1.1.0")
        self.stack.state_dir.mkdir(mode=0o700)
        fd = os.open(self.stack.state_dir / "lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self.assertRaises(StackUpdateBusy):
            self.apply(plan)

    def test_a_failed_staged_check_changes_no_link(self) -> None:
        self.stack.failing_argv.add("/probe")
        with self.assertRaisesRegex(StackUpdateError, "staged check 0 failed; no link"):
            self.apply(self.plan(proxy="1.1.0", codex="0.2.0"))
        manifest = self.stack.load()
        self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")
        self.assertEqual(self.stack.restarts(), [])

    def test_refuses_an_existing_version_directory_that_differs_from_the_plan(self) -> None:
        plan = self.plan(codex="0.2.0")
        (self.stack.codex_versions / "0.2.0").mkdir()
        (self.stack.codex_versions / "0.2.0" / MARKER_NAME).write_text(
            json.dumps({"component": "codex", "version": "0.2.0", "artifact": {"integrity": "x"}})
        )
        with self.assertRaisesRegex(StackUpdateError, "does not match the plan"):
            self.apply(plan)

    def test_refuses_a_download_that_differs_from_the_published_checksum(self) -> None:
        plan = self.plan(proxy="1.1.0")
        self.stack.urls[PROXY_URL.format(version="1.1.0")] = _tarball({"proxy": b"evil"})
        with self.assertRaisesRegex(StackUpdateError, "published checksum"):
            self.apply(plan)
        self.assertEqual(sorted(p.name for p in self.stack.proxy_versions.iterdir()), ["1.0.0"])

    def test_refuses_an_npm_install_that_differs_from_the_plan(self) -> None:
        plan = self.plan(codex="0.2.0")
        self.stack.lock_integrity["0.2.0"] = "sha512-other"
        with self.assertRaisesRegex(StackUpdateError, "do not match the plan"):
            self.apply(plan)
        self.assertEqual(sorted(p.name for p in self.stack.codex_versions.iterdir()), ["0.1.0"])

    def test_a_failed_live_gate_restores_every_switched_link(self) -> None:
        self.stack.inactive_units.add("codex-daemon.service")
        record = self.apply(self.plan(proxy="1.1.0", codex="0.2.0"))
        self.assertEqual(record["status"], "restored")
        self.assertIn("unit codex-daemon.service", record["failure"])
        manifest = self.stack.load()
        self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")
        self.assertEqual(installed_version(manifest.component("codex")), "0.1.0")
        self.assertEqual([c.version for c in manifest.components], ["1.0.0", "0.1.0"])
        self.assertEqual(
            self.stack.restarts(),
            ["proxy.service", "codex-daemon.service", "codex-daemon.service", "proxy.service"],
        )

    def test_an_unexpected_error_mid_switch_restores_every_link(self) -> None:
        plan = self.plan(proxy="1.1.0", codex="0.2.0")
        real_live_gates = stack_update._live_gates

        def explode(env: Environment, component: Any, allow: bool) -> str | None:
            if component.component_id == "codex":
                raise RuntimeError("boom")
            return real_live_gates(env, component, allow)

        with mock.patch.object(stack_update, "_live_gates", explode):
            record = self.apply(plan)
        self.assertEqual(record["status"], "restored")
        self.assertIn("unexpected RuntimeError", record["failure"])
        manifest = self.stack.load()
        self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")
        self.assertEqual(installed_version(manifest.component("codex")), "0.1.0")

    def test_an_interrupt_mid_switch_restores_and_propagates(self) -> None:
        plan = self.plan(proxy="1.1.0", codex="0.2.0")
        real_restart = stack_update._restart_units
        calls = {"n": 0}

        def interrupt(env: Environment, units: Any) -> bool:
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return real_restart(env, units)

        with mock.patch.object(stack_update, "_restart_units", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan)
        manifest = self.stack.load()
        self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")
        self.assertEqual(installed_version(manifest.component("codex")), "0.1.0")
        records = list((self.stack.state_dir / "switches").iterdir())
        saved = json.loads(records[0].read_text())
        self.assertEqual(
            (saved["status"], saved["failure"]), ("restored", "interrupted during the switch")
        )

    def test_a_failed_link_flip_is_not_restored_as_if_it_had_switched(self) -> None:
        plan = self.plan(proxy="1.1.0", codex="0.2.0")
        real_flip = stack_update._flip_link

        def flip(link: Path, target: str | None) -> None:
            if link == self.stack.codex_link and target is not None and "0.2.0" in target:
                raise PermissionError("read-only")
            real_flip(link, target)

        with mock.patch.object(stack_update, "_flip_link", flip):
            record = self.apply(plan)
        self.assertEqual(record["status"], "restored")
        self.assertEqual(self.stack.restarts(), ["proxy.service", "proxy.service"])

    def test_a_failed_pin_restores_instead_of_completing(self) -> None:
        plan = self.plan(proxy="1.1.0")
        # The pin fails once; restoring the old pins afterwards succeeds.
        failing_once = mock.Mock(side_effect=[OSError("disk full"), None])
        with mock.patch.object(stack_update, "_pin_versions", failing_once):
            record = self.apply(plan)
        self.assertEqual(record["status"], "restored")
        self.assertIn("unexpected OSError", record["failure"])
        self.assertEqual(installed_version(self.stack.load().component("proxy")), "1.0.0")

    def enable_lifecycle_scripts(self) -> None:
        data = self.stack.manifest()
        data["components"][0]["source"]["lifecycle_scripts"] = True
        self.stack.write_manifest(data)

    def test_lifecycle_scripts_run_only_after_the_tree_is_verified(self) -> None:
        self.enable_lifecycle_scripts()
        self.assertEqual(self.apply(self.plan(codex="0.2.0"))["status"], "completed")
        npm = [call[1] for call in self.stack.calls if call[0].endswith("/npm")]
        self.assertEqual(npm, ["install", "ci", "--version", "rebuild"])

    def test_no_package_script_runs_for_a_mismatched_tree(self) -> None:
        self.enable_lifecycle_scripts()
        plan = self.plan(codex="0.2.0")
        self.stack.tampered_dependency = True
        with self.assertRaisesRegex(StackUpdateError, "do not match the plan"):
            self.apply(plan)
        self.assertEqual(self.stack.rebuilt, [])

    def test_the_plan_pins_every_dependency(self) -> None:
        plan = self.plan(codex="0.2.0")
        self.stack.tampered_dependency = True
        with self.assertRaisesRegex(StackUpdateError, "do not match the plan"):
            self.apply(plan)
        self.assertEqual(sorted(p.name for p in self.stack.codex_versions.iterdir()), ["0.1.0"])

    def test_plan_refuses_a_lockfile_that_disagrees_with_the_registry(self) -> None:
        self.stack.plan_integrity["0.2.0"] = "sha512-other"
        with self.assertRaisesRegex(StackUpdateError, "disagrees with the registry"):
            self.plan(codex="0.2.0")

    def test_a_failed_final_record_write_restores_links_and_pins(self) -> None:
        plan = self.plan(proxy="1.1.0")
        real_write = stack_update._write_private_json

        def write(path: Path, data: Any) -> None:
            if data.get("status") == "completed":
                raise OSError("disk full")
            real_write(path, data)

        with mock.patch.object(stack_update, "_write_private_json", write):
            record = self.apply(plan)
        self.assertEqual(record["status"], "restored")
        manifest = self.stack.load()
        self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")
        self.assertEqual([c.version for c in manifest.components], ["1.0.0", "0.1.0"])

    def test_an_interrupt_after_the_pins_restores_them_too(self) -> None:
        plan = self.plan(proxy="1.1.0")
        real_pin = stack_update._pin_versions
        calls = {"n": 0}

        def pin(manifest: Any, versions: Any) -> None:
            real_pin(manifest, versions)
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt

        with mock.patch.object(stack_update, "_pin_versions", pin):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan)
        manifest = self.stack.load()
        self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")
        self.assertEqual([c.version for c in manifest.components], ["1.0.0", "0.1.0"])

    def test_changes_during_staging_are_refused_before_any_flip(self) -> None:
        real_stage = stack_update._stage

        def change_manifest(*args: Any) -> Path:
            directory = real_stage(*args)
            data = json.loads(self.stack.manifest_path.read_text())
            data["components"][1]["units"] = ["other.service"]
            self.stack.write_manifest(data)
            return directory

        def change_link(*args: Any) -> Path:
            directory = real_stage(*args)
            self.stack._installed(self.stack.codex_versions, "codex", "0.1.5")
            self.stack.codex_link.unlink()
            os.symlink(self.stack.codex_versions / "0.1.5", self.stack.codex_link)
            return directory

        for change, message in ((change_manifest, "manifest changed"), (change_link, "links")):
            with self.subTest(message):
                manifest = self.stack.load()
                plan = build_plan(manifest, {"proxy": "1.1.0"}, self.stack.env())
                with mock.patch.object(stack_update, "_stage", change):
                    with self.assertRaisesRegex(StackUpdateError, message):
                        apply_plan(
                            manifest, plan, expected_digest=plan["digest"], env=self.stack.env()
                        )
                self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")
                self.assertEqual(self.stack.restarts(), [])

    def test_lifecycle_scripts_are_refused_with_npm_12(self) -> None:
        self.enable_lifecycle_scripts()
        self.stack.npm_version = "12.0.2"
        with self.assertRaisesRegex(StackUpdateError, "older than 12"):
            self.apply(self.plan(codex="0.2.0"))
        self.assertEqual(self.stack.rebuilt, [])
        self.assertEqual(sorted(p.name for p in self.stack.codex_versions.iterdir()), ["0.1.0"])

    def test_two_plans_of_the_same_tree_bind_the_same_lockfile(self) -> None:
        first = self.plan(codex="0.2.0")["steps"][0]
        second = self.plan(codex="0.2.0")["steps"][0]
        self.assertEqual(first["published"], second["published"])
        self.assertEqual(first["lock"]["package-lock.json"]["name"], "stack-update-codex")

    def test_a_link_switched_before_a_flip_error_is_restored(self) -> None:
        plan = self.plan(proxy="1.1.0")
        real_fsync = stack_update._fsync_dir
        failed = {"done": False}

        def fsync(path: Path) -> None:
            proxy_flipped = os.readlink(self.stack.proxy_link).endswith("1.1.0")
            if path == self.stack.proxy_link.parent and proxy_flipped and not failed["done"]:
                failed["done"] = True
                raise OSError("fsync failed after the rename")
            real_fsync(path)

        with mock.patch.object(stack_update, "_fsync_dir", fsync):
            record = self.apply(plan)
        self.assertTrue(failed["done"])
        self.assertEqual(record["status"], "restored")
        manifest = self.stack.load()
        self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")
        self.assertEqual([c.version for c in manifest.components], ["1.0.0", "0.1.0"])

    def test_the_manifest_recheck_keeps_the_trust_checks(self) -> None:
        real_stage = stack_update._stage

        def loosen(*args: Any) -> Path:
            directory = real_stage(*args)
            self.stack.manifest_path.chmod(0o666)
            return directory

        manifest = self.stack.load()
        plan = build_plan(manifest, {"proxy": "1.1.0"}, self.stack.env())
        with mock.patch.object(stack_update, "_stage", loosen):
            with self.assertRaisesRegex(StackUpdateError, "group- or world-writable"):
                apply_plan(manifest, plan, expected_digest=plan["digest"], env=self.stack.env())
        self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")

    def test_records_and_links_are_made_durable(self) -> None:
        with mock.patch.object(stack_update.os, "fsync", wraps=os.fsync) as fsync:
            self.apply(self.plan(proxy="1.1.0"))
        # Record file and directory, each link flip's directory, manifest pins.
        self.assertGreaterEqual(fsync.call_count, 6)

    def test_a_failed_http_gate_restores_the_link(self) -> None:
        del self.stack.urls[HEALTH_URL]
        record = self.apply(self.plan(proxy="1.1.0"))
        self.assertEqual(record["status"], "restored")
        self.assertIn("live check 0", record["failure"])
        self.assertEqual(installed_version(self.stack.load().component("proxy")), "1.0.0")

    def add_inference_smoke(self) -> None:
        data = self.stack.manifest()
        data["components"][0]["checks"].append(
            {"phase": "staged", "argv": ["{dir}/smoke"], "inference": True}
        )
        self.stack.write_manifest(data)
        self.stack.failing_argv.add("/smoke")

    def test_live_inference_checks_are_skipped_by_default(self) -> None:
        self.add_inference_smoke()
        self.assertEqual(self.apply(self.plan(codex="0.2.0"))["status"], "completed")
        self.assertFalse(any(call[0].endswith("/smoke") for call in self.stack.calls))

    def test_live_inference_checks_run_when_explicitly_allowed(self) -> None:
        self.add_inference_smoke()
        with self.assertRaisesRegex(StackUpdateError, "staged check 1 failed"):
            self.apply(self.plan(codex="0.2.0"), allow_inference=True)

    def test_plan_requires_npm_integrity(self) -> None:
        self.stack.urls["https://registry.npmjs.org/@example%2Fcodex/0.2.0"] = b'{"dist": {}}'
        with self.assertRaisesRegex(StackUpdateError, "no integrity"):
            self.plan(codex="0.2.0")


class RollbackTests(StackUpdateTestCase):
    def switch(self) -> dict[str, Any]:
        plan = self.plan(proxy="1.1.0", codex="0.2.0")
        return apply_plan(
            self.stack.load(), plan, expected_digest=plan["digest"], env=self.stack.env()
        )

    def test_rolls_back_a_completed_switch(self) -> None:
        switch = self.switch()
        record = rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())
        self.assertEqual(record["status"], "completed")
        manifest = self.stack.load()
        self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")
        self.assertEqual(installed_version(manifest.component("codex")), "0.1.0")
        self.assertEqual([c.version for c in manifest.components], ["1.0.0", "0.1.0"])

    def test_rollback_restores_the_pins_recorded_before_the_switch(self) -> None:
        data = self.stack.manifest()
        data["components"][0]["version"] = "0.0.9"
        self.stack.write_manifest(data)
        switch = self.switch()
        pins = {cid: entry["pin_before"] for cid, entry in switch["components"].items()}
        self.assertEqual(pins, {"proxy": "1.0.0", "codex": "0.0.9"})
        rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())
        self.assertEqual([c.version for c in self.stack.load().components], ["1.0.0", "0.0.9"])

    def test_a_second_rollback_after_an_interrupted_one_restores_all_pins(self) -> None:
        switch = self.switch()
        # An interrupted first rollback already put the proxy link back.
        self.stack.proxy_link.unlink()
        os.symlink(self.stack.proxy_versions / "1.0.0", self.stack.proxy_link)
        record = rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())
        self.assertEqual(
            (record["status"], record["components"]), ("completed", ["proxy", "codex"])
        )
        self.assertEqual([c.version for c in self.stack.load().components], ["1.0.0", "0.1.0"])

    def test_a_rollback_interrupted_after_its_links_completes_on_repeat(self) -> None:
        switch = self.switch()
        # The first rollback put both links back, then stopped before units and pins.
        for link, versions, version in (
            (self.stack.proxy_link, self.stack.proxy_versions, "1.0.0"),
            (self.stack.codex_link, self.stack.codex_versions, "0.1.0"),
        ):
            link.unlink()
            os.symlink(versions / version, link)
        self.assertEqual([c.version for c in self.stack.load().components], ["1.1.0", "0.2.0"])
        self.stack.calls.clear()
        record = rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())
        self.assertEqual(record["status"], "completed")
        self.assertEqual([c.version for c in self.stack.load().components], ["1.0.0", "0.1.0"])
        self.assertEqual(self.stack.restarts(), ["codex-daemon.service", "proxy.service"])
        with self.assertRaisesRegex(StackUpdateError, "already rolled back"):
            rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())

    def test_rollback_refuses_a_manifest_that_moved_the_link(self) -> None:
        switch = self.switch()
        moved = self.stack.root / "codex" / "moved-current"
        os.symlink(self.stack.codex_versions / "0.1.0", moved)
        data = json.loads(self.stack.manifest_path.read_text())
        data["components"][0]["link"] = str(moved)
        self.stack.write_manifest(data)
        with self.assertRaisesRegex(StackUpdateError, "link path changed"):
            rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())
        self.assertEqual(
            os.readlink(self.stack.codex_link), str(self.stack.codex_versions / "0.2.0")
        )

    def plan_and_apply(self, **targets: str) -> dict[str, Any]:
        manifest = self.stack.load()
        plan = build_plan(manifest, targets, self.stack.env())
        return apply_plan(manifest, plan, expected_digest=plan["digest"], env=self.stack.env())

    def test_an_unfinished_rollback_blocks_new_switches_until_repeated(self) -> None:
        switch = self.switch()
        real_write = stack_update._write_private_json

        def write(path: Path, data: Any) -> None:
            rollback = data.get("rollback") if isinstance(data, dict) else None
            if isinstance(rollback, dict) and rollback.get("status") == "completed":
                raise OSError("disk full")
            real_write(path, data)

        with mock.patch.object(stack_update, "_write_private_json", write):
            with self.assertRaises(OSError):
                rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())
        with self.assertRaisesRegex(StackUpdateError, "did not finish"):
            self.plan_and_apply(codex="0.3.0")
        record = rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())
        self.assertEqual(record["status"], "completed")
        with self.assertRaisesRegex(StackUpdateError, "already rolled back"):
            rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())
        self.assertEqual(self.plan_and_apply(codex="0.3.0")["status"], "completed")

    def test_an_unreconciled_switch_blocks_new_switches(self) -> None:
        self.stack.inactive_units.add("codex-daemon.service")
        with mock.patch.object(stack_update, "_restore_pins", return_value=False):
            switch = self.switch()
        self.assertEqual(switch["status"], "restore_failed")
        self.stack.inactive_units.clear()
        with self.assertRaisesRegex(StackUpdateError, "unreconciled"):
            self.plan_and_apply(codex="0.3.0")

    def test_rollback_refuses_while_a_later_switch_owns_the_link(self) -> None:
        first = self.switch()
        self.assertEqual(self.plan_and_apply(codex="0.3.0")["status"], "completed")
        with self.assertRaisesRegex(StackUpdateError, "a later switch"):
            rollback_switch(self.stack.load(), first["id"], env=self.stack.env())

    def test_refuses_when_a_link_changed_after_the_switch(self) -> None:
        switch = self.switch()
        self.stack._installed(self.stack.codex_versions, "codex", "0.1.5")
        self.stack.codex_link.unlink()
        os.symlink(self.stack.codex_versions / "0.1.5", self.stack.codex_link)
        with self.assertRaisesRegex(StackUpdateError, "changed after this switch"):
            rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())

    def test_skips_a_recorded_link_that_never_switched(self) -> None:
        # A crash between recording the intent and flipping leaves the link as before.
        switch = self.switch()
        self.stack.codex_link.unlink()
        os.symlink(self.stack.codex_versions / "0.1.0", self.stack.codex_link)
        record = rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())
        self.assertEqual(record["status"], "completed")
        manifest = self.stack.load()
        self.assertEqual(installed_version(manifest.component("proxy")), "1.0.0")
        with self.assertRaisesRegex(StackUpdateError, "already rolled back"):
            rollback_switch(manifest, switch["id"], env=self.stack.env())

    def test_refuses_restored_switches_and_invalid_ids(self) -> None:
        self.stack.inactive_units.add("proxy.service")
        switch = self.switch()
        self.assertEqual(switch["status"], "restored")
        with self.assertRaisesRegex(StackUpdateError, "apply already restored"):
            rollback_switch(self.stack.load(), switch["id"], env=self.stack.env())
        with self.assertRaisesRegex(StackUpdateError, "invalid switch record id"):
            rollback_switch(self.stack.load(), "../../etc/passwd", env=self.stack.env())


class ToolTests(StackUpdateTestCase):
    def test_status_reports_pinned_and_installed_versions(self) -> None:
        status = stack_status(self.stack.load())
        self.assertEqual(
            status,
            [
                {"component": "proxy", "pinned": "1.0.0", "installed": "1.0.0", "managed": True},
                {"component": "codex", "pinned": "0.1.0", "installed": "0.1.0", "managed": True},
            ],
        )

    def test_http_get_refuses_a_downgrade_to_plain_http(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "http://example.com/file"
        response.read.return_value = b"data"
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(StackUpdateError, "redirected to plain http"):
                stack_update._http_get("https://example.com/file", 10)
        opener.open.side_effect = http.client.IncompleteRead(b"")
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(StackUpdateError, "request to example.com failed"):
                stack_update._http_get("https://example.com/file", 10)

    def test_every_redirect_is_checked_before_it_is_followed(self) -> None:
        cases = (
            ("https://example.com/a", "http://example.com/b", "plain http"),
            ("http://127.0.0.1:8317/a", "http://example.com/b", "off the machine"),
        )
        for origin, target, message in cases:
            with self.subTest(target):
                handler = stack_update._GuardedRedirect(origin)
                request = urllib.request.Request(origin)
                with self.assertRaisesRegex(StackUpdateError, message):
                    handler.redirect_request(request, None, 302, "Found", {}, target)
        handler = stack_update._GuardedRedirect("https://example.com/a")
        followed = handler.redirect_request(
            urllib.request.Request("https://example.com/a"),
            None,
            302,
            "Found",
            {},
            "https://cdn.example.com/b",
        )
        self.assertEqual(followed.full_url if followed else None, "https://cdn.example.com/b")

    def test_zip_members_cannot_escape_or_link(self) -> None:
        destination = self.stack.root / "zip-out"
        destination.mkdir()
        for name, attr in (("../escape", 0), ("link", 0o120777 << 16)):
            with self.subTest(name):
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w") as archive:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = attr
                    archive.writestr(info, b"x")
                with zipfile.ZipFile(buffer) as archive:
                    with self.assertRaises(StackUpdateError):
                        stack_update._safe_zip_extract(archive, destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_zip_extraction_keeps_the_executable_bit_only(self) -> None:
        destination = self.stack.root / "zip-exec"
        destination.mkdir()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, mode in (("bin/tool", 0o104755), ("README", 0o100644)):
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                archive.writestr(info, b"x")
        with zipfile.ZipFile(buffer) as archive:
            stack_update._safe_zip_extract(archive, destination)
        self.assertEqual(oct((destination / "bin/tool").stat().st_mode & 0o7777), "0o755")
        self.assertEqual((destination / "README").stat().st_mode & 0o111, 0)

    def test_version_output_on_stderr_passes(self) -> None:
        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="tool 1.2.3\n")

        env = Environment(run=run)
        self.assertTrue(stack_update._argv_passes(env, ["/bin/tool"], "1.2.3", 5.0))

    def test_loopback_checks_cannot_follow_a_redirect_off_the_machine(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "http://169.254.169.254/latest"
        response.read.return_value = b"data"
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(StackUpdateError, "off the machine"):
                stack_update._http_get("http://127.0.0.1:8317/v1/models", 10)

    def test_a_malformed_switch_record_is_a_refusal(self) -> None:
        manifest = self.stack.load()
        path = stack_update._record_path(manifest, "20260930T120000Z-0123abcd")
        path.write_text("{not json")
        with self.assertRaisesRegex(StackUpdateError, "malformed"):
            rollback_switch(manifest, "20260930T120000Z-0123abcd", env=self.stack.env())

    def test_install_copy_is_immutable_and_idempotent(self) -> None:
        destination = self.stack.root / "tool"
        first = install_copy(destination)
        second = install_copy(destination)
        self.assertEqual(first, second)
        self.assertEqual(first.read_bytes(), Path(stack_update.__file__).read_bytes())
        self.assertEqual(oct(first.stat().st_mode & 0o777), "0o500")
        self.assertEqual(os.readlink(destination / "current"), first.parent.name)

    def test_module_uses_only_the_standard_library(self) -> None:
        tree = ast.parse(Path(stack_update.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, "relative import")
                imported.add(str(node.module).split(".")[0])
        self.assertEqual(sorted(imported - set(sys.stdlib_module_names) - {"__future__"}), [])

    def test_cli_plans_applies_and_reports_errors(self) -> None:
        out = self.stack.root / "plan.json"
        env = self.stack.env()
        manifest = ["--manifest", str(self.stack.manifest_path)]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = stack_update.main(
                [*manifest, "plan", "--latest", "codex", "--out", str(out)], env=env
            )
        self.assertEqual(code, 0)
        plan = json.loads(out.read_text())
        self.assertEqual(oct(out.stat().st_mode & 0o777), "0o600")
        self.assertEqual(plan["steps"][0]["to"], "0.2.0")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = stack_update.main([*manifest, "apply", str(out), "--digest", "0" * 64], env=env)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buffer.getvalue())["ok"], False)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = stack_update.main(
                [*manifest, "apply", str(out), "--digest", plan["digest"]], env=env
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["result"]["status"], "completed")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = stack_update.main(
                [*manifest, "apply", str(self.stack.root / "missing.json"), "--digest", "0"],
                env=env,
            )
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", json.loads(buffer.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()
