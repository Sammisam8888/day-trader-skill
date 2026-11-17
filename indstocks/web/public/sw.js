// Service worker: shows the trader's push messages and runs the
// notification buttons (sell now / exit all now) even when the page is closed.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (_) { d = { title: "trader", body: event.data && event.data.text() }; }
  if (d.type === "close") {
    event.waitUntil(self.registration.getNotifications({ tag: d.tag }).then((ns) => ns.forEach((n) => n.close())));
    return;
  }
  const opts = {
    body: d.body || "",
    tag: d.tag || undefined,
    renotify: !!d.renotify || (!d.silent && !!d.tag),
    silent: !!d.silent,
    requireInteraction: !!d.sticky,
    data: d,
    actions: d.actions || [],
    timestamp: Date.now(),
  };
  event.waitUntil(self.registration.showNotification(d.title || "trader", opts));
});

async function act(body) {
  const r = await fetch("/api/action", { method: "POST", headers: { "Content-Type": "application/json" },
                                         body: JSON.stringify(body) });
  return r.json();
}

self.addEventListener("notificationclick", (event) => {
  const d = event.notification.data || {};
  const run = async () => {
    if (event.action === "sell" || event.action === "exitall") {
      const body = event.action === "sell" ? { action: "exit_now", token: d.token } : { action: "exit_all_now" };
      let res;
      try { res = await act(body); } catch (e) { res = { ok: false, message: String(e) }; }
      await self.registration.showNotification(res.ok ? "Exit sent" : "Exit FAILED: open the app",
        { body: res.message || "", tag: "ack", renotify: true });
      return;
    }
    event.notification.close();
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    if (wins.length) return wins[0].focus();
    return self.clients.openWindow("/");
  };
  event.waitUntil(run());
});
