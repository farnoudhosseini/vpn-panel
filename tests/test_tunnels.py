"""Tunnel transports: catalogue, plans and generated artefacts."""

from __future__ import annotations

import pytest

from panel.models import Node, Tunnel
from panel.services import paths, tunnels


@pytest.fixture()
def nodes(session):
    local = Node(name="iran", address="198.51.100.10", is_local=False, enabled=True, scheme="http")
    remote = Node(name="kharej", address="203.0.113.20", is_local=False, enabled=True, scheme="http")
    session.add_all([local, remote])
    session.flush()
    return local, remote


def make_tunnel(session, local, remote, **overrides) -> Tunnel:
    spec = tunnels.get_transport(overrides.pop("transport", "wireguard"))
    tunnel = Tunnel(
        name=overrides.pop("name", "link-1"),
        engine=spec.engine,
        transport=spec.key,
        mode=overrides.pop("mode", spec.backpack_mode or "direct"),
        local_node_id=local.id,
        remote_node_id=remote.id,
        listen_port=overrides.pop("listen_port", 51820),
        mtu=overrides.pop("mtu", 1420),
        forwarded_ports=overrides.pop("forwarded_ports", []),
        options=overrides.pop("options", {}),
    )
    session.add(tunnel)
    session.flush()
    return tunnel


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------


def test_catalogue_lists_every_transport_with_its_engine() -> None:
    catalogue = tunnels.transport_catalogue()
    keys = {item["key"] for item in catalogue}
    assert {"wireguard", "gre", "ipip", "udp", "kcp", "quic", "ws", "wss", "xdi"} <= keys
    for item in catalogue:
        assert item["engine"] in {"backpack", "native"}
        assert item["layer"] in {"l3", "l4"}
        assert item["label"]


def test_unknown_transport_is_rejected() -> None:
    with pytest.raises(tunnels.TunnelError):
        tunnels.get_transport("not-a-transport")


def test_backpack_requirements_are_declared() -> None:
    assert "CAP_NET_RAW" in tunnels.get_transport("xdi").requirements
    assert tunnels.get_transport("wss").encrypted is True
    assert tunnels.get_transport("udp").encrypted is False


# --------------------------------------------------------------------------
# WireGuard
# --------------------------------------------------------------------------


def test_wireguard_plan_generates_two_configurations(session, nodes) -> None:
    local, remote = nodes
    tunnel = make_tunnel(session, local, remote, transport="wireguard", mode="direct")

    plan = tunnels.plan_tunnel(tunnel, local, remote)
    assert plan.engine == "native"
    local_conf = next(spec for spec in plan.local.files if spec.path.endswith(".conf"))
    remote_conf = next(spec for spec in plan.remote.files if spec.path.endswith(".conf"))

    assert "[Interface]" in local_conf.content
    assert "PrivateKey" in local_conf.content
    assert "[Peer]" in local_conf.content
    # The two sides must not share a private key.
    assert local_conf.content != remote_conf.content
    assert local_conf.path.startswith(paths.posix(paths.wireguard_dir()))
    assert "\\" not in local_conf.path


def test_wireguard_keys_are_stable_until_rotated(session, nodes) -> None:
    local, remote = nodes
    tunnel = make_tunnel(session, local, remote, transport="wireguard", mode="direct")
    first = tunnels.wireguard_keypairs(tunnel)
    again = tunnels.wireguard_keypairs(tunnel)
    rotated = tunnels.wireguard_keypairs(tunnel, rotate=True)
    assert first == again
    assert rotated["private_local"] != first["private_local"]
    # Private keys are stored encrypted in the database, never in the clear.
    assert first["private_local"] not in (tunnel.options or {}).values()


def test_wireguard_endpoint_is_only_configured_on_the_dialing_side(session, nodes) -> None:
    local, remote = nodes
    tunnel = make_tunnel(
        session, local, remote, transport="wireguard", mode="direct", listen_port=51820
    )
    plan = tunnels.plan_tunnel(tunnel, local, remote)
    local_conf = next(spec for spec in plan.local.files if spec.path.endswith(".conf"))
    remote_conf = next(spec for spec in plan.remote.files if spec.path.endswith(".conf"))

    # The local end listens; the remote end dials back to it.
    assert f"ListenPort = {tunnel.listen_port}" in local_conf.content
    assert "Endpoint =" not in local_conf.content
    assert f"Endpoint = {local.address}:{tunnel.listen_port}" in remote_conf.content


