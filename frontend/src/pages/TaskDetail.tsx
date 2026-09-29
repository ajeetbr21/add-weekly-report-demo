import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge, Card, Empty, ErrorBox, JsonView, fmt } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api } from "../services/api";

const LIVE = new Set(["PENDING", "PLANNING", "RUNNING", "RETRYING", "WAITING_APPROVAL"]);

export default function TaskDetailPage() {
  const { id = "" } = useParams();
  const task = usePoll(() => api.task(id), [id], 2500);
  const live = task.data ? LIVE.has(task.data.status) : true;
  const events = usePoll(() => api.taskEvents(id), [id, task.data?.status, task.data?.updated_at]);
  const tools = usePoll(() => api.taskTools(id), [id, task.data?.status, task.data?.updated_at]);
  const runs = usePoll(() => api.taskRuns(id), [id, task.data?.status]);
  const approvals = usePoll(() => api.approvals(), [id, task.data?.status]);
  const t = task.data;
  const res = t?.result;
  const pending = (approvals.data?.items ?? []).filter((a) => a.task_id === id && a.status === "PENDING");

  return (
    <div className="page">
      <h1><Link to="/tasks">Tasks</Link> / {id} {t && <Badge value={t.status} />}</h1>
      <ErrorBox error={task.error} />
      {t && <>
        <Card title="Request">
          <p className="request">{t.original_request}</p>
          <div className="row muted small">source: {t.source} · agent: {t.selected_agent ?? "-"} · priority {t.priority} ·
            created {fmt(t.created_at)} · retries {t.retry_count}{live && " · live ⟳"}</div>
          <div className="row">
            {live && <button onClick={async () => { await api.cancelTask(id); void task.reload(); }}>Cancel</button>}
            {(t.status === "FAILED" || t.status === "CANCELLED") &&
              <button onClick={async () => { await api.retryTask(id); void task.reload(); }}>Retry</button>}
          </div>
        </Card>

        {pending.map((a) => (
          <Card key={a.id} title={<>Approval required <Badge value={a.risk_level} /></>}>
            <p><b>Action:</b> <code>{a.action_summary}</code></p>
            <div className="row">
              <button className="primary" onClick={async () => { await api.decide(a.id, true); void approvals.reload(); void task.reload(); }}>APPROVE</button>
              <button className="danger" onClick={async () => { await api.decide(a.id, false, "rejected in web UI"); void approvals.reload(); void task.reload(); }}>REJECT</button>
            </div>
          </Card>))}

        {t.error && <Card title="Error"><pre className="json">{t.error}</pre></Card>}

        {res && <Card title={<>Result {res.offline && <span className="badge amber">offline mode (no LLM)</span>}
          {res.validation && <Badge value={res.validation.valid ? "success" : "ERROR"} />}</>}
          actions={res.report_id && <Link to={`/reports/${res.report_id}`}>view report →</Link>}>
          <p className="summary">{res.summary}</p>
          {!!res.findings?.length && <><h4>Findings</h4><ul>{res.findings.map((f, i) => <li key={i}>{f}</li>)}</ul></>}
          {!!res.recommendations?.length && <><h4>Recommendations</h4><ul>{res.recommendations.map((f, i) => <li key={i}>{f}</li>)}</ul></>}
          {!!res.configuration_required?.length && <div className="warn">{res.configuration_required.map((c, i) => <div key={i}>⚠ {c}</div>)}</div>}
          {!!res.applied_lessons?.length && <><h4>Lessons applied from memory</h4><ul>{res.applied_lessons.map((l) => <li key={l.memory_id}>{l.content}</li>)}</ul></>}
          {res.validation?.warnings?.length ? <p className="muted small">Validation: {res.validation.warnings.join(" · ")}</p> : null}
        </Card>}

        {t.status === "COMPLETED" && <FeedbackBox taskId={id} />}

        <Card title="Plan & steps">
          {t.plan && <p className="muted small">{String(t.plan.complexity)} plan by {String(t.plan.planner)}: {String(t.plan.rationale ?? "")}</p>}
          {!t.steps.length ? <Empty>Not planned yet.</Empty> : (
            <table><thead><tr><th>#</th><th>Agent</th><th>Objective</th><th>Status</th><th>Attempts</th></tr></thead>
              <tbody>{t.steps.map((s) => (
                <tr key={s.step_index}><td>{s.step_index}</td><td>{s.agent_id}</td><td>{s.objective}</td>
                  <td><Badge value={s.status} /></td><td>{s.attempts}</td></tr>))}</tbody></table>)}
        </Card>

        <div className="grid2">
          <Card title="Agent runs">
            {!runs.data?.length ? <Empty>none</Empty> : <table><tbody>{runs.data.map((r) => (
              <tr key={r.id}><td>{r.agent_id}</td><td><Badge value={r.status} /></td><td className="muted">{r.model_provider}/{r.model}</td>
                <td className="muted">{r.iterations} it · {r.prompt_tokens + r.completion_tokens} tok</td></tr>))}</tbody></table>}
          </Card>
          <Card title="Tool executions">
            {!tools.data?.length ? <Empty>none</Empty> : <table><tbody>{tools.data.map((x) => (
              <tr key={x.id} title={JSON.stringify(x.arguments)}><td>{x.tool_id}</td><td><Badge value={x.risk_level} /></td>
                <td><Badge value={x.status} /></td><td className="muted">{x.duration_ms ?? "-"} ms</td></tr>))}</tbody></table>}
          </Card>
        </div>

        <Card title={`Task journal (${events.data?.length ?? 0} events)`}>
          <table className="journal"><tbody>{(events.data ?? []).map((e) => (
            <tr key={e.id}><td className="muted nowrap">{new Date(e.timestamp).toLocaleTimeString()}</td>
              <td><code>{e.event_type}</code></td><td className="muted">{e.agent_id ?? ""}{e.step_index !== null ? ` #${e.step_index}` : ""}</td>
              <td>{e.message}{Object.keys(e.metadata).length > 0 &&
                <details><summary className="muted small">metadata</summary><JsonView value={e.metadata} /></details>}</td></tr>))}
          </tbody></table>
        </Card>
      </>}
    </div>
  );
}

function FeedbackBox({ taskId }: { taskId: string }) {
  const [text, setText] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  async function send(content: string, rating?: string) {
    try {
      const r = await api.feedback(taskId, content, rating);
      setMsg(`Feedback stored. ${r.candidates.length} learning candidate(s): ` +
        r.candidates.map((c) => `${c.normalized_lesson} [${c.status}]`).join("; "));
      setText("");
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  }
  return (
    <Card title="Feedback">
      <div className="row">
        <button onClick={() => send("This solution worked.", "POSITIVE")}>👍 Worked</button>
        <button onClick={() => send("That was incorrect.", "NEGATIVE")}>👎 Incorrect</button>
      </div>
      <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)}
        placeholder='What should Atlas do differently next time? e.g. "Before restarting anything, check CloudWatch status and recent events."' />
      <button className="primary" disabled={!text.trim()} onClick={() => send(text)}>Send feedback</button>
      {msg && <p className="ok">{msg} <Link to="/learning">review →</Link></p>}
      <ErrorBox error={err} />
    </Card>
  );
}
