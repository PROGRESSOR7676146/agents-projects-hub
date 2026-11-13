from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
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
    environment.pop("HUB_PUBLISH_PYTHON", None)
    environment.pop("HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE", None)
    for role in ("AUTHOR", "COMMITTER"):
        for field in ("NAME", "EMAIL"):
            environment.pop(f"GIT_{role}_{field}", None)
    return environment


class PublishPreflightTests(unittest.TestCase):
    def test_streamed_command_reports_progress_before_it_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            output = root / "stdout"
            errors = root / "stderr"
            child = root / "child.py"
            child.write_text(
                "import pathlib, sys, time\n"
                "print('visible progress', flush=True)\n"
                "print('visible diagnostic', file=sys.stderr, flush=True)\n"
                "deadline = time.monotonic() + 40\n"
                "while not pathlib.Path(sys.argv[1]).exists() and time.monotonic() < deadline:\n"
                "    time.sleep(0.02)\n"
                "sys.exit(7)\n",
                encoding="utf-8",
            )
            parent = root / "parent.py"
            parent.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "from hermes_codex_router.publish_preflight import _run\n"
                "result = _run([sys.executable, sys.argv[1], sys.argv[2]], "
                "cwd=Path(sys.argv[3]), timeout=45, stream=True)\n"
                "print(f'child exit: {result.returncode}', flush=True)\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = os.pathsep.join(
                filter(
                    None,
                    (
                        str(Path(publish_preflight.__file__).resolve().parents[1]),
                        environment.get("PYTHONPATH", ""),
                    ),
                )
            )
            with (
                output.open("w", encoding="utf-8") as stdout,
                errors.open("w", encoding="utf-8") as stderr,
            ):
                process = subprocess.Popen(
                    [sys.executable, str(parent), str(child), str(release), str(root)],
                    stdout=stdout,
                    stderr=stderr,
                    stdin=subprocess.DEVNULL,
                    env=environment,
                )
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if "visible progress" in output.read_text(
                            encoding="utf-8"
                        ) and "visible diagnostic" in errors.read_text(encoding="utf-8"):
                            break
                        if process.poll() is not None:
                            break
                        time.sleep(0.02)
                    self.assertIn("visible progress", output.read_text(encoding="utf-8"))
                    self.assertIn("visible diagnostic", errors.read_text(encoding="utf-8"))
                    self.assertIsNone(process.poll(), "progress must arrive while the child runs")
                finally:
                    release.touch()
                    try:
                        process.wait(timeout=50)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        raise
            self.assertEqual(process.returncode, 0)
            self.assertIn("child exit: 7", output.read_text(encoding="utf-8"))

    def test_command_timeouts_allow_local_git_extra_time(self) -> None:
        commands = (
            (["git", "status", "--porcelain"], None, 300),
            (["git", "--no-replace-objects", "rev-parse", "HEAD"], None, 300),
            (["git", "check-ref-format", "refs/heads/example"], None, 300),
            (["git", "config", "--local", "--get", "example.key"], None, 300),
            (["git", "show-ref", "--verify", "refs/heads/example"], None, 300),
            (["git", "worktree", "list"], None, 300),
            (["git", "fetch", "origin"], None, 60),
            (["git", "push", "origin"], None, 60),
            (["git", "ls-remote", "origin"], None, 60),
            (["git", "-c", "example.value=1", "status"], None, 60),
            (["gh", "status"], None, 60),
            (["git", "fetch", "origin"], 7, 7),
            (["git", "status"], 7, 7),
        )
        for run in (publish_preflight._run, publish_preflight._run_bytes):
            for argv, timeout, expected in commands:
                with (
                    self.subTest(run=run.__name__, argv=argv, timeout=timeout),
                    patch("hermes_codex_router.publish_preflight.subprocess.run") as subprocess_run,
                ):
                    run(argv, cwd=Path("/home/example"), timeout=timeout)
                    self.assertEqual(subprocess_run.call_args.kwargs["timeout"], expected)

    def test_command_timeouts_remain_fail_closed(self) -> None:
        for run in (publish_preflight._run, publish_preflight._run_bytes):
            with (
                self.subTest(run=run.__name__),
                patch(
                    "hermes_codex_router.publish_preflight.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(["git", "status"], 300),
                ),
                self.assertRaises(publish_preflight.PreflightError),
            ):
                run(["git", "status"], cwd=Path("/home/example"))

        with (
            patch(
                "hermes_codex_router.publish_preflight.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["validator"], 10),
            ),
            self.assertRaises(publish_preflight.PreflightError),
        ):
            publish_preflight._run(
                ["validator"], cwd=Path("/home/example"), timeout=10, stream=True
            )

    def test_nested_fixture_drops_outer_identity_and_policy(self) -> None:
        names = ["HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE"] + [
            f"GIT_{role}_{field}" for role in ("AUTHOR", "COMMITTER") for field in ("NAME", "EMAIL")
        ]
        with patch.dict(os.environ, {name: "fictional" for name in names}):
            clean = _isolated_git_environment()
        self.assertTrue(all(name not in clean for name in names))

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

    def test_deletion_only_push_skips_checkout_and_publication_validation(self) -> None:
        for oid_length in (40, 64):
            source = f"(delete) {'0' * oid_length} refs/heads/old {'a' * oid_length}\n"
            with (
                self.subTest(oid_length=oid_length),
                patch("sys.stdin", io.StringIO(source)),
                patch.object(publish_preflight, "_require_checkout_import") as checkout,
                patch.object(publish_preflight, "_git_stdout") as git_stdout,
                patch.object(publish_preflight, "_run", wraps=publish_preflight._run) as run,
                patch.object(publish_preflight.privacy_scan, "scan_history") as history,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(publish_preflight.main([]), 0)
                checkout.assert_not_called()
                git_stdout.assert_not_called()
                history.assert_not_called()
                self.assertTrue(
                    all(
                        call.args[0][:2] == ["git", "check-ref-format"]
                        for call in run.call_args_list
                    )
                )

    def test_mixed_deletion_and_update_runs_full_preflight(self) -> None:
        head = "a" * 40
        source = (
            f"(delete) {'0' * 40} refs/heads/old {'b' * 40}\n"
            f"refs/heads/new {head} refs/heads/new {'0' * 40}\n"
        )
        calls: list[list[str]] = []

        def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with (
            patch("sys.stdin", io.StringIO(source)),
            patch.object(publish_preflight, "_require_checkout_import"),
            patch.object(publish_preflight, "_git_stdout", return_value=head),
            patch.object(publish_preflight, "_resolve_exact_ref", return_value=head),
            patch.object(
                publish_preflight, "_resolve_commit_without_replacements", return_value=head
            ),
            patch.object(publish_preflight, "_run", side_effect=run),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(publish_preflight.main([]), 0)

        self.assertEqual(sum(Path(argv[-1]).name == "validate.py" for argv in calls), 1)
        self.assertEqual(sum(argv[:2] == ["git", "status"] for argv in calls), 2)

    def test_mixed_refs_are_classified_before_checking_deletion_ref_formats(self) -> None:
        source = (
            f"(delete) {'0' * 40} refs/heads/old {'b' * 40}\n"
            f"refs/heads/new {'a' * 40} refs/heads/new {'0' * 40}\n"
        )
        with patch.object(publish_preflight, "_run") as run:
            self.assertFalse(publish_preflight._deletion_only(source))
            run.assert_not_called()

    def test_non_utf8_hook_input_fails_closed_without_traceback(self) -> None:
        raw = f"(delete) {'0' * 40} refs/heads/".encode() + b"\xff" + f" {'a' * 40}\n".encode()
        for encoding, errors in (
            ("utf-8", "strict"),
            ("utf-8", "surrogateescape"),
            ("latin-1", "strict"),
        ):
            with (
                self.subTest(encoding=encoding, errors=errors),
                io.TextIOWrapper(io.BytesIO(raw), encoding=encoding, errors=errors) as source,
                patch("sys.stdin", source),
                patch.object(publish_preflight, "_run") as run,
                redirect_stderr(io.StringIO()) as stderr,
            ):
                self.assertEqual(publish_preflight.main([]), 1)
                run.assert_not_called()
                self.assertIn("Publication preflight failed:", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

    def test_malformed_deletion_input_fails_closed(self) -> None:
        zero, oid = "0" * 40, "a" * 40
        real_run = publish_preflight._run

        def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["git", "check-ref-format"]:
                return real_run(argv, **kwargs)  # type: ignore[arg-type]
            if argv[:2] == ["git", "status"]:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            self.fail(f"malformed input reached an unexpected command: {argv}")

        for source in (
            f"(delete) {zero} refs/heads/old\n",
            f"(delete) {zero} refs/heads/old not-an-oid\n",
            f"(unknown) {zero} refs/heads/old {oid}\n",
            f"(delete) {zero} refs/heads/../bad {oid}\n",
            f"(delete) {zero} refs/heads/old {oid}\nmalformed\n",
            f"refs/heads/new {oid} refs/heads/../bad {zero}\n",
            f"(delete) {zero} refs/heads/old {oid}\n"
            f"refs/heads/new {oid} refs/heads/../bad {zero}\n",
        ):
            with (
                self.subTest(source=source),
                patch("sys.stdin", io.StringIO(source)),
                patch.object(publish_preflight, "_require_checkout_import"),
                patch.object(publish_preflight, "_git_stdout", return_value=oid),
                patch.object(publish_preflight, "_run", side_effect=run),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(publish_preflight.main([]), 1)

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
            commit_source = root / ".githooks" / "pre-commit"
            commit_source.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
            commit_source.chmod(0o755)
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
            for name, expected in (("pre-push", source), ("pre-commit", commit_source)):
                installed = hook_directory / name
                self.assertEqual(installed.read_bytes(), expected.read_bytes())
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
            calls: list[tuple[list[str], dict[str, str] | None, bool]] = []

            def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
                environment = kwargs.get("env")
                calls.append(
                    (
                        argv,
                        environment if isinstance(environment, dict) else None,
                        not bool(kwargs.get("capture_output")),
                    )
                )
                if argv[:3] == ["git", "status", "--porcelain"]:
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                if argv[:2] == ["git", "rev-parse"]:
                    return subprocess.CompletedProcess(argv, 0, stdout=f"{'a' * 40}\n", stderr="")
                if argv[0] == "gh":
                    raise AssertionError("the preflight must not query GitHub")
                if Path(argv[-1]).name == "validate.py":
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="PASS documentation (0.10s)\n", stderr=""
                    )
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            stdout = io.StringIO()
            with (
                patch.object(publish_preflight, "ROOT", root),
                patch.object(publish_preflight, "_require_checkout_import"),
                patch.dict(os.environ, {}, clear=True),
                patch("hermes_codex_router.publish_preflight.subprocess.run", fake_run),
                patch("sys.stdin", io.StringIO("")),
                redirect_stdout(stdout),
            ):
                self.assertEqual(publish_preflight.main([]), 0)

            validator = next(call for call in calls if Path(call[0][-1]).name == "validate.py")
            self.assertTrue(
                validator[2], "canonical validation must forward progress during a push"
            )
            self.assertEqual(validator[0][0], os.fsdecode(os.fsencode(sys.executable)))
            environment = validator[1]
            self.assertIsNotNone(environment)
            assert environment is not None
            self.assertEqual(environment["PYTHONPATH"].split(os.pathsep)[0], str(root / "src"))
            self.assertIn("passed", stdout.getvalue())
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
            patch("hermes_codex_router.publish_preflight.subprocess.run", fake_run),
            patch("sys.stdin", io.StringIO("")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(publish_preflight.main([]), 1)
        self.assertIn("changed during validation", stderr.getvalue())

    def test_main_rejects_a_failing_streamed_validator(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            calls.append(argv)
            status = 7 if Path(argv[-1]).name == "validate.py" else 0
            return subprocess.CompletedProcess(argv, status, stdout="", stderr="")

        stderr = io.StringIO()
        with (
            patch.object(publish_preflight, "_require_checkout_import"),
            patch.object(publish_preflight, "_git_stdout", return_value="a" * 40),
            patch.object(publish_preflight, "_run", side_effect=fake_run),
            patch("sys.stdin", io.StringIO("")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(publish_preflight.main([]), 1)
        self.assertIn("canonical repository validation failed", stderr.getvalue())
        self.assertEqual(sum(argv[:2] == ["git", "status"] for argv in calls), 1)

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
            # Tagger identity is publishable; the message leak keeps the tag private.
            message = "fictional private tag for owner@" + "private.invalid"
            git("tag", "-a", "v1.0.0", "-m", message, env=tag_identity)
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

    def test_install_refuses_an_incomplete_hook_set_without_partial_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            source = root / ".githooks" / "pre-push"
            source.parent.mkdir()
            source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            policy = base / "public-author-policy"
            policy.write_text("owner@example.com\n", encoding="ascii")
            policy.chmod(0o600)
            with (
                patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)}),
                self.assertRaises(publish_preflight.PreflightError) as raised,
            ):
                publish_preflight._install(root)
            self.assertIn("pre-commit", str(raised.exception))
            self.assertFalse((root / ".git" / publish_preflight.HOOK_ROOT).exists())

    def installed_fixture(self, base: Path) -> tuple[Path, Path, Path]:
        """A repository with version-one hooks installed and version-two sources."""
        root = base / "checkout"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        hooks = root / ".githooks"
        hooks.mkdir()
        policy = base / "public-author-policy"
        policy.write_text("owner@example.com\n", encoding="ascii")
        policy.chmod(0o600)
        for name in publish_preflight.MANAGED_HOOKS:
            (hooks / name).write_text(f"#!/bin/sh\n# {name} v1\n", encoding="utf-8")
        with patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)}):
            publish_preflight._install(root)
        for name in publish_preflight.MANAGED_HOOKS:
            (hooks / name).write_text(f"#!/bin/sh\n# {name} v2\n", encoding="utf-8")
        return root, root / ".git" / publish_preflight.HOOK_ROOT / "active", policy

    def local_config(self, root: Path) -> dict[str, str]:
        keys = ("core.hooksPath", "hub.publicAuthorEmailFile", "hub.publishPython")
        return {
            key: subprocess.run(
                ["git", "config", "--local", "--get", key],
                cwd=root,
                text=True,
                capture_output=True,
            ).stdout
            for key in keys
        }

    def assert_hooks(self, hook_directory: Path, version: str) -> None:
        for name in publish_preflight.MANAGED_HOOKS:
            self.assertEqual(
                (hook_directory / name).read_text(encoding="utf-8"),
                f"#!/bin/sh\n# {name} {version}\n",
            )
        self.assertEqual(
            sorted(path.name for path in hook_directory.iterdir()),
            sorted(publish_preflight.MANAGED_HOOKS),
        )

    def assert_version_one_hooks(self, hook_directory: Path) -> None:
        self.assert_hooks(hook_directory, "v1")

    def test_update_switches_the_whole_set_and_removes_the_old_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, active, policy = self.installed_fixture(Path(directory))
            sets = active.parent / "sets"
            first = os.readlink(active)
            with patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)}):
                publish_preflight._install(root)
            self.assertNotEqual(os.readlink(active), first)
            self.assert_hooks(active, "v2")
            self.assertEqual(
                [path.name for path in sets.iterdir()], [Path(os.readlink(active)).name]
            )
            self.assertEqual(self.local_config(root)["core.hooksPath"], f"{active}\n")

    def test_persistent_filesystem_failure_keeps_the_previous_set_active(self) -> None:
        for operation in ("rename", "symlink", "replace"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root, active, policy = self.installed_fixture(Path(directory))
                before = self.local_config(root)
                sets_before = sorted(path.name for path in (active.parent / "sets").iterdir())

                def fail(*_args: object, **_kwargs: object) -> None:
                    raise OSError(28, "No space left on device")

                with (
                    patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)}),
                    patch.object(publish_preflight.os, operation, side_effect=fail),
                    self.assertRaises(publish_preflight.PreflightError) as raised,
                ):
                    publish_preflight._install(root)
                self.assertNotIn("could not be confirmed", str(raised.exception))
                self.assert_version_one_hooks(active)
                self.assertEqual(self.local_config(root), before)
                self.assertEqual(
                    sorted(path.name for path in (active.parent / "sets").iterdir()), sets_before
                )

    def test_failed_configuration_restores_hooks_and_local_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, hook_directory, policy = self.installed_fixture(Path(directory))
            subprocess.run(
                ["git", "config", "--local", "hub.publishPython", "/home/example/python"],
                cwd=root,
                check=True,
            )
            before = self.local_config(root)
            real_run = publish_preflight._run

            def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ["git", "config", "--local"] and argv[3] == "hub.publishPython":
                    return subprocess.CompletedProcess(argv, 1, "", "")
                return real_run(argv, **kwargs)  # type: ignore[arg-type]

            with (
                patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)}),
                patch.object(publish_preflight, "_run", side_effect=run),
                self.assertRaises(publish_preflight.PreflightError),
            ):
                publish_preflight._install(root)
            self.assert_version_one_hooks(hook_directory)
            self.assertEqual(self.local_config(root), before)
            self.assertEqual(len(list((hook_directory.parent / "sets").iterdir())), 1)

    def test_rollback_that_cannot_be_confirmed_is_reported_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, active, policy = self.installed_fixture(Path(directory))
            real_run = publish_preflight._run
            real_replace = os.replace
            switches: list[str] = []

            def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ["git", "config", "--local"] and argv[3] == "hub.publishPython":
                    return subprocess.CompletedProcess(argv, 1, "", "")
                return real_run(argv, **kwargs)  # type: ignore[arg-type]

            def replace(source: object, destination: object) -> None:
                switches.append(str(destination))
                if len(switches) > 1:
                    raise OSError(28, "No space left on device")
                real_replace(source, destination)  # type: ignore[arg-type]

            with (
                patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)}),
                patch.object(publish_preflight, "_run", side_effect=run),
                patch.object(publish_preflight.os, "replace", side_effect=replace),
                self.assertRaises(publish_preflight.PreflightError) as raised,
            ):
                publish_preflight._install(root)
            self.assertIn("could not be confirmed restored", str(raised.exception))
            self.assertEqual(switches, [str(active), str(active)])
            # The link never points at a partial set, whichever set it ended on.
            self.assert_hooks(active, "v2")

    def test_persistent_configuration_failure_never_leaves_git_without_hooks(self) -> None:
        for migration in (False, True):
            with self.subTest(migration=migration), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "checkout"
                root.mkdir()
                subprocess.run(["git", "init", "-q"], cwd=root, check=True)
                hooks = root / ".githooks"
                hooks.mkdir()
                legacy = root / ".git" / publish_preflight.LEGACY_HOOK_DIRECTORY
                if migration:
                    legacy.mkdir(mode=0o700)
                for name in publish_preflight.MANAGED_HOOKS:
                    (hooks / name).write_text(f"#!/bin/sh\n# {name} v2\n", encoding="utf-8")
                    if migration:
                        (legacy / name).write_text(f"#!/bin/sh\n# {name} v1\n", encoding="utf-8")
                if migration:
                    subprocess.run(
                        ["git", "config", "--local", "core.hooksPath", str(legacy)],
                        cwd=root,
                        check=True,
                    )
                policy = base / "public-author-policy"
                policy.write_text("owner@example.com\n", encoding="ascii")
                policy.chmod(0o600)
                lock = root / ".git" / "config.lock"
                real_run = publish_preflight._run

                def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                    result = real_run(argv, **kwargs)  # type: ignore[arg-type]
                    if argv[:4] == ["git", "config", "--local", "core.hooksPath"] and (
                        len(argv) == 5 and result.returncode == 0
                    ):
                        # From here on Git can no longer change its configuration.
                        lock.write_text("", encoding="utf-8")
                    return result

                try:
                    with (
                        patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)}),
                        patch.object(publish_preflight, "_run", side_effect=run),
                        self.assertRaises(publish_preflight.PreflightError) as raised,
                    ):
                        publish_preflight._install(root)
                finally:
                    lock.unlink(missing_ok=True)
                self.assertIn("could not be confirmed", str(raised.exception))
                effective = subprocess.run(
                    ["git", "config", "--get", "core.hooksPath"],
                    cwd=root,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                # Whatever Git points at still holds a complete gate.
                self.assert_hooks(Path(effective), "v2")
                if migration:
                    self.assert_version_one_hooks(legacy)

    def test_failed_worktree_update_restores_every_worktree_on_first_install(self) -> None:
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
                ["git", "commit", "-q", "-m", "example commit"], cwd=root, env=identity, check=True
            )
            subprocess.run(
                ["git", "config", "extensions.worktreeConfig", "true"], cwd=root, check=True
            )
            linked = base / "linked"
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", "linked-example", str(linked)],
                cwd=root,
                check=True,
            )
            previous: dict[Path, Path] = {}
            for worktree in (root, linked):
                old_hooks = base / f"old-hooks-{worktree.name}"
                old_hooks.mkdir()
                for name in publish_preflight.MANAGED_HOOKS:
                    (old_hooks / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    (old_hooks / name).chmod(0o700)
                subprocess.run(
                    ["git", "config", "--worktree", "core.hooksPath", str(old_hooks)],
                    cwd=worktree,
                    check=True,
                )
                previous[worktree] = old_hooks
            hooks = root / ".githooks"
            hooks.mkdir()
            for name in publish_preflight.MANAGED_HOOKS:
                (hooks / name).write_text(f"#!/bin/sh\n# {name} v1\n", encoding="utf-8")
            policy = base / "public-author-policy"
            policy.write_text("owner@example.com\n", encoding="ascii")
            policy.chmod(0o600)
            before = self.local_config(root)
            real_run = publish_preflight._run

            def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if (
                    argv[:4] == ["git", "config", "--worktree", "core.hooksPath"]
                    and len(argv) == 5
                    and Path(str(kwargs["cwd"])) == linked
                ):
                    return subprocess.CompletedProcess(argv, 1, "", "")
                return real_run(argv, **kwargs)  # type: ignore[arg-type]

            with (
                patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)}),
                patch.object(publish_preflight, "_run", side_effect=run),
                self.assertRaises(publish_preflight.PreflightError) as raised,
            ):
                publish_preflight._install(root)
            self.assertNotIn("could not be confirmed", str(raised.exception))
            for worktree, old_hooks in previous.items():
                effective = subprocess.run(
                    ["git", "config", "--get", "core.hooksPath"],
                    cwd=worktree,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                self.assertEqual(effective, str(old_hooks))
                for name in publish_preflight.MANAGED_HOOKS:
                    self.assertTrue((old_hooks / name).is_file())
            self.assertEqual(self.local_config(root), before)
            self.assertFalse(
                os.path.lexists(root / ".git" / publish_preflight.HOOK_ROOT / "active")
            )

    def test_migration_from_the_single_directory_keeps_it_until_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            legacy = root / ".git" / publish_preflight.LEGACY_HOOK_DIRECTORY
            legacy.mkdir(mode=0o700)
            hooks = root / ".githooks"
            hooks.mkdir()
            for name in publish_preflight.MANAGED_HOOKS:
                (legacy / name).write_text(f"#!/bin/sh\n# {name} v1\n", encoding="utf-8")
                (hooks / name).write_text(f"#!/bin/sh\n# {name} v2\n", encoding="utf-8")
            subprocess.run(
                ["git", "config", "--local", "core.hooksPath", str(legacy)], cwd=root, check=True
            )
            policy = base / "public-author-policy"
            policy.write_text("owner@example.com\n", encoding="ascii")
            policy.chmod(0o600)
            before = self.local_config(root)
            with (
                patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)}),
                patch.object(
                    publish_preflight.os, "replace", side_effect=OSError(28, "No space left")
                ),
                self.assertRaises(publish_preflight.PreflightError),
            ):
                publish_preflight._install(root)
            self.assert_version_one_hooks(legacy)
            self.assertEqual(self.local_config(root), before)

            with patch.dict(os.environ, {"HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE": str(policy)}):
                publish_preflight._install(root)
            active = root / ".git" / publish_preflight.HOOK_ROOT / "active"
            self.assert_hooks(active, "v2")
            self.assertEqual(self.local_config(root)["core.hooksPath"], f"{active}\n")
            self.assertFalse(legacy.exists())

    def test_commit_hook_validates_exactly_what_is_committed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "checkout"
            root.mkdir()
            log = base / "calls.log"
            environment = {
                **_isolated_git_environment(),
                "GIT_AUTHOR_NAME": "Example Author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_NAME": "Example Committer",
                "GIT_COMMITTER_EMAIL": "committer@example.com",
            }

            def git(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", *args], cwd=root, env=environment, text=True, capture_output=True
                )

            git("init", "-q")
            (root / "scripts").mkdir()
            (root / "scripts" / "validate.py").write_text("", encoding="utf-8")
            (root / "tracked.txt").write_text("one\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-q", "-m", "initial")
            hooks = base / "hooks"
            hooks.mkdir()
            hook = hooks / "pre-commit"
            hook.write_bytes((publish_preflight.ROOT / ".githooks" / "pre-commit").read_bytes())
            hook.chmod(0o700)
            interpreter = base / "fake python"
            git("config", "--local", "core.hooksPath", str(hooks))
            git("config", "--local", "hub.publishPython", str(interpreter))

            def install_interpreter(help_text: str) -> None:
                # Git exports the commit's index to hooks; the validator must
                # never see it, or nested test repositories would use it.
                interpreter.write_text(
                    "#!/bin/sh\n"
                    'if [ "${GIT_DIR+x}" = x ] || [ "${GIT_INDEX_FILE+x}" = x ]; then\n'
                    "  exit 9\n"
                    "fi\n"
                    'if [ "$2" = --help ]; then\n'
                    f"  echo '{help_text}'\n"
                    "  exit 0\n"
                    "fi\n"
                    f'echo "$*" >> "{log}"\n'
                    "exit 0\n",
                    encoding="utf-8",
                )
                interpreter.chmod(0o700)

            def calls() -> list[str]:
                return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

            install_interpreter("--profile {canonical,commit,focused}")
            (root / "tracked.txt").write_text("two\n", encoding="utf-8")
            git("add", "tracked.txt")
            committed = git("commit", "-q", "-m", "staged change")
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertTrue(calls()[-1].endswith("scripts/validate.py --profile commit"))

            # `commit -a` validates Git's temporary commit index, not the old one.
            (root / "tracked.txt").write_text("three\n", encoding="utf-8")
            committed = git("commit", "-q", "-a", "-m", "all tracked changes")
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertEqual(len(calls()), 2)

            (root / "tracked.txt").write_text("four\n", encoding="utf-8")
            (root / "other.txt").write_text("staged\n", encoding="utf-8")
            git("add", "other.txt")
            refused = git("commit", "-q", "-m", "partial")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("unstaged changes", refused.stderr)
            git("add", "tracked.txt")
            (root / "untracked.txt").write_text("loose\n", encoding="utf-8")
            refused = git("commit", "-q", "-m", "with untracked")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("untracked files", refused.stderr)
            self.assertEqual(len(calls()), 2)

            (root / "untracked.txt").unlink()
            install_interpreter("--profile {canonical,focused}")
            committed = git("commit", "-q", "-m", "older validator")
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertTrue(calls()[-2].endswith("scripts/validate.py --profile focused"))
            self.assertIn("-m hermes_codex_router.privacy_scan", calls()[-1])
            self.assertTrue(calls()[-1].endswith("--history"))

            # A namespace has a different Python path from the host's local
            # config. The explicit launch override must actually execute it.
            git("config", "--local", "hub.publishPython", "/missing/host-python")
            environment["HUB_PUBLISH_PYTHON"] = str(interpreter)
            (root / "tracked.txt").write_text("namespace change\n", encoding="utf-8")
            git("add", "tracked.txt")
            committed = git("commit", "-q", "-m", "namespace interpreter")
            self.assertEqual(committed.returncode, 0, committed.stderr)
            self.assertTrue(calls()[-1].endswith("--history"))


if __name__ == "__main__":
    unittest.main()
