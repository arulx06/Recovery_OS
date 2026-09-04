import type { DashboardSummary, RevenueCase } from "../api";
import { Card, actionLabel, formatRupees } from "./ui";

export function DashboardOverview({ data, cases }: { data: DashboardSummary | null; cases: RevenueCase[] }) {
  if (!data) {
    return (
      <Card>
        <div className="text-sm text-gray-500">Loading dashboard summary…</div>
      </Card>
    );
  }
  const recoveryRate = data.revenue_total > 0 ? (data.revenue_recovered / data.revenue_total) * 100 : 0;
  const activeCases = cases.filter((item) => !["RECOVERED", "STOPPED", "DISPUTED"].includes(item.state));
  const activeWithAction = activeCases.filter((item) => item.chosen_action);
  const immediateContact = new Set(["CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY", "FOLLOW_UP_PTP"]);
  const noImmediateContact = activeWithAction.filter((item) => !immediateContact.has(item.chosen_action ?? ""));
  const protectedRevenue = noImmediateContact.reduce((sum, item) => sum + (item.amount ?? 0), 0);
  const diagnosed = cases.filter((item) => item.failure_category).length;
  const actionSelected = cases.filter((item) => item.chosen_action).length;
  const interventionGroups = [
    { key: "WAIT", label: "Wait", actions: ["WAIT"] },
    { key: "WAIT_FOR_NATIVE_RETRY", label: "Razorpay retry", actions: ["WAIT_FOR_NATIVE_RETRY"] },
    { key: "CREATE_PAYMENT_LINK", label: "Payment link", actions: ["CREATE_PAYMENT_LINK"] },
    { key: "CONTACT_CUSTOMER", label: "Customer contact", actions: ["CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY", "FOLLOW_UP_PTP"] },
    { key: "ESCALATE", label: "Escalate", actions: ["ESCALATE"] },
  ].map((group) => ({
    ...group,
    count: cases.filter((item) => group.actions.includes(item.chosen_action ?? "")).length,
  }));
  const maxMix = Math.max(1, ...interventionGroups.map((item) => item.count));
  const stats = [
    { label: "Revenue at risk", value: formatRupees(data.revenue_at_risk), sub: "Across active recovery cases" },
    { label: "Recovered", value: formatRupees(data.revenue_recovered), sub: `${data.recovered_cases} provider-confirmed cases` },
    { label: "Recovery rate", value: `${recoveryRate.toFixed(1)}%`, sub: "Share of tracked revenue" },
    { label: "Active recoveries", value: String(data.open_cases), sub: `${data.waiting_cases} waiting · ${data.human_review_cases} in review` },
  ];
  const pipeline = [
    { label: "Cases", value: data.total_cases },
    { label: "Diagnosed", value: diagnosed },
    { label: "Action selected", value: actionSelected },
    { label: "Active", value: data.open_cases },
    { label: "Recovered", value: data.recovered_cases },
  ];
  return (
    <div className="space-y-5" data-testid="overview-page">
      <div>
        <p className="text-sm font-semibold uppercase tracking-[0.16em] text-blue-300">Recovery command center</p>
        <div className="mt-2 flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
          <h1 className="text-3xl font-semibold tracking-tight text-white">Recover revenue without contacting everyone.</h1>
          <p className="max-w-xl text-sm leading-relaxed text-slate-400">Every failed payment gets a reason, a safety check, and the lowest-friction action that still protects recovery value.</p>
        </div>
      </div>

      <Card padding="p-0" className="overflow-hidden">
        <div className="grid grid-cols-2 lg:grid-cols-4">
          {stats.map((stat, index) => (
            <div key={stat.label} className={`px-6 py-5 ${index > 0 ? "border-l border-[#242d3b]" : ""} ${index > 1 ? "border-t border-[#242d3b] lg:border-t-0" : ""}`}>
              <div className="text-sm text-slate-400">{stat.label}</div>
              <div className="mt-1 text-3xl font-semibold tracking-tight text-white">{stat.value}</div>
              <div className="mt-1 text-xs text-slate-500">{stat.sub}</div>
            </div>
          ))}
        </div>
      </Card>

      {activeWithAction.length > 0 && (
        <div className="flex items-center gap-4 rounded-lg border border-blue-500/20 bg-blue-500/[0.08] px-5 py-3" data-testid="hero-insight">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-blue-400/15 text-lg text-blue-200">↓</div>
          <div className="text-sm text-slate-300">
            <span className="font-semibold text-white">{noImmediateContact.length} of {activeWithAction.length} active cases</span> with a selected action avoid immediate customer contact, protecting <span className="font-semibold text-blue-200">{formatRupees(protectedRevenue)}</span> with quieter interventions.
          </div>
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-[1.35fr_1fr]">
        <Card>
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-lg font-semibold">Recovery pipeline</h2>
              <p className="mt-1 text-sm text-slate-400">From failure signal to provider-confirmed outcome.</p>
            </div>
            <span className="text-xs font-medium text-slate-500">Live case state</span>
          </div>
          <div className="mt-7 flex items-start">
            {pipeline.map((item, index) => (
              <div key={item.label} className="flex min-w-0 flex-1 items-start">
                <div className="min-w-0 flex-1 text-center">
                  <div className={`mx-auto flex h-12 w-12 items-center justify-center rounded-full border text-lg font-semibold ${index === pipeline.length - 1 ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-300" : "border-blue-400/25 bg-blue-400/[0.08] text-blue-100"}`}>{item.value}</div>
                  <div className="mt-2 text-xs font-medium text-slate-300">{item.label}</div>
                </div>
                {index < pipeline.length - 1 && <div className="mt-6 h-px w-5 shrink-0 bg-slate-700 lg:w-9" />}
              </div>
            ))}
          </div>
        </Card>

        <Card>
          <h2 className="text-lg font-semibold">Intervention mix</h2>
          <p className="mt-1 text-sm text-slate-400">What RecoveryOS chose across current cases.</p>
          <div className="mt-5 space-y-3">
            {interventionGroups.map((item) => (
              <div key={item.key} className="grid grid-cols-[130px_1fr_24px] items-center gap-3">
                <span className="truncate text-sm text-slate-300" title={actionLabel(item.key)}>{item.label}</span>
                <div className="h-2 rounded-full bg-slate-800">
                  <div className="h-2 rounded-full bg-blue-400" style={{ width: `${(item.count / maxMix) * 100}%` }} />
                </div>
                <span className="text-right text-sm font-semibold text-slate-200">{item.count}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>

      <Card padding="px-5 py-4" className="bg-[#0e131b]">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center">
          <div className="min-w-[190px]">
            <div className="text-sm font-semibold text-white">How RecoveryOS works</div>
            <div className="mt-0.5 text-xs text-slate-500">One auditable decision path</div>
          </div>
          <div className="flex flex-1 items-center overflow-x-auto">
            {["Diagnose", "Apply guardrails", "Compare interventions", "Execute", "Reconcile"].map((step, index, all) => (
              <div key={step} className="flex flex-1 items-center whitespace-nowrap">
                <div className="rounded-md bg-slate-800/70 px-3 py-2 text-sm font-medium text-slate-200">{step}</div>
                {index < all.length - 1 && (
                  <span className="flex flex-1 items-center pl-3 pr-1 text-slate-400" aria-hidden="true">
                    <span className="h-px flex-1 bg-current opacity-70" />
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" className="shrink-0 -ml-px block">
                      <path d="M6 3L11 8L6 13" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      </Card>
    </div>
  );
}
