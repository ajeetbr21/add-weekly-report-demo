"""ORM models for every persistent concept in the platform."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.models.base import Base, TimestampMixin, new_uuid, utcnow

EMBED_DIM = get_settings().embedding_dimensions


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)


def _ts(nullable: bool = True) -> Mapped[datetime | None]:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


# ------------------------------------------------------------------------------------------------
# identity / workspace
# ------------------------------------------------------------------------------------------------
class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(32), default="owner", nullable=False)
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_users_source_external"),)


class Project(TimestampMixin, Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)


# ------------------------------------------------------------------------------------------------
# tasks
# ------------------------------------------------------------------------------------------------
class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    session_key: Mapped[str | None] = mapped_column(String(200))
    original_request: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_request: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    selected_agent: Mapped[str | None] = mapped_column(String(64))
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    # session memory: temporary, per-task working state (never auto-promoted to long-term memory)
    session: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = _ts()
    idempotency_key: Mapped[str | None] = mapped_column(String(300), unique=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = _ts()
    completed_at: Mapped[datetime | None] = _ts()

    steps: Mapped[list[TaskStep]] = relationship(back_populates="task", order_by="TaskStep.step_index",
                                                 cascade="all, delete-orphan", lazy="selectin")

    __table_args__ = (
        Index("ix_tasks_status_priority_created", "status", "priority", "created_at"),
        Index("ix_tasks_project_created", "project_id", "created_at"),
        Index("ix_tasks_parent", "parent_task_id"),
        Index("ix_tasks_session_key", "session_key"),
    )


class TaskEvent(Base):
    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), default=utcnow,
                                                nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(64))
    step_index: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    __table_args__ = (Index("ix_task_events_task_id_id", "task_id", "id"),
                      Index("ix_task_events_type_ts", "event_type", "timestamp"))


class TaskStep(TimestampMixin, Base):
    __tablename__ = "task_steps"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    depends_on: Mapped[list[int]] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    # agent loop checkpoint (messages, iteration, pending tool call) for approval resume / crash recovery
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = _ts()
    completed_at: Mapped[datetime | None] = _ts()
    task: Mapped[Task] = relationship(back_populates="steps")
    __table_args__ = (UniqueConstraint("task_id", "step_index", name="uq_task_steps_task_step"),)


# ------------------------------------------------------------------------------------------------
# agents / tools
# ------------------------------------------------------------------------------------------------
class AgentDefinition(TimestampMixin, Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)  # routing hints
    tools: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)     # tool-id glob patterns
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    model_preference: Mapped[str] = mapped_column(String(32), default="default", nullable=False)
    memory_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    step_index: Mapped[int | None] = mapped_column(Integer)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    model: Mapped[str | None] = mapped_column(String(200))
    model_provider: Mapped[str | None] = mapped_column(String(64))
    runtime: Mapped[str | None] = mapped_column(String(64))
    iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = _ts()
    __table_args__ = (Index("ix_agent_runs_task", "task_id"), Index("ix_agent_runs_started", "started_at"))


class ToolDefinition(TimestampMixin, Base):
    __tablename__ = "tools"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)  # "<provider>:<name>"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    toolkit: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    risk_level: Mapped[str] = mapped_column(String(16), default="READ", nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = _ts()


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    step_index: Mapped[int | None] = mapped_column(Integer)
    agent_id: Mapped[str | None] = mapped_column(String(64))
    tool_id: Mapped[str] = mapped_column(String(200), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)  # redacted
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # normalized result
    error: Mapped[str | None] = mapped_column(Text)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id", ondelete="SET NULL"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (Index("ix_tool_exec_task", "task_id"), Index("ix_tool_exec_started", "started_at"))


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    step_index: Mapped[int | None] = mapped_column(Integer)
    agent_id: Mapped[str | None] = mapped_column(String(64))
    tool_id: Mapped[str] = mapped_column(String(200), nullable=False)
    action_summary: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)  # redacted
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    decided_at: Mapped[datetime | None] = _ts()
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decision_channel: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_approvals_status", "status"), Index("ix_approvals_task", "task_id"))


# ------------------------------------------------------------------------------------------------
# memory
# ------------------------------------------------------------------------------------------------
class Memory(TimestampMixin, Base):
    __tablename__ = "memories"
    id: Mapped[uuid.UUID] = _uuid_pk()
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str | None] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    agent_id: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    last_accessed_at: Mapped[datetime | None] = _ts()
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embeddings: Mapped[list[MemoryEmbedding]] = relationship(back_populates="memory", cascade="all, delete-orphan",
                                                             lazy="noload")
    __table_args__ = (
        Index("ix_memories_type_status", "type", "status"),
        Index("ix_memories_project", "project_id"),
        Index("ix_memories_task", "task_id"),
        Index("ix_memories_hash_scope", "content_hash", "project_id"),
    )


class MemoryEmbedding(Base):
    __tablename__ = "memory_embeddings"
    id: Mapped[uuid.UUID] = _uuid_pk()
    memory_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("memories.id", ondelete="CASCADE"), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    memory: Mapped[Memory] = relationship(back_populates="embeddings")
    __table_args__ = (UniqueConstraint("memory_id", "model", name="uq_memory_embeddings_memory_model"),)


class Session(TimestampMixin, Base):
    """Maps a conversation/session key to its runtime state (Letta agent id or local fallback state)."""

    __tablename__ = "sessions"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    runtime: Mapped[str] = mapped_column(String(32), nullable=False, default="local")
    letta_agent_id: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)


# ------------------------------------------------------------------------------------------------
# feedback / learning
# ------------------------------------------------------------------------------------------------
class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="web")
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_feedback_task", "task_id"),)


class LearningCandidate(Base):
    __tablename__ = "learning_candidates"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    feedback_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("feedback.id", ondelete="SET NULL"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    agent_id: Mapped[str | None] = mapped_column(String(64))
    original_feedback: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_lesson: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    domain_tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="CANDIDATE", nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    validation_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    validated_at: Mapped[datetime | None] = _ts()
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    __table_args__ = (Index("ix_learning_candidates_status", "status"),)

    @property
    def success_rate(self) -> float | None:
        total = self.success_count + self.failure_count
        return None if total == 0 else round(self.success_count / total, 3)


class Lesson(TimestampMixin, Base):
    __tablename__ = "lessons"
    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("learning_candidates.id", ondelete="SET NULL"))
    memory_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("memories.id", ondelete="SET NULL"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    domain_tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="APPROVED", nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Skill(TimestampMixin, Base):
    __tablename__ = "skills"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    domain_tags: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    steps: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    source_lesson_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("memories.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(16), default="APPROVED", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


# ------------------------------------------------------------------------------------------------
# reports / schedules / webhooks / system
# ------------------------------------------------------------------------------------------------
class Report(Base):
    __tablename__ = "reports"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    executive_summary: Mapped[str] = mapped_column(Text, nullable=False)
    final_status: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("task_id", name="uq_reports_task_id"),)


class ScheduledTask(TimestampMixin, Base):
    __tablename__ = "scheduled_tasks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    request: Mapped[str] = mapped_column(Text, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(16), nullable=False)
    cron_expression: Mapped[str | None] = mapped_column(String(100))
    run_at: Mapped[datetime | None] = _ts()
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    next_run_at: Mapped[datetime | None] = _ts()
    last_run_at: Mapped[datetime | None] = _ts()
    last_task_id: Mapped[str | None] = mapped_column(String(32))
    run_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    __table_args__ = (Index("ix_scheduled_due", "enabled", "next_run_at"),)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    signature_valid: Mapped[bool | None] = mapped_column(Boolean)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("source", "event_id", name="uq_webhook_events_source_event"),)


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    __table_args__ = (Index("ix_system_events_ts", "timestamp"),)


class AuditLog(Base):
    """Security audit trail: approvals, auth failures, learning decisions, config-affecting actions."""

    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[str | None] = mapped_column(String(300))
    extra: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    __table_args__ = (Index("ix_audit_logs_ts", "timestamp"),)
