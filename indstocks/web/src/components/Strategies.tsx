import { ArrowLeft, CheckCircle2, CircleDashed, CircleAlert, CircleX, Search, Target } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { getPipeline, getStrategies, getStrategy, type PipelineResp, type StratDetail, type StratOverview, type StratRow } from "../api";
import { clsx } from "../fmt";
import { Badge, Card, Empty } from "../ui";

// The Strategies tab: the pipeline's health, every strategy's wallet and day, and a
// page per strategy with its trades, its log, the AI's learnings and its history.

const rs = (x: number | null | undefined, d = 0) =>
  x == null || !Number.isFinite(x) ? "--" : `${x > 0 ? "+" : x < 0 ? "-" : ""}Rs ${Math.abs(x).toLocaleString("en-IN", { maximumFractionDigits: d })}`;
const toneOf = (x: number | null | undefined) => (x == null ? "text-dim" : x > 0 ? "text-up" : x < 0 ? "text-down" : "text-dim");
const KIND: Record<string, { label: string; tone: string }> = {
  fixed: { label: "fixed", tone: "dim" }, inverted: { label: "inverted", tone: "warn" },
  ai_tuned: { label: "AI-tuned", tone: "info" }, ai_trader: { label: "AI trader", tone: "up" },
  ai_agent: { label: "AI agent", tone: "up" },
  futures: { label: "futures", tone: "warn" }, options: { label: "options", tone: "info" },
};
const ago = (s: number | null) => (s == null ? "never" : s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago`
  : s < 172800 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`);

function StageIcon({ st }: { st: string }) {
  if (st === "ok") return <CheckCircle2 size={14} className="text-up" />;
  if (st === "late") return <CircleAlert size={14} className="text-warn" />;
  if (st === "down") return <CircleX size={14} className="text-down" />;
  return <CircleDashed size={14} className="text-faint" />;
}

function Pipeline({ p }: { p: PipelineResp | null }) {
  if (!p) return null;
  const bad = p.stages.filter((s) => s.status === "down" || s.status === "late").length;
  return (
    <Card title="Pipeline" right={<span className="text-xs text-faint">{p.broker === "paper" ? "paper broker" : "LIVE broker"} | {bad ? `${bad} need attention` : "all on time"}</span>}>
      <div className="flex flex-wrap items-stretch gap-1.5">
        {p.stages.map((s, i) => (
          <div key={s.key} className="flex items-center gap-1.5">
            <div title={`${s.what} | last: ${s.last ?? "never"}`}
              className={clsx("min-w-[118px] rounded-lg border px-2 py-1.5 text-xs",
                s.status === "ok" ? "border-up/30 bg-up/5" : s.status === "late" ? "border-warn/40 bg-warn/5"
                  : s.status === "down" ? "border-down/40 bg-down/10" : "border-line bg-panel-2")}>
              <div className="flex items-center gap-1 font-semibold"><StageIcon st={s.status} />{s.name}</div>
              <div className="text-[11px] text-faint">{s.status === "idle" ? "not due now" : ago(s.age_s)}</div>
            </div>
            {i < p.stages.length - 1 && <span className="hidden text-faint sm:inline">{"\u2192"}</span>}
          </div>))}
      </div>
    </Card>
  );
}

function Spark({ h, target }: { h: { day: string; net: number }[]; target: number }) {
  const last = h.slice(-12);
  if (!last.length) return <span className="text-faint">--</span>;
  const max = Math.max(target || 1, ...last.map((x) => Math.abs(x.net)));
  return (
    <span className="inline-flex h-6 items-center gap-[2px]" title={last.map((x) => `${x.day}: ${rs(x.net)}`).join("\n")}>
      {last.map((x) => (
        <span key={x.day} className={clsx("w-[5px] rounded-sm", x.net >= target && target > 0 ? "bg-up" : x.net > 0 ? "bg-up/60" : "bg-down/70")}
          style={{ height: `${Math.max(2, (Math.abs(x.net) / max) * 24)}px` }} />))}
    </span>
  );
}

