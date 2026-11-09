import { LogOut, Shield, ShieldAlert } from "lucide-react";
import type { Closed, Position } from "../api";
import { clsx, money, n, pct, price, signed, tone } from "../fmt";
import { Badge, Btn, Card, Empty, Flash } from "../ui";

/** Where LTP sits between stop and target, as a bar with an entry tick. */
function Ladder({ p }: { p: Position }) {
  if (p.stop == null || p.target == null) return null;
  // left = stop, right = target, for longs and shorts alike
  const lo = n(p.stop), hi = n(p.target), span = hi - lo;
  if (!span) return null;
  const at = (x: number) => Math.min(100, Math.max(0, ((x - lo) / span) * 100));
  const ltp = at(n(p.ltp)), entry = at(n(p.avg));
  const winning = p.qty > 0 ? n(p.ltp) >= n(p.avg) : n(p.ltp) <= n(p.avg);
  return (
    <div className="mt-3">
      <div className="relative h-2 rounded-full bg-gradient-to-r from-down/40 via-line-2 to-up/40">
        <div className="absolute top-1/2 h-3 w-px -translate-y-1/2 bg-dim" style={{ left: `${entry}%` }} title="entry" />
        <div className={clsx("absolute top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-bg shadow",
          winning ? "bg-up" : "bg-down")} style={{ left: `${ltp}%` }} title="LTP" />
      </div>
      <div className="num mt-1.5 flex justify-between text-[11px]">
        <span className="text-down">stop {price(p.stop)}{p.stop_live ? "" : " (planned)"}</span>
        <span className="text-faint">entry {price(p.avg)}</span>
        <span className="text-up">target {price(p.target)}</span>
      </div>
    </div>
  );
}

export function Positions({ items, onExit }: { items: Position[]; onExit: (p: Position) => void }) {
  return (
    <Card title={`Live positions · ${items.length}`}>
      {items.length === 0 ? <Empty>No open intraday positions.</Empty> : (
        <div className="grid gap-3 md:grid-cols-2">
          {items.map((p) => (
            <div key={p.token} className="rounded-xl border border-line bg-panel-2/60 p-3.5">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="font-semibold">{p.symbol}</span>
                    <Badge tone={p.qty > 0 ? "up" : "down"}>{p.qty > 0 ? "LONG" : "SHORT"} {Math.abs(p.qty)}</Badge>
                    <Badge tone={p.source === "ai" ? "info" : "warn"}>{p.source === "ai" ? "AI" : "MANUAL"}</Badge>
                  </div>
                  <div className="num mt-1 text-xs text-dim">avg {price(p.avg)} · ltp <Flash value={n(p.ltp)}>{price(p.ltp)}</Flash></div>
                </div>
                <div className="text-right">
                  <div className={clsx("num text-lg font-semibold", tone(p.pnl))}>{signed(p.pnl)}</div>
                  <div className={clsx("num text-xs", tone(p.pnl))}>{pct(p.pct)}</div>
                </div>
              </div>
              <Ladder p={p} />
              <div className="mt-3 flex items-center justify-between gap-2">
                <span className={clsx("inline-flex items-center gap-1 text-xs", p.stop_live ? "text-dim" : "text-down")}>
                  {p.stop_live ? <Shield size={13} /> : <ShieldAlert size={13} />}
                  {p.stop_live ? "stop resting at NSE" : p.source === "ai" ? "no stop yet — monitor placing" : "manual: ±% guard"}
                </span>
                <Btn variant="danger" className="px-2.5 py-1.5 text-xs" onClick={() => onExit(p)}><LogOut size={13} />Exit</Btn>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

export function ClosedTrades({ items }: { items: Closed[] }) {
  if (!items.length) return null;
  return (
    <Card title={`Closed today · ${items.length}`} pad={false}>
      <div className="divide-y divide-line">
        {items.map((c) => (
          <div key={c.token} className="flex items-center justify-between gap-3 px-4 py-2.5">
            <div>
              <div className="font-medium">{c.symbol} <span className="text-xs text-faint">× {c.qty}</span></div>
              <div className="num text-xs text-dim">{price(c.buy_avg)} → {price(c.sell_avg)} · fees {money(c.charges)}</div>
            </div>
            <div className="text-right">
              <div className={clsx("num font-semibold", tone(c.net))}>{signed(c.net)}</div>
              <Badge tone={n(c.net) > 0 ? "up" : "down"}>{n(c.net) > 0 ? "WIN" : "LOSS"}</Badge>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}
