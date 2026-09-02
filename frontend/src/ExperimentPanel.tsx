import { useState } from "react";
import { api } from "./api";
import type { ArmSummary, ExperimentSummary } from "./api";

function formatRupees(n: number): string {
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

function ArmCard({ label, arm, highlight }: { label: string; arm: ArmSummary; highlight?: boolean }) {
  return (
    <div className={`rounded-xl border p-4 ${highlight ? "border-emerald-700 bg-emerald-950/30" : "border-white/10 bg-white/5"}`}>
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
        <dt className="text-gray-500">Contacts</dt>
        <dd className="text-right">{arm.contacts}</dd>
        <dt className="text-gray-500">Escalations</dt>
        <dd className="text-right">{arm.escalations}</dd>
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

  const run = async () => {
    setRunning(true);
    setError(null);
    try {
      const seedNum = seed.trim() === "" ? undefined : Number(seed);
      const summary = await api.runExperiment(count, seedNum);
      setResult(summary);
    } catch (e) {
      setError(e instanceof Error ? e.message : "experiment failed");
    } finally {
      setRunning(false);
    }
  };

  return (
    <section className="mb-10 rounded-xl border border-white/10 bg-white/5">
      <div className="flex flex-col gap-3 border-b border-white/10 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="text-sm font-medium">Baseline vs. adaptive experiment</h2>
          <p className="mt-0.5 text-xs text-gray-500">
            Synthetic simulation benchmark — not production Razorpay lift. See ARCHITECTURE.md.
          </p>
        </div>
        <div className="flex items-center gap-2">
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
            className="rounded bg-emerald-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
          >
            {running ? "Running…" : "Run experiment"}
          </button>
        </div>
      </div>

      <div className="p-4">
        {error && <div className="mb-4 rounded bg-red-950/50 px-3 py-2 text-sm text-red-300">{error}</div>}

        {!result && !error && (
          <div className="py-8 text-center text-sm text-gray-500">
            Click "Run experiment" to send {count} matched synthetic scenarios through both the
            baseline and adaptive policies and compare what each recovers.
          </div>
        )}

        {result && (
          <div>
            <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm">
                Incremental recovered:{" "}
                <span className={result.incremental_recovered >= 0 ? "font-semibold text-emerald-400" : "font-semibold text-red-400"}>
                  {result.incremental_recovered >= 0 ? "+" : ""}
                  {formatRupees(result.incremental_recovered)}
                </span>
              </div>
              <a
                href={api.experimentCsvUrl(result.run_id)}
                className="rounded border border-white/10 px-3 py-1.5 text-xs text-gray-300 hover:bg-white/10"
                download
              >
                Download audit CSV
              </a>
            </div>

            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <ArmCard label="Baseline (fixed policy)" arm={result.arms.baseline} />
              <ArmCard label="Adaptive (ML policy)" arm={result.arms.adaptive} highlight={result.incremental_recovered >= 0} />
            </div>

            <div className="mt-3 text-right text-[10px] text-gray-600">run_id: {result.run_id}</div>
          </div>
        )}
      </div>
    </section>
  );
}
