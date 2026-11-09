import { Bot, Copy, FileText } from "lucide-react";
import { useEffect, useState } from "react";
import { getBrain, getBrainCall, type BrainCall, type BrainDecision } from "../api";
import { clsx, money0, price, signed } from "../fmt";
import { Badge, Card, Empty } from "../ui";

const OUTCOME: Record<string, string> = {
  placed: "up", adjusted: "up", exited: "up", cancelled: "info", would_place: "info", would_exit: "info",
  refused: "warn", failed: "down", ignored: "dim",
};

function decisionLine(d: BrainDecision) {
  if (d.action === "enter")
    return <><b className={d.side === "BUY" ? "text-up" : "text-down"}>{d.side}</b> {d.symbol} · {d.order} {price(d.entry)} · SL {price(d.stop)} · TP {price(d.target)}{d.size != null && d.size !== 1 ? ` · size ${d.size}` : ""}{d.confidence != null ? ` · conf ${d.confidence}` : ""}</>;
  if (d.action === "adjust") return <><b className="text-info">ADJUST</b> {d.symbol}{d.stop != null ? ` · SL → ${price(d.stop)}` : ""}{d.target != null ? ` · TP → ${price(d.target)}` : ""}</>;
  if (d.action === "exit") return <><b className="text-warn">EXIT</b> {d.symbol}</>;
  if (d.action === "cancel") return <><b className="text-dim">CANCEL</b> {d.order_id}</>;
  return <>{d.action}</>;
}

function PromptModal({ day, id, onClose }: { day: string; id: string; onClose: () => void }) {
  const [p, setP] = useState<{ prompt: string; reply: string } | null>(null);
  const [tab, setTab] = useState<"prompt" | "reply">("prompt");
  useEffect(() => { getBrainCall(day, id).then(setP).catch(() => {}); }, [day, id]);
  let pretty = "";
  if (p) {
    const text = tab === "prompt" ? p.prompt : p.reply;
    const i = text.indexOf("STATE:\n");
    try { pretty = i >= 0 ? text.slice(0, i) + "STATE:\n" + JSON.stringify(JSON.parse(text.slice(i + 7)), null, 2) : JSON.stringify(JSON.parse(text), null, 2); }
    catch { pretty = text; }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-3" onClick={onClose}>
      <div className="flex max-h-[90vh] w-full max-w-4xl flex-col rounded-2xl border border-line-2 bg-panel" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-line px-4 py-3">
          <div className="flex gap-1">
            {(["prompt", "reply"] as const).map((x) => (
              <button key={x} onClick={() => setTab(x)} className={clsx("rounded-md px-3 py-1 text-sm", tab === x ? "bg-line-2 text-fg" : "text-dim")}>
                {x === "prompt" ? "What the server asked" : "What Claude replied"}</button>
            ))}
          </div>
          <div className="flex gap-2">
            <button onClick={() => navigator.clipboard?.writeText(pretty)} className="rounded-md border border-line-2 px-2 py-1 text-xs text-dim hover:text-fg"><Copy size={12} className="inline" /> copy</button>
            <button onClick={onClose} className="text-dim hover:text-fg">✕</button>
          </div>
        </div>
        <pre className="num flex-1 overflow-auto whitespace-pre-wrap break-words p-4 text-[12px] leading-relaxed text-dim">{p ? pretty : "loading…"}</pre>
      </div>
    </div>
  );
}

