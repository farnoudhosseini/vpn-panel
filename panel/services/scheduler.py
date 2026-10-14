"""Background jobs.

Runs inside the panel process only. Jobs are deliberately small and
idempotent; every one of them opens its own database session so a failure in
one cannot hold a transaction open for the others.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from ..core.logging import get_logger
from ..db import session_scope
from ..models import Node, SystemSetting, utcnow
from . import certbot, node_client, traffic
from .plan import cleanup_paths

log = get_logger("scheduler")

METRICS_SECONDS = 30
ACCOUNTING_SECONDS = 30
MAINTENANCE_SECONDS = 6 * 3600
CERT_SECONDS = 12 * 3600

_scheduler: BackgroundScheduler | None = None
_last_report: dict[str, Any] = {}


def last_report() -> dict[str, Any]:
    return dict(_last_report)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def refresh_nodes() -> dict[str, Any]:
    """Poll every enabled node for capabilities and metrics.

    The poll runs in two phases and the probes run concurrently: nodes are
    contacted with no database session open (a slow node must not hold SQLite's
    single writer), and the results are committed in one short transaction
    afterwards. Probing in parallel keeps the job's wall clock bounded no
    matter how many nodes are offline.
    """
    summary: dict[str, Any] = {"online": 0, "offline": 0, "errors": []}

    with session_scope() as session:
        nodes = list(session.scalars(select(Node).where(Node.enabled.is_(True))))

    def _probe(node: Node) -> tuple[Node, Any, Exception | None]:
        try:
            return node, node_client.probe(node, timeout=node_client.PROBE_TIMEOUT), None
        except node_client.NodeUnreachable as exc:
            return node, None, exc

    results: list[tuple[Node, Any, Exception | None]] = []
    if nodes:
        with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
            results = list(pool.map(_probe, nodes))

    with session_scope() as session:
        for node, ready, failure in results:
            live = session.get(Node, node.id)
            if live is None:
                continue
            if failure is not None:
                node_client.mark_offline(session, live, failure)
                summary["offline"] += 1
                if len(summary["errors"]) < 10:
                    summary["errors"].append(f"{node.name}: {failure}")
                continue
            node_client.refresh_node(session, live, ready=ready)
            summary["online"] += 1

    log.debug("node refresh: %s", summary)
    return summary


def collect_traffic() -> dict[str, Any]:
    global _last_report
    with session_scope() as session:
        report = traffic.collect(session)
    # Session closed: closing sessions on the nodes can now take its time.
    closed = traffic.apply_disconnects(report)
    _last_report = report.as_dict()
    if closed:
        _last_report["disconnected"] = closed
    if report.limited or report.expired:
        log.info(
            "enforcement: %s limited, %s expired, %s session(s) closed",
            len(report.limited),
            len(report.expired),
            closed,
        )
    return _last_report


def maintenance() -> dict[str, Any]:
    """Prune time series, stale counters and old audit rows."""
    from ..models import AuditLog, Event

    result: dict[str, Any] = {}
    with session_scope() as session:
        result.update(traffic.prune(session))

        audit_cutoff = utcnow() - timedelta(days=90)
        result["audit_logs"] = int(
            session.query(AuditLog)
            .filter(AuditLog.ts < audit_cutoff)
            .delete(synchronize_session=False)
            or 0
        )
        event_cutoff = utcnow() - timedelta(days=30)
        result["events"] = int(
            session.query(Event)
            .filter(Event.ts < event_cutoff, Event.acknowledged.is_(True))
            .delete(synchronize_session=False)
            or 0
        )
    return result


def renew_certificates() -> dict[str, Any]:
    if not certbot.available():
        return {"ok": True, "skipped": "certbot not installed"}
    try:
        return certbot.renew()
    except certbot.CertificateError as exc:
        log.warning("certificate renewal skipped: %s", exc)
        return {"ok": False, "message": str(exc)}


def cleanup_expired_artifacts() -> dict[str, Any]:
    """Remove generated client bundles for deleted clients."""
    with session_scope() as session:
        setting = session.get(SystemSetting, "pending_cleanup")
        targets: list[str] = list((setting.value or {}).get("paths", [])) if setting else []
        if setting:
            setting.value = {"paths": []}
    if not targets:
        return {"removed": 0}
    report = cleanup_paths(targets)
    return {"removed": sum(1 for item in report if item.get("ok")), "detail": report}


def queue_cleanup(paths_to_remove: list[str]) -> None:
    """Register paths for the next maintenance pass (used by delete handlers)."""
    with session_scope() as session:
        setting = session.get(SystemSetting, "pending_cleanup")
        existing: list[str] = []
        if setting:
            existing = list((setting.value or {}).get("paths", []))
        existing.extend(paths_to_remove)
        if setting:
            setting.value = {"paths": existing[:500]}
        else:
            session.add(SystemSetting(key="pending_cleanup", value={"paths": existing[:500]}))


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def start() -> BackgroundScheduler | None:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 20},
    )
    scheduler.add_job(
        refresh_nodes,
        IntervalTrigger(seconds=METRICS_SECONDS),
        id="node_metrics",
        next_run_time=utcnow() + timedelta(seconds=2),
    )
    scheduler.add_job(
        collect_traffic,
        IntervalTrigger(seconds=ACCOUNTING_SECONDS),
        id="traffic_accounting",
        next_run_time=utcnow() + timedelta(seconds=5),
    )
    scheduler.add_job(
        maintenance,
        IntervalTrigger(seconds=MAINTENANCE_SECONDS),
        id="maintenance",
        next_run_time=utcnow() + timedelta(minutes=10),
    )
    scheduler.add_job(
        renew_certificates,
        IntervalTrigger(seconds=CERT_SECONDS),
        id="certificates",
        next_run_time=utcnow() + timedelta(minutes=30),
    )
    scheduler.add_job(
        cleanup_expired_artifacts,
        IntervalTrigger(seconds=600),
        id="cleanup",
        next_run_time=utcnow() + timedelta(minutes=5),
    )
    scheduler.start()
    _scheduler = scheduler
    log.info("background scheduler started")
    return scheduler


def stop() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:  # pragma: no cover
        log.debug("scheduler shutdown raised", exc_info=True)
    _scheduler = None
    log.info("background scheduler stopped")


def is_running() -> bool:
    return bool(_scheduler and _scheduler.running)


def job_status() -> list[dict[str, Any]]:
    if _scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "pending": job.pending,
        }
        for job in _scheduler.get_jobs()
    ]


def run_job_now(job_id: str) -> dict[str, Any]:
    """Trigger a job synchronously (used by the admin API)."""
    jobs = {
        "node_metrics": refresh_nodes,
        "traffic_accounting": collect_traffic,
        "maintenance": maintenance,
        "certificates": renew_certificates,
        "cleanup": cleanup_expired_artifacts,
    }
    handler = jobs.get(job_id)
    if handler is None:
        raise KeyError(job_id)
    return handler()
