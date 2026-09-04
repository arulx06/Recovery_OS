import { useCallback, useEffect, useState } from "react";
import { api, getDemoTokenValue, setDemoToken } from "./api";
import type { CaseDetail, DashboardSummary, HealthResponse, RevenueCase } from "./api";
import { CaseDetailView } from "./components/CaseDetail";
import { CaseList } from "./components/CaseList";
import { DashboardOverview } from "./components/DashboardOverview";
import ExperimentPanel from "./components/ExperimentPanel";
import { HealthPanel } from "./components/HealthPanel";
import { Card, ErrorState, Loading, actionLabel, failureLabel, formatRupees, humanize, stateLabel } from "./components/ui";

type Tab = "overview" | "cases" | "experiments" | "system";

const DEMO_SCENARIOS = [
  { paymentId: "pay_demo_C_wait_001", label: "Silent recovery", detail: "WAIT" },
  { paymentId: "pay_demo_B_link_001", label: "Payment Link", detail: "Razorpay Test Mode" },
  { paymentId: "pay_demo_A_auth_001", label: "Promise to Pay", detail: "Validated customer intent" },
  { paymentId: "pay_demo_D_adaptive_001", label: "Friction-aware choice", detail: "Adaptive comparison" },
  { paymentId: "pay_demo_E_injection_001", label: "Prompt injection", detail: "Human review" },
];

function shortIdentifier(value: string | null | undefined): string {
  if (!value) return "Identifier unavailable";
  if (value.length <= 22) return value;
  return `${value.slice(0, 14)}…${value.slice(-5)}`;
}

function caseOptionLabel(item: Pick<RevenueCase, "id" | "amount" | "failure_category" | "chosen_action" | "state" | "razorpay_payment_id">): string {
  return `${formatRupees(item.amount)} · ${failureLabel(item.failure_category)} · ${actionLabel(item.chosen_action)} · ${stateLabel(item.state)} · ${shortIdentifier(item.razorpay_payment_id ?? item.id)}`;
}

function CaseNavigator({
  cases,
  selectedId,
  detail,
  onBack,
  onSelect,
}: {
  cases: RevenueCase[];
  selectedId: string;
  detail: CaseDetail | null;
  onBack: () => void;
  onSelect: (id: string) => void;
}) {
  const selectedCase = cases.find((item) => item.id === selectedId);
  const currentAction = detail?.latest_action?.action_type ?? detail?.decisions.at(-1)?.chosen_action ?? selectedCase?.chosen_action ?? null;
  const currentAmount = detail?.amount ?? selectedCase?.amount;
  const currentFailure = detail?.failure_category ?? selectedCase?.failure_category;
  const currentState = detail?.state ?? selectedCase?.state;
  const currentIdentifier = detail?.razorpay_payment_id ?? selectedCase?.razorpay_payment_id ?? selectedId;
  const currentLabel = detail
    ? caseOptionLabel({
        id: detail.id,
        amount: detail.amount,
        failure_category: detail.failure_category,
        chosen_action: currentAction,
        state: detail.state,
        razorpay_payment_id: detail.razorpay_payment_id,
      })
    : selectedCase
      ? caseOptionLabel(selectedCase)
      : `Case ${shortIdentifier(selectedId)}`;
  const selectedIsListed = cases.some((item) => item.id === selectedId);

  return (
    <nav className="flex flex-col gap-2 rounded-xl border border-[#242d3b] bg-[#0e131b] p-2 sm:flex-row sm:items-center" aria-label="Case navigation">
      <button
        onClick={onBack}
        className="inline-flex h-10 shrink-0 items-center gap-2 rounded-lg px-3 text-sm font-semibold text-slate-400 hover:bg-slate-800/70 hover:text-white"
      >
        <span aria-hidden>←</span>
        All cases
      </button>
      <div className="hidden h-7 w-px bg-[#2a3443] sm:block" aria-hidden />
      <label className="group relative min-w-0 flex-1 cursor-pointer rounded-lg border border-slate-700 bg-[#111720] px-3 py-1.5 transition hover:border-slate-500 hover:bg-[#151c27] focus-within:border-blue-400 focus-within:ring-2 focus-within:ring-blue-500/25">
        <span className="flex min-w-0 items-center gap-2 pr-8">
          <span className="shrink-0 text-sm font-semibold text-white">{formatRupees(currentAmount)}</span>
          <span className="truncate text-sm font-medium text-slate-300">{failureLabel(currentFailure)}</span>
        </span>
        <span className="mt-0.5 flex min-w-0 items-center gap-2 pr-8 text-2xs text-slate-500">
          <span className="truncate text-blue-200">{actionLabel(currentAction)}</span>
          <span aria-hidden>·</span>
          <span className="shrink-0">{stateLabel(currentState)}</span>
          <span className="hidden truncate font-mono lg:inline" title={currentIdentifier}>· {shortIdentifier(currentIdentifier)}</span>
        </span>
        <svg className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500 transition group-hover:text-slate-300" viewBox="0 0 20 20" fill="none" aria-hidden>
          <path d="m6 8 4 4 4-4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <select
          aria-label="Switch selected recovery case"
          value={selectedId}
          onChange={(event) => onSelect(event.target.value)}
          className="absolute inset-0 h-full w-full cursor-pointer opacity-0 focus:outline-none"
        >
          {!selectedIsListed && <option value={selectedId}>{currentLabel}</option>}
          {cases.map((item) => <option key={item.id} value={item.id}>{caseOptionLabel(item)}</option>)}
        </select>
      </label>
      <div className="flex shrink-0 items-center gap-2 px-2 text-2xs text-slate-500" aria-live="polite">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" aria-hidden />
        Detail refreshes every 10s
      </div>
    </nav>
  );
}

