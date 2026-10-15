"""LangGraph agent: one tool-calling agent, timed and logged per step.

Graph:  START -> agent --(tool calls?)--> tools -> agent ... -> END

Every turn is one DB transaction: the user's message, any property writes,
the assistant reply, tool-call logs and flags are committed together. If the
model fails mid-turn, property writes are rolled back and the error is
logged + flagged in a fresh transaction.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Annotated, AsyncIterator, Optional, TypedDict

from langchain_core.messages import (AIMessage, BaseMessage, HumanMessage, SystemMessage,
                                     ToolMessage, messages_from_dict, messages_to_dict)
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI  # imported at startup: ~seconds on first use otherwise
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from . import config
from .db import Conversation, Flag, Message, Property, SessionLocal, ToolCall, User, now
from .prompts import build_system_prompt
from .tools import WRITE_TOOLS, ToolContext, build_tools

log = logging.getLogger("agent")

# ------------------------------------------------------------------ model routing
_STRONG_PATTERNS = re.compile(
    r"\b(what if|what-if|suppose|hypothetical|imagine|scenario|if i (sell|sold|buy|bought|add|rent)|"
    r"compare|comparison|versus|vs\.?|better|worse|should i|recommend|diversif|rebalanc|strategy|"
    r"analy[sz]e|analysis|risk|concentrat|exposure|why)\b", re.I)


def route(text: str, history_turns: int) -> tuple[str, str]:
    """Cheap, deterministic router — no extra LLM call on the hot path.

    fast   = lookups, filters, single add/update (most traffic)
    strong = comparisons, what-ifs, multi-part or reasoning questions
    """
    if _STRONG_PATTERNS.search(text):
        return "strong", "reasoning keyword (compare / what-if / analysis)"
    if len(text) > 220 or text.count("?") > 1 or len(re.findall(r"\band\b", text, re.I)) >= 2:
        return "strong", "long or multi-part request"
    if re.fullmatch(r"\s*(which|what about|and|why|how about)\b.*", text, re.I) and history_turns and len(text) < 40:
        return "strong", "short follow-up that depends on context"
    return "fast", "simple lookup / action"


def make_llm(model: str):
    return ChatOpenAI(
        model=model, api_key=config.OPENROUTER_API_KEY or "missing-key",
        base_url=config.OPENROUTER_BASE_URL, temperature=0.1, timeout=config.LLM_TIMEOUT_S,
        max_tokens=config.MAX_OUTPUT_TOKENS,
        extra_body={"provider": {"sort": config.OPENROUTER_PROVIDER_SORT}} if config.OPENROUTER_PROVIDER_SORT else None,
        max_retries=1, stream_usage=True,
        default_headers={"HTTP-Referer": "https://github.com/portfolio-analyst", "X-Title": "AI Portfolio Analyst"},
    )


# Tests swap this to inject a scripted model.
LLM_FACTORY = make_llm

HANDOFF_RE = re.compile(r"\b(human|real person|someone from|talk to (a|an|your)|call me|agent please|"
                        r"customer care|support team|relationship manager|speak to)\b", re.I)


# ------------------------------------------------------------------ run state
class RunStats:
    def __init__(self):
        self.t0 = time.perf_counter()
        self.model_ms = 0.0
        self.tools_ms = 0.0
        self.first_token_ms: Optional[float] = None
        self.llm_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.models_used: list[str] = []
        self.route = ""
        self.route_reason = ""
        self.tool_log: list[dict] = []
        self.errors: list[str] = []
        # UI events (tool_start/tool_end/status) raised inside graph nodes, drained by
        # the stream loop. Plain list instead of LangGraph's stream writer so it works
        # identically on Python 3.10 (writer needs contextvars propagation, 3.11+).
        self.events: list[dict] = []

    def total_ms(self) -> float:
        return (time.perf_counter() - self.t0) * 1000


class State(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def _short(obj, limit=6000) -> str:
    s = json.dumps(obj, default=str, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + '…"(truncated)"'


# ------------------------------------------------------------------ token budget
def _compact_schema(node):
    """Shrink a JSON schema for the model: drop titles, null-unions and null defaults.

    Pydantic emits `anyOf: [{type: X}, {type: null}]`, `title` and `default: null`
    for every optional field; across 12 tools that is thousands of tokens on every
    call. Validation still uses the full pydantic models, so nulls are accepted.
    """
    if isinstance(node, list):
        return [_compact_schema(x) for x in node]
    if not isinstance(node, dict):
        return node
    if "anyOf" in node:
        opts = [o for o in node["anyOf"] if o.get("type") != "null"]
        if len(opts) > 1 and {"type": "string"} in opts:
            opts = [{"type": "string"}]  # money/area: "₹4.2 crore" or a number both parse from a string
        if len(opts) == 1:
            merged = {**{k: v for k, v in node.items() if k != "anyOf"}, **opts[0]}
            return _compact_schema(merged)
    out = {}
    for k, v in node.items():
        if k == "title" or (k == "default" and v is None):
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _compact_schema(pv) for pk, pv in v.items()}  # keep field names even if 'title'
        else:
            out[k] = _compact_schema(v)
    return out


def tool_schemas(tools) -> list[dict]:
    from langchain_core.utils.function_calling import convert_to_openai_tool
    return [_compact_schema(convert_to_openai_tool(t)) for t in tools]


_DROP_FOR_MODEL = {"value_inr", "annual_rent_inr", "total_value_inr", "total_annual_rent_inr",
                   "non_earning_value_inr", "property_type_raw", "tenant_status", "user_id"}


def _for_model(result, limit=5000) -> str:
    """Tool result as sent to the model: formatted ₹ strings only (raw INR duplicates
    removed; the full result is still logged for the console)."""
    def strip(x):
        if isinstance(x, dict):
            return {k: strip(v) for k, v in x.items()
                    if not (k in _DROP_FOR_MODEL and (k.replace("_inr", "") in x or k in ("property_type_raw", "tenant_status", "user_id")))
                    and v is not None}
        if isinstance(x, list):
            return [strip(i) for i in x]
        return x
    return _short(strip(result), limit)


def build_graph(ctx: ToolContext, stats: RunStats, primary: str, fallback: str):
    tools = build_tools(ctx)
    by_name = {t.name: t for t in tools}
    schemas = tool_schemas(tools)
    llms = {m: LLM_FACTORY(m).bind_tools(schemas) for m in dict.fromkeys([primary, fallback])}

    writer = stats.events.append

    # config is passed explicitly to the model call so token streaming callbacks
    # also work on Python 3.10 (no implicit contextvar propagation there).
    async def agent_node(state: State, config: RunnableConfig):
        last_err = None
        for i, model in enumerate(dict.fromkeys([primary, fallback])):
            t = time.perf_counter()
            try:
                if i:
                    writer({"type": "status", "text": "Retrying with backup model…"})
                msg = await llms[model].ainvoke(state["messages"], config)
                dt = (time.perf_counter() - t) * 1000
                stats.model_ms += dt
                stats.llm_calls += 1
                stats.models_used.append(model)
                if i:
                    stats.route = "fallback"
                    stats.route_reason += f" → fallback after error: {last_err}"
                um = getattr(msg, "usage_metadata", None) or {}
                stats.tokens_in += um.get("input_tokens", 0) or 0
                stats.tokens_out += um.get("output_tokens", 0) or 0
                return {"messages": [msg]}
            except Exception as e:  # network, rate-limit, bad tool JSON, timeout
                stats.model_ms += (time.perf_counter() - t) * 1000
                last_err = f"{type(e).__name__}: {str(e)[:200]}"
                stats.errors.append(f"model {model}: {last_err}")
        raise RuntimeError(f"All models failed: {last_err}")

    def tools_node(state: State):
        ai: AIMessage = state["messages"][-1]
        out = []
        step = sum(1 for m in state["messages"] if isinstance(m, AIMessage))
        for call in ai.tool_calls:
            name, args = call["name"], call.get("args", {})
            writer({"type": "tool_start", "name": name, "args": args})
            t = time.perf_counter()
            err = None
            try:
                if name not in by_name:
                    raise ValueError(f"unknown tool {name}")
                result = by_name[name].invoke(args)
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:300]}"
                if not ctx.db.is_active:
                    # DB session broken -> abort the whole turn (writes get rolled back)
                    raise
                result = {"error": err, "instruction": "Tell the user this step failed; do not invent a result."}
                bad_args = type(e).__name__ in ("ValidationError", "ToolException")
                ctx.event("error", "medium" if bad_args else "high", f"Tool {name} failed: {err}")
            dt = (time.perf_counter() - t) * 1000
            stats.tools_ms += dt
            stats.tool_log.append({"step": step, "name": name, "args": args, "result": result,
                                   "duration_ms": round(dt, 2), "error": err})
            writer({"type": "tool_end", "name": name, "duration_ms": round(dt, 1), "ok": err is None,
                    "saved": bool(isinstance(result, dict) and result.get("saved"))})
            out.append(ToolMessage(content=_for_model(result), tool_call_id=call["id"], name=name))
        return {"messages": out}

    def should_continue(state: State):
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    g = StateGraph(State)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    return g.compile()


# ------------------------------------------------------------------ history
def build_history(conv: Conversation) -> list[BaseMessage]:
    """Last N turns in full (incl. tool calls/results), older turns as text only."""
    msgs = conv.messages
    turns: list[tuple[Message, Optional[Message]]] = []
    i = 0
    while i < len(msgs):
        if msgs[i].role == "user":
            nxt = msgs[i + 1] if i + 1 < len(msgs) and msgs[i + 1].role == "assistant" else None
            turns.append((msgs[i], nxt))
            i += 2 if nxt else 1
        else:
            i += 1
    turns = turns[-(config.FULL_HISTORY_TURNS + config.TEXT_HISTORY_TURNS):]
    out: list[BaseMessage] = []
    for n, (u, a) in enumerate(turns):
        full = n >= len(turns) - config.FULL_HISTORY_TURNS
        out.append(HumanMessage(u.content))
        if a is None:
            continue
        if full and a.trace:
            try:
                msgs = messages_from_dict(a.trace)
                if n < len(turns) - 1:
                    # older turns: keep the tool calls but shorten their results
                    msgs = [ToolMessage(content=m.content[:600] + ("…" if len(m.content) > 600 else ""),
                                        tool_call_id=m.tool_call_id, name=m.name)
                            if isinstance(m, ToolMessage) else m for m in msgs]
                out.extend(msgs)
                continue
            except Exception:
                pass
        out.append(AIMessage(a.content or ""))
    return out


# ------------------------------------------------------------------ public API
async def run_turn(conversation_id: int, text: str) -> AsyncIterator[dict]:
    """Run one user turn. Yields stream events; always ends with 'done' or 'error'."""
    finished = False
    try:
        async for ev in _run_turn(conversation_id, text):
            if ev.get("type") in ("done", "error"):
                finished = True
            yield ev
    except Exception as e:  # anything unexpected (DB down, bug) -> never leave the client hanging
        log.exception("turn failed")
        if not finished:
            _record_crash(conversation_id, text, f"{type(e).__name__}: {str(e)[:300]}")
            yield {"type": "error", "text": "Something went wrong on the server and nothing was changed. Please try again."}


def _record_crash(conversation_id: int, text: str, err: str):
    db = SessionLocal()
    try:
        if db.get(Conversation, conversation_id) is None:
            return
        db.add(Message(conversation_id=conversation_id, role="user", content=text))
        m = Message(conversation_id=conversation_id, role="assistant", error=err,
                    content="Something went wrong on the server and nothing was changed. Please try again.")
        db.add(m)
        db.flush()
        db.add(Flag(conversation_id=conversation_id, message_id=m.id, kind="error", severity="high",
                    note=err, source="auto"))
        db.commit()
    except Exception:
        log.exception("could not record crash")
    finally:
        db.close()


async def _run_turn(conversation_id: int, text: str) -> AsyncIterator[dict]:
    stats = RunStats()
    db = SessionLocal()
    try:
        conv = db.get(Conversation, conversation_id)
        if conv is None:
            yield {"type": "error", "text": "Conversation not found"}
            return
        user = db.get(User, conv.user_id)
        history = build_history(conv)
        user_msg = Message(conversation_id=conv.id, role="user", content=text)
        db.add(user_msg)
        if not conv.messages or conv.title in (None, "", "New conversation"):
            conv.title = (text[:57] + "…") if len(text) > 60 else text
        db.flush()

        props = (db.query(Property).filter(Property.user_id == user.user_id, Property.status == "Active")
                 .order_by(Property.property_id).all())
        system = SystemMessage(build_system_prompt(user, props, conv.scenario or []))
        tier, reason = route(text, len(history))
        primary = config.MODEL_STRONG if tier == "strong" else config.MODEL_FAST
        fallback = config.MODEL_FALLBACK if primary != config.MODEL_FALLBACK else config.MODEL_STRONG
        stats.route, stats.route_reason = tier, reason
        yield {"type": "meta", "route": tier, "model": primary, "reason": reason}

        ctx = ToolContext(db=db, user_id=user.user_id, conversation_id=conv.id)
        graph = build_graph(ctx, stats, primary, fallback)
        start_msgs = [system, *history, HumanMessage(text)]
        final_state_msgs: list[BaseMessage] = []
        error: Optional[str] = None

        try:
            async for mode, payload in graph.astream(
                    {"messages": start_msgs}, stream_mode=["messages", "values"],
                    config={"recursion_limit": config.MAX_AGENT_STEPS * 2 + 1}):
                while stats.events:
                    yield stats.events.pop(0)
                if mode == "messages":
                    chunk, meta = payload
                    if meta.get("langgraph_node") == "agent" and isinstance(chunk.content, str) and chunk.content:
                        if stats.first_token_ms is None:
                            stats.first_token_ms = stats.total_ms()
                        yield {"type": "token", "text": chunk.content}
                elif mode == "values":
                    final_state_msgs = payload["messages"]
        except GraphRecursionError:
            error = f"Stopped after {config.MAX_AGENT_STEPS} reasoning steps"
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:300]}"

        while stats.events:
            yield stats.events.pop(0)

        new_msgs = final_state_msgs[len(start_msgs):]
        final_ai = next((m for m in reversed(new_msgs) if isinstance(m, AIMessage) and not m.tool_calls), None)
        answer = (final_ai.content if final_ai and isinstance(final_ai.content, str) else "") or ""

        if error:
            # Undo any property writes from this failed turn, keep the user's message.
            db.rollback()
            db = _reopen(db)
            conv = db.get(Conversation, conversation_id)
            if conv.title in (None, "", "New conversation"):
                conv.title = (text[:57] + "…") if len(text) > 60 else text
            user_msg = Message(conversation_id=conv.id, role="user", content=text)
            db.add(user_msg)
            answer = ("Sorry — I couldn't complete that just now, and nothing was changed in your portfolio. "
                      "I've flagged it for the team. Please try again in a moment.")
            ctx.events = [{"kind": "error", "severity": "high", "note": error}] + \
                [e for e in ctx.events if e["kind"] == "handoff"]
            stats.tool_log = [t for t in stats.tool_log if t["name"] not in WRITE_TOOLS]
        elif not answer.strip():
            answer = "I wasn't able to put an answer together for that. Could you rephrase it?"
            ctx.event("unanswered", "medium", "Model returned no final answer")

        # Deterministic safety net: explicit request for a human always flags,
        # even if the model forgot to call request_human_handoff.
        if HANDOFF_RE.search(text) and not any(e["kind"] == "handoff" for e in ctx.events):
            ctx.event("handoff", "medium", "User asked for a human (keyword match)")

        a = Message(
            conversation_id=conv.id, role="assistant", content=answer,
            model=", ".join(dict.fromkeys(stats.models_used)) or None, route=stats.route,
            route_reason=stats.route_reason, llm_calls=stats.llm_calls,
            latency_total_ms=round(stats.total_ms(), 1), latency_model_ms=round(stats.model_ms, 1),
            latency_tools_ms=round(stats.tools_ms, 2),
            latency_first_token_ms=round(stats.first_token_ms, 1) if stats.first_token_ms else None,
            tokens_in=stats.tokens_in, tokens_out=stats.tokens_out,
            error="; ".join(stats.errors + ([error] if error else [])) or None,
            trace=None if error else messages_to_dict(new_msgs),
        )
        db.add(a)
        db.flush()
        for t in stats.tool_log:
            db.add(ToolCall(message_id=a.id, conversation_id=conv.id, step=t["step"], name=t["name"],
                            args=json.loads(json.dumps(t["args"], default=str)),
                            result=json.loads(json.dumps(t["result"], default=str)),
                            duration_ms=t["duration_ms"], error=t["error"]))
        if stats.errors and not error:
            ctx.event("error", "low", "Primary model failed, fallback answered: " + "; ".join(stats.errors))
        seen = set()
        for e in ctx.events:
            key = (e["kind"], e["note"])
            if key in seen:
                continue
            seen.add(key)
            db.add(Flag(conversation_id=conv.id, message_id=a.id, kind=e["kind"], severity=e["severity"],
                        note=e["note"], source="agent" if e["kind"] == "handoff" else "auto"))
        conv.updated_at = now()
        db.commit()
        yield {"type": "done", "message": serialize_message(a, with_tools=True),
               "user_message_id": user_msg.id}
    finally:
        db.close()


def _reopen(db):
    db.close()
    return SessionLocal()


def serialize_message(m: Message, with_tools: bool = False) -> dict:
    d = {
        "id": m.id, "role": m.role, "content": m.content,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }
    if m.role == "assistant":
        d.update(model=m.model, route=m.route, route_reason=m.route_reason, llm_calls=m.llm_calls,
                 latency_total_ms=m.latency_total_ms, latency_model_ms=m.latency_model_ms,
                 latency_tools_ms=m.latency_tools_ms, latency_first_token_ms=m.latency_first_token_ms,
                 tokens_in=m.tokens_in, tokens_out=m.tokens_out, error=m.error)
        tcs = m.tool_calls
        d["hypothetical"] = any(
            t.name == "what_if" or (isinstance(t.result, dict) and str(t.result.get("data_basis", "")).startswith("HYPOTHETICAL"))
            for t in tcs)
        d["saved"] = [t.result.get("property", {}).get("property_id") or t.result.get("property_id")
                      for t in tcs if isinstance(t.result, dict) and t.result.get("saved")]
        d["steps"] = [t.name for t in tcs]
        if with_tools:
            d["tool_calls"] = [{"id": t.id, "step": t.step, "name": t.name, "args": t.args, "result": t.result,
                                "duration_ms": t.duration_ms, "error": t.error} for t in m.tool_calls]
    return d
