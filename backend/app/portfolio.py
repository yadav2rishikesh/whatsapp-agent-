"""Pure portfolio maths over plain dicts.

The same functions run on the real portfolio (from the DB) and on a
hypothetical copy (real portfolio + scenario changes), so what-if numbers
are computed exactly like real ones.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from .normalize import TYPE_CATEGORY, fmt_inr, region_of

YIELD_NOTE = "n/a — earns no rent ({occ})"


def _share(p: dict) -> float:
    return (p.get("ownership_percent") or 100) / 100.0


def eff_value(p: dict) -> float:
    return (p.get("current_value_inr") or 0) * _share(p)


def eff_rent(p: dict) -> float:
    return (p.get("annual_rent_inr") or 0) * _share(p)


def prop_yield(p: dict) -> Optional[float]:
    """Gross yield %. None when the property earns no rent (vacant/self-occupied)."""
    v, r = p.get("current_value_inr") or 0, p.get("annual_rent_inr") or 0
    if v <= 0 or r <= 0:
        return None
    return round(r / v * 100, 2)


def pct(part: float, whole: float) -> Optional[float]:
    return round(part / whole * 100, 2) if whole else None


def describe(p: dict) -> dict:
    """Compact property view returned to the model and UI."""
    y = prop_yield(p)
    out = {
        "property_id": p["property_id"],
        "type": p["property_type"],
        "sub_type": p.get("sub_type"),
        "locality": p.get("locality"),
        "city": p.get("city"),
        "area_sqft": p.get("area_sqft"),
        "value_inr": p.get("current_value_inr"),
        "value": fmt_inr(p.get("current_value_inr")),
        "annual_rent_inr": p.get("annual_rent_inr") or 0,
        "annual_rent": fmt_inr(p.get("annual_rent_inr") or 0),
        "occupancy": p.get("occupancy_status"),
        "gross_yield_pct": y,
        "value_per_sqft_inr": round(p["current_value_inr"] / p["area_sqft"]) if p.get("area_sqft") and p.get("current_value_inr") else None,
        "ownership_percent": p.get("ownership_percent") or 100,
    }
    if y is None:
        occ = p.get("occupancy_status") or "Unknown"
        out["yield_note"] = YIELD_NOTE.format(occ="occupancy unknown" if occ == "Unknown" else occ.lower())
    if p.get("purchase_price_inr"):
        out["purchase_price"] = fmt_inr(p["purchase_price_inr"])
    if p.get("hypothetical"):
        out["hypothetical"] = True
    return out


# ------------------------------------------------------------------ filters
def apply_filters(props: Iterable[dict], f: Optional[dict]) -> list[dict]:
    if not f:
        return list(props)
    out = []
    for p in props:
        if f.get("property_ids") and p["property_id"] not in f["property_ids"]:
            continue
        if f.get("exclude_property_ids") and p["property_id"] in f["exclude_property_ids"]:
            continue
        if f.get("types") and p["property_type"] not in f["types"]:
            continue
        if f.get("exclude_types") and p["property_type"] in f["exclude_types"]:
            continue
        if f.get("category") and TYPE_CATEGORY.get(p["property_type"]) != f["category"]:
            continue
        if f.get("cities"):
            wanted = {c.lower() for c in f["cities"]}
            region = (region_of(p.get("city")) or "").lower()
            if (p.get("city") or "").lower() not in wanted and region not in wanted:
                continue
        if f.get("exclude_cities"):
            ex = {c.lower() for c in f["exclude_cities"]}
            if (p.get("city") or "").lower() in ex or (region_of(p.get("city")) or "").lower() in ex:
                continue
        loc_text = f"{p.get('locality') or ''} {p.get('location_raw') or ''}".lower()
        if f.get("locality_contains") and not any(s.lower() in loc_text for s in f["locality_contains"]):
            continue
        if f.get("exclude_locality_contains") and any(s.lower() in loc_text for s in f["exclude_locality_contains"]):
            continue
        if f.get("occupancy") and p.get("occupancy_status") not in f["occupancy"]:
            continue
        v = p.get("current_value_inr") or 0
        if f.get("min_value_inr") is not None and v < f["min_value_inr"]:
            continue
        if f.get("max_value_inr") is not None and v > f["max_value_inr"]:
            continue
        r = p.get("annual_rent_inr") or 0
        if f.get("min_rent_inr") is not None and r < f["min_rent_inr"]:
            continue
        a = p.get("area_sqft") or 0
        if f.get("min_area_sqft") is not None and a < f["min_area_sqft"]:
            continue
        if f.get("max_area_sqft") is not None and a > f["max_area_sqft"]:
            continue
        out.append(p)
    return out


# ------------------------------------------------------------------ metrics
def _group(props: list[dict], key) -> dict:
    total_v = sum(eff_value(p) for p in props)
    groups: dict = defaultdict(list)
    for p in props:
        groups[key(p) or "Unknown"].append(p)
    res = {}
    for g, items in sorted(groups.items(), key=lambda kv: -sum(eff_value(p) for p in kv[1])):
        v = sum(eff_value(p) for p in items)
        r = sum(eff_rent(p) for p in items)
        res[g] = {
            "count": len(items),
            "value_inr": v, "value": fmt_inr(v),
            "share_of_value_pct": pct(v, total_v),
            "annual_rent_inr": r, "annual_rent": fmt_inr(r),
            "gross_yield_pct": pct(r, v),
            "property_ids": [p["property_id"] for p in items],
        }
    return res


def summary(props: list[dict]) -> dict:
    """Headline metrics for a set of properties."""
    n = len(props)
    if n == 0:
        return {"count": 0, "note": "No properties match."}
    total_v = sum(eff_value(p) for p in props)
    total_r = sum(eff_rent(p) for p in props)
    earning = [p for p in props if (p.get("annual_rent_inr") or 0) > 0]
    earning_v = sum(eff_value(p) for p in earning)
    occ = defaultdict(lambda: {"count": 0, "value_inr": 0.0, "property_ids": []})
    for p in props:
        o = occ[p.get("occupancy_status") or "Unknown"]
        o["count"] += 1
        o["value_inr"] += eff_value(p)
        o["property_ids"].append(p["property_id"])
    for o in occ.values():
        o["value"] = fmt_inr(o["value_inr"])
        o["share_of_value_pct"] = pct(o["value_inr"], total_v)
    non_earning = [p for p in props if (p.get("annual_rent_inr") or 0) <= 0]
    largest = max(props, key=eff_value)
    total_area = sum(p.get("area_sqft") or 0 for p in props)
    return {
        "count": n,
        "total_value_inr": total_v, "total_value": fmt_inr(total_v),
        "total_annual_rent_inr": total_r, "total_annual_rent": fmt_inr(total_r),
        "gross_yield_all_pct": pct(total_r, total_v),
        "gross_yield_income_producing_pct": pct(total_r, earning_v),
        "yield_note": "'all' divides rent by total value incl. vacant/self-occupied; 'income_producing' uses only rent-earning properties.",
        "total_area_sqft": total_area,
        "avg_value_per_sqft_inr": round(total_v / total_area) if total_area else None,
        "by_type": _group(props, lambda p: p["property_type"]),
        "by_category": _group(props, lambda p: TYPE_CATEGORY.get(p["property_type"])),
        "by_city": _group(props, lambda p: p.get("city")),
        "occupancy": dict(occ),
        "vacancy_rate_by_count_pct": pct(occ["Vacant"]["count"], n) if "Vacant" in occ else 0.0,
        "vacancy_rate_by_value_pct": pct(occ["Vacant"]["value_inr"], total_v) if "Vacant" in occ else 0.0,
        "non_earning_value_inr": sum(eff_value(p) for p in non_earning),
        "non_earning_value": fmt_inr(sum(eff_value(p) for p in non_earning)),
        "largest_asset": {"property_id": largest["property_id"], "share_of_value_pct": pct(eff_value(largest), total_v)},
    }


METRICS = {
    "value": lambda p: p.get("current_value_inr"),
    "rent": lambda p: p.get("annual_rent_inr") or 0,
    "yield": prop_yield,
    "area": lambda p: p.get("area_sqft"),
    "value_per_sqft": lambda p: (p["current_value_inr"] / p["area_sqft"]) if p.get("area_sqft") else None,
    "rent_per_sqft": lambda p: ((p.get("annual_rent_inr") or 0) / p["area_sqft"]) if p.get("area_sqft") else None,
}


def rank(props: list[dict], metric: str, order: str = "desc", limit: int = 3) -> dict:
    fn = METRICS[metric]
    scored = [(fn(p), p) for p in props]
    excluded = [p for v, p in scored if v is None]
    scored = [(v, p) for v, p in scored if v is not None]
    scored.sort(key=lambda t: t[0], reverse=(order == "desc"))
    top = scored[: max(1, limit)]
    res = {
        "metric": metric, "order": order,
        "ranked": [{"rank": i + 1, "metric_value": round(v, 2), **describe(p)} for i, (v, p) in enumerate(top)],
        "considered": len(scored),
    }
    # surface ties so the model doesn't pretend there is a single winner
    if len(scored) > 1 and len(top) >= 1:
        ties = [p["property_id"] for v, p in scored if v == top[0][0]]
        if len(ties) > 1:
            res["tie_for_first"] = ties
    if excluded:
        res["excluded_no_data"] = [
            {"property_id": p["property_id"], "reason": describe(p).get("yield_note", "missing data")} for p in excluded
        ]
    return res


def data_coverage(props: list[dict]) -> dict:
    """What can and cannot be answered from the stored fields."""
    n = len(props)
    with_pp = [p["property_id"] for p in props if p.get("purchase_price_inr")]
    unknown_occ = [p["property_id"] for p in props if p.get("occupancy_status") == "Unknown"]
    return {
        "properties": n,
        "purchase_price_available_for": with_pp,
        "purchase_price_missing_count": n - len(with_pp),
        "appreciation_roi_answerable": bool(with_pp),
        "date_fields": "none — no purchase date, lease dates or valuation history are stored",
        "time_based_questions_answerable": False,
        "occupancy_unknown_for": unknown_occ,
        "not_stored": [
            "purchase date / holding period", "historical valuations", "lease start/end, rent escalation",
            "expenses (maintenance, property tax) — so only GROSS yield, never net yield",
            "loans / EMI / leverage", "market comparables",
        ],
    }


def appreciation(props: list[dict]) -> dict:
    rows, missing = [], []
    for p in props:
        pp = p.get("purchase_price_inr")
        if not pp:
            missing.append(p["property_id"])
            continue
        gain = p["current_value_inr"] - pp
        rows.append({"property_id": p["property_id"], "purchase_price": fmt_inr(pp),
                     "current_value": fmt_inr(p["current_value_inr"]), "gain": fmt_inr(gain),
                     "appreciation_pct": pct(gain, pp)})
    return {
        "computed_for": rows,
        "missing_purchase_price": missing,
        "note": ("Purchase price is not recorded for these properties, so appreciation/ROI cannot be computed. "
                 "No purchase dates are stored either, so annualised returns (CAGR) are never computable.")
        if missing else "Annualised return needs a purchase date, which is not stored.",
    }
