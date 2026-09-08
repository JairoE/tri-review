"""tri-review command line entry point."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from . import config, context, gating, github, history, triage
from .errors import NothingToReview, PRNotFoundError, TriReviewError

console = Console()


@click.command()
@click.option("--pr", default=None, help="Pull request number. Omit to detect the current branch's PR.")
@click.option(
    "--repo",
    default=None,
    metavar="OWNER/NAME",
    help=(
        "Review a PR in this repository instead of the current directory's. "
        "File contents are read from GitHub at the PR's head commit, so no "
        "checkout is needed."
    ),
)
@click.option(
    "--url",
    default=None,
    metavar="PR_URL",
    help=(
        "A GitHub pull request URL. Sets --repo and --pr from it. Passing one "
        "that contradicts an explicit --repo or --pr is an error rather than a "
        "silent override."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be sent to the models, then exit without calling them.",
)
@click.option(
    "--model",
    "models",
    multiple=True,
    metavar="MODEL_ID",
    help=(
        "Model to review with. Repeat to pick the panel, e.g. "
        "--model gpt-5.6-terra --model claude-sonnet-5 --model gemini-3.7-flash. "
        "Defaults to the three configured slots. At least two are required."
    ),
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Also write the raw Markdown report to this file.",
)
@click.option(
    "--exclude",
    "excludes",
    multiple=True,
    metavar="PATTERN",
    help=(
        "Glob pattern to drop from the diff before sending it anywhere, e.g. "
        "--exclude '**/*.lock'. Repeat for more patterns. Useful when a PR's diff "
        "is too large for a model's context or rate limit -- excluding lockfiles, "
        "generated clients, or fixtures can bring it under the line."
    ),
)
@click.option(
    "--no-default-excludes",
    is_flag=True,
    help=(
        "Review documentation, lockfiles and generated blobs too, instead of "
        "skipping them. Use this when the prose itself is what you want a "
        "second opinion on."
    ),
)
@click.option(
    "--fresh",
    is_flag=True,
    help=(
        "Ignore any stored review of this PR and buy a new one, even if nothing "
        "has changed since the last run."
    ),
)
@click.option(
    "--triage/--no-triage",
    "use_triage",
    default=None,
    help=(
        "Before reviewing, ask the cheapest model whether the diff changes "
        "behaviour at all, and skip the review if it plainly does not (a "
        "comment-only or formatting-only change). Off unless TRI_REVIEW_TRIAGE "
        "is set: it is the one gate that costs money and can be wrong."
    ),
)
@click.version_option(package_name="tri-review")
def main(
    pr: str | None,
    repo: str | None,
    url: str | None,
    dry_run: bool,
    models: tuple[str, ...],
    output: Path | None,
    excludes: tuple[str, ...],
    no_default_excludes: bool,
    fresh: bool,
    use_triage: bool | None,
) -> None:
    """Review a GitHub pull request with three LLMs and report their consensus."""
    load_dotenv()
    try:
        if url:
            repo, pr = _merge_url(url, repo, pr)
        _run(
            pr, repo, dry_run, models, output, excludes,
            no_default_excludes, fresh, use_triage,
        )
    except NothingToReview as exc:
        # Not a failure: nothing was found worth spending on, and nothing was
        # spent. Exiting non-zero here would fail a CI check on a docs-only PR.
        console.print(f"[bold green]Nothing to review.[/bold green] {exc}")
        # Stable and unstyled, for the Action (and anything else parsing output)
        # to tell a free path skip from a triage skip that did pay for a call.
        console.print(f"tri-review-status: skipped:{exc.gate}", highlight=False)
        sys.exit(0)
    except TriReviewError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)


def _merge_url(url: str, repo: str | None, pr: str | None) -> tuple[str, str]:
    """Resolve --url, refusing to silently overrule a conflicting --repo/--pr.

    Taking the URL's word for it would discard the flag the user typed and
    review a different pull request than they asked for, without saying so.
    Everywhere else this CLI rejects contradictory input before the PR is
    fetched and money is spent; this is the one place that did not.
    """
    url_repo, url_pr = github.parse_pr_url(url)

    if repo and repo != url_repo:
        raise click.BadParameter(
            f"--url points at {url_repo} but --repo says {repo}. "
            "Pass one or the other, or make them agree.",
            param_hint="--url",
        )
    if pr and str(pr) != url_pr:
        raise click.BadParameter(
            f"--url points at PR #{url_pr} but --pr says #{pr}. "
            "Pass one or the other, or make them agree.",
            param_hint="--url",
        )
    return url_repo, url_pr


def _resolve_models(selected: tuple[str, ...]) -> list[str]:
    """Pick the review panel: --model flags if given, else the configured slots.

    Validated here rather than per-node so a typo fails before the PR is
    fetched, instead of surfacing later as an unexplained missing review.
    """
    from . import config
    from .providers import PROVIDER_PREFIXES, provider_of

    if not selected:
        return [config.model_a(), config.model_b(), config.model_c()]

    # The same model twice cannot corroborate itself -- it would just pay for one
    # opinion and report it as consensus. Collapse before counting.
    models = list(dict.fromkeys(selected))

    if len(models) < 2:
        raise click.BadParameter(
            f"need at least 2 distinct models to triangulate, got {len(models)}. "
            "Pass --model twice or more with different IDs, or omit it to use the "
            "configured three.",
            param_hint="--model",
        )

    unknown = [name for name in models if provider_of(name) is None]
    if unknown:
        supported = ", ".join(
            f"{provider} ({', '.join(p + '*' for p in prefixes)})"
            for provider, prefixes in PROVIDER_PREFIXES.items()
        )
        raise click.BadParameter(
            f"unrecognized model ID(s): {', '.join(unknown)}. Supported: {supported}.",
            param_hint="--model",
        )

    return models


def _resolve_excludes(
    explicit: tuple[str, ...], skip_defaults: bool
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (payload patterns, patterns that justify skipping the run).

    --exclude always adds rather than replaces: someone narrowing a huge diff
    with `--exclude '**/fixtures/**'` is not also asking to start reviewing
    lockfiles. Replacing the whole set is what TRI_REVIEW_EXCLUDE is for.

    The two sets differ only in the built-ins. Whatever the user names is taken
    at face value in both roles -- they are stating what they do not want
    reviewed, and second-guessing that would be worse than honouring it.
    """
    if skip_defaults:
        patterns = tuple(dict.fromkeys(explicit))
        return patterns, patterns
    payload = tuple(dict.fromkeys((*config.default_excludes(), *explicit)))
    skippable = tuple(dict.fromkeys((*config.skip_eligible_excludes(), *explicit)))
    return payload, skippable