function Md({ text }: { text: string }) {
  // a small renderer for the learnings: headings, bullets, quotes, paragraphs
  const lines = text.split("\n");
  return (
    <div className="grid gap-1 text-sm leading-relaxed">
      {lines.map((l, i) => {
        if (l.startsWith("# ")) return <h3 key={i} className="text-base font-semibold text-fg">{l.slice(2)}</h3>;
        if (l.startsWith("## ")) return <h4 key={i} className="mt-2 text-xs font-semibold uppercase tracking-wider text-dim">{l.slice(3)}</h4>;
        if (l.startsWith("- ")) return <div key={i} className="flex gap-2"><span className="text-faint">{"\u2022"}</span><span>{l.slice(2)}</span></div>;
        if (l.startsWith("> ")) return <div key={i} className="border-l-2 border-info/50 pl-2 text-dim">{l.slice(2)}</div>;
        if (!l.trim() || l.startsWith("|")) return null;
        return <p key={i} className="text-dim">{l}</p>;
      })}
    </div>
  );
}

function Detail({ name, onBack, onOpen }: { name: string; onBack: () => void; onOpen?: (m: string) => void }) {
  const [day, setDay] = useState<string | undefined>(undefined);
  const [d, setD] = useState<StratDetail | null>(null);
  const [tab, setTab] = useState<"trades" | "learning" | "log">("trades");
  useEffect(() => {
    let live = true;
    const load = async () => { try { const r = await getStrategy(name, day); if (live) setD(r); } catch { /* offline */ } };
    load();
    const id = setInterval(load, 10000);
    return () => { live = false; clearInterval(id); };
  }, [name, day]);
  if (!d) return <Empty>loading {name}...</Empty>;
  const s = d.summary;
  const k = KIND[d.kind] ?? KIND.fixed;
  const closed = d.trades.filter((t) => t.net != null);
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={onBack} className="inline-flex items-center gap-1 rounded-lg border border-line-2 bg-panel-2 px-2 py-1 text-sm text-dim hover:text-fg"><ArrowLeft size={14} />All strategies</button>
        <h2 className="text-lg font-semibold">{d.name}</h2>
        <Badge tone={k.tone}>{k.label}</Badge>
        <select value={d.day} onChange={(e) => setDay(e.target.value)} className="ml-auto rounded-lg border border-line-2 bg-panel-2 px-2 py-1 text-sm">
          {[...new Set([...d.days, d.day])].sort().reverse().map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[["Day net", rs(s?.today.net)], ["Trades (won)", s ? `${s.today.trades} (${s.today.wins})` : "--"],
          ["Wallet / target", s ? `Rs ${s.wallet.toLocaleString("en-IN")} / Rs ${s.target}` : "--"],
          ["All days", s ? `${rs(s.total)} | ${s.green_days}/${s.days} green | ${s.target_days} hit target` : "--"]].map(([l, v], i) => (
          <div key={l} className="rounded-xl border border-line bg-panel/80 p-3">
            <div className="text-[11px] uppercase tracking-wider text-faint">{l}</div>
            <div className={clsx("num mt-1 font-semibold", i === 0 && toneOf(s?.today.net))}>{v}</div>
          </div>))}
      </div>

      {s && s.history.length > 0 && (
        <Card title="Day by day">
          <div className="flex flex-wrap gap-1.5">
            {s.history.slice().reverse().map((h) => (
              <button key={h.day} onClick={() => setDay(h.day)}
                className={clsx("rounded-md border px-2 py-1 text-xs num", h.day === d.day ? "border-info" : "border-line",
                  h.net >= s.target && s.target > 0 ? "bg-up/15" : h.net > 0 ? "bg-up/5" : "bg-down/10")}>
                {h.day.slice(5)} <span className={toneOf(h.net)}>{rs(h.net)}</span>
              </button>))}
          </div>
        </Card>)}

      {d.knobs && (
        <Card title={d.kind === "ai_tuned" ? "Knobs the AI set" : "Knobs and tested ranges"}
          right={d.knobs.set_at && <span className="text-xs text-faint">set {d.knobs.set_at.slice(11, 16)}</span>}>
          {d.knobs.doc && <p className="mb-2 text-sm text-dim">{d.knobs.doc.split("\n").join(" ")}</p>}
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(d.knobs.ranges).map(([kk, [lo, hi]]) => (
              <span key={kk} className="rounded-md border border-line bg-panel-2 px-2 py-0.5 text-xs num">
                {kk} <span className="text-fg">{d.knobs?.values?.[kk] ?? "default"}</span> <span className="text-faint">({lo}..{hi})</span>
              </span>))}
          </div>
          {d.knobs.why && <p className="mt-2 text-xs text-faint">AI: {d.knobs.why}</p>}
        </Card>)}

      {d.agent && (
        <Card title="What the AI agent follows" right={<span className="text-xs text-faint">{d.agent.decisions.length} decisions today</span>}>
          {d.agent.decisions.length === 0 ? <Empty>No decision yet today (first at 09:42, then every 15 minutes).</Empty> : (() => {
            const now = d.agent.decisions[d.agent.decisions.length - 1];
            const fol = Object.entries(now.follow);
            return (
              <div className="grid gap-2">
                <p className="text-sm text-dim"><span className="text-faint">{now.at.slice(11, 16)}</span> {now.day_read}</p>
                <div className="flex flex-wrap gap-1.5">
                  {now.stand_aside && <Badge tone="warn">standing aside</Badge>}
                  {fol.length === 0 && !now.stand_aside && <span className="text-xs text-faint">following no model</span>}
                  {fol.map(([m, f]) => (
                    <button key={m} onClick={() => onOpen?.(m)} title={f.why}
                      className="rounded-md border border-line bg-panel-2 px-2 py-0.5 text-xs num hover:border-info">
                      {m} <span className="text-fg">x{f.risk}</span>{f.side !== "both" && <span className="text-faint"> {f.side}</span>}
                    </button>))}
                </div>
                {now.note && <p className="text-xs text-faint">AI: {now.note}</p>}
                <details className="text-xs text-dim"><summary className="cursor-pointer text-faint">all decisions</summary>
                  <ul className="mt-1 grid gap-1">{d.agent.decisions.slice().reverse().map((x, i) => (
                    <li key={i}><span className="num text-faint">{x.at.slice(11, 16)}</span> follow {Object.keys(x.follow).join(", ") || "none"}
                      {x.exit.length > 0 && ` | exit ${x.exit.join(", ")}`}{x.exit_all && " | EXIT ALL"}{x.stand_aside && " | stand aside"}{x.note && ` - ${x.note}`}</li>))}
                  </ul></details>
              </div>);
          })()}
        </Card>)}

      <div className="flex gap-1">
        {(["trades", "learning", "log"] as const).map((t) => (
          <button key={t} onClick={() => setTab(t)} className={clsx("rounded-lg px-3 py-1.5 text-sm",
            tab === t ? "bg-panel-2 text-fg ring-1 ring-line-2" : "text-dim hover:text-fg")}>
            {t === "trades" ? `Trades (${d.trades.length})` : t === "learning" ? "Learnings" : "Log"}</button>))}
      </div>

      {tab === "trades" && (
        <Card title={`Trades on ${d.day}`} right={closed.length > 0 && <span className={clsx("num text-sm", toneOf(closed.reduce((a, t) => a + (t.net ?? 0), 0)))}>{rs(closed.reduce((a, t) => a + (t.net ?? 0), 0))} booked</span>}>
          {d.trades.length === 0 ? <Empty>No trades on {d.day}.</Empty> : ["ai_trader", "futures", "options"].includes(d.kind) ? (
            <div className="overflow-x-auto"><table className="num w-full text-sm">
              <thead><tr className="text-left text-[11px] text-faint">{["time", "event", "stock", "side", "qty", "price", "stop", "target", "net", "note"].map((h) => <th key={h} className="py-1 pr-2 font-normal">{h}</th>)}</tr></thead>
              <tbody>{d.trades.map((t, i) => (
                <tr key={i} className="border-t border-line">
                  <td className="py-1.5 pr-2 text-dim">{t.time}</td><td className="pr-2">{t.event}</td><td className="pr-2">{t.stock}</td>
                  <td className="pr-2">{t.side}</td><td className="pr-2">{t.qty}</td><td className="pr-2">{t.price}</td><td className="pr-2">{t.stop}</td>
                  <td className="pr-2">{t.target}</td><td className={clsx("pr-2", toneOf(Number(t.net) || null))}>{t.net}</td>
                  <td className="max-w-[260px] truncate text-xs text-faint" title={t.note}>{t.note}</td>
                </tr>))}</tbody>
            </table></div>
          ) : (
            <div className="overflow-x-auto"><table className="num w-full text-sm">
              <thead><tr className="text-left text-[11px] text-faint">{["in", "out", "stock", "side", "qty", "entry", "stop", "target", "exit", "why", "net"].map((h) => <th key={h} className="py-1 pr-2 font-normal">{h}</th>)}</tr></thead>
              <tbody>{d.trades.map((t, i) => (
                <tr key={i} className="border-t border-line">
                  <td className="py-1.5 pr-2 text-dim">{t.opened}</td><td className="pr-2 text-dim">{t.closed ?? <Badge tone="info">open</Badge>}</td>
                  <td className="pr-2">{t.stock}</td><td className={clsx("pr-2", t.side === "BUY" ? "text-up" : "text-down")}>{t.side}</td>
                  <td className="pr-2">{t.qty}</td><td className="pr-2">{t.entry}</td><td className="pr-2">{t.stop}</td><td className="pr-2">{t.target}</td>
                  <td className="pr-2">{t.exit ?? ""}</td><td className="pr-2 text-xs text-faint">{t.why}{t.followed && <span className="block">via {t.followed}</span>}</td>
                  <td className={clsx("text-right", toneOf(t.net))}>{t.net == null ? "" : rs(t.net, 2)}</td>
                </tr>))}</tbody>
            </table></div>)}
        </Card>)}

      {tab === "learning" && (
        <Card title={`The AI's learnings, ${d.day}`}>
          {d.learning_md ? <Md text={d.learning_md} /> : <Empty>Learnings are written at 16:10 on trading days.</Empty>}
        </Card>)}

      {tab === "log" && (
        <Card title={`Trade log, ${d.day}`} right={<span className="text-xs text-faint">strategies/{d.name}/logs/{d.day}.md</span>}>
          {d.log_rows.length === 0 ? <Empty>No log for {d.day}.</Empty> : (
            <div className="overflow-x-auto"><table className="num w-full text-xs">
              <thead><tr className="text-left text-[11px] text-faint">{["time", "event", "stock", "side", "qty", "price", "stop", "target", "net", "note"].map((h) => <th key={h} className="py-1 pr-2 font-normal">{h}</th>)}</tr></thead>
              <tbody>{d.log_rows.map((r, i) => (
                <tr key={i} className="border-t border-line">
                  {(["time", "event", "stock", "side", "qty", "price", "stop", "target", "net", "note"] as const).map((c) => (
                    <td key={c} className={clsx("py-1 pr-2", c === "net" && toneOf(Number(r.net) || null), c === "note" && "max-w-[300px] truncate text-faint")} title={c === "note" ? r.note : undefined}>{r[c]}</td>))}
                </tr>))}</tbody>
            </table></div>)}
          {d.log_md.includes("DAY TARGET") && <p className="mt-2 inline-flex items-center gap-1 text-sm text-up"><Target size={14} />{d.log_md.split("\n").find((l) => l.includes("DAY TARGET"))?.replace("> ", "")}</p>}
        </Card>)}
    </div>
  );
}

