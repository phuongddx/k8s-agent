"""The diagnose graph: intake -> triage -> investigate -> diagnose -> propose_fix (config only)."""

import contextlib
import json
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError, field_validator

from k8s_agent import prompts
from k8s_agent.diffcheck import PathNotAllowed, check_paths, overlays_for, paths_in_diff
from k8s_agent.llm import ChatModel, ModelReply, ModelTimeout, ModelUnavailable, parse_json_object
from k8s_agent.proposals import ProposalStore
from k8s_agent.render import Renderer, RenderError
from k8s_agent.repo import RepoError, Workspace
from k8s_agent.settings import Budgets
from k8s_agent.tools.cluster import ClusterTools
from k8s_agent.tools.toolbox import Toolbox

MAX_FILES_BYTES = 32768
PROPOSE_RESERVE_SECONDS = 20
MODEL_RESERVE_SECONDS = 5  # a model call must end this long before the graph deadline
MODEL_RETRY_DELAY_SECONDS = 1.0  # wait before the graph's one retry when the server sent no Retry-After
MODEL_RETRY_MIN_SECONDS = 10  # a retry needs at least this much usable time left after the wait
MAX_SUMMARY_CHARS = 72
MAX_REASON_CHARS = 200


class DeadlineExceeded(Exception):
    """The graph passed its overall time limit."""


class NoModelTime(DeadlineExceeded):
    """The graph deadline, not the model, stopped a model call: no time to start one, no time to retry,
    or a call timed out on a timeout that had been cut below the model's own."""


class _OutOfTime(Exception):
    """propose_fix would start work with less than PROPOSE_RESERVE_SECONDS left."""


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

    @field_validator("summary")
    @classmethod
    def _one_short_line(cls, value: str) -> str:
        """The summary becomes a commit and PR title: one line, at most MAX_SUMMARY_CHARS."""
        line = " ".join(value.split())
        if not line:
            raise ValueError("summary must not be empty")
        if len(line) > MAX_SUMMARY_CHARS:
            line = line[: MAX_SUMMARY_CHARS - 1].rstrip() + "\u2026"
        return line


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
    time_hit: bool
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
    model_timeout: float = 40  # per-call timeout when time allows; a call is cut shorter near the deadline
    sleep: Callable[[float], None] = time.sleep


def _usage(state: State, *replies: ModelReply) -> dict:
    return {
        "prompt_tokens": state.get("prompt_tokens", 0) + sum(r.prompt_tokens for r in replies),
        "completion_tokens": state.get("completion_tokens", 0) + sum(r.completion_tokens for r in replies),
    }


def _structured[T: BaseModel](
    complete: Callable[[list[dict]], ModelReply],
    messages: list[dict],
    schema: type[T],
    before_call: Callable[[], None] | None = None,
) -> tuple[T | None, list[ModelReply]]:
    """Ask for JSON, validate it, and re-ask once on failure. `before_call` runs ahead of every model call."""
    replies = []
    convo = list(messages)
    for _ in range(2):
        if before_call:
            before_call()
        reply = complete(convo)
        replies.append(reply)
        try:
            return schema.model_validate(parse_json_object(reply.content)), replies
        except (ValueError, ValidationError) as exc:
            error = str(exc).splitlines()[0]
            convo += [reply.as_message(), {"role": "user", "content": prompts.RETRY_JSON.format(error=error)}]
    return None, replies


def _unfinished_diagnosis(state: State) -> Diagnosis:
    """A low-confidence diagnosis from what investigate gathered, for when the model cannot answer in time."""
    cause = "The model did not finish in time to write a diagnosis."
    findings = next(
        (m["content"] for m in reversed(state["messages"]) if m["role"] == "assistant" and m.get("content")), ""
    )
    if findings:
        cause += f" Last findings from the investigation: {findings[: MAX_REASON_CHARS * 2]}"
    counts = Counter(state.get("tools_used", []))
    tools = ", ".join(f"{name} x{n}" for name, n in counts.items()) or "none"
    return Diagnosis(
        cause=cause,
        confidence="low",
        evidence=[Evidence(source="tools", detail=f"Tools called: {tools}")],
        fix_kind="unknown",
    )


def _grab(fn: Callable[[], object]) -> object:
    try:
        return fn()
    except Exception as exc:  # triage records every failure as evidence and carries on
        return {"error": f"{type(exc).__name__}: {exc}"}


