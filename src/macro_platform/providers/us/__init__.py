"""United States provider adapters. Implement each upstream in its own module."""

from macro_platform.providers.us.fixture import (
    US_FIXTURE_CONTRACT_ROLE_BINDINGS,
    US_PRODUCTION_PRIMARY_ROLES,
    US_PROVIDER_ID,
    UsFixtureProvider,
    register_us_provider_roles,
)

__all__ = [
    "US_FIXTURE_CONTRACT_ROLE_BINDINGS",
    "US_PROVIDER_ID",
    "US_PRODUCTION_PRIMARY_ROLES",
    "UsFixtureProvider",
    "register_us_provider_roles",
]
