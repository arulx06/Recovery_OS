import { useEffect, useState, useCallback } from "react";
import { api } from "../api";
import type { RevenueCase, CaseFilters } from "../api";
import { Badge, Card, SectionTitle, EmptyState, Loading, ErrorState, formatRupees, formatDate } from "./ui";

const STATE_OPTIONS = ["", "WAITING", "ACTION_SCHEDULED", "AWAITING_OUTCOME", "HUMAN_REVIEW", "RECOVERED", "STOPPED", "DISPUTED"];
const CATEGORY_OPTIONS = ["", "TRANSIENT_INFRASTRUCTURE", "SUBSCRIPTION_PENDING_NATIVE_RETRY", "INSUFFICIENT_BALANCE", "CUSTOMER_AUTHENTICATION", "INVALID_INSTRUMENT", "MANDATE_ISSUE", "PERMANENT_HARD_FAILURE", "UNKNOWN"];
const ACTION_OPTIONS = ["", "WAIT", "WAIT_FOR_NATIVE_RETRY", "CREATE_PAYMENT_LINK", "CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY", "ESCALATE", "STOP", "FOLLOW_UP_PTP"];
const POLICY_OPTIONS = ["", "baseline", "shadow", "adaptive", "adaptive_fallback"];

function stateTone(state: string) {
  if (state === "RECOVERED") return "success" as const;
  if (state === "HUMAN_REVIEW") return "warning" as const;
  if (state === "DISPUTED" || state === "STOPPED") return "danger" as const;
  if (state === "WAITING") return "info" as const;
  if (state === "AWAITING_OUTCOME" || state === "ACTION_SCHEDULED") return "info" as const;
  return "neutral" as const;
}

