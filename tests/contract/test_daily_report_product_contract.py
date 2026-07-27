from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
GOLDEN_DIR = ROOT / "tests" / "golden"
SPEC_PATH = ROOT / "docs" / "daily-report-v1.md"
FEISHU_CARD_PATH = GOLDEN_DIR / "daily_report_v1_feishu_card.json"

EXPECTED_SECTION_IDS = {
    "executive_summary",
    "cn_highlights",
    "hk_highlights",
    "us_highlights",
    "key_movements",
    "upcoming_calendar",
    "data_quality_notice",
    "source_references",
}


def load_json_fixture(name: str) -> dict[str, Any]:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def assert_section_limits(report: dict[str, Any]) -> None:
    for section_id, section in report["sections"].items():
        assert section["section_id"] == section_id
        visible_text = section.get("text", "")
        if not visible_text:
            visible_text = "".join(
                str(item.get(field, ""))
                for item in section.get("items", [])
                if isinstance(item, dict)
                for field in ("label", "text", "name")
            )
        assert section["character_count"] == len(visible_text)
        assert section["character_count"] <= section["max_characters"]


def assert_section_facts_are_in_snapshot(report: dict[str, Any]) -> None:
    declared_fact_ids = set(report["input_snapshot"]["fact_ids"])
    declared_source_ref_ids = {
        item["source_ref_id"] for item in report["sections"]["source_references"]["items"]
    }

    def walk(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value, *[item for child in value.values() for item in walk(child)]]
        if isinstance(value, list):
            return [item for child in value for item in walk(child)]
        return []

    for section in report["sections"].values():
        for item in walk(section):
            assert set(item.get("fact_ids", [])) <= declared_fact_ids
            assert set(item.get("source_ref_ids", [])) <= declared_source_ref_ids


def test_daily_report_v1_success_fixture_is_the_canonical_complete_contract() -> None:
    report = load_json_fixture("daily_report_v1_success.json")

    assert report["contract_name"] == "DailyReport"
    assert report["contract_version"] == "1.0"
    assert report["report_date"] == "2026-07-23"
    assert report["timezone"] == "Asia/Shanghai"
    assert report["schedule"]["calendar_lookahead_days"] == 7
    assert report["schedule"]["freshness"] == {
        "market_close_max_age_hours": 36,
        "official_news_max_age_hours": 24,
        "macro_observation_max_age_days": 45,
    }
    assert report["status"] == "complete"
    assert report["publication"]["decision"] == "published"
    assert report["publication"]["scheduled_publish_at"] == "2026-07-23T00:30:00Z"
    assert report["publication"]["published_at"] == "2026-07-23T00:30:12Z"
    assert report["generated_at"] == "2026-07-23T00:20:04Z"
    assert set(report["sections"]) == EXPECTED_SECTION_IDS
    assert all(
        report["sections"][section_id]["status"] == "complete"
        for section_id in EXPECTED_SECTION_IDS
    )
    assert_section_limits(report)
    assert_section_facts_are_in_snapshot(report)


def test_daily_report_v1_incomplete_fixture_fails_closed_with_exact_missing_inputs() -> None:
    report = load_json_fixture("daily_report_v1_incomplete.json")

    assert report["contract_name"] == "DailyReport"
    assert report["contract_version"] == "1.0"
    assert report["status"] == "incomplete"
    assert report["publication"] == {
        "decision": "not_published",
        "reason_code": "REQUIRED_INPUT_UNAVAILABLE",
        "scheduled_publish_at": "2026-07-24T00:30:00Z",
        "published_at": None,
    }
    assert report["data_quality"]["missing_required_inputs"] == [
        {
            "input_id": "market.hk.core_indices.previous_close",
            "reason_code": "REQUIRED_INPUT_UNAVAILABLE",
            "reason": "no validated core-index close before cutoff",
        },
        {
            "input_id": "news.cn.official_headlines_24h",
            "reason_code": "REQUIRED_INPUT_UNAVAILABLE",
            "reason": "official headlines arrived after cutoff",
        },
    ]
    assert report["data_quality"]["late_inputs"] == [
        {
            "input_id": "news.cn.official_headlines_24h",
            "reason_code": "LATE_DATA",
            "reason": "available_at is after 2026-07-24T00:15:00Z",
        }
    ]
    assert report["data_quality"]["unavailable_inputs"] == [
        {
            "input_id": "market.hk.core_indices.previous_close",
            "reason_code": "REQUIRED_INPUT_UNAVAILABLE",
            "reason": "no validated core-index close before cutoff",
        }
    ]
    assert set(report["sections"]) == EXPECTED_SECTION_IDS
    assert report["sections"]["hk_highlights"]["status"] == "unavailable"
    assert report["sections"]["data_quality_notice"]["status"] == "complete"
    assert_section_limits(report)
    assert_section_facts_are_in_snapshot(report)


def test_daily_report_v1_spec_references_both_canonical_examples_and_operating_rules() -> None:
    spec = SPEC_PATH.read_text(encoding="utf-8")

    for required_text in (
        "daily_report_v1_success.json",
        "daily_report_v1_incomplete.json",
        "Asia/Shanghai",
        "late_data_cutoff_local",
        "`observed_at`",
        "`released_at`",
        "`generated_at`",
        "Feishu",
    ):
        assert required_text in spec


def test_daily_report_v1_feishu_card_fixture_is_delivery_only() -> None:
    card = json.loads(FEISHU_CARD_PATH.read_text(encoding="utf-8"))
    report = load_json_fixture("daily_report_v1_success.json")

    assert card["schema"] == "2.0"
    assert card["report_contract_version"] == "1.0"
    assert card["report_id"] == "daily-report-2026-07-23-v1"
    assert card["publication_decision"] == "published"
    assert card["body"]["elements"]
    assert all(element["tag"] in {"markdown", "hr"} for element in card["body"]["elements"])
    assert all(element["tag"] != "note" for element in card["body"]["elements"])
    assert report["sections"]["executive_summary"]["text"] in card["body"]["elements"][0]["content"]
    assert (
        report["sections"]["data_quality_notice"]["text"] in card["body"]["elements"][2]["content"]
    )


def test_daily_report_v1_contract_checks_nested_fact_and_source_references() -> None:
    report = load_json_fixture("daily_report_v1_success.json")
    report["sections"]["key_movements"]["items"][0]["fact_ids"] = ["fact.unknown"]

    with pytest.raises(AssertionError):
        assert_section_facts_are_in_snapshot(report)

    report = load_json_fixture("daily_report_v1_success.json")
    report["sections"]["upcoming_calendar"]["items"][0]["source_ref_ids"] = ["src.unknown"]

    with pytest.raises(AssertionError):
        assert_section_facts_are_in_snapshot(report)


@pytest.mark.parametrize(
    "fixture_name",
    ["daily_report_v1_success.json", "daily_report_v1_incomplete.json"],
)
def test_daily_report_v1_fixtures_have_no_secret_or_provider_credential_fields(
    fixture_name: str,
) -> None:
    report = load_json_fixture(fixture_name)
    serialized = json.dumps(report, ensure_ascii=False).lower()

    for forbidden in ("api_key", "apikey", "access_token", "password", "cookie", "secret"):
        assert forbidden not in serialized
