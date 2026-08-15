Create a .env file at the root of your project:
OPENAI_API_KEY=your_openai_key
ANTHROPIC_API_KEY=your_anthropic_key
GOOGLE_API_KEY=your_google_key
GITHUB_TOKEN=your_github_token # For fetching PRs via API

2. Defining the Graph State (state.py)
   In LangGraph, the State is a dictionary that gets passed between nodes. Each node returns a dictionary that updates this state.

# state.py

from typing import TypedDict, List, Annotated
import operator

class ReviewState(TypedDict):
pr_number: str
diff: str
context: str # 'reviews' will aggregate outputs from all models. # The Annotated type with operator.add means new reviews are appended to the list, not overwritten.
reviews: Annotated[List[str], operator.add]
final_report: str

3. Creating the Graph Nodes (nodes.py)
   Nodes are just Python functions that take the state, perform an action, and return a dictionary containing state updates.

# nodes.py

import os
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from state import ReviewState

# --- 1. Context Builder Node ---

def fetch_pr_context(state: ReviewState):
\"\"\"Fetches the PR diff and repo context.\"\"\"
pr_number = state.get("pr_number")

    # In a real app, use PyGithub or gitpython here to fetch the diff.
    # For MVP, we simulate pulling the diff:
    mock_diff = f"Mock Git Diff for PR #{pr_number}\\n+ def insecure_hash(password):\\n+    return md5(password)"

    return {"diff": mock_diff, "context": "Repo language: Python."}

# --- 2. Parallel Review Nodes (Fan-Out) ---

REVIEW_PROMPT = \"\"\"You are an expert code reviewer.
Review the following PR diff. Focus on bugs, security, and logic errors.
Ignore styling nitpicks. Keep it concise.
\"\"\"

def review_model_a(state: ReviewState):
llm = ChatOpenAI(model="gpt-4o", temperature=0.2)
messages = [
SystemMessage(content=REVIEW_PROMPT),
HumanMessage(content=f"Context: {state['context']}\\n\\nDiff: {state['diff']}")
]
response = llm.invoke(messages)
return {"reviews": [f"### Model A (GPT-4o)\\n{response.content}"]}

def review_model_b(state: ReviewState):
llm = ChatAnthropic(model="claude-3-5-sonnet-20240620", temperature=0.2)
messages = [
SystemMessage(content=REVIEW_PROMPT),
HumanMessage(content=f"Context: {state['context']}\\n\\nDiff: {state['diff']}")
]
response = llm.invoke(messages)
return {"reviews": [f"### Model B (Claude)\\n{response.content}"]}

def review_model_c(state: ReviewState):
llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro", temperature=0.2)
messages = [
SystemMessage(content=REVIEW_PROMPT),
HumanMessage(content=f"Context: {state['context']}\\n\\nDiff: {state['diff']}")
]
response = llm.invoke(messages)
return {"reviews": [f"### Model C (Gemini)\\n{response.content}"]}

# --- 3. Synthesizer Node (Fan-In) ---

def synthesize_reviews(state: ReviewState):
llm = ChatOpenAI(model="gpt-4o", temperature=0.1) # Using a strong model for reasoning

    reviews_text = "\\n\\n---\\n\\n".join(state["reviews"])

    sys_prompt = \"\"\"You are the Lead Engineer Synthesizer.
    You will receive 3 code reviews from different AI models.
    Your task:
    1. Identify 'Consensus Findings' (caught by 2+ models).
    2. Identify 'Unique Insights' (high-value catches by 1 model).
    3. Output a final Markdown report. Do not repeat the raw reviews.
    \"\"\"

    messages = [
        SystemMessage(content=sys_prompt),
        HumanMessage(content=f"Here are the reviews:\\n\\n{reviews_text}")
    ]

    response = llm.invoke(messages)
    return {"final_report": response.content}

4. Assembling the LangGraph Workflow (graph.py)
   This is where the magic happens. We wire the nodes together into a DAG, defining how state flows.

