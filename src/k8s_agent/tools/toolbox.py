"""The fifteen tools the investigate step may call: schemas for the model and one dispatcher.

Errors (forbidden, not found, bad arguments, Loki down) become the tool result, so the model
can adapt; they never abort the diagnosis."""

import json
import logging
import time
from collections.abc import Callable

from k8s_agent.kube import KubeError
from k8s_agent.logs import log_event
from k8s_agent.repo import RepoError, Workspace
from k8s_agent.tools.cluster import ClusterTools
from k8s_agent.tools.loki import LokiClient, LokiQueryError, LokiUnavailable

_S = {"type": "string"}
_I = {"type": "integer"}
_B = {"type": "boolean"}


def _spec(name: str, description: str, properties: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)},
        },
    }


TOOL_SPECS = [
    _spec(
        "list_workloads", "Deployments, StatefulSets and DaemonSets with desired and ready replicas.", {"namespace": _S}
    ),
    _spec(
        "describe_workload",
        "Replicas, images, resources, selector and conditions of one workload.",
        {"namespace": _S, "name": _S, "kind": {"type": "string", "enum": ["Deployment", "StatefulSet", "DaemonSet"]}},
        ("namespace", "name"),
    ),
    _spec(
        "list_pods",
        "Pods with phase, restarts, container state and last termination reason and exit code.",
        {"namespace": _S, "workload": _S},
        ("namespace",),
    ),
    _spec(
        "describe_pod",
        "One pod: state, resources, owners, failing conditions.",
        {"namespace": _S, "name": _S},
        ("namespace", "name"),
    ),
    _spec(
        "pod_logs",
        "Last log lines of a pod (at most 200 lines or 32 KB), with timestamps. "
        "previous=true reads the previous (crashed) container.",
        {"namespace": _S, "pod": _S, "container": _S, "previous": _B, "lines": _I},
        ("namespace", "pod"),
    ),
    _spec(
        "events",
        "The last 20 events in a namespace, optionally only for objects whose name starts with `involved`.",
        {"namespace": _S, "involved": _S},
        ("namespace",),
    ),
    _spec(
        "top_pods",
        "CPU (millicores) and memory (MiB) per pod in a namespace, from metrics.k8s.io.",
        {"namespace": _S},
        ("namespace",),
    ),
    _spec("top_nodes", "Node CPU and memory usage against allocatable memory."),
    _spec(
        "rollout_status",
        "Whether a rollout is complete, with conditions and ReplicaSet revisions.",
        {"namespace": _S, "name": _S, "kind": _S},
        ("namespace", "name"),
    ),
    _spec("get_ingressroutes", "Traefik IngressRoutes: host match rules, services and middlewares.", {"namespace": _S}),
    _spec(
        "get_endpoints",
        "Ready and not-ready endpoint addresses of a Service.",
        {"namespace": _S, "name": _S},
        ("namespace", "name"),
    ),
    _spec(
        "get_configmap",
        "Data of one ConfigMap. Secrets are not readable.",
        {"namespace": _S, "name": _S},
        ("namespace", "name"),
    ),
    _spec(
        "loki_query",
        "LogQL query over Loki (7-day retention). Window `hours` defaults to 1, max 168. "
        'Traefik access logs: {namespace="kube-system", app="traefik"}.',
        {"query": _S, "hours": _I, "limit": _I},
        ("query",),
    ),
    _spec(
        "read_repo_file",
        "A file from the config repo with line numbers, for path:line citations.",
        {"path": _S, "start_line": _I, "max_lines": _I},
        ("path",),
    ),
    _spec(
        "repo_log",
        "Recent commits touching a path in the config repo, e.g. apps/<service>/.",
        {"path": _S, "limit": _I},
        ("path",),
    ),
]


class Toolbox:
    def __init__(self, cluster: ClusterTools, loki: LokiClient, workspace: Workspace, max_bytes: int = 32768) -> None:
        self._max_bytes = max_bytes
        self.loki_unavailable = False
        self._registry: dict[str, Callable[..., object]] = {
            **cluster.registry(),
            "loki_query": loki.query,
            "read_repo_file": workspace.read_file,
            "repo_log": workspace.log,
        }

    def specs(self) -> list[dict]:
        return TOOL_SPECS

    def reset(self) -> None:
        self.loki_unavailable = False

    def call(self, name: str, arguments: dict) -> str:
        started = time.monotonic()
        status = "ok"
        fn = self._registry.get(name)
        try:
            if fn is None:
                if "secret" in name.lower():
                    log_event("tool.secret_request", logging.WARNING, tool=name)
                raise LookupError(f"unknown tool {name}")
            result: object = fn(**arguments)
        except KubeError as exc:
            status, result = "error", {"error": f"Kubernetes API {exc.status}: {exc.message}"}
        except LokiUnavailable as exc:
            self.loki_unavailable = True
            status, result = "error", {"error": f"{exc}. Loki is unavailable; use pod_logs instead."}
        except (LokiQueryError, RepoError, LookupError, ValueError, TypeError) as exc:
            status, result = "error", {"error": str(exc)}
        text = json.dumps(result, default=str, ensure_ascii=False)
        if len(text.encode()) > self._max_bytes:
            text = text.encode()[: self._max_bytes].decode(errors="ignore") + " ...[truncated]"
        log_event("tool.call", tool=name, status=status, duration_ms=round((time.monotonic() - started) * 1000))
        return text
