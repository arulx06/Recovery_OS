import type { DashboardSummary, HealthResponse } from "../api";
import { Badge, Card, humanize } from "./ui";

function statusTone(status: string | undefined) {
  if (["ok", "connected", "ready", "recoveryos"].includes((status ?? "").toLowerCase())) return "success" as const;
  if (["disabled", "unavailable"].includes((status ?? "").toLowerCase())) return "muted" as const;
  return "warning" as const;
}

function RuntimeRow({ name, status, detail }: { name: string; status: string; detail: string }) {
  return (
    <div className="flex items-center justify-between gap-4 border-b border-[#242d3b] py-3 last:border-0 last:pb-0 first:pt-0">
      <div><div className="text-sm font-semibold text-slate-200">{name}</div><div className="mt-0.5 text-xs text-slate-500">{detail}</div></div>
      <Badge tone={statusTone(status)}>{humanize(status)}</Badge>
    </div>
  );
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
    return <Card><h2 className="text-xl font-semibold">Runtime unavailable</h2><p className="mt-2 text-sm text-rose-300">The frontend cannot reach the RecoveryOS API.</p></Card>;
  }
  if (!health && !dashboard) return <Card><div className="text-sm text-slate-500">Loading runtime state…</div></Card>;

  const policy = health?.adaptive_policy;
  const llm = health?.llm ?? dashboard?.llm;
  const queueStatus = dashboard?.queue.enabled ? dashboard.queue.status : "disabled";
  return (
    <div className="space-y-5" data-testid="system-page">
      <div>
        <p className="text-sm font-semibold uppercase tracking-[0.16em] text-blue-300">System</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight text-white">Operational state and authority</h1>
        <p className="mt-2 text-sm text-slate-400">Detailed infrastructure, policy, and integration metadata lives here, away from the recovery workflow.</p>
      </div>

      <div className="grid gap-5 lg:grid-cols-2 xl:grid-cols-4">
        <Card>
          <div className="mb-5"><div className="text-sm font-semibold uppercase tracking-[0.12em] text-slate-500">Runtime</div><h2 className="mt-1 text-xl font-semibold text-white">Core services</h2></div>
          <RuntimeRow name="API" status={health?.status ?? "unknown"} detail={health?.service ?? "RecoveryOS backend"} />
          <RuntimeRow name="PostgreSQL" status={health?.database ?? dashboard?.database ?? "unknown"} detail="Application source of truth" />
          <RuntimeRow name="Redis" status={health?.redis ?? "unknown"} detail="Reconstructable transport" />
          <RuntimeRow name="Worker queue" status={queueStatus ?? "unknown"} detail={health?.queue ?? dashboard?.queue.name ?? "recoveryos"} />
        </Card>

        <Card>
          <div className="mb-5"><div className="text-sm font-semibold uppercase tracking-[0.12em] text-slate-500">Decision system</div><h2 className="mt-1 text-xl font-semibold text-white">Policy and scoring</h2></div>
          <RuntimeRow name="Policy mode" status={dashboard?.policy_mode ?? policy?.configured_mode ?? "baseline"} detail="Baseline, shadow, or adaptive" />
          <RuntimeRow name="Recovery model" status={policy?.model_available ?? dashboard?.model.available ? "ready" : "unavailable"} detail={`${policy?.model_version ?? dashboard?.model.version ?? "No model"} · ${policy?.fingerprint_short ?? dashboard?.model.fingerprint_short ?? "no fingerprint"}`} />
          <RuntimeRow name="Friction profile" status="ready" detail={`${dashboard?.friction.profile ?? "—"} · weight ${dashboard?.friction.weight ?? "—"}`} />
        </Card>

        <Card>
          <div className="mb-5"><div className="text-sm font-semibold uppercase tracking-[0.12em] text-slate-500">External integrations</div><h2 className="mt-1 text-xl font-semibold text-white">Provider boundaries</h2></div>
          <RuntimeRow name="Razorpay" status={dashboard?.razorpay.is_test_mode ? "ready" : dashboard?.razorpay.simulated ? "disabled" : "unavailable"} detail={dashboard?.razorpay.mode_label ?? "Unavailable"} />
          <RuntimeRow name="Language model" status={llm?.enabled ? "ready" : "disabled"} detail={llm?.enabled ? `${llm.provider} · ${llm.model ?? "model"}` : "Deterministic fallback active"} />
          <div className="mt-4 rounded-lg bg-slate-800/50 px-3 py-3 text-xs leading-relaxed text-slate-400">Razorpay events establish payment truth. The optional LLM assists language only.</div>
        </Card>

        <Card>
          <div className="mb-5"><div className="text-sm font-semibold uppercase tracking-[0.12em] text-slate-500">Safety</div><h2 className="mt-1 text-xl font-semibold text-white">Demo boundaries</h2></div>
          <div className="space-y-3">
            {["Razorpay Test Mode only", "Messages remain DRAFT / NOT SENT", "Synthetic evaluation data", "Deterministic guardrails are authoritative"].map((item) => (
              <div key={item} className="flex items-start gap-3 text-sm text-slate-300"><span className="mt-0.5 text-emerald-300">✓</span><span>{item}</span></div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
