"""Hand-built cluster snapshots shaped like vps192 workloads, one per runbook symptom.

Used by the unit tests and by the manual eval in evals/run_eval.py. Values are synthetic."""

from dataclasses import dataclass, field

NODE = "node-1"
NODE_ALLOCATABLE = "7935Mi"
SHA_API = "4f1d2c3b" * 5
SHA_GW = "7e6d5c4b" * 5
SHA_FE = "2b3c4d5e" * 5


@dataclass
class Scenario:
    name: str
    question: str
    namespace: str
    workload: str
    symptom: str
    expect_fix_kind: str
    expect_cause: list[str]
    snapshot: dict[str, object]
    repo_files: dict[str, str]
    loki_lines: list[tuple[str, str]] = field(default_factory=list)


def deployment(
    ns,
    name,
    image,
    replicas=1,
    ready=1,
    updated=None,
    mem_request="128Mi",
    mem_limit="256Mi",
    conditions=(),
    generation=1,
    observed=1,
):
    return {
        "kind": "Deployment",
        "metadata": {"namespace": ns, "name": name, "generation": generation},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": name,
                            "image": image,
                            "resources": {"requests": {"memory": mem_request}, "limits": {"memory": mem_limit}},
                        }
                    ]
                }
            },
        },
        "status": {
            "replicas": replicas,
            "readyReplicas": ready,
            "availableReplicas": ready,
            "updatedReplicas": replicas if updated is None else updated,
            "observedGeneration": observed,
            "conditions": list(conditions),
        },
    }


def pod(
    ns,
    name,
    app,
    image,
    phase="Running",
    ready=True,
    restarts=0,
    waiting=None,
    last=None,
    problems=(),
    mem_request="128Mi",
    mem_limit="256Mi",
    node=NODE,
):
    state = {"waiting": waiting} if waiting else {"running": {"startedAt": "2026-11-05T09:00:00Z"}}
    status = {"name": app, "ready": ready, "restartCount": restarts, "image": image, "state": state}
    if last:
        status["lastState"] = {"terminated": last}
    return {
        "metadata": {
            "namespace": ns,
            "name": name,
            "labels": {"app": app},
            "ownerReferences": [{"kind": "ReplicaSet", "name": f"{app}-6d5f7"}],
        },
        "spec": {
            "nodeName": node if phase != "Pending" else None,
            "containers": [
                {
                    "name": app,
                    "image": image,
                    "resources": {"requests": {"memory": mem_request}, "limits": {"memory": mem_limit}},
                }
            ],
        },
        "status": {
            "phase": phase,
            "containerStatuses": [] if phase == "Pending" else [status],
            "conditions": list(problems),
        },
    }


def event(ns, kind, name, reason, message, ts, type_="Warning", count=1):
    return {
        "metadata": {"namespace": ns, "name": f"{name}.{reason.lower()}", "creationTimestamp": ts},
        "involvedObject": {"kind": kind, "name": name, "namespace": ns},
        "reason": reason,
        "message": message,
        "type": type_,
        "count": count,
        "lastTimestamp": ts,
    }


