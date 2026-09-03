const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

function getDemoToken(): string | null {
  try {
    return sessionStorage.getItem("demo_admin_token");
  } catch {
    return null;
  }
}

function authHeaders(): Record<string, string> {
  const t = getDemoToken();
  return t ? { "X-Demo-Admin-Token": t } : {};
}

export type HealthResponse = {
  status: string;
  service: string;
  database: string;
  redis?: string;
  queue?: string;
  adaptive_policy?: {
    configured_mode: string;
    model_available: boolean;
    model_version: string | null;
    fingerprint: string | null;
    fingerprint_short: string | null;
    feature_schema_compatible: boolean;
  };
  llm?: {
    enabled: boolean;
    provider: string;
    configured: boolean;
    model: string | null;
    message_drafting: string;
    ptp_extraction: string;
    prompt_versions: { ptp_extraction: string; message_draft: string };
    schema_versions: { ptp: string; message: string };
  };
};

export type DashboardSummary = {
  revenue_at_risk: number;
  revenue_recovered: number;
  revenue_total: number;
  total_cases: number;
  open_cases: number;
  recovered_cases: number;
  waiting_cases: number;
  human_review_cases: number;
  stopped_cases: number;
  disputed_cases: number;
  active_ptps: number;
  by_state: Record<string, number>;
  by_category: Record<string, number>;
  policy_mode: string;
  model: {
    available: boolean;
    version: string | null;
    fingerprint: string | null;
    fingerprint_short: string | null;
    feature_schema_compatible: boolean;
  };
  friction: { profile: string; weight: number };
  queue: { status: string; name: string; enabled: boolean };
  razorpay: {
    api_enabled: boolean;
    key_configured: boolean;
    is_test_mode: boolean;
    mode_label: string;
    simulated: boolean;
  };
  llm: HealthResponse["llm"];
  database: string;
};

export type RevenueCase = {
  id: string;
  amount: number | null;
  currency?: string | null;
  state: string;
  failure_category: string | null;
  error_source: string | null;
  error_step?: string | null;
  error_reason: string | null;
  chosen_action: string | null;
  policy_mode?: string | null;
  friction_profile?: string | null;
  friction_score?: number | null;
  latest_action_type?: string | null;
  latest_action_status?: string | null;
  payment_link_state?: string | null;
  ptp_status?: string | null;
  contact_count?: number | null;
  razorpay_payment_id?: string | null;
  razorpay_subscription_id?: string | null;
  razorpay_payment_link_id: string | null;
  created_at: string | null;
  updated_at?: string | null;
};

export type CaseFilters = {
  state?: string;
  failure_category?: string;
  chosen_action?: string;
  policy_mode?: string;
  search?: string;
  limit?: number;
  offset?: number;
};

export type FailureExplanation = {
  failure_category: string;
  category_label: string;
  category_meaning: string;
  raw_signal: Record<string, string | null>;
  raw_signal_present: Record<string, string>;
  raw_examples: string;
};

export type CandidateScore = {
  action: string;
  allowed: boolean | null;
  blocked_reason: string | null;
  p_recovery: number | null;
  cost: number | null;
  expected_value: number | null;
  expected_recovered_value: number | null;
  friction_score: number | null;
  friction_weight: number | null;
  utility: number | null;
  friction_penalty: number | null;
  selected: boolean;
};

export type DecisionInspector = {
  decision_id: string;
  chosen_action: string;
  policy_mode: string | null;
  explanation: string | null;
  expected_value: number | null;
  created_at: string | null;
  guardrails: Record<string, unknown>;
  candidates: CandidateScore[];
  model_provenance: {
    policy_mode: string | null;
    model_version: string | null;
    model_fingerprint: string | null;
    friction_profile: string | null;
    friction_weight: number | null;
    _provenance?: Record<string, unknown>;
  };
  fallback: string | null;
  is_adaptive: boolean;
};

export type TimelineEvent = {
  timestamp: string | null;
  type: string;
  title: string;
  description: string;
  category: string;
  metadata: Record<string, unknown>;
  severity: string;
};

