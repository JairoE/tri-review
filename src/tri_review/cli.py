"""tri-review command line entry point."""

from __future__ import annotations

import sys

import click
from dotenv import load_dotenv

from . import github
from .errors import TriReviewError


@click.command()
@click.option("--pr", default=None, help="Pull request number. Omit to detect the current branch's PR.")
@click.version_option(package_name="tri-review")
def main(pr: str | None) -> None:
    """Review a GitHub pull request with three LLMs and report their consensus."""
    load_dotenv()
    try:
        _run(pr)
    except TriReviewError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(exc.exit_code)


def _run(pr: str | None) -> None:
    github.preflight()
    pr_number = pr or github.detect_pr()
    diff = github.fetch_diff(pr_number)
    click.echo(f"PR #{pr_number}: fetched {len(diff.splitlines())} diff lines")


if __name__ == "__main__":
    main()
