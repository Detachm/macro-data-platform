from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from macro_platform.services import report_day_policy as policy_module
from macro_platform.services.report_day_policy import (
    ALWAYS_REQUIRED_REPORT_INPUT_IDS,
    ExchangeReportDayPolicy,
    ReportDayPolicyUnavailableError,
    report_calendar_payload,
)
from macro_platform.services.report_input_quality import (
    REQUIRED_REPORT_INPUT_IDS,
    ReportInputQualityGate,
)
from macro_platform.storage.reporting import ReportInputSnapshot


def test_report_day_policy_requires_all_inputs_on_a_regular_day() -> None:
    policy = ExchangeReportDayPolicy().evaluate(date(2026, 7, 27))

    assert policy.day_type == "regular"
    assert set(policy.required_input_ids) == REQUIRED_REPORT_INPUT_IDS
    assert policy.optional_input_ids == ()
    assert {item.status for item in policy.regions} == {"scheduled_session"}


def test_report_day_policy_marks_all_markets_optional_on_weekends() -> None:
    policy = ExchangeReportDayPolicy().evaluate(date(2026, 7, 26))

    assert policy.day_type == "weekend"
    assert set(policy.required_input_ids) == ALWAYS_REQUIRED_REPORT_INPUT_IDS
    assert set(policy.optional_input_ids) == {
        "market.cn.core_indices.previous_close",
        "market.hk.core_indices.previous_close",
        "market.us.core_indices.previous_close",
    }
    assert {item.status for item in policy.regions} == {"weekend_closed"}
    assert {item.previous_session for item in policy.regions} == {"2026-07-24"}


def test_report_day_policy_is_region_specific_for_exchange_holidays() -> None:
    policy = ExchangeReportDayPolicy().evaluate(date(2026, 7, 3))
    by_region = {item.region: item for item in policy.regions}

    assert policy.day_type == "regional_holiday"
    assert policy.optional_input_ids == ("market.us.core_indices.previous_close",)
    assert by_region["CN"].status == "scheduled_session"
    assert by_region["HK"].status == "scheduled_session"
    assert by_region["US"].status == "exchange_holiday"
    assert by_region["US"].previous_session == "2026-07-02"
    assert report_calendar_payload(policy.to_payload(), report_date=date(2026, 7, 3)) == {
        "day_type": "holiday",
        "holiday_notice": "US 市场因交易所节假日休市；其他地区按正常输入门禁。",
    }


def test_weekend_report_calendar_contains_an_explicit_closure_notice() -> None:
    report_date = date(2026, 7, 26)
    policy = ExchangeReportDayPolicy().evaluate(report_date)

    assert report_calendar_payload(policy.to_payload(), report_date=report_date) == {
        "day_type": "weekend",
        "holiday_notice": "周末，CN/HK/US 市场休市；本期发布宏观消息、日历和分析。",
    }


def test_report_day_policy_fails_closed_when_a_calendar_cannot_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        policy_module.exchange_calendars,
        "get_calendar",
        lambda _name: (_ for _ in ()).throw(ValueError("coverage gap")),
    )

    with pytest.raises(ReportDayPolicyUnavailableError, match="cannot resolve"):
        ExchangeReportDayPolicy().evaluate(date(2026, 7, 27))


