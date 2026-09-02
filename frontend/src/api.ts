const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export type HealthResponse = {
  status: string;
  service: string;
  database: string;
};

export type RevenueCase = {
  id: string;
  amount: number | null;
  state: string;
  failure_category: string | null;
  error_source: string | null;
  error_reason: string | null;
  chosen_action: string | null;
  razorpay_payment_link_id: string | null;
  created_at: string | null;
};

export type ArmSummary = {
  cases: number;
  amount_at_risk: number;
  amount_recovered: number;
  recovery_rate: number;
  contacts: number;
  escalations: number;
  action_cost_proxy: number;
  realized_net_value: number;
  action_distribution: Record<string, number>;
};

export type ExperimentSummary = {
  run_id: string;
  found: boolean;
  case_count: number;
  created_at: string | null;
  arms: {
    baseline: ArmSummary;
    adaptive: ArmSummary;
  };
  incremental_recovered: number;
};

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    throw new Error(`${path} returned ${res.status}`);
  }
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    throw new Error(`${path} returned ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => get<HealthResponse>("/health"),
  cases: () => get<RevenueCase[]>("/cases"),
  runExperiment: (count: number, seed?: number) =>
    post<ExperimentSummary>("/experiments", { count, seed: seed ?? null }),
  experimentCsvUrl: (runId: string) => `${API_BASE}/experiments/${runId}/export.csv`,
};
