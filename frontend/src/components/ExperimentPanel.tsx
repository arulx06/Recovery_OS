import { useState, useEffect, useCallback } from "react";
import { api } from "../api";
import type { ArmSummary, ExperimentSummary } from "../api";
import { Badge, Card, SectionTitle, ErrorState, formatRupees } from "./ui";
import { RevenueFrictionChart, ActionDistribution } from "./Charts";

function ArmCard({ label, arm, tone }: { label: string; arm: ArmSummary; tone: "neutral" | "adaptive" }) {
  const isAdaptive = tone === "adaptive";
  return (
    <div className={`rounded-xl border p-4 ${isAdaptive ? "border-sky-800/50 bg-sky-950/20" : "border-white/10 bg-white/[0.03]"}`}>
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium">{label}</h3>
        <span className="text-xs text-gray-500">{arm.cases} cases</span>
      </div>
      <dl className="grid grid-cols-2 gap-y-2 text-sm">
        <dt className="text-gray-500">At risk</dt>
        <dd className="text-right">{formatRupees(arm.amount_at_risk)}</dd>
        <dt className="text-gray-500">Recovered</dt>
        <dd className="text-right font-semibold">{formatRupees(arm.amount_recovered)}</dd>
        <dt className="text-gray-500">Recovery rate</dt>
        <dd className="text-right">{(arm.recovery_rate * 100).toFixed(1)}%</dd>
        <dt className="text-gray-500">Contact actions</dt>
        <dd className="text-right">{arm.contacts} ({(arm.contact_rate ?? 0).toFixed(1)}%)</dd>
        <dt className="text-gray-500">Friction score</dt>
        <dd className="text-right">{arm.friction_score ?? "—"}</dd>
        <dt className="text-gray-500">Escalations</dt>
        <dd className="text-right">{arm.escalations}</dd>
        <dt className="text-gray-500">Action cost proxy</dt>
        <dd className="text-right">{formatRupees(arm.action_cost_proxy)}</dd>
        <dt className="text-gray-500">Realized net value</dt>
        <dd className="text-right">{formatRupees(arm.realized_net_value)}</dd>
        {arm.realized_policy_utility !== undefined && (
          <>
            <dt className="text-gray-500">Policy utility</dt>
            <dd className="text-right font-mono text-xs">{formatRupees(arm.realized_policy_utility)}</dd>
          </>
        )}
        {arm.recovered_per_contact !== undefined && (
          <>
            <dt className="text-gray-500">Recovered / contact</dt>
            <dd className="text-right">{formatRupees(arm.recovered_per_contact ?? 0)}</dd>
          </>
        )}
      </dl>
      <div className="mt-3 border-t border-white/10 pt-3">
        <div className="mb-1 text-xs uppercase tracking-wide text-gray-500">Action distribution</div>
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(arm.action_distribution)
            .sort((a, b) => b[1] - a[1])
            .map(([action, count]) => (
              <span key={action} className="rounded bg-white/10 px-1.5 py-0.5 text-[10px] text-gray-300">
                {action} × {count}
              </span>
            ))}
        </div>
        {(arm.waits !== undefined || arm.payment_links !== undefined) && (
          <div className="mt-2 text-[11px] text-gray-500">
            waits {arm.waits ?? 0} · native {arm.native_retry_waits ?? 0} · links {arm.payment_links ?? 0} · ptp {arm.ptps ?? 0}
          </div>
        )}
      </div>
    </div>
  );
}

