import type { ComponentPropsWithoutRef, ReactNode } from "react";

export function Badge({
  children,
  tone = "neutral",
  size = "md",
}: {
  children: ReactNode;
  tone?: "neutral" | "success" | "warning" | "danger" | "info" | "muted";
  size?: "sm" | "md";
}) {
  const tones: Record<string, string> = {
    neutral: "bg-slate-700/50 text-slate-200 border-slate-600/40",
    success: "bg-emerald-500/10 text-emerald-300 border-emerald-500/20",
    warning: "bg-amber-500/10 text-amber-200 border-amber-500/20",
    danger: "bg-rose-500/10 text-rose-300 border-rose-500/20",
    info: "bg-blue-500/10 text-blue-200 border-blue-500/20",
    muted: "bg-slate-800/70 text-slate-400 border-slate-700/60",
  };
  const sizes = size === "sm" ? "px-2 py-0.5 text-xs" : "px-2.5 py-1 text-xs";
  return (
    <span className={`inline-flex items-center rounded-md border font-semibold ${tones[tone]} ${sizes}`}>
      {children}
    </span>
  );
}

export function Card({
  children,
  className = "",
  padding = "p-5",
  ...props
}: {
  children: ReactNode;
  className?: string;
  padding?: string;
} & Omit<ComponentPropsWithoutRef<"section">, "children">) {
  return <section {...props} className={`rounded-xl border border-[#242d3b] bg-[#11161f] shadow-[0_18px_50px_rgba(0,0,0,0.16)] ${padding} ${className}`}>{children}</section>;
}

export function SectionTitle({ children, subtitle }: { children: ReactNode; subtitle?: ReactNode }) {
  return (
    <div className="mb-4">
      <h3 className="text-lg font-semibold tracking-tight text-slate-100">{children}</h3>
      {subtitle && <p className="mt-1 text-base leading-relaxed text-slate-400">{subtitle}</p>}
    </div>
  );
}

export function EmptyState({ title, description }: { title: string; description?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-white/10 bg-white/[0.02] px-6 py-10 text-center">
      <div className="text-sm font-medium text-gray-300">{title}</div>
      {description && <div className="mt-1 text-sm text-gray-500">{description}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-gray-400">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-white/20 border-t-white/60" aria-hidden />
      {label}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="rounded-lg border border-red-900/40 bg-red-950/30 px-4 py-3 text-sm text-red-300 flex items-center justify-between gap-3">
      <span>{message}</span>
      {onRetry && (
        <button
          onClick={onRetry}
          className="rounded bg-white/10 px-2.5 py-1 text-xs font-medium text-gray-100 hover:bg-white/15"
        >
          Retry
        </button>
      )}
    </div>
  );
}

export function formatRupees(n: number | null | undefined): string {
  if (n == null) return "—";
  const value = Number(n);
  if (!Number.isFinite(value)) return "—";
  return `₹${value.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export function formatAmount(amount: number | null | undefined, currency = "INR"): string {
  if (amount == null) return "—";
  const c = currency ?? "INR";
  return `${formatRupees(amount)} ${c}`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString("en-GB", {
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: "Asia/Kolkata",
    }) + " IST";
  } catch {
    return iso;
  }
}

const ACTION_LABELS: Record<string, string> = {
  WAIT: "Wait",
  WAIT_FOR_NATIVE_RETRY: "Wait for Razorpay retry",
  CREATE_PAYMENT_LINK: "Payment link",
  CONTACT_CUSTOMER: "Contact customer",
  COLLECT_PROMISE_TO_PAY: "Collect Promise to Pay",
  FOLLOW_UP_PTP: "Promise follow-up",
  ESCALATE: "Escalate",
  STOP: "Stop recovery",
};

const STATE_LABELS: Record<string, string> = {
  WAITING: "Waiting",
  ACTION_SCHEDULED: "Action scheduled",
  AWAITING_OUTCOME: "Awaiting outcome",
  HUMAN_REVIEW: "Human review",
  RECOVERED: "Recovered",
  STOPPED: "Stopped",
  DISPUTED: "Disputed",
};

const FAILURE_LABELS: Record<string, string> = {
  TRANSIENT_INFRASTRUCTURE: "Temporary bank issue",
  SUBSCRIPTION_PENDING_NATIVE_RETRY: "Razorpay retry pending",
  INSUFFICIENT_BALANCE: "Insufficient balance",
  CUSTOMER_AUTHENTICATION: "Authentication failed",
  INVALID_INSTRUMENT: "Invalid payment instrument",
  MANDATE_ISSUE: "Mandate issue",
  PERMANENT_HARD_FAILURE: "Permanent payment failure",
  UNCLASSIFIED: "Needs classification",
  UNKNOWN: "Unknown failure",
};

export function actionLabel(value: string | null | undefined): string {
  return value ? ACTION_LABELS[value] ?? humanize(value) : "No action selected";
}

export function stateLabel(value: string | null | undefined): string {
  return value ? STATE_LABELS[value] ?? humanize(value) : "Unknown state";
}

export function failureLabel(value: string | null | undefined): string {
  return value ? FAILURE_LABELS[value] ?? humanize(value) : "Not yet diagnosed";
}

export function humanize(value: string): string {
  const text = value.replaceAll("_", " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function stateTone(state: string): "neutral" | "success" | "warning" | "danger" | "info" | "muted" {
  if (state === "RECOVERED") return "success";
  if (state === "HUMAN_REVIEW") return "warning";
  if (state === "DISPUTED" || state === "STOPPED") return "danger";
  if (state === "WAITING" || state === "AWAITING_OUTCOME" || state === "ACTION_SCHEDULED") return "info";
  return "neutral";
}