def build_snapshot(
    workloads,
    pods,
    events=(),
    pod_usage=None,
    node_used="4800Mi",
    replicasets=(),
    ingressroutes=(),
    endpoints=(),
    configmaps=(),
    logs=None,
):
    """Return {api path: object} for every path ClusterTools reads."""
    snap: dict[str, object] = {}
    namespaces = sorted({w["metadata"]["namespace"] for w in workloads})
    for kind, plural in (("Deployment", "deployments"), ("StatefulSet", "statefulsets"), ("DaemonSet", "daemonsets")):
        items = [w for w in workloads if w["kind"] == kind]
        snap[f"/apis/apps/v1/{plural}"] = {"items": items}
        for w in items:
            snap[f"/apis/apps/v1/namespaces/{w['metadata']['namespace']}/{plural}/{w['metadata']['name']}"] = w
    for ns in namespaces:
        ns_pods = [p for p in pods if p["metadata"]["namespace"] == ns]
        snap[f"/api/v1/namespaces/{ns}/pods"] = {"items": ns_pods}
        for p in ns_pods:
            snap[f"/api/v1/namespaces/{ns}/pods/{p['metadata']['name']}"] = p
        snap[f"/api/v1/namespaces/{ns}/events"] = {"items": [e for e in events if e["metadata"]["namespace"] == ns]}
        usage = (pod_usage or {}).get(ns, {})
        snap[f"/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods"] = {
            "items": [
                {"metadata": {"name": name}, "containers": [{"name": "main", "usage": {"cpu": "20m", "memory": mem}}]}
                for name, mem in usage.items()
            ]
        }
        snap[f"/apis/apps/v1/namespaces/{ns}/replicasets"] = {
            "items": [r for r in replicasets if r["metadata"]["namespace"] == ns]
        }
        snap[f"/apis/traefik.io/v1alpha1/namespaces/{ns}/ingressroutes"] = {
            "items": [r for r in ingressroutes if r["metadata"]["namespace"] == ns]
        }
    snap["/apis/traefik.io/v1alpha1/ingressroutes"] = {"items": list(ingressroutes)}
    snap["/api/v1/nodes"] = {
        "items": [{"metadata": {"name": NODE}, "status": {"allocatable": {"memory": NODE_ALLOCATABLE}}}]
    }
    snap["/apis/metrics.k8s.io/v1beta1/nodes"] = {
        "items": [{"metadata": {"name": NODE}, "usage": {"cpu": "900m", "memory": node_used}}]
    }
    for ep in endpoints:
        snap[f"/api/v1/namespaces/{ep['metadata']['namespace']}/endpoints/{ep['metadata']['name']}"] = ep
    for cm in configmaps:
        snap[f"/api/v1/namespaces/{cm['metadata']['namespace']}/configmaps/{cm['metadata']['name']}"] = cm
    for (ns, pod_name, previous), text in (logs or {}).items():
        path = f"/api/v1/namespaces/{ns}/pods/{pod_name}/log"
        snap[f"{path}?previous=true" if previous else path] = text
    return snap


OPENCLAW = deployment(
    "openclaw", "openclaw", "ghcr.io/openclaw/openclaw@sha256:" + "a" * 64, mem_request="1Gi", mem_limit="2560Mi"
)
OPENCLAW_POD = pod("openclaw", "openclaw-7c9d8-abcde", "openclaw", "ghcr.io/openclaw/openclaw@sha256:" + "a" * 64)


def _kustomization(svc: str, image: str, tag: str, namespace: str | None = None) -> str:
    return (
        f"apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: {namespace or svc}\n"
        f"resources:\n  - ../base\n  - configmap.yaml\nimages:\n  - name: {image}\n    newTag: {tag}\n"
    )


def _base(svc: str, image: str, mem_limit: str = "256Mi", mem_request: str = "128Mi") -> dict[str, str]:
    return {
        f"apps/{svc}/base/kustomization.yaml": "resources:\n  - deployment.yaml\n",
        f"apps/{svc}/base/deployment.yaml": (
            f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {svc}\nspec:\n  replicas: 1\n"
            f"  selector:\n    matchLabels:\n      app: {svc}\n  template:\n    metadata:\n      labels:\n"
            f"        app: {svc}\n    spec:\n      containers:\n        - name: {svc}\n          image: {image}\n"
            f"          envFrom:\n            - configMapRef:\n                name: {svc}-config\n"
            f"          resources:\n            requests:\n              memory: {mem_request}\n"
            f"            limits:\n              memory: {mem_limit}\n"
        ),
    }


