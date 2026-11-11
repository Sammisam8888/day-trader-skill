import { X } from "lucide-react";
import { useEffect, useState } from "react";
import { getTrade, type Trade } from "../api";
import { clsx, money, n, price, signed, tone } from "../fmt";
import { Badge, Row } from "../ui";

const STATUS: Record<string, string> = { resting: "warn", open: "info", closed: "dim", cancelled: "dim", unknown: "dim" };

/** One trade end to end, opened from the Claude tab or a link. */
export default function TradeDrawer({ day, id, onClose, onCall }:
  { day: string; id: string; onClose: () => void; onCall?: (callId: string) => void }) {
  const [t, setT] = useState<Trade | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => getTrade(day, id).then((x) => alive && setT(x)).catch(() => {});
    load();
    const iv = setInterval(load, 5000);
    return () => { alive = false; clearInterval(iv); };
  }, [day, id]);
  const p = t?.plan ?? {};
  const win = n(p.net_if_target), lose = n(p.net_if_stop);
  const span = Math.abs(win) + Math.abs(lose) || 1;
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/60" onClick={onClose}>
      <aside className="h-full w-full max-w-lg overflow-y-auto border-l border-line-2 bg-panel p-5" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start justify-between">
          <div>
            <div className="text-xs text-faint">trade · order {id}</div>
            {t && !t.error && (
              <h3 className="mt-1 flex items-center gap-2 text-xl font-semibold">
                {t.symbol}
                <Badge tone={t.side === "B" ? "up" : "down"}>{t.side === "B" ? "LONG" : "SHORT"}</Badge>
                <Badge tone={STATUS[t.status] ?? "dim"}>{t.status.toUpperCase()}</Badge>
              </h3>
            )}
          </div>
          <button onClick={onClose} className="rounded p-1 text-dim hover:text-fg"><X size={20} /></button>
        </div>
        {!t ? <p className="mt-6 text-sm text-dim">loading…</p> : t.error ? <p className="mt-6 text-sm text-down">{t.error}</p> : (
          <div className="mt-5 space-y-5">
            <section>
              <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-dim">If it goes all the way, after charges</h4>
              <div className="grid grid-cols-2 gap-2">
                <div className="rounded-xl border border-up/30 bg-up-bg p-3">
                  <div className="text-xs text-up/80">target {price(p.target)} hits</div>
                  <div className="num text-xl font-semibold text-up">{signed(win)}</div>
                </div>
                <div className="rounded-xl border border-down/30 bg-down-bg p-3">
                  <div className="text-xs text-down/80">stop {price(p.stop)} hits</div>
                  <div className="num text-xl font-semibold text-down">{signed(lose)}</div>
                </div>
              </div>
              <div className="mt-2 flex h-2 overflow-hidden rounded-full">
                <div className="bg-down/70" style={{ width: `${(Math.abs(lose) / span) * 100}%` }} />
                <div className="bg-up/70" style={{ width: `${(Math.abs(win) / span) * 100}%` }} />
              </div>
              <div className="num mt-1 text-xs text-faint">reward : risk {(Math.abs(win) / (Math.abs(lose) || 1)).toFixed(2)} after charges</div>
            </section>

            {(t.status === "open" || t.status === "closed") && (
              <section className="rounded-xl border border-line bg-panel-2/60 p-3">
                {t.status === "open" ? (
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-dim">Open now · ltp <span className="num text-fg">{price(t.ltp)}</span></span>
                    <span className={clsx("num text-lg font-semibold", tone(t.live_pnl))}>{signed(t.live_pnl)}</span>
                  </div>
                ) : t.realised ? (
                  <div className="flex items-center justify-between">
                    <span className="text-sm text-dim">Closed · gross {signed(t.realised.gross)} · fees {money(t.realised.charges)}</span>
                    <span className={clsx("num text-lg font-semibold", tone(t.realised.net))}>{signed(t.realised.net)}</span>
                  </div>
                ) : <span className="text-sm text-dim">Closed</span>}
              </section>
            )}

            {t.context && (
              <section>
                <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-dim">Why this trade</h4>
                <Row k="Strategy" v={t.context.setup ? t.context.setup.replace("_", " ") : "Claude's own read (unlisted)"} />
                <Row k="Market direction at entry" v={<Badge tone={t.context.direction === "UP" ? "up" : t.context.direction === "DOWN" ? "down" : "dim"}>{t.context.direction ?? "—"}</Badge>} />
                <Row k="Sentiment bias at entry" v={t.context.sentiment_bias != null ? String(t.context.sentiment_bias) : "—"} />
                {t.context.valid_minutes != null && <Row k="Entry valid for" v={`${t.context.valid_minutes} min`} />}
                {(t.context.advisory ?? []).length > 0 && (
                  <div className="mt-2 rounded-lg border border-warn/30 bg-warn-bg p-2 text-xs text-warn">
                    Signals that disagreed (Claude decided anyway): {(t.context.advisory ?? []).join("; ")}
                  </div>)}
              </section>)}

            <section>
              <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-dim">Plan</h4>
              <Row k="Quantity" v={String(p.qty ?? "—")} />
              <Row k="Order" v={`${p.order_type ?? ""}${p.trigger ? ` · trigger ${price(p.trigger)}` : ""} · limit ${price(p.entry)}`} />
              <Row k="Stop-loss / target" v={<><span className="text-down">{price(p.stop)}</span> / <span className="text-up">{price(p.target)}</span></>} />
              <Row k="Position value" v={money(p.position_value)} />
              <Row k="Leverage" v={`${p.leverage}x`} />
              <Row k="Charges, round trip" v={money(p.costs)} />
              <Row k="Price when placed" v={price(p.ltp_at_placement)} />
            </section>

            <section>
              <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-dim">Timeline</h4>
              <ol className="space-y-1.5 text-sm">
                <li className="text-dim"><span className="num text-faint">{t.placed_at?.slice(11, 19)}</span> placed by {t.decided_by === "claude" ? "Claude" : "the rule"}</li>
                {t.entry_order && <li className="text-dim">entry {String(t.entry_order.status)} · filled {String(t.entry_order.filled)}/{String(t.entry_order.qty)}{n(t.entry_order.avg) ? ` @ ${price(t.entry_order.avg)}` : ""}</li>}
                {t.stop_order && <li className="text-dim">stop at the exchange {price(t.stop_order.trigger)} · {String(t.stop_order.status)}</li>}
                {t.stop_moves.map((m, i) => <li key={i} className="text-dim"><span className="num text-faint">{m.ts?.slice(11, 19)}</span> stop {m.from} → {m.to} ({m.reason})</li>)}
                {t.exit && <li className="text-dim"><span className="num text-faint">{t.exit.ts?.slice(11, 19)}</span> target exit at ltp {t.exit.ltp}{t.exit.order ? ` · ${String(t.exit.order.status)}` : ""}</li>}
              </ol>
            </section>

            <section>
              <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-dim">{t.decided_by === "claude" ? "Claude's reasoning" : "Rule"}</h4>
              <p className="text-sm leading-relaxed text-dim">{t.why}{t.confidence != null ? ` (confidence ${t.confidence})` : ""}</p>
              {t.call_id && onCall && <button onClick={() => onCall(t.call_id)} className="mt-2 text-sm text-info hover:underline">View the full conversation →</button>}
            </section>
          </div>
        )}
      </aside>
    </div>
  );
}
