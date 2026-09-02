import { useEffect, useState } from "react";
import { api } from "./api";
import type { HealthResponse, RevenueCase } from "./api";
import ExperimentPanel from "./ExperimentPanel";

function StatusPill({ ok, label }: { ok: boolean | null; label: string }) {
  const color =
    ok === null ? "bg-gray-600" : ok ? "bg-emerald-600" : "bg-red-600";
  return (
    <span className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-medium ${color}`}>
      <span className="h-2 w-2 rounded-full bg-white/80" />
      {label}
    </span>
  );
}

const ACTION_COLORS: Record<string, string> = {
  WAIT: "bg-gray-700 text-gray-200",
  WAIT_FOR_NATIVE_RETRY: "bg-gray-700 text-gray-200",
  CONTACT_CUSTOMER: "bg-sky-800 text-sky-100",
  CREATE_PAYMENT_LINK: "bg-sky-800 text-sky-100",
  ESCALATE: "bg-amber-800 text-amber-100",
  STOP: "bg-red-900 text-red-100",
};

function ActionBadge({ action }: { action: string | null }) {
  if (!action) return <span className="text-gray-600">—</span>;
  const classes = ACTION_COLORS[action] ?? "bg-gray-700 text-gray-200";
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${classes}`}>
      {action}
    </span>
  );
}

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [cases, setCases] = useState<RevenueCase[] | null>(null);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealthError(true));
    api.cases().then(setCases).catch(() => setCases([]));
  }, []);

  const backendUp = healthError ? false : health ? health.status === "ok" : null;

  return (
    <div className="min-h-screen px-6 py-8 md:px-12 lg:px-24">
      <header className="mb-10 flex flex-col gap-3 border-b border-white/10 pb-6 md:flex-row md:items-center md:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">RecoveryOS</h1>
          <p className="mt-1 max-w-2xl text-sm text-gray-400">
            Adaptive revenue recovery controller for Razorpay. When a payment fails,
            RecoveryOS decides whether to wait, retry, contact the customer, send a
            payment link, collect a promise-to-pay, escalate — or deliberately do
            nothing — and measures which decisions actually recover the most money
            with the least customer friction.
          </p>
        </div>
        <div className="flex flex-col items-start gap-2 md:items-end">
          <StatusPill ok={backendUp} label={backendUp === null ? "checking backend…" : backendUp ? "backend healthy" : "backend unreachable"} />
          {health && (
            <span className="text-xs text-gray-500">db: {health.database}</span>
          )}
        </div>
      </header>

      <section className="mb-10 grid grid-cols-1 gap-4 md:grid-cols-4">
        {[
          { label: "Revenue at risk", value: "—" },
          { label: "Recovered (simulated)", value: "—" },
          { label: "Active cases", value: cases ? cases.length : "—" },
          { label: "Phase", value: "7 · Measurement dashboard" },
        ].map((stat) => (
          <div key={stat.label} className="rounded-xl border border-white/10 bg-white/5 p-4">
            <div className="text-xs uppercase tracking-wide text-gray-500">{stat.label}</div>
            <div className="mt-2 text-xl font-semibold">{stat.value}</div>
          </div>
        ))}
      </section>

      <ExperimentPanel />

      <section className="rounded-xl border border-white/10 bg-white/5">
        <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
          <h2 className="text-sm font-medium">Recovery cases</h2>
          <span className="text-xs text-gray-500">
            {cases === null ? "loading…" : `${cases.length} case(s)`}
          </span>
        </div>

        {cases !== null && cases.length === 0 && (
          <div className="px-4 py-10 text-center text-sm text-gray-500">
            No cases yet — send a test webhook (see README) to see one land
            here with a diagnosis and a chosen recovery action.
          </div>
        )}

        {cases !== null && cases.length > 0 && (
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-gray-500">
              <tr>
                <th className="px-4 py-2">Case</th>
                <th className="px-4 py-2">Amount</th>
                <th className="px-4 py-2">State</th>
                <th className="px-4 py-2">Failure category</th>
                <th className="px-4 py-2">Action</th>
                <th className="px-4 py-2">Created</th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c) => (
                <tr key={c.id} className="border-t border-white/5">
                  <td className="px-4 py-2 font-mono text-xs text-gray-400">{c.id.slice(0, 8)}</td>
                  <td className="px-4 py-2">{c.amount ?? "—"}</td>
                  <td className="px-4 py-2">{c.state}</td>
                  <td className="px-4 py-2">
                    {c.failure_category ?? "—"}
                    {c.error_reason && (
                      <div className="text-xs text-gray-500">{c.error_reason}</div>
                    )}
                  </td>
                  <td className="px-4 py-2">
                    <ActionBadge action={c.chosen_action} />
                    {c.razorpay_payment_link_id && (
                      <div className="mt-1 font-mono text-[10px] text-gray-500">
                        {c.razorpay_payment_link_id}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-2 text-gray-400">{c.created_at ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <footer className="mt-10 text-xs text-gray-600">
        Phase 7 — one click runs a matched baseline-vs-adaptive experiment
        and produces a downloadable, case-level audit CSV. See
        ARCHITECTURE.md for the full pipeline.
      </footer>
    </div>
  );
}
