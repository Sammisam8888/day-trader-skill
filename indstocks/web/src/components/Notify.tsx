import { Bell, BellOff } from "lucide-react";
import { useEffect, useState } from "react";
import { Btn } from "../ui";

// Phone notifications: a service worker + Web Push subscription. Browsers only
// allow this on HTTPS, so on the plain-http Tailscale address it points to the
// private https://<host>.ts.net address instead.
const HTTPS_HOST = "ip-172-26-2-71.tailb7a0b4.ts.net";

function b64ToBytes(s: string): Uint8Array {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function subscribe(): Promise<string> {
  const reg = await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;
  const { key } = await (await fetch("/api/push/key")).json();
  if (!key) throw new Error("server has no push key");
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
  const r = await (await fetch("/api/push/subscribe", { method: "POST", headers: { "Content-Type": "application/json" },
                                                         body: JSON.stringify(sub.toJSON()) })).json();
  if (!r.ok) throw new Error(r.message || "subscribe refused");
  return "on";
}

export default function Notify() {
  const secure = window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window;
  const [state, setState] = useState<string>(secure ? Notification.permission : "insecure");
  const [msg, setMsg] = useState("");

  const enable = async () => {
    try {
      const p = await Notification.requestPermission();
      setState(p);
      if (p !== "granted") return;
      await subscribe();
      setState("on");
      const t = await (await fetch("/api/action", { method: "POST", headers: { "Content-Type": "application/json" },
                                                     body: JSON.stringify({ action: "push_test" }) })).json();
      setMsg(t.message || "");
    } catch (e) { setMsg(String(e)); }
  };

  useEffect(() => {
    if (!secure) return;
    if (Notification.permission === "granted") subscribe().then(() => setState("on")).catch((e) => setMsg(String(e)));
    else if (Notification.permission === "default") enable();          // ask on first open
  }, []);

  if (state === "on") return null;
  if (state === "insecure") {
    return (
      <div className="mx-3 mt-2 rounded-lg border border-line bg-card px-3 py-2 text-sm">
        <BellOff size={14} className="mr-1 inline" /> Phone notifications need the private HTTPS address:{" "}
        <a className="text-acc underline" href={`https://${HTTPS_HOST}/`}>https://{HTTPS_HOST}</a>
      </div>
    );
  }
  return (
    <div className="mx-3 mt-2 flex items-center justify-between gap-2 rounded-lg border border-line bg-card px-3 py-2 text-sm">
      <span>{state === "denied" ? "Notifications are blocked: allow them in the browser's site settings." :
             "Get every trade, fill, exit and live P&L as a phone notification, with a Sell now button."}
             {msg && <span className="ml-1 text-faint">{msg}</span>}</span>
      {state !== "denied" && <Btn variant="primary" className="px-2.5 py-1.5" onClick={enable}><Bell size={14} />Enable</Btn>}
    </div>
  );
}
