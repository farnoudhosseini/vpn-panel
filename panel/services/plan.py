"""Declarative change plans.

A plan is a small, inspectable description of what should exist on a host:
files to write, systemd units to install and services to (re)start. The panel
renders a plan and either applies it locally or ships it to a node agent — the
node never receives a shell command it has to interpret beyond the plan.

Because a node agent may run as root, :func:`validate` refuses any path outside
the directories Panah owns. That keeps a compromised control plane from turning
into arbitrary file write on the fleet.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ..core.logging import get_logger
from . import paths, shell

log = get_logger("plan")


class PlanError(RuntimeError):
    pass


@dataclass(slots=True)
class FileSpec:
    path: str
    content: str
    mode: int = 0o600

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "content": self.content, "mode": self.mode}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileSpec:
        return cls(
            path=str(data["path"]),
            content=str(data.get("content", "")),
            mode=int(data.get("mode", 0o600)),
        )


@dataclass(slots=True)
class UnitSpec:
    name: str
    content: str
    reload: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "content": self.content, "reload": self.reload}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UnitSpec:
        return cls(
            name=str(data["name"]),
            content=str(data.get("content", "")),
            reload=bool(data.get("reload", True)),
        )


@dataclass(slots=True)
class CommandSpec:
    """A systemd/sysctl level action expressed as an argument vector."""

    args: list[str]
    ignore_failure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"args": list(self.args), "ignore_failure": self.ignore_failure}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandSpec:
        return cls(
            args=[str(a) for a in data.get("args", [])],
            ignore_failure=bool(data.get("ignore_failure", False)),
        )


@dataclass(slots=True)
class SidePlan:
    role: str
    node_id: int
    node_name: str
    files: list[FileSpec] = field(default_factory=list)
    units: list[UnitSpec] = field(default_factory=list)
    commands: list[CommandSpec] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    service: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "node_id": self.node_id,
            "node_name": self.node_name,
            "files": [f.to_dict() for f in self.files],
            "units": [u.to_dict() for u in self.units],
            "commands": [c.to_dict() for c in self.commands],
            "removals": list(self.removals),
            "service": self.service,
            "summary": dict(self.summary),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SidePlan:
        return cls(
            role=str(data.get("role", "")),
            node_id=int(data.get("node_id", 0)),
            node_name=str(data.get("node_name", "")),
            files=[FileSpec.from_dict(item) for item in data.get("files", [])],
            units=[UnitSpec.from_dict(item) for item in data.get("units", [])],
            commands=[CommandSpec.from_dict(item) for item in data.get("commands", [])],
            removals=[str(item) for item in data.get("removals", [])],
            service=data.get("service"),
            summary=dict(data.get("summary", {})),
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

SYSTEMD_DIRS = (Path("/etc/systemd/system"), Path("/lib/systemd/system"))
UNIT_NAME_PATTERN = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789@._-"


def allowed_roots() -> list[PurePosixPath]:
    """Directories a plan may write to.

    Compared as POSIX paths because a plan describes a Linux host, no matter
    which platform rendered it.
    """
    roots = [
        paths.config_dir(),
        paths.openvpn_root(),
        paths.wireguard_dir(),
        paths.backpack_dir(),
        paths.fake_site_root(),
        paths.nginx_sites_dir(),
        paths.nginx_enabled_dir(),
        paths.run_dir(),
        paths.data_dir(),
        paths.log_dir(),
        Path("/etc/nginx/stream.d"),
        *SYSTEMD_DIRS,
    ]
    return [PurePosixPath(paths.posix(root)) for root in roots]


def _normalise(raw: str) -> PurePosixPath:
    return PurePosixPath(paths.posix(raw))


def validate_path(raw: str) -> Path:
    """Return the local ``Path`` for a managed location, or raise.

    Rejects control characters, relative paths and anything that escapes the
    managed roots (including via ``..``), which is what keeps a compromised
    control plane from writing arbitrary files on a node.
    """
    if not raw or any(char in raw for char in ("\x00", "\n", "\r")):
        raise PlanError("path is empty or contains control characters")

    candidate = _normalise(raw)
    if not candidate.is_absolute():
        raise PlanError(f"path must be absolute: {raw}")
    # ``..`` is resolved by PurePosixPath, so traversal cannot survive this.
    normalised = PurePosixPath(candidate)
    if ".." in normalised.parts:
        raise PlanError(f"path contains traversal: {raw}")

    for root in allowed_roots():
        if normalised == root or root in normalised.parents:
            return Path(paths.posix(normalised))
    raise PlanError(f"path outside the managed roots: {raw}")


def validate_unit_name(name: str) -> str:
    if not name or len(name) > 128:
        raise PlanError("invalid unit name")
    if any(char not in UNIT_NAME_PATTERN for char in name):
        raise PlanError(f"invalid character in unit name {name!r}")
    if not name.endswith((".service", ".target", ".socket")):
        raise PlanError(f"unit name must end with .service: {name!r}")
    return name


def validate(plan: SidePlan) -> SidePlan:
    """Reject any plan that escapes the managed roots."""
    for spec in plan.files:
        validate_path(spec.path)
    for unit in plan.units:
        validate_unit_name(unit.name)
    for command in plan.commands:
        if not command.args:
            raise PlanError("empty command in plan")
        binary = Path(command.args[0]).name
        if binary not in shell.ALLOWED_BINARIES:
            raise PlanError(f"command not allowed in plan: {binary}")
    for removal in plan.removals:
        validate_path(removal)
    if plan.service:
        validate_unit_name(plan.service)
    return plan


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def apply(plan: SidePlan, *, validate_first: bool = True) -> dict[str, Any]:
    """Write files, install units and run the plan's commands on this host."""
    if validate_first:
        validate(plan)

    report: dict[str, Any] = {
        "role": plan.role,
        "node": plan.node_name,
        "ok": True,
        "files": [],
        "units": [],
        "commands": [],
        "removals": [],
    }

    for spec in plan.files:
        try:
            target = Path(spec.path)
            shell.write_file_locked(target, spec.content, mode=spec.mode)
            report["files"].append({"path": spec.path, "ok": True})
        except OSError as exc:
            report["files"].append({"path": spec.path, "ok": False, "error": str(exc)})
            report["ok"] = False

    needs_reload = False
    for unit in plan.units:
        try:
            validate_unit_name(unit.name)
            exact = _unit_path(unit.name)
            shell.write_file_locked(exact, unit.content, mode=0o644)
            report["units"].append({"name": unit.name, "path": str(exact), "ok": True})
            needs_reload = needs_reload or unit.reload
        except (PlanError, OSError) as exc:
            report["units"].append({"name": unit.name, "ok": False, "error": str(exc)})
            report["ok"] = False

    if needs_reload and shell.available("systemctl"):
        result = shell.run(["systemctl", "daemon-reload"], timeout=30)
        if not result.ok:
            report["ok"] = False
            report["commands"].append({"args": ["systemctl", "daemon-reload"], "ok": False,
                                       "message": result.message})
    elif needs_reload:
        report["units"].append(
            {"name": "systemd", "ok": False, "error": "systemctl unavailable"}
        )

    for command in plan.commands:
        result = shell.run(command.args, timeout=90)
        report["commands"].append(
            {"args": command.args, "ok": result.ok, "message": result.message}
        )
        if not result.ok and not command.ignore_failure:
            report["ok"] = False

    for target in plan.removals:
        try:
            Path(target).unlink(missing_ok=True)
            report["removals"].append({"path": target, "ok": True})
        except OSError as exc:
            report["removals"].append({"path": target, "ok": False, "error": str(exc)})
            report["ok"] = False

    if not report["ok"]:
        log.warning("plan for %s finished with failures: %s", plan.node_name, report)
    return report


