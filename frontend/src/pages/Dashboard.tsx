import { Link } from "react-router-dom";
import NewTask from "../components/NewTask";
import { Badge, Card, Empty, ErrorBox, Stat, fmt, short } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api } from "../services/api";

export default function Dashboard() {
  const { data, error } = usePoll(() => api.dashboard(), [], 4000);
  const integ = usePoll(() => api.integrations(), [], 30000);
  const by = data?.tasks.by_status ?? {};
  return (
    <div className="page">
      <h1>Dashboard</h1>
      <Card title="New task"><NewTask /></Card>
      <ErrorBox error={error} />
      <div className="stats">
        <Stat label="Active tasks" value={data?.tasks.active ?? "…"} tone="blue" />
        <Stat label="Completed" value={by.COMPLETED ?? 0} tone="green" />
        <Stat label="Failed" value={by.FAILED ?? 0} tone="red" />
        <Stat label="Pending approvals" value={data?.pending_approvals ?? 0} tone="amber" />
        <Stat label="Approved lessons" value={data?.approved_lessons ?? 0} />
        <Stat label="Tasks (24h)" value={data?.tasks.last_24h ?? 0} />
      </div>
      <div className="grid2">
        <Card title="Recent tasks" actions={<Link to="/tasks">all →</Link>}>
          {!data?.recent_tasks.length ? <Empty>No tasks yet.</Empty> : (
            <table><tbody>
              {data.recent_tasks.map((t) => (
                <tr key={t.id}>
                  <td><Link to={`/tasks/${t.id}`}>{t.id}</Link></td>
                  <td><Badge value={t.status} /></td>
                  <td className="muted">{t.selected_agent}</td>
                  <td>{short(t.request, 60)}</td>
                </tr>))}
            </tbody></table>)}
        </Card>
        <Card title="System health">
          <table><tbody>
            {Object.entries(integ.data ?? {}).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td><Badge value={String(v.status ?? (v.api_token_required !== undefined ? (v.api_token_required ? "ACTIVE" : "DISABLED") : "-"))} /></td>
                <td className="muted">{short(String(v.detail ?? v.version ?? v.migration ?? ""), 60)}</td></tr>))}
          </tbody></table>
        </Card>
        <Card title="Recent agent runs">
          {!data?.recent_agent_runs.length ? <Empty>No agent runs yet.</Empty> : (
            <table><tbody>
              {data.recent_agent_runs.map((r) => (
                <tr key={r.id}><td><Link to={`/tasks/${r.task_id}`}>{r.task_id}</Link></td><td>{r.agent_id}</td>
                  <td><Badge value={r.status} /></td><td className="muted">{r.model_provider}/{r.model}</td></tr>))}
            </tbody></table>)}
        </Card>
        <Card title="Recent tool executions">
          {!data?.recent_tool_executions.length ? <Empty>No tool calls yet.</Empty> : (
            <table><tbody>
              {data.recent_tool_executions.map((t) => (
                <tr key={t.id}><td>{t.tool_id}</td><td><Badge value={t.risk_level} /></td><td><Badge value={t.status} /></td>
                  <td className="muted">{t.duration_ms ?? "-"} ms</td></tr>))}
            </tbody></table>)}
        </Card>
        <Card title="Recent learning" actions={<Link to="/learning">review →</Link>}>
          {!data?.recent_learning.length ? <Empty>No feedback-derived lessons yet.</Empty> : (
            <ul className="list">{data.recent_learning.map((l) => (
              <li key={l.id}><Badge value={l.status} /> <span className="muted">{l.category}</span> {short(l.lesson, 110)}</li>))}</ul>)}
        </Card>
        <Card title="Memory">
          <div className="row wrap">{Object.entries(data?.memory ?? {}).map(([k, v]) => <Stat key={k} label={k} value={v} />)}</div>
          <ul className="list small">{data?.system_events.map((e, i) => (
            <li key={i} className="muted">{fmt(e.ts)} · {e.component} · {e.event} {e.message ?? ""}</li>))}</ul>
        </Card>
      </div>
    </div>
  );
}
