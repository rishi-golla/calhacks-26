"""Facility coordinates and lane mileage. Addresses come from public records (FMCSA, SEC); coordinates from the
US Census geocoder (public, no key), falling back to the state centroid when an address doesn't match."""
import math

import httpx

CENSUS = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
ROAD_FACTOR = 1.18  # great-circle -> highway miles, typical for US truckload lanes
STATE_CENTROIDS = {
    "AL": (32.8, -86.8), "AK": (64.7, -152.0), "AZ": (34.3, -111.7), "AR": (34.9, -92.4), "CA": (37.2, -119.5),
    "CO": (39.0, -105.5), "CT": (41.6, -72.7), "DE": (39.0, -75.5), "FL": (28.6, -82.4), "GA": (32.7, -83.4),
    "HI": (20.8, -156.3), "ID": (44.4, -114.6), "IL": (40.0, -89.2), "IN": (39.9, -86.3), "IA": (42.1, -93.5),
    "KS": (38.5, -98.4), "KY": (37.5, -85.3), "LA": (31.1, -92.0), "ME": (45.4, -69.2), "MD": (39.0, -76.8),
    "MA": (42.3, -71.8), "MI": (44.3, -85.4), "MN": (46.3, -94.3), "MS": (32.7, -89.7), "MO": (38.4, -92.5),
    "MT": (47.0, -109.6), "NE": (41.5, -99.8), "NV": (39.3, -116.6), "NH": (43.7, -71.6), "NJ": (40.2, -74.7),
    "NM": (34.4, -106.1), "NY": (42.9, -75.5), "NC": (35.6, -79.4), "ND": (47.5, -100.5), "OH": (40.3, -82.8),
    "OK": (35.6, -97.5), "OR": (43.9, -120.6), "PA": (40.9, -77.8), "RI": (41.7, -71.5), "SC": (33.9, -80.9),
    "SD": (44.4, -100.2), "TN": (35.9, -86.4), "TX": (31.5, -99.3), "UT": (39.3, -111.7), "VT": (44.1, -72.7),
    "VA": (37.5, -78.9), "WA": (47.4, -120.5), "WV": (38.6, -80.6), "WI": (44.6, -89.9), "WY": (43.0, -107.6),
}


def geocode(address: str | None, state: str | None = None, client: httpx.Client | None = None) -> dict:
    """{'lat','lon','geo_source'}; Census address match when possible, else the state centroid."""
    if address:
        try:
            c = client or httpx.Client(timeout=20)
            r = c.get(CENSUS, params={"address": address, "benchmark": "Public_AR_Current", "format": "json"})
            m = r.json()["result"]["addressMatches"]
            if m:
                xy = m[0]["coordinates"]
                return {"lat": round(xy["y"], 5), "lon": round(xy["x"], 5), "geo_source": "census_geocoder"}
        except (httpx.HTTPError, ValueError, KeyError):
            pass
    lat, lon = STATE_CENTROIDS.get((state or "").upper(), (39.8, -98.6))
    return {"lat": lat, "lon": lon, "geo_source": "state_centroid"}


def great_circle_mi(a: dict, b: dict) -> float:
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def road_miles(a: dict, b: dict) -> int:
    return max(15, round(great_circle_mi(a, b) * ROAD_FACTOR))


def along(a: dict, b: dict, frac: float) -> dict:
    """Point at `frac` of the way from a to b (linear in lat/lon; fine for GPS breadcrumbs)."""
    return {"lat": round(a["lat"] + (b["lat"] - a["lat"]) * frac, 5),
            "lon": round(a["lon"] + (b["lon"] - a["lon"]) * frac, 5)}


def transit_hours(miles: float) -> float:
    """HOS-aware solo-driver estimate: ~50 mph, 11 h driving per 10 h reset."""
    drive = miles / 50
    return drive + 10 * math.floor(drive / 11)
