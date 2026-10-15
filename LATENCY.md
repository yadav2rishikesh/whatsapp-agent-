# Latency

## Where the time goes

A turn is: model call → tool(s) → model call → answer. A typical lookup uses **2 LLM calls** (pick a tool, then write the answer). A compound question uses 2–3, and only if the model chains tools sequentially. Everything that isn't the model is measured below.

### Measured locally (no network, 50 runs each)

| Component | p50 | p95 |
|---|---|---|
| `search_properties` | 0.90 ms | 1.36 ms |
| `portfolio_summary` | 0.89 ms | 1.24 ms |
| `rank_properties` | 0.89 ms | 1.12 ms |
| `compare` | 0.91 ms | 1.31 ms |
| `what_if` | 1.87 ms | 2.23 ms |
| `check_data_availability` | 0.68 ms | 0.88 ms |
| Whole turn with an instant mock model (graph + history load + prompt build + DB commit + logging) | 29 ms | — |

So **everything except the model is about 30 ms**. End-to-end latency is set almost entirely by the LLM round-trips through OpenRouter.

### Model time: measured with real OpenRouter calls

Measured on 15 Oct 2026, running locally (Mumbai → OpenRouter), **before** the token-budget optimisations below. Figures are read from the business console.

| Request | Route / model | LLM calls | Total | Model | Tools | First token | Input / output tokens |
|---|---|---|---|---|---|---|---|
| What's my portfolio worth? | fast / gpt-4.1-mini | 2 | 5.57 s | 5.18 s | 3 ms | 4.76 s | 8,430 / 138 |
| Which property earns the highest rent? | fast / gpt-4.1-mini | 2 | 3.80 s | — | — | — | — |
| Which one is better? (follow-up) | strong / gpt-4.1 | 2 | 4.25 s | 4.13 s | 3 ms | 3.25 s | 9,369 / 203 |
| I want to talk to a human | fast / gpt-4.1-mini | 2 | 3.39 s | 3.26 s | 1 ms | 3.14 s | 10,293 / 46 |
| What's my ROI so far? | fast / gpt-4.1-mini | 2 | 11.24 s | — | — | — | — |
| What if I sell my vacant property? | strong / gpt-4.1 | — | 18.78 s | — | — | — | — |
| **All 7 turns** | | | **p50 5.57 s / p95 18.78 s** | avg 8.23 s | avg 4 ms | | |

Findings:
- **The model is effectively all of the latency.** Tools average 4 ms. Graph, DB and logging add about 30 ms.
- **Most of the model time comes before the first token.** First token takes 3.1–4.8 s, because each call carries about 8–10k input tokens. More than half of that was tool schemas, and the rest is the prompt plus history that grows each turn.
- **Variance is high.** The two slow turns (11.2 s and 18.8 s) are outliers against a 3.4–5.6 s typical. Provider-side variance is the likely cause, which is what latency-sorted routing (below) targets.

**After optimisation**, measured live on Vercel (function in `syd1`, next to the Supabase database) on 15 Oct 2026:

| Request | Route / model | LLM calls | Total | Model | Tools | First token | Input / output tokens |
|---|---|---|---|---|---|---|---|
| What's my portfolio worth? (before region move) | fast / gpt-4.1-mini | 3 | 9.79 s | 6.90 s | 365 ms | 8.89 s | 10,026 / 318 |
| What's my portfolio worth? (after) | fast / gpt-4.1-mini | 2 | **5.04 s** | 4.84 s | **5 ms** | 4.21 s | 6,770 / 210 |
| What's my ROI so far? (after) | fast / gpt-4.1-mini | — | **3.57 s** | — | — | — | — |

Moving the function next to the database cut tool time from 365 ms to 5 ms and non-model overhead from about 2.5 s to about 0.2 s. Blocking the repeated tool call removed one model round trip. **A typical analytical question now takes about 3.5–5 s end to end, about 95% of it model time.**

## What was optimised

