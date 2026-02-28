"""
LA Street Sweeping Telegram Bot
================================
Tells you when street sweeping happens at your LA address.

Architecture:
  1. User sends /sweep <address> or just texts an address
  2. Bot geocodes via ArcGIS World Geocoder (free tier: 20k/month with API key)
  3. Spatial query against LA's Clean_Street_Routes FeatureServer
  4. Returns sweep day, time window, week schedule, and next sweep date

Setup:
  pip install python-telegram-bot httpx

  1. Message @BotFather on Telegram → /newbot → get your token
  2. Set TELEGRAM_BOT_TOKEN env var (or paste below)
  3. python la_sweep_bot.py
"""

import json
import os
import logging
import re
from collections import Counter
from datetime import datetime, date, timedelta, time as dt_time
from zoneinfo import ZoneInfo

from cachetools import TTLCache
import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from db import (
    init_db,
    add_subscription,
    remove_subscription,
    remove_all_subscriptions,
    get_user_subscriptions,
    get_all_subscriptions,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ARCGIS_API_KEY = os.environ.get("ARCGIS_API_KEY", "")
LA_TZ = ZoneInfo("America/Los_Angeles")

# ArcGIS geocoder (authenticated with API key for 20k free geocodes/month)
GEOCODE_URL = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
# Layer 0 = Centerlines_Centroid_Routes_v2 (centerlines with sweep schedule joined)
ROUTES_URL = "https://services5.arcgis.com/7nsPwEMP38bSkCjy/arcgis/rest/services/Clean_Street_Routes/FeatureServer/0/query"

# 2026 LA city holidays (no sweep enforcement)
HOLIDAYS_2026 = {
    date(2026, 1, 1),  # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents' Day
    date(2026, 3, 31),  # Cesar Chavez Day
    date(2026, 5, 25),  # Memorial Day
    date(2026, 7, 3),  # Independence Day (observed)
    date(2026, 9, 7),  # Labor Day
    date(2026, 11, 11),  # Veterans Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 11, 27),  # Day after Thanksgiving
    date(2026, 12, 25),  # Christmas
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# In-memory TTL caches — keeps API quota usage low for repeated lookups
_geocode_cache: TTLCache[str, dict | None] = TTLCache(
    maxsize=1024, ttl=604_800
)  # 7 days
_routes_cache: TTLCache[tuple, list[dict]] = TTLCache(
    maxsize=2048, ttl=86_400
)  # 24 hours

# ---------------------------------------------------------------------------
# ArcGIS helpers
# ---------------------------------------------------------------------------


def normalize_address(address: str) -> str:
    """Append ', Los Angeles, CA' if the address doesn't mention LA."""
    if not re.search(r"\blos angeles\b|\bla\b", address, re.IGNORECASE):
        return address + ", Los Angeles, CA"
    return address


async def geocode_address(address: str) -> dict | None:
    """Geocode an address using ArcGIS World Geocoder. Returns {x, y, match_addr}."""
    cache_key = " ".join(address.lower().split())
    if cache_key in _geocode_cache:
        logger.info(f"Geocode cache hit: '{cache_key}'")
        return _geocode_cache[cache_key]

    params = {
        "f": "json",
        "singleLine": address,
        "outFields": "Match_addr,Addr_type",
        "maxLocations": 5,
        # Bias toward LA
        "location": "-118.25,34.05",
        "distance": 50000,
    }
    if ARCGIS_API_KEY:
        params["token"] = ARCGIS_API_KEY
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(GEOCODE_URL, params=params)
        data = resp.json()

    candidates = data.get("candidates", [])
    if not candidates:
        _geocode_cache[cache_key] = None
        return None

    best = max(candidates, key=lambda c: c.get("score", 0))
    loc = best["location"]
    result = {
        "x": loc["x"],
        "y": loc["y"],
        "match_addr": best["attributes"].get("Match_addr", address),
        "score": best.get("score", 0),
    }
    _geocode_cache[cache_key] = result
    return result


async def query_sweep_routes(x: float, y: float, radius_ft: int = 200) -> list[dict]:
    """
    Spatial query: find sweep routes within `radius_ft` feet of a point.
    Returns list of route attribute dicts.

    Uses an envelope (bounding box) because this FeatureServer rejects
    the `units` param needed for point-buffer queries.
    ~0.000003 degrees/ft at LA's latitude.
    """
    cache_key = (round(x, 4), round(y, 4), radius_ft)
    if cache_key in _routes_cache:
        logger.info(f"Routes cache hit: {cache_key}")
        return _routes_cache[cache_key]

    deg_offset = radius_ft * 0.000003
    envelope = f"{x - deg_offset},{y - deg_offset},{x + deg_offset},{y + deg_offset}"

    params = {
        "f": "json",
        "geometry": envelope,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "Route,Posted_Day,Posted_Time,Boundaries,Weeks,Day_Short,STNAME,TDIR,STSFX",
        "returnGeometry": "false",
        "resultRecordCount": 10,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(ROUTES_URL, params=params)
        data = resp.json()

    if "error" in data:
        logger.error(f"ArcGIS query error: {data['error']}")
        return []

    features = data.get("features", [])
    result = [f["attributes"] for f in features]
    _routes_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Schedule logic
# ---------------------------------------------------------------------------

DAY_NUM = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4}

# Official 2026 sweep week calendar — from LA StreetsLA PDF
# https://streets.lacity.gov/sites/default/files/2025-12/Sweeping2026.pdf
# Each month has exactly 4 posted sweep weeks starting on the first full
# Monday-Friday row. Partial weeks at month edges are non-posted.
# Week 1 & 3 match "1st & 3rd" schedule, week 2 & 4 match "2nd & 4th".
_SWEEP_MONDAYS_2026 = [
    # January
    (date(2026, 1, 5), 1),
    (date(2026, 1, 12), 2),
    (date(2026, 1, 19), 3),
    (date(2026, 1, 26), 4),
    # February
    (date(2026, 2, 2), 1),
    (date(2026, 2, 9), 2),
    (date(2026, 2, 16), 3),
    (date(2026, 2, 23), 4),
    # March
    (date(2026, 3, 2), 1),
    (date(2026, 3, 9), 2),
    (date(2026, 3, 16), 3),
    (date(2026, 3, 23), 4),
    # April
    (date(2026, 4, 6), 1),
    (date(2026, 4, 13), 2),
    (date(2026, 4, 20), 3),
    (date(2026, 4, 27), 4),
    # May
    (date(2026, 5, 4), 1),
    (date(2026, 5, 11), 2),
    (date(2026, 5, 18), 3),
    (date(2026, 5, 25), 4),
    # June
    (date(2026, 6, 1), 1),
    (date(2026, 6, 8), 2),
    (date(2026, 6, 15), 3),
    (date(2026, 6, 22), 4),
    # July
    (date(2026, 7, 6), 1),
    (date(2026, 7, 13), 2),
    (date(2026, 7, 20), 3),
    (date(2026, 7, 27), 4),
    # August
    (date(2026, 8, 3), 1),
    (date(2026, 8, 10), 2),
    (date(2026, 8, 17), 3),
    (date(2026, 8, 24), 4),
    # September
    (date(2026, 9, 7), 1),
    (date(2026, 9, 14), 2),
    (date(2026, 9, 21), 3),
    (date(2026, 9, 28), 4),
    # October
    (date(2026, 10, 5), 1),
    (date(2026, 10, 12), 2),
    (date(2026, 10, 19), 3),
    (date(2026, 10, 26), 4),
    # November
    (date(2026, 11, 2), 1),
    (date(2026, 11, 9), 2),
    (date(2026, 11, 16), 3),
    (date(2026, 11, 23), 4),
    # December
    (date(2026, 12, 7), 1),
    (date(2026, 12, 14), 2),
    (date(2026, 12, 21), 3),
    (date(2026, 12, 28), 4),
]

# Build lookup: date → sweep week number (1-4)
# Dates absent from this dict are non-posted (no sweeping on posted routes)
SWEEP_WEEK_2026: dict[date, int] = {}
for _monday, _week in _SWEEP_MONDAYS_2026:
    for _offset in range(5):  # Mon through Fri
        SWEEP_WEEK_2026[_monday + timedelta(days=_offset)] = _week


def _valid_weeks(schedule: str) -> set[int]:
    """Parse a schedule string like '1 & 3' or '2 & 4' into valid week numbers."""
    if "1" in schedule and "3" in schedule:
        return {1, 3}
    if "2" in schedule and "4" in schedule:
        return {2, 4}
    return {1, 2, 3, 4}


def next_sweep_dates(sweep_day_name: str, schedule: str, count: int = 3) -> list[date]:
    """
    Given a weekday name and schedule like '1st & 3rd' or '2nd & 4th',
    return the next `count` sweep dates using the official 2026 calendar.
    """
    target_dow = DAY_NUM.get(sweep_day_name)
    if target_dow is None:
        return []

    valid = _valid_weeks(schedule)

    today = datetime.now(LA_TZ).date()
    results = []
    d = today
    for _ in range(120):
        if d.weekday() == target_dow:
            week = SWEEP_WEEK_2026.get(d)
            if week in valid and d not in HOLIDAYS_2026:
                if d >= today:
                    results.append(d)
                    if len(results) >= count:
                        break
        d += timedelta(days=1)
    return results


def is_sweep_today(sweep_day_name: str, schedule: str) -> bool:
    """Check if sweeping is happening today."""
    today = datetime.now(LA_TZ).date()
    if today in HOLIDAYS_2026:
        return False
    if today.strftime("%A") != sweep_day_name:
        return False
    week = SWEEP_WEEK_2026.get(today)
    if week is None:
        return False
    return week in _valid_weeks(schedule)


# ---------------------------------------------------------------------------
# Format response
# ---------------------------------------------------------------------------


def format_street_summary(details: dict) -> str:
    """Format structured sweep details into a display card."""
    street_label = details["street_name"]
    days = details["sweep_days"]
    schedule = details["sweep_schedule"]
    times = details["sweep_time"]

    lines = [f"🧹 *{street_label}*"]
    if days:
        lines.append(f"📅 {' & '.join(days)}")
    if schedule:
        lines.append(f"🔄 {schedule}")
    if times:
        lines.append(f"🕐 {times}")

    # Sweep status — check each day
    if days and schedule:
        sweep_today = any(is_sweep_today(d, schedule) for d in days)
        if sweep_today:
            lines.append("\n⚠️ *SWEEPING TODAY — MOVE YOUR CAR!*")
        all_dates: list[date] = []
        for d in days:
            all_dates.extend(next_sweep_dates(d, schedule, count=3))
        all_dates.sort()
        if all_dates:
            dates_str = ", ".join(d.strftime("%a %b %-d") for d in all_dates[:4])
            lines.append(f"\n📆 Next: {dates_str}")

    return "\n".join(lines)


async def get_sweep_details(x: float, y: float) -> dict:
    """Coords → filtered routes → structured details dict."""
    raw_routes = await query_sweep_routes(x, y, radius_ft=200)
    if not raw_routes:
        raw_routes = await query_sweep_routes(x, y, radius_ft=500)

    routes = [r for r in raw_routes if r.get("Posted_Day")]

    if routes:
        street_counts = Counter(r.get("STNAME", "") for r in routes)
        primary_street = street_counts.most_common(1)[0][0]
        routes = [r for r in routes if r.get("STNAME") == primary_street]

    if not routes:
        return {"found": False}

    first = routes[0]
    days: list[str] = list(
        dict.fromkeys(d for r in routes if isinstance(d := r.get("Posted_Day"), str))
    )
    times: list[str] = list(
        dict.fromkeys(t for r in routes if isinstance(t := r.get("Posted_Time"), str))
    )
    street = " ".join(
        filter(None, [first.get("STNAME", ""), first.get("STSFX", "")])
    ).upper()
    schedule = first.get("Weeks", "")

    return {
        "found": True,
        "routes": routes,
        "sweep_days": days,
        "sweep_schedule": schedule,
        "sweep_time": ", ".join(times) if times else None,
        "street_name": street,
    }


async def lookup_sweep_info(x: float, y: float) -> dict:
    """Coords → filtered routes → formatted summary. Telegram-agnostic."""
    details = await get_sweep_details(x, y)
    if not details["found"]:
        return {
            "found": False,
            "text": (
                "No posted sweep routes found nearby. "
                "This street may not have posted sweeping, or it might be "
                "outside the City of LA."
            ),
        }
    return {"found": True, "text": format_street_summary(details)}


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "🧹 *LA Street Sweeping Bot*\n\n"
    "Send me an address and I'll tell you the street sweeping schedule.\n\n"
    "*Look up:*\n"
    "• `/sweep 1234 Main St, Los Angeles`\n"
    "• Just type an address\n"
    "• Or share your 📍 location!\n\n"
    "*Notifications:*\n"
    "• `/subscribe 1234 Main St` — get alerts before sweeping\n"
    "• `/mysubs` — see your subscriptions\n"
    "• `/unsubscribe` — remove alerts\n\n"
    "Data from City of LA StreetsLA via ArcGIS."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def handle_sweep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sweep <address> command."""
    if not update.message:
        return
    if context.args:
        address = " ".join(context.args)
    else:
        await update.message.reply_text(
            "Please provide an address.\nExample: `/sweep 1234 Main St, Los Angeles`",
            parse_mode="Markdown",
        )
        return
    await _lookup_address(update, address)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle free-text address input."""
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    if not text:
        return
    # Simple heuristic: if it looks like an address (has a number), process it
    if any(c.isdigit() for c in text):
        await _lookup_address(update, text)
    else:
        await update.message.reply_text(
            "Send me a street address to look up sweeping.\n"
            "Example: `1234 Main St, Los Angeles`",
            parse_mode="Markdown",
        )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle shared GPS location — skip geocoding, query routes directly."""
    if not update.message or not update.message.location:
        return
    loc = update.message.location
    await _lookup_coords(
        update,
        x=loc.longitude,
        y=loc.latitude,
        label=f"{loc.latitude:.5f}, {loc.longitude:.5f}",
    )


async def _lookup_address(update: Update, address: str) -> None:
    """Geocode a text address, then look up routes."""
    if not update.message:
        return
    address = normalize_address(address)

    await update.message.reply_text("🔍 Looking up your address...")

    geo = await geocode_address(address)
    if not geo or geo["score"] < 70:
        await update.message.reply_text(
            "❌ Couldn't find that address. Try including the full street name and zip code."
        )
        return

    logger.info(f"Geocoded '{address}' → ({geo['x']}, {geo['y']}) score={geo['score']}")
    await _lookup_coords(update, x=geo["x"], y=geo["y"], label=geo["match_addr"])


async def _lookup_coords(update: Update, x: float, y: float, label: str) -> None:
    """Spatial query by coordinates → Telegram reply."""
    if not update.message:
        return

    result = await lookup_sweep_info(x, y)

    map_link = (
        "[Check the map](https://labss.maps.arcgis.com/apps/dashboards/"
        "ad01106434a443a69924c54f1e8edbf7)"
    )

    if not result["found"]:
        await update.message.reply_text(
            f"📍 *{label}*\n\n{result['text']}\n\n{map_link}",
            parse_mode="Markdown",
        )
        return

    header = f"📍 *{label}*\n"
    footer = (
        "\n\n[View on LA Map]"
        "(https://labss.maps.arcgis.com/apps/dashboards/ad01106434a443a69924c54f1e8edbf7)"
    )

    await update.message.reply_text(
        header + result["text"] + footer,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Subscription handlers
# ---------------------------------------------------------------------------


async def handle_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /subscribe <address> command."""
    if not update.message:
        return
    if not context.args:
        await update.message.reply_text(
            "Please provide an address.\n"
            "Example: `/subscribe 1234 Main St, Los Angeles`",
            parse_mode="Markdown",
        )
        return

    address = normalize_address(" ".join(context.args))

    await update.message.reply_text("🔍 Looking up your address...")

    geo = await geocode_address(address)
    if not geo or geo["score"] < 70:
        await update.message.reply_text(
            "❌ Couldn't find that address. Try including the full street name and zip code."
        )
        return

    details = await get_sweep_details(x=geo["x"], y=geo["y"])
    if not details["found"]:
        await update.message.reply_text(
            "No posted sweep routes found at that address. Can't subscribe."
        )
        return

    err = await add_subscription(
        chat_id=update.message.chat_id,
        x=geo["x"],
        y=geo["y"],
        label=geo["match_addr"],
        sweep_days=details["sweep_days"],
        sweep_schedule=details["sweep_schedule"],
        sweep_time=details["sweep_time"],
        street_name=details["street_name"],
    )
    if err:
        await update.message.reply_text(err)
        return

    days_str = " & ".join(details["sweep_days"])
    await update.message.reply_text(
        f"✅ Subscribed to sweep alerts!\n\n"
        f"📍 {geo['match_addr']}\n"
        f"🧹 {details['street_name']}\n"
        f"📅 {days_str} ({details['sweep_schedule']})\n"
        f"🕐 {details['sweep_time'] or 'Check posted signs'}\n\n"
        f"You'll get notifications 2 days and 1 day before each sweep.\n"
        f"Use /mysubs to see your subscriptions.",
    )


async def handle_mysubs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mysubs command — list active subscriptions."""
    if not update.message:
        return

    subs = await get_user_subscriptions(update.message.chat_id)
    if not subs:
        await update.message.reply_text(
            "You don't have any subscriptions yet.\n"
            "Use `/subscribe <address>` to get sweep alerts.",
            parse_mode="Markdown",
        )
        return

    lines = ["📋 Your Subscriptions\n"]
    for i, sub in enumerate(subs, 1):
        sweep_days = json.loads(sub["sweep_days"])
        days_str = " & ".join(sweep_days)

        # Compute next sweep date
        all_dates: list[date] = []
        for day_name in sweep_days:
            all_dates.extend(next_sweep_dates(day_name, sub["sweep_schedule"], count=1))
        all_dates.sort()
        next_date = all_dates[0].strftime("%a %b %-d") if all_dates else "—"

        lines.append(
            f"{i}. 📍 {sub['label']}\n"
            f"   🧹 {sub['street_name'] or '—'} — {days_str} ({sub['sweep_schedule']})\n"
            f"   📆 Next: {next_date}"
        )

    lines.append("\nTo unsubscribe: /unsubscribe <number> or /unsubscribe all")
    await update.message.reply_text("\n".join(lines))


async def handle_unsubscribe(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /unsubscribe [number|all] command."""
    if not update.message:
        return
    chat_id = update.message.chat_id

    arg = " ".join(context.args).strip().lower() if context.args else ""

    if arg == "all":
        count = await remove_all_subscriptions(chat_id)
        if count:
            await update.message.reply_text(f"✅ Removed all {count} subscription(s).")
        else:
            await update.message.reply_text("You don't have any subscriptions.")
        return

    if arg.isdigit():
        pos = int(arg)
        subs = await get_user_subscriptions(chat_id)
        if not subs or pos < 1 or pos > len(subs):
            await update.message.reply_text(
                "Invalid number. Use `/mysubs` to see your subscriptions.",
                parse_mode="Markdown",
            )
            return
        sub = subs[pos - 1]
        await remove_subscription(chat_id, sub["id"])
        await update.message.reply_text(
            f"✅ Unsubscribed from sweep alerts for {sub['label']}."
        )
        return

    # No argument or invalid — show usage
    await update.message.reply_text(
        "Usage:\n"
        "• `/unsubscribe 1` — remove subscription #1\n"
        "• `/unsubscribe all` — remove all\n\n"
        "Use `/mysubs` to see your subscriptions.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Notification job
# ---------------------------------------------------------------------------


async def send_notifications(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily job: send 2-day and 1-day sweep warnings to subscribers."""
    today = datetime.now(LA_TZ).date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    all_subs = await get_all_subscriptions()
    logger.info(f"Notification check: {len(all_subs)} subscription(s)")

    blocked_chats: set[int] = set()
    for sub in all_subs:
        if sub["chat_id"] in blocked_chats:
            continue
        sweep_days = json.loads(sub["sweep_days"])
        schedule = sub["sweep_schedule"]

        # Collect upcoming dates across all sweep days for this subscription
        upcoming: list[date] = []
        for day_name in sweep_days:
            upcoming.extend(next_sweep_dates(day_name, schedule, count=2))

        # Check for 1-day or 2-day warning (1-day takes priority)
        msg = None
        for sweep_date in sorted(upcoming):
            if sweep_date == tomorrow:
                msg = (
                    f"⚠️ Sweep TOMORROW!\n"
                    f"📍 {sub['label']}\n"
                    f"🧹 {sub['street_name'] or 'Your street'}\n"
                    f"📅 {sweep_date.strftime('%A %b %-d')}\n"
                    f"🕐 {sub['sweep_time'] or 'Check posted signs'}\n\n"
                    f"Move your car tonight!"
                )
                break
            elif sweep_date == day_after:
                msg = (
                    f"📋 Sweep in 2 days\n"
                    f"📍 {sub['label']}\n"
                    f"🧹 {sub['street_name'] or 'Your street'}\n"
                    f"📅 {sweep_date.strftime('%A %b %-d')}\n"
                    f"🕐 {sub['sweep_time'] or 'Check posted signs'}"
                )
                # Don't break — a closer (tomorrow) date may appear next

        if msg:
            try:
                await context.bot.send_message(chat_id=sub["chat_id"], text=msg)
            except Forbidden:
                logger.info(
                    f"User {sub['chat_id']} blocked bot, removing subscriptions"
                )
                blocked_chats.add(sub["chat_id"])
                await remove_all_subscriptions(sub["chat_id"])
            except Exception:
                logger.exception(f"Failed to send notification to {sub['chat_id']}")


async def post_init(application: Application) -> None:
    """Called after Application.initialize() — set up DB and daily job."""
    await init_db()
    application.job_queue.run_daily(  # type: ignore[union-attr]
        send_notifications,
        time=dt_time(hour=7, minute=0, tzinfo=LA_TZ),
        name="daily_sweep_notifications",
    )
    logger.info("Scheduled daily sweep notifications at 7:00 AM LA time")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("=" * 60)
        print("Set your bot token!")
        print("  export TELEGRAM_BOT_TOKEN='your-token-from-botfather'")
        print("=" * 60)
        return

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("sweep", handle_sweep))
    app.add_handler(CommandHandler("subscribe", handle_subscribe))
    app.add_handler(CommandHandler("mysubs", handle_mysubs))
    app.add_handler(CommandHandler("unsubscribe", handle_unsubscribe))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
