import { useCallback, useEffect, useState } from "react";
import Markdown from "../components/Markdown";
import { Msg, fmtMs, get, localStorageGet, localStorageSet, post, timeOf } from "../api";

type Overview = {
  users: number; conversations: number; messages: number; open_flags: number;
  flags_by_kind: Record<string, number>;
  latency_ms: { p50: number | null; p95: number | null; avg_model: number | null; avg_tools: number | null };
  routes: Record<string, number>; tool_usage: Record<string, number>; errors: number;
};
type AdminUser = {
  user_id: string; name: string; city: string; preferences: string; conversations: number;
  open_flags: number; last_active: string | null; property_count: number;
};
type ConvRow = {
  id: number; user_id: string; user_name: string; title: string; messages: number; updated_at: string;
  open_flags: { id: number; kind: string; severity: string }[]; errors: number;
  avg_latency_ms: number | null; scenario_active: boolean;
};
type FlagT = { id: number; message_id: number | null; kind: string; severity: string; note: string; source: string; resolved: boolean; created_at: string };
type Detail = {
  id: number; title: string; user: { user_id: string; name: string; city: string };
  scenario: any[]; messages: (Msg & { hypothetical?: boolean })[]; flags: FlagT[];
  property_changes: { property_id: string; action: string; changes: Record<string, [any, any]>; created_at: string }[];
};

const FLAG_LABEL: Record<string, string> = {
  error: "Error", handoff: "Handoff request", data_gap: "Unanswerable (missing data)",
  unanswered: "Unanswered", write_blocked: "Change needed more info", manual: "Flagged by team",
};

