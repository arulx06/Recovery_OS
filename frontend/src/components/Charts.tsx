import type { ArmSummary } from "../api";
import { formatRupees } from "./ui";

function barWidth(value: number, max: number) {
  if (max <= 0) return "0%";
  return `${Math.max(0, Math.min(100, (value / max) * 100))}%`;
}

function humanizeAction(action: string) {
  const labels: Record<string, string> = {
    WAIT: "Wait",
    WAIT_FOR_NATIVE_RETRY: "Wait for native retry",
    CREATE_PAYMENT_LINK: "Create payment link",
    COLLECT_PROMISE_TO_PAY: "Collect promise to pay",
    CONTACT_CUSTOMER: "Contact customer",
    ESCALATE: "Escalate to specialist",
    STOP: "Stop recovery",
  };
  return labels[action] ?? action.toLowerCase().replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

export function RevenueFrictionChart({ baseline, adaptive }: { baseline: ArmSummary; adaptive: ArmSummary }) {
  const maxRecovered = Math.max(baseline.amount_recovered, adaptive.amount_recovered, 1);
  const hasFriction = baseline.friction_score !== undefined && adaptive.friction_score !== undefined;
  const baselineFriction = baseline.friction_score ?? 0;
  const adaptiveFriction = adaptive.friction_score ?? 0;
  const maxFriction = Math.max(baselineFriction, adaptiveFriction, 1);
  const recoveredDelta = adaptive.amount_recovered - baseline.amount_recovered;
  const frictionDelta = adaptiveFriction - baselineFriction;

  return (
    <section className="rounded-2xl border border-white/10 bg-gradient-to-br from-slate-900/90 to-black/30 p-4" aria-labelledby="tradeoff-title">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <div className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Decision trade-off</div>
          <h3 id="tradeoff-title" className="mt-1 text-base font-semibold text-white">Revenue recovered vs customer friction</h3>
        </div>
        <div className="flex items-center gap-3 text-xs text-slate-500">
          <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-slate-500" />Baseline</span>
          <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-cyan-400" />Adaptive</span>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="rounded-xl border border-emerald-400/15 bg-emerald-400/[0.04] p-3.5">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[13px] font-medium text-slate-300">Revenue recovered</span>
            <span className={`font-mono text-xs ${recoveredDelta >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
              {recoveredDelta === 0 ? "No difference" : `${recoveredDelta > 0 ? "+" : "−"}${formatRupees(Math.abs(recoveredDelta))} adaptive`}
            </span>
          </div>
          <div className="mt-4 space-y-3">
            <div>
              <div className="mb-1.5 flex items-center justify-between text-[13px]">
                <span className="text-slate-500">Baseline</span>
                <span className="font-mono text-slate-300">{formatRupees(baseline.amount_recovered)}</span>
              </div>
              <div className="h-2.5 overflow-hidden rounded-full bg-white/[0.06]">
                <div className="h-full rounded-full bg-slate-500" style={{ width: barWidth(baseline.amount_recovered, maxRecovered) }} />
              </div>
            </div>
            <div>
              <div className="mb-1.5 flex items-center justify-between text-[13px]">
                <span className="font-medium text-cyan-300">Adaptive</span>
                <span className="font-mono font-semibold text-cyan-100">{formatRupees(adaptive.amount_recovered)}</span>
              </div>
              <div className="h-2.5 overflow-hidden rounded-full bg-white/[0.06]">
                <div className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-emerald-400" style={{ width: barWidth(adaptive.amount_recovered, maxRecovered) }} />
              </div>
            </div>
          </div>
        </div>

        <div className="rounded-xl border border-amber-400/15 bg-amber-400/[0.04] p-3.5">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[13px] font-medium text-slate-300">Customer friction</span>
            <span className={`font-mono text-xs ${!hasFriction || frictionDelta === 0 ? "text-slate-500" : frictionDelta < 0 ? "text-emerald-300" : "text-amber-300"}`}>
              {!hasFriction ? "Not reported" : frictionDelta === 0 ? "No difference" : `${frictionDelta > 0 ? "+" : "−"}${Math.abs(frictionDelta).toLocaleString("en-IN")} adaptive`}
            </span>
          </div>
          <div className="mt-4 space-y-3">
            <div>
              <div className="mb-1.5 flex items-center justify-between text-[13px]">
                <span className="text-slate-500">Baseline</span>
                <span className="font-mono text-slate-300">{baseline.friction_score?.toLocaleString("en-IN") ?? "—"}</span>
              </div>
              <div className="h-2.5 overflow-hidden rounded-full bg-white/[0.06]">
                <div className="h-full rounded-full bg-slate-500" style={{ width: barWidth(baselineFriction, maxFriction) }} />
              </div>
            </div>
            <div>
              <div className="mb-1.5 flex items-center justify-between text-[13px]">
                <span className="font-medium text-cyan-300">Adaptive</span>
                <span className="font-mono font-semibold text-cyan-100">{adaptive.friction_score?.toLocaleString("en-IN") ?? "—"}</span>
              </div>
              <div className="h-2.5 overflow-hidden rounded-full bg-white/[0.06]">
                <div className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-amber-400" style={{ width: barWidth(adaptiveFriction, maxFriction) }} />
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

export function ActionDistribution({ baseline, adaptive }: { baseline: ArmSummary; adaptive: ArmSummary }) {
  const allActions = Array.from(new Set([...Object.keys(baseline.action_distribution), ...Object.keys(adaptive.action_distribution)]))
    .sort((left, right) => {
      const leftTotal = (baseline.action_distribution[left] ?? 0) + (adaptive.action_distribution[left] ?? 0);
      const rightTotal = (baseline.action_distribution[right] ?? 0) + (adaptive.action_distribution[right] ?? 0);
      return rightTotal - leftTotal;
    });
  const maxCount = Math.max(1, ...Object.values(baseline.action_distribution), ...Object.values(adaptive.action_distribution));

  return (
    <div>
      <p className="mb-4 text-[13px] text-slate-500">Chosen actions across matched scenarios, ordered by total usage.</p>
      <div className="grid grid-cols-1 gap-x-8 gap-y-4 lg:grid-cols-2">
        {allActions.map((action) => {
          const baselineCount = baseline.action_distribution[action] ?? 0;
          const adaptiveCount = adaptive.action_distribution[action] ?? 0;
          return (
            <div key={action}>
              <div className="mb-2 text-[13px] font-medium text-slate-300" title={action}>{humanizeAction(action)}</div>
              <div className="space-y-1.5">
                <div className="grid grid-cols-[68px_1fr_32px] items-center gap-2 text-xs">
                  <span className="text-slate-600">Baseline</span>
                  <div className="h-2 overflow-hidden rounded-full bg-white/[0.05]">
                    <div className="h-full rounded-full bg-slate-500" style={{ width: barWidth(baselineCount, maxCount) }} />
                  </div>
                  <span className="text-right font-mono text-slate-500">{baselineCount}</span>
                </div>
                <div className="grid grid-cols-[68px_1fr_32px] items-center gap-2 text-xs">
                  <span className="text-cyan-500">Adaptive</span>
                  <div className="h-2 overflow-hidden rounded-full bg-white/[0.05]">
                    <div className="h-full rounded-full bg-cyan-400" style={{ width: barWidth(adaptiveCount, maxCount) }} />
                  </div>
                  <span className="text-right font-mono text-cyan-300">{adaptiveCount}</span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
