"""Supervised host entrypoint for the paid XtQuant data centre."""

from __future__ import annotations

import argparse
import importlib
import ipaddress
import json
import os
import signal
import socket
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Protocol


class XtQuantDataCenter(Protocol):
    def set_token(self, token: str) -> None: ...

    def set_data_home_dir(self, data_home_dir: str) -> None: ...

    def set_init_markets(self, markets: list[str]) -> None: ...

    def set_allow_optmize_address(self, allow_list: list[str]) -> None: ...

    def init(self, start_local_service: bool = True) -> None: ...

    def listen(self, ip: str, port: int) -> tuple[str, int]: ...

    def shutdown(self) -> None: ...


@dataclass(frozen=True, slots=True)
class XtQuantHostConfig:
    token: str = field(repr=False)
    bind_address: str
    port: int
    data_home: Path
    init_markets: tuple[str, ...]
    optimize_addresses: tuple[str, ...]
    health_interval_seconds: float


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or check the host XtQuant data centre")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="run the supervised data-centre process")
    check_parser = subparsers.add_parser("check", help="perform a safe TCP readiness check")
    check_parser.add_argument("--timeout-seconds", type=float, default=3.0)
    check_parser.add_argument("--wait-seconds", type=float, default=0.0)
    check_parser.add_argument("--interval-seconds", type=float, default=2.0)
    arguments = parser.parse_args()

    if arguments.command == "check":
        bind_address, port = _connection_target_from_environment()
        ready = wait_until_tcp_ready(
            bind_address,
            port,
            timeout_seconds=arguments.timeout_seconds,
            wait_seconds=arguments.wait_seconds,
            interval_seconds=arguments.interval_seconds,
        )
        print(
            json.dumps(
                {"service": "xtquant-data-center", "status": "ready" if ready else "not_ready"},
                separators=(",", ":"),
            )
        )
        raise SystemExit(0 if ready else 1)

    config = config_from_environment()
    raise SystemExit(serve(config))


def config_from_environment(environ: dict[str, str] | None = None) -> XtQuantHostConfig:
    values = os.environ if environ is None else environ
    token = values.get("XTQUANT_TOKEN", "").strip()
    if not token:
        raise ValueError("XTQUANT_TOKEN must be provided by the protected EnvironmentFile")
    bind_address = _parse_bind_address(values.get("XTQUANT_BIND_ADDRESS", ""))
    port = _parse_port(values.get("XTQUANT_PORT", "58615"))
    data_home_value = values.get("XTQUANT_DATA_HOME", "").strip()
    if not data_home_value:
        raise ValueError("XTQUANT_DATA_HOME must be configured")
    data_home = Path(data_home_value)
    if not data_home.is_absolute():
        raise ValueError("XTQUANT_DATA_HOME must be absolute")
    init_markets = _csv(values.get("XTQUANT_INIT_MARKETS", "HK"))
    if not init_markets:
        raise ValueError("XTQUANT_INIT_MARKETS must contain at least one market")
    optimize_addresses = _csv(values.get("XTQUANT_OPTIMIZE_ADDRESSES", ""))
    health_interval_seconds = float(values.get("XTQUANT_HEALTH_INTERVAL_SECONDS", "10"))
    if not 1 <= health_interval_seconds <= 300:
        raise ValueError("XTQUANT_HEALTH_INTERVAL_SECONDS must be between 1 and 300")
    return XtQuantHostConfig(
        token=token,
        bind_address=bind_address,
        port=port,
        data_home=data_home,
        init_markets=init_markets,
        optimize_addresses=optimize_addresses,
        health_interval_seconds=health_interval_seconds,
    )


def serve(
    config: XtQuantHostConfig,
    *,
    stop_event: threading.Event | None = None,
    data_center: XtQuantDataCenter | None = None,
) -> int:
    _require_data_home(config.data_home)
    if not port_available(config.bind_address, config.port):
        raise OSError(f"XtQuant listen port {config.port} is already occupied")
    resolved_data_center = data_center or _load_data_center()
    resolved_stop_event = stop_event or threading.Event()
    if stop_event is None:
        _install_signal_handlers(resolved_stop_event)

    initialized = False
    try:
        resolved_data_center.set_token(config.token)
        resolved_data_center.set_data_home_dir(str(config.data_home))
        resolved_data_center.set_init_markets(list(config.init_markets))
        if config.optimize_addresses:
            resolved_data_center.set_allow_optmize_address(list(config.optimize_addresses))
        resolved_data_center.init(False)
        initialized = True
        listened_address, listened_port = resolved_data_center.listen(
            config.bind_address, config.port
        )
        if listened_address != config.bind_address or listened_port != config.port:
            raise RuntimeError("XtQuant data centre listened on an unexpected endpoint")
        print(json.dumps({"service": "xtquant-data-center", "status": "started"}))
        while not resolved_stop_event.wait(config.health_interval_seconds):
            if not tcp_ready(config.bind_address, config.port, timeout_seconds=2.0):
                raise ConnectionError("XtQuant data-centre TCP health check failed")
        return 0
    finally:
        if initialized:
            resolved_data_center.shutdown()


def port_available(address: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            candidate.bind((address, port))
        except OSError:
            return False
    return True


def tcp_ready(address: str, port: int, *, timeout_seconds: float) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def wait_until_tcp_ready(
    address: str,
    port: int,
    *,
    timeout_seconds: float,
    wait_seconds: float,
    interval_seconds: float,
) -> bool:
    if timeout_seconds <= 0 or wait_seconds < 0 or interval_seconds <= 0:
        raise ValueError("TCP check durations must be positive")
    deadline = time.monotonic() + wait_seconds
    while True:
        if tcp_ready(address, port, timeout_seconds=timeout_seconds):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval_seconds, remaining))


def _connection_target_from_environment() -> tuple[str, int]:
    address = _parse_bind_address(os.environ.get("XTQUANT_BIND_ADDRESS", ""))
    return address, _parse_port(os.environ.get("XTQUANT_PORT", "58615"))


def _parse_bind_address(value: str) -> str:
    address = value.strip()
    if not address:
        raise ValueError("XTQUANT_BIND_ADDRESS must be an explicit host address")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as error:
        raise ValueError("XTQUANT_BIND_ADDRESS must be an IP address") from error
    if parsed.version != 4:
        raise ValueError("XTQUANT_BIND_ADDRESS currently supports IPv4 only")
    if parsed.is_unspecified:
        raise ValueError("XTQUANT_BIND_ADDRESS must not expose every host interface")
    return address


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError("XTQUANT_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("XTQUANT_PORT must be between 1 and 65535")
    return port


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _require_data_home(path: Path) -> None:
    if not path.is_dir():
        raise FileNotFoundError("XTQUANT_DATA_HOME must already exist")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise PermissionError("XTQUANT_DATA_HOME is not readable and writable")


def _load_data_center() -> XtQuantDataCenter:
    module: ModuleType = importlib.import_module("xtquant.xtdatacenter")
    return module


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def request_stop(_signal_number: int, _frame: object) -> None:
        stop_event.set()

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_number, request_stop)


__all__: Sequence[str] = (
    "XtQuantHostConfig",
    "config_from_environment",
    "main",
    "port_available",
    "serve",
    "tcp_ready",
    "wait_until_tcp_ready",
)
