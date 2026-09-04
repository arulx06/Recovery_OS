import { useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import type { CandidateScore, CaseDetail, DecisionInspector, TimelineEvent } from "../api";
import {
  Badge,
  Card,
  EmptyState,
  actionLabel,
  failureLabel,
  formatDate,
  formatRupees,
  humanize,
  stateLabel,
  stateTone,
} from "./ui";

type GuardrailValue = {
  limit?: number;
  used?: number;
  pass?: boolean;
  hours?: number;
  amount?: number;
  next_allowed_at?: string | null;
};

type DetailTab = "customer" | "timeline" | "technical";

function CopyId({ id, label = "Case" }: { id: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard.writeText(id);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      }}
      className="rounded text-xs font-medium text-slate-500 hover:text-slate-200"
      title={`${label} ID: ${id}. Click to copy.`}
      aria-label={`Copy ${label.toLowerCase()} ID ${id}`}
    >
      {copied ? "Copied" : `${label} ${id.slice(0, 8)}`}
    </button>
  );
}

function actionReason(action: string | null, detail: CaseDetail): string {
  if (detail.human_review_reason) return "Untrusted customer input failed deterministic validation, so automation paused for human review.";
  if (action === "WAIT") return "The failure appears temporary; waiting avoids unnecessary customer contact while preserving a durable retry path.";
  if (action === "WAIT_FOR_NATIVE_RETRY") return "Razorpay already has a native retry path, avoiding duplicate customer work.";
  if (action === "CREATE_PAYMENT_LINK" && detail.failure_category === "INVALID_INSTRUMENT") return "A new payment path avoids retrying the invalid instrument.";
  if (action === "CREATE_PAYMENT_LINK") return "A Payment Link provides a lower-friction recovery path than another direct retry.";
  if (action === "CONTACT_CUSTOMER") return "Customer authentication is required and contact is within the configured safety limits.";
  if (action === "COLLECT_PROMISE_TO_PAY") return "A structured payment commitment is more useful than another blind retry.";
  if (action === "ESCALATE") return "Automation cannot resolve this case safely, so it was routed to an operator.";
  if (action === "STOP") return "Another recovery attempt is low-value or disallowed by policy.";
  return detail.failure_explanation.category_meaning;
}

function blockedReasonLabel(reason: string | null): string {
  if (!reason) return "Guardrail restriction";
  if (/semantic|infeasible|failure/i.test(reason)) return "Incompatible with failure";
  if (/cooldown/i.test(reason)) return "Cooldown active";
  if (/amount/i.test(reason)) return "Above amount limit";
  if (/contact/i.test(reason)) return "Contact limit reached";
  if (/attempt/i.test(reason)) return "Attempt budget exhausted";
  return humanize(reason);
}

function CaseHeader({ detail, inspector }: { detail: CaseDetail; inspector: DecisionInspector | null }) {
  const chosenAction = inspector?.chosen_action ?? detail.decisions.at(-1)?.chosen_action ?? null;
  return (
    <Card padding="p-0" className="overflow-hidden" data-testid="case-hero">
      <div className="grid gap-4 px-4 py-4 sm:px-5 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-start">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-semibold tracking-tight text-white sm:text-3xl">{formatRupees(detail.amount)}</h1>
            <Badge tone={stateTone(detail.state)}>{stateLabel(detail.state)}</Badge>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
            <span className="font-semibold text-slate-100">{failureLabel(detail.failure_category)}</span>
            <span className="hidden text-slate-700 sm:inline" aria-hidden>•</span>
            <span className="text-slate-400">Selected: <strong className="font-semibold text-blue-200">{actionLabel(chosenAction)}</strong></span>
          </div>
          <p className="mt-1 max-w-4xl text-sm leading-relaxed text-slate-500">{detail.failure_explanation.category_meaning}</p>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500 lg:max-w-96 lg:justify-end">
          <span className="truncate font-mono" title={detail.razorpay_payment_id ?? detail.id}>{detail.razorpay_payment_id ?? "Payment ID unavailable"}</span>
          <span aria-hidden>•</span>
          <span>Updated {formatDate(detail.updated_at)}</span>
          <span aria-hidden>•</span>
          <CopyId id={detail.id} />
          <span aria-hidden>•</span>
          <span>{inspector?.policy_mode ? humanize(inspector.policy_mode) : "Baseline"} policy</span>
        </div>
      </div>
      <div className="border-t border-[#242d3b] bg-[#0e131b] px-4 py-2.5 text-sm leading-relaxed text-slate-400 sm:px-5">
        <span className="font-semibold text-slate-200">Why it won: </span>{actionReason(chosenAction, detail)}
      </div>
    </Card>
  );
}