export default function ExperimentPanel() {
  const [count, setCount] = useState(500);
  const [seed, setSeed] = useState<string>("11");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ExperimentSummary | null>(null);
  const [recent, setRecent] = useState<Array<{ run_id: string; created_at: string | null }>>([]);

  const loadRecent = useCallback(async () => {
    try {
      const r = await api.experiments();
      setRecent(r.slice(0, 10));
    } catch {
      /* ignore */
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
    setError(null);
    try {
      const s = await api.experiment(runId);
      setResult(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load run");
    }
  };

  return (
    <Card>
      <div className="flex flex-col gap-3 border-b border-white/10 pb-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <SectionTitle subtitle="Synthetic simulation benchmark — not production Razorpay lift. Policy comparison with revenue vs friction tradeoff.">
            Baseline vs. adaptive experiment
          </SectionTitle>
          <div className="mt-1">
            <Badge tone="warning" size="sm">SYNTHETIC SIMULATION · NOT PRODUCTION LIFT</Badge>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-gray-500">
            cases
            <input
              type="number"
              min={1}
              max={5000}
              value={count}
              onChange={(e) => setCount(Number(e.target.value))}
              className="ml-1.5 w-20 rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-gray-200"
            />
          </label>
          <label className="text-xs text-gray-500">
            seed
            <input
              type="text"
              value={seed}
              onChange={(e) => setSeed(e.target.value)}
              placeholder="random"
              className="ml-1.5 w-20 rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-gray-200"
            />
          </label>
          <button
            onClick={run}
            disabled={running}
            className="rounded bg-sky-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-600 disabled:opacity-50"
          >
            {running ? "Running…" : "Run experiment"}
          </button>
        </div>
      </div>

      {recent.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[11px]">
          <span className="text-gray-500">Recent runs:</span>
          {recent.map((r) => (
            <button
              key={r.run_id}
              onClick={() => loadRun(r.run_id)}
              className={`rounded border px-1.5 py-0.5 font-mono text-[11px] hover:bg-white/10 ${result?.run_id === r.run_id ? "border-sky-700 bg-sky-900/30 text-sky-200" : "border-white/10 text-gray-400"}`}
              title={r.run_id}
            >
              {r.run_id.slice(0, 8)}
            </button>
          ))}
        </div>
      )}

      <div className="mt-4">
        {error && <ErrorState message={error} onRetry={run} />}

        {!result && !error && (
          <div className="py-8 text-center text-sm text-gray-500">
            Click &quot;Run experiment&quot; to send {count} matched synthetic scenarios through both the baseline and adaptive policies and compare what each recovers.
            <div className="mt-2 text-xs text-gray-600">Common-random numbers: same action → identical outcome. Download CSV for audit.</div>
          </div>
        )}

        {result && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-white/10 bg-black/20 px-3 py-2">
              <div className="text-sm">
                Incremental gross recovered (synthetic):{" "}
                <span className={result.incremental_recovered >= 0 ? "font-semibold text-emerald-400" : "font-semibold text-red-400"}>
                  {result.incremental_recovered >= 0 ? "+" : ""}
                  {formatRupees(result.incremental_recovered)}
                </span>
                <span className="ml-2 text-xs text-gray-500">
                  seed {result.resolved_seed ?? "—"} · {result.evaluation_friction_profile ?? "balanced"} × {result.evaluation_friction_weight ?? "—"} · {result.model_version ?? ""} {result.model_fingerprint ? `· ${String(result.model_fingerprint).slice(0, 8)}` : ""} · {result.scenario_count ?? result.case_count / 2} scenarios
                </span>
              </div>
              <button
                onClick={() => api.downloadExperimentCsv(result.run_id).catch((e) => setError(e instanceof Error ? e.message : "CSV download failed"))}
                className="rounded border border-white/10 px-3 py-1.5 text-xs text-gray-300 hover:bg-white/10"
              >
                Download audit CSV
              </button>
            </div>

            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <ArmCard label="Baseline (fixed policy)" arm={result.arms.baseline} tone="neutral" />
              <ArmCard label="Adaptive (ML policy)" arm={result.arms.adaptive} tone="adaptive" />
            </div>

            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <RevenueFrictionChart baseline={result.arms.baseline} adaptive={result.arms.adaptive} />
              <ActionDistribution baseline={result.arms.baseline} adaptive={result.arms.adaptive} />
            </div>

            <div className="flex items-center justify-between text-[11px] text-gray-600">
              <span>run_id: <span className="font-mono">{result.run_id}</span></span>
              <span>{result.created_at ? new Date(result.created_at).toLocaleString() : ""} · synthetic simulation</span>
            </div>

            <div className="rounded-lg border border-amber-900/30 bg-amber-950/20 p-3 text-xs leading-relaxed text-amber-200/80">
              RecoveryOS trades revenue vs customer friction explicitly. Adaptive&apos;s utility = <span className="font-mono">P×amount − cost − weight×friction</span>. Higher weight tolerates less contact. This evaluation is synthetic — not production Razorpay lift — but validates code and utility ordering.
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