def crashloop() -> Scenario:
    ns = name = "dropitx-api"
    image = "ghcr.io/dropitx-io/dropitx-api:" + SHA_API
    p = pod(
        ns,
        "dropitx-api-6d5f7-x2k9p",
        name,
        image,
        ready=False,
        restarts=14,
        waiting={"reason": "CrashLoopBackOff", "message": "back-off 5m0s restarting failed container"},
        last={"reason": "Error", "exitCode": 1, "finishedAt": "2026-11-05T09:58:12Z"},
    )
    return Scenario(
        name="crashloop",
        question="why is dropitx-api crashlooping?",
        namespace=ns,
        workload=name,
        symptom="crashloop",
        expect_fix_kind="config",
        expect_cause=["DATABASE_URL"],
        snapshot=build_snapshot(
            [deployment(ns, name, image, ready=0), OPENCLAW],
            [p, OPENCLAW_POD],
            events=[
                event(
                    ns,
                    "Pod",
                    p["metadata"]["name"],
                    "BackOff",
                    "Back-off restarting failed container dropitx-api",
                    "2026-11-05T09:58:30Z",
                    count=60,
                )
            ],
            pod_usage={ns: {p["metadata"]["name"]: "40Mi"}},
            configmaps=[
                {
                    "metadata": {"namespace": ns, "name": "dropitx-api-config"},
                    "data": {"PORT": "8080", "LOG_LEVEL": "info"},
                }
            ],
            logs={
                (ns, p["metadata"]["name"], True): "2026-11-05T09:58:11.902Z starting dropitx-api\n"
                "2026-11-05T09:58:12.004Z FATAL config: DATABASE_URL is not set\n"
            },
        ),
        repo_files={
            **_base(name, "ghcr.io/dropitx-io/dropitx-api"),
            f"apps/{name}/vps192/kustomization.yaml": _kustomization(name, "ghcr.io/dropitx-io/dropitx-api", SHA_API),
            f"apps/{name}/vps192/configmap.yaml": (
                "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dropitx-api-config\ndata:\n"
                '  PORT: "8080"\n  LOG_LEVEL: info\n'
            ),
        },
    )


def oomkilled() -> Scenario:
    ns = name = "llm-gateway"
    image = "ghcr.io/phuongddx/llm-gateway:" + SHA_GW
    p = pod(
        ns,
        "llm-gateway-5b8c4-q7w2e",
        name,
        image,
        restarts=6,
        last={"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-11-05T09:41:03Z"},
        mem_request="64Mi",
        mem_limit="128Mi",
    )
    return Scenario(
        name="oomkilled",
        question="llm-gateway keeps restarting",
        namespace=ns,
        workload=name,
        symptom="oom",
        expect_fix_kind="config",
        expect_cause=["OOM", "memory limit", "128Mi"],
        snapshot=build_snapshot(
            [deployment(ns, name, image, mem_request="64Mi", mem_limit="128Mi"), OPENCLAW],
            [p, OPENCLAW_POD],
            pod_usage={ns: {p["metadata"]["name"]: "121Mi"}},
            node_used="4900Mi",
        ),
        repo_files={
            **_base(name, "ghcr.io/phuongddx/llm-gateway", mem_limit="128Mi", mem_request="64Mi"),
            f"apps/{name}/vps192/kustomization.yaml": _kustomization(name, "ghcr.io/phuongddx/llm-gateway", SHA_GW),
            f"apps/{name}/vps192/configmap.yaml": (
                'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: llm-gateway-config\ndata:\n  PORT: "8000"\n'
            ),
        },
    )


