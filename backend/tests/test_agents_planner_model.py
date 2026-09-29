from __future__ import annotations

import httpx
import pytest

from app.agents.registry import AgentRegistry
from app.core.errors import ConfigurationRequiredError
from app.database.session import session_scope
from app.integrations.omniroute.client import ModelClient
from app.memory.context import MemoryItem
from app.orchestrator.planner import Planner
from tests.helpers import ScriptedGateway, final, tool_call


async def test_registry_lists_builtin_agents(client):
    agents = (await client.get("/api/agents")).json()
    assert {a["id"] for a in agents} >= {"research", "coding", "aws_devops", "general"}
    r = await client.patch("/api/agents/research", json={"status": "DISABLED"})
    assert r.json()["status"] == "DISABLED"
    async with session_scope() as s:
        assert "research" not in {a.id for a in await AgentRegistry(s).list()}


@pytest.mark.parametrize("request_text,expected", [
    ("Analyze today's AWS alarms and summarize anything that needs attention", "aws_devops"),
    ("Debug the failing pytest in the repository and open a pull request", "coding"),
    ("Research the documentation for PostgreSQL vacuum best practices", "research"),
    ("Organize my notes", "general"),
    # words inside paths / URLs are not intent ("restart" here must not route to the AWS agent)
    ("Delete the file notes/restart-demo.txt from the workspace", "general"),
    ("Research the documentation at https://example.com/aws/ec2/restart and summarize it", "research"),
])
async def test_routing(request_text, expected):
    async with session_scope() as s:
        scores = await AgentRegistry(s).route(request_text)
        plan, meta = await Planner(AgentRegistry(s), ModelClient()).plan(request_text, [])
    assert plan.steps[0].agent_id == expected, scores
    assert meta["offline"] is True and plan.planner == "heuristic"


async def test_complex_multi_agent_plan_heuristic():
    req = "Investigate this AWS EC2 problem, research the documentation, and prepare a report."
    async with session_scope() as s:
        plan, _ = await Planner(AgentRegistry(s), ModelClient()).plan(req, [
            MemoryItem(id="1", type="PROCEDURAL", title=None, content="Check CloudWatch first", score=0.9)])
    assert plan.complexity == "complex"
    assert [st.agent_id for st in plan.steps][:2] == ["aws_devops", "research"]
    assert plan.steps[1].depends_on == [0] and plan.needs_report
    assert any("CloudWatch" in g for g in plan.guidance)


async def test_llm_plan_used_and_validated():
    gw = ScriptedGateway(lambda body: {"role": "assistant", "content":
                                       '{"complexity":"complex","needs_report":true,"rationale":"r","steps":['
                                       '{"agent_id":"aws_devops","objective":"look","depends_on":[]},'
                                       '{"agent_id":"research","objective":"docs","depends_on":[0, 5]}]}'})
    async with session_scope() as s:
        plan, meta = await Planner(AgentRegistry(s), gw.client()).plan("aws issue", [])
    assert plan.planner == "llm" and meta["model"] == "test-reasoning"
    assert plan.steps[1].depends_on == [0]  # invalid forward dependency dropped
    bad = ScriptedGateway(lambda b: {"role": "assistant", "content": '{"steps":[{"agent_id":"hacker","objective":"x"}]}'})
    async with session_scope() as s:
        plan, meta = await Planner(AgentRegistry(s), bad.client()).plan("aws alarms", [])
    assert plan.planner == "heuristic" and meta.get("llm_plan_rejected")


async def test_model_client_routing_and_tool_calls():
    gw = ScriptedGateway(lambda body: tool_call("local__get_current_time", {"timezone": "UTC"}))
    mc = gw.client(omniroute_api_key="k-123")
    resp = await mc.chat([{"role": "user", "content": "hi"}], tier="coding",
                         tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}])
    assert gw.requests[0]["body"]["model"] == "test-coding"
    assert gw.requests[0]["auth"] == "Bearer k-123"
    assert gw.requests[0]["path"] == "/v1/chat/completions"
    assert resp.tool_calls[0].name == "local__get_current_time" and resp.tool_calls[0].arguments == {"timezone": "UTC"}
    assert resp.usage == {"prompt_tokens": 11, "completion_tokens": 7} and not resp.offline
    health = await mc.health()
    assert health["status"] == "OK" and health["routing"]["fast"] == "test-fast"
    resp2 = await gw.client().chat([{"role": "user", "content": "x"}])
    assert gw.requests[-1]["body"]["model"] == "test-default"
    assert resp2.tool_calls


async def test_model_client_offline_and_error_fallback(settings):
    mc = ModelClient()
    r = await mc.chat([{"role": "user", "content": "x"}])
    assert r.offline and "not enabled" in r.fallback_reason
    s = settings.model_copy(update={"llm_offline_fallback": False})
    with pytest.raises(ConfigurationRequiredError):
        await ModelClient(settings=s).chat([{"role": "user", "content": "x"}])

    def boom(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "no provider configured"})

    s2 = settings.model_copy(update={"omniroute_enabled": True, "omniroute_base_url": "http://gw.test/v1"})
    calls = {"n": 0}

    def counting(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return boom(req)

    mc = ModelClient(settings=s2, transport=httpx.MockTransport(counting))
    r = await mc.chat([{"role": "user", "content": "x"}])
    assert r.offline and "HTTP 400" in r.fallback_reason and calls["n"] == 1
    # circuit breaker: the next call does not hit the failing gateway again
    r = await mc.chat([{"role": "user", "content": "x"}])
    assert r.offline and "recently failed" in r.fallback_reason and calls["n"] == 1
    # a URL without OMNIROUTE_ENABLED is not used (explicit opt-in before prompts leave the machine)
    s3 = settings.model_copy(update={"omniroute_base_url": "http://gw.test/v1"})
    assert not ModelClient(settings=s3).configured


async def test_offline_reason_is_shortened():
    from app.agents.heuristics import short_reason

    raw = 'OmniRoute recently failed, retrying later: HTTP 403: {"error":{"message":"oc/big-pickle: auth"}}'
    assert short_reason(raw) == "OmniRoute recently failed, retrying later: HTTP 403"
    assert short_reason(None) == "OmniRoute not configured"


async def test_final_json_parsing():
    gw = ScriptedGateway(lambda b: {"role": "assistant", "content": "```json\n{\"summary\": \"done\"}\n```"})
    data, _ = await gw.client().chat_json([{"role": "user", "content": "x"}])
    assert data == {"summary": "done"}
    assert final("x")["content"].startswith("{")
