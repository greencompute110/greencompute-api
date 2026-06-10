# Green Compute — API & CLI Guide

Programmatic access for spinning model/container workloads up and down on the
Green Compute fleet — including the internal **A4000 test node**.

- **Base URL:** `https://api.green-compute.com`
- **Auth:** every request carries your API key in the **`X-API-Key`** header.
  The OpenAI-compatible inference endpoints (`/v1/*`) also accept
  `Authorization: Bearer <key>`.
- **Interactive docs:** `https://api.green-compute.com/docs` (OpenAPI/Swagger).

---

## 1. Concepts

| Term | What it is |
|------|------------|
| **Workload** | A reusable *spec*: container image + runtime config + resource requirements (GPU model, VRAM, count). Define once, deploy many times. |
| **Deployment** | A *running instance* of a workload on the fleet — the actual container(s). **Spin up = create a deployment; spin down = terminate it.** |
| **Scheduler** | Picks a node that satisfies the workload's `requirements` (GPU model, VRAM, count). You don't pick a node by IP — you constrain it with requirements. |

---

## 2. Get an API key

Ask an admin for a key, or mint one (admin/provisioning credential required):

```bash
curl -X POST https://api.green-compute.com/platform/api-keys \
  -H "X-API-Key: <ADMIN_KEY>" -H "Content-Type: application/json" \
  -d '{"name":"team-testing"}'
# → { "key_id": "...", "secret": "gc_..." }   ← save the secret; it's shown once
```

Use that `secret` as your `X-API-Key` for everything below.

> ⚠️ **The key must be bound to a user.** A bare master/admin key (no `user_id`)
> gets `403 {"detail":"api key must be bound to a user"}` on workload/deployment
> calls, because those resources need an owner. Mint a per-user key (the call
> above, run by an admin, binds the new key to a user) and use that.

---

## 3. Targeting the internal A4000 node

The A4000 box is a normal fleet node but is **hidden from public rentals** and is
intended for internal model-deploy testing. To make a deployment land there,
constrain the workload to its GPU class:

```json
"requirements": {
  "supported_gpu_models": ["a4000"],   // only the A4000 node qualifies → lands on .205
  "min_vram_gb_per_gpu": 16,           // A4000 = 16 GB/GPU; keep ≤ 16
  "gpu_count": 1                       // 1–16 per workload (this box has 10, so up to 10 here)
}
```

> If you **omit** `supported_gpu_models`, the scheduler is free to place the
> workload on any node with enough VRAM (likely a 4090/5090). Set it to
> `["a4000"]` to pin to the test box.

> 🚫 **Do NOT use the `vllm` template/`create-vllm` for the A4000.** That shortcut
> hardcodes `min_vram_gb_per_gpu=24` and doesn't accept `supported_gpu_models`, so
> the scheduler will reject the 16 GB A4000 outright. For the A4000 you must use
> the **raw** workload create (§4a) with `min_vram_gb_per_gpu: 16` and
> `supported_gpu_models: ["a4000"]`. (The template is fine for 4090/5090.)

---

## 4. Spin up → use → spin down (REST)

### 4a. Create a workload (vLLM model, pinned to A4000)

Use the **raw** create (not the `vllm` template — see §3):

```bash
curl -X POST https://api.green-compute.com/platform/workloads \
  -H "X-API-Key: $GC_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "qwen05b-a4000-test",
    "kind": "inference",
    "image": "vllm/vllm-openai:v0.19.1-cu130-ubuntu2404",
    "requirements": {
      "gpu_count": 1,
      "min_vram_gb_per_gpu": 16,
      "supported_gpu_models": ["a4000"]
    },
    "runtime": {
      "runtime_kind": "vllm",
      "model_identifier": "Qwen/Qwen2.5-0.5B-Instruct"
    }
  }'
# → { "workload_id": "wl_...", ... }
```

- **The `image` you send is ignored for vLLM on the A4000.** The box is pinned to
  a **CUDA-12** vLLM build (`vllm/vllm-openai:v0.8.5`) via its node-agent config,
  because the default cu130 image has no kernels for Ampere `sm_86`
  (`cudaErrorNoKernelImageForDevice`). So `image` is a required-but-ignored
  placeholder here; what matters is `runtime.runtime_kind:"vllm"` +
  `runtime.model_identifier` + `requirements`. ✅ *Verified end-to-end:
  Qwen2.5-0.5B-Instruct deployed to the A4000 and served a chat completion.*
- A4000 = **16 GB/GPU** — pick models that fit (≈≤3B comfortably; 7B is tight and
  can OOM). For larger models raise `gpu_count` (tensor-parallel) up to 16 (this
  box has 10 GPUs, so up to 10 here).
- **Generic container** (not a model): set `"kind":"pod"`, point `image` at your
  container, and drop the `runtime` block — see **§4f** for the full pod recipe
  (SSH, your own key, extra ports, up to 10 GPUs).

