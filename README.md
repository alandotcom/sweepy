# LA Street Sweeping Telegram Bot

A Telegram bot that tells you when street sweeping happens at your LA address.

## How it works

```
You: /sweep 230 Bernard Ave, Venice
Bot: 📍 230 Bernard Ave, Venice, California, 90291
     🧹 Route 12P125 Tu
     🛣 Street: S BERNARD AVE
     📅 Day: Tuesday
     🔄 Weeks: 2 & 4
     🕐 Time: 8 am - 10 am
     📍 Area: Santa Monica C/B to Indiana Ave-Lincoln Blvd to Main St
     📆 Next sweeps: Tue Mar 10, Tue Mar 24, Tue Apr 14
```

You can also share your GPS location directly — no address needed.

## Architecture

```
User → Telegram Bot API → Python bot
  ↓
  Text address?  →  ArcGIS World Geocoder (address → lat/lng)
  GPS location?  →  Use coordinates directly
  ↓
  LA Clean_Street_Routes FeatureServer (spatial envelope query)
  ↓
  Schedule logic (1st/3rd vs 2nd/4th week calc + holidays)
  ↓
  Formatted response → Telegram
```

**Data source**: City of LA StreetsLA — same data powering the
[official ArcGIS dashboard](https://labss.maps.arcgis.com/apps/dashboards/ad01106434a443a69924c54f1e8edbf7)

Feature Service: `https://services5.arcgis.com/7nsPwEMP38bSkCjy/arcgis/rest/services/Clean_Street_Routes/FeatureServer`

## Setup

### 1. Create your Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the token it gives you

### 2. Install and run

```bash
uv sync
echo "TELEGRAM_BOT_TOKEN=your-token-here" > .env
uv run python la_sweep_bot.py
```

## Commands

| Input | Description |
|---------|-------------|
| `/start`, `/help` | Welcome message and usage |
| `/sweep <address>` | Look up sweeping for an address |
| *any text with numbers* | Auto-detected as address lookup |
| *shared location* 📍 | Look up sweeping at your GPS coordinates |

## FeatureServer fields

The bot queries Layer 0 (`Centerlines_Centroid_Routes_v2`). Relevant fields:

| Field | Example |
|-------|---------|
| `Route` | `12P125 Tu` |
| `Posted_Day` | `Tuesday` |
| `Posted_Time` | `8 am - 10 am` |
| `Weeks` | `2 & 4` |
| `Boundaries` | `Santa Monica C/B to Indiana Ave-Lincoln Blvd to Main St` |
| `STNAME`, `TDIR`, `STSFX` | `BERNARD`, `S`, `AVE` |

Inspect the full schema at:
```
https://services5.arcgis.com/7nsPwEMP38bSkCjy/arcgis/rest/services/Clean_Street_Routes/FeatureServer/0?f=json
```

## Customization ideas

- **Daily alerts**: Add a scheduler (APScheduler) to notify saved users each morning
- **Redis cache**: Cache geocode results and route lookups for repeat addresses
- **Deploy**: Run on Railway, Fly.io, or a $5 VPS with systemd

## Notes

- All APIs are free — ArcGIS geocoder (no key needed) and LA FeatureServer (public open data)
- Holiday list is hardcoded for 2026 — update annually
- $73 ticket vs. free bot. The bot wins.
