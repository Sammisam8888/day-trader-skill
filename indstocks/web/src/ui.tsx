import { useEffect, useRef, useState, type ReactNode } from "react";
import { clsx } from "./fmt";

export function Card({ title, right, children, className, pad = true }:
  { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={clsx("rounded-2xl border border-line bg-panel/80 backdrop-blur-sm shadow-[0_1px_0_#ffffff08_inset]", className)}>
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-dim">{title}</h2>
          <div className="flex items-center gap-2">{right}</div>
        </header>
      )}
      <div className={pad ? "p-4" : ""}>{children}</div>
    </section>
  );
}

const TONES: Record<string, string> = {
  up: "bg-up-bg text-up ring-up/25",
  down: "bg-down-bg text-down ring-down/25",
  warn: "bg-warn-bg text-warn ring-warn/25",
  info: "bg-info-bg text-info ring-info/25",
  dim: "bg-panel-2 text-dim ring-line-2",
};

export function Badge({ tone = "dim", children, className }: { tone?: string; children: ReactNode; className?: string }) {
  return (
    <span className={clsx("inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-semibold ring-1 ring-inset whitespace-nowrap", TONES[tone] ?? TONES.dim, className)}>
      {children}
    </span>
  );
}

export function Dot({ tone = "up", live = false }: { tone?: string; live?: boolean }) {
  const c = tone === "up" ? "bg-up" : tone === "down" ? "bg-down" : tone === "warn" ? "bg-warn" : tone === "info" ? "bg-info" : "bg-faint";
  return (
    <span className="relative inline-flex h-2 w-2">
      {live && <span className={clsx("dot-ping absolute inline-flex h-full w-full rounded-full", c)} />}
      <span className={clsx("relative inline-flex h-2 w-2 rounded-full", c)} />
    </span>
  );
}

export function Btn({ children, onClick, variant = "ghost", className, disabled, type = "button" }:
  { children: ReactNode; onClick?: () => void; variant?: "ghost" | "primary" | "danger" | "solid";
    className?: string; disabled?: boolean; type?: "button" | "submit" }) {
  const v = {
    ghost: "border border-line-2 bg-panel-2 hover:bg-line text-fg",
    primary: "bg-info text-[#04101f] hover:brightness-110 font-semibold",
    danger: "border border-down/40 bg-down-bg text-down hover:bg-down/20",
    solid: "bg-up text-[#03140d] hover:brightness-110 font-semibold",
  }[variant];
  return (
    <button type={type} disabled={disabled} onClick={onClick}
      className={clsx("inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-sm transition disabled:opacity-40", v, className)}>
      {children}
    </button>
  );
}

/** A number that flashes green/red when it changes (tradecn's flash-cell). */
export function Flash({ value, children, className }: { value: number | null | undefined; children: ReactNode; className?: string }) {
  const prev = useRef(value);
  const [cls, setCls] = useState("");
  useEffect(() => {
    if (prev.current != null && value != null && value !== prev.current) {
      setCls(value > prev.current ? "flash-up" : "flash-down");
      const t = setTimeout(() => setCls(""), 900);
      prev.current = value;
      return () => clearTimeout(t);
    }
    prev.current = value;
  }, [value]);
  return <span className={clsx("num px-0.5", cls, className)}>{children}</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-xl border border-dashed border-line-2 px-4 py-8 text-center text-sm text-faint">{children}</div>;
}

export function Row({ k, v, className }: { k: ReactNode; v: ReactNode; className?: string }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-dashed border-line py-1.5 text-[13px] last:border-0">
      <span className="text-dim">{k}</span><span className={clsx("num font-medium", className)}>{v}</span>
    </div>
  );
}
