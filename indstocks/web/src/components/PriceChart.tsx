import { createChart, CrosshairMode, LineStyle, type IChartApi, type ISeriesApi, type IPriceLine } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { getCandles, type Candles, type State } from "../api";
import { clsx, price } from "../fmt";
import { Card, Empty } from "../ui";

const LEVELS: { key: keyof Candles["levels"]; title: string; color: string; style: LineStyle }[] = [
  { key: "range_high", title: "range high", color: "#5b6779", style: LineStyle.Dotted },
  { key: "range_low", title: "range low", color: "#5b6779", style: LineStyle.Dotted },
  { key: "trigger", title: "trigger", color: "#fbbf24", style: LineStyle.Dashed },
  { key: "stop", title: "stop", color: "#fb7185", style: LineStyle.Solid },
  { key: "target", title: "target", color: "#34d399", style: LineStyle.Solid },
];

export default function PriceChart({ s }: { s: State }) {
  const symbols = s.symbols.map((x) => x.symbol);
  const [sym, setSym] = useState(symbols[0] ?? "NIFTYBEES");
  const [data, setData] = useState<Candles | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vol = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lines = useRef<IPriceLine[]>([]);

  useEffect(() => { if (!symbols.includes(sym) && symbols[0]) setSym(symbols[0]); }, [symbols.join(",")]);

  useEffect(() => {
    let alive = true;
    const load = () => getCandles(sym).then((d) => alive && setData(d)).catch(() => {});
    load();
    const t = setInterval(load, 60_000);
    return () => { alive = false; clearInterval(t); };
  }, [sym]);

  useEffect(() => {
    if (!box.current) return;
    const c = createChart(box.current, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: "#8b98a9", fontSize: 11 },
      grid: { vertLines: { color: "#141b25" }, horzLines: { color: "#141b25" } },
      rightPriceScale: { borderColor: "#1c2430" },
      timeScale: { borderColor: "#1c2430", timeVisible: true, secondsVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
    });
    series.current = c.addCandlestickSeries({
      upColor: "#34d399", downColor: "#fb7185", borderUpColor: "#34d399", borderDownColor: "#fb7185",
      wickUpColor: "#34d39999", wickDownColor: "#fb718599",
    });
    vol.current = c.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" } });
    vol.current.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    chart.current = c;
    return () => { c.remove(); chart.current = null; };
  }, []);

  useEffect(() => {
    if (!series.current || !vol.current || !data) return;
    series.current.setData(data.candles as never);
    vol.current.setData(data.candles.map((c) => ({ time: c.time, value: c.volume,
      color: c.close >= c.open ? "#34d39933" : "#fb718533" })) as never);
    lines.current.forEach((l) => series.current?.removePriceLine(l));
    lines.current = LEVELS.filter((l) => data.levels[l.key] != null).map((l) =>
      series.current!.createPriceLine({ price: data.levels[l.key]!, color: l.color, lineWidth: 1,
        lineStyle: l.style, axisLabelVisible: true, title: l.title }));
    // live stop from an open position overrides the planned one
    const pos = s.positions.find((p) => p.symbol === sym);
    if (pos?.stop_live && pos.stop != null && pos.stop !== data.levels.stop) {
      lines.current.push(series.current.createPriceLine({ price: pos.stop, color: "#fb7185", lineWidth: 2,
        lineStyle: LineStyle.Solid, axisLabelVisible: true, title: "stop (live)" }));
    }
    chart.current?.timeScale().fitContent();
  }, [data, s.positions.map((p) => `${p.symbol}:${p.stop}`).join(",")]);

  const ltp = s.symbols.find((x) => x.symbol === sym)?.ltp;
  return (
    <Card title="Price" pad={false} right={
      <div className="flex gap-1">
        {symbols.map((x) => (
          <button key={x} onClick={() => setSym(x)}
            className={clsx("rounded-md px-2 py-1 text-xs font-semibold", x === sym ? "bg-line-2 text-fg" : "text-dim hover:text-fg")}>{x}</button>
        ))}
      </div>}>
      <div className="flex items-baseline gap-3 px-4 pt-3">
        <span className="text-lg font-semibold">{sym}</span>
        <span className="num text-lg">{price(ltp)}</span>
        {data?.levels.trigger != null && <span className="text-xs text-faint">plan: trigger {price(data.levels.trigger)} · stop {price(data.levels.stop)} · target {price(data.levels.target)}</span>}
      </div>
      <div className="relative h-72 px-1 pb-2">
        <div ref={box} className="absolute inset-0" />
        {(!data || data.candles.length === 0) && (
          <div className="absolute inset-0 flex items-center justify-center p-6">
            <Empty>{data?.error === "not logged in" ? "Log in to Shoonya to load candles." : data?.error || "No candles yet for this session."}</Empty>
          </div>
        )}
      </div>
    </Card>
  );
}
