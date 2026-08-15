"""tri-review command line entry point."""

from __future__ import annotations

import sys

import click
from dotenv import load_dotenv

from . import context, github
from .errors import TriReviewError


@click.command()
@click.option("--pr", default=None, help="Pull request number. Omit to detect the current branch's PR.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be sent to the models, then exit without calling them.",
)
@click.version_option(package_name="tri-review")
def main(pr: str | None, dry_run: bool) -> None:
    """Review a GitHub pull request with three LLMs and report their consensus."""
    load_dotenv()
    try:
        _run(pr, dry_run)
    except TriReviewError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(exc.exit_code)


def _run(pr: str | None, dry_run: bool) -> None:
    github.preflight()
    pr_number = pr or github.detect_pr()
    diff = github.fetch_diff(pr_number)
    ctx = context.build_context(diff)

    if dry_run:
        _print_context_summary(pr_number, ctx)
        return

    _print_context_summary(pr_number, ctx)
    click.echo("\n(review pipeline not wired up yet)")


def _print_context_summary(pr_number: str, ctx: context.ReviewContext) -> None:
    click.echo(f"PR #{pr_number}")
    click.echo(f"  diff lines:       {len(ctx.diff.splitlines())}")
    click.echo(f"  files included:   {len(ctx.files)}")
    for path in ctx.files:
        click.echo(f"    + {path}")
    if ctx.dropped:
        click.echo(f"  WARNING: {len(ctx.dropped)} file(s) over token budget, diff hunks only:")
        for path in ctx.dropped:
            click.echo(f"    - {path}")
    if ctx.missing:
        click.echo(f"  not readable locally ({len(ctx.missing)}), diff hunks only:")
        for path in ctx.missing:
            click.echo(f"    ? {path}")
    click.echo(f"  estimated tokens: {ctx.estimated_tokens:,}")


if __name__ == "__main__":
    main()
