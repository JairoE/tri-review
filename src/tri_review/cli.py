"""tri-review command line entry point."""

from __future__ import annotations

import click
from dotenv import load_dotenv


@click.command()
@click.option("--pr", default=None, help="Pull request number. Omit to detect the current branch's PR.")
@click.version_option(package_name="tri-review")
def main(pr: str | None) -> None:
    """Review a GitHub pull request with three LLMs and report their consensus."""
    load_dotenv()
    click.echo(f"tri-review: pr={pr or 'auto-detect'}")


if __name__ == "__main__":
    main()