def _snapshot(
    report_date: date,
    *,
    report_day_policy: dict[str, object],
    quality_overrides: dict[str, dict[str, object]] | None = None,
) -> ReportInputSnapshot:
    as_of = datetime.combine(report_date, datetime.min.time(), tzinfo=UTC)
    quality = {
        input_id: {
            "status": "available",
            "required": input_id in report_day_policy["required_input_ids"],
        }
        for input_id in REQUIRED_REPORT_INPUT_IDS
    }
    quality.update(quality_overrides or {})
    snapshot_id = f"snapshot-{report_date.isoformat()}"
    timestamp = as_of.isoformat().replace("+00:00", "Z")
    payload = {
        "snapshot_id": snapshot_id,
        "snapshot_version": "1.0",
        "as_of": timestamp,
        "cutoff_at": timestamp,
        "fingerprint_sha256": "a" * 64,
        "fact_ids": ["fact.report-day-policy"],
        "facts": [{"fact_id": "fact.report-day-policy"}],
        "input_quality": quality,
        "report_day_policy": report_day_policy,
    }
    return ReportInputSnapshot(
        snapshot_id=snapshot_id,
        snapshot_version="1.0",
        report_date=report_date,
        as_of=as_of,
        cutoff_at=as_of,
        fingerprint_sha256="a" * 64,
        fact_ids=["fact.report-day-policy"],
        payload=payload,
    )


def test_weekend_market_closure_degrades_without_blocking_publication() -> None:
    report_date = date(2026, 7, 26)
    policy = ExchangeReportDayPolicy().evaluate(report_date).to_payload()
    market_quality = {
        input_id: {
            "status": "unavailable",
            "required": False,
            "reason": "market is closed for this report date",
        }
        for input_id in policy["optional_input_ids"]
    }

    result = ReportInputQualityGate().evaluate(
        _snapshot(report_date, report_day_policy=policy, quality_overrides=market_quality)
    )

    assert result.status == "degraded"
    assert {issue.code for issue in result.issues} == {"UNAVAILABLE_OPTIONAL_INPUT"}


def test_available_previous_closes_still_disclose_scheduled_market_closure() -> None:
    report_date = date(2026, 7, 26)
    policy = ExchangeReportDayPolicy().evaluate(report_date).to_payload()

    result = ReportInputQualityGate().evaluate(_snapshot(report_date, report_day_policy=policy))

    assert result.status == "degraded"
    assert {issue.code for issue in result.issues} == {"SCHEDULED_MARKET_CLOSURE"}


def test_weekend_policy_does_not_downgrade_missing_news_or_calendar() -> None:
    report_date = date(2026, 7, 26)
    policy = ExchangeReportDayPolicy().evaluate(report_date).to_payload()

    result = ReportInputQualityGate().evaluate(
        _snapshot(
            report_date,
            report_day_policy=policy,
            quality_overrides={
                "news.cn.official_headlines_24h": {
                    "status": "missing",
                    "required": True,
                    "reason": "official CN news is unexpectedly missing",
                }
            },
        )
    )

    assert result.status == "blocked"
    assert any(issue.code == "MISSING_REQUIRED_INPUT" for issue in result.issues)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("required_input_ids", []),
        ("policy_id", "unknown-policy"),
        ("day_type", "regular"),
    ],
)
def test_malformed_report_day_policy_fails_closed(field: str, value: object) -> None:
    report_date = date(2026, 7, 26)
    policy = ExchangeReportDayPolicy().evaluate(report_date).to_payload()
    policy[field] = value

    result = ReportInputQualityGate().evaluate(_snapshot(report_date, report_day_policy=policy))

    assert result.status == "blocked"
    assert result.issues[0].code == "REPORT_DAY_POLICY_INVALID"


def test_missing_closed_market_quality_is_an_explicit_degradation() -> None:
    report_date = date(2026, 7, 26)
    policy = ExchangeReportDayPolicy().evaluate(report_date).to_payload()
    snapshot = _snapshot(report_date, report_day_policy=policy)
    snapshot.payload["input_quality"] = {
        input_id: value
        for input_id, value in snapshot.payload["input_quality"].items()
        if input_id not in policy["optional_input_ids"]
    }

    result = ReportInputQualityGate().evaluate(snapshot)

    assert result.status == "degraded"
    assert [issue.code for issue in result.issues] == [
        "OPTIONAL_INPUT_UNAVAILABLE",
        "OPTIONAL_INPUT_UNAVAILABLE",
        "OPTIONAL_INPUT_UNAVAILABLE",
    ]
