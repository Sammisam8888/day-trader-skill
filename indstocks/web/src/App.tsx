import { Activity as ActivityIcon, Bot, Gauge, LayoutDashboard, ListOrdered, LogIn, MessageSquare, Pause, Play, Settings2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { act, getState, type Order, type Position, type State } from "./api";
import Activity from "./components/Activity";
import Brain from "./components/Brain";
import Market from "./components/Market";
import TradeDrawer from "./components/TradeDrawer";
import Blotter, { ConfirmCancel } from "./components/Blotter";
import Chat from "./components/Chat";
import Journal from "./components/Journal";
import Kpis, { STATUS } from "./components/Kpis";
import PnlChart from "./components/PnlChart";
import PriceChart from "./components/PriceChart";
import { ClosedTrades, Positions } from "./components/Positions";
import { clsx, price } from "./fmt";
import { Badge, Btn, Card, Dot, Flash } from "./ui";

type Tab = "overview" | "market" | "claude" | "orders" | "activity" | "chat";
const TABS: { id: Tab; label: string; icon: React.ReactNode }[] = [
  { id: "overview", label: "Overview", icon: <LayoutDashboard size={18} /> },
  { id: "market", label: "Market", icon: <Gauge size={18} /> },
  { id: "claude", label: "Claude", icon: <Bot size={18} /> },
  { id: "orders", label: "Orders", icon: <ListOrdered size={18} /> },
  { id: "activity", label: "Activity", icon: <ActivityIcon size={18} /> },
  { id: "chat", label: "Chat", icon: <MessageSquare size={18} /> },
];

const toMin = (t: string) => { const [h, m] = t.split(":").map(Number); return h * 60 + m; };

function Timeline({ s }: { s: State }) {
  if (!s.session) return <span className="text-xs text-faint">no session today</span>;
  const { open, or_end, last_entry, flat_by, close } = s.session;
  const now = new Date(s.now);
  const cur = now.getHours() * 60 + now.getMinutes();
  const a = toMin(open), b = toMin(close);
  const at = (t: number) => Math.min(100, Math.max(0, ((t - a) / (b - a)) * 100));
  const marks: [string, string][] = [[or_end, "range"], [last_entry, "last entry"], [flat_by, "flatten"]];
  return (
    <div className="w-full md:w-[460px]">
      <div className="relative h-1.5 rounded-full bg-line">
        <div className="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-info/60 to-up/60" style={{ width: `${at(cur)}%` }} />
        {marks.map(([t, l]) => <div key={l} title={`${l} ${t}`} className="absolute top-1/2 h-3 w-px -translate-y-1/2 bg-dim" style={{ left: `${at(toMin(t))}%` }} />)}
        {cur >= a && cur <= b && <div className="absolute top-1/2 h-3 w-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-bg bg-fg" style={{ left: `${at(cur)}%` }} />}
      </div>
      <div className="num mt-1 flex justify-between text-[10px] text-faint">
        <span>{open}</span><span>range {or_end}</span><span>entries till {last_entry}</span><span>flat {flat_by}</span><span>{close}</span>
      </div>
    </div>
  );
}

function FeedHealth({ s, err }: { s: State | null; err: string }) {
  if (err) return <Badge tone="down">offline</Badge>;
  if (!s) return <Badge>connecting</Badge>;
  if (!s.login.ok) return <Badge tone="info">no broker session</Badge>;
  const age = s.data.age ?? 0;
  const t = s.data.source === "monitor" ? (age < 12 ? "up" : "warn") : "warn";
  return <Badge tone={t}><Dot tone={t} live={t === "up"} />{s.data.source === "monitor" ? `live · ${Math.round(age)}s` : "direct · 15s"}</Badge>;
}

function Login({ s, toast }: { s: State; toast: (m: string, ok: boolean) => void }) {
  const [code, setCode] = useState("");
  if (s.login.ok) return null;
  return (
    <Card className="border-info/40 shadow-[0_0_0_1px_#60a5fa22,0_10px_40px_-10px_#60a5fa33]">
      <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
        <div>
          <h3 className="flex items-center gap-2 text-lg font-semibold"><LogIn size={18} className="text-info" />Log in to Shoonya</h3>
          <p className="mt-1 max-w-xl text-sm text-dim">Nothing trades or gets protected until today's session exists. Shoonya ends sessions every night, so this is once per trading day.</p>
        </div>
        {s.login.configured ? (
          <div className="flex w-full flex-col gap-2 md:w-auto md:min-w-[360px]">
            {s.login.auto && <Btn variant="solid" onClick={async () => { const r = await act("autologin"); toast(r.message, r.ok); }}>Log in automatically</Btn>}
            <a href="/shoonya/login" className="rounded-lg bg-info px-4 py-2.5 text-center text-sm font-semibold text-[#04101f] hover:brightness-110">{s.login.auto ? "Log in manually" : "Log in to Shoonya"}</a>
            <form className="flex gap-2" onSubmit={async (e) => { e.preventDefault(); const r = await act("login_code", { code }); toast(r.message, r.ok); setCode(""); }}>
              <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="or paste the redirected URL / code"
                className="min-w-0 flex-1 rounded-lg border border-line-2 bg-bg px-3 py-2 text-sm outline-none focus:border-info" />
              <Btn type="submit" disabled={!code}>Use</Btn>
            </form>
          </div>
        ) : <p className="text-sm text-warn">Credentials not set: run <code className="text-fg">sudo bash scripts/setup-shoonya.sh</code> or use Settings.</p>}
      </div>
    </Card>
  );
}

export default function App() {
  const [s, setS] = useState<State | null>(null);
  const [err, setErr] = useState("");
  const [tab, setTab] = useState<Tab>(() => {
    try { return (localStorage.getItem("tab") as Tab) || "overview"; } catch { return "overview"; }
  });
  const [toasts, setToasts] = useState<{ id: number; m: string; ok: boolean }[]>([]);
  const [confirm, setConfirm] = useState<Order | null>(null);
  const [trade, setTrade] = useState<{ day: string; id: string } | null>(null);
  const [focusCall, setFocusCall] = useState<string | null>(null);

  const toast = useCallback((m: string, ok = true) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, m, ok }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000);
  }, []);

  const load = useCallback(async () => {
    try { const d = await getState(); if (d.error) throw new Error(d.error); setS(d); setErr(""); }
    catch (e) { setErr(String(e)); }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    const q = new URLSearchParams(location.search);
    if (q.get("trade")) setTrade({ day: q.get("day") || new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" }), id: q.get("trade")! });
    if (q.get("flash")) { toast(q.get("flash")!, q.get("err") !== "1"); history.replaceState(null, "", "/"); }
    return () => clearInterval(t);
  }, [load, toast]);

  useEffect(() => { try { localStorage.setItem("tab", tab); } catch { /* private mode */ } window.scrollTo(0, 0); }, [tab]);

  const run = async (action: string, body: Record<string, unknown> = {}) => {
    const r = await act(action, body); toast(r.message, r.ok); load(); return r;
  };
  const exit = (p: Position) => { if (confirm == null && window.confirm(`Sell ${p.symbol} × ${p.qty} now with a marketable limit?`)) run("exit", { token: p.token }); };

  const st = s ? STATUS[s.status] : null;
  return (
    <div className="mx-auto max-w-[1400px] px-3 pb-24 pt-3 sm:px-5 lg:pb-8">
      <header className="sticky top-0 z-30 -mx-3 mb-4 border-b border-line bg-bg/85 px-3 py-3 backdrop-blur-md sm:-mx-5 sm:px-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-up/30 to-info/30 ring-1 ring-line-2">
              <svg viewBox="0 0 32 32" className="h-5 w-5"><path d="M5 22l6-7 5 4 10-12" stroke="#34d399" strokeWidth="3" fill="none" strokeLinecap="round" strokeLinejoin="round" /></svg>
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="font-semibold tracking-tight">autotrader</span>
                <Badge>Shoonya</Badge>
                {s?.session?.special && <Badge tone="warn">MUHURAT</Badge>}
              </div>
              <div className="num text-xs text-faint">{s ? new Date(s.now).toLocaleString("en-IN", { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "…"}{s?.login.uid ? ` · ${s.login.uid}` : ""}</div>
            </div>
          </div>
          <div className="order-3 w-full md:order-none md:w-auto">{s && <Timeline s={s} />}</div>
          <div className="flex items-center gap-2">
            <FeedHealth s={s} err={err} />
            {st && <Badge tone={st.tone} className="px-2 py-1"><Dot tone={st.tone} live={s?.status === "live"} />{st.label}</Badge>}
            {s && (s.status === "halted"
              ? <Btn variant="solid" className="px-2.5 py-1.5" onClick={() => run("resume")}><Play size={14} />Resume</Btn>
              : <Btn variant="danger" className="px-2.5 py-1.5" onClick={() => { const r = prompt("Halt new entries. Reason?", "manual halt"); if (r !== null) run("halt", { reason: r }); }}><Pause size={14} />Halt</Btn>)}
            <a href="/classic?tab=controls" className="rounded-lg border border-line-2 bg-panel-2 p-2 text-dim hover:text-fg" title="Settings, limits, terminal"><Settings2 size={16} /></a>
          </div>
        </div>
        {s && s.symbols.length > 0 && (
          <div className="no-scrollbar mt-3 flex gap-4 overflow-x-auto text-sm">
            {s.symbols.map((x) => (
              <span key={x.symbol} className="flex shrink-0 items-center gap-2">
                <span className="text-dim">{x.symbol}</span><Flash value={x.ltp} className="font-semibold">{price(x.ltp)}</Flash>
              </span>
            ))}
          </div>
        )}
        <nav className="mt-3 hidden gap-1 lg:flex">
          {TABS.map((t) => (
            <button key={t.id} onClick={() => setTab(t.id)} className={clsx("flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm",
              tab === t.id ? "bg-panel-2 text-fg ring-1 ring-line-2" : "text-dim hover:text-fg")}>{t.icon}{t.label}</button>
          ))}
        </nav>
      </header>

      {!s ? (
        <div className="grid gap-3 lg:grid-cols-4">{[0, 1, 2, 3].map((i) => <div key={i} className="h-28 animate-pulse rounded-2xl bg-panel" />)}</div>
      ) : (
        <main className="space-y-4">
          <Login s={s} toast={toast} />
          {s.data.error && s.login.ok && <Card><p className="text-sm text-down">{s.data.error}</p></Card>}
          {tab === "overview" && (
            <>
              <Kpis s={s} />
              <div className="grid gap-4 xl:grid-cols-5">
                <div className="xl:col-span-3"><PriceChart s={s} /></div>
                <div className="xl:col-span-2"><PnlChart s={s} /></div>
              </div>
              <Positions items={s.positions} onExit={exit} />
              <div className="grid gap-4 xl:grid-cols-2">
                <Blotter orders={s.orders} onCancel={setConfirm} compact />
                <div className="space-y-4"><ClosedTrades items={s.closed} /><Journal s={s} /></div>
              </div>
            </>
          )}
          {tab === "market" && <Market />}
          {tab === "claude" && <Brain focus={focusCall} onTrade={(day, id) => setTrade({ day, id })} />}
          {tab === "orders" && <Blotter orders={s.orders} onCancel={setConfirm} />}
          {tab === "activity" && <Activity items={s.decisions} />}
          {tab === "chat" && <Chat msgs={s.chat} mode={s.chat_mode} send={async (t) => { const r = await act("chat", { text: t }); if (!r.ok) toast(r.message, false); await load(); }} />}
        </main>
      )}

      <nav className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-6 border-t border-line bg-bg/95 pb-[env(safe-area-inset-bottom)] backdrop-blur-md lg:hidden">
        {TABS.map((t) => (
          <button key={t.id} onClick={() => setTab(t.id)} className={clsx("flex flex-col items-center gap-0.5 py-2.5 text-[11px]", tab === t.id ? "text-info" : "text-faint")}>
            {t.icon}{t.label}
          </button>
        ))}
      </nav>

      <div className="pointer-events-none fixed inset-x-0 top-3 z-50 flex flex-col items-center gap-2 px-3">
        {toasts.map((t) => (
          <div key={t.id} className={clsx("pointer-events-auto max-w-md rounded-xl border px-4 py-2.5 text-sm shadow-2xl backdrop-blur",
            t.ok ? "border-up/30 bg-up-bg/95 text-up" : "border-down/30 bg-down-bg/95 text-down")}>{t.m}</div>
        ))}
      </div>
      {trade && <TradeDrawer day={trade.day} id={trade.id} onClose={() => setTrade(null)}
        onCall={(cid) => { setTrade(null); setFocusCall(cid); setTab("claude"); }} />}
      {confirm && <ConfirmCancel o={confirm} onNo={() => setConfirm(null)} onYes={() => { run("cancel", { order_id: confirm.id }); setConfirm(null); }} />}
    </div>
  );
}
