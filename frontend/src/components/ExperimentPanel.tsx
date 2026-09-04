import { useState, useEffect, useCallback } from "react";
import { api } from "../api";
import type { ArmSummary, ExperimentSummary } from "../api";
import { Card, ErrorState, formatRupees } from "./ui";
import { RevenueFrictionChart, ActionDistribution } from "./Charts";

function formatPercent(value: number | undefined) {
  return value === undefined ? "—" : `${value.toFixed(1)}%`;
}

function formatSignedRupees(value: number) {
  if (value === 0) return formatRupees(0);
  return `${value > 0 ? "+" : "−"}${formatRupees(Math.abs(value))}`;
}

function formatDelta(value: number, suffix = "") {
  if (value === 0) return "No change";
  return `${value > 0 ? "+" : "−"}${Math.abs(value).toFixed(1)}${suffix}`;
}

function buildComparisonStatement(result: ExperimentSummary) {
  const { baseline, adaptive } = result.arms;
  const recoveredDelta = result.incremental_recovered;
  const recovery = recoveredDelta > 0
    ? `recovered ${formatRupees(recoveredDelta)} more`
    : recoveredDelta < 0
      ? `recovered ${formatRupees(Math.abs(recoveredDelta))} less`
      : "recovered the same amount";

  if (baseline.friction_score === undefined || adaptive.friction_score === undefined) {
    return `Adaptive ${recovery}; customer friction was not reported.`;
  }

  const frictionDelta = adaptive.friction_score - baseline.friction_score;
  if (frictionDelta === 0) return `Adaptive ${recovery} with the same customer friction.`;

  if (baseline.friction_score > 0) {
    const frictionPercent = (Math.abs(frictionDelta) / baseline.friction_score) * 100;
    return `Adaptive ${recovery} with ${frictionPercent.toFixed(1)}% ${frictionDelta < 0 ? "lower" : "higher"} customer friction.`;
  }

  return `Adaptive ${recovery} with ${Math.abs(frictionDelta).toLocaleString("en-IN")} ${frictionDelta < 0 ? "fewer" : "more"} friction points.`;
}

function ComparisonRow({
  label,
  baseline,
  adaptive,
  delta,
  lowerIsBetter = false,
}: {
  label: string;
  baseline: string;
  adaptive: string;
  delta: string;
  lowerIsBetter?: boolean;
}) {
  return (
    <div className="grid grid-cols-[1.2fr_1fr_1fr] items-center gap-3 border-t border-white/[0.07] px-4 py-3 text-sm first:border-t-0 sm:grid-cols-[minmax(120px,1.2fr)_minmax(100px,1fr)_minmax(100px,1fr)_minmax(90px,.8fr)]">
      <div className="font-medium text-slate-300">
        {label}
        {lowerIsBetter && <span className="ml-1.5 text-xs font-normal text-slate-600">lower is better</span>}
      </div>
      <div className="font-mono text-slate-300">{baseline}</div>
      <div className="font-mono font-semibold text-cyan-200">{adaptive}</div>
      <div className="hidden text-right font-mono text-xs text-slate-500 sm:block">{delta}</div>
    </div>
  );
}

