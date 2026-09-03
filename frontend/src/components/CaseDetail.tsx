import { useState } from "react";
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

function CopyId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(id);
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      }}
      className="text-xs font-medium text-slate-500 hover:text-slate-200"
      title="Copy full ID"
    >
      {copied ? "Copied" : `Case ${id.slice(0, 8)}`}
    </button>
  );
}

function actionReason(action: string | null, detail: CaseDetail): string {
  if (detail.human_review_reason) return "An untrusted customer reply failed deterministic validation. Recovery is paused for human review, and no payment state was changed.";
  if (action === "WAIT") return "This looks temporary. Waiting avoids an unnecessary customer interruption while keeping a durable recovery schedule.";
  if (action === "WAIT_FOR_NATIVE_RETRY") return "Razorpay already has a native retry path, so RecoveryOS waits instead of creating duplicate customer work.";
  if (action === "CREATE_PAYMENT_LINK" && detail.failure_category === "INVALID_INSTRUMENT") return "The current payment instrument is no longer valid. A fresh payment path is the best allowed intervention.";
  if (action === "CREATE_PAYMENT_LINK") return "Another direct retry may fail again. A Payment Link offers a recoverable path with less friction than contacting the customer.";
  if (action === "CONTACT_CUSTOMER") return "The customer can resolve this authentication failure, and contact is within the configured safety limits.";
  if (action === "COLLECT_PROMISE_TO_PAY") return "A structured payment commitment is more useful than another blind retry.";
  if (action === "ESCALATE") return "Automation cannot safely resolve this case, so RecoveryOS routes it to an operator.";
  if (action === "STOP") return "Stopping is safer than another low-value or disallowed recovery attempt.";
  return detail.failure_explanation.category_meaning;
}

function blockedReasonLabel(reason: string | null): string {
  if (!reason) return "Guardrail restriction";
  if (/semantic|infeasible|failure/i.test(reason)) return "Not compatible with this failure";
  if (/cooldown/i.test(reason)) return "Contact cooldown active";
  if (/amount/i.test(reason)) return "Above automation amount limit";
  if (/contact/i.test(reason)) return "Contact limit reached";
  if (/attempt/i.test(reason)) return "Attempt budget exhausted";
  return humanize(reason);
}