export type CaseDetail = {
  id: string;
  amount: number | null;
  currency: string | null;
  state: string;
  failure_category: string | null;
  error_source: string | null;
  error_step: string | null;
  error_reason: string | null;
  razorpay_payment_id: string | null;
  razorpay_subscription_id: string | null;
  razorpay_payment_link_id: string | null;
  source: string;
  created_at: string | null;
  updated_at: string | null;
  failure_explanation: FailureExplanation;
  guardrails: Record<string, unknown>;
  friction: {
    profile: string;
    weight: number | null;
    components: Array<{
      action: string;
      base: number;
      contact_increment: number;
      previous_contacts: number | null;
      total_friction: number;
      friction_penalty: number | null;
    }>;
    chosen_action_actual?: Record<string, unknown>;
  };
  explainability_scopes: {
    guardrails: "current_case_state";
    friction: "current_case_state";
    decision_inspectors: "persisted_decision_time";
  };
  provider_truth: {
    has_payment_link: boolean;
    payment_link_id: string | null;
    reference_id?: string | null;
    short_url: string | null;
    simulated: boolean | null;
    mode_label: string | null;
    reconciled: boolean;
    status: string;
    result?: Record<string, unknown>;
  };
  timeline: TimelineEvent[];
  shadow: {
    baseline_chosen: string | null;
    adaptive_suggested: string | null;
    disagreement: boolean;
    fingerprint: string | null;
    friction_profile: string | null;
    candidates: Record<string, unknown> | null;
    adaptive_utility: number | null;
  } | null;
  adaptive_fallback: { reason: string } | null;
  human_review_reason: { event: string; detail: Record<string, unknown> } | null;
  latest_action: { id: string; action_type: string; status: string; scheduled_for: string | null; executed_at: string | null } | null;
  decisions: Array<{
    id: string;
    chosen_action: string;
    expected_value: number | null;
    alternatives: Record<string, unknown> | null;
    guardrails_applied: Record<string, unknown> | null;
    explanation: string | null;
    policy_mode: string | null;
    model_version: string | null;
    model_fingerprint: string | null;
    friction_profile: string | null;
    friction_weight: number | null;
    created_at: string | null;
  }>;
  decision_inspectors: DecisionInspector[];
  actions: Array<{
    id: string;
    action_type: string;
    status: string;
    scheduled_for: string | null;
    executed_at: string | null;
    result: Record<string, unknown> | null;
    promise_to_pay_id: string | null;
    attempt_count: number;
    max_attempts: number;
    claimed_at: string | null;
    enqueued_at: string | null;
    queue_job_id: string | null;
    last_error: string | null;
    created_at: string | null;
  }>;
  messages: Array<{
    id: string;
    direction: string;
    channel: string | null;
    body: string;
    extracted: Record<string, unknown> | null;
    generation_method: string | null;
    llm_provider: string | null;
    llm_model: string | null;
    prompt_version: string | null;
    schema_version: string | null;
    status: string | null;
    created_at: string | null;
  }>;
  promises_to_pay: Array<{
    id: string;
    promised_amount: number | null;
    promised_date: string | null;
    confidence: number | null;
    status: string;
    extraction_method: string | null;
    llm_provider: string | null;
    llm_model: string | null;
    prompt_version: string | null;
    schema_version: string | null;
    amount_method: string | null;
    reasoning_code: string | null;
    source_message_id: string | null;
    created_at: string | null;
  }>;
  audit_trail: Array<{ event: string; detail: Record<string, unknown> | null; created_at: string | null }>;
  payment_events: Array<{ id: string; event_type: string; razorpay_event_id: string | null; razorpay_payment_id: string | null; razorpay_payment_link_id: string | null; received_at: string | null }>;
};

export type ArmSummary = {
  cases: number;
  amount_at_risk: number;
  amount_recovered: number;
  recovery_rate: number;
  contacts: number;
  contact_rate?: number;
  contacts_per_100?: number;
  escalations: number;
  waits?: number;
  native_retry_waits?: number;
  payment_links?: number;
  ptps?: number;
  action_cost_proxy: number;
  friction_score?: number;
  utility?: number;
  realized_policy_utility?: number;
  realized_net_value: number;
  recovered_per_contact?: number;
  action_distribution: Record<string, number>;
};

export type ExperimentSummary = {
  run_id: string;
  found: boolean;
  case_count: number;
  created_at: string | null;
  scenario_count?: number;
  resolved_seed?: number;
  evaluation_friction_weight?: number;
  evaluation_friction_profile?: string;
  model_version?: string | null;
  model_fingerprint?: string | null;
  feature_schema_version?: string | null;
  arms: {
    baseline: ArmSummary;
    adaptive: ArmSummary;
  };
  incremental_recovered: number;
};

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { headers: { ...authHeaders() } });
  if (!res.ok) {
    const text = await res.text();
    // Never surface raw stack trace — truncate
    throw new Error(`${path} returned ${res.status}: ${text.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${path} returned ${res.status}: ${text.slice(0, 300)}`);
  }
  return res.json() as Promise<T>;
}

async function downloadExperimentCsv(runId: string): Promise<void> {
  const path = `/experiments/${runId}/export.csv`;
  const res = await fetch(`${API_BASE}${path}`, { headers: { ...authHeaders() } });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${path} returned ${res.status}: ${text.slice(0, 200)}`);
  }
  const url = URL.createObjectURL(await res.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = `recoveryos_experiment_${runId}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

export function setDemoToken(token: string | null) {
  try {
    if (token) sessionStorage.setItem("demo_admin_token", token);
    else sessionStorage.removeItem("demo_admin_token");
  } catch {}
}
export function getDemoTokenValue() { return getDemoToken(); }

function buildCaseQuery(filters: CaseFilters): string {
  const params = new URLSearchParams();
  if (filters.state) params.set("state", filters.state);
  if (filters.failure_category) params.set("failure_category", filters.failure_category);
  if (filters.chosen_action) params.set("chosen_action", filters.chosen_action);
  if (filters.policy_mode) params.set("policy_mode", filters.policy_mode);
  if (filters.search) params.set("search", filters.search);
  if (filters.limit !== undefined) params.set("limit", String(filters.limit));
  if (filters.offset !== undefined) params.set("offset", String(filters.offset));
  const q = params.toString();
  return q ? `/cases?${q}` : "/cases";
}

export const api = {
  health: () => get<HealthResponse>("/health"),
  readiness: () => get<Record<string, unknown>>("/ready"),
  liveness: () => get<Record<string, unknown>>("/live"),
  dashboardSummary: () => get<DashboardSummary>("/dashboard/summary"),
  cases: (filters?: CaseFilters) => get<RevenueCase[]>(filters ? buildCaseQuery(filters) : "/cases"),
  caseDetail: (id: string) => get<CaseDetail>(`/cases/${id}`),
  runExperiment: (count: number, seed?: number) =>
    post<ExperimentSummary>("/experiments", { count, seed: seed ?? null }),
  experiment: (runId: string) => get<ExperimentSummary>(`/experiments/${runId}`),
  experiments: () => get<Array<{ run_id: string; created_at: string | null }>>("/experiments"),
  downloadExperimentCsv,
  demoReset: () => post<{status:string; deleted_cases:number}>("/admin/demo-reset", {}),
  demoCheck: () => get<{demo_cases: unknown[]; count:number}>("/admin/demo-check"),
};
