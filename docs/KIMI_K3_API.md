# Kimi K3 on GreenCompute — API Guide

Kimi K3 (2.8T parameters, MoE) served on **72× RTX 5090 running on 100% renewable
energy**. OpenAI-compatible, so any existing client or agent framework works by
changing two lines.

| | |
|---|---|
| **Base URL** | `https://api.green-compute.com/v1` |
| **Model ID** | `kimi-k3` |
| **Auth** | `Authorization: Bearer <YOUR_KEY>` |
| **Price** | **$2.00 / 1M input · $10.00 / 1M output** |
| **Context** | 49,152 tokens |

At $2/$10 this is the cheapest Kimi K3 available anywhere — the reference price
across other providers is $3/$15, and the next cheapest endpoint is $2.90/$14.

---

## Quick start

```bash
curl https://api.green-compute.com/v1/chat/completions \
  -H "Authorization: Bearer $GREENCOMPUTE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kimi-k3",
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
    model="kimi-k3",
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

### 2. Expect ~25 tokens/sec, so plan for slow responses

A 300-token reply takes roughly 12 seconds; a long reasoning answer can take
1–2 minutes. This is a 2.8T model on consumer GPUs — the trade for the price.

- **Set a generous client timeout (1800s).** The default 30–60s in most SDKs
  and agent frameworks will abort mid-generation, and a large prompt can spend
  over a minute in prefill before the first token appears.
- **Stream** if a human is waiting, so they see progress.

```python
client = OpenAI(base_url="...", api_key="...", timeout=1800.0)
```

### 3. Concurrency is capped at 8 in-flight requests

Beyond that, requests queue. If you are fanning out an agent swarm, keep a
semaphore at or below 8 rather than letting the queue absorb it.

### 4. Long prompts cost prefill time on top of generation

The ~25 tok/s above is *decode* speed and is independent of prompt size, but a
large prompt must be read before generation starts, and prefill cost grows
faster than linearly. Measured end-to-end with a short answer:

| prompt | total |
|---|---|
| ~1.4k tokens | ~14 s |
| ~25k tokens | ~80 s |
| ~40k tokens | ~60–90 s |

Timings vary run to run by roughly 2× at the top of the range, so treat these as
rough. A long-context request is dominated by prefill; a long *answer* is
dominated by decode. Budget for both.

### 5. For large documents, chunk rather than sending one huge prompt

Splitting a large input into ~40k-token chunks and combining the results is
faster end-to-end than one maximal request, uses all 8 concurrent slots instead
of serialising on one, and keeps any single failure small. Because prefill grows
super-linearly, two 40k requests finish sooner than one 80k request would.

A prompt over the 49,152-token limit is rejected in well under a second, so it
costs you nothing to retry smaller — but note the error text currently reads
`upstream connection failed` rather than naming the real cause, so a rejection
that returns instantly almost always means "prompt too long", not a network
fault.

### 6. Tool calling works — the parsers are enabled

`--tool-call-parser kimi_k3` and `--enable-auto-tool-choice` are on, so
standard OpenAI-style `tools` / `tool_choice` work. (Without those the model
returns tool calls as plain text and every call silently fails.)

---

## Streaming

```python
stream = client.chat.completions.create(
    model="kimi-k3",
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
    model="kimi-k3",
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
export OPENAI_MODEL="kimi-k3"
```

---

## Billing

Charged per token, settled after each response.

| | Price | Example |
|---|---|---|
| Input | $2.00 / 1M | 500-token prompt = $0.001 |
| Output | $10.00 / 1M | 800-token answer = $0.008 |

**Reasoning tokens are billed as output**, and K3 generates a lot of them — a
typical answer produces far more output than a non-reasoning model would. Budget
accordingly.

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
| `502` | Upstream timeout | Retry. K3 requests are allowed 1800s, so this is rare |

`no healthy deployment available` is occasionally returned for a few seconds
immediately after a replica finishes rebuilding, even though the model is up. It
fails fast (~3s) rather than hanging, and a single retry succeeds — so treat it
as retryable rather than as an outage.

**On availability, honestly:** K3 runs as a single replica across 72 GPUs with
no redundancy. A node fault takes it offline and it self-heals, but recovery
takes 20–40 minutes because 1.5 TB of weights must reload. Build in retry and
a fallback model if your agent needs guaranteed uptime.

---

## Cursor

Cursor can use K3 as a custom OpenAI-compatible model.

1. **Settings → Models → OpenAI API Key**
2. Paste your GreenCompute key
3. Enable **Override OpenAI Base URL** and set it to `https://api.green-compute.com/v1`
4. Under **Model Names**, add `kimi-k3`
5. Click **Verify**

What works and what does not, honestly:

| | |
|---|---|
| Chat / Ask | ✅ |
| Agent tool calls | ✅ |
| Tab autocomplete | ❌ — uses Cursor's own models, a custom base URL cannot change it |
| Codebase indexing | ❌ — same reason |

**Context is the real constraint.** K3 accepts 49,152 tokens; Cursor's own models
run far larger and it will happily attach more of your codebase than that. Keep
`@`-references tight — a few files, not whole folders — or requests will be
rejected for length.

**Expect it to feel slow.** ~25 tok/s decode, and a reasoning model thinks before
answering, so a substantial edit runs a minute or more. Fine for "explain this",
"review this function", or a tricky bug; frustrating for rapid back-and-forth.

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
