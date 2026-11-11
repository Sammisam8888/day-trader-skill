import { CalendarDays, Undo2, Wrench } from "lucide-react";
import { useEffect, useState } from "react";
import { act, getControl, type ControlState } from "../api";
import { clsx } from "../fmt";
import { Badge, Btn, Card } from "../ui";

const LABEL: Record<string, string> = {
  max_loss_per_trade: "Max loss / trade", daily_loss_limit: "Daily loss limit", max_trades: "Trades / day",
  max_concurrent: "Open at once", max_leverage: "Leverage",
};
const OUT: Record<string, string> = { applied: "up", partly: "warn", refused: "down", ignored: "dim" };

export default function Control() {
  const [c, setC] = useState<ControlState | null>(null);
  const [msg, setMsg] = useState("");
  const load = () => getControl().then(setC).catch(() => {});
  useEffect(() => { load(); const iv = setInterval(load, 20000); return () => clearInterval(iv); }, []);
  if (!c) return null;
  const today = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
  const run = async (action: string, body: Record<string, unknown> = {}) => {
    const r = await act(action, body); setMsg(r.message); load();
  };
  return (
    <div className="grid gap-4 lg:grid-cols-5">
      <Card className="lg:col-span-3" title={<span className="inline-flex items-center gap-2"><Wrench size={14} />Claude's control for today</span>}
        right={c.halt ? <Badge tone="down">HALTED by {c.halt.by}</Badge> : <Badge tone="up">trading allowed</Badge>}>
        {c.day_mode && (
          <div className={clsx("mb-3 flex flex-wrap items-center gap-2 rounded-lg border p-3 text-sm",
            c.day_mode.halted ? "border-down/30 bg-down-bg" : c.day_mode.flipped ? "border-warn/30 bg-warn-bg" : "border-line bg-panel-2/60")}>
            <Badge tone={c.day_mode.halted ? "down" : c.day_mode.flipped ? "warn" : "up"}>
              {c.day_mode.halted ? "DAY STOPPED" : c.day_mode.flipped ? `FLIPPED ${c.day_mode.flipped_at ?? ""}` : "NORMAL"}</Badge>
            <span className="text-dim">losing trades {c.day_mode.losers.normal}{c.day_mode.flipped ? ` · losing flips ${c.day_mode.losers.flip}/${c.day_mode.flip_halt_after}` : ` / ${c.day_mode.stop_after}`}</span>
            <span className="text-dim">· opposite side (paper mirror) <span className={Number(c.day_mode.mirror_pnl) >= 0 ? "text-up" : "text-down"}>{Number(c.day_mode.mirror_pnl) >= 0 ? "+" : ""}{Number(c.day_mode.mirror_pnl).toFixed(0)}</span></span>
            {c.day_mode.halted && <span className="w-full text-xs text-down">{c.day_mode.halted}</span>}
          </div>)}
        {c.halt && (
          <div className="mb-3 flex items-center justify-between gap-3 rounded-lg border border-down/30 bg-down-bg p-3 text-sm">
            <span>{c.halt.reason}</span><Btn variant="ghost" onClick={() => run("control_resume")}>Resume</Btn>
          </div>)}
        <table className="num w-full text-sm">
          <thead><tr className="text-left text-[11px] text-faint"><th className="py-1 font-normal">limit</th><th className="font-normal">yours</th><th className="font-normal">today</th><th className="font-normal">Claude's ceiling</th></tr></thead>
          <tbody>{Object.keys(c.base).map((k) => {
            const t = c.risk[k]; const up = k in c.raised;
            return (
              <tr key={k} className="border-t border-line">
                <td className="py-1.5 text-dim">{LABEL[k] ?? k}</td><td>{c.base[k]}</td>
                <td className={clsx(t && (up ? "text-warn" : "text-info"))}>{t ?? "—"}{up ? " ↑" : t ? " ↓" : ""}</td>
                <td className="text-faint">{k === "daily_loss_limit" ? "never raised" : c.ceilings[k] ?? "no raises"}</td>
              </tr>);
          })}
          {(c.risk.size || c.risk.sides) && <tr className="border-t border-line"><td className="py-1.5 text-dim">Size / sides</td><td>1 / both</td>
            <td className="text-info">{c.risk.size ?? "1"} / {(c.risk.sides ?? "BOTH").toLowerCase()}</td><td /></tr>}
          </tbody>
        </table>
        <div className="mt-4 text-[11px] font-semibold uppercase tracking-wider text-dim">Commands today</div>
        {c.history.length === 0 ? <p className="mt-1 text-sm text-faint">None. Claude issues these rarely: halts, risk changes, calendar fixes.</p> : (
          <ul className="mt-1 space-y-2">{c.history.map((h, i) => (
            <li key={i} className="flex items-start justify-between gap-3 border-t border-line pt-2 text-sm">
              <div>
                <span className="num text-faint">{h.ts.slice(11, 16)}</span> <Badge tone={OUT[h.outcome] ?? "dim"}>{h.outcome}</Badge>{" "}
                <b>{h.command}</b> <span className="text-faint">by {h.by}</span>
                <div className="text-dim">{h.message}</div>{h.reason && <div className="text-[12px] italic text-faint">“{h.reason}”</div>}
              </div>
              {(h.outcome === "applied" || h.outcome === "partly") && h.by !== "owner" && ["halt_today", "flatten_now", "set_risk_today", "calendar_add"].includes(h.command) && (
                <Btn variant="ghost" onClick={() => run("control_undo", { day: today, index: i })}><Undo2 size={14} /> Undo</Btn>)}
            </li>))}</ul>)}
        {msg && <p className="mt-2 text-xs text-info">{msg}</p>}
      </Card>
      <Card className="lg:col-span-2" title={<span className="inline-flex items-center gap-2"><CalendarDays size={14} />Next 10 days</span>}
        right={<span className="text-[11px] text-faint">{c.nse_synced ? `NSE list synced ${c.nse_synced.slice(5, 16).replace("T", " ")}` : "NSE list not synced yet"}</span>}>
        <ul className="space-y-1 text-sm">{c.upcoming.map((d) => (
          <li key={d.date} className="flex items-center justify-between border-t border-line py-1.5 first:border-0">
            <span className="num"><span className="inline-block w-9 text-faint">{d.dow}</span>{d.date.slice(5)}</span>
            <span className="flex flex-wrap justify-end gap-1">
              {d.special ? <Badge tone="info">special {d.special}</Badge> : d.trading ? <Badge tone="up">session</Badge> : <Badge tone="dim">closed</Badge>}
              {d.events.map((e, i) => <Badge key={i} tone="warn">{e}</Badge>)}
            </span>
          </li>))}</ul>
      </Card>
    </div>
  );
}
