"""FastAPI app: chat API (SSE streaming) + business/admin API."""
from __future__ import annotations

import json
import re
import statistics
from contextlib import asynccontextmanager
from collections import Counter
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func

from . import config
from .agent import run_turn, serialize_message
from .db import (Conversation, Flag, Message, Property, PropertyAudit, SessionLocal, ToolCall,
                 User)
from .normalize import fmt_inr
from .portfolio import describe
from .seed import seed
from .tools import _to_dict

@asynccontextmanager
async def lifespan(_app):
    seed()
    yield


app = FastAPI(title="AI Portfolio Analyst", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"],
                   allow_headers=["*"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def admin_auth(x_admin_token: Optional[str] = Header(None)):
    if config.ADMIN_TOKEN and x_admin_token != config.ADMIN_TOKEN:
        raise HTTPException(401, "Admin token required")


@app.get("/api/health")
def health():
    return {"ok": True, "llm_configured": bool(config.OPENROUTER_API_KEY),
            "models": {"fast": config.MODEL_FAST, "strong": config.MODEL_STRONG, "fallback": config.MODEL_FALLBACK}}


# ================================================================ chat side
def _active_props(db, uid):
    return (db.query(Property).filter(Property.user_id == uid, Property.status == "Active")
            .order_by(Property.property_id).all())


@app.get("/api/users")
def list_users(db=Depends(get_db)):
    out = []
    for u in db.query(User).order_by(User.user_id).all():
        props = _active_props(db, u.user_id)
        total = sum((p.current_value_inr or 0) * (p.ownership_percent or 100) / 100 for p in props)
        out.append({"user_id": u.user_id, "name": u.name, "city": u.city, "preferences": u.preferences,
                    "property_count": len(props), "total_value": fmt_inr(total)})
    return out


@app.get("/api/users/{user_id}/portfolio")
def user_portfolio(user_id: str, db=Depends(get_db)):
    if not db.get(User, user_id):
        raise HTTPException(404, "User not found")
    return [describe(_to_dict(p)) for p in _active_props(db, user_id)]


@app.get("/api/users/{user_id}/conversations")
def user_conversations(user_id: str, db=Depends(get_db)):
    convs = (db.query(Conversation).filter(Conversation.user_id == user_id)
             .order_by(Conversation.updated_at.desc()).all())
    return [{"id": c.id, "title": c.title, "updated_at": c.updated_at.isoformat(),
             "last_message": (re.sub(r"[*_`#|>]+", "", c.messages[-1].content or "")[:80] if c.messages else None)} for c in convs]


class NewConversation(BaseModel):
    user_id: str


@app.post("/api/conversations")
def create_conversation(body: NewConversation, db=Depends(get_db)):
    if not db.get(User, body.user_id):
        raise HTTPException(404, "User not found")
    c = Conversation(user_id=body.user_id, scenario=[])
    db.add(c)
    db.commit()
    return {"id": c.id, "title": c.title}


@app.get("/api/conversations/{cid}")
def get_conversation(cid: int, db=Depends(get_db)):
    c = db.get(Conversation, cid)
    if not c:
        raise HTTPException(404, "Conversation not found")
    return {"id": c.id, "user_id": c.user_id, "title": c.title, "scenario_active": bool(c.scenario),
            "messages": [serialize_message(m) for m in c.messages]}


class ChatIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


@app.post("/api/conversations/{cid}/messages")
async def send_message(cid: int, body: ChatIn):
    """Server-Sent Events: meta, status, tool_start, tool_end, token, done."""
    async def gen():
        async for ev in run_turn(cid, body.text.strip()):
            yield f"data: {json.dumps(ev, default=str, ensure_ascii=False)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/conversations/{cid}/messages/sync")
async def send_message_sync(cid: int, body: ChatIn):
    """Same as above but returns one JSON response (handy for curl/tests)."""
    final = None
    async for ev in run_turn(cid, body.text.strip()):
        if ev["type"] in ("done", "error"):
            final = ev
    return final


# ================================================================ business side
@app.get("/api/admin/overview", dependencies=[Depends(admin_auth)])
def admin_overview(db=Depends(get_db)):
    am = db.query(Message).filter(Message.role == "assistant").all()
    lat = sorted(m.latency_total_ms for m in am if m.latency_total_ms)
    model_lat = [m.latency_model_ms for m in am if m.latency_model_ms]
    tool_lat = [m.latency_tools_ms for m in am if m.latency_tools_ms is not None]

    def p(q):
        return round(lat[min(len(lat) - 1, int(q * len(lat)))]) if lat else None
    tools = Counter(n for (n,) in db.query(ToolCall.name).all())
    return {
        "users": db.query(User).count(),
        "conversations": db.query(Conversation).count(),
        "messages": db.query(Message).count(),
        "open_flags": db.query(Flag).filter(Flag.resolved.is_(False)).count(),
        "flags_by_kind": dict(db.query(Flag.kind, func.count()).filter(Flag.resolved.is_(False)).group_by(Flag.kind).all()),
        "latency_ms": {"p50": p(0.5), "p95": p(0.95),
                       "avg_model": round(statistics.mean(model_lat)) if model_lat else None,
                       "avg_tools": round(statistics.mean(tool_lat), 1) if tool_lat else None},
        "routes": dict(Counter(m.route for m in am if m.route)),
        "tool_usage": dict(tools.most_common()),
        "errors": sum(1 for m in am if m.error),
    }


@app.get("/api/admin/users", dependencies=[Depends(admin_auth)])
def admin_users(db=Depends(get_db)):
    out = []
    for u in db.query(User).order_by(User.user_id).all():
        convs = db.query(Conversation).filter(Conversation.user_id == u.user_id).all()
        open_flags = sum(1 for c in convs for f in c.flags if not f.resolved)
        last = max((c.updated_at for c in convs), default=None)
        out.append({"user_id": u.user_id, "name": u.name, "city": u.city, "preferences": u.preferences,
                    "conversations": len(convs), "open_flags": open_flags,
                    "last_active": last.isoformat() if last else None,
                    "property_count": len(_active_props(db, u.user_id))})
    return out


@app.get("/api/admin/conversations", dependencies=[Depends(admin_auth)])
def admin_conversations(user_id: Optional[str] = None, flagged: Optional[bool] = None, db=Depends(get_db)):
    q = db.query(Conversation)
    if user_id:
        q = q.filter(Conversation.user_id == user_id)
    out = []
    names = {u.user_id: u.name for u in db.query(User).all()}
    for c in q.order_by(Conversation.updated_at.desc()).all():
        open_flags = [f for f in c.flags if not f.resolved]
        if flagged is True and not open_flags:
            continue
        if flagged is False and open_flags:
            continue
        asst = [m for m in c.messages if m.role == "assistant"]
        out.append({
            "id": c.id, "user_id": c.user_id, "user_name": names.get(c.user_id), "title": c.title,
            "messages": len(c.messages), "updated_at": c.updated_at.isoformat(),
            "open_flags": [{"id": f.id, "kind": f.kind, "severity": f.severity} for f in open_flags],
            "errors": sum(1 for m in asst if m.error),
            "avg_latency_ms": round(statistics.mean([m.latency_total_ms for m in asst if m.latency_total_ms])) if asst else None,
            "scenario_active": bool(c.scenario),
        })
    return out


@app.get("/api/admin/conversations/{cid}", dependencies=[Depends(admin_auth)])
def admin_conversation(cid: int, db=Depends(get_db)):
    c = db.get(Conversation, cid)
    if not c:
        raise HTTPException(404, "Conversation not found")
    u = db.get(User, c.user_id)
    audits = db.query(PropertyAudit).filter(PropertyAudit.conversation_id == cid).order_by(PropertyAudit.id).all()
    return {
        "id": c.id, "title": c.title, "user": {"user_id": u.user_id, "name": u.name, "city": u.city},
        "scenario": c.scenario or [],
        "messages": [serialize_message(m, with_tools=True) for m in c.messages],
        "flags": [{"id": f.id, "message_id": f.message_id, "kind": f.kind, "severity": f.severity, "note": f.note,
                   "source": f.source, "resolved": f.resolved, "created_at": f.created_at.isoformat()} for f in c.flags],
        "property_changes": [{"property_id": a.property_id, "action": a.action, "changes": a.changes,
                              "created_at": a.created_at.isoformat()} for a in audits],
    }


class FlagIn(BaseModel):
    note: str
    severity: str = "medium"
    message_id: Optional[int] = None


@app.post("/api/admin/conversations/{cid}/flags", dependencies=[Depends(admin_auth)])
def add_flag(cid: int, body: FlagIn, db=Depends(get_db)):
    if not db.get(Conversation, cid):
        raise HTTPException(404, "Conversation not found")
    f = Flag(conversation_id=cid, message_id=body.message_id, kind="manual", severity=body.severity,
             note=body.note, source="admin")
    db.add(f)
    db.commit()
    return {"id": f.id}


@app.post("/api/admin/flags/{fid}/resolve", dependencies=[Depends(admin_auth)])
def resolve_flag(fid: int, db=Depends(get_db)):
    f = db.get(Flag, fid)
    if not f:
        raise HTTPException(404, "Flag not found")
    f.resolved = True
    db.commit()
    return {"ok": True}

