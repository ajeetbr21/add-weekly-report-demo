import { useState } from "react";
import { Badge, Card, ErrorBox } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api } from "../services/api";
import type { Agent } from "../types/api";

export default function Agents() {
  const { data, error, reload } = usePoll(() => api.agents(), []);
  return (
    <div className="page">
      <h1>Agents</h1>
      <p className="muted">Agents are discovered by the orchestrator through the registry. Edits here are explicit,
        audited operator actions; the learning engine never changes agent instructions.</p>
      <ErrorBox error={error} />
      {data?.map((a) => <AgentCard key={a.id} agent={a} onChange={reload} />)}
    </div>
  );
}

function AgentCard({ agent, onChange }: { agent: Agent; onChange: () => void }) {
  const [editing, setEditing] = useState(false);
  const [instructions, setInstructions] = useState(agent.instructions);
  const [err, setErr] = useState<string | null>(null);
  const toggle = async () => {
    await api.updateAgent(agent.id, { status: agent.status === "ACTIVE" ? "DISABLED" : "ACTIVE" });
    onChange();
  };
  const save = async () => {
    try { await api.updateAgent(agent.id, { instructions }); setEditing(false); onChange(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <Card title={<>{agent.name} <code className="muted">{agent.id}</code> <Badge value={agent.status} /></>}
      actions={<><button onClick={toggle}>{agent.status === "ACTIVE" ? "Disable" : "Enable"}</button>
        <button onClick={() => setEditing(!editing)}>{editing ? "Close" : "Edit instructions"}</button></>}>
      <p>{agent.description}</p>
      <div className="row wrap">{agent.capabilities.map((c) => <span key={c} className="tag">{c}</span>)}</div>
      <p className="small muted">Model tier: <b>{agent.model_preference}</b> · Tools: {agent.tools.map((t) => <code key={t}>{t} </code>)}</p>
      <p className="small muted">Memory policy: {JSON.stringify(agent.memory_policy)}</p>
      {editing && <>
        <textarea rows={12} value={instructions} onChange={(e) => setInstructions(e.target.value)} />
        <button className="primary" onClick={save}>Save</button>
        <ErrorBox error={err} />
      </>}
    </Card>
  );
}
