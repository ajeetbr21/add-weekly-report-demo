import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Card, Empty, ErrorBox, JsonView, fmt } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api } from "../services/api";

export default function Approvals() {
  const [status, setStatus] = useState("PENDING");
  const { data, error, reload } = usePoll(() => api.approvals(status || undefined), [status], 3000);
  const [err, setErr] = useState<string | null>(null);
  const decide = async (id: string, approve: boolean) => {
    try { await api.decide(id, approve, approve ? undefined : "rejected in web UI"); void reload(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="page">
      <h1>Approvals</h1>
      <p className="muted">Destructive or approval-required tool calls pause their task in WAITING_APPROVAL. Nothing is
        executed until you approve. Decisions are audited; Telegram offers the same buttons.</p>
      <ErrorBox error={error ?? err} />
      <Card title={`${data?.total ?? 0} approval(s)`} actions={
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          {["PENDING", "APPROVED", "REJECTED", ""].map((s) => <option key={s} value={s}>{s || "all"}</option>)}
        </select>}>
        {!data?.items.length ? <Empty>Nothing waiting for you.</Empty> : data.items.map((a) => (
          <div key={a.id} className="candidate">
            <div className="row"><Badge value={a.status} /><Badge value={a.risk_level} />
              <Link to={`/tasks/${a.task_id}`}>{a.task_id}</Link><span className="muted small">{a.agent_id} · {fmt(a.requested_at)}</span></div>
            <p><b>Action:</b> <code>{a.action_summary}</code></p>
            <details><summary className="muted small">arguments (secrets redacted)</summary><JsonView value={a.arguments} /></details>
            {a.status === "PENDING" ? (
              <div className="row">
                <button className="primary" onClick={() => decide(a.id, true)}>APPROVE</button>
                <button className="danger" onClick={() => decide(a.id, false)}>REJECT</button>
              </div>) : <p className="muted small">{a.status} by {a.decided_by} via {a.decision_channel} · {fmt(a.decided_at)} {a.reason ?? ""}</p>}
          </div>))}
      </Card>
    </div>
  );
}
