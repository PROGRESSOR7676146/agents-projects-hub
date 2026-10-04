from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[reportMissingModuleSource]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
_BOOL_TAG = "tag:yaml.org,2002:bool"


class GithubActionsLoader(yaml.SafeLoader):
    """Parse GitHub Actions YAML without treating its ``on`` key as a boolean."""


GithubActionsLoader.yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for _initial, _resolvers in GithubActionsLoader.yaml_implicit_resolvers.items():
    GithubActionsLoader.yaml_implicit_resolvers[_initial] = [
        resolver for resolver in _resolvers if resolver[0] != _BOOL_TAG
    ]
GithubActionsLoader.add_implicit_resolver(
    _BOOL_TAG,
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _workflow(path: Path) -> dict[str, Any]:
    loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=GithubActionsLoader)
    if not isinstance(loaded, dict):
        raise AssertionError(f"workflow {path.name} must be a mapping")
    return cast(dict[str, Any], loaded)


def _jobs(workflow: dict[str, Any]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        raise AssertionError("workflow jobs must be a mapping")
    return cast(dict[str, Any], jobs)


def _assert_local_reusable_workflow(job: dict[str, Any]) -> None:
    _assert_required_execution(job)
    if job.get("uses") != "./.github/workflows/validate.yml":
        raise AssertionError("workflow must call the local reusable validation workflow")
    if "secrets" in job:
        raise AssertionError("validation workflow must not inherit secrets")


def _assert_required_execution(job: dict[str, Any]) -> None:
    for item in [job, *job.get("steps", [])]:
        if "if" in item or item.get("continue-on-error", False) is not False:
            raise AssertionError("required execution must not skip or tolerate failure")


def _assert_namespace_job(job: dict[str, Any]) -> None:
    _assert_required_execution(job)
    if job.get("runs-on") != "ubuntu-24.04":
        raise AssertionError("namespace job must use Ubuntu 24.04")
    if job.get("env") != {"HUB_REQUIRE_NAMESPACE_TESTS": "1"}:
        raise AssertionError("namespace job must require real namespace tests")
    if "strategy" in job or "needs" in job:
        raise AssertionError("namespace job must run independently and unconditionally")
    steps = job.get("steps")
    if not isinstance(steps, list) or len(steps) != 7:
        raise AssertionError("namespace job must have seven required setup and test steps")
    checkout, setup_python, setup_uv, tools, apparmor, install, invocation = steps
    if checkout != {"uses": "actions/checkout@v6"}:
        raise AssertionError("namespace job must check out the event commit")
    if setup_python != {"uses": "actions/setup-python@v6", "with": {"python-version": "3.12"}}:
        raise AssertionError("namespace job must select Python 3.12")
    if setup_uv != {"uses": "astral-sh/setup-uv@v7"}:
        raise AssertionError("namespace job must install uv")
    expected_tools = (
        "sudo apt-get update\n"
        "sudo apt-get install --yes bubblewrap apparmor\n"
        "/usr/bin/bwrap --help | grep -F -- '--bind-fd'\n"
        "/usr/bin/bwrap --help | grep -F -- '--ro-bind-fd'\n"
    )
    if tools.get("run") != expected_tools:
        raise AssertionError("namespace job must install compatible system bubblewrap")
    expected_apparmor = (
        "sudo install -m 0644 /dev/stdin /etc/apparmor.d/hub-ci-bwrap <<'APPARMOR'\n"
        "abi <abi/4.0>,\n"
        "include <tunables/global>\n"
        "profile hub-ci-bwrap /usr/bin/bwrap flags=(unconfined) {\n"
        "  userns,\n"
        "}\n"
        "APPARMOR\n"
        "sudo apparmor_parser --replace /etc/apparmor.d/hub-ci-bwrap\n"
    )
    if apparmor.get("run") != expected_apparmor:
        raise AssertionError("namespace job must scope CI userns access to system bwrap")
    if install.get("env") != {"UV_PYTHON": "3.12"} or install.get("run") != (
        "uv sync --locked --extra dev"
    ):
        raise AssertionError("namespace job must install locked Python 3.12 dev dependencies")
    if invocation.get("run") != (
        ".venv/bin/python -m unittest -v "
        "tests.test_claude_file_sandbox.ClaudeFileSandboxTests."
        "test_namespace_denies_private_symlink_git_write_and_host_paths "
        "tests.test_claude_permission_host_roundtrip.PermissionHostRoundtripTests."
        "test_namespace_client_preserves_peer_gate_and_atomic_allow_deny "
        "tests.test_claude_custody_rehearsal.ClaudeCustodyRehearsalTests."
        "test_namespace_blocks_authority_aliases_and_privilege_but_shares_network"
    ):
        raise AssertionError("namespace job must run all three real namespace scenarios")


def _assert_ruleset_required_checks(script: str, workflows: Path) -> None:
    ci_job = next(
        name
        for name, job in _jobs(_workflow(workflows / "ci.yml")).items()
        if job.get("uses") == "./.github/workflows/validate.yml"
    )
    validation_jobs = _jobs(_workflow(workflows / "validate.yml"))
    expected = {
        f"{ci_job} / validate ({version})"
        for version in validation_jobs["validate"]["strategy"]["matrix"]["python-version"]
    }
    expected.add(f"{ci_job} / namespace")
    actual = set(re.findall(r'"context": "([^"]+)"', script))
    if actual != expected:
        raise AssertionError(f"ruleset required checks mismatch: {actual ^ expected}")


def _assert_validation_contract(workflows: Path) -> None:
    validation = _workflow(workflows / "validate.yml")
    if validation.get("on") != {"workflow_call": None}:
        raise AssertionError("validation workflow must be callable only as a reusable workflow")
    if validation.get("permissions") != {"contents": "read"}:
        raise AssertionError("validation workflow must have read-only contents permission")

    validation_jobs = _jobs(validation)
    if set(validation_jobs) != {"validate", "namespace"}:
        raise AssertionError("validation workflow must have matrix and namespace jobs")
    validate_job = validation_jobs["validate"]
    _assert_namespace_job(validation_jobs["namespace"])
    _assert_required_execution(validate_job)
    if validate_job.get("runs-on") != "ubuntu-latest":
        raise AssertionError("validation job runner changed")
    strategy = validate_job.get("strategy")
    if not isinstance(strategy, dict) or strategy.get("fail-fast") is not False:
        raise AssertionError("validation matrix must not fail fast")
    matrix = strategy.get("matrix")
    if matrix != {"python-version": ["3.11", "3.12", "3.13"]}:
        raise AssertionError("validation matrix must cover Python 3.11 through 3.13")
    steps = validate_job.get("steps")
    if not isinstance(steps, list):
        raise AssertionError("validation job must define steps")
    checkout_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("uses") == "actions/checkout@v6"
    ]
    if len(checkout_steps) != 1:
        raise AssertionError("validation must use one checked-in checkout action")
    checkout = checkout_steps[0]
    checkout_with = checkout.get("with")
    if not isinstance(checkout_with, dict) or checkout_with.get("fetch-depth") != 0:
        raise AssertionError("validation checkout must retain complete history")
    if "ref" in checkout_with:
        raise AssertionError("validation checkout must use the event commit")
    prepare = next(
        (
            step
            for step in steps
            if isinstance(step, dict)
            and step.get("name") == "Prepare optional public author policy"
        ),
        None,
    )
    if not isinstance(prepare, dict) or prepare.get("env") != {
        "HUB_PUBLIC_GIT_AUTHOR_EMAIL": "${{ vars.HUB_PUBLIC_GIT_AUTHOR_EMAIL }}"
    }:
        raise AssertionError("public author policy must come only from the repository variable")
    if "${{" in cast(str, prepare.get("run", "")):
        raise AssertionError("Actions expressions must not be embedded in policy shell source")
    validation_commands: list[str] = [
        cast(str, step["run"])
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    if validation_commands != [
        "uv sync --locked --extra dev",
        "python scripts/prepare_public_author_policy.py",
        ".venv/bin/python scripts/validate.py",
        "timeout --signal=ABRT --kill-after=60 900 .venv/bin/python -X faulthandler "
        "-m unittest discover -s tests -q",
    ]:
        raise AssertionError("validation must prepare policy input and run only the canonical gate")
    install = next(
        step
        for step in steps
        if isinstance(step, dict) and step.get("run") == "uv sync --locked --extra dev"
    )
    if install.get("env") != {"UV_PYTHON": "${{ matrix.python-version }}"}:
        raise AssertionError("locked install must target the matrix Python version")

    ci = _workflow(workflows / "ci.yml")
    if ci.get("on") != {"push": {"branches": ["main"]}, "pull_request": None}:
        raise AssertionError("CI triggers changed")
    if ci.get("permissions") != {"contents": "read"}:
        raise AssertionError("CI workflow default permission must be read-only")
    if ci.get("concurrency") != {
        "group": "ci-${{ github.workflow }}-${{ github.ref }}",
        "cancel-in-progress": True,
    }:
        raise AssertionError("CI cancellation policy changed")
    ci_jobs = _jobs(ci)
    _assert_local_reusable_workflow(ci_jobs.get("validation", {}))

    release = _workflow(workflows / "release.yml")
    if release.get("on") != {"push": {"tags": ["v*"]}}:
        raise AssertionError("release trigger changed")
    release_jobs = _jobs(release)
    _assert_local_reusable_workflow(release_jobs.get("validation", {}))
    release_job = release_jobs.get("release")
    if not isinstance(release_job, dict):
        raise AssertionError("release publication job is missing")
    if release_job.get("needs") != "validation" or "if" in release_job:
        raise AssertionError(
            "release publication must depend on validation without a bypass condition"
        )
    _assert_required_execution(release_job)
    if release.get("permissions") != {"contents": "read"}:
        raise AssertionError("release workflow default permission must be read-only")
    if release_job.get("permissions") != {"contents": "write"}:
        raise AssertionError("only release publication may have contents write permission")
    for job_name, job in {**ci_jobs, **release_jobs}.items():
        permissions = job.get("permissions") if isinstance(job, dict) else None
        if (
            job_name != "release"
            and isinstance(permissions, dict)
            and permissions.get("contents") == "write"
        ):
            raise AssertionError("only release publication may have contents write permission")
    release_steps = release_job.get("steps")
    if not isinstance(release_steps, list):
        raise AssertionError("release job must define steps")
    release_checkout = next(
        (
            step
            for step in release_steps
            if isinstance(step, dict) and step.get("uses") == "actions/checkout@v6"
        ),
        None,
    )
    if not isinstance(release_checkout, dict):
        raise AssertionError("release must check out the event commit")
    release_checkout_with = release_checkout.get("with")
    if not isinstance(release_checkout_with, dict) or release_checkout_with.get("fetch-depth") != 0:
        raise AssertionError("release checkout must retain complete history")
    if "ref" in release_checkout_with:
        raise AssertionError("release checkout must use the event commit")
    release_commands: list[str] = [
        cast(str, step["run"])
        for step in release_steps
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    if not any(
        "git rev-parse HEAD" in command and "RELEASE_TAG" in command for command in release_commands
    ):
        raise AssertionError("release must verify event SHA and peeled tag before publication")
    if 'gh release create "$RELEASE_TAG" --verify-tag --generate-notes' not in release_commands:
        raise AssertionError("release publication command changed")

    validator = " ".join((ROOT / "scripts" / "validate.py").read_text(encoding="utf-8").split())
    for required_stage in (
        '"hermes_codex_router.privacy_scan", str(ROOT), "--history"',
        "check_release_lock()",
        '"hermes_codex_router.hotspot_audit", str(ROOT)',
        '("full tests", lambda: run_test_modules(jobs=jobs))',
        '"hermes_codex_router.documentation_contract", str(ROOT)',
        '"hermes_codex_router.cli",',
    ):
        if required_stage not in validator:
            raise AssertionError(f"canonical validation gate lost required stage: {required_stage}")


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_contract(self) -> None:
        _assert_validation_contract(WORKFLOWS)

    def test_ruleset_requires_the_checks_the_validation_matrix_reports(self) -> None:
        script = (ROOT / "scripts" / "configure-github.sh").read_text(encoding="utf-8")
        _assert_ruleset_required_checks(script, WORKFLOWS)

    def test_ruleset_rejects_missing_namespace_required_check(self) -> None:
        script = (ROOT / "scripts" / "configure-github.sh").read_text(encoding="utf-8")
        original = (
            '{"context": "validation / validate (3.13)"},\n'
            '          {"context": "validation / namespace"}'
        )
        self.assertIn(original, script)
        script = script.replace(original, '{"context": "validation / validate (3.13)"}')
        with self.assertRaisesRegex(AssertionError, "ruleset required checks mismatch"):
            _assert_ruleset_required_checks(script, WORKFLOWS)

    def test_contract_rejects_missing_or_bypassed_namespace_invocation(self) -> None:
        for mutation in ("missing", "missing_custody", "wrong_command", "tolerate_failure"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                workflows = Path(directory) / "workflows"
                shutil.copytree(WORKFLOWS, workflows)
                path = workflows / "validate.yml"
                validation = _workflow(path)
                invocation = validation["jobs"]["namespace"]["steps"][-1]
                if mutation == "missing":
                    invocation["run"] = ""
                elif mutation == "missing_custody":
                    invocation["run"] = invocation["run"].rstrip().rsplit(" ", 1)[0]
                elif mutation == "wrong_command":
                    invocation["run"] = "echo skipped"
                else:
                    invocation["run"] += " || true"
                path.write_text(yaml.safe_dump(validation), encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "real namespace scenarios"):
                    _assert_validation_contract(workflows)

    def test_contract_rejects_missing_namespace_job_or_strict_env(self) -> None:
        for mutation in ("missing_job", "missing_env", "disabled_env"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                workflows = Path(directory) / "workflows"
                shutil.copytree(WORKFLOWS, workflows)
                path = workflows / "validate.yml"
                validation = _workflow(path)
                if mutation == "missing_job":
                    del validation["jobs"]["namespace"]
                elif mutation == "missing_env":
                    del validation["jobs"]["namespace"]["env"]
                else:
                    validation["jobs"]["namespace"]["env"]["HUB_REQUIRE_NAMESPACE_TESTS"] = "0"
                path.write_text(yaml.safe_dump(validation), encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "namespace"):
                    _assert_validation_contract(workflows)

    def test_contract_rejects_skipped_or_error_tolerant_validation(self) -> None:
        cases = (
            ("validate.yml", "validate", False, "if", "false"),
            ("validate.yml", "validate", False, "continue-on-error", True),
            ("validate.yml", "validate", True, "if", "false"),
            ("validate.yml", "validate", True, "continue-on-error", True),
            ("validate.yml", "namespace", False, "if", "false"),
            ("validate.yml", "namespace", False, "continue-on-error", True),
            ("validate.yml", "namespace", True, "if", "false"),
            ("validate.yml", "namespace", True, "continue-on-error", True),
            ("release.yml", "validation", False, "if", "false"),
            ("ci.yml", "validation", False, "if", "false"),
            ("release.yml", "release", True, "continue-on-error", True),
        )
        for filename, job_name, step_level, key, value in cases:
            with self.subTest(filename=filename, job=job_name, step=step_level, key=key):
                with tempfile.TemporaryDirectory() as directory:
                    workflows = Path(directory) / "workflows"
                    shutil.copytree(WORKFLOWS, workflows)
                    path = workflows / filename
                    workflow = _workflow(path)
                    target = workflow["jobs"][job_name]
                    if step_level:
                        target = next(
                            step
                            for step in target["steps"]
                            if "run" in step
                            and (
                                filename != "validate.yml"
                                or (
                                    job_name == "validate"
                                    and step["run"] == ".venv/bin/python scripts/validate.py"
                                )
                                or (
                                    job_name == "namespace"
                                    and "test_namespace_client_preserves" in step["run"]
                                )
                            )
                        )
                    target[key] = value
                    path.write_text(yaml.safe_dump(workflow), encoding="utf-8")
                    with self.assertRaisesRegex(AssertionError, "skip|tolerate"):
                        _assert_validation_contract(workflows)

    def test_contract_rejects_missing_or_shell_interpolated_policy_preparation(self) -> None:
        for replacement, error in (
            ("", "public author policy"),
            (
                "      - name: Prepare optional public author policy\n"
                "        env:\n"
                "          HUB_PUBLIC_GIT_AUTHOR_EMAIL: "
                "${{ vars.HUB_PUBLIC_GIT_AUTHOR_EMAIL }}\n"
                "        run: echo '${{ vars.HUB_PUBLIC_GIT_AUTHOR_EMAIL }}'\n",
                "expressions",
            ),
        ):
            with self.subTest(error=error):
                with tempfile.TemporaryDirectory() as directory:
                    workflows = Path(directory) / "workflows"
                    shutil.copytree(WORKFLOWS, workflows)
                    validation = workflows / "validate.yml"
                    text = validation.read_text(encoding="utf-8")
                    start = text.index("      - name: Prepare optional public author policy\n")
                    end = text.index("      - name: Canonical repository validation\n")
                    validation.write_text(
                        text[:start] + replacement + text[end:],
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(AssertionError, error):
                        _assert_validation_contract(workflows)

    def test_release_revision_guard_checks_head_event_and_annotated_tag(self) -> None:
        release = _workflow(WORKFLOWS / "release.yml")
        guard = next(
            step
            for step in release["jobs"]["release"]["steps"]
            if step.get("name") == "Verify release revision"
        )
        self.assertEqual(
            guard["env"],
            {"EVENT_SHA": "${{ github.sha }}", "RELEASE_TAG": "${{ github.ref_name }}"},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args: str) -> str:
                return subprocess.run(
                    (
                        "git",
                        "-c",
                        "user.name=Example Reviewer",
                        "-c",
                        "user.email=reviewer@example.com",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "tag.gpgsign=false",
                        *args,
                    ),
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            def check(event_sha: str) -> int:
                return subprocess.run(
                    ("bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", guard["run"]),
                    cwd=root,
                    env={**os.environ, "EVENT_SHA": event_sha, "RELEASE_TAG": "v0.0.1"},
                    capture_output=True,
                    text=True,
                    check=False,
                ).returncode

            git("init", "-q")
            git("commit", "--allow-empty", "-m", "Example candidate")
            candidate = git("rev-parse", "HEAD")
            git("tag", "-a", "v0.0.1", "-m", "Example release")
            self.assertNotEqual(git("rev-parse", "v0.0.1"), candidate)
            self.assertEqual(check(candidate), 0)
            self.assertNotEqual(check("0" * 40), 0)
            git("commit", "--allow-empty", "-m", "Example different checkout")
            self.assertNotEqual(check(candidate), 0)
            self.assertNotEqual(check(git("rev-parse", "HEAD")), 0)

    def test_contract_rejects_missing_release_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workflows = Path(directory) / "workflows"
            shutil.copytree(WORKFLOWS, workflows)
            release = workflows / "release.yml"
            release.write_text(
                release.read_text(encoding="utf-8").replace("    needs: validation\n", ""),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "depend on validation"):
                _assert_validation_contract(workflows)

    def test_contract_rejects_release_bypass_condition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workflows = Path(directory) / "workflows"
            shutil.copytree(WORKFLOWS, workflows)
            release = workflows / "release.yml"
            release.write_text(
                release.read_text(encoding="utf-8").replace(
                    "    needs: validation\n", "    needs: validation\n    if: always()\n"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "without a bypass"):
                _assert_validation_contract(workflows)

    def test_contract_rejects_missing_python_313(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workflows = Path(directory) / "workflows"
            shutil.copytree(WORKFLOWS, workflows)
            validation = workflows / "validate.yml"
            validation.write_text(
                validation.read_text(encoding="utf-8").replace(
                    '["3.11", "3.12", "3.13"]', '["3.11", "3.12"]'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AssertionError, "3.11 through 3.13"):
                _assert_validation_contract(workflows)
