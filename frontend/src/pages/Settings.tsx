import { useState } from "react";
import { Card, ErrorBox } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api, getToken, setToken } from "../services/api";

export default function Settings() {
  const [token, setTok] = useState(getToken());
  const [saved, setSaved] = useState(false);
  const projects = usePoll(() => api.projects(), []);
  const [p, setP] = useState({ slug: "", name: "", keywords: "" });
  const [err, setErr] = useState<string | null>(null);
  const addProject = async () => {
    try {
      await api.createProject({ slug: p.slug, name: p.name, keywords: p.keywords.split(",").map((k) => k.trim()).filter(Boolean) });
      setP({ slug: "", name: "", keywords: "" });
      void projects.reload();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
  };
  return (
    <div className="page">
      <h1>Settings</h1>
      <Card title="API access">
        <p className="muted small">With Docker Compose the web proxy injects ATLAS_API_TOKEN automatically. When running the UI
          with <code>npm run dev</code> against a token-protected API, paste the token here (stored in this browser only).</p>
        <div className="row">
          <input className="grow" type="password" value={token} onChange={(e) => { setTok(e.target.value); setSaved(false); }} placeholder="ATLAS_API_TOKEN" />
          <button onClick={() => { setToken(token); setSaved(true); }}>Save</button>
        </div>
        {saved && <p className="ok">Saved.</p>}
        <p className="small"><a href="/docs" target="_blank" rel="noreferrer">OpenAPI docs (/docs)</a></p>
      </Card>
      <Card title="Projects / workspaces">
        <p className="muted small">Tasks are assigned to the project whose keywords best match the request; memory retrieval respects project boundaries.</p>
        <table><tbody>{projects.data?.map((x) => (
          <tr key={x.id}><td><b>{x.name}</b> <code>{x.slug}</code></td><td className="small">{x.keywords.join(", ")}</td></tr>))}</tbody></table>
        <div className="row wrap">
          <input placeholder="slug (e.g. aws-ops)" value={p.slug} onChange={(e) => setP({ ...p, slug: e.target.value })} />
          <input placeholder="name" value={p.name} onChange={(e) => setP({ ...p, name: e.target.value })} />
          <input className="grow" placeholder="keywords: aws, cloudwatch, ec2" value={p.keywords} onChange={(e) => setP({ ...p, keywords: e.target.value })} />
          <button className="primary" disabled={!p.slug || !p.name} onClick={addProject}>Add project</button>
        </div>
        <ErrorBox error={err} />
      </Card>
    </div>
  );
}
