import { Link, useParams } from "react-router-dom";
import { Badge, Card, Empty, ErrorBox, fmt, short } from "../components/ui";
import { usePoll } from "../hooks/usePoll";
import { api } from "../services/api";

/** Minimal Markdown renderer (headings, bullets, bold, code) - enough for Atlas reports. */
function Markdown({ text }: { text: string }) {
  const inline = (s: string) =>
    s.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) =>
      part.startsWith("**") ? <b key={i}>{part.slice(2, -2)}</b> : part.startsWith("`") ? <code key={i}>{part.slice(1, -1)}</code> : part);
  const out: JSX.Element[] = [];
  let list: string[] = [];
  const flush = () => { if (list.length) { out.push(<ul key={out.length}>{list.map((l, i) => <li key={i}>{inline(l)}</li>)}</ul>); list = []; } };
  for (const line of text.split("\n")) {
    if (line.startsWith("- ")) { list.push(line.slice(2)); continue; }
    flush();
    if (line.startsWith("# ")) out.push(<h2 key={out.length}>{inline(line.slice(2))}</h2>);
    else if (line.startsWith("## ")) out.push(<h4 key={out.length}>{inline(line.slice(3))}</h4>);
    else if (line.trim()) out.push(<p key={out.length}>{inline(line)}</p>);
  }
  flush();
  return <div className="markdown">{out}</div>;
}

export default function Reports() {
  const { id } = useParams();
  const list = usePoll(() => api.reports(), []);
  const one = usePoll(() => (id ? api.report(id) : Promise.resolve(null)), [id]);
  return (
    <div className="page">
      <h1>Reports</h1>
      <ErrorBox error={list.error ?? one.error} />
      {id && one.data && (
        <Card title={one.data.title} actions={<><Badge value={one.data.final_status} /><Link to={`/tasks/${one.data.task_id}`}>task →</Link></>}>
          <Markdown text={one.data.markdown} />
        </Card>)}
      <Card title={`All reports (${list.data?.total ?? 0})`}>
        {!list.data?.items.length ? <Empty>Reports are generated for multi-agent, scheduled and report-style tasks.</Empty> : (
          <table><tbody>{list.data.items.map((r) => (
            <tr key={r.id}><td><Link to={`/reports/${r.id}`}>{short(r.title, 80)}</Link></td><td><Badge value={r.final_status} /></td>
              <td>{short(r.executive_summary, 90)}</td><td className="muted">{fmt(r.created_at)}</td></tr>))}</tbody></table>)}
      </Card>
    </div>
  );
}