function candidateRecovery(candidate: CandidateScore): string {
  if (candidate.allowed === false) return "Blocked";
  if (candidate.p_recovery == null) return "Not scored";
  return `${(candidate.p_recovery * 100).toFixed(1)}%`;
}

function DecisionComparison({ inspector }: { inspector: DecisionInspector | null }) {
  if (!inspector) {
    return (
      <section className="p-4 sm:p-5" data-testid="decision-comparison">
        <p className="text-xs font-semibold uppercase tracking-[0.14em] text-blue-300">Recovery decision</p>
        <div className="mt-3 rounded-lg border border-dashed border-white/10 px-4 py-6 text-center">
          <div className="text-sm font-medium text-slate-300">No decision recorded yet</div>
          <div className="mt-1 text-sm text-slate-500">This case has been detected but no recovery action has been selected.</div>
        </div>
      </section>
    );
  }

  return (
    <section className="min-w-0" data-testid="decision-comparison" aria-labelledby="decision-title">
      <div className="flex flex-col gap-1 border-b border-[#242d3b] px-4 py-3 sm:flex-row sm:items-end sm:justify-between sm:px-5">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.14em] text-blue-300">Why this action?</p>
          <h2 id="decision-title" className="mt-0.5 text-base font-semibold text-white">Recovery alternatives</h2>
        </div>
        <span className="text-2xs text-slate-600">Decision snapshot · {formatDate(inspector.created_at)}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-left text-xs">
          <thead className="bg-[#0e131b] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2 font-medium sm:px-5">Intervention</th>
              <th className="px-3 py-2 font-medium">Eligible</th>
              <th className="px-3 py-2 font-medium">Recovery</th>
              <th className="px-3 py-2 font-medium">Friction</th>
              <th className="px-3 py-2 font-medium">Utility</th>
              <th className="px-4 py-2 text-right font-medium sm:px-5">Result</th>
            </tr>
          </thead>
          <tbody>
            {inspector.candidates.map((candidate) => (
              <tr key={candidate.action} className={`border-t border-[#202735] ${candidate.selected ? "bg-blue-500/[0.07] shadow-[inset_2px_0_0_#6c91ff]" : ""}`}>
                <td className="px-4 py-2.5 sm:px-5">
                  <div className={`font-semibold ${candidate.selected ? "text-white" : "text-slate-300"}`}>{actionLabel(candidate.action)}</div>
                </td>
                <td className="px-3 py-2.5">
                  {candidate.allowed === false ? (
                    <div><span className="font-medium text-rose-300">Blocked</span><div className="mt-0.5 max-w-36 text-2xs text-rose-200/60">{blockedReasonLabel(candidate.blocked_reason)}</div></div>
                  ) : candidate.allowed === true ? <span className="font-medium text-emerald-300">Yes</span> : <span className="text-slate-600">Unavailable</span>}
                </td>
                <td className="px-3 py-2.5">
                  <span className="font-medium text-slate-200">{candidateRecovery(candidate)}</span>
                  {candidate.expected_recovered_value != null && <div className="mt-0.5 text-2xs text-slate-600">{formatRupees(candidate.expected_recovered_value)} expected</div>}
                </td>
                <td className="px-3 py-2.5">
                  {candidate.friction_score != null ? (
                    <><span className="font-medium text-slate-200">{candidate.friction_score.toFixed(0)}</span>{candidate.friction_penalty != null && <div className="mt-0.5 text-2xs text-slate-600">{formatRupees(candidate.friction_penalty)} penalty</div>}</>
                  ) : <span className="text-slate-600">Not available</span>}
                </td>
                <td className="px-3 py-2.5 font-medium text-slate-200">{candidate.utility != null ? formatRupees(candidate.utility) : <span className="font-normal text-slate-600">Not scored</span>}</td>
                <td className="px-4 py-2.5 text-right sm:px-5">{candidate.selected ? <span className="font-semibold text-blue-200">Selected</span> : <span className="sr-only">Not selected</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="border-t border-[#242d3b] bg-[#0e131b] px-4 py-2.5 text-2xs leading-relaxed text-slate-500 sm:px-5">
        {inspector.is_adaptive ? "Utility includes expected recovery, action cost, and customer friction." : "Baseline ordering is shown; model probabilities are intentionally not scored."}
        {inspector.explanation && <span className="ml-2 text-slate-400">{inspector.explanation}</span>}
      </div>
    </section>
  );
}

function guardrailRows(detail: CaseDetail) {
  const guardrails = detail.guardrails as Record<string, GuardrailValue | string[]>;
  const definitions: Array<{ key: string; label: string; value: (item: GuardrailValue) => string }> = [
    { key: "max_contacts_per_case", label: "Contact limit", value: (item) => `${item.used ?? 0} / ${item.limit ?? "Not set"}` },
    { key: "max_contacts_per_7_days", label: "7-day cap", value: (item) => `${item.used ?? 0} / ${item.limit ?? "Not set"}` },
    { key: "cooldown", label: "Cooldown", value: (item) => item.pass === false ? `${item.hours ?? "?"}h active` : "Clear" },
    { key: "max_automated_amount", label: "Amount", value: (item) => `${formatRupees(item.amount ?? detail.amount)} / ${formatRupees(item.limit)}` },
    { key: "max_total_attempts", label: "Attempts", value: (item) => `${item.used ?? 0} / ${item.limit ?? "Not set"}` },
  ];
  return definitions.flatMap((definition) => {
    const item = guardrails[definition.key];
    return item && !Array.isArray(item) ? [{ ...definition, item }] : [];
  });
}

function DecisionContext({ detail, environmentMode }: { detail: CaseDetail; environmentMode?: string }) {
  const rows = guardrailRows(detail);
  const latestAction = detail.latest_action;
  const provider = detail.provider_truth;
  const mode = provider.mode_label && provider.mode_label !== "unavailable"
    ? provider.mode_label
    : provider.simulated
      ? "Simulated"
      : environmentMode;

  return (
    <aside className="border-t border-[#242d3b] bg-[#0e131b] xl:border-l xl:border-t-0" aria-label="Decision context">
      <section className="px-4 py-3.5 sm:px-5" data-testid="guardrail-checklist" aria-labelledby="safety-title">
        <div className="flex items-center justify-between gap-3">
          <h2 id="safety-title" className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-400">Safety</h2>
          <span className="text-2xs text-slate-600">Current case state</span>
        </div>
        <div className="mt-2 space-y-1">
          {rows.map((row) => (
            <div key={row.key} className="grid grid-cols-[16px_minmax(0,1fr)_auto] items-center gap-2 py-1 text-xs">
              <span className={row.item.pass === false ? "text-rose-300" : "text-emerald-300"} aria-hidden>{row.item.pass === false ? "!" : "✓"}</span>
              <span className="text-slate-300">{row.label}<span className="sr-only">: {row.item.pass === false ? "blocked" : "passed"}</span></span>
              <span className="text-right font-medium text-slate-400">{row.value(row.item)}</span>
            </div>
          ))}
          {rows.length === 0 && <div className="py-2 text-xs text-slate-500">Safety status is not available.</div>}
        </div>
      </section>

      <section className="border-t border-[#242d3b] px-4 py-3.5 sm:px-5" data-testid="provider-truth" aria-labelledby="execution-title">
        <div className="flex items-center justify-between gap-3">
          <h2 id="execution-title" className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-400">Execution</h2>
          {mode && <span className="text-2xs font-medium text-slate-500">{mode}</span>}
        </div>
        <div className="mt-2.5 space-y-2.5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-slate-100">{actionLabel(latestAction?.action_type)}</div>
              <div className="mt-0.5 text-xs text-slate-500">Latest recovery action</div>
            </div>
            <span className={`text-xs font-semibold ${latestAction?.status === "EXECUTED" ? "text-emerald-300" : "text-blue-200"}`}>{stateLabel(latestAction?.status)}</span>
          </div>

          {provider.has_payment_link ? (
            <div className="rounded-md border border-emerald-500/15 bg-emerald-500/[0.045] px-3 py-2.5">
              <div className="flex items-center justify-between gap-3 text-xs">
                <span className="font-medium text-slate-300">Razorpay Payment Link</span>
                <span className="font-semibold text-emerald-300">{stateLabel(provider.status)}</span>
              </div>
              <div className={`mt-1 text-xs font-medium ${detail.state === "RECOVERED" ? "text-emerald-300" : "text-slate-400"}`}>
                {detail.state === "RECOVERED" ? "Payment confirmed by Razorpay" : provider.reconciled ? "Provider state reconciled" : "Awaiting provider confirmation"}
              </div>
              {provider.short_url && <a href={provider.short_url} target="_blank" rel="noreferrer" className="mt-2 inline-block text-xs font-semibold text-blue-300 hover:text-blue-200">Open payment link ↗</a>}
            </div>
          ) : (
            <div className="text-xs text-slate-500">No provider payment artifact is required for this action.</div>
          )}
        </div>

        <details className="mt-3 border-t border-[#242d3b] pt-2.5 text-xs">
          <summary className="cursor-pointer font-medium text-slate-500 hover:text-slate-300">View provider details</summary>
          <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-2xs">
            <dt className="text-slate-600">Action ID</dt><dd className="truncate font-mono text-slate-400" title={latestAction?.id ?? undefined}>{latestAction?.id ?? "Not available"}</dd>
            <dt className="text-slate-600">Payment Link ID</dt><dd className="truncate font-mono text-slate-400" title={provider.payment_link_id ?? undefined}>{provider.payment_link_id ?? "Not available"}</dd>
            <dt className="text-slate-600">Reference</dt><dd className="truncate font-mono text-slate-400" title={provider.reference_id ?? undefined}>{provider.reference_id ?? "Not available"}</dd>
            <dt className="text-slate-600">Reconciled</dt><dd className="text-slate-400">{provider.reconciled ? "Yes" : "No"}</dd>
            <dt className="text-slate-600">Simulated</dt><dd className="text-slate-400">{provider.simulated == null ? "Unknown" : provider.simulated ? "Yes" : "No"}</dd>
          </dl>
        </details>
      </section>
    </aside>
  );
}

function DecisionWorkspace({ detail, inspector, environmentMode }: { detail: CaseDetail; inspector: DecisionInspector | null; environmentMode?: string }) {
  return (
    <Card padding="p-0" className="overflow-hidden" data-testid="decision-workspace">
      <div className="grid items-start xl:grid-cols-[minmax(0,2fr)_minmax(300px,1fr)]">
        <DecisionComparison inspector={inspector} />
        <DecisionContext detail={detail} environmentMode={environmentMode} />
      </div>
    </Card>
  );
}

function CustomerActivity({ detail }: { detail: CaseDetail }) {
  const hasPromises = detail.promises_to_pay.length > 0;
  return (
    <div data-testid="customer-conversation">
      {detail.human_review_reason && (
        <div className="mb-3 rounded-md border border-amber-500/20 bg-amber-500/[0.06] px-3 py-2 text-sm text-amber-100">
          <span className="font-semibold">Safety intervention:</span> untrusted instructions were rejected and recovery was routed to human review.
        </div>
      )}
      <div className={`grid gap-4 ${hasPromises ? "lg:grid-cols-[minmax(0,1.3fr)_minmax(280px,0.7fr)]" : ""}`}>
        <section aria-labelledby="customer-messages-title">
          <div className="flex items-center justify-between gap-3">
            <h3 id="customer-messages-title" className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-400">Customer interaction</h3>
            <span className="text-2xs text-slate-600">{detail.messages.length} {detail.messages.length === 1 ? "message" : "messages"}</span>
          </div>
          <div className="mt-3 space-y-3">
            {detail.messages.map((message) => {
              const outbound = message.direction === "outbound";
              return (
                <div key={message.id} className={`flex ${outbound ? "justify-start" : "justify-end"}`}>
                  <div className="max-w-3xl">
                    <div className={`mb-1 flex items-center gap-2 text-2xs text-slate-500 ${outbound ? "" : "justify-end"}`}>
                      <span className="font-semibold text-slate-300">{outbound ? "RecoveryOS" : "Customer"}</span>
                      {message.status && <span className={message.status === "DRAFT" ? "text-amber-300" : "text-emerald-300"}>{message.status === "DRAFT" ? "Draft · not sent" : stateLabel(message.status)}</span>}
                    </div>
                    <div className={`rounded-md px-3 py-2.5 text-base leading-relaxed ${outbound ? "bg-slate-800/70 text-slate-200" : "bg-blue-500/10 text-blue-50"}`}>{message.body}</div>
                  </div>
                </div>
              );
            })}
            {detail.messages.length === 0 && <div className="rounded-md border border-dashed border-white/10 px-3 py-3 text-sm text-slate-500">No customer messages for this case.</div>}
          </div>
        </section>

        {hasPromises ? (
          <section className="border-t border-[#242d3b] pt-4 lg:border-l lg:border-t-0 lg:pl-4 lg:pt-0" aria-labelledby="ptp-title">
            <h3 id="ptp-title" className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-400">Promise to Pay</h3>
            <div className="mt-3 space-y-3">
              {detail.promises_to_pay.map((promise) => {
                const followUp = detail.actions.find((item) => item.promise_to_pay_id === promise.id);
                return (
                  <div key={promise.id} className="rounded-md bg-[#0e131b] p-3">
                    <div className="flex items-start justify-between gap-3">
                      <div><div className="text-xl font-semibold text-white">{formatRupees(promise.promised_amount)}</div><div className="mt-0.5 text-xs text-slate-500">By {formatDate(promise.promised_date)}</div></div>
                      <span className="text-xs font-semibold text-blue-200">{stateLabel(promise.status)}</span>
                    </div>
                    <div className="mt-3 grid grid-cols-2 gap-3 border-t border-[#242d3b] pt-2.5 text-xs">
                      <div><span className="text-slate-600">Confidence</span><div className="mt-0.5 font-medium text-slate-300">{promise.confidence != null ? `${Math.round(promise.confidence * 100)}%` : "Not available"}</div></div>
                      <div><span className="text-slate-600">Extraction</span><div className="mt-0.5 font-medium text-slate-300">{promise.llm_provider ? "LLM + rules" : "Deterministic"}</div></div>
                    </div>
                    {followUp && <div className="mt-2.5 text-xs text-blue-200">Follow-up scheduled {formatDate(followUp.scheduled_for)}</div>}
                  </div>
                );
              })}
            </div>
          </section>
        ) : (
          <div className="flex items-center justify-between gap-3 border-t border-[#242d3b] pt-3 text-xs">
            <span className="font-medium text-slate-400">Promise to Pay</span>
            <span className="text-slate-600">None</span>
          </div>
        )}
      </div>
    </div>
  );
}

function importantTimeline(events: TimelineEvent[]): TimelineEvent[] {
  const productEvents = events.filter((event) =>
    ["payment_event", "decision", "action", "customer_message", "promise_to_pay"].includes(event.type) ||
    /recover|diagnos|promise|decision|payment failed/i.test(event.title),
  );
  const source = productEvents.length > 0 ? productEvents : events;
  if (source.length <= 6) return source;
  return [...source.slice(0, 2), ...source.slice(-4)];
}

function timelineTitle(event: TimelineEvent): string {
  const value = `${event.type} ${event.title}`.toLowerCase();
  if (value.includes("payment.failed") || value.includes("payment failed")) return "Payment failed";
  if (value.includes("diagnos")) return "Failure diagnosed";
  if (value.includes("decision")) return "Recovery action selected";
  if (value.includes("customer replied")) return "Customer replied";
  if (value.includes("promise") && value.includes("record")) return "Promise recorded";
  if (value.includes("promise") && value.includes("pending")) return "Promise validated";
  if (value.includes("follow-up") || value.includes("follow_up")) return "Follow-up scheduled";
  if (value.includes("recover")) return "Recovery confirmed";
  if (value.includes("execut")) return "Action executed";
  if (value.includes("schedul")) return "Action scheduled";
  return event.title;
}

function timelineTone(event: TimelineEvent): string {
  const value = `${event.severity} ${event.title}`.toLowerCase();
  if (/recover|paid|confirmed|executed|success/.test(value)) return "border-emerald-500/40 text-emerald-300";
  if (/fail|review|warn|broken|disput/.test(value)) return "border-amber-500/40 text-amber-300";
  return "border-blue-500/35 text-blue-300";
}

function RecoveryTimeline({ detail }: { detail: CaseDetail }) {
  const visible = importantTimeline(detail.timeline);
  return (
    <div data-testid="recovery-timeline">
      <div className="flex items-end justify-between gap-3">
        <div><p className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-400">Recovery timeline</p><h3 className="mt-0.5 text-base font-semibold text-white">Important events</h3></div>
        <span className="text-2xs text-slate-600">{detail.timeline.length} audit events retained</span>
      </div>
      {visible.length > 0 ? (
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
          {visible.map((event, index) => (
            <div key={`${event.timestamp}-${index}`} className={`border-l-2 pl-3 ${timelineTone(event)}`}>
              <div className="text-2xs font-semibold">{String(index + 1).padStart(2, "0")}</div>
              <div className="mt-1 text-sm font-semibold text-slate-200">{timelineTitle(event)}</div>
              <div className="mt-1 text-2xs leading-snug text-slate-600">{formatDate(event.timestamp)}</div>
            </div>
          ))}
        </div>
      ) : <div className="mt-3 text-xs text-slate-500">No timeline events recorded.</div>}
      <details className="mt-4 border-t border-[#242d3b] pt-3">
        <summary className="cursor-pointer text-xs font-semibold text-slate-400 hover:text-white">View full audit trail</summary>
        <div className="mt-3 max-h-80 space-y-1.5 overflow-auto pr-2">
          {detail.timeline.map((event, index) => (
            <div key={`${event.timestamp}-${index}`} className="grid gap-1 rounded-md bg-[#0b1017] px-3 py-2 sm:grid-cols-[145px_1fr] sm:gap-3">
              <div className="text-2xs text-slate-600">{formatDate(event.timestamp)}</div>
              <div><div className="text-xs font-medium text-slate-300">{event.title}</div><div className="mt-0.5 text-2xs text-slate-500">{event.description}</div></div>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}

function TechnicalDetails({ detail, inspector }: { detail: CaseDetail; inspector: DecisionInspector | null }) {
  return (
    <div data-testid="technical-details">
      <div className="grid gap-4 md:grid-cols-3">
        <section><h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Identifiers</h3><div className="mt-2 space-y-1.5 break-all font-mono text-xs text-slate-400"><div>{detail.id}</div><div>{detail.latest_action?.id ?? "No action ID"}</div><div>{detail.razorpay_payment_id ?? "No payment ID"}</div><div>{detail.razorpay_payment_link_id ?? "No payment link ID"}</div><div>{detail.razorpay_subscription_id ?? "No subscription ID"}</div></div></section>
        <section><h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Policy provenance</h3><div className="mt-2 space-y-1.5 font-mono text-xs text-slate-400"><div>{inspector?.model_provenance.policy_mode ?? "Baseline"}</div><div>{inspector?.model_provenance.model_version ?? "No model"}</div><div>{inspector?.model_provenance.model_fingerprint ?? "No fingerprint"}</div><div>{inspector?.model_provenance.friction_profile ?? detail.friction.profile}</div></div></section>
        <section><h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Normalized signal</h3><div className="mt-2 space-y-1.5 font-mono text-xs text-slate-400"><div>source: {detail.error_source ?? "Not available"}</div><div>step: {detail.error_step ?? "Not available"}</div><div>reason: {detail.error_reason ?? "Not available"}</div></div></section>
      </div>
      <details className="mt-4 border-t border-[#242d3b] pt-3">
        <summary className="cursor-pointer text-xs font-semibold text-slate-400 hover:text-white">View raw case data</summary>
        <pre className="mt-3 max-h-80 overflow-auto rounded-lg bg-black/30 p-3 text-2xs leading-relaxed text-slate-500">{JSON.stringify({ actions: detail.actions, decisions: detail.decisions, provider_truth: detail.provider_truth, payment_events: detail.payment_events, audit_trail: detail.audit_trail, friction: detail.friction, shadow: detail.shadow }, null, 2)}</pre>
      </details>
    </div>
  );
}

function ActivityWorkspace({ detail, inspector }: { detail: CaseDetail; inspector: DecisionInspector | null }) {
  const defaultTab: DetailTab = detail.messages.length > 0 || detail.promises_to_pay.length > 0 || detail.human_review_reason ? "customer" : "timeline";
  const [activeTab, setActiveTab] = useState<DetailTab>(defaultTab);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const customerCount = detail.messages.length + detail.promises_to_pay.length;
  const tabs: Array<{ id: DetailTab; label: string }> = [
    { id: "customer", label: `Customer${customerCount > 0 ? ` (${customerCount})` : ""}` },
    { id: "timeline", label: `Timeline (${Math.min(importantTimeline(detail.timeline).length, 6)})` },
    { id: "technical", label: "Technical" },
  ];

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex = index;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    else return;
    event.preventDefault();
    setActiveTab(tabs[nextIndex].id);
    tabRefs.current[nextIndex]?.focus();
  };

  return (
    <Card padding="p-0" className="overflow-hidden" data-testid="case-activity-workspace">
      <div className="flex overflow-x-auto border-b border-[#242d3b] bg-[#0e131b] px-3" role="tablist" aria-label="Case activity details">
        {tabs.map((tab, index) => (
          <button
            key={tab.id}
            ref={(node) => { tabRefs.current[index] = node; }}
            type="button"
            role="tab"
            id={`case-tab-${tab.id}`}
            aria-selected={activeTab === tab.id}
            aria-controls={`case-panel-${tab.id}`}
            tabIndex={activeTab === tab.id ? 0 : -1}
            onClick={() => setActiveTab(tab.id)}
            onKeyDown={(event) => onTabKeyDown(event, index)}
            className={`relative shrink-0 px-3 py-3 text-xs font-semibold ${activeTab === tab.id ? "text-white after:absolute after:inset-x-3 after:bottom-0 after:h-0.5 after:bg-blue-400" : "text-slate-500 hover:text-slate-300"}`}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id={`case-panel-${activeTab}`}
        aria-labelledby={`case-tab-${activeTab}`}
        tabIndex={0}
        className="p-4 sm:p-5"
      >
        {activeTab === "customer" && <CustomerActivity detail={detail} />}
        {activeTab === "timeline" && <RecoveryTimeline detail={detail} />}
        {activeTab === "technical" && <TechnicalDetails detail={detail} inspector={inspector} />}
      </div>
    </Card>
  );
}

export function CaseDetailView({ detail, environmentMode }: { detail: CaseDetail | null; environmentMode?: string }) {
  if (!detail) return <Card><EmptyState title="Select a case to inspect" description="Open any case to see the selected intervention and why it won." /></Card>;
  const inspector = detail.decision_inspectors.at(-1) ?? null;
  return (
    <div className="space-y-3 pb-6" data-testid="case-detail-page">
      <CaseHeader detail={detail} inspector={inspector} />
      <DecisionWorkspace detail={detail} inspector={inspector} environmentMode={environmentMode} />
      <ActivityWorkspace key={detail.id} detail={detail} inspector={inspector} />
    </div>
  );
}
