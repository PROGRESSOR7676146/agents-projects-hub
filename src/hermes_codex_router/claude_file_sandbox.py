"""Claude policy facade over the provider-neutral pinned process namespace.

The caller still owns invocation, human permissions and cleanup. A failed
namespace launch must never be retried without its isolation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .claude_mount_pins import SandboxLaunch
from .process_namespace import SANDBOX_HOME, NamespaceRuntime, ProcessNamespaceConfig
from .process_namespace import NamespaceError as FileToolSandboxError

_SAFE_ENV = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "LANG",
        "LC_ALL",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
    }
)


@dataclass(frozen=True)
class FileToolSandboxConfig:
    """Preserve the explicit Claude constructor and narrow file-tool policy."""

    bwrap_executable: Path
    project_root: Path
    provider_home: Path
    runtime_roots: tuple[Path, ...]
    claude_executable: Path
    python_executable: Path
    hook_code_root: Path
    permission_socket: Path
    private_paths: tuple[Path, ...]
    _namespace: ProcessNamespaceConfig = field(init=False, repr=False)

    def __post_init__(self) -> None:
        runtime = NamespaceRuntime(
            bwrap_executable=self.bwrap_executable,
            executable=self.claude_executable,
            runtime_roots=self.runtime_roots,
            auxiliary_executables=(self.python_executable,),
            code_root=self.hook_code_root,
        )
        object.__setattr__(
            self,
            "_namespace",
            ProcessNamespaceConfig(
                runtime=runtime,
                project_root=self.project_root,
                session_home=self.provider_home,
                private_paths=self.private_paths,
                project_access="read-write",
                network="shared",
                permission_socket=self.permission_socket,
            ),
        )

    def wrap(self, argv: Sequence[str], env: Mapping[str, str], cwd: Path | str) -> SandboxLaunch:
        if set(env) - _SAFE_ENV:
            raise FileToolSandboxError("provider environment includes unapproved names")
        if argv and argv[0] != str(self.claude_executable):
            raise FileToolSandboxError("provider executable differs from pinned Claude executable")
        child = dict(env)
        child["CLAUDE_CONFIG_DIR"] = f"{SANDBOX_HOME}/.claude"
        return self._namespace.wrap(argv, child, cwd)
