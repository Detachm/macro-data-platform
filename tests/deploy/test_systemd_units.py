from __future__ import annotations

from pathlib import Path


def test_xtquant_systemd_unit_is_supervised_and_hardened() -> None:
    unit = Path("deploy/systemd/xtquant-data-center.service").read_text(encoding="utf-8")

    for required in (
        "User=macro-data",
        "EnvironmentFile=/etc/macro-data-platform/xtquant.env",
        "BindsTo=xtquant-firewall.service",
        "After=network-online.target xtquant-firewall.service",
        "macro-data-xtquant-server serve",
        "macro-data-xtquant-server check --protocol --wait-seconds 180",
        "Restart=on-failure",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ReadWritePaths=/mnt/data/macro-data-platform/xtquant",
    ):
        assert required in unit
    assert "XTQUANT_TOKEN=" not in unit


def test_xtquant_firewall_unit_is_ordered_and_reversible() -> None:
    unit = Path("deploy/systemd/xtquant-firewall.service").read_text(encoding="utf-8")
    script = Path("deploy/systemd/xtquant-firewall").read_text(encoding="utf-8")

    for required in (
        "Requires=k3s.service",
        "Before=xtquant-data-center.service",
        "EnvironmentFile=/etc/macro-data-platform/xtquant-firewall.env",
        "xtquant-firewall apply",
        "xtquant-firewall check",
        "xtquant-firewall remove",
        "RemainAfterExit=yes",
        "CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW",
    ):
        assert required in unit

    for required in (
        'chain="MACRO-XTQUANT"',
        "-i lo",
        '"$XTQUANT_CNI_INTERFACE"',
        '"$XTQUANT_POD_CIDR"',
        "--mark 0x20000/0x20000",
        "--reject-with tcp-reset",
        "delete_jump",
        "remove_rules",
    ):
        assert required in script

    assert "XTQUANT_TOKEN" not in unit
    assert "XTQUANT_TOKEN" not in script


def test_xtquant_environment_example_contains_no_secret() -> None:
    example = Path("deploy/systemd/xtquant.env.example").read_text(encoding="utf-8")
    values = {
        key: value
        for line in example.splitlines()
        if line and not line.startswith("#")
        for key, value in (line.split("=", 1),)
    }

    assert values["XTQUANT_TOKEN"] == ""
    assert values["XTQUANT_BIND_ADDRESS"] == ""
    assert values["XTQUANT_PORT"] == "58615"
    assert values["XTQUANT_INIT_MARKETS"] == "HK"


def test_xtquant_firewall_environment_example_is_host_specific() -> None:
    example = Path("deploy/systemd/xtquant-firewall.env.example").read_text(encoding="utf-8")
    values = {
        key: value
        for line in example.splitlines()
        if line and not line.startswith("#")
        for key, value in (line.split("=", 1),)
    }

    assert values == {
        "XTQUANT_BIND_ADDRESS": "",
        "XTQUANT_POD_CIDR": "",
        "XTQUANT_CNI_INTERFACE": "cni0",
        "XTQUANT_PORT": "58615",
    }
