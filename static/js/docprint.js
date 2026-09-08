/* The bar above a generated sheet.
 *
 * The download is a plain link, so it works with this file blocked or missing.
 * All this adds is the waiting state: the server renders the PDF on demand and
 * that takes a second or two, during which an untouched button looks broken.
 *
 * Fetching it here rather than letting the browser navigate also means a
 * failure can be reported in the page, in the document's own language, instead
 * of replacing the sheet with an error page. */
(function () {
  "use strict";

  var printBtn = document.getElementById("printBtn");
  if (printBtn) {
    printBtn.addEventListener("click", function () { window.print(); });
  }

  var dl = document.getElementById("pdfBtn");
  var msg = document.getElementById("pdfMsg");
  if (!dl || !dl.getAttribute("href")) return;

  // No blob support: leave the plain link alone, it still downloads.
  if (!window.fetch || !window.URL || !window.URL.createObjectURL) return;

  var idle = msg ? msg.textContent : "";
  var busy = false;

  function nameFrom(header, fallback) {
    if (!header) return fallback;
    var star = /filename\*=UTF-8''([^;]+)/i.exec(header);
    if (star) {
      try { return decodeURIComponent(star[1]); } catch (e) { /* fall through */ }
    }
    var plain = /filename="([^"]+)"/i.exec(header);
    return plain ? plain[1] : fallback;
  }

  function done(text, bad) {
    busy = false;
    dl.classList.remove("busy");
    if (!msg) return;
    msg.textContent = text;
    msg.classList.toggle("bad", !!bad);
  }

  dl.addEventListener("click", function (ev) {
    if (busy) { ev.preventDefault(); return; }
    ev.preventDefault();
    busy = true;
    dl.classList.add("busy");
    if (msg) {
      msg.classList.remove("bad");
      msg.textContent = msg.getAttribute("data-working") || "";
    }
    fetch(dl.getAttribute("href"), { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("http " + r.status);
        var name = nameFrom(r.headers.get("Content-Disposition"), "document.pdf");
        return r.blob().then(function (blob) { return { blob: blob, name: name }; });
      })
      .then(function (out) {
        var url = window.URL.createObjectURL(out.blob);
        var a = document.createElement("a");
        a.href = url;
        a.download = out.name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        // Revoking immediately can cancel the download in some browsers.
        setTimeout(function () { window.URL.revokeObjectURL(url); }, 30000);
        done(idle, false);
      })
      .catch(function () {
        done((msg && msg.getAttribute("data-failed")) || idle, true);
      });
  });
})();