function SecondaryArmMetrics({ label, arm, adaptive = false }: { label: string; arm: ArmSummary; adaptive?: boolean }) {
  return (
    <div className={`rounded-xl border p-4 ${adaptive ? "border-cyan-400/20 bg-cyan-400/[0.04]" : "border-white/10 bg-black/20"}`}>
      <div className={`mb-3 text-sm font-semibold ${adaptive ? "text-cyan-200" : "text-slate-200"}`}>{label}</div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-[13px]">
        <dt className="text-slate-500">Amount at risk</dt>
        <dd className="text-right font-mono text-slate-300">{formatRupees(arm.amount_at_risk)}</dd>
        <dt className="text-slate-500">Contact actions</dt>
        <dd className="text-right font-mono text-slate-300">{arm.contacts.toLocaleString("en-IN")}</dd>
        <dt className="text-slate-500">Escalations</dt>
        <dd className="text-right font-mono text-slate-300">{arm.escalations.toLocaleString("en-IN")}</dd>
        <dt className="text-slate-500">Action cost proxy</dt>
        <dd className="text-right font-mono text-slate-300">{formatRupees(arm.action_cost_proxy)}</dd>
        <dt className="text-slate-500">Realized net value</dt>
        <dd className="text-right font-mono text-slate-300">{formatRupees(arm.realized_net_value)}</dd>
        {arm.realized_policy_utility !== undefined && (
          <>
            <dt className="text-slate-500">Policy utility</dt>
            <dd className="text-right font-mono text-slate-300">{formatRupees(arm.realized_policy_utility)}</dd>
          </>
        )}
        {arm.recovered_per_contact !== undefined && (
          <>
            <dt className="text-slate-500">Recovered / contact</dt>
            <dd className="text-right font-mono text-slate-300">{formatRupees(arm.recovered_per_contact)}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

export default function ExperimentPanel() {
  const [count, setCount] = useState(100);
  const [seed, setSeed] = useState<string>("11");
  const [running, setRunning] = useState(false);
  const [loadingRunId, setLoadingRunId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ExperimentSummary | null>(null);
  const [recent, setRecent] = useState<Array<{ run_id: string; created_at: string | null }>>([]);

  const loadRecent = useCallback(async () => {
    try {
      const r = await api.experiments();
      setRecent(r.slice(0, 10));
    } catch {
      /* Recent runs are supplementary; the experiment remains usable without them. */
    }
  }, []);

  useEffect(() => {
    loadRecent();
  }, [loadRecent]);

  const run = async () => {
    setRunning(true);
    setError(null);
    try {
      const seedNum = seed.trim() === "" ? undefined : Number(seed);
      if (seed.trim() !== "" && Number.isNaN(seedNum)) throw new Error("seed must be a number");
      const summary = await api.runExperiment(count, seedNum);
      setResult(summary);
      loadRecent();
    } catch (e) {
      setError(e instanceof Error ? e.message : "experiment failed");
    } finally {
      setRunning(false);
    }
  };

  const loadRun = async (runId: string) => {
    setLoadingRunId(runId);
    setError(null);
    try {
      const summary = await api.experiment(runId);
      setResult(summary);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load run");
    } finally {
      setLoadingRunId(null);
    }
  };

  const baseline = result?.arms.baseline;
  const adaptive = result?.arms.adaptive;
  const hasPolicyUtility = baseline?.realized_policy_utility !== undefined && adaptive?.realized_policy_utility !== undefined;
  const baselineScore = baseline ? (hasPolicyUtility ? (baseline.realized_policy_utility ?? baseline.realized_net_value) : baseline.realized_net_value) : 0;
  const adaptiveScore = adaptive ? (hasPolicyUtility ? (adaptive.realized_policy_utility ?? adaptive.realized_net_value) : adaptive.realized_net_value) : 0;
  const leadingPolicy = adaptiveScore > baselineScore ? "Adaptive" : baselineScore > adaptiveScore ? "Baseline" : null;
  const frictionDelta = baseline?.friction_score !== undefined && adaptive?.friction_score !== undefined
    ? adaptive.friction_score - baseline.friction_score
    : undefined;
  const contactDelta = baseline?.contact_rate !== undefined && adaptive?.contact_rate !== undefined
    ? adaptive.contact_rate - baseline.contact_rate
    : undefined;

  return (
    <Card padding="p-0" className="overflow-hidden border-slate-700/70 bg-[#08111f] shadow-2xl shadow-cyan-950/20">
      <div className="border-b border-amber-300/20 bg-gradient-to-r from-amber-400/15 via-amber-300/[0.07] to-transparent px-5 py-2.5 text-center text-xs font-semibold tracking-[0.18em] text-amber-200">
        SYNTHETIC SIMULATION / NOT PRODUCTION LIFT
      </div>

      <div className="border-b border-white/[0.07] px-5 py-4">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <div className="mb-1 text-xs font-medium uppercase tracking-[0.2em] text-cyan-400">Policy decision room</div>
            <h2 className="text-xl font-semibold tracking-tight text-white">Baseline vs adaptive recovery</h2>
            <p className="mt-1 text-base text-slate-400">Matched scenarios. Same random outcomes. One auditable policy comparison.</p>
          </div>
          <div className="flex flex-wrap items-end gap-2.5">
            <label className="text-[13px] font-medium text-slate-400">
              <span className="mb-1 block">cases</span>
              <input
                type="number"
                min={1}
                max={1000}
                value={count}
                onChange={(e) => setCount(Number(e.target.value))}
                className="w-24 rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-cyan-400/60 focus:ring-2 focus:ring-cyan-400/10"
              />
            </label>
            <label className="text-[13px] font-medium text-slate-400">
              <span className="mb-1 block">seed</span>
              <input
                type="text"
                value={seed}
                onChange={(e) => setSeed(e.target.value)}
                placeholder="random"
                className="w-24 rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-cyan-400/60 focus:ring-2 focus:ring-cyan-400/10"
              />
            </label>
            <button
              type="button"
              onClick={run}
              disabled={running}
              className="h-[38px] rounded-lg bg-cyan-400 px-4 text-sm font-semibold text-slate-950 shadow-lg shadow-cyan-950/50 transition hover:bg-cyan-300 disabled:cursor-wait disabled:opacity-60"
            >
              {running ? "Running…" : "Run experiment"}
            </button>
          </div>
        </div>

        {recent.length > 0 && (
          <div className="mt-3 flex flex-wrap items-center gap-1.5 text-xs" aria-label="Recent experiment runs">
            <span className="mr-1 text-slate-600">Recent runs</span>
            {recent.map((recentRun) => {
              const loading = loadingRunId === recentRun.run_id;
              return (
                <button
                  type="button"
                  key={recentRun.run_id}
                  onClick={() => loadRun(recentRun.run_id)}
                  disabled={loadingRunId !== null}
                  className={`rounded-md border px-2 py-1 font-mono transition hover:bg-white/10 disabled:cursor-wait ${result?.run_id === recentRun.run_id ? "border-cyan-400/40 bg-cyan-400/10 text-cyan-200" : "border-white/10 text-slate-500"}`}
                  title={`Load run ${recentRun.run_id}`}
                >
                  {loading ? "Loading…" : recentRun.run_id.slice(0, 8)}
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="p-5">
        {error && <div className="mb-4"><ErrorState message={error} onRetry={run} /></div>}

        {!result && !error && (
          <div className="rounded-xl border border-dashed border-white/10 bg-black/20 px-6 py-12 text-center">
            <div className="text-base font-medium text-slate-200">Ready to compare both policies</div>
            <p className="mx-auto mt-2 max-w-2xl text-base leading-relaxed text-slate-500">
              Run {count} matched scenarios to compare recovered revenue and customer friction. Every case is auditable in the downloadable CSV.
            </p>
          </div>
        )}

        {result && baseline && adaptive && (
          <div className="space-y-4">
            <section aria-live="polite" className={`relative overflow-hidden rounded-2xl border px-5 py-4 ${leadingPolicy === "Adaptive" ? "border-emerald-400/30 bg-emerald-400/[0.07]" : leadingPolicy === "Baseline" ? "border-violet-400/30 bg-violet-400/[0.07]" : "border-slate-500/30 bg-white/[0.04]"}`}>
              <div className="absolute inset-y-0 right-0 w-64 bg-gradient-to-l from-white/[0.04] to-transparent" aria-hidden />
              <div className="relative flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <div className={`text-xs font-semibold uppercase tracking-[0.2em] ${leadingPolicy === "Adaptive" ? "text-emerald-300" : leadingPolicy === "Baseline" ? "text-violet-300" : "text-slate-400"}`}>Simulation comparison</div>
                  <h3 className="mt-1 text-2xl font-semibold tracking-tight text-white">
                    {leadingPolicy ? `${leadingPolicy} produced higher simulated utility` : "The policies produced equal simulated utility"}
                  </h3>
                  <p className="mt-1.5 text-base text-slate-300">{buildComparisonStatement(result)}</p>
                </div>
                <div className="shrink-0 rounded-xl border border-white/10 bg-black/20 px-4 py-3 text-right">
                  <div className="text-xs uppercase tracking-wider text-slate-500">Adaptive recovered delta</div>
                  <div className={`mt-1 font-mono text-2xl font-semibold ${result.incremental_recovered >= 0 ? "text-emerald-300" : "text-rose-300"}`}>
                    {formatSignedRupees(result.incremental_recovered)}
                  </div>
                  <div className="mt-0.5 text-xs text-slate-500">Compared by {hasPolicyUtility ? "realized policy utility" : "realized net value"}</div>
                </div>
              </div>
            </section>

            <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1.05fr_.95fr]">
              <section className="overflow-hidden rounded-2xl border border-white/10 bg-black/20" aria-labelledby="primary-comparison-title">
                <div className="grid grid-cols-[1.2fr_1fr_1fr] gap-3 border-b border-white/10 bg-white/[0.03] px-4 py-3 text-xs font-semibold uppercase tracking-wider text-slate-500 sm:grid-cols-[minmax(120px,1.2fr)_minmax(100px,1fr)_minmax(100px,1fr)_minmax(90px,.8fr)]">
                  <h3 id="primary-comparison-title">Primary outcome</h3>
                  <div>Baseline</div>
                  <div className="text-cyan-300">Adaptive</div>
                  <div className="hidden text-right sm:block">Adaptive Δ</div>
                </div>
                <ComparisonRow
                  label="Revenue recovered"
                  baseline={formatRupees(baseline.amount_recovered)}
                  adaptive={formatRupees(adaptive.amount_recovered)}
                  delta={formatSignedRupees(result.incremental_recovered)}
                />
                <ComparisonRow
                  label="Recovery rate"
                  baseline={formatPercent(baseline.recovery_rate * 100)}
                  adaptive={formatPercent(adaptive.recovery_rate * 100)}
                  delta={formatDelta((adaptive.recovery_rate - baseline.recovery_rate) * 100, " pp")}
                />
                <ComparisonRow
                  label="Contact rate"
                  baseline={formatPercent(baseline.contact_rate)}
                  adaptive={formatPercent(adaptive.contact_rate)}
                  delta={contactDelta === undefined ? "—" : formatDelta(contactDelta, " pp")}
                  lowerIsBetter
                />
                <ComparisonRow
                  label="Customer friction"
                  baseline={baseline.friction_score?.toLocaleString("en-IN") ?? "—"}
                  adaptive={adaptive.friction_score?.toLocaleString("en-IN") ?? "—"}
                  delta={frictionDelta === undefined ? "—" : frictionDelta === 0 ? "No change" : `${frictionDelta > 0 ? "+" : "−"}${Math.abs(frictionDelta).toLocaleString("en-IN")}`}
                  lowerIsBetter
                />
              </section>

              <RevenueFrictionChart baseline={baseline} adaptive={adaptive} />
            </div>

            <details className="group rounded-xl border border-white/[0.08] bg-black/15">
              <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-sm font-medium text-slate-400 transition hover:text-slate-200">
                <span>Secondary metrics</span>
                <span className="text-lg font-light text-slate-600 transition group-open:rotate-45" aria-hidden>+</span>
              </summary>
              <div className="grid grid-cols-1 gap-3 border-t border-white/[0.07] p-4 md:grid-cols-2">
                <SecondaryArmMetrics label="Baseline · fixed policy" arm={baseline} />
                <SecondaryArmMetrics label="Adaptive · ML policy" arm={adaptive} adaptive />
              </div>
            </details>

            <details className="group rounded-xl border border-white/[0.08] bg-black/15">
              <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-3 text-sm font-medium text-slate-400 transition hover:text-slate-200">
                <span>Action distribution</span>
                <span className="text-lg font-light text-slate-600 transition group-open:rotate-45" aria-hidden>+</span>
              </summary>
              <div className="border-t border-white/[0.07] p-4">
                <ActionDistribution baseline={baseline} adaptive={adaptive} />
              </div>
            </details>

            <div className="flex flex-col gap-3 rounded-xl border border-white/[0.07] bg-white/[0.02] px-4 py-3 text-xs text-slate-500 md:flex-row md:items-center md:justify-between">
              <div className="leading-relaxed">
                <span className="text-slate-400">Run {result.run_id.slice(0, 8)}</span>
                {` · seed ${result.resolved_seed ?? "—"} · ${result.scenario_count ?? result.case_count / 2} scenarios`}
                {result.evaluation_friction_profile ? ` · ${result.evaluation_friction_profile} profile` : ""}
                {result.model_version ? ` · model ${result.model_version}` : ""}
                {result.model_fingerprint ? `/${String(result.model_fingerprint).slice(0, 8)}` : ""}
                {result.created_at ? ` · ${new Date(result.created_at).toLocaleString()}` : ""}
              </div>
              <button
                type="button"
                onClick={() => api.downloadExperimentCsv(result.run_id).catch((e) => setError(e instanceof Error ? e.message : "CSV download failed"))}
                className="shrink-0 rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-[13px] font-medium text-slate-300 transition hover:border-cyan-400/30 hover:bg-cyan-400/[0.06] hover:text-cyan-200"
              >
                Download audit CSV
              </button>
            </div>

            <p className="px-1 text-sm leading-relaxed text-slate-600">
              Policy utility prices recovery, action cost, and customer friction together. This benchmark validates simulator behavior and utility ordering; controlled production measurement is required to establish lift.
            </p>
          </div>
        )}
      </div>
    </Card>
  );
}
