from __future__ import annotations

from pathlib import Path


def test_xtquant_systemd_unit_is_supervised_and_hardened() -> None:
    unit = Path("deploy/systemd/xtquant-data-center.service").read_text(encoding="utf-8")

    for required in (
        "User=macro-data",
        "EnvironmentFile=/etc/macro-data-platform/xtquant.env",
        "macro-data-xtquant-server serve",
        "macro-data-xtquant-server check --wait-seconds 180",
        "Restart=on-failure",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ReadWritePaths=/mnt/data/macro-data-platform/xtquant",
    ):
        assert required in unit
    assert "XTQUANT_TOKEN=" not in unit


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