def imagepull() -> Scenario:
    # The repo directory (apps/stress-app-be) differs from the namespace (stress-api), as on vps192.
    ns, name = "stress-api", "stress-app-be"
    image = "ghcr.io/phuongddx/stress-app-be:abc1234"
    p = pod(
        ns,
        "stress-app-be-7f6d9-m3n4b",
        name,
        image,
        ready=False,
        waiting={"reason": "ImagePullBackOff", "message": f'Back-off pulling image "{image}": manifest unknown'},
    )
    return Scenario(
        name="imagepull",
        question="stress-app-be is not starting after the last deploy",
        namespace=ns,
        workload=name,
        symptom="imagepull",
        expect_fix_kind="config",
        expect_cause=["abc1234", "newTag", "manifest"],
        snapshot=build_snapshot(
            [deployment(ns, name, image, ready=0), OPENCLAW],
            [p, OPENCLAW_POD],
            events=[
                event(
                    ns,
                    "Pod",
                    p["metadata"]["name"],
                    "Failed",
                    f'Failed to pull image "{image}": manifest unknown',
                    "2026-11-05T10:02:00Z",
                    count=9,
                )
            ],
        ),
        repo_files={
            **_base(name, "ghcr.io/phuongddx/stress-app-be"),
            f"apps/{name}/vps192/kustomization.yaml": _kustomization(
                name, "ghcr.io/phuongddx/stress-app-be", "abc1234", namespace=ns
            ),
            f"apps/{name}/vps192/configmap.yaml": (
                'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: stress-app-be-config\ndata:\n  PORT: "3000"\n'
            ),
        },
    )


def pending() -> Scenario:
    ns = name = "dropitx-fe"
    image = "ghcr.io/dropitx-io/dropitx:" + SHA_FE
    p = pod(
        ns,
        "dropitx-fe-84c6b-z8x7c",
        name,
        image,
        phase="Pending",
        ready=False,
        problems=[
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "0/1 nodes are available: 1 Insufficient memory.",
            }
        ],
        mem_request="8Gi",
        mem_limit="8Gi",
    )
    return Scenario(
        name="pending",
        question="dropitx-fe rollout is stuck",
        namespace=ns,
        workload=name,
        symptom="pending",
        expect_fix_kind="config",
        expect_cause=["Insufficient memory", "8Gi", "request"],
        snapshot=build_snapshot(
            [
                deployment(
                    ns,
                    name,
                    image,
                    ready=0,
                    updated=0,
                    mem_request="8Gi",
                    mem_limit="8Gi",
                    generation=2,
                    observed=2,
                    conditions=[
                        {
                            "type": "Progressing",
                            "status": "False",
                            "reason": "ProgressDeadlineExceeded",
                            "message": 'ReplicaSet "dropitx-fe-84c6b" has timed out progressing.',
                        }
                    ],
                ),
                OPENCLAW,
            ],
            [p, OPENCLAW_POD],
            events=[
                event(
                    ns,
                    "Pod",
                    p["metadata"]["name"],
                    "FailedScheduling",
                    "0/1 nodes are available: 1 Insufficient memory.",
                    "2026-11-05T10:05:00Z",
                    count=12,
                )
            ],
            replicasets=[
                {
                    "metadata": {
                        "namespace": ns,
                        "name": "dropitx-fe-84c6b",
                        "annotations": {"deployment.kubernetes.io/revision": "2"},
                        "ownerReferences": [{"kind": "Deployment", "name": name}],
                    },
                    "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
                    "status": {"replicas": 1, "readyReplicas": 0},
                }
            ],
        ),
        repo_files={
            **_base(name, "ghcr.io/dropitx-io/dropitx", mem_limit="8Gi", mem_request="8Gi"),
            f"apps/{name}/vps192/kustomization.yaml": _kustomization(name, "ghcr.io/dropitx-io/dropitx", SHA_FE),
            f"apps/{name}/vps192/configmap.yaml": (
                'apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dropitx-fe-config\ndata:\n  PORT: "3000"\n'
            ),
        },
    )


