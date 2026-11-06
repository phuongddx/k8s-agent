"""Fixed, read-only cluster tools. Each returns a compact summary, never a raw object,
so the model sees the fields that matter and the token count stays small."""

from collections.abc import Callable

from k8s_agent.kube import KubeApi, KubeError

WORKLOAD_PLURALS = {"Deployment": "deployments", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets"}
METRICS = "/apis/metrics.k8s.io/v1beta1"
TRAEFIK = "/apis/traefik.io/v1alpha1"
_BINARY = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}
_DECIMAL = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def parse_quantity(quantity: str) -> float:
    for suffix, factor in _BINARY.items():
        if quantity.endswith(suffix):
            return float(quantity[: -len(suffix)]) * factor
    if quantity and quantity[-1] in _DECIMAL:
        return float(quantity[:-1]) * _DECIMAL[quantity[-1]]
    return float(quantity)


def mem_mi(quantity: str | None) -> int | None:
    return None if quantity is None else round(parse_quantity(quantity) / 2**20)


def cpu_m(quantity: str | None) -> int | None:
    return None if quantity is None else round(parse_quantity(quantity) * 1000)


def _time_of(obj: dict) -> str:
    return obj.get("lastTimestamp") or obj.get("eventTime") or obj["metadata"].get("creationTimestamp") or ""


def _container_state(status: dict) -> dict:
    state = status.get("state", {})
    out: dict = {
        "name": status["name"],
        "ready": status.get("ready", False),
        "restarts": status.get("restartCount", 0),
        "image": status.get("image"),
    }
    if "waiting" in state:
        out["state"] = "waiting"
        out["reason"] = state["waiting"].get("reason")
        out["message"] = state["waiting"].get("message")
    elif "terminated" in state:
        out["state"] = "terminated"
        out["reason"] = state["terminated"].get("reason")
        out["exit_code"] = state["terminated"].get("exitCode")
    else:
        out["state"] = "running"
    last = status.get("lastState", {}).get("terminated")
    if last:
        out["last_termination"] = {
            "reason": last.get("reason"),
            "exit_code": last.get("exitCode"),
            "finished_at": last.get("finishedAt"),
        }
    return out


def _resources(spec: dict) -> list[dict]:
    return [
        {
            "container": c["name"],
            "image": c.get("image"),
            "requests": c.get("resources", {}).get("requests", {}),
            "limits": c.get("resources", {}).get("limits", {}),
        }
        for c in spec.get("containers", [])
    ]


def _labels_match(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in selector.items())


