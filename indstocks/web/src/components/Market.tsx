import { Gauge, Newspaper, Scale, ShieldAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { getSentiment, type Score, type SentimentResp } from "../api";
import { clsx } from "../fmt";
import { Badge, Card, Empty, Row } from "../ui";

const toneOf = (x: number | null | undefined, cut = 0.3) => (x == null ? "dim" : x >= cut ? "up" : x <= -cut ? "down" : "dim");
const word = (x: number) => (x >= 0.3 ? "bullish" : x <= -0.3 ? "bearish" : "neutral");
const sgn = (x: number | null | undefined, d = 2) => (x == null ? "—" : `${x > 0 ? "+" : ""}${x.toFixed(d)}`);

function BiasBar({ v }: { v: number }) {
  const pos = ((v + 1) / 2) * 100;
  return (
    <div className="relative mt-3 h-2.5 rounded-full bg-gradient-to-r from-down/70 via-line-2 to-up/70">
      <div className="absolute top-1/2 h-4 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded bg-fg shadow" style={{ left: `${pos}%` }} />
      <div className="absolute -bottom-5 left-0 text-[10px] text-faint">bearish</div>
      <div className="absolute -bottom-5 right-0 text-[10px] text-faint">bullish</div>
    </div>
  );
}

function ScoreTable({ title, rows, labels }: { title: string; rows: Record<string, Score>; labels: Record<string, string> }) {
  const keys = Object.keys(rows);
  return (
    <div>
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-dim">{title}</div>
      {keys.length === 0 ? <div className="text-sm text-faint">no closed trades yet</div> : (
        <table className="num w-full text-sm">
          <thead><tr className="text-left text-[11px] text-faint"><th className="py-1 font-normal">group</th><th className="font-normal">trades</th><th className="font-normal">win %</th><th className="text-right font-normal">net ₹</th></tr></thead>
          <tbody>{keys.map((k) => (
            <tr key={k} className="border-t border-line">
              <td className="py-1.5 text-dim">{labels[k] ?? k}</td><td>{rows[k].trades}</td>
              <td>{rows[k].trades ? Math.round((rows[k].wins / rows[k].trades) * 100) : 0}%</td>
              <td className={clsx("text-right", rows[k].net >= 0 ? "text-up" : "text-down")}>{sgn(rows[k].net, 0)}</td>
            </tr>))}</tbody>
        </table>
      )}
    </div>
  );
}

export default function Market() {
  const [d, setD] = useState<SentimentResp | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => getSentiment().then((x) => alive && setD(x)).catch(() => {});
    load();
    const iv = setInterval(load, 30000);
    return () => { alive = false; clearInterval(iv); };
  }, []);
  if (!d) return <Card title="Market"><Empty>loading…</Empty></Card>;
  const s = d.snapshot;
  const dirNow = d.direction[d.direction.length - 1];
  const chart = d.history.map((h) => ({ t: h.ts.slice(11, 16), bias: h.bias }));
  const dirMap = new Map(d.direction.map((x) => [x.ts.slice(11, 16), x.score]));
  chart.forEach((c) => { (c as Record<string, unknown>).direction = dirMap.get(c.t) ?? null; });
  const blocked = s ? Object.entries(s.tilt.blocked) : [];
  return (
    <div className="space-y-4">
      {!s ? <Card title="Market sentiment"><Empty>No sentiment collected yet. It runs at 08:45 and every 15 minutes in the session.</Empty></Card> : (
        <>
          <div className="grid gap-4 lg:grid-cols-3">
            <Card title={<span className="inline-flex items-center gap-2"><Gauge size={14} />Sentiment bias</span>}
              right={<Badge tone={d.fresh ? "up" : "warn"}>{d.fresh ? `as of ${s.ts.slice(11, 16)}` : `stale · ${s.ts.slice(11, 16)}`}</Badge>}>
              <div className="flex items-baseline gap-3">
                <span className={clsx("num text-3xl font-semibold", `text-${toneOf(s.tilt.bias)}`)}>{sgn(s.tilt.bias)}</span>
                <span className="text-sm capitalize text-dim">{word(s.tilt.bias)}</span>
                {s.tilt.size < 1 && <Badge tone="warn">size ×{s.tilt.size}</Badge>}
              </div>
              <BiasBar v={s.tilt.bias} />
              <p className="mt-7 text-xs leading-relaxed text-faint">{s.tilt.why}</p>
              {dirNow && <div className="mt-3 flex items-center gap-2 text-sm">price direction now
                <Badge tone={dirNow.state === "UP" ? "up" : dirNow.state === "DOWN" ? "down" : "dim"}>{dirNow.state} {sgn(dirNow.score)}</Badge></div>}
            </Card>
            <Card title={<span className="inline-flex items-center gap-2"><Scale size={14} />Positioning & flows</span>}>
              <Row k="India VIX" v={s.positioning?.vix ? <span className={clsx((s.positioning.vix.change_pct ?? 0) > 5 && "text-warn")}>{s.positioning.vix.level} ({sgn(s.positioning.vix.change_pct, 1)}%)</span> : <span className="text-faint">{s.positioning?.vix_error ?? "—"}</span>} />
              <Row k="Nifty 50" v={s.positioning?.nifty ? `${s.positioning.nifty.level} (${sgn(s.positioning.nifty.change_pct)}%)` : "—"} />
              {s.positioning?.options ? <>
                <Row k="Put/call OI" v={<span className={`text-${toneOf((s.positioning.options.pcr ?? 1) - 1, 0.2)}`}>{s.positioning.options.pcr}</span>} />
                <Row k="Call wall · resistance" v={<span className="text-down">{s.positioning.options.call_wall}</span>} />
                <Row k="Put wall · support" v={<span className="text-up">{s.positioning.options.put_wall}</span>} />
              </> : <Row k="Option chain" v={<span className="text-faint">{s.positioning?.options_error ?? "—"}</span>} />}
              <Row k="FII cash" v={s.flows?.fii ? <span className={s.flows.fii.net_cr >= 0 ? "text-up" : "text-down"}>{sgn(s.flows.fii.net_cr, 0)} cr</span> : <span className="text-faint">{s.flows?.error ? "unavailable" : "—"}</span>} />
              <Row k="DII cash" v={s.flows?.dii ? <span className={s.flows.dii.net_cr >= 0 ? "text-up" : "text-down"}>{sgn(s.flows.dii.net_cr, 0)} cr</span> : "—"} />
            </Card>
            <Card title={<span className="inline-flex items-center gap-2"><ShieldAlert size={14} />Events & blocks today</span>}>
              {(s.events?.market ?? []).length === 0 && Object.keys(s.events?.stocks ?? {}).length === 0 && blocked.length === 0
                ? <div className="text-sm text-faint">No events, nothing blocked.</div> : (
                <ul className="space-y-1.5 text-sm">
                  {(s.events?.market ?? []).map((e, i) => <li key={i}><Badge tone={e.window ? "warn" : "info"}>{e.at ?? "all day"}</Badge> <span className="text-dim">{e.what}</span></li>)}
                  {blocked.map(([sym, sides]) => Object.entries(sides).map(([side, why]) => (
                    <li key={sym + side}><Badge tone="down">no {side === "BUY" ? "longs" : "shorts"}</Badge> <b>{sym}</b> <span className="text-dim">{why}</span></li>)))}
                </ul>)}
              {s.events?.error && <p className="mt-2 text-xs text-faint">{s.events.error}</p>}
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-5">
            <Card className="lg:col-span-3" title={<span className="inline-flex items-center gap-2"><Newspaper size={14} />Claude's news read</span>}
              right={s.news_ts && <span className="text-xs text-faint">read {s.news_ts.slice(11, 16)}</span>}>
              {s.news?.market ? <>
                <div className="flex items-center gap-2"><Badge tone={toneOf(s.news.market.score)}>news {sgn(s.news.market.score)}</Badge><span className="text-sm">{s.news.market.summary}</span></div>
                {s.news.market.drivers.length > 0 && <ul className="mt-2 list-disc pl-5 text-sm text-dim">{s.news.market.drivers.map((x, i) => <li key={i}>{x}</li>)}</ul>}
                {Object.keys(s.news.stocks ?? {}).length > 0 && (
                  <div className="mt-3 space-y-1">{Object.entries(s.news.stocks!).map(([sym, v]) => (
                    <div key={sym} className="flex items-start gap-2 text-sm"><Badge tone={toneOf(v.score)}>{sym} {sgn(v.score)}</Badge>
                      <span className="text-dim">{v.why}{v.material ? " · material" : ""}</span></div>))}</div>)}
              </> : <div className="text-sm text-faint">{s.news_error ?? "no news read yet"}</div>}
              {(s.headlines ?? []).length > 0 && (
                <details className="mt-3"><summary className="cursor-pointer text-xs text-info">{s.headlines!.length} headlines read</summary>
                  <ul className="mt-2 max-h-72 space-y-1 overflow-y-auto text-xs text-dim">{s.headlines!.map((h, i) => <li key={i}><span className="num text-faint">{h.t}</span> <span className="text-faint">{h.src}</span> · {h.h}</li>)}</ul>
                </details>)}
              {(s.news_errors ?? []).length > 0 && <p className="mt-2 text-[11px] text-faint">unreachable: {s.news_errors!.join("; ")}</p>}
            </Card>
            <Card className="lg:col-span-2" title="Bias vs price direction today">
              {chart.length < 2 ? <Empty>Builds up through the session.</Empty> : (
                <div className="h-56"><ResponsiveContainer>
                  <LineChart data={chart} margin={{ left: -20, right: 8, top: 8 }}>
                    <CartesianGrid stroke="var(--color-line)" vertical={false} />
                    <XAxis dataKey="t" tick={{ fontSize: 10, fill: "var(--color-faint)" }} />
                    <YAxis domain={[-1, 1]} tick={{ fontSize: 10, fill: "var(--color-faint)" }} />
                    <ReferenceLine y={0} stroke="var(--color-line-2)" />
                    <Tooltip contentStyle={{ background: "var(--color-panel)", border: "1px solid var(--color-line-2)", fontSize: 12 }} />
                    <Line dataKey="bias" name="sentiment" stroke="var(--color-info)" dot={false} strokeWidth={2} />
                    <Line dataKey="direction" name="price direction" stroke="var(--color-warn)" dot={false} strokeWidth={2} connectNulls />
                  </LineChart>
                </ResponsiveContainer></div>)}
            </Card>
          </div>
        </>
      )}
      <Card title="Does it work? Closed trades, last 60 days">
        <div className="grid gap-6 md:grid-cols-2">
          <ScoreTable title="vs sentiment at entry" rows={d.scorecard.sentiment}
            labels={{ with: "went with sentiment", against: "went against it", neutral: "sentiment neutral", unknown: "no sentiment" }} />
          <ScoreTable title="vs price direction at entry" rows={d.scorecard.direction}
            labels={{ UP: "market UP", DOWN: "market DOWN", MIXED: "market MIXED", FORMING: "before 09:30", unknown: "not recorded" }} />
        </div>
        <p className="mt-3 text-xs text-faint">If trades that went with the sentiment do no better than the rest after a few weeks, the sentiment tilt is not earning its place and should be switched off.</p>
      </Card>
    </div>
  );
}