def gateway_5xx() -> Scenario:
    ns, name = "auth", "keycloak"
    image = "quay.io/keycloak/keycloak@sha256:" + "b" * 64
    p = pod(
        ns,
        "keycloak-59d7c-p0o9i",
        name,
        image,
        ready=False,
        restarts=0,
        problems=[
            {
                "type": "Ready",
                "status": "False",
                "reason": "ContainersNotReady",
                "message": "containers with unready status: [keycloak]",
            }
        ],
        mem_limit="768Mi",
    )
    return Scenario(
        name="5xx",
        question="sso.dropitx.site returns 502",
        namespace=ns,
        workload=name,
        symptom="5xx",
        expect_fix_kind="app-code",
        expect_cause=["readiness", "not ready", "database"],
        snapshot=build_snapshot(
            [deployment(ns, name, image, ready=0, mem_limit="768Mi"), OPENCLAW],
            [p, OPENCLAW_POD],
            events=[
                event(
                    ns,
                    "Pod",
                    p["metadata"]["name"],
                    "Unhealthy",
                    "Readiness probe failed: HTTP probe failed with statuscode: 503",
                    "2026-11-05T10:07:00Z",
                    count=40,
                )
            ],
            pod_usage={ns: {p["metadata"]["name"]: "590Mi"}},
            ingressroutes=[
                {
                    "metadata": {"namespace": ns, "name": "keycloak"},
                    "spec": {
                        "entryPoints": ["websecure"],
                        "routes": [
                            {"match": "Host(`sso.dropitx.site`)", "services": [{"name": "keycloak", "port": 8080}]}
                        ],
                    },
                }
            ],
            endpoints=[
                {
                    "metadata": {"namespace": ns, "name": "keycloak"},
                    "subsets": [
                        {
                            "notReadyAddresses": [{"ip": "10.42.0.31", "targetRef": {"name": p["metadata"]["name"]}}],
                            "ports": [{"port": 8080}],
                        }
                    ],
                }
            ],
            logs={
                (
                    ns,
                    p["metadata"]["name"],
                    False,
                ): "2026-11-05T10:06:58.100Z WARN [org.hibernate] Unable to acquire JDBC Connection\n"
                "2026-11-05T10:06:58.300Z ERROR [io.quarkus] Failed to start: connection to database refused\n"
            },
        ),
        repo_files={
            **_base(name, "quay.io/keycloak/keycloak", mem_limit="768Mi"),
            f"apps/{ns}/vps192/kustomization.yaml": (
                "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: auth\n"
                "resources:\n  - ../base\n"
            ),
            f"apps/{ns}/base/kustomization.yaml": "resources:\n  - deployment.yaml\n",
            f"apps/{ns}/base/deployment.yaml": _base(name, "quay.io/keycloak/keycloak")[
                f"apps/{name}/base/deployment.yaml"
            ],
        },
        loki_lines=[
            (
                "2026-11-05T10:07:01.000Z",
                '{"RequestHost":"sso.dropitx.site","DownstreamStatus":502,"ServiceName":"auth-keycloak-8080@kubernetes"}',
            ),
        ],
    )


def healthy() -> Scenario:
    return Scenario(
        name="other",
        question="is openclaw healthy?",
        namespace="openclaw",
        workload="openclaw",
        symptom="other",
        expect_fix_kind="none",
        expect_cause=["healthy", "no problem", "nothing wrong"],
        snapshot=build_snapshot(
            [OPENCLAW], [OPENCLAW_POD], pod_usage={"openclaw": {OPENCLAW_POD["metadata"]["name"]: "1927Mi"}}
        ),
        repo_files={"apps/openclaw/vps192/kustomization.yaml": "resources: []\n"},
    )


ALL = {s.name: s for s in (crashloop(), oomkilled(), imagepull(), pending(), gateway_5xx(), healthy())}


def ingress(ns, name, host, service, port, path="/", tls_hosts=None, class_name="traefik", path_type="Prefix"):
    """A networking.k8s.io/v1 Ingress routing one host and path to one Service port."""
    return {
        "metadata": {
            "namespace": ns,
            "name": name,
            "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...large dump...}"},
        },
        "spec": {
            "ingressClassName": class_name,
            "tls": [{"hosts": tls_hosts if tls_hosts is not None else [host], "secretName": f"{name}-tls"}],
            "rules": [
                {
                    "host": host,
                    "http": {
                        "paths": [
                            {
                                "path": path,
                                "pathType": path_type,
                                "backend": {"service": {"name": service, "port": {"number": port}}},
                            }
                        ]
                    },
                }
            ],
        },
    }
