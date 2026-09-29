import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../services/api";
import { ErrorBox } from "./ui";

export default function NewTask() {
  const [text, setText] = useState("");
  const [priority, setPriority] = useState(5);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nav = useNavigate();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    setBusy(true);
    try {
      const { task } = await api.createTask({ request: text, priority });
      nav(`/tasks/${task.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="new-task" onSubmit={submit}>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="What should Atlas do? e.g. Analyze today's AWS alarms and summarize anything that needs attention."
        rows={3}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit(e); }}
      />
      <div className="row">
        <label className="muted">Priority
          <select value={priority} onChange={(e) => setPriority(Number(e.target.value))}>
            {[1, 2, 3, 4, 5, 6, 7, 8, 9].map((p) => <option key={p} value={p}>{p}{p === 1 ? " (urgent)" : ""}</option>)}
          </select>
        </label>
        <button className="primary" disabled={busy || !text.trim()}>{busy ? "Creating…" : "Run task"}</button>
      </div>
      <ErrorBox error={error} />
    </form>
  );
}
