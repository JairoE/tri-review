export type StatusValue =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "pending"
  | "ok";

interface StatusStyle {
  dot: string;
  badge: string;
}

const STYLES: Record<StatusValue, StatusStyle> = {
  queued: { dot: "bg-slate-400", badge: "bg-slate-800/70 text-slate-300 ring-slate-600/60" },
  running: { dot: "bg-blue-400 animate-pulse", badge: "bg-blue-950/70 text-blue-300 ring-blue-700/60" },
  succeeded: { dot: "bg-emerald-400", badge: "bg-emerald-950/70 text-emerald-300 ring-emerald-700/60" },
  failed: { dot: "bg-red-400", badge: "bg-red-950/70 text-red-300 ring-red-700/60" },
  pending: { dot: "bg-slate-500 animate-pulse", badge: "bg-slate-800/70 text-slate-400 ring-slate-600/60" },
  ok: { dot: "bg-emerald-400", badge: "bg-emerald-950/70 text-emerald-300 ring-emerald-700/60" },
};

interface StatusBadgeProps {
  status: StatusValue;
  /** "badge" renders a labelled pill; "dot" renders just the color indicator. */
  variant?: "badge" | "dot";
  className?: string;
}

export default function StatusBadge({ status, variant = "badge", className = "" }: StatusBadgeProps) {
  const style = STYLES[status];

  if (variant === "dot") {
    return (
      <span
        role="img"
        aria-label={`status: ${status}`}
        title={status}
        className={`inline-block h-2 w-2 shrink-0 rounded-full ${style.dot} ${className}`}
      />
    );
  }

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium capitalize ring-1 ring-inset ${style.badge} ${className}`}
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
      {status}
    </span>
  );
}
