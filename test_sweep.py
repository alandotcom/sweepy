import json
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

import db as db_mod
from la_sweep_bot import (
    LA_TZ,
    SWEEP_WEEK_2026,
    format_street_summary,
    is_sweep_today,
    next_sweep_dates,
    normalize_address,
)


def _patch_today(d, hour=12):
    """Mock la_sweep_bot.datetime.now() to return the given date at the given hour."""
    return patch(  # type: ignore[no-matching-overload]
        "la_sweep_bot.datetime",
        wraps=__import__("datetime").datetime,
        **{"now.return_value": datetime(d.year, d.month, d.day, hour, 0, tzinfo=LA_TZ)},
    )


class TestSweepWeekCalendar:
    def test_first_week(self):
        assert SWEEP_WEEK_2026[date(2026, 3, 2)] == 1  # Mon of 1st week

    def test_second_week(self):
        assert SWEEP_WEEK_2026[date(2026, 3, 9)] == 2

    def test_third_week(self):
        assert SWEEP_WEEK_2026[date(2026, 3, 16)] == 3

    def test_fourth_week(self):
        assert SWEEP_WEEK_2026[date(2026, 3, 23)] == 4

    def test_partial_week_is_non_posted(self):
        # Mar 30-31 is a partial 5th week — should NOT be in the lookup
        assert date(2026, 3, 30) not in SWEEP_WEEK_2026
        assert date(2026, 3, 31) not in SWEEP_WEEK_2026

    def test_month_starting_midweek_partial_is_non_posted(self):
        # July starts on Wed — Jul 1-2 are non-posted
        assert date(2026, 7, 1) not in SWEEP_WEEK_2026
        assert date(2026, 7, 2) not in SWEEP_WEEK_2026
        # Jul 6 (Monday) starts week 1
        assert SWEEP_WEEK_2026[date(2026, 7, 6)] == 1

    def test_weekends_not_in_lookup(self):
        # Saturday and Sunday should never be in the lookup
        assert date(2026, 3, 7) not in SWEEP_WEEK_2026  # Saturday
        assert date(2026, 3, 8) not in SWEEP_WEEK_2026  # Sunday

    def test_full_week_coverage(self):
        # Week of Jan 12-16 should all be week 2
        for d in range(12, 17):
            assert SWEEP_WEEK_2026[date(2026, 1, d)] == 2


class TestIsSweepToday:
    def test_sweep_day_matches(self):
        # 2026-03-09 is Monday, 2nd occurrence → "2 & 4" should match
        with _patch_today(date(2026, 3, 9)):
            assert is_sweep_today("Monday", "2 & 4") is True

    def test_wrong_day(self):
        # 2026-03-09 is Monday, not Tuesday
        with _patch_today(date(2026, 3, 9)):
            assert is_sweep_today("Tuesday", "2 & 4") is False

    def test_wrong_week(self):
        # 2026-03-09 is 2nd Monday → "1 & 3" should not match
        with _patch_today(date(2026, 3, 9)):
            assert is_sweep_today("Monday", "1 & 3") is False

    def test_holiday_skipped(self):
        # 2026-01-19 is MLK Day (holiday) and a Monday
        with _patch_today(date(2026, 1, 19)):
            assert is_sweep_today("Monday", "1 & 3") is False


class TestNextSweepDates:
    def test_returns_correct_count(self):
        with _patch_today(date(2026, 3, 1)):
            dates = next_sweep_dates("Monday", "2 & 4", count=4)
            assert len(dates) == 4

    def test_all_dates_are_correct_day(self):
        with _patch_today(date(2026, 3, 1)):
            dates = next_sweep_dates("Monday", "2 & 4", count=4)
            assert all(d.weekday() == 0 for d in dates)  # 0 = Monday

    def test_all_dates_in_correct_weeks(self):
        with _patch_today(date(2026, 3, 1)):
            dates = next_sweep_dates("Monday", "2 & 4", count=4)
            weeks = [SWEEP_WEEK_2026[d] for d in dates]
            assert all(w in (2, 4) for w in weeks)

    def test_skips_holidays(self):
        with _patch_today(date(2026, 1, 1)):
            dates = next_sweep_dates("Monday", "1 & 3", count=4)
            # MLK Day is 2026-01-19, should not appear
            assert date(2026, 1, 19) not in dates

    def test_invalid_day_returns_empty(self):
        with _patch_today(date(2026, 3, 1)):
            assert next_sweep_dates("Saturday", "1 & 3") == []