export default function Strategies() {
  const [ov, setOv] = useState<StratOverview | null>(null);
  const [pl, setPl] = useState<PipelineResp | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [kind, setKind] = useState<string>("all");
  const [q, setQ] = useState("");
  useEffect(() => {
    let live = true;
    const load = async () => {
      try { const [a, b] = await Promise.all([getStrategies(), getPipeline()]); if (live) { setOv(a); setPl(b); } } catch { /* offline */ }
    };
    load();
    const id = setInterval(load, 10000);
    return () => { live = false; clearInterval(id); };
  }, []);
  const rows = useMemo(() => (ov?.strategies ?? []).filter((r) => (kind === "all" || r.kind === kind) && r.name.toLowerCase().includes(q.toLowerCase())), [ov, kind, q]);

  if (sel) return <Detail key={sel} name={sel} onBack={() => setSel(null)} onOpen={setSel} />;
  if (!ov) return <Empty>loading strategies...</Empty>;
  const hit = ov.strategies.filter((r) => r.today.target_hit);
  const traded = ov.strategies.filter((r) => r.today.trades > 0 || r.today.open > 0);
  const best = ov.strategies[0];

  return (
    <div className="grid gap-4">
      <Pipeline p={pl} />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[["Strategies", `${ov.strategies.length} | ${traded.length} traded today`],
          ["Hit the Rs " + ov.target.toLocaleString("en-IN") + " target", `${hit.length}`],
          ["All strategies today", rs(ov.total_today)],
          ["Best today", best ? `${best.name} ${rs(best.today.net)}` : "--"]].map(([l, v], i) => (
          <div key={l} className="rounded-xl border border-line bg-panel/80 p-3">
            <div className="text-[11px] uppercase tracking-wider text-faint">{l}</div>
            <div className={clsx("num mt-1 truncate font-semibold", i === 2 && toneOf(ov.total_today))}>{v}</div>
          </div>))}
      </div>

      <Card title={`Every strategy, ${ov.day}`} right={<span className="text-xs text-faint">Rs {ov.wallet.toLocaleString("en-IN")} paper wallet each | tap one to open</span>}>
        <div className="mb-3 flex flex-wrap items-center gap-1.5">
          {["all", "ai_agent", "ai_trader", "ai_tuned", "fixed", "inverted", "futures", "options"].map((k2) => (
            <button key={k2} onClick={() => setKind(k2)} className={clsx("rounded-full border px-2.5 py-0.5 text-xs",
              kind === k2 ? "border-info text-fg" : "border-line text-dim")}>{k2 === "all" ? "all" : KIND[k2].label}</button>))}
          <label className="ml-auto flex items-center gap-1 rounded-lg border border-line bg-panel-2 px-2 py-1 text-xs">
            <Search size={12} className="text-faint" /><input value={q} onChange={(e) => setQ(e.target.value)} placeholder="find"
              className="w-24 bg-transparent outline-none" /></label>
        </div>
        <div className="overflow-x-auto"><table className="num w-full text-sm">
          <thead><tr className="text-left text-[11px] text-faint">
            <th className="py-1 pr-2 font-normal">strategy</th><th className="pr-2 text-right font-normal">today</th>
            <th className="pr-2 font-normal">trades</th><th className="pr-2 font-normal">target</th>
            <th className="pr-2 font-normal">last days</th><th className="pr-2 text-right font-normal">all days</th>
            <th className="font-normal">latest learning</th></tr></thead>
          <tbody>{rows.map((r: StratRow) => (
            <tr key={r.name} onClick={() => setSel(r.name)} className="cursor-pointer border-t border-line hover:bg-panel-2">
              <td className="py-2 pr-2"><div className="font-medium">{r.name}</div><Badge tone={KIND[r.kind].tone}>{KIND[r.kind].label}</Badge></td>
              <td className={clsx("pr-2 text-right font-semibold", toneOf(r.today.net))}>{rs(r.today.net)}</td>
              <td className="pr-2 text-dim">{r.today.trades}{r.today.trades ? ` (${r.today.wins}W)` : ""}{r.today.open ? ` +${r.today.open} open` : ""}</td>
              <td className="pr-2">{r.today.target_hit ? <Badge tone="up"><Target size={11} />hit</Badge> : <span className="text-faint">--</span>}</td>
              <td className="pr-2"><Spark h={r.history} target={r.target} /></td>
              <td className={clsx("pr-2 text-right", toneOf(r.total))}>{rs(r.total)}<div className="text-[11px] text-faint">{r.green_days}/{r.days} green</div></td>
              <td className="max-w-[280px] truncate text-xs text-dim" title={r.learning?.text}>{r.learning ? `${r.learning.day.slice(5)}: ${r.learning.text}` : ""}</td>
            </tr>))}</tbody>
        </table></div>
      </Card>
    </div>
  );
}
