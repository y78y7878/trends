"""Official names of TWSE/TPEx listed codes, used to catch stock codes Gemini gets wrong.

Gemini sometimes pairs a company with another company's code (「冠德」 → 2466, which is 冠西電).
The daily-quote endpoints of TWSE and TPEx list every trading code with its official short
name; they are cached next to the database (``data.listed_companies.json``) and refreshed
weekly. Without network access the check is skipped rather than blocking resolution.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

from trends.database import DB_PATH


TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
CACHE_PATH = DB_PATH.with_name(f"{DB_PATH.stem}.listed_companies.json")
MAX_AGE_SECONDS = 7 * 24 * 3600
_NOISE = re.compile(r"股份有限公司|有限公司|公司|-KY|KY|-創|\*|\s")


def fetch_listed(timeout: float = 30) -> dict[str, str]:
    names: dict[str, str] = {}
    response = requests.get(TWSE_URL, timeout=timeout)
    response.raise_for_status()
    for item in response.json():
        names[str(item["Code"]).strip()] = str(item["Name"]).strip()
    response = requests.get(TPEX_URL, timeout=timeout)
    response.raise_for_status()
    for item in response.json():
        code = str(item.get("SecuritiesCompanyCode") or "").strip()
        if code:
            names.setdefault(code, str(item.get("CompanyName") or "").strip())
    return names


def load_listed(cache_path: Path = CACHE_PATH, max_age: float = MAX_AGE_SECONDS) -> dict[str, str]:
    """Code -> official short name; an empty dict when neither the cache nor the network works."""
    cached: dict[str, str] = {}
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        if cached and time.time() - cache_path.stat().st_mtime < max_age:
            return cached
    try:
        fresh = fetch_listed()
    except (requests.RequestException, ValueError, KeyError) as error:
        print(f"[LISTED] official code list unavailable ({error}); using {'cache' if cached else 'no check'}")
        return cached
    cache_path.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
    return fresh


def _is_subsequence(short: str, full: str) -> bool:
    characters = iter(full)
    return all(character in characters for character in short)


def name_matches(name: str, official: str) -> bool:
    """True when one name abbreviates the other: every character of the shorter one appears in
    order in the longer one (「中信金」 in 「中國信託金融控股」, 「網家」 in 「網路家庭」)."""
    a, b = _NOISE.sub("", name).upper(), _NOISE.sub("", official).upper()
    if not a or not b:
        return False
    short, full = (a, b) if len(a) <= len(b) else (b, a)
    return _is_subsequence(short, full)
