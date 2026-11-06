"""The diagnose graph: intake -> triage -> investigate -> diagnose -> propose_fix (config only)."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from k8s_agent import prompts
from k8s_agent.diffcheck import PathNotAllowed, check_paths, overlays_for, paths_in_diff
from k8s_agent.llm import ChatModel, ModelReply, parse_json_object
from k8s_agent.proposals import ProposalStore
from k8s_agent.render import Renderer, RenderError
from k8s_agent.repo import RepoError, Workspace
from k8s_agent.settings import Budgets
from k8s_agent.tools.cluster import ClusterTools
from k8s_agent.tools.toolbox import Toolbox

MAX_FILES_BYTES = 32768


class DeadlineExceeded(Exception):
    """The graph passed its overall time limit."""


class Intake(BaseModel):
    namespace: str | None = None
    workload: str | None = None
    kind: Literal["Deployment", "StatefulSet", "DaemonSet"] | None = None
    symptom: Literal["crashloop", "oom", "imagepull", "pending", "5xx", "other"] = "other"
    clarify: str | None = None


class Evidence(BaseModel):
    source: str
    detail: str


class Diagnosis(BaseModel):
    cause: str
    confidence: Literal["high", "medium", "low"]
    evidence: list[Evidence]
    fix_kind: Literal["config", "app-code", "capacity", "none", "unknown"]
    fix_summary: str | None = None


class FileEdit(BaseModel):
    path: str
    content: str


class FixPlan(BaseModel):
    summary: str
    edits: list[FileEdit]


class State(TypedDict, total=False):
    question: str
    deadline: float
    target: dict
    clarify: str
    base_sha: str | None
    repo_error: str | None
    service_dir: str | None
    triage_report: dict
    messages: list[dict]
    tools_used: list[str]
    budget_hit: bool
    diagnosis_result: dict
    prompt_tokens: int
    completion_tokens: int


@dataclass
class Deps:
    model: ChatModel
    toolbox: Toolbox
    cluster: ClusterTools
    workspace: Workspace
    renderer: Renderer
    store: ProposalStore
    budgets: Budgets = field(default_factory=Budgets)
    clock: Callable[[], float] = time.monotonic


def _usage(state: State, *replies: ModelReply) -> dict:
    return {
        "prompt_tokens": state.get("prompt_tokens", 0) + sum(r.prompt_tokens for r in replies),
        "completion_tokens": state.get("completion_tokens", 0) + sum(r.completion_tokens for r in replies),
    }


def _structured[T: BaseModel](
    model: ChatModel, messages: list[dict], schema: type[T]
) -> tuple[T | None, list[ModelReply]]:
    """Ask for JSON, validate it, and re-ask once on failure."""
    replies = []
    convo = list(messages)
    for _ in range(2):
        reply = model.complete(convo)
        replies.append(reply)
        try:
            return schema.model_validate(parse_json_object(reply.content)), replies
        except (ValueError, ValidationError) as exc:
            error = str(exc).splitlines()[0]
            convo += [reply.as_message(), {"role": "user", "content": prompts.RETRY_JSON.format(error=error)}]
    return None, replies


def _grab(fn: Callable[[], object]) -> object:
    try:
        return fn()
    except Exception as exc:  # triage records every failure as evidence and carries on
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_graph(deps: Deps):
    def check_deadline(state: State) -> None:
        if deps.clock() >= state["deadline"]:
            raise DeadlineExceeded()

    def intake(state: State) -> dict:
        check_deadline(state)
        deps.toolbox.reset()
        inventory = deps.cluster.list_workloads()
        messages = [
            {"role": "system", "content": prompts.INTAKE},
            {
                "role": "user",
                "content": f"Question: {state['question']}\n\n" + prompts.untrusted("workload inventory", inventory),
            },
        ]
        parsed, replies = _structured(deps.model, messages, Intake)
        usage = _usage(state, *replies)
        known = {(w["namespace"], w["name"]): w["kind"] for w in inventory}
        names = ", ".join(f"{ns}/{name}" for ns, name in sorted(known)[:30])
        if parsed is None:
            return {**usage, "clarify": f"Which workload is this about? Known workloads: {names}"}
        if parsed.clarify:
            return {**usage, "clarify": parsed.clarify}
        key = (parsed.namespace, parsed.workload)
        if key not in known:
            return {
                **usage,
                "clarify": f"I could not find {parsed.namespace}/{parsed.workload}. "
                f"Which workload do you mean? Known workloads: {names}",
            }
        target = {"namespace": key[0], "workload": key[1], "kind": known[key], "symptom": parsed.symptom}
        return {**usage, "target": target}

    def triage(state: State) -> dict:
        check_deadline(state)
        t = state["target"]
        ns, name, kind = t["namespace"], t["workload"], t["kind"]
        report: dict = {
            "workload": _grab(lambda: deps.cluster.describe_workload(ns, name, kind)),
            "pods": _grab(lambda: deps.cluster.list_pods(ns, name)),
            "events": _grab(lambda: deps.cluster.events(ns, limit=20)),
            "pod_memory": _grab(lambda: deps.cluster.top_pods(ns)),
            "node_memory": _grab(lambda: deps.cluster.top_nodes()),
        }
        base_sha, repo_error, service_dir = None, None, None
        try:
            base_sha = deps.workspace.sync()
            service_dir = deps.workspace.service_dir(ns)
            if service_dir:
                report["recent_commits"] = _grab(lambda: deps.workspace.log(f"{service_dir}/", 5))
            else:
                report["recent_commits"] = {"error": f"no apps/ directory deploys to namespace {ns}"}
        except RepoError as exc:
            repo_error = str(exc)
            report["recent_commits"] = {"error": f"repository unavailable: {exc}"}
        return {"triage_report": report, "base_sha": base_sha, "repo_error": repo_error, "service_dir": service_dir}

    def investigate(state: State) -> dict:
        check_deadline(state)
        t = state["target"]
        end = min(deps.clock() + deps.budgets.investigate_seconds, state["deadline"])
        messages: list[dict] = [
            {"role": "system", "content": prompts.INVESTIGATE},
            {
                "role": "user",
                "content": (
                    f"Question: {state['question']}\nTarget: {t['namespace']}/{t['workload']} ({t['kind']}), "
                    f"symptom class: {t['symptom']}\n\nTriage already collected:\n"
                    + prompts.untrusted("triage", state["triage_report"])
                ),
            },
        ]
        calls, budget_hit, tools_used, replies = 0, False, [], []
        while True:
            if deps.clock() >= end:
                budget_hit = True
                break
            reply = deps.model.complete(messages, deps.toolbox.specs())
            replies.append(reply)
            messages.append(reply.as_message())
            if not reply.tool_calls:
                break
            for call in reply.tool_calls:
                if calls >= deps.budgets.max_tool_calls or deps.clock() >= end:
                    budget_hit = True
                    content = json.dumps({"error": "budget exhausted; no more tool calls"})
                else:
                    calls += 1
                    tools_used.append(call.name)
                    content = prompts.untrusted(call.name, deps.toolbox.call(call.name, call.arguments))
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            if calls >= deps.budgets.max_tool_calls:
                budget_hit = True
            if budget_hit:
                break
        return {**_usage(state, *replies), "messages": messages, "tools_used": tools_used, "budget_hit": budget_hit}

    def diagnose(state: State) -> dict:
        check_deadline(state)
        messages = state["messages"] + [{"role": "user", "content": prompts.DIAGNOSE}]
        parsed, replies = _structured(deps.model, messages, Diagnosis)
        if parsed is None:
            parsed = Diagnosis(
                cause="The model did not return a valid diagnosis.", confidence="low", evidence=[], fix_kind="unknown"
            )
        result = parsed.model_dump()
        if state.get("budget_hit"):
            result["confidence"] = "low"
            b = deps.budgets
            result["cause"] = (
                f"Budget hit ({b.max_tool_calls} tool calls or {b.investigate_seconds:g} s) "
                f"before the investigation finished. {result['cause']}"
            )
        if deps.toolbox.loki_unavailable:
            result["evidence"].append({"source": "loki", "detail": "Loki was unavailable; Kubernetes logs only."})
        if state.get("repo_error"):
            result["evidence"].append({"source": "repo", "detail": f"Config repo unavailable: {state['repo_error']}"})
        return {**_usage(state, *replies), "diagnosis_result": result}

    def propose_fix(state: State) -> dict:
        check_deadline(state)
        result = dict(state["diagnosis_result"])
        service_dir, base_sha = state["service_dir"], state["base_sha"]
        assert base_sha is not None and service_dir is not None
        replies: list[ModelReply] = []
        reason = "the model returned no usable fix"
        try:
            files = []
            for path in deps.workspace.list_files(f"{service_dir}/"):
                files.append(deps.workspace.read_file(path, max_lines=400))
                if len(json.dumps(files).encode()) > MAX_FILES_BYTES:
                    files.pop()
                    break
            messages = [
                {"role": "system", "content": prompts.PROPOSE},
                {
                    "role": "user",
                    "content": (
                        f"Question: {state['question']}\nDiagnosis: {json.dumps(result)}\n\n"
                        + prompts.untrusted("repository files (line numbers are not part of the content)", files)
                    ),
                },
            ]
            for attempt in range(2):
                plan, new_replies = _structured(deps.model, messages, FixPlan)
                replies += new_replies
                if plan is None:
                    break
                deps.workspace.reset(base_sha)
                for edit in plan.edits:
                    deps.workspace.write_file(edit.path, edit.content)
                diff = deps.workspace.diff()
                if not diff:
                    reason = "the proposed edits changed nothing"
                    break
                service = check_paths(paths_in_diff(diff))
                try:
                    for overlay in overlays_for(deps.workspace.path, service):
                        deps.renderer.render(deps.workspace.path, overlay)
                except RenderError as exc:
                    reason = f"kustomize build failed: {exc.detail[:300]}"
                    if attempt == 0:
                        messages += [
                            {"role": "assistant", "content": plan.model_dump_json()},
                            {
                                "role": "user",
                                "content": prompts.REPAIR + "\n" + prompts.untrusted("kustomize error", exc.detail),
                            },
                        ]
                        continue
                    break
                proposal = deps.store.add(
                    diff=diff,
                    base_sha=base_sha,
                    service=service,
                    summary=plan.summary,
                    question=state["question"],
                    cause=result["cause"],
                    evidence=result["evidence"],
                )
                result.update(fix_summary=plan.summary, proposal_id=proposal.id, diff=diff)
                return {**_usage(state, *replies), "diagnosis_result": result}
        except (RepoError, PathNotAllowed) as exc:
            reason = str(exc)
        finally:
            try:
                deps.workspace.reset(base_sha)
            except RepoError:
                pass
        summary = result.get("fix_summary") or "Config change likely needed"
        result["fix_summary"] = f"{summary} (no diff: {reason})"
        return {**_usage(state, *replies), "diagnosis_result": result}

    def after_intake(state: State) -> str:
        return END if state.get("clarify") else "triage"

    def after_diagnose(state: State) -> str:
        wants_fix = (
            state["diagnosis_result"]["fix_kind"] == "config" and state.get("base_sha") and state.get("service_dir")
        )
        return "propose_fix" if wants_fix else END

    graph = StateGraph(State)
    graph.add_node("intake", intake)
    graph.add_node("triage", triage)
    graph.add_node("investigate", investigate)
    graph.add_node("diagnose", diagnose)
    graph.add_node("propose_fix", propose_fix)
    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", after_intake, ["triage", END])
    graph.add_edge("triage", "investigate")
    graph.add_edge("investigate", "diagnose")
    graph.add_conditional_edges("diagnose", after_diagnose, ["propose_fix", END])
    graph.add_edge("propose_fix", END)
    return graph.compile()


RESULT_KEYS = ("cause", "confidence", "evidence", "fix_kind", "fix_summary", "proposal_id", "diff")


def run_diagnosis(graph, deps: Deps, question: str) -> tuple[dict, dict]:
    """Run the graph. Returns (result for the MCP caller, stats for the log)."""
    final: State = graph.invoke({"question": question, "deadline": deps.clock() + deps.budgets.graph_seconds})
    target = final.get("target", {})
    stats = {
        "namespace": target.get("namespace"),
        "workload": target.get("workload"),
        "symptom": target.get("symptom"),
        "tools": final.get("tools_used", []),
        "calls": len(final.get("tools_used", [])),
        "budget_hit": final.get("budget_hit", False),
        "prompt_tokens": final.get("prompt_tokens", 0),
        "completion_tokens": final.get("completion_tokens", 0),
    }
    if final.get("clarify"):
        return {"clarify": final["clarify"]}, stats
    diagnosis = final["diagnosis_result"]
    return {k: diagnosis[k] for k in RESULT_KEYS if diagnosis.get(k) is not None}, stats