function CaseHero({ detail, inspector }: { detail: CaseDetail; inspector: DecisionInspector | null }) {
  const chosenAction = inspector?.chosen_action ?? detail.decisions.at(-1)?.chosen_action ?? null;
  return (
    <Card padding="p-0" className="overflow-hidden" data-testid="case-hero">
      <div className="grid lg:grid-cols-[1.15fr_0.85fr]">
        <div className="px-7 py-6 lg:px-8">
          <div className="flex items-center gap-3">
            <CopyId id={detail.id} />
            <span className="h-1 w-1 rounded-full bg-slate-600" />
            <span className="text-xs text-slate-500">Updated {formatDate(detail.updated_at)}</span>
          </div>
          <div className="mt-5 flex flex-wrap items-end gap-x-5 gap-y-2">
            <div className="text-4xl font-semibold tracking-tight text-white">{formatRupees(detail.amount)}</div>
            <div className="pb-1 text-base text-slate-400">revenue at risk</div>
          </div>
          <div className="mt-5">
            <div className="text-sm font-semibold uppercase tracking-[0.12em] text-slate-500">What failed</div>
            <h1 className="mt-1 text-2xl font-semibold text-slate-100">{failureLabel(detail.failure_category)}</h1>
            <p className="mt-2 max-w-3xl text-sm leading-relaxed text-slate-400">{detail.failure_explanation.category_meaning}</p>
          </div>
        </div>

        <div className="border-t border-[#273244] bg-blue-500/[0.075] px-7 py-6 lg:border-l lg:border-t-0 lg:px-8">
          <div className="flex items-start justify-between gap-4">
            <div>
              <div className="text-sm font-semibold uppercase tracking-[0.12em] text-blue-300">RecoveryOS chose</div>
              <div className="mt-2 text-3xl font-semibold tracking-tight text-white">{actionLabel(chosenAction)}</div>
            </div>
            <Badge tone={stateTone(detail.state)}>{stateLabel(detail.state)}</Badge>
          </div>
          <p className="mt-5 text-base leading-relaxed text-slate-300">{actionReason(chosenAction, detail)}</p>
          <div className="mt-5 flex flex-wrap items-center gap-3 text-xs text-slate-500">
            <span>{inspector?.policy_mode ? humanize(inspector.policy_mode) : "Baseline"} policy</span>
            {inspector?.is_adaptive && <><span>·</span><span>Friction-aware ranking</span></>}
            {detail.provider_truth.has_payment_link && <><span>·</span><span>Provider reference persisted</span></>}
          </div>
        </div>
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
    return <Card><EmptyState title="No decision recorded yet" description="This case has been detected but no recovery action has been selected." /></Card>;
  }
  return (
    <Card padding="p-0" className="overflow-hidden" data-testid="decision-comparison">
      <div className="flex flex-col gap-2 border-b border-[#242d3b] px-6 py-5 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-sm font-semibold uppercase tracking-[0.14em] text-blue-300">Why this action?</p>
          <h2 className="mt-1 text-xl font-semibold text-white">Recovery alternatives, compared</h2>
        </div>
        <div className="text-xs text-slate-500">Persisted at decision time · {formatDate(inspector.created_at)}</div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[840px] text-left text-sm">
          <thead className="bg-[#0e131b] text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-6 py-3 font-medium">Intervention</th>
              <th className="px-4 py-3 font-medium">Eligibility</th>
              <th className="px-4 py-3 font-medium">Recovery chance</th>
              <th className="px-4 py-3 font-medium">Customer friction</th>
              <th className="px-4 py-3 font-medium">Net utility</th>
              <th className="px-6 py-3 text-right font-medium">Result</th>
            </tr>
          </thead>
          <tbody>
            {inspector.candidates.map((candidate) => (
              <tr key={candidate.action} className={`border-t border-[#202735] ${candidate.selected ? "bg-blue-500/[0.09] shadow-[inset_3px_0_0_#6c91ff]" : ""}`}>
                <td className="px-6 py-3.5">
                  <div className={`font-semibold ${candidate.selected ? "text-white" : "text-slate-200"}`}>{actionLabel(candidate.action)}</div>
                  {candidate.expected_recovered_value != null && <div className="mt-0.5 text-xs text-slate-500">{formatRupees(candidate.expected_recovered_value)} expected recovery</div>}
                </td>
                <td className="px-4 py-3.5">
                  {candidate.allowed === false ? (
                    <div><Badge tone="danger" size="sm">Blocked</Badge><div className="mt-1 max-w-48 text-xs text-rose-200/70">{blockedReasonLabel(candidate.blocked_reason)}</div></div>
                  ) : candidate.allowed === true ? <span className="font-medium text-emerald-300">Eligible</span> : <span className="text-slate-500">Unavailable</span>}
                </td>
                <td className="px-4 py-3.5 font-semibold text-slate-200">{candidateRecovery(candidate)}</td>
                <td className="px-4 py-3.5">
                  {candidate.friction_score != null ? <><span className="font-semibold text-slate-200">{candidate.friction_score.toFixed(0)}</span>{candidate.friction_penalty != null && <div className="mt-0.5 text-xs text-slate-500">{formatRupees(candidate.friction_penalty)} penalty</div>}</> : <span className="text-slate-500">—</span>}
                </td>
                <td className="px-4 py-3.5 font-semibold text-slate-100">{candidate.utility != null ? formatRupees(candidate.utility) : "—"}</td>
                <td className="px-6 py-3.5 text-right">{candidate.selected ? <Badge tone="info">Selected</Badge> : <span className="text-slate-600">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex flex-col gap-2 border-t border-[#242d3b] bg-[#0e131b] px-6 py-3 text-xs text-slate-500 md:flex-row md:items-center md:justify-between">
        <span>{inspector.is_adaptive ? "Net utility = expected recovered value − action cost − customer friction penalty." : "Baseline decisions show eligibility and ordering; model probabilities are intentionally unavailable."}</span>
        {inspector.explanation && <span className="max-w-2xl text-slate-400">{inspector.explanation}</span>}
      </div>
    </Card>
  );
}

function GuardrailChecklist({ detail }: { detail: CaseDetail }) {
  const guardrails = detail.guardrails as Record<string, GuardrailValue | string[]>;
  const definitions: Array<{ key: string; label: string; value: (item: GuardrailValue) => string }> = [
    { key: "max_contacts_per_case", label: "Contact limit", value: (item) => `${item.used ?? 0} of ${item.limit ?? "—"} used` },
    { key: "max_contacts_per_7_days", label: "Seven-day contact cap", value: (item) => `${item.used ?? 0} of ${item.limit ?? "—"} used` },
    { key: "cooldown", label: "Contact cooldown", value: (item) => item.pass === false ? `${item.hours ?? "—"}-hour window active` : "No active cooldown" },
    { key: "max_automated_amount", label: "Automation amount", value: (item) => `${formatRupees(item.amount ?? detail.amount)} of ${formatRupees(item.limit)}` },
    { key: "max_total_attempts", label: "Attempt budget", value: (item) => `${item.used ?? 0} of ${item.limit ?? "—"} used` },
  ];
  const rows = definitions.flatMap((definition) => {
    const item = guardrails[definition.key];
    return item && !Array.isArray(item) ? [{ ...definition, item }] : [];
  });
  return (
    <Card data-testid="guardrail-checklist">
      <p className="text-sm font-semibold uppercase tracking-[0.14em] text-slate-500">Safety checks</p>
      <h2 className="mt-1 text-xl font-semibold text-white">Rules before ranking</h2>
      <p className="mt-2 text-sm leading-relaxed text-slate-400">Deterministic guardrails decide what the policy is allowed to compare.</p>
      <div className="mt-5 divide-y divide-[#242d3b]">
        {rows.map((row) => (
          <div key={row.key} className="flex items-start gap-3 py-3 first:pt-0 last:pb-0">
            <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs font-bold ${row.item.pass === false ? "bg-rose-500/15 text-rose-300" : "bg-emerald-500/15 text-emerald-300"}`} aria-label={row.item.pass === false ? "Blocked" : "Passed"}>{row.item.pass === false ? "!" : "✓"}</span>
            <div className="min-w-0">
              <div className="text-sm font-medium text-slate-200">{row.label}</div>
              <div className="mt-0.5 text-xs text-slate-500">{row.value(row.item)}</div>
            </div>
          </div>
        ))}
        {rows.length === 0 && <div className="text-sm text-slate-500">Safety status is not available for this case.</div>}
      </div>
    </Card>
  );
}

function ProviderTruth({ detail, environmentMode }: { detail: CaseDetail; environmentMode?: string }) {
  if (!detail.provider_truth.has_payment_link) return null;
  const mode = detail.provider_truth.mode_label && detail.provider_truth.mode_label !== "unavailable" ? detail.provider_truth.mode_label : environmentMode;
  return (
    <Card data-testid="provider-truth">
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-semibold uppercase tracking-[0.14em] text-emerald-300">Provider truth</p>
            {mode && <Badge tone="success">{mode}</Badge>}
          </div>
          <h2 className="mt-2 text-xl font-semibold text-white">Razorpay Payment Link</h2>
          <p className="mt-2 max-w-3xl text-sm leading-relaxed text-slate-400">The durable Action ID is the provider reference. RecoveryOS validates the provider response before adoption; only provider evidence can mark the case recovered.</p>
        </div>
        <div className="min-w-72 rounded-lg bg-[#0b1017] px-4 py-3">
          <div className="flex items-center justify-between gap-4">
            <span className="text-xs text-slate-500">Action status</span>
            <Badge tone={detail.provider_truth.status === "EXECUTED" ? "success" : "neutral"}>{stateLabel(detail.provider_truth.status)}</Badge>
          </div>
          {detail.provider_truth.short_url && <a href={detail.provider_truth.short_url} target="_blank" rel="noreferrer" className="mt-3 block truncate text-sm font-medium text-blue-300 underline decoration-blue-400/40 underline-offset-4">{detail.provider_truth.short_url}</a>}
          {detail.state === "RECOVERED" && <div className="mt-3 text-sm font-semibold text-emerald-300">Payment confirmed by Razorpay</div>}
        </div>
      </div>
    </Card>
  );
}

function CustomerConversation({ detail }: { detail: CaseDetail }) {
  if (detail.messages.length === 0 && detail.promises_to_pay.length === 0 && !detail.human_review_reason) return null;
  return (
    <Card padding="p-0" className="overflow-hidden" data-testid="customer-conversation">
      <div className="border-b border-[#242d3b] px-6 py-5">
        <p className="text-sm font-semibold uppercase tracking-[0.14em] text-blue-300">Customer interaction</p>
        <h2 className="mt-1 text-xl font-semibold text-white">Conversation and Promise to Pay</h2>
        <p className="mt-1 text-sm text-slate-400">Language can assist interpretation. Deterministic validation decides what becomes workflow state.</p>
      </div>
      {detail.human_review_reason && (
        <div className="border-b border-amber-500/20 bg-amber-500/[0.07] px-6 py-3 text-sm text-amber-100">
          <span className="font-semibold">Safety intervention:</span> untrusted instructions were rejected and the case was routed to human review. No promise or payment outcome was created.
        </div>
      )}
      <div className="grid gap-0 lg:grid-cols-[1.08fr_0.92fr]">
        <div className="space-y-4 px-6 py-5 lg:border-r lg:border-[#242d3b]">
          {detail.messages.map((message) => {
            const outbound = message.direction === "outbound";
            return (
              <div key={message.id} className={`flex ${outbound ? "justify-start" : "justify-end"}`}>
                <div className={`max-w-[85%] ${outbound ? "" : "text-right"}`}>
                  <div className="mb-1.5 flex items-center gap-2 text-xs text-slate-500">
                    <span className="font-semibold text-slate-300">{outbound ? "RecoveryOS" : "Customer"}</span>
                    {message.status && <Badge tone={message.status === "DRAFT" ? "warning" : "success"} size="sm">{message.status === "DRAFT" ? "Draft · not sent" : stateLabel(message.status)}</Badge>}
                  </div>
                  <div className={`rounded-lg px-4 py-3 text-left text-sm leading-relaxed ${outbound ? "bg-slate-800/80 text-slate-200" : "bg-blue-500/15 text-blue-50"}`}>{message.body}</div>
                </div>
              </div>
            );
          })}
          {detail.messages.length === 0 && <div className="text-sm text-slate-500">No customer messages for this case.</div>}
        </div>
        <div className="bg-[#0e131b] px-6 py-5">
          {detail.promises_to_pay.length > 0 ? detail.promises_to_pay.map((promise) => {
            const followUp = detail.actions.find((action) => action.promise_to_pay_id === promise.id);
            return (
              <div key={promise.id}>
                <div className="flex items-center justify-between gap-3">
                  <div className="text-sm font-semibold text-slate-400">RecoveryOS extraction</div>
                  <Badge tone={promise.status === "PENDING" ? "info" : promise.status === "KEPT" ? "success" : promise.status === "BROKEN" ? "danger" : "muted"}>{stateLabel(promise.status)}</Badge>
                </div>
                <div className="mt-5 text-sm font-semibold uppercase tracking-[0.12em] text-blue-300">Promise to Pay</div>
                <div className="mt-2 text-3xl font-semibold tracking-tight text-white">{formatRupees(promise.promised_amount)}</div>
                <div className="mt-1 text-sm text-slate-400">Promised by {formatDate(promise.promised_date)}</div>
                <div className="mt-5 grid grid-cols-2 gap-3">
                  <div className="rounded-lg bg-slate-800/60 p-3"><div className="text-xs text-slate-500">Confidence</div><div className="mt-1 text-lg font-semibold text-white">{promise.confidence != null ? `${Math.round(promise.confidence * 100)}%` : "—"}</div></div>
                  <div className="rounded-lg bg-slate-800/60 p-3"><div className="text-xs text-slate-500">Validation</div><div className="mt-1 text-sm font-semibold text-white">{promise.llm_provider ? "LLM + rules" : "Deterministic"}</div></div>
                </div>
                {followUp && <div className="mt-4 flex items-center gap-2 rounded-lg border border-blue-500/20 bg-blue-500/[0.07] px-3 py-2 text-sm text-blue-100"><span className="text-blue-300">✓</span><span>Follow-up scheduled for {formatDate(followUp.scheduled_for)}</span></div>}
              </div>
            );
          }) : (
            <div className="flex min-h-48 flex-col items-center justify-center text-center">
              <div className="text-sm font-semibold text-slate-300">No Promise to Pay created</div>
              <div className="mt-1 max-w-sm text-sm text-slate-500">Customer text cannot create recovery state unless deterministic validation succeeds.</div>
            </div>
          )}
        </div>
      </div>
    </Card>
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

function RecoveryTimeline({ detail }: { detail: CaseDetail }) {
  const visible = importantTimeline(detail.timeline);
  return (
    <Card data-testid="recovery-timeline">
      <div className="flex flex-col gap-2 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-sm font-semibold uppercase tracking-[0.14em] text-slate-500">Recovery timeline</p>
          <h2 className="mt-1 text-xl font-semibold text-white">Important events</h2>
        </div>
        <span className="text-xs text-slate-500">{detail.timeline.length} total audit events retained</span>
      </div>
      <div className="mt-5 grid gap-3 md:grid-cols-3 xl:grid-cols-6">
        {visible.map((event, index) => (
          <div key={`${event.timestamp}-${index}`} className="relative border-l border-slate-700 pl-3">
            <div className="text-xs font-semibold text-blue-300">{String(index + 1).padStart(2, "0")}</div>
            <div className="mt-1 text-sm font-semibold text-slate-200">{timelineTitle(event)}</div>
            <div className="mt-1 text-xs text-slate-500">{formatDate(event.timestamp)}</div>
          </div>
        ))}
      </div>
      <details className="mt-5 border-t border-[#242d3b] pt-4">
        <summary className="cursor-pointer text-sm font-semibold text-slate-300 hover:text-white">View full audit trail</summary>
        <div className="mt-4 max-h-96 space-y-2 overflow-auto pr-2">
          {detail.timeline.map((event, index) => (
            <div key={`${event.timestamp}-${index}`} className="flex gap-4 rounded-md bg-[#0b1017] px-3 py-2.5">
              <div className="w-36 shrink-0 text-xs text-slate-500">{formatDate(event.timestamp)}</div>
              <div><div className="text-sm font-medium text-slate-200">{event.title}</div><div className="mt-0.5 text-xs text-slate-500">{event.description}</div></div>
            </div>
          ))}
        </div>
      </details>
    </Card>
  );
}

function TechnicalDetails({ detail, inspector }: { detail: CaseDetail; inspector: DecisionInspector | null }) {
  return (
    <details className="rounded-xl border border-[#242d3b] bg-[#0e131b]">
      <summary className="cursor-pointer px-5 py-4 text-sm font-semibold text-slate-300 hover:text-white">Technical details</summary>
      <div className="border-t border-[#242d3b] px-5 py-5">
        <div className="grid gap-4 md:grid-cols-3">
          <div><div className="text-xs uppercase tracking-wide text-slate-500">Provider IDs</div><div className="mt-2 space-y-1 break-all font-mono text-xs text-slate-400"><div>{detail.razorpay_payment_id ?? "—"}</div><div>{detail.razorpay_payment_link_id ?? "—"}</div><div>{detail.razorpay_subscription_id ?? "—"}</div></div></div>
          <div><div className="text-xs uppercase tracking-wide text-slate-500">Model provenance</div><div className="mt-2 space-y-1 font-mono text-xs text-slate-400"><div>{inspector?.model_provenance.model_version ?? "No model"}</div><div>{inspector?.model_provenance.model_fingerprint?.slice(0, 16) ?? "—"}</div><div>{inspector?.model_provenance.friction_profile ?? "—"}</div></div></div>
          <div><div className="text-xs uppercase tracking-wide text-slate-500">Normalized signal</div><div className="mt-2 space-y-1 font-mono text-xs text-slate-400"><div>source: {detail.error_source ?? "—"}</div><div>step: {detail.error_step ?? "—"}</div><div>reason: {detail.error_reason ?? "—"}</div></div></div>
        </div>
        <pre className="mt-5 max-h-80 overflow-auto rounded-lg bg-black/30 p-4 text-xs leading-relaxed text-slate-500">{JSON.stringify({ actions: detail.actions, decisions: detail.decisions, provider_truth: detail.provider_truth, payment_events: detail.payment_events, audit_trail: detail.audit_trail }, null, 2)}</pre>
      </div>
    </details>
  );
}

export function CaseDetailView({ detail, environmentMode }: { detail: CaseDetail | null; environmentMode?: string }) {
  if (!detail) return <Card><EmptyState title="Select a case to inspect" description="Open any case to see the selected intervention and why it won." /></Card>;
  const inspector = detail.decision_inspectors.at(-1) ?? null;
  return (
    <div className="space-y-5 pb-56" data-testid="case-detail-page">
      <CaseHero detail={detail} inspector={inspector} />
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <DecisionComparison inspector={inspector} />
        <GuardrailChecklist detail={detail} />
      </div>
      <ProviderTruth detail={detail} environmentMode={environmentMode} />
      <div id="customer"><CustomerConversation detail={detail} /></div>
      <RecoveryTimeline detail={detail} />
      <TechnicalDetails detail={detail} inspector={inspector} />
    </div>
  );
}
