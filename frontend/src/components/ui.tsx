import type { ReactNode } from "react";

const STATUS_COLORS: Record<string, string> = {
  COMPLETED: "green", SUCCESS: "green", APPROVED: "green", ACTIVE: "green", OK: "green", ready: "green",
  CONFIGURED: "green", success: "green",
  RUNNING: "blue", PLANNING: "blue", PENDING: "gray", CANDIDATE: "amber", RETRYING: "amber",
  WAITING_APPROVAL: "amber", DEGRADED: "amber", CONFIGURATION_REQUIRED: "amber", DISABLED: "gray",
  FAILED: "red", ERROR: "red", REJECTED: "red", CANCELLED: "gray", UNREACHABLE: "red", DEPRECATED: "gray",
  BLOCKED: "red", NOT_MIGRATED: "red", READ: "green", WRITE: "amber", DESTRUCTIVE: "red",
};

export function Badge({ value }: { value: string | null | undefined }) {
  const v = value ?? "-";
  return <span className={`badge ${STATUS_COLORS[v] ?? "gray"}`}>{v}</span>;
}

export function Card({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card-head">
          <h3>{title}</h3>
          <div className="row">{actions}</div>
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className={`stat ${tone ?? ""}`}>
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="error">⚠ {error}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function JsonView({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

export const fmt = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "-");
export const short = (s: string | null | undefined, n = 90) => (!s ? "" : s.length > n ? s.slice(0, n) + "…" : s);
