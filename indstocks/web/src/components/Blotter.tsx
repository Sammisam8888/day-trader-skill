import { ChevronDown, X } from "lucide-react";
import { useState } from "react";
import type { Order } from "../api";
import { clsx, money, price } from "../fmt";
import { Badge, Btn, Card, Empty, Row } from "../ui";

const KIND: Record<string, { label: string; tone: string }> = {
  entry: { label: "ENTRY", tone: "info" }, stop: { label: "STOP", tone: "down" },
  target: { label: "TARGET", tone: "up" }, flatten: { label: "FLATTEN", tone: "warn" },
  chat: { label: "CHAT", tone: "info" }, exit: { label: "EXIT", tone: "dim" }, buy: { label: "BUY", tone: "dim" },
};
const STATUS_TONE = { wait: "warn", fill: "up", dead: "dim" } as const;

function OrderRow({ o, onCancel, wide }: { o: Order; onCancel: (o: Order) => void; wide: boolean }) {
  // Full table columns only in the wide (Orders tab) view on desktop; the
  // compact card and phones use the stacked layout.
  const W = (c: string) => (wide ? c : "");
  const [open, setOpen] = useState(false);
  const k = KIND[o.kind] ?? KIND.buy;
  const t = (o.time || "").split(" ")[0]?.slice(0, 8);
  return (
    <div className={clsx("border-b border-line last:border-0", !o.pending && "opacity-70")}>
      <div className={clsx("grid grid-cols-[1fr_auto] items-center gap-2 px-4 py-2.5", W("md:grid-cols-[70px_1.6fr_70px_70px_1fr_1fr_170px_60px]"))}>
        <span className={clsx("num hidden text-xs text-faint", W("md:block"))}>{t || "—"}</span>
        <div className="flex min-w-0 items-center gap-2">
          <Badge tone={o.side === "B" ? "up" : "down"}>{o.side === "B" ? "BUY" : "SELL"}</Badge>
          <span className="truncate font-semibold">{o.symbol}</span>
          <Badge tone={k.tone} className="hidden sm:inline-flex">{k.label}</Badge>
          <Badge tone={o.source === "ai" ? "info" : "warn"}>{o.source === "ai" ? "AI" : "YOU"}</Badge>
        </div>
        <span className={clsx("num hidden text-right text-sm", W("md:block"))}>{o.filled}/{o.qty}</span>
        <span className={clsx("hidden text-xs text-dim", W("md:block"))}>{o.type}</span>
        <span className={clsx("num hidden text-right text-sm", W("md:block"))}>{o.trigger ? price(o.trigger) : "—"}</span>
        <span className={clsx("num hidden text-right text-sm", W("md:block"))}>{o.avg ? price(o.avg) : price(o.price)}</span>
        <span className={clsx("hidden justify-end", W("md:flex"))}><Badge tone={STATUS_TONE[o.tone]}>{o.label}</Badge></span>
        <div className="flex items-center justify-end gap-1">
          {o.plan && <button onClick={() => setOpen(!open)} className="rounded p-1 text-dim hover:text-fg" aria-label="why">
            <ChevronDown size={16} className={clsx("transition", open && "rotate-180")} /></button>}
          {o.pending && <button onClick={() => onCancel(o)} className="rounded p-1 text-down/80 hover:text-down" aria-label="cancel"><X size={16} /></button>}
        </div>
      </div>
      <div className={clsx("num -mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 px-4 pb-2 text-xs text-dim", W("md:hidden"))}>
        <Badge tone={STATUS_TONE[o.tone]}>{o.label}</Badge>
        <span>{o.filled}/{o.qty}</span><span>{o.type}</span>
        {o.trigger ? <span>trg {price(o.trigger)}</span> : null}<span>lmt {price(o.price)}</span>
        {o.ltp != null && <span>ltp {price(o.ltp)}</span>}
      </div>
      {o.reason && <div className="px-4 pb-2 text-xs text-down">{o.reason}</div>}
      {open && o.plan && (
        <div className="mx-4 mb-3 rounded-xl border border-line bg-bg/60 p-3">
          <p className="mb-2 text-[13px] leading-relaxed text-dim"><b className="text-fg">Why: </b>{o.plan.why}</p>
          <Row k="Stop / target" v={<><span className="text-down">{price(o.plan.stop)}</span> / <span className="text-up">{price(o.plan.target)}</span></>} />
          <Row k="Position value" v={money(o.plan.position_value)} />
          <Row k="Leverage (order / cash)" v={`${o.plan.leverage}x`} />
          <Row k="Max loss at stop" v={`−${money(o.plan.risk)}`} className="text-down" />
          <Row k="Gross if target" v={`+${money(o.plan.gross_if_target)}`} className="text-up" />
          <Row k="Charges, round trip" v={`−${money(o.plan.costs)}`} />
          <Row k="Net if target" v={money(o.plan.net_if_target)} className="text-up" />
        </div>
      )}
    </div>
  );
}

export default function Blotter({ orders, onCancel, compact }: { orders: Order[]; onCancel: (o: Order) => void; compact?: boolean }) {
  const [all, setAll] = useState(!compact);
  const open = orders.filter((o) => o.pending);
  const shown = all ? orders : open;
  return (
    <Card title={`Orders · ${open.length} open`} pad={false}
      right={<button onClick={() => setAll(!all)} className="text-xs text-dim hover:text-fg">{all ? "open only" : `all ${orders.length}`}</button>}>
      <div className={clsx("hidden grid-cols-[70px_1.6fr_70px_70px_1fr_1fr_170px_60px] gap-2 border-b border-line px-4 py-2 text-[10px] font-semibold uppercase tracking-wider text-faint", !compact && "md:grid")}>
        <span>time</span><span>order</span><span className="text-right">filled</span><span>type</span>
        <span className="text-right">trigger</span><span className="text-right">price</span><span className="text-right">status</span><span />
      </div>
      {shown.length === 0 ? <div className="p-4"><Empty>{all ? "No orders today." : "No open orders."}</Empty></div>
        : shown.map((o) => <OrderRow key={o.id} o={o} onCancel={onCancel} wide={!compact} />)}
    </Card>
  );
}

export function ConfirmCancel({ o, onYes, onNo }: { o: Order; onYes: () => void; onNo: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 p-4 sm:items-center" onClick={onNo}>
      <div className="w-full max-w-sm rounded-2xl border border-line-2 bg-panel p-5" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-lg font-semibold">Cancel {o.symbol} {o.kind}?</h3>
        <p className="mt-2 text-sm text-dim">{o.kind === "stop"
          ? "This is the stop protecting an open position. Without it nothing sells if price falls."
          : o.kind === "entry" ? "The entry and, if it filled, its stop are both cancelled." : "This order will be cancelled at the exchange."}</p>
        <div className="mt-5 flex gap-2"><Btn className="flex-1" onClick={onNo}>Keep</Btn><Btn variant="danger" className="flex-1" onClick={onYes}>Cancel order</Btn></div>
      </div>
    </div>
  );
}