class TestFormatStreetSummary:
    def _make_details(self, **overrides):
        defaults = {
            "found": True,
            "street_name": "VENICE BLVD",
            "sweep_days": ["Monday"],
            "sweep_schedule": "2 & 4",
            "sweep_time": "8am-10am",
        }
        defaults.update(overrides)
        return defaults

    def test_street_label(self):
        result = format_street_summary(self._make_details())
        assert "VENICE BLVD" in result

    def test_consolidates_days(self):
        result = format_street_summary(
            self._make_details(sweep_days=["Monday", "Tuesday"])
        )
        assert "Monday & Tuesday" in result

    def test_single_day(self):
        result = format_street_summary(self._make_details(sweep_days=["Monday"]))
        assert "Monday" in result
        assert "Monday & Monday" not in result

    def test_shows_posted_time_directly(self):
        result = format_street_summary(self._make_details(sweep_time="8am-10am"))
        assert "8am-10am" in result

    def test_shows_schedule(self):
        result = format_street_summary(self._make_details(sweep_schedule="2 & 4"))
        assert "2 & 4" in result


# ---------------------------------------------------------------------------
# Subscription DB tests (use in-memory SQLite)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
async def mem_db(monkeypatch):
    """Use a temp file SQLite database for subscription tests."""
    import os
    import tempfile

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr(db_mod, "DB_PATH", tmp.name)
    await db_mod.init_db()
    yield tmp.name
    os.unlink(tmp.name)


@pytest.mark.asyncio
class TestSubscriptionDB:
    async def test_add_and_get(self, mem_db):
        err = await db_mod.add_subscription(
            chat_id=123,
            x=-118.25,
            y=34.05,
            label="Test Addr",
            sweep_days=["Monday"],
            sweep_schedule="1 & 3",
            sweep_time="8am-10am",
            street_name="MAIN ST",
        )
        assert err is None
        subs = await db_mod.get_user_subscriptions(123)
        assert len(subs) == 1
        assert subs[0]["label"] == "Test Addr"
        assert json.loads(subs[0]["sweep_days"]) == ["Monday"]

    async def test_upsert_same_location(self, mem_db):
        await db_mod.add_subscription(
            chat_id=123,
            x=-118.25,
            y=34.05,
            label="Old Label",
            sweep_days=["Monday"],
            sweep_schedule="1 & 3",
            sweep_time=None,
            street_name=None,
        )
        await db_mod.add_subscription(
            chat_id=123,
            x=-118.25,
            y=34.05,
            label="New Label",
            sweep_days=["Tuesday"],
            sweep_schedule="2 & 4",
            sweep_time="10am-12pm",
            street_name="VENICE BLVD",
        )
        subs = await db_mod.get_user_subscriptions(123)
        assert len(subs) == 1
        assert subs[0]["label"] == "New Label"

    async def test_subscription_cap(self, mem_db):
        for i in range(db_mod.MAX_SUBSCRIPTIONS_PER_USER):
            err = await db_mod.add_subscription(
                chat_id=123,
                x=-118.0 + i * 0.01,
                y=34.0,
                label=f"Addr {i}",
                sweep_days=["Monday"],
                sweep_schedule="1 & 3",
                sweep_time=None,
                street_name=None,
            )
            assert err is None

        err = await db_mod.add_subscription(
            chat_id=123,
            x=-117.0,
            y=34.0,
            label="One too many",
            sweep_days=["Monday"],
            sweep_schedule="1 & 3",
            sweep_time=None,
            street_name=None,
        )
        assert err is not None
        assert "5" in err

    async def test_remove_by_id(self, mem_db):
        await db_mod.add_subscription(
            chat_id=123,
            x=-118.25,
            y=34.05,
            label="Test",
            sweep_days=["Monday"],
            sweep_schedule="1 & 3",
            sweep_time=None,
            street_name=None,
        )
        subs = await db_mod.get_user_subscriptions(123)
        count = await db_mod.remove_subscription(123, subs[0]["id"])
        assert count == 1
        assert await db_mod.get_user_subscriptions(123) == []

    async def test_remove_all(self, mem_db):
        for i in range(3):
            await db_mod.add_subscription(
                chat_id=123,
                x=-118.0 + i * 0.01,
                y=34.0,
                label=f"Addr {i}",
                sweep_days=["Monday"],
                sweep_schedule="1 & 3",
                sweep_time=None,
                street_name=None,
            )
        count = await db_mod.remove_all_subscriptions(123)
        assert count == 3
        assert await db_mod.get_user_subscriptions(123) == []

    async def test_get_empty(self, mem_db):
        subs = await db_mod.get_user_subscriptions(999)
        assert subs == []

    async def test_get_all_subscriptions(self, mem_db):
        await db_mod.add_subscription(
            chat_id=100,
            x=-118.25,
            y=34.05,
            label="User 1",
            sweep_days=["Monday"],
            sweep_schedule="1 & 3",
            sweep_time=None,
            street_name=None,
        )
        await db_mod.add_subscription(
            chat_id=200,
            x=-118.30,
            y=34.10,
            label="User 2",
            sweep_days=["Friday"],
            sweep_schedule="2 & 4",
            sweep_time=None,
            street_name=None,
        )
        all_subs = await db_mod.get_all_subscriptions()
        assert len(all_subs) == 2


