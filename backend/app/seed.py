"""Load the CSV dataset into the database (idempotent)."""
from __future__ import annotations

import csv

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import DATA_DIR
from .db import Base, Property, User, engine
from .normalize import (normalize_occupancy, normalize_type, parse_inr, parse_location,
                        parse_number, parse_value_range)

_SEED_LOCK_ID = 7_310_001  # arbitrary app-wide id for pg_advisory_xact_lock


def seed(force: bool = False) -> dict:
    """Create tables and load the CSVs, once, using ONE connection and ONE transaction.

    On Vercel the first page load fires several requests at once, each on a fresh
    serverless instance running this. A transaction-scoped Postgres advisory lock
    makes them take turns: the first creates + seeds, the rest wait, see the data
    and skip. Transaction-scoped (not session-scoped) so it is also safe behind
    Supabase's transaction pooler. Only one DB connection is used per call.
    """
    try:
        with engine.begin() as conn:  # commits on success, rolls back on error
            if conn.dialect.name == "postgresql":
                conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SEED_LOCK_ID})
            Base.metadata.create_all(conn)
            with Session(bind=conn, autoflush=False) as db:
                return _seed(db, force)
    except IntegrityError:
        # Only reachable without the lock (SQLite + several processes): someone
        # else seeded first, so there is nothing left to do.
        return {"seeded": False, "note": "already seeded"}


def _seed(db: Session, force: bool) -> dict:
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
    # Users must exist before properties reference them. Postgres enforces the
    # foreign key (SQLite doesn't by default), and the unit of work doesn't
    # order the two tables because there is no relationship() between them.
    db.flush()

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
    db.flush()  # the surrounding engine.begin() commits
    return {"seeded": True}


if __name__ == "__main__":
    import sys
    print(seed(force="--force" in sys.argv))
