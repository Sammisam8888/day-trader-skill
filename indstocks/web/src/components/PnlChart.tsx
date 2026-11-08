import { Area, AreaChart, CartesianGrid, Line, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { State } from "../api";
import { signed } from "../fmt";
import { Card, Empty } from "../ui";

const toMin = (t: string) => { const [h, m] = t.split(":").map(Number); return h * 60 + m; };
const fromMin = (m: number) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;

export default function PnlChart({ s }: { s: State }) {
  const open = s.session ? toMin(s.session.open) : 555;
  const close = s.session ? toMin(s.session.close) : 930;
  const data = s.series.map((p) => ({ ...p, m: toMin(p.t) }));
  const vals = data.map((d) => d.net);
  const max = Math.max(0, ...vals), min = Math.min(0, ...vals);
  const zero = max === min ? 0.5 : max / (max - min);              // gradient split at zero
  const last = data.at(-1)?.net ?? 0;
  const ticks: number[] = [];
  for (let m = open; m <= close; m += 60) ticks.push(m);
  return (
    <Card title="Intraday P&L" right={<span className={`num text-sm font-semibold ${last >= 0 ? "text-up" : "text-down"}`}>{signed(last)}</span>}>
      {data.length < 2 ? (
        <Empty>The curve fills in once the session starts and the fill monitor records a point every minute.</Empty>
      ) : (
        <div className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 6, right: 6, left: -8, bottom: 0 }}>
              <defs>
                <linearGradient id="pnlStroke" x1="0" y1="0" x2="0" y2="1">
                  <stop offset={zero} stopColor="#34d399" /><stop offset={zero} stopColor="#fb7185" />
                </linearGradient>
                <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset={0} stopColor="#34d399" stopOpacity={0.35} />
                  <stop offset={zero} stopColor="#34d399" stopOpacity={0.02} />
                  <stop offset={zero} stopColor="#fb7185" stopOpacity={0.02} />
                  <stop offset={1} stopColor="#fb7185" stopOpacity={0.35} />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#1c2430" vertical={false} />
              <XAxis dataKey="m" type="number" domain={[open, close]} ticks={ticks} tickFormatter={fromMin}
                stroke="#5b6779" fontSize={11} tickLine={false} axisLine={false} />
              <YAxis stroke="#5b6779" fontSize={11} tickLine={false} axisLine={false} width={56}
                tickFormatter={(v) => `₹${Math.round(v)}`} />
              <ReferenceLine y={0} stroke="#263041" />
              <Tooltip contentStyle={{ background: "#0d1117", border: "1px solid #263041", borderRadius: 10, fontSize: 12 }}
                labelFormatter={(m) => fromMin(Number(m))}
                formatter={(v: number, k: string) => [signed(v), k === "net" ? "net" : "realised"]} />
              <Area type="monotone" dataKey="net" stroke="url(#pnlStroke)" strokeWidth={2} fill="url(#pnlFill)" isAnimationActive={false} />
              <Line type="stepAfter" dataKey="realised" stroke="#8b98a9" strokeDasharray="4 4" dot={false} strokeWidth={1} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
      <div className="mt-2 flex gap-4 text-[11px] text-faint">
        <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-4 rounded bg-up" />net after charges</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-0 w-4 border-t border-dashed border-dim" />realised</span>
      </div>
    </Card>
  );
}