function useCaseDeepLink(): [string | null, (id: string | null) => void] {
  const read = () => new URLSearchParams(window.location.search).get("case");
  const [caseId, setCaseId] = useState<string | null>(read());
  useEffect(() => {
    const onPop = () => setCaseId(read());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const set = useCallback((id: string | null) => {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("case", id);
    else url.searchParams.delete("case");
    window.history.pushState({}, "", url.toString());
    setCaseId(id);
  }, []);
  return [caseId, set];
}

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState(false);
  const [dashboard, setDashboard] = useState<DashboardSummary | null>(null);
  const [dashboardError, setDashboardError] = useState<string | null>(null);
  const [overviewCases, setOverviewCases] = useState<RevenueCase[]>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [selectedCaseId, setSelectedCaseId] = useCaseDeepLink();
  const [detailResult, setDetailResult] = useState<{ caseId: string; detail: CaseDetail | null; error: string | null }>({ caseId: "", detail: null, error: null });
  const [demoTokenInput, setDemoTokenInput] = useState(() => getDemoTokenValue() ?? "");
  const [tokenSaved, setTokenSaved] = useState(false);

  useEffect(() => {
    const refresh = () => {
      api.health().then((next) => { setHealth(next); setHealthError(false); }).catch(() => setHealthError(true));
      api.dashboardSummary().then((next) => { setDashboard(next); setDashboardError(null); }).catch((error) => setDashboardError(error instanceof Error ? error.message : "Dashboard unavailable"));
      api.cases({ limit: 200, offset: 0 }).then(setOverviewCases).catch(() => {});
    };
    refresh();
    const interval = window.setInterval(refresh, 30000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    if (selectedCaseId) setTab("cases");
  }, [selectedCaseId]);

  useEffect(() => {
    if (!selectedCaseId) return;
    let cancelled = false;
    const fetchDetail = () => {
      api.caseDetail(selectedCaseId)
        .then((detail) => { if (!cancelled) setDetailResult({ caseId: selectedCaseId, detail, error: null }); })
        .catch((error) => { if (!cancelled) setDetailResult({ caseId: selectedCaseId, detail: null, error: error instanceof Error ? error.message : "Failed to load case" }); });
    };
    fetchDetail();
    const timer = window.setInterval(fetchDetail, 10000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [selectedCaseId]);

  const handleSelectCase = (id: string) => {
    setSelectedCaseId(id);
    setTab("cases");
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const openScenario = (paymentId: string) => {
    const match = overviewCases.find((item) => item.razorpay_payment_id === paymentId);
    if (match) handleSelectCase(match.id);
  };

  const backendUp = !healthError && health != null && ["ok", "degraded"].includes(health.status);
  const detail = detailResult.caseId === selectedCaseId ? detailResult.detail : null;
  const detailError = detailResult.caseId === selectedCaseId ? detailResult.error : null;
  const detailLoading = selectedCaseId !== null && detailResult.caseId !== selectedCaseId;
  const modeLabel = dashboard?.razorpay.is_test_mode ? "TEST MODE" : dashboard?.razorpay.simulated ? "SIMULATED" : "MODE UNKNOWN";
  const policyLabel = humanize(dashboard?.policy_mode ?? health?.adaptive_policy?.configured_mode ?? "baseline");

  return (
    <div className="min-h-screen">
      <header className="border-b border-[#202735] bg-[#090c11]/95 backdrop-blur">
        <div className="app-header-row mx-auto max-w-[1720px] gap-4 px-5 py-3 lg:px-8">
          <button onClick={() => setTab("overview")} className="flex min-w-56 items-center gap-3 text-left" aria-label="Open overview">
            <div className="brand-mark text-base font-bold shadow-[0_8px_24px_rgba(59,130,246,0.2)]">R</div>
            <div><div className="text-lg font-semibold tracking-tight text-white">RecoveryOS</div><div className="text-xs text-slate-500">Adaptive Revenue Recovery</div></div>
          </button>

          <nav className="app-primary-nav flex items-center gap-1 rounded-lg bg-[#11161f] p-1" aria-label="Primary">
            {(["overview", "cases", "experiments", "system"] as Tab[]).map((item) => (
              <button
                key={item}
                onClick={() => setTab(item)}
                aria-current={tab === item ? "page" : undefined}
                className={`flex-1 rounded-md px-4 py-2 text-sm font-semibold capitalize lg:flex-none ${tab === item ? "bg-slate-700/70 text-white" : "text-slate-400 hover:bg-slate-800/70 hover:text-slate-200"}`}
              >
                {item}
              </button>
            ))}
          </nav>

          <div className="app-header-actions flex items-center gap-2">
            <label className="hidden xl:block">
              <span className="sr-only">Demo scenarios</span>
              <select
                aria-label="Demo scenarios"
                value=""
                onChange={(event) => openScenario(event.target.value)}
                className="h-9 w-48 rounded-md border border-slate-700 bg-[#11161f] px-3 text-sm font-medium text-slate-300"
              >
                <option value="">Demo scenarios</option>
                {DEMO_SCENARIOS.map((scenario) => (
                  <option key={scenario.paymentId} value={scenario.paymentId} disabled={!overviewCases.some((item) => item.razorpay_payment_id === scenario.paymentId)}>{scenario.label}</option>
                ))}
              </select>
            </label>
            <div className={`inline-flex h-9 items-center gap-2 rounded-md border px-3 text-sm font-semibold ${backendUp ? "border-emerald-500/20 bg-emerald-500/[0.08] text-emerald-300" : "border-rose-500/20 bg-rose-500/[0.08] text-rose-300"}`}>
              <span className={`h-2 w-2 rounded-full ${backendUp ? "bg-emerald-400" : "bg-rose-400"}`} />
              {backendUp ? "Healthy" : health ? "Degraded" : "Checking"}
            </div>
            <div className="hidden h-9 items-center rounded-md border border-slate-700 bg-[#11161f] px-3 text-xs font-semibold text-slate-300 sm:flex">
              <span className="text-blue-300">{modeLabel}</span><span className="mx-2 text-slate-700">|</span>{policyLabel}
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1720px] px-5 py-7 lg:px-8 lg:py-8">
        {tab === "overview" && (
          <div className="space-y-5">
            {dashboardError && <ErrorState message={dashboardError} />}
            {!dashboard && !dashboardError && <Loading label="Loading recovery overview…" />}
            {dashboard && <DashboardOverview data={dashboard} cases={overviewCases} />}
          </div>
        )}

        {tab === "cases" && (
          <>
            <div className={selectedCaseId ? "hidden" : undefined}>
              <CaseList onSelect={handleSelectCase} selectedId={selectedCaseId} />
            </div>
            {selectedCaseId && (
              <div className="space-y-3">
                <CaseNavigator
                  cases={overviewCases}
                  selectedId={selectedCaseId}
                  detail={detail}
                  onBack={() => setSelectedCaseId(null)}
                  onSelect={handleSelectCase}
                />
                {detailLoading ? <Card><Loading label="Loading decision evidence…" /></Card> : detailError ? <Card><ErrorState message={detailError} /></Card> : <CaseDetailView detail={detail} environmentMode={dashboard?.razorpay.mode_label} />}
              </div>
            )}
          </>
        )}

        {tab === "experiments" && <ExperimentPanel />}

        {tab === "system" && (
          <div className="space-y-5">
            <HealthPanel health={health} dashboard={dashboard} healthError={healthError} />
            <div className="grid gap-5 lg:grid-cols-2">
              <details className="rounded-xl border border-[#242d3b] bg-[#11161f]">
                <summary className="cursor-pointer px-5 py-4 text-sm font-semibold text-slate-300">Operator access</summary>
                <div className="border-t border-[#242d3b] p-5">
                  <p className="text-base leading-relaxed text-slate-400">If demo-token protection is enabled, store the operator token transiently in this browser session.</p>
                  <div className="mt-4 flex gap-2"><input value={demoTokenInput} onChange={(event) => setDemoTokenInput(event.target.value)} placeholder="Demo admin token" type="password" className="h-10 flex-1 rounded-md border border-slate-700 bg-[#090d13] px-3 text-sm text-slate-200" /><button onClick={() => { setDemoToken(demoTokenInput || null); setTokenSaved(true); window.setTimeout(() => setTokenSaved(false), 2000); }} className="rounded-md bg-slate-100 px-4 text-sm font-semibold text-slate-900">Save</button><button onClick={() => { setDemoToken(null); setDemoTokenInput(""); }} className="rounded-md border border-slate-700 px-4 text-sm font-semibold text-slate-300">Clear</button></div>
                  {tokenSaved && <div className="mt-2 text-xs text-emerald-300">Stored in sessionStorage.</div>}
                </div>
              </details>
              <details className="rounded-xl border border-[#242d3b] bg-[#11161f]">
                <summary className="cursor-pointer px-5 py-4 text-sm font-semibold text-slate-300">Runtime configuration</summary>
                <div className="grid gap-4 border-t border-[#242d3b] p-5 text-sm text-slate-400 sm:grid-cols-2">
                  <div><div className="text-xs uppercase tracking-wide text-slate-500">Storage authority</div><div className="mt-1 text-slate-200">PostgreSQL authoritative</div><div className="mt-1 text-xs">Redis/RQ reconstructable</div></div>
                  <div><div className="text-xs uppercase tracking-wide text-slate-500">Decision profile</div><div className="mt-1 text-slate-200">{dashboard?.friction.profile ?? "—"} · weight {dashboard?.friction.weight ?? "—"}</div><div className="mt-1 text-xs">Model {dashboard?.model.fingerprint_short ?? "not loaded"}</div></div>
                </div>
              </details>
            </div>
          </div>
        )}
      </main>

      <footer className="mx-auto mt-4 max-w-[1720px] border-t border-[#202735] px-5 py-5 text-xs text-slate-600 lg:px-8">
        <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between"><span>RecoveryOS · Provider truth from Razorpay · Application truth in PostgreSQL</span><span>Test Mode · Synthetic evaluation · Messages not sent</span></div>
      </footer>
    </div>
  );
}