### 4b. Spin it up (create a deployment)

```bash
curl -X POST https://api.green-compute.com/platform/deployments \
  -H "X-API-Key: $GC_KEY" -H "Content-Type: application/json" \
  -d '{ "workload_id": "wl_...", "requested_instances": 1, "accept_fee": true }'
# → { "deployment_id": "dep_...", "state": "scheduled", ... }
```

### 4c. Watch it come up

```bash
curl -s https://api.green-compute.com/platform/deployments/dep_... \
  -H "X-API-Key: $GC_KEY"
# state goes: scheduled → pulling/starting → ready
```
List everything: `GET /platform/deployments`.

### 4d. Call the model (OpenAI-compatible)

```bash
curl -X POST https://api.green-compute.com/v1/chat/completions \
  -H "Authorization: Bearer $GC_KEY" -H "Content-Type: application/json" \
  -d '{ "model": "Qwen/Qwen2.5-7B-Instruct",
        "messages": [{"role":"user","content":"hello"}] }'
```
`/v1/completions` and `/v1/embeddings` work the same way. Point any OpenAI SDK at
`https://api.green-compute.com/v1` with your key as the API key.

### 4e. Spin it down (terminate)

```bash
curl -X DELETE https://api.green-compute.com/platform/deployments/dep_... \
  -H "X-API-Key: $GC_KEY"
```
Suspend/resume instead of destroy: `POST /platform/deployments/{id}/resume`.

### 4f. Pods (generic containers): SSH access + extra ports

A **pod** (`"kind":"pod"`) is a raw container with SSH injected — RunPod/Vast
style. Three things commonly trip people up:

**1. You do NOT set an SSH key — the platform generates one for you.**
When the pod starts, the node auto-generates an ephemeral keypair, injects the
public key into the container's `authorized_keys`, and hands you back the
**private key**. Retrieve it once the pod is `ready`:

```bash
# Poll until ready first (endpoint is null while it's still "scheduled"):
curl -s https://api.green-compute.com/platform/deployments/dep_... -H "X-API-Key: $GC_KEY"
#   ... "state":"ready", "endpoint":"ssh://root@217.138.104.205:30123",
#       "port_mappings":{"8080":30412}

# Then pull the SSH details (host/port/user + private key):
curl -s https://api.green-compute.com/platform/deployments/dep_.../ssh -H "X-API-Key: $GC_KEY"
# → { "ssh_host":"217.138.104.205", "ssh_port":30123, "ssh_username":"root",
#     "ssh_command":"ssh root@217.138.104.205 -p 30123",
#     "private_key":"-----BEGIN OPENSSH PRIVATE KEY-----\n..." }
```