export default function AdminPage() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [rows, setRows] = useState<ConvRow[]>([]);
  const [filterUser, setFilterUser] = useState<string | null>(null);
  const [onlyFlagged, setOnlyFlagged] = useState(false);
  const [sel, setSel] = useState<number | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [needToken, setNeedToken] = useState(false);
  const [token, setToken] = useState(localStorageGet("adminToken") || "");

  const load = useCallback(async () => {
    try {
      const q = new URLSearchParams();
      if (filterUser) q.set("user_id", filterUser);
      if (onlyFlagged) q.set("flagged", "true");
      const [o, u, r] = await Promise.all([
        get<Overview>("/api/admin/overview", true),
        get<AdminUser[]>("/api/admin/users", true),
        get<ConvRow[]>(`/api/admin/conversations?${q}`, true),
      ]);
      setOv(o); setUsers(u); setRows(r); setNeedToken(false);
    } catch (e) {
      if (String(e).startsWith("Error: 401")) setNeedToken(true);
    }
  }, [filterUser, onlyFlagged]);

  const loadDetail = useCallback(() => {
    if (sel != null) get<Detail>(`/api/admin/conversations/${sel}`, true).then(setDetail);
  }, [sel]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { loadDetail(); }, [loadDetail]);
  useEffect(() => {
    const t = setInterval(() => { load(); loadDetail(); }, 10000);
    return () => clearInterval(t);
  }, [load, loadDetail]);

  async function resolve(fid: number) {
    await post(`/api/admin/flags/${fid}/resolve`, {}, true);
    loadDetail(); load();
  }
  async function addFlag() {
    const note = window.prompt("What needs attention in this conversation?");
    if (!note || sel == null) return;
    await post(`/api/admin/conversations/${sel}/flags`, { note }, true);
    loadDetail(); load();
  }

  if (needToken) {
    return (
      <div className="token-gate">
        <h1>Business console</h1>
        <p>Enter the admin token configured on the server.</p>
        <form onSubmit={(e) => { e.preventDefault(); localStorageSet("adminToken", token); load(); }}>
          <input type="password" value={token} onChange={(e) => setToken(e.target.value)} aria-label="Admin token" />
          <button className="btn-primary">Open console</button>
        </form>
      </div>
    );
  }

  return (
    <div className="admin">
      {ov && (
        <section className="kpis" aria-label="Overview">
          <Kpi label="Conversations" value={ov.conversations} sub={`${ov.messages} messages`} />
          <Kpi label="Needs attention" value={ov.open_flags} sub={Object.entries(ov.flags_by_kind).map(([k, v]) => `${v} ${(FLAG_LABEL[k] || k).toLowerCase()}`).join(", ") || "nothing open"} alert={ov.open_flags > 0} />
          <Kpi label="Response time p50 / p95" value={`${fmtMs(ov.latency_ms.p50)} / ${fmtMs(ov.latency_ms.p95)}`} sub={`model ${fmtMs(ov.latency_ms.avg_model)} avg, tools ${fmtMs(ov.latency_ms.avg_tools)} avg`} />
          <Kpi label="Model routing" value={Object.entries(ov.routes).map(([k, v]) => `${v} ${k}`).join(" / ") || "–"} sub={`${ov.errors} turns with errors`} />
        </section>
      )}

      <div className="admin-body">
        <aside className="admin-left">
          <div className="seg" role="tablist">
            <button role="tab" aria-selected={!onlyFlagged} className={!onlyFlagged ? "on" : ""} onClick={() => setOnlyFlagged(false)}>All</button>
            <button role="tab" aria-selected={onlyFlagged} className={onlyFlagged ? "on" : ""} onClick={() => setOnlyFlagged(true)}>Needs attention</button>
          </div>
          <h2 className="side-h">Users</h2>
          <ul className="ulist">
            <li><button className={filterUser == null ? "on" : ""} onClick={() => setFilterUser(null)}>Everyone</button></li>
            {users.map((u) => (
              <li key={u.user_id}>
                <button className={filterUser === u.user_id ? "on" : ""} onClick={() => setFilterUser(u.user_id)}>
                  <span>{u.name}<small>{u.city}, {u.conversations} chats</small></span>
                  {u.open_flags > 0 && <span className="count">{u.open_flags}</span>}
                </button>
              </li>
            ))}
          </ul>
          <h2 className="side-h">Conversations</h2>
          <ul className="clist">
            {rows.length === 0 && <li className="empty-note">No conversations match this filter.</li>}
            {rows.map((r) => (
              <li key={r.id}>
                <button className={sel === r.id ? "on" : ""} onClick={() => setSel(r.id)}>
                  <span className="c-title">{r.title}</span>
                  <span className="c-meta">{r.user_name}, {r.messages} msgs, {timeOf(r.updated_at)}</span>
                  {r.open_flags.length > 0 && (
                    <span className="c-flags">
                      {r.open_flags.map((f) => <span key={f.id} className={`flag-dot sev-${f.severity}`} title={FLAG_LABEL[f.kind] || f.kind} />)}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <main className="admin-main">
          {!detail && <div className="empty-main">Pick a conversation to see its messages, tool calls and timings.</div>}
          {detail && (
            <>
              <header className="d-head">
                <div>
                  <h1>{detail.title}</h1>
                  <p>{detail.user.name} ({detail.user.user_id}), {detail.user.city}</p>
                </div>
                <button className="btn-ghost" onClick={addFlag}>Flag conversation</button>
              </header>

              {detail.flags.length > 0 && (
                <section className="panel">
                  <h2>Flags</h2>
                  <ul className="flags">
                    {detail.flags.map((f) => (
                      <li key={f.id} className={`${f.resolved ? "resolved" : ""} sev-${f.severity}`}>
                        <span className="flag-kind">{FLAG_LABEL[f.kind] || f.kind}</span>
                        <span className="flag-note">{f.note}</span>
                        <span className="flag-src">{f.source}, {timeOf(f.created_at)}</span>
                        {f.resolved ? <span className="flag-done">Resolved</span> : <button className="btn-small" onClick={() => resolve(f.id)}>Resolve</button>}
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {(detail.property_changes.length > 0 || detail.scenario.length > 0) && (
                <section className="panel two">
                  {detail.property_changes.length > 0 && (
                    <div>
                      <h2>Saved changes to portfolio</h2>
                      <ul className="changes">
                        {detail.property_changes.map((c, i) => (
                          <li key={i}><strong>{c.action} {c.property_id}</strong>
                            {Object.entries(c.changes).filter(([k]) => k !== "property_type_raw").map(([k, v]) => (
                              <span key={k}>{k}: {String(v[0] ?? "—")} → {String(v[1] ?? "—")}</span>
                            ))}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {detail.scenario.length > 0 && (
                    <div>
                      <h2>Active what-if (not saved)</h2>
                      <pre className="json">{JSON.stringify(detail.scenario, null, 1)}</pre>
                    </div>
                  )}
                </section>
              )}

              <section className="timeline">
                {detail.messages.map((m) => m.role === "user" ? (
                  <div key={m.id} className="t-user"><span className="who">User</span><p>{m.content}</p><time>{timeOf(m.created_at)}</time></div>
                ) : (
                  <AssistantTrace key={m.id} m={m} flagged={detail.flags.filter((f) => f.message_id === m.id && !f.resolved)} />
                ))}
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  );
}

function Kpi({ label, value, sub, alert }: { label: string; value: string | number; sub?: string; alert?: boolean }) {
  return (
    <div className={`kpi ${alert ? "alert" : ""}`}>
      <span className="kpi-label">{label}</span>
      <strong>{value}</strong>
      {sub && <span className="kpi-sub">{sub}</span>}
    </div>
  );
}

function AssistantTrace({ m, flagged }: { m: Msg & { hypothetical?: boolean }; flagged: FlagT[] }) {
  const total = m.latency_total_ms || 0;
  const model = m.latency_model_ms || 0;
  const tools = m.latency_tools_ms || 0;
  const other = Math.max(0, total - model - tools);
  const w = (x: number) => (total ? `${Math.max(0.5, (x / total) * 100)}%` : "0%");
  return (
    <div className={`t-asst ${m.error ? "has-err" : ""} ${flagged.length ? "has-flag" : ""}`}>
      <div className="t-top">
        <span className="who">Aria</span>
        <span className={`route r-${m.route}`}>{m.route}</span>
        <span className="model">{m.model || "no model response"}</span>
        {m.hypothetical && <span className="hypo-tag">hypothetical</span>}
        <time>{timeOf(m.created_at)}</time>
      </div>
      <Markdown text={m.content} />
      <div className="lat" aria-label="Latency breakdown">
        <div className="bar">
          <span className="b-model" style={{ width: w(model) }} title={`Model ${fmtMs(model)}`} />
          <span className="b-tools" style={{ width: w(tools) }} title={`Tools ${fmtMs(tools)}`} />
          <span className="b-other" style={{ width: w(other) }} title={`Other ${fmtMs(other)}`} />
        </div>
        <span className="lat-legend">
          Total <b>{fmtMs(total)}</b> · model {fmtMs(model)} ({m.llm_calls ?? 0} calls) · tools {fmtMs(tools)}
          {m.latency_first_token_ms != null && <> · first token {fmtMs(m.latency_first_token_ms)}</>}
          {m.tokens_in ? <> · {m.tokens_in.toLocaleString()} in / {m.tokens_out?.toLocaleString()} out tokens</> : null}
        </span>
      </div>
      {m.route_reason && <p className="why">Routed {m.route}: {m.route_reason}</p>}
      {m.error && <p className="t-err">Error: {m.error}</p>}
      {m.tool_calls && m.tool_calls.length > 0 && (
        <ol className="tools">
          {m.tool_calls.map((t) => (
            <li key={t.id} className={t.error ? "fail" : ""}>
              <details>
                <summary>
                  <code>{t.name}</code>
                  <span className="t-args">{summarizeArgs(t.args)}</span>
                  <span className="t-ms">{fmtMs(t.duration_ms)}</span>
                </summary>
                <div className="io">
                  <div><h3>Arguments</h3><pre className="json">{JSON.stringify(t.args, null, 1)}</pre></div>
                  <div><h3>Result</h3><pre className="json">{JSON.stringify(t.result, null, 1)}</pre></div>
                </div>
              </details>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

function summarizeArgs(a: any): string {
  if (!a || typeof a !== "object") return "";
  const s = JSON.stringify(a);
  return s.length > 90 ? s.slice(0, 90) + "…" : s;
}
