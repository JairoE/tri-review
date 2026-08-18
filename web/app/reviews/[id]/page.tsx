"use client";

import { use, useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  ApiError,
  subscribeToReview,
  type Finding,
  type Review,
} from "@/lib/api";
import Markdown from "@/components/Markdown";
import StatusBadge from "@/components/StatusBadge";

function messageFor(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

/** Tint the finding count by the worst severity present, per the severity color convention. */
function findingCountColor(findings: Finding[]): string {
  if (findings.some((f) => f.severity === "critical")) return "text-red-400";
  if (findings.some((f) => f.severity === "major")) return "text-amber-400";
  if (findings.length > 0) return "text-slate-400";
  return "text-slate-500";
}

function ErrorBox({ message }: { message: string }) {
  return (
    <div className="rounded-md border border-red-800 bg-red-950/40 px-4 py-3 text-sm text-red-300">
      {message}
    </div>
  );
}

export default function ReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  // Keying on `id` forces a clean remount when the route param changes, so
  // every piece of state below starts fresh instead of needing to be reset
  // synchronously inside an effect.
  return <ReviewView key={id} id={id} />;
}

function ReviewView({ id }: { id: string }) {
  const reviewId = Number(id);
  const invalidId = !Number.isFinite(reviewId);

  const [review, setReview] = useState<Review | null>(null);
  const [loading, setLoading] = useState(!invalidId);
  const [error, setError] = useState<string | null>(
    invalidId ? `"${id}" is not a valid review id.` : null,
  );
  const [streamError, setStreamError] = useState<string | null>(null);

  useEffect(() => {
    if (invalidId) return;

    let cancelled = false;

    const refetch = () => {
      api
        .getReview(reviewId)
        .then((next) => {
          if (!cancelled) {
            setReview(next);
            setError(null);
          }
        })
        .catch((err) => {
          if (!cancelled) setError(messageFor(err));
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    };

    refetch();

    // The row is the source of truth — every stream event triggers a fresh
    // GET rather than patching state from the event payload.
    const unsubscribe = subscribeToReview(
      reviewId,
      () => refetch(),
      (message) => {
        if (!cancelled) setStreamError(message);
      },
    );

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [reviewId, invalidId]);

  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-1 flex-col gap-6 px-6 py-8">
      <Link href="/" className="w-fit text-sm text-slate-500 transition hover:text-slate-300">
        ← back to reviews
      </Link>

      {loading && (
        <div className="animate-pulse space-y-4">
          <div className="h-6 w-1/3 rounded bg-slate-800" />
          <div className="h-4 w-1/4 rounded bg-slate-800" />
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-64 rounded-lg border border-slate-800 bg-slate-900/40" />
            ))}
          </div>
        </div>
      )}

      {!loading && !review && error && <ErrorBox message={error} />}

      {review && (
        <>
          <header className="space-y-3 border-b border-slate-800 pb-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <h1 className="truncate text-xl font-semibold text-slate-100">
                  {review.pr_title || `PR #${review.pr_number}`}
                </h1>
                <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-slate-400">
                  <a
                    href={review.pr_url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="font-mono text-blue-400 hover:text-blue-300 hover:underline"
                  >
                    {review.repo}#{review.pr_number}
                  </a>
                  <span className="text-slate-700">·</span>
                  <span>{review.pr_author}</span>
                  {review.head_sha && (
                    <>
                      <span className="text-slate-700">·</span>
                      <span className="font-mono text-xs text-slate-500">{review.head_sha.slice(0, 7)}</span>
                    </>
                  )}
                </div>
              </div>
              <StatusBadge status={review.status} />
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
              {review.models.map((model) => (
                <span
                  key={model}
                  className="rounded-full border border-slate-700 bg-slate-900 px-2 py-0.5 font-mono text-xs text-slate-400"
                >
                  {model}
                </span>
              ))}
            </div>

            {review.context && (
              <p className="text-xs text-slate-500">
                {review.context.files_included.length} file
                {review.context.files_included.length === 1 ? "" : "s"} reviewed · ~
                {review.context.estimated_tokens.toLocaleString()} tokens
              </p>
            )}
          </header>

          {error && <ErrorBox message={error} />}
          {streamError && (
            <p className="text-xs text-amber-400">
              {streamError} The page will keep showing the last known state.
            </p>
          )}

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {review.columns.map((column, index) => (
              <div
                key={`${column.model}-${index}`}
                className="flex flex-col overflow-hidden rounded-lg border border-slate-800 bg-slate-900/40"
              >
                <div className="flex items-center justify-between gap-2 border-b border-slate-800 px-4 py-2.5">
                  <div className="flex min-w-0 items-center gap-2">
                    <StatusBadge status={column.status} variant="dot" />
                    <span className="truncate font-mono text-sm font-medium text-slate-200">
                      {column.model}
                    </span>
                  </div>
                  {column.status === "ok" && (
                    <span className={`shrink-0 text-xs font-medium ${findingCountColor(column.findings)}`}>
                      {column.findings.length} finding{column.findings.length === 1 ? "" : "s"}
                    </span>
                  )}
                </div>

                <div className="max-h-[70vh] overflow-y-auto px-4 py-3">
                  {column.status === "pending" && (
                    <div className="animate-pulse space-y-2">
                      <div className="h-3 w-3/4 rounded bg-slate-800" />
                      <div className="h-3 w-full rounded bg-slate-800" />
                      <div className="h-3 w-5/6 rounded bg-slate-800" />
                      <p className="pt-2 text-sm text-slate-500">waiting for {column.model}…</p>
                    </div>
                  )}

                  {column.status !== "pending" &&
                    (column.markdown ? (
                      <Markdown>{column.markdown}</Markdown>
                    ) : column.status === "failed" && column.error ? (
                      <p className="text-sm text-red-400">{column.error}</p>
                    ) : (
                      <p className="text-sm text-slate-500">No output.</p>
                    ))}
                </div>
              </div>
            ))}
          </div>

          <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-5">
            <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
              Consensus
            </h2>

            {review.report_md ? (
              <Markdown>{review.report_md}</Markdown>
            ) : review.status === "failed" ? (
              <div className="space-y-2 rounded-md border border-red-900 bg-red-950/30 p-4">
                <p className="text-sm text-red-300">
                  {review.error || "The consensus report could not be generated."}
                </p>
                <p className="text-xs text-red-400/80">
                  Fewer than two models reported successfully, so there was nothing to triangulate
                  into a consensus. The individual model reviews above are still valid — read them
                  side by side.
                </p>
              </div>
            ) : (
              <p className="text-sm text-slate-500">
                {review.status === "queued" || review.status === "running"
                  ? "Waiting for all models to finish before synthesizing a consensus…"
                  : "No consensus report was generated."}
              </p>
            )}
          </section>
        </>
      )}
    </div>
  );
}
