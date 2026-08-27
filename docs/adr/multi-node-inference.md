# Multi-node distributed inference — scoping

**Status:** proposed / not started · **Date:** 2026-07-13
**Trigger:** CEO ask — serve Kimi K3 (2.8T MoE) as a catalog model on multi-5090
nodes, "like SN53 does on 80x 5090".

---

## TL;DR

Green Compute today serves **one model on one node, capped at 8 GPUs**. Kimi K3
needs roughly **1.4 TB of VRAM (~56–64x RTX 5090) coordinated as a single
engine** over a fast interconnect. Closing that gap is a **new product line
(a distributed-inference platform) plus dedicated co-located cluster hardware** —
on the order of **1–2 quarters of engineering and real capex** — not a catalog
entry. Recommend: do not pursue on the current fleet; ship the single-node
capability we already almost have, and treat multi-node as a separate strategic
decision.

---

## 1. Why K3 doesn't fit

| | |
|---|---|
| K3 total params | **2.78T** (MoE: 896 routed experts, 16 active/token) |
| Active params | ~50–104B (disputed) — *irrelevant to VRAM: all expert weights must be resident* |
| Shipped format | **native MXFP4** weights + MXFP8 activations (quantization-aware trained). **No FP8/INT4/AWQ checkpoint at launch** — you cannot simply quantize it smaller |
| Weights on disk | **~1.4–1.56 TB** (MXFP4); ~5.6 TB BF16-equivalent |
| Min serving memory | **~2.3 TB** aggregate (vendor guidance: 8x B300, or 16x B200) |
| One 8x5090 node | 256 GB raw → **~200–220 GB usable** after KV cache + overhead |
| **Gap** | **~6–8x larger than an entire node**, before 1M-context KV cache |

A single K3 replica therefore needs **~7–8 of our nodes (56–64 GPUs) acting as
one engine**. SN53's ~80x 5090 figure is consistent once KV cache and headroom
are added — i.e. SN53 is doing exactly the cross-box distributed serving we
don't do.

