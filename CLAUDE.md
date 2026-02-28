# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                        # Install/sync dependencies
uv run python la_sweep_bot.py  # Run the bot (needs TELEGRAM_BOT_TOKEN in .env)
uv run ruff check .            # Lint
uv run ruff format .           # Format
uv run ty check .              # Type check
```

## Architecture

Single-file Telegram bot (`la_sweep_bot.py`) that looks up LA street sweeping schedules. Stateless — no database.

**Flow:** User sends address or GPS location → geocode via ArcGIS (skipped for GPS) → spatial envelope query against LA's Clean_Street_Routes FeatureServer → format schedule with next sweep dates → reply.

**Key sections in `la_sweep_bot.py`:**
- **Config**: Bot token (from `.env`), ArcGIS URLs, 2026 LA holidays
- **ArcGIS helpers**: `geocode_address()` (text → coords), `query_sweep_routes()` (coords → route attributes)
- **Schedule logic**: `next_sweep_dates()` and `is_sweep_today()` use week-of-month occurrence (1st/3rd or 2nd/4th) with holiday skipping
- **Telegram handlers**: `/sweep <address>`, free text, GPS location sharing — all funnel into `_lookup_coords()`

## Important Context

- The FeatureServer rejects `units=esriFeet` for point-buffer queries, so `query_sweep_routes()` uses an envelope (bounding box) workaround instead.
- Actual FeatureServer field names: `Route`, `Posted_Day`, `Posted_Time`, `Weeks`, `Boundaries`, `Day_Short`, `STNAME`, `TDIR`, `STSFX`. These differ from what ArcGIS docs might suggest.
- Holiday list is hardcoded per-year (currently 2026 only). User has confirmed dates are correct.
- All APIs are free: ArcGIS World Geocoder (no key, non-stored results) and LA City FeatureServer (open data).
- Redis caching is planned but not yet implemented.
