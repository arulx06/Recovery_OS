import { useState } from "react";
import type { CaseDetail } from "../api";
import { Badge, Card, SectionTitle, EmptyState, formatRupees, formatDate } from "./ui";

function StateBadge({ state }: { state: string }) {
  const tone = state === "RECOVERED" ? "success" : state === "HUMAN_REVIEW" ? "warning" : state === "DISPUTED" || state === "STOPPED" ? "danger" : state === "WAITING" ? "info" : "neutral";
  return <Badge tone={tone as never}>{state}</Badge>;
}

function ActionBadge({ action }: { action: string | null }) {
  if (!action) return <span className="text-gray-600">—</span>;
  return <Badge tone="info">{action}</Badge>;
}

function CopyId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(id);
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
      className="font-mono text-[11px] text-gray-500 hover:text-gray-300"
      title="Copy full ID"
    >
      {id.slice(0, 8)}… {copied ? "copied" : ""}
    </button>
  );
}

export function CaseDetailView({ detail }: { detail: CaseDetail | null }) {
  const [showRaw, setShowRaw] = useState(false);
  if (!detail) {
    return (
      <Card>
        <EmptyState title="Select a case to inspect" description="Click any row in the case list to open the decision inspector, timeline, and provider truth." />
      </Card>
    );
  }

  const latestInspector = detail.decision_inspectors[detail.decision_inspectors.length - 1] ?? null;
  const hasAdaptive = detail.decision_inspectors.some((d) => d.is_adaptive);
  const sumRecovered = detail.state === "RECOVERED";

  return (
    <div className="space-y-4">
      {/* Header overview */}
      <Card>
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-semibold tracking-tight">Case {detail.id.slice(0, 8)}</h2>
              <CopyId id={detail.id} />
              <Badge tone={detail.source === "razorpay" ? "neutral" : "muted"} size="sm">{detail.source}</Badge>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <StateBadge state={detail.state} />
              <Badge tone="muted" size="sm">{detail.failure_category ?? "UNKNOWN"}</Badge>
              <ActionBadge action={latestInspector?.chosen_action ?? detail.decisions[detail.decisions.length - 1]?.chosen_action ?? null} />
              {detail.provider_truth.mode_label && <Badge tone={detail.provider_truth.simulated ? "muted" : "success"} size="sm">{detail.provider_truth.mode_label}</Badge>}
              {sumRecovered && <Badge tone="success">RECOVERED — provider truth</Badge>}
            </div>
            <div className="mt-2 text-xs text-gray-500">
              created {formatDate(detail.created_at)} · updated {formatDate(detail.updated_at)} · razorpay payment <span className="font-mono text-gray-400">{detail.razorpay_payment_id ?? "—"}</span>
              {detail.razorpay_subscription_id && <span> · subscription <span className="font-mono">{detail.razorpay_subscription_id}</span></span>}
            </div>
          </div>
          <div className="rounded-lg border border-white/10 bg-black/20 p-3 text-right">
            <div className="text-[11px] uppercase tracking-wide text-gray-500">Amount</div>
            <div className="text-xl font-semibold">{formatRupees(detail.amount)} <span className="text-sm font-normal text-gray-500">{detail.currency}</span></div>
            <div className="mt-1 text-xs text-gray-500">
              policy <span className="font-mono text-gray-300">{latestInspector?.policy_mode ?? "baseline"}</span>
              {latestInspector?.is_adaptive && latestInspector.model_provenance.model_version ? ` · ${latestInspector.model_provenance.model_version} ${latestInspector.model_provenance.model_fingerprint?.slice(0, 8) ?? ""}` : ""}
            </div>
            {detail.adaptive_fallback && <div className="mt-1 text-xs text-amber-300">Adaptive fallback → baseline</div>}
          </div>
        </div>

        {detail.human_review_reason && (
          <div className="mt-3 rounded-lg border border-amber-900/30 bg-amber-950/20 px-3 py-2 text-xs text-amber-200/80">
            <span className="font-medium">Human review:</span> {detail.human_review_reason.event} —{" "}
            <span className="font-mono text-[11px]">{JSON.stringify(detail.human_review_reason.detail).slice(0, 300)}</span>
          </div>
        )}
      </Card>

      {/* Why payment failed */}
      <Card>
        <SectionTitle subtitle="Raw normalized signal → failure category → human meaning (deterministic, no LLM)">Why the payment failed</SectionTitle>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
          <div className="rounded-lg border border-white/10 bg-black/20 p-3">
            <div className="text-[11px] uppercase tracking-wide text-gray-500">Raw normalized signal</div>
            <div className="mt-2 space-y-1 font-mono text-xs">
              <div>source: <span className="text-gray-300">{detail.failure_explanation.raw_signal.error_source ?? "—"}</span></div>
              <div>step: <span className="text-gray-300">{detail.failure_explanation.raw_signal.error_step ?? "—"}</span></div>
              <div>reason: <span className="text-gray-300">{detail.failure_explanation.raw_signal.error_reason ?? "—"}</span></div>
            </div>
            {Object.keys(detail.failure_explanation.raw_signal_present).length === 0 && <div className="mt-2 text-xs text-gray-600">No specific signal — falls through to UNKNOWN</div>}
          </div>
          <div className="rounded-lg border border-sky-900/30 bg-sky-950/20 p-3">
            <div className="text-[11px] uppercase tracking-wide text-sky-400">Failure category</div>
            <div className="mt-2">
              <Badge tone="info">{detail.failure_explanation.failure_category}</Badge>
              <div className="mt-1 text-xs font-medium text-sky-200">{detail.failure_explanation.category_label}</div>
            </div>
            <div className="mt-1 text-[11px] text-gray-500">examples: {detail.failure_explanation.raw_examples}</div>
          </div>
          <div className="rounded-lg border border-white/10 bg-black/20 p-3">
            <div className="text-[11px] uppercase tracking-wide text-gray-500">Human meaning</div>
            <div className="mt-2 text-sm text-gray-200">{detail.failure_explanation.category_meaning}</div>
            <div className="mt-2 text-[11px] text-gray-500">Deterministic taxonomy — 8 categories</div>
          </div>
        </div>
        <div className="mt-3 flex items-center gap-2 text-center text-xs text-gray-500">
          <span className="flex-1 rounded bg-white/5 py-1">signal</span>
          <span>→</span>
          <span className="flex-1 rounded bg-sky-950/30 py-1 text-sky-300">category</span>
          <span>→</span>
          <span className="flex-1 rounded bg-white/5 py-1">meaning</span>
        </div>
      </Card>

      {/* Decision Inspector — central feature */}
      <Card>
        <SectionTitle subtitle="What RecoveryOS considered — allowed vs blocked — with P(recovery), expected value, friction, utility. Only persisted facts; missing fields show unavailable.">
          Decision Inspector — central
        </SectionTitle>
        {detail.decision_inspectors.length === 0 && <div className="text-sm text-gray-500">No decisions yet — case may be newly detected</div>}
        {detail.decision_inspectors.map((ins, idx) => (
          <div key={ins.decision_id} className="mb-4 rounded-lg border border-white/10 bg-black/20 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-gray-300">Decision {idx + 1}</span>
                <Badge tone={ins.is_adaptive ? "success" : "neutral"} size="sm">{ins.policy_mode ?? "baseline"}</Badge>
                {ins.is_adaptive && ins.model_provenance.model_version && <span className="text-[11px] font-mono text-gray-500">{ins.model_provenance.model_version} · {ins.model_provenance.model_fingerprint?.slice(0, 8) ?? "—"}</span>}
                <span className="text-[11px] text-gray-600">{formatDate(ins.created_at)}</span>
              </div>
              <ActionBadge action={ins.chosen_action} />
            </div>
            {ins.explanation && <div className="mt-2 text-xs leading-relaxed text-gray-400">“{ins.explanation}”</div>}
            {ins.fallback && <div className="mt-1 text-xs text-amber-300">{ins.fallback}</div>}

            {/* Candidate table */}
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-[11px] uppercase text-gray-500">
                  <tr>
                    <th className="px-2 py-1">Action</th>
                    <th className="px-2 py-1">Allowed?</th>
                    <th className="px-2 py-1">P(recovery)</th>
                    <th className="px-2 py-1">Exp. value</th>
                    <th className="px-2 py-1">Cost</th>
                    <th className="px-2 py-1">Friction</th>
                    <th className="px-2 py-1">Penalty</th>
                    <th className="px-2 py-1">Utility</th>
                    <th className="px-2 py-1">Selected?</th>
                  </tr>
                </thead>
                <tbody>
                  {ins.candidates.map((c) => (
                    <tr key={c.action} className={`border-t border-white/5 ${c.selected ? "bg-sky-950/20" : ""}`}>
                      <td className="px-2 py-1.5">
                        <span className={`rounded px-1.5 py-0.5 font-mono text-[11px] ${c.selected ? "bg-sky-800 text-sky-100" : "bg-white/10 text-gray-300"}`}>{c.action}</span>
                      </td>
                      <td className="px-2 py-1.5">
                        {c.allowed === true ? <Badge tone="success" size="sm">allowed</Badge> : c.allowed === false ? <Badge tone="danger" size="sm">blocked</Badge> : <span className="text-gray-600">unavailable</span>}
                        {c.blocked_reason && <div className="text-[11px] text-gray-500 max-w-[160px] truncate" title={c.blocked_reason}>{c.blocked_reason}</div>}
                      </td>
                      <td className="px-2 py-1.5 font-mono">{c.p_recovery != null ? c.p_recovery.toFixed(3) : <span className="text-gray-600">unavailable</span> as unknown as string}</td>
                      <td className="px-2 py-1.5 font-mono">{c.expected_recovered_value != null ? formatRupees(c.expected_recovered_value) : c.expected_value != null ? formatRupees(c.expected_value) : <span className="text-gray-600">—</span> as unknown as string}</td>
                      <td className="px-2 py-1.5 font-mono">{c.cost != null ? formatRupees(c.cost) : <span className="text-gray-600">—</span> as unknown as string}</td>
                      <td className="px-2 py-1.5 font-mono">{c.friction_score != null ? c.friction_score.toFixed(1) : <span className="text-gray-600">—</span> as unknown as string}</td>
                      <td className="px-2 py-1.5 font-mono">{c.friction_penalty != null ? formatRupees(c.friction_penalty) : <span className="text-gray-600">—</span> as unknown as string}</td>
                      <td className="px-2 py-1.5 font-mono font-semibold">{c.utility != null ? formatRupees(c.utility) : <span className="text-gray-600 font-normal">—</span> as unknown as string}</td>
                      <td className="px-2 py-1.5">{c.selected ? <Badge tone="success" size="sm">✓ selected</Badge> : <span className="text-gray-600">—</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!hasAdaptive && <div className="mt-2 text-[11px] text-gray-600">Baseline has no ML probabilities — this is correct. Baseline picks highest-ranked allowed action for this category.</div>}
            {ins.is_adaptive && (
              <div className="mt-2 rounded bg-white/5 px-2 py-1.5 text-[11px] leading-relaxed text-gray-500">
                utility = expected recovered amount − action cost − friction penalty. Selected action has highest utility among allowed. Weight:{" "}
                <span className="font-mono text-gray-300">{ins.model_provenance.friction_weight ?? "—"} INR/point</span> · profile{" "}
                <span className="font-mono text-gray-300">{ins.model_provenance.friction_profile ?? "—"}</span>
              </div>
            )}
            {/* Guardrails for this decision */}
            <div className="mt-2 text-[11px] text-gray-600">Guardrails: {ins.guardrails ? JSON.stringify(ins.guardrails).slice(0, 180) : "—"}</div>
          </div>
        ))}

        {/* Shadow mode visibility */}
        {detail.shadow && (
          <div className="rounded-lg border border-amber-900/30 bg-amber-950/20 p-3">
            <div className="text-xs font-medium text-amber-200">Shadow mode — executed vs recommended</div>
            <div className="mt-2 grid grid-cols-2 gap-3 text-xs">
              <div className="rounded bg-black/30 p-2">
                <div className="text-[11px] uppercase text-gray-500">Executed (baseline)</div>
                <div className="mt-1"><Badge tone="neutral">{detail.shadow.baseline_chosen ?? "—"}</Badge></div>
              </div>
              <div className="rounded bg-amber-900/20 p-2 border border-amber-800/30">
                <div className="text-[11px] uppercase text-amber-400">Shadow recommendation (adaptive)</div>
                <div className="mt-1"><Badge tone="warning">{detail.shadow.adaptive_suggested ?? "—"}</Badge></div>
                {detail.shadow.disagreement && <div className="mt-1 text-[11px] text-amber-300">Adaptive would have selected {detail.shadow.adaptive_suggested} instead of {detail.shadow.baseline_chosen}.</div>}
              </div>
            </div>
            <div className="mt-2 text-[11px] text-gray-500">Shadow emits audit only — no second Action, queue job, or provider call. Candidates: {detail.shadow.candidates ? Object.keys(detail.shadow.candidates).length + " ranked" : "unavailable"}</div>
          </div>
        )}
      </Card>

      {/* Guardrail visibility + Friction */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Card>
          <SectionTitle subtitle="Computed from current case/contact state at read time — AI cannot bypass">Current guardrail status</SectionTitle>
          {(() => {
            const g = detail.guardrails as Record<string, { limit?: number; used?: number; pass?: boolean; next_allowed_at?: string | null; hours?: number }>;
            if (!g || Object.keys(g).length === 0) return <div className="text-xs text-gray-500">unavailable for this case</div>;
            const rows: Array<[string, { limit?: unknown; used?: unknown; pass?: boolean; next_allowed_at?: string | null; hours?: number }]> = Object.entries(g);
            return (
              <div className="space-y-2">
                {rows.map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between rounded border border-white/10 bg-black/20 px-2 py-1.5 text-xs">
                    <span className="text-gray-400">{k.replaceAll("_", " ")}</span>
                    <span className="flex items-center gap-2">
                      {v.pass === true ? <Badge tone="success" size="sm">PASS</Badge> : v.pass === false ? <Badge tone="danger" size="sm">BLOCKED</Badge> : <Badge tone="muted" size="sm">—</Badge>}
                      <span className="font-mono text-[11px] text-gray-500">
                        {v.used !== undefined && v.limit !== undefined ? `${v.used} / ${v.limit}` : v.limit !== undefined ? `limit ${v.limit}` : ""}
                        {v.hours ? ` · ${v.hours}h cooldown` : ""}
                      </span>
                    </span>
                  </div>
                ))}
                <div className="text-[11px] text-gray-600">Current eligibility, not a reconstruction of prior decisions. Decision-time guardrails remain in each persisted Decision Inspector above.</div>
              </div>
            );
          })()}
        </Card>

        <Card>
          <SectionTitle subtitle="Current contact state applied to the fixed friction formula">Current friction surface</SectionTitle>
          <div className="text-xs text-gray-400">Profile: <span className="font-mono text-gray-200">{detail.friction.profile}</span> {detail.friction.weight != null ? `· weight ${detail.friction.weight}` : "· unavailable"}</div>
          {detail.friction.weight == null && <div className="mt-1 text-[11px] text-gray-600">Baseline has no friction weighting — this is correct.</div>}
          <div className="mt-2 space-y-1">
            {detail.friction.components
              .sort((a, b) => a.total_friction - b.total_friction)
              .slice(0, 6)
              .map((c) => (
                <div key={c.action} className="flex items-center justify-between rounded bg-white/[0.03] px-2 py-1 text-xs">
                  <span className="font-mono text-[11px] text-gray-300">{c.action}</span>
                  <span className="font-mono text-[11px] text-gray-500">
                    base {c.base} {c.previous_contacts ? `+ ${c.previous_contacts}×${c.contact_increment}` : ""} = {c.total_friction} {c.friction_penalty != null ? `· penalty ${formatRupees(c.friction_penalty)}` : ""}
                  </span>
                </div>
              ))}
          </div>
          {detail.friction.chosen_action_actual && (
            <div className="mt-2 rounded bg-sky-950/20 px-2 py-1.5 text-[11px] text-sky-200">
              Chosen {String((detail.friction.chosen_action_actual as Record<string, unknown>).action ?? "")} — friction {String((detail.friction.chosen_action_actual as Record<string, unknown>).friction_score ?? "—")} × {String(latestInspector?.model_provenance.friction_weight ?? "—")} → penalty{" "}
              {formatRupees(((detail.friction.chosen_action_actual as Record<string, unknown>).friction_penalty as number) ?? null)} · utility{" "}
              {formatRupees(((detail.friction.chosen_action_actual as Record<string, unknown>).utility as number) ?? null)}
            </div>
          )}
          <div className="mt-2 text-[11px] leading-relaxed text-gray-500">This surface is recomputed at read time, not historical decision-time evidence. Persisted candidate friction and utility remain in the Decision Inspector. WAIT has low friction — doing nothing can be valuable.</div>
        </Card>
      </div>

      {/* Timeline */}
      <Card>
        <SectionTitle subtitle="Chronological journey — derived from PaymentEvent, Decision, Action, CustomerMessage, PromiseToPay, AuditEvent. No fabricated events.">Recovery timeline</SectionTitle>
        {detail.timeline.length === 0 ? (
          <div className="text-xs text-gray-500">No timeline events yet</div>
        ) : (
          <div className="relative pl-6">
            <div className="absolute left-2 top-0 bottom-0 w-px bg-white/10" />
            <div className="space-y-3">
              {detail.timeline.slice(0, 60).map((ev, i) => (
                <div key={i} className="relative">
                  <div className={`absolute left-[-22px] top-1 h-2 w-2 rounded-full ${ev.severity === "success" ? "bg-emerald-500" : ev.severity === "warning" ? "bg-amber-500" : ev.severity === "error" ? "bg-red-500" : "bg-gray-500"}`} />
                  <div className="rounded-lg border border-white/10 bg-black/20 px-3 py-2">
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <div className="text-xs font-medium text-gray-200">{ev.title}</div>
                        <div className="mt-0.5 text-xs text-gray-500">{ev.description}</div>
                      </div>
                      <span className="whitespace-nowrap text-[11px] text-gray-600">{ev.timestamp ? formatDate(ev.timestamp) : ""}</span>
                    </div>
                    <div className="mt-1 flex gap-1">
                      <Badge tone="muted" size="sm">{ev.type}</Badge>
                      <Badge tone="muted" size="sm">{ev.category}</Badge>
                    </div>
                  </div>
                </div>
              ))}
            </div>
            {detail.timeline.length > 60 && <div className="mt-2 text-xs text-gray-600">+{detail.timeline.length - 60} more events — see audit trail</div>}
          </div>
        )}
      </Card>

      {/* Temporal runtime + Provider truth + Customer intelligence in grid */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Card>
          <SectionTitle subtitle="SCHEDULED → EXECUTING → EXECUTED · bounded retries · reconciliation">Temporal runtime</SectionTitle>
          {detail.actions.length === 0 ? (
            <div className="text-xs text-gray-500">No actions yet</div>
          ) : (
            <div className="space-y-2">
              {detail.actions.map((a) => (
                <div key={a.id} className="rounded border border-white/10 bg-black/20 p-2">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs text-gray-200">{a.action_type}</span>
                    <Badge tone={a.status === "EXECUTED" ? "success" : a.status === "FAILED" ? "danger" : a.status === "SCHEDULED" ? "info" : a.status === "CANCELLED" ? "muted" : "neutral"} size="sm">{a.status}</Badge>
                  </div>
                  <div className="mt-1 grid grid-cols-2 gap-1 text-[11px] text-gray-500">
                    <span>scheduled: {a.scheduled_for ? formatDate(a.scheduled_for) : "—"}</span>
                    <span>executed: {a.executed_at ? formatDate(a.executed_at) : "—"}</span>
                    <span>attempts: {a.attempt_count}/{a.max_attempts}</span>
                    <span>queue: {a.queue_job_id ? a.queue_job_id.slice(0, 16) + "…" : "—"}</span>
                  </div>
                  {a.last_error && <div className="mt-1 text-[11px] text-amber-300">last error: {a.last_error.slice(0, 180)}</div>}
                  {a.result && <div className="mt-1 text-[11px] font-mono text-gray-600 truncate">{JSON.stringify(a.result).slice(0, 180)}</div>}
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card>
          <SectionTitle subtitle="Razorpay is provider-authoritative truth">Razorpay / Provider truth</SectionTitle>
          {!detail.provider_truth.has_payment_link ? (
            <div className="text-xs text-gray-500">No Payment Link for this case</div>
          ) : (
            <div className="space-y-2 text-xs">
              <div className="flex items-center gap-2">
                <Badge tone={detail.provider_truth.simulated ? "muted" : "success"}>{detail.provider_truth.mode_label ?? "unavailable"}</Badge>
                {detail.provider_truth.reconciled && <Badge tone="info" size="sm">reconciled</Badge>}
                <Badge tone={detail.provider_truth.status === "EXECUTED" ? "success" : "neutral"} size="sm">{detail.provider_truth.status}</Badge>
              </div>
              <div className="rounded bg-black/20 p-2 font-mono text-[11px] leading-relaxed">
                <div>link id: <span className="text-sky-300">{detail.provider_truth.payment_link_id ?? "—"}</span></div>
                <div>reference_id: <span className="text-gray-300">{String(detail.provider_truth.reference_id ?? "—").slice(0, 20)}</span></div>
                <div>short_url: {detail.provider_truth.short_url ? <a href={detail.provider_truth.short_url} target="_blank" rel="noreferrer" className="text-sky-400 underline">{detail.provider_truth.short_url}</a> : <span className="text-gray-500">unavailable</span>}</div>
                <div>amount: {detail.provider_truth.result?.amount != null ? `${String(detail.provider_truth.result.amount)} paise` : "—"} · currency {String(detail.provider_truth.result?.currency ?? "—")}</div>
              </div>
              <div className="text-[11px] text-gray-500">Provider reconciliation via <span className="font-mono">GET ?reference_id=Action.id</span> — validated before adoption. Only <span className="font-mono">payment_link.paid</span> webhook moves to RECOVERED.</div>
              {detail.state === "RECOVERED" && <div className="rounded bg-emerald-950/30 px-2 py-1 text-emerald-300">Recovered at {formatDate(detail.updated_at)} — provider truth</div>}
            </div>
          )}
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Card>
          <SectionTitle subtitle="Outbound drafts are DRAFT / NOT SENT — manual only · LLM never invents amount/link">Customer intelligence</SectionTitle>
          {detail.messages.length === 0 ? (
            <div className="text-xs text-gray-500">No customer messages yet</div>
          ) : (
            <div className="space-y-2">
              {detail.messages.map((m) => (
                <div key={m.id} className="rounded border border-white/10 bg-black/20 p-2">
                  <div className="flex items-center gap-2">
                    <Badge tone={m.direction === "outbound" ? "info" : "neutral"} size="sm">{m.direction}</Badge>
                    <Badge tone={m.status === "DRAFT" ? "warning" : m.status === "RECEIVED" ? "success" : "muted"} size="sm">{m.status ?? m.direction}</Badge>
                    <span className="text-[11px] text-gray-500">{m.generation_method ?? "—"} {m.llm_provider ? `· ${m.llm_provider}/${m.llm_model ?? ""}` : ""} · {m.prompt_version ?? ""}</span>
                  </div>
                  <div className="mt-1 rounded bg-white/5 p-2 text-sm leading-relaxed text-gray-200 whitespace-pre-wrap break-words">{m.body}</div>
                  {m.extracted && <div className="mt-1 font-mono text-[11px] text-gray-500">extracted: {JSON.stringify(m.extracted).slice(0, 220)}</div>}
                  <div className="mt-1 text-[11px] text-gray-600">{formatDate(m.created_at)} · channel {m.channel ?? "—"}</div>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card>
          <SectionTitle subtitle="PENDING → KEPT / BROKEN / SUPERSEDED — linked follow-up after merchant-local day">Promise-to-Pay</SectionTitle>
          {detail.promises_to_pay.length === 0 ? (
            <div className="text-xs text-gray-500">No promises yet</div>
          ) : (
            <div className="space-y-2">
              {detail.promises_to_pay.map((p) => (
                <div key={p.id} className="rounded border border-white/10 bg-black/20 p-2">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{formatRupees(p.promised_amount)} <span className="text-xs font-normal text-gray-500">by {p.promised_date ? formatDate(p.promised_date) : "—"}</span></span>
                    <Badge tone={p.status === "PENDING" ? "info" : p.status === "KEPT" ? "success" : p.status === "BROKEN" ? "danger" : "muted"}>{p.status}</Badge>
                  </div>
                  <div className="mt-1 text-[11px] text-gray-500">
                    {p.extraction_method ?? "—"} · {p.llm_provider ?? "deterministic"} {p.llm_model ? `· ${p.llm_model}` : ""} · prompt {p.prompt_version ?? "ptp-v1"} · amount method {p.amount_method ?? "—"} · conf {p.confidence ?? "—"} · {p.reasoning_code ?? ""}
                  </div>
                  {p.source_message_id && <div className="mt-1 font-mono text-[11px] text-gray-600">source message {p.source_message_id.slice(0, 8)}</div>}
                  {(() => {
                    const linkedAction = detail.actions.find((a) => a.promise_to_pay_id === p.id);
                    return linkedAction ? <div className="mt-1 text-[11px] text-gray-500">linked {linkedAction.action_type} {linkedAction.status} scheduled {linkedAction.scheduled_for ? formatDate(linkedAction.scheduled_for) : "—"}</div> : null;
                  })()}
                </div>
              ))}
            </div>
          )}
          <div className="mt-2 text-[11px] leading-relaxed text-gray-500">
            Customer &quot;I&apos;ll pay 8000 Friday&quot; → parsed ₹8,000 · Friday (merchant IST) → PENDING → follow-up scheduled after promised day exclusive end-of-day UTC.
          </div>
        </Card>
      </div>

      {/* Audit / Provenance */}
      <Card>
        <SectionTitle subtitle="First-class provenance for auditability">Audit / Provenance</SectionTitle>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2 text-xs">
          <div className="rounded bg-black/20 p-2">
            <div className="text-[11px] uppercase text-gray-500">Recovery policy</div>
            <div className="mt-1 font-mono text-gray-300">{latestInspector?.policy_mode ?? "baseline"} {latestInspector?.is_adaptive ? "· friction " + String(latestInspector.model_provenance.friction_profile ?? "") : ""}</div>
          </div>
          <div className="rounded bg-black/20 p-2">
            <div className="text-[11px] uppercase text-gray-500">Model</div>
            <div className="mt-1 font-mono text-gray-300">{latestInspector?.model_provenance.model_version ?? "none"} {latestInspector?.model_provenance.model_fingerprint ? `· ${latestInspector?.model_provenance.model_fingerprint.slice(0, 8)}` : ""}</div>
          </div>
          <div className="rounded bg-black/20 p-2">
            <div className="text-[11px] uppercase text-gray-500">Customer-intelligence method</div>
            <div className="mt-1 text-gray-300">{detail.messages.find((m) => m.direction === "outbound")?.generation_method ?? "deterministic"} · prompt message-v1 / ptp-v1</div>
          </div>
          <div className="rounded bg-black/20 p-2">
            <div className="text-[11px] uppercase text-gray-500">Payment truth</div>
            <div className="mt-1 text-gray-300">Razorpay webhook — payment.captured / payment_link.paid</div>
          </div>
        </div>

        <div className="mt-3">
          <button onClick={() => setShowRaw((v) => !v)} className="text-xs text-gray-500 underline hover:text-gray-300">
            {showRaw ? "Hide technical details (raw JSON)" : "Show technical details (raw audit + alternatives)"}
          </button>
          {showRaw && (
            <pre className="mt-2 max-h-[320px] overflow-auto rounded bg-black/40 p-3 text-[11px] leading-relaxed text-gray-400 whitespace-pre-wrap break-words">
              {JSON.stringify({ decisions: detail.decisions, audit_trail: detail.audit_trail.slice(0, 20), payment_events: detail.payment_events }, null, 2)}
            </pre>
          )}
        </div>
      </Card>
    </div>
  );
}
