"""United States provider adapters. Implement each upstream in its own module."""

from macro_platform.providers.us.fixture import (
    US_FIXTURE_CONTRACT_ROLE_BINDINGS,
    US_PRODUCTION_PRIMARY_ROLES,
    US_PROVIDER_ID,
    UsFixtureProvider,
    register_us_provider_roles,
)
from macro_platform.providers.us.twelve_data import (
    TWELVE_DATA_PRIMARY_ROLE,
    TWELVE_DATA_PROVIDER_ID,
    TwelveDataDailyBarsProvider,
    TwelveDataInstrument,
    register_us_twelve_data_provider_roles,
)

__all__ = [
    "US_FIXTURE_CONTRACT_ROLE_BINDINGS",
    "US_PROVIDER_ID",
    "US_PRODUCTION_PRIMARY_ROLES",
    "UsFixtureProvider",
    "register_us_provider_roles",
    "TWELVE_DATA_PRIMARY_ROLE",
    "TWELVE_DATA_PROVIDER_ID",
    "TwelveDataDailyBarsProvider",
    "TwelveDataInstrument",
    "register_us_twelve_data_provider_roles",
]
