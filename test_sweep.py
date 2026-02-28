from datetime import date
from unittest.mock import patch

from la_sweep_bot import (
    format_street_summary,
    get_week_occurrence,
    is_sweep_today,
    next_sweep_dates,
)


class TestGetWeekOccurrence:
    def test_first_week(self):
        assert get_week_occurrence(date(2026, 3, 2)) == 1  # 1st Monday

    def test_second_week(self):
        assert get_week_occurrence(date(2026, 3, 9)) == 2  # 2nd Monday

    def test_third_week(self):
        assert get_week_occurrence(date(2026, 3, 16)) == 3  # 3rd Monday

    def test_fourth_week(self):
        assert get_week_occurrence(date(2026, 3, 23)) == 4  # 4th Monday

    def test_fifth_week(self):
        assert get_week_occurrence(date(2026, 3, 30)) == 5  # 5th Monday


class TestIsSweepToday:
    def _patch_today(self, d):
        from datetime import datetime

        from la_sweep_bot import LA_TZ

        return patch(
            "la_sweep_bot.datetime",
            wraps=__import__("datetime").datetime,
            **{"now.return_value": datetime(d.year, d.month, d.day, 12, 0, tzinfo=LA_TZ)},
        )

    def test_sweep_day_matches(self):
        # 2026-03-09 is Monday, 2nd occurrence → "2 & 4" should match
        with self._patch_today(date(2026, 3, 9)):
            assert is_sweep_today("Monday", "2 & 4") is True

    def test_wrong_day(self):
        # 2026-03-09 is Monday, not Tuesday
        with self._patch_today(date(2026, 3, 9)):
            assert is_sweep_today("Tuesday", "2 & 4") is False

    def test_wrong_week(self):
        # 2026-03-09 is 2nd Monday → "1 & 3" should not match
        with self._patch_today(date(2026, 3, 9)):
            assert is_sweep_today("Monday", "1 & 3") is False

    def test_holiday_skipped(self):
        # 2026-01-19 is MLK Day (holiday) and a Monday
        with self._patch_today(date(2026, 1, 19)):
            assert is_sweep_today("Monday", "1 & 3") is False


class TestNextSweepDates:
    def _patch_today(self, d):
        from datetime import datetime

        from la_sweep_bot import LA_TZ

        return patch(
            "la_sweep_bot.datetime",
            wraps=__import__("datetime").datetime,
            **{"now.return_value": datetime(d.year, d.month, d.day, 12, 0, tzinfo=LA_TZ)},
        )

    def test_returns_correct_count(self):
        with self._patch_today(date(2026, 3, 1)):
            dates = next_sweep_dates("Monday", "2 & 4", count=4)
            assert len(dates) == 4

    def test_all_dates_are_correct_day(self):
        with self._patch_today(date(2026, 3, 1)):
            dates = next_sweep_dates("Monday", "2 & 4", count=4)
            assert all(d.weekday() == 0 for d in dates)  # 0 = Monday

    def test_all_dates_in_correct_weeks(self):
        with self._patch_today(date(2026, 3, 1)):
            dates = next_sweep_dates("Monday", "2 & 4", count=4)
            occurrences = [get_week_occurrence(d) for d in dates]
            assert all(o in (2, 4) for o in occurrences)

    def test_skips_holidays(self):
        with self._patch_today(date(2026, 1, 1)):
            dates = next_sweep_dates("Monday", "1 & 3", count=4)
            # MLK Day is 2026-01-19, should not appear
            assert date(2026, 1, 19) not in dates

    def test_invalid_day_returns_empty(self):
        with self._patch_today(date(2026, 3, 1)):
            assert next_sweep_dates("Saturday", "1 & 3") == []


class TestFormatStreetSummary:
    def _make_route(self, **overrides):
        defaults = {
            "Route": "12P356 M",
            "STNAME": "VENICE",
            "STSFX": "BLVD",
            "TDIR": "E",
            "Posted_Day": "Monday",
            "Posted_Time": "8am-10am",
            "Weeks": "2 & 4",
            "Boundaries": "Main to Grand",
        }
        defaults.update(overrides)
        return defaults

    def test_street_label_excludes_tdir(self):
        result = format_street_summary([self._make_route()])
        assert "VENICE BLVD" in result
        # TDIR should NOT appear in the label
        assert "E VENICE" not in result

    def test_consolidates_days(self):
        routes = [
            self._make_route(Posted_Day="Monday"),
            self._make_route(Posted_Day="Tuesday"),
        ]
        result = format_street_summary(routes)
        assert "Monday & Tuesday" in result

    def test_deduplicates_days(self):
        routes = [
            self._make_route(Posted_Day="Monday"),
            self._make_route(Posted_Day="Monday"),
        ]
        result = format_street_summary(routes)
        # Should appear once, not "Monday & Monday"
        assert "Monday & Monday" not in result
        assert "Monday" in result

    def test_shows_posted_time_directly(self):
        result = format_street_summary([self._make_route(Posted_Time="8am-10am")])
        assert "8am-10am" in result

    def test_shows_schedule(self):
        result = format_street_summary([self._make_route(Weeks="2 & 4")])
        assert "2 & 4" in result
