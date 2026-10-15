"""Agent tools.

Design rules:
- Every number the user sees comes from these functions, never from the LLM.
- Every tool is scoped to the current user; the model cannot pass a user_id.
- Money/area/type/city inputs are accepted as the user said them
  ("₹4.2 crore", "Bangalore", "commercial office") and normalised here.
- Each result echoes `interpreted_as` so the admin trace shows exactly how a
  request was understood.
- Hypothetical (what-if) changes live on the conversation, never in the
  properties table.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Literal, Optional, Union

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, model_validator

from . import portfolio as pf
from .db import Conversation, Property, PropertyAudit, User
from .normalize import (CANONICAL_TYPES, REGION_ALIASES, canonical_city, fmt_inr,
                        normalize_occupancy, normalize_type, parse_inr, parse_location,
                        parse_number)

Money = Optional[Union[str, float]]


@dataclass
class ToolContext:
    db: object
    user_id: str
    conversation_id: int
    events: list = field(default_factory=list)   # things the runner turns into flags

    def event(self, kind: str, severity: str, note: str):
        self.events.append({"kind": kind, "severity": severity, "note": note})


# ------------------------------------------------------------------ schemas

def _is_unset(v) -> bool:
    """Placeholder values models emit for fields they meant to leave out."""
    if v is None:
        return True
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, str):
        return v.strip().lower() in ("", "0", "0.0", "null", "none", "n/a")
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


class Lenient(BaseModel):
    """Base for every tool-argument model.

    LLMs often fill *every* field of a schema with empty/zero placeholders
    ("max_value": 0, "types": [], "category": "") instead of omitting them.
    Taken literally, max_value=0 filters out the whole portfolio and
    annual_rent=0 would wipe real rent. So empty strings, empty lists, zeros and
    "null" are treated as NOT SET. (A real zero rent is expressed through
    occupancy = Vacant/Self-occupied, which clears rent explicitly.)
    """

    @model_validator(mode="before")
    @classmethod
    def _drop_placeholders(cls, data):
        if not isinstance(data, dict):
            return data
        out = {}
        for k, v in data.items():
            if isinstance(v, list):
                v = [x for x in v if not _is_unset(x)]
            if not _is_unset(v):
                out[k] = v
        return out

class Filters(Lenient):
    property_ids: Optional[list[str]] = None
    exclude_property_ids: Optional[list[str]] = None
    types: Optional[list[str]] = Field(None, description="e.g. ['Retail']")
    exclude_types: Optional[list[str]] = None
    category: Optional[Literal["Residential", "Commercial"]] = None
    cities: Optional[list[str]] = Field(None, description="City or region ('Delhi NCR')")
    exclude_cities: Optional[list[str]] = None
    locality_contains: Optional[list[str]] = Field(None, description="e.g. ['Bandra']")
    exclude_locality_contains: Optional[list[str]] = Field(None, description="'exclude Bandra' -> ['Bandra']")
    occupancy: Optional[list[str]] = Field(None, description="Tenanted/Vacant/Self-occupied/Unknown")
    min_value: Money = Field(None, description="e.g. '10 crore'")
    max_value: Money = None
    min_rent: Money = None
    min_area_sqft: Optional[float] = None
    max_area_sqft: Optional[float] = None


def _norm_filters(f: Optional[Filters]) -> tuple[dict, list[str]]:
    """Return (filters for portfolio.apply_filters, warnings)."""
    if not f:
        return {}, []
    if isinstance(f, dict):
        f = Filters(**f)
    warn: list[str] = []
    out: dict = {}
    if f.property_ids:
        out["property_ids"] = [x.upper().strip() for x in f.property_ids]
    if f.exclude_property_ids:
        out["exclude_property_ids"] = [x.upper().strip() for x in f.exclude_property_ids]
    for src, dst in (("types", "types"), ("exclude_types", "exclude_types")):
        vals = getattr(f, src)
        if vals:
            norm = []
            for t in vals:
                n = normalize_type(t)
                if n:
                    norm.append(n)
                else:
                    warn.append(f"Unknown property type '{t}' ignored. Known: {CANONICAL_TYPES}")
            if norm:
                out[dst] = norm
    if f.category:
        out["category"] = f.category
    if f.cities:
        cities = []
        for c in f.cities:
            key = c.lower().strip()
            if key in ("delhi", "new delhi"):
                # Colloquially "Delhi" means the NCR (Gurugram, Noida...). Made explicit to the user.
                cities.append("Delhi NCR")
                warn.append("'Delhi' interpreted as Delhi NCR (Delhi, Gurugram, Noida, Ghaziabad, Faridabad) — mention this to the user.")
            else:
                cities.append(REGION_ALIASES.get(key, canonical_city(c)))
        out["cities"] = cities
    if f.exclude_cities:
        out["exclude_cities"] = [REGION_ALIASES.get(c.lower().strip(), canonical_city(c)) for c in f.exclude_cities]
    if f.locality_contains:
        out["locality_contains"] = f.locality_contains
    if f.exclude_locality_contains:
        out["exclude_locality_contains"] = f.exclude_locality_contains
    if f.occupancy:
        out["occupancy"] = [normalize_occupancy(o) or o for o in f.occupancy]
    for src in ("min_value", "max_value", "min_rent"):
        v = getattr(f, src)
        if v not in (None, ""):
            parsed = parse_inr(v)
            if parsed is None:
                warn.append(f"Could not parse {src}='{v}'")
            else:
                out[src + "_inr"] = parsed
    if f.min_area_sqft is not None:
        out["min_area_sqft"] = f.min_area_sqft
    if f.max_area_sqft is not None:
        out["max_area_sqft"] = f.max_area_sqft
    return out, warn


def _readable(filters: dict) -> dict:
    r = dict(filters)
    for k in ("min_value_inr", "max_value_inr", "min_rent_inr"):
        if k in r:
            r[k] = f"{r[k]} ({fmt_inr(r[k])})"
    return r


# ------------------------------------------------------------------ data access
def _to_dict(p: Property) -> dict:
    return {c.name: getattr(p, c.name) for c in Property.__table__.columns}


def real_portfolio(ctx: ToolContext) -> list[dict]:
    rows = (ctx.db.query(Property)
            .filter(Property.user_id == ctx.user_id, Property.status == "Active")
            .order_by(Property.property_id).all())
    return [_to_dict(p) for p in rows]


def _conversation(ctx: ToolContext) -> Conversation:
    return ctx.db.get(Conversation, ctx.conversation_id)


def hypothetical_portfolio(ctx: ToolContext) -> tuple[list[dict], list[str]]:
    """Real portfolio with the conversation's what-if changes applied (in memory only)."""
    props = {p["property_id"]: p for p in real_portfolio(ctx)}
    notes = []
    for i, ch in enumerate(_conversation(ctx).scenario or []):
        act = ch.get("action")
        if act == "add":
            hp = ch["property"]
            props[hp["property_id"]] = hp
        elif act == "remove":
            props.pop(ch["property_id"], None)
        elif act == "modify":
            if ch["property_id"] in props:
                p = copy.deepcopy(props[ch["property_id"]])
                p.update(ch["fields"])
                p["hypothetical"] = True
                props[ch["property_id"]] = p
            else:
                notes.append(f"change {i + 1}: {ch['property_id']} no longer exists")
    return list(props.values()), notes


