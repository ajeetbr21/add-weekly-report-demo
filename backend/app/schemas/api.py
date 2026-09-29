"""Request/response models for the HTTP API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core.enums import FeedbackRating, ScheduleType, TaskSource
from app.schemas.common import ORMModel


# ---------------------------------------------------------------- tasks
class TaskCreate(BaseModel):
    request: str = Field(min_length=1, max_length=20000, description="Natural-language task request")
    source: TaskSource = TaskSource.API
    project: str | None = Field(default=None, description="Project slug; auto-detected when omitted")
    user_external_id: str | None = Field(default=None, max_length=200)
    user_display_name: str | None = Field(default=None, max_length=200)
    priority: int = Field(default=5, ge=1, le=9, description="1 = most urgent, 9 = least")
    parent_task_id: str | None = None
    session_key: str | None = Field(default=None, max_length=200)
    idempotency_key: str | None = Field(default=None, max_length=300)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("request")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("request must not be blank")
        return v


class TaskStepOut(ORMModel):
    step_index: int
    agent_id: str
    objective: str
    depends_on: list[int]
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    attempts: int
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TaskOut(ORMModel):
    id: str
    parent_task_id: str | None = None
    project_id: uuid.UUID | None = None
    source: str
    user_id: uuid.UUID | None = None
    session_key: str | None = None
    original_request: str
    normalized_request: str | None = None
    status: str
    priority: int
    selected_agent: str | None = None
    plan: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    retry_count: int
    current_step: int
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class TaskDetail(TaskOut):
    steps: list[TaskStepOut] = []
    session: dict[str, Any] = {}


class TaskCreated(BaseModel):
    task: TaskOut
    created: bool = True


class TaskEventOut(ORMModel):
    id: int
    task_id: str
    timestamp: datetime
    event_type: str
    agent_id: str | None = None
    step_index: int | None = None
    message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias="extra")


# ---------------------------------------------------------------- agents
class AgentOut(ORMModel):
    id: str
    name: str
    description: str
    capabilities: list[str]
    keywords: list[str]
    tools: list[str]
    instructions: str
    model_preference: str
    memory_policy: dict[str, Any]
    status: str
    updated_at: datetime


class AgentUpdate(BaseModel):
    description: str | None = None
    instructions: str | None = Field(default=None, max_length=20000)
    capabilities: list[str] | None = None
    keywords: list[str] | None = None
    tools: list[str] | None = None
    model_preference: str | None = Field(default=None, pattern="^(default|fast|reasoning|coding)$")
    status: str | None = Field(default=None, pattern="^(ACTIVE|DISABLED)$")


class AgentRunOut(ORMModel):
    id: uuid.UUID
    task_id: str
    step_index: int | None
    agent_id: str
    status: str
    model: str | None
    model_provider: str | None
    runtime: str | None
    iterations: int
    prompt_tokens: int
    completion_tokens: int
    output_summary: str | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None


# ---------------------------------------------------------------- tools / approvals
class ToolOut(ORMModel):
    id: str
    name: str
    provider: str
    toolkit: str | None
    description: str
    capabilities: list[str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    risk_level: str
    requires_approval: bool
    enabled: bool
    last_seen_at: datetime | None


class ToolExecutionOut(ORMModel):
    id: uuid.UUID
    task_id: str
    step_index: int | None
    agent_id: str | None
    tool_id: str
    arguments: dict[str, Any]
    risk_level: str
    status: str
    result: dict[str, Any] | None
    error: str | None
    approval_id: uuid.UUID | None
    started_at: datetime
    duration_ms: int | None


class ApprovalOut(ORMModel):
    id: uuid.UUID
    task_id: str
    step_index: int | None
    agent_id: str | None
    tool_id: str
    action_summary: str
    risk_level: str
    arguments: dict[str, Any]
    status: str
    requested_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    decision_channel: str | None
    reason: str | None


class ApprovalDecision(BaseModel):
    approve: bool
    decided_by: str = Field(default="web-user", max_length=200)
    channel: str = Field(default="web", max_length=32)
    reason: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------- memory
class MemoryCreate(BaseModel):
    type: str = Field(pattern="^(CORE|SEMANTIC|EPISODIC|PROCEDURAL|EVIDENCE)$")
    content: str = Field(min_length=3, max_length=20000)
    title: str | None = Field(default=None, max_length=300)
    project: str | None = None
    agent_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0, le=1)
    importance: float = Field(default=0.6, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryOut(ORMModel):
    id: uuid.UUID
    type: str
    title: str | None
    content: str
    source: str
    task_id: str | None
    project_id: uuid.UUID | None
    agent_id: str | None
    tags: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias="extra")
    confidence: float
    importance: float
    status: str
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None
    usage_count: int


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    project: str | None = None
    types: list[str] | None = None
    agent_id: str | None = None
    limit: int = Field(default=8, ge=1, le=50)


class MemorySearchHit(BaseModel):
    memory: MemoryOut
    score: float
    similarity: float


class MemoryUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(ACTIVE|SUPERSEDED|DEPRECATED|ARCHIVED)$")
    importance: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)


# ---------------------------------------------------------------- feedback / learning
class FeedbackCreate(BaseModel):
    task_id: str
    content: str = Field(min_length=1, max_length=5000)
    rating: FeedbackRating | None = None
    channel: str = Field(default="web", max_length=32)
    user_external_id: str | None = None


class FeedbackOut(ORMModel):
    id: uuid.UUID
    task_id: str
    channel: str
    rating: str
    content: str
    processed: bool
    created_at: datetime


class LearningCandidateOut(ORMModel):
    id: uuid.UUID
    task_id: str | None
    feedback_id: uuid.UUID | None
    project_id: uuid.UUID | None
    agent_id: str | None
    original_feedback: str
    normalized_lesson: str
    category: str
    domain_tags: list[str]
    confidence: float
    status: str
    requires_approval: bool
    validation_notes: str | None
    created_at: datetime
    validated_at: datetime | None
    usage_count: int
    success_count: int
    failure_count: int
    success_rate: float | None


class FeedbackResult(BaseModel):
    feedback: FeedbackOut
    candidates: list[LearningCandidateOut]


class LearningDecision(BaseModel):
    approve: bool
    decided_by: str = "web-user"
    edited_lesson: str | None = Field(default=None, max_length=5000)
    reason: str | None = None


class LessonOut(ORMModel):
    id: uuid.UUID
    candidate_id: uuid.UUID | None
    memory_id: uuid.UUID | None
    project_id: uuid.UUID | None
    title: str
    content: str
    category: str
    domain_tags: list[str]
    status: str
    usage_count: int
    success_count: int
    failure_count: int
    created_at: datetime


class SkillCreate(BaseModel):
    name: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=3, max_length=5000)
    domain_tags: list[str] = Field(default_factory=list)
    steps: list[str] = Field(min_length=1)
    source_lesson_ids: list[uuid.UUID] = Field(default_factory=list)


class SkillOut(ORMModel):
    id: uuid.UUID
    name: str
    description: str
    domain_tags: list[str]
    steps: list[str]
    source_lesson_ids: list[str]
    memory_id: uuid.UUID | None
    status: str
    version: int
    usage_count: int
    success_count: int
    failure_count: int
    created_at: datetime


# ---------------------------------------------------------------- reports
class ReportOut(ORMModel):
    id: uuid.UUID
    task_id: str
    project_id: uuid.UUID | None
    title: str
    objective: str
    executive_summary: str
    final_status: str
    content: dict[str, Any]
    markdown: str
    created_at: datetime


# ---------------------------------------------------------------- schedules
class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    request: str = Field(min_length=1, max_length=20000)
    schedule_type: ScheduleType
    cron_expression: str | None = Field(default=None, max_length=100)
    run_at: datetime | None = Field(default=None, description="ONCE: fire time; DAILY/WEEKLY/MONTHLY: anchor time")
    timezone: str = "UTC"
    project: str | None = None
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    enabled: bool | None = None
    request: str | None = None
    name: str | None = None


class ScheduleOut(ORMModel):
    id: uuid.UUID
    name: str
    project_id: uuid.UUID | None
    request: str
    schedule_type: str
    cron_expression: str | None
    run_at: datetime | None
    timezone: str
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_task_id: str | None
    run_count: int
    created_at: datetime


# ---------------------------------------------------------------- projects / webhooks
class ProjectCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,98}$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    keywords: list[str] = Field(default_factory=list)


class ProjectOut(ORMModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    keywords: list[str]
    created_at: datetime


class CustomWebhook(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    request: str = Field(min_length=1, max_length=20000)
    event_type: str | None = Field(default="custom", max_length=100)
    project: str | None = None
    priority: int = Field(default=5, ge=1, le=9)
    payload: dict[str, Any] = Field(default_factory=dict)


class WebhookAccepted(BaseModel):
    event_id: str
    task_id: str | None
    duplicate: bool
