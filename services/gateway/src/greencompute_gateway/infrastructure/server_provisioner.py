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

import ipaddress
import logging
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


class HostNotAllowedError(ValueError):
    """Raised when an onboarding ssh_host resolves to a disallowed
    (private/loopback/link-local/multicast/reserved/unspecified) address.
    Subclasses ValueError so the route layer maps it to a 400."""


def resolve_and_guard_host(host: str, port: int = 22) -> str:
    """Resolve `host` and REJECT it if ANY resolved address is private,
    loopback, link-local, multicast, reserved, or unspecified (0.0.0.0/::).

    This is the load-bearing SSRF control for provider onboarding: it stops a
    user pointing the gateway's outbound SSH at internal infra
    (169.254.169.254 cloud metadata, 127.0.0.1, 10/8, 172.16/12, 192.168/16,
    ::1, fc00::/7, the control-plane/postgres containers, etc.).

    Returns the original host on success (we keep the hostname so paramiko can
    still do its own connect/host-key handling); raises HostNotAllowedError on
    any violation. An operator may further restrict via the CIDR allowlist env
    GREENCOMPUTE_ONBOARD_HOST_ALLOWLIST.
    """
    host = (host or "").strip()
    if not host:
        raise HostNotAllowedError("ssh_host is required")
    # Reject control chars / whitespace outright (also blocks header/CRLF
    # smuggling into downstream commands).
    if any(c in host for c in "\r\n\t ") or any(ord(c) < 32 for c in host):
        raise HostNotAllowedError("ssh_host contains invalid characters")

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise HostNotAllowedError(f"could not resolve ssh_host: {host}") from exc

    addrs: list[ipaddress._BaseAddress] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            raise HostNotAllowedError(f"ssh_host resolved to an invalid address: {ip_str}")
        addrs.append(addr)

    if not addrs:
        raise HostNotAllowedError(f"ssh_host did not resolve to any address: {host}")

    allowlist = _host_allowlist()
    for addr in addrs:
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise HostNotAllowedError(
                f"ssh_host {host} resolves to a disallowed address ({addr}) — "
                "private/loopback/link-local/multicast/reserved hosts are not "
                "allowed for onboarding"
            )
        if allowlist and not any(addr in net for net in allowlist):
            raise HostNotAllowedError(
                f"ssh_host {host} ({addr}) is not in the onboarding allowlist"
            )
    return host


def _host_allowlist() -> list:
    """Optional operator allowlist of CIDRs (comma-separated) that onboarding
    targets must fall within. Empty/unset = no extra restriction beyond the
    private/loopback/etc. rejection."""
    raw = (os.environ.get("GREENCOMPUTE_ONBOARD_HOST_ALLOWLIST", "") or "").strip()
    if not raw:
        return []
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("ignoring invalid onboarding allowlist CIDR: %r", part)
    return nets


def _env_safe(value: str, *, field: str) -> str:
    """Reject control characters (esp. CR/LF) in a value destined for the
    node `.env`. A newline in payout_address/hotkey/hf_token/label would
    inject/override arbitrary GREENCOMPUTE_* env lines on the provisioned
    node (e.g. flip auth mode, repoint the validator URL). The base64 heredoc
    protects the SHELL transport but NOT the .env file CONTENT, so this guard
    must run at render time."""
    if value is None:
        return ""
    if any(c in value for c in "\r\n") or any(ord(c) < 32 and c != "\t" for c in value):
        raise ValueError(f"{field} contains invalid control characters")
    return value


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


def _known_hosts_path() -> str:
    """Where the gateway persists captured host keys (TOFU pin file). Override
    via env for tests / non-default deployments."""
    return (
        os.environ.get("GREENCOMPUTE_ONBOARD_KNOWN_HOSTS", "").strip()
        or "/var/lib/greencompute/onboard_known_hosts"
    )


