/* Signing page logic.
   Loaded as an external file because the document pages run under a strict
   Content-Security-Policy (script-src 'self') - an inline <script> here is
   blocked by the browser and never executes. Everything the page needs comes
   from data- attributes on #sign-cfg, so no server data is inlined as code. */
(function () {
  var cfgEl = document.getElementById("sign-cfg");
  if (!cfgEl) return;
  var CFG = cfgEl.dataset;
  var SID = CFG.sid, TOK = CFG.token;
  var T = JSON.parse(CFG.i18n || "{}");

  function el(id) { return document.getElementById(id); }

  /* ---- signature pad ---------------------------------------------------- */
  var c = el("pad"), drawn = false, drawing = false, x = c && c.getContext("2d");
  if (c) {
    var pos = function (e) {
      var r = c.getBoundingClientRect(), p = e.touches ? e.touches[0] : e;
      return [(p.clientX - r.left) * (c.width / r.width), (p.clientY - r.top) * (c.height / r.height)];
    };
    var start = function (e) { drawing = true; x.beginPath(); x.moveTo.apply(x, pos(e)); e.preventDefault(); };
    var move = function (e) {
      if (!drawing) return;
      var p = pos(e);
      x.lineTo(p[0], p[1]); x.lineWidth = 2; x.lineCap = "round"; x.strokeStyle = "#1a1f2b"; x.stroke();
      drawn = true; e.preventDefault();
    };
    var end = function () { drawing = false; };
    c.addEventListener("mousedown", start);
    c.addEventListener("mousemove", move);
    window.addEventListener("mouseup", end);
    c.addEventListener("touchstart", start);
    c.addEventListener("touchmove", move);
    c.addEventListener("touchend", end);
    el("clr").addEventListener("click", function () {
      x.clearRect(0, 0, c.width, c.height); drawn = false;
    });
  }

  /* ---- qualified signing ------------------------------------------------ */
  function post(path, body) {
    return fetch("/api/sign/" + TOK + "/qtsp" + path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (r) { return r.json().then(function (j) { return [r.ok, j]; }); });
  }
  function qmsg(text, cls) {
    var n = el("q_state");
    if (n) { n.textContent = text; n.className = "qstate" + (cls ? " " + cls : ""); }
  }
  function qok() { qmsg(T.q_done, "ok"); }
  function on(id, fn) { var b = el(id); if (b) b.addEventListener("click", fn); return b; }

  var qLoad = on("q_load", function () {
    qLoad.disabled = true; qmsg("");
    post("/credentials", { email: el("s_email").value }).then(function (a) {
      qLoad.disabled = false;
      if (!a[0]) return qmsg(a[1].error || "Error", "bad");
      var sel = el("q_cid");
      sel.innerHTML = "";
      (a[1].credentials || []).forEach(function (cr) {
        var o = document.createElement("option");
        o.value = cr.id;
        o.textContent = (cr.subject || cr.id) + (cr.valid_to ? " · " + cr.valid_to : "");
        sel.appendChild(o);
      });
      el("q_creds").className = "";
    }).catch(function () { qLoad.disabled = false; qmsg("Network error", "bad"); });
  });

  var qSend = on("q_send", function () {
    qSend.disabled = true;
    post("/otp", { credential_id: el("q_cid").value }).then(function (a) {
      qSend.disabled = false;
      qmsg(a[0] ? T.code_sent : (a[1].error || "Error"), a[0] ? "" : "bad");
    }).catch(function () { qSend.disabled = false; });
  });

  var qSign = on("q_sign", function () {
    qSign.disabled = true; qmsg("");
    post("/sign", {
      credential_id: el("q_cid").value, pin: el("q_pin").value, otp: el("q_otp").value,
    }).then(function (a) {
      qSign.disabled = false;
      if (a[0]) qok(); else qmsg(a[1].error || "Error", "bad");
    }).catch(function () { qSign.disabled = false; qmsg("Network error", "bad"); });
  });

  var qPoll = on("q_poll", function () {
    qPoll.disabled = true;
    post("/poll", {}).then(function (a) {
      qPoll.disabled = false;
      if (!a[0]) return qmsg(a[1].error || "Error", "bad");
      if (a[1].state === "signed") qok();
      else if (a[1].state === "rejected") qmsg(T.q_reject, "bad");
    }).catch(function () { qPoll.disabled = false; });
  });

  var qCopy = on("q_copy", function () {
    var h = el("q_hash").textContent;
    if (navigator.clipboard) navigator.clipboard.writeText(h);
    qCopy.textContent = T.q_copied;
  });

  var qFile = on("q_send_file", function () {
    var f = el("q_file").files[0];
    if (!f) return qmsg(T.q_upload, "bad");
    qFile.disabled = true;
    var rd = new FileReader();
    rd.onload = function () {
      post("/detached", { signature: rd.result, signed_hash: el("q_hash").textContent })
        .then(function (a) {
          qFile.disabled = false;
          if (a[0]) qok(); else qmsg(a[1].error || "Error", "bad");
        })
        .catch(function () { qFile.disabled = false; qmsg("Network error", "bad"); });
    };
    rd.readAsDataURL(f);
  });

  /* ---- Seam's own e-mail code (levels without a provider) ---------------- */
  var otpBtn = on("otp_send", function () {
    otpBtn.disabled = true;
    fetch("/api/signatures/" + SID + "/otp", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: TOK }),
    }).then(function (r) { return r.json(); }).then(function (j) {
      el("otp_note").textContent = j.code ? T.code_sent + "  " + j.code : T.code_sent;
      otpBtn.disabled = false;
    }).catch(function () { otpBtn.disabled = false; });
  });

  /* ---- submit ------------------------------------------------------------ */
  el("sf").addEventListener("submit", function (e) {
    e.preventDefault();
    var b = el("go"), err = el("err");
    err.textContent = ""; b.disabled = true;
    var payload = {
      token: TOK,
      name: el("s_name").value,
      role: el("s_role").value,
      email: el("s_email").value,
      consent: el("s_consent").checked,
      signature_img: drawn ? c.toDataURL("image/png") : "",
    };
    var code = el("s_code");
    if (code) payload.code = code.value;
    fetch("/api/signatures/" + SID + "/sign", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (r) { return r.json().then(function (j) { return [r.ok, j]; }); })
      .then(function (a) {
        if (a[0]) location.href = "/signature/" + SID;
        else { err.textContent = a[1].error || "Error"; b.disabled = false; }
      })
      .catch(function () { err.textContent = "Network error"; b.disabled = false; });
  });
})();
