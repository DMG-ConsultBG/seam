/* Service worker.

   Three jobs:
   - receive Web Push and raise an OS notification even with the app closed;
   - raise a notification on demand while a tab is open;
   - make the app installable (the manifest is already served).

   Pushes carry NO payload by design. The server sends a bare tickle signed with
   VAPID; this worker then fetches the notification over the user's own session.
   Nothing about the notification - not the text, not the reference - ever
   passes through Google's or Mozilla's push service. */

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  e.waitUntil((async () => {
    let title = "Seam", body = "", url = "/";
    try {
      const r = await fetch("/api/notifications?limit=1", { credentials: "include" });
      if (r.ok) {
        const j = await r.json();
        const n = (j.items || [])[0];
        if (n) {
          // `text` is rendered by the server in the recipient's own language.
          // `body` is the raw internal description and is a last resort only.
          body = n.text || n.body || "";
          url = n.order_id ? "/#/o/" + n.order_id
              : n.workspace_id ? "/#/w/" + n.workspace_id : "/";
        }
      }
    } catch (_) { /* offline: still tell the user something arrived */ }
    await self.registration.showNotification(title, {
      body, icon: "/static/icon.svg", badge: "/static/icon.svg",
      tag: "seam-push", data: { url }, renotify: true,
    });
  })());
});

self.addEventListener("message", (e) => {
  const d = e.data || {};
  if (d.type !== "notify" || !d.title) return;
  self.registration.showNotification(d.title, {
    body: d.body || "",
    icon: "/static/icon.svg",
    badge: "/static/icon.svg",
    tag: d.tag || "seam",
    data: { url: d.url || "/" },
    renotify: true,
  });
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const target = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true })
    .then((list) => {
      for (const c of list) {
        if ("focus" in c) { c.navigate(target); return c.focus(); }
      }
      return self.clients.openWindow(target);
    }));
});
