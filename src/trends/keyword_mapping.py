from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz


DEFAULT_MAPPING_PATH = Path(__file__).resolve().parents[2] / "keyword_theme_mapping.csv"
MAPPING_COLUMNS = ("keyword", "type", "stock_id")
MAPPING_TYPES = ("company", "industry", "theme")


@lru_cache(maxsize=4)
def _read_mapping(path: str, modified_at: int) -> tuple[tuple[str, str, str], ...]:
    del modified_at
    with Path(path).open(encoding="utf-8-sig", newline="") as file:
        rows = [
            (
                row["keyword"].strip(),
                row["type"].strip().lower(),
                row["stock_id"].strip().upper(),
            )
            for row in csv.DictReader(file)
            if all(row.get(column, "").strip() for column in MAPPING_COLUMNS)
            and row["type"].strip().lower() in MAPPING_TYPES
        ]
    return tuple(rows)


def load_keyword_mapping(path: str | Path = DEFAULT_MAPPING_PATH) -> pd.DataFrame:
    mapping_path = Path(path).resolve()
    modified_at = mapping_path.stat().st_mtime_ns
    rows = _read_mapping(str(mapping_path), modified_at)
    return pd.DataFrame(rows, columns=MAPPING_COLUMNS)


def _normalize(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def match_keyword(
    keyword: str,
    mapping: pd.DataFrame,
    threshold: int = 88,
) -> tuple[str, str | None, list[str], int]:
    """Return canonical keyword, event type, related stock IDs, and match score."""
    normalized_keyword = _normalize(keyword)
    if not normalized_keyword or mapping.empty:
        return keyword.strip(), None, [], 0

    candidates = mapping[["keyword", "type"]].drop_duplicates().copy()
    candidates["normalized"] = candidates["keyword"].map(_normalize)
    exact = candidates[candidates["normalized"] == normalized_keyword]
    if not exact.empty:
        selected = exact.iloc[0]
        score = 100
    else:
        if len(normalized_keyword) < 3:
            return keyword.strip(), None, [], 0
        candidates["score"] = candidates["normalized"].map(
            lambda value: max(
                fuzz.ratio(normalized_keyword, value),
                fuzz.partial_ratio(normalized_keyword, value) if len(value) >= 3 else 0,
            )
        )
        candidates["keyword_length"] = candidates["normalized"].str.len()
        candidates = candidates.sort_values(["score", "keyword_length"], ascending=[False, False])
        best_index = candidates.index[0]
        score = int(candidates.iloc[0]["score"])
        if score < threshold:
            return keyword.strip(), None, [], score
        selected = candidates.loc[best_index]

    related_stock_ids = mapping.loc[
        (mapping["keyword"] == selected["keyword"])
        & (mapping["type"] == selected["type"]),
        "stock_id",
    ].drop_duplicates().tolist()
    return str(selected["keyword"]), str(selected["type"]), related_stock_ids, score


def get_stock_pool(mapping: pd.DataFrame | None = None) -> list[str]:
    keyword_mapping = mapping if mapping is not None else load_keyword_mapping()
    return sorted(keyword_mapping["stock_id"].drop_duplicates().tolist())