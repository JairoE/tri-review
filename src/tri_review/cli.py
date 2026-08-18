"""tri-review command line entry point."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from . import context, github
from .errors import TriReviewError

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
    help="A GitHub pull request URL. Sets --repo and --pr from it.",
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
        "--model gpt-5.1 --model claude-opus-5 --model gemini-2.5-pro. "
        "Defaults to the three configured slots. At least two are required."
    ),
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Also write the raw Markdown report to this file.",
)
@click.version_option(package_name="tri-review")
def main(
    pr: str | None,
    repo: str | None,
    url: str | None,
    dry_run: bool,
    models: tuple[str, ...],
    output: Path | None,
) -> None:
    """Review a GitHub pull request with three LLMs and report their consensus."""
    load_dotenv()
    try:
        if url:
            repo, pr = github.parse_pr_url(url)
        _run(pr, repo, dry_run, models, output)
    except TriReviewError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)


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


def _run(
    pr: str | None,
    repo: str | None,
    dry_run: bool,
    selected: tuple[str, ...],
    output: Path | None,
) -> None:
    # Imported here so --help and --dry-run stay fast and key-free.
    from .graph import build_review_graph

    models = _resolve_models(selected)

    github.preflight(repo)
    pr_number = pr or github.detect_pr(repo)

    if dry_run:
        ctx = context.preview_context(pr_number, repo)
        _print_context(pr_number, ctx)
        console.print(f"\n[dim]would review with: {', '.join(models)}[/dim]")
        console.print("[dim]--dry-run: stopping before any model call.[/dim]")
        return

    console.print(
        Panel(
            f"[bold cyan]tri-review[/bold cyan]  {repo + ' ' if repo else ''}PR #{pr_number}\n"
            f"[dim]{'  ·  '.join(models)}[/dim]",
            expand=False,
        )
    )

    app = build_review_graph(models=models)
    report = _stream_graph(app, pr_number, repo, len(models))

    console.print()
    console.print(Markdown(report))

    if output:
        try:
            output.write_text(report, encoding="utf-8")
        except OSError as exc:
            # The report is already on screen and the models are already paid
            # for, so a bad path is a warning, not a failed run.
            console.print(f"\n[yellow]Could not write {output}: {exc}[/yellow]")
        else:
            console.print(f"\n[green]Wrote report to[/green] {output}")


def _stream_graph(app, pr_number: str, repo: str | None, model_count: int) -> str:
    """Drive the graph, reporting each node's outcome as it lands."""
    report = ""

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console,
    ) as progress:
        task = progress.add_task("Gathering PR context...", total=None)

        initial = {"pr_number": pr_number, "repo": repo or "", "results": []}
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
                        _print_result(result, via=progress.console)
                elif node == "synthesize":
                    progress.update(task, description="Synthesizing...")
                    report = update["final_report"]

    return report


def _print_result(result, via=console) -> None:
    if result.ok:
        count = len(result.findings)
        noun = "finding" if count == 1 else "findings"
        via.print(f"  [green]OK[/green] {result.model} — {count} {noun}")
    else:
        via.print(f"  [red]FAIL[/red] {result.model} — {result.error}")


def _print_context(pr_number: str, ctx: context.ReviewContext, via=console) -> None:
    via.print(f"PR #{pr_number}")
    via.print(f"  diff lines:       {len(ctx.diff.splitlines())}")
    via.print(f"  files included:   {len(ctx.files)}")
    for path in ctx.files:
        via.print(f"    [green]+[/green] {path}")
    if ctx.dropped:
        via.print(
            f"  [yellow]WARNING: {len(ctx.dropped)} file(s) over token budget, "
            f"diff hunks only:[/yellow]"
        )
        for path in ctx.dropped:
            via.print(f"    [yellow]-[/yellow] {path}")
    if ctx.missing:
        via.print(f"  not readable locally ({len(ctx.missing)}), diff hunks only:")
        for path in ctx.missing:
            via.print(f"    ? {path}")
    if ctx.rejected:
        via.print(
            f"  [bold red]REFUSED: {len(ctx.rejected)} diff path(s) point outside this "
            f"repository and were not read:[/bold red]"
        )
        for path in ctx.rejected:
            via.print(f"    [red]![/red] {path}")
    via.print(f"  estimated tokens: {ctx.estimated_tokens:,}")
    if ctx.diff_overflow:
        via.print(
            f"  [yellow]WARNING: the diff alone is {ctx.diff_overflow:,} tokens over the "
            f"budget. No file contents were included and the models may reject it.[/yellow]"
        )


if __name__ == "__main__":
    main()
