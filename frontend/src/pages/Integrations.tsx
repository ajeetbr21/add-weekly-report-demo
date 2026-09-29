import { useState } from "react";
import { Badge, Card, Empty, ErrorBox, JsonView, short } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api } from "../services/api";

export default function Integrations() {
  const integ = usePoll(() => api.integrations(), [], 20000);
  const tools = usePoll(() => api.tools(), []);
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    try { await api.refreshTools(); await tools.reload(); await integ.reload(); } finally { setBusy(false); }
  };
  return (
    <div className="page">
      <h1>Integrations</h1>
      <ErrorBox error={integ.error ?? tools.error} />
      <div className="grid2">
        {Object.entries(integ.data ?? {}).map(([name, v]) => (
          <Card key={name} title={<>{name} {v.status !== undefined && <Badge value={String(v.status)} />}</>}>
            <JsonView value={v} />
          </Card>))}
      </div>
      <Card title={`Tool registry (${tools.data?.length ?? 0})`} actions={<button onClick={refresh} disabled={busy}>{busy ? "Refreshing…" : "Re-discover tools"}</button>}>
        {!tools.data?.length ? <Empty>No tools discovered. Is the local MCP server running? Composio requires configuration.</Empty> : (
          <table><thead><tr><th>Tool</th><th>Provider</th><th>Toolkit</th><th>Risk</th><th>Approval</th><th>Description</th></tr></thead>
            <tbody>{tools.data.map((t) => (
              <tr key={t.id}><td><code>{t.id}</code></td><td>{t.provider}</td><td>{t.toolkit ?? "-"}</td><td><Badge value={t.risk_level} /></td>
                <td>{t.requires_approval ? "required" : "-"}</td><td className="small">{short(t.description, 120)}</td></tr>))}</tbody></table>)}
      </Card>
    </div>
  );
}
