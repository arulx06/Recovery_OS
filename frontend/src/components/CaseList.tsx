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

function FilterSelect({
  label,
  value,
  onChange,
  options,
  emptyLabel,
  format,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
  emptyLabel: string;
  format: (value: string) => string;
}) {
  return (
    <label className="min-w-0 text-xs font-medium text-slate-400">
      {label}
      <span className="relative mt-1.5 block">
        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="h-10 w-full appearance-none rounded-md border border-slate-700 bg-[#090d13] py-0 pl-3 pr-9 text-sm text-slate-200 hover:border-slate-600"
        >
          {options.map((option) => <option key={option} value={option}>{option ? format(option) : emptyLabel}</option>)}
        </select>
        <svg className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" viewBox="0 0 20 20" fill="none" aria-hidden>
          <path d="m6 8 4 4 4-4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
    </label>
  );
}

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
  const hasFilters = Boolean(state || category || action || search);

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
      <div className="flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-sm font-semibold uppercase tracking-[0.16em] text-blue-300">Recovery cases</p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight text-white sm:text-3xl">What failed, and what happens next?</h1>
          <p className="mt-2 text-base text-slate-400">Open a case to inspect diagnosis, safety checks, alternatives, and provider evidence.</p>
        </div>
        <button
          onClick={fetchCases}
          disabled={loading}
          className="inline-flex h-10 self-start items-center rounded-md border border-slate-700 px-3 text-sm font-medium text-slate-300 hover:border-slate-500 hover:bg-slate-800 disabled:cursor-wait disabled:opacity-60"
        >
          {loading ? "Refreshing…" : "Refresh cases"}
        </button>
      </div>

      <Card padding="p-0" className="overflow-hidden">
        <div className="border-b border-[#242d3b] bg-[#0e131b] p-4 sm:p-5">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-[minmax(250px,1fr)_180px_220px_220px_auto] xl:items-end">
            <label className="min-w-0 text-xs font-medium text-slate-400 md:col-span-2 xl:col-span-1">
              Search cases
              <span className="relative mt-1.5 block">
                <svg className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-600" viewBox="0 0 20 20" fill="none" aria-hidden>
                  <circle cx="8.5" cy="8.5" r="4.75" stroke="currentColor" strokeWidth="1.5" />
                  <path d="m12 12 4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
                <input
                  type="search"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="Payment, link, or case ID"
                  className="h-10 w-full rounded-md border border-slate-700 bg-[#090d13] pl-9 pr-3 text-sm text-slate-100 placeholder:text-slate-600 hover:border-slate-600"
                />
              </span>
            </label>
            <FilterSelect label="State" value={state} onChange={setState} options={STATE_OPTIONS} emptyLabel="All states" format={stateLabel} />
            <FilterSelect label="Failure" value={category} onChange={setCategory} options={CATEGORY_OPTIONS} emptyLabel="All failures" format={failureLabel} />
            <FilterSelect label="Recovery action" value={action} onChange={setAction} options={ACTION_OPTIONS} emptyLabel="All actions" format={actionLabel} />
            <button
              onClick={clearFilters}
              disabled={!hasFilters}
              className="h-10 rounded-md px-3 text-sm font-medium text-slate-400 hover:bg-slate-800 hover:text-white disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-slate-400"
            >
              Clear
            </button>
          </div>
        </div>

        <div className="flex items-center justify-between gap-4 border-b border-[#242d3b] px-4 py-3 text-sm sm:px-5" aria-live="polite">
          <span className="font-medium text-slate-200">{cases === null ? "Loading cases" : `${filtered.length} ${filtered.length === 1 ? "case" : "cases"}`}</span>
          <span className="text-xs text-slate-500">Newest activity first</span>
        </div>

        {loading && cases === null && <div className="p-6"><Loading label="Loading recovery cases…" /></div>}
        {error && <div className="p-5"><ErrorState message={error} onRetry={fetchCases} /></div>}
        {!loading && !error && filtered.length === 0 && (
          <div className="p-5">
            <EmptyState
              title={hasFilters ? "No cases match these filters" : "No recovery cases yet"}
              description={hasFilters ? "Clear or adjust the filters to return to recovery cases." : "New payment failures will appear here when they enter recovery."}
            />
          </div>
        )}

        {!error && filtered.length > 0 && (
          <>
            <div className="divide-y divide-[#202735] lg:hidden">
              {filtered.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => onSelect(item.id)}
                  aria-label={`Open case ${item.id.slice(0, 8)}, ${formatRupees(item.amount)}, ${stateLabel(item.state)}`}
                  className={`block w-full px-4 py-4 text-left hover:bg-blue-500/[0.05] sm:px-5 ${selectedId === item.id ? "bg-blue-500/[0.08] shadow-[inset_3px_0_0_#6c91ff]" : ""}`}
                >
                  <span className="flex items-start justify-between gap-3">
                    <span>
                      <span className="block text-lg font-semibold text-white">{formatRupees(item.amount)}</span>
                      <span className="mt-0.5 block text-sm font-medium text-slate-200">{failureLabel(item.failure_category)}</span>
                    </span>
                    <Badge tone={stateTone(item.state)}>{stateLabel(item.state)}</Badge>
                  </span>
                  <span className="mt-3 grid grid-cols-2 gap-3">
                    <span>
                      <span className="block text-2xs font-medium uppercase tracking-wide text-slate-600">Recovery action</span>
                      <span className="mt-1 block text-sm font-medium text-blue-200">{actionLabel(item.chosen_action)}</span>
                    </span>
                    <span className="text-right">
                      <span className="block text-2xs font-medium uppercase tracking-wide text-slate-600">Last activity</span>
                      <span className="mt-1 block text-xs text-slate-500">{formatDate(item.updated_at ?? item.created_at)}</span>
                    </span>
                  </span>
                  <span className="mt-3 flex items-center justify-between gap-3 border-t border-[#242d3b] pt-3">
                    <span className="truncate font-mono text-xs text-slate-500" title={item.razorpay_payment_id ?? item.id}>{item.razorpay_payment_id ?? item.id}</span>
                    <span className="shrink-0 text-xs text-slate-600">Open →</span>
                  </span>
                </button>
              ))}
            </div>

          <div className="hidden max-h-[calc(100vh-330px)] min-h-[420px] overflow-auto lg:block">
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
                    className={`group cursor-pointer border-b border-[#202735] text-sm hover:bg-blue-500/[0.05] focus-within:bg-blue-500/[0.07] ${selectedId === item.id ? "bg-blue-500/[0.08] shadow-[inset_3px_0_0_#6c91ff]" : ""}`}
                  >
                    <td className="px-5 py-3.5">
                      <button type="button" className="max-w-64 rounded text-left" aria-label={`Open case ${item.id.slice(0, 8)}`}>
                        <span className="block font-semibold text-slate-100">{shortCaseName(item)}</span>
                        <span className="mt-0.5 block truncate font-mono text-xs text-slate-500" title={item.razorpay_payment_id ?? item.id}>{item.razorpay_payment_id ?? item.id}</span>
                      </button>
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
          </>
        )}
      </Card>
    </div>
  );
}
