import { useEffect, useMemo, useState } from "react";
import { getDesk, type DeskResp, type DeskRow } from "../api";
import { clsx } from "../fmt";

// The Desk: one dense live screen built from the system's own feeds.
// Amber-on-black, monospace, every number refreshed every 3 seconds.

const f = (x: number | null | undefined, d = 2, sign = true) =>
  x == null ? "--" : `${sign && x > 0 ? "+" : ""}${x.toFixed(d)}`;
const tone = (x: number | null | undefined, cut = 0) =>
  x == null ? "text-[#7a6a4a]" : x > cut ? "text-[#4ade80]" : x < -cut ? "text-[#f87171]" : "text-[#d4b483]";
type SortKey = "chg_5m" | "chg_1m" | "momentum" | "vol_x" | "imbalance" | "symbol";

function Bar({ v }: { v: number | null }) {
  // book pressure: 0 = all sellers waiting, 1 = all buyers
  if (v == null) return <span className="text-[#7a6a4a]">--</span>;
  const w = Math.round(v * 100);
  return (
    <span className="inline-flex h-2 w-16 overflow-hidden rounded-sm bg-[#3b1d1d] align-middle">
      <span className="bg-[#166534]" style={{ width: `${w}%` }} />
    </span>
  );
}

function Panel({ title, right, children, className }: { title: string; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={clsx("min-w-0 border border-[#3a2f1c] bg-[#0b0905]", className)}>
      <div className="flex items-center justify-between border-b border-[#3a2f1c] bg-[#1a140a] px-2 py-1 text-[11px] font-bold uppercase tracking-widest text-[#f5b942]">
        <span>{title}</span>{right}
      </div>
      <div className="p-2">{children}</div>
    </section>
  );
}

