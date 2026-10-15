# Decision log

Each entry gives the decision, the alternatives considered, and why.

---

### D1. Structured SQL plus deterministic tools, **not RAG**
- **Decision:** Properties live in a relational table. The agent answers through typed Python tools that filter and aggregate.
- **Alternatives:** RAG over property rows (embed each row, retrieve the top-k, let the LLM read and calculate), or text-to-SQL.
- **Why:** The data is structured and numeric, and every question is a filter, sum, rank or ratio. RAG retrieves *similar text*, so a question like "total value" would fetch k chunks and leave the LLM to add them up, which is both incomplete (top-k) and arithmetic-prone. Tools give exact, reproducible answers. Text-to-SQL was rejected because model-written SQL is harder to scope safely to one user and to validate, and writes would need a separate path anyway.
- **When RAG would be right:** Unstructured documents such as lease agreements, valuation reports or market notes. That would be an *additional* retrieval tool beside these, not a replacement.

### D2. Single agent with tools, not multi-agent
- **Decision:** One LangGraph tool-calling agent (agent ⇄ tools loop).
- **Alternatives:** Router agent → CRUD agent / analyst agent / scenario agent.
- **Why:** Every extra agent hop costs another LLM round-trip (+0.5–2 s each) and is another place where context gets lost between agents. With 12 tools there's no tool-selection overload. Multi-turn context such as "which one is better?" or "exclude Bandra" is simplest when one agent sees the whole conversation.

### D3. LangGraph `StateGraph` with custom nodes, not the prebuilt ReAct agent
- **Why:** The custom agent and tool nodes let us time **every** model call and tool call, run fallback models inside the node, emit live UI events (`tool_start`, `tool_end`), and log args and results to the DB. That is the observability the business console needs.

### D4. Rule-based model routing (no router LLM)
- **Decision:** A regex/length heuristic picks a **fast** model (`gpt-4.1-mini`) for lookups and CRUD. It picks a **strong** model (`gpt-4.1`) for compare, what-if, "better", "should I", multi-part and short context-dependent follow-ups. If the chosen model fails, a **fallback** from another provider (`gemini-2.5-flash`) answers.
- **Alternatives:** An LLM classifier as router, or one strong model for everything.
- **Why:** An LLM router adds a full round-trip to *every* message. The heuristic costs about 0 ms and is explainable: the reason is logged per message and shown in the console. Most traffic is simple lookups, so most turns run on the cheaper, faster model. A fallback on a different provider covers provider outages and rate limits.
- **Trade-off:** The heuristic can mis-route. The cost of that is only slightly worse quality or slightly higher latency, never wrong numbers, because the numbers come from tools either way.

### D5. Hypotheticals live on the conversation, never in the properties table
- **Decision:** `what_if` appends changes (add, remove, modify) to `conversations.scenario` (JSON). Analysis tools take `use_hypothetical=true` to compute on *real + scenario* in memory. The same maths functions run on both.
- **Alternatives:** A copy-on-write sandbox table, or keeping hypotheticals only in the LLM's context.
- **Why:** Keeping it in context alone means the LLM would do the what-if arithmetic, which is exactly what we must avoid. A sandbox table is heavier. Storing it on the conversation makes the scenario persistent across turns, inspectable by the business team, and **structurally unable** to touch real data: the write tools never read the scenario.

### D6. Deterministic normalisation in code
- **Property type:** "Commercial Office" and "Office" both become **Office**. Keyword groups are:

  | Canonical | Matches |
  |---|---|
  | Retail | retail, shop, showroom, mall, high-street, store |
  | Office | office, commercial office, coworking, IT/business park, bare "commercial" |
  | Residential | residential, apartment, flat, villa, bungalow, penthouse, house, BHK |
  | Industrial | warehouse, industrial, logistics, factory |
  | Land | land, plot |

  Category used for "residential vs commercial": **Commercial = Retail + Office + Industrial**, Residential = Residential. The original label is kept in `property_type_raw`, and `sub_type` is kept as is.
- **City:** Parsed from "Locality, City". If the last part is a **state** ("Alibaug, Maharashtra"), the first part becomes the city. Aliases like Bangalore→Bengaluru and Gurgaon→Gurugram are applied. **Regions** let "Delhi NCR" match Gurugram and Noida, because U003's home city is "Delhi NCR" but no property says Delhi.
- **Money:** "₹4.2 crore", "12.5 Cr", "72 lakh", "50L", "1,20,00,000" and plain numbers are parsed in code. The LLM passes the user's words, not a computed number.
- **User value preference:** Stored as min/max, because the CSV mixes single values and ranges.

