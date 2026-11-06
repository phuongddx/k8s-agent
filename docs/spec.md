<!--
Copied from phuongddx/vps192-k8s-config, path docs/features/k8s-troubleshoot-agent/spec.md,
at commit 323f9fb (323f9fb3770d379a91f5a61e967c4f07710bf0b9), on 2026-11-06.
The copy in vps192-k8s-config is the source of truth; update this copy if that file changes.
-->

> Public copy. Operational security details of the private config repo (credential scopes, open hardening gates, secret names) are omitted here; the private repo's spec is the source of truth.

# Spec: Kubernetes troubleshooting agent on vps192

Status: approved
Date: 2026-11-05

## Problem

When something breaks on vps192 (a crashloop, an OOMKill, a stuck rollout, 5xx at the ingress), the owner diagnoses it by hand: SSH as `k3s-deploy`, a series of `kubectl` commands, Grafana Explore for logs, then reading manifests in this repo. That needs a laptop and takes time. The owner wants to ask about a symptom from Telegram, get the cause with evidence, and, when the fix is a config change in this repo, get a pull request to review and merge.

Who is affected: the owner (sole operator), the `vps192-k8s-config` repo (receives fix PRs), and every workload on vps192 (subject of diagnosis).

## Context read

- `CLAUDE.md`: operating constraints, secret policy, deploy flow, OpenClaw runtime notes.
- `docs/features/logs-viewer/spec.md`: Loki at `loki.logging.svc:3100`, `auth_enabled: false`, 7-day retention.
- `plans/261012-1436-dropitx-on-vps192/FINAL-REPORT.md:34-36`: `MemAvailable` drift and the 1 GiB trigger.
- `apps/openclaw/base/deployment.yaml:19`: `automountServiceAccountToken: false`; OpenClaw has no cluster access today.
- `apps/openclaw/vps192/configmap.yaml:20,30,83`: model `zai/glm-5.3` at `https://api.z.ai/api/coding/paas/v4`; the prompt rule forbidding pushes and PRs to this repo.
- `.github/workflows/validate.yml:3-31`: runs on every PR and on push to `main`; all the per-service Age keys in job env; runs `scripts/validate.sh` from the checked-out ref.
- `.github/workflows/deploy.yml:1-160`: push to `main` (docs paths ignored) and `workflow_dispatch`; `validate` job also carries the Age keys and runs `scripts/validate.sh`.
- `scripts/validate.sh:50`: Namespace count asserted as 7. `.sops.yaml`: one Age rule per service.
- Live, read-only, 2026-11-05: `MemAvailable` 2875 MiB of 7935 MiB; openclaw 1927Mi, keycloak 593Mi, grafana 253Mi.
- `gh pr list --state merged --limit 100`: all 61 merged PRs, promotion PRs included, were authored and merged by `phuongddx`.

Gaps:
- No `ARCHITECTURE.md`, `docs/engineering/`, or `.agents/` in this repo. Noted, not fixed here.

## References

- OpenClaw MCP docs, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/mcp.md: `mcp.servers` with `streamable-http`, `requestTimeoutMs`, `toolFilter`. Informed the OpenClaw ↔ agent interface. Read from `main`, not checked against 2026.9.9 (see Verifications).
- OpenClaw skills docs, https://raw.githubusercontent.com/openclaw/openclaw/main/docs/tools/skills.md: `<workspace>/skills/<name>/SKILL.md`. Informed the OpenClaw skill.
- Loki HTTP API, https://grafana.com/docs/loki/latest/reference/loki-http-api/: `query_range` parameters. Informed the Loki tool.
- GitHub fine-grained PAT permissions, https://docs.github.com/en/rest/overview/permissions-required-for-fine-grained-personal-access-tokens: Contents write is repo-wide. Informed the decision that the token cannot be the only guard.
- GitHub rulesets, https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets, and plans, https://docs.github.com/en/get-started/learning-about-github/githubs-plans: branch protection and rulesets are not enforced on Free private repos. Informed G1.
- GitHub commits API, https://docs.github.com/en/rest/commits/commits: `GET /repos/{owner}/{repo}/commits/{sha}/pulls`. Informed G1's per-commit check.
- HolmesGPT README, https://raw.githubusercontent.com/robusta-dev/holmesgpt/master/README.md: read-only, no write path. Informed rejecting it, since the owner wants PRs from the agent itself.
- containers/kubernetes-mcp-server configuration, https://raw.githubusercontent.com/containers/kubernetes-mcp-server/main/docs/configuration.md: considered for approach 1, which the owner did not pick.

