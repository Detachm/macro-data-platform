from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from macro_platform.providers.base import ProviderSchemaError
from macro_platform.providers.hk.release_calendar import parse_censtatd_release_calendar
from macro_platform.providers.us.release_calendar import parse_bea_release_calendar

NOW = datetime(2026, 7, 28, tzinfo=UTC)


def test_hk_calendar_reschedule_keeps_identity_and_changes_source_revision() -> None:
    original = parse_censtatd_release_calendar(
        _xlsx_bytes("46230", "Consumer Price Index for July 2026"),
        fetched_at=NOW,
        source_url="https://www.censtatd.gov.hk/calendar.xlsx",
        provider_id="hk.censtatd.release-calendar.v1",
        source_name="C&SD schedule",
    )[0]
    rescheduled = parse_censtatd_release_calendar(
        _xlsx_bytes("46231", "Consumer Price Index for July 2026"),
        fetched_at=NOW,
        source_url="https://www.censtatd.gov.hk/calendar.xlsx",
        provider_id="hk.censtatd.release-calendar.v1",
        source_name="C&SD schedule",
    )[0]

    assert original.release_id == rescheduled.release_id
    assert original.source.provider_record_id == rescheduled.source.provider_record_id
    assert original.source.checksum_sha256 != rescheduled.source.checksum_sha256
    assert original.scheduled_at != rescheduled.scheduled_at


def test_bea_calendar_reschedule_keeps_identity_and_changes_source_revision() -> None:
    original = parse_bea_release_calendar(
        _bea_html("July 30"),
        fetched_at=NOW,
        source_url="https://www.bea.gov/news/schedule",
        provider_id="us.official.release-calendar.v1",
    )[0]
    rescheduled = parse_bea_release_calendar(
        _bea_html("July 31"),
        fetched_at=NOW,
        source_url="https://www.bea.gov/news/schedule",
        provider_id="us.official.release-calendar.v1",
    )[0]

    assert original.release_id == rescheduled.release_id
    assert original.source.provider_record_id == rescheduled.source.provider_record_id
    assert original.source.checksum_sha256 != rescheduled.source.checksum_sha256
    assert original.scheduled_at != rescheduled.scheduled_at


@pytest.mark.parametrize("case", ["not-a-zip", "bad-date"])
def test_hk_calendar_rejects_malformed_or_non_numeric_workbooks(case: str) -> None:
    content = (
        b"not-a-zip"
        if case == "not-a-zip"
        else _xlsx_bytes("not-a-date", "Consumer Price Index for July 2026")
    )
    with pytest.raises(ProviderSchemaError):
        parse_censtatd_release_calendar(
            content,
            fetched_at=NOW,
            source_url="https://www.censtatd.gov.hk/calendar.xlsx",
            provider_id="hk.censtatd.release-calendar.v1",
            source_name="C&SD schedule",
        )


def test_bea_calendar_rejects_a_page_without_dated_rows() -> None:
    with pytest.raises(ProviderSchemaError, match="no dated releases"):
        parse_bea_release_calendar(
            '<h2>Year 2026</h2><table><tr><td class="scheduled-date">TBA</td>'
            '<td class="release-title">GDP</td></tr></table>',
            fetched_at=NOW,
            source_url="https://www.bea.gov/news/schedule",
            provider_id="us.official.release-calendar.v1",
        )


def _bea_html(release_date: str) -> str:
    return f"""
    <h2>Year 2026</h2><table><tr>
      <td class="scheduled-date"><div>{release_date}</div><small>8:30 AM</small></td>
      <td class="release-title">GDP (Advance Estimate), 2nd Quarter 2026</td>
    </tr></table>
    """


def _xlsx_bytes(serial: str, title: str) -> bytes:
    rows = (
        (
            "Release Date",
            "Subject",
            "Sub-subject",
            "Series",
            "Title",
            "Footnote No",
            "Footnote Content",
        ),
        (serial, "Prices", "Consumer Prices", "Consumer Price Index", title, "", ""),
    )
    strings = [value for row in rows for value in row if not value.isdigit()]
    string_index = {value: index for index, value in enumerate(strings)}
    xml_rows: list[str] = []
    for row_number, row in enumerate(rows, start=1):
        cells: list[str] = []
        for column, value in zip("ABCDEFG", row, strict=True):
            if value.isdigit():
                cells.append(f'<c r="{column}{row_number}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{column}{row_number}" t="s"><v>{string_index[value]}</v></c>')
        xml_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    shared = "".join(f"<si><t>{value}</t></si>" for value in strings)
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared}</sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>",
        )
    return output.getvalue()
