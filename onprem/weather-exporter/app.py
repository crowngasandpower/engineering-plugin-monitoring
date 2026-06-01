"""Weather exporter for Crown monitoring.

Polls Open-Meteo for current conditions at configured locations on each
Prometheus scrape and exposes them as gauge metrics.

Open-Meteo is free, needs no API key, and combines ECMWF/UK-Met forecasts.
At 60s scrape × 1 location that's 1,440 calls/day — well under their
10,000/day non-commercial limit.

Locations are configured via the WEATHER_LOCATIONS env var, a comma-separated
list of `name:lat:lon`. Default: Bury, Lancashire (Crown HQ).

Metrics exposed (all gauges, labelled `location="<name>"`):

  outside_temperature_celsius
  outside_apparent_temperature_celsius
  outside_humidity_percent
  outside_wind_speed_kmh
  outside_wind_direction_degrees
  outside_precipitation_mm
  outside_pressure_hpa
  outside_weather_code          (WMO weather code — see Open-Meteo docs)
  outside_scrape_success        (1 = last fetch ok, 0 = failed)
"""

import os
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Response

app = FastAPI(title="Crown Weather Exporter", version="0.1.0")

API_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_SECONDS = float(os.environ.get("WEATHER_TIMEOUT_SECONDS", "10"))


@dataclass
class Location:
    name: str
    latitude: float
    longitude: float


def parse_locations() -> list[Location]:
    raw = os.environ.get("WEATHER_LOCATIONS", "bury:53.5933:-2.2966")
    out: list[Location] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            continue
        name, lat, lon = parts
        try:
            out.append(Location(name=name.strip(), latitude=float(lat), longitude=float(lon)))
        except ValueError:
            continue
    return out


LOCATIONS = parse_locations()

# Field → (metric name, HELP text). Order defines output order.
METRIC_MAP: dict[str, tuple[str, str]] = {
    "temperature_2m":       ("outside_temperature_celsius",          "Current outside air temperature in degrees Celsius."),
    "apparent_temperature": ("outside_apparent_temperature_celsius", "Feels-like (apparent) outside temperature in degrees Celsius."),
    "relative_humidity_2m": ("outside_humidity_percent",             "Relative humidity at 2m in percent."),
    "wind_speed_10m":       ("outside_wind_speed_kmh",               "Wind speed at 10m in km/h."),
    "wind_direction_10m":   ("outside_wind_direction_degrees",       "Wind direction at 10m in meteorological degrees."),
    "precipitation":        ("outside_precipitation_mm",             "Precipitation over the last hour in mm."),
    "pressure_msl":         ("outside_pressure_hpa",                 "Mean sea-level pressure in hPa."),
    "weather_code":         ("outside_weather_code",                 "Open-Meteo WMO weather code (0=clear, 3=overcast, 61=light rain, etc.)."),
}

CURRENT_FIELDS = ",".join(METRIC_MAP.keys())


async def fetch_location(client: httpx.AsyncClient, loc: Location) -> dict | None:
    params = {
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "current": CURRENT_FIELDS,
        "timezone": "UTC",
    }
    try:
        resp = await client.get(API_URL, params=params, timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json().get("current", {})
    except (httpx.HTTPError, ValueError):
        return None


def format_block(metric_name: str, help_text: str, samples: list[tuple[str, float]]) -> str:
    lines = [f"# HELP {metric_name} {help_text}", f"# TYPE {metric_name} gauge"]
    for labels, value in samples:
        lines.append(f"{metric_name}{{{labels}}} {value}")
    return "\n".join(lines)


@app.get("/metrics")
async def metrics() -> Response:
    per_metric: dict[str, list[tuple[str, float]]] = {name: [] for name, _ in METRIC_MAP.values()}
    scrape_ok: list[tuple[str, float]] = []

    async with httpx.AsyncClient() as client:
        for loc in LOCATIONS:
            data = await fetch_location(client, loc)
            labels = f'location="{loc.name}"'
            scrape_ok.append((labels, 1.0 if data else 0.0))
            if not data:
                continue
            for field, (metric_name, _) in METRIC_MAP.items():
                value = data.get(field)
                if value is None:
                    continue
                per_metric[metric_name].append((labels, float(value)))

    chunks = [
        format_block(metric_name, help_text, per_metric[metric_name])
        for field, (metric_name, help_text) in METRIC_MAP.items()
    ]
    chunks.append(format_block(
        "outside_scrape_success",
        "1 if the last Open-Meteo fetch succeeded for this location, 0 otherwise.",
        scrape_ok,
    ))
    return Response(content="\n".join(chunks) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "locations": [loc.name for loc in LOCATIONS]}
