import type { ArmSummary } from "../api";
import { formatRupees } from "./ui";

export function RevenueFrictionChart({ baseline, adaptive }: { baseline: ArmSummary; adaptive: ArmSummary }) {
  // Simple side-by-side bars: contact rate vs recovery rate vs friction vs utility
  // Use SVG for no dependency
  const maxFriction = Math.max(baseline.friction_score ?? 0, adaptive.friction_score ?? 0, 1);
  const maxRecovered = Math.max(baseline.amount_recovered, adaptive.amount_recovered, 1);
  const bRecPct = (baseline.amount_recovered / Math.max(baseline.amount_at_risk, 1)) * 100;
  const aRecPct = (adaptive.amount_recovered / Math.max(adaptive.amount_at_risk, 1)) * 100;
  const bContact = baseline.contact_rate ?? 0;
  const aContact = adaptive.contact_rate ?? 0;

  const rows = [
    { label: "Recovery rate", b: bRecPct, a: aRecPct, unit: "%", max: 100 },
    { label: "Contact rate", b: bContact, a: aContact, unit: "%", max: 100 },
    { label: "Friction score", b: baseline.friction_score ?? 0, a: adaptive.friction_score ?? 0, unit: "", max: maxFriction },
    { label: "Recovered", b: baseline.amount_recovered, a: adaptive.amount_recovered, unit: "₹", max: maxRecovered },
  ];

  return (
    <div className="space-y-3">
      <div className="text-[11px] uppercase tracking-wide text-gray-500">Revenue vs friction tradeoff</div>
      <div className="rounded-lg border border-white/10 bg-black/20 p-3">
        <div className="grid gap-3">
          {rows.map((r) => (
            <div key={r.label}>
              <div className="mb-1 flex items-center justify-between text-[11px]">
                <span className="text-gray-400">{r.label}</span>
                <span className="font-mono text-[11px] text-gray-500">
                  B {r.unit === "₹" ? formatRupees(r.b) : r.unit === "%" ? `${r.b.toFixed(1)}%` : String(Math.round(r.b))} · A {r.unit === "₹" ? formatRupees(r.a) : r.unit === "%" ? `${r.a.toFixed(1)}%` : String(Math.round(r.a))}
                </span>
              </div>
              <div className="flex gap-2">
                <div className="flex-1">
                  <div className="flex items-center gap-1">
                    <span className="w-6 text-[10px] text-gray-600">B</span>
                    <div className="h-2 flex-1 rounded bg-white/5">
                      <div className="h-2 rounded bg-gray-500" style={{ width: `${Math.min(100, (r.b / r.max) * 100)}%` }} />
                    </div>
                  </div>
                  <div className="mt-1 flex items-center gap-1">
                    <span className="w-6 text-[10px] font-medium text-sky-400">A</span>
                    <div className="h-2 flex-1 rounded bg-white/5">
                      <div className="h-2 rounded bg-sky-500" style={{ width: `${Math.min(100, (r.a / r.max) * 100)}%` }} />
                    </div>
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-3 text-[11px] leading-relaxed text-gray-500">
          Adaptive explicitly prices customer friction. More contact can recover more, but utility = <span className="font-mono text-gray-400">P×amount − cost − weight×friction</span>. Judge story: balanced friction reduces contacts while keeping recovery.
        </div>
      </div>
    </div>
  );
}

export function ActionDistribution({ baseline, adaptive }: { baseline: ArmSummary; adaptive: ArmSummary }) {
  const allActions = Array.from(new Set([...Object.keys(baseline.action_distribution), ...Object.keys(adaptive.action_distribution)])).sort();
  const maxCount = Math.max(
    1,
    ...Object.values(baseline.action_distribution),
    ...Object.values(adaptive.action_distribution)
  );
  return (
    <div className="space-y-3">
      <div className="text-[11px] uppercase tracking-wide text-gray-500">Action distribution — baseline vs adaptive</div>
      <div className="rounded-lg border border-white/10 bg-black/20 p-3">
        <div className="space-y-2">
          {allActions.map((action) => {
            const b = baseline.action_distribution[action] ?? 0;
            const a = adaptive.action_distribution[action] ?? 0;
            return (
              <div key={action} className="grid grid-cols-[110px_1fr] items-center gap-2 text-xs">
                <span className="font-mono text-[11px] text-gray-400 truncate" title={action}>{action}</span>
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <span className="w-4 text-[10px] text-gray-600">B</span>
                    <div className="h-2 flex-1 rounded bg-white/5">
                      <div className="h-2 rounded bg-gray-500" style={{ width: `${(b / maxCount) * 100}%` }} />
                    </div>
                    <span className="w-6 text-right font-mono text-[11px] text-gray-400">{b}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="w-4 text-[10px] font-medium text-sky-400">A</span>
                    <div className="h-2 flex-1 rounded bg-white/5">
                      <div className="h-2 rounded bg-sky-500" style={{ width: `${(a / maxCount) * 100}%` }} />
                    </div>
                    <span className="w-6 text-right font-mono text-[11px] text-sky-300">{a}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 text-[11px]">
          <div className="rounded bg-white/5 px-2 py-1">
            <span className="text-gray-500">Baseline</span>{" "}
            <span className="font-mono text-gray-300">
              {baseline.cases} cases · {formatRupees(baseline.amount_recovered)} · friction {baseline.friction_score ?? "—"}
            </span>
          </div>
          <div className="rounded bg-sky-900/30 px-2 py-1">
            <span className="text-sky-300">Adaptive</span>{" "}
            <span className="font-mono text-sky-200">
              {adaptive.cases} cases · {formatRupees(adaptive.amount_recovered)} · friction {adaptive.friction_score ?? "—"}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