def test_wireguard_unit_is_activated_through_wg_quick(session, nodes) -> None:
    local, remote = nodes
    tunnel = make_tunnel(session, local, remote, transport="wireguard", mode="direct")
    plan = tunnels.plan_tunnel(tunnel, local, remote)
    iface = tunnels.interface_name(tunnel)
    assert plan.local.service == f"wg-quick@{iface}.service"
    commands = [" ".join(command.args) for command in plan.local.commands]
    assert any(f"systemctl enable --now wg-quick@{iface}.service" == c for c in commands)
    # Both ends receive their own configuration with distinct addresses.
    assert plan.local.files[0].content != plan.remote.files[0].content


# --------------------------------------------------------------------------
# BackPack
# --------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["udp", "kcp", "quic", "ws", "wss"])
def test_backpack_plan_renders_a_toml_and_a_unit(session, nodes, transport) -> None:
    local, remote = nodes
    tunnel = make_tunnel(
        session, local, remote, transport=transport, mode="reverse", listen_port=3389
    )
    plan = tunnels.plan_tunnel(tunnel, local, remote)
    assert plan.engine == "backpack"

    for side in (plan.local, plan.remote):
        config = next(spec for spec in side.files if spec.path.endswith(".toml"))
        assert config.path.startswith(paths.posix(paths.backpack_dir()))
        assert "token = " in config.content
        unit = next(spec for spec in side.files if spec.path.endswith(".service"))
        assert "backpack" in unit.content
        assert "Restart=always" in unit.content
        # A plan is rendered for a Linux node: never a backslash separator.
        assert chr(92) not in config.path

    # One end listens, the other dials: the two files must not be identical.
    local_toml = next(spec for spec in plan.local.files if spec.path.endswith(".toml"))
    remote_toml = next(spec for spec in plan.remote.files if spec.path.endswith(".toml"))
    assert "[server]" in local_toml.content and "[client]" in remote_toml.content
    assert remote_toml.content != local_toml.content


def test_backpack_reverse_plan_forwards_ports(session, nodes) -> None:
    local, remote = nodes
    tunnel = make_tunnel(
        session,
        local,
        remote,
        transport="udp",
        mode="reverse",
        listen_port=20000,
        forwarded_ports=[{"expose": 1194, "target": 1194, "protocol": "udp"}],
    )
    plan = tunnels.plan_tunnel(tunnel, local, remote)
    local_toml = next(spec for spec in plan.local.files if spec.path.endswith(".toml"))
    assert "udp:1194=1194" in local_toml.content
    assert plan.local.summary["forwarded_ports"] == ["udp:1194=1194"]


def test_shared_token_is_stable(session, nodes) -> None:
    local, remote = nodes
    tunnel = make_tunnel(session, local, remote, transport="udp")
    first = tunnels.ensure_token(tunnel)
    assert first and tunnels.ensure_token(tunnel) == first
    assert len(first) >= 16


# --------------------------------------------------------------------------
# Kernel carriers
# --------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["gre", "ipip", "sit"])
def test_kernel_tunnel_plan_uses_the_hardened_unit(session, nodes, transport) -> None:
    local, remote = nodes
    tunnel = make_tunnel(session, local, remote, transport=transport, mode="direct")
    plan = tunnels.plan_tunnel(tunnel, local, remote)
    unit = next(spec for spec in plan.local.files if spec.path.endswith(".service"))
    assert f"ip tunnel add {tunnels.interface_name(tunnel)} mode {transport}" in unit.content
    assert "Type=oneshot" in unit.content
    assert "ExecStop=-/sbin/ip link del" in unit.content
    assert plan.local.service == tunnels.unit_name(tunnel)
    # The remote side is told to point back at the local node.
    remote_unit = next(spec for spec in plan.remote.files if spec.path.endswith(".service"))
    assert f"local {remote.address}" in remote_unit.content.replace(f"local {local.address}", "")


def test_interface_name_is_short_and_unique(session, nodes) -> None:
    local, remote = nodes
    first = make_tunnel(session, local, remote, name="alpha", transport="gre", mode="direct")
    second = make_tunnel(session, local, remote, name="beta", transport="gre", mode="direct")
    assert len(tunnels.interface_name(first)) <= 15
    assert tunnels.interface_name(first) != tunnels.interface_name(second)


def test_teardown_reports_the_artifacts(session, nodes) -> None:
    local, remote = nodes
    tunnel = make_tunnel(session, local, remote, transport="udp")
    report = tunnels.teardown(tunnel)
    assert report["service"] == tunnels.backpack_unit_name(tunnel.name)

    kernel = make_tunnel(session, local, remote, name="kernel-link", transport="gre", mode="direct")
    report = tunnels.teardown(kernel)
    assert report["interface"] == tunnels.interface_name(kernel)


def test_backpack_info_reports_absence_cleanly(sandbox) -> None:
    info = tunnels.backpack_info()
    assert info["installed"] is False