`GET /deployments/{id}/ssh` returns **404 "SSH not available"** until the pod has
an `ssh://` endpoint (i.e. it's `ready`). It applies to **pods only** — inference
deployments are reached via `/v1/*`, not SSH.

CLI shortcut (writes the key + prints a ready-to-run command):

```bash
greencompute deployments ssh dep_... --save-key ~/.ssh/gc_pod_key
#   private key written to ~/.ssh/gc_pod_key
#   ssh -i ~/.ssh/gc_pod_key -p 30123 root@217.138.104.205
#   exposed port 8080 -> 217.138.104.205:30412
```

**2. To use your OWN key instead, put its public half in the workload `metadata`.**
It's appended to `authorized_keys` alongside the generated one, so you can SSH in
without ever pulling the generated private key.

**3. Expose extra TCP ports via `metadata.requested_ports`** (container ports;
port 22 is reserved; max 10). The chosen host ports come back as
`port_mappings` (`{container_port: host_port}`) on the deployment, all reachable
at the same `ssh_host`.

Full pod create — 10× A4000, your own key, two exposed ports, 100 GB disk:

```bash
curl -X POST https://api.green-compute.com/platform/workloads \
  -H "X-API-Key: $GC_KEY" -H "Content-Type: application/json" \
  -d '{
    "name": "a4000-pod",
    "kind": "pod",
    "image": "ghcr.io/pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime",
    "requirements": {
      "gpu_count": 10,
      "min_vram_gb_per_gpu": 16,
      "supported_gpu_models": ["a4000"]
    },
    "metadata": {
      "ssh_public_keys": ["ssh-ed25519 AAAA... you@laptop"],
      "requested_ports": [8080, 8888],
      "volume_size_gb": 100
    }
  }'
```

> Note: `gpu_count`, `requested_ports`, etc. live where shown — `gpu_count`/
> `supported_gpu_models` under `requirements`, the SSH keys/ports under
> `metadata`. (`gpu_count > 16` returns a clean **422**; up to **16** is allowed —
> this box has 10.) The CLI equivalent is one line:

```bash
greencompute workloads create-pod --name a4000-pod \
  --image ghcr.io/pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime \
  --gpu-count 10 --gpu-model a4000 \
  --ssh-pubkey "ssh-ed25519 AAAA... you@laptop" \
  --port 8080 --port 8888 --volume-size-gb 100
# then: greencompute deploy --workload-id wl_... --wait
#       greencompute deployments ssh dep_... --save-key ~/.ssh/gc_pod_key
```

---

## 5. Endpoint reference

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/platform/workloads` | Create a workload (spec) |
| `GET` | `/platform/workloads` · `/{id}` | List / get workloads |
| `POST` | `/platform/deployments` | **Spin up** a deployment |
| `GET` | `/platform/deployments` · `/{id}` | List / get deployments |
| `PATCH` | `/platform/deployments/{id}` | Update (scale, lifecycle) |
| `DELETE` | `/platform/deployments/{id}` | **Spin down** (terminate) |
| `POST` | `/platform/deployments/{id}/resume` | Resume a suspended deployment |
| `GET` | `/platform/deployments/{id}/ssh` | SSH host/port/user + private key — **pods only**, `ready` only (§4f) |
| `GET` | `/platform/deployments/{id}/stats` | Live utilization |
| `POST` | `/v1/chat/completions` · `/v1/completions` · `/v1/embeddings` | Inference (OpenAI-compatible) |
| `GET` | `/platform/nodes/supported` | GPU classes the fleet can serve |

---

## 6. CLI (`greencompute`)

```bash
# Install the internal SDK (it ships the `greencompute` CLI) per the team's setup,
# e.g.  pip install -e greencompute/sdk   from the monorepo.
greencompute config init --base-url https://api.green-compute.com --api-key $GC_KEY

# NOTE: `create-vllm` (and the `vllm` template) hardcode 24 GB VRAM + no
# gpu-model pin → they will NOT land on the A4000. For A4000, create the
# workload with the raw REST call in §4a, then deploy by --workload-id below.
# `create-vllm` is the easy path only for 4090/5090:
greencompute workloads create-vllm --model Qwen/Qwen2.5-7B-Instruct   # 4090/5090 only

# Pods on the A4000 (GPU pin + your key + ports) — one command:
greencompute workloads create-pod --name a4000-pod \
  --image ghcr.io/pytorch/pytorch:2.7.0-cuda12.8-cudnn9-runtime \
  --gpu-count 10 --gpu-model a4000 --ssh-pubkey "ssh-ed25519 AAAA... you@laptop" \
  --port 8080 --volume-size-gb 100

greencompute deploy --workload-id wl_... --wait     # spin up, block until ready
greencompute deployments list                       # see what's running
greencompute deployments get dep_...                # status/details (incl. endpoint, port_mappings)
greencompute deployments wait dep_...               # block until ready/terminal
greencompute deployments ssh dep_... --save-key ~/.ssh/gc_pod_key   # pod SSH key + command
```

> The `deploy --name --image` shortcut only sets `gpu_count`/`min_vram_gb`, **not**
> `supported_gpu_models`/`kind`/SSH/ports. For the A4000 use `workloads create-pod`
> (above) or `workloads create-vllm`/the raw REST call (§4a), then `greencompute
> deploy --workload-id …`.

Other groups: `greencompute workloads|deployments|images|builds|keys|secrets --help`.

---

## 7. Direct access to the box (raw / ad-hoc testing)

For throwaway experiments that don't need the platform's scheduling/tracking:

- **SSH + Docker** on the node itself — fastest for one-off `docker run` tests.
- **Node-agent API** on the box at `:8007` — inspect/tear down runtimes directly:
  - `GET  /agent/v1/runtimes` — list running runtimes
  - `GET  /agent/v1/gpu-status` — GPU utilization
  - `DELETE /agent/v1/deployments/{deployment_id}/terminate` — stop a runtime
  - Auth: header `X-Agent-Auth: <node auth secret>`
  - Note: *creating* runtimes still flows through the platform (the node-agent
    pulls work from leases), so use §4 to spin new things up.

---

## 8. Limits & notes

- **GPUs per workload:** 1–16 (`requirements.gpu_count`). The A4000 box has 10
  GPUs, so a single pod can claim all 10 — or run several smaller workloads side
  by side. (`gpu_count > 8` used to return a 500; fixed — cap is now 16 and an
  over-cap request gets a clean 422.)
- **Instances per deployment:** 1–64 (`requested_instances`).
- **Extra pod ports:** up to 10 via `metadata.requested_ports` (port 22 reserved).
  Mappings come back as `port_mappings` on the deployment (§4f).
- **A4000 VRAM:** 16 GB/GPU — pick models/configs that fit (or shard across GPUs).
- **A4000 visibility:** intentionally hidden from the public rentals page; this is
  expected and does not affect scheduling or the API.
- **Billing:** deployments accrue usage against the key's account (`accept_fee:true`).
  Terminate when done.
