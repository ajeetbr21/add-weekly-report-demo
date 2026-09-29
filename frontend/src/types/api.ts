export interface Page<T> { items: T[]; total: number; limit: number; offset: number }

export type Json = Record<string, unknown>;

export interface Task {
  id: string; parent_task_id: string | null; project_id: string | null; source: string; session_key: string | null;
  original_request: string; normalized_request: string | null; status: string; priority: number;
  selected_agent: string | null; plan: Json | null; result: TaskResult | null; error: string | null;
  retry_count: number; current_step: number; created_at: string; started_at: string | null;
  completed_at: string | null; updated_at: string;
}

export interface StepResult {
  step_index: number; agent_id: string; status: string; summary: string; findings: string[];
  actions_taken: string[]; recommendations: string[]; tools_used: { tool_id: string; status: string; summary: string }[];
  evidence: Json[]; model: string; offline: boolean; confidence: number;
}

export interface TaskResult {
  summary: string; findings?: string[]; recommendations?: string[]; offline?: boolean;
  validation?: { valid: boolean; issues: string[]; warnings: string[]; confidence: number };
  configuration_required?: string[]; applied_lessons?: { memory_id: string; content: string }[];
  steps?: StepResult[]; report_id?: string; memory?: Json; session_runtime?: string; synthesis?: string;
}

export interface TaskStep {
  step_index: number; agent_id: string; objective: string; depends_on: number[]; status: string;
  result: Json | null; error: string | null; attempts: number; started_at: string | null; completed_at: string | null;
}

export interface TaskDetail extends Task { steps: TaskStep[]; session: Json }

export interface TaskEvent {
  id: number; task_id: string; timestamp: string; event_type: string; agent_id: string | null;
  step_index: number | null; message: string | null; metadata: Json;
}

export interface Agent {
  id: string; name: string; description: string; capabilities: string[]; keywords: string[]; tools: string[];
  instructions: string; model_preference: string; memory_policy: Json; status: string; updated_at: string;
}

export interface AgentRun {
  id: string; task_id: string; step_index: number | null; agent_id: string; status: string; model: string | null;
  model_provider: string | null; runtime: string | null; iterations: number; prompt_tokens: number;
  completion_tokens: number; output_summary: string | null; error: string | null; started_at: string;
  completed_at: string | null;
}

export interface Tool {
  id: string; name: string; provider: string; toolkit: string | null; description: string; capabilities: string[];
  input_schema: Json; risk_level: string; requires_approval: boolean; enabled: boolean; last_seen_at: string | null;
}

export interface ToolExecution {
  id: string; task_id: string; step_index: number | null; agent_id: string | null; tool_id: string;
  arguments: Json; risk_level: string; status: string; result: Json | null; error: string | null;
  approval_id: string | null; started_at: string; duration_ms: number | null;
}

export interface Approval {
  id: string; task_id: string; step_index: number | null; agent_id: string | null; tool_id: string;
  action_summary: string; risk_level: string; arguments: Json; status: string; requested_at: string;
  decided_at: string | null; decided_by: string | null; decision_channel: string | null; reason: string | null;
}

export interface Memory {
  id: string; type: string; title: string | null; content: string; source: string; task_id: string | null;
  project_id: string | null; agent_id: string | null; tags: string[]; metadata: Json; confidence: number;
  importance: number; status: string; created_at: string; updated_at: string; last_accessed_at: string | null;
  usage_count: number;
}

export interface MemoryHit { memory: Memory; score: number; similarity: number }

export interface Candidate {
  id: string; task_id: string | null; original_feedback: string; normalized_lesson: string; category: string;
  domain_tags: string[]; confidence: number; status: string; requires_approval: boolean;
  validation_notes: string | null; created_at: string; validated_at: string | null; usage_count: number;
  success_count: number; failure_count: number; success_rate: number | null;
}

export interface Lesson {
  id: string; title: string; content: string; category: string; domain_tags: string[]; status: string;
  usage_count: number; success_count: number; failure_count: number; created_at: string; memory_id: string | null;
}

export interface Skill {
  id: string; name: string; description: string; domain_tags: string[]; steps: string[]; status: string;
  version: number; usage_count: number; created_at: string;
}

export interface Report {
  id: string; task_id: string; title: string; objective: string; executive_summary: string; final_status: string;
  content: Json; markdown: string; created_at: string;
}

export interface Schedule {
  id: string; name: string; request: string; schedule_type: string; cron_expression: string | null;
  run_at: string | null; timezone: string; enabled: boolean; next_run_at: string | null; last_run_at: string | null;
  last_task_id: string | null; run_count: number; created_at: string;
}

export interface Project { id: string; slug: string; name: string; description: string | null; keywords: string[] }

export interface Dashboard {
  tasks: { by_status: Record<string, number>; last_24h: number; active: number };
  recent_tasks: { id: string; status: string; source: string; request: string; selected_agent: string | null; created_at: string }[];
  pending_approvals: number;
  recent_agent_runs: AgentRun[];
  recent_tool_executions: ToolExecution[];
  recent_learning: { id: string; lesson: string; status: string; category: string; created_at: string }[];
  approved_lessons: number;
  memory: Record<string, number>;
  system_events: { ts: string; component: string; event: string; message: string | null }[];
}
