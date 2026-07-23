from __future__ import annotations

import hashlib
import json
from typing import Any


def normalize_canonical_symbol(source_symbol: str) -> str:
    """Normalize a platform canonical symbol without assigning it to a region."""

    source = source_symbol.strip()
    venue_mic, separator, raw_local_symbol = source.partition(":")
    if separator != ":" or ":" in raw_local_symbol:
        raise ValueError("canonical symbol requires '<MIC>:<LOCAL_SYMBOL>'")

    normalized_mic = venue_mic.strip().upper()
    normalized_local_symbol = raw_local_symbol.strip().upper()
    if len(normalized_mic) != 4 or not normalized_mic.isalnum():
        raise ValueError("canonical symbol MIC must be four alphanumeric characters")
    if not normalized_local_symbol:
        raise ValueError("canonical symbol local symbol is empty")

    return f"{normalized_mic}:{normalized_local_symbol}"


def stable_id(namespace: str, *parts: object) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{namespace}_{digest}"


def canonical_json_checksum(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
