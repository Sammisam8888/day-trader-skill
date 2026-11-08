import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { State } from "../api";
import { clsx, n, signed, tone } from "../fmt";
import { Card, Empty } from "../ui";

export default function Journal({ s }: { s: State }) {
  const days = s.stats.history.map((d) => ({ ...d, net: n(d.net) || 0, label: d.day.slice(5) }));
  const total = days.reduce((a, d) => a + d.net, 0);
  const winDays = days.filter((d) => d.net > 0).length;
  const best = Math.max(0, ...days.map((d) => d.net)), worst = Math.min(0, ...days.map((d) => d.net));
  const metric = (k: string, v: React.ReactNode, cls = "") => (
    <div className="rounded-xl border border-line bg-panel-2/50 px-3 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-wider text-faint">{k}</div>
      <div className={clsx("num mt-0.5 font-semibold", cls)}>{v}</div>
    </div>
  );
  return (
    <Card title="Trade journal · last 30 sessions">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {metric("Net", signed(total), tone(total))}
        {metric("Green days", `${winDays}/${days.length}`)}
        {metric("Best day", signed(best), "text-up")}
        {metric("Worst day", signed(worst), "text-down")}
      </div>
      {days.length < 2 ? <div className="mt-3"><Empty>Each session's result is added here after the flatten.</Empty></div> : (
        <div className="mt-3 h-40">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={days} margin={{ top: 4, right: 4, left: -12, bottom: 0 }}>
              <XAxis dataKey="label" stroke="#5b6779" fontSize={10} tickLine={false} axisLine={false} />
              <YAxis stroke="#5b6779" fontSize={10} tickLine={false} axisLine={false} tickFormatter={(v) => `₹${v}`} />
              <Tooltip cursor={{ fill: "#ffffff08" }} contentStyle={{ background: "#0d1117", border: "1px solid #263041", borderRadius: 10, fontSize: 12 }}
                formatter={(v: number) => [signed(v), "net"]} />
              <Bar dataKey="net" radius={[4, 4, 0, 0]} isAnimationActive={false}>
                {days.map((d, i) => <Cell key={i} fill={d.net >= 0 ? "#34d399" : "#fb7185"} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  );
}
