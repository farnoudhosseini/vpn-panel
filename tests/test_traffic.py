"""Traffic accounting: deltas, billing, resets and enforcement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from panel.models import (
    Client,
    ClientStatus,
    Inbound,
    Node,
    ResetPolicy,
    TrafficCounter,
    utcnow,
)
from panel.services import node_client, traffic

# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_compute_delta_grows() -> None:
    assert traffic.compute_delta(100, 250) == 150


def test_compute_delta_of_a_restarted_counter_counts_everything() -> None:
    # OpenVPN restarts its counters when a session reconnects.
    assert traffic.compute_delta(5000, 120) == 120


def test_compute_delta_ignores_negative_readings() -> None:
    assert traffic.compute_delta(10, -1) == 0


@pytest.mark.parametrize("multiplier", [1.0, 2.0, 0.5])
def test_bill_applies_the_multiplier(multiplier: float) -> None:
    assert traffic.bill(100, 300, multiplier) == pytest.approx(400 * multiplier)


def test_bill_treats_a_zero_multiplier_as_one() -> None:
    assert traffic.bill(100, 100, 0) == 200


def test_next_reset_daily_is_midnight() -> None:
    reference = datetime(2026, 3, 10, 13, 45, tzinfo=UTC)
    result = traffic.next_reset_at(ResetPolicy.DAILY.value, reference)
    assert result is not None and (result.hour, result.minute, result.second) == (0, 0, 0)
    assert result > reference


def test_next_reset_weekly_lands_on_a_saturday() -> None:
    reference = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
    result = traffic.next_reset_at(ResetPolicy.WEEKLY.value, reference)
    assert result is not None and result.weekday() == 5  # Saturday


def test_next_reset_monthly_moves_to_the_first() -> None:
    reference = datetime(2026, 12, 20, 9, 0, tzinfo=UTC)
    result = traffic.next_reset_at(ResetPolicy.MONTHLY.value, reference)
    assert result is not None
    assert (result.year, result.month, result.day) == (2027, 1, 1)


def test_next_reset_never() -> None:
    assert traffic.next_reset_at(ResetPolicy.NEVER.value) is None


def make_client(**overrides) -> Client:
    values = {
        "id": 1,
        "inbound_id": 1,
        "node_id": 1,
        "name": "c",
        "common_name": "c1",
        "sub_id": "s1",
        "enabled": True,
        "data_limit_bytes": 0,
        "multiplier": 1.0,
        "billed_bytes": 0.0,
    }
    values.update(overrides)
    return Client(**values)


def test_evaluate_active_client_is_active() -> None:
    assert traffic.evaluate_client(make_client()) == ClientStatus.ACTIVE.value


def test_evaluate_disabled_client() -> None:
    assert traffic.evaluate_client(make_client(enabled=False)) == ClientStatus.DISABLED.value


def test_evaluate_limited_client() -> None:
    client = make_client(data_limit_bytes=1000, billed_bytes=1500)
    assert traffic.evaluate_client(client) == ClientStatus.LIMITED.value


def test_evaluate_expired_client() -> None:
    client = make_client(expire_at=utcnow() - timedelta(minutes=1))
    assert traffic.evaluate_client(client) == ClientStatus.EXPIRED.value


def test_evaluate_ignores_a_client_on_hold() -> None:
    client = make_client(status=ClientStatus.ON_HOLD.value, data_limit_bytes=1, billed_bytes=99)
    assert traffic.evaluate_client(client) is None


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


@pytest.fixture()
def world(session):
    """One local node with an inbound and a client that has a quota."""
    node = Node(name="local", address="127.0.0.1", is_local=True, enabled=True)
    session.add(node)
    session.flush()
    inbound = Inbound(
        node_id=node.id,
        tag="udp-main",
        listen_port=1194,
        network_cidr="10.8.0.0/24",
        enabled=True,
    )
    session.add(inbound)
    session.flush()
    client = Client(
        inbound_id=inbound.id,
        node_id=node.id,
        name="Ali",
        common_name="ali",
        sub_id="sub-ali",
        data_limit_bytes=1_000,
        multiplier=2.0,
    )
    session.add(client)
    session.flush()
    return session, node, inbound, client


def test_collect_bills_with_the_multiplier(world, monkeypatch) -> None:
    session, _node, _inbound, client = world
    monkeypatch.setattr(
        node_client,
        "fetch_sessions",
        lambda node, tag, **kwargs: [
            {"common_name": "ali", "bytes_received": 300, "bytes_sent": 700}
        ],
    )

    report = traffic.collect(session)
    assert report.sessions == 1
    assert report.bytes_up == 300 and report.bytes_down == 700
    # (300 + 700) * 2.0
    assert report.billed == pytest.approx(2000)
    assert client.billed_bytes == pytest.approx(2000)
    assert client.up_bytes == 300
    assert client.down_bytes == 700


def test_collect_only_bills_the_delta(world, monkeypatch) -> None:
    session, _node, _inbound, client = world
    readings = iter([1000, 1600])
    monkeypatch.setattr(
        node_client,
        "fetch_sessions",
        lambda node, tag, **kwargs: [
            {"common_name": "ali", "bytes_received": next(readings), "bytes_sent": 0}
        ],
    )

    traffic.collect(session)
    first = client.billed_bytes
    traffic.collect(session)
    # Only the 600 byte increase is billed, at multiplier 2.
    assert client.billed_bytes == pytest.approx(first + 1200)


def test_collect_marks_a_client_limited_and_queues_a_disconnect(world, monkeypatch) -> None:
    session, _node, _inbound, client = world
    monkeypatch.setattr(
        node_client,
        "fetch_sessions",
        lambda node, tag, **kwargs: [
            {"common_name": "ali", "bytes_received": 900, "bytes_sent": 900}
        ],
    )

    report = traffic.collect(session)
    assert client.status == ClientStatus.LIMITED.value
    assert "Ali" in report.limited
    # Enforcement must not touch the network while the transaction is open.
    assert report.disconnects == [
        {"node_id": client.node_id, "tag": "udp-main", "common_name": "ali"}
    ]


def test_apply_disconnects_uses_a_short_lived_session(world, monkeypatch) -> None:
    _session, node, _inbound, _client = world

    calls: list[tuple[int, str, str]] = []

    def fake_disconnect(node_id, tag, common_name, *, timeout=None):
        calls.append((node_id, tag, common_name))
        return True

    monkeypatch.setattr(node_client, "disconnect_by_id", fake_disconnect)
    report = traffic.AccountingReport()
    report.disconnects.append({"node_id": node.id, "tag": "udp-main", "common_name": "ali"})
    assert traffic.apply_disconnects(report) == 1
    assert calls == [(node.id, "udp-main", "ali")]


def test_collect_survives_an_unreachable_node(world, monkeypatch) -> None:
    session, _node, _inbound, client = world

    def unreachable(*_args, **_kwargs):
        raise node_client.NodeUnreachable("no route to host")

    monkeypatch.setattr(node_client, "fetch_sessions", unreachable)
    report = traffic.collect(session)
    assert report.errors and "no route to host" in report.errors[0]
    assert report.clients_updated == 0
    assert client.billed_bytes == 0


def test_collect_matches_an_existing_counter(world, monkeypatch) -> None:
    session, node, inbound, _client = world
    session.add(
        TrafficCounter(
            node_id=node.id,
            inbound_tag=inbound.tag,
            common_name="ali",
            raw_received=500,
            raw_sent=500,
        )
    )
    session.flush()
    monkeypatch.setattr(
        node_client,
        "fetch_sessions",
        lambda node, tag, **kwargs: [
            {"common_name": "ali", "bytes_received": 600, "bytes_sent": 500}
        ],
    )
    report = traffic.collect(session)
    assert report.bytes_up == 100 and report.bytes_down == 0


def test_reset_policy_clears_usage(world) -> None:
    session, _node, _inbound, client = world
    client.reset_policy = ResetPolicy.DAILY.value
    client.reset_at = utcnow() - timedelta(minutes=1)
    client.billed_bytes = 500
    client.up_bytes = 400
    client.status = ClientStatus.LIMITED.value

    report = traffic.collect(session)
    assert client.billed_bytes == 0
    assert client.status == ClientStatus.ACTIVE.value
    assert report.errors == []


def test_usage_series_returns_points(world, monkeypatch) -> None:
    session, _node, _inbound, _client = world
    monkeypatch.setattr(
        node_client,
        "fetch_sessions",
        lambda node, tag, **kwargs: [
            {"common_name": "ali", "bytes_received": 10, "bytes_sent": 20}
        ],
    )
    traffic.collect(session)
    series = traffic.usage_series(session, hours=24)
    assert isinstance(series, list)
