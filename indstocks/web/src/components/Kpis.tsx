import { ArrowDownRight, ArrowUpRight, Bot, Trophy, Wallet } from "lucide-react";
import type { State } from "../api";
import { clsx, money, money0, n, pct, signed, tone } from "../fmt";
import { Badge, Dot, Flash } from "../ui";

function Tile({ label, icon, children, foot, accent }:
  { label: string; icon: React.ReactNode; children: React.ReactNode; foot?: React.ReactNode; accent?: string }) {
  return (
    <div className={clsx("relative overflow-hidden rounded-2xl border border-line bg-panel/80 p-4", accent)}>
      <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-dim">
        <span className="text-faint">{icon}</span>{label}
      </div>
      <div className="mt-2 text-2xl font-semibold tracking-tight sm:text-[28px]">{children}</div>
      {foot && <div className="mt-1.5 text-xs text-dim">{foot}</div>}
    </div>
  );
}

export const STATUS = {
  live: { label: "LIVE", tone: "up", hint: "placing real orders" },
  shadow: { label: "SHADOW", tone: "warn", hint: "deciding and logging only" },
  halted: { label: "HALTED", tone: "down", hint: "no new entries" },
  login: { label: "LOGIN NEEDED", tone: "info", hint: "tap Log in to Shoonya" },
} as const;

export default function Kpis({ s }: { s: State }) {
  const net = n(s.pnl.net);
  const up = net >= 0;
  const held = s.positions.reduce((a, p) => a + Math.abs(n(p.current)), 0);
  const lev = s.cash ? held / n(s.cash) : 0;
  const live = s.positions.length > 0;
  const st = STATUS[s.status];
  const wr = s.stats.win_rate;
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Tile label="Capital" icon={<Wallet size={14} />}
        foot={<>held {money0(held)} · <span className={lev > n(s.limits.max_leverage || 1) ? "text-down" : ""}>{lev.toFixed(2)}x</span></>}>
        <span className="num">{s.cash == null ? "—" : money0(s.cash)}</span>
      </Tile>
      <Tile label="Today's net P&L" icon={up ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}
        accent={net > 0 ? "shadow-[inset_0_0_0_1px_#34d39926]" : net < 0 ? "shadow-[inset_0_0_0_1px_#fb718526]" : ""}
        foot={<span className="num">realised {signed(s.pnl.realised)} · open {signed(s.pnl.open)} · fees {money(s.pnl.charges)}</span>}>
        <span className="inline-flex items-center gap-2">
          <Flash value={net} className={tone(net)}>{signed(net)}</Flash>
          {live && <Dot tone={up ? "up" : "down"} live />}
        </span>
      </Tile>
      <Tile label="Win rate" icon={<Trophy size={14} />}
        foot={<span className="num">today {s.stats.today_wins}/{s.stats.today_trades} · all {s.stats.wins}/{s.stats.trades}</span>}>
        <span className="num">{wr == null ? "—" : pct(wr, 0).replace("+", "")}</span>
      </Tile>
      <Tile label="Agent" icon={<Bot size={14} />}
        foot={s.status === "halted" && s.halt_reason ? s.halt_reason : `${st.hint}${s.session ? ` · ${s.session.phase.replace(/_/g, " ")}` : ""}`}>
        <span className="inline-flex items-center gap-2.5">
          <Dot tone={st.tone} live={s.status === "live"} />
          <Badge tone={st.tone} className="px-2 py-1 text-sm">{st.label}</Badge>
        </span>
      </Tile>
    </div>
  );
}
