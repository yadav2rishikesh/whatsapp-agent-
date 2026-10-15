"""Load the CSV dataset into the database (idempotent)."""
from __future__ import annotations

import csv

from .config import DATA_DIR
from .db import Property, SessionLocal, User, init_db
from .normalize import (normalize_occupancy, normalize_type, parse_inr, parse_location,
                        parse_number, parse_value_range)


def seed(force: bool = False) -> dict:
    init_db()
    db = SessionLocal()
    try:
        if db.query(User).count() and not force:
            return {"seeded": False}
        if force:
            db.query(Property).delete()
            db.query(User).delete()

        with open(DATA_DIR / "users.csv", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                lo, hi = parse_value_range(r["portfolio_value_preference_inr"])
                db.add(User(
                    user_id=r["user_id"], name=r["name"], city=r["city"],
                    preferences=r["preferences"], preferred_locations=r["preferred_locations"],
                    value_pref_min_inr=lo, value_pref_max_inr=hi,
                    value_pref_raw=r["portfolio_value_preference_inr"],
                ))

        with open(DATA_DIR / "properties.csv", newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                loc = parse_location(r["location"])
                occ = normalize_occupancy(r["occupancy_status"]) or "Unknown"
                db.add(Property(
                    property_id=r["property_id"], user_id=r["user_id"],
                    property_type=normalize_type(r["property_type"]) or r["property_type"],
                    property_type_raw=r["property_type"], sub_type=r["sub_type"] or None,
                    location_raw=r["location"], locality=loc["locality"], city=loc["city"],
                    state=loc["state"], area_sqft=parse_number(r["area_sqft"]),
                    current_value_inr=parse_inr(r["current_estimated_value_inr"]),
                    purchase_price_inr=parse_inr(r["purchase_price_inr"]) if r["purchase_price_inr"] else None,
                    annual_rent_inr=parse_inr(r["annual_rent_inr"]) or 0,
                    occupancy_status=occ, tenant_status="Yes" if occ == "Tenanted" else "No",
                    ownership_percent=parse_number(r["ownership_percent"]) or 100,
                    status=r["status"] or "Active", source="seed",
                ))
        db.commit()
        return {"seeded": True}
    finally:
        db.close()


if __name__ == "__main__":
    import sys
    print(seed(force="--force" in sys.argv))
