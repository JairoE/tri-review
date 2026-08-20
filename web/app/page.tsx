"use client";

import { useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, ApiError, type Health, type PreviewResponse, type Review } from "@/lib/api";
import StatusBadge from "@/components/StatusBadge";

function messageFor(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const diffSec = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (diffSec < 60) return "just now";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.round(diffHour / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  return date.toLocaleDateString();
}

function ErrorBox({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
      {message}
    </div>
  );
}

export default function Home() {
  const router = useRouter();

  const [url, setUrl] = useState("");
  const [previewedUrl, setPreviewedUrl] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const [health, setHealth] = useState<Health | null>(null);
  const [healthDismissed, setHealthDismissed] = useState(false);
  const [healthError, setHealthError] = useState<string | null>(null);

  const [history, setHistory] = useState<Review[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    api
      .health()
      .then((result) => {
        if (!cancelled) setHealth(result);
      })
      .catch((error) => {
        if (!cancelled) setHealthError(messageFor(error));
      });

    api
      .listReviews()
      .then((reviews) => {
        if (!cancelled) {
          setHistory(reviews);
          setHistoryError(null);
        }
      })
      .catch((error) => {
        if (!cancelled) setHistoryError(messageFor(error));
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const handlePreview = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = url.trim();
    if (!trimmed || previewLoading) return;

    setPreviewLoading(true);
    setPreviewError(null);
    setPreview(null);
    try {
      const result = await api.preview(trimmed);
      setPreview(result);
      setPreviewedUrl(trimmed);
    } catch (error) {
      setPreviewError(messageFor(error));
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleCancel = () => {
    setPreview(null);
    setPreviewedUrl(null);
  };

  const handleRunReview = async () => {
    if (!previewedUrl || starting) return;
    setStarting(true);
    setPreviewError(null);
    try {
      const { id } = await api.startReview(previewedUrl);
      router.push(`/reviews/${id}`);
    } catch (error) {
      setPreviewError(messageFor(error));
      setStarting(false);
    }
  };

  const missingKeys = health
    ? (Object.entries(health.provider_keys) as [string, boolean][])
        .filter(([, present]) => !present)
        .map(([name]) => name)
    : [];

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-8 px-6 py-12">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-semibold tracking-tight text-slate-100">tri-review</h1>
        <p className="text-sm text-slate-400">
          Paste a public GitHub PR URL. Three LLMs review it independently, and you read all three
          side by side.
        </p>
      </header>

      {healthError && <ErrorBox message={healthError} />}

      {health && !health.ready && !healthDismissed && (
        <div className="relative rounded-md border border-amber-700/70 bg-amber-950/40 px-4 py-3 pr-9 text-sm text-amber-200">
          <button
            type="button"
            onClick={() => setHealthDismissed(true)}
            aria-label="Dismiss warning"
            className="absolute right-2.5 top-2.5 text-amber-400/80 transition hover:text-amber-200"
          >
            ✕
          </button>
          <p className="font-medium">Missing provider API keys</p>
          <p className="mt-1 text-amber-300/90">
            {missingKeys.length > 0 ? missingKeys.join(", ") : "Not enough providers"} not configured.
            At least two provider keys are needed — without them, every review will fail at
            synthesis.
          </p>
        </div>
      )}

      <form onSubmit={handlePreview} className="flex flex-col gap-3 sm:flex-row">
        <input
          type="text"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://github.com/owner/repo/pull/123"
          autoFocus
          className="flex-1 rounded-md border border-slate-700 bg-slate-900 px-4 py-3 font-mono text-sm text-slate-100 placeholder:text-slate-600 outline-none focus:border-slate-500 focus:ring-1 focus:ring-slate-500"
        />
        <button
          type="submit"
          disabled={previewLoading || !url.trim()}
          className="shrink-0 rounded-md bg-slate-100 px-5 py-3 text-sm font-medium text-slate-900 transition hover:bg-white disabled:cursor-not-allowed disabled:opacity-50"
        >
          {previewLoading ? "Checking…" : "Review"}
        </button>
      </form>

      {previewLoading && (
        <div className="animate-pulse space-y-3 rounded-lg border border-slate-800 bg-slate-900/40 p-5">
          <div className="h-4 w-1/3 rounded bg-slate-800" />
          <div className="h-3 w-2/3 rounded bg-slate-800" />
          <div className="h-3 w-1/2 rounded bg-slate-800" />
        </div>
      )}

      {previewError && <ErrorBox message={previewError} />}

      {preview && (
        <div className="space-y-4 rounded-lg border border-slate-700 bg-slate-900/60 p-5">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-slate-500">Review this PR?</p>
            <p className="mt-1 font-mono text-sm text-slate-100">
              {preview.repo}#{preview.pr_number}
            </p>
          </div>

          <div className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-3">
            <div>
              <p className="text-xs text-slate-500">Estimated tokens</p>
              <p className="mt-0.5 text-slate-200">{preview.context.estimated_tokens.toLocaleString()}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">Token budget</p>
              <p className="mt-0.5 text-slate-200">{preview.token_budget.toLocaleString()}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">Files included</p>
              <p className="mt-0.5 text-slate-200">{preview.context.files_included.length}</p>
            </div>
          </div>

          <div>
            <p className="text-xs text-slate-500">Models</p>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {preview.models.map((model) => (
                <span
                  key={model}
                  className="rounded-full border border-slate-700 bg-slate-800/60 px-2 py-0.5 font-mono text-xs text-slate-300"
                >
                  {model}
                </span>
              ))}
            </div>
          </div>

          {preview.context.files_included.length > 0 && (
            <details className="text-sm">
              <summary className="cursor-pointer text-slate-400 hover:text-slate-200">
                Files included ({preview.context.files_included.length})
              </summary>
              <ul className="mt-2 max-h-40 space-y-0.5 overflow-y-auto rounded border border-slate-800 bg-slate-950/60 p-2 font-mono text-xs text-slate-400">
                {preview.context.files_included.map((path) => (
                  <li key={path} className="truncate">
                    {path}
                  </li>
                ))}
              </ul>
            </details>
          )}

          {preview.context.dropped.length > 0 && (
            <div className="rounded-md border border-amber-800/60 bg-amber-950/20 p-3">
              <p className="text-xs font-semibold text-amber-400">
                Dropped — over token budget ({preview.context.dropped.length})
              </p>
              <ul className="mt-1 max-h-28 space-y-0.5 overflow-y-auto font-mono text-xs text-amber-300/90">
                {preview.context.dropped.map((path) => (
                  <li key={path} className="truncate">
                    {path}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {preview.context.missing.length > 0 && (
            <div className="rounded-md border border-slate-700 bg-slate-800/30 p-3">
              <p className="text-xs font-semibold text-slate-400">
                Missing ({preview.context.missing.length})
              </p>
              <ul className="mt-1 max-h-28 space-y-0.5 overflow-y-auto font-mono text-xs text-slate-400">
                {preview.context.missing.map((path) => (
                  <li key={path} className="truncate">
                    {path}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {preview.context.rejected.length > 0 && (
            <div className="rounded-md border border-red-700 bg-red-950/50 p-3">
              <p className="text-xs font-semibold text-red-400">
                Refused: outside the repository ({preview.context.rejected.length})
              </p>
              <ul className="mt-1 max-h-28 space-y-0.5 overflow-y-auto font-mono text-xs text-red-300">
                {preview.context.rejected.map((path) => (
                  <li key={path} className="truncate">
                    {path}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="flex gap-3 pt-1">
            <button
              type="button"
              onClick={handleRunReview}
              disabled={starting}
              className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {starting ? "Starting…" : "Run review"}
            </button>
            <button
              type="button"
              onClick={handleCancel}
              disabled={starting}
              className="rounded-md border border-slate-700 px-4 py-2 text-sm font-medium text-slate-300 transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      <section className="space-y-3 border-t border-slate-800 pt-6">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">History</h2>

        {historyLoading && <p className="text-sm text-slate-500">Loading…</p>}

        {!historyLoading && historyError && <ErrorBox message={historyError} />}

        {!historyLoading && !historyError && history.length === 0 && (
          <p className="text-sm text-slate-500">No reviews yet. Paste a PR URL above to get started.</p>
        )}

        {!historyLoading && !historyError && history.length > 0 && (
          <ul className="divide-y divide-slate-800 overflow-hidden rounded-lg border border-slate-800">
            {history.map((review) => (
              <li key={review.id}>
                <Link
                  href={`/reviews/${review.id}`}
                  className="flex items-center justify-between gap-4 px-4 py-3 transition hover:bg-slate-900/60"
                >
                  <div className="min-w-0">
                    <p className="truncate font-mono text-sm text-slate-200">
                      {review.repo}#{review.pr_number}
                    </p>
                    <p className="truncate text-xs text-slate-500">{review.pr_title || "Untitled PR"}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    <span className="text-xs text-slate-500">{relativeTime(review.created_at)}</span>
                    <StatusBadge status={review.status} />
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
