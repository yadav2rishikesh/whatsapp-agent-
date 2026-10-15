# Walkthrough script (about 8 minutes)

Open two tabs: **Chat** (`/`) and **Business console** (`/admin`).

### 0:00 Framing (30 s)
"The brief says CRUD is the setup layer and the analyst is the product. So I put the effort into three things: every number comes from deterministic tools, real and hypothetical data are structurally separate, and every turn is observable."

### 0:30 The 6 sample requests (2 min)
Use **Rahul (U001)**:
1. "Show me my retail properties" → P001, P003. Point at the live step "Searching your properties".
2. "Which of my properties are above ₹10 crore?" → P001 only. In the console, show `interpreted_as: min_value_inr 100000000 (₹10 Cr)`.
3. "Change my Bandra retail property value to ₹12.5 crore" → the "Saved to portfolio: P001" receipt. Open the **Portfolio** drawer and show it updated. There was no search call, because the property index in the prompt resolved Bandra → P001 (DECISIONS D10).

Switch to **Arjun (U003)**: "Which property gives me the highest annual rent?" → Golf Course Road (P007).
Switch to **Neha (U004)**: "Add a 3000 sq ft retail property in Indiranagar worth ₹4.2 crore" → city inferred as Bengaluru and occupancy saved as Unknown, and Aria asks whether it's rented.
Switch to **Priya (U002)**: "Show me my properties in Mumbai" → P004 and P005. Alibaug is correctly *not* Mumbai.

### 2:30 Analysis and context (2 min), as Rahul
- "Give me an overview": total, by type, gross yield (all vs income-producing), and the proactive note that the vacant Andheri office holds over a quarter of the value.
- "Exclude Bandra": the same summary with a filter. Then "which one is better?" → a goal-dependent comparison.
- "Residential vs commercial?" → Rahul has 100% commercial.

### 4:30 Real vs hypothetical (1.5 min)
- "What if I sell Andheri?" → amber "Hypothetical — not saved" bubble with before → after. The header shows "What-if active".
- "And buy a ₹5 Cr office in Pune rented at ₹40 L?" → the scenario stacks.
- Open the **Portfolio** drawer: nothing changed. In the console, the scenario JSON is on the conversation, not in the properties table.

### 6:00 Honesty (45 s)
- "What's my ROI?" / "How much has Bandra appreciated since I bought it?" → purchase price and dates aren't stored, and Aria offers what it can show. In the console this appears as a low-severity "data gap" flag, so the business can see demand for that data.

### 6:45 Business console (1.5 min)
- KPIs: p50/p95, model vs tool time, fast/strong routing.
- Open a conversation: the per-message latency bar shows the model takes about 99% and the tools about 1 ms. Expand a tool call to show its arguments and result. Point out the route reason.
- "Needs attention": as any user, type "I want to talk to a human", then show the flag and resolve it.

### 8:00 Wrap (30 s)
- Single agent, deterministic tools, no RAG (structured numeric data, DECISIONS D1).
- Latency: everything except the model is about 30 ms, so I optimised by cutting model calls: no router LLM, property index in the prompt, fat summary tool, prompt-cache ordering, and streaming.
- Next steps: SQL push-down for large portfolios, a WhatsApp webhook on the same `run_turn`, and RAG only if lease documents get added.
