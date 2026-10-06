from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

import pandas as pd

from trends.timeutil import (
    PACIFIC,
    TAIPEI,
    format_db_datetime,
    local_to_utc,
    parse_db_datetime,
    taipei_date,
    taipei_day_start_utc,
    utc_now,
    utc_series_to_taipei_date,
    utc_to_taipei,
)


class ParseFormatTests(unittest.TestCase):
    def test_parses_both_stored_layouts(self) -> None:
        self.assertEqual(parse_db_datetime("2026-10-01 03:36:57"), datetime(2026, 10, 1, 3, 36, 57))
        self.assertEqual(parse_db_datetime("2026-08-21 15:27:59.000000"), datetime(2026, 8, 21, 15, 27, 59))

    def test_rejects_unknown_layout_instead_of_guessing(self) -> None:
        for value in ("2026-10-01", "2026/10/01 03:36:57", "2026-10-01T03:36:57Z"):
            with self.assertRaises(ValueError):
                parse_db_datetime(value)

    def test_missing_values_return_none(self) -> None:
        self.assertIsNone(parse_db_datetime(None))
        self.assertIsNone(parse_db_datetime(pd.NaT))

    def test_format_is_canonical_and_converts_aware_values(self) -> None:
        self.assertEqual(format_db_datetime(datetime(2026, 10, 1, 3, 36, 57, 999)), "2026-10-01 03:36:57")
        aware = datetime(2026, 10, 1, 11, 0, tzinfo=TAIPEI)
        self.assertEqual(format_db_datetime(aware), "2026-10-01 03:00:00")


class ConversionTests(unittest.TestCase):
    def test_pacific_daylight_time_to_utc(self) -> None:
        self.assertEqual(local_to_utc(datetime(2026, 10, 5, 20, 40), PACIFIC), datetime(2026, 10, 6, 3, 40))

    def test_pacific_standard_time_to_utc(self) -> None:
        self.assertEqual(local_to_utc(datetime(2026, 12, 1, 12, 0), PACIFIC), datetime(2026, 12, 1, 20, 0))

    def test_taipei_to_utc(self) -> None:
        self.assertEqual(local_to_utc(datetime(2026, 8, 21, 15, 27, 59), TAIPEI), datetime(2026, 8, 21, 7, 27, 59))

    def test_ambiguous_and_nonexistent_times_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            local_to_utc(datetime(2026, 11, 1, 1, 30), PACIFIC)
        with self.assertRaises(ValueError):
            local_to_utc(datetime(2026, 3, 8, 2, 30), PACIFIC)
        self.assertEqual(
            local_to_utc(datetime(2026, 11, 1, 1, 30), PACIFIC, strict=False),
            datetime(2026, 11, 1, 8, 30),
        )

    def test_rejects_aware_input(self) -> None:
        with self.assertRaises(ValueError):
            local_to_utc(datetime(2026, 1, 1, tzinfo=UTC), PACIFIC)


class TaipeiBusinessDateTests(unittest.TestCase):
    def test_taipei_day_boundary_is_utc_16_00(self) -> None:
        self.assertEqual(taipei_date(datetime(2026, 10, 5, 15, 59)), date(2026, 10, 5))
        self.assertEqual(taipei_date(datetime(2026, 10, 5, 16, 0)), date(2026, 10, 6))
        self.assertEqual(taipei_day_start_utc(date(2026, 8, 21)), datetime(2026, 8, 20, 16, 0))

    def test_utc_to_taipei_is_aware(self) -> None:
        converted = utc_to_taipei(datetime(2026, 10, 6, 6, 13))
        self.assertEqual((converted.hour, converted.minute), (14, 13))
        self.assertEqual(converted.tzinfo, TAIPEI)

    def test_utc_now_is_naive(self) -> None:
        self.assertIsNone(utc_now().tzinfo)

    def test_series_conversion_handles_mixed_layouts(self) -> None:
        values = pd.Series(["2026-10-05 15:59:00", "2026-10-05 16:00:00.000000", None])
        result = utc_series_to_taipei_date(values)
        self.assertEqual(result.iloc[0], pd.Timestamp("2026-10-05"))
        self.assertEqual(result.iloc[1], pd.Timestamp("2026-10-06"))
        self.assertTrue(pd.isna(result.iloc[2]))


if __name__ == "__main__":
    unittest.main()
