from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from macro_platform.normalization.common import (
    TimezoneRequiredError,
    canonical_json_checksum,
    canonicalize_url,
    normalize_title_for_matching,
    stable_id,
    to_utc,
)


def test_stable_id_is_deterministic_and_namespaced() -> None:
    assert stable_id("news", "a", 1) == stable_id("news", "a", 1)
    assert stable_id("news", "a", 1).startswith("news_")
    assert stable_id("news", "a", 1) != stable_id("news", "a", 2)


def test_prv_017_checksum_ignores_mapping_key_order() -> None:
    assert canonical_json_checksum({"b": 2, "a": 1}) == canonical_json_checksum({"a": 1, "b": 2})


def test_news_002_url_canonicalization_removes_tracking_and_fragment() -> None:
    actual = canonicalize_url("HTTPS://Example.COM/a?utm_source=x&b=2&a=1&spm=abc#section")
    assert actual == "https://example.com/a?a=1&b=2"


def test_news_003_title_normalization_handles_width_case_spacing_and_punctuation() -> None:
    assert normalize_title_for_matching(" ＡＢＣ， Rate\nCUT！ ") == "abc rate cut"
    assert normalize_title_for_matching("abc rate cut") == "abc rate cut"


def test_news_matching_keeps_numeric_and_negation_tokens_distinct() -> None:
    assert normalize_title_for_matching("增长1.0%") != normalize_title_for_matching("增长10%")
    assert normalize_title_for_matching("不构成违约") != normalize_title_for_matching("构成违约")
    assert normalize_title_for_matching("市場") == normalize_title_for_matching("市场")


def test_time_001_to_utc_converts_aware_datetime() -> None:
    value = datetime(2026, 7, 23, 16, tzinfo=timezone(timedelta(hours=8)))
    assert to_utc(value) == datetime(2026, 7, 23, 8, tzinfo=UTC)


def test_time_002_to_utc_rejects_naive_datetime() -> None:
    with pytest.raises(TimezoneRequiredError):
        to_utc(datetime(2026, 7, 23, 8))
