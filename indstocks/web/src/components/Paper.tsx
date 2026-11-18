import { useEffect, useState } from "react";
import { getPaper, type PaperResp } from "../api";
import { clsx } from "../fmt";
import { Badge, Card, Empty } from "../ui";

const rs = (x: string | number | undefined) => {
  const n = Number(x ?? 0);
  return `${n > 0 ? "+" : n < 0 ? "-" : ""}Rs ${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
};
const hhmm = (t?: number) => (t ? new Date(t * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" }) : "");

export default function Paper() {
  const [d, setD] = useState<PaperResp | null>(null);
  const [only, setOnly] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const load = async () => { try { const r = await getPaper(); if (live) setD(r); } catch { /* offline */ } };
    load();
    const id = setInterval(load, 10000);
    return () => { live = false; clearInterval(id); };
  }, []);
  if (!d) return <Empty>loading the paper bench...</Empty>;
  if (!d.summary) return <Empty>No paper trading yet today. The bench starts with the market.</Empty>;
  const rows = Object.entries(d.summary.models).sort((a, b) => Number(b[1].net) - Number(a[1].net));
  const trades = d.trades.filter((t) => !only || t.model === only);
  const total = Number(d.summary.total);
  return (
    <div className="grid gap-4">
      <Card title="Paper bench: every model, live prices, paper money"
        right={<span className={clsx("num text-lg font-semibold", total >= 0 ? "text-up" : "text-down")}>{rs(total)}</span>}>
        <p className="mb-3 text-xs text-faint">Rs 10,000 per model at 5x, real charges, no broker. "_inv" models take the opposite side of the same signal. Tap a model to filter its trades.</p>
        <div className="overflow-x-auto"><table className="num w-full text-sm">
          <thead><tr className="text-left text-[11px] text-faint">
            <th className="py-1 font-normal">model</th><th className="font-normal">net</th><th className="font-normal">trades</th>
            <th className="font-normal">win %</th><th className="font-normal">open</th><th className="font-normal">kill @75 would fire</th></tr></thead>
          <tbody>{rows.map(([m, r]) => (
            <tr key={m} onClick={() => setOnly(only === m ? null : m)}
              className={clsx("cursor-pointer border-t border-line hover:bg-panel-2", only === m && "bg-panel-2")}>
              <td className="py-1.5">{m}{m.endsWith("_inv") && <Badge className="ml-1">inverted</Badge>}</td>
              <td className={Number(r.net) >= 0 ? "text-up" : "text-down"}>{rs(r.net)}</td>
              <td>{r.trades}</td>
              <td>{r.trades ? Math.round((r.wins / r.trades) * 100) : 0}%</td>
              <td>{r.open_trades || ""}</td>
              <td className="text-dim">{r.would_kill ? `${hhmm(r.would_kill.t)} at ${rs(r.would_kill.net)}` : ""}</td>
            </tr>))}</tbody>
        </table></div>
      </Card>
      <Card title={only ? `Paper trades: ${only}` : "Paper trades (newest first)"} right={only && <button className="text-xs text-info" onClick={() => setOnly(null)}>show all</button>}>
        {trades.length === 0 ? <Empty>no paper trades yet</Empty> : (
          <div className="overflow-x-auto"><table className="num w-full text-sm">
            <thead><tr className="text-left text-[11px] text-faint">
              <th className="py-1 font-normal">time</th><th className="font-normal">model</th><th className="font-normal">stock</th>
              <th className="font-normal">side</th><th className="font-normal">entry</th><th className="font-normal">exit</th>
              <th className="font-normal">why</th><th className="text-right font-normal">net</th></tr></thead>
            <tbody>{trades.slice(0, 200).map((t, i) => (
              <tr key={i} className="border-t border-line">
                <td className="py-1.5 text-dim">{hhmm(t.opened ?? t.t)}{t.ev === "close" ? `-${hhmm(t.t)}` : ""}</td>
                <td>{t.model}</td><td>{t.symbol}</td>
                <td className={t.side === "B" ? "text-up" : "text-down"}>{t.side === "B" ? "BUY" : "SELL"} {t.qty}</td>
                <td>{t.entry}</td><td>{t.exit ?? <Badge tone="info">open</Badge>}</td>
                <td className="text-dim">{t.why ?? ""}</td>
                <td className={clsx("text-right", Number(t.net ?? 0) >= 0 ? "text-up" : "text-down")}>{t.net != null ? rs(t.net) : ""}</td>
              </tr>))}</tbody>
          </table></div>
        )}
      </Card>
    </div>
  );
}