def _gate_paths(
    pr_number: str,
    repo: str | None,
    patterns: tuple[str, ...],
    skippable: tuple[str, ...],
) -> tuple[list[str], tuple[str, ...]]:
    """Stop before spending anything if nothing the PR touches is worth reviewing.

    The cheapest gate there is: one metadata call, no diff body, no file reads,
    no model calls. Only path globs decide here -- a pattern cannot be wrong
    about whether `README.md` is Markdown, which is what makes skipping on it
    safe to do automatically.
    """
    paths, complete = github.fetch_changed_files(pr_number, repo)
    reviewable, excluded = gating.partition(paths, patterns)

    if reviewable or not paths:
        # An empty file list is not a skip decision -- let the diff fetch report
        # what is actually going on with the PR.
        return excluded, patterns
    if not complete:
        # The file list is not the whole PR, so "everything is excluded" may only
        # be true of the part we were shown. Reviewing a PR that did not need it
        # costs money; not reviewing one that did costs the user the entire point
        # of the tool.
        console.print(
            "[yellow]Note: this PR's file list came back incomplete, so the skip "
            "check was inconclusive. Reviewing.[/yellow]"
        )
        return excluded, patterns

    # Nothing is left to review -- but being dropped from the payload is not the
    # same as being grounds for skipping the PR. A lockfile-only change is the
    # case this separates out: not worth tokens alongside real code, and exactly
    # the shape of a dependency bump that must not pass unreviewed on its own.
    remaining, still_excluded = gating.partition(paths, skippable)
    if remaining:
        console.print(
            f"[yellow]Every changed file is excluded from the payload, but "
            f"{len(remaining)} of them are not grounds for skipping a review "
            f"(dependency or generated files that still record a decision). "
            f"Reviewing {escape(', '.join(remaining[:5]))}"
            + (f" and {len(remaining) - 5} more" if len(remaining) > 5 else "")
            + ".[/yellow]"
        )
        return still_excluded, skippable

    listing = "\n".join(f"  - {escape(path)}" for path in excluded[:20])
    if len(excluded) > 20:
        listing += f"\n  ... and {len(excluded) - 20} more"
    raise NothingToReview(
        f"All {len(excluded)} file(s) changed by PR #{pr_number} match the "
        f"exclude patterns, so there is no code to triangulate:\n{listing}\n"
        "Re-run with --no-default-excludes to review them anyway."
    )


