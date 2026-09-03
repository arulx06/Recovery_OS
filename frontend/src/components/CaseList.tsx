import { useCallback, useEffect, useDeferredValue, useState } from "react";
import { api } from "../api";
import type { RevenueCase } from "../api";
import {
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Loading,
  actionLabel,
  failureLabel,
  formatDate,
  formatRupees,
  stateLabel,
  stateTone,
} from "./ui";

const STATE_OPTIONS = ["", "WAITING", "ACTION_SCHEDULED", "AWAITING_OUTCOME", "HUMAN_REVIEW", "RECOVERED", "STOPPED", "DISPUTED"];
const CATEGORY_OPTIONS = ["", "TRANSIENT_INFRASTRUCTURE", "SUBSCRIPTION_PENDING_NATIVE_RETRY", "INSUFFICIENT_BALANCE", "CUSTOMER_AUTHENTICATION", "INVALID_INSTRUMENT", "MANDATE_ISSUE", "PERMANENT_HARD_FAILURE", "UNCLASSIFIED"];
const ACTION_OPTIONS = ["", "WAIT", "WAIT_FOR_NATIVE_RETRY", "CREATE_PAYMENT_LINK", "CONTACT_CUSTOMER", "COLLECT_PROMISE_TO_PAY", "ESCALATE", "STOP", "FOLLOW_UP_PTP"];

