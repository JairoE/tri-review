# Product Requirements Document (PRD): `tri-review` CLI

## 1\. Product Overview

**Name:** `tri-review` (Triangulated PR Reviewer) **Goal:** A CLI application executed at the root of a local git repository. It fetches an open Pull Request, gathers local repository context, processes the PR through three distinct LLMs, and outputs a synthesized comparison highlighting the highest-value code changes to make. **Target Audience:** Software Engineers, Tech Leads, and Open-Source Maintainers who want rigorous, hallucination-free AI code reviews.

## 2\. User Flow

1.  The user navigates to their local repository in the terminal.
2.  The user runs `tri-review --pr 123` (or simply `tri-review` to detect the current branch's PR).
3.  The CLI identifies the PR diff and packages it with relevant repository context (e.g., file structure, imported files).
4.  The CLI concurrently sends the context and diff to Model A, Model B, and Model C.
5.  A "Synthesizer" module evaluates the three responses, identifying consensus and filtering out edge-case hallucinations.
6.  The CLI outputs a formatted Markdown report in the terminal outlining:
    - **Consensus Findings:** Issues caught by 2 or more models.
    - **Unique Insights:** High-value observations caught by only one model.
    - **Actionable Next Steps:** Specific code snippets to change.

## 3\. Core Features & Requirements

### 3.1. Context & Diff Gathering Engine

- **PR Retrieval:** Integration with Git/GitHub APIs to pull the target PR's diff.
- **Repo Context:** Since it runs locally, the tool must read the local file system to map dependencies or provide full-file context for the files modified in the PR diff.
- **Token Management:** A chunking or formatting mechanism to ensure the combined Diff + Context fits within the context windows of all three target models.

### 3.2. Multi-Model Orchestration (The Graph)

- **Concurrent Execution:** The tool must execute the API calls to the three models asynchronously to minimize waiting time.
- **Agnostic Prompts:** A standardized system prompt that instructs the models to behave as expert reviewers, focusing on logic, security, and performance rather than stylistic nitpicks.

### 3.3. Synthesis & Comparison Engine

- **Consensus Algorithm:** A final LLM call (the Synthesizer) that takes the raw outputs from Model A, B, and C as its input.
- **Categorization:** The synthesizer must cross-reference the reviews and output a structured JSON or Markdown file comparing the results.

### 3.4. Output & UX

- **Terminal UI:** Rich terminal output (using a library like `Rich` in Python or `Ink` in Node.js) with loading spinners for parallel tasks.
- _(Optional)_ **Automated Commenting:** A flag (e.g., `--comment`) to automatically post the synthesized review as a comment on the GitHub PR.

## 4\. Architecture: The Graph Workflow

Because this is a Graph solution, the architecture follows a strict state machine:

1.  **State Initiation:** `{"pr_id": 123, "diff": null, "context": null, "reviews": [], "final_report": null}`
2.  **Node 1 (Context Builder):** Fetches diff and local context. Updates State.
3.  **Parallel Nodes (The Fan-Out):**
    - **Node 2A:** Prompts Model A (e.g., GPT-4o).
    - **Node 2B:** Prompts Model B (e.g., Claude 3.5 Sonnet).
    - **Node 2C:** Prompts Model C (e.g., Gemini 1.5 Pro).
    - _All append their output to the `reviews` array in the State._

4.  **Node 3 (The Synthesizer / Fan-In):** Reads the `reviews` array. Compares them. Updates `final_report`.
5.  **Node 4 (Output):** Renders `final_report` to the CLI.

## 5\. Tech Stack Recommendations

- **Language:** Python
- **Framework:** **LangGraph** Python
- **Terminal UI:** `Rich` (if Python) for beautiful, readable CLI spinners and Markdown rendering.
- **Git Integration:** `PyGithub` / `Octokit` combined with native CLI git commands.