def _triage_gate(
    pr_number: str, repo: str | None, patterns: tuple[str, ...], use_triage: bool | None
) -> str:
    """Optionally ask a cheap model whether the diff is worth reviewing.

    Returns the diff when it fetched one, so the graph does not ask GitHub for
    the same bytes a second time. Returns "" when triage is off.

    Unlike the path gate, this one can be wrong, which is why it is opt-in and
    why every outcome except a confident "no behaviour change" leads to a review.
    """
    enabled = config.triage_enabled() if use_triage is None else use_triage
    if not enabled:
        return ""

    diff = github.fetch_diff(pr_number, repo, patterns)
    verdict = triage.assess(diff)

    if verdict is None:
        console.print("[dim]Triage could not reach a verdict. Reviewing.[/dim]")
        return diff
    if verdict.changes_behavior:
        return diff

    raise NothingToReview(
        f"Triage ({config.triage_model()}) found no behaviour change in PR "
        f"#{pr_number}: {verdict.reason}\n"
        "This is a model's judgement, not a path rule, so it can be wrong. "
        "Re-run with --no-triage to review anyway.",
        gate="triage",
    )


def _repo_identity(repo: str | None, meta: dict) -> str | None:
    """The "owner/name" this PR belongs to, for keying its stored history.

    In --repo mode the caller already said. In cwd mode it is read back off the
    PR's own URL rather than by asking gh a second question. Returns None if it
    cannot be determined, which costs the run its history and nothing else.
    """
    if repo:
        return repo
    try:
        return github.parse_pr_url(meta.get("url") or "")[0]
    except PRNotFoundError:
        # The only thing parse_pr_url raises. Anything else is a bug worth
        # seeing rather than a run that quietly stops caching.
        console.print("[dim]Could not identify this PR's repository; not storing history.[/dim]")
        return None


