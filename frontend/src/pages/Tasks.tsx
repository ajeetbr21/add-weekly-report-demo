import { useState } from "react";
import { Link } from "react-router-dom";
import NewTask from "../components/NewTask";
import { Badge, Card, Empty, ErrorBox, fmt, short } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api } from "../services/api";

const STATUSES = ["", "PENDING", "PLANNING", "RUNNING", "WAITING_APPROVAL", "RETRYING", "COMPLETED", "FAILED", "CANCELLED"];
const LIMIT = 25;

export default function Tasks() {
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const { data, error } = usePoll(() => api.tasks({ status, q, limit: LIMIT, offset }), [status, q, offset], 4000);
  return (
    <div className="page">
      <h1>Tasks</h1>
      <Card title="New task"><NewTask /></Card>
      <Card title={`All tasks (${data?.total ?? 0})`} actions={<>
        <input placeholder="search…" value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
        <select value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }}>
          {STATUSES.map((s) => <option key={s} value={s}>{s || "any status"}</option>)}
        </select></>}>
        <ErrorBox error={error} />
        {!data?.items.length ? <Empty>No tasks.</Empty> : (
          <table>
            <thead><tr><th>ID</th><th>Status</th><th>Source</th><th>Agent</th><th>Request</th><th>Created</th></tr></thead>
            <tbody>{data.items.map((t) => (
              <tr key={t.id}>
                <td><Link to={`/tasks/${t.id}`}>{t.id}</Link></td><td><Badge value={t.status} /></td>
                <td>{t.source}</td><td>{t.selected_agent ?? "-"}</td><td>{short(t.original_request, 80)}</td>
                <td className="muted">{fmt(t.created_at)}</td>
              </tr>))}
            </tbody>
          </table>)}
        <div className="row pager">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIMIT))}>← prev</button>
          <span className="muted">{offset + 1}–{Math.min(offset + LIMIT, data?.total ?? 0)} of {data?.total ?? 0}</span>
          <button disabled={!data || offset + LIMIT >= data.total} onClick={() => setOffset(offset + LIMIT)}>next →</button>
        </div>
      </Card>
    </div>
  );
}
