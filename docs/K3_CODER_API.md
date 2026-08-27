# Kimi K3 Coder on GreenCompute — API Guide

Kimi K3 Coder served at a **verified 1,000,000-token context** on **48× RTX 5090
running on 100% renewable energy**. OpenAI-compatible, so any existing client or
agent framework works by changing two lines.

| | |
|---|---|
| **Base URL** | `https://api.green-compute.com/v1` |
| **Model ID** | `k3-coder` |
| **Auth** | `Authorization: Bearer <YOUR_KEY>` |
| **Price** | **$1.00 / 1M input · $5.00 / 1M output** |
| **Context** | **1,048,576 tokens** |

## The context number is measured, not advertised

Plenty of endpoints put a large number in their metadata. This one was verified
end to end before it was published:

| | |
|---|---|
| Configured limit | 1,048,576 |
| Physical KV pool in the boot logs | 1,048,576 |
| **Largest real prompt served** | **1,010,214 tokens** |
| Retrieval at that size | **5 of 5 needles**, at the start, quarter, middle, three-quarter and end |

The test used the checkpoint's own tokenizer (not a character estimate) and real
source files with unique `file:line` markers — repeated filler would prove
allocation, not comprehension.

## What the model is

A pruned Kimi K3: 320 of the original 896 routed experts retained, ~1.03T total
parameters, native MXFP4, expert selection calibrated for code. It is smaller and
cheaper to serve than full K3, which is why the price is half.

**Use it for code**: repository analysis, refactors across many files, review,
agent workflows. Published comparisons show small code-perplexity degradation but
noticeably weaker general knowledge, so do not treat it as a general-purpose
frontier model.

---

## Quick start

```bash
curl https://api.green-compute.com/v1/chat/completions \
  -H "Authorization: Bearer $GREENCOMPUTE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "k3-coder",
    "messages": [{"role": "user", "content": "What is the capital of France?"}],
    "max_tokens": 600
  }'
```

## Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://api.green-compute.com/v1",
    api_key="YOUR_KEY",
)

resp = client.chat.completions.create(
    model="k3-coder",
    messages=[{"role": "user", "content": "Explain mixture-of-experts briefly."}],
    max_tokens=600,          # see "budget max_tokens generously" below
)
print(resp.choices[0].message.content)
```

---

## Read this before wiring up an agent

### 1. K3 is a reasoning model — budget `max_tokens` generously

K3 *thinks* before it answers. That thinking is generated as tokens, and if
`max_tokens` cuts it off mid-thought you get **no answer at all** — `content`
comes back holding raw chain-of-thought and `reasoning` comes back empty.

- **Use `max_tokens` ≥ 600.** At 220 the model never finished thinking.
- The response splits cleanly when it completes:
  - `message.content` → the final answer
  - `message.reasoning` → the chain of thought

```python
msg = resp.choices[0].message
answer = msg.content              # show this to the user
thinking = getattr(msg, "reasoning", None)   # usually hide this
```

### 2. Expect ~15 tokens/sec, so plan for slow responses

A 300-token reply takes roughly 20 seconds. This is a ~1T model on consumer
GPUs — the trade for the price and the context.

**Warm it up.** The first request after a restart pays CUDA-graph capture:
measured **343s**, versus **54s** for a *larger* prompt immediately after. A cold
first call is not representative — send a throwaway request before benchmarking
or before pointing a tool at it.

- **Set a generous client timeout (1800s).** The default 30–60s in most SDKs
  and agent frameworks will abort mid-generation, and a large prompt can spend
  over a minute in prefill before the first token appears.
- **Stream** if a human is waiting, so they see progress.

```python
client = OpenAI(base_url="...", api_key="...", timeout=1800.0)
```

### 3. One request at a time

The server runs with `max_running_requests=1`: a single request occupies the
whole 48-GPU cluster and everything else queues behind it. At a full 1M context
that is a **~25 minute** block on every other caller.

Do not fan out an agent swarm at this endpoint. If you need concurrency, use one
of the smaller catalog models on the same base URL.

### 4. Prefill dominates long-context requests

Decode speed is independent of prompt size, but the whole prompt must be read
before the first token appears. Measured, warm:

| prompt | time to first token |
|---|---|
| ~44k tokens | ~54 s |
| ~73k tokens | ~73 s |
| **~1,010k tokens** | **~1,476 s (24.6 min)** — prefill ~684 tok/s |

So a million-token analysis is a coffee-break operation, not an interactive one.
Set your client timeout accordingly (1800s+) and prefer streaming so you can see
it working.

### 5. Send the whole repository — but know what it costs

With a 1M-token context you no longer need to chunk. A ~1M-token prompt costs
**$1.00** in input and about 25 minutes of prefill, and retrieval was verified at
that size (5/5 needles at every depth).

For anything that fits in ~100k tokens, prefer the smaller prompt: it returns in
under two minutes and costs a tenth as much. Reach for the full million when you
genuinely need whole-repository reasoning in one shot.

### 6. Tool calling works — the parsers are enabled

`--tool-call-parser kimi_k3` and `--enable-auto-tool-choice` are on, so
standard OpenAI-style `tools` / `tool_choice` work. (Without those the model
returns tool calls as plain text and every call silently fails.)

---

## Streaming

```python
stream = client.chat.completions.create(
    model="k3-coder",
    messages=[{"role": "user", "content": "Write a haiku about wind power."}],
    max_tokens=600,
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.content:
        print(delta.content, end="", flush=True)
```

Billing is exact on the streaming path: if a client disconnects mid-stream you
are charged for the prompt plus the tokens actually delivered, not the whole
completion.

---

## Agent frameworks

Anything that speaks the OpenAI API works. Point the base URL at GreenCompute
and raise the timeout.

**LangChain**
```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    model="k3-coder",
    base_url="https://api.green-compute.com/v1",
    api_key="YOUR_KEY",
    timeout=1800,
    max_tokens=600,
)
```

**Environment-variable style** (works with most CLIs and harnesses, including
Nous Hermes Agent, aider, and similar):
```bash
export OPENAI_BASE_URL="https://api.green-compute.com/v1"
export OPENAI_API_KEY="YOUR_KEY"
export OPENAI_MODEL="k3-coder"
```

---

## Billing

Charged per token, settled after each response.

| | Price | Example |
|---|---|---|
| Input | $1.00 / 1M | 500-token prompt ≈ $0.0005 · a **1M-token repo ≈ $1.00** |
| Output | $5.00 / 1M | 800-token answer = $0.004 |

**Reasoning tokens are billed as output**, and this model thinks before it
answers, so a typical reply produces more output than a non-reasoning model
would. Budget accordingly.

Check your balance:
```bash
curl https://api.green-compute.com/platform/billing/balance \
  -H "Authorization: Bearer $GREENCOMPUTE_API_KEY"
