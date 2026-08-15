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
    "--dry-run",
    is_flag=True,
    help="Show what would be sent to the models, then exit without calling them.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Also write the raw Markdown report to this file.",
)
@click.version_option(package_name="tri-review")
def main(pr: str | None, dry_run: bool, output: Path | None) -> None:
    """Review a GitHub pull request with three LLMs and report their consensus."""
    load_dotenv()
    try:
        _run(pr, dry_run, output)
    except TriReviewError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)


def _run(pr: str | None, dry_run: bool, output: Path | None) -> None:
    # Imported here so --help and --dry-run stay fast and key-free.
    from . import config
    from .graph import build_review_graph

    github.preflight()
    pr_number = pr or github.detect_pr()

    if dry_run:
        ctx = context.build_context(github.fetch_diff(pr_number))
        _print_context(pr_number, ctx)
        console.print("\n[dim]--dry-run: stopping before any model call.[/dim]")
        return

    models = [config.model_a(), config.model_b(), config.model_c()]
    console.print(
        Panel(
            f"[bold cyan]tri-review[/bold cyan]  PR #{pr_number}\n"
            f"[dim]{'  ·  '.join(models)}[/dim]",
            expand=False,
        )
    )

    app = build_review_graph(models=models)
    report = _stream_graph(app, pr_number)

    console.print()
    console.print(Markdown(report))

    if output:
        output.write_text(report, encoding="utf-8")
        console.print(f"\n[green]Wrote report to[/green] {output}")


def _stream_graph(app, pr_number: str) -> str:
    """Drive the graph, reporting each node's outcome as it lands."""
    report = ""

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console,
    ) as progress:
        task = progress.add_task("Gathering PR context...", total=None)

        for chunk in app.stream({"pr_number": pr_number, "results": []}, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "fetch_context":
                    progress.update(task, description="Reviewing with 3 models...")
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
    via.print(f"  estimated tokens: {ctx.estimated_tokens:,}")


if __name__ == "__main__":
    main()
