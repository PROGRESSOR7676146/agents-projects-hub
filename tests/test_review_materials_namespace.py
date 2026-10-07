"""Selected sealed text reaches an isolated actor without source project mounts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.process_namespace import (
    NamespaceError,
    NamespaceRuntime,
    ProcessNamespaceConfig,
)
from hermes_codex_router.review_materials import MaterialSelection, build_review_capsule
from tests.fd_fixture import assert_descriptor_cleanup
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_namespace_runtime,
)

_ACTOR = r"""
import hashlib, json, os, pathlib, subprocess, sys
inputs = json.loads(sys.argv[1])
role = sys.argv[2]
raw = sys.stdin.buffer.read(1024 * 1024 + 1)
if not 0 < len(raw) <= 1024 * 1024 or hashlib.sha256(raw).hexdigest() != inputs['digest']:
    raise AssertionError('unbound or oversized capsule')
document = json.loads(raw)
if set(document) != {'version', 'binding', 'files'} or document['version'] != 1:
    raise AssertionError('capsule schema')
if document['binding'] != 'example-result' or len(document['files']) != 1:
    raise AssertionError('unexpected material binding')
entry = document['files'][0]
text = entry['text'].encode()
if entry['name'] != 'visible.txt' or entry['size'] != len(text) or hashlib.sha256(text).hexdigest() != entry['sha256']:
    raise AssertionError('material content or digest differs')
denied = {}
for path in inputs['hidden']:
    try:
        pathlib.Path(path).read_bytes()
    except OSError as error:
        denied[path] = error.errno
    else:
        raise AssertionError('unselected source material reachable')
for process in pathlib.Path('/proc').iterdir():
    if not process.name.isdigit(): continue
    try:
        descriptors = list(process.joinpath('fd').iterdir())
    except (PermissionError, FileNotFoundError): continue
    for descriptor in descriptors:
        try:
            info = descriptor.stat()
        except (PermissionError, FileNotFoundError): continue
        if [info.st_dev, info.st_ino] == inputs['capsule_inode']:
            raise AssertionError('host capsule FD inherited')
pathlib.Path('/home/example/' + role).write_text('private-session-output')
result = {'text': entry['text'], 'denied': denied}
if role == 'parent':
    child = subprocess.run([sys.executable, '-I', '-c', inputs['source'], sys.argv[1], 'child'],
        input=raw, capture_output=True, timeout=10, check=False, close_fds=True)
    if child.returncode: raise AssertionError('child failed: ' + child.stderr.decode())
    result['child'] = json.loads(child.stdout)
print(json.dumps(result))
"""


class ReviewMaterialsNamespaceTests(unittest.TestCase):
    def test_sealed_selected_stdin_material_without_source_project_or_git(self) -> None:
        bwrap = Path(shutil.which("bwrap") or "/usr/bin/bwrap")
        require_namespace_runtime(self, bwrap)
        python = Path("/usr/bin/python3.12")
        roots = (
            python,
            Path("/usr/lib/python3.12"),
            Path("/usr/lib/x86_64-linux-gnu"),
            Path("/usr/lib64"),
        )
        if not all(path.exists() for path in roots):
            namespace_unavailable(self, "system Python runtime fixture unavailable")
        try:
            runtime = NamespaceRuntime(bwrap, python, roots)
        except NamespaceError:
            namespace_unavailable(self, "system runtime is not immutable to worker")
        with tempfile.TemporaryDirectory(prefix="example-capsule-witness-") as directory:
            base = Path(directory)
            original, skeleton, home = (base / name for name in ("original", "skeleton", "session"))
            for path in (original, skeleton, home):
                path.mkdir(mode=0o700)
                if path != home:
                    (path / ".git").mkdir()
            selected = original / "visible.txt"
            selected.write_text("explicitly-authorized-text", encoding="utf-8")
            (original / "unselected.txt").write_text(
                "unselected-fictional-material", encoding="utf-8"
            )
            (original / ".git" / "HEAD").write_text("fictional-private-git-head", encoding="utf-8")
            selection = MaterialSelection(
                "visible.txt",
                selected.stat().st_size,
                hashlib.sha256(selected.read_bytes()).hexdigest(),
            )
            config = ProcessNamespaceConfig(runtime, skeleton, home, (original,))
            with assert_descriptor_cleanup(self):
                with build_review_capsule(
                    original, (selection,), binding="example-result"
                ) as capsule:
                    selected.write_text("changed-after-sealing", encoding="utf-8")
                    info = os.fstat(capsule.fileno())
                    inputs = {
                        "digest": capsule.digest,
                        "capsule_inode": [info.st_dev, info.st_ino],
                        "source": _ACTOR,
                        "hidden": [
                            str(original / "visible.txt"),
                            str(original / "unselected.txt"),
                            str(original / ".git" / "HEAD"),
                            ".git/HEAD",
                        ],
                    }
                    with config.wrap(
                        [str(python), "-I", "-c", _ACTOR, json.dumps(inputs), "parent"],
                        {},
                        skeleton,
                    ) as launch:
                        self.assertNotIn(str(original), launch.argv)
                        self.assertNotIn(capsule.fileno(), launch.pass_fds)
                        run = subprocess.run(
                            launch.argv,
                            env=launch.environment,
                            cwd="/",
                            input=capsule.read(),
                            close_fds=True,
                            pass_fds=launch.pass_fds,
                            capture_output=True,
                            timeout=20,
                            check=False,
                        )
                    if run.returncode and namespace_permission_refused(run.stderr.decode()):
                        namespace_unavailable(self, "kernel disallows user namespaces")
                    self.assertEqual(run.returncode, 0, run.stderr.decode())
                    result = json.loads(run.stdout)
                    child = result.pop("child")
                    for observed in (result, child):
                        self.assertEqual(observed["text"], "explicitly-authorized-text")
                        self.assertEqual(set(observed["denied"]), set(inputs["hidden"]))
                        self.assertTrue(all(value == 2 for value in observed["denied"].values()))
            self.assertEqual((home / "parent").read_text(), "private-session-output")
            self.assertEqual((home / "child").read_text(), "private-session-output")
            self.assertEqual(selected.read_text(), "changed-after-sealing")