def _run(
    pr: str | None,
    repo: str | None,
    dry_run: bool,
    selected: tuple[str, ...],
    output: Path | None,
    excludes: tuple[str, ...],
    no_default_excludes: bool = False,
    fresh: bool = False,
    use_triage: bool | None = None,
) -> None:
    models = _resolve_models(selected)
    patterns, skippable = _resolve_excludes(excludes, no_default_excludes)

    github.preflight(repo)
    pr_number = pr or github.detect_pr(repo)

    skipped, patterns = _gate_paths(pr_number, repo, patterns, skippable)
    if skipped:
        console.print(
            f"[dim]Skipping {len(skipped)} excluded file(s): "
            f"{escape(', '.join(skipped[:5]))}"
            + (f", +{len(skipped) - 5} more" if len(skipped) > 5 else "")
            + "[/dim]"
        )

    if dry_run:
        ctx = context.preview_context(pr_number, repo, patterns)
        _print_context(pr_number, ctx)
        console.print(f"\n[dim]would review with: {', '.join(models)}[/dim]")
        console.print("[dim]--dry-run: stopping before any model call.[/dim]")
        return

    meta = github.fetch_pr_meta(pr_number, repo)
    head_sha = meta["head_sha"]
    identity = _repo_identity(repo, meta)

    # In cwd mode the file bodies come from the working tree while the stored
    # review is keyed on the PR's head SHA. If those two disagree, the review
    # this run produces is not a review of that commit -- so it must neither be
    # replayed from nor written to history. In --repo mode the contents are read
    # at the SHA itself, so there is nothing to disagree with.
    tree_reason = github.working_tree_reason(head_sha) if repo is None else None

    if identity and not fresh and _replay(
        identity, pr_number, head_sha, models, patterns, output, tree_reason
    ):
        return

    diff = _triage_gate(pr_number, repo, patterns, use_triage)

    console.print(
        Panel(
            f"[bold cyan]tri-review[/bold cyan]  {repo + ' ' if repo else ''}PR #{pr_number}\n"
            f"[dim]{'  ·  '.join(models)}[/dim]",
            expand=False,
        )
    )

    # Imported here, and below every path that returns early, because pulling in
    # langgraph drags langchain_core's runnables in with it -- tens of seconds
    # before a single line is printed. A run that decides to spend nothing must
    # not pay for the machinery it decided not to use, which is the whole point
    # of the gates above. (The import used to sit at the top of this function,
    # where its "keeps --dry-run fast" comment was not actually true.)
    from .graph import build_review_graph

    app = build_review_graph(models=models, use_cache=not fresh)
    report, results = _stream_graph(
        app, pr_number, repo, patterns, len(models), head_sha, diff
    )

    console.print()
    console.print(Markdown(report))

    _write_output(report, output)

    if identity and tree_reason:
        console.print(
            f"[dim]Not storing this review: {tree_reason}, so it is not a review "
            f"of {head_sha[:8]} and must not be replayed as one.[/dim]"
        )
    elif identity:
        saved = history.save(
            history.RunRecord(
                repo=identity,
                pr=str(pr_number),
                head_sha=head_sha,
                models=list(models),
                excludes=list(patterns),
                report=report,
                results=results,
            )
        )
        if saved is None:
            # Said out loud, because the visible consequence is the next run
            # paying for this same review again with no explanation.
            console.print(
                "[yellow]Could not store this review, so re-running will not "
                "replay it. Set TRI_REVIEW_HISTORY_DIR to a writable path.[/yellow]"
            )


def _replay(
    identity: str,
    pr_number: str,
    head_sha: str,
    models: list[str],
    patterns: tuple[str, ...],
    output: Path | None,
    tree_reason: str | None = None,
) -> bool:
    """Print the stored review if it still answers the question. True if it did."""
    record = history.load(identity, str(pr_number))
    if record is None:
        return False

    reason = history.stale_reason(record, head_sha, models, patterns)
    if reason is not None:
        console.print(f"[dim]Stored review is out of date ({reason}). Reviewing.[/dim]")
        return False

    if tree_reason is not None:
        # Checked here rather than before loading, so the message only appears
        # when there was actually something to replay.
        console.print(
            f"[dim]Not replaying the stored review: {tree_reason}, so the files "
            "on disk are not the ones it was written about.[/dim]"
        )
        return False

    console.print(
        f"[bold green]No change since the last review[/bold green] of "
        f"{head_sha[:8]} on {record.created_at}. Replaying it — pass --fresh to "
        "buy a new one.\n"
    )
    console.print(Markdown(record.report))
    _write_output(record.report, output)
    return True


