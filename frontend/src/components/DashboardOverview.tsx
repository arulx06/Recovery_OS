import type { DashboardSummary } from "../api";
import { Badge, Card, SectionTitle, formatRupees } from "./ui";

export function DashboardOverview({ data }: { data: DashboardSummary | null }) {
  if (!data) {
    return (
      <Card>
        <div className="text-sm text-gray-500">Loading dashboard summary…</div>
      </Card>
    );
  }
  const stats = [
    { label: "Revenue at risk", value: formatRupees(data.revenue_at_risk), sub: `${data.open_cases} open cases` },
    { label: "Revenue recovered", value: formatRupees(data.revenue_recovered), sub: `${data.recovered_cases} recovered` },
    { label: "Total cases", value: String(data.total_cases), sub: `waiting ${data.waiting_cases} · human review ${data.human_review_cases}` },
    { label: "Active promises", value: String(data.active_ptps), sub: `${data.disputed_cases} disputed · ${data.stopped_cases} stopped` },
  ];
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        {stats.map((s) => (
          <Card key={s.label} className="bg-white/[0.06]">
            <div className="text-[11px] uppercase tracking-wide text-gray-500">{s.label}</div>
            <div className="mt-1 text-xl font-semibold tracking-tight">{s.value}</div>
            <div className="mt-1 text-xs text-gray-500">{s.sub}</div>
          </Card>
        ))}
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <Card>
          <SectionTitle subtitle="Open vs recovered">By state</SectionTitle>
          <div className="space-y-1.5">
            {Object.entries(data.by_state)
              .sort((a, b) => b[1] - a[1])
              .map(([state, count]) => (
                <div key={state} className="flex items-center justify-between text-xs">
                  <span className="flex items-center gap-2">
                    <Badge tone={state === "RECOVERED" ? "success" : state === "HUMAN_REVIEW" ? "warning" : state === "DISPUTED" ? "danger" : state === "WAITING" ? "info" : "neutral"} size="sm">{state}</Badge>
                  </span>
                  <span className="font-mono text-gray-300">{count}</span>
                </div>
              ))}
            {Object.keys(data.by_state).length === 0 && <div className="text-xs text-gray-500">No cases yet</div>}
          </div>
        </Card>

        <Card>
          <SectionTitle subtitle="Failure taxonomy distribution">By category</SectionTitle>
          <div className="space-y-1.5">
            {Object.entries(data.by_category)
              .sort((a, b) => b[1] - a[1])
              .slice(0, 8)
              .map(([cat, count]) => (
                <div key={cat} className="flex items-center justify-between text-xs">
                  <span className="text-gray-300 truncate pr-2">{cat}</span>
                  <span className="font-mono text-gray-400">{count}</span>
                </div>
              ))}
            {Object.keys(data.by_category).length === 0 && <div className="text-xs text-gray-500">No categories yet</div>}
          </div>
        </Card>

        <Card>
          <SectionTitle subtitle="Policy & friction">Control plane</SectionTitle>
          <div className="space-y-2 text-xs">
            <div className="flex justify-between">
              <span className="text-gray-500">Policy mode</span>
              <Badge tone={data.policy_mode === "adaptive" ? "success" : data.policy_mode === "shadow" ? "warning" : "neutral"}>{data.policy_mode}</Badge>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">Model</span>
              <span className="font-mono text-gray-300">{data.model.version ?? "none"} {data.model.fingerprint_short ? `· ${data.model.fingerprint_short}` : ""}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">Friction profile</span>
              <span className="text-gray-300">{data.friction.profile} × {data.friction.weight}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-gray-500">Queue</span>
              <Badge tone={data.queue.status === "connected" ? "success" : data.queue.status === "disabled" ? "muted" : "warning"} size="sm">{data.queue.status}</Badge>
            </div>
            <div className="pt-2 text-[11px] text-gray-600 leading-relaxed">
              PostgreSQL is authoritative. Redis/RQ is transport. RAZORPAY TEST MODE only · SYNTHETIC evaluation · drafts DRAFT / NOT SENT.
            </div>
          </div>
        </Card>
      </div>
    </div>
  );
}