def _portfolio(ctx: ToolContext, use_hypothetical: bool) -> tuple[list[dict], dict]:
    if use_hypothetical:
        scen = _conversation(ctx).scenario or []
        if not scen:
            return real_portfolio(ctx), {"data_basis": "REAL (no what-if scenario is active)"}
        props, notes = hypothetical_portfolio(ctx)
        meta = {"data_basis": "HYPOTHETICAL — real portfolio + what-if changes, nothing saved",
                "scenario_changes": len(scen)}
        if notes:
            meta["scenario_warnings"] = notes
        return props, meta
    return real_portfolio(ctx), {"data_basis": "REAL"}


def _owned(ctx: ToolContext, property_id: str) -> Optional[Property]:
    p = ctx.db.get(Property, (property_id or "").upper().strip())
    if p is None or p.user_id != ctx.user_id or p.status != "Active":
        return None
    return p


def _next_property_id(ctx: ToolContext) -> str:
    ids = [r[0] for r in ctx.db.query(Property.property_id).all()]
    nums = [int(i[1:]) for i in ids if i.startswith("P") and i[1:].isdigit()]
    return f"P{(max(nums) + 1 if nums else 1):03d}"


def _audit(ctx: ToolContext, pid: str, action: str, changes: dict):
    ctx.db.add(PropertyAudit(property_id=pid, conversation_id=ctx.conversation_id,
                             action=action, changes=changes))


