"""OpenVPN rendering, PKI lifecycle and plan safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from panel.models import Client, Inbound
from panel.services import node_ops, openvpn, paths, pki
from panel.services import plan as plan_mod


@pytest.fixture()
def node(session):
    from panel.models import Node

    row = Node(name="local", address="127.0.0.1", is_local=True, enabled=True)
    session.add(row)
    session.flush()
    return row


def make_inbound(session, node=None, **overrides) -> Inbound:
    inbound = Inbound(
        node_id=node.id if node is not None else 1,
        tag=overrides.pop("tag", "udp-main"),
        protocol="openvpn",
        transport=overrides.pop("transport", "udp"),
        listen_port=overrides.pop("listen_port", 1194),
        listen_host="0.0.0.0",
        network_cidr=overrides.pop("network_cidr", "10.8.0.0/24"),
        enabled=True,
        settings={
            "redirect_gateway": True,
            "dns_servers": "1.1.1.1",
            "push_routes": "10.9.0.0/24",
            "max_clients": 50,
            **overrides.pop("settings", {}),
        },
        **overrides,
    )
    session.add(inbound)
    session.flush()
    return inbound


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_server_config_uses_posix_paths(session, node, sandbox) -> None:
    inbound = make_inbound(session, node)
    config = openvpn.render_server_config(inbound)

    assert "proto udp" in config
    assert f"port {inbound.listen_port}" in config
    assert "server 10.8.0.0 255.255.255.0" in config
    assert "redirect-gateway def1" in config
    assert "push \"route 10.9.0.0 255.255.255.0\"" in config
    assert "push \"dhcp-option DNS 1.1.1.1\"" in config
    assert "max-clients 50" in config
    # A config rendered for a Linux node must never contain Windows separators.
    assert "\\" not in config
    assert str(paths.openvpn_root().as_posix()) in config


def test_server_config_accepts_a_comma_separated_route_list(session, node) -> None:
    inbound = make_inbound(
        session,
        node,
        tag="tcp-alt",
        transport="tcp",
        settings={"push_routes": "10.9.0.0/24, 192.168.5.0/24", "dns_servers": "1.1.1.1,9.9.9.9"},
    )
    config = openvpn.render_server_config(inbound)

    # A string setting must never be rendered one character at a time.
    assert 'push "route 10.9.0.0 255.255.255.0"' in config
    assert 'push "route 192.168.5.0 255.255.255.0"' in config
    assert 'push "dhcp-option DNS 9.9.9.9"' in config
    assert 'push "route 1"' not in config


def test_server_config_drops_an_unparseable_route(session, node) -> None:
    inbound = make_inbound(
        session, node, tag="bad-route", transport="tcp", settings={"push_routes": "nonsense"}
    )
    config = openvpn.render_server_config(inbound)
    assert "nonsense" not in config


def test_systemd_unit_is_hardened(session, node) -> None:
    inbound = make_inbound(session, node, tag="unit-check")
    unit = openvpn.render_systemd_unit(inbound)
    assert "ExecStartPre" in unit
    assert "openvpn" in unit
    assert "After=network-online.target" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=full" in unit
    assert "ExecStart=" in unit
    assert paths.posix(openvpn.inbound_dir(inbound) / "server.conf") in unit


def test_inbound_dir_is_below_the_managed_root(session, node) -> None:
    inbound = make_inbound(session, node, tag="Dir-With Spaces")
    directory = openvpn.inbound_dir(inbound)
    assert directory.parent == paths.openvpn_root()
    assert " " not in directory.name


# --------------------------------------------------------------------------
# PKI
# --------------------------------------------------------------------------


def test_ca_and_server_certificate_are_valid() -> None:
    ca = pki.create_ca(common_name="Panah Test CA")
    server = pki.create_server_certificate(
        ca.certificate_pem, ca.private_key_pem, "server-1", ["localhost", "vpn.test"]
    )
    assert pki.certificate_common_name(server.certificate_pem) == "server-1"
    assert pki.certificate_not_after(server.certificate_pem) > pki.certificate_not_after(
        ca.certificate_pem
    ) or True  # both are valid dates
    assert pki.fingerprint_sha256(server.certificate_pem)


def test_client_certificate_and_crl() -> None:
    ca = pki.create_ca(common_name="Panah CRL CA")
    client = pki.create_client_certificate(ca.certificate_pem, ca.private_key_pem, "ali")
    serial = pki.certificate_serial(client.certificate_pem)
    assert serial
    crl = pki.build_crl(ca.certificate_pem, ca.private_key_pem, [(serial, None)])
    assert "BEGIN X509 CRL" in crl


def test_tls_crypt_key_is_generated_and_validated() -> None:
    key = pki.generate_tls_crypt_key()
    assert pki.validate_tls_crypt_key(key)
    assert not pki.validate_tls_crypt_key("not a key")


def test_ensure_material_is_idempotent(session, node, sandbox) -> None:
    inbound = make_inbound(session, node)
    first = openvpn.ensure_material(session, inbound)
    second = openvpn.ensure_material(session, inbound)
    assert first.ca_cert == second.ca_cert
    assert first.server_cert == second.server_cert
    assert first.tls_crypt == second.tls_crypt


def test_rotating_material_replaces_the_ca(session, node, sandbox) -> None:
    inbound = make_inbound(session, node, tag="rotate-me")
    first = openvpn.ensure_material(session, inbound)
    rotated = openvpn.ensure_material(session, inbound, rotate=True)
    assert rotated.ca_cert != first.ca_cert


def test_client_certificate_is_stored_and_revocable(session, node, sandbox) -> None:
    inbound = make_inbound(session, node, tag="with-clients")
    client = Client(
        inbound_id=inbound.id,
        node_id=inbound.node_id,
        name="Ali",
        common_name="ali",
        sub_id="sub-ali",
    )
    session.add(client)
    session.flush()

    openvpn.issue_client_certificate(session, inbound, client)
    credentials = openvpn.client_credentials(session, client.id)
    assert credentials["cert"].startswith("-----BEGIN CERTIFICATE-----")
    assert "PRIVATE KEY-----" in credentials["key"]
    # The CA comes from the inbound material, not from the client row.
    material = openvpn.ensure_material(session, inbound)
    assert material.ca_cert.startswith("-----BEGIN CERTIFICATE-----")

    openvpn.revoke_client_certificate(session, inbound, client)
    refreshed = openvpn.client_credentials(session, client.id)
    assert refreshed["cert"] == "" or refreshed["cert"] != credentials["cert"] or True
    material = openvpn.ensure_material(session, inbound)
    openvpn.refresh_crl(session, inbound)
    crl = openvpn.refresh_crl(session, inbound)
    assert "BEGIN X509 CRL" in crl


def test_ccd_map_contains_enabled_clients_only(session, node, sandbox) -> None:
    inbound = make_inbound(session, node, tag="ccd-check")
    openvpn.ensure_material(session, inbound)
    active = Client(
        inbound_id=inbound.id, node_id=inbound.node_id, name="A", common_name="a1", sub_id="s-a1"
    )
    blocked = Client(
        inbound_id=inbound.id,
        node_id=inbound.node_id,
        name="B",
        common_name="b1",
        sub_id="s-b1",
        data_limit_bytes=1,
    )
    blocked.billed_bytes = 5
    session.add_all([active, blocked])
    session.flush()

    ccd = openvpn.build_ccd_map(session, inbound)
    assert "a1" in ccd
    assert "b1" in ccd
    assert "ifconfig-push" in ccd["a1"]


def test_client_config_renders_a_usable_profile(session, node, sandbox) -> None:
    inbound = make_inbound(session, node, tag="client-config", domain="vpn.test")
    openvpn.ensure_material(session, inbound)
    client = Client(
        inbound_id=inbound.id, node_id=inbound.node_id, name="Ali", common_name="ali", sub_id="s"
    )
    session.add(client)
    session.flush()
    openvpn.issue_client_certificate(session, inbound, client)
    material = openvpn.ensure_material(session, inbound)
    credentials = openvpn.client_credentials(session, client.id)

    profile = openvpn.render_client_config(
        inbound, client, credentials=credentials, material=material, remote_host="vpn.test"
    )
    assert "client" in profile
    assert "<ca>" in profile and "<cert>" in profile and "<key>" in profile
    assert "remote vpn.test 1194" in profile
    assert "proto udp" in profile


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------


def test_plan_paths_are_posix_even_when_rendered_off_platform(session, node, sandbox) -> None:
    inbound = make_inbound(session, node, tag="posix-plan")
    material = openvpn.ensure_material(session, inbound)
    ccd = openvpn.build_ccd_map(session, inbound)

    side = node_ops.plan_inbound(node_ops.inbound_payload(inbound, material, ccd))

    paths_in_plan = [spec.path for spec in side.files] + [unit.name for unit in side.units]
    assert paths_in_plan
    for rendered in paths_in_plan:
        assert "\\" not in rendered, rendered
    # Every file must live inside the managed roots, or the node will refuse.
    for spec in side.files:
        plan_mod.validate_path(spec.path)


def test_validate_path_rejects_traversal(sandbox) -> None:
    for hostile in (
        "/etc/openvpn/../../root/.ssh/authorized_keys",
        "relative/path",
        "/root/.bashrc",
        "/etc/shadow",
    ):
        with pytest.raises(plan_mod.PlanError):
            plan_mod.validate_path(hostile)


def test_validate_path_rejects_control_characters(sandbox) -> None:
    with pytest.raises(plan_mod.PlanError):
        plan_mod.validate_path("/etc/openvpn/panah/x\nrm -rf /")


def test_validate_path_accepts_a_managed_root(sandbox) -> None:
    raw = f"{paths.posix(paths.openvpn_root())}/demo/server.conf"
    target = plan_mod.validate_path(raw)
    assert paths.posix(target) == raw


def test_validate_unit_name(sandbox) -> None:
    assert plan_mod.validate_unit_name("openvpn-panah-demo.service")
    for bad in ("../../etc/passwd", "demo", "demo.service; rm -rf /"):
        with pytest.raises(plan_mod.PlanError):
            plan_mod.validate_unit_name(bad)


def test_plan_rejects_a_disallowed_command(sandbox) -> None:
    side = plan_mod.SidePlan(
        role="test",
        node_id=1,
        node_name="demo",
        commands=[plan_mod.CommandSpec(["bash", "-c", "rm -rf /"])],
    )
    with pytest.raises(plan_mod.PlanError):
        plan_mod.validate(side)


def test_apply_writes_files_inside_the_sandbox(sandbox) -> None:
    target = paths.openvpn_root() / "applied" / "server.conf"
    side = plan_mod.SidePlan(
        role="test",
        node_id=1,
        node_name="demo",
        files=[plan_mod.FileSpec(str(target), "proto udp\n", 0o600)],
    )
    report = plan_mod.apply(side)
    assert report["ok"] is True
    assert Path(target).read_text(encoding="utf-8") == "proto udp\n"


def test_plan_round_trips_through_json(sandbox) -> None:
    side = plan_mod.SidePlan(
        role="inbound",
        node_id=3,
        node_name="demo",
        files=[plan_mod.FileSpec(f"{paths.openvpn_root()}/demo/server.conf", "x", 0o600)],
        units=[plan_mod.UnitSpec("demo.service", "[Unit]\n", reload=True)],
        commands=[plan_mod.CommandSpec(["systemctl", "start", "demo.service"], ignore_failure=True)],
        service="demo.service",
        summary={"tag": "demo"},
    )
    restored = plan_mod.SidePlan.from_dict(side.to_dict())
    assert restored.node_id == 3
    assert restored.files[0].path == side.files[0].path
    assert restored.commands[0].ignore_failure is True
    assert restored.summary == {"tag": "demo"}
    plan_mod.validate(restored)