Sources: [MarkTechPost launch coverage](https://www.marktechpost.com/2026/07/16/moonshot-ai-releases-kimi-k3-a-2-8-trillion-parameter-open-moe-model-with-kimi-delta-attention-and-1m-context/),
[RunPod K3 FAQ](https://www.runpod.io/articles/guides/kimi-k3-technical-faq),
[HF model overview](https://huggingface.co/blog/ResterChed/kimi-k3-model-overview-mxfp4-quantization-open-wei).
K3 is post-cutoff and specifics are still settling — treat sizes as ~90%
confidence, directionally certain.

## 2. What our stack does today (verified in code)

- **One model = one `docker run` on one node.** No Ray, no `--nnodes`, no
  pipeline/expert parallelism across boxes. Repo-wide grep for
  `nnodes|pipeline.parallel|ray start|ray.init` → **zero hits**.
  (`greencompute-node/.../domain/inference.py:687+`)
- **Hard 8-GPU ceiling in the catalog schema.** `ModelCatalogEntry.gpu_count`
  and `CatalogSubmission.gpu_count` are `Field(ge=1, le=8)`
  (`greencompute/protocol/.../models.py:1135,1159`). A >8-GPU model **cannot be
  expressed**. (`WorkloadRequirements.gpu_count` allows ≤16 for the internal
  10x A4000 box, but placement is still single-node.)
- **Placement is strictly per-node.** Flux requires
  `gpu_count <= inference_gpu_count` of a *single* node
  (`greencompute-api/.../domain/flux.py:64,69–70`) and pins each replica to one
  `target_node_id` (`application/services.py:744,835–839`). Nothing assembles a
  replica from GPUs spanning two boxes.
- **"Multi-node" in our code means one miner owning several boxes** (inventory /
  scoring), never one model spanning boxes.
- ~~**Within-node TP was broken**~~ — `tensor_parallel_size` never reached vLLM,
  so multi-GPU models silently ran at TP=1. **Fixed 2026-07-13**
  (greencompute-node `fix-tensor-parallel-propagation`); this was the
  prerequisite for any real 8x5090 serving.

## 3. What would have to be built

| # | Component | Effort | Notes |
|---|---|---|---|
| A | ~~Within-node TP propagation~~ | ~~S~~ | **Done** 2026-07-13 — unblocks single-node 8-GPU serving |
| B | **Multi-node launcher** in node-agent: Ray head+workers or vLLM/SGLang `--nnodes`/`--pipeline-parallel-size`; distributed-replica lifecycle, health, teardown | **L** (multi-week) | Structural rewrite — node-agent assumes 1 `docker run` = 1 replica |
| C | **Cross-node placement engine**: co-schedule GPU blocks across several nodes/hotkeys for ONE replica; partial-failure handling; topology/co-location constraints | **L** (multi-week) | Flux is single-node to its core |
| D | **Interconnect provisioning**: inter-node NCCL, RDMA fabric, IP/port coordination, secure networking between miner boxes | **L**, often *physically impossible* on the current fleet | See §4 |
| E | **Catalog/workload schema**: drop `le=8`, add node-count + parallelism degree (TP×PP) + interconnect requirements; scoring/billing for multi-node replicas | **M** | Also: how does a multi-node replica get scored/paid? |
| F | **MXFP4 + 1M-context support**: vLLM/SGLang MXFP4 weight support, KDA/MLA attention, KV-cache strategy at 1M context | **M–L** | Gated on upstream framework maturity |

**Total: ~1–2 quarters of focused engineering**, and it only pays off if the
hardware in §4 exists.

## 4. The interconnect reality (the actual blocker)

Even with all the software, the physical fleet can't run it:

- **RTX 5090 is a consumer card: no NVLink, no NVSwitch.** Within a box, GPUs
  talk over PCIe; across boxes, whatever Ethernet the miner happens to have.
- **Our miners are dispersed, heterogeneous, rented consumer boxes** in
  different datacenters — no guaranteed RDMA/InfiniBand, typically none.
- Multi-node MoE shuttles expert activations between nodes **every token**.
  Tensor parallelism over commodity Ethernet between geographically separate
  boxes is so latency-bound that throughput collapses. This is why credible K3
  deployments specify tight clusters (8x B300 single-node; 16x B200 two-node
  with fast fabric), not scattered consumer boxes.
- **SN53's 80x 5090 is presumably a purpose-built, co-located cluster with real
  interconnect** — a different physical substrate than our miner fleet. Matching
  it means *building or renting that cluster*, not just writing code.

## 5. Recommendation

1. **Ship single-node properly (now).** With the TP fix landed, an 8x5090 node
   can genuinely serve models up to **~200–220 GB** of weights. That's a
   substantial catalog upgrade and costs us nothing further. **Do this.**
2. **Offer a "Kimi" that fits one node (watch-and-wait).** If Moonshot ships a
   K3-mini/distill or a single-node-sized quantized checkpoint, host that and
   market it as the Kimi option. *No such checkpoint exists at launch.*
   Note K2 (~1T) does **not** fit one node either.
3. **Treat full multi-node K3 as a strategic bet, not a ticket.** It requires
   both the §3 build **and** dedicated co-located high-interconnect hardware
   (§4). That's a company-direction + capex decision for the CEO — worth taking
   only if distributed serving of frontier open models is a product we want to
   *be in*, not as a one-off to list K3.

## 6. Open questions for the CEO

- Is the goal "have K3 on the menu" (→ options 1–2, or route to an external
  provider) or "be a distributed-inference platform" (→ option 3)?
- Is there appetite for dedicated co-located cluster hardware, or must
  everything run on the dispersed miner fleet? (This single answer decides
  whether option 3 is even possible.)
- How would multi-node replicas be scored and paid? One replica spanning 8
  miners breaks the current per-hotkey scoring/emission model.