# ------------------------------------------------------------------ field validation
class PropertyFields(Lenient):
    property_type: Optional[str] = Field(None, description="Retail / Office / Residential / Industrial / Land (free text ok)")
    sub_type: Optional[str] = Field(None, description="e.g. Apartment, High-street retail, Office floor")
    location: Optional[str] = Field(None, description="'Locality, City' or just locality, e.g. 'Indiranagar'")
    city: Optional[str] = Field(None, description="Only if the user stated it or confirmed it")
    area_sqft: Optional[Union[str, float]] = None
    current_value: Money = Field(None, description="As the user said it, e.g. '₹4.2 crore'")
    annual_rent: Money = Field(None, description="Annual rent; monthly figures must be multiplied by 12 first and stated")
    occupancy: Optional[str] = Field(None, description="Tenanted / Vacant / Self-occupied")
    ownership_percent: Optional[float] = None
    purchase_price: Money = None


def _clean_fields(ctx: ToolContext, f: PropertyFields, creating: bool) -> tuple[dict, list[str], list[str]]:
    """Validate & normalise. Returns (fields, errors, assumptions)."""
    out, errors, assumptions = {}, [], []
    if f.property_type is not None:
        t = normalize_type(f.property_type)
        if not t:
            errors.append(f"property_type '{f.property_type}' not recognised (use one of {CANONICAL_TYPES})")
        else:
            out["property_type"] = t
            out["property_type_raw"] = f.property_type
    if f.sub_type:
        out["sub_type"] = f.sub_type
    if f.location:
        has_city = bool(f.city) and f.city.strip().lower() not in f.location.lower()
        loc = parse_location(f"{f.location}, {f.city}" if has_city else f.location)
        if not loc["city"]:
            home = ctx.db.get(User, ctx.user_id).city
            home_city = canonical_city(home) if home and "ncr" not in home.lower() else None
            if home_city:
                loc = parse_location(f"{f.location}, {home_city}")
                assumptions.append(f"City not given and '{f.location}' is not a known locality — assumed {home_city} (user's home city). Confirm with the user.")
            else:
                errors.append(f"Could not determine the city for '{f.location}'. Ask the user which city.")
        elif "," not in f.location and not f.city and loc["locality"] != loc["city"]:
            assumptions.append(f"City inferred as {loc['city']} from locality '{f.location}'.")
        same = (loc["locality"] or "").lower() == (loc["city"] or "").lower()
        out.update(location_raw=(loc["city"] if same else f"{loc['locality']}, {loc['city']}") if loc["city"] else f.location,
                   locality=loc["locality"], city=loc["city"], state=loc["state"])
    if f.area_sqft not in (None, ""):
        a = parse_number(f.area_sqft)
        if not a or a <= 0:
            errors.append(f"area_sqft '{f.area_sqft}' invalid")
        elif a > 2_000_000:
            errors.append(f"area_sqft {a:,.0f} looks implausible — confirm with user")
        else:
            out["area_sqft"] = a
    for src, dst in (("current_value", "current_value_inr"), ("annual_rent", "annual_rent_inr"),
                     ("purchase_price", "purchase_price_inr")):
        v = getattr(f, src)
        if v in (None, ""):
            continue
        n = parse_inr(v)
        if n is None or n < 0:
            errors.append(f"{src} '{v}' could not be parsed as an INR amount")
        else:
            out[dst] = n
    if out.get("current_value_inr") is not None and out["current_value_inr"] < 100_000:
        errors.append(f"value {fmt_inr(out['current_value_inr'])} looks too small — did the user mean lakh/crore?")
    if f.occupancy:
        o = normalize_occupancy(f.occupancy)
        if not o:
            errors.append(f"occupancy '{f.occupancy}' not recognised (Tenanted / Vacant / Self-occupied)")
        else:
            out["occupancy_status"] = o
    if f.ownership_percent is not None:
        if not 0 < f.ownership_percent <= 100:
            errors.append("ownership_percent must be between 0 and 100")
        else:
            out["ownership_percent"] = f.ownership_percent
    return out, errors, assumptions