function shortCaseName(item: RevenueCase): string {
  const payment = item.razorpay_payment_id ?? "";
  const match = payment.match(/pay_demo_([A-Z](?:_NATIVE)?)_/i);
  if (match) return `Demo ${match[1].replace("_", " ")}`;
  return `Case ${item.id.slice(0, 8)}`;
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
  const [state, setState] = useState("");
  const [category, setCategory] = useState("");
  const [action, setAction] = useState("");
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search.trim().toLowerCase());

  const fetchCases = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setCases(await api.cases({ limit: 200, offset: 0 }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to load recovery cases");
      setCases([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchCases();
  }, [fetchCases]);

  const filtered = (cases ?? []).filter((item) => {
    if (state && item.state !== state) return false;
    if (category && item.failure_category !== category) return false;
    if (action && item.chosen_action !== action) return false;
    if (!deferredSearch) return true;
    return [item.id, item.razorpay_payment_id, item.razorpay_payment_link_id]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(deferredSearch));
  });

  const clearFilters = () => {
    setState("");
    setCategory("");
    setAction("");
    setSearch("");
  };

  return (
    <div className="space-y-5" data-testid="case-list-page">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-sm font-semibold uppercase tracking-[0.16em] text-blue-300">Recovery cases</p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight text-white">What failed, and what happens next?</h1>
          <p className="mt-2 text-sm text-slate-400">Open a case to inspect diagnosis, safety checks, alternatives, and provider evidence.</p>
        </div>
        <button onClick={fetchCases} className="self-start rounded-md border border-slate-700 px-3 py-2 text-sm font-medium text-slate-300 hover:border-slate-500 hover:bg-slate-800">
          Refresh cases
        </button>
      </div>

      <Card padding="p-0" className="overflow-hidden">
        <div className="flex flex-col gap-3 border-b border-[#242d3b] bg-[#0e131b] px-5 py-4 lg:flex-row lg:items-end">
          <label className="min-w-0 flex-1 text-xs font-medium text-slate-400">
            Search cases
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Payment ID, link ID, or case ID"
              className="mt-1.5 h-10 w-full rounded-md border border-slate-700 bg-[#090d13] px-3 text-sm text-slate-100 placeholder:text-slate-600"
            />
          </label>
          <label className="text-xs font-medium text-slate-400">
            State
            <select value={state} onChange={(event) => setState(event.target.value)} className="mt-1.5 h-10 w-full min-w-44 rounded-md border border-slate-700 bg-[#090d13] px-3 text-sm text-slate-200">
              {STATE_OPTIONS.map((value) => <option key={value} value={value}>{value ? stateLabel(value) : "All states"}</option>)}
            </select>
          </label>
          <label className="text-xs font-medium text-slate-400">
            Failure
            <select value={category} onChange={(event) => setCategory(event.target.value)} className="mt-1.5 h-10 w-full min-w-52 rounded-md border border-slate-700 bg-[#090d13] px-3 text-sm text-slate-200">
              {CATEGORY_OPTIONS.map((value) => <option key={value} value={value}>{value ? failureLabel(value) : "All failures"}</option>)}
            </select>
          </label>
          <label className="text-xs font-medium text-slate-400">
            Recovery action
            <select value={action} onChange={(event) => setAction(event.target.value)} className="mt-1.5 h-10 w-full min-w-52 rounded-md border border-slate-700 bg-[#090d13] px-3 text-sm text-slate-200">
              {ACTION_OPTIONS.map((value) => <option key={value} value={value}>{value ? actionLabel(value) : "All actions"}</option>)}
            </select>
          </label>
          <button onClick={clearFilters} className="h-10 rounded-md px-3 text-sm font-medium text-slate-400 hover:bg-slate-800 hover:text-white">Clear</button>
        </div>

        <div className="flex items-center justify-between border-b border-[#242d3b] px-5 py-3 text-sm">
          <span className="font-medium text-slate-200">{filtered.length} cases</span>
          <span className="text-xs text-slate-500">Newest activity first</span>
        </div>

        {loading && <div className="p-6"><Loading label="Loading recovery cases…" /></div>}
        {error && <div className="p-5"><ErrorState message={error} onRetry={fetchCases} /></div>}
        {!loading && !error && filtered.length === 0 && <div className="p-5"><EmptyState title="No cases match these filters" description="Clear filters to return to all recovery cases." /></div>}

        {!loading && !error && filtered.length > 0 && (
          <div className="max-h-[calc(100vh-330px)] min-h-[420px] overflow-auto">
            <table className="w-full min-w-[1050px] text-left">
              <thead className="sticky top-0 z-10 bg-[#11161f] text-xs uppercase tracking-wide text-slate-500 shadow-[0_1px_0_#242d3b]">
                <tr>
                  <th className="px-5 py-3 font-medium">Customer / case</th>
                  <th className="px-4 py-3 font-medium">Amount</th>
                  <th className="px-4 py-3 font-medium">Failure</th>
                  <th className="px-4 py-3 font-medium">Recovery action</th>
                  <th className="px-4 py-3 font-medium">State</th>
                  <th className="px-5 py-3 text-right font-medium">Last activity</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((item) => (
                  <tr
                    key={item.id}
                    onClick={() => onSelect(item.id)}
                    onKeyDown={(event) => event.key === "Enter" && onSelect(item.id)}
                    tabIndex={0}
                    role="button"
                    aria-label={`Open case ${item.id.slice(0, 8)}`}
                    className={`group cursor-pointer border-b border-[#202735] text-sm hover:bg-blue-500/[0.05] ${selectedId === item.id ? "bg-blue-500/[0.08] shadow-[inset_3px_0_0_#6c91ff]" : ""}`}
                  >
                    <td className="px-5 py-3.5">
                      <div className="font-semibold text-slate-100">{shortCaseName(item)}</div>
                      <div className="mt-0.5 max-w-64 truncate font-mono text-xs text-slate-500">{item.razorpay_payment_id ?? item.id}</div>
                    </td>
                    <td className="px-4 py-3.5 text-base font-semibold text-white">{formatRupees(item.amount)}</td>
                    <td className="px-4 py-3.5">
                      <div className="font-medium text-slate-200">{failureLabel(item.failure_category)}</div>
                      {item.error_reason && <div className="mt-0.5 text-xs text-slate-500">{item.error_reason.replaceAll("_", " ")}</div>}
                    </td>
                    <td className="px-4 py-3.5">
                      <div className="font-medium text-blue-200">{actionLabel(item.chosen_action)}</div>
                      {item.latest_action_status && <div className="mt-0.5 text-xs text-slate-500">{stateLabel(item.latest_action_status)}</div>}
                    </td>
                    <td className="px-4 py-3.5"><Badge tone={stateTone(item.state)}>{stateLabel(item.state)}</Badge></td>
                    <td className="px-5 py-3.5 text-right text-xs text-slate-500">{formatDate(item.updated_at ?? item.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
