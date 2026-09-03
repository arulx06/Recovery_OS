import type { ReactNode } from "react";

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
    neutral: "bg-white/10 text-gray-200 border-white/10",
    success: "bg-emerald-900/40 text-emerald-200 border-emerald-800/50",
    warning: "bg-amber-900/40 text-amber-200 border-amber-800/50",
    danger: "bg-red-900/40 text-red-200 border-red-800/50",
    info: "bg-sky-900/40 text-sky-200 border-sky-800/50",
    muted: "bg-gray-800 text-gray-400 border-gray-700",
  };
  const sizes = size === "sm" ? "px-1.5 py-0.5 text-[11px]" : "px-2 py-0.5 text-xs";
  return (
    <span className={`inline-flex items-center rounded-md border font-medium ${tones[tone]} ${sizes}`}>
      {children}
    </span>
  );
}

export function Card({
  children,
  className = "",
  padding = "p-4",
}: {
  children: ReactNode;
  className?: string;
  padding?: string;
}) {
  return <div className={`rounded-xl border border-white/10 bg-white/[0.04] ${padding} ${className}`}>{children}</div>;
}

export function SectionTitle({ children, subtitle }: { children: ReactNode; subtitle?: ReactNode }) {
  return (
    <div className="mb-3">
      <h3 className="text-sm font-semibold tracking-tight text-gray-100">{children}</h3>
      {subtitle && <p className="mt-1 text-xs text-gray-500">{subtitle}</p>}
    </div>
  );
}

export function EmptyState({ title, description }: { title: string; description?: string }) {
  return (
    <div className="rounded-lg border border-dashed border-white/10 bg-white/[0.02] px-6 py-10 text-center">
      <div className="text-sm font-medium text-gray-300">{title}</div>
      {description && <div className="mt-1 text-xs text-gray-500">{description}</div>}
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
  if (n == null || Number.isNaN(n)) return "—";
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
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