export function CaseList({
  onSelect,
  selectedId,
}: {
  onSelect: (id: string) => void;
  selectedId: string | null;
}) {
  const [cases, setCases] = useState<RevenueCase[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState<CaseFilters>({ limit: 50, offset: 0 });
  const [searchInput, setSearchInput] = useState("");

  const fetchCases = useCallback(async (f: CaseFilters) => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.cases(f);
      setCases(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load cases");
      setCases([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchCases(filters);
  }, [fetchCases, filters]);

  const applySearch = () => {
    setFilters((p) => ({ ...p, search: searchInput.trim() || undefined, offset: 0 }));
  };

  const updateFilter = (key: keyof CaseFilters, value: string) => {
    setFilters((p) => ({ ...p, [key]: value || undefined, offset: 0 }));
  };

  const clearFilters = () => {
    setSearchInput("");
    setFilters({ limit: 50, offset: 0 });
  };

  return (
    <Card>
      <div className="flex flex-col gap-3 border-b border-white/10 pb-3 md:flex-row md:items-center md:justify-between">
        <SectionTitle subtitle="Operational recovery cases — PostgreSQL is source of truth">Recovery cases</SectionTitle>
        <div className="flex items-center gap-2 text-xs">
          <span className="text-gray-500">{cases ? `${cases.length} shown` : "—"}</span>
          <button onClick={() => fetchCases(filters)} className="rounded border border-white/10 px-2 py-1 text-gray-300 hover:bg-white/10">Refresh</button>
        </div>
      </div>

      {/* Filters */}
      <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-6">
        <label className="text-[11px] text-gray-500">
          state
          <select value={filters.state ?? ""} onChange={(e) => updateFilter("state", e.target.value)} className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-gray-200">
            {STATE_OPTIONS.map((o) => (
              <option key={o} value={o}>{o || "— all —"}</option>
            ))}
          </select>
        </label>
        <label className="text-[11px] text-gray-500">
          failure category
          <select value={filters.failure_category ?? ""} onChange={(e) => updateFilter("failure_category", e.target.value)} className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-gray-200">
            {CATEGORY_OPTIONS.map((o) => (
              <option key={o} value={o}>{o || "— all —"}</option>
            ))}
          </select>
        </label>
        <label className="text-[11px] text-gray-500">
          chosen action
          <select value={filters.chosen_action ?? ""} onChange={(e) => updateFilter("chosen_action", e.target.value)} className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-gray-200">
            {ACTION_OPTIONS.map((o) => (
              <option key={o} value={o}>{o || "— all —"}</option>
            ))}
          </select>
        </label>
        <label className="text-[11px] text-gray-500">
          policy mode
          <select value={filters.policy_mode ?? ""} onChange={(e) => updateFilter("policy_mode", e.target.value)} className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-gray-200">
            {POLICY_OPTIONS.map((o) => (
              <option key={o} value={o}>{o || "— all —"}</option>
            ))}
          </select>
        </label>
        <label className="col-span-2 text-[11px] text-gray-500">
          search (payment / case / link)
          <div className="mt-1 flex gap-1">
            <input
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && applySearch()}
              placeholder="pay_demo_…, plink_…, or case id"
              className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-gray-200 placeholder:text-gray-600"
            />
            <button onClick={applySearch} className="rounded bg-white/10 px-2 py-1 text-xs text-gray-200 hover:bg-white/15">Search</button>
          </div>
        </label>
      </div>
      <div className="mt-2 flex items-center gap-2">
        <button onClick={clearFilters} className="text-xs text-gray-500 hover:text-gray-300 underline">Clear filters</button>
        <span className="text-[11px] text-gray-600">Server-side filtering — not browser-only</span>
      </div>

      {/* List */}
      <div className="mt-4">
        {loading && <Loading label="Loading cases…" />}
        {error && <ErrorState message={error} onRetry={() => fetchCases(filters)} />}
        {!loading && !error && cases && cases.length === 0 && (
          <EmptyState title="No cases match filters" description="Try clearing filters or seed demo cases via backend/scripts/seed_demo.py" />
        )}
        {!loading && !error && cases && cases.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-gray-500">
                <tr>
                  <th className="px-2 py-2">Case</th>
                  <th className="px-2 py-2">Amount</th>
                  <th className="px-2 py-2">State</th>
                  <th className="px-2 py-2">Failure</th>
                  <th className="px-2 py-2">Action</th>
                  <th className="px-2 py-2">Policy</th>
                  <th className="px-2 py-2">Friction</th>
                  <th className="px-2 py-2">Link / PTP</th>
                  <th className="px-2 py-2">Updated</th>
                </tr>
              </thead>
              <tbody>
                {cases.map((c) => (
                  <tr
                    key={c.id}
                    onClick={() => onSelect(c.id)}
                    className={`cursor-pointer border-t border-white/5 hover:bg-white/[0.04] ${selectedId === c.id ? "bg-sky-950/30" : ""}`}
                    tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && onSelect(c.id)}
                    role="button"
                    aria-label={`Open case ${c.id.slice(0, 8)}`}
                  >
                    <td className="px-2 py-2 font-mono text-xs text-gray-400">
                      {c.id.slice(0, 8)}
                      <div className="text-[10px] text-gray-600 truncate max-w-[90px]">{c.razorpay_payment_id ?? ""}</div>
                    </td>
                    <td className="px-2 py-2 font-mono text-xs">{c.amount != null ? formatRupees(c.amount) : "—"}</td>
                    <td className="px-2 py-2">
                      <Badge tone={stateTone(c.state)} size="sm">{c.state}</Badge>
                    </td>
                    <td className="px-2 py-2">
                      <span className="text-xs text-gray-300">{c.failure_category ?? "—"}</span>
                      {c.error_reason && <div className="text-[11px] text-gray-500 truncate max-w-[140px]">{c.error_reason}</div>}
                    </td>
                    <td className="px-2 py-2">
                      {c.chosen_action ? <Badge tone="info" size="sm">{c.chosen_action}</Badge> : <span className="text-gray-600 text-xs">—</span>}
                      {c.latest_action_status && <div className="text-[11px] text-gray-500">{c.latest_action_status}</div>}
                    </td>
                    <td className="px-2 py-2 text-xs text-gray-400">{c.policy_mode ?? "baseline"}</td>
                    <td className="px-2 py-2 text-xs">
                      {c.friction_score != null ? (
                        <span className="font-mono text-amber-300">{c.friction_score.toFixed(1)}</span>
                      ) : (
                        <span className="text-gray-600">—</span>
                      )}
                      {c.contact_count != null && <div className="text-[11px] text-gray-500">{c.contact_count} contacts</div>}
                    </td>
                    <td className="px-2 py-2 text-xs">
                      {c.razorpay_payment_link_id ? (
                        <span className="font-mono text-[11px] text-sky-300 truncate max-w-[100px] inline-block" title={c.razorpay_payment_link_id}>{c.razorpay_payment_link_id.slice(0, 14)}…</span>
                      ) : (
                        <span className="text-gray-600">—</span>
                      )}
                      {c.ptp_status && <div className="text-[11px]"><Badge tone="info" size="sm">{c.ptp_status}</Badge></div>}
                    </td>
                    <td className="px-2 py-2 text-[11px] text-gray-500 whitespace-nowrap">{formatDate(c.updated_at ?? c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {cases && cases.length >= (filters.limit ?? 50) && (
        <div className="mt-3 flex items-center justify-between text-xs text-gray-500">
          <span>Showing first {filters.limit} — use search/filters or increase limit</span>
          <div className="flex gap-1">
            <button onClick={() => setFilters((p) => ({ ...p, limit: Math.min(200, (p.limit ?? 50) + 50) }))} className="rounded border border-white/10 px-2 py-1 hover:bg-white/10">Load more</button>
          </div>
        </div>
      )}
    </Card>
  );
}
