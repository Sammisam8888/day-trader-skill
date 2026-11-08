import type { LogEvent } from "../api";
import { clsx } from "../fmt";
import { Badge, Card, Empty } from "../ui";

const TONE: Record<string, string> = {
  placed: "info", would_place: "info", stop_placed: "up", stop_moved: "up", target_exit: "up", sold: "up",
  logged_in: "up", flatten_done: "up", day_summary: "info",
  blocked: "down", stop_failed: "down", order_failed: "down", sell_failed: "down", ALERT: "down",
  flatten_incomplete: "down", error: "down", halted: "down", login_required: "warn",
  skip: "dim", no_data: "dim", unsized: "warn", entry_cancelled: "warn", run_start: "dim", run_end: "dim",
};

function detail(r: LogEvent): string {
  const g = (k: string) => (r[k] == null ? "" : String(r[k]));
  switch (r.event) {
    case "blocked": return ((r.reasons as string[]) || []).join(" · ");
    case "placed": case "would_place":
      return `${g("qty")} @ trigger ${g("trigger")} → stop ${g("stop")} / target ${g("target")} · risk ₹${g("risk")} · net if target ₹${g("net_if_target")}`;
    case "stop_placed": return `stop ${g("trigger")} for ${g("qty")}`;
    case "stop_moved": return `${g("from")} → ${g("to")} (${g("reason")}) at ltp ${g("ltp")}`;
    case "target_exit": return `target ${g("target")} reached at ${g("ltp")}`;
    case "ALERT": return `${g("level")}: ${g("action")}`;
    case "run_start": return `cash ₹${g("cash")} · fills ${g("fills")} · resting ${g("resting")} · per order ₹${g("per_order")}`;
    case "day_summary": return `net ₹${g("net")} · ${g("wins")}/${g("trades")} wins`;
    default: {
      const skip = new Set(["ts", "event", "job", "symbol", "name", "live"]);
      return Object.entries(r).filter(([k]) => !skip.has(k)).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ").slice(0, 260);
    }
  }
}

export default function Activity({ items }: { items: LogEvent[] }) {
  return (
    <Card title={`Decisions & events · ${items.length}`} pad={false}>
      {items.length === 0 ? <div className="p-4"><Empty>Nothing logged today yet.</Empty></div> : (
        <ol className="relative ml-6 border-l border-line">
          {items.map((r, i) => {
            const t = TONE[r.event] ?? "dim";
            return (
              <li key={i} className="relative py-2.5 pl-4 pr-4">
                <span className={clsx("absolute -left-[5px] top-4 h-2.5 w-2.5 rounded-full ring-4 ring-panel",
                  t === "up" ? "bg-up" : t === "down" ? "bg-down" : t === "warn" ? "bg-warn" : t === "info" ? "bg-info" : "bg-faint")} />
                <div className="flex flex-wrap items-center gap-2">
                  <span className="num text-xs text-faint">{r.ts.slice(11, 19)}</span>
                  <Badge tone={t}>{r.event.replace(/_/g, " ")}</Badge>
                  {r.symbol ? <span className="text-sm font-semibold">{String(r.symbol)}</span> : null}
                  <span className="text-[11px] text-faint">{r.job}</span>
                </div>
                <p className="mt-1 break-words text-[13px] text-dim">{detail(r)}</p>
              </li>
            );
          })}
        </ol>
      )}
    </Card>
  );
}
