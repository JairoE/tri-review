"""The Action's model inputs reach the CLI as the flags they document.

Runs the "Run tri-review" step's shell exactly as action.yml has it, with a
stub `tri-review` on PATH that records its argv. A mistake here is invisible
until a consumer's run quietly reviews with the default panel instead of the
one their workflow names.
"""

import os
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent

_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$ARGV_FILE"
"""


def _argv(tmp_path, **inputs) -> list[str]:
    steps = yaml.safe_load((ROOT / "action.yml").read_text())["runs"]["steps"]
    step = next(s for s in steps if s["name"] == "Run tri-review")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "tri-review"
    stub.write_text(_STUB)
    stub.chmod(0o755)
    argv_file = tmp_path / "argv"

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        "REPORT": str(tmp_path / "rt" / "report.md"),
        "ARGV_FILE": str(argv_file),
        "INPUT_PR_NUMBER": "7",
        "INPUT_MODELS": "",
        "INPUT_SYNTHESIZER": "",
        "INPUT_EFFORT": "",
        "INPUT_EXCLUDE": "",
        "INPUT_TRIAGE": "false",
        **inputs,
    }
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return argv_file.read_text().splitlines()


def _values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_reviewers_become_one_reviewer_flag_each(tmp_path):
    argv = _argv(tmp_path, INPUT_MODELS="gpt-6-sol@high  gpt-5.6-sol@high\nopenai:my-finetune")
    assert _values(argv, "--reviewer") == [
        "gpt-6-sol@high",
        "gpt-5.6-sol@high",
        "openai:my-finetune",
    ]


def test_synthesizer_and_effort_are_passed_through(tmp_path):
    argv = _argv(
        tmp_path,
        INPUT_MODELS="gpt-6-sol gpt-5.6-sol",
        INPUT_SYNTHESIZER="gpt-astra@medium",
        INPUT_EFFORT="high",
    )
    assert _values(argv, "--synthesizer") == ["gpt-astra@medium"]
    assert _values(argv, "--effort") == ["high"]


def test_unset_inputs_add_no_flags(tmp_path):
    """Empty means the CLI's own defaults, not an empty-string flag."""
    argv = _argv(tmp_path)
    assert "--reviewer" not in argv
    assert "--synthesizer" not in argv
    assert "--effort" not in argv


def test_a_spec_is_never_globbed(tmp_path):
    """A `*` in an input must reach the CLI as text, not as the files it matches."""
    (tmp_path / "gpt-a").write_text("")
    (tmp_path / "gpt-b").write_text("")
    argv = _argv(tmp_path, INPUT_MODELS="gpt-* claude-opus-5")
    assert _values(argv, "--reviewer") == ["gpt-*", "claude-opus-5"]


def test_reviewers_wins_over_the_older_models_input():
    """The env line picks `reviewers` first; GitHub evaluates the expression."""
    steps = yaml.safe_load((ROOT / "action.yml").read_text())["runs"]["steps"]
    step = next(s for s in steps if s["name"] == "Run tri-review")
    assert step["env"]["INPUT_MODELS"] == "${{ inputs.reviewers || inputs.models }}"


def test_every_new_input_is_declared():
    inputs = yaml.safe_load((ROOT / "action.yml").read_text())["inputs"]
    for name in ("reviewers", "models", "synthesizer", "effort"):
        assert name in inputs
        assert inputs[name].get("required") is False
