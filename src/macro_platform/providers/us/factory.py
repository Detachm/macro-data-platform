"""Explicit environment selection for US fixture and Twelve Data provider graphs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Literal

import httpx
from pydantic import SecretStr

from macro_platform.config import Settings
from macro_platform.contracts.provider import Dataset
from macro_platform.providers.registry import ProviderRegistry
from macro_platform.providers.us.fixture import UsFixtureProvider, register_us_provider_roles
from macro_platform.providers.us.release_calendar import (
    US_OFFICIAL_CALENDAR_ROLE,
    UsOfficialReleaseCalendarProvider,
)
from macro_platform.providers.us.twelve_data import (
    TWELVE_DATA_DEFAULT_INSTRUMENTS,
    TwelveDataDailyBarsProvider,
    TwelveDataInstrument,
    register_us_twelve_data_provider_roles,
)

UsProviderMode = Literal["fixture", "live"]
AppEnvironment = Literal["development", "test", "production"]


def create_us_provider_registry(
    *,
    app_env: AppEnvironment,
    provider_mode: UsProviderMode,
    registry: ProviderRegistry | None = None,
    api_key: SecretStr | None = None,
    live_instruments: Sequence[TwelveDataInstrument] = TWELVE_DATA_DEFAULT_INSTRUMENTS,
    cursor_signing_secret: str | None = None,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] | None = None,
    fixture_name: str = "success",
) -> ProviderRegistry:
    """Create the US provider graph without allowing a production fixture fallback.

    ``live_instruments`` is deliberately an explicit dependency.  A ticker is
    not enough to establish a durable platform instrument identity, so runtime
    composition must supply the reviewed alias/instrument mapping.
    """

    if app_env == "production" and provider_mode != "live":
        raise ValueError("production US provider mode must be live")

    resolved_registry = registry if registry is not None else ProviderRegistry()
    if provider_mode == "fixture":
        fixture_provider = (
            UsFixtureProvider.from_fixture(fixture_name)
            if clock is None
            else UsFixtureProvider.from_fixture(fixture_name, clock=clock)
        )
        register_us_provider_roles(resolved_registry, fixture_provider)
        return resolved_registry

    if api_key is None or not api_key.get_secret_value().strip():
        raise ValueError("live Twelve Data provider requires a runtime API key")
    if not cursor_signing_secret:
        raise ValueError("live Twelve Data provider requires a cursor signing secret")
    live_provider = TwelveDataDailyBarsProvider(
        api_key=api_key,
        instruments=live_instruments,
        client=client,
        cursor_signing_secret=cursor_signing_secret,
        clock=clock,
    )
    register_us_twelve_data_provider_roles(resolved_registry, live_provider)
    calendar_provider = (
        UsOfficialReleaseCalendarProvider(
            client=client,
            cursor_signing_secret=cursor_signing_secret,
        )
        if clock is None
        else UsOfficialReleaseCalendarProvider(
            client=client,
            cursor_signing_secret=cursor_signing_secret,
            clock=clock,
        )
    )
    resolved_registry.register(calendar_provider)
    resolved_registry.bind_role(
        US_OFFICIAL_CALENDAR_ROLE,
        calendar_provider.provider_id,
        required_dataset=Dataset.MACRO_RELEASES,
    )
    return resolved_registry


def create_us_provider_registry_from_settings(
    settings: Settings,
    *,
    registry: ProviderRegistry | None = None,
    live_instruments: Sequence[TwelveDataInstrument] = TWELVE_DATA_DEFAULT_INSTRUMENTS,
    client: httpx.AsyncClient | None = None,
    clock: Callable[[], datetime] | None = None,
    fixture_name: str = "success",
) -> ProviderRegistry:
    """Read US live credentials from runtime settings without exposing their values."""

    return create_us_provider_registry(
        app_env=settings.app_env,
        provider_mode=settings.us_provider_mode,
        registry=registry,
        api_key=settings.twelve_data_api_key,
        live_instruments=live_instruments,
        cursor_signing_secret=(
            None
            if settings.twelve_data_cursor_secret is None
            else settings.twelve_data_cursor_secret.get_secret_value()
        ),
        client=client,
        clock=clock,
        fixture_name=fixture_name,
    )


__all__ = ["create_us_provider_registry", "create_us_provider_registry_from_settings"]
