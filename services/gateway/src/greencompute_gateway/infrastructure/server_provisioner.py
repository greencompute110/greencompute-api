"""SSH-based provisioning of a miner node-agent onto a provider's server.

Given SSH access to a fresh Linux + NVIDIA box, this automates exactly what
an operator does by hand:

  1. detect GPU (nvidia-smi), CPU cores, RAM, public IP
  2. ensure Docker + NVIDIA Container Toolkit are installed + configured
  3. clone the protocol + node-agent repos (or pull if present)
  4. write a `.env` (HMAC auth mode, generated auth_secret, detected
     hardware, unique node_id, public control-plane/validator URLs, the
     box's own public IP as api_base_url)
  5. `docker compose up -d`
  6. verify `:8007/readyz`

Runs in a background thread (paramiko is blocking). Progress is streamed
back via the `on_log` / `on_status` callbacks so the UI can show it live.
The SSH credential is held only for the duration of this call and is never
persisted.

Security posture: this connects OUT to a provider-supplied host with
provider-supplied credentials. It is gated at the route layer to
authenticated users onboarding a *whitelisted* hotkey. We do not execute
anything the provider sends back as data; we only run our own fixed command
set and parse known-format output.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


# Public endpoints the provisioned node should talk to. Arbitrary provider
# boxes can be anywhere, so default to the public subdomains; override via
# gateway env for non-standard deployments.
def _control_plane_url() -> str:
    return (os.environ.get("GREENCOMPUTE_ONBOARD_CONTROL_PLANE_URL", "").strip()
            or "https://control.green-compute.com")


def _validator_url() -> str:
    return (os.environ.get("GREENCOMPUTE_ONBOARD_VALIDATOR_URL", "").strip()
            or "https://validator.green-compute.com")


def _repo_urls() -> tuple[str, str]:
    protocol = (os.environ.get("GREENCOMPUTE_ONBOARD_PROTOCOL_REPO", "").strip()
                or "https://github.com/greencompute110/greencompute.git")
    node = (os.environ.get("GREENCOMPUTE_ONBOARD_NODE_REPO", "").strip()
            or "https://github.com/greencompute110/greencompute-node.git")
    return protocol, node


@dataclass
class ProvisionInputs:
    server_id: str
    hotkey: str
    payout_address: str
    label: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    ssh_password: str
    ssh_private_key: str
    hf_token: str
    node_id: str


@dataclass
class ProvisionResult:
    ok: bool
    error: str = ""
    gpu_model: str = ""
    gpu_count: int = 0
    vram_gb_per_gpu: int = 0
    cpu_cores: int = 0
    memory_gb: int = 0
    public_ip: str = ""


# --- gpu model normalization (matches billing_rates keys) ------------------

def _normalize_gpu(raw: str) -> str:
    s = raw.lower()
    if "5090" in s:
        return "rtx5090"
    if "4090" in s:
        return "rtx4090"
    if "h100" in s:
        return "h100"
    if "a100" in s:
        return "a100"
    if "l40" in s:
        return "l40s"
    # fall back to a stripped token
    return re.sub(r"[^a-z0-9]", "", s)[:32] or "unknown"


class _SSH:
    """Thin paramiko wrapper with a logging `run`."""

    def __init__(self, inp: ProvisionInputs, on_log):
        import paramiko  # imported lazily so the module loads even if absent

        self._paramiko = paramiko
        self.on_log = on_log
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = dict(
            hostname=inp.ssh_host,
            port=inp.ssh_port,
            username=inp.ssh_user,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
            look_for_keys=False,
            allow_agent=False,
        )
        if inp.ssh_private_key.strip():
            from io import StringIO
            key = None
            last_exc = None
            for kcls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                try:
                    key = kcls.from_private_key(StringIO(inp.ssh_private_key))
                    break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
            if key is None:
                raise ValueError(f"could not parse SSH private key: {last_exc}")
            connect_kwargs["pkey"] = key
        elif inp.ssh_password:
            connect_kwargs["password"] = inp.ssh_password
        else:
            raise ValueError("no SSH credential provided (password or private key)")
        self.client.connect(**connect_kwargs)

    def run(self, cmd: str, *, timeout: int = 600, log_cmd: str | None = None, check: bool = True) -> tuple[int, str]:
        """Run a command, stream a short header to the log, return (rc, output)."""
        self.on_log(f"$ {log_cmd or cmd}\n")
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        out_chunks: list[str] = []
        # Read combined output incrementally so long installs stream.
        channel = stdout.channel
        while True:
            if channel.recv_ready():
                data = channel.recv(4096).decode(errors="replace")
                out_chunks.append(data)
                self.on_log(data)
            if channel.recv_stderr_ready():
                data = channel.recv_stderr(4096).decode(errors="replace")
                out_chunks.append(data)
                self.on_log(data)
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            time.sleep(0.05)
        rc = channel.recv_exit_status()
        out = "".join(out_chunks)
        if check and rc != 0:
            raise RuntimeError(f"command failed (rc={rc}): {log_cmd or cmd}\n{out[-2000:]}")
        return rc, out

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass


def provision_server(inp: ProvisionInputs, on_log, on_status) -> ProvisionResult:
    """Main entry — runs the full provisioning. Returns a ProvisionResult.
    Never raises; failures are captured in result.error + logged."""
    try:
        import paramiko  # noqa: F401
    except ImportError:
        msg = "paramiko not installed in gateway image"
        on_log(msg + "\n")
        return ProvisionResult(ok=False, error=msg)

    ssh: _SSH | None = None
    try:
        on_status("provisioning")
        on_log(f"Connecting to {inp.ssh_user}@{inp.ssh_host}:{inp.ssh_port} …\n")
        ssh = _SSH(inp, on_log)
        on_log("SSH connected.\n\n")

        # --- 1. detect hardware -------------------------------------------
        on_log("== Detecting hardware ==\n")
        rc, gpu_out = ssh.run(
            "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true",
            log_cmd="nvidia-smi (gpu query)", check=False,
        )
        gpu_lines = [l.strip() for l in gpu_out.splitlines() if l.strip()]
        if not gpu_lines:
            raise RuntimeError(
                "no NVIDIA GPU detected (nvidia-smi returned nothing). "
                "Ensure the NVIDIA driver is installed (>=545) before onboarding."
            )
        gpu_count = len(gpu_lines)
        first = gpu_lines[0]
        name_part = first.split(",")[0].strip()
        gpu_model = _normalize_gpu(name_part)
        # VRAM: parse "24576 MiB" -> 24
        vram_gb = 0
        m = re.search(r"(\d+)\s*MiB", first)
        if m:
            vram_gb = round(int(m.group(1)) / 1024)
        on_log(f"  GPUs: {gpu_count} x {name_part}  ->  model={gpu_model}, vram={vram_gb}GB each\n")

        _, cpu_out = ssh.run("nproc", log_cmd="nproc", check=False)
        cpu_cores = int(re.sub(r"\D", "", cpu_out.strip() or "0") or 0)
        _, mem_out = ssh.run(
            "awk '/MemTotal/ {print int($2/1024/1024)}' /proc/meminfo",
            log_cmd="memory total (GB)", check=False,
        )
        memory_gb = int(re.sub(r"\D", "", mem_out.strip() or "0") or 0)
        _, ip_out = ssh.run(
            "curl -fsSL -4 https://ifconfig.me 2>/dev/null || curl -fsSL -4 https://api.ipify.org 2>/dev/null || true",
            log_cmd="detect public IP", check=False,
        )
        public_ip = ip_out.strip().splitlines()[-1].strip() if ip_out.strip() else ""
        on_log(f"  CPU cores: {cpu_cores} | RAM: {memory_gb}GB | public IP: {public_ip or 'unknown'}\n\n")
        if not public_ip:
            raise RuntimeError(
                "could not determine the box's public IP — the gateway must be "
                "able to reach this node at <public_ip>:8007 for inference routing."
            )

        # --- 2. ensure docker + nvidia toolkit ----------------------------
        on_log("== Ensuring Docker + NVIDIA Container Toolkit ==\n")
        ssh.run(
            "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && "
            "apt-get install -y -qq curl git ca-certificates >/dev/null 2>&1; echo apt-base-ok",
            log_cmd="apt-get update + base packages", timeout=600,
        )
        # Docker
        ssh.run(
            "if ! command -v docker >/dev/null 2>&1; then "
            "curl -fsSL https://get.docker.com | sh >/dev/null 2>&1; fi; "
            "docker --version",
            log_cmd="install docker (if missing)", timeout=600,
        )
        # docker compose plugin
        ssh.run(
            "docker compose version >/dev/null 2>&1 || "
            "apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1; "
            "docker compose version",
            log_cmd="ensure docker compose plugin", timeout=600, check=False,
        )
        # NVIDIA container toolkit
        ssh.run(
            "if ! command -v nvidia-ctk >/dev/null 2>&1; then "
            "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | "
            "gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg 2>/dev/null; "
            "curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | "
            "sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' "
            "> /etc/apt/sources.list.d/nvidia-container-toolkit.list; "
            "apt-get update -qq >/dev/null 2>&1; "
            "apt-get install -y -qq nvidia-container-toolkit >/dev/null 2>&1; fi; "
            "nvidia-ctk --version 2>/dev/null | head -1",
            log_cmd="install nvidia-container-toolkit (if missing)", timeout=600, check=False,
        )
        ssh.run(
            "nvidia-ctk runtime configure --runtime=docker >/dev/null 2>&1; "
            "systemctl restart docker >/dev/null 2>&1 || service docker restart >/dev/null 2>&1 || true; "
            "sleep 3; docker info >/dev/null 2>&1 && echo docker-ok",
            log_cmd="configure docker nvidia runtime + restart", timeout=120, check=False,
        )
        on_log("\n")

        # --- 3. clone repos ----------------------------------------------
        on_log("== Cloning repos ==\n")
        protocol_repo, node_repo = _repo_urls()
        ssh.run(
            f"mkdir -p /opt/greencompute && cd /opt/greencompute && "
            f"(test -d greencompute/.git && (cd greencompute && git pull -q) || git clone -q {protocol_repo} greencompute) && "
            f"(test -d greencompute-node/.git && (cd greencompute-node && git pull -q) || git clone -q {node_repo} greencompute-node) && "
            f"echo repos-ok",
            log_cmd="clone/update protocol + node repos", timeout=300,
        )
        on_log("\n")

        # --- 4. write .env -----------------------------------------------
        on_log("== Writing node .env ==\n")
        auth_secret = "gcs_" + secrets.token_urlsafe(24)
        node_id = inp.node_id
        envfile = _render_env(
            hotkey=inp.hotkey,
            payout=inp.payout_address,
            auth_secret=auth_secret,
            control_plane_url=_control_plane_url(),
            validator_url=_validator_url(),
            api_base_url=f"http://{public_ip}:8007",
            node_id=node_id,
            gpu_model=gpu_model,
            gpu_count=gpu_count,
            vram_gb=vram_gb,
            cpu_cores=cpu_cores,
            memory_gb=memory_gb,
            hf_token=inp.hf_token,
        )
        # Write the .env via a heredoc to avoid quoting hell. PROTOCOL_DIR
        # points the compose mount at the sibling protocol clone.
        import base64
        env_b64 = base64.b64encode(envfile.encode()).decode()
        ssh.run(
            f"cd /opt/greencompute/greencompute-node && "
            f"echo {env_b64} | base64 -d > .env && "
            f"grep -q PROTOCOL_DIR .env || echo 'PROTOCOL_DIR=../greencompute/protocol' >> .env && "
            f"echo env-written",
            log_cmd="write .env (auth_secret redacted)",
        )
        on_log(f"  node_id={node_id}  auth_mode=hmac  api_base_url=http://{public_ip}:8007\n\n")

        # --- 5. start the node-agent -------------------------------------
        on_log("== Starting node-agent (docker compose up -d) ==\n")
        ssh.run(
            "cd /opt/greencompute/greencompute-node && docker compose up -d 2>&1 | tail -8",
            log_cmd="docker compose up -d", timeout=900,
        )
        on_log("\n")

        # --- 6. verify ----------------------------------------------------
        on_log("== Verifying node-agent health ==\n")
        ready = False
        for attempt in range(1, 13):  # ~2 min
            rc, out = ssh.run(
                "curl -fsS -m 5 http://127.0.0.1:8007/readyz 2>/dev/null || echo NOTREADY",
                log_cmd=f"readyz check (attempt {attempt})", check=False,
            )
            if "NOTREADY" not in out and out.strip():
                ready = True
                break
            time.sleep(10)
        if not ready:
            raise RuntimeError(
                "node-agent did not become ready on :8007 within ~2 min. "
                "Check `docker compose logs` on the box."
            )
        on_log("  node-agent is READY on :8007\n\n")

        # firewall reminder (best-effort open of 8007 to the world; provider
        # may scope further). We only attempt if ufw is active.
        ssh.run(
            "command -v ufw >/dev/null 2>&1 && ufw status | grep -q active && "
            "ufw allow 8007/tcp >/dev/null 2>&1 && echo 'opened 8007 in ufw' || "
            "echo 'no ufw / not active — ensure port 8007 is reachable from the validator'",
            log_cmd="ensure port 8007 reachable", check=False,
        )
        on_log("\n✅ Provisioning complete. The node will register + start sending heartbeats.\n")
        on_log("   It appears in the fleet once the validator sees it (whitelist required).\n")

        return ProvisionResult(
            ok=True, gpu_model=gpu_model, gpu_count=gpu_count,
            vram_gb_per_gpu=vram_gb, cpu_cores=cpu_cores, memory_gb=memory_gb,
            public_ip=public_ip,
        )
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        log.warning("provisioning failed for server %s: %s", inp.server_id, err)
        on_log(f"\n❌ FAILED: {err}\n")
        return ProvisionResult(ok=False, error=err)
    finally:
        if ssh is not None:
            ssh.close()


def _render_env(**kw) -> str:
    return f"""# Auto-generated by Green Compute provider onboarding.