# {"balance_credits":2499,"balance_usd":24.99}
```

Full transaction history is at `/platform/billing/ledger`.

A request against an empty balance returns **HTTP 402**.

---

## Errors

| Code | Meaning | What to do |
|---|---|---|
| `402` | No balance | Top up |
| `409` | Model temporarily unavailable | Retry with backoff — the replica may be rebuilding (~20–40 min after a node fault) |
| `502` | Upstream timeout | Retry. k3-coder requests are allowed 3600s, so this is rare |

`no healthy deployment available` is occasionally returned for a few seconds
immediately after a replica finishes rebuilding, even though the model is up. It
fails fast (~3s) rather than hanging, and a single retry succeeds — so treat it
as retryable rather than as an outage.

**On availability, honestly:** k3-coder runs as a single instance across 48 GPUs
on six nodes with no redundancy. The six ranks are one failure domain — if any
one dies they all do, and supervision restarts the whole set. Recovery takes
roughly 10 minutes because 587 GiB of weights must reload. Build in retry, and
fall back to a smaller catalog model on the same base URL if your agent needs
guaranteed uptime.

---

## Cursor

Cursor can use K3 as a custom OpenAI-compatible model.

1. **Settings → Models → OpenAI API Key**
2. Paste your GreenCompute key
3. Enable **Override OpenAI Base URL** and set it to `https://api.green-compute.com/v1`
4. Under **Model Names**, add `k3-coder`
5. Click **Verify**

What works and what does not, honestly:

| | |
|---|---|
| Chat / Ask | ✅ |
| Agent tool calls | ✅ |
| Tab autocomplete | ❌ — uses Cursor's own models, a custom base URL cannot change it |
| Codebase indexing | ❌ — same reason |

**Context is not the constraint any more.** At 1,048,576 tokens you can `@`-attach
whole folders — the limit is your patience, not the window.

**Expect it to feel slow, and plan around that.** ~15 tok/s decode, and it thinks
before answering, so a substantial edit runs a couple of minutes; a genuinely
huge attachment can take far longer to prefill. Excellent for "read this whole
service and explain the data flow", "review this module", or a subtle bug.
Frustrating for rapid back-and-forth — and Tab completion always uses Cursor's
own models regardless of this setting.

**It serves one request at a time.** If your editor fires a second call while one
is running, it queues behind it.

---

## Other models

The same key and base URL serve the rest of the catalog at $0.20 / 1M input and
$0.60 / 1M output — much faster and much cheaper, and the right default for work
that does not need frontier reasoning.

List what is currently live (public, no auth needed):
```bash
curl https://validator.green-compute.com/validator/v1/catalog-status
```
`running_replicas: 0` means that model is not currently being served.

`GET /v1/models` returns the servable models in OpenAI format, so tools that
discover models from the endpoint (Cursor, aider, Continue) work without extra
configuration.