# graph.py

from langgraph.graph import StateGraph, END
from state import ReviewState
from nodes import (
fetch_pr_context,
review_model_a,
review_model_b,
review_model_c,
synthesize_reviews
)

def build_review_graph(): # Initialize the graph with our TypedDict state
workflow = StateGraph(ReviewState)

    # 1. Add all nodes to the graph
    workflow.add_node("fetch_context", fetch_pr_context)
    workflow.add_node("model_a", review_model_a)
    workflow.add_node("model_b", review_model_b)
    workflow.add_node("model_c", review_model_c)
    workflow.add_node("synthesizer", synthesize_reviews)

    # 2. Define the edges (The Flow)

    # Start -> Context Node
    workflow.set_entry_point("fetch_context")

    # Context -> Models (Fan-out)
    # LangGraph automatically runs these in parallel when directed from a single node
    workflow.add_edge("fetch_context", "model_a")
    workflow.add_edge("fetch_context", "model_b")
    workflow.add_edge("fetch_context", "model_c")

    # Models -> Synthesizer (Fan-in)
    workflow.add_edge("model_a", "synthesizer")
    workflow.add_edge("model_b", "synthesizer")
    workflow.add_edge("model_c", "synthesizer")

    # Synthesizer -> End
    workflow.add_edge("synthesizer", END)

    # Compile the graph into an executable application
    return workflow.compile()

5. Building the CLI (cli.py)
   We use Click for parsing commands and Rich for a beautiful loading spinner and markdown output.

# cli.py

import click
import os
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from graph import build_review_graph

load_dotenv()
console = Console()

@click.command()
@click.option('--pr', required=True, help='The Pull Request number to review.')
def main(pr):
\"\"\"tri-review: Triangulated AI Code Reviewer\"\"\"

    console.print(Panel(f"[bold cyan]tri-review[/bold cyan] starting on PR #{pr}...", expand=False))

    # Compile Graph
    app = build_review_graph()

    # Initial State
    inputs = {"pr_number": pr, "reviews": []}

    # Execute Graph with Rich Spinner
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:

        task_id = progress.add_task("[yellow]Gathering Context & Running Parallel AI Reviews...", total=None)

        try:
            # invoke() runs the graph synchronously but internal parallel edges are run asynchronously
            # by LangGraph using Python's asyncio under the hood.
            result = app.invoke(inputs)
            progress.update(task_id, description="[green]Synthesis Complete!")

        except Exception as e:
            console.print(f"[bold red]Error during execution:[/bold red] {e}")
            return

    # Output the Synthesized Markdown
    console.print("\\n[bold green]✅ Review Complete. Final Synthesis:[/bold green]\\n")
    md = Markdown(result["final_report"])
    console.print(md)

if **name** == '**main**':
main()

6. How to Run the MVP

- Ensure your .env is loaded with API keys.
- Run the tool via the command line:
  python cli.py --pr 42

Next Steps for Production

- Git Integration: Replace the mocked fetch_pr_context logic with API calls to GitHub (e.g., using PyGithub) to grab the actual PR diff.
- Chunking Strategy: If the diff is massive, you may need a node before the fan-out that chunks the diff into smaller payloads or uses an LLM to pre-filter unchanged or boilerplate files.
- Streaming Output: Hook into LangGraph's .stream() method instead of .invoke() to stream the progress of each node to the user via the terminal UI.
  """
  with open("tri_review_implementation.md", "w") as f:
  f.write(content)
  print("File generated successfully: tri_review_implementation.md")

```text?code_stdout&code_event_index=1
File generated successfully: tri_review_implementation.md


The document contains a full MVP (Minimum Viable Product) guide. It leverages Python, LangGraph for the fan-out/fan-in architecture, LangChain for model integrations (OpenAI, Anthropic, Google), and Click/Rich for building a polished CLI tool.
```
