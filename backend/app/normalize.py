"""Deterministic normalisation of messy inputs.

Everything here is plain Python on purpose: property types, cities and
money amounts must never depend on the LLM "getting it right".
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------- types
CANONICAL_TYPES = ["Retail", "Office", "Residential", "Industrial", "Land"]

# keyword -> canonical type. Whole-word match, checked in order, first hit wins
# (so "warehouse" is not Residential via "house", "small office" is not Retail via "mall").
_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("warehouse", "Industrial"), ("industrial", "Industrial"), ("logistics", "Industrial"),
    ("factory", "Industrial"), ("godown", "Industrial"),
    ("office", "Office"), ("offices", "Office"), ("coworking", "Office"), ("co-working", "Office"),
    ("it park", "Office"), ("business park", "Office"),
    ("retail", "Retail"), ("shop", "Retail"), ("shops", "Retail"), ("showroom", "Retail"),
    ("mall", "Retail"), ("high-street", "Retail"), ("high street", "Retail"), ("store", "Retail"),
    ("residential", "Residential"), ("apartment", "Residential"), ("flat", "Residential"),
    ("villa", "Residential"), ("bungalow", "Residential"), ("penthouse", "Residential"),
    ("house", "Residential"), ("home", "Residential"), ("bhk", "Residential"), ("\\d+\\s*bhk", "Residential"),
    ("land", "Land"), ("plot", "Land"),
    # bare "commercial" with nothing else is treated as Office (dataset uses
    # "Commercial Office"); documented in DECISIONS.md
    ("commercial", "Office"),
]

# Category used for "residential vs commercial" comparisons.
TYPE_CATEGORY = {
    "Retail": "Commercial", "Office": "Commercial", "Industrial": "Commercial",
    "Residential": "Residential", "Land": "Land",
}


def normalize_type(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().lower()
    for canon in CANONICAL_TYPES:
        if s == canon.lower():
            return canon
    for kw, canon in _TYPE_KEYWORDS:
        if re.search(rf"(?<![a-z]){kw}(?![a-z])", s):
            return canon
    return None


# ---------------------------------------------------------------- occupancy
OCCUPANCY_VALUES = ["Tenanted", "Vacant", "Self-occupied", "Unknown"]


def normalize_occupancy(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = raw.strip().lower().replace("_", "-")
    if not s:
        return None
    if any(k in s for k in ("self", "owner", "own use", "personal")):
        return "Self-occupied"
    if any(k in s for k in ("vacant", "empty", "unlet", "not rented", "unoccupied")):
        return "Vacant"
    if any(k in s for k in ("tenant", "leased", "rented", "let", "occupied")):
        return "Tenanted"
    if "unknown" in s:
        return "Unknown"
    return None


# ---------------------------------------------------------------- locations
CITY_ALIASES = {
    "bombay": "Mumbai", "mumbai": "Mumbai",
    "bangalore": "Bengaluru", "bengaluru": "Bengaluru",
    "gurgaon": "Gurugram", "gurugram": "Gurugram",
    "new delhi": "Delhi", "delhi": "Delhi",
    "noida": "Noida", "greater noida": "Greater Noida",
    "navi mumbai": "Navi Mumbai", "thane": "Thane", "pune": "Pune",
    "hyderabad": "Hyderabad", "chennai": "Chennai", "kolkata": "Kolkata",
    "ahmedabad": "Ahmedabad", "alibaug": "Alibaug", "alibag": "Alibaug",
    "goa": "Goa", "lonavala": "Lonavala", "ghaziabad": "Ghaziabad", "faridabad": "Faridabad",
}

STATES = {
    "maharashtra", "karnataka", "haryana", "uttar pradesh", "up", "delhi ncr",
    "tamil nadu", "telangana", "gujarat", "goa", "west bengal", "kerala", "rajasthan",
}

STATE_OF_CITY = {
    "Mumbai": "Maharashtra", "Navi Mumbai": "Maharashtra", "Thane": "Maharashtra",
    "Pune": "Maharashtra", "Alibaug": "Maharashtra", "Lonavala": "Maharashtra",
    "Bengaluru": "Karnataka", "Gurugram": "Haryana", "Faridabad": "Haryana",
    "Noida": "Uttar Pradesh", "Greater Noida": "Uttar Pradesh", "Ghaziabad": "Uttar Pradesh",
    "Delhi": "Delhi", "Hyderabad": "Telangana", "Chennai": "Tamil Nadu",
    "Kolkata": "West Bengal", "Ahmedabad": "Gujarat", "Goa": "Goa",
}

# Regions let "my Delhi NCR properties" match Gurugram + Noida.
REGIONS = {
    "Delhi NCR": {"Delhi", "Gurugram", "Noida", "Greater Noida", "Ghaziabad", "Faridabad"},
    "Mumbai Metropolitan Region": {"Mumbai", "Navi Mumbai", "Thane"},
}
REGION_ALIASES = {
    "ncr": "Delhi NCR", "delhi ncr": "Delhi NCR", "delhi-ncr": "Delhi NCR",
    "mmr": "Mumbai Metropolitan Region", "mumbai metropolitan region": "Mumbai Metropolitan Region",
}

# Small locality gazetteer used only when a user gives a locality without a
# city ("Add a property in Indiranagar"). Unknown localities fall back to the
# user's home city and the agent is told it was an assumption.
LOCALITY_CITY = {
    "bandra": "Mumbai", "bandra west": "Mumbai", "bandra east": "Mumbai", "bkc": "Mumbai",
    "andheri": "Mumbai", "andheri east": "Mumbai", "andheri west": "Mumbai",
    "worli": "Mumbai", "lower parel": "Mumbai", "juhu": "Mumbai", "powai": "Mumbai",
    "goregaon": "Mumbai", "malad": "Mumbai", "colaba": "Mumbai", "dadar": "Mumbai",
    "khar": "Mumbai", "santacruz": "Mumbai", "chembur": "Mumbai", "nariman point": "Mumbai",
    "indiranagar": "Bengaluru", "koramangala": "Bengaluru", "whitefield": "Bengaluru",
    "hsr layout": "Bengaluru", "jayanagar": "Bengaluru", "mg road": "Bengaluru",
    "electronic city": "Bengaluru", "hebbal": "Bengaluru",
    "golf course road": "Gurugram", "cyber city": "Gurugram", "dlf": "Gurugram",
    "sohna road": "Gurugram", "connaught place": "Delhi", "saket": "Delhi",
    "hauz khas": "Delhi", "vasant kunj": "Delhi",
    "hinjewadi": "Pune", "koregaon park": "Pune", "baner": "Pune",
}


def canonical_city(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().lower()
    if s in CITY_ALIASES:
        return CITY_ALIASES[s]
    return raw.strip().title()


def region_of(city: Optional[str]) -> Optional[str]:
    for region, cities in REGIONS.items():
        if city in cities:
            return region
    return None


def parse_location(raw: str) -> dict:
    """Split 'Locality, City' into parts.

    Handles the dataset's exceptions:
      'Alibaug, Maharashtra'   -> last part is a state, so city = Alibaug
      'Noida Sector 62, Noida' -> locality keeps its text, city = Noida
      'Indiranagar'            -> no comma; city looked up in the gazetteer
    """
    raw = (raw or "").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    locality, city, state = None, None, None
    if len(parts) >= 2:
        last = parts[-1]
        if last.lower() in STATES and last.lower() not in CITY_ALIASES:
            state = last.title() if last.lower() != "up" else "Uttar Pradesh"
            if len(parts) >= 3:
                city = canonical_city(parts[-2])
                locality = ", ".join(parts[:-2])
            else:
                locality = parts[0]
                # "Indiranagar, Karnataka" -> known locality, so city = Bengaluru;
                # "Alibaug, Maharashtra" -> unknown locality, so it is the city itself
                city = LOCALITY_CITY.get(parts[0].lower()) or canonical_city(parts[0])
        else:
            city = canonical_city(last)
            locality = ", ".join(parts[:-1])
    elif len(parts) == 1:
        only = parts[0]
        if only.lower() in CITY_ALIASES:
            city = canonical_city(only)
            locality = city
        else:
            locality = only
            city = LOCALITY_CITY.get(only.lower())
            if not city:
                # "Bandra West Mumbai" -> trailing city name without a comma
                low = only.lower()
                for alias in sorted(CITY_ALIASES, key=len, reverse=True):
                    if low.endswith(" " + alias):
                        city = CITY_ALIASES[alias]
                        locality = only[: -len(alias)].strip()
                        break
    state = state or STATE_OF_CITY.get(city or "")
    return {"locality": locality, "city": city, "state": state}


# ---------------------------------------------------------------- money
_UNIT = {
    "crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000, "crs": 10_000_000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000, "l": 100_000,
    "million": 1_000_000, "mn": 1_000_000, "m": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000,
    "thousand": 1_000, "k": 1_000,
}
_MONEY_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)*)\s*(?P<unit>crores?|crs?|lakhs?|lacs?|million|mn|billion|bn|thousand|k|l|m)?\b",
    re.IGNORECASE,
)


def parse_inr(value) -> Optional[int]:
    """'₹4.2 crore' -> 42000000, '72 lakh' -> 7200000, '1,20,00,000' -> 12000000."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    s = str(value).strip().lower().replace("₹", " ")
    s = re.sub(r"\b(rs\.?|inr)(?=[\s\d]|$)", " ", s)
    m = _MONEY_RE.search(s)
    if not m:
        return None
    num = float(m.group("num").replace(",", ""))
    unit = (m.group("unit") or "").lower()
    return int(round(num * _UNIT.get(unit, 1)))


def parse_number(value) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:[.,]\d+)*", str(value))
    return float(m.group(0).replace(",", "")) if m else None


def parse_value_range(raw: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """users.csv has both '50000000' and '30000000-80000000'."""
    if not raw:
        return None, None
    parts = [p for p in re.split(r"\s*-\s*", str(raw).strip()) if p]
    if len(parts) == 2:
        return parse_inr(parts[0]), parse_inr(parts[1])
    v = parse_inr(parts[0])
    return v, v


def fmt_inr(v: Optional[float]) -> str:
    """Human format in crore/lakh, e.g. 120000000 -> '₹12 Cr'."""
    if v is None:
        return "n/a"
    v = float(v)
    if abs(v) >= 10_000_000:
        return f"₹{v / 10_000_000:,.2f} Cr".replace(".00 Cr", " Cr")
    if abs(v) >= 100_000:
        return f"₹{v / 100_000:,.2f} L".replace(".00 L", " L")
    return f"₹{v:,.0f}"
