import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Card, Empty, ErrorBox, fmt } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api } from "../services/api";
import type { Candidate } from "../types/api";

export default function Learning() {
  const [status, setStatus] = useState("CANDIDATE");
  const cands = usePoll(() => api.candidates(status || undefined), [status], 5000);
  const lessons = usePoll(() => api.lessons(), [cands.data]);
  const skills = usePoll(() => api.skills(), [lessons.data]);
  return (
    <div className="page">
      <h1>Learning</h1>
      <p className="muted">Feedback → learning candidate → validation → approval → lesson/skill → procedural or semantic
        memory → retrieved for future tasks. Learning never modifies code, security settings, agent instructions or the schema.</p>
      <Card title="Learning candidates" actions={
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          {["CANDIDATE", "APPROVED", "REJECTED", "DEPRECATED", ""].map((s) => <option key={s} value={s}>{s || "all"}</option>)}
        </select>}>
        <ErrorBox error={cands.error} />
        {!cands.data?.items.length ? <Empty>No candidates with this status.</Empty> :
          cands.data.items.map((c) => <CandidateRow key={c.id} c={c} onDone={cands.reload} />)}
      </Card>
      <Card title={`Lessons (${lessons.data?.length ?? 0})`}>
        {!lessons.data?.length ? <Empty>No lessons yet.</Empty> : (
          <table><thead><tr><th>Lesson</th><th>Category</th><th>Tags</th><th>Used</th><th>✓/✗</th><th>Status</th><th /></tr></thead>
            <tbody>{lessons.data.map((l) => (
              <tr key={l.id}><td>{l.content}</td><td>{l.category}</td><td className="small">{l.domain_tags.join(", ")}</td>
                <td>{l.usage_count}</td><td>{l.success_count}/{l.failure_count}</td><td><Badge value={l.status} /></td>
                <td>{l.status === "APPROVED" && <button className="small" onClick={async () => { await api.deprecateLesson(l.id); void lessons.reload(); }}>deprecate</button>}</td></tr>))}
            </tbody></table>)}
      </Card>
      <Card title={`Skills (${skills.data?.length ?? 0})`}>
        {!skills.data?.length ? <Empty>No skills yet. Approved procedures are grouped into per-domain playbooks.</Empty> :
          skills.data.map((s) => (
            <div key={s.id} className="skill"><b>{s.name}</b> <span className="muted">v{s.version} · {s.domain_tags.join(", ")}</span>
              <p className="small">{s.description}</p><ol>{s.steps.map((st, i) => <li key={i}>{st}</li>)}</ol></div>))}
      </Card>
    </div>
  );
}

function CandidateRow({ c, onDone }: { c: Candidate; onDone: () => void }) {
  const [text, setText] = useState(c.normalized_lesson);
  const [err, setErr] = useState<string | null>(null);
  const decide = async (approve: boolean) => {
    try { await api.decideCandidate(c.id, approve, approve && text !== c.normalized_lesson ? text : undefined); onDone(); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="candidate">
      <div className="row"><Badge value={c.status} /> <b>{c.category}</b> <span className="muted small">
        confidence {c.confidence.toFixed(2)} · {c.domain_tags.join(", ")} · {fmt(c.created_at)}
        {c.task_id && <> · <Link to={`/tasks/${c.task_id}`}>{c.task_id}</Link></>}</span></div>
      <p className="muted small">Feedback: “{c.original_feedback}”</p>
      {c.status === "CANDIDATE" ? <>
        <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} />
        <div className="row">
          <button className="primary" onClick={() => decide(true)}>Approve lesson</button>
          <button className="danger" onClick={() => decide(false)}>Reject</button>
        </div>
      </> : <p>{c.normalized_lesson}</p>}
      {c.validation_notes && <p className="small muted">Validation: {c.validation_notes}</p>}
      <ErrorBox error={err} />
    </div>
  );
}
