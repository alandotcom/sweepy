"""
LA Street Sweeping Telegram Bot
================================
Tells you when street sweeping happens at your LA address.

Architecture:
  1. User sends /sweep <address> or just texts an address
  2. Bot geocodes via ArcGIS World Geocoder (free, no key needed for <50 req/day)
  3. Spatial query against LA's Clean_Street_Routes FeatureServer
  4. Returns sweep day, time window, week schedule, and next sweep date

Setup:
  pip install python-telegram-bot httpx

  1. Message @BotFather on Telegram → /newbot → get your token
  2. Set TELEGRAM_BOT_TOKEN env var (or paste below)
  3. python la_sweep_bot.py
"""

import os
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from cachetools import TTLCache
import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
LA_TZ = ZoneInfo("America/Los_Angeles")

# LA's public ArcGIS feature services (no auth needed)
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
        "maxLocations": 1,
        # Bias toward LA
        "location": "-118.25,34.05",
        "distance": 50000,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(GEOCODE_URL, params=params)
        data = resp.json()

    candidates = data.get("candidates", [])
    if not candidates:
        _geocode_cache[cache_key] = None
        return None

    best = candidates[0]
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


def get_week_occurrence(d: date) -> int:
    """Return which occurrence of this weekday in the month (1-5)."""
    return (d.day - 1) // 7 + 1


def next_sweep_dates(sweep_day_name: str, schedule: str, count: int = 3) -> list[date]:
    """
    Given a weekday name and schedule like '1st & 3rd' or '2nd & 4th',
    return the next `count` sweep dates (skipping holidays).
    """
    target_dow = DAY_NUM.get(sweep_day_name)
    if target_dow is None:
        return []

    if "1" in schedule and "3" in schedule:
        valid_weeks = {1, 3}
    elif "2" in schedule and "4" in schedule:
        valid_weeks = {2, 4}
    else:
        valid_weeks = {1, 2, 3, 4}  # fallback

    today = datetime.now(LA_TZ).date()
    results = []
    d = today
    # Scan up to 120 days out
    for _ in range(120):
        if d.weekday() == target_dow:
            occ = get_week_occurrence(d)
            if occ in valid_weeks and d not in HOLIDAYS_2026:
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
    occ = get_week_occurrence(today)
    if "1" in schedule and "3" in schedule:
        return occ in (1, 3)
    elif "2" in schedule and "4" in schedule:
        return occ in (2, 4)
    return False


# ---------------------------------------------------------------------------
# Format response
# ---------------------------------------------------------------------------


def format_route_info(attrs: dict) -> str:
    """Format a single route's attributes into a readable message."""
    # Actual field names from Clean_Street_Routes FeatureServer:
    #   Route, Posted_Day, Posted_Time, Boundaries, Weeks, Day_Short,
    #   STNAME, TDIR, STSFX

    route = attrs.get("Route") or "Unknown"
    day_name = attrs.get("Posted_Day") or ""
    posted_time = attrs.get("Posted_Time") or ""
    schedule = attrs.get("Weeks") or ""
    boundaries = attrs.get("Boundaries") or ""
    street = attrs.get("STNAME") or ""
    direction = attrs.get("TDIR") or ""
    suffix = attrs.get("STSFX") or ""

    street_label = " ".join(filter(None, [direction, street, suffix]))

    lines = [f"🧹 *Route {route}*"]
    if street_label:
        lines.append(f"🛣 Street: {street_label}")
    if day_name:
        lines.append(f"📅 Day: {day_name}")
    if schedule:
        lines.append(f"🔄 Weeks: {schedule}")
    if posted_time:
        lines.append(f"🕐 Time: {posted_time}")
    if boundaries:
        lines.append(f"📍 Area: {boundaries}")

    # Sweep status
    if day_name and schedule:
        if is_sweep_today(day_name, schedule):
            lines.append("\n⚠️ *SWEEPING TODAY — MOVE YOUR CAR!*")
        upcoming = next_sweep_dates(day_name, schedule, count=3)
        if upcoming:
            dates_str = ", ".join(d.strftime("%a %b %-d") for d in upcoming)
            lines.append(f"\n📆 Next sweeps: {dates_str}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "🧹 *LA Street Sweeping Bot*\n\n"
    "Send me an address and I'll tell you the street sweeping schedule.\n\n"
    "Examples:\n"
    "• `/sweep 1234 Main St, Los Angeles`\n"
    "• `/sweep 456 N Fairfax Ave 90036`\n"
    "• Just type an address\n"
    "• Or share your 📍 location!\n\n"
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
    if "los angeles" not in address.lower() and "la" not in address.lower():
        address += ", Los Angeles, CA"

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
    """Core logic: spatial query by coordinates → respond."""
    if not update.message:
        return

    routes = await query_sweep_routes(x, y, radius_ft=200)
    if not routes:
        routes = await query_sweep_routes(x, y, radius_ft=500)

    if not routes:
        await update.message.reply_text(
            f"📍 *{label}*\n\n"
            "No posted sweep routes found nearby. "
            "This street may not have posted sweeping, or it might be "
            "outside the City of LA.\n\n"
            "[Check the map](https://labss.maps.arcgis.com/apps/dashboards/ad01106434a443a69924c54f1e8edbf7)",
            parse_mode="Markdown",
        )
        return

    header = f"📍 *{label}*\n"

    seen = set()
    unique_routes = []
    for r in routes:
        key = (r.get("Route", ""), r.get("Posted_Day", ""))
        if key not in seen:
            seen.add(key)
            unique_routes.append(r)

    route_msgs = [format_route_info(r) for r in unique_routes[:4]]
    body = "\n\n---\n\n".join(route_msgs)

    footer = (
        "\n\n[View on LA Map]"
        "(https://labss.maps.arcgis.com/apps/dashboards/ad01106434a443a69924c54f1e8edbf7)"
    )

    await update.message.reply_text(
        header + body + footer,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


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

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("sweep", handle_sweep))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
