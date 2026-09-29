import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Card, Empty, ErrorBox, Stat, fmt, short } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api } from "../services/api";
import type { MemoryHit } from "../types/api";

const TYPES = ["", "CORE", "SEMANTIC", "EPISODIC", "PROCEDURAL", "EVIDENCE"];

export default function MemoryPage() {
  const [type, setType] = useState("");
  const [offset, setOffset] = useState(0);
  const list = usePoll(() => api.memories({ type, limit: 25, offset }), [type, offset]);
  const stats = usePoll(() => api.memoryStats(), [list.data]);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<MemoryHit[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [newMem, setNewMem] = useState({ type: "SEMANTIC", content: "", tags: "" });

  const search = async () => {
    try { setHits(await api.searchMemory(query)); setErr(null); } catch (e) { setErr(String(e)); }
  };
  const add = async () => {
    try {
      await api.createMemory({ type: newMem.type, content: newMem.content,
        tags: newMem.tags.split(",").map((t) => t.trim()).filter(Boolean) });
      setNewMem({ ...newMem, content: "" });
      void list.reload();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <div className="page">
      <h1>Memory</h1>
      <div className="stats">{Object.entries(stats.data ?? {}).map(([k, v]) => <Stat key={k} label={k} value={v} />)}</div>
      <ErrorBox error={err ?? list.error} />
      <Card title="Semantic search (pgvector)">
        <div className="row">
          <input className="grow" value={query} onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && search()} placeholder="e.g. how to troubleshoot EC2 restarts" />
          <button className="primary" onClick={search} disabled={!query.trim()}>Search</button>
        </div>
        {hits && (!hits.length ? <Empty>No relevant memories.</Empty> : (
          <table><thead><tr><th>Score</th><th>Sim.</th><th>Type</th><th>Content</th></tr></thead>
            <tbody>{hits.map((h) => (
              <tr key={h.memory.id}><td>{h.score.toFixed(3)}</td><td className="muted">{h.similarity.toFixed(3)}</td>
                <td><Badge value={h.memory.type} /></td><td>{short(h.memory.content, 160)}</td></tr>))}</tbody></table>))}
      </Card>
      <Card title="Add knowledge">
        <div className="row">
          <select value={newMem.type} onChange={(e) => setNewMem({ ...newMem, type: e.target.value })}>
            {TYPES.filter(Boolean).map((t) => <option key={t}>{t}</option>)}
          </select>
          <input placeholder="tags, comma separated" value={newMem.tags} onChange={(e) => setNewMem({ ...newMem, tags: e.target.value })} />
        </div>
        <textarea rows={2} value={newMem.content} onChange={(e) => setNewMem({ ...newMem, content: e.target.value })}
          placeholder="e.g. Production RDS instance is prod-db-1 in eu-west-1" />
        <button className="primary" disabled={newMem.content.trim().length < 3} onClick={add}>Store memory</button>
      </Card>
      <Card title={`Memories (${list.data?.total ?? 0})`} actions={
        <select value={type} onChange={(e) => { setType(e.target.value); setOffset(0); }}>
          {TYPES.map((t) => <option key={t} value={t}>{t || "all types"}</option>)}</select>}>
        {!list.data?.items.length ? <Empty>No memories yet. They are created by task consolidation and learning.</Empty> : (
          <table><thead><tr><th>Type</th><th>Content</th><th>Tags</th><th>Conf.</th><th>Imp.</th><th>Used</th><th>Source</th><th>Status</th><th /></tr></thead>
            <tbody>{list.data.items.map((m) => (
              <tr key={m.id}>
                <td><Badge value={m.type} /></td>
                <td title={m.content}>{short(m.content, 140)}{m.task_id && <> · <Link to={`/tasks/${m.task_id}`}>{m.task_id}</Link></>}</td>
                <td className="small">{m.tags.join(", ")}</td><td>{m.confidence.toFixed(2)}</td><td>{m.importance.toFixed(2)}</td>
                <td>{m.usage_count}</td><td className="muted small">{m.source}<br />{fmt(m.created_at)}</td><td><Badge value={m.status} /></td>
                <td>{m.status === "ACTIVE" && <button className="small" onClick={async () => { await api.updateMemory(m.id, { status: "ARCHIVED" }); void list.reload(); }}>archive</button>}</td>
              </tr>))}</tbody></table>)}
        <div className="row pager">
          <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 25))}>← prev</button>
          <button disabled={!list.data || offset + 25 >= list.data.total} onClick={() => setOffset(offset + 25)}>next →</button>
        </div>
      </Card>
    </div>
  );
}
