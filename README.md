# AI Real Estate Portfolio Analyst

A WhatsApp-style chat agent ("Aria") that manages and analyses a user's property portfolio, plus a business console for the team to monitor conversations, tool calls, latency and flags.

- **Live app:** `<frontend URL>`. The chat is at `/` and the business console at `/admin`.
- **API:** `<backend URL>/api/health`

| Doc | What's in it |
|---|---|
| [SOUL.md](SOUL.md) | Agent role, tone, skills, tools, uncertainty, do's/don'ts, questions, handoff |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Diagram, request lifecycle, data model |
| [DECISIONS.md](DECISIONS.md) | Decision log and trade-offs (incl. why not RAG) |
| [LATENCY.md](LATENCY.md) | Measured breakdown, optimisations, scaling plan |
| [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) | 5–10 minute demo script |

---

## What it does

**Chat (`/`)**: pick a demo user (Rahul, Priya, Arjun or Neha) and talk to Aria. It can:

- **Look up and filter:** "Show me my retail properties", "above ₹10 crore", "my Delhi NCR properties".
- **Analyse:** total value, value by type, retail/office/residential exposure, highest rent, highest/lowest value, value by city, occupancy and vacancy, gross yield, residential vs commercial.
- **Hold context across turns:** "Which one is better?", "Exclude Bandra", "now only the tenanted ones".
- **Keep real and hypothetical apart:** "What if I sell Andheri?" is computed on a scenario layer stored on the conversation and **never written** to the portfolio. The UI marks those replies "Hypothetical — not saved".
- **Change the real portfolio:** add, update (value, rent, occupancy, area, purchase price…) and archive (with confirmation). Every change is audited and shown as old → new.
- **Be honest about gaps:** ROI, appreciation and anything time-based get a plain "not available, here's why".
- **Hand off:** asks for a human when needed and flags the conversation.

**Business console (`/admin`)**:

- KPIs: conversations, open flags, p50/p95 latency, model vs tool time, and the fast/strong/fallback route mix.
- Users list, and conversations filterable by user and by "Needs attention".
- One conversation in full. For every reply it shows the route and reason, the model, a latency bar (model / tools / other), first-token time, tokens, and each **tool call with its arguments and result**.
- Flags (auto, agent or manual) with a resolve button, saved property changes, and the active what-if scenario.

## Stack

| Layer | Choice |
|---|---|
| Agent framework | LangGraph (custom `StateGraph`: agent ⇄ tools) + LangChain tools |
| Model gateway | OpenRouter (OpenAI-compatible) |
| Models | fast `openai/gpt-4.1-mini`, strong `openai/gpt-4.1`, fallback `google/gemini-2.5-flash` (all configurable) |
| Backend | FastAPI, SQLAlchemy, SSE streaming |
| DB | SQLite locally, Postgres in production (`DATABASE_URL`) |
| Frontend | React 18 + Vite + TypeScript, plain CSS |

## Run locally

**Backend** (Python 3.10+)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                 # put your OPENROUTER_API_KEY in it
uvicorn app.main:app --reload --port 8000
```

The database is created and seeded from `backend/data/*.csv` on first start. To reset the demo data, run `python -m app.seed --force`.

**Frontend** (in a second terminal)

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173 (proxies /api to :8000)
```

If port 8000 is taken by another app, start the backend with `--port 8010` and create `frontend/.env` with `BACKEND_URL=http://localhost:8010`.

**Tests** (no API key needed; a scripted model stands in for the LLM)

```bash
cd backend && pytest -q
```

The tests cover all 6 sample requests end to end through the graph, what-if-never-writes, data-gap and handoff flags, model-failure rollback, streaming events, the router and the normalisers.

## Deploy (Vercel, one project)

`vercel.json` builds the React frontend as static files and runs the FastAPI backend as a Python function (`api/index.py`), so the chat (`/`), the business console (`/admin`) and the API (`/api`) share one URL.

1. On vercel.com choose **Add New → Project** and import this repo. Keep the root directory as it is (`./`).
2. Add the environment variables:
   - `OPENROUTER_API_KEY`
   - `DATABASE_URL`: a Postgres URL, e.g. the Supabase **Session pooler** URI. Without it the app uses temporary SQLite in `/tmp`, which resets on cold starts.
3. Click **Deploy**. Then check `/api/health`, which should show `"llm_configured": true`.

The function region is `sin1` (Singapore), which is close to India and to the Supabase Sydney database. Each request can run for up to 60 s (Vercel Hobby allows 300 s); the longest measured reply was about 19 s.

## Assumptions

- **Property types:** "Commercial Office" and "Office" both map to **Office**. Commercial = Retail + Office (+ Industrial). The full mapping is in DECISIONS D6.
- **City** is the last part of "Locality, City". When that part is a state ("Alibaug, Maharashtra"), the locality is used as the city.
- **Regions:** "Delhi NCR" = Delhi, Gurugram, Noida, Ghaziabad, Faridabad. This is how Arjun's "Delhi NCR" home city connects to his properties.
- **Yield** = annual rent ÷ current value, **gross**, because no expenses are stored. Vacant, self-occupied and unknown-occupancy properties show "n/a". Portfolio yield is shown both on all value and on income-producing value.
- **Ownership %** scales value and rent in aggregates. It is 100% everywhere in the seed data.
- **New properties without occupancy** are stored as `Unknown`, not `Vacant`, and Aria asks about them.
- **A locality with no city** is inferred from a small gazetteer ("Indiranagar" → Bengaluru) or falls back to the user's home city. Either way, it is stated to the user.
- **Adds and updates** are applied directly when the instruction is explicit, as in R005 and R006. Archiving requires confirmation.
- **No authentication:** the demo user switcher stands in for a WhatsApp phone number.

## Limitations

- `purchase_price_inr` is blank for all properties and no dates exist, so ROI, appreciation, CAGR, holding period and trends can't be computed. The agent says so. If a user provides a purchase price, appreciation becomes available for that property.
- There is no market data, so Aria can't judge whether a valuation is fair or predict prices.
- Aggregates are computed in Python over the user's rows, which is fine for hundreds of properties per user. LATENCY.md has the SQL push-down plan for larger portfolios.
- The router is heuristic and may occasionally send a simple question to the strong model, or the reverse. This affects quality and latency only, never correctness of numbers.
- No real WhatsApp integration (out of scope per the brief). The chat API is channel-agnostic, so a WhatsApp webhook would call the same `run_turn`.
- The SQLite default is for local use. Use Postgres in production.

## External dependencies

| Dependency | Used for | Without it |
|---|---|---|
| OpenRouter API (key required) | All LLM calls | Chat replies with a flagged error; the console and data still work |
| Postgres (Supabase / Neon) | Persistent storage in production | Falls back to SQLite |
| Google Fonts (Instrument Sans) | UI typeface | Falls back to the system font |
| Vercel | Hosting (static frontend + Python function) | Any Python host works (`uvicorn app.main:app`) |

## Repo layout

```
backend/
  app/
    main.py        FastAPI routes (chat SSE + admin)
    agent.py       LangGraph graph, router, fallback, turn transaction, logging, flags
    tools.py       12 tools (read / write / what-if / handoff)
    portfolio.py   pure maths: filters, summary, rank, coverage, appreciation
    normalize.py   types, cities/regions, ₹ Cr/L parsing
    prompts.py     system prompt (condensed SOUL.md)
    db.py, seed.py, config.py
  data/            dataset CSVs
  tests/
frontend/
  src/pages/ChatPage.tsx, AdminPage.tsx, src/styles.css
SOUL.md  DECISIONS.md  LATENCY.md  docs/
```
