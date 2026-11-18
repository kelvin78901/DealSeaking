"""Micro Center store directory + ZIP-based store selection.

Addresses from Micro Center's official store list (Oct 2026); coordinates are the
ZIP centroid, which is plenty for "stores within N miles of me".
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class McStore:
    name: str
    id: str
    state: str
    address: str
    zip: str
    lat: float
    lon: float


STORES: tuple[McStore, ...] = tuple(McStore(*r) for r in (
    ("Phoenix", "205", "AZ", "4531 E Thomas Rd", "85018", 33.4958, -111.9883),
    ("Santa Clara", "195", "CA", "5201 Stevens Creek Blvd", "95051", 37.3483, -121.9844),
    ("Tustin", "101", "CA", "1100 E Edinger Ave", "92780", 33.7364, -117.8229),
    ("Denver", "181", "CO", "8000 E Quincy Ave", "80237", 39.6431, -104.8987),
    ("Miami", "185", "FL", "7795 W Flagler St M31", "33144", 25.7626, -80.3096),
    ("Duluth", "065", "GA", "2340 Pleasant Hill Rd", "30096", 33.9845, -84.1529),
    ("Marietta", "041", "GA", "1275 Powers Ferry Rd SE Ste 50", "30067", 33.9282, -84.4733),
    ("Chicago", "151", "IL", "2645 N Elston Ave", "60647", 41.9209, -87.7043),
    ("Westmont", "025", "IL", "80 E Ogden Ave", "60559", 41.7728, -87.9757),
    ("Indianapolis", "165", "IN", "5702 E 86th St", "46250", 39.9048, -86.0673),
    ("Overland Park", "191", "KS", "9294 Metcalf Ave", "66212", 38.9568, -94.6832),
    ("Cambridge", "121", "MA", "730 Memorial Dr", "02139", 42.3647, -71.1042),
    ("Rockville", "085", "MD", "1776 E Jefferson St Ste 203", "20852", 39.0496, -77.1204),
    ("Parkville", "125", "MD", "1957 E Joppa Rd", "21234", 39.3876, -76.5418),
    ("Madison Heights", "055", "MI", "32800 Concord Dr", "48071", 42.5016, -83.1027),
    ("St. Louis Park", "045", "MN", "3710 Highway 100 South", "55416", 44.9497, -93.3373),
    ("Brentwood", "095", "MO", "87 Brentwood Promenade Ct", "63144", 38.6182, -90.3484),
    ("Charlotte", "175", "NC", "4744 South Blvd", "28217", 35.1714, -80.9084),
    ("North Jersey", "075", "NJ", "263 McLean Blvd, Paterson", "07504", 40.9122, -74.1452),
    ("Westbury", "171", "NY", "655 Merrick Ave", "11590", 40.7557, -73.5723),
    ("Brooklyn", "115", "NY", "850 3rd Ave", "11232", 40.6521, -74.0018),
    ("Flushing", "145", "NY", "71-43 Kissena Blvd, Queens", "11367", 40.728, -73.8195),
    ("Yonkers", "105", "NY", "750-A Central Park Ave", "10704", 40.9176, -73.8593),
    ("Columbus", "141", "OH", "747 Bethel Rd", "43214", 40.0535, -83.0187),
    ("Mayfield Heights", "051", "OH", "1349 Som Center Rd", "44124", 41.501, -81.4694),
    ("Sharonville", "071", "OH", "11755 Mosteller Rd", "45241", 39.2638, -84.4092),
    ("St. Davids", "061", "PA", "550 E Lancaster Ave Ste C", "19087", 40.0612, -75.3999),
    ("Austin", "215", "TX", "12707 N Mopac Expy", "78727", 30.4254, -97.7195),
    ("Houston", "155", "TX", "5305 S Rice Ave", "77081", 29.7119, -95.4845),
    ("Dallas", "131", "TX", "13929 N Central Expressway", "75243", 32.9103, -96.7285),
    ("Fairfax", "081", "VA", "3089 Nutley St", "22031", 38.8604, -77.2649),
))
SHIPPABLE = McStore("Shippable", "029", "", "线上可发货", "", 0.0, 0.0)

BY_NAME = {s.name.lower(): s for s in STORES + (SHIPPABLE,)}
BY_ID = {s.id: s for s in STORES + (SHIPPABLE,)}
# aliases people actually type
for alias, name in {"st louis park": "St. Louis Park", "st davids": "St. Davids", "paterson": "North Jersey",
                    "queens": "Flushing", "paramus": "North Jersey", "online": "Shippable",
                    "shipping": "Shippable"}.items():
    BY_NAME[alias] = BY_NAME[name.lower()]


def find_store(value) -> McStore:
    s = str(value).strip()
    if s.lower() in BY_NAME:
        return BY_NAME[s.lower()]
    if s.isdigit() and s.zfill(3) in BY_ID:
        return BY_ID[s.zfill(3)]
    raise ValueError(f"未知的 Micro Center 门店: {value!r}。可用：{', '.join(x.name for x in STORES)}, Shippable")


@lru_cache(maxsize=256)
def geocode_zip(zip_code: str) -> tuple[float, float, str]:
    """US ZIP → (lat, lon, 'City, ST'), offline via the `zipcodes` package."""
    import zipcodes

    z = str(zip_code).strip()[:5]
    try:
        m = zipcodes.matching(z)
    except Exception:
        m = []
    if not m:
        raise ValueError(f"无效的美国 ZIP：{zip_code!r}")
    return float(m[0]["lat"]), float(m[0]["long"]), f"{m[0]['city']}, {m[0]['state']}"


def miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def stores_by_distance(zip_code: str) -> list[tuple[McStore, float]]:
    lat, lon, _ = geocode_zip(zip_code)
    return sorted(((s, round(miles(lat, lon, s.lat, s.lon), 1)) for s in STORES), key=lambda x: x[1])


def stores_near(zip_code: str, radius_miles: float) -> list[tuple[McStore, float]]:
    """Stores within the radius; if none, the single nearest one (so there's always something to watch)."""
    ranked = stores_by_distance(zip_code)
    inside = [x for x in ranked if x[1] <= radius_miles]
    return inside or ranked[:1]
