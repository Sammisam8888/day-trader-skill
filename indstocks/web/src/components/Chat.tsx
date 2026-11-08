import { Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ChatMsg } from "../api";
import { clsx } from "../fmt";
import { Btn, Card } from "../ui";

const CHIPS = ["status", "why", "stop", "continue", "cancel all", "sell all", "scan every 5 minutes"];

export default function Chat({ msgs, mode, send }: { msgs: ChatMsg[]; mode: string; send: (t: string) => Promise<void> }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => end.current?.scrollIntoView({ block: "end" }), [msgs.length, busy]);
  const go = async (t: string) => {
    if (!t.trim() || busy) return;
    setBusy(true); setText("");
    try { await send(t.trim()); } finally { setBusy(false); }
  };
  return (
    <Card title="Chat with the agent" right={<span className="text-[11px] text-faint">{mode === "commands" ? "commands only" : "AI via server Claude"}</span>}>
      <div className="flex h-[58vh] flex-col">
        <div className="flex-1 space-y-2 overflow-y-auto pr-1">
          {msgs.filter((m) => m.role !== "system").map((m, i) => (
            <div key={i} className={clsx("max-w-[86%] whitespace-pre-wrap rounded-2xl px-3.5 py-2 text-sm leading-relaxed",
              m.role === "user" ? "ml-auto rounded-br-md bg-info/90 text-[#04101f]" : "rounded-bl-md border border-line bg-panel-2")}>
              {m.text}
              <div className={clsx("num mt-1 text-right text-[10px]", m.role === "user" ? "text-[#04101f]/60" : "text-faint")}>{m.ts.slice(11, 16)}</div>
            </div>
          ))}
          {busy && <div className="w-fit rounded-2xl rounded-bl-md border border-line bg-panel-2 px-3.5 py-2 text-sm text-dim">thinking…</div>}
          <div ref={end} />
        </div>
        <div className="no-scrollbar mt-3 flex gap-1.5 overflow-x-auto">
          {CHIPS.map((c) => <button key={c} onClick={() => go(c)} className="shrink-0 rounded-full border border-line-2 px-2.5 py-1 text-xs text-dim hover:text-fg">{c}</button>)}
        </div>
        <form className="mt-2 flex gap-2" onSubmit={(e) => { e.preventDefault(); go(text); }}>
          <input value={text} onChange={(e) => setText(e.target.value)} placeholder="buy 40 NIFTYBEES · why · max positions 2 …"
            className="min-w-0 flex-1 rounded-xl border border-line-2 bg-bg px-3 py-2.5 text-[15px] outline-none focus:border-info" />
          <Btn type="submit" variant="primary" disabled={busy}><Send size={16} /></Btn>
        </form>
      </div>
    </Card>
  );
}