@pytest.mark.asyncio
class TestNotificationLogic:
    """Test the date-matching logic used by send_notifications."""

    async def test_1_day_before_triggers(self):
        # If today is Sunday Mar 8, sweep is Monday Mar 9 (2nd Monday, "2 & 4")
        with _patch_today(date(2026, 3, 8), hour=7):
            dates = next_sweep_dates("Monday", "2 & 4", count=2)
            tomorrow = date(2026, 3, 9)
            assert tomorrow in dates

    async def test_2_days_before_triggers(self):
        # If today is Saturday Mar 7, sweep is Monday Mar 9
        with _patch_today(date(2026, 3, 7), hour=7):
            dates = next_sweep_dates("Monday", "2 & 4", count=2)
            day_after = date(2026, 3, 9)
            assert day_after in dates

    async def test_3_days_before_no_trigger(self):
        # If today is Friday Mar 6, sweep is Monday Mar 9 — 3 days away, no notification
        with _patch_today(date(2026, 3, 6), hour=7):
            today = date(2026, 3, 6)
            tomorrow = today + timedelta(days=1)
            day_after = today + timedelta(days=2)
            dates = next_sweep_dates("Monday", "2 & 4", count=2)
            # The nearest sweep date (Mar 9) should not be tomorrow or day_after
            assert all(d != tomorrow and d != day_after for d in dates)

    async def test_holiday_not_triggered(self):
        # MLK Day 2026-01-19 is a Monday — should be skipped
        with _patch_today(date(2026, 1, 18), hour=7):  # day before
            dates = next_sweep_dates("Monday", "1 & 3", count=2)
            assert date(2026, 1, 19) not in dates


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


class TestNormalizeAddress:
    def test_appends_la_when_missing(self):
        assert normalize_address("123 Main St") == "123 Main St, Los Angeles, CA"

    def test_keeps_explicit_los_angeles(self):
        assert (
            normalize_address("123 Main St, Los Angeles") == "123 Main St, Los Angeles"
        )

    def test_keeps_explicit_la_abbreviation(self):
        assert normalize_address("123 Main St, LA") == "123 Main St, LA"

    def test_la_case_insensitive(self):
        assert normalize_address("123 Main St, la") == "123 Main St, la"

    def test_substring_la_in_place_still_appends(self):
        # "place" contains "la" but it's not the word "LA"
        result = normalize_address("123 Place St")
        assert result.endswith(", Los Angeles, CA")

    def test_substring_la_in_lake_still_appends(self):
        result = normalize_address("456 Lake Ave")
        assert result.endswith(", Los Angeles, CA")

    def test_substring_la_in_glendale_still_appends(self):
        result = normalize_address("789 Main St, Glendale")
        assert result.endswith(", Los Angeles, CA")

    def test_substring_la_in_atlantic_still_appends(self):
        result = normalize_address("100 Atlantic Blvd")
        assert result.endswith(", Los Angeles, CA")

    def test_la_as_word_keeps(self):
        # "LA 90012" — LA as standalone word
        assert not normalize_address("123 Main St, LA 90012").endswith(
            ", Los Angeles, CA"
        )