export default function Brain({ onTrade, focus }: { onTrade: (day: string, id: string) => void; focus?: string | null }) {
  const today = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
  const [day, setDay] = useState(today);
  const [calls, setCalls] = useState<BrainCall[] | null>(null);
  const [onlyActive, setOnlyActive] = useState(true);
  const [prompt, setPrompt] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => getBrain(day).then((d) => alive && setCalls(d.calls)).catch(() => {});
    load();
    const iv = setInterval(load, 15000);
    return () => { alive = false; clearInterval(iv); };
  }, [day]);

  useEffect(() => {
    if (focus && calls) document.getElementById(`call-${focus}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [focus, calls]);

  const shown = (calls ?? []).filter((c) => !onlyActive || c.decisions.length || c.error || c.id === focus);
  const total = calls?.length ?? 0;
  return (
    <Card title={<span className="inline-flex items-center gap-2"><Bot size={14} />Claude's decisions · {shown.length}/{total} scans</span>}
      pad={false} right={
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1.5 text-xs text-dim"><input type="checkbox" checked={onlyActive} onChange={(e) => setOnlyActive(e.target.checked)} />only with decisions</label>
          <input type="date" value={day} max={today} onChange={(e) => setDay(e.target.value)} className="rounded-md border border-line-2 bg-bg px-2 py-1 text-xs text-fg" />
        </div>}>
      <div className="hidden grid-cols-[86px_1.1fr_1.7fr_1.6fr_150px] gap-3 border-b border-line px-4 py-2 text-[10px] font-semibold uppercase tracking-wider text-faint lg:grid">
        <span>time</span><span>server asked</span><span>Claude replied</span><span>action taken</span><span>trade</span>
      </div>
      {calls == null ? <div className="p-4"><Empty>loading…</Empty></div>
        : shown.length === 0 ? <div className="p-4"><Empty>{total ? "No scan produced a decision. Untick the filter to see every scan." : "No scans recorded for this day."}</Empty></div>
        : shown.map((c) => (
          <div key={c.id} id={`call-${c.id}`} className={clsx("grid gap-3 border-b border-line px-4 py-3 last:border-0 lg:grid-cols-[86px_1.1fr_1.7fr_1.6fr_150px]", c.id === focus && "bg-info-bg/40")}>
            <div>
              <div className="num text-sm font-semibold">{c.ts.slice(11, 19)}</div>
              <div className="mt-1 flex flex-wrap gap-1">
                <Badge tone={c.source === "claude" ? "info" : "warn"}>{c.source === "claude" ? "Claude" : c.source === "rules" ? "rule" : c.source}</Badge>
              </div>
              {c.latency_s != null && <div className="num mt-1 text-[11px] text-faint">{c.latency_s}s</div>}
            </div>
            <div className="text-[13px] text-dim">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-faint lg:hidden">Server asked · </span>
              {c.asked.symbols} markets · cash {money0(c.asked.cash)} · {c.asked.positions} open · {c.asked.resting} resting · P&L {signed(c.asked.pnl)}
              <div className="text-[11px] text-faint">{c.phase.replace(/_/g, " ")}</div>
              <button onClick={() => setPrompt(c.id)} className="mt-1 inline-flex items-center gap-1 text-xs text-info hover:underline"><FileText size={12} />full prompt ({Math.round(c.prompt_chars / 1000)}k chars)</button>
            </div>
            <div className="text-[13px]">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-faint lg:hidden">Claude replied · </span>
              {c.error && <div className="text-down">error: {c.error}</div>}
              {c.view && <p className="italic text-dim">“{c.view}”</p>}
              {c.decisions.length === 0 ? <div className="text-faint">no trade this scan</div> : (
                <ol className="mt-1 space-y-1.5">
                  {c.decisions.map((d, i) => (
                    <li key={i} className="num">
                      <span className="text-faint">{i + 1}.</span> {decisionLine(d)}
                      {d.reason && <div className="pl-4 text-[12px] text-dim">{d.reason}</div>}
                    </li>
                  ))}
                </ol>
              )}
            </div>
            <div className="text-[13px]">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-faint lg:hidden">Action taken · </span>
              {c.actions.length === 0 ? <span className="text-faint">—</span> : (
                <ol className="space-y-1.5">
                  {c.actions.map((a, i) => (
                    <li key={i}><span className="text-faint">{a.decision + 1}.</span> <Badge tone={OUTCOME[a.outcome] ?? "dim"}>{a.outcome.replace("_", " ").toUpperCase()}</Badge>
                      <div className="pl-4 text-[12px] text-dim">{a.reason}</div></li>
                  ))}
                </ol>
              )}
            </div>
            <div className="flex flex-col gap-1.5">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-faint lg:hidden">Trade</span>
              {c.actions.filter((a) => a.order_id).length === 0 ? <span className="text-sm text-faint">—</span>
                : c.actions.filter((a) => a.order_id).map((a) => (
                  <button key={a.order_id} onClick={() => onTrade(day, a.order_id!)}
                    className="rounded-lg border border-line-2 bg-panel-2 px-2.5 py-1.5 text-left text-xs hover:border-info">
                    <span className="text-info">open trade ↗</span>
                    <div className="text-faint">{a.order_status ?? a.outcome}</div>
                  </button>
                ))}
            </div>
          </div>
        ))}
      {prompt && <PromptModal day={day} id={prompt} onClose={() => setPrompt(null)} />}
    </Card>
  );
}
