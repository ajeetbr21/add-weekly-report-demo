// Typed client for the local FastAPI backend. The UI talks ONLY to this backend.
import type {
  Agent, AgentRun, Approval, Candidate, Dashboard, Lesson, Memory, MemoryHit, Page, Project, Report,
  Schedule, Skill, Task, TaskDetail, TaskEvent, Tool, ToolExecution,
} from "../types/api";

const TOKEN_KEY = "atlas.apiToken";
export const getToken = () => localStorage.getItem(TOKEN_KEY) ?? "";
export const setToken = (t: string) => (t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY));

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const err = data?.error ?? {};
    throw new ApiError(res.status, err.code ?? "http_error", err.message ?? res.statusText, err.details);
  }
  return data as T;
}

const qs = (params: Record<string, string | number | undefined | null>) => {
  const p = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return p.length ? "?" + new URLSearchParams(p.map(([k, v]) => [k, String(v)])).toString() : "";
};

export const api = {
  ready: () => request<{ status: string; database: Record<string, unknown> }>("GET", "/ready"),
  dashboard: () => request<Dashboard>("GET", "/api/system/dashboard"),
  integrations: () => request<Record<string, Record<string, unknown>>>("GET", "/api/integrations"),

  tasks: (p: { status?: string; q?: string; limit?: number; offset?: number } = {}) =>
    request<Page<Task>>("GET", "/api/tasks" + qs(p)),
  task: (id: string) => request<TaskDetail>("GET", `/api/tasks/${id}`),
  createTask: (body: { request: string; project?: string; priority?: number }) =>
    request<{ task: Task; created: boolean }>("POST", "/api/tasks", { source: "web", user_external_id: "web-user", session_key: "web:web-user", ...body }),
  taskEvents: (id: string) => request<TaskEvent[]>("GET", `/api/tasks/${id}/events`),
  taskTools: (id: string) => request<ToolExecution[]>("GET", `/api/tasks/${id}/tool-executions`),
  taskRuns: (id: string) => request<AgentRun[]>("GET", `/api/tasks/${id}/agent-runs`),
  taskReport: (id: string) => request<Report | null>("GET", `/api/tasks/${id}/report`),
  cancelTask: (id: string) => request<Task>("POST", `/api/tasks/${id}/cancel`),
  retryTask: (id: string) => request<Task>("POST", `/api/tasks/${id}/retry`),

  agents: () => request<Agent[]>("GET", "/api/agents"),
  updateAgent: (id: string, body: Partial<Agent>) => request<Agent>("PATCH", `/api/agents/${id}`, body),
  agentRuns: (limit = 50) => request<Page<AgentRun>>("GET", `/api/agents/runs?limit=${limit}`),

  tools: () => request<Tool[]>("GET", "/api/tools"),
  refreshTools: () => request<{ providers: Record<string, unknown> }>("POST", "/api/tools/refresh"),
  toolExecutions: (limit = 50) => request<Page<ToolExecution>>("GET", `/api/tools/executions?limit=${limit}`),

  approvals: (status?: string) => request<Page<Approval>>("GET", "/api/approvals" + qs({ status })),
  decide: (id: string, approve: boolean, reason?: string) =>
    request<Approval>("POST", `/api/approvals/${id}/decision`, { approve, reason, decided_by: "web-user", channel: "web" }),

  memories: (p: { type?: string; q?: string; limit?: number; offset?: number } = {}) =>
    request<Page<Memory>>("GET", "/api/memory" + qs(p)),
  memoryStats: () => request<Record<string, number>>("GET", "/api/memory/stats"),
  searchMemory: (query: string, types?: string[]) =>
    request<MemoryHit[]>("POST", "/api/memory/search", { query, types, limit: 10 }),
  createMemory: (body: { type: string; content: string; title?: string; tags?: string[]; project?: string }) =>
    request<Memory>("POST", "/api/memory", body),
  updateMemory: (id: string, body: { status?: string }) => request<Memory>("PATCH", `/api/memory/${id}`, body),

  feedback: (task_id: string, content: string, rating?: string) =>
    request<{ feedback: unknown; candidates: Candidate[] }>("POST", "/api/feedback", { task_id, content, rating, channel: "web" }),
  candidates: (status?: string) => request<Page<Candidate>>("GET", "/api/learning/candidates" + qs({ status })),
  decideCandidate: (id: string, approve: boolean, edited_lesson?: string) =>
    request<Candidate>("POST", `/api/learning/candidates/${id}/decision`, { approve, edited_lesson, decided_by: "web-user" }),
  lessons: () => request<Lesson[]>("GET", "/api/learning/lessons"),
  deprecateLesson: (id: string) => request<Lesson>("POST", `/api/learning/lessons/${id}/deprecate`),
  skills: () => request<Skill[]>("GET", "/api/learning/skills"),

  reports: () => request<Page<Report>>("GET", "/api/reports"),
  report: (id: string) => request<Report>("GET", `/api/reports/${id}`),

  schedules: () => request<Schedule[]>("GET", "/api/schedules"),
  createSchedule: (body: Record<string, unknown>) => request<Schedule>("POST", "/api/schedules", body),
  updateSchedule: (id: string, body: Record<string, unknown>) => request<Schedule>("PATCH", `/api/schedules/${id}`, body),
  deleteSchedule: (id: string) => request<void>("DELETE", `/api/schedules/${id}`),
  runSchedule: (id: string) => request<{ task: Task }>("POST", `/api/schedules/${id}/run-now`),

  projects: () => request<Project[]>("GET", "/api/projects"),
  createProject: (body: { slug: string; name: string; description?: string; keywords: string[] }) =>
    request<Project>("POST", "/api/projects", body),
};