1. **No router LLM.** Model choice is a regex/length rule (about 0 ms) instead of an extra classification call.
2. **Fast model by default.** Only compare, what-if, reasoning and context-heavy follow-ups go to the larger model.
3. **Property index in the prompt.** It holds IDs and labels with no numbers, so "update my Bandra retail" resolves to P001 without a search round-trip. That saves a full LLM call on most write requests.
4. **Fat tools, few calls.** `portfolio_summary` returns totals, by-type, by-city, occupancy, vacancy and concentration in **one** call, so "give me an overview" doesn't fan out into 5 tool calls and 5 model steps.
5. **Prompt-cache-friendly ordering.** Tool schemas (about 3.3k tokens after compaction) and the static rules come first, and the per-user parts come last. Providers that cache prefixes, including OpenAI through OpenRouter, can then reuse the expensive prefix across turns and users.
6. **History capped.** History is bounded (see 10 below), so input tokens stop growing after a few turns.
7. **Streaming and live steps.** The user sees "Calculating portfolio totals…" within milliseconds of the first model decision, and the answer streams token by token. Perceived wait is much shorter than total time.
8. **Compact tool schemas.** Pydantic's `title`, `anyOf: [X, null]` and `default: null` are stripped before binding, and field descriptions are shortened. Tool schemas went from about 5.4k to about 3.3k tokens per call, roughly −38%, on *every* LLM call (`agent._compact_schema`). Validation still runs on the full pydantic models.
9. **Leaner tool results.** The model gets formatted values only (`₹12 Cr`). Raw INR duplicates are dropped, while the console still logs the full result.
10. **History trimmed.** 2 turns are replayed in full instead of 3, and only the latest keeps full tool results. Older ones are cut to 600 characters, and text-only history drops from 8 turns to 6.
11. **Latency-sorted provider routing.** `provider.sort = "latency"` asks OpenRouter to try the lowest-latency provider for the model first (`OPENROUTER_PROVIDER_SORT`).
12. **Capped output (`max_tokens = 1200`).** Replies are short anyway. Without the cap, OpenRouter reserves credit for the model's full 64k output window and rejects low-balance keys with 402.
13. **Serverless DB connections.** On Vercel each request opens one DB connection and closes it (no pool), so idle instances never hold Supabase's limited pooler slots (DECISIONS D15). This costs one connection setup per request; a long-running deployment keeps pooling.
14. **Function next to the database.** Live on Vercel, a summary question took 9.8 s: 6.9 s model (3 calls), 0.37 s tools, and about 2.5 s of DB round trips between the function (Singapore) and Supabase (Sydney). The function now runs in `syd1`, the same AWS region as the database.
15. **No repeated tool calls.** The live trace showed `gpt-4.1-mini` calling `portfolio_summary` twice with identical arguments. A repeat of an identical read call in the same turn now returns the earlier result with "answer now", instead of running again.
16. **Bounded loops.** At most 6 agent steps, a 30 s model timeout and 1 retry, then the fallback model. A runaway loop can't hang a request.

## Scaling to higher volume

| Pressure | Plan |
|---|---|
| **Concurrent users** | The API is stateless (all state is in the DB), so scale horizontally behind a load balancer. Move SQLite to Postgres (already supported through `DATABASE_URL`) with connection pooling (PgBouncer). |
| **LLM rate limits / cost** | Spread load across providers through OpenRouter, keep the fast model as default, and cache common read answers per (user, question, portfolio version). The cache is invalidated on any write to that user's portfolio. |
| **Large portfolios** (thousands of properties) | Push filters and aggregates into SQL (`GROUP BY`, indexed `user_id`, `city`, `property_type`) instead of Python. Replace the in-prompt property index with a `find_property` fuzzy-search tool, and paginate `search_properties`. |
| **Tail latency** | Lower the timeout and use hedged requests (fire the fallback if the primary hasn't produced its first token in N ms). Run independent tool calls in parallel within one step. |
| **Async writes** | Move tool-call and trace logging off the request path (queue → worker) once logging volume matters. Today it is about 1 ms per turn. |
| **Observability at scale** | Ship the per-turn timings already recorded to OpenTelemetry/LangSmith and alert on p95 and error-rate flags. |