def _reconcile_rent_occupancy(rec: dict, assumptions: list[str], errors: list[str]):
    """Keep rent / occupancy / tenant_status consistent."""
    occ = rec.get("occupancy_status")
    rent = rec.get("annual_rent_inr")
    if occ in ("Vacant", "Self-occupied") and rent:
        errors.append(f"Rent {fmt_inr(rent)} given but occupancy is {occ}. Ask the user which is correct.")
    if occ in ("Vacant", "Self-occupied"):
        rec["annual_rent_inr"] = 0
    if rent and not occ:
        rec["occupancy_status"] = "Tenanted"
        assumptions.append("Rent was given, so occupancy set to Tenanted.")
    if rec.get("occupancy_status"):
        rec["tenant_status"] = "Yes" if rec["occupancy_status"] == "Tenanted" else "No"


# ================================================================== tools
def build_tools(ctx: ToolContext) -> list[StructuredTool]:

    # ---------------- read
    class SearchArgs(Lenient):
        filters: Optional[Filters] = None
        sort_by: Optional[Literal["value", "rent", "yield", "area", "value_per_sqft"]] = None
        use_hypothetical: bool = Field(False, description="True only when the user is asking about the active what-if scenario")

    def search_properties(filters: Optional[Filters] = None, sort_by: Optional[str] = None,
                          use_hypothetical: bool = False) -> dict:
        """List the user's properties matching filters (type, city/region, locality, value/rent/area ranges, occupancy). Returns each property's details incl. gross yield."""
        props, meta = _portfolio(ctx, use_hypothetical)
        f, warn = _norm_filters(filters)
        rows = pf.apply_filters(props, f)
        if sort_by:
            fn = pf.METRICS[sort_by]
            rows.sort(key=lambda p: (fn(p) is None, -(fn(p) or 0)))
        res = {**meta, "interpreted_as": _readable(f), "count": len(rows),
               "properties": [pf.describe(p) for p in rows]}
        if warn:
            res["warnings"] = warn
        if not rows:
            res["note"] = f"No match. The user has {len(props)} properties in total."
        return res

    class GetArgs(Lenient):
        property_id: str

    def get_property(property_id: str) -> dict:
        """Full details of one property by ID, including its change history."""
        p = _owned(ctx, property_id)
        if not p:
            return {"error": f"{property_id} not found in this user's active portfolio"}
        hist = (ctx.db.query(PropertyAudit).filter(PropertyAudit.property_id == p.property_id)
                .order_by(PropertyAudit.id).all())
        d = _to_dict(p)
        return {**pf.describe(d), "location_raw": d["location_raw"], "state": d["state"],
                "original_type_label": d["property_type_raw"], "source": d["source"],
                "purchase_price": fmt_inr(d["purchase_price_inr"]) if d["purchase_price_inr"] else "not recorded",
                "change_history": [{"action": h.action, "changes": h.changes} for h in hist]}

    class SummaryArgs(Lenient):
        filters: Optional[Filters] = None
        use_hypothetical: bool = False

    def portfolio_summary(filters: Optional[Filters] = None, use_hypothetical: bool = False) -> dict:
        """Totals and breakdowns: total value, rent, gross yield (all vs income-producing), value by type / category (residential vs commercial) / city with % exposure, occupancy & vacancy, largest-asset concentration."""
        props, meta = _portfolio(ctx, use_hypothetical)
        f, warn = _norm_filters(filters)
        rows = pf.apply_filters(props, f)
        res = {**meta, "interpreted_as": _readable(f), **pf.summary(rows)}
        if warn:
            res["warnings"] = warn
        return res

    class RankArgs(Lenient):
        metric: Literal["value", "rent", "yield", "area", "value_per_sqft", "rent_per_sqft"]
        order: Literal["desc", "asc"] = Field("desc", description="desc = highest first")
        limit: int = Field(3, ge=1, le=20)
        filters: Optional[Filters] = None
        use_hypothetical: bool = False

    def rank_properties(metric: str, order: str = "desc", limit: int = 3,
                        filters: Optional[Filters] = None, use_hypothetical: bool = False) -> dict:
        """Highest/lowest properties by value, rent, gross yield, area, value per sqft or rent per sqft. Properties with no rent are excluded from yield ranking and listed separately."""
        props, meta = _portfolio(ctx, use_hypothetical)
        f, warn = _norm_filters(filters)
        res = {**meta, "interpreted_as": _readable(f), **pf.rank(pf.apply_filters(props, f), metric, order, limit)}
        if warn:
            res["warnings"] = warn
        return res

    class CompareArgs(Lenient):
        group_by: Optional[Literal["type", "category", "city", "occupancy"]] = Field(
            None, description="Compare groups, e.g. category for residential vs commercial")
        property_ids: Optional[list[str]] = Field(None, description="Compare specific properties side by side")
        filters: Optional[Filters] = None
        use_hypothetical: bool = False

    def compare(group_by: Optional[str] = None, property_ids: Optional[list[str]] = None,
                filters: Optional[Filters] = None, use_hypothetical: bool = False) -> dict:
        """Side-by-side comparison of specific properties, or of groups (type, category = residential vs commercial, city, occupancy) on value, share, rent and yield."""
        props, meta = _portfolio(ctx, use_hypothetical)
        f, warn = _norm_filters(filters)
        rows = pf.apply_filters(props, f)
        res = {**meta, "interpreted_as": _readable(f)}
        if property_ids:
            ids = [i.upper().strip() for i in property_ids]
            found = [p for p in rows if p["property_id"] in ids]
            res["properties"] = [pf.describe(p) for p in found]
            missing = sorted(set(ids) - {p["property_id"] for p in found})
            if missing:
                res["not_found"] = missing
            res["note"] = ("Gross yield ignores costs. 'Better' depends on the user's goal "
                           "(income → yield; capital size → value; risk → vacancy/concentration).")
        else:
            key = {"type": lambda p: p["property_type"],
                   "category": lambda p: pf.TYPE_CATEGORY.get(p["property_type"]),
                   "city": lambda p: p.get("city"),
                   "occupancy": lambda p: p.get("occupancy_status")}[group_by or "category"]
            res["group_by"] = group_by or "category"
            res["groups"] = pf._group(rows, key)
        if warn:
            res["warnings"] = warn
        return res

    class CoverageArgs(Lenient):
        question_topic: str = Field(..., description="What the user asked about, e.g. 'ROI', 'appreciation', 'growth over last year'")

    def check_data_availability(question_topic: str) -> dict:
        """Call BEFORE answering anything about ROI, appreciation, capital gains, returns, purchase price, dates, trends over time, lease expiry, expenses, net yield or loans. Says what is and isn't stored and computes appreciation where purchase price exists."""
        props = real_portfolio(ctx)
        cov = pf.data_coverage(props)
        appr = pf.appreciation(props)
        if not cov["appreciation_roi_answerable"] or any(
                w in question_topic.lower() for w in ("time", "year", "month", "trend", "growth", "history", "date", "lease", "since")):
            ctx.event("data_gap", "low", f"Asked about '{question_topic}' — required data not stored.")
        return {"topic": question_topic, **cov, "appreciation": appr}

    # ---------------- write
    class CreateArgs(PropertyFields):
        allow_duplicate: bool = Field(False, description="Set true only after the user confirms a flagged near-duplicate is a different property")

    def create_property(allow_duplicate: bool = False, **kw) -> dict:
        """Add a new property to the user's REAL portfolio (saved to DB). Needs at least property_type, location, area_sqft and current_value. Never use for hypotheticals — use what_if."""
        f = PropertyFields(**kw)
        rec, errors, assumptions = _clean_fields(ctx, f, creating=True)
        missing = [k for k in ("property_type", "location", "area_sqft", "current_value")
                   if getattr(f, k) in (None, "")]
        _reconcile_rent_occupancy(rec, assumptions, errors)
        if missing or errors:
            ctx.event("write_blocked", "low", f"create_property needs input: missing={missing} errors={errors}")
            return {"status": "needs_input", "saved": False, "missing_fields": missing, "errors": errors,
                    "instruction": "Ask the user for these, then call create_property again."}
        if "occupancy_status" not in rec:
            rec["occupancy_status"] = "Unknown"
            rec["tenant_status"] = "No"
            rec["annual_rent_inr"] = 0
            assumptions.append("Occupancy/rent not given — saved as 'Unknown' with no rent. Ask the user whether it is tenanted.")
        if not allow_duplicate:
            for p in real_portfolio(ctx):
                same_place = (p.get("locality") or "").lower() == (rec.get("locality") or "").lower()
                close_area = p.get("area_sqft") and abs(p["area_sqft"] - rec["area_sqft"]) / p["area_sqft"] < 0.05
                if same_place and p["property_type"] == rec["property_type"] and close_area:
                    return {"status": "possible_duplicate", "saved": False, "existing": pf.describe(p),
                            "instruction": "Ask whether this is the same property (maybe they meant update) or a new one."}
        pid = _next_property_id(ctx)
        prop = Property(property_id=pid, user_id=ctx.user_id, status="Active", source="chat",
                        ownership_percent=rec.pop("ownership_percent", 100), **rec)
        ctx.db.add(prop)
        _audit(ctx, pid, "create", {k: [None, v] for k, v in rec.items()})
        ctx.db.flush()
        return {"status": "created", "saved": True, "property": pf.describe(_to_dict(prop)),
                "assumptions": assumptions}

    class UpdateArgs(PropertyFields):
        property_id: str = Field(..., description="Resolve from the property index / context, e.g. 'Bandra retail' -> P001")

    def update_property(property_id: str, **kw) -> dict:
        """Change fields of an existing REAL property (saved to DB): value, rent, occupancy, area, type, sub_type, ownership %, purchase price, location. Only for real changes the user asked for — what-ifs go to what_if."""
        p = _owned(ctx, property_id)
        if not p:
            return {"status": "error", "saved": False, "error": f"{property_id} not found in this user's active portfolio"}
        f = PropertyFields(**kw)
        rec, errors, assumptions = _clean_fields(ctx, f, creating=False)
        if not rec and not errors:
            return {"status": "needs_input", "saved": False, "errors": ["No fields to change were given."]}
        merged = {"occupancy_status": rec.get("occupancy_status"),
                  "annual_rent_inr": rec.get("annual_rent_inr")}
        # rent set > 0 on a vacant property without occupancy change -> becomes tenanted
        if merged["annual_rent_inr"] and not merged["occupancy_status"] and p.occupancy_status != "Tenanted":
            rec["occupancy_status"] = "Tenanted"
            assumptions.append(f"Rent added, so occupancy changed {p.occupancy_status} → Tenanted.")
        # occupancy -> vacant/self-occupied clears rent
        check = {"occupancy_status": rec.get("occupancy_status"), "annual_rent_inr": rec.get("annual_rent_inr")}
        if check["occupancy_status"] in ("Vacant", "Self-occupied") and check["annual_rent_inr"]:
            errors.append(f"Rent given but occupancy set to {check['occupancy_status']} — ask which is right.")
        if errors:
            ctx.event("write_blocked", "low", f"update_property {property_id}: {errors}")
            return {"status": "needs_input", "saved": False, "errors": errors}
        if rec.get("occupancy_status") in ("Vacant", "Self-occupied") and p.annual_rent_inr:
            rec["annual_rent_inr"] = 0
            assumptions.append(f"Occupancy now {rec['occupancy_status']}, so annual rent set to 0 (was {fmt_inr(p.annual_rent_inr)}).")
        if rec.get("occupancy_status"):
            rec["tenant_status"] = "Yes" if rec["occupancy_status"] == "Tenanted" else "No"
        changes = {}
        for k, v in rec.items():
            old = getattr(p, k)
            if old != v:
                changes[k] = [old, v]
                setattr(p, k, v)
        if not changes:
            return {"status": "unchanged", "saved": False, "property": pf.describe(_to_dict(p)),
                    "note": "Values already match; nothing changed."}
        _audit(ctx, p.property_id, "update", changes)
        ctx.db.flush()
        readable = {k: [fmt_inr(o) if k.endswith("_inr") else o, fmt_inr(n) if k.endswith("_inr") else n]
                    for k, (o, n) in changes.items() if k != "property_type_raw"}
        return {"status": "updated", "saved": True, "changes_old_new": readable,
                "property": pf.describe(_to_dict(p)), "assumptions": assumptions}

    class ArchiveArgs(Lenient):
        property_id: str
        confirmed: bool = Field(False, description="True only after the user explicitly confirmed removal in this conversation")
        reason: Optional[str] = Field(None, description="e.g. 'sold'")

    def archive_property(property_id: str, confirmed: bool = False, reason: Optional[str] = None) -> dict:
        """Remove a property from the REAL portfolio (e.g. sold). Soft delete: kept in DB as Archived. Requires explicit user confirmation."""
        p = _owned(ctx, property_id)
        if not p:
            return {"status": "error", "error": f"{property_id} not found"}
        if not confirmed:
            return {"status": "confirmation_required", "saved": False, "property": pf.describe(_to_dict(p)),
                    "instruction": "Ask the user to confirm removing this property. If they only want to see the effect, use what_if."}
        p.status = "Archived"
        _audit(ctx, p.property_id, "archive", {"status": ["Active", "Archived"], "reason": [None, reason]})
        ctx.db.flush()
        return {"status": "archived", "saved": True, "property_id": p.property_id}

    # ---------------- hypotheticals
    class Change(Lenient):
        action: Literal["add", "remove", "modify"]
        property_id: Optional[str] = Field(None, description="For remove/modify")
        fields: Optional[PropertyFields] = Field(None, description="For add (new property) or modify (changed fields)")

    class WhatIfArgs(Lenient):
        changes: list[Change] = Field(..., description="Hypothetical changes to layer on the real portfolio")
        replace_existing: bool = Field(False, description="True to start a fresh scenario instead of adding to the current one")

    def what_if(changes: list, replace_existing: bool = False) -> dict:
        """Hypothetical scenario: 'what if I sell X', 'what if I buy a ₹5 Cr office in Pune', 'what if Andheri gets rented at ₹50L'. Changes are kept on this conversation only and NEVER saved to the portfolio. Returns real vs hypothetical metrics."""
        conv = _conversation(ctx)
        scen = [] if replace_existing else list(conv.scenario or [])
        real_ids = {p["property_id"]: p for p in real_portfolio(ctx)}
        hyp_ids = {c["property"]["property_id"] for c in scen if c["action"] == "add"}
        errors = []
        for ch in changes:
            ch = ch if isinstance(ch, Change) else Change(**ch)
            if ch.action == "add":
                rec, errs, assum = _clean_fields(ctx, ch.fields or PropertyFields(), creating=True)
                _reconcile_rent_occupancy(rec, assum, errs)
                if "current_value_inr" not in rec:
                    errs.append("hypothetical add needs a value")
                if "property_type" not in rec:
                    errs.append("hypothetical add needs a property type")
                if errs:
                    errors.extend(errs)
                    continue
                n = len([c for c in scen if c["action"] == "add"]) + 1
                rec.setdefault("occupancy_status", "Vacant")
                rec.setdefault("annual_rent_inr", 0)
                rec.update(property_id=f"H{n}", user_id=ctx.user_id, hypothetical=True,
                           ownership_percent=rec.get("ownership_percent", 100))
                scen.append({"action": "add", "property": rec})
            else:
                pid = (ch.property_id or "").upper().strip()
                if pid not in real_ids and pid not in hyp_ids:
                    errors.append(f"{pid} not in portfolio")
                    continue
                if ch.action == "remove":
                    scen.append({"action": "remove", "property_id": pid})
                else:
                    rec, errs, assum = _clean_fields(ctx, ch.fields or PropertyFields(), creating=False)
                    base = real_ids.get(pid, {})
                    if rec.get("annual_rent_inr") and "occupancy_status" not in rec:
                        rec["occupancy_status"] = "Tenanted"
                    if rec.get("occupancy_status") in ("Vacant", "Self-occupied"):
                        rec["annual_rent_inr"] = 0
                    if errs:
                        errors.extend(errs)
                        continue
                    if not rec:
                        errors.append(f"no fields to modify for {pid}")
                        continue
                    _ = base
                    scen.append({"action": "modify", "property_id": pid, "fields": rec})
        conv.scenario = scen
        ctx.db.flush()
        real = pf.summary(real_portfolio(ctx))
        hyp_props, notes = hypothetical_portfolio(ctx)
        hyp = pf.summary(hyp_props)

        def delta(k):
            a, b = real.get(k), hyp.get(k)
            return None if a is None or b is None else round(b - a, 2)

        keys = ["count", "total_value_inr", "total_annual_rent_inr", "gross_yield_all_pct",
                "gross_yield_income_producing_pct", "vacancy_rate_by_value_pct"]
        return {
            "data_basis": "HYPOTHETICAL — nothing was saved to the portfolio",
            "active_scenario_changes": scen,
            "errors": errors,
            "real": {k: real.get(k) for k in keys} | {"total_value": real.get("total_value"),
                                                       "by_type": {t: g["share_of_value_pct"] for t, g in real.get("by_type", {}).items()}},
            "hypothetical": {k: hyp.get(k) for k in keys} | {"total_value": hyp.get("total_value"),
                                                              "by_type": {t: g["share_of_value_pct"] for t, g in hyp.get("by_type", {}).items()}},
            "delta": {k: delta(k) for k in keys},
            "warnings": notes,
        }

    def clear_what_if() -> dict:
        """Discard the active hypothetical scenario (back to real data only)."""
        conv = _conversation(ctx)
        n = len(conv.scenario or [])
        conv.scenario = []
        ctx.db.flush()
        return {"status": "cleared", "changes_discarded": n}

    # ---------------- escalation
    class HandoffArgs(Lenient):
        reason: str = Field(..., description="Why a human is needed")
        severity: Literal["low", "medium", "high"] = "medium"

    def request_human_handoff(reason: str, severity: str = "medium") -> dict:
        """Flag this conversation for the business team: user asks for a human, wants advice beyond the data (legal, tax, buy/sell recommendation), is unhappy, or something keeps failing."""
        ctx.event("handoff", severity, reason)
        return {"status": "flagged", "message": "The team has been notified and will follow up."}

    S = StructuredTool.from_function
    return [
        S(search_properties, name="search_properties", args_schema=SearchArgs),
        S(get_property, name="get_property", args_schema=GetArgs),
        S(portfolio_summary, name="portfolio_summary", args_schema=SummaryArgs),
        S(rank_properties, name="rank_properties", args_schema=RankArgs),
        S(compare, name="compare", args_schema=CompareArgs),
        S(check_data_availability, name="check_data_availability", args_schema=CoverageArgs),
        S(create_property, name="create_property", args_schema=CreateArgs),
        S(update_property, name="update_property", args_schema=UpdateArgs),
        S(archive_property, name="archive_property", args_schema=ArchiveArgs),
        S(what_if, name="what_if", args_schema=WhatIfArgs),
        S(clear_what_if, name="clear_what_if"),
        S(request_human_handoff, name="request_human_handoff", args_schema=HandoffArgs),
    ]


WRITE_TOOLS = {"create_property", "update_property", "archive_property"}