## Goals

1. From Telegram, the owner asks about a symptom and gets, within about two minutes, a cause, a confidence level, and evidence that cites events, log lines with timestamps, or manifest `path:line`.
2. When the fix is a config change under `apps/**`, the agent proposes a diff; on the owner's "open PR" it opens a PR to this repo as a bot account, and the PR passes `validate`.
3. The agent has no way to write to the cluster or read Secret values, and nothing it does can deploy without the owner merging.
4. OpenClaw never holds the cluster credential or the bot token.

## Non-goals

- No web UI, PostgreSQL, Prometheus, or RAG over docs.
- No proactive alerting or watching. Diagnosis runs only when asked.
- No PRs to app repos (`stress-app-be`, `llm-gateway`, `dropitx-*`) and no edits outside `apps/**`. App-code causes get a diagnosis and a hint only.
- No direct writes to the cluster (step 5 of the reference diagram is replaced by the PR flow).
- No persistent agent state; no HA; single replica.
- No change to OpenClaw's memory limit or its `exec` mode.

## Constraints

- Production mutations (pushes, PRs, merges, deploy dispatches, cluster applies) need explicit owner approval (`CLAUDE.md`).
- All deploys go through PR → green `validate` → merge → `deploy vps192`. No Flux or Argo.
- Images immutable: 40-hex SHA tags for built images, digest pins for upstream.
- Secrets: SOPS only, one Age identity per service, values entered by the owner on the VPS workbench, never printed or logged.
- Services ClusterIP only; no new public host, so no SSO gate or DNS change.
- Memory: the new workload stays within a 512Mi limit; re-measure after rollout.
- GitHub Free: no enforced branch protection on this private repo. Any token that can push a branch can push `main`.
- OpenClaw's PVC file `/home/node/.openclaw/openclaw.json` is the config of record; ConfigMap changes reach it only via `openclaw config set`.
- Hardening (RBAC scope, NetworkPolicy) is in scope because this plan calls for it.

## Design

### Components

**Repo `phuongddx/k8s-agent` (new).** Python 3.12, LangGraph, FastAPI, the `mcp` Python SDK, the `kubernetes` client, pytest. CI builds `ghcr.io/phuongddx/k8s-agent:<40-hex sha>` for linux/amd64 and opens a promotion PR here, using environment `vps192-promotion` and secret `VPS192_CONFIG_PROMOTION_TOKEN`, the same pattern as `llm-gateway`. The image contains no secrets and is published public like `llm-gateway`'s. The image also contains `git` and a checksum-pinned `kustomize`.

**Namespace `k8s-agent` in this repo** (`apps/k8s-agent/{base,vps192}`):

- Deployment `k8s-agent`, 1 replica, memory request 256Mi / limit 512Mi (estimates; adjusted from measurement after rollout). An `emptyDir` holds the repo clone.
- ServiceAccount `k8s-agent` with a ClusterRole of verbs `get`, `list`, `watch` only, on:
  - core: `pods`, `pods/log`, `events`, `nodes`, `services`, `endpoints`, `persistentvolumeclaims`, `namespaces`, `configmaps`
  - `apps`: `deployments`, `statefulsets`, `daemonsets`, `replicasets`
  - `batch`: `jobs`
  - `networking.k8s.io`: `ingresses`
  - `traefik.io`: `ingressroutes`, `middlewares`
  - `metrics.k8s.io`: `pods`, `nodes`
  - No rule mentions `secrets`. No write verb appears anywhere.
