"""Tool abstractions shared by all providers (MCP-compatible: name + description + JSON-schema input)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.enums import RiskLevel


@dataclass
class ToolSpec:
    id: str                       # "<provider>:<name>"
    name: str                     # name as known by the provider (MCP tool name)
    provider: str                 # local | composio | ...
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    toolkit: str | None = None    # github | aws | google | workspace | web ...
    capabilities: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.READ
    requires_approval: bool = False

    @property
    def function_name(self) -> str:
        """OpenAI function names must match ^[a-zA-Z0-9_-]{1,64}$."""
        return re.sub(r"[^a-zA-Z0-9_-]", "__", self.id)[:64]

    def to_openai_tool(self) -> dict[str, Any]:
        schema = self.input_schema or {"type": "object", "properties": {}}
        risk_note = f" [risk={self.risk_level}{', requires human approval' if self.requires_approval else ''}]"
        return {"type": "function", "function": {"name": self.function_name,
                                                 "description": (self.description or self.name)[:900] + risk_note,
                                                 "parameters": schema}}


@dataclass
class RawToolResult:
    content: list[dict[str, Any]]           # MCP content blocks as dicts ({"type":"text","text":...})
    structured: Any | None = None
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolProviderUnavailable(Exception):
    """Provider not configured / unreachable. Surfaces as CONFIGURATION_REQUIRED."""


class ToolProvider(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    async def list_tools(self) -> list[ToolSpec]: ...

    async def call_tool(self, spec: ToolSpec, arguments: dict[str, Any]) -> RawToolResult: ...

    async def health(self) -> dict[str, Any]: ...


_DESTRUCTIVE = re.compile(r"(delete|remove|destroy|terminate|drop|purge|wipe|revoke|deregister|detach|stop_instance"
                          r"|reboot|restart|force_push|archive_repo|kill)", re.I)
_WRITE = re.compile(r"(create|update|put|post|send|write|modify|edit|add|set|patch|merge|upload|commit|push|star"
                    r"|fork|invite|assign|comment|start|run|execute|trigger|apply|enable|disable|insert|replace)", re.I)


def classify_risk(name: str, annotations: dict[str, Any] | None = None) -> RiskLevel:
    """Classify tool risk. MCP ToolAnnotations win when present, otherwise name heuristics
    (conservative: anything that looks like a mutation is at least WRITE)."""
    ann = annotations or {}
    if ann.get("destructiveHint") is True and ann.get("readOnlyHint") is not True:
        return RiskLevel.DESTRUCTIVE
    if ann.get("readOnlyHint") is True:
        return RiskLevel.READ
    if _DESTRUCTIVE.search(name):
        return RiskLevel.DESTRUCTIVE
    if _WRITE.search(name):
        return RiskLevel.WRITE
    return RiskLevel.READ


TOOLKIT_CAPABILITIES = {
    "github": ["github", "repository", "code", "pull-request", "issues"],
    "aws": ["aws", "cloud", "infrastructure", "ec2", "rds", "s3", "eks", "cloudwatch", "cost"],
    "google": ["google", "gmail", "calendar", "drive", "docs", "sheets"],
    "web": ["web", "research", "http", "documentation"],
    "workspace": ["files", "workspace", "notes", "code"],
    "system": ["time", "utility"],
}


def infer_toolkit(name: str) -> str | None:
    n = name.lower()
    for prefix, toolkit in (("github", "github"), ("aws", "aws"), ("cloudwatch", "aws"), ("ec2", "aws"),
                            ("gmail", "google"), ("googlecalendar", "google"), ("googledrive", "google"),
                            ("googledocs", "google"), ("googlesheets", "google"), ("google", "google")):
        if n.startswith(prefix):
            return toolkit
    return None
