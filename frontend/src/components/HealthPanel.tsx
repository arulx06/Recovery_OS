import type { DashboardSummary, HealthResponse } from "../api";
import { Badge, Card, SectionTitle } from "./ui";

function readiness(pill: string) {
  if (pill === "connected" || pill === "ok") return "READY";
  if (pill === "disabled") return "DISABLED";
  return "DEGRADED";
}

function toneFor(status: string) {
  if (status === "connected" || status === "ok" || status === "READY") return "success" as const;
  if (status === "disabled") return "muted" as const;
  return "warning" as const;
}

export function HealthPanel({
  health,
  dashboard,
  healthError,
}: {
  health: HealthResponse | null;
  dashboard: DashboardSummary | null;
  healthError: boolean;
}) {
  if (healthError) {
    return (
      <Card>
        <SectionTitle>System health</SectionTitle>
        <div className="text-sm text-red-300">Backend unreachable — check that uvicorn is running on {":8000"} and DATABASE_URL is valid.</div>
      </Card>
    );
  }
  if (!health && !dashboard) {
    return (
      <Card>
        <SectionTitle>System health</SectionTitle>
        <div className="text-sm text-gray-500">Loading health…</div>
      </Card>
    );
  }
  const db = health?.database ?? dashboard?.database ?? "—";
  const redis = health?.redis ?? dashboard?.queue.status ?? "—";
  const queue = health?.queue ?? dashboard?.queue.name ?? "—";
  const policy = health?.adaptive_policy ?? (dashboard ? { configured_mode: dashboard.policy_mode, model_available: dashboard.model.available, model_version: dashboard.model.version, fingerprint: dashboard.model.fingerprint, fingerprint_short: dashboard.model.fingerprint_short, feature_schema_compatible: dashboard.model.feature_schema_compatible } : undefined);
  const llm = health?.llm ?? dashboard?.llm;
  const razorpay = dashboard?.razorpay;

  return (
    <Card>
      <SectionTitle subtitle="PostgreSQL is authoritative; Redis/RQ is reconstructable transport">System health & readiness</SectionTitle>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {[
          { label: "Backend", value: health?.status ?? "unknown", sub: health?.service ?? "recoveryos-backend" },
          { label: "Database", value: readiness(db), sub: db },
          { label: "Redis", value: readiness(redis), sub: redis },
          { label: "Queue", value: dashboard?.queue.enabled ? queue : "disabled", sub: dashboard?.queue.enabled ? "enabled" : "TASK_QUEUE_ENABLED=false" },
        ].map((r) => (
          <div key={r.label} className="rounded-lg border border-white/10 bg-black/20 p-3">
            <div className="text-[11px] uppercase tracking-wide text-gray-500">{r.label}</div>
            <div className="mt-1 flex items-center gap-2">
              <Badge tone={toneFor(r.value)} size="sm">{r.value}</Badge>
            </div>
            <div className="mt-1 text-[11px] text-gray-500 truncate">{r.sub}</div>
          </div>
        ))}
      </div>

      <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
        <div className="rounded-lg border border-white/10 bg-black/20 p-3">
          <div className="text-xs font-medium text-gray-300">Adaptive policy</div>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <Badge tone="info">{policy?.configured_mode ?? "baseline"}</Badge>
            {policy?.model_available ? (
              <Badge tone="success">model ready</Badge>
            ) : (
              <Badge tone="muted">model unavailable → baseline fallback</Badge>
            )}
          </div>
          <div className="mt-2 text-[11px] text-gray-500">
            model: {policy?.model_version ?? "none"} {policy?.fingerprint_short ? `· ${policy?.fingerprint_short}` : ""}
          </div>
          {dashboard?.friction && (
            <div className="mt-1 text-[11px] text-gray-500">
              friction: {dashboard.friction.profile} × {dashboard.friction.weight}
            </div>
          )}
          <div className="mt-1 text-[11px] text-gray-600">fingerprint identifies exact trained artifact</div>
        </div>

        <div className="rounded-lg border border-white/10 bg-black/20 p-3">
          <div className="text-xs font-medium text-gray-300">Razorpay integration</div>
          <div className="mt-2">
            <Badge tone={razorpay?.mode_label?.includes("TEST MODE") ? "success" : razorpay?.simulated ? "muted" : "warning"}>{razorpay?.mode_label ?? "unavailable"}</Badge>
          </div>
          <div className="mt-2 text-[11px] text-gray-500">
            {razorpay?.simulated ? "SIMULATED LINK — https://rzp.io/simulated/plink_sim_* — not live money" : razorpay?.is_test_mode ? "Razorpay Test Mode — genuine plink_* + rzp.io URL — TEST MODE ≠ production" : "—"}
          </div>
          <div className="mt-1 text-[11px] text-gray-600">Provider truth via Razorpay webhooks</div>
        </div>

        <div className="rounded-lg border border-white/10 bg-black/20 p-3">
          <div className="text-xs font-medium text-gray-300">Customer intelligence (LLM)</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {llm?.enabled ? (
              <>
                <Badge tone="success">{llm.provider}/{llm.model ?? "—"}</Badge>
                <Badge tone={llm.message_drafting === "available" ? "success" : llm.message_drafting === "disabled" ? "muted" : "warning"}>draft: {llm.message_drafting}</Badge>
                <Badge tone={llm.ptp_extraction === "available" ? "success" : llm.ptp_extraction === "disabled" ? "muted" : "warning"}>PTP: {llm.ptp_extraction}</Badge>
              </>
            ) : (
              <Badge tone="muted">disabled — deterministic fallback</Badge>
            )}
          </div>
          <div className="mt-2 text-[11px] text-gray-500">
            drafts: DRAFT / NOT SENT — manual only · prompt: {llm?.prompt_versions.message_draft ?? "message-v1"} / {llm?.prompt_versions.ptp_extraction ?? "ptp-v1"}
          </div>
          <div className="mt-1 text-[11px] text-gray-600">LLM never controls money moves</div>
        </div>
      </div>
    </Card>
  );
}
