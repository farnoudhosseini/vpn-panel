"""Subprocess helpers.

All host interaction goes through :func:`run`, which never uses a shell, always
applies a timeout, and records failures instead of raising them by default so
callers can decide how to react.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..core.logging import get_logger, redact

log = get_logger("shell")

DEFAULT_TIMEOUT = 30
# Binaries the panel is allowed to execute. Anything else is rejected so a
# compromised value stored in the database cannot become an RCE primitive.
ALLOWED_BINARIES = frozenset(
    {
        "openvpn",
        "ip",
        "iptables",
        "ip6tables",
        "nft",
        "sysctl",
        "wg",
        "wg-quick",
        "systemctl",
        "journalctl",
        "nginx",
        "certbot",
        "openssl",
        "easyrsa",
        "tar",
        "curl",
        "chmod",
        "chown",
        "mkdir",
        "cp",
        "mv",
        "rm",
        "ls",
        "test",
        "ss",
        "apt-get",
        "apt",
        "dpkg",
        "backpack",
        "modprobe",
        "hostname",
        "getent",
        "timedatectl",
        "useradd",
        "telegraf",
    }
)


@dataclass(slots=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.error is None

    @property
    def output(self) -> str:
        return (self.stdout or "").strip()

    @property
    def message(self) -> str:
        if self.error:
            return self.error
        if self.timed_out:
            return "command timed out"
        text = (self.stderr or self.stdout or "").strip()
        return text[-600:] if text else f"exit code {self.returncode}"


def is_root() -> bool:
    if os.name == "nt":  # pragma: no cover - development only
        return False
    return os.geteuid() == 0


def which(binary: str) -> str | None:
    return shutil.which(binary)


def available(binary: str) -> bool:
    return which(binary) is not None


def run(
    args: Sequence[str | Path],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    check: bool = False,
    input_text: str | None = None,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    allow_unlisted: bool = False,
) -> CommandResult:
    """Execute ``args`` without a shell.

    ``allow_unlisted`` exists for the installer/CLI path where a tarball or an
    arbitrary operator command must run; API-triggered code must leave it off.
    """
    argv = [str(a) for a in args]
    if not argv:
        return CommandResult(args=[], returncode=2, error="empty command")

    binary = Path(argv[0]).name
    if not allow_unlisted and binary not in ALLOWED_BINARIES:
        log.error("refused to execute non-allowlisted binary %r", binary)
        return CommandResult(args=argv, returncode=126, error=f"binary not allowed: {binary}")

    resolved = which(argv[0]) if not os.path.isabs(argv[0]) else argv[0]
    if not resolved:
        return CommandResult(args=argv, returncode=127, error=f"command not found: {argv[0]}")
    argv[0] = resolved

    merged_env = os.environ.copy()
    merged_env.setdefault("DEBIAN_FRONTEND", "noninteractive")
    if env:
        merged_env.update(env)

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            input=input_text,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
        )
    except subprocess.TimeoutExpired:
        log.warning("command timed out: %s", redact(" ".join(argv)))
        return CommandResult(args=argv, returncode=124, timed_out=True)
    except OSError as exc:
        return CommandResult(args=argv, returncode=126, error=str(exc))

    result = CommandResult(
        args=argv,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    if check and not result.ok:
        log.warning("command failed (%s): %s", result.returncode, redact(result.message))
    else:
        log.debug("ran %s -> %s", " ".join(Path(a).name for a in argv), result.returncode)
    return result


def run_many(
    commands: Iterable[Sequence[str | Path]],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    allow_unlisted: bool = False,
) -> list[CommandResult]:
    return [
        run(cmd, timeout=timeout, allow_unlisted=allow_unlisted) for cmd in commands
    ]


def write_file_locked(
    path: Path,
    content: str,
    *,
    mode: int = 0o600,
    owner: tuple[str, str] | None = None,
) -> None:
    """Atomically write a file with restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    finally:
        try:
            os.chmod(tmp, mode)
        except OSError:  # pragma: no cover
            pass
    os.replace(tmp, path)
    if owner and is_root():
        import pwd

        try:
            entry = pwd.getpwnam(owner[0])
            os.chown(path, entry.pw_uid, entry.pw_gid)
        except (KeyError, OSError):  # pragma: no cover
            log.debug("could not chown %s to %s", path, owner[0])
