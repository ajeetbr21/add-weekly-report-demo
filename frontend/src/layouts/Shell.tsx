import { NavLink, Outlet } from "react-router-dom";
import { api } from "../services/api";
import { usePoll } from "../hooks/usePoll";

const NAV = [
  ["/", "Dashboard"], ["/tasks", "Tasks"], ["/agents", "Agents"], ["/memory", "Memory"], ["/learning", "Learning"],
  ["/approvals", "Approvals"], ["/reports", "Reports"], ["/schedules", "Schedules"], ["/integrations", "Integrations"],
  ["/settings", "Settings"],
] as const;

export default function Shell() {
  const ready = usePoll(() => api.ready(), [], 15000);
  const pending = usePoll(() => api.approvals("PENDING"), [], 5000);
  const ok = ready.data?.status === "ready";
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">◆ Atlas<small>local multi-agent platform</small></div>
        <nav>
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
              {label}
              {label === "Approvals" && (pending.data?.total ?? 0) > 0 && <span className="pill">{pending.data?.total}</span>}
            </NavLink>
          ))}
        </nav>
        <div className={`health ${ok ? "ok" : "bad"}`}>{ok ? "● backend ready" : `● ${ready.error ?? "backend not ready"}`}</div>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
