"""Manual eval against the real model. Not run in CI.

    ZAI_API_KEY=... uv run python -m evals.run_eval [--scenario crashloop] [--no-prompt]

Prints each diagnosis, asks the owner "right cause?" and "cites real evidence?", and writes
the answers to evals/results/<UTC timestamp>.jsonl (gitignored). Exit 0 when every scenario ran,
1 when any scenario recorded an error row, 2 when ZAI_API_KEY is missing."""

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from evals.harness import run_scenario
from k8s_agent.graph import DeadlineExceeded
from k8s_agent.llm import ModelUnavailable, OpenAIChatModel
from k8s_agent.render import KustomizeRenderer
from tests.graph_fakes import FakeRenderer
from tests.scenarios import ALL

RESULTS = Path(__file__).parent / "results"


def ask(prompt: str) -> bool | None:
    """The owner's answer, or None when stdin ends before an answer (the question stays unanswered)."""
    try:
        return input(f"{prompt} [y/n] ").strip().lower().startswith("y")
    except EOFError:
        return None


def _answer(value: bool | None) -> str:
    return "-" if value is None else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=sorted(ALL), action="append")
    parser.add_argument("--no-prompt", action="store_true", help="print automatic hints only")
    args = parser.parse_args(argv)
    key = os.environ.get("ZAI_API_KEY", "")
    if not key:
        print("ZAI_API_KEY is not set", file=sys.stderr)
        return 2
    model = OpenAIChatModel(
        os.environ.get("MODEL_BASE_URL", "https://api.z.ai/api/coding/paas/v4"),
        key,
        os.environ.get("MODEL", "glm-5.3"),
        timeout=60,
    )
    if shutil.which("kustomize"):
        renderer = KustomizeRenderer()
    else:
        print("warning: kustomize is not on PATH; using FakeRenderer, so overlays are not built", file=sys.stderr)
        renderer = FakeRenderer()
    RESULTS.mkdir(exist_ok=True)
    out_path = RESULTS / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    rows = []
    for name in args.scenario or sorted(ALL):
        try:
            row = run_scenario(ALL[name], model, renderer)
        except ModelUnavailable as exc:
            row = {"scenario": name, "error": f"model unavailable: {exc}"}
        except DeadlineExceeded as exc:
            row = {"scenario": name, "error": type(exc).__name__}
        except Exception as exc:  # one failing scenario must not stop the rest; only the type is recorded
            row = {"scenario": name, "error": type(exc).__name__}
        print(json.dumps(row, indent=2))
        if not args.no_prompt and "error" not in row:
            row["owner_right_cause"] = ask(f"[{name}] Right cause?")
            row["owner_real_evidence"] = ask(f"[{name}] Cites real evidence?")
        rows.append(row)
        with out_path.open("a") as f:
            f.write(json.dumps(row) + "\n")
    print(f"\n{'scenario':<10} {'cause':<6} {'fix':<5} {'grounded':<9} owner")
    for r in rows:
        if "error" in r:
            print(f"{r['scenario']:<10} {'-':<6} {'-':<5} {'-':<9} error: {r['error']}")
            continue
        a = r.get("auto", {})
        owner = f"{_answer(r.get('owner_right_cause'))}/{_answer(r.get('owner_real_evidence'))}"
        grounded = f"{a.get('grounded', 0)}/{a.get('evidence', 0)}"
        print(
            f"{r['scenario']:<10} {a.get('cause_hint', '-')!s:<6} {a.get('fix_kind_match', '-')!s:<5} "
            f"{grounded:<9} {owner}"
        )
    print(f"\nwrote {out_path}")
    return 1 if any("error" in r for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
