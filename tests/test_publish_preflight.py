from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import publish_preflight


def _isolated_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    names = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    for name in names:
        environment.pop(name, None)
    return environment


class PublishPreflightTests(unittest.TestCase):
    def test_push_refs_require_the_checked_out_head_and_a_named_local_ref(self) -> None:
        head = "a" * 40
        publish_preflight._validate_push_refs(
            f"refs/heads/topic {head} refs/heads/topic {'b' * 40}\n",
            head,
        )
        publish_preflight._validate_push_refs(
            f"(delete) {'0' * 40} refs/heads/old {'c' * 40}\n",
            head,
        )
        with self.assertRaisesRegex(publish_preflight.PreflightError, "checked-out HEAD"):
            publish_preflight._validate_push_refs(
                f"refs/heads/other {'d' * 40} refs/heads/other {'e' * 40}\n",
                head,
            )
        with self.assertRaisesRegex(publish_preflight.PreflightError, "local ref"):
            publish_preflight._validate_push_refs(
                f"(unknown) {head} refs/tags/v1.0.0 {'0' * 40}\n",
                head,
            )

    def test_remote_policy_comparison_is_exact_and_does_not_disclose_value(self) -> None:
        declared = b"owner@example.com"
        completed = subprocess.CompletedProcess(
            ["gh"], 0, stdout=b'{"value":"owner@example.com"}', stderr=b""
        )
        with patch(
            "hermes_codex_router.publish_preflight.subprocess.run", return_value=completed
        ) as run:
            publish_preflight._require_matching_repository_variable(
                declared, Path("/repo"), "example-org/example-repository"
            )
        self.assertEqual(run.call_args.args[0][:4], ["gh", "api", "--method", "GET"])

        mismatch = subprocess.CompletedProcess(
            ["gh"], 0, stdout=b'{"value":"different@example.com"}', stderr=b""
        )
        stderr = io.StringIO()
        with (
            patch("hermes_codex_router.publish_preflight.subprocess.run", return_value=mismatch),
            redirect_stderr(stderr),
            self.assertRaises(publish_preflight.PreflightError),
        ):
            publish_preflight._require_matching_repository_variable(
                declared, Path("/repo"), "example-org/example-repository"
            )
        self.assertNotIn("owner@example.com", stderr.getvalue())
        self.assertNotIn("different@example.com", stderr.getvalue())

    def test_remote_lookup_failure_does_not_echo_cli_output(self) -> None:
        completed = subprocess.CompletedProcess(
            ["gh"], 1, stdout=b"private-value", stderr=b"private-diagnostic"
        )
        with (
            patch("hermes_codex_router.publish_preflight.subprocess.run", return_value=completed),
            self.assertRaisesRegex(publish_preflight.PreflightError, "repository variable"),
        ):
            publish_preflight._require_matching_repository_variable(
                b"owner@example.com", Path("/repo"), "example-org/example-repository"
            )

    def test_remote_policy_rejects_noncanonical_bytes(self) -> None:
        for value in (
            "owner@example.com\r",
            "owner@example.com\r\n",
            "owner@example.com\n\n",
            " owner@example.com",
            "owner@example.com ",
        ):
            with self.subTest(value=repr(value)):
                completed = subprocess.CompletedProcess(
                    ["gh"],
                    0,
                    stdout=json.dumps({"value": value}).encode("utf-8"),
                    stderr=b"",
                )
                with (
                    patch(
                        "hermes_codex_router.publish_preflight.subprocess.run",
                        return_value=completed,
                    ),
                    self.assertRaises(publish_preflight.PreflightError),
                ):
                    publish_preflight._require_matching_repository_variable(
                        b"owner@example.com",
                        Path("/repo"),
                        "example-org/example-repository",
                    )

    def test_github_remote_parser_supports_checkout_url_forms(self) -> None:
        self.assertEqual(
            publish_preflight._github_repository(
                "https://github.com/example-org/example-repository.git"
            ),
            "example-org/example-repository",
        )
        self.assertEqual(
            publish_preflight._github_repository(
                "git@" + "github.com:example-org/example-repository.git"
            ),
            "example-org/example-repository",
        )
        self.assertEqual(
            publish_preflight._github_repository(
                "ssh://git@" + "github.com/example-org/example-repository.git"
            ),
            "example-org/example-repository",
        )
        with self.assertRaisesRegex(publish_preflight.PreflightError, "supported GitHub"):
            publish_preflight._github_repository("https://example.com/org/repository.git")

    def test_resolve_policy_uses_env_then_local_git_config(self) -> None:
        root = Path("/repo")
        with patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": "/policy"}):
            self.assertEqual(publish_preflight._policy_path(root), Path("/policy"))

        completed = subprocess.CompletedProcess(
            ["git"], 0, stdout="/configured-policy\n", stderr=""
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("hermes_codex_router.publish_preflight.subprocess.run", return_value=completed),
        ):
            self.assertEqual(publish_preflight._policy_path(root), Path("/configured-policy"))

    def test_install_places_hook_in_shared_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            identity = {
                **_isolated_git_environment(),
                "GIT_AUTHOR_NAME": "Example Author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_NAME": "Example Committer",
                "GIT_COMMITTER_EMAIL": "committer@example.com",
            }
            (root / "tracked.txt").write_text("example\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, env=identity, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "example commit"],
                cwd=root,
                env=identity,
                check=True,
            )
            subprocess.run(
                ["git", "config", "extensions.worktreeConfig", "true"],
                cwd=root,
                check=True,
            )
            linked = base / "linked"
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", "linked-example", str(linked)],
                cwd=root,
                check=True,
            )
            old_hooks = base / "old-hooks"
            old_hooks.mkdir()
            (old_hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (old_hooks / "pre-push").chmod(0o700)
            subprocess.run(
                ["git", "config", "--worktree", "core.hooksPath", str(old_hooks)],
                cwd=linked,
                check=True,
            )
            source = root / ".githooks" / "pre-push"
            source.parent.mkdir()
            source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            source.chmod(0o755)
            policy = base / "public-author-policy"
            policy.write_text("owner@example.com\n", encoding="ascii")
            policy.chmod(0o600)
            with patch.dict(
                os.environ,
                {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)},
                clear=True,
            ):
                publish_preflight._install(root)

            hook_directory = Path(
                subprocess.run(
                    ["git", "config", "--local", "--get", "core.hooksPath"],
                    cwd=root,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
            )
            installed = hook_directory / "pre-push"
            self.assertEqual(installed.read_bytes(), source.read_bytes())
            self.assertEqual(installed.stat().st_mode & 0o777, 0o700)
            self.assertEqual(hook_directory.stat().st_mode & 0o777, 0o700)
            linked_hook_directory = subprocess.run(
                ["git", "config", "--get", "core.hooksPath"],
                cwd=linked,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(linked_hook_directory, str(hook_directory))
            configured_policy = subprocess.run(
                ["git", "config", "--local", "--get", "hub.publicAuthorEmailFile"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(configured_policy, str(policy))
            configured_python = subprocess.run(
                ["git", "config", "--local", "--get", "hub.publishPython"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(configured_python, sys.executable)

    def test_worktree_inventory_skips_only_explicit_prunable_records(self) -> None:
        inventory = (
            b"worktree /home/example/active\0HEAD "
            + b"a" * 40
            + b"\0branch refs/heads/main\0\0"
            + b"worktree /home/example/missing\0HEAD "
            + b"b" * 40
            + b"\0prunable gitdir file points to non-existent location\0\0"
        )
        completed = subprocess.CompletedProcess(["git"], 0, stdout=inventory, stderr=b"")
        with patch("hermes_codex_router.publish_preflight.subprocess.run", return_value=completed):
            self.assertEqual(
                publish_preflight._registered_worktrees(Path("/repo")),
                [Path("/home/example/active")],
            )

    def test_main_binds_imports_and_rechecks_checkout_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "hermes_codex_router").mkdir(parents=True)
            (root / "scripts").mkdir()
            (root / "scripts" / "validate.py").write_text("", encoding="utf-8")
            policy = root.parent / "fictional-public-author-policy"
            policy.write_text("owner@example.com\n", encoding="ascii")
            policy.chmod(0o600)
            calls: list[tuple[list[str], dict[str, str] | None]] = []

            def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
                environment = kwargs.get("env")
                calls.append((argv, environment if isinstance(environment, dict) else None))
                if argv[:3] == ["git", "status", "--porcelain"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                if argv[:2] == ["git", "rev-parse"]:
                    return subprocess.CompletedProcess(argv, 0, stdout=f"{'a' * 40}\n", stderr="")
                if argv[:4] == ["gh", "repo", "view", "--json"]:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=b'{"nameWithOwner":"example-org/example-repository"}',
                        stderr=b"",
                    )
                if argv[:4] == ["gh", "api", "--method", "GET"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=b'{"value":"owner@example.com"}', stderr=b""
                    )
                if Path(argv[-1]).name == "validate.py":
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="PASS documentation (0.10s)\n", stderr=""
                    )
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            stdout = io.StringIO()
            with (
                patch.object(publish_preflight, "ROOT", root),
                patch.object(
                    publish_preflight,
                    "_read_declared_email",
                    return_value=b"owner@example.com",
                ),
                patch.object(publish_preflight, "_require_checkout_import"),
                patch.dict(
                    os.environ,
                    {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)},
                    clear=True,
                ),
                patch("hermes_codex_router.publish_preflight.subprocess.run", fake_run),
                patch("sys.stdin", io.StringIO("")),
                redirect_stdout(stdout),
            ):
                self.assertEqual(publish_preflight.main([]), 0)

            validator = next(call for call in calls if Path(call[0][-1]).name == "validate.py")
            self.assertEqual(validator[0][0], os.fsdecode(os.fsencode(sys.executable)))
            environment = validator[1]
            self.assertIsNotNone(environment)
            assert environment is not None
            self.assertEqual(environment["PYTHONPATH"].split(os.pathsep)[0], str(root / "src"))
            self.assertEqual(environment["HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE"], str(policy))
            self.assertIn("passed", stdout.getvalue())
            self.assertIn("PASS documentation (0.10s)", stdout.getvalue())
            status_calls = [
                call for call in calls if call[0][:3] == ["git", "status", "--porcelain"]
            ]
            self.assertEqual(len(status_calls), 2)

    def test_main_rejects_a_checkout_changed_during_validation(self) -> None:
        status_calls = 0

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            nonlocal status_calls
            if argv[:3] == ["git", "status", "--porcelain"]:
                status_calls += 1
                output = "" if status_calls == 1 else " M fictional.py\n"
                return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
            if argv[:2] == ["git", "rev-parse"]:
                return subprocess.CompletedProcess(argv, 0, stdout=f"{'a' * 40}\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        stderr = io.StringIO()
        with (
            patch.object(publish_preflight, "_require_checkout_import"),
            patch.object(publish_preflight, "_policy_path", return_value=Path("/policy")),
            patch.object(
                publish_preflight, "_read_declared_email", return_value=b"owner@example.com"
            ),
            patch.object(publish_preflight, "_require_matching_repository_variable"),
            patch("hermes_codex_router.publish_preflight.subprocess.run", fake_run),
            patch("sys.stdin", io.StringIO("")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(publish_preflight.main([]), 1)
        self.assertIn("changed during validation", stderr.getvalue())

    def test_main_rejects_a_policy_file_changed_during_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "validate.py").write_text("", encoding="utf-8")
            policy = base / "public-author-policy"
            policy.write_text("owner@example.com\n", encoding="ascii")
            policy.chmod(0o600)

            def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
                if argv[:3] == ["git", "status", "--porcelain"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                if argv[:2] == ["git", "rev-parse"]:
                    return subprocess.CompletedProcess(argv, 0, stdout=f"{'a' * 40}\n", stderr="")
                if Path(argv[-1]).name == "validate.py":
                    policy.write_text("changed@example.com\n", encoding="ascii")
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            stderr = io.StringIO()
            with (
                patch.object(publish_preflight, "ROOT", root),
                patch.object(publish_preflight, "_require_checkout_import"),
                patch.object(publish_preflight, "_require_matching_repository_variable"),
                patch.dict(
                    os.environ,
                    {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)},
                    clear=True,
                ),
                patch("hermes_codex_router.publish_preflight.subprocess.run", fake_run),
                patch("sys.stdin", io.StringIO("")),
                redirect_stderr(stderr),
            ):
                self.assertEqual(publish_preflight.main([]), 1)
            self.assertIn("policy file changed", stderr.getvalue())

    def test_dangling_annotated_tag_cannot_be_published_as_a_raw_oid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str, env: dict[str, str] | None = None) -> str:
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    text=True,
                    capture_output=True,
                    check=True,
                    env=env,
                ).stdout.strip()

            identity = {
                **_isolated_git_environment(),
                "GIT_AUTHOR_NAME": "Example Author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_NAME": "Example Committer",
                "GIT_COMMITTER_EMAIL": "committer@example.com",
            }
            git("init", "-q", env=identity)
            (root / "tracked.txt").write_text("example\n", encoding="utf-8")
            git("add", "tracked.txt", env=identity)
            git("commit", "-q", "-m", "example commit", env=identity)
            head = git("rev-parse", "HEAD", env=identity)
            tag_identity = {
                **identity,
                "GIT_COMMITTER_NAME": "Fictional Private",
                "GIT_COMMITTER_EMAIL": "owner@" + "private.invalid",
            }
            git("tag", "-a", "v1.0.0", "-m", "fictional private tag", env=tag_identity)
            tag_oid = git("rev-parse", "refs/tags/v1.0.0", env=identity)
            self.assertTrue(publish_preflight.privacy_scan.scan_history(root))

            def resolve_commit(oid: str) -> str:
                return publish_preflight._resolve_commit_without_replacements(root, oid)

            publish_preflight._validate_push_refs(
                f"refs/tags/v1.0.0 {tag_oid} refs/tags/v1.0.0 {'0' * 40}\n",
                head,
                resolve_commit,
                lambda ref: publish_preflight._resolve_exact_ref(root, ref),
            )
            git(
                "update-ref",
                "--create-reflog",
                "refs/tags/v1.0.0",
                head,
                tag_oid,
                env=identity,
            )
            self.assertFalse(publish_preflight.privacy_scan.scan_history(root))

            with self.assertRaisesRegex(publish_preflight.PreflightError, "local ref"):
                publish_preflight._validate_push_refs(
                    f"(unknown) {tag_oid} refs/tags/v1.0.0 {'0' * 40}\n",
                    head,
                    resolve_commit,
                    lambda ref: git("rev-parse", ref, env=identity),
                )
            reflog_ref = "refs/tags/v1.0.0@{1}"
            self.assertEqual(git("rev-parse", reflog_ref, env=identity), tag_oid)
            with self.assertRaisesRegex(publish_preflight.PreflightError, "checked local ref"):
                publish_preflight._validate_push_refs(
                    f"{reflog_ref} {tag_oid} refs/tags/v1.0.0 {'0' * 40}\n",
                    head,
                    resolve_commit,
                    lambda ref: publish_preflight._resolve_exact_ref(root, ref),
                )

    def test_tag_commit_peeling_ignores_replace_objects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = {
                **_isolated_git_environment(),
                "GIT_AUTHOR_NAME": "Example Author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_NAME": "Example Committer",
                "GIT_COMMITTER_EMAIL": "committer@example.com",
            }

            def git(*args: str) -> str:
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    text=True,
                    capture_output=True,
                    check=True,
                    env=identity,
                ).stdout.strip()

            git("init", "-q")
            (root / "tracked.txt").write_text("old\n", encoding="utf-8")
            git("add", "tracked.txt")
            git("commit", "-q", "-m", "old example commit")
            old_commit = git("rev-parse", "HEAD")
            git("tag", "-a", "v0.9.0", "-m", "old example tag")
            old_tag = git("rev-parse", "refs/tags/v0.9.0")
            (root / "tracked.txt").write_text("new\n", encoding="utf-8")
            git("commit", "-q", "-am", "new example commit")
            head = git("rev-parse", "HEAD")
            git("tag", "-a", "replacement-example", "-m", "replacement example tag")
            replacement_tag = git("rev-parse", "refs/tags/replacement-example")
            git("replace", old_tag, replacement_tag)

            self.assertEqual(git("rev-parse", f"{old_tag}^{{commit}}"), head)
            self.assertEqual(
                publish_preflight._resolve_commit_without_replacements(root, old_tag),
                old_commit,
            )
            with self.assertRaisesRegex(publish_preflight.PreflightError, "checked-out HEAD"):
                publish_preflight._validate_push_refs(
                    f"refs/tags/v0.9.0 {old_tag} refs/tags/v0.9.0 {'0' * 40}\n",
                    head,
                    lambda oid: publish_preflight._resolve_commit_without_replacements(root, oid),
                    lambda ref: publish_preflight._resolve_exact_ref(root, ref),
                )

    def test_hook_invokes_versioned_preflight_with_checkout_pythonpath(self) -> None:
        hook = (publish_preflight.ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")
        self.assertIn("PYTHONPATH", hook)
        self.assertIn("hub.publishPython", hook)
        self.assertIn("--local-env-vars", hook)
        self.assertIn("hermes_codex_router.publish_preflight", hook)
        self.assertNotIn("--no-verify", hook)

    def test_hook_clears_calling_repository_environment_before_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            interpreter = root / "fake python"
            interpreter.write_text(
                "#!/bin/sh\n"
                'if [ "${GIT_DIR+x}" = x ] || [ "${GIT_INDEX_FILE+x}" = x ]; then\n'
                "  exit 9\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            interpreter.chmod(0o700)
            subprocess.run(
                ["git", "config", "--local", "hub.publishPython", str(interpreter)],
                cwd=root,
                check=True,
            )
            hook = root / "pre-push"
            hook.write_bytes((publish_preflight.ROOT / ".githooks" / "pre-push").read_bytes())
            hook.chmod(0o700)
            git_dir = subprocess.run(
                ["git", "rev-parse", "--absolute-git-dir"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            environment = _isolated_git_environment()
            environment["GIT_DIR"] = git_dir
            environment["GIT_INDEX_FILE"] = str(Path(git_dir) / "index")
            result = subprocess.run(
                [
                    str(hook),
                    "origin",
                    "https://github.com/example-org/example-repository.git",
                ],
                cwd=root,
                env=environment,
                input="",
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
