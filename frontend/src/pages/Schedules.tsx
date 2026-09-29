import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Card, Empty, ErrorBox, fmt } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api } from "../services/api";

export default function Schedules() {
  const { data, error, reload } = usePoll(() => api.schedules(), [], 10000);
  const [form, setForm] = useState({ name: "", request: "", schedule_type: "DAILY", cron_expression: "", run_at: "", timezone: "UTC" });
  const [err, setErr] = useState<string | null>(null);
  const create = async () => {
    try {
      await api.createSchedule({ ...form, cron_expression: form.cron_expression || undefined,
        run_at: form.run_at ? new Date(form.run_at).toISOString() : undefined });
      setForm({ ...form, name: "", request: "" });
      setErr(null);
      void reload();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="page">
      <h1>Schedules</h1>
      <p className="muted">Persistent local scheduler. Each firing creates a normal task (source: scheduler) and is
        deduplicated with an idempotency key.</p>
      <Card title="New schedule">
        <div className="row wrap">
          <input placeholder="name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <select value={form.schedule_type} onChange={(e) => setForm({ ...form, schedule_type: e.target.value })}>
            {["ONCE", "DAILY", "WEEKLY", "MONTHLY", "CRON"].map((t) => <option key={t}>{t}</option>)}
          </select>
          {form.schedule_type === "CRON" ?
            <input placeholder="cron, e.g. 0 8 * * 1-5" value={form.cron_expression} onChange={(e) => setForm({ ...form, cron_expression: e.target.value })} /> :
            <label className="muted small">{form.schedule_type === "ONCE" ? "run at" : "first run / time of day"}
              <input type="datetime-local" value={form.run_at} onChange={(e) => setForm({ ...form, run_at: e.target.value })} /></label>}
          <input placeholder="timezone" value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })} />
        </div>
        <textarea rows={2} placeholder="request, e.g. Summarize today's AWS alarms" value={form.request}
          onChange={(e) => setForm({ ...form, request: e.target.value })} />
        <button className="primary" disabled={!form.name || !form.request} onClick={create}>Create schedule</button>
        <ErrorBox error={err} />
      </Card>
      <Card title={`Schedules (${data?.length ?? 0})`}>
        <ErrorBox error={error} />
        {!data?.length ? <Empty>No schedules.</Empty> : (
          <table><thead><tr><th>Name</th><th>Type</th><th>Cron</th><th>Next run</th><th>Last task</th><th>Runs</th><th>Enabled</th><th /></tr></thead>
            <tbody>{data.map((s) => (
              <tr key={s.id} title={s.request}><td>{s.name}</td><td>{s.schedule_type}</td><td><code>{s.cron_expression ?? "-"}</code></td>
                <td>{fmt(s.next_run_at)}</td><td>{s.last_task_id ? <Link to={`/tasks/${s.last_task_id}`}>{s.last_task_id}</Link> : "-"}</td>
                <td>{s.run_count}</td><td><Badge value={s.enabled ? "ACTIVE" : "DISABLED"} /></td>
                <td className="nowrap">
                  <button className="small" onClick={async () => { await api.runSchedule(s.id); void reload(); }}>run now</button>
                  <button className="small" onClick={async () => { await api.updateSchedule(s.id, { enabled: !s.enabled }); void reload(); }}>{s.enabled ? "disable" : "enable"}</button>
                  <button className="small danger" onClick={async () => { await api.deleteSchedule(s.id); void reload(); }}>delete</button>
                </td></tr>))}</tbody></table>)}
      </Card>
    </div>
  );
}
