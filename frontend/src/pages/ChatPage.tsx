import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import Markdown from "../components/Markdown";
import {
  ConvSummary, Msg, Property, StreamEvent, TOOL_LABELS, User, fmtMs, get,
  localStorageGet, localStorageSet, post, streamChat, timeOf,
} from "../api";

type ChatMsg = Msg & { hypothetical?: boolean; saved?: string[]; steps?: string[] };
type Step = { name: string; done: boolean; ok?: boolean; ms?: number };

const SUGGESTIONS = [
  "What's my portfolio worth?",
  "Which property earns the highest rent?",
  "Show my exposure by property type",
  "What if I sell my vacant property?",
  "What's my ROI so far?",
];

function initials(name: string) {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2);
}

export default function ChatPage() {
  const [users, setUsers] = useState<User[]>([]);
  const [uid, setUid] = useState<string>(localStorageGet("uid") || "U001");
  const [convs, setConvs] = useState<ConvSummary[]>([]);
  const [cid, setCid] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [scenario, setScenario] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const [steps, setSteps] = useState<Step[]>([]);
  const [status, setStatus] = useState<string>("");
  const [portfolio, setPortfolio] = useState<Property[]>([]);
  const [showPortfolio, setShowPortfolio] = useState(false);
  const [showList, setShowList] = useState(false);
  const [err, setErr] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const justCreated = useRef<number | null>(null);  // skip reloading a chat we just created mid-send
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const user = users.find((u) => u.user_id === uid);

  const loadUsers = useCallback(() => get<User[]>("/api/users").then((u) => { setUsers(u); setErr(""); }).catch(() =>
    setErr("Can't reach the backend. Check it is running and open /api/health on the backend port.")), []);
  const loadConvs = useCallback(() => get<ConvSummary[]>(`/api/users/${uid}/conversations`).then(setConvs), [uid]);
  const loadPortfolio = useCallback(() => get<Property[]>(`/api/users/${uid}/portfolio`).then(setPortfolio), [uid]);

  useEffect(() => { loadUsers(); }, [loadUsers]);
  useEffect(() => {
    localStorageSet("uid", uid);
    setCid(null); setMessages([]); setScenario(false);
    loadConvs(); loadPortfolio();
  }, [uid, loadConvs, loadPortfolio]);

  useEffect(() => {
    if (cid == null) return;
    if (justCreated.current === cid) { justCreated.current = null; return; }
    get<{ messages: ChatMsg[]; scenario_active: boolean }>(`/api/conversations/${cid}`).then((c) => {
      setMessages(c.messages); setScenario(c.scenario_active);
    });
  }, [cid]);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages, draft, steps]);

  async function send(text: string) {
    text = text.trim();
    if (!text || busy) return;
    setErr("");
    const tempUser: ChatMsg = { id: -Date.now(), role: "user", content: text, created_at: new Date().toISOString() };
    setMessages((m) => [...m, tempUser]);
    setInput(""); setBusy(true); setDraft(""); setSteps([]); setStatus("typing…");
    let id = cid;
    let gotDone = false;
    try {
      if (id == null) {
        const c = await post<{ id: number }>("/api/conversations", { user_id: uid });
        id = c.id; justCreated.current = c.id; setCid(c.id);
      }
      await streamChat(id, text, (e: StreamEvent) => {
        if (e.type === "tool_start") {
          setDraft("");  // any preamble before a tool call is replaced by the final answer
          setSteps((s) => [...s, { name: e.name, done: false }]);
          setStatus(TOOL_LABELS[e.name] || e.name);
        } else if (e.type === "tool_end") {
          setSteps((s) => {
            const c = [...s];
            const i = c.map((x) => x.name).lastIndexOf(e.name);
            if (i >= 0) c[i] = { ...c[i], done: true, ok: e.ok, ms: e.duration_ms };
            return c;
          });
          setStatus("typing…");
        } else if (e.type === "status") {
          setDraft("");  // e.g. retrying on the backup model: discard partial text
          setStatus(e.text);
        } else if (e.type === "token") {
          setDraft((d) => d + e.text);
        } else if (e.type === "done") {
          gotDone = true;
          const m = e.message as ChatMsg;
          setMessages((ms) => [...ms.filter((x) => x.id !== tempUser.id),
            { ...tempUser, id: e.user_message_id }, m]);
          if (m.saved && m.saved.length) loadPortfolio();
          if (m.steps?.includes("what_if")) setScenario(true);
          if (m.steps?.includes("clear_what_if")) setScenario(false);
        } else if (e.type === "error") {
          setErr(e.text);
        }
      });
    } catch {
      setMessages((ms) => ms.filter((x) => x.id !== tempUser.id));
      setInput(text);
      setErr("Couldn't reach the server, so the message wasn't sent. Check the backend is running and send again.");
    } finally {
      if (!gotDone && id != null) {
        // server ended without a normal reply: resync with what it actually saved
        get<{ messages: ChatMsg[] }>(`/api/conversations/${id}`).then((c) => setMessages(c.messages)).catch(() => {});
      }
      setBusy(false); setDraft(""); setSteps([]); setStatus("");
      loadConvs(); loadUsers();
      inputRef.current?.focus();
    }
  }

  function onSubmit(e: FormEvent) { e.preventDefault(); send(input); }
  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); }
  }

  function newChat() { setCid(null); setMessages([]); setScenario(false); setShowList(false); inputRef.current?.focus(); }

  return (
    <div className={`chat-layout ${showPortfolio ? "with-drawer" : ""} ${showList ? "list-open" : ""}`}>
      {/* ---------------- chat list */}
      <aside className="chatlist" aria-label="Conversations">
        <div className="who">
          <label htmlFor="user-select">Chatting as</label>
          <select id="user-select" value={uid} onChange={(e) => setUid(e.target.value)}>
            {users.map((u) => (
              <option key={u.user_id} value={u.user_id}>{u.name} ({u.city})</option>
            ))}
          </select>
          {user && <p className="who-meta">{user.property_count} properties, worth {user.total_value}</p>}
        </div>
        <button className="btn-primary new-chat" onClick={newChat}>New chat</button>
        <ul className="convs">
          {convs.length === 0 && <li className="empty-note">No conversations yet. Start one with a question about your portfolio.</li>}
          {convs.map((c) => (
            <li key={c.id}>
              <button className={`conv ${c.id === cid ? "active" : ""}`} onClick={() => { setCid(c.id); setShowList(false); }}>
                <span className="conv-title">{c.title}</span>
                <span className="conv-time">{timeOf(c.updated_at)}</span>
                {c.last_message && <span className="conv-last">{c.last_message}</span>}
              </button>
            </li>
          ))}
        </ul>
      </aside>

      {/* ---------------- conversation */}
      <section className="conversation" aria-label="Chat with Aria">
        <header className="conv-header">
          <button className="icon-btn only-mobile" onClick={() => setShowList((v) => !v)} aria-label="Show conversations">
            <svg width="20" height="20" viewBox="0 0 24 24"><path d="M3 6h18M3 12h18M3 18h18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>
          </button>
          <div className="avatar aria-avatar" aria-hidden="true">A</div>
          <div className="conv-header-text">
            <strong>Aria</strong>
            <span className={busy ? "status live" : "status"}>{busy ? status : "Portfolio analyst"}</span>
          </div>
          {scenario && <span className="scenario-pill" title="A hypothetical scenario is active in this chat. Nothing in it is saved.">What-if active</span>}
          <button className={`btn-ghost ${showPortfolio ? "on" : ""}`} onClick={() => setShowPortfolio((v) => !v)} aria-expanded={showPortfolio}>
            Portfolio
          </button>
        </header>

        <div className="wall" ref={scroller}>
          {messages.length === 0 && !busy && (
            <div className="welcome">
              <div className="avatar aria-avatar big" aria-hidden="true">A</div>
              <h1>Hi {user?.name.split(" ")[0] || "there"}, I'm Aria.</h1>
              <p>Ask about your properties, add or update one, or try a what-if. Numbers come straight from your portfolio records.</p>
              <div className="chips">
                {SUGGESTIONS.map((s) => <button key={s} className="chip" onClick={() => send(s)}>{s}</button>)}
              </div>
            </div>
          )}

          {messages.map((m) => <Bubble key={m.id} m={m} />)}

          {busy && (
            <div className="row in">
              <div className="bubble in pending">
                {steps.length > 0 && (
                  <ul className="steps" aria-live="polite">
                    {steps.map((s, i) => (
                      <li key={i} className={s.done ? (s.ok ? "ok" : "fail") : "run"}>
                        <span className="dot" aria-hidden="true" />
                        {TOOL_LABELS[s.name] || s.name}
                        {s.done && <span className="ms">{fmtMs(s.ms)}</span>}
                      </li>
                    ))}
                  </ul>
                )}
                {draft ? <Markdown text={draft} /> : <span className="typing" aria-label="Aria is typing"><i /><i /><i /></span>}
              </div>
            </div>
          )}
          {err && <div className="sys-note error" role="alert">{err}</div>}
        </div>

        <form className="composer" onSubmit={onSubmit}>
          <textarea
            ref={inputRef} rows={1} value={input} placeholder="Message Aria"
            onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} aria-label="Message"
          />
          <button type="submit" className="send" disabled={!input.trim() || busy} aria-label="Send">
            <svg width="20" height="20" viewBox="0 0 24 24"><path d="M3 20.5 21 12 3 3.5l2.8 8.5L3 20.5Zm2.8-8.5H13" stroke="currentColor" strokeWidth="2" fill="none" strokeLinejoin="round" /></svg>
          </button>
        </form>
      </section>

      {/* ---------------- portfolio drawer */}
      {showPortfolio && (
        <aside className="drawer" aria-label="Your portfolio">
          <div className="drawer-head">
            <h2>Your portfolio</h2>
            <button className="icon-btn" onClick={() => setShowPortfolio(false)} aria-label="Close portfolio">✕</button>
          </div>
          <p className="drawer-sub">Live records. Changes you make in chat appear here; what-ifs don't.</p>
          <ul className="props">
            {portfolio.map((p) => (
              <li key={p.property_id} className="prop">
                <div className="prop-top">
                  <span className={`tag t-${p.type.toLowerCase()}`}>{p.type}</span>
                  <span className="pid">{p.property_id}</span>
                </div>
                <strong>{p.locality}, {p.city}</strong>
                <span className="prop-sub">{p.sub_type || "—"} · {p.area_sqft?.toLocaleString("en-IN")} sq ft</span>
                <dl>
                  <div><dt>Value</dt><dd>{p.value}</dd></div>
                  <div><dt>Rent / yr</dt><dd>{p.annual_rent}</dd></div>
                  <div><dt>Yield</dt><dd>{p.gross_yield_pct != null ? `${p.gross_yield_pct}%` : "n/a"}</dd></div>
                </dl>
                <span className={`occ o-${p.occupancy.toLowerCase().replace(/[^a-z]/g, "")}`}>{p.occupancy}</span>
              </li>
            ))}
          </ul>
        </aside>
      )}
    </div>
  );
}

function Bubble({ m }: { m: ChatMsg }) {
  const out = m.role === "user";
  return (
    <div className={`row ${out ? "out" : "in"}`}>
      <div className={`bubble ${out ? "out" : "in"} ${m.hypothetical ? "hypo" : ""} ${m.error ? "err" : ""}`}>
        {m.hypothetical && <span className="hypo-label">Hypothetical — not saved</span>}
        {out ? <p className="plain">{m.content}</p> : <Markdown text={m.content} />}
        {!out && m.saved && m.saved.length > 0 && (
          <span className="receipt">Saved to portfolio: {m.saved.filter(Boolean).join(", ")}</span>
        )}
        <span className="meta">
          {!out && m.latency_total_ms != null && <span title="End-to-end response time">{fmtMs(m.latency_total_ms)}</span>}
          {timeOf(m.created_at)}
        </span>
      </div>
    </div>
  );
}