def _unit_path(name: str) -> Path:
    """Prefer /etc/systemd/system, which always takes precedence."""
    primary = Path("/etc/systemd/system") / name
    return primary


def service_state(name: str) -> tuple[bool, str]:
    if not name:
        return False, "unknown"
    if not shell.available("systemctl"):
        return False, "systemd-unavailable"
    result = shell.run(["systemctl", "is-active", name], timeout=10)
    state = result.output or "unknown"
    return state == "active", state


def service_action(name: str, action: str) -> tuple[bool, str]:
    """Run one of a fixed set of systemd verbs against a validated unit."""
    if action not in {"start", "stop", "restart", "reload", "enable", "disable"}:
        raise PlanError(f"unsupported service action {action!r}")
    validate_unit_name(name)
    args = ["systemctl", action]
    if action in {"enable", "disable"}:
        args.append("--now")
    args.append(name)
    result = shell.run(args, timeout=90)
    return result.ok, result.message


def cleanup_paths(items: Iterable[str]) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for raw in items:
        try:
            target = validate_path(raw)
            if target.is_dir():
                shell.run(["rm", "-rf", str(target)])
            else:
                os.unlink(target)
            report.append({"path": raw, "ok": True})
        except (PlanError, OSError) as exc:
            report.append({"path": raw, "ok": False, "error": str(exc)})
    return report
