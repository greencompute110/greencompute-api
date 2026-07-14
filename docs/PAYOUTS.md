# Miner payouts — manual quarterly distribution

**Policy (CEO/sales, 2026-07-13):** whitelisted miners do **not** earn on-chain
the moment they are approved. Payouts are **manual and quarterly**. The validator
routes **100% of its on-chain weight to a single team-controlled accumulator
hotkey**, which collects all subnet alpha emission. At the end of each quarter
the team distributes the accumulated alpha to miners **off-chain**, proportional
to the performance the validator recorded for them.

This gives us a hold on funds (no instant earning on approval), a single place
alpha lands, and a transparent, replayable performance record to distribute by.

---

## The accumulator hotkey

```
5FmpATtoNvMUisuqPNeanXxXDkhaDpYJShCNi4Xm4cXkwWKH   (uid 155 as of 2026-07-13)
```

- Hardcoded default in `services/validator/.../config.py` as
  `PAYOUT_ACCUMULATOR_HOTKEY`.
- The **uid is resolved live from the metagraph** every epoch — never hardcode
  the uid, it can be reassigned if the hotkey re-registers.
- Override without a code change via `GREENCOMPUTE_PAYOUT_ACCUMULATOR_HOTKEY`.

---

## What the validator does each epoch

Unchanged from before, except the final on-chain step:

1. Probe every whitelisted miner (inference canary) and record `ProbeResult`s.
2. Compute a `ScoreCard` per miner and persist it (`scorecard`, and the
   append-only `scorecard_history`, one row per `(hotkey, epoch)`).
3. Publish a `WeightSnapshot` — the per-miner performance ledger (this is what
   the audit replays; it is **not** what goes on-chain while accumulation is on).
4. **On-chain:** set `weights = { accumulator_uid: 1.0 }` — 100% to the
   accumulator, 0 to everyone else.
   (`ValidatorService._commit_accumulator_weight`.)

**Fail-safe:** if the accumulator hotkey is not in the metagraph, the validator
sets **no** weights that epoch rather than fall back to distributing — that would
leak the exact emissions this policy is meant to withhold.

So: miners are fully scored every epoch, but all alpha accrues to the
accumulator. The scorecards are the record we pay out by.

---

## The distribution formula

### Per-epoch performance score

Each epoch the score engine computes, per miner
(`domain/scoring.py::ScoreEngine.compute_scorecard`):

```
final_score = capacity_weight
            × security^α
            × reliability^β
            × performance^γ
            × fraud_penalty
            × utilization^δ
            × (1 + rental_bonus)
```

| Factor | Meaning | Default |
|---|---|---|
| `capacity_weight` | Σ over the miner's nodes of `gpu_count × vram_gb_per_gpu` — how much hardware it brings | — |
| `security` | security tier multiplier | `1.0` |
| `reliability` | `base × success_rate × readiness_penalty` over a trailing 7-day probe window | β = `1.3` |
| `performance` | `0.5 × latency_component + 0.5 × throughput_component` | γ = `1.1` |
| `fraud_penalty` | `signature × proxy × consistency × readiness × success` (≤ 1.0) | — |
| `utilization` | `(inference_gpus + rental_gpus) / total_gpus` — how busy the node is | δ = `0.8` |
| `rental_bonus` | small bonus for serving rentals, capped | ≤ `0.1` |

Exponents are `score_alpha/beta/gamma/delta` in config (env-overridable). This is
exactly the score the on-chain weight used to be proportional to — we just
accumulate instead of distributing it live.

### Per-quarter share

For each miner *i*, sum its per-epoch `final_score` over every epoch in the
quarter it was scored:

```
S_i     = Σ  final_score(i, epoch)      over epochs in [quarter_start, quarter_end)
share_i = S_i / Σ_j S_j
payout_i = total_alpha_accumulated × share_i
```

Summing across epochs (rather than averaging) means a miner is rewarded for both
**how well** it performed and **how much of the quarter** it was online and
serving — a node offline for half the quarter accrues scores for fewer epochs.
If you'd rather normalise out uptime and pay purely on average quality, use
`AVG(final_score)` instead of `SUM(final_score)` below.

### The exact query

The performance ledger is the `scorecard_history` table (append-only, one row
per hotkey per epoch). Quarterly shares:

```sql
WITH q AS (
  SELECT hotkey, SUM(final_score) AS score_sum
  FROM   scorecard_history
  WHERE  computed_at >= :quarter_start   -- e.g. '2026-04-01'
    AND  computed_at <  :quarter_end     -- e.g. '2026-07-01'
  GROUP  BY hotkey
)
SELECT hotkey,
       score_sum,
       score_sum / SUM(score_sum) OVER () AS share      -- fraction of the pot
FROM   q
ORDER  BY share DESC;
```

Multiply each `share` by the total alpha the accumulator received over the
quarter to get each miner's payout. (Only hotkeys that were whitelisted and
scored appear — non-whitelisted miners are skipped upstream and never earn.)

### Worked example

Quarter accumulated **10,000 α**. Three miners:

| hotkey | Σ final_score | share | payout |
|---|---|---|---|
| A | 6,000 | 60% | 6,000 α |
| B | 3,000 | 30% | 3,000 α |
| C | 1,000 | 10% | 1,000 α |

---

## Auditability

The per-epoch scorecards and the `WeightSnapshot` are anchored on-chain in the
per-epoch audit report (`generate_audit_report` → `Commitments.set_commitment`),
so an independent party (greencompute-audit) can replay the ScoreEngine formula
from the raw probes and verify the performance numbers we distribute by were not
fudged. The audit proves **scoring honesty**; the accumulation routing is a
separate, documented business policy.

---

## Configuration

| Env var | Default | Effect |
|---|---|---|
| `GREENCOMPUTE_PAYOUT_ACCUMULATION_ENABLED` | `true` | `true` = 100% weight to the accumulator (this policy). `false` = distribute on-chain per-miner by `final_score`. |
| `GREENCOMPUTE_PAYOUT_ACCUMULATOR_HOTKEY` | `5FmpATto…4cXkwWKH` | The hotkey that accumulates alpha. |

Switching `PAYOUT_ACCUMULATION_ENABLED=false` reverts to on-chain per-miner
distribution (`_commit_distributed_weights`) with no other change — the
scorecards feeding it are computed identically.
