"""Automatic hints for the manual eval. The owner's y/n answers are the score of record."""

import re

from tests.scenarios import Scenario

_TRAILING = ".,;:"
_DISTINCTIVE_MARKS = "/:@"


def _tokens(text: str) -> list[str]:
    """Lowercase, split on whitespace, and strip trailing punctuation from each token."""
    return [t for t in (raw.lower().rstrip(_TRAILING) for raw in text.split()) if t]


def _facts_of(item: dict, path: str) -> list[str]:
    """The facts one Kubernetes-shaped object exposes. Field values only, never schema key names."""
    meta = item.get("metadata", {})
    name = meta.get("name", "")
    if "involvedObject" in item:
        return [
            item.get("reason", ""),
            item.get("message", ""),
            f"{item['involvedObject'].get('kind', '')} {item['involvedObject'].get('name', '')}",
        ]
    if "subsets" in item:
        facts = [name]
        for subset in item["subsets"]:
            facts += [
                f"not ready endpoint {a['ip']} for pod {a.get('targetRef', {}).get('name', '')}"
                for a in subset.get("notReadyAddresses", [])
            ]
            facts += [f"port {p['port']}" for p in subset.get("ports", [])]
        return facts
    if "routes" in item.get("spec", {}):
        facts = [name]
        for route in item["spec"]["routes"]:
            for svc in route.get("services", []):
                facts.append(f"{route.get('match', '')} service {svc.get('name', '')} port {svc.get('port', '')}")
        return facts
    if "containers" in item and "spec" not in item:  # pod metrics
        return [f"pod {name} memory usage {item['containers'][0]['usage']['memory']}"]
    if "usage" in item and "status" not in item:  # node metrics
        return [f"node {name} memory usage {item['usage']['memory']}"]
    status = item.get("status", {})
    if "allocatable" in status:
        return [f"node {name} allocatable memory {status['allocatable']['memory']}"]
    if "phase" in status:  # pod
        facts = [name, f"phase {status['phase']}"]
        for container in item.get("spec", {}).get("containers", []):
            facts.append(container.get("image", ""))
            res = container.get("resources", {})
            facts += [
                f"memory request {res.get('requests', {}).get('memory', '')}",
                f"memory limit {res.get('limits', {}).get('memory', '')}",
            ]
        for cs in status.get("containerStatuses", []):
            facts += [cs.get("image", ""), f"restarts {cs.get('restartCount', 0)}"]
            waiting = cs.get("state", {}).get("waiting", {})
            facts += [waiting.get("reason", ""), waiting.get("message", "")]
            terminated = cs.get("lastState", {}).get("terminated", {})
            if terminated:
                facts += [
                    terminated.get("reason", ""),
                    f"exit code {terminated.get('exitCode', '')}",
                    terminated.get("finishedAt", ""),
                ]
        for cond in status.get("conditions", []):
            facts += [cond.get("reason", ""), cond.get("message", "")]
        return facts
    if "kind" in item:  # Deployment, StatefulSet, DaemonSet
        facts = [name]
        for container in item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
            facts.append(container.get("image", ""))
            res = container.get("resources", {})
            facts += [
                f"memory request {res.get('requests', {}).get('memory', '')}",
                f"memory limit {res.get('limits', {}).get('memory', '')}",
            ]
        facts.append(f"{status.get('readyReplicas', 0)} of {status.get('replicas', 0)} replicas ready")
        for cond in status.get("conditions", []):
            facts += [cond.get("reason", ""), cond.get("message", "")]
        return facts
    if "template" in item.get("spec", {}):  # ReplicaSet
        facts = [name]
        facts += [c.get("image", "") for c in item["spec"]["template"]["spec"]["containers"]]
        facts += [o.get("name", "") for o in meta.get("ownerReferences", [])]
        return facts
    if "data" in item:  # ConfigMap
        return [name] + [f"{k} {v}" for k, v in item["data"].items()]
    return []


def corpus(scenario: Scenario) -> str:
    """The facts the agent could have seen for this scenario: one per line, no decoy workloads, no schema keys."""
    ns = scenario.namespace
    facts: list[str] = [scenario.workload, ns]
    for path, obj in scenario.snapshot.items():
        if isinstance(obj, str):  # pod logs, current or previous
            if f"/namespaces/{ns}/" in path:
                facts += obj.splitlines()
            continue
        items = obj.get("items", []) if isinstance(obj, dict) and "items" in obj else [obj]
        for item in items:
            if "/nodes" in path:
                facts += _facts_of(item, path)
                continue
            item_ns = item.get("metadata", {}).get("namespace")
            in_scope = (item_ns == ns) if item_ns is not None else (f"/namespaces/{ns}/" in path)
            if in_scope:
                facts += _facts_of(item, path)
    facts = [f for f in dict.fromkeys(facts) if f]
    facts += [
        line
        for path, text_ in scenario.repo_files.items()
        if f"apps/{scenario.workload}/" in path or f"apps/{ns}/" in path
        for line in text_.splitlines()
    ]
    facts += [f"{ts} {line}" for ts, line in scenario.loki_lines]
    return "\n".join(facts)


def grounded(detail: str, text: str) -> bool:
    """True if three consecutive tokens of `detail` appear in `text` as whole words, or if `detail` has one
    distinctive token (4+ characters, a digit or one of / : @) that appears as a whole word in `text`."""
    detail_tokens = _tokens(detail)
    corpus_tokens = _tokens(text)
    if any(
        len(t) >= 4 and (any(c.isdigit() for c in t) or any(m in t for m in _DISTINCTIVE_MARKS)) and t in corpus_tokens
        for t in detail_tokens
    ):
        return True
    windows = {tuple(corpus_tokens[i : i + 3]) for i in range(len(corpus_tokens) - 2)}
    return any(tuple(detail_tokens[i : i + 3]) in windows for i in range(len(detail_tokens) - 2))


def score(scenario: Scenario, result: dict) -> dict:
    if "clarify" in result or "error" in result:
        return {"cause_hint": False, "fix_kind_match": False, "grounded": 0, "evidence": 0}
    text = corpus(scenario)
    evidence = result.get("evidence", [])
    cause = result.get("cause", "")
    return {
        "cause_hint": any(re.search(rf"\b{re.escape(k)}\b", cause, re.IGNORECASE) for k in scenario.expect_cause),
        "fix_kind_match": result.get("fix_kind") == scenario.expect_fix_kind,
        "grounded": sum(grounded(e.get("detail", ""), text) for e in evidence),
        "evidence": len(evidence),
    }
