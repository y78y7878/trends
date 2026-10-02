from __future__ import annotations

import csv
import re
from pathlib import Path

from rapidfuzz import fuzz, process


DEFAULT_MAPPING_PATH = Path(__file__).resolve().parents[2] / "keyword_mapping.csv"


def load_keyword_mapping(path: str | Path = DEFAULT_MAPPING_PATH) -> dict[str, str]:
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        return {
            row["alias"].strip(): row["stock_id"].strip().upper()
            for row in csv.DictReader(file)
            if row.get("alias", "").strip() and row.get("stock_id", "").strip()
        }


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def match_stock(keyword: str, mapping: dict[str, str], threshold: int = 78) -> tuple[str | None, int]:
    """Return (stock_id, score); exact alias matches always win."""
    normalized_mapping = {_normalize(alias): stock_id for alias, stock_id in mapping.items()}
    normalized_keyword = _normalize(keyword)
    if normalized_keyword in normalized_mapping:
        return normalized_mapping[normalized_keyword], 100
    if not normalized_keyword:
        return None, 0

    best_score = 0
    best_stock_id = None
    for alias, stock_id in mapping.items():
        normalized_alias = _normalize(alias)
        score = max(
            fuzz.ratio(normalized_keyword, normalized_alias),
            fuzz.partial_ratio(normalized_keyword, normalized_alias),
        )
        if score > best_score:
            best_stock_id, best_score = stock_id, int(score)
    return (best_stock_id, best_score) if best_score >= threshold else (None, best_score)


def fuzzy_match(keyword: str, mapping: dict[str, str], threshold: int = 78) -> str | None:
    return match_stock(keyword, mapping, threshold)[0]