def _write_output(report: str, output: Path | None) -> None:
    if not output:
        return
    try:
        output.write_text(report, encoding="utf-8")
    except OSError as exc:
        # The report is already on screen and the models are already paid
        # for, so a bad path is a warning, not a failed run.
        console.print(f"\n[yellow]Could not write {output}: {exc}[/yellow]")
    else:
        console.print(f"\n[green]Wrote report to[/green] {output}")


def _stream_graph(
    app,
    pr_number: str,
    repo: str | None,
    excludes: tuple[str, ...],
    model_count: int,
    head_sha: str = "",
    diff: str = "",
) -> tuple[str, list]:
    """Drive the graph, reporting each node's outcome as it lands.

    Returns the report and the individual results. The results are what a later
    run compares against to say which findings were resolved, so they have to
    survive the graph rather than being folded into prose and discarded.
    """
    report = ""
    results: list = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console,
    ) as progress:
        task = progress.add_task("Gathering PR context...", total=None)

        initial = {
            "pr_number": pr_number,
            "repo": repo or "",
            # Already resolved by the caller, which needed it for the no-op
            # gate. Passing it on saves the context node a second lookup.
            "head_ref": head_sha,
            # Only set when the triage gate already fetched it.
            "diff": diff,
            "excludes": excludes,
            "results": [],
        }
        for chunk in app.stream(initial, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "fetch_context":
                    progress.update(task, description=f"Reviewing with {model_count} models...")
                    if update.get("context") is not None:
                        _print_context(pr_number, update["context"], via=progress.console)
                elif node.startswith("review_"):
                    # In "updates" mode each chunk carries only that node's
                    # own contribution, so this is exactly one result.
                    for result in update.get("results", []):
                        results.append(result)
                        _print_result(result, via=progress.console)
                elif node == "synthesize":
                    progress.update(task, description="Synthesizing...")
                    report = update["final_report"]

    return report, results


def _print_result(result, via=console) -> None:
    if result.ok:
        count = len(result.findings)
        noun = "finding" if count == 1 else "findings"
        suffix = " [dim](cached)[/dim]" if getattr(result, "cached", False) else ""
        via.print(f"  [green]OK[/green] {result.model} — {count} {noun}{suffix}")
    else:
        via.print(f"  [red]FAIL[/red] {result.model} — {escape(str(result.error))}")


def _print_context(pr_number: str, ctx: context.ReviewContext, via=console) -> None:
    via.print(f"PR #{pr_number}")
    via.print(f"  diff lines:       {len(ctx.diff.splitlines())}")
    via.print(f"  files included:   {len(ctx.files)}")
    for path in ctx.files:
        via.print(f"    [green]+[/green] {escape(path)}")
    if ctx.dropped:
        via.print(
            f"  [yellow]WARNING: {len(ctx.dropped)} file(s) over token budget, "
            f"diff hunks only:[/yellow]"
        )
        for path in ctx.dropped:
            via.print(f"    [yellow]-[/yellow] {escape(path)}")
    if ctx.missing:
        via.print(f"  not readable locally ({len(ctx.missing)}), diff hunks only:")
        for path in ctx.missing:
            via.print(f"    ? {escape(path)}")
    if ctx.rejected:
        via.print(
            f"  [bold red]REFUSED: {len(ctx.rejected)} diff path(s) point outside this "
            f"repository and were not read:[/bold red]"
        )
        for path in ctx.rejected:
            via.print(f"    [red]![/red] {escape(path)}")
    via.print(f"  estimated tokens: {ctx.estimated_tokens:,}")
    if ctx.diff_overflow:
        via.print(
            f"  [yellow]WARNING: the diff alone is {ctx.diff_overflow:,} tokens over the "
            f"budget. No file contents were included and the models may reject it.[/yellow]"
        )


if __name__ == "__main__":
    main()
