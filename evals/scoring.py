"""Automatic hints for the manual eval. The owner's y/n answers are the score of record."""

import json
import re

from tests.scenarios import Scenario

_WORD = re.compile(r"[a-z0-9_.:/-]+")


def corpus(scenario: Scenario) -> str:
    """Every fact the agent could have seen for this scenario, lowercased."""
    parts = [json.dumps(scenario.snapshot), *scenario.repo_files.values(), *(line for _, line in scenario.loki_lines)]
    parts += [ts for ts, _ in scenario.loki_lines]
    return "\n".join(parts).lower()


def grounded(detail: str, text: str, window: int = 3) -> bool:
    """True if any `window` consecutive words of `detail` appear in `text`, or one 12+ character word does."""
    words = _WORD.findall(detail.lower())
    if any(len(w) >= 12 and w in text for w in words):
        return True
    return any(" ".join(words[i : i + window]) in text for i in range(len(words) - window + 1))


def score(scenario: Scenario, result: dict) -> dict:
    if "clarify" in result or "error" in result:
        return {"cause_hint": False, "fix_kind_match": False, "grounded": 0, "evidence": 0}
    text = corpus(scenario)
    evidence = result.get("evidence", [])
    cause = result.get("cause", "").lower()
    return {
        "cause_hint": any(k.lower() in cause for k in scenario.expect_cause),
        "fix_kind_match": result.get("fix_kind") == scenario.expect_fix_kind,
        "grounded": sum(grounded(e.get("detail", ""), text) for e in evidence),
        "evidence": len(evidence),
    }