### D7. Rent, occupancy and yield rules
- Vacant and self-occupied properties get yield **n/a**, not 0%, and are excluded from yield rankings, which list them separately.
- Portfolio yield is reported **both** on all value and on income-producing value only, because the two tell different stories.
- `tenant_status` is derived from occupancy on every write, so it can't drift.
- Contradictions such as rent given while occupancy is "vacant" are rejected so the agent asks. Rent added to a vacant property flips it to Tenanted, and the tool reports that as an assumption.
- A new property with no occupancy info is stored as **Unknown**, not Vacant, so it doesn't silently distort vacancy statistics. Aria then asks the user.

### D8. Missing data is surfaced by a tool, not hard-coded in the prompt
- `check_data_availability` inspects the actual records. Today it says purchase price is missing for 12 of 12 properties and no date fields exist. If a user adds a purchase price, appreciation becomes computable for that property with no code change. Data-gap questions are flagged (low severity), so the business team can see demand for that data.

### D9. Writes: direct when clear, confirmation only for destructive actions
- Add and update go through when the instruction is explicit, which matches the sample requests R005 and R006. Archive needs `confirmed=true`. Near-duplicate adds are caught. Every write is audited (`property_audit`: old → new, conversation id).
- **Each turn is one DB transaction.** If the model fails after a write, the write is rolled back, so the user never ends up with a half-applied change and no reply.

### D10. Property index in the system prompt
- The prompt lists each property's ID, type, locality, city and occupancy, but **no numbers**. This lets the model resolve "my Bandra retail" → P001 without a lookup round-trip (about 1 model call saved on R006-type requests), while still forcing every figure through a tool. At 12 properties this is a few hundred tokens. Past a few hundred properties, it would be replaced by a `find_property` tool.

### D11. History: last 2 turns with tool calls, older turns as text
- Replaying tool calls and results gives the model the exact numbers it showed the user. That is what "which one is better?" needs. Only the latest turn keeps full tool results; the one before is cut to 600 characters, and older turns are text only. Real measurements showed input tokens growing past 10k per call, and first-token time follows input size (LATENCY.md).

### D12. Streaming (SSE) and visible tool steps
- The UI shows each tool step live ("Calculating portfolio totals… 1 ms") and streams the answer as it is generated. Perceived latency is time to first visible activity, not end-to-end time.

### D13. Stack and hosting
- FastAPI + SQLAlchemy (SQLite locally, Postgres in production with the same code), React + Vite frontend. The two stay separate folders in the code, but deploy as **one Vercel project**: the frontend is built as static files, and the backend runs as a Python function (`api/index.py`) with a 60 s max duration (Hobby allows 300 s; the longest measured reply was about 19 s). No Docker or server management, which suits a small assessment. Trade-off: functions are stateless, so persistent data needs Postgres (`DATABASE_URL`), and SQLite falls back to `/tmp`.

### D14. Placeholder tool arguments are treated as "not set"
- **Found in real testing:** after the tool schemas were compacted (LATENCY.md), `gpt-4.1-mini` started filling *every* optional field with placeholders (`"types": []`, `"category": ""`, `"max_value": 0`). Taken literally, `max_value = 0` filtered out the whole portfolio, and Aria answered "no properties recorded". The console trace showed four identical `portfolio_summary` calls returning 0 rows.
- **Decision:** All tool-argument models inherit `Lenient`, which drops `""`, `[]`, `0` and `"null"` before validation. The prompt also tells the model to omit unused arguments.
- **Trade-off:** A literal 0 can't be passed as a value. That's acceptable: zero rent is expressed through occupancy (Vacant/Self-occupied clears rent explicitly), and no other field has a meaningful zero. It also prevents a model-filled `annual_rent: 0` from silently wiping real rent on a value update. Regression tests replay the exact placeholder-filled calls.

### D15. Postgres-safe, race-safe startup on serverless
- **Found when deploying to Vercel + Supabase:** two bugs that SQLite never showed. (1) Seeding inserted properties before users, and Postgres enforces the `properties.user_id` foreign key while SQLite doesn't by default. (2) The first page load fires several API requests at once; each lands on a fresh serverless instance, and they raced on `CREATE TABLE` and the inserts.
- **Decision:** Flush users before properties. Wrap table creation and seeding in a Postgres advisory lock (`pg_advisory_lock`), so one instance sets up while the others wait and then skip. Startup errors are logged instead of crashing the function. Verified with 10 simultaneous cold starts × 3 rounds on Postgres 16 (exactly one seeds, 0 errors), plus the full test and verification suites run against Postgres.
