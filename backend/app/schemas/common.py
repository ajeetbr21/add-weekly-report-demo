from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    details: object | None = None
    correlation_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class NormalizedResult(BaseModel):
    """Provider-independent tool/agent result format."""

    status: str  # success | error | configuration_required | rejected | waiting_approval
    summary: str
    data: object | None = None
    evidence: list[dict] = []
    errors: list[str] = []
    metadata: dict = {}
