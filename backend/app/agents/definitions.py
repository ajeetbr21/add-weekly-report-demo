"""Built-in agent definitions. They are seeded into the `agents` table on startup; after that the
database row is authoritative (instructions/tools/status can be edited through the API).

To add an agent: append an AgentSpec here (or POST it via the API). The orchestrator discovers agents
through the registry and never needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

COMMON_RULES = """
Operating rules:
- Work only on the objective you were given. Do not start unrelated work.
- Use tools when they provide real data. Never invent tool output, resource names, metrics or URLs.
- If a needed integration is not configured, say so explicitly ("CONFIGURATION REQUIRED: ...").
- Destructive actions require human approval; the platform will pause and ask. Prefer read-only steps first.
- Apply the lessons and procedures provided under "Relevant memory" when they fit the situation.
- When finished, reply with ONLY a JSON object:
  {"summary": str, "findings": [str], "actions_taken": [str], "recommendations": [str],
   "facts": [str], "confidence": number between 0 and 1}
  "facts" are durable, verified facts worth remembering long-term (may be empty).
""".strip()


@dataclass
class AgentSpec:
    id: str
    name: str
    description: str
    capabilities: list[str]
    keywords: list[str]
    tools: list[str]
    instructions: str
    model_preference: str = "default"
    memory_policy: dict[str, Any] = field(default_factory=dict)


RESEARCH = AgentSpec(
    id="research",
    name="Research Agent",
    description="Researches topics and documentation, extracts information, collects evidence and writes "
                "structured summaries.",
    capabilities=["research", "documentation analysis", "information extraction", "structured summaries",
                  "evidence collection"],
    keywords=["research", "documentation", "docs", "doc", "explain", "compare", "article", "articles",
              "evidence", "learn", "practice", "practices", "guide", "guidance", "url", "http", "https",
              "information", "reference", "sources", "paper", "whitepaper", "investigate-docs"],
    tools=["local:http_fetch", "local:get_current_time", "local:read_workspace_file", "local:search_workspace",
           "local:list_workspace_files", "composio:*SEARCH*", "composio:GOOGLEDOCS_*", "composio:GOOGLEDRIVE_*"],
    instructions=f"""You are the Research Agent of Atlas, a local multi-agent platform.
You research questions using documentation and web sources, extract the relevant facts, and produce
structured, evidence-backed summaries. Cite the URLs/documents you used in findings.
{COMMON_RULES}""",
    model_preference="reasoning",
    memory_policy={"retrieve_types": ["SEMANTIC", "EVIDENCE", "PROCEDURAL", "EPISODIC"], "top_k": 6,
                   "tags": ["research"]},
)

CODING = AgentSpec(
    id="coding",
    name="Coding Agent",
    description="Analyzes repositories, writes and modifies code, debugs, runs tests, reviews code and "
                "drives GitHub workflows.",
    capabilities=["repository analysis", "code generation", "code modification", "debugging", "test execution",
                  "code review", "github workflows"],
    keywords=["code", "coding", "repo", "repository", "bug", "debug", "test", "tests", "pytest", "pull", "pr",
              "github", "function", "refactor", "python", "typescript", "javascript", "review", "commit", "branch",
              "merge", "issue", "compile", "build", "lint", "stacktrace", "exception", "error", "api"],
    tools=["local:list_workspace_files", "local:read_workspace_file", "local:search_workspace",
           "local:git_repo_summary", "local:write_workspace_note", "local:run_pytest", "local:delete_workspace_file",
           "composio:GITHUB_*"],
    instructions=f"""You are the Coding Agent of Atlas.
You analyze repositories, explain and debug code, propose or make code changes, run tests and handle
GitHub workflows (issues, pull requests, reviews) through the available tools. Code changes to the Atlas
platform itself must go through a normal development workflow (branch + pull request), never directly.
{COMMON_RULES}""",
    model_preference="coding",
    memory_policy={"retrieve_types": ["PROCEDURAL", "SEMANTIC", "EPISODIC"], "top_k": 6, "tags": ["coding"]},
)

AWS_DEVOPS = AgentSpec(
    id="aws_devops",
    name="AWS / DevOps Agent",
    description="Investigates AWS infrastructure (EC2, RDS, S3, EKS, CloudWatch), troubleshoots incidents, "
                "analyzes cost and runs operational workflows.",
    capabilities=["aws investigation", "ec2", "rds", "s3", "eks", "cloudwatch", "infrastructure troubleshooting",
                  "cost analysis", "operational workflows"],
    keywords=["aws", "ec2", "rds", "s3", "eks", "cloudwatch", "alarm", "alarms", "lambda", "infrastructure",
              "incident", "outage", "cost", "billing", "kubernetes", "k8s", "server", "instance", "instances",
              "restart", "deploy", "deployment", "latency", "cpu", "memory", "disk", "iam", "vpc", "devops",
              "database", "cluster", "logs", "metrics", "ops", "downtime", "load", "balancer", "elb", "ecs"],
    tools=["composio:AWS*", "composio:COMPOSIO_*", "local:get_current_time", "local:http_fetch"],
    instructions=f"""You are the AWS / DevOps Agent of Atlas.
You investigate AWS resources and incidents (EC2, RDS, S3, EKS, CloudWatch alarms/metrics/logs), analyze
cost, and run operational workflows. Always gather read-only evidence (status, alarms, recent events, metrics)
before proposing any change. Restarts, deletions, scaling and other mutations are high-risk and need approval.
{COMMON_RULES}""",
    model_preference="reasoning",
    memory_policy={"retrieve_types": ["PROCEDURAL", "EPISODIC", "SEMANTIC", "EVIDENCE"], "top_k": 6,
                   "tags": ["aws", "devops"]},
)

GENERAL = AgentSpec(
    id="general",
    name="General Automation Agent",
    description="Handles generic automation, multi-step tasks, tool execution and coordination when no "
                "specialist fits.",
    capabilities=["generic automation", "multi-step tasks", "tool execution", "task coordination"],
    keywords=["automate", "automation", "workflow", "schedule", "remind", "email", "calendar", "note", "notes",
              "file", "files", "time", "date", "list", "organize", "send", "save", "delete", "cleanup"],
    tools=["local:*", "composio:*"],
    instructions=f"""You are the General Automation Agent of Atlas.
You handle general requests and multi-step automation using whatever tools are available.
{COMMON_RULES}""",
    model_preference="default",
    memory_policy={"retrieve_types": ["SEMANTIC", "PROCEDURAL", "EPISODIC", "CORE"], "top_k": 6, "tags": []},
)

BUILTIN_AGENTS: list[AgentSpec] = [RESEARCH, CODING, AWS_DEVOPS, GENERAL]
FALLBACK_AGENT_ID = "general"
