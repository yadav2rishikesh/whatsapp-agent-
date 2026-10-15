export const API = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") || "";

export type User = {
  user_id: string; name: string; city: string; preferences: string;
  property_count: number; total_value: string;
};
export type Property = {
  property_id: string; type: string; sub_type: string | null; locality: string; city: string;
  area_sqft: number; value: string; annual_rent: string; occupancy: string;
  gross_yield_pct: number | null; yield_note?: string;
};
export type ConvSummary = { id: number; title: string; updated_at: string; last_message: string | null };
export type ToolCall = {
  id: number; step: number; name: string; args: any; result: any; duration_ms: number; error: string | null;
};
export type Msg = {
  id: number; role: "user" | "assistant"; content: string; created_at: string | null;
  model?: string | null; route?: string | null; route_reason?: string | null; llm_calls?: number | null;
  latency_total_ms?: number | null; latency_model_ms?: number | null; latency_tools_ms?: number | null;
  latency_first_token_ms?: number | null; tokens_in?: number | null; tokens_out?: number | null;
  error?: string | null; tool_calls?: ToolCall[];
};

export type StreamEvent =
  | { type: "meta"; route: string; model: string; reason: string }
  | { type: "status"; text: string }
  | { type: "tool_start"; name: string; args: any }
  | { type: "tool_end"; name: string; duration_ms: number; ok: boolean; saved: boolean }
  | { type: "token"; text: string }
  | { type: "done"; message: Msg; user_message_id: number }
  | { type: "error"; text: string };

function adminHeaders(): Record<string, string> {
  const t = localStorageGet("adminToken");
  return t ? { "X-Admin-Token": t } : {};
}

export function localStorageGet(k: string): string | null {
  try { return localStorage.getItem(k); } catch { return null; }
}
export function localStorageSet(k: string, v: string) {
  try { localStorage.setItem(k, v); } catch { /* storage unavailable */ }
}

export async function get<T>(path: string, admin = false): Promise<T> {
  const r = await fetch(API + path, { headers: admin ? adminHeaders() : {} });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export async function post<T>(path: string, body: unknown = {}, admin = false): Promise<T> {
  const r = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(admin ? adminHeaders() : {}) },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

/** POST a chat message and read the SSE stream. */
export async function streamChat(cid: number, text: string, onEvent: (e: StreamEvent) => void) {
  const r = await fetch(`${API}/api/conversations/${cid}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!r.ok || !r.body) throw new Error(`Server returned ${r.status}`);
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 2);
      if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)));
    }
  }
}

export const TOOL_LABELS: Record<string, string> = {
  search_properties: "Searching your properties",
  get_property: "Opening property details",
  portfolio_summary: "Calculating portfolio totals",
  rank_properties: "Ranking properties",
  compare: "Comparing",
  check_data_availability: "Checking what data is available",
  create_property: "Adding property",
  update_property: "Updating property",
  archive_property: "Removing property",
  what_if: "Running what-if scenario",
  clear_what_if: "Clearing scenario",
  request_human_handoff: "Notifying the team",
};

export function fmtMs(ms?: number | null) {
  if (ms == null) return "–";
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
}

export function timeOf(iso: string | null) {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "Z");
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
