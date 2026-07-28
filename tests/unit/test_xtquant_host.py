from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from macro_platform.host.xtquant_data_center import (
    XtQuantHostConfig,
    config_from_environment,
    serve,
)
from macro_platform.host.xtquant_entitlement_probe import probe_hk_index_entitlements


class _DataCenter:
    def __init__(self, address: str, port: int) -> None:
        self.address = address
        self.port = port
        self.calls: list[tuple[str, object]] = []

    def set_token(self, token: str) -> None:
        self.calls.append(("set_token", token))

    def set_data_home_dir(self, data_home_dir: str) -> None:
        self.calls.append(("set_data_home_dir", data_home_dir))

    def set_init_markets(self, markets: list[str]) -> None:
        self.calls.append(("set_init_markets", markets))

    def set_allow_optmize_address(self, allow_list: list[str]) -> None:
        self.calls.append(("set_allow_optmize_address", allow_list))

    def init(self, start_local_service: bool = True) -> None:
        self.calls.append(("init", start_local_service))

    def listen(self, ip: str, port: int) -> tuple[str, int]:
        self.calls.append(("listen", (ip, port)))
        return self.address, self.port

    def shutdown(self) -> None:
        self.calls.append(("shutdown", None))


def _config(tmp_path: Path, port: int) -> XtQuantHostConfig:
    return XtQuantHostConfig(
        token="test-only-token",
        bind_address="127.0.0.1",
        port=port,
        data_home=tmp_path,
        init_markets=("HK",),
        optimize_addresses=(),
        health_interval_seconds=1,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_xtquant_host_config_requires_a_specific_address_and_hides_token() -> None:
    environment = {
        "XTQUANT_TOKEN": "test-only-token",
        "XTQUANT_BIND_ADDRESS": "127.0.0.1",
        "XTQUANT_PORT": "58615",
        "XTQUANT_DATA_HOME": "/tmp/xtquant-test",
        "XTQUANT_INIT_MARKETS": "HK,SH",
        "XTQUANT_OPTIMIZE_ADDRESSES": "first.example:123,second.example:456",
        "XTQUANT_HEALTH_INTERVAL_SECONDS": "15",
    }

    config = config_from_environment(environment)

    assert config.init_markets == ("HK", "SH")
    assert config.optimize_addresses == ("first.example:123", "second.example:456")
    assert "test-only-token" not in repr(config)
    with pytest.raises(ValueError, match="must not expose"):
        config_from_environment(environment | {"XTQUANT_BIND_ADDRESS": "0.0.0.0"})


def test_xtquant_server_starts_without_killing_ports_and_shuts_down(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    port = _free_port()
    data_center = _DataCenter("127.0.0.1", port)
    stopped = threading.Event()
    stopped.set()

    exit_code = serve(
        _config(tmp_path, port),
        stop_event=stopped,
        data_center=data_center,
    )

    assert exit_code == 0
    assert ("init", False) in data_center.calls
    assert ("listen", ("127.0.0.1", port)) in data_center.calls
    assert data_center.calls[-1] == ("shutdown", None)
    assert "test-only-token" not in capsys.readouterr().out


def test_xtquant_server_fails_closed_when_the_port_is_occupied(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        data_center = _DataCenter("127.0.0.1", port)

        with pytest.raises(OSError, match="already occupied"):
            serve(
                _config(tmp_path, port),
                stop_event=threading.Event(),
                data_center=data_center,
            )

    assert data_center.calls == []


def test_xtquant_server_shuts_down_when_the_listener_endpoint_is_unexpected(
    tmp_path: Path,
) -> None:
    port = _free_port()
    data_center = _DataCenter("127.0.0.1", port + 1)

    with pytest.raises(RuntimeError, match="unexpected endpoint"):
        serve(
            _config(tmp_path, port),
            stop_event=threading.Event(),
            data_center=data_center,
        )

    assert data_center.calls[-1] == ("shutdown", None)


class _MetadataClient:
    def __init__(self, details: dict[str, dict[str, Any]]) -> None:
        self.details = details
        self.downloaded = False

    def connect(self, ip: str, port: int) -> object:
        return (ip, port)

    def download_sector_data(self) -> None:
        self.downloaded = True

    def get_sector_list(self) -> list[str]:
        return ["A股指数", "港股指数", "香港主板"]

    def get_stock_list_in_sector(self, sector_name: str) -> list[str]:
        return list(self.details) if sector_name == "港股指数" else []

    def get_instrument_detail(
        self, stock_code: str, iscomplete: bool = False
    ) -> dict[str, Any] | None:
        del iscomplete
        return self.details.get(stock_code)


def test_xtquant_entitlement_probe_returns_only_confirmed_index_identities() -> None:
    client = _MetadataClient(
        {
            "source-one.HK": {
                "InstrumentName": "恒生指数",
                "ExchangeID": "HK",
                "ProductType": "index",
                "PreClose": 12345.67,
            },
            "source-two.HK": {
                "InstrumentName": "Hang Seng TECH Index",
                "ExchangeID": "HK",
                "ProductType": {"unexpected": "vendor-payload"},
                "PreClose": 4567.89,
            },
            "unrelated.HK": {
                "InstrumentName": "恒生指数期货",
                "ExchangeID": "HK",
                "ProductType": "future",
            },
        }
    )

    result = probe_hk_index_entitlements(client)

    assert client.downloaded is True
    assert result.status == "confirmed"
    assert {match.source_symbol for match in result.matches} == {
        "source-one.HK",
        "source-two.HK",
    }
    assert {match.product_type for match in result.matches} == {"index", None}
    assert all(not hasattr(match, "pre_close") for match in result.matches)


def test_xtquant_entitlement_probe_fails_when_identity_is_missing_or_ambiguous() -> None:
    incomplete = probe_hk_index_entitlements(
        _MetadataClient({"only-hsi.HK": {"InstrumentName": "恒生指数"}})
    )
    ambiguous = probe_hk_index_entitlements(
        _MetadataClient(
            {
                "hsi-one.HK": {"InstrumentName": "恒生指数"},
                "hsi-two.HK": {"InstrumentName": "Hang Seng Index"},
                "hstech.HK": {"InstrumentName": "恒生科技指数"},
            }
        )
    )

    assert incomplete.status == "incomplete"
    assert incomplete.missing_targets == ("hang_seng_tech_index",)
    assert ambiguous.status == "ambiguous"
    assert ambiguous.ambiguous_targets == ("hang_seng_index",)
