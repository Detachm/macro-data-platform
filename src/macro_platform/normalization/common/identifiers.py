from __future__ import annotations

import hashlib
import json
from typing import Any


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