GREENCOMPUTE_CONTROL_PLANE_URL={kw['control_plane_url']}
GREENCOMPUTE_MINER_VALIDATOR_URL={kw['validator_url']}

GREENCOMPUTE_MINER_HOTKEY={kw['hotkey']}
GREENCOMPUTE_MINER_PAYOUT_ADDRESS={kw['payout']}
GREENCOMPUTE_MINER_AUTH_SECRET={kw['auth_secret']}
GREENCOMPUTE_MINER_API_BASE_URL={kw['api_base_url']}
GREENCOMPUTE_MINER_NODE_ID={kw['node_id']}

# Auth: HMAC (no Bittensor wallet on this box).
GREENCOMPUTE_AUTH_MODE=hmac

# Hardware (auto-detected).
GREENCOMPUTE_GPU_MODEL={kw['gpu_model']}
GREENCOMPUTE_GPU_COUNT={kw['gpu_count']}
GREENCOMPUTE_VRAM_GB_PER_GPU={kw['vram_gb']}
GREENCOMPUTE_CPU_CORES={kw['cpu_cores']}
GREENCOMPUTE_MEMORY_GB={kw['memory_gb']}
GREENCOMPUTE_PERFORMANCE_SCORE=1.0
GREENCOMPUTE_GPU_SPLIT_UNITS=100

# Backends.
GREENCOMPUTE_POD_BACKEND=process
GREENCOMPUTE_VM_BACKEND=stub
GREENCOMPUTE_INFERENCE_BACKEND=docker
GREENCOMPUTE_SUPPORTED_WORKLOAD_KINDS=inference,pod,vm

# HuggingFace (for gated model pulls).
HF_TOKEN={kw['hf_token']}
HF_HOME=/root/.cache/huggingface

# Agent loop.
GREENCOMPUTE_BOOTSTRAP_MINER=true
GREENCOMPUTE_ENABLE_BACKGROUND_WORKERS=true
GREENCOMPUTE_WORKER_POLL_INTERVAL_SECONDS=1.0
"""