export default function Desk() {
  const [d, setD] = useState<DeskResp | null>(null);
  const [sel, setSel] = useState<string>("NIFTYBEES");
  const [sort, setSort] = useState<SortKey>("chg_5m");
  const [err, setErr] = useState(false);
  useEffect(() => {
    let live = true;
    const load = async () => {
      try { const r = await getDesk(); if (live) { setD(r); setErr(false); } } catch { if (live) setErr(true); }
    };
    load();
    const id = setInterval(load, 3000);
    return () => { live = false; clearInterval(id); };
  }, []);

  const rows = useMemo(() => {
    const m = [...(d?.monitor ?? [])];
    m.sort((a, b) => sort === "symbol" ? a.symbol.localeCompare(b.symbol)
      : Math.abs(Number(b[sort] ?? 0)) - Math.abs(Number(a[sort] ?? 0)));
    return m;
  }, [d, sort]);
  const cur: DeskRow | undefined = d?.monitor.find((r) => r.symbol === sel) ?? rows[0];
  const peers = cur ? (d?.monitor ?? []).filter((r) => r.sector === cur.sector && r.symbol !== cur.symbol) : [];
  const sectors = Object.entries(d?.internals?.sectors ?? {}).sort((a, b) => b[1] - a[1]);
  const adv = d?.internals?.advances ?? 0, dec = d?.internals?.declines ?? 0;

  const H = ({ k, children }: { k: SortKey; children: React.ReactNode }) => (
    <th onClick={() => setSort(k)} className={clsx("cursor-pointer px-1 py-1 text-right font-normal", sort === k ? "text-[#f5b942]" : "text-[#9a875f]")}>{children}</th>
  );

  return (
    <div className="font-mono text-[12px] leading-tight text-[#d4b483]">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 border border-[#3a2f1c] bg-[#1a140a] px-2 py-1.5">
        <span className="font-bold tracking-widest text-[#f5b942]">DESK</span>
        <span>BREADTH <span className="text-[#4ade80]">{adv}</span>/<span className="text-[#f87171]">{dec}</span></span>
        {sectors.filter(([n]) => n === "NIFTY 50" || n === "NIFTY BANK" || n === "INDIA VIX").map(([n, v]) => (
          <span key={n}>{n} <span className={tone(n === "INDIA VIX" ? -v : v)}>{f(v)}%</span></span>))}
        <span className="ml-auto text-[#7a6a4a]">{err ? "OFFLINE" : `${d?.monitor.length ?? 0} live | 3s`}</span>
      </div>

      <div className="grid gap-2 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <Panel title="Monitor" right={<span className="text-[#7a6a4a] normal-case tracking-normal">tap a column to sort, a row to open</span>}>
          {rows.length === 0 ? <div className="py-6 text-center text-[#7a6a4a]">The tape starts at 09:05 on trading days.</div> : (
            <div className="max-h-[520px] overflow-auto">
              <table className="w-full whitespace-nowrap">
                <thead className="sticky top-0 bg-[#0b0905]"><tr>
                  <th onClick={() => setSort("symbol")} className="cursor-pointer px-1 py-1 text-left font-normal text-[#9a875f]">SYM</th>
                  <th className="px-1 text-right font-normal text-[#9a875f]">LAST</th>
                  <H k="chg_1m">1M%</H><H k="chg_5m">5M%</H><H k="vol_x">VOLx</H><H k="momentum">MOM</H>
                  <H k="imbalance">BOOK</H>
                </tr></thead>
                <tbody>{rows.map((r) => (
                  <tr key={r.symbol} onClick={() => setSel(r.symbol)}
                    className={clsx("cursor-pointer border-t border-[#1f1910] hover:bg-[#1a140a]", cur?.symbol === r.symbol && "bg-[#241b0c]")}>
                    <td className="px-1 py-[3px]"><span className={r.traded ? "text-[#f5b942]" : ""}>{r.symbol}</span> <span className="text-[10px] text-[#7a6a4a]">{r.sector}</span></td>
                    <td className="px-1 text-right">{r.ltp?.toFixed(2) ?? "--"}</td>
                    <td className={clsx("px-1 text-right", tone(r.chg_1m))}>{f(r.chg_1m)}</td>
                    <td className={clsx("px-1 text-right", tone(r.chg_5m))}>{f(r.chg_5m)}</td>
                    <td className={clsx("px-1 text-right", (r.vol_x ?? 0) >= 2 ? "text-[#f5b942]" : "")}>{f(r.vol_x, 1, false)}</td>
                    <td className={clsx("px-1 text-right", tone(r.momentum, 0.2))}>{f(r.momentum)}</td>
                    <td className="px-1 text-right"><Bar v={r.imbalance} /></td>
                  </tr>))}</tbody>
              </table>
            </div>)}
        </Panel>

        <div className="grid min-w-0 content-start gap-2">
          <Panel title={cur ? `${cur.symbol}  ${cur.sector}` : "Stock"} right={cur && <span className="normal-case tracking-normal text-[#7a6a4a]">moves with {cur.index}</span>}>
            {!cur ? <div className="text-[#7a6a4a]">--</div> : (
              <div className="grid gap-2">
                <div className="flex items-baseline gap-3">
                  <span className="text-2xl text-[#fde68a]">{cur.ltp?.toFixed(2) ?? "--"}</span>
                  <span className={tone(cur.chg_5m)}>{f(cur.chg_5m)}% 5m</span>
                  <span className={tone(cur.chg_1m)}>{f(cur.chg_1m)}% 1m</span>
                </div>
                <div className="grid grid-cols-2 gap-x-4 gap-y-1">
                  <span>MOMENTUM <span className={tone(cur.momentum, 0.2)}>{f(cur.momentum)}</span></span>
                  <span>VOLUME <span className={(cur.vol_x ?? 0) >= 2 ? "text-[#f5b942]" : ""}>{f(cur.vol_x, 1, false)}x normal</span></span>
                  <span>UPTICKS <span>{cur.upticks == null ? "--" : `${Math.round(cur.upticks * 100)}%`}</span></span>
                  <span>SPREAD <span>{f(cur.spread_bps, 1, false)} bps</span></span>
                  <span className="col-span-2">BOOK {cur.imbalance == null ? "--" : `${Math.round(cur.imbalance * 100)}% buyers waiting`} <Bar v={cur.imbalance} /></span>
                  <span className="col-span-2">INDEX {cur.index} <span className={tone(d?.internals?.sectors?.[cur.index])}>{f(d?.internals?.sectors?.[cur.index])}%</span></span>
                </div>
                <div>
                  <div className="mb-1 text-[11px] uppercase tracking-widest text-[#9a875f]">Peers ({cur.sector})</div>
                  {peers.length === 0 ? <span className="text-[#7a6a4a]">none on the tape</span> : peers.map((p) => (
                    <button key={p.symbol} onClick={() => setSel(p.symbol)} className="mr-3 inline-block hover:text-[#f5b942]">
                      {p.symbol} <span className={tone(p.chg_5m)}>{f(p.chg_5m)}</span>
                    </button>))}
                </div>
              </div>)}
          </Panel>

          <Panel title="Sectors">
            <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
              {sectors.filter(([n]) => n !== "INDIA VIX").map(([n, v]) => (
                <div key={n} className="flex justify-between gap-2">
                  <span className="truncate">{n.replace("NIFTY ", "")}</span>
                  <span className={tone(v)}>{f(v)}%</span>
                </div>))}
              {sectors.length === 0 && <span className="text-[#7a6a4a]">waiting for NSE</span>}
            </div>
          </Panel>

          <Panel title="AI read" right={<span className="normal-case tracking-normal text-[#7a6a4a]">{d?.ai?.ts?.slice(11, 16)}</span>}>
            <p className="font-sans text-[12px] leading-snug text-[#e7d6b0]">{d?.ai?.view ?? "no scan yet today"}</p>
          </Panel>
        </div>

        <Panel title="News and shocks">
          <div className="max-h-[260px] overflow-auto">
            {(d?.news ?? []).length === 0 ? <span className="text-[#7a6a4a]">nothing yet today</span> : d!.news.map((n, i) => (
              <div key={i} className="border-t border-[#1f1910] py-1 first:border-0">
                <span className="text-[#7a6a4a]">{(n.ts ?? "").slice(11, 16)} </span>
                {n.symbol && <span className="text-[#f5b942]">{n.symbol} </span>}
                {n.event === "price_shock" ? <span className={tone(n.move_pct ?? 0)}>price shock {f(n.move_pct ?? 0)}%</span>
                  : <span className="font-sans">{n.headline}</span>}
                {n.score != null && <span className={clsx("ml-1", tone(n.score, 0.1))}>[{f(n.score, 1)}]</span>}
                {n.source && <span className="ml-1 text-[#7a6a4a]">{n.source}</span>}
              </div>))}
          </div>
        </Panel>

        <Panel title="Paper bench leaders">
          {(d?.bench ?? []).length === 0 ? <span className="text-[#7a6a4a]">no paper trades yet today</span> : (
            <table className="w-full">
              <tbody>{d!.bench.map((b) => (
                <tr key={b.model} className="border-t border-[#1f1910] first:border-0">
                  <td className="py-[3px]">{b.model}</td>
                  <td className="text-right text-[#9a875f]">{b.wins}/{b.trades}</td>
                  <td className={clsx("text-right", tone(b.net))}>{f(b.net, 0)}</td>
                </tr>))}</tbody>
            </table>)}
        </Panel>
      </div>
    </div>
  );
}