- Secret `k8s-agent-secrets` from `secrets/vps192/k8s-agent/k8s-agent-secrets.enc.yaml`, keys `ZAI_API_KEY` (a copy of the owner's GLM key) and `BOT_GH_TOKEN`. New Age identity (its GitHub secret follows the repo's per-service naming), a `.sops.yaml` rule for `^secrets/vps192/k8s-agent/.*\.enc\.yaml$`, and deploy wiring.
- ConfigMap with non-secret settings: `MODEL=glm-5.3`, `MODEL_BASE_URL=https://api.z.ai/api/coding/paas/v4`, `LOKI_URL=http://loki.logging.svc:3100`, `REPO=phuongddx/vps192-k8s-config`, `BOT_LOGIN` (the bot account's login, set once the account exists), budgets.
- Service `k8s-agent`, ClusterIP, port 8080.
- NetworkPolicy: ingress to `k8s-agent` pods only from pods in namespace `openclaw`. Egress unrestricted.

**Repo plumbing.** `infrastructure/base/namespaces.yaml` gains `k8s-agent`. `scripts/validate.sh`: Namespace count 7 → 8, expected-file entries for the overlay and the secret, `k8s-agent` in the no-local-namespace loop, and render rules that the image is a 40-hex tag and every Service is ClusterIP. `deploy.yml`: a matrix row for `k8s-agent` (namespace `k8s-agent`, rollout `deployment/k8s-agent`, health = pod Ready) and the new Age key in the env blocks; `workflow_dispatch` options gain `k8s-agent`.

**OpenClaw changes.** No new pods, no new secrets.

- `mcp.servers.k8s-agent`: `{ url: "http://k8s-agent.k8s-agent.svc:8080/mcp", transport: "streamable-http", requestTimeoutMs: 180000 }`, added to the ConfigMap and applied to the PVC file with `openclaw config set`.
- Skill `skills/vps192-diagnose/SKILL.md` in the OpenClaw workspace: for questions about vps192 workloads call `diagnose`; relay the answer; call `open_pr` only when the owner asks for it in the current chat.
- System prompt: the existing ban on pushing to or opening PRs on this repo stays. OpenClaw itself never gets write access here; PRs come only from the agent.

### Interface

The agent serves MCP over streamable HTTP at `/mcp` with two tools:

- `diagnose(question: str)` → `{ cause, confidence: high|medium|low, evidence: [{source, detail}], fix_kind: config|app-code|capacity|none|unknown, fix_summary?, proposal_id?, diff?, clarify? }`. If the question is ambiguous, only `clarify` is set, holding a question for the owner.
- `open_pr(proposal_id: str)` → `{ pr_url }` or `{ error }`.

Plus `GET /healthz` for the readiness probe.

### The graph

`diagnose` runs these nodes in order:

1. `intake` (one LLM call): map the question to namespace, workload and symptom class, using a live workload inventory. If ambiguous, return `clarify`.
2. `triage` (no LLM): always collect workload status and replicas; pod phase, restarts, last termination reason and exit code; the last 20 events; pod and node memory from metrics; the last 5 commits under `apps/<svc>/` in the clone.
3. `investigate` (LLM tool loop): read-only tools only. Budgets: 12 tool calls, 90 s, 200 lines or 32 KB per log fetch, Loki window 1 h by default and 168 h at most.
4. `diagnose` (structured output): the `diagnose` result fields above. If a budget was hit, confidence is capped at `low` and the cause says so.
5. `propose_fix` (only when `fix_kind == config`): the model edits files in the clone; the agent builds a unified diff, rejects any path outside `apps/**`, runs `kustomize build` on each changed overlay, allows one repair attempt on failure, and otherwise returns the diagnosis without a diff. A proposal stores `{id, diff, base_sha, created_at}` in memory and expires after 24 h.

`open_pr(proposal_id)`:

1. Look up the proposal; fail if missing or expired.
2. `git fetch`; if `origin/main` ≠ `base_sha`, fail with "main changed, ask again". No rebasing.
3. Apply the diff, re-check paths, render again.
4. Commit as the bot, `fix(<svc>): <summary>`; push `agent/<svc>-<short id>`.
5. Open the PR with the question, cause, evidence, and "Opened by k8s-agent; owner review required."
6. Return the PR URL.

**Tools available to `investigate`** (fixed Python functions, no shell, no generic kubectl): `list_workloads`, `describe_workload`, `list_pods`, `describe_pod`, `pod_logs(previous?)`, `events`, `top_pods`, `top_nodes`, `rollout_status`, `get_ingressroutes`, `get_endpoints`, `get_configmap`, `loki_query`, `read_repo_file`, `repo_log`.

**Runbook given to `investigate`:**

| Symptom | Checks | Typical `fix_kind` |
|---|---|---|
| CrashLoopBackOff / Error | previous-container logs, exit code, recent image or config commits | config or app-code |
| OOMKilled / memory pressure | limit vs usage, node allocatable vs total usage, top consumers | config if node headroom exists, else capacity |
| ImagePullBackOff | pull error message, `newTag` in the kustomization | config |
| Rollout stuck / Pending | rollout status, ReplicaSet and scheduling events, PVC binding | config or capacity |
| 5xx at ingress | Traefik logs in Loki by host, IngressRoute → Service → endpoints readiness, app logs at the same timestamps | config or app-code |
| Other | general tool loop | any |

Tool output enters the model marked as untrusted data.

**Telegram reply** (OpenClaw formats it, under about 1500 characters): one line of cause and confidence, 3-5 evidence bullets, then one of "Fix ready: <summary>. Reply 'open PR'.", "App code: likely <repo>, <hint>.", "Capacity: <numbers>.", or "Nothing wrong found."

### PR safety

**Bot account.** A separate GitHub account with 2FA, collaborator on `phuongddx/vps192-k8s-config` only. A personal-account collaborator always has write access. Token: classic PAT with `repo` scope and no `workflow` scope, 90-day expiry; without `workflow` scope GitHub rejects pushes that change `.github/workflows/`. Kill switch: remove the collaborator.

**Threats and guards.**

| # | Bot action | Stopped by |
|---|---|---|
| T1 | Push to `main` directly | G1 |
| T2 | Merge its own PR | G1 (`merged_by` must be `phuongddx`) |
| T3 | Land a commit on `main` that deploys with the owner's next merge | G1 (checks every commit since the last successful deploy) |
| T4 | Force-push `main` to an older merged commit | G1 (rejects forced pushes) |
| T5 | Edit `scripts/` in a PR, its own or a push onto an owner PR branch, to exfiltrate Age keys from `validate.yml` | G2: `validate.yml` has no Age keys |
| T6 | Push to `main` so a workflow runs its scripts with Age keys | G1 runs before any job that holds keys; `validate.yml` has no keys |
| T7 | Edit workflows | no `workflow` scope |
| T8 | Touch files outside `apps/**` | agent allowlist in code; G2 path check |
| T9 | Run an old workflow version from a stale branch | handled by a hardening gate in the private repo before the bot is invited |
| T10 | Re-run an owner run (`github.actor` stays the owner on re-run) | G1 rejects any run whose `github.triggering_actor` is not `phuongddx` |

**G1: deploy guard.** A new first job `guard` in `deploy.yml`. Inline bash and `gh api` with `GITHUB_TOKEN` only; no checkout, no repo scripts. Permissions `contents: read`, `pull-requests: read`, `actions: read`. Every other job declares `needs: guard`.

0. Any run whose `github.triggering_actor` is not `phuongddx` fails, which covers re-runs by anyone else.
1. `workflow_dispatch`: pass only if `github.actor == 'phuongddx'`. The commit check below still runs, unless the owner sets the boolean input `skip_guard`. That input is the recovery path after a revert.
2. `push` with `github.event.forced == true`: fail.
3. Base = `head_sha` of the latest successful `deploy vps192` run on `main`; if none, `github.event.before`. List commits `base...github.sha` with the compare API; fail closed if the list is truncated or longer than 250.
4. Each commit passes if it belongs to a PR with `base.ref == main`, `merged_at` set, and `merged_by.login == 'phuongddx'` (read from `GET pulls/{n}`), or if every file it changes matches the deploy-ignored paths (a rename counts both its old and new path; `.md` files under `apps/` or `secrets/` never count as docs-only) (`plans/**`, `docs/**`, `**.md`, `.claude/**`, `.superpowers/**`). A file list of 300 entries (one full page from "Get a commit") is not trusted: the commit fails the docs-only test.
5. On failure, print the offending SHAs and exit non-zero. Nothing deploys.

The `notify` job runs without a checkout and posts to Slack inline, so no repo file runs on an unverified commit.

**G2: validate hygiene.** In `validate.yml`:

1. `validate.yml` holds no Age keys and runs no SOPS decryption. Decryption runs only in `deploy.yml`'s `validate` job, after the `guard` job has verified every commit. No condition on PR author or sender can keep bot code (pushed to `main`, pushed under an owner commit, or brought back by a reopen) out of a job that later holds the keys, because steps share the runner.
2. For every PR whose author is not `phuongddx` (fail closed, so it covers the bot whatever its login), the path check fails if any changed path is outside `apps/`. It runs first after checkout, before any repo file runs, and diffs with `git diff --no-renames --name-only` so a rename out of a protected path is caught. The bot cannot remove the check, because its token cannot push workflow changes (T7).

Prerequisite: OpenClaw holds no credential that can write to the config repo (tracked privately).

### Rollout order

Each step is its own PR or its own approved action.

0. Owner prerequisites: bot account and PAT; the OpenClaw credential prerequisite above; confirm Z.ai's terms allow this use of the Coding Plan key.
1. PR: G1 and G2. Merged before the bot has access. Confirm a normal owner PR still validates and deploys.
2. Create `phuongddx/k8s-agent`: code, tests, image CI, promotion workflow.
3. PR: namespace, RBAC, NetworkPolicy, Deployment pinned to the first image SHA, SOPS secret (values entered on the VPS workbench), `.sops.yaml`, `validate.sh`, `deploy.yml`. Then the post-deploy checks.
3a. Hardening gate in the private repo (secret rotation and stale-ref cleanup) before the bot exists.
4. Invite the bot as collaborator. Run the live G1 and G2 tests.
5. PR: OpenClaw ConfigMap (`mcp.servers.k8s-agent`, skill). Then `openclaw config set` on the PVC file.
6. End-to-end from Telegram on real current state. Optional, separately approved: a throwaway Deployment with a nonexistent image tag as a staged failure.
7. Final report and evidence.

### Verifications done first in the plan

These are unverified facts the design leans on. Each has a fallback.

1. OpenClaw 2026.9.9 supports `mcp.servers` with `streamable-http`. Fallback: the skill calls the agent over plain HTTP with `curl` (`POST /diagnose`, `POST /proposals/{id}/pr`), exposing the same two operations.
2. A fine-grained PAT owned by the bot can access a collaborator repo on a personal account with Contents and Pull requests write and no Workflows. If yes, use it instead of the classic PAT.
3. K3s on this node enforces NetworkPolicy (embedded network policy controller enabled). Fallback: a shared bearer token in `k8s-agent-secrets` and OpenClaw's MCP `headers`, accepting that the token then sits in OpenClaw's env. Outcome: enforcement could not be confirmed; bearer-token access control adopted (`MCP_BEARER_TOKEN`).
4. Z.ai's Coding Plan endpoint accepts this workload under its terms. Fallback: a pay-per-token Z.ai key at the standard endpoint.
5. On `pull_request` `synchronize`, `github.event.sender.login` is the account that pushed the new commits. Fallback: no decryption in PR-triggered `validate.yml` at all; SOPS decryption runs only in `deploy.yml` after G1. Outcome: the fallback was adopted after review (2026-11-05), so `sender` is no longer load-bearing.
6. Optional hardening: the last 8 commits on `main` are signature-verified (checked 2026-11-05). `verified == true` alone does not prove the owner, since the bot can sign its own commits; G1 could additionally require the signer to be GitHub's web-flow key or `phuongddx`. Decide during planning whether this is worth adding.

## Error handling

| Failure | Behaviour |
|---|---|
| Model timeout, error, or rate limit | One retry with backoff, then "Model unavailable, try again shortly." Never a partial diagnosis presented as complete. |
| Tool forbidden or not found | Returned to the model as the tool result. A forbidden call on a Secret is logged as a warning. |
| Budget hit | Diagnose with what was gathered; confidence capped at `low`; reply says the budget was hit. |
| Loki unavailable | Continue with Kubernetes logs; evidence notes Loki was unavailable. |
| Concurrent request | One diagnosis at a time; the second gets "Busy with another diagnosis." |
| Pod restart with a pending proposal | `open_pr` returns "Proposal not found, ask again." |
| `main` moved | `open_pr` returns "main changed, ask again." |
| Overall time | Graph hard limit 150 s, below OpenClaw's 180 s MCP timeout. |
| G1 blocks a deploy | Merge a revert PR. Before dispatching, confirm `git diff <last successful deploy sha>..origin/main -- scripts/ .github/` is empty, because dispatch with `skip_guard` skips the commit check and runs `scripts/validate.sh` from `main` with the Age keys. Then the owner runs `workflow_dispatch` with `skip_guard` set; that run becomes the next base. If the guard fails on a fresh owner merge because GitHub has not yet linked the merge commit to its PR, the owner re-runs the failed job. |

The agent logs structured JSON to stdout (collected by Alloy into Loki): question, tool names, durations, token counts. Never prompts, tool output bodies, or tokens.

## Testing

**`k8s-agent` repo, in CI:**

- Tools against a fake Kubernetes client with fixtures for CrashLoop, OOMKilled, ImagePull, Pending, and 5xx.
- Path allowlist, diff apply, proposal expiry, and the base-SHA check.
- Graph tests with a scripted fake model: budgets enforced, structured output valid, `clarify` path, no-diff path.

**`k8s-agent` repo, by hand:** six recorded scenarios (cluster snapshots with values scrubbed) against real GLM, scored on right cause and real cited evidence.

**After cluster deploy, recorded in evidence:**

- `kubectl auth can-i --as=system:serviceaccount:k8s-agent:k8s-agent`: `get secrets -A` → no; `create pods` → no; `get pods/log -A` → yes.
- A pod outside namespace `openclaw` cannot reach `k8s-agent:8080`.
- Measured memory, and the request/limit adjusted to it.

**Guards:**

- G1 logic run against past `main` ranges (pass) and constructed ranges (fail) before merge.
- Live, after step 4 and with owner approval: the bot pushes a harmless non-docs commit to `main`; G1 fails and nothing deploys; recovery via revert PR and dispatch.
- G2: the bot opens a PR touching `scripts/`; the path check fails.

## Risk acceptance

- **Memory.** 256-512Mi more on a node whose `MemAvailable` has ranged 1300-2875 MiB. Fallback: lower the limit, or scale to 0 when not needed.
- **Prompt injection.** Log lines can steer the model. It can only read non-Secret cluster state and propose `apps/**` diffs; it cannot see tokens, write to the cluster, or deploy. A misleading PR is caught at the owner's review.
- **Soft approval gate.** OpenClaw decides when to call `open_pr`, so an unrequested PR is possible. Merge remains the hard gate.
- **Data exposure to the model provider.** Pod logs, events and ConfigMaps are sent to Z.ai. Logs may contain whatever apps print.
- **Collaborator write reaches every branch.** The bot can push onto an owner PR branch, including `promote/*`, before it is merged. `validate.yml` holds no Age keys, but the owner's merge then deploys whatever the branch holds, and `deploy.yml` decrypts after G1 passes. Control: GitHub notifies the owner of pushes to their PRs, and the owner re-reads "Files changed" immediately before merging.
- **SOPS errors surface after merge.** A broken encrypted file is caught by `deploy.yml`'s `validate` job, before any apply, instead of on the PR. Deploys stay blocked until a fix PR merges.
- **Old workflow versions.** Until gate 3a is done and verified, the bot must not be invited.
- **Loopback from rendering.** Model-written kustomizations can still reach the pod's loopback (only the agent's own bearer-protected server listens there). The pod must never use `hostNetwork`.
- **Detective, not preventive, guard.** G1 stops deploys but cannot stop a bot push to `main` from landing in history.
