"""
Web frontend for LA Street Sweeping lookup.
Runs alongside the Telegram bot, reusing all sweep logic.
"""

import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from la_sweep_bot import geocode_address, lookup_sweep_info

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


class CoordsRequest(BaseModel):
    lat: float
    lon: float


class AddressRequest(BaseModel):
    address: str


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.post("/api/lookup")
async def api_lookup(req: CoordsRequest):
    result = await lookup_sweep_info(x=req.lon, y=req.lat)
    return result


@app.post("/api/address")
async def api_address(req: AddressRequest):
    address = req.address.strip()
    if not address:
        return {"found": False, "text": "Please enter an address."}

    if "los angeles" not in address.lower() and "la" not in address.lower():
        address += ", Los Angeles, CA"

    geo = await geocode_address(address)
    if not geo or geo["score"] < 70:
        return {
            "found": False,
            "text": "Couldn't find that address. Try including the full street name and zip code.",
        }

    result = await lookup_sweep_info(x=geo["x"], y=geo["y"])
    result["address"] = geo["match_addr"]
    return result


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
