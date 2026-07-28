from __future__ import annotations

import json
import signal
import socket
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from macro_platform.host import xtquant_data_center as host_server
from macro_platform.host import xtquant_entitlement_probe as entitlement_probe
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


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"XTQUANT_TOKEN": ""}, "XTQUANT_TOKEN"),
        ({"XTQUANT_BIND_ADDRESS": ""}, "explicit host address"),
        ({"XTQUANT_BIND_ADDRESS": "host.example"}, "must be an IP address"),
        ({"XTQUANT_BIND_ADDRESS": "::1"}, "supports IPv4 only"),
        ({"XTQUANT_PORT": "invalid"}, "must be an integer"),
        ({"XTQUANT_PORT": "70000"}, "between 1 and 65535"),
        ({"XTQUANT_DATA_HOME": ""}, "must be configured"),
        ({"XTQUANT_DATA_HOME": "relative"}, "must be absolute"),
        ({"XTQUANT_INIT_MARKETS": ","}, "at least one market"),
        ({"XTQUANT_HEALTH_INTERVAL_SECONDS": "0"}, "between 1 and 300"),
    ],
)
def test_xtquant_host_config_rejects_unsafe_values(override: dict[str, str], message: str) -> None:
    environment = {
        "XTQUANT_TOKEN": "test-only-token",
        "XTQUANT_BIND_ADDRESS": "127.0.0.1",
        "XTQUANT_DATA_HOME": "/tmp/xtquant-test",
    }

    with pytest.raises(ValueError, match=message):
        config_from_environment(environment | override)


@pytest.mark.parametrize(
    ("ready", "expected_exit", "expected_status"),
    [(True, 0, "ready"), (False, 1, "not_ready")],
)
def test_xtquant_check_cli_reports_only_readiness(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ready: bool,
    expected_exit: int,
    expected_status: str,
) -> None:
    observed: dict[str, object] = {}

    def fake_wait(
        address: str,
        port: int,
        *,
        timeout_seconds: float,
        wait_seconds: float,
        interval_seconds: float,
    ) -> bool:
        observed.update(
            address=address,
            port=port,
            timeout_seconds=timeout_seconds,
            wait_seconds=wait_seconds,
            interval_seconds=interval_seconds,
        )
        return ready

    monkeypatch.setenv("XTQUANT_BIND_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("XTQUANT_PORT", "58615")
    monkeypatch.setattr(host_server, "wait_until_tcp_ready", fake_wait)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "macro-data-xtquant-server",
            "check",
            "--timeout-seconds",
            "1",
            "--wait-seconds",
            "2",
            "--interval-seconds",
            "0.5",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        host_server.main()

    assert raised.value.code == expected_exit
    assert observed == {
        "address": "127.0.0.1",
        "port": 58615,
        "timeout_seconds": 1.0,
        "wait_seconds": 2.0,
        "interval_seconds": 0.5,
    }
    assert json.loads(capsys.readouterr().out)["status"] == expected_status


def test_xtquant_serve_cli_delegates_to_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, _free_port())
    monkeypatch.setattr(sys, "argv", ["macro-data-xtquant-server", "serve"])
    monkeypatch.setattr(host_server, "config_from_environment", lambda: config)
    monkeypatch.setattr(host_server, "serve", lambda received: 7 if received is config else 8)

    with pytest.raises(SystemExit) as raised:
        host_server.main()

    assert raised.value.code == 7


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


def test_xtquant_server_stops_when_its_tcp_health_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ImmediateHealthCheck(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            del timeout
            return False

    port = _free_port()
    data_center = _DataCenter("127.0.0.1", port)
    config = replace(_config(tmp_path, port), optimize_addresses=("feed.example:58615",))
    monkeypatch.setattr(host_server, "tcp_ready", lambda *_args, **_kwargs: False)

    with pytest.raises(ConnectionError, match="health check failed"):
        serve(config, stop_event=ImmediateHealthCheck(), data_center=data_center)

    assert ("set_allow_optmize_address", ["feed.example:58615"]) in data_center.calls
    assert data_center.calls[-1] == ("shutdown", None)


def test_xtquant_tcp_helpers_wait_and_validate_durations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        assert host_server.tcp_ready("127.0.0.1", port, timeout_seconds=1) is True

    assert host_server.tcp_ready("127.0.0.1", port, timeout_seconds=0.01) is False
    attempts = iter((False, True))
    monkeypatch.setattr(host_server, "tcp_ready", lambda *_args, **_kwargs: next(attempts))
    monkeypatch.setattr(host_server.time, "sleep", lambda _seconds: None)
    assert (
        host_server.wait_until_tcp_ready(
            "127.0.0.1",
            port,
            timeout_seconds=1,
            wait_seconds=1,
            interval_seconds=0.1,
        )
        is True
    )
    with pytest.raises(ValueError, match="durations"):
        host_server.wait_until_tcp_ready(
            "127.0.0.1",
            port,
            timeout_seconds=0,
            wait_seconds=1,
            interval_seconds=1,
        )


def test_xtquant_host_private_boundaries_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(FileNotFoundError):
        host_server._require_data_home(tmp_path / "missing")
    monkeypatch.setattr(host_server.os, "access", lambda *_args: False)
    with pytest.raises(PermissionError):
        host_server._require_data_home(tmp_path)

    vendor_module = ModuleType("xtquant.xtdatacenter")
    monkeypatch.setattr(host_server.importlib, "import_module", lambda _name: vendor_module)
    assert host_server._load_data_center() is vendor_module

    installed: dict[signal.Signals, Any] = {}
    monkeypatch.setattr(
        host_server.signal,
        "signal",
        lambda number, handler: installed.__setitem__(number, handler),
    )
    stopped = threading.Event()
    host_server._install_signal_handlers(stopped)
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert stopped.is_set()


class _MetadataClient:
    def __init__(self, details: dict[str, dict[str, Any]]) -> None:
        self.details = details
        self.downloaded = False
        self.connected: tuple[str, int] | None = None

    def connect(self, ip: str, port: int) -> object:
        self.connected = (ip, port)
        return self.connected

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


def test_xtquant_entitlement_cli_emits_minimal_confirmed_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _MetadataClient(
        {
            "hsi.HK": {"ProductName": "恒生指数", "ProductType": 1},
            "hstech.HK": {"InstrumentName": "恒生科技指数", "ExchangeID": "HK"},
        }
    )
    monkeypatch.setattr(entitlement_probe, "_load_metadata_client", lambda: client)
    monkeypatch.setattr(
        sys,
        "argv",
        ["macro-data-xtquant-probe", "--host", "127.0.0.1", "--sector", "港股指数"],
    )

    with pytest.raises(SystemExit) as raised:
        entitlement_probe.main()

    payload = json.loads(capsys.readouterr().out)
    assert raised.value.code == 0
    assert client.connected == ("127.0.0.1", 58615)
    assert payload["status"] == "confirmed"
    assert payload["symbols_scanned"] == 2
    assert "PreClose" not in payload


def test_xtquant_entitlement_probe_fails_when_identity_is_missing_or_ambiguous() -> None:
    incomplete = probe_hk_index_entitlements(
        _MetadataClient(
            {
                "empty.HK": {},
                "only-hsi.HK": {"InstrumentName": "恒生指数"},
            }
        )
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


def test_xtquant_metadata_client_loader_uses_vendor_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vendor_module = ModuleType("xtquant.xtdata")
    monkeypatch.setattr(entitlement_probe.importlib, "import_module", lambda _name: vendor_module)

    assert entitlement_probe._load_metadata_client() is vendor_module