class ClusterTools:
    def __init__(self, api: KubeApi, log_max_lines: int = 200, log_max_bytes: int = 32768) -> None:
        self._api = api
        self._log_max_lines = log_max_lines
        self._log_max_bytes = log_max_bytes

    def _workload(self, namespace: str, name: str, kind: str | None) -> tuple[str, dict]:
        kinds = [kind] if kind else list(WORKLOAD_PLURALS)
        for k in kinds:
            if k not in WORKLOAD_PLURALS:
                raise ValueError(f"kind must be one of {sorted(WORKLOAD_PLURALS)}")
            try:
                return k, self._api.get_json(f"/apis/apps/v1/namespaces/{namespace}/{WORKLOAD_PLURALS[k]}/{name}")
            except KubeError as exc:
                if exc.status != 404:
                    raise
        raise KubeError(404, f"{namespace}/{name}", f"no Deployment, StatefulSet or DaemonSet named {name}")

    def list_workloads(self, namespace: str | None = None) -> list[dict]:
        out = []
        for kind, plural in WORKLOAD_PLURALS.items():
            for item in self._api.get_json(f"/apis/apps/v1/{plural}").get("items", []):
                meta, status = item["metadata"], item.get("status", {})
                if namespace and meta["namespace"] != namespace:
                    continue
                desired = item.get("spec", {}).get("replicas", status.get("desiredNumberScheduled"))
                ready = status.get("readyReplicas", status.get("numberReady", 0))
                out.append(
                    {
                        "namespace": meta["namespace"],
                        "name": meta["name"],
                        "kind": kind,
                        "desired": desired,
                        "ready": ready,
                    }
                )
        return sorted(out, key=lambda w: (w["namespace"], w["name"]))

    def describe_workload(self, namespace: str, name: str, kind: str | None = None) -> dict:
        kind, obj = self._workload(namespace, name, kind)
        spec, status = obj.get("spec", {}), obj.get("status", {})
        return {
            "kind": kind,
            "namespace": namespace,
            "name": name,
            "desired": spec.get("replicas", status.get("desiredNumberScheduled")),
            "ready": status.get("readyReplicas", status.get("numberReady", 0)),
            "updated": status.get("updatedReplicas", status.get("updatedNumberScheduled", 0)),
            "available": status.get("availableReplicas", status.get("numberAvailable", 0)),
            "generation": obj["metadata"].get("generation"),
            "observed_generation": status.get("observedGeneration"),
            "selector": spec.get("selector", {}).get("matchLabels", {}),
            "containers": _resources(spec.get("template", {}).get("spec", {})),
            "conditions": [
                {k: c.get(k) for k in ("type", "status", "reason", "message")} for c in status.get("conditions", [])
            ],
        }

    def _pod_summary(self, pod: dict) -> dict:
        status = pod.get("status", {})
        containers = [_container_state(c) for c in status.get("containerStatuses", [])]
        return {
            "name": pod["metadata"]["name"],
            "phase": status.get("phase"),
            "node": pod.get("spec", {}).get("nodeName"),
            "restarts": sum(c["restarts"] for c in containers),
            "containers": containers,
            "problems": [
                {k: c.get(k) for k in ("type", "reason", "message")}
                for c in status.get("conditions", [])
                if c.get("status") == "False"
            ],
        }

    def list_pods(self, namespace: str, workload: str | None = None) -> list[dict]:
        selector: dict[str, str] = {}
        params = None
        if workload:
            selector = self.describe_workload(namespace, workload)["selector"]
            params = {"labelSelector": ",".join(f"{k}={v}" for k, v in selector.items())}
        pods = self._api.get_json(f"/api/v1/namespaces/{namespace}/pods", params).get("items", [])
        return [self._pod_summary(p) for p in pods if _labels_match(selector, p["metadata"].get("labels", {}))]

    def describe_pod(self, namespace: str, name: str) -> dict:
        pod = self._api.get_json(f"/api/v1/namespaces/{namespace}/pods/{name}")
        summary = self._pod_summary(pod)
        summary["resources"] = _resources(pod.get("spec", {}))
        summary["owners"] = [f"{o['kind']}/{o['name']}" for o in pod["metadata"].get("ownerReferences", [])]
        return summary

    def pod_logs(
        self, namespace: str, pod: str, container: str | None = None, previous: bool = False, lines: int | None = None
    ) -> dict:
        max_lines = min(lines or self._log_max_lines, self._log_max_lines)
        params: dict[str, str | int | bool] = {
            "tailLines": max_lines,
            "limitBytes": self._log_max_bytes,
            "timestamps": True,
            "previous": previous,
        }
        if container:
            params["container"] = container
        text = self._api.get_text(f"/api/v1/namespaces/{namespace}/pods/{pod}/log", params)
        kept = text.splitlines()[-max_lines:]
        body = "\n".join(kept)
        truncated = len(kept) < len(text.splitlines())
        while len(body.encode()) > self._log_max_bytes:
            kept = kept[1:]
            body = "\n".join(kept)
            truncated = True
        return {
            "pod": pod,
            "container": container,
            "previous": previous,
            "lines": len(kept),
            "truncated": truncated,
            "log": body,
        }

    def events(self, namespace: str, involved: str | None = None, limit: int = 20) -> list[dict]:
        items = self._api.get_json(f"/api/v1/namespaces/{namespace}/events").get("items", [])
        if involved:
            items = [e for e in items if e.get("involvedObject", {}).get("name", "").startswith(involved)]
        items.sort(key=_time_of)
        return [
            {
                "time": _time_of(e),
                "type": e.get("type"),
                "reason": e.get("reason"),
                "object": f"{e['involvedObject'].get('kind')}/{e['involvedObject'].get('name')}",
                "message": e.get("message"),
                "count": e.get("count", 1),
            }
            for e in items[-min(limit, 20) :]
        ]

    def top_pods(self, namespace: str) -> list[dict]:
        items = self._api.get_json(f"{METRICS}/namespaces/{namespace}/pods").get("items", [])
        return [
            {
                "pod": m["metadata"]["name"],
                "cpu_m": sum(cpu_m(c["usage"]["cpu"]) or 0 for c in m.get("containers", [])),
                "memory_mi": sum(mem_mi(c["usage"]["memory"]) or 0 for c in m.get("containers", [])),
            }
            for m in items
        ]

    def top_nodes(self) -> list[dict]:
        usage = {m["metadata"]["name"]: m["usage"] for m in self._api.get_json(f"{METRICS}/nodes").get("items", [])}
        out = []
        for node in self._api.get_json("/api/v1/nodes").get("items", []):
            name = node["metadata"]["name"]
            allocatable = mem_mi(node.get("status", {}).get("allocatable", {}).get("memory"))
            used = mem_mi(usage.get(name, {}).get("memory"))
            out.append(
                {
                    "node": name,
                    "cpu_m": cpu_m(usage.get(name, {}).get("cpu")),
                    "memory_mi": used,
                    "allocatable_memory_mi": allocatable,
                    "memory_percent": round(100 * used / allocatable) if used and allocatable else None,
                }
            )
        return out

    def rollout_status(self, namespace: str, name: str, kind: str | None = None) -> dict:
        workload = self.describe_workload(namespace, name, kind)
        desired = workload["desired"] or 0
        complete = (
            workload["observed_generation"] == workload["generation"]
            and workload["updated"] == desired
            and workload["ready"] == desired
        )
        replicasets = []
        if workload["kind"] == "Deployment":
            items = self._api.get_json(f"/apis/apps/v1/namespaces/{namespace}/replicasets").get("items", [])
            for rs in items:
                if not any(o.get("name") == name for o in rs["metadata"].get("ownerReferences", [])):
                    continue
                replicasets.append(
                    {
                        "name": rs["metadata"]["name"],
                        "revision": rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision"),
                        "replicas": rs.get("status", {}).get("replicas", 0),
                        "ready": rs.get("status", {}).get("readyReplicas", 0),
                        "images": [
                            c.get("image")
                            for c in rs.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                        ],
                    }
                )
            replicasets.sort(key=lambda r: int(r["revision"] or 0))
        return {
            "kind": workload["kind"],
            "name": name,
            "complete": complete,
            "desired": desired,
            "updated": workload["updated"],
            "ready": workload["ready"],
            "conditions": workload["conditions"],
            "replicasets": replicasets,
        }

    def get_ingressroutes(self, namespace: str | None = None) -> list[dict]:
        path = f"{TRAEFIK}/namespaces/{namespace}/ingressroutes" if namespace else f"{TRAEFIK}/ingressroutes"
        out = []
        for item in self._api.get_json(path).get("items", []):
            spec = item.get("spec", {})
            out.append(
                {
                    "namespace": item["metadata"]["namespace"],
                    "name": item["metadata"]["name"],
                    "entry_points": spec.get("entryPoints", []),
                    "routes": [
                        {
                            "match": r.get("match"),
                            "services": [f"{s.get('name')}:{s.get('port')}" for s in r.get("services", [])],
                            "middlewares": [m.get("name") for m in r.get("middlewares", [])],
                        }
                        for r in spec.get("routes", [])
                    ],
                }
            )
        return out

    def get_endpoints(self, namespace: str, name: str) -> dict:
        obj = self._api.get_json(f"/api/v1/namespaces/{namespace}/endpoints/{name}")
        ready, not_ready = [], []
        for subset in obj.get("subsets", []) or []:
            ports = [p.get("port") for p in subset.get("ports", [])]
            for key, bucket in (("addresses", ready), ("notReadyAddresses", not_ready)):
                for a in subset.get(key, []) or []:
                    bucket.append({"ip": a.get("ip"), "pod": a.get("targetRef", {}).get("name"), "ports": ports})
        return {"namespace": namespace, "name": name, "ready": ready, "not_ready": not_ready}

    def get_configmap(self, namespace: str, name: str) -> dict:
        obj = self._api.get_json(f"/api/v1/namespaces/{namespace}/configmaps/{name}")
        return {"namespace": namespace, "name": name, "data": obj.get("data", {})}

    def registry(self) -> dict[str, Callable[..., object]]:
        return {
            "list_workloads": self.list_workloads,
            "describe_workload": self.describe_workload,
            "list_pods": self.list_pods,
            "describe_pod": self.describe_pod,
            "pod_logs": self.pod_logs,
            "events": self.events,
            "top_pods": self.top_pods,
            "top_nodes": self.top_nodes,
            "rollout_status": self.rollout_status,
            "get_ingressroutes": self.get_ingressroutes,
            "get_endpoints": self.get_endpoints,
            "get_configmap": self.get_configmap,
        }
