"""Trusted system-runtime mounts for opt-in, offline native fixtures only."""

from __future__ import annotations

from pathlib import Path


def native_runtime_mounts() -> list[str]:
    argv: list[str] = []
    for source in ("/usr/bin", "/usr/lib", "/usr/lib64"):
        if Path(source).is_dir():
            argv.extend(("--ro-bind", source, source))
        elif source != "/usr/lib64":
            raise RuntimeError("offline native system runtime is unavailable")
    for source in ("/bin", "/lib", "/lib64"):
        path = Path(source)
        if path.is_symlink():
            target = path.readlink()
            resolved = path.resolve(strict=True)
            if not any(
                resolved.is_relative_to(root)
                for root in (Path("/usr/bin"), Path("/usr/lib"), Path("/usr/lib64"))
            ):
                raise RuntimeError("offline native runtime link is unsupported")
            argv.extend(("--symlink", str(target), source))
        elif path.is_dir():
            argv.extend(("--ro-bind", source, source))
        elif source != "/lib64":
            raise RuntimeError("offline native system runtime is unavailable")
    return argv
