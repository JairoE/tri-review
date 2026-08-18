/**
 * The backend contract.
 *
 * The browser talks to FastAPI directly rather than through a Next rewrite:
 * a dev proxy is the classic place for an SSE stream to get buffered, and the
 * backend already allows this origin by CORS. One less thing between the
 * worker thread and the screen.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export type Severity = "critical" | "major" | "minor";
export type Category = "bug" | "security" | "performance" | "logic";

export interface Finding {
  file: string;
  line: number | null;
  severity: Severity;
  category: Category;
  title: string;
  detail: string;
  suggested_fix: string | null;
}

/** One model's column in the side-by-side view. */
export interface Column {
  model: string;
  status: "pending" | "ok" | "failed";
  findings: Finding[];
  error: string | null;
  /** The model's review, already rendered as Markdown by the backend. */
  markdown: string;
}

export interface ContextSummary {
  files_included: string[];
  dropped: string[];
  missing: string[];
  rejected: string[];
  estimated_tokens: number;
  diff_lines: number;
  diff_overflow: number;
}

export interface Review {
  id: number;
  repo: string;
  pr_number: string;
  pr_title: string;
  pr_author: string;
  pr_url: string;
  head_sha: string;
  models: string[];
  status: "queued" | "running" | "succeeded" | "failed";
  created_at: string | null;
  finished_at: string | null;
  columns: Column[];
  context: ContextSummary | null;
  report_md: string;
  error: string;
}

export interface PreviewResponse {
  repo: string;
  pr_number: string;
  context: ContextSummary;
  models: string[];
  token_budget: number;
}

export interface PullRequest {
  repo: string;
  number: string;
  title: string;
  author: string;
  updated_at: string;
  head_sha: string;
  state: string;
  url: string;
}

export interface Health {
  status: string;
  models: string[];
  provider_keys: { openai: boolean; anthropic: boolean; google: boolean };
  keys_present: number;
  ready: boolean;
}

/** The shape the backend sends for a domain error. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
    });
  } catch {
    // A dead backend is the most common failure in a local-first app, and the
    // browser's own message for it ("Failed to fetch") explains nothing.
    throw new ApiError(
      `Cannot reach the tri-review API at ${API_BASE}. Is the backend running?`,
      0,
    );
  }

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiError(
      body?.detail ?? body?.message ?? `Request failed (${response.status})`,
      response.status,
    );
  }
  return body as T;
}

export const api = {
  health: () => request<Health>("/api/health"),

  resolve: (url: string) =>
    request<PullRequest>("/api/pulls/resolve", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  preview: (url: string) =>
    request<PreviewResponse>("/api/reviews/preview", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  startReview: (url: string) =>
    request<{ id: number; status: string }>("/api/reviews", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  getReview: (id: number) => request<Review>(`/api/reviews/${id}`),

  listReviews: () => request<Review[]>("/api/reviews"),
};

export type StreamEvent =
  | { type: "snapshot"; review: Review }
  | { type: "context"; context: ContextSummary }
  | { type: "model_result"; model: string }
  | { type: "report"; report_md: string }
  | { type: "done"; status: string; error: string };

/**
 * Subscribe to a review's progress.
 *
 * The stream opens with a full snapshot replayed from the database, so a page
 * refreshed mid-review — or opened on a finished one — renders the same way as
 * one that watched from the start. Callers re-fetch on each event rather than
 * patching state from the payload: the row is the source of truth, and a
 * refetch is cheap on localhost.
 */
export function subscribeToReview(
  id: number,
  onEvent: (event: StreamEvent) => void,
  onError?: (message: string) => void,
): () => void {
  const source = new EventSource(`${API_BASE}/api/reviews/${id}/events`);

  const handle = (name: string) => (event: MessageEvent) => {
    try {
      const data = JSON.parse(event.data);
      if (name === "snapshot") onEvent({ type: "snapshot", review: data });
      else if (name === "context") onEvent({ type: "context", context: data.context });
      else if (name === "model_result")
        onEvent({ type: "model_result", model: data.result.model });
      else if (name === "report") onEvent({ type: "report", report_md: data.report_md });
      else if (name === "done")
        onEvent({ type: "done", status: data.status, error: data.error });
    } catch {
      /* a malformed frame is not worth tearing the page down for */
    }
  };

  for (const name of ["snapshot", "context", "model_result", "report", "done"]) {
    source.addEventListener(name, handle(name));
  }

  source.addEventListener("done", () => source.close());

  source.onerror = () => {
    // EventSource retries on its own; only a closed stream is terminal.
    if (source.readyState === EventSource.CLOSED) {
      onError?.("Lost the connection to the review stream.");
    }
  };

  return () => source.close();
}