def _make_host_key_policy(paramiko, on_log):
    """Trust-on-first-use with pinning: capture+store the host key on the
    FIRST connect, and (because we load the known_hosts file before connecting)
    a CHANGED key on re-provision triggers paramiko's RejectPolicy / known-hosts
    mismatch (SSHException), which we let propagate. We never blindly AutoAdd a
    key that contradicts a stored one."""

    class _PinFirstSeenPolicy(paramiko.MissingHostKeyPolicy):
        """Only fires when the host key is MISSING from known_hosts (i.e. first
        contact). A key that is PRESENT-but-DIFFERENT never reaches this policy
        — paramiko raises BadHostKeyException before calling it — so this is
        safe TOFU. Captures the fingerprint to the provision log for audit."""

        def missing_host_key(self, client, hostname, key):  # noqa: D401
            fp = key.get_fingerprint().hex()
            try:
                on_log(f"  host key (first contact) {key.get_name()} SHA fingerprint={fp}\n")
            except Exception:  # noqa: BLE001
                pass
            client.get_host_keys().add(hostname, key.get_name(), key)
            path = _known_hosts_path()
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                client.save_host_keys(path)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not persist onboarding host key to %s: %s", path, exc)

    return _PinFirstSeenPolicy()


class _SSH:
    """Thin paramiko wrapper with a logging `run`."""

    def __init__(self, inp: ProvisionInputs, on_log):
        import paramiko  # imported lazily so the module loads even if absent

        self._paramiko = paramiko
        self.on_log = on_log
        # Anti-DNS-rebind: re-validate the destination right before connecting,
        # not only at the API edge. A host that passed the sync pre-queue check
        # could re-resolve to an internal IP by the time the background task
        # runs; this closes that window.
        resolve_and_guard_host(inp.ssh_host, inp.ssh_port)
        self.client = paramiko.SSHClient()
        # Load any previously-captured host keys so a CHANGED key on
        # re-provision is rejected (BadHostKeyException) instead of silently
        # trusted. First contact is captured by the pin policy below.
        try:
            kh = _known_hosts_path()
            if os.path.exists(kh):
                self.client.load_host_keys(kh)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load onboarding known_hosts: %s", exc)
        self.client.set_missing_host_key_policy(_make_host_key_policy(paramiko, on_log))
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
    # Sanitize every value that lands in the .env. A CR/LF in any of these
    # would inject/override arbitrary GREENCOMPUTE_* lines on the node.
    hotkey = _env_safe(kw["hotkey"], field="hotkey")
    payout = _env_safe(kw["payout"], field="payout_address")
    auth_secret = _env_safe(kw["auth_secret"], field="auth_secret")
    api_base_url = _env_safe(kw["api_base_url"], field="api_base_url")
    node_id = _env_safe(kw["node_id"], field="node_id")
    gpu_model = _env_safe(kw["gpu_model"], field="gpu_model")
    hf_token = _env_safe(kw["hf_token"], field="hf_token")
    control_plane_url = _env_safe(kw["control_plane_url"], field="control_plane_url")
    validator_url = _env_safe(kw["validator_url"], field="validator_url")
    return f"""# Auto-generated by Green Compute provider onboarding.
GREENCOMPUTE_CONTROL_PLANE_URL={control_plane_url}
GREENCOMPUTE_MINER_VALIDATOR_URL={validator_url}

GREENCOMPUTE_MINER_HOTKEY={hotkey}
GREENCOMPUTE_MINER_PAYOUT_ADDRESS={payout}
GREENCOMPUTE_MINER_AUTH_SECRET={auth_secret}
GREENCOMPUTE_MINER_API_BASE_URL={api_base_url}
GREENCOMPUTE_MINER_NODE_ID={node_id}

# Auth: HMAC (no Bittensor wallet on this box).
GREENCOMPUTE_AUTH_MODE=hmac

# Hardware (auto-detected).
GREENCOMPUTE_GPU_MODEL={gpu_model}
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
HF_TOKEN={hf_token}
HF_HOME=/root/.cache/huggingface

# Agent loop.
GREENCOMPUTE_BOOTSTRAP_MINER=true
GREENCOMPUTE_ENABLE_BACKGROUND_WORKERS=true
GREENCOMPUTE_WORKER_POLL_INTERVAL_SECONDS=1.0
"""
