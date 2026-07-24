from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from opencc import OpenCC  # type: ignore[import-untyped]

from macro_platform.normalization.common.identifiers import canonical_json_checksum

TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "ref",
    "spm",
}

_TRADITIONAL_TO_SIMPLIFIED = OpenCC("t2s")
_CJK_OR_DIGIT = r"\u3400-\u9fff0-9"


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            urlencode(sorted(query)),
            "",
        )
    )


def normalize_title_for_matching(title: str) -> str:
    """Normalize a title only for matching, never for persisted content.

    Punctuation and ordinary whitespace both become token boundaries, while
    punctuation between digits remains a numeric boundary.  This makes title
    formatting variants comparable without collapsing ``1-0`` into ``10``.
    """

    normalized = _TRADITIONAL_TO_SIMPLIFIED.convert(unicodedata.normalize("NFKC", title)).casefold()
    normalized = re.sub(r"\+(?=\d)", " plus ", normalized)
    normalized = re.sub(r"[-−](?=\d)", " minus ", normalized)
    characters: list[str] = []
    for index, character in enumerate(normalized):
        if unicodedata.category(character).startswith("P"):
            before = normalized[index - 1] if index else ""
            after = normalized[index + 1] if index + 1 < len(normalized) else ""
            if before.isdecimal() and after.isdecimal():
                characters.append("." if character in {".", ","} else "|")
            else:
                characters.append(" ")
        elif character.isspace():
            characters.append(" ")
        else:
            characters.append(character)

    matched = re.sub(r"\s+", " ", "".join(characters)).strip()
    return re.sub(rf"(?<=[{_CJK_OR_DIGIT}]) (?=[{_CJK_OR_DIGIT}])", "", matched)


def news_cluster_id(
    *,
    canonical_url: str | None,
    content_hash_sha256: str,
    title: str,
    entity_ids: tuple[str, ...],
    published_at: datetime,
) -> str:
    """Build the shared, coarse news-cluster key.

    URL and content hash are retained as exact-deduplication signals for the
    storage layer.  A cluster deliberately uses normalized title, entities and
    a UTC daily window so syndicated copies with distinct URLs or bodies can
    still be presented together.
    """

    normalized_title = normalize_title_for_matching(title)
    cluster_key: dict[str, object] = {
        "title": normalized_title,
        "entity_ids": sorted(entity_ids),
        "published_window": published_at.astimezone(UTC).date().isoformat(),
    }
    if not normalized_title:
        cluster_key["canonical_url"] = canonical_url
        cluster_key["content_hash_sha256"] = content_hash_sha256
    return canonical_json_checksum(cluster_key)