def build_graph(deps: Deps):
    def check_deadline(state: State) -> None:
        if deps.clock() >= state["deadline"]:
            raise DeadlineExceeded()

    def complete_within_deadline(state: State, messages: list[dict], tools: list[dict] | None = None) -> ModelReply:
        """One model call that ends before the graph deadline, with at most one graph-level retry.

        The SDK's own retry is off: it could sleep through a long Retry-After past the deadline. Each failure
        is classified first. NoModelTime means the deadline stopped us: no usable time, or a timeout that had
        been cut below the model's own. Anything else is the model's fault and becomes ModelUnavailable. A
        transient failure (timeout, connection error, 429, 5xx) is retried once, only when the wait plus a
        real attempt still fit; a failed retry is classified the same way."""

        def usable() -> float:
            return state["deadline"] - deps.clock() - MODEL_RESERVE_SECONDS

        for attempt in (1, 2):
            if usable() <= 0:
                raise NoModelTime()
            timeout = min(deps.model_timeout, usable())
            try:
                return deps.model.complete(messages, tools, timeout=timeout, max_retries=0)
            except ModelUnavailable as exc:
                if isinstance(exc, ModelTimeout) and timeout < deps.model_timeout:
                    raise NoModelTime() from exc
                if exc.retryable and attempt == 1:
                    delay = MODEL_RETRY_DELAY_SECONDS if exc.retry_after is None else exc.retry_after
                    if usable() - delay >= MODEL_RETRY_MIN_SECONDS:
                        deps.sleep(delay)
                        continue
                if isinstance(exc, ModelTimeout):
                    raise ModelUnavailable(str(exc)) from exc
                raise
        raise AssertionError("unreachable")

    def intake(state: State) -> dict:
        check_deadline(state)
        deps.toolbox.reset()
        inventory = deps.cluster.list_workloads()
        messages = [
            {"role": "system", "content": prompts.INTAKE},
            {
                "role": "user",
                "content": f"Question: {state['question']}\n\n" + prompts.untrusted("workload-inventory", inventory),
            },
        ]
        parsed, replies = _structured(
            lambda convo: complete_within_deadline(state, convo),
            messages,
            Intake,
            before_call=lambda: check_deadline(state),
        )
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
        report: dict = {}
        # A check ahead of every network or git call: none starts after the deadline. A call already
        # running at the deadline is not interrupted. The checks stay outside _grab, which would swallow them.
        for key, fetch in (
            ("workload", lambda: deps.cluster.describe_workload(ns, name, kind)),
            ("pods", lambda: deps.cluster.list_pods(ns, name)),
            ("events", lambda: deps.cluster.events(ns, limit=20)),
            ("pod_memory", lambda: deps.cluster.top_pods(ns)),
            ("node_memory", lambda: deps.cluster.top_nodes()),
        ):
            check_deadline(state)
            report[key] = _grab(fetch)
        base_sha, repo_error, service_dir = None, None, None
        check_deadline(state)
        try:
            base_sha = deps.workspace.sync()
            service_dir = deps.workspace.service_dir(ns)
            if service_dir:
                check_deadline(state)
                report["recent_commits"] = _grab(lambda: deps.workspace.log(f"{service_dir}/", 5))
            else:
                report["recent_commits"] = {"error": f"no apps/ directory deploys to namespace {ns}"}
        except (RepoError, OSError) as exc:
            repo_error = str(exc)
            report["recent_commits"] = {"error": f"repository unavailable: {exc}"}
        return {"triage_report": report, "base_sha": base_sha, "repo_error": repo_error, "service_dir": service_dir}

    def investigate(state: State) -> dict:
        check_deadline(state)
        t = state["target"]
        start = deps.clock()
        end = min(start + deps.budgets.investigate_seconds, state["deadline"])
        deadline_ends_it = state["deadline"] <= start + deps.budgets.investigate_seconds
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
        calls, tools_used, replies = 0, [], []
        limits = {"budget_hit": False, "time_hit": False}

        def hit_limit() -> None:
            """Time ran out: the graph deadline is a time hit, the investigate budget a budget hit."""
            limits["time_hit" if deadline_ends_it else "budget_hit"] = True

        while True:
            if deps.clock() >= end:
                hit_limit()
                break
            try:
                reply = complete_within_deadline(state, messages, deps.toolbox.specs())
            except NoModelTime:  # no time for a model call: diagnose with what is gathered
                limits["time_hit"] = True
                break
            replies.append(reply)
            messages.append(reply.as_message())
            if not reply.tool_calls:
                break
            for call in reply.tool_calls:
                if calls >= deps.budgets.max_tool_calls:
                    limits["budget_hit"] = True
                    content = json.dumps({"error": "budget exhausted; no more tool calls"})
                elif deps.clock() >= end:
                    hit_limit()
                    content = json.dumps({"error": "budget exhausted; no more tool calls"})
                else:
                    calls += 1
                    label = deps.toolbox.label(call.name)
                    tools_used.append(label)
                    content = prompts.untrusted(label, deps.toolbox.call(call.name, call.arguments))
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
            if calls >= deps.budgets.max_tool_calls:
                limits["budget_hit"] = True
            if limits["budget_hit"] or limits["time_hit"]:
                break
        return {**_usage(state, *replies), "messages": messages, "tools_used": tools_used, **limits}

    def diagnose(state: State) -> dict:
        check_deadline(state)
        messages = state["messages"] + [{"role": "user", "content": prompts.DIAGNOSE}]
        time_hit = state.get("time_hit", False)
        try:
            parsed, replies = _structured(
                lambda convo: complete_within_deadline(state, convo),
                messages,
                Diagnosis,
                before_call=lambda: check_deadline(state),
            )
        except NoModelTime:
            parsed, replies = _unfinished_diagnosis(state), []
            time_hit = True
        if parsed is None:
            parsed = Diagnosis(
                cause="The model did not return a valid diagnosis.", confidence="low", evidence=[], fix_kind="unknown"
            )
        result = parsed.model_dump()
        budget_hit = state.get("budget_hit", False)
        if budget_hit or time_hit:
            result["confidence"] = "low"
            b = deps.budgets
            budget = f"Budget hit ({b.max_tool_calls} tool calls or {b.investigate_seconds:g} s)"
            if budget_hit and time_hit:
                prefix = f"{budget} and the time limit was reached before the investigation finished."
            elif budget_hit:
                prefix = f"{budget} before the investigation finished."
            else:
                prefix = "Time limit reached before the investigation finished;"
            result["cause"] = f"{prefix} {result['cause']}"
        if deps.toolbox.loki_unavailable:
            result["evidence"].append({"source": "loki", "detail": "Loki was unavailable; Kubernetes logs only."})
        if state.get("repo_error"):
            result["evidence"].append({"source": "repo", "detail": f"Config repo unavailable: {state['repo_error']}"})
        return {**_usage(state, *replies), "diagnosis_result": result, "time_hit": time_hit}

    def propose_fix(state: State) -> dict:
        result = dict(state["diagnosis_result"])
        service_dir, base_sha = state["service_dir"], state["base_sha"]
        assert base_sha is not None and service_dir is not None

        def keep_time() -> None:
            if state["deadline"] - deps.clock() < PROPOSE_RESERVE_SECONDS:
                raise _OutOfTime()

        def without_diff(reason: str, replies: list[ModelReply]) -> dict:
            summary = result.get("fix_summary") or "Config change likely needed"
            result["fix_summary"] = f"{summary} (no diff: {reason})"
            return {**_usage(state, *replies), "diagnosis_result": result}

        try:
            keep_time()
        except _OutOfTime:
            return without_diff("not enough time left to prepare a fix", [])
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
                        "Line numbers in the files below are not part of the content.\n"
                        + prompts.untrusted("repository-files", files)
                    ),
                },
            ]
            for attempt in range(2):
                plan, new_replies = _structured(
                    lambda convo: complete_within_deadline(state, convo), messages, FixPlan, before_call=keep_time
                )
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
                if f"apps/{service}" != service_dir:
                    reason = "fix touched a different service than the one diagnosed"
                    break
                overlays = overlays_for(deps.workspace.path, service)
                if not overlays:
                    reason = f"no kustomize overlay for apps/{service}"
                    break
                try:
                    for overlay in overlays:
                        keep_time()
                        deps.renderer.render(deps.workspace.path, overlay)
                except RenderError as exc:
                    reason = f"kustomize build failed: {exc.detail[:300]}"
                    if attempt == 0:
                        messages += [
                            {"role": "assistant", "content": plan.model_dump_json()},
                            {
                                "role": "user",
                                "content": prompts.REPAIR + "\n" + prompts.untrusted("kustomize-error", exc.detail),
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
        except (_OutOfTime, NoModelTime):
            reason = "not enough time left to prepare a fix"
        except (RepoError, PathNotAllowed) as exc:
            reason = str(exc)[:MAX_REASON_CHARS]
        except ModelUnavailable:
            reason = "the model was unavailable while preparing the fix"
        except Exception as exc:  # a failed fix must never discard the finished diagnosis
            reason = f"unexpected {type(exc).__name__} while preparing the fix"
        finally:
            with contextlib.suppress(Exception):  # a failed cleanup must not mask the result; sync() resets again
                deps.workspace.reset(base_sha)
        return without_diff(reason, replies)

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
        "time_hit": final.get("time_hit", False),
        "prompt_tokens": final.get("prompt_tokens", 0),
        "completion_tokens": final.get("completion_tokens", 0),
    }
    if final.get("clarify"):
        return {"clarify": final["clarify"]}, stats
    diagnosis = final["diagnosis_result"]
    return {k: diagnosis[k] for k in RESULT_KEYS if diagnosis.get(k) is not None}, stats
