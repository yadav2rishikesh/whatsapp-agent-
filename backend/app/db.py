"""Database models. SQLite locally, Postgres in production (same code)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        String, Text, create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import DATABASE_URL

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    user_id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    city = Column(String)
    preferences = Column(String)
    preferred_locations = Column(String)
    value_pref_min_inr = Column(Integer)
    value_pref_max_inr = Column(Integer)
    value_pref_raw = Column(String)


class Property(Base):
    __tablename__ = "properties"
    property_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.user_id"), index=True, nullable=False)
    property_type = Column(String, nullable=False)          # normalised: Retail/Office/...
    property_type_raw = Column(String)                       # as given in source
    sub_type = Column(String)
    location_raw = Column(String)
    locality = Column(String)
    city = Column(String, index=True)
    state = Column(String)
    area_sqft = Column(Float)
    current_value_inr = Column(Float)
    purchase_price_inr = Column(Float, nullable=True)
    annual_rent_inr = Column(Float, default=0)
    occupancy_status = Column(String)                        # Tenanted/Vacant/Self-occupied/Unknown
    tenant_status = Column(String)                           # Yes/No, kept in sync with occupancy
    ownership_percent = Column(Float, default=100)
    status = Column(String, default="Active")                # Active/Archived (soft delete)
    source = Column(String, default="seed")                  # seed / chat
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)


class PropertyAudit(Base):
    """Every write the agent makes, so a human can trace and undo it."""
    __tablename__ = "property_audit"
    id = Column(Integer, primary_key=True, autoincrement=True)
    property_id = Column(String, index=True)
    conversation_id = Column(Integer, index=True, nullable=True)
    action = Column(String)            # create / update / archive
    changes = Column(JSON)             # {field: [old, new]}
    created_at = Column(DateTime(timezone=True), default=now)


class Conversation(Base):
    __tablename__ = "conversations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.user_id"), index=True)
    title = Column(String, default="New conversation")
    scenario = Column(JSON, default=list)   # hypothetical changes, never written to properties
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)
    messages = relationship("Message", back_populates="conversation", order_by="Message.id")
    flags = relationship("Flag", back_populates="conversation", order_by="Flag.id")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), index=True)
    role = Column(String)                      # user / assistant
    content = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now)
    # observability (assistant messages only)
    model = Column(String)
    route = Column(String)                     # fast / strong / fallback
    route_reason = Column(String)
    llm_calls = Column(Integer)
    latency_total_ms = Column(Float)
    latency_model_ms = Column(Float)
    latency_tools_ms = Column(Float)
    latency_first_token_ms = Column(Float)
    tokens_in = Column(Integer)
    tokens_out = Column(Integer)
    error = Column(Text)
    # internal LangChain messages of this turn (AI tool calls + tool results),
    # replayed as context on the next turns
    trace = Column(JSON)
    conversation = relationship("Conversation", back_populates="messages")
    tool_calls = relationship("ToolCall", back_populates="message", order_by="ToolCall.id")


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("messages.id"), index=True)
    conversation_id = Column(Integer, index=True)
    step = Column(Integer)
    name = Column(String)
    args = Column(JSON)
    result = Column(JSON)
    duration_ms = Column(Float)
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), default=now)
    message = relationship("Message", back_populates="tool_calls")


class Flag(Base):
    """Something in a conversation a human should look at."""
    __tablename__ = "flags"
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), index=True)
    message_id = Column(Integer, nullable=True)
    kind = Column(String)          # error / handoff / data_gap / unanswered / write_blocked / manual
    severity = Column(String)      # high / medium / low
    note = Column(Text)
    source = Column(String)        # auto / agent / admin
    resolved = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=now)
    conversation = relationship("Conversation", back_populates="flags")


def init_db() -> None:
    Base.metadata.create_all(engine)
