/* ===========================================================================
   Seam - SPA front-end
   Hash-routed, dependency-free. i18n via i18n.js (t / tpl* helpers).
   ========================================================================== */
"use strict";

const State = {
  user: null,
  org: null,
  templates: {},
  notif: { items: [], unread: 0 },
  _notifTimer: null,
  _feedTimer: null,
};

const SEAM_MARK = `<svg class="seam-mark" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
  <rect x="1" y="1" width="30" height="30" rx="8" fill="url(#smg)"/>
  <defs><linearGradient id="smg" x1="0" y1="0" x2="32" y2="32"><stop stop-color="#3f7bff"/><stop offset="1" stop-color="#2552c8"/></linearGradient></defs>
  <path class="seam-stitch" d="M16 4.5 V27.5" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-dasharray="3.2 3.4"/>
  <path d="M9.5 9.5 H6.5 a2 2 0 0 0-2 2 V20.5 a2 2 0 0 0 2 2 H9.5" stroke="#cfe0ff" stroke-width="2" fill="none" stroke-linecap="round"/>
  <path d="M22.5 9.5 H25.5 a2 2 0 0 1 2 2 V20.5 a2 2 0 0 1-2 2 H22.5" stroke="#cfe0ff" stroke-width="2" fill="none" stroke-linecap="round"/>
</svg>`;

/* ---- tiny DOM helpers --------------------------------------------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function initials(name) {
  return (name || "?").trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
}
function colorFor(str) {
  const palette = ["#2f6df0", "#7a4fd0", "#1f9d6b", "#c9851a", "#d24b4b", "#0e8aa3", "#b8782a"];
  let h = 0; for (const c of (str || "")) h = (h * 31 + c.charCodeAt(0)) % 9973;
  return palette[h % palette.length];
}
function parseUTC(s) {
  if (!s) return null;
  const m = String(s).match(/(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/);
  if (!m) return new Date(s);
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
}
function relTime(s) {
  const d = parseUTC(s); if (!d) return "";
  const sec = Math.round((Date.now() - d.getTime()) / 1000);
  if (sec < 45) return t("just_now");
  if (sec < 90) return t("minute_ago");
  const min = Math.round(sec / 60);
  if (min < 60) return t("minutes_ago", { n: min });
  const hr = Math.round(min / 60);
  if (hr < 2) return t("hour_ago");
  if (hr < 24) return t("hours_ago", { n: hr });
  const day = Math.round(hr / 24);
  if (day === 1) return t("yesterday");
  if (day < 30) return t("days_ago", { n: day });
  return d.toLocaleDateString(LANG, { day: "numeric", month: "short", year: "numeric" });
}

/* ---- toast / modal / overlay ------------------------------------------- */
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .25s"; setTimeout(() => el.remove(), 260); }, 3200);
}
function errMsg(err) {
  if (err && err.code) {
    const info = err.info || {};
    let vars = info;
    if (err.code === "field_required") vars = { field: info.tpl ? tplField(info.tpl, info.key) : (info.field || "") };
    const key = "err_" + err.code;
    const msg = t(key, vars);
    if (msg !== key) return msg;
  }
  return (err && err.message) || t("err_error");
}
function closeModal() { $("#modal-root").innerHTML = ""; }
/* `opts.locked` removes the ways out - no ✕, no click on the backdrop, no
   Escape. Used by exactly one dialogue: the question asked on a first sign-in,
   which has an answer for every case ("all of them" included) and so cannot be
   dismissed into a half-configured state. */
function openModal(title, bodyHTML, footHTML, opts) {
  const locked = !!(opts && opts.locked);
  $("#modal-root").innerHTML = `
    <div class="modal-bg" data-bg${locked ? " data-locked" : ""}>
      <div class="modal" role="dialog">
        <div class="m-head"><h3>${esc(title)}</h3>${
          locked ? "" : `<button class="iconbtn" data-close>✕</button>`}</div>
        <div class="m-body">${bodyHTML}</div>
        ${footHTML ? `<div class="m-foot">${footHTML}</div>` : ""}
      </div>
    </div>`;
  if (!locked) {
    $("#modal-root [data-bg]").addEventListener("mousedown", (e) => { if (e.target.dataset.bg !== undefined) closeModal(); });
    $$("#modal-root [data-close]").forEach((b) => b.addEventListener("click", closeModal));
  }
  return $("#modal-root .modal");
}
function modalLocked() { return !!$("#modal-root [data-locked]"); }
function closeOverlay() { $("#overlay-root").innerHTML = ""; }
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closeOverlay();
  if (!modalLocked()) closeModal();
});
document.addEventListener("click", (e) => {
  const ov = $("#overlay-root");
  if (ov.firstChild && !ov.contains(e.target) && !e.target.closest("[data-overlay-toggle]")) closeOverlay();
});

/* ---- language menu ------------------------------------------------------ */
function currentLang() { return LANGS.find((l) => l.code === LANG) || LANGS[0]; }
function langLabel(code) { const l = LANGS.find((x) => x.code === code); return l ? l.name : code; }
function openLangMenu(e) {
  e.stopPropagation();
  if ($("#overlay-root").querySelector(".lang-menu")) { closeOverlay(); return; }
  $("#overlay-root").innerHTML = `<div class="menu lang-menu">
    <div class="mhead"><div class="nm">${esc(t("language"))}</div></div>
    ${LANGS.map((l) => `<div class="mi ${l.code === LANG ? "sel" : ""}" data-lang="${l.code}"><span class="flag">${l.flag}</span> ${esc(l.name)} ${l.code === LANG ? "✓" : ""}</div>`).join("")}
  </div>`;
  $$("#overlay-root [data-lang]").forEach((m) => m.addEventListener("click", () => { closeOverlay(); setLang(m.dataset.lang); }));
}

/* ---- currency menu ------------------------------------------------------ */
function openCcyMenu(e) {
  e.stopPropagation();
  if ($("#overlay-root").querySelector(".ccy-menu")) { closeOverlay(); return; }
  $("#overlay-root").innerHTML = `<div class="menu ccy-menu">
    <div class="mhead"><div class="nm">${esc(t("currency"))}</div></div>
    ${CURRENCIES.map((c) => `<div class="mi ${c.code === CCY ? "sel" : ""}" data-ccy="${c.code}"><span class="ccy-sym">${esc(c.sym)}</span> ${esc(c.code)} ${c.code === CCY ? "✓" : ""}</div>`).join("")}
  </div>`;
  $$("#overlay-root [data-ccy]").forEach((m) => m.addEventListener("click", () => { closeOverlay(); setCcy(m.dataset.ccy); }));
}

/* ---- theme -------------------------------------------------------------- */
function getTheme() { return document.documentElement.getAttribute("data-theme") || "light"; }
function setTheme(th) {
  document.documentElement.setAttribute("data-theme", th);
  try { localStorage.setItem("seam_theme", th); } catch {}
  const icon = th === "dark" ? "☀" : "☾";
  ["#themeBtn", "#themeBtnAuth"].forEach((s) => { const b = $(s); if (b) b.textContent = icon; });
}
function toggleTheme() { setTheme(getTheme() === "dark" ? "light" : "dark"); }

/* ---- routing ------------------------------------------------------------ */
function go(path) { location.hash = path; }
function currentRoute() { return location.hash.replace(/^#/, "") || "/"; }
window.addEventListener("hashchange", render);

/* =========================================================================
   AUTH
   ======================================================================== */
function authTopControls() {
  return `<div class="auth-top">
    <button class="lang-fab icon-only" id="themeBtnAuth" title="${esc(t("theme_" + (getTheme() === "dark" ? "light" : "dark")))}">${getTheme() === "dark" ? "☀" : "☾"}</button>
    <button class="lang-fab" id="langBtnAuth" data-overlay-toggle title="${esc(t("language"))}">
      <span class="flag">${currentLang().flag}</span> ${esc(currentLang().name)} <span class="caret">▾</span></button>
  </div>`;
}
function wireAuthControls() {
  const tb = $("#themeBtnAuth"); if (tb) tb.addEventListener("click", toggleTheme);
  const lb = $("#langBtnAuth"); if (lb) lb.addEventListener("click", openLangMenu);
}
/* The join, drawn. Two materials meet down the middle of the sign-in screen
   and a sewn line holds them together - which is the product, and the name.
   It sews itself once on load and then stays still; a stitch that keeps
   stitching is a fidget, not an idea. */
const SEAM_STITCH = `<div class="auth-seam" aria-hidden="true"></div>`;

/* This panel describes the product to somebody who has not used it. It is not
   a poster: a heading that names the thing, what it is and what it keeps, then
   the four areas of function with a factual line each. */
function authAside() {
  const area = (n) => `<div class="pt"><b>${esc(t("pt" + n + "_t"))}</b><span>${esc(t("pt" + n + "_s"))}</span></div>`;
  return `<div class="auth-aside">
    <div class="aside-content">
      ${SEAM_MARK}
      <div class="eyebrow">${esc(t("hero_kicker"))}</div>
      <h2>${esc(t("hero_title"))}</h2>
      <p>${esc(t("hero_p"))}</p>
      <div class="pts">${[1, 2, 3, 4].map(area).join("")}</div>
      <!-- Real paths, not hash routes: these are server-rendered so a crawler
           and a reader with JavaScript off both get the words. -->
      <div class="aside-links">
        <a href="/terms">${esc(t("nav_terms"))}</a>
        <a href="/privacy-policy">${esc(t("nav_privacy"))}</a>
        <a href="/faq">${esc(t("nav_faq"))}</a>
      </div>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------ *
 *  Taxes and profit
 *
 *  Revenue comes from the invoices this company issued; the costs that
 *  decide whether there is a profit are entered here, because the
 *  platform cannot know a rent or a wage bill. The rates are the
 *  published ones for the country until somebody confirms their own,
 *  and the screen says which of the two it is showing.
 * ------------------------------------------------------------------ */
const FIN = { year: new Date().getFullYear(), quarter: "", month: "", basis: "accrual" };

/* One line of the calculation. `money` is fmtMoney bound to the workspace
   currency, which is what the rest of this file uses. */
function finMoney(v) { return fmtMoney(Number(v || 0), CCY, LANG); }

/* A filing deadline, said in the reader's language. The form keeps its own
   name; the deadline is prose. Assembling it from a number and a fragment does
   not work in half of these languages, so each phrasing is a whole sentence. */
function finDue(due) {
  if (!due) return "";
  if (due.rule) return t("fin_due_" + due.rule, { n: due.day });
  if (due.months_after) return t("fin_due_months_after", { n: due.months_after });
  if (due.month) {
    // The whole date, not the month alone: the formatter then declines the
    // month correctly, which Polish, Ukrainian and Russian require after a
    // day number and which gluing a number to a month name does not do.
    const when = new Date(2026, due.month - 1, due.day)
      .toLocaleDateString(LANG, { day: "numeric", month: "long" });
    return t("fin_due_date", { date: when });
  }
  return "";
}

/* A filing line: what it is called, and when it is usually due. The name is
   never translated - it is the name printed on the form. */
function finRow2(label, name, due) {
  return `<div class="fin-row"><span>${esc(label)}</span>
    <b>${esc(name)}${due ? ` <span class="dim">· ${esc(due)}</span>` : ""}</b></div>`;
}

function finRow(label, value, opts) {
  const o = opts || {};
  return `<div class="fin-row ${o.cls || ""}">
    <span>${esc(label)}</span><b>${esc(finMoney(value))}</b></div>`;
}

async function viewFinance() {
  shell(crumb([{ label: t("home"), href: "#/" }, { label: t("fin_title") }]),
    `<div class="page"><div class="loading-full"><div class="spin"></div></div></div>`);
  // The calculation is behind the gate; the filing calendar and the links to
  // the tax authority are not. Charging somebody to be told the address of
  // their own revenue agency would be absurd, so the two are fetched apart and
  // a locked calculation leaves the rest of the screen standing.
  let r = null, ex = { items: [] }, cfg = null, locked = null;
  try {
    [ex, cfg] = await Promise.all([API.expenses(), API.financeSettings()]);
  } catch (err) { toast(errMsg(err), "err"); return; }
  try {
    const q = `year=${FIN.year}${FIN.quarter ? "&quarter=" + FIN.quarter : ""}` +
              `${FIN.month ? "&month=" + FIN.month : ""}&basis=${FIN.basis}`;
    r = await API.finance(q);
  } catch (err) {
    if (err && err.code === "feature_locked") locked = err;
    else { toast(errMsg(err), "err"); return; }
  }

  const c = (r && r.costs) || {};
  const warn = (locked || r.rates_confirmed) ? "" : `<div class="card pad fin-warn mb12">
    <span>${esc(t("fin_unconfirmed", { country: r.country, when: r.rates_reviewed }))}</span>
    <button class="btn sm" id="finConfirm">${esc(t("fin_confirm"))}</button></div>`;

  const years = [];
  for (let y = new Date().getFullYear(); y >= new Date().getFullYear() - 4; y--) years.push(y);

  shell(crumb([{ label: t("home"), href: "#/" }, { label: t("fin_title") }]), `<div class="page">
    <h1>${esc(t("fin_title"))}</h1>
    <p class="lead">${esc(t("fin_sub"))}</p>
    ${warn}

    <div class="card pad mb12">
      <div class="flex gap8 wrap center">
        <label class="field inline"><span>${esc(t("fin_year"))}</span>
          <select class="select" id="finYear">${years.map((y) =>
            `<option ${y === FIN.year ? "selected" : ""}>${y}</option>`).join("")}</select></label>
        <label class="field inline"><span>${esc(t("fin_quarter"))}</span>
          <select class="select" id="finQ"><option value="">—</option>${[1, 2, 3, 4].map((q) =>
            `<option value="${q}" ${String(FIN.quarter) === String(q) ? "selected" : ""}>Q${q}</option>`).join("")}</select></label>
        <label class="field inline"><span>${esc(t("fin_basis"))}</span>
          <select class="select" id="finBasis">
            <option value="accrual" ${FIN.basis === "accrual" ? "selected" : ""}>${esc(t("fin_basis_accrual"))}</option>
            <option value="cash" ${FIN.basis === "cash" ? "selected" : ""}>${esc(t("fin_basis_cash"))}</option>
          </select></label>
        <div class="spacer"></div>
        <a class="btn ghost sm" href="/api/finance/export?year=${FIN.year}${FIN.quarter ? "&quarter=" + FIN.quarter : ""}&basis=${FIN.basis}">${esc(t("fin_export"))}</a>
      </div>
    </div>

    <div class="fin-grid">
      ${locked ? `<div class="card pad fin-locked">
        <b>${esc(t("fin_title"))}</b>
        <p class="dim mt8">${esc(t("err_feature_locked"))}</p>
        <a class="btn primary mt8" href="#/plans">${esc(t("ent_buy"))}</a>
      </div>` : `
      <div class="card pad">
        <b>${esc(t("fin_revenue"))}</b>
        <div class="fin-big">${esc(finMoney(r.revenue))}</div>
        <p class="dim" style="font-size:12px">${esc(t("fin_from_platform", { n: r.sources.invoices }))}</p>
        <div class="fin-sep"></div>
        <b>${esc(t("fin_costs"))}</b>
        ${finRow(t("fin_operating"), c.operating)}
        ${finRow(t("fin_lease"), c.lease)}
        ${finRow(t("fin_salary"), c.salary)}
        ${finRow(t("fin_employer") + " (" + r.payroll_employer_rate + "%)", c.employer_contributions)}
        ${finRow(t("fin_other"), c.other)}
        ${finRow(t("fin_total"), c.total, { cls: "total" })}
        <div class="fin-sep"></div>
        ${finRow(t("fin_before"), r.profit_before_tax)}
        ${finRow(t("fin_tax") + " (" + r.profit_tax_rate + "%)", r.profit_tax)}
        <div class="fin-row net"><span>${esc(t("fin_net"))}</span><b>${esc(finMoney(r.net_profit))}</b></div>
      </div>`}

      <div class="card pad">
        ${locked ? "" : `<b>${esc(t("fin_vat_due"))}</b>
        <div class="fin-big">${r.vat.exempt ? esc(t("fin_vat_exempt")) : esc(finMoney(r.vat.due))}</div>
        ${r.vat.exempt ? `<p class="dim" style="font-size:12px">${esc(t("fin_r_" + (r.vat_exempt_reason || "other")))}</p>`
          : `${finRow(t("fin_vat_charged"), r.vat.charged)}${finRow(t("fin_vat_paid"), r.vat.paid)}`}
        <div class="fin-sep"></div>`}
        <b>${esc(t("fin_settings"))}</b>
        <label class="field"><span>${esc(t("fin_rate_profit"))}</span>
          <input class="input" id="finProfit" type="number" step="0.01" min="0" max="100"
            placeholder="${esc(t("fin_default", { n: cfg.defaults.profit_tax }))}"
            value="${cfg.profit_rate === null ? "" : cfg.profit_rate}" /></label>
        <label class="field"><span>${esc(t("fin_rate_payroll"))}</span>
          <input class="input" id="finPayroll" type="number" step="0.01" min="0" max="100"
            placeholder="${esc(t("fin_default", { n: cfg.defaults.payroll_employer }))}"
            value="${cfg.payroll_rate === null ? "" : cfg.payroll_rate}" /></label>
        <label class="check"><input type="checkbox" id="finVatEx" ${cfg.vat_exempt ? "checked" : ""} />
          <span>${esc(t("fin_vat_exempt"))}</span></label>
        <label class="field"><span>${esc(t("fin_exempt_reason"))}</span>
          <select class="select" id="finReason">${["", ...cfg.reasons].map((k) =>
            `<option value="${k}" ${k === cfg.vat_exempt_reason ? "selected" : ""}>${k ? esc(t("fin_r_" + k)) : "—"}</option>`).join("")}</select></label>
        <label class="check"><input type="checkbox" id="finProfitEx" ${cfg.profit_tax_exempt ? "checked" : ""} />
          <span>${esc(t("fin_exempt_profit"))}</span></label>
        <button class="btn primary block mt8" id="finSave">${esc(t("save"))}</button>
        <p class="dim" style="font-size:11.5px;margin-top:10px">${esc(t("fin_not_advice"))}</p>
      </div>

      <div class="card pad fin-filing">
        <b>${esc(t("fin_filing"))}</b>
        <p class="dim" style="font-size:12px">${esc(t("fin_filing_hint"))}</p>
        ${cfg.filing.vat_form ? `
          ${finRow2(t("fin_f_vat"), cfg.filing.vat_form, finDue(cfg.filing.vat_due))}
          ${finRow2(t("fin_f_profit"), cfg.filing.profit_form, finDue(cfg.filing.profit_due))}
          ${cfg.filing.eu_sales ? finRow2(t("fin_f_eu"), cfg.filing.eu_sales, "") : ""}
          ${cfg.filing.digital.length ? `<div class="fin-row"><span>${esc(t("fin_f_digital"))}</span>
            <b>${cfg.filing.digital.map(esc).join(" · ")}</b></div>` : ""}`
          : `<p class="dim mt8">${esc(t("fin_f_none"))}</p>`}
        ${(cfg.portals || []).length ? `
          <div class="fin-sep"></div>
          <b>${esc(t("fin_portals"))}</b>
          <p class="dim" style="font-size:12px">${esc(t("fin_portals_hint"))}</p>
          <div class="portal-links">
            ${cfg.portals.map((p) => `<a class="portal" href="${esc(p.url)}"
                target="_blank" rel="noopener noreferrer">
              <span class="pk">${esc(t("fin_p_" + p.category.replace(/\d$/, "")))}</span>
              <b>${esc(p.name)}</b>
              <span class="go" aria-hidden="true">↗</span></a>`).join("")}
          </div>` : ""}
      </div>
    </div>

    <div class="card pad mt12">
      <div class="flex between center"><b>${esc(t("fin_expenses"))}</b>
        <button class="btn sm" id="expAdd">${esc(t("fin_exp_add"))}</button></div>
      <p class="dim" style="font-size:12px">${esc(t("fin_exp_hint"))}</p>
      ${(ex.items || []).length ? `<div class="an-rows mt8">${ex.items.map((e) => `
        <div class="an-row">
          <div><b>${esc(e.label)}</b>
            <div class="dim" style="font-size:12px">${esc(t("fin_" + e.kind))} ·
              ${e.recurring ? esc(t("fin_exp_monthly")) : esc(t("fin_exp_once"))}</div></div>
          <div class="flex gap8 center"><b>${esc(finMoney(e.amount))}</b>
            <button class="iconbtn sm" data-del-exp="${e.id}" title="${esc(t("delete"))}">✕</button></div>
        </div>`).join("")}</div>`
        : `<p class="dim mt8">${esc(t("fin_exp_none"))}</p>`}
    </div>
  </div>`);

  const reload = () => viewFinance();
  $("#finYear").addEventListener("change", (e) => { FIN.year = Number(e.target.value); reload(); });
  $("#finQ").addEventListener("change", (e) => { FIN.quarter = e.target.value; reload(); });
  $("#finBasis").addEventListener("change", (e) => { FIN.basis = e.target.value; reload(); });
  const save = async () => {
    try {
      await API.saveFinanceSettings({
        profit_rate: $("#finProfit").value, payroll_rate: $("#finPayroll").value,
        vat_exempt: $("#finVatEx").checked, vat_exempt_reason: $("#finReason").value,
        profit_tax_exempt: $("#finProfitEx").checked,
      });
      toast(t("saved"), "ok"); reload();
    } catch (err) { toast(errMsg(err), "err"); }
  };
  $("#finSave").addEventListener("click", save);
  if ($("#finConfirm")) $("#finConfirm").addEventListener("click", save);
  $("#expAdd").addEventListener("click", () => openExpense(reload));
  $$("[data-del-exp]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.deleteExpense(b.dataset.delExp); reload(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
}

function openExpense(done) {
  openModal(t("fin_exp_add"), `
    <label class="field"><span>${esc(t("fin_exp_label"))}</span>
      <input class="input" id="exLabel" /></label>
    <div class="flex gap8">
      <label class="field" style="flex:1"><span>${esc(t("fin_exp_amount"))}</span>
        <input class="input" id="exAmount" type="number" step="0.01" min="0" /></label>
      <label class="field" style="flex:1"><span>${esc(t("fin_exp_vat"))}</span>
        <input class="input" id="exVat" type="number" step="0.01" min="0" value="0" /></label>
    </div>
    <label class="field"><span>${esc(t("fin_costs"))}</span>
      <select class="select" id="exKind">${["operating", "lease", "salary", "other"].map((k) =>
        `<option value="${k}">${esc(t("fin_" + k))}</option>`).join("")}</select></label>
    <label class="check"><input type="checkbox" id="exRec" checked />
      <span>${esc(t("fin_exp_monthly"))}</span></label>
    <label class="field"><span>${esc(t("date"))}</span>
      <input class="input" id="exDate" type="date" /></label>`,
    `<button class="btn primary" id="exSave">${esc(t("save"))}</button>`);
  $("#exSave").addEventListener("click", async () => {
    try {
      const rec = $("#exRec").checked;
      await API.addExpense({
        label: $("#exLabel").value, kind: $("#exKind").value,
        amount: $("#exAmount").value, vat_amount: $("#exVat").value,
        recurring: rec,
        on_date: rec ? null : $("#exDate").value,
        starts_on: rec ? ($("#exDate").value || null) : null,
      });
      closeModal(); if (done) done();
    } catch (err) { toast(errMsg(err), "err"); }
  });
}

function viewLogin() {
  $("#app").innerHTML = `<div class="auth-wrap">${authTopControls()}${SEAM_STITCH}${authAside()}
    <div class="auth-main"><div class="auth-card">
      <h1>${esc(t("login_title"))}</h1>
      <p class="lead">${esc(t("login_lead"))}</p>
      <form id="loginForm">
        <label class="field"><span>${esc(t("email"))}</span><input class="input" type="email" name="email" required autocomplete="email" /></label>
        <label class="field"><span>${esc(t("password"))}</span><input class="input" type="password" name="password" required autocomplete="current-password" /></label>
        <button class="btn primary block" type="submit">${esc(t("login_btn"))}</button>
      </form>
      <div id="eidBox"></div>
      <div class="auth-switch"><a href="#" id="forgotLink">${esc(t("forgot_link"))}</a></div>
      <div class="auth-switch">${esc(t("no_account"))} <a href="#/register">${esc(t("register_link"))}</a></div>
    </div></div></div>`;
  wireAuthControls();
  // Drawn only after the server says which schemes this instance can actually
  // complete: a sign-in button that leads nowhere is worse than none.
  API.eidProviders().then((r) => {
    const box = $("#eidBox");
    if (!box || !r.providers.length) return;
    box.innerHTML = `<div class="auth-or">${esc(t("eid_or"))}</div>` +
      r.providers.map((p) => `<button class="btn block mt8 eid-btn" data-eidgo="${esc(p.key)}">${esc(p.name)}</button>`).join("");
    $$("[data-eidgo]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      try { const s = await API.eidStart(b.dataset.eidgo); location.href = s.url; }
      catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
    }));
  }).catch(() => {});
  $("#loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#loginForm button"); btn.disabled = true;
    const fd = new FormData(e.target);
    try {
      const r = await API.login({ email: fd.get("email"), password: fd.get("password") });
      if (r && r.totp_required) { btn.disabled = false; openTotpChallenge(); return; }
      await boot(); go("/");
    } catch (err) { toast(errMsg(err), "err"); btn.disabled = false; }
  });
  const fl = $("#forgotLink");
  if (fl) fl.addEventListener("click", (e) => { e.preventDefault(); openForgotModal(); });
}

/* The second half of signing in. The password alone got us nowhere: the server
   is holding a five-minute pending state and nothing else. */
function openTotpChallenge() {
  const m = openModal(t("sec_2fa"), `
    <p class="muted" style="font-size:13px">${esc(t("sec_challenge"))}</p>
    <label class="field"><span>${esc(t("sec_code"))}</span>
      <input class="input" id="tcCode" inputmode="numeric" maxlength="6"
             autocomplete="one-time-code" style="max-width:180px" /></label>
    <details><summary style="cursor:pointer;font-size:12.5px;color:var(--text-2)">${esc(t("sec_lost"))}</summary>
      <label class="field mt8"><span>${esc(t("sec_recovery"))}</span>
        <input class="input" id="tcRec" autocomplete="off" style="max-width:260px" /></label>
    </details>`,
    `<button class="btn primary" id="tcGo">${esc(t("login_btn"))}</button>`);
  const go2 = async () => {
    const b = $("#tcGo"); b.disabled = true;
    try {
      await API.twoFactorVerify(m.querySelector("#tcCode").value.trim(),
                                m.querySelector("#tcRec").value.trim());
      closeModal(); await boot(); go("/");
    } catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
  };
  $("#tcGo").addEventListener("click", go2);
  m.querySelector("#tcCode").addEventListener("keydown", (e) => { if (e.key === "Enter") go2(); });
  m.querySelector("#tcCode").focus();
}

/* ---- password reset ------------------------------------------------------ */
function openForgotModal() {
  openModal(t("forgot_title"), `
    <p class="muted">${esc(t("forgot_hint"))}</p>
    <label class="field"><span>${esc(t("email"))}</span><input class="input" id="fgEmail" type="email" /></label>
    <div id="fgOut"></div>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="fgSend">${esc(t("forgot_send"))}</button>`);
  $("#fgSend").addEventListener("click", async () => {
    const b = $("#fgSend"); b.disabled = true;
    try {
      const r = await API.forgotPassword($("#fgEmail").value.trim());
      // "Sent" when nothing was sent is the cruellest possible answer: the
      // person waits for a letter that is never coming. If this installation
      // has no mail configured, say so and point at the one person who can
      // actually help.
      $("#fgOut").innerHTML = r.no_mail
        ? `<p class="muted" style="margin-top:10px">⚠ ${esc(t("forgot_mail_off"))}</p>`
        : `<p class="muted" style="margin-top:10px">✅ ${esc(t("forgot_sent"))}</p>` +
          (r.reset_link ? `<p style="margin-top:8px"><a class="btn ghost sm" href="${esc(r.reset_link)}">${esc(t("forgot_open_link"))} ↗</a>
             <span class="dim" style="font-size:11.5px;display:block;margin-top:6px">${esc(t("forgot_no_smtp"))}</span></p>` : "");
      b.disabled = false;
    } catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
  });
}

function viewReset(token) {
  $("#app").innerHTML = `<div class="auth-wrap">${authTopControls()}${SEAM_STITCH}${authAside()}
    <div class="auth-main"><div class="auth-card">
      <h1>${esc(t("reset_title"))}</h1>
      <p class="lead">${esc(t("reset_lead"))}</p>
      <form id="resetForm">
        <label class="field"><span>${esc(t("reset_new"))}</span><input class="input" type="password" name="p1" required minlength="8" /></label>
        <label class="field"><span>${esc(t("reset_repeat"))}</span><input class="input" type="password" name="p2" required minlength="8" /></label>
        <button class="btn primary block" type="submit">${esc(t("reset_save"))}</button>
      </form>
      <div class="auth-switch"><a href="#/">${esc(t("login_link"))}</a></div>
    </div></div></div>`;
  wireAuthControls();
  $("#resetForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    if (fd.get("p1") !== fd.get("p2")) { toast(t("reset_mismatch"), "err"); return; }
    const btn = $("#resetForm button"); btn.disabled = true;
    try {
      await API.resetPassword(token, fd.get("p1"));
      toast(t("reset_done"), "ok");
      history.replaceState({}, "", location.pathname);
      State._resetToken = null; location.hash = "#/"; render();
    } catch (err) { toast(errMsg(err), "err"); btn.disabled = false; }
  });
}

let regSide = "buyer";

const REG_COUNTRIES = ["BG", "RO", "GR", "TR", "IT", "DE", "RU", "RS", "MK", "UA", "GB", "OTHER"];
const COUNTRY_REG = {
  BG: { term: "ЕИК / Bulstat", ex: "205643632" }, RO: { term: "CUI / CIF", ex: "RO14837428" },
  GR: { term: "ΑΦΜ", ex: "094014967" }, TR: { term: "VKN", ex: "1234567890" },
  IT: { term: "Partita IVA", ex: "12345678901" }, DE: { term: "USt-IdNr", ex: "DE123456789" },
  RU: { term: "ИНН", ex: "7712345678" }, RS: { term: "PIB", ex: "123456789" },
  MK: { term: "ЕДБ", ex: "4030000000000" }, UA: { term: "ЄДРПОУ", ex: "12345678" },
  GB: { term: "Company no. / VAT", ex: "12345678" }, OTHER: { term: "VAT / Reg. no.", ex: "" },
};
function defaultCountryForLang(lang) {
  return { bg: "BG", ro: "RO", el: "GR", tr: "TR", it: "IT", de: "DE", ru: "RU", en: "GB" }[lang] || "OTHER";
}
function countryName(code) {
  if (code === "OTHER") return t("country_other");
  try { return new Intl.DisplayNames([LANG], { type: "region" }).of(code) || code; } catch (e) { return code; }
}
function regPlaceholder(code) {
  const r = COUNTRY_REG[code] || COUNTRY_REG.OTHER;
  return r.ex ? `${r.term} · ${r.ex}` : r.term;
}

function viewRegister() {
  const defCountry = defaultCountryForLang(LANG);
  const countryOptions = REG_COUNTRIES.map((c) => `<option value="${c}" ${c === defCountry ? "selected" : ""}>${esc(countryName(c))}</option>`).join("");
  $("#app").innerHTML = `<div class="auth-wrap">${authTopControls()}${SEAM_STITCH}${authAside()}
    <div class="auth-main"><div class="auth-card">
      <h1>${esc(t("reg_title"))}</h1>
      <p class="lead">${esc(t("reg_lead"))}</p>
      <div class="side-toggle" id="sideToggle">
        <div class="opt sel" data-side="buyer"><div class="t">${esc(t("side_business_t"))}</div><div class="s">${esc(t("side_business_s"))}</div></div>
        <div class="opt" data-side="partner"><div class="t">${esc(t("side_partner_t"))}</div><div class="s">${esc(t("side_partner_s"))}</div></div>
      </div>
      <form id="regForm">
        <label class="field"><span>${esc(t("your_name"))}</span><input class="input" name="name" required autocomplete="name" /></label>
        <label class="field"><span id="orgLabel">${esc(t("company_name"))}</span><input class="input" name="org_name" required /></label>
        <div class="row2" id="regNumField">
          <label class="field"><span>${esc(t("reg_country_label"))}</span><select class="select" id="regCountry" name="country">${countryOptions}</select></label>
          <label class="field"><span>${esc(t("reg_number_label"))}</span><input class="input" name="reg_number" id="regNumInput" placeholder="${esc(regPlaceholder(defCountry))}" autocomplete="off" /></label>
        </div>
        <label class="field"><span>${esc(t("email"))}</span><input class="input" type="email" name="email" required autocomplete="email" />
          <span class="hint" id="emailHint">${esc(t("company_email_hint"))}</span></label>
        <label class="field"><span>${esc(t("password"))}</span><input class="input" type="password" name="password" required minlength="8" autocomplete="new-password" /></label>
        <label class="field"><span>${esc(t("captcha_label"))}</span>
          <div class="captcha-row">
            <span class="captcha-img" id="captchaImg"></span>
            <button type="button" class="btn ghost sm" id="captchaRefresh" title="${esc(t("captcha_refresh"))}">↻</button>
          </div>
          <input class="input mt8" name="captcha" autocomplete="off" required />
        </label>
        <button class="btn primary block" type="submit">${esc(t("reg_btn"))}</button>
      </form>
      <div class="auth-switch">${esc(t("have_account"))} <a href="#/login">${esc(t("login_link"))}</a></div>
    </div></div></div>`;
  wireAuthControls();
  regSide = "buyer";
  let captchaId = null;
  async function loadCaptcha() {
    try { const c = await API.captcha(); captchaId = c.id; $("#captchaImg").innerHTML = c.svg; }
    catch (e) { $("#captchaImg").textContent = "—"; }
  }
  loadCaptcha();
  $("#captchaRefresh").addEventListener("click", loadCaptcha);
  const rc = $("#regCountry");
  if (rc) rc.addEventListener("change", () => { $("#regNumInput").placeholder = regPlaceholder(rc.value); });
  function applySide() {
    $("#orgLabel").textContent = regSide === "buyer" ? t("company_name") : t("company_name_partner");
    $("#regNumField").classList.toggle("hide", regSide !== "buyer");
    $("#emailHint").classList.toggle("hide", regSide !== "buyer");
  }
  applySide();
  $$("#sideToggle .opt").forEach((o) => o.addEventListener("click", () => {
    $$("#sideToggle .opt").forEach((x) => x.classList.remove("sel"));
    o.classList.add("sel"); regSide = o.dataset.side; applySide();
  }));
  $("#regForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#regForm button"); btn.disabled = true;
    const fd = new FormData(e.target);
    try {
      await API.register({
        name: fd.get("name"), org_name: fd.get("org_name"), email: fd.get("email"),
        password: fd.get("password"), side: regSide, reg_number: fd.get("reg_number"),
        country: fd.get("country"), captcha_id: captchaId, captcha_text: fd.get("captcha"),
      });
      await boot(); go("/");
    } catch (err) { toast(errMsg(err), "err"); btn.disabled = false; loadCaptcha(); }
  });
}

/* =========================================================================
   APP SHELL
   ======================================================================== */
function setIndustry(tplKey) {
  const builtin = ["general", "ecommerce", "agency", "construction", "restaurant", "auto-import"];
  const k = tplKey && builtin.indexOf(tplKey) !== -1 ? tplKey : (tplKey ? "general" : null);
  if (k) document.documentElement.setAttribute("data-industry", k);
  else document.documentElement.removeAttribute("data-industry");
}

/* =========================================================================
   WHAT THIS COMPANY ACTUALLY DOES

   Seam covers seven trades, and most companies work in one. Showing a builder
   a harvest supply agreement is not generosity, it is noise between them and
   the thing they came for. So the company says what it does once, and the
   library, the workspace types and two of the sections narrow to that.

   It is a filter, never a wall. "Everything" is one button away in the
   sidebar, the choice can be changed from the menu, and nothing is ever
   removed - a document outside the chosen trades is still there the moment
   the filter is off.
   ======================================================================== */
const SECTOR_ICON = {
  general: "📁", ecommerce: "🛍️", agency: "🎨", construction: "🏗️",
  restaurant: "🍽️", agriculture: "🌾", "auto-import": "🚗",
};

/* Which parts of the platform belong to which trade. Anything not listed
   here is for everyone - most of it is. */
const NAV_SECTORS = {
  "/store": ["ecommerce", "general", "restaurant", "agriculture"],
  "/vehicles": ["auto-import", "construction", "agriculture"],
};

function mySectors() { return (State.sectors && State.sectors.sectors) || []; }

/* On when the company has named its trades and has not asked to see the rest.
   An empty list is a deliberate "show me everything" and turns it off too. */
function narrowing() {
  return !State.showAllSectors && mySectors().length > 0;
}

function inMySectors(list) {
  if (!narrowing()) return true;
  if (!list || !list.length) return true;          // belongs to everyone
  const mine = mySectors();
  return list.some((s) => mine.indexOf(s) !== -1);
}

function sectorChip(s) {
  return `${SECTOR_ICON[s] || "◆"} ${esc(tplLabel(s))}`;
}

async function openSectorChooser(first) {
  const all = (State.sectors && State.sectors.all) || Object.keys(SECTOR_ICON);
  let picked = new Set(mySectors());
  const card = (s) => `
    <button type="button" class="sec-pick ${picked.has(s) ? "on" : ""}" data-pick="${esc(s)}">
      <span class="sp-ic">${SECTOR_ICON[s] || "◆"}</span>
      <span class="sp-tx"><b>${esc(tplLabel(s))}</b><small>${esc(tplTagline(s))}</small></span>
      <span class="sp-tick">✓</span>
    </button>`;

  openModal(t(first ? "sec_welcome" : "sec_change"), `
    <p class="dim" style="font-size:13px;margin-bottom:14px">${esc(t("sec_hint"))}</p>
    <div class="sec-picks">${all.map(card).join("")}</div>
    <p class="dim mt12" style="font-size:12px">${esc(t("sec_note"))}</p>`,
    `${first ? "" : `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>`}
     <button class="btn ghost" id="secAll">${esc(t("sec_show_all"))}</button>
     <button class="btn primary" id="secSave">${esc(t("sec_save"))}</button>`,
    { locked: !!first });

  const sync = () => $$("[data-pick]").forEach((b) =>
    b.classList.toggle("on", picked.has(b.dataset.pick)));
  $$("[data-pick]").forEach((b) => b.addEventListener("click", () => {
    if (picked.has(b.dataset.pick)) picked.delete(b.dataset.pick);
    else picked.add(b.dataset.pick);
    sync();
    const save = $("#secSave");
    if (save) save.disabled = picked.size === 0;
  }));
  const save = $("#secSave");
  if (save) save.disabled = picked.size === 0;

  const commit = async (list) => {
    try {
      const r = await API.sectorsSet(list);
      State.sectors = Object.assign(State.sectors || {}, r);
      State.showAllSectors = list.length === 0;
      docSector = "mine";
      closeModal();
      toast(t("saved"), "ok");
      render();
    } catch (err) { toast(errMsg(err), "err"); }
  };
  // "Show me everything" is stored as an empty list: asked and answered, with
  // the answer being "all of it". That is not the same as never having asked.
  $("#secAll").addEventListener("click", () => commit([]));
  save.addEventListener("click", () => commit([...picked]));
}

function shell(crumbsHTML, contentHTML) {
  const unread = State.notif.unread;
  /* Under the wordmark belongs the one fact the chrome can state: which company
     you are signed in as. A person can hold accounts on both sides of the same
     trade, so this is worth having in view. A slogan there was noise: anyone
     reading it is already inside the product. */
  const orgLine = (State.org && State.org.name) || "";
  $("#app").innerHTML = `
    <div class="topbar">
      <div class="brand" onclick="location.hash='#/'">${SEAM_MARK}<div>Seam${orgLine ? `<br><small>${esc(orgLine)}</small>` : ""}</div></div>
      <div class="crumbs">${crumbsHTML || ""}</div>
      <div class="spacer"></div>
      <button class="iconbtn ccy-btn" id="ccyBtn" data-overlay-toggle title="${esc(t("currency"))}">${esc(currentCcy().sym)}</button>
      <button class="iconbtn" id="themeBtn" title="${esc(t("theme_" + (getTheme() === "dark" ? "light" : "dark")))}">${getTheme() === "dark" ? "☀" : "☾"}</button>
      <button class="iconbtn" id="langBtn" data-overlay-toggle title="${esc(t("language"))}"><span class="flag">${currentLang().flag}</span></button>
      <button class="iconbtn ${unread ? "has-unread" : ""}" id="notifBtn" data-overlay-toggle title="${esc(t("notifications"))}">🔔${unread ? `<span class="notif-dot">${unread > 9 ? "9+" : unread}</span>` : ""}</button>
      <div class="avatar" id="userBtn" data-overlay-toggle title="${esc(State.user.name)}">${initials(State.user.name)}</div>
    </div>
    <div class="layout">${sidenav()}<div id="content">${contentHTML}</div></div>`;
  $("#ccyBtn").addEventListener("click", openCcyMenu);
  $("#themeBtn").addEventListener("click", toggleTheme);
  $("#langBtn").addEventListener("click", openLangMenu);
  $("#notifBtn").addEventListener("click", toggleNotif);
  $("#userBtn").addEventListener("click", toggleUserMenu);
  const st = $("#secToggle");
  if (st) st.addEventListener("click", () => {
    State.showAllSectors = !State.showAllSectors;
    // The document filter follows the same switch, or the two disagree and
    // the library looks broken.
    docSector = State.showAllSectors ? "all" : "mine";
    render();
  });
}

function sidenav() {
  const r = currentRoute();
  const isBiz = State.org && State.org.kind === "business";
  const item = (href, icon, label, active) =>
    `<a class="navitem ${active ? "active" : ""}" href="#${href}"><span class="ni-ic">${icon}</span><span class="ni-tx">${esc(label)}</span></a>`;
  // A section outside the company's trades is hidden, not removed: the toggle
  // underneath brings it back, and the route still works if it is bookmarked.
  const forMe = (href) => inMySectors(NAV_SECTORS[href]);
  const maybe = (href, icon, label, active) => (forMe(href) ? item(href, icon, label, active) : "");
  const hidden = Object.keys(NAV_SECTORS).filter((h) => !forMe(h)).length;
  const toggle = !mySectors().length ? "" : `
    <button class="nav-toggle" id="secToggle" title="${esc(t("sec_toggle_hint"))}">
      <span class="ni-ic">${State.showAllSectors ? "🎯" : "🧭"}</span>
      <span class="ni-tx">${esc(State.showAllSectors ? t("sec_only_mine") : t("sec_show_all"))}</span>
      ${hidden && !State.showAllSectors ? `<span class="nav-count">${hidden}</span>` : ""}
    </button>`;

  return `<nav class="sidenav">
    ${item("/", "🏠", t("home"), r === "/" || r === "" || r.startsWith("/w/") || r.startsWith("/o/"))}
    ${item("/analytics", "📊", t("nav_analytics"), r.startsWith("/analytics"))}
    ${item("/assistant", "✨", t("nav_assistant"), r.startsWith("/assistant"))}
    ${isBiz ? maybe("/store", "🛒", t("nav_store"), r.startsWith("/store")) : ""}
    ${isBiz ? maybe("/vehicles", "🚗", t("vh_title"), r.startsWith("/vehicles") || r.startsWith("/passport/vehicle")) : ""}
    ${isBiz ? item("/templates", "🧩", t("nav_templates"), r.startsWith("/templates")) : ""}
    ${item("/docs", "📄", t("nav_docs"), r.startsWith("/docs"))}
    ${isBiz ? item("/money", "💰", t("mn_title"), r.startsWith("/money")) : ""}
    ${item("/finance", "🧮", t("nav_finance"), r.startsWith("/finance"))}
    ${item("/network", "🕸", t("nav_network"), r.startsWith("/network"))}
    ${item("/directory", "📇", t("dir_title"), r.startsWith("/directory"))}
    ${item("/messages", "💬", t("inbox_title"), r.startsWith("/messages"))}
    ${isBiz ? item("/integrations", "🔌", t("nav_integrations"), r.startsWith("/integrations")) : ""}
    ${item("/plans", "💠", t("nav_plans"), r.startsWith("/plans"))}
    ${toggle}
  </nav>`;
}

function crumb(parts) {
  return parts.map((p, i) => {
    const last = i === parts.length - 1;
    const item = last ? `<b>${esc(p.label)}</b>` : `<a href="#${p.href}">${esc(p.label)}</a>`;
    return (i ? `<span class="sep">›</span>` : "") + item;
  }).join("");
}

function toggleUserMenu(e) {
  e.stopPropagation();
  if ($("#overlay-root").querySelector(".user-menu")) { closeOverlay(); return; }
  const isBiz = State.org && State.org.kind === "business";
  $("#overlay-root").innerHTML = `<div class="menu user-menu">
    <div class="mhead"><div class="nm">${esc(State.user.name)}</div><div class="em">${esc(State.user.email)}</div>
      <div class="mt8"><span class="badge ${isBiz ? "buyer" : "partner"}">${esc((State.org && State.org.name) || "")} · ${esc(isBiz ? t("account_business") : t("account_partner"))}</span></div>
      <div class="mt8"><span class="badge ${State.user.verified ? "ok" : "warn"} dot">${esc(State.user.verified ? t("account_verified") : t("account_unverified"))}</span></div></div>
    <div class="mi" id="profileBtn"><span>👤</span> ${esc(t("prof_title_modal"))}</div>
    ${isBiz ? `<div class="mi" id="sectorsBtn"><span>🎯</span> ${esc(t("sec_change"))}</div>` : ""}
    ${isBiz ? `<div class="mi" id="tplMgrBtn"><span>🧩</span> ${esc(t("nav_templates"))}</div>` : ""}
    ${isBiz ? `<div class="mi" id="importBtn"><span>📥</span> ${esc(t("im_title"))}</div>` : ""}
    ${isBiz ? `<div class="mi" id="healthBtn"><span>🩺</span> ${esc(t("hl_title"))}</div>` : ""}
    ${isBiz ? `<div class="mi" id="mailBtn"><span>✉️</span> ${esc(t("sm_title"))}</div>` : ""}
    ${isBiz ? `<div class="mi" id="billingBtn"><span>💶</span> ${esc(t("bill_title"))}</div>` : ""}
    <div class="mi" id="teamBtn"><span>👥</span> ${esc(t("tm_title"))}</div>
    <div class="mi" id="securityBtn"><span>🔒</span> ${esc(t("sec_title"))}</div>
    <div class="mi" id="privacyBtn"><span>🛡</span> ${esc(t("pv_title"))}</div>
    <div class="mi" id="notifPrefBtn"><span>🔔</span> ${esc(t("nt_settings"))}</div>
    <div class="mi sep" id="logoutBtn">↩ ${esc(t("logout"))}</div>
  </div>`;
  $("#profileBtn").addEventListener("click", () => { closeOverlay(); openProfile("person"); });
  const sb = $("#sectorsBtn");
  if (sb) sb.addEventListener("click", () => { closeOverlay(); openSectorChooser(false); });
  const tm = $("#tplMgrBtn");
  if (tm) tm.addEventListener("click", () => { closeOverlay(); go("/templates"); });
  const ib = $("#importBtn");
  if (ib) ib.addEventListener("click", () => { closeOverlay(); openImport(); });
  const hb = $("#healthBtn");
  if (hb) hb.addEventListener("click", () => { closeOverlay(); openHealth(); });
  // Shown to every business account; the endpoints behind them are guarded, so
  // a company that does not run this installation is told no by the server.
  const mb = $("#mailBtn");
  if (mb) mb.addEventListener("click", () => { closeOverlay(); openMail(); });
  const bb = $("#billingBtn");
  if (bb) bb.addEventListener("click", () => { closeOverlay(); openBilling(); });
  $("#teamBtn").addEventListener("click", () => { closeOverlay(); openTeam(); });
  $("#securityBtn").addEventListener("click", () => { closeOverlay(); openSecurity(); });
  $("#privacyBtn").addEventListener("click", () => { closeOverlay(); openPrivacy(); });
  $("#notifPrefBtn").addEventListener("click", () => { closeOverlay(); openNotifySettings(); });
  $("#logoutBtn").addEventListener("click", async () => { await API.logout(); State.user = null; closeOverlay(); go("/login"); location.reload(); });
}

/* ---- security: second factor and where you are signed in ----------------- *
 * Two things a company needs and cannot build for itself: a second factor that
 * does not depend on any national scheme, and the ability to see and cut off
 * every place the account is open.                                            */
async function openSecurity() {
  let st = { enabled: false, recovery_left: 0 }, ss = { sessions: [] };
  try { [st, ss] = await Promise.all([API.twoFactor(), API.sessions()]); }
  catch (err) { toast(errMsg(err), "err"); return; }
  let links = { links: [] }, cat = { providers: [], country: "", verified: "" };
  try { [links, cat] = await Promise.all([API.eidLinks(), API.eidCatalogue()]); } catch {}

  const rows = ss.sessions.map((s) => `
    <div class="net-row">
      <div class="grow"><b>${esc(s.device || t("sec_unknown_device"))}</b>
        ${s.current ? ` <span class="badge ok dot">${esc(t("sec_this_device"))}</span>` : ""}
        <div class="dim" style="font-size:11.5px">${esc(s.ip)} · ${esc(t("sec_last_seen"))} ${relTime(s.last_seen)}</div></div>
      <div class="net-side">${s.current ? "" :
        `<button class="btn ghost sm" data-killsess="${s.id}">${esc(t("sec_revoke"))}</button>`}</div>
    </div>`).join("");

  openModal(t("sec_title"), `
    <div class="section-sub">${esc(t("pw_title"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("pw_hint"))}</p>
      <label class="field mt8"><span>${esc(t("pw_current"))}</span>
        <input class="input" id="pwCur" type="password" autocomplete="current-password" /></label>
      <label class="field"><span>${esc(t("pw_new"))}</span>
        <input class="input" id="pwNew" type="password" autocomplete="new-password" /></label>
      <div class="pw-meter" id="pwMeter"><div class="pw-bar"><i></i></div><span class="pw-tx"></span></div>
      <label class="field"><span>${esc(t("pw_repeat"))}</span>
        <input class="input" id="pwNew2" type="password" autocomplete="new-password" /></label>
      <button class="btn primary sm mt8" id="pwSave">${esc(t("pw_save"))}</button>
    </div>

    <div class="section-sub mt16">${esc(t("sec_2fa"))}</div>
    <div class="card pad">
      <div class="flex gap8 center wrap">
        <span class="badge ${st.enabled ? "ok" : "warn"} dot">${esc(st.enabled ? t("sec_on") : t("sec_off"))}</span>
        ${st.enabled ? `<span class="dim" style="font-size:12px">${esc(t("sec_codes_left", { n: st.recovery_left }))}</span>` : ""}
      </div>
      <p class="dim mt8" style="font-size:12.5px">${esc(t("sec_2fa_hint"))}</p>
      <div id="secBody" class="mt12"></div>
    </div>
    <div class="section-sub mt16">${esc(t("eid_title"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("eid_hint"))}</p>
      ${eidLinkRows(links.links)}
      ${eidOfferRows(cat)}
      <div class="dim mt8" style="font-size:11px">${esc(t("eid_verified", { d: cat.verified || "" }))}</div>
    </div>

    <div class="section-sub mt16">${esc(t("sec_sessions"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("sec_sessions_hint", { h: ss.idle_hours, d: ss.max_days }))}</p>
      <div class="mt8">${rows || `<p class="muted" style="font-size:13px">${esc(t("ni_none"))}</p>`}</div>
      ${ss.sessions.length > 1 ? `<button class="btn ghost sm mt12" id="secKillAll">${esc(t("sec_revoke_others"))}</button>` : ""}
    </div>`);

  // The strength shown here applies the same three tests the server does, on
  // the same blocklist, fetched from it. A meter that calls "password1"
  // acceptable while the server refuses it is worse than no meter: it sends
  // people to a wall it told them was a door.
  const meterEl = $("#pwMeter"), pwNew = $("#pwNew");
  if (!State._pwRules) {
    try { State._pwRules = await API.passwordRules(); }
    catch { State._pwRules = { min: 8, common: [] }; }
  }
  const rules = State._pwRules;
  const common = new Set(rules.common || []);
  const gauge = () => {
    const pw = pwNew.value;
    const bar = meterEl.querySelector("i"), tx = meterEl.querySelector(".pw-tx");
    if (!pw) { meterEl.className = "pw-meter"; bar.style.width = "0"; tx.textContent = ""; return; }
    const who = ((State.user || {}).email || "").split("@")[0].toLowerCase();
    const org = ((State.org || {}).name || "").toLowerCase();
    const low = pw.toLowerCase(), flat = low.replace(/[^a-z0-9]+/g, "");
    const stem = low.replace(/[0-9]+$/, "") || low;
    const words = (who + " " + org).split(/[^a-z0-9]+/).filter((w) => w.length >= 4);
    words.push(org.replace(/[^a-z0-9]+/g, ""));
    let level, msg;
    if (pw.length < (rules.min || 8) || !/[A-Za-z]/.test(pw) || !/\d/.test(pw)) {
      level = "weak"; msg = t("pw_g_short");
    } else if (common.has(low) || common.has(stem) || new Set(low).size <= 3) {
      level = "weak"; msg = t("pw_g_common");
    } else if (words.some((w) => w.length >= 4 && flat.indexOf(w) !== -1)) {
      level = "weak"; msg = t("pw_g_obvious");
    } else if (pw.length >= 14 || (pw.length >= 11 && /[^A-Za-z0-9]/.test(pw))) {
      level = "strong"; msg = t("pw_g_strong");
    } else { level = "ok"; msg = t("pw_g_ok"); }
    meterEl.className = "pw-meter " + level;
    bar.style.width = { weak: "33%", ok: "66%", strong: "100%" }[level];
    tx.textContent = msg;
  };
  pwNew.addEventListener("input", gauge);

  $("#pwSave").addEventListener("click", async () => {
    const cur = $("#pwCur").value, nw = pwNew.value, again = $("#pwNew2").value;
    if (nw !== again) { toast(t("pw_mismatch"), "err"); return; }
    const btn = $("#pwSave"); btn.disabled = true;
    try {
      await API.changePassword(cur, nw);
      toast(t("pw_changed"), "ok");
      openSecurity();
    } catch (err) { toast(errMsg(err), "err"); btn.disabled = false; }
  });

  const body = $("#secBody");
  const drawOff = () => {
    body.innerHTML = `<button class="btn primary sm" id="secStart">${esc(t("sec_enable"))}</button>`;
    $("#secStart").addEventListener("click", async () => {
      try {
        const s = await API.twoFactorSetup();
        body.innerHTML = `
          <p style="font-size:12.5px;line-height:1.6">${esc(t("sec_scan"))}</p>
          <div class="hashrow"><code id="secKey">${esc(s.grouped)}</code>
            <button id="secCopy">${esc(t("nr_copy"))}</button></div>
          <a class="btn ghost sm mt8" href="${esc(s.uri)}">${esc(t("sec_open_app"))} ↗</a>
          <label class="field mt12"><span>${esc(t("sec_code"))}</span>
            <input class="input" id="secCode" inputmode="numeric" maxlength="6" autocomplete="one-time-code" style="max-width:180px" /></label>
          <button class="btn primary sm" id="secConfirm">${esc(t("sec_confirm"))}</button>`;
        $("#secCopy").addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(s.secret); toast(t("int_copied"), "ok"); }
          catch (e) { toast(errMsg(e), "err"); }
        });
        $("#secConfirm").addEventListener("click", async () => {
          try {
            const r = await API.twoFactorEnable($("#secCode").value.trim());
            // Shown once and never again: Seam keeps only the hashes.
            body.innerHTML = `<div class="note-box"><b>${esc(t("sec_codes"))}</b><br>
              <span style="font-size:12px">${esc(t("sec_codes_hint"))}</span>
              <div class="mt8" style="font-family:monospace;font-size:13px;line-height:1.9">
                ${r.recovery_codes.map(esc).join("<br>")}</div></div>`;
          } catch (err) { toast(errMsg(err), "err"); }
        });
      } catch (err) { toast(errMsg(err), "err"); }
    });
  };
  const drawOn = () => {
    body.innerHTML = `<label class="field"><span>${esc(t("sec_password"))}</span>
        <input class="input" id="secPw" type="password" autocomplete="current-password" style="max-width:260px" /></label>
      <button class="btn ghost sm" id="secOff">${esc(t("sec_disable"))}</button>`;
    $("#secOff").addEventListener("click", async () => {
      try { await API.twoFactorDisable($("#secPw").value); toast(t("saved"), "ok"); closeModal(); openSecurity(); }
      catch (err) { toast(errMsg(err), "err"); }
    });
  };
  (st.enabled ? drawOn : drawOff)();

  $$("[data-eidlink]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { const r = await API.eidStart(b.dataset.eidlink); location.href = r.url; }
    catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
  }));
  $$("[data-eiddrop]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.eidUnlink(b.dataset.eiddrop); closeModal(); openSecurity(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));

  $$("[data-killsess]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.sessionRevoke(b.dataset.killsess); closeModal(); openSecurity(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  const ka = $("#secKillAll");
  if (ka) ka.addEventListener("click", async () => {
    try { await API.sessionsRevokeOthers(); toast(t("saved"), "ok"); closeModal(); openSecurity(); }
    catch (err) { toast(errMsg(err), "err"); }
  });
}

/* ---- what this installation does with data ------------------------------ *
 * Deliberately not a policy page. The middle section is read from the running
 * configuration, so it says what this instance does rather than what the
 * software could in principle be made to do.                                  */
async function openPrivacy() {
  let p = null;
  try { p = await API.privacy(); }
  catch (err) { toast(errMsg(err), "err"); return; }

  const stored = p.stored.map((k) => `
    <div class="pv-row"><b>${esc(t("pv_s_" + k))}</b></div>`).join("");

  const leaves = p.leaves.map((l) => `
    <div class="pv-row ${l.on ? "" : "off"}">
      <div class="grow"><b>${esc(t("pv_l_" + l.key))}</b>
        ${l.on && l.where ? `<div class="dim mono" style="font-size:11px;word-break:break-all">${esc(l.where)}</div>` : ""}</div>
      <span class="badge ${l.on ? "ok" : ""} dot">${esc(l.on ? t("pv_on") : t("pv_off"))}</span>
    </div>`).join("");

  const never = p.never.map((k) => `
    <div class="pv-row"><b>${esc(t("pv_n_" + k))}</b></div>`).join("");

  const r = p.retention;
  openModal(t("pv_title"), `
    <p class="muted" style="font-size:13px;line-height:1.6">${esc(t("pv_lead"))}</p>

    <div class="section-sub mt16">${esc(t("pv_stored"))}</div>
    <div class="card pad">${stored}</div>

    <div class="section-sub mt16">${esc(t("pv_leaves"))}</div>
    <div class="card pad">${leaves}</div>

    <div class="section-sub mt16">${esc(t("pv_never"))}</div>
    <div class="card pad">${never}</div>

    <div class="section-sub mt16">${esc(t("pv_retention"))}</div>
    <div class="card pad">
      <div class="pv-row"><b>${esc(t("pv_r_errors", { n: r.error_log }))}</b></div>
      <div class="pv-row"><b>${esc(t("pv_r_sessions", { h: r.session_idle_hours, d: r.session_max_days }))}</b></div>
      <div class="pv-row"><b>${esc(t("pv_r_backups", { n: r.backups }))}</b></div>
      <div class="pv-row"><b>${esc(t("pv_r_self_hosted"))}</b></div>
    </div>

    <div class="section-sub mt16">${esc(t("pv_rights"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("pv_export_hint"))}</p>
      <a class="btn primary sm mt8" href="/api/account/export">${esc(t("pv_export"))}</a>
      <div class="note-box mt16">${esc(t("pv_erase_hint"))}</div>
      <button class="btn ghost sm mt12" id="pvClose">${esc(t("pv_erase"))}</button>
    </div>`);

  $("#pvClose").addEventListener("click", openCloseAccount);
}

/* Erasure. The audit trail is append-only and that is the point of it, so this
   removes the person and leaves what they did attributable to a number. The
   dialog says so before anything happens. */
async function openCloseAccount() {
  let e = { colleagues: 0, relationships: 0 };
  try { e = await API.closeEffect(); }
  catch (err) { toast(errMsg(err), "err"); return; }

  const blocked = e.must_hand_over;
  const m = openModal(t("pv_erase"), `
    <div class="note-box">${esc(t("pv_erase_what"))}</div>
    <div class="pv-row mt12"><b>${esc(t("pv_erase_kept"))}</b></div>
    <div class="pv-row"><b>${esc(t("pv_erase_scope", { n: e.relationships }))}</b></div>
    ${e.last_owner ? `<div class="note-box mt12">${esc(t("pv_erase_last_owner"))}</div>` : ""}
    ${blocked ? `<div class="note-box mt12">${esc(t("err_owner_must_hand_over"))}</div>` : `
      <label class="field mt16"><span>${esc(t("sec_password"))}</span>
        <input class="input" id="pvPw" type="password" autocomplete="current-password" style="max-width:260px" /></label>`}`,
    blocked ? `<button class="btn ghost" data-close>${esc(t("close"))}</button>`
            : `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
               <button class="btn danger" id="pvGo">${esc(t("pv_erase_confirm"))}</button>`);
  if (blocked) return;

  $("#pvGo").addEventListener("click", async () => {
    if (!confirm(t("pv_erase_sure"))) return;
    const b = $("#pvGo"); b.disabled = true;
    try {
      await API.closeAccount(m.querySelector("#pvPw").value);
      closeModal();
      State.user = null;
      location.href = "/";
    } catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
  });
}

/* ---- bringing existing work in ------------------------------------------ *
 * Preview first, commit second. A company arrives with its counterparties in a
 * spreadsheet; if day one is retyping them, nothing else matters. */
function openImport() {
  const problem = (p) => p ? t("im_p_" + p) : "";
  openModal(t("im_title"), `
    <p class="dim" style="font-size:12.5px">${esc(t("im_hint"))}</p>
    <label class="field"><span>${esc(t("im_what"))}</span>
      <select class="select" id="imKind">
        <option value="partners">${esc(t("im_partners"))}</option>
        <option value="orders">${esc(t("im_orders"))}</option>
      </select></label>
    <div class="dim" id="imCols" style="font-size:11.5px;margin-top:-6px"></div>
    <label class="field"><span>${esc(t("im_file"))}</span>
      <input type="file" id="imFile" accept=".csv,.txt" /></label>
    <button class="btn ghost sm" id="imPreview">${esc(t("im_preview"))}</button>
    <div id="imResult" class="mt12"></div>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
     <button class="btn primary" id="imCommit" disabled>${esc(t("im_commit"))}</button>`);

  const COLS = {
    partners: t("im_cols_partners"),
    orders: t("im_cols_orders"),
  };
  const drawCols = () => { $("#imCols").textContent = COLS[$("#imKind").value]; };
  $("#imKind").addEventListener("change", () => { drawCols(); $("#imResult").innerHTML = "";
                                                  $("#imCommit").disabled = true; });
  drawCols();

  let content = "";
  $("#imPreview").addEventListener("click", () => {
    const f = ($("#imFile").files || [])[0];
    if (!f) return toast(t("im_pick"), "err");
    const rd = new FileReader();
    rd.onload = async () => {
      content = rd.result;
      try {
        const r = await API.importPreview($("#imKind").value, content);
        $("#imResult").innerHTML = `
          <div class="flex gap8 center wrap">
            <span class="badge ok">${esc(t("im_will_create", { n: r.summary.create }))}</span>
            ${r.summary.exists ? `<span class="badge">${esc(t("im_exists", { n: r.summary.exists }))}</span>` : ""}
            ${r.summary.skip ? `<span class="badge warn">${esc(t("im_skipped", { n: r.summary.skip }))}</span>` : ""}
          </div>
          <div class="an-rows mt12" style="max-height:260px;overflow:auto">${r.plan.map((p) => `
            <div class="an-row"><div class="grow">
              <b>${esc(p.name || p.title || "—")}</b>
              <div class="dim" style="font-size:11.5px">${esc(t("im_row", { n: p.row }))}${
                p.workspace ? " · " + esc(p.workspace) : ""}</div></div>
              <span class="badge ${p.action === "create" ? "ok" : p.action === "skip" ? "warn" : ""}">${esc(t("im_a_" + p.action))}</span>
              ${p.problem ? `<span class="dim" style="font-size:11.5px">${esc(problem(p.problem))}</span>` : ""}
            </div>`).join("")}</div>`;
        $("#imCommit").disabled = r.summary.create === 0;
      } catch (err) { toast(errMsg(err), "err"); }
    };
    rd.readAsText(f);
  });

  $("#imCommit").addEventListener("click", async () => {
    $("#imCommit").disabled = true;
    try {
      const r = await API.importCommit($("#imKind").value, content);
      toast(t("im_done", { n: r.created }), "ok");
      // Without SMTP the invite links are shown rather than silently going nowhere.
      if ((r.invite_links || []).length) {
        $("#imResult").innerHTML = `<p class="dim" style="font-size:12.5px">${esc(t("im_links"))}</p>
          ${r.invite_links.map((l) => `<div class="hashrow"><code>${esc(l.email)} · ${esc(l.link)}</code></div>`).join("")}`;
      } else { closeModal(); }
      render();
    } catch (err) { toast(errMsg(err), "err"); $("#imCommit").disabled = false; }
  });
}

/* ---- is anything failing, and when was the last backup ------------------- */
async function openHealth() {
  let h, e, pf;
  try { [h, e] = await Promise.all([API.health(), API.errors()]); }
  catch (err) { return toast(errMsg(err), "err"); }
  try { pf = await API.preflight(); } catch { pf = null; }

  // Before customers arrive, the only question that matters is whether this is
  // safe to put in front of them. Blockers first, and say so plainly.
  const icon = { blocker: "✕", important: "!", optional: "·" };
  const preflightBlock = !pf ? "" : `
    <div class="card pad" style="margin-bottom:16px">
      <div class="flex between center wrap gap8">
        <b>${esc(t("pf_title"))}</b>
        <span class="badge ${pf.ready ? "ok" : "danger"} dot">${esc(
          pf.ready ? t("pf_ready") : t("pf_blocked", { n: pf.blockers }))}</span>
      </div>
      <p class="dim mt8" style="font-size:12.5px">${esc(t("pf_hint"))}</p>
      ${(pf.untested_here || []).length ? `<p class="pf-exempt mt8">⚠ ${
        esc(t("pf_untested_here", { host: pf.host || "", n: pf.untested_here.length }))}<br>
        <span class="dim">${pf.untested_here.map((k) => esc(t("pf_" + k))).join(", ")}</span></p>` : ""}
      <div class="an-rows mt8">${pf.checks.filter((c) => !c.ok).map((c) => `
        <div class="an-row">
          <span class="ap-ic ${c.level === "blocker" ? "danger" : c.level === "important" ? "warn" : ""}">${icon[c.level]}</span>
          <div class="grow"><b>${esc(t("pf_" + c.key))}</b>
            <div class="dim" style="font-size:11.5px">${esc(t("pf_why_" + c.key))}</div></div>
          <span class="badge ${c.level === "blocker" ? "danger" : c.level === "important" ? "warn" : ""}">${esc(t("pf_l_" + c.level))}</span>
        </div>`).join("") || `<p class="muted" style="font-size:13px">${esc(t("pf_all_good"))}</p>`}
      </div>
    </div>`;
  const mb = (n) => (n / 1048576).toFixed(1) + " MB";
  const svc = (on) => `<span class="badge ${on ? "ok" : ""}">${esc(on ? t("hl_on") : t("hl_off"))}</span>`;

  openModal(t("hl_title"), `
    ${preflightBlock}
    <div class="an-tiles">
      <div class="an-tile"><div class="n ${h.errors.unseen ? "" : ""}">${h.errors.unseen}</div>
        <div class="l">${esc(t("hl_unseen"))}</div></div>
      <div class="an-tile"><div class="n sm">${esc(mb(h.storage.database + h.storage.uploads))}</div>
        <div class="l">${esc(t("hl_storage"))}</div></div>
      <div class="an-tile"><div class="n sm">${esc(h.backup ? relTime(h.backup.at) : t("hl_never"))}</div>
        <div class="l">${esc(t("hl_backup"))}</div></div>
    </div>
    <div class="an-legend mt12">
      <span>${esc(t("nt_ch_email"))} ${svc(h.services.email)}</span>
      <span>${esc(t("nt_ch_sms"))} ${svc(h.services.sms)}</span>
      <span>${esc(t("cn_cloud"))} ${svc(h.services.cloud)}</span>
      <span>${esc(t("qt_title"))} ${svc(h.services.qtsp)}</span>
    </div>
    <div class="flex gap8 mt12">
      <button class="btn primary sm" id="hlBackup">${esc(t("hl_backup_now"))}</button>
      ${h.errors.unseen ? `<button class="btn ghost sm" id="hlSeen">${esc(t("hl_mark_seen"))}</button>` : ""}
    </div>
    ${h.backup && h.backup.encrypted ? `<p class="pf-exempt mt12">🔐 ${esc(t("hl_backup_enc"))}<br>
      <code>python restore_backup.py &lt;${esc(t("hl_backup_file"))}&gt;</code></p>` : ""}
    <div class="section-title" style="margin:20px 0 10px">${esc(t("hl_errors"))}</div>
    <div class="an-rows" style="max-height:280px;overflow:auto">${e.errors.length ? e.errors.map((x) => `
      <div class="an-row"><div class="grow">
        <b>${esc(x.kind)}${x.seen ? "" : " ·"}</b> <span class="dim">${esc(x.message)}</span>
        <div class="dim mono" style="font-size:11px">${esc(x.method)} ${esc(x.path)} · ${relTime(x.at)}${x.user ? " · " + esc(x.user) : ""}</div>
      </div>
      <button class="btn ghost sm" data-err="${x.id}">${esc(t("hl_details"))}</button>
      </div>
      <pre id="errbody-${x.id}" class="hl-tb" hidden>${esc(x.traceback || "")}</pre>`).join("")
      : `<p class="muted" style="font-size:13px">${esc(t("hl_no_errors"))}</p>`}</div>`,
    `<button class="btn ghost" data-close>${esc(t("close"))}</button>`);

  $$("[data-err]").forEach((b) => b.addEventListener("click", () => {
    const pre = $("#errbody-" + b.dataset.err);
    pre.hidden = !pre.hidden;
  }));
  const bk = $("#hlBackup");
  if (bk) bk.addEventListener("click", async () => {
    bk.disabled = true;
    try {
      const r = await API.backup(true);
      toast(t("hl_backup_done", { n: (r.bytes / 1048576).toFixed(1) }), "ok");
      openHealth();
    } catch (err) { toast(errMsg(err), "err"); bk.disabled = false; }
  });
  const sn = $("#hlSeen");
  if (sn) sn.addEventListener("click", async () => {
    try { await API.errorsSeen(); openHealth(); } catch (err) { toast(errMsg(err), "err"); }
  });
}

/* ---- who you are, and who your company is -------------------------------
 * Two cards, because they answer different questions. A message signed with a
 * name and a face is a person; a contract is with a company. Both are visible
 * only to people who already share a company or a workspace - the same
 * boundary as everything else here. Nothing on either card is public. */
let profileTab = "person";

function avatarBox(url, name, size) {
  const s = size || 76;
  return url
    ? `<img class="pf-photo" src="${esc(url)}" alt="" style="width:${s}px;height:${s}px">`
    : `<div class="pf-photo pf-initials" style="width:${s}px;height:${s}px;font-size:${Math.round(s / 2.6)}px">${
        esc(initials(name || "?"))}</div>`;
}

function linkRows(links, editable) {
  const rows = (links || []).map((l, i) => `
    <div class="pf-link" data-link-row="${i}">
      <input class="input" data-link-label placeholder="${esc(t("prof_link_label"))}" value="${esc(l.label || "")}"${
        editable ? "" : " readonly"}>
      <input class="input" data-link-url placeholder="https://" value="${esc(l.url || "")}"${
        editable ? "" : " readonly"}>
      ${editable ? `<button class="btn ghost sm" data-link-del="${i}">✕</button>` : ""}
    </div>`).join("");
  return `<div id="pfLinks">${rows}</div>${
    editable ? `<button class="btn ghost sm mt8" id="pfLinkAdd">+ ${esc(t("prof_link_add"))}</button>` : ""}`;
}

function readLinks() {
  return $$("#pfLinks .pf-link").map((r) => ({
    label: r.querySelector("[data-link-label]").value.trim(),
    url: r.querySelector("[data-link-url]").value.trim(),
  })).filter((l) => l.url);
}

async function openProfile(tab) {
  profileTab = tab || profileTab;
  let d;
  try { d = await API.profile(); } catch (err) { return toast(errMsg(err), "err"); }
  const p = d.person, c = d.company || {};
  const isCompany = profileTab === "company";
  const canEdit = !isCompany || d.can_edit_company;

  const tabs = `<div class="flex gap8 wrap" style="margin-bottom:14px">
    <button class="btn ${!isCompany ? "primary" : "ghost"} sm" data-pf-tab="person">${esc(t("prof_person"))}</button>
    <button class="btn ${isCompany ? "primary" : "ghost"} sm" data-pf-tab="company">${esc(t("prof_company"))}</button>
  </div>`;

  const photoBlock = `
    <div class="pf-head">
      ${avatarBox(isCompany ? c.logo : p.avatar, isCompany ? c.name : p.name)}
      ${canEdit ? `<div class="pf-photo-acts">
        <label class="btn ghost sm">${esc(t("prof_photo_pick"))}
          <input type="file" id="pfPhoto" accept="image/png,image/jpeg,image/webp,image/gif" hidden></label>
        ${(isCompany ? c.logo : p.avatar) ? `<button class="btn ghost sm" id="pfPhotoDel">${esc(t("tm_remove"))}</button>` : ""}
        <div class="dim" style="font-size:11.5px">${esc(t("prof_photo_hint"))}</div>
      </div>` : ""}
    </div>`;

  const bodyPerson = `
    ${photoBlock}
    <label class="field"><span>${esc(t("prof_name"))}</span>
      <input class="input" value="${esc(p.name)}" readonly></label>
    <label class="field"><span>${esc(t("prof_title"))}</span>
      <input class="input" id="pfTitle" maxlength="80" placeholder="${esc(t("prof_title_ph"))}" value="${esc(p.title)}"></label>
    <label class="field"><span>${esc(t("prof_bio"))}</span>
      <textarea class="input" id="pfBio" rows="4" maxlength="1200" placeholder="${esc(t("prof_bio_ph"))}">${esc(p.bio)}</textarea></label>
    <div class="section-title" style="margin:18px 0 8px">${esc(t("prof_contacts"))}</div>
    <label class="field"><span>${esc(t("prof_email"))}</span>
      <input class="input" value="${esc(p.email)}" readonly></label>
    <label class="field"><span>${esc(t("prof_phone"))}</span>
      <input class="input" id="pfPhone" maxlength="40" value="${esc(p.phone)}"></label>
    <div class="section-title" style="margin:18px 0 8px">${esc(t("prof_links"))}</div>
    <p class="dim" style="font-size:12px;margin-bottom:8px">${esc(t("prof_links_hint"))}</p>
    ${linkRows(p.links, true)}`;

  const bodyCompany = `
    ${photoBlock}
    ${standingPanel(c)}
    <label class="field"><span>${esc(t("prof_co_name"))}</span>
      <input class="input" value="${esc(c.name || "")}" readonly></label>
    ${c.reg_number ? `<label class="field"><span>${esc(t("d_reg"))}</span>
      <input class="input" value="${esc(c.reg_number)}" readonly></label>` : ""}
    <label class="field"><span>${esc(t("pf_posture"))}</span>
      <select class="input" id="pfPosture"${canEdit ? "" : " disabled"}>
        ${["hiring", "seeking", "both"].map((p) =>
          `<option value="${p}"${c.posture === p ? " selected" : ""}>${esc(t("pf_posture_" + p))}</option>`).join("")}
      </select></label>
    <p class="dim" style="font-size:12px;margin:-4px 0 12px">${esc(t("pf_posture_hint"))}</p>
    <label class="field"><span>${esc(t("pf_headline"))}</span>
      <input class="input" id="pfHeadline" maxlength="120" placeholder="${esc(t("pf_headline_ph"))}" value="${
        esc(c.headline || "")}"${canEdit ? "" : " readonly"}></label>
    <p class="dim" style="font-size:12px;margin:-4px 0 12px">${esc(t("pf_headline_hint"))}</p>
    <div class="grid2">
      <label class="field"><span>${esc(t("pf_size"))}</span>
        <select class="input" id="pfSize"${canEdit ? "" : " disabled"}>
          <option value="">${esc(t("dir_any"))}</option>
          ${["solo", "small", "medium", "large"].map((s) =>
            `<option value="${s}"${c.size_band === s ? " selected" : ""}>${esc(t("pf_size_" + s))}</option>`).join("")}
        </select></label>
      <label class="field"><span>${esc(t("pf_founded"))}</span>
        <input class="input" id="pfFounded" maxlength="4" inputmode="numeric" placeholder="2014" value="${
          esc(c.founded || "")}"${canEdit ? "" : " readonly"}></label>
    </div>
    <label class="field"><span>${esc(t("pf_areas"))}</span>
      <input class="input" id="pfAreas" maxlength="200" placeholder="${esc(t("pf_areas_ph"))}" value="${
        esc((c.areas || []).join(", "))}"${canEdit ? "" : " readonly"}></label>
    <label class="field"><span>${esc(t("prof_about"))}</span>
      <textarea class="input" id="pfAbout" rows="5" maxlength="2000" placeholder="${esc(t("prof_about_ph"))}"${
        canEdit ? "" : " readonly"}>${esc(c.about || "")}</textarea></label>
    <label class="field"><span>${esc(t("prof_website"))}</span>
      <input class="input" id="pfSite" maxlength="300" placeholder="https://" value="${esc(c.website || "")}"${
        canEdit ? "" : " readonly"}></label>
    <div class="section-title" style="margin:18px 0 8px">${esc(t("prof_sectors"))}</div>
    <div class="pf-sectors">${(c.sectors || []).length
      ? (c.sectors || []).map((s) => `<span class="doc-tag">${sectorChip(s)}</span>`).join("")
      : `<span class="dim" style="font-size:12.5px">${esc(t("sec_none_yet"))}</span>`}
      <button class="btn ghost sm" id="pfSectors">${esc(t("sec_change"))}</button></div>
    <div class="section-title" style="margin:18px 0 8px">${esc(t("prof_contacts"))}</div>
    <label class="field"><span>${esc(t("pf_office_email"))}</span>
      <input class="input" id="pfCoEmail" maxlength="254" placeholder="${esc(t("pf_office_email_ph"))}" value="${
        esc(c.contact_email || "")}"${canEdit ? "" : " readonly"}></label>
    <p class="dim" style="font-size:12px;margin:-4px 0 12px">${esc(t("pf_office_email_hint"))}</p>
    <div class="section-title" style="margin:18px 0 8px">${esc(t("pf_portfolio"))}</div>
    <p class="dim" style="font-size:12px;margin-bottom:8px">${esc(t("pf_portfolio_hint"))}</p>
    <button class="btn ghost sm" id="pfWorks">${esc(t("pf_works_open", { n: c.portfolio_count || 0 }))}</button>
    <div class="section-title" style="margin:18px 0 8px">${esc(t("prof_links"))}</div>
    <p class="dim" style="font-size:12px;margin-bottom:8px">${esc(t("prof_links_hint_co"))}</p>
    ${linkRows(c.links, canEdit)}
    ${canEdit ? "" : `<p class="dim mt12" style="font-size:12px">${esc(t("prof_readonly"))}</p>`}`;

  openModal(t("prof_title_modal"), tabs + (isCompany ? bodyCompany : bodyPerson),
    `<button class="btn ghost" data-close>${esc(t("close"))}</button>
     ${canEdit ? `<button class="btn primary" id="pfSave">${esc(t("save"))}</button>` : ""}`);

  $$("[data-pf-tab]").forEach((b) => b.addEventListener("click", () => openProfile(b.dataset.pfTab)));

  const add = $("#pfLinkAdd");
  if (add) add.addEventListener("click", () => {
    $("#pfLinks").insertAdjacentHTML("beforeend", `
      <div class="pf-link">
        <input class="input" data-link-label placeholder="${esc(t("prof_link_label"))}">
        <input class="input" data-link-url placeholder="https://">
        <button class="btn ghost sm" data-link-del="x">✕</button>
      </div>`);
    wireLinkDelete();
  });
  function wireLinkDelete() {
    $$("[data-link-del]").forEach((b) => {
      b.onclick = () => b.closest(".pf-link").remove();
    });
  }
  wireLinkDelete();

  const sec = $("#pfSectors");
  if (sec) sec.addEventListener("click", () => openSectorChooser(false));
  const works = $("#pfWorks");
  if (works) works.addEventListener("click", openPortfolio);

  const pick = $("#pfPhoto");
  if (pick) pick.addEventListener("change", async () => {
    const f = pick.files && pick.files[0];
    if (!f) return;
    try {
      await API.profilePhoto(isCompany ? "company" : "person", f);
      toast(t("saved"), "ok");
      openProfile(profileTab);
    } catch (err) { toast(errMsg(err), "err"); }
  });
  const del = $("#pfPhotoDel");
  if (del) del.addEventListener("click", async () => {
    try { await API.profilePhotoClear(isCompany ? "company" : "person"); openProfile(profileTab); }
    catch (err) { toast(errMsg(err), "err"); }
  });

  const save = $("#pfSave");
  if (save) save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      if (isCompany) {
        await API.profileCompanySet({
          about: $("#pfAbout").value, website: $("#pfSite").value, links: readLinks(),
          posture: $("#pfPosture").value, headline: $("#pfHeadline").value,
          size_band: $("#pfSize").value, founded: $("#pfFounded").value,
          contact_email: $("#pfCoEmail").value,
          areas: $("#pfAreas").value.split(",").map((s) => s.trim()).filter(Boolean),
        });
      } else {
        await API.profileSet({
          title: $("#pfTitle").value, bio: $("#pfBio").value,
          phone: $("#pfPhone").value, links: readLinks(),
        });
      }
      toast(t("saved"), "ok");
      closeModal();
      render();
    } catch (err) { toast(errMsg(err), "err"); save.disabled = false; }
  });
}

/* Somebody else's card, opened from the team list or a workspace. */
async function openPersonCard(uid) {
  let p;
  try { p = await API.profileOf(uid); } catch (err) { return toast(errMsg(err), "err"); }
  const c = p.company || {};
  const link = (l) => `<a class="pf-out" href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${
    esc(l.label || l.url)} ↗</a>`;
  openModal(p.name, `
    <div class="pf-head">${avatarBox(p.avatar, p.name)}
      <div><b style="font-size:16px">${esc(p.name)}</b>
        ${p.title ? `<div class="dim">${esc(p.title)}</div>` : ""}
        ${c.name ? `<div class="dim" style="font-size:12.5px">${esc(c.name)}${
          c.verified ? ` · ${esc(t("account_verified"))}` : ""}</div>` : ""}</div></div>
    ${p.bio ? `<p style="font-size:13.5px;white-space:pre-wrap">${esc(p.bio)}</p>` : ""}
    <div class="section-title" style="margin:18px 0 8px">${esc(t("prof_contacts"))}</div>
    <div class="an-rows">
      <div class="an-row"><div class="grow"><b>${esc(t("prof_email"))}</b>
        <div class="dim">${esc(p.email)}</div></div></div>
      ${p.phone ? `<div class="an-row"><div class="grow"><b>${esc(t("prof_phone"))}</b>
        <div class="dim">${esc(p.phone)}</div></div></div>` : ""}
    </div>
    ${(p.links || []).length ? `<div class="section-title" style="margin:18px 0 8px">${esc(t("prof_links"))}</div>
      <div class="pf-outs">${p.links.map(link).join("")}</div>` : ""}
    ${c.about ? `<div class="section-title" style="margin:18px 0 8px">${esc(t("prof_company"))}</div>
      <p style="font-size:13px;white-space:pre-wrap">${esc(c.about)}</p>` : ""}
    ${c.website ? `<div class="pf-outs">${link({ label: c.website, url: c.website })}</div>` : ""}
    ${(c.links || []).length ? `<div class="pf-outs mt8">${c.links.map(link).join("")}</div>` : ""}`,
    `<button class="btn ghost" data-close>${esc(t("close"))}</button>`);
}

/* ---- mail ----------------------------------------------------------------
 * The setting the rest depends on. Without it an invitation goes nowhere, a
 * forgotten password cannot be recovered by the person who forgot it, and the
 * advanced signature level has no mailbox to verify. */
/* The providers people actually use, with the settings they publish. Nobody
   should have to look up a port number to make invitations work. `note` says
   what the password field wants, because for most of these it is not the
   mailbox password - and that is the step everyone gets wrong. */
const SMTP_PRESETS = [
  // Bulgarian mailboxes first: this is where most of these companies are, and
  // both of these want TLS from the first byte on 465 rather than STARTTLS.
  { id: "abv", name: "ABV.bg", host: "smtp.abv.bg", port: 465, ssl: true, note: "sm_p_abv" },
  { id: "mailbg", name: "Mail.bg", host: "smtp.mail.bg", port: 465, ssl: true, note: "sm_p_app" },
  { id: "icloud", name: "iCloud Mail (Apple)", host: "smtp.mail.me.com", port: 587,
    tls: true, note: "sm_p_icloud" },
  { id: "gmail", name: "Gmail / Google Workspace", host: "smtp.gmail.com", port: 587,
    tls: true, note: "sm_p_gmail" },
  { id: "outlook", name: "Microsoft 365 / Outlook", host: "smtp.office365.com", port: 587,
    tls: true, note: "sm_p_ms" },
  { id: "zoho", name: "Zoho Mail", host: "smtp.zoho.eu", port: 587, tls: true, note: "sm_p_app" },
  { id: "brevo", name: "Brevo", host: "smtp-relay.brevo.com", port: 587, tls: true, note: "sm_p_key" },
  { id: "mailgun", name: "Mailgun", host: "smtp.eu.mailgun.org", port: 587, tls: true, note: "sm_p_key" },
  { id: "sendgrid", name: "SendGrid", host: "smtp.sendgrid.net", port: 587, tls: true,
    note: "sm_p_sendgrid" },
  { id: "postmark", name: "Postmark", host: "smtp.postmarkapp.com", port: 587, tls: true, note: "sm_p_key" },
  // live.smtp, not sandbox.smtp: the sandbox accepts everything and delivers
  // nothing, so a password reset would look sent and never arrive.
  { id: "mailtrap", name: "Mailtrap", host: "live.smtp.mailtrap.io", port: 587, tls: true,
    user: "api", note: "sm_p_mailtrap" },
  { id: "superhosting", name: "SuperHosting / cPanel", host: "mail.example.com", port: 587,
    tls: true, note: "sm_p_own" },
];

async function openMail() {
  let s;
  try { s = await API.smtp(); } catch (err) { return toast(errMsg(err), "err"); }

  openModal(t("sm_title"), `
    <div class="flex gap8 center wrap" style="margin-bottom:12px">
      <span class="badge ${s.enabled ? "ok" : "warn"} dot">${esc(s.enabled ? t("sm_on") : t("sm_off"))}</span>
      ${s.from_env ? `<span class="badge">${esc(t("sm_from_env"))}</span>` : ""}
    </div>
    <p class="dim" style="font-size:12.5px">${esc(t("sm_hint"))}</p>
    ${s.from_env ? `<p class="dim mt12" style="font-size:12.5px">${esc(t("sm_env_note"))}</p>` : `
      <div class="section-title" style="margin:16px 0 8px">${esc(t("sm_provider"))}</div>
      <div class="sector-filter">${SMTP_PRESETS.map((p) =>
        `<button type="button" class="sec-chip" data-smtp-preset="${esc(p.id)}">${esc(p.name)}</button>`
      ).join("")}</div>
      <p class="dim" id="smNote" style="font-size:12px;min-height:16px"></p>
      <div class="row2 mt12">
        <label class="field"><span>${esc(t("sm_host"))}</span>
          <input class="input" id="smHost" value="${esc(s.host || "")}" placeholder="smtp.example.com" /></label>
        <label class="field"><span>${esc(t("sm_port"))}</span>
          <input class="input" id="smPort" type="number" min="1" max="65535" value="${esc(String(s.port || 587))}" /></label>
      </div>
      <div class="row2">
        <label class="field"><span>${esc(t("sm_user"))}</span>
          <input class="input" id="smUser" value="${esc(s.user || "")}" autocomplete="off" /></label>
        <label class="field"><span>${esc(t("sm_pass"))}</span>
          <input class="input" id="smPass" type="password" autocomplete="new-password"
            placeholder="${esc(s.has_password ? t("sm_pass_kept") : "")}" /></label>
      </div>
      <label class="field"><span>${esc(t("sm_sender"))}</span>
        <input class="input" id="smSender" value="${esc(s.sender || "")}" placeholder="seam@example.com" />
        <span class="dim" id="smFromWarn" style="font-size:11.5px;color:#c9851a"></span></label>
      <label class="field"><span>${esc(t("sm_enc"))}</span>
        <select class="select" id="smEnc">
          <option value="starttls"${!s.ssl && s.tls !== false ? " selected" : ""}>${esc(t("sm_enc_starttls"))}</option>
          <option value="ssl"${s.ssl ? " selected" : ""}>${esc(t("sm_enc_ssl"))}</option>
          <option value="none"${!s.ssl && s.tls === false ? " selected" : ""}>${esc(t("sm_enc_none"))}</option>
        </select></label>
      <p class="dim" style="font-size:11.5px">${esc(t("sm_tls_note"))}</p>
      <div class="flex gap8 wrap mt12">
        <button class="btn primary sm" id="smSave">${esc(t("save"))}</button>
        <button class="btn ghost sm" id="smTest" ${s.enabled ? "" : "disabled"}>${esc(t("sm_test"))}</button>
      </div>`}`,
    `<button class="btn ghost" data-close>${esc(t("close"))}</button>`);

  // Filling the fields in, not saving: the person still sees what they are
  // about to store, and a preset with the wrong regional host is caught before
  // it becomes a setting that silently never delivers.
  $$("[data-smtp-preset]").forEach((b) => b.addEventListener("click", () => {
    const p = SMTP_PRESETS.find((x) => x.id === b.dataset.smtpPreset);
    if (!p) return;
    $$("[data-smtp-preset]").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    $("#smHost").value = p.host;
    $("#smPort").value = p.port;
    $("#smEnc").value = p.ssl ? "ssl" : (p.tls === false ? "none" : "starttls");
    $("#smNote").textContent = t(p.note);
    // A few providers want a fixed user name rather than the mailbox: filling
    // it in is one less thing to mistype, and "api" does not look like a user
    // name so it is the kind of thing people change back.
    if (p.user) $("#smUser").value = p.user;
    if (!$("#smSender").value && $("#smUser").value && $("#smUser").value.includes("@")) {
      $("#smSender").value = $("#smUser").value;
    }
    (p.user ? $("#smPass") : $("#smUser")).focus();
  }));

  // The commonest failure after a wrong password: authenticating as one
  // mailbox and putting a different address in From. ABV and iCloud reject
  // that outright. Only warned about when the user name is itself an address -
  // for SendGrid the user is literally "apikey" and any sender is fine.
  const fromWarn = () => {
    const el = $("#smFromWarn");
    if (!el) return;
    const u = ($("#smUser").value || "").trim().toLowerCase();
    const f = ($("#smSender").value || "").trim().toLowerCase();
    const dom = (x) => (x.indexOf("@") !== -1 ? x.split("@")[1] : "");
    el.textContent = (dom(u) && dom(f) && dom(u) !== dom(f)) ? t("sm_from_warn") : "";
  };
  ["#smUser", "#smSender"].forEach((sel) => {
    const e = $(sel);
    if (e) e.addEventListener("input", fromWarn);
  });
  fromWarn();

  const save = $("#smSave");
  if (save) save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      const enc = $("#smEnc").value;
      await API.smtpSave({
        host: $("#smHost").value.trim(), port: $("#smPort").value.trim(),
        user: $("#smUser").value.trim(), password: $("#smPass").value,
        sender: $("#smSender").value.trim(),
        tls: enc === "starttls", ssl: enc === "ssl",
      });
      toast(t("saved"), "ok");
      openMail();
    } catch (err) { toast(errMsg(err), "err"); save.disabled = false; }
  });
  const test = $("#smTest");
  if (test) test.addEventListener("click", async () => {
    test.disabled = true;
    try {
      const r = await API.smtpTest("");
      toast(t("sm_sent", { to: r.to }), "ok");
    } catch (err) { toast(errMsg(err), "err"); }
    test.disabled = false;
  });
}

/* ---- payments that need a person ----------------------------------------
 * A card payment activates itself: the processor signs a webhook and Seam
 * believes the signature. A bank transfer cannot do that, so the customer's
 * "I have paid" is a claim, and whoever runs this installation decides it
 * against the statement they can actually see. */
async function openBilling(status) {
  let d;
  try { d = await API.billingClaims(status || "pending"); }
  catch (err) { return toast(errMsg(err), "err"); }

  const tabs = ["pending", "approved", "rejected", "all"];
  const cur = status || "pending";
  const rows = d.claims.length ? d.claims.map((c) => `
    <div class="an-row">
      <div class="grow">
        <b>${esc(c.org || ("#" + c.org_id))}</b>
        <span class="badge">${esc(c.kind === "template" ? t("bill_k_template") : t("bill_k_plan"))}</span>
        ${c.amount ? `<span class="badge accent">${esc(displayMoneyText(c.amount))}</span>` : ""}
        <div class="dim" style="font-size:11.5px">${esc(relTime(c.created_at))}${
          c.note ? " · " + esc(c.note) : ""}${
          c.decided_at ? " · " + esc(t("bill_decided", { when: relTime(c.decided_at) })) : ""}</div>
      </div>
      ${c.status === "pending" ? `
        <button class="btn primary sm" data-approve="${c.id}">${esc(t("bill_approve"))}</button>
        <button class="btn ghost sm" data-reject="${c.id}">${esc(t("bill_reject"))}</button>`
      : `<span class="badge ${c.status === "approved" ? "ok" : "danger"}">${
          esc(t("bill_st_" + c.status))}</span>`}
    </div>`).join("") : `<p class="muted" style="font-size:13px">${esc(t("bill_none"))}</p>`;

  // If neither webhook is configured, every card payment will also land here as
  // a claim. Better to say it than to let the operator wonder.
  const warn = (!d.stripe_webhook && !d.lemon_webhook)
    ? `<div class="card pad" style="background:#fdf3e0;border-color:#e8d5a8;margin-bottom:14px">
        <b>${esc(t("bill_no_webhook"))}</b>
        <p class="dim mt8" style="font-size:12.5px">${esc(t("bill_no_webhook_hint"))}</p></div>` : "";

  openModal(t("bill_title"), `
    ${warn}
    <div class="flex gap8 wrap" style="margin-bottom:12px">${tabs.map((s) =>
      `<button class="btn ${s === cur ? "primary" : "ghost"} sm" data-bill-tab="${s}">${
        esc(t("bill_tab_" + s))}</button>`).join("")}</div>
    <div class="an-rows" style="max-height:340px;overflow:auto">${rows}</div>
    <div class="section-title" style="margin:20px 0 10px">${esc(t("bill_ledger"))}</div>
    <div class="an-rows" style="max-height:200px;overflow:auto">${d.ledger.length ? d.ledger.map((l) => `
      <div class="an-row"><div class="grow">
        <b>${esc(l.provider)}</b> <span class="dim">${esc(l.event)}</span>
        <div class="dim mono" style="font-size:11px">${esc(l.result)}${
          l.org_id ? " · #" + l.org_id : ""} · ${relTime(l.at)}</div>
      </div></div>`).join("") : `<p class="muted" style="font-size:13px">${esc(t("bill_no_events"))}</p>`}</div>`,
    `<button class="btn ghost" data-close>${esc(t("close"))}</button>`);

  $$("[data-bill-tab]").forEach((b) =>
    b.addEventListener("click", () => openBilling(b.dataset.billTab)));
  const decide = async (id, what) => {
    try { await API.billingDecide(id, what); toast(t("bill_saved"), "ok"); openBilling(cur); }
    catch (err) { toast(errMsg(err), "err"); }
  };
  $$("[data-approve]").forEach((b) =>
    b.addEventListener("click", () => decide(b.dataset.approve, "approve")));
  $$("[data-reject]").forEach((b) =>
    b.addEventListener("click", () => decide(b.dataset.reject, "reject")));
}

/* ---- the team inside one company ---------------------------------------- *
 * A company is not one login. Colleagues are added with a role and set their
 * own password through a single-use link, so nobody types a password for
 * someone else. */
async function openTeam() {
  let s;
  try { s = await API.team(); } catch (err) { return toast(errMsg(err), "err"); }
  const canManage = s.my_role === "owner" || s.my_role === "admin";
  const roleOpts = (cur, id) => ["admin", "member", "viewer"].map((r) =>
    `<option value="${r}"${cur === r ? " selected" : ""}>${esc(t("tm_role_" + r))}</option>`).join("");

  const rows = s.members.map((m) => `
    <div class="au-rule">
      <button class="pf-tap" data-person="${m.id}" title="${esc(t("prof_open_card"))}">${
        avatarBox(m.avatar, m.name, 34)}</button>
      <div class="grow"><b>${esc(m.name)}${m.is_me ? ` · <span class="dim">${esc(t("tm_you"))}</span>` : ""}</b>
        <div class="dim" style="font-size:11.5px">${esc(m.title || m.email)}</div></div>
      ${m.role === "owner" || !canManage || m.is_me
        ? `<span class="badge ${m.role === "owner" ? "ok" : ""}">${esc(t("tm_role_" + m.role))}</span>`
        : `<select class="select" data-tm-role="${m.id}" style="max-width:150px">${roleOpts(m.role, m.id)}</select>
           <button class="btn ghost sm" data-tm-del="${m.id}">${esc(t("tm_remove"))}</button>`}
    </div>`).join("");

  openModal(t("tm_title"), `
    <p class="dim" style="font-size:12.5px">${esc(t("tm_hint"))}</p>
    <div class="mt12">${rows}</div>
    ${canManage ? `
      <div class="section-title" style="margin:20px 0 10px">${esc(t("tm_invite"))}</div>
      <div class="grid2">
        <label class="field"><span>${esc(t("tm_name"))} *</span><input class="input" id="tmName" /></label>
        <label class="field"><span>${esc(t("tm_email"))} *</span><input class="input" id="tmEmail" type="email" /></label>
      </div>
      <label class="field"><span>${esc(t("tm_role"))}</span>
        <select class="select" id="tmRole">${roleOpts("member")}</select></label>
      <div class="dim" style="font-size:11.5px">${esc(t("tm_invite_note"))}</div>
      <button class="btn primary sm mt12" id="tmSend">${esc(t("tm_invite"))}</button>
      <div id="tmLink" class="mt12"></div>` : ""}`,
    `<button class="btn ghost" data-close>${esc(t("close"))}</button>`);

  $$("[data-person]").forEach((b) => b.addEventListener("click", () => openPersonCard(b.dataset.person)));
  $$("[data-tm-role]").forEach((sel) => sel.addEventListener("change", async () => {
    try { await API.teamRole(sel.dataset.tmRole, sel.value); toast(t("saved"), "ok"); openTeam(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-tm-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(t("tm_remove_ask"))) return;
    try { await API.teamRemove(b.dataset.tmDel); toast(t("saved"), "ok"); openTeam(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  const send = $("#tmSend");
  if (send) send.addEventListener("click", async () => {
    send.disabled = true;
    try {
      const r = await API.teamInvite({
        name: $("#tmName").value.trim(), email: $("#tmEmail").value.trim(),
        role: $("#tmRole").value,
      });
      toast(t("saved"), "ok");
      // Without SMTP the link is shown here rather than silently going nowhere.
      if (r.invite_link) {
        $("#tmLink").innerHTML = `<div class="hashrow"><code>${esc(r.invite_link)}</code>
          <button id="tmCopy">${esc(t("int_copy"))}</button></div>
          <div class="dim" style="font-size:11.5px;margin-top:6px">${esc(t("tm_link_note"))}</div>`;
        $("#tmCopy").addEventListener("click", async () => {
          try { await navigator.clipboard.writeText(r.invite_link); toast(t("int_copied"), "ok"); }
          catch (e) { toast(errMsg(e), "err"); }
        });
        $("#tmName").value = ""; $("#tmEmail").value = "";
      } else { openTeam(); }
    } catch (err) { toast(errMsg(err), "err"); }
    send.disabled = false;
  });
}

/* Where each person wants to hear about things. In-app is always on because it
   is the record; the rest is per user. */
async function openNotifySettings() {
  let s;
  try { s = await API.notifySettings(); } catch (err) { return toast(errMsg(err), "err"); }
  const isBiz = State.org && State.org.kind === "business";
  const perm = ("Notification" in window) ? Notification.permission : "unsupported";
  const smsCfg = s.sms || {};

  openModal(t("nt_settings"), `
    <p class="dim" style="font-size:12.5px">${esc(t("nt_hint"))}</p>
    <label class="field checkbox-field"><input type="checkbox" id="ntInapp" checked disabled />
      <span>${esc(t("nt_ch_inapp"))} · <span class="dim">${esc(t("nt_always"))}</span></span></label>
    <label class="field checkbox-field"><input type="checkbox" id="ntEmail"${s.channels.email ? " checked" : ""}${s.available.email ? "" : " disabled"} />
      <span>${esc(t("nt_ch_email"))}${s.available.email ? "" : ` · <span class="dim">${esc(t("nt_unavailable"))}</span>`}</span></label>
    <label class="field checkbox-field"><input type="checkbox" id="ntSms"${s.channels.sms ? " checked" : ""}${s.available.sms ? "" : " disabled"} />
      <span>${esc(t("nt_ch_sms"))}${s.available.sms ? "" : ` · <span class="dim">${esc(t("nt_unavailable"))}</span>`}</span></label>
    <label class="field"><span>${esc(t("nt_phone"))}</span>
      <input class="input" id="ntPhone" placeholder="+359…" value="${esc(s.phone)}" /></label>
    <label class="field checkbox-field"><input type="checkbox" id="ntPush"${s.channels.push ? " checked" : ""} />
      <span>${esc(t("nt_ch_push"))}</span></label>
    <div class="dim" style="font-size:11.5px;margin-top:-6px">${esc(t("nt_push_note"))}${
      perm === "denied" ? ` · <b>${esc(t("nt_push_blocked"))}</b>` : ""}</div>
    <div class="flex gap8 center wrap" style="margin-top:8px">
      <span class="badge ${(s.push || {}).devices ? "ok" : ""}">${esc(t("nt_push_devices", { n: (s.push || {}).devices || 0 }))}</span>
      <button class="btn ghost sm" id="ntPushTest">${esc(t("nt_push_test"))}</button>
    </div>

    ${isBiz ? `<div class="section-title" style="margin:20px 0 10px">${esc(t("nt_gateway"))}</div>
      <p class="dim" style="font-size:12.5px">${esc(t("nt_gateway_hint"))}</p>
      <label class="field"><span>${esc(t("nt_provider"))}</span>
        <select class="select" id="ntProv"><option value="">${esc(t("qt_none"))}</option>
          ${s.sms_providers.map((p) => `<option value="${p}"${smsCfg.provider === p ? " selected" : ""}>${esc(t("nt_prov_" + p))}</option>`).join("")}
        </select></label>
      <div id="ntProvFields"></div>
      <div class="flex gap8 mt12">
        <button class="btn ghost sm" id="ntSaveGw">${esc(t("save"))}</button>
        <button class="btn ghost sm" id="ntTestSms">${esc(t("nt_send_test"))}</button>
      </div>` : ""}`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
     <button class="btn primary" id="ntSave">${esc(t("save"))}</button>`);

  const FIELDS = {
    twilio: [["account_sid", "Account SID"], ["auth_token", "Auth token"], ["from", t("nt_from")]],
    bearer: [["url", "URL"], ["api_key", "API key"], ["from", t("nt_from")]],
    form: [["url", "URL"], ["to_field", t("nt_to_field")], ["text_field", t("nt_text_field")],
           ["username", t("nt_username")], ["password", t("nt_password")]],
  };
  const drawGw = () => {
    const box = $("#ntProvFields");
    if (!box) return;
    const p = $("#ntProv").value;
    box.innerHTML = !p ? "" : FIELDS[p].map(([k, label]) => {
      const stored = (smsCfg.secrets_set || {})[k];
      const val = (smsCfg.provider === p && !stored) ? (smsCfg[k] || "") : "";
      const secret = k === "auth_token" || k === "api_key" || k === "password";
      return `<label class="field"><span>${esc(label)}</span>
        <input class="input" data-gw="${k}" type="${secret ? "password" : "text"}" autocomplete="off"
          value="${esc(val)}" placeholder="${stored ? esc(t("qt_stored")) : ""}" /></label>`;
    }).join("");
  };
  if ($("#ntProv")) { $("#ntProv").addEventListener("change", drawGw); drawGw(); }

  const saveGw = async () => {
    const payload = { provider: $("#ntProv").value };
    $$("[data-gw]").forEach((i) => { payload[i.dataset.gw] = i.value.trim(); });
    try { await API.smsConfig(payload); toast(t("saved"), "ok"); }
    catch (err) { toast(errMsg(err), "err"); }
  };
  if ($("#ntSaveGw")) $("#ntSaveGw").addEventListener("click", saveGw);
  $("#ntPushTest").addEventListener("click", async () => {
    if ((await askPushPermission()) !== "granted") return toast(t("nt_push_denied"), "err");
    try { const r = await API.pushTest(); toast(t("nt_push_sent", { n: r.sent }), "ok"); }
    catch (err) { toast(errMsg(err), "err"); }
  });
  if ($("#ntTestSms")) $("#ntTestSms").addEventListener("click", async () => {
    try { const r = await API.smsTest($("#ntPhone").value.trim()); toast(t("nt_test_ok") + " " + (r.detail || ""), "ok"); }
    catch (err) { toast(errMsg(err), "err"); }
  });

  $("#ntSave").addEventListener("click", async () => {
    const push = $("#ntPush").checked;
    if (push) {
      const p = await askPushPermission();
      if (p !== "granted") { $("#ntPush").checked = false; toast(t("nt_push_denied"), "err"); }
    }
    State.pushOn = $("#ntPush").checked;
    if (!State.pushOn) { try { await API.pushUnsubscribe(""); } catch {} }
    try {
      await API.notifySettingsSave({
        channels: { email: $("#ntEmail").checked, sms: $("#ntSms").checked, push: State.pushOn },
        phone: $("#ntPhone").value.trim(),
      });
      closeModal(); toast(t("saved"), "ok");
    } catch (err) { toast(errMsg(err), "err"); }
  });
}

async function toggleNotif(e) {
  e.stopPropagation();
  if ($("#overlay-root").querySelector(".dropdown")) { closeOverlay(); return; }
  await refreshNotif();
  const items = State.notif.items;
  const list = items.length ? items.map(notifRow).join("") :
    `<div class="empty" style="padding:34px"><p>${esc(t("notif_empty"))}</p></div>`;
  $("#overlay-root").innerHTML = `<div class="dropdown">
    <div class="dd-head"><span>${esc(t("notifications"))}</span>${State.notif.unread ? `<button class="btn ghost sm" id="readAll">${esc(t("mark_read"))}</button>` : ""}</div>
    <div class="dd-list">${list}</div></div>`;
  $$("#overlay-root .nitem").forEach((n) => n.addEventListener("click", () => {
    const oid = n.dataset.oid, wid = n.dataset.wid;
    closeOverlay();
    if (oid) go(`/o/${oid}`);
    else if (wid) go(`/w/${wid}`);
    else if ((n.dataset.kind || "").startsWith("store")) go("/store");
    else if ((n.dataset.kind || "").startsWith("template")) go("/templates");
    else if (n.dataset.kind === "contact_request") go("/directory");
    else if (/^(intro_|need_)/.test(n.dataset.kind || "")) go("/network");
    API.markRead([+n.dataset.id]).then(refreshBadge);
  }));
  const ra = $("#readAll");
  if (ra) ra.addEventListener("click", async (ev) => { ev.stopPropagation(); await API.markRead(); await refreshNotif(); toggleNotif({ stopPropagation() {} }); refreshBadge(); });
}

function notifIcon(kind) {
  return ({ status_changed: "→", order_created: "🆕", term_proposed: "✎", term_agreed: "✓", comment: "💬", partner_joined: "🤝", invite: "✉", field_updated: "✎", store_incoming: "🛒", template_paid: "💳", attachment_added: "📷", ws_message: "💬", invbg_invoice: "🧾", signature_signed: "✍️", document_ready: "📄", automation: "⚡",
    approval_decided: "✔", payout_recorded: "💶", intro_request: "🤝", intro_decided: "🤝",
    need_reply: "🕸" }[kind]) || "•";
}
function notifRow(n) {
  return `<div class="nitem ${n.read ? "" : "unread"}" data-id="${n.id}" data-kind="${esc(n.kind || "")}" data-oid="${n.order_id || ""}" data-wid="${n.workspace_id || ""}">
    <div class="ni-ico">${notifIcon(n.kind)}</div>
    <div class="ni-body">${esc(notifText(n))}<div class="ni-time">${relTime(n.created_at)}</div></div>
  </div>`;
}

/* A calendar day in the reader's locale. The agreement alerts carry an ISO
   date and a Bulgarian reader should not be shown an American one. */
function fmtDay(iso) {
  const d = new Date((iso || "") + "T00:00:00");
  if (isNaN(d)) return iso || "";
  try { return d.toLocaleDateString(LANG, { day: "numeric", month: "short", year: "numeric" }); }
  catch { return iso; }
}

function notifText(n) {
  let meta = {}; try { meta = JSON.parse(n.meta_json || "{}"); } catch {}
  const tk = meta.tpl;
  const ref = meta.ref || n.order_ref || "";
  switch (n.kind) {
    case "order_created": return t("nt_order_created", { title: meta.title || "", ref });
    case "status_changed": return t("nt_status_changed", { ref, status: tplStage(tk, meta.to, meta.to) });
    case "term_proposed": return t("nt_term_proposed", { ref, term: tplField(tk, meta.term, meta.term) });
    case "term_agreed": return t("nt_term_agreed", { ref, term: tplField(tk, meta.term, meta.term) });
    // What an agreement does after it is made. The term is named by its own
    // label in the reader's language, not by the key it is stored under.
    case "term_due_soon":
    case "term_due_today":
    case "term_overdue":
    case "term_review":
      return t("nt_" + n.kind, { ref, term: tplField(tk, meta.term, meta.term),
                                 date: fmtDay(meta.date) });
    case "payment_received": return t("nt_payment_received", { ref });
    case "comment": return t("nt_comment", { ref, by: meta.by || "" });
    case "field_updated": return t("nt_field_updated", { ref });
    case "partner_joined": return t("nt_partner_joined", { name: meta.name || "" });
    case "invite": return t("nt_invite", { ws: meta.ws || "" });
    case "store_incoming": return t("nt_store_incoming", { ref: meta.ref || ref, product: meta.product || "" });
    case "template_paid": return t("nt_template_paid", { label: meta.label || "" });
    case "attachment_added": return t("nt_attachment_added", { ref: meta.ref || ref });
    case "ws_message": return t("nt_ws_message", { ws: meta.ws || "", by: meta.by || "" });
    case "invbg_invoice": return t("nt_invbg_invoice", { ref: meta.ref || ref, number: meta.number || "" });
    case "signature_signed": return t("nt_signature_signed", { ref: meta.ref || ref, by: meta.by || "" });
    case "document_ready": return `${meta.ref || ref} · ${t("doc_" + meta.doc)} · ${t("nt_document_ready")}`;
    case "approval_decided": return t("nt_approval_decided", { ref, decision: t("ap_state_" + (meta.decision === "approved" ? "approved" : "rejected")) });
    case "payout_recorded": return t(meta.status === "paid" ? "nt_payout_paid" : "nt_payout_recorded", { amount: meta.amount || "" });
    case "contact_request": return t("nt_contact_request", { name: meta.name || "" });
    case "intro_request": return t("nt_intro_request", { name: meta.name || "" });
    case "intro_decided": return t("nt_intro_decided");
    case "need_reply": return t("nt_need_reply", { name: meta.name || "", title: meta.title || "" });
    default: return n.body || n.kind;
  }
}

/* ---- browser notifications ---------------------------------------------- *
 * The service worker raises an OS notification while Seam is open in a tab.
 * Real background push (phone closed) would need VAPID signing, which this
 * backend deliberately does not carry - the UI says so rather than implying
 * more than it delivers. */
async function registerSW() {
  if (!("serviceWorker" in navigator)) return null;
  if (State._sw) return State._sw;
  try {
    // Root path, so the worker's scope covers the whole app rather than /static/.
    State._sw = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    return State._sw;
  } catch { return null; }
}

/* Subscribe this browser to real Web Push. The server sends a payload-less
   tickle; the worker fetches the notification itself, so nothing about it
   travels through the push service. */
function urlB64ToUint8(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function subscribePush() {
  const reg = await registerSW();
  if (!reg || !("PushManager" in window)) return false;
  try {
    const ready = await navigator.serviceWorker.ready;
    let sub = await ready.pushManager.getSubscription();
    if (!sub) {
      const { key } = await API.pushKey();
      sub = await ready.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8(key),
      });
    }
    const j = sub.toJSON();
    await API.pushSubscribe({ endpoint: j.endpoint, keys: j.keys });
    return true;
  } catch { return false; }
}

async function askPushPermission() {
  if (!("Notification" in window)) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  if (Notification.permission !== "granted") {
    const p = await Notification.requestPermission();
    if (p !== "granted") return p;
  }
  await registerSW();
  await subscribePush();
  return "granted";
}

function pushNotify(title, body, url) {
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (!State.pushOn) return;
  // Use the registration we hold rather than serviceWorker.ready, which only
  // resolves once a worker controls this page - one navigation later.
  registerSW().then((reg) => {
    const w = reg && (reg.active || reg.waiting || reg.installing);
    if (w) w.postMessage({ type: "notify", title, body, url, tag: "seam" });
    else new Notification(title, { body, icon: "/static/icon.svg" });
  }).catch(() => {});
}

async function refreshNotif() {
  const seen = State.notif && State.notif.items ? State.notif.items.map((n) => n.id) : null;
  try { State.notif = await API.notifications(); } catch { return; }
  // Only announce items that arrived after this session started, so opening the
  // app never fires a burst of notifications for history.
  if (seen && document.hidden !== undefined) {
    (State.notif.items || []).filter((n) => !n.read && seen.indexOf(n.id) === -1)
      .slice(0, 3)
      .forEach((n) => pushNotify("Seam", notifText(n),
        "/#" + (n.order_id ? "/o/" + n.order_id : n.workspace_id ? "/w/" + n.workspace_id : "/")));
  }
}
function refreshBadge() {
  const b = $("#notifBtn"); if (!b) return;
  const u = State.notif.unread;
  b.classList.toggle("has-unread", !!u);
  b.innerHTML = `🔔${u ? `<span class="notif-dot">${u > 9 ? "9+" : u}</span>` : ""}`;
}

/* =========================================================================
   DASHBOARD
   ======================================================================== */
async function viewDashboard() {
  shell(crumb([{ label: t("home") }]), `<div class="page"><div class="loading-full"><div class="spin"></div></div></div>`);
  let workspaces = [], invites = [], pendingList = [];
  try { [workspaces, invites, pendingList] = await Promise.all([API.workspaces(), API.invites(), API.pending()]); }
  catch (err) { toast(errMsg(err), "err"); }

  const isBusiness = State.org && State.org.kind === "business";
  const verifyBanner = (State.user && !State.user.verified) ? `
    <div class="card pad mt8 verify-banner">
      <div class="flex between center wrap gap12">
        <div><b>${esc(t("verify_title"))}</b><div class="muted">${esc(t("verify_text"))}</div></div>
        <button class="btn primary" id="verifyBtn">✓ ${esc(t("verify_cta"))}</button>
      </div></div>` : "";
  const inviteBanner = invites.length ? `
    <div class="card pad mt8 invite-banner">
      <div class="flex between center wrap gap12">
        <div><b>${esc(t("invites_many", { n: invites.length }))}</b>
        <div class="muted">${invites.map((i) => esc(i.workspace_name)).join(", ")}</div></div>
        <div class="flex gap8 wrap">${invites.map((i) => `<button class="btn primary sm" data-accept="${i.token}">${esc(t("accept_ws", { name: i.workspace_name }))}</button>`).join("")}</div>
      </div></div>` : "";

  const grid = workspaces.length ? `<div class="ws-grid">${workspaces.map(wsCard).join("")}</div>` :
    emptyState("🧩", t("empty_ws_title"),
      isBusiness ? t("empty_ws_business") : t("empty_ws_partner"),
      isBusiness ? `<button class="btn primary" id="newWsEmpty"><span class="ico">＋</span> ${esc(t("new_ws"))}</button>` : "");

  const sub = workspaces.length ? t("ws_many", { n: workspaces.length }) : t("subtitle_default");
  shell(crumb([{ label: t("home") }]), `<div class="page">
    <div class="page-head">
      <div><h1>${esc(t("greeting", { name: State.user.name.split(" ")[0] }))}</h1>
      <div class="sub">${esc(sub)}</div></div>
      ${isBusiness ? `<button class="btn primary" id="newWs"><span class="ico">＋</span> ${esc(t("new_ws"))}</button>` : ""}
    </div>
    ${verifyBanner}${inviteBanner}
    ${pendingList.length ? `<div class="card pad mt8 pending-card">
      <b>⏳ ${esc(t("pending_title"))}</b>
      <div class="mt8">${pendingList.map((p) => `<div class="pend-row" data-po="${p.id}">
        <span class="mono dim">${esc(p.ref)}</span><b class="pr-t">${esc(p.title)}</b>
        <span class="dim pr-ws">${esc(p.workspace)}</span>
        <span class="badge ${p.days >= 3 ? "danger" : "warn"} dot">${esc(p.days > 0 ? t("pending_days", { n: p.days }) : t("pending_today"))}</span>
      </div>`).join("")}</div></div>` : ""}
    <div class="mt16">${grid}</div>
  </div>`);

  $$(".pend-row").forEach((r) => r.addEventListener("click", () => go(`/o/${r.dataset.po}`)));

  const vb = $("#verifyBtn");
  if (vb) vb.addEventListener("click", async () => {
    if (!State._verifyToken) return;
    try { await API.verify(State._verifyToken); toast(t("verify_done"), "ok"); await boot(); viewDashboard(); }
    catch (err) { toast(errMsg(err), "err"); }
  });

  $$("[data-accept]").forEach((b) => b.addEventListener("click", async () => {
    try { const r = await API.acceptInvite(b.dataset.accept); toast(t("joined"), "ok"); go(`/w/${r.workspace_id}`); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("#newWs, #newWsEmpty").forEach((b) => b && b.addEventListener("click", openCreateWorkspace));
  $$(".ws-card").forEach((c) => c.addEventListener("click", () => go(`/w/${c.dataset.id}`)));
  animateCounts();
}

function wsCard(w) {
  const c = w.counts;
  const partner = w.partner ? `<span>${esc(w.partner.name)}</span>` :
    (w.pending_invite ? `<span class="dim">${esc(t("invited", { email: w.pending_invite }))}</span>` : `<span class="dim">${esc(t("no_partner"))}</span>`);
  return `<div class="card ws-card" data-id="${w.id}">
    <div class="top">
      <div><h2>${esc(w.name)}</h2><div class="tpl">${esc(tplLabel(w.template, w.template_label))}</div></div>
      <span class="badge ${w.side}">${esc(w.side === "buyer" ? t("you_business") : t("you_partner"))}</span>
    </div>
    <div class="parties"><span>${esc(w.buyer.name)}</span><span class="vs seam-vs">↔</span>${partner}</div>
    <div class="ws-stats">
      <div class="s"><span class="n" data-count="${c.open}">0</span><span class="l">${esc(t("stat_open"))}</span></div>
      <div class="s ${c.awaiting_you ? "alert" : ""}"><span class="n" data-count="${c.awaiting_you}">0</span><span class="l">${esc(t("stat_for_you"))}</span></div>
      <div class="s"><span class="n" data-count="${c.total}">0</span><span class="l">${esc(t("stat_total"))}</span></div>
    </div>
  </div>`;
}

function animateCounts() {
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    $$(".ws-stats .n[data-count]").forEach((el) => (el.textContent = el.dataset.count));
    return;
  }
  $$(".ws-stats .n[data-count]").forEach((el) => {
    const target = +el.dataset.count || 0;
    if (target === 0) { el.textContent = "0"; return; }
    const dur = 520; const start = performance.now();
    function step(now) {
      const p = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(eased * target);
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  });
}

function emptyState(icon, title, text, actionHTML) {
  return `<div class="empty"><div class="big">${icon}</div><h3>${esc(title)}</h3><p>${esc(text)}</p>${actionHTML || ""}</div>`;
}

/* ---- create workspace modal -------------------------------------------- */
function openCreateWorkspace() {
  const activeCustom = (State.customTemplates || []).filter((c) => c.status === "active").map((c) => c.key);
  const keys = (State.builtinKeys || Object.keys(State.templates)).concat(activeCustom);
  // A workspace type is a trade. Ones outside the company's own go behind a
  // line rather than away: a builder who takes on one delivery job a year still
  // has to be able to open that workspace.
  const isCustom = (k) => activeCustom.indexOf(k) !== -1;
  const near = keys.filter((k) => isCustom(k) || inMySectors([k]));
  const far = keys.filter((k) => near.indexOf(k) === -1);
  let chosen = near.indexOf("general") !== -1 ? "general" : (near[0] || keys[0]);
  const opt = (k) => {
    const cfg = State.templates[k] || {};
    const tag = cfg.recommended ? `<span class="badge accent" style="font-size:10px">${esc(t("recommended"))}</span>`
      : (isCustom(k) ? `<span class="badge partner" style="font-size:10px">${esc(t("nav_templates"))}</span>` : "");
    return `<div class="tpl-opt ${k === chosen ? "sel" : ""}" data-tpl="${k}"><div class="n">${esc(tplLabel(k, cfg.label))} ${tag}</div><div class="d">${esc(tplTagline(k, cfg.tagline))}</div></div>`;
  };
  const opts = near.map(opt).join("") + (far.length ? `
    <button type="button" class="tpl-more" id="tplMore">${esc(t("sec_more_types", { n: far.length }))}</button>
    <div id="tplFar" hidden>${far.map(opt).join("")}</div>` : "");
  openModal(t("cws_title"), `
    <label class="field"><span>${esc(t("cws_name"))}</span><input class="input" id="wsName" placeholder="${esc(t("cws_name_ph"))}" /></label>
    <label class="field"><span>${esc(t("cws_type"))}</span></label>
    <div class="tpl-pick" id="tplPick">${opts}</div>
    <a href="#/templates" id="cwsCustomCta" class="cws-cta">🧩 ${esc(t("cws_custom_cta"))} →</a>
    <label class="field mt16"><span>${esc(t("cws_partner"))}</span><input class="input" id="wsPartner" type="email" placeholder="partner@example.com" /></label>
    <p class="dim" style="font-size:12.5px">${esc(t("cws_partner_hint"))}</p>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="wsCreate">${esc(t("create"))}</button>`);
  $$("#tplPick .tpl-opt").forEach((o) => o.addEventListener("click", () => {
    $$("#tplPick .tpl-opt").forEach((x) => x.classList.remove("sel")); o.classList.add("sel"); chosen = o.dataset.tpl;
  }));
  const more = $("#tplMore");
  if (more) more.addEventListener("click", () => { $("#tplFar").hidden = false; more.remove(); });
  const cta = $("#cwsCustomCta");
  if (cta) cta.addEventListener("click", () => closeModal());
  $("#wsCreate").addEventListener("click", async () => {
    const name = $("#wsName").value.trim();
    if (!name) { toast(t("enter_name"), "err"); return; }
    const btn = $("#wsCreate"); btn.disabled = true;
    try {
      const ws = await API.createWorkspace({ name, template: chosen, partner_email: $("#wsPartner").value.trim() });
      closeModal(); toast(t("ws_created"), "ok"); go(`/w/${ws.id}`);
    } catch (err) { toast(errMsg(err), "err"); btn.disabled = false; }
  });
}

/* =========================================================================
   CUSTOM (PAID) TEMPLATES
   ======================================================================== */
async function refreshCustom() {
  try {
    State.customTemplates = await API.customTemplates();
    State.customTemplates.forEach((ct) => { State.templates[ct.key] = ct.config; });
  } catch {}
}

async function viewTemplates() {
  shell(crumb([{ label: t("home"), href: "/" }, { label: t("nav_templates") }]),
    `<div class="page"><div class="loading-full"><div class="spin"></div></div></div>`);
  await refreshCustom();
  const list = State.customTemplates || [];
  const grid = list.length ? `<div class="ws-grid">${list.map(tplCard).join("")}</div>`
    : emptyState("🧩", t("tpl_none"), t("tpl_mgr_sub"), `<button class="btn primary" id="newTplE"><span class="ico">＋</span> ${esc(t("tpl_new"))}</button>`);
  shell(crumb([{ label: t("home"), href: "/" }, { label: t("nav_templates") }]), `<div class="page">
    <div class="page-head">
      <div><h1>${esc(t("tpl_mgr_title"))}</h1><div class="sub">${esc(t("tpl_mgr_sub"))}</div></div>
      <div class="flex gap8 wrap">
        <a class="btn ghost" href="#/addons">${esc(t("ad_title"))}</a>
      <button class="btn primary" id="newTpl"><span class="ico">＋</span> ${esc(t("tpl_new"))}</button>
      </div>
    </div>
    ${grid}
  </div>`);
  $$("#newTpl, #newTplE").forEach((b) => b && b.addEventListener("click", openTemplateBuilder));
  $$("[data-activate]").forEach((b) => b.addEventListener("click", () => {
    const ct = (State.customTemplates || []).find((c) => c.id == b.dataset.activate); if (ct) openPurchase(ct);
  }));
  $$("[data-markpaid]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.confirmInvoice(b.dataset.markpaid); toast(t("toast_payment_ok"), "ok"); await refreshCustom(); viewTemplates(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-deltpl]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.deleteCustomTemplate(b.dataset.deltpl); await refreshCustom(); viewTemplates(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$(".ws-card[data-tid]").forEach((c) => c.addEventListener("click", (e) => {
    if (e.target.closest("button") || e.target.closest("a")) return;
    const ct = (State.customTemplates || []).find((x) => x.id == c.dataset.tid);
    if (ct && ct.status === "draft") openPurchase(ct);
  }));
}

function tplCard(ct) {
  const cfg = ct.config || {};
  const active = ct.status === "active";
  const pending = ct.status === "pending_invoice";
  const planTxt = ct.plan ? (ct.plan === "monthly" ? t("buy_monthly") : t("buy_onetime")) : "";
  const priceTxt = ct.price ? displayMoneyText(ct.price) : "";
  const stBadge = active ? `<span class="badge ok dot">${esc(t("tpl_st_active"))}</span>`
    : pending ? `<span class="badge warn dot">${esc(t("tpl_st_pending"))}</span>`
    : `<span class="badge warn dot">${esc(t("tpl_st_draft"))}</span>`;
  const actions = active ? `<span class="badge">${esc(planTxt)} ${esc(priceTxt)}</span>`
    : pending ? `<a class="btn ghost sm" href="/proforma/${ct.id}" target="_blank" rel="noopener">${esc(t("proforma"))} ↗</a>
                 <button class="btn primary sm" data-markpaid="${ct.id}">${esc(t("mark_paid"))}</button>`
    : `<button class="btn primary sm" data-activate="${ct.id}">${esc(t("tpl_activate"))}</button>`;
  return `<div class="card ws-card" data-tid="${ct.id}">
    <div class="top">
      <div><h2>${esc(cfg.label || ct.label)}</h2><div class="tpl">${esc(cfg.tagline || "")}</div></div>
      ${stBadge}
    </div>
    <div class="parties dim">${(cfg.fields || []).length} · ${(cfg.terms || []).length} · ${(cfg.stages || []).length}</div>
    <div class="flex between center wrap gap8" style="border-top:1px solid var(--border);padding-top:12px">
      <div class="flex gap8 wrap center">${actions}</div>
      <button class="btn ghost sm" data-deltpl="${ct.id}">${esc(t("tpl_delete"))}</button>
    </div>
  </div>`;
}

function openPurchase(ct) {
  const p = State.pricing || {};
  const provider = p.provider || (p.stripe ? "stripe" : "demo");
  const cardLabel = provider === "stripe" ? t("pay_card") : provider === "link" ? t("pay_link") : t("pay_card");
  const cardHint = provider === "link" ? (p.webhook ? t("pay_link_hint_auto") : t("pay_link_hint")) : (provider === "demo" ? t("pay_card_demo") : "");
  openModal(t("buy_title"), `
    <p class="muted">${esc(t("buy_sub"))}</p>
    <div class="plan-grid">
      <div class="plan-opt" data-plan="monthly"><div class="pn">${esc(t("buy_monthly"))}</div><div class="pp">${esc(displayMoneyText(p.monthly || "EUR 12"))}</div></div>
      <div class="plan-opt" data-plan="onetime"><div class="pn">${esc(t("buy_onetime"))}</div><div class="pp">${esc(displayMoneyText(p.onetime || "EUR 120"))}</div></div>
    </div>
    <div class="section-title">${esc(t("pay_method"))}</div>
    <div class="plan-grid">
      <div class="plan-opt sel" data-pm="card"><div class="pn">💳 ${esc(cardLabel)}</div>${cardHint ? `<div class="dim" style="font-size:11px;margin-top:4px">${esc(cardHint)}</div>` : ""}</div>
      <div class="plan-opt" data-pm="invoice"><div class="pn">🏦 ${esc(t("pay_invoice"))}</div></div>
    </div>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="buyBtn" disabled>${esc(t("buy_cta"))}</button>`);
  let plan = null, method = "card";
  $$("#modal-root [data-plan]").forEach((o) => o.addEventListener("click", () => {
    $$("#modal-root [data-plan]").forEach((x) => x.classList.remove("sel")); o.classList.add("sel");
    plan = o.dataset.plan; $("#buyBtn").disabled = false;
  }));
  $$("#modal-root [data-pm]").forEach((o) => o.addEventListener("click", () => {
    $$("#modal-root [data-pm]").forEach((x) => x.classList.remove("sel")); o.classList.add("sel");
    method = o.dataset.pm;
  }));
  $("#buyBtn").addEventListener("click", async () => {
    if (!plan) return;
    const btn = $("#buyBtn"); btn.disabled = true;
    try {
      const r = await API.checkoutTemplate(ct.id, { plan, method });
      if (r.checkout_url) { window.location.href = r.checkout_url; return; }
      closeModal();
      if (r.payment_url) {
        toast(t("tpl_st_pending"), "ok");
        window.open(r.payment_url, "_blank", "noopener");
      } else if (r.status === "pending_invoice") {
        toast(t("proforma_issued"), "ok");
        window.open("/proforma/" + ct.id, "_blank", "noopener");
      } else {
        toast(t("toast_tpl_active"), "ok");
      }
      await refreshCustom(); viewTemplates();
    } catch (err) { toast(errMsg(err), "err"); btn.disabled = false; }
  });
}

const BUILDER_TYPES = ["text", "textarea", "number", "money", "date", "select"];
function typeOptions() { return BUILDER_TYPES.map((tp) => `<option value="${tp}">${esc(t("type_" + tp))}</option>`).join(""); }
function builderRow(kind) {
  const opt = `<input class="input bcell" data-k="options" placeholder="${esc(t("b_options"))}" style="display:none" />`;
  const req = kind === "field" ? `<label class="brow-chk"><input type="checkbox" data-k="required" /> ${esc(t("b_required"))}</label>` : "";
  const typ = kind === "stage" ? "" : `<select class="select bcell" data-k="type">${typeOptions()}</select>`;
  const opts = kind === "stage" ? "" : opt;
  return `<div class="brow" data-row>
    <input class="input bcell grow" data-k="label" placeholder="${esc(t("b_row_label"))}" />
    ${typ}${req}${opts}
    <button type="button" class="btn ghost sm" data-rm title="${esc(t("b_remove"))}">✕</button>
  </div>`;
}
function wireBuilderRow(row) {
  const rm = row.querySelector("[data-rm]"); if (rm) rm.addEventListener("click", () => row.remove());
  const ty = row.querySelector('[data-k="type"]');
  if (ty) ty.addEventListener("change", () => {
    const o = row.querySelector('[data-k="options"]'); if (o) o.style.display = ty.value === "select" ? "" : "none";
  });
}

function openTemplateBuilder() {
  const m = openModal(t("builder_title"), `
    <label class="field"><span>${esc(t("b_label"))} *</span><input class="input" id="tLabel" /></label>
    <label class="field"><span>${esc(t("b_tagline"))}</span><input class="input" id="tTagline" /></label>
    <label class="field"><span>${esc(t("b_noun"))}</span><input class="input" id="tNoun" /></label>
    <div class="section-title">${esc(t("b_fields"))}</div><div id="fRep"></div>
    <button type="button" class="btn ghost sm" id="addF">＋ ${esc(t("b_add"))}</button>
    <div class="section-title">${esc(t("b_terms"))}</div><div id="tRep"></div>
    <button type="button" class="btn ghost sm" id="addT">＋ ${esc(t("b_add"))}</button>
    <div class="section-title">${esc(t("b_stages"))}</div><div id="sRep"></div>
    <button type="button" class="btn ghost sm" id="addS">＋ ${esc(t("b_add"))}</button>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="tSave">${esc(t("b_save"))}</button>`);
  m.classList.add("wide");
  const add = (sel, kind) => { const c = $(sel); c.insertAdjacentHTML("beforeend", builderRow(kind)); wireBuilderRow(c.lastElementChild); };
  // seed a few starter rows
  add("#fRep", "field"); add("#fRep", "field");
  add("#tRep", "term");
  add("#sRep", "stage"); add("#sRep", "stage"); add("#sRep", "stage");
  $("#addF").addEventListener("click", () => add("#fRep", "field"));
  $("#addT").addEventListener("click", () => add("#tRep", "term"));
  $("#addS").addEventListener("click", () => add("#sRep", "stage"));

  const collect = (sel) => $$(`${sel} [data-row]`).map((row) => {
    const get = (k) => { const el = row.querySelector(`[data-k="${k}"]`); return el ? (el.type === "checkbox" ? el.checked : el.value.trim()) : ""; };
    const o = { label: get("label"), type: get("type") || "text", required: get("required") };
    const opts = get("options"); if (opts) o.options = opts.split(",").map((x) => x.trim()).filter(Boolean);
    return o;
  }).filter((r) => r.label);

  $("#tSave").addEventListener("click", async () => {
    const payload = {
      label: $("#tLabel").value.trim(), tagline: $("#tTagline").value.trim(), item_noun: $("#tNoun").value.trim(),
      fields: collect("#fRep"), terms: collect("#tRep"), stages: collect("#sRep"),
    };
    if (!payload.label) { toast(t("enter_name"), "err"); return; }
    const btn = $("#tSave"); btn.disabled = true;
    try {
      const ct = await API.createCustomTemplate(payload);
      await refreshCustom(); closeModal(); toast(t("toast_tpl_created"), "ok");
      openPurchase(ct);
    } catch (err) { toast(errMsg(err), "err"); btn.disabled = false; }
  });
}

/* =========================================================================
   BUSINESS DIGITAL PASSPORT
   One permanent record per counterparty: identity, orders, agreed terms,
   documents, communication and the full audit trail.
   ======================================================================== */
/* ---- passport for one record (project, vehicle, shipment) ---------------- *
 * The counterparty passport, one level down: everything that ever happened to
 * this one thing, gathered from records that already exist. */
async function viewRecordPassport(oid) {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("pr_title") }]);
  shell(cr, `<div class="page"><div class="loading-full"><div class="spin"></div></div></div>`);
  let p;
  try { p = await API.passportOrder(oid); }
  catch (err) { toast(errMsg(err), "err"); go("/"); return; }

  const s = p.subject, st = p.stats;
  const stat = (n, l) => `<div class="pp-stat"><div class="n">${esc(String(n))}</div><div class="l">${esc(l)}</div></div>`;
  const rows = (items, empty, fn) => items.length ? items.map(fn).join("")
    : `<p class="muted" style="font-size:13px">${esc(empty)}</p>`;

  shell(cr, `<div class="page">
    <div class="page-head">
      <div><div class="flex center gap8"><span class="mono dim">${esc(s.ref)}</span>
        <span class="badge">${esc(s.status_label)}</span></div>
        <h1 style="margin-top:2px">${esc(s.title)}</h1>
        <div class="sub">${esc(t("pr_sub"))}</div></div>
      <div class="flex gap8 center wrap">
        <a class="btn ghost sm" href="#/o/${s.id}">${esc(t("pr_open_record"))}</a>
        <a class="btn ghost sm" href="/protocol/${s.id}" target="_blank" rel="noopener">🖨 ${esc(t("protocol_btn"))}</a>
      </div>
    </div>

    <div class="card pad">
      <div class="pp-id">
        <div><span class="dim">${esc(t("pr_workspace"))}</span><b><a href="#/w/${s.workspace.id}">${esc(s.workspace.name)}</a></b></div>
        ${s.counterparty ? `<div><span class="dim">${esc(t("pr_counterparty"))}</span><b><a href="#/passport/${s.counterparty.id}">${esc(s.counterparty.name)}</a></b></div>` : ""}
        <div><span class="dim">${esc(t("pr_vertical"))}</span><b>${esc(tplLabel(s.template, s.template))}</b></div>
        <div><span class="dim">${esc(t("pr_opened"))}</span><b>${esc((s.created_at || "").slice(0, 10))}</b></div>
      </div>
    </div>

    <div class="pp-stats mt16">
      ${stat(displayMoneyText(st.value), t("pr_value"))}
      ${stat(st.approvals.total ? `${st.approvals.done}/${st.approvals.total}` : "—", t("ap_title"))}
      ${stat(st.signatures, t("an_act_sigs"))}
      ${stat(st.documents, t("an_act_docs"))}
      ${stat(st.payouts, t("po_tab"))}
      ${stat(st.events, t("an_act_events"))}
    </div>

    ${p.fields.length ? `<div class="section-title">${esc(t("pr_details"))}</div>
      <div class="card pad"><div class="pp-id">${p.fields.map((f) =>
        `<div><span class="dim">${esc(f.label)}</span><b>${esc(f.value)}</b></div>`).join("")}</div></div>` : ""}

    <div class="section-title">${esc(t("pr_terms"))}</div>
    <div class="card pad">${rows(p.terms, t("pr_no_terms"), (x) => `
      <div class="an-row"><div class="grow"><b>${esc(x.label)}</b></div>
        <span>${esc(displayMoneyText(x.value))}</span>
        <span class="badge ${x.state === "agreed" ? "ok" : "warn"}">${esc(x.state === "agreed" ? t("term_agreed") : t("term_proposed"))}</span>
      </div>`)}</div>

    <div class="an-grid mt16">
      <div class="card pad">
        <div class="an-head"><span class="an-title">${esc(t("sig_title"))}</span></div>
        ${rows(p.signatures, t("sig_empty"), (x) => `
          <div class="an-row"><div class="grow"><b>${esc(x.title)}</b>
            <div class="dim mono" style="font-size:10.5px;word-break:break-all">${esc(x.hash)}</div></div>
            <span class="badge ${x.status === "signed" ? "ok" : "warn"}">${esc(x.status === "signed" ? t("sig_signed") : t("sig_pending"))}</span>
            ${x.status === "signed" ? `<a class="btn ghost sm" href="/signature/${x.id}" target="_blank" rel="noopener">${esc(t("sig_cert"))}</a>` : ""}
          </div>`)}
      </div>
      <div class="card pad">
        <div class="an-head"><span class="an-title">${esc(t("ap_title"))}</span></div>
        ${rows(p.approvals, t("ap_none_hint"), (x) => `
          <div class="an-row"><div class="grow"><b>${esc(x.label)}</b>
            <div class="dim" style="font-size:11.5px">${esc(x.decided_by || "")}${x.decided_at ? " · " + relTime(x.decided_at) : ""}</div></div>
            <span class="badge ${{ approved: "ok", rejected: "danger", pending: "warn" }[x.status] || ""}">${esc(t("ap_state_" + (x.status === "skipped" ? "none" : x.status === "pending" ? "pending" : x.status)))}</span>
          </div>`)}
      </div>
    </div>

    <div class="an-grid mt16">
      <div class="card pad">
        <div class="an-head"><span class="an-title">${esc(t("po_tab"))}</span></div>
        ${rows(p.payouts, t("po_empty"), (x) => `
          <div class="an-row"><div class="grow"><b>${esc(displayMoneyText(x.amount))}</b>
            <div class="dim" style="font-size:11.5px">${esc(x.description || "")}</div></div>
            <span class="badge ${x.status === "paid" ? "ok" : "warn"}">${esc(t("po_st_" + x.status))}</span>
          </div>`)}
      </div>
      <div class="card pad">
        <div class="an-head"><span class="an-title">${esc(t("pr_files"))}</span></div>
        ${rows(p.attachments, t("pr_no_files"), (x) => `
          <div class="an-row"><div class="grow"><b>${esc(x.name)}</b>
            <div class="dim" style="font-size:11.5px">${esc(x.kind)} · ${relTime(x.at)}</div></div>
            <a class="btn ghost sm" href="/uploads/${s.id}/${esc(x.file)}" target="_blank" rel="noopener">${esc(t("an_open_btn"))}</a>
          </div>`)}
      </div>
    </div>

    <div class="section-title">${esc(t("timeline_title"))}</div>
    <div class="card pad"><div class="timeline">${
      p.events.length ? p.events.slice().reverse().map((e) => timelineItem(e, false)).join("")
                      : `<p class="muted">${esc(t("no_events"))}</p>`}</div></div>
  </div>`);
}

/* ---- vehicles ------------------------------------------------------------ *
 * A car is bought, shipped, cleared, registered and sold - several records over
 * months. The VIN is what stays constant, so it is the thing the history hangs
 * on rather than any one order. */
async function viewVehicles() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("vh_title") }]);
  let list = { vehicles: [] };
  try { list = await API.vehicles(State.vhQuery || ""); }
  catch (err) { toast(errMsg(err), "err"); }

  const rows = list.vehicles.filter((v) => v.status !== "archived");
  shell(cr, `<div class="page">
    <div class="page-head">
      <div><h1>${esc(t("vh_title"))}</h1><div class="sub">${esc(t("vh_sub"))}</div></div>
      <button class="btn primary" id="vhNew"><span class="ico">＋</span> ${esc(t("vh_new"))}</button>
    </div>
    <div class="card pad">
      <input class="input" id="vhSearch" placeholder="${esc(t("vh_search"))}" value="${esc(State.vhQuery || "")}" />
      <div class="an-rows mt12">${rows.length ? rows.map((v) => `
        <div class="an-row">
          <div class="grow"><b>${esc(v.label)}</b>
            <div class="dim mono" style="font-size:11.5px">${esc(v.vin)}${v.plate ? " · " + esc(v.plate) : ""}</div>
            <div class="dim" style="font-size:11.5px">${[v.colour, v.fuel,
              v.mileage ? v.mileage.toLocaleString(LANG) + " km" : ""].filter(Boolean).map(esc).join(" · ")}</div>
          </div>
          <span class="dim">${esc(t("vh_records", { n: v.orders }))}</span>
          <a class="btn ghost sm" href="#/passport/vehicle/${v.id}">🛡 ${esc(t("pr_title"))}</a>
        </div>`).join("") : `<p class="muted" style="font-size:13px">${esc(t("vh_empty"))}</p>`}
      </div>
    </div>
  </div>`);

  let timer;
  $("#vhSearch").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { State.vhQuery = e.target.value.trim(); viewVehicles(); }, 350);
  });
  $("#vhNew").addEventListener("click", () => {
    openModal(t("vh_new"), `
      <label class="field"><span>VIN *</span>
        <input class="input" id="vhVin" maxlength="17" placeholder="WVWZZZ1KZAW123456" /></label>
      <div class="grid2">
        <label class="field"><span>${esc(t("vh_make"))}</span><input class="input" id="vhMake" /></label>
        <label class="field"><span>${esc(t("vh_model"))}</span><input class="input" id="vhModel" /></label>
      </div>
      <div class="grid2">
        <label class="field"><span>${esc(t("vh_year"))}</span><input class="input" id="vhYear" type="number" min="1900" max="2100" /></label>
        <label class="field"><span>${esc(t("vh_plate"))}</span><input class="input" id="vhPlate" /></label>
      </div>
      <div class="grid2">
        <label class="field"><span>${esc(t("vh_colour"))}</span><input class="input" id="vhColour" /></label>
        <label class="field"><span>${esc(t("vh_fuel"))}</span><input class="input" id="vhFuel" /></label>
      </div>
      <label class="field"><span>${esc(t("vh_mileage"))}</span><input class="input" id="vhMileage" type="number" min="0" /></label>`,
      `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
       <button class="btn primary" id="vhSave">${esc(t("save"))}</button>`);
    $("#vhSave").addEventListener("click", async () => {
      try {
        await API.vehicleCreate({
          vin: $("#vhVin").value, make: $("#vhMake").value, model: $("#vhModel").value,
          year: $("#vhYear").value, plate: $("#vhPlate").value, colour: $("#vhColour").value,
          fuel: $("#vhFuel").value, mileage: $("#vhMileage").value,
        });
        closeModal(); toast(t("saved"), "ok"); viewVehicles();
      } catch (err) { toast(errMsg(err), "err"); }
    });
  });
}

async function viewVehiclePassport(vid) {
  const cr = crumb([{ label: t("home"), href: "/" },
                    { label: t("vh_title"), href: "/vehicles" }, { label: t("pr_title") }]);
  shell(cr, `<div class="page"><div class="loading-full"><div class="spin"></div></div></div>`);
  let p;
  try { p = await API.passportVehicle(vid); }
  catch (err) { toast(errMsg(err), "err"); go("/vehicles"); return; }

  const s = p.subject, st = p.stats;
  const stat = (n, l) => `<div class="pp-stat"><div class="n">${esc(String(n))}</div><div class="l">${esc(l)}</div></div>`;
  shell(cr, `<div class="page">
    <div class="page-head">
      <div><h1>${esc(s.label)}</h1>
        <div class="sub mono">${esc(s.vin)}${s.plate ? " · " + esc(s.plate) : ""}</div>
        <div class="sub">${esc(t("vh_passport_sub"))}</div></div>
      <a class="btn ghost sm" href="#/vehicles">${esc(t("vh_title"))}</a>
    </div>

    <div class="card pad">
      <div class="pp-id">
        <div><span class="dim">${esc(t("vh_make"))}</span><b>${esc(s.make || "—")}</b></div>
        <div><span class="dim">${esc(t("vh_model"))}</span><b>${esc(s.model || "—")}</b></div>
        <div><span class="dim">${esc(t("vh_year"))}</span><b>${esc(s.year || "—")}</b></div>
        <div><span class="dim">${esc(t("vh_colour"))}</span><b>${esc(s.colour || "—")}</b></div>
        <div><span class="dim">${esc(t("vh_fuel"))}</span><b>${esc(s.fuel || "—")}</b></div>
        <div><span class="dim">${esc(t("vh_mileage"))}</span><b>${s.mileage ? esc(s.mileage.toLocaleString(LANG)) + " km" : "—"}</b></div>
      </div>
    </div>

    <div class="pp-stats mt16">
      ${stat(st.records, t("vh_records_l"))}
      ${stat(displayMoneyText(st.value), t("pr_value"))}
      ${stat(st.signatures, t("an_act_sigs"))}
      ${stat(st.documents, t("an_act_docs"))}
      ${stat(st.payouts, t("po_tab"))}
      ${stat(st.events, t("an_act_events"))}
    </div>

    <div class="section-title">${esc(t("vh_history"))}</div>
    <div class="card pad">${p.orders.length ? p.orders.map((o) => `
      <div class="an-row"><div class="grow"><b>${esc(o.ref)} · ${esc(o.title)}</b>
        <div class="dim" style="font-size:11.5px">${esc(o.workspace)}${o.role ? " · " + esc(o.role) : ""}</div></div>
        <span class="badge">${esc(o.status_label)}</span>
        <a class="btn ghost sm" href="#/o/${o.id}">${esc(t("an_open_btn"))}</a>
      </div>`).join("") : `<p class="muted" style="font-size:13px">${esc(t("vh_no_records"))}</p>`}</div>

    ${p.signatures.length ? `<div class="section-title">${esc(t("sig_title"))}</div>
      <div class="card pad">${p.signatures.map((x) => `
        <div class="an-row"><div class="grow"><b>${esc(x.title)}</b>
          <div class="dim mono" style="font-size:10.5px;word-break:break-all">${esc(x.hash)}</div></div>
          <span class="badge ${x.status === "signed" ? "ok" : "warn"}">${esc(x.status === "signed" ? t("sig_signed") : t("sig_pending"))}</span>
          ${x.status === "signed" ? `<a class="btn ghost sm" href="/signature/${x.id}" target="_blank" rel="noopener">${esc(t("sig_cert"))}</a>` : ""}
        </div>`).join("")}</div>` : ""}

    <div class="section-title">${esc(t("timeline_title"))}</div>
    <div class="card pad"><div class="timeline">${
      p.events.length ? p.events.slice().reverse().map((e) => timelineItem(e, false)).join("")
                      : `<p class="muted">${esc(t("no_events"))}</p>`}</div></div>
  </div>`);
}

/* ---- passport for one product ------------------------------------------- */
async function viewProductPassport(pid) {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("nav_store"), href: "/store" },
                    { label: t("pr_product") }]);
  shell(cr, `<div class="page"><div class="loading-full"><div class="spin"></div></div></div>`);
  let p;
  try { p = await API.passportProduct(pid); }
  catch (err) { toast(errMsg(err), "err"); go("/store"); return; }

  const s = p.subject, st = p.stats;
  const stat = (n, l) => `<div class="pp-stat"><div class="n">${esc(String(n))}</div><div class="l">${esc(l)}</div></div>`;
  shell(cr, `<div class="page">
    <div class="page-head">
      <div class="flex center gap8">
        ${s.image ? `<img src="${esc(s.image)}" alt="" style="width:56px;height:56px;object-fit:cover;border-radius:10px" />` : ""}
        <div><h1>${esc(s.name)}</h1>
          <div class="sub">${esc(t("pr_product_sub"))}</div></div>
      </div>
      <a class="btn ghost sm" href="#/store">${esc(t("nav_store"))}</a>
    </div>

    <div class="card pad">
      <div class="pp-id">
        <div><span class="dim">${esc(t("st_price"))}</span><b>${esc(displayMoneyText(s.price))}</b></div>
        <div><span class="dim">SKU</span><b>${esc(s.sku || "—")}</b></div>
        <div><span class="dim">${esc(t("st_type"))}</span><b>${esc(s.type === "digital" ? t("st_digital") : t("st_physical"))}</b></div>
        <div><span class="dim">${esc(t("pr_opened"))}</span><b>${esc((s.created_at || "").slice(0, 10))}</b></div>
      </div>
    </div>

    <div class="pp-stats mt16">
      ${stat(st.orders, t("pr_sold"))}
      ${stat(st.fulfilled, t("an_closed"))}
      ${stat(displayMoneyText(st.revenue), t("pr_revenue"))}
      ${stat(s.available, t("pr_available"))}
      ${st.keys_total ? stat(`${st.keys_free}/${st.keys_total}`, t("pr_keys")) : ""}
    </div>

    <div class="section-title">${esc(t("pr_history"))}</div>
    <div class="card pad">${p.orders.length ? p.orders.map((o) => `
      <div class="an-row"><div class="grow"><b>${esc(o.ref)}</b>
        <div class="dim" style="font-size:11.5px">${esc(o.customer || "")} · ${relTime(o.created_at)}</div></div>
        <span class="dim">×${o.qty}</span>
        <span class="badge ${o.status === "fulfilled" ? "ok" : "warn"}">${esc(t("st_status_" + o.status))}</span>
      </div>`).join("") : `<p class="muted" style="font-size:13px">${esc(t("pr_no_history"))}</p>`}</div>
  </div>`);
}

async function viewPassport(orgId) {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("passport_title") }]);
  shell(cr, `<div class="page"><div class="loading-full"><div class="spin"></div></div></div>`);
  let p;
  try { p = await API.passport(orgId); }
  catch (err) { toast(errMsg(err), "err"); go("/"); return; }

  const o = p.org, s = p.stats;
  const stat = (n, l) => `<div class="pp-stat"><div class="n">${esc(String(n))}</div><div class="l">${esc(l)}</div></div>`;
  const cItems = ["phone", "whatsapp", "viber", "telegram", "alt_email"]
    .filter((k) => p.contacts && p.contacts[k])
    .map((k) => `<a class="emg-chip" href="${esc(EMERGENCY_LINKS[k](p.contacts[k]))}" target="_blank" rel="noopener">${EMERGENCY_ICONS[k]} ${esc(p.contacts[k])}</a>`).join("");

  const orderRows = p.orders.length ? p.orders.map((x) => `<tr data-pp-order="${x.id}">
      <td class="mono dim">${esc(x.ref)}</td>
      <td>${esc(x.title)}<div class="dim" style="font-size:11.5px">${esc(x.workspace)}</div></td>
      <td><span class="badge">${esc(x.status_label)}</span></td>
      <td class="dim">${x.terms.filter((tm) => tm.state === "agreed").length}/${x.terms.length}</td>
      <td class="dim">${relTime(x.updated_at)}</td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">${esc(t("passport_no_orders"))}</td></tr>`;

  shell(cr, `<div class="page">
    <div class="page-head"><div>
      <h1>${esc(o.name)} ${o.verified ? `<span class="badge ok dot">${esc(t("company_verified"))}</span>` : ""}</h1>
      <div class="sub">${esc(t("passport_sub"))}</div></div>
      <a class="btn ghost" href="/passport/${o.id}" target="_blank" rel="noopener">🖨 ${esc(t("passport_print"))}</a>
    </div>

    <div class="card pad">
      <div class="pp-id">
        <div><span class="dim">${esc(o.id_label)}</span><b>${esc(o.reg_number || "—")}</b></div>
        <div><span class="dim">${esc(t("cr_country"))}</span><b>${esc(o.country || "—")}</b></div>
        <div><span class="dim">${esc(t("passport_role"))}</span><b>${esc(o.kind === "business" ? t("role_business") : t("role_partner"))}</b></div>
        <div><span class="dim">${esc(t("passport_since"))}</span><b>${esc((o.since || "").slice(0, 10))}</b></div>
      </div>
      ${cItems ? `<div class="emg-list mt12">${cItems}</div>` : ""}
    </div>

    <div class="pp-stats mt16">
      ${stat(s.orders, t("passport_orders"))}
      ${stat(s.open, t("stat_open"))}
      ${stat(s.terms_agreed, t("passport_terms_agreed"))}
      ${stat(s.terms_open, t("passport_terms_open"))}
      ${stat(s.documents, t("passport_docs"))}
      ${stat(s.messages, t("passport_messages"))}
      ${stat(displayMoneyText(s.value), t("passport_value"))}
    </div>

    <div class="section-title">${esc(t("passport_orders"))}</div>
    <div class="card pad0"><table class="tbl"><thead><tr>
      <th>${esc(t("col_ref"))}</th><th>${esc(t("corder_title_label"))}</th>
      <th>${esc(t("col_status"))}</th><th>${esc(t("terms_title"))}</th><th>${esc(t("col_updated"))}</th>
    </tr></thead><tbody>${orderRows}</tbody></table></div>

    <div class="section-title">${esc(t("passport_audit"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12px;margin-bottom:8px">${esc(t("passport_audit_hint"))}</p>
      <div class="timeline">${p.events.length ? p.events.map((e) => timelineItem(e, true)).join("")
        : `<p class="muted">${esc(t("no_events"))}</p>`}</div>
    </div>
  </div>`);

  $$("[data-pp-order]").forEach((r) => r.addEventListener("click", () => go(`/o/${r.dataset.ppOrder}`)));
  $$("[data-go-order]").forEach((a) => a.addEventListener("click", () => go(`/o/${a.dataset.goOrder}`)));
}

/* =========================================================================
   DOCUMENT LIBRARY (region-aware corporate templates, print to PDF)
   ======================================================================== */
const DOC_CATALOG = [
  ["cat_invoicing", [["proforma_doc", "🧾"], ["invoice", "💶"], ["commercial_offer", "📊"], ["reconciliation", "⚖️"]]],
  ["cat_sector", [["fulfillment", "📦"], ["ip_transfer", "🎨"], ["act19", "🧱"], ["supply_recurring", "🍽️"], ["harvest_supply", "🌾"]]],
  ["cat_logistics", [["transport_order", "🚚"], ["cmr", "🧭"], ["delivery_note", "📥"]]],
  ["cat_contracts", [["service", "🤝"], ["subcontract", "🏗️"], ["consignment", "📦"]]],
  ["cat_protocols", [["handover", "📋"], ["claim", "⚠️"], ["site", "🚧"]]],
  ["cat_declarations", [["nda", "🔒"], ["gdpr", "🛡️"], ["poa", "✒️"]]],
  ["cat_auto", [["car_sale", "🚗"], ["car_handover", "🔑"], ["car_customs", "🛂"]]],
];

/* Which industry verticals each document belongs to (for the sector filter). */
const ALL_SECTORS = ["general", "ecommerce", "agency", "construction", "restaurant", "agriculture", "auto-import"];
const DOC_SECTORS = {
  proforma_doc: ALL_SECTORS, invoice: ALL_SECTORS, commercial_offer: ALL_SECTORS,
  reconciliation: ALL_SECTORS, nda: ALL_SECTORS, gdpr: ALL_SECTORS, poa: ALL_SECTORS,
  service: ["general", "agency", "construction", "ecommerce"],
  subcontract: ["construction", "general", "agency"],
  consignment: ["ecommerce", "general", "auto-import"],
  handover: ["general", "construction", "agency", "auto-import"],
  claim: ["general", "ecommerce", "construction", "restaurant", "agriculture"],
  site: ["construction"],
  transport_order: ["ecommerce", "construction", "restaurant", "agriculture", "auto-import"],
  cmr: ["ecommerce", "auto-import", "restaurant", "agriculture"],
  delivery_note: ["ecommerce", "restaurant", "agriculture", "general"],
  car_sale: ["auto-import"], car_handover: ["auto-import"], car_customs: ["auto-import"],
  fulfillment: ["ecommerce"], ip_transfer: ["agency"],
  act19: ["construction"], supply_recurring: ["restaurant"],
  harvest_supply: ["agriculture"],
};
/* "mine" is the company's own trades. It is the default the moment the company
   has named them, so the library opens on what that company actually uses. */
let docSector = "mine";
const DOC_INVOICE = ["invoice", "proforma_doc", "commercial_offer", "delivery_note"];
const DOC_NOTES = ["handover", "poa", "claim", "site", "car_sale", "car_handover",
                   "transport_order", "cmr", "consignment", "car_customs"];
const DOC_AMOUNT = ["car_sale", "transport_order", "reconciliation", "consignment", "car_customs"];
function docTitle(kind) { return t("doc_" + kind); }

/* The shelf, as a screen.

   Two different things live under "Documents": the blank templates you fill in,
   and the paperwork you have actually filed. They were one list; a builder
   looking for last year's signed contract does not want a blank NDA in the way,
   so they are two tabs. */
let docsTab = "templates";

function fileIcon(mime, name) {
  const ext = String(name || "").split(".").pop().toLowerCase();
  if ((mime || "").startsWith("image/")) return "🖼";
  if (ext === "pdf") return "📕";
  if (ext === "csv" || ext === "xls" || ext === "xlsx" || ext === "ods") return "📊";
  if (ext === "zip") return "🗜";
  return "📄";
}

function fileSize(n) {
  const kb = (n || 0) / 1024;
  return kb < 1024 ? kb.toFixed(0) + " kB" : (kb / 1024).toFixed(1) + " MB";
}

async function viewArchive() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("nav_docs") },
                    { label: t("ar_title") }]);
  let d;
  try { d = await API.files(); }
  catch (err) { toast(errMsg(err), "err"); d = { files: [], accepts: [] }; }
  // Needed to name the relationship a document belongs to, and to offer the
  // list when filing one. Fetched here rather than assumed to be loaded.
  if (!State.workspaces) {
    try { State.workspaces = await API.workspaces(); } catch { State.workspaces = []; }
  }

  const spaces = State.workspaces || [];
  const wsName = (id) => (spaces.find((w) => w.id === id) || {}).name || "";

  const row = (f) => `
    <div class="an-row">
      <span style="font-size:20px">${fileIcon(f.mime, f.name)}</span>
      <div class="grow">
        <b>${esc(f.title)}</b>
        ${f.source === "generated" ? `<span class="badge accent">${esc(t("ar_generated"))}</span>` : ""}
        ${f.shared ? `<span class="badge ok dot">${esc(t("ar_shared"))}</span>` : ""}
        ${!f.mine ? `<span class="badge partner">${esc(t("ar_from_them"))}</span>` : ""}
        <div class="dim" style="font-size:11.5px">${esc(f.name)} · ${esc(fileSize(f.size))}${
          f.workspace_id ? " · " + esc(wsName(f.workspace_id)) : ""} · ${relTime(f.at)}</div>
        ${f.note ? `<div class="dim" style="font-size:11.5px">${esc(f.note)}</div>` : ""}
      </div>
      <a class="btn ghost sm" href="${esc(f.url)}">${esc(t("ar_download"))}</a>
      ${f.mine ? `
        ${f.workspace_id ? `<button class="btn ghost sm" data-ar-share="${f.id}" data-on="${
          f.shared ? 1 : 0}">${esc(f.shared ? t("ar_unshare") : t("ar_share"))}</button>` : ""}
        <button class="btn ghost sm" data-ar-del="${f.id}">${esc(t("tm_remove"))}</button>` : ""}
    </div>`;

  shell(cr, `<div class="page">
    <div class="page-head"><div><h1>${esc(t("ar_title"))}</h1>
      <div class="sub">${esc(t("ar_sub"))}</div></div>
      <button class="btn primary" id="arAdd">${esc(t("ar_add"))}</button></div>

    <div class="flex gap8 wrap" style="margin:0 0 16px">
      <button class="btn ghost sm" data-docs-tab="templates">${esc(t("docs_title"))}</button>
      <button class="btn primary sm" data-docs-tab="archive">${esc(t("ar_title"))}</button>
    </div>

    <div class="an-rows" id="arDrop">${d.files.length ? d.files.map(row).join("")
      : `<p class="muted" style="font-size:13px">${esc(t("ar_none"))}</p>`}</div>
    <p class="dim mt16" style="font-size:12px">${esc(t("ar_note", {
      list: (d.accepts || []).join(", "), mb: d.max_mb || 25 }))}</p>
  </div>`);

  $$("[data-docs-tab]").forEach((b) => b.addEventListener("click", () => {
    docsTab = b.dataset.docsTab; render();
  }));
  $("#arAdd").addEventListener("click", () => openFileUpload(null, () => viewArchive()));

  $$("[data-ar-share]").forEach((b) => b.addEventListener("click", async () => {
    try {
      await API.fileUpdate(b.dataset.arShare, { shared: b.dataset.on !== "1" });
      toast(t("saved"), "ok"); viewArchive();
    } catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-ar-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(t("ar_delete_ask"))) return;
    try { await API.fileDelete(b.dataset.arDel); toast(t("saved"), "ok"); viewArchive(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));

  // Dropping a file onto the list is how people expect to file one.
  const drop = $("#arDrop");
  ["dragover", "dragenter"].forEach((e) => drop.addEventListener(e, (ev) => {
    ev.preventDefault(); drop.classList.add("dropping");
  }));
  ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, () => drop.classList.remove("dropping")));
  drop.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    const files = ev.dataTransfer && ev.dataTransfer.files;
    if (!files || !files.length) return;
    try { await API.fileUpload(files, {}); toast(t("ar_filed"), "ok"); viewArchive(); }
    catch (err) { toast(errMsg(err), "err"); }
  });
}

/* One dialogue for filing, wherever it is opened from: the archive, a
   workspace, or an order. `ctx` carries where it lands. */
function openFileUpload(ctx, after) {
  ctx = ctx || {};
  const spaces = (State.workspaces || []).filter((w) => (w.partner || {}).id);
  openModal(t("ar_add"), `
    <label class="field"><span>${esc(t("ar_file"))}</span>
      <input class="input" type="file" id="ufFile" multiple></label>
    <label class="field"><span>${esc(t("ar_name"))}</span>
      <input class="input" id="ufTitle" maxlength="160" placeholder="${esc(t("ar_name_ph"))}"></label>
    <label class="field"><span>${esc(t("ar_note_field"))}</span>
      <input class="input" id="ufNote" maxlength="400"></label>
    ${ctx.workspace_id ? "" : `
      <label class="field"><span>${esc(t("ar_belongs"))}</span>
        <select class="select" id="ufWs">
          <option value="">${esc(t("ar_company_only"))}</option>
          ${spaces.map((w) => `<option value="${w.id}">${esc(w.name)}</option>`).join("")}
        </select></label>`}
    <label class="field checkbox-field"><input type="checkbox" id="ufShare" ${
      ctx.shared ? "checked" : ""}> <span>${esc(t("ar_share_with"))}</span></label>
    <p class="dim" style="font-size:12px">${esc(t("ar_share_note"))}</p>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
     <button class="btn primary" id="ufGo">${esc(t("ar_file_it"))}</button>`);

  $("#ufGo").addEventListener("click", async () => {
    const inp = $("#ufFile");
    if (!inp.files || !inp.files.length) { toast(t("ar_pick"), "err"); return; }
    const wsSel = $("#ufWs");
    const ws = ctx.workspace_id || (wsSel && wsSel.value ? Number(wsSel.value) : null);
    const btn = $("#ufGo"); btn.disabled = true;
    try {
      await API.fileUpload(inp.files, {
        title: $("#ufTitle").value.trim(), note: $("#ufNote").value.trim(),
        workspace_id: ws, order_id: ctx.order_id,
        shared: $("#ufShare").checked && !!ws,
      });
      toast(t("ar_filed"), "ok");
      closeModal();
      if (after) after();
    } catch (err) { toast(errMsg(err), "err"); btn.disabled = false; }
  });
}

async function viewDocs() {
  if (docsTab === "archive") return viewArchive();
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("nav_docs") }]);
  if (!State._compliance) { try { State._compliance = await API.compliance(); } catch { State._compliance = { countries: [], org_country: "" }; } }
  const comp = State._compliance || { countries: [], org_country: "" };
  let ent = { paid: true, used: [], limit: 2, total: 23 };
  try { ent = await API.docsEntitlement(); } catch {}
  State._docEnt = ent;
  const canOpen = (k) => ent.paid || ent.used.indexOf(k) !== -1 || ent.used.length < ent.limit;
  const refRows = (comp.countries || []).filter((c) => c.code !== "OTHER").map((c) => `<tr>
      <td><b>${esc(c.code)}</b></td><td>${esc(c.id_label)}</td>
      <td>${esc(c.vat_prefix || "—")}</td><td class="mono">${c.vat_rate}%</td>
      <td class="dim">${esc(c.authority)}</td><td>${c.eu ? "EU" : "—"}</td></tr>`).join("");
  // Without a choice made, "mine" would hide everything, so it falls back to
  // the full library rather than to an empty screen.
  const mine = mySectors();
  if (docSector === "mine" && !mine.length) docSector = "all";
  const inSector = (k) => docSector === "all" ||
    (docSector === "mine" ? inMySectors(DOC_SECTORS[k])
                          : (DOC_SECTORS[k] || []).indexOf(docSector) !== -1);
  const chips = (mine.length ? [["mine", "🎯", t("sector_mine")]] : [])
    .concat([["all", "◆", t("sector_all")]])
    .concat(ALL_SECTORS.map((s) => [s, SECTOR_ICON[s], tplLabel(s)]));
  const hiddenCount = docSector === "mine"
    ? DOC_CATALOG.reduce((n, [, items]) => n + items.filter(([k]) => !inSector(k)).length, 0) : 0;
  const filterRow = `<div class="sector-filter">${chips.map(([s, ico, lab]) =>
    `<button class="sec-chip ${docSector === s ? "on" : ""}" data-sector="${esc(s)}">${ico} ${esc(lab)}</button>`).join("")}
    ${hiddenCount ? `<span class="sec-hidden">${esc(t("sector_hidden", { n: hiddenCount }))}</span>` : ""}</div>`;

  const sections = DOC_CATALOG.map(([cat, items]) => {
    const vis = items.filter(([k]) => inSector(k));
    if (!vis.length) return "";
    return `<div class="section-title">${esc(t(cat))}</div>
    <div class="ws-grid">${vis.map(([k, ico]) => {
      const open = canOpen(k);
      return `<div class="card ws-card doc-card ${open ? "" : "locked"}">
      <div class="doc-ico">${ico}</div>
      <h2>${esc(docTitle(k))}</h2>
      <div class="doc-tags">${(DOC_SECTORS[k] || []).length === ALL_SECTORS.length
        ? `<span class="doc-tag">${esc(t("sector_all"))}</span>`
        : (DOC_SECTORS[k] || []).map((s) => `<span class="doc-tag">${esc(tplLabel(s))}</span>`).join("")}</div>
      <div style="border-top:1px solid var(--border);padding-top:12px">
        ${open ? `<button class="btn primary sm" data-doc-open="${k}">${esc(t("doc_generate"))}</button>`
          : `<a class="doc-lock" href="#/plans">🔒 ${esc(t("doc_locked"))}</a>`}</div>
    </div>`; }).join("")}</div>`;
  }).join("");

  const quota = ent.paid ? "" : `<div class="quota-banner">
      <span>🔒 ${esc(t("docs_quota", { used: ent.used.length, limit: ent.limit, total: ent.total }))}</span>
      <a class="btn primary sm" href="#/plans">${esc(t("docs_upgrade"))}</a></div>`;
  shell(cr, `<div class="page">
    <div class="page-head"><div><h1>${esc(t("docs_title"))}</h1><div class="sub">${esc(t("docs_sub"))}</div></div></div>
    <div class="flex gap8 wrap" style="margin:0 0 16px">
      <button class="btn primary sm" data-docs-tab="templates">${esc(t("docs_title"))}</button>
      <button class="btn ghost sm" data-docs-tab="archive">${esc(t("ar_title"))}</button>
    </div>
    ${quota}
    ${filterRow}
    ${sections}
    <div class="section-title">${esc(t("compliance_ref"))}</div>
    <div class="card pad0"><table class="tbl compliance-tbl"><thead><tr>
      <th>${esc(t("cr_country"))}</th><th>${esc(t("cr_id"))}</th><th>${esc(t("cr_vat_prefix"))}</th>
      <th>${esc(t("cr_vat_rate"))}</th><th>${esc(t("cr_authority"))}</th><th>${esc(t("cr_eu"))}</th>
    </tr></thead><tbody>${refRows}</tbody></table></div>
    <p class="dim mt12" style="font-size:12px">${esc(t("compliance_note"))}${comp.verified ? ` · ${esc(t("cr_verified"))}: ${esc(comp.verified)}` : ""}</p>
    <p class="dim mt16" style="font-size:12px">${esc(t("docs_note"))}</p>
  </div>`);
  $$("[data-doc-open]").forEach((b) => b.addEventListener("click", () => openDocForm(b.dataset.docOpen)));
  $$("[data-sector]").forEach((b) => b.addEventListener("click", () => { docSector = b.dataset.sector; viewDocs(); }));
  $$("[data-docs-tab]").forEach((b) => b.addEventListener("click", () => {
    docsTab = b.dataset.docsTab; viewDocs();
  }));
}

/* The language a document is written in is a property of the document, not of
   whoever is making it: a Bulgarian company invoicing a German customer sends
   a German invoice while working in Bulgarian itself. */
function docLangRow() {
  return `<label class="field"><span>${esc(t("d_lang"))}</span>
    <select class="select" id="dLang">${LANGS.map((l) =>
      `<option value="${l.code}"${l.code === LANG ? " selected" : ""}>${l.flag} ${esc(l.name)}</option>`
    ).join("")}</select>
    <span class="dim" style="font-size:11.5px">${esc(t("d_lang_hint"))}</span></label>`;
}

function docLangParam(q) {
  const sel = $("#dLang");
  if (sel && sel.value) q.set("lang", sel.value);
  return q;
}

function openDocForm(kind) {
  const org = State.org || {};
  const partyRows = `<div class="row2">
      <label class="field"><span>${esc(t("d_party_a"))}</span><input class="input" id="dA" value="${esc(org.name || "")}" /></label>
      <label class="field"><span>${esc(t("d_reg"))} · A</span><input class="input" id="dRegA" value="${esc(org.reg_number || "")}" /></label>
      <label class="field"><span>${esc(t("d_party_b"))}</span><input class="input" id="dB" /></label>
      <label class="field"><span>${esc(t("d_reg"))} · B</span><input class="input" id="dRegB" /></label>
    </div>` + docLangRow();

  if (DOC_INVOICE.indexOf(kind) !== -1) {
    const isQty = kind === "delivery_note";
    // Quantity, unit and unit price rather than one number: an invoice has to
    // say how much of what, at what price. The line total is worked out and
    // shown as it is typed, so a mistake is visible before the document is.
    const lineRow = () => `<div class="inv-line5">
      <input class="input" data-inv-desc placeholder="${esc(t("d_line_desc"))}" />
      <input class="input" data-inv-qty type="number" step="0.001" min="0"
        placeholder="${esc(t("d_qty"))}" />
      <input class="input" data-inv-unit placeholder="${esc(t("d_unit_ph"))}" maxlength="16" />
      ${isQty ? "" : `<input class="input" data-inv-price type="number" step="0.01" min="0"
        placeholder="${esc(t("d_unit_price"))}" />
      <span class="inv-sum" data-inv-sum>—</span>`}
      <button class="btn ghost sm" data-inv-del>✕</button></div>`;
    const comp = State._compliance || { countries: [], org_country: "" };
    const meta = (comp.countries || []).find((c) => c.code === comp.org_country) || { vat_rate: 20, vat_prefix: "" };
    const vatDoc = kind === "invoice" || kind === "proforma_doc";
    const prefVatA = meta.vat_prefix && org.reg_number ? meta.vat_prefix + org.reg_number : "";
    const vatBlock = vatDoc ? `
      <div class="row2">
        <label class="field"><span>${esc(t("d_vat_a"))}</span><input class="input" id="dVatA" value="${esc(prefVatA)}" /></label>
        <label class="field"><span>${esc(t("d_vat_b"))}</span><input class="input" id="dVatB" /></label>
      </div>
      <div class="row2">
        <label class="field"><span>${esc(t("d_vat_rate"))}</span><input class="input" id="dVatRate" type="number" min="0" max="30" step="0.5" value="${meta.vat_rate}" /></label>
        <label class="field checkbox-field"><input type="checkbox" id="dRC" /> <span>${esc(t("d_reverse_charge"))}</span></label>
      </div>` : "";
    const today = new Date().toISOString().slice(0, 10);
    // Dates and where to pay. The date of supply is a separate legal fact from
    // the date the invoice was written, so it is asked for rather than assumed.
    const legalBlock = vatDoc ? `
      <div class="row2">
        <label class="field"><span>${esc(t("d_date"))}</span><input class="input" id="dDate" type="date" value="${today}" /></label>
        <label class="field"><span>${esc(t("d_tax_date"))}</span><input class="input" id="dTaxDate" type="date" /></label>
      </div>
      <div class="row2">
        <label class="field"><span>${esc(t("d_terms"))}</span><input class="input" id="dTerms" type="number" min="0" max="365" placeholder="14" /></label>
        <label class="field"><span>${esc(t("d_due"))}</span><input class="input" id="dDue" type="date" /></label>
      </div>
      <div class="row2">
        <label class="field"><span>${esc(t("d_addr_a"))}</span><input class="input" id="dAdrA" maxlength="160" /></label>
        <label class="field"><span>${esc(t("d_addr_b"))}</span><input class="input" id="dAdrB" maxlength="160" /></label>
      </div>
      <div class="section-title">${esc(t("d_pay_title"))}</div>
      <div class="row2">
        <label class="field"><span>IBAN</span><input class="input" id="dIban" maxlength="40" /></label>
        <label class="field"><span>BIC</span><input class="input" id="dBic" maxlength="16" /></label>
      </div>
      <div class="row2">
        <label class="field"><span>${esc(t("d_bank"))}</span><input class="input" id="dBank" maxlength="80" /></label>
        <label class="field"><span>${esc(t("d_compiled"))}</span><input class="input" id="dBy" maxlength="80" value="${esc((State.user || {}).name || "")}" /></label>
      </div>` : "";

    openModal(docTitle(kind), `${partyRows}
      <label class="field"><span>${esc(t("d_number"))}</span><input class="input" id="dNum" placeholder="${esc(t("d_number_ph"))}" /></label>
      ${vatBlock}
      ${legalBlock}
      <div class="section-title">${esc(t("d_lines"))}</div>
      <div id="invLines">${lineRow() + lineRow()}</div>
      <button class="btn ghost sm mt8" id="invAdd">＋ ${esc(t("d_add_line"))}</button>
      <div class="inv-grand" id="invGrand"></div>
      <label class="field checkbox-field mt12"><input type="checkbox" id="dKeep" checked>
        <span>${esc(t("ar_keep"))}</span></label>`,
      `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="dGen">${esc(t("doc_generate"))} ↗</button>`);

    const recalc = () => {
      let net = 0;
      $$(".inv-line5").forEach((row) => {
        const q = parseFloat(row.querySelector("[data-inv-qty]").value) || 0;
        const pEl = row.querySelector("[data-inv-price]");
        const sEl = row.querySelector("[data-inv-sum]");
        if (!pEl || !sEl) return;
        const p = parseFloat(pEl.value) || 0;
        const line = Math.round(q * p * 100) / 100;
        sEl.textContent = q && p ? displayMoneyText("EUR " + line.toFixed(2)) : "—";
        net += line;
      });
      const g = $("#invGrand");
      if (!g || isQty) return;
      const vr = $("#dVatRate"), rc = $("#dRC");
      const rate = rc && rc.checked ? 0 : (parseFloat(vr && vr.value) || 0);
      const vat = Math.round(net * rate) / 100;
      g.innerHTML = net
        ? `<span>${esc(t("d_net"))}: <b>${esc(displayMoneyText("EUR " + net.toFixed(2)))}</b></span>
           <span>${esc(t("d_vat_amount"))}: <b>${esc(displayMoneyText("EUR " + vat.toFixed(2)))}</b></span>
           <span>${esc(t("d_gross"))}: <b>${esc(displayMoneyText("EUR " + (net + vat).toFixed(2)))}</b></span>`
        : "";
    };

    const wire = () => {
      $$("[data-inv-del]").forEach((b) => { b.onclick = () => {
        if ($$(".inv-line5").length > 1) { b.closest(".inv-line5").remove(); recalc(); }
      }; });
      $$(".inv-line5 input").forEach((i) => { i.oninput = recalc; });
    };
    wire();
    ["#dVatRate", "#dRC"].forEach((s) => { const e = $(s); if (e) e.addEventListener("input", recalc); });
    $("#invAdd").addEventListener("click", () => { $("#invLines").insertAdjacentHTML("beforeend", lineRow()); wire(); });
    recalc();

    $("#dGen").addEventListener("click", () => {
      const q = new URLSearchParams();
      const put = (key, sel) => { const e = $(sel); if (e && e.value.trim()) q.set(key, e.value.trim()); };
      q.set("a", $("#dA").value.trim()); q.set("b", $("#dB").value.trim());
      put("rega", "#dRegA"); put("regb", "#dRegB"); put("number", "#dNum");
      put("vata", "#dVatA"); put("vatb", "#dVatB"); put("vatrate", "#dVatRate");
      put("date", "#dDate"); put("taxdate", "#dTaxDate"); put("due", "#dDue"); put("terms", "#dTerms");
      put("adra", "#dAdrA"); put("adrb", "#dAdrB");
      put("iban", "#dIban"); put("bic", "#dBic"); put("bank", "#dBank"); put("by", "#dBy");
      const rc = $("#dRC");
      if (rc && rc.checked) q.set("rc", "1");
      $$(".inv-line5").forEach((row) => {
        const d = row.querySelector("[data-inv-desc]").value.trim();
        const qty = row.querySelector("[data-inv-qty]").value.trim();
        const unit = row.querySelector("[data-inv-unit]").value.trim();
        const pEl = row.querySelector("[data-inv-price]");
        const price = pEl ? pEl.value.trim() : "";
        if (!d && !qty && !price) return;
        q.append("desc", d || "-");
        q.append("qty", qty);
        q.append("unit", unit);
        q.append("price", price);
        // The bare amount stays alongside, so a line given only as a total
        // still produces one.
        q.append("amount", qty && price ? String(Number(qty) * Number(price)) : (price || qty || "0"));
      });
      const docPath = "/docs/" + kind + "?" + docLangParam(q).toString();
      window.open(docPath, "_blank", "noopener");

      // Keep a copy as a PDF, not as a link that rebuilds it. A template
      // changes, a price list changes, and last March's invoice has to stay
      // last March's.
      if ($("#dKeep") && $("#dKeep").checked) {
        API.fileArchive({ path: docPath, title: (docTitle(kind) + " " + $("#dNum").value.trim()).trim() })
          .then(() => toast(t("ar_kept"), "ok"))
          .catch((err) => toast(errMsg(err), "err"));
      }

      // Record it, so it can be chased, reconciled against the bank statement
      // and counted. A sheet nobody remembers issuing is a sheet nobody
      // notices going unpaid.
      if (kind === "invoice" || kind === "proforma_doc") {
        const lines = $$(".inv-line5").map((r2) => ({
          desc: r2.querySelector("[data-inv-desc]").value.trim(),
          qty: r2.querySelector("[data-inv-qty]").value.trim(),
          unit: r2.querySelector("[data-inv-unit]").value.trim(),
          price: (r2.querySelector("[data-inv-price]") || {}).value || "",
        })).filter((l) => l.desc || l.qty || l.price);
        const sel = $("#dLang");
        API.invoiceCreate({
          kind: kind === "invoice" ? "invoice" : "proforma",
          number: $("#dNum").value.trim(), customer: $("#dB").value.trim() || "-",
          customer_reg: $("#dRegB").value.trim(),
          customer_vat: ($("#dVatB") || {}).value || "",
          customer_addr: ($("#dAdrB") || {}).value || "",
          lines: lines, vat_rate: Number(($("#dVatRate") || {}).value || 0),
          reverse_charge: !!(rc && rc.checked),
          issue_date: ($("#dDate") || {}).value || "",
          tax_date: ($("#dTaxDate") || {}).value || "",
          due_date: ($("#dDue") || {}).value || "",
          lang: sel ? sel.value : LANG,
          query: Object.fromEntries(q.entries()),
        }).then(() => toast(t("mn_recorded"), "ok"))
          // A record that failed to save must not look like a document that
          // failed to generate: the sheet is already open in the other tab.
          .catch((err) => toast(errMsg(err), "err"));
      }
      closeModal();
    });
    return;
  }

  const showNotes = DOC_NOTES.indexOf(kind) !== -1;
  const showAmount = DOC_AMOUNT.indexOf(kind) !== -1;
  openModal(docTitle(kind), `${partyRows}
    <label class="field"><span>${esc(t("d_subject"))}</span><textarea class="textarea" id="dSubj" rows="2"></textarea></label>
    ${showAmount ? `<label class="field"><span>${esc(t("d_amount"))}</span><input class="input" id="dAmount" type="number" step="0.01" min="0" placeholder="0.00" /></label>` : ""}
    ${showNotes ? `<label class="field"><span>${esc(t("d_notes"))}</span><input class="input" id="dNotes" /></label>` : ""}
    <label class="field"><span>${esc(t("d_city"))}</span><input class="input" id="dCity" /></label>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="dGen">${esc(t("doc_generate"))} ↗</button>`);
  $("#dGen").addEventListener("click", () => {
    const q = new URLSearchParams({
      a: $("#dA").value.trim(), b: $("#dB").value.trim(),
      rega: $("#dRegA").value.trim(), regb: $("#dRegB").value.trim(),
      subject: $("#dSubj").value.trim(), city: $("#dCity").value.trim(),
    });
    const n = $("#dNotes"); if (n && n.value.trim()) q.set("notes", n.value.trim());
    const am = $("#dAmount"); if (am && am.value.trim()) q.set("amount", am.value.trim());
    window.open("/docs/" + kind + "?" + docLangParam(q).toString(), "_blank", "noopener");
    closeModal();
  });
}

/* =========================================================================
   INTEGRATIONS: connector directory + outbound webhooks + inbound keys
   Everything a company already runs daily can receive Seam events.
   ======================================================================== */
/* A product name stays as it is in every language - "Еконт" is called Еконт in
   Lisbon. A generic description is not a name and must be translated, so it is
   marked with a leading section sign and resolved when the row is drawn. */
const CONNECTORS = [
  ["conn_cat_courier", "🚚", ["Еконт", "Speedy", "DPD", "DHL", "GLS", "Sameday", "FAN Courier", "ACS"]],
  ["conn_cat_accounting", "📊", ["Microinvest", "SAP", "Oracle NetSuite", "Odoo", "1C", "Xero", "QuickBooks", "Ajur"]],
  ["conn_cat_commerce", "🛍️", ["Shopify", "WooCommerce", "Magento", "PrestaShop", "eMAG", "Amazon", "OLX", "Etsy"]],
  ["conn_cat_wms", "🏭", ["§conn_wms_generic", "Zoho Inventory", "Katana", "Excel / CSV"]],
  ["conn_cat_pos", "🧾", ["§conn_pos_generic", "Datecs", "Tremol", "Square"]],
  ["conn_cat_comms", "💬", ["Slack", "Microsoft Teams", "Viber", "Telegram", "Email / SMTP"]],
  ["conn_cat_automation", "⚡", ["Zapier", "Make", "n8n", "Power Automate"]],
  ["conn_cat_payments", "💳", ["Stripe", "PayPal", "Revolut", "Lemon Squeezy", "Wise"]],
  ["conn_cat_office", "📅", ["Google Calendar", "Outlook", "Google Sheets", "Trello", "Asana"]],
  ["conn_cat_custom", "🔧", ["Webhook", "REST API", "§conn_csv_export"]],
];

function connName(n) { return n[0] === "§" ? t(n.slice(1)) : n; }

/* =========================================================================
   NATIVE CONNECTORS: calendar, banking, cloud storage
   ======================================================================== */
function connectorsSection(cal, cloud) {
  const cfg = (cloud || {}).cloud || null;
  return `<div class="section-title">${esc(t("cn_title"))}</div>
    <div class="an-grid">
      <div class="card pad">
        <div class="an-head"><span class="an-title">📅 ${esc(t("cn_cal"))}</span></div>
        <p class="dim" style="font-size:12.5px">${esc(t("cn_cal_hint"))}</p>
        ${cal ? `<div class="hashrow mt12"><code id="calUrl">${esc(cal.url)}</code>
          <button id="calCopy">${esc(t("int_copy"))}</button></div>
          <div class="flex gap8 mt12">
            <a class="btn ghost sm" href="${esc(cal.url)}">${esc(t("cn_cal_open"))}</a>
            <button class="btn ghost sm" id="calRotate">${esc(t("cn_cal_rotate"))}</button>
          </div>
          <div class="dim mt8" style="font-size:11.5px">${esc(t("cn_cal_note"))}</div>` : ""}
      </div>

      <div class="card pad">
        <div class="an-head"><span class="an-title">☁️ ${esc(t("cn_cloud"))}</span>
          <span class="badge ${cfg && cfg.ready ? "ok" : ""}">${esc(cfg && cfg.ready ? t("qt_ready") : t("qt_unset"))}</span></div>
        <p class="dim" style="font-size:12.5px">${esc(t("cn_cloud_hint"))}</p>
        <label class="field"><span>${esc(t("cn_provider"))}</span>
          <select class="select" id="clProv"><option value="">${esc(t("qt_none"))}</option>
            ${(cloud ? cloud.providers : []).map((p) =>
              `<option value="${p}"${cfg && cfg.provider === p ? " selected" : ""}>${esc(t("cn_cl_" + p))}</option>`).join("")}
          </select></label>
        <div id="clFields"></div>
        <div class="flex gap8 mt12">
          <button class="btn primary sm" id="clSave">${esc(t("save"))}</button>
          <button class="btn ghost sm" id="clTest"${cfg && cfg.ready ? "" : " disabled"}>${esc(t("qt_test"))}</button>
        </div>
      </div>
    </div>

    <div class="section-title">🏦 ${esc(t("cn_bank"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("cn_bank_hint"))}</p>
      <div class="flex gap8 center wrap mt12">
        <input type="file" id="bkFile" accept=".xml,.txt,.sta,.940" />
        <button class="btn primary sm" id="bkUpload">${esc(t("cn_bank_read"))}</button>
      </div>
      <div id="bkResult" class="mt12"></div>
      <div class="dim mt8" style="font-size:11.5px">${esc(t("cn_bank_formats"))}</div>
    </div>`;
}

function wireConnectors(cal, cloud, reload) {
  const cfg = (cloud || {}).cloud || null;

  const cc = $("#calCopy");
  if (cc) cc.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#calUrl").textContent); toast(t("int_copied"), "ok"); }
    catch (e) { toast(errMsg(e), "err"); }
  });
  const cr = $("#calRotate");
  if (cr) cr.addEventListener("click", async () => {
    if (!confirm(t("cn_cal_rotate_ask"))) return;
    try { await API.calendarRotate(); toast(t("saved"), "ok"); reload(); }
    catch (err) { toast(errMsg(err), "err"); }
  });

  const FIELDS = {
    s3: [["endpoint", "Endpoint URL"], ["region", t("cn_region")], ["bucket", t("cn_bucket")],
         ["prefix", t("cn_prefix")], ["access_key", "Access key"], ["secret_key", "Secret key"]],
    webdav: [["endpoint", "WebDAV URL"], ["prefix", t("cn_prefix")],
             ["username", t("nt_username")], ["password", t("nt_password")]],
  };
  const drawCl = () => {
    const box = $("#clFields");
    if (!box) return;
    const p = $("#clProv").value;
    box.innerHTML = !p ? "" : FIELDS[p].map(([k, label]) => {
      const stored = (cfg && cfg.secrets_set || {})[k];
      const val = (cfg && cfg.provider === p && !stored) ? (cfg[k] || "") : "";
      const secret = k === "secret_key" || k === "password";
      return `<label class="field"><span>${esc(label)}</span>
        <input class="input" data-cl="${k}" type="${secret ? "password" : "text"}" autocomplete="off"
          value="${esc(val)}" placeholder="${stored ? esc(t("qt_stored")) : ""}" /></label>`;
    }).join("");
  };
  if ($("#clProv")) { $("#clProv").addEventListener("change", drawCl); drawCl(); }
  if ($("#clSave")) $("#clSave").addEventListener("click", async () => {
    const payload = { provider: $("#clProv").value };
    $$("[data-cl]").forEach((i) => { payload[i.dataset.cl] = i.value.trim(); });
    try { await API.cloudSave(payload); toast(t("saved"), "ok"); reload(); }
    catch (err) { toast(errMsg(err), "err"); }
  });
  if ($("#clTest")) $("#clTest").addEventListener("click", async () => {
    try { const r = await API.cloudTest(); toast(t("qt_test_ok") + " " + (r.detail || ""), "ok"); }
    catch (err) { toast(errMsg(err), "err"); }
  });

  const bu = $("#bkUpload");
  if (bu) bu.addEventListener("click", () => {
    const f = ($("#bkFile").files || [])[0];
    if (!f) return toast(t("cn_bank_pick"), "err");
    const rd = new FileReader();
    rd.onload = async () => {
      try {
        const r = await API.bankStatement(rd.result);
        renderBankMatches(r, reload);
      } catch (err) { toast(errMsg(err), "err"); }
    };
    rd.readAsText(f);
  });
}

function renderBankMatches(r, reload) {
  const box = $("#bkResult");
  const matched = r.matches.filter((m) => m.payout_id);
  box.innerHTML = `
    <div class="flex gap8 center wrap">
      <span class="badge">${esc(r.format)}</span>
      <span class="dim">${esc(t("cn_bank_read_n", { n: r.transactions }))}, ${esc(t("cn_bank_read_c", { n: r.credits }))}</span>
      <span class="badge ${matched.length ? "ok" : "warn"}">${esc(t("cn_bank_matched", { n: matched.length }))}</span>
    </div>
    <div class="an-rows mt12">${r.matches.map((m) => `
      <div class="an-row">
        <div class="grow"><b>${esc(m.transaction.amount)} ${esc(m.transaction.currency)}</b>
          <div class="dim" style="font-size:11.5px">${esc(m.transaction.date)} · ${esc(m.transaction.reference || "")}</div>
          ${m.payout ? `<div class="dim" style="font-size:11.5px">→ ${esc(m.payout.description || m.payout.reference)}</div>` : ""}
        </div>
        ${m.payout_id
          ? `<label class="flex gap8 center"><input type="checkbox" data-bk="${m.payout_id}" checked />
             <span class="badge ok">${esc(t("cn_bank_by_" + m.reason))}</span></label>`
          : `<span class="badge ${m.reason === "ambiguous" ? "warn" : ""}">${esc(m.reason === "ambiguous" ? t("cn_bank_ambiguous") : t("cn_bank_nomatch"))}</span>`}
      </div>`).join("")}</div>
    ${matched.length ? `<button class="btn primary sm mt12" id="bkConfirm">${esc(t("cn_bank_confirm"))}</button>` : ""}`;

  const bc = $("#bkConfirm");
  if (bc) bc.addEventListener("click", async () => {
    const ids = $$("[data-bk]").filter((i) => i.checked).map((i) => Number(i.dataset.bk));
    if (!ids.length) return;
    try {
      const res = await API.bankReconcile(ids, r.format.toUpperCase());
      toast(t("cn_bank_done", { n: res.reconciled }), "ok");
      reload();
    } catch (err) { toast(errMsg(err), "err"); }
  });
}

/* =========================================================================
   APPROVAL CHAINS (roadmap 9) - the settings side; the per-order card that
   uses them lives with the order detail.
   ======================================================================== */
function openChainEditor(members, templates, done) {
  const stepRow = (s) => `<div class="au-row" data-step>
    <input class="input" data-s-label placeholder="${esc(t("ap_step_label"))}" value="${esc((s && s.label) || "")}" />
    <select class="select" data-s-user>
      <option value="">${esc(t("ap_anyone"))}</option>
      ${members.map((m) => `<option value="${m.id}"${s && s.user_id === m.id ? " selected" : ""}>${esc(m.name)}</option>`).join("")}
    </select>
    <button class="btn ghost sm" data-s-del type="button">✕</button>
  </div>`;

  openModal(t("ap_chain_new"), `
    <label class="field"><span>${esc(t("ap_chain_name"))} *</span>
      <input class="input" id="chName" maxlength="80" /></label>
    <div class="grid2">
      <label class="field"><span>${esc(t("ap_chain_tpl"))}</span>
        <select class="select" id="chTpl"><option value="">${esc(t("ap_any_tpl"))}</option>
          ${templates.map((k) => `<option value="${esc(k)}">${esc(tplLabel(k, k))}</option>`).join("")}</select></label>
      <label class="field"><span>${esc(t("ap_chain_threshold"))}</span>
        <input class="input" id="chThr" type="number" min="0" step="100" value="0" /></label>
    </div>
    <div class="field"><span>${esc(t("ap_chain_steps"))} *</span>
      <div id="chSteps">${stepRow(null)}</div>
      <button class="btn ghost sm" id="chAdd" type="button">＋ ${esc(t("ap_add_step"))}</button>
      <div class="dim" style="font-size:11.5px;margin-top:6px">${esc(t("ap_chain_hint"))}</div></div>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
     <button class="btn primary" id="chSave">${esc(t("save"))}</button>`);

  const wire = () => $$("[data-s-del]").forEach((b) => b.onclick = () => {
    if ($$("[data-step]").length > 1) b.closest("[data-step]").remove();
  });
  wire();
  $("#chAdd").onclick = () => { $("#chSteps").insertAdjacentHTML("beforeend", stepRow(null)); wire(); };
  $("#chSave").onclick = async () => {
    const steps = $$("[data-step]").map((r) => ({
      label: r.querySelector("[data-s-label]").value.trim(),
      user_id: Number(r.querySelector("[data-s-user]").value) || null,
    })).filter((s) => s.label);
    try {
      await API.chainCreate({
        name: $("#chName").value.trim(), template: $("#chTpl").value || null,
        threshold: Number($("#chThr").value) || 0, steps,
      });
      closeModal(); toast(t("saved"), "ok"); done();
    } catch (err) { toast(errMsg(err), "err"); }
  };
}

function chainSection(ch) {
  if (!ch) return "";
  const rows = ch.chains.length ? ch.chains.map((c) => `
    <div class="au-rule">
      <div class="grow"><b>${esc(c.name)}</b>
        <div class="dim" style="font-size:11.5px">${c.steps.map((s) => esc(s.label)).join(" → ")}</div>
        <div class="dim" style="font-size:11px">${
          c.template ? esc(tplLabel(c.template, c.template)) : esc(t("ap_any_tpl"))
        }${c.threshold ? " · " + esc(t("ap_above", { v: displayMoneyText("EUR " + c.threshold) })) : ""}</div>
      </div>
      <span class="badge ${c.active ? "ok" : ""}">${esc(c.active ? t("au_on") : t("au_off"))}</span>
      <button class="btn ghost sm" data-ch-toggle="${c.id}">${esc(c.active ? t("au_disable") : t("au_enable"))}</button>
      <button class="btn ghost sm" data-ch-del="${c.id}">${esc(t("int_revoke"))}</button>
    </div>`).join("") : `<p class="muted" style="font-size:13px">${esc(t("ap_chain_empty"))}</p>`;

  return `<div class="section-title">${esc(t("ap_chains_title"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("ap_chains_hint"))}</p>
      <div class="mt12">${rows}</div>
      <button class="btn primary sm mt12" id="chNew">＋ ${esc(t("ap_chain_new"))}</button>
    </div>`;
}

function wireChains(ch, reload) {
  if (!ch) return;
  const nb = $("#chNew");
  const templates = Object.keys(State.templates || {});
  if (nb) nb.addEventListener("click", () => openChainEditor(ch.members, templates, reload));
  $$("[data-ch-toggle]").forEach((b) => b.addEventListener("click", async () => {
    const c = ch.chains.find((x) => x.id == b.dataset.chToggle);
    try { await API.chainToggle(c.id, !c.active); reload(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-ch-del]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.chainDelete(b.dataset.chDel); reload(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
}

/* =========================================================================
   WORKFLOW AUTOMATION (roadmap 3)

   Rules are data - a trigger, conditions that must all hold, and actions. The
   builder below writes that shape; the engine on the server runs it inside the
   same transaction as the event that fired it and logs every run.
   ======================================================================== */
function autoRuleSummary(r) {
  const act = (a) => {
    if (a.type === "notify") return t("au_a_notify");
    if (a.type === "message") return t("au_a_message");
    if (a.type === "set_status") return t("au_a_set_status") + " → " + esc(a.status || "");
    if (a.type === "create_document") return t("au_a_create_document") + " → " + t("doc_" + a.doc);
    if (a.type === "request_signature") return t("au_a_signature");
    if (a.type === "webhook") return t("au_a_webhook");
    if (a.type === "flag") return t("au_a_flag");
    return a.type;
  };
  return r.actions.map(act).join(" · ");
}

function autoCondRow(c) {
  return `<div class="au-row" data-cond>
    <input class="input" data-c-field placeholder="${esc(t("au_field_ph"))}" value="${esc((c && c.field) || "")}" />
    <select class="select" data-c-op>${["eq", "ne", "contains", "gt", "lt", "any"]
      .map((o) => `<option value="${o}"${c && c.op === o ? " selected" : ""}>${esc(t("au_op_" + o))}</option>`).join("")}</select>
    <input class="input" data-c-value placeholder="${esc(t("au_value_ph"))}" value="${esc((c && c.value) || "")}" />
    <button class="btn ghost sm" data-c-del type="button">✕</button>
  </div>`;
}

function autoActRow(a, stages, docs) {
  const type = (a && a.type) || "notify";
  const opt = (v) => `<option value="${v}"${type === v ? " selected" : ""}>${esc(t("au_a_" + v))}</option>`;
  return `<div class="au-row act" data-act>
    <select class="select" data-a-type>${["notify", "message", "set_status", "request_signature", "create_document", "webhook", "flag"].map(opt).join("")}</select>
    <div class="au-arg" data-a-arg>${autoActArg(type, a, stages, docs)}</div>
    <button class="btn ghost sm" data-a-del type="button">✕</button>
  </div>`;
}

function autoActArg(type, a, stages, docs) {
  a = a || {};
  if (type === "create_document") {
    return `<select class="select" data-a-doc>${(docs || []).map((d) =>
      `<option value="${esc(d)}"${a.doc === d ? " selected" : ""}>${esc(t("doc_" + d))}</option>`).join("")}</select>`;
  }
  if (type === "set_status") {
    return `<select class="select" data-a-status>${stages.map((s) =>
      `<option value="${esc(s.key)}"${a.status === s.key ? " selected" : ""}>${esc(s.label)}</option>`).join("")}</select>`;
  }
  if (type === "request_signature") {
    return `<select class="select" data-a-level>${["simple", "advanced", "qualified"].map((l) =>
      `<option value="${l}"${a.level === l ? " selected" : ""}>${esc(t("sig_l_" + l))}</option>`).join("")}</select>
      <input class="input" data-a-email placeholder="${esc(t("sig_signer"))} e-mail" value="${esc(a.email || "")}" />`;
  }
  if (type === "webhook") {
    return `<input class="input" data-a-url placeholder="https://…" value="${esc(a.url || "")}" />`;
  }
  return `<input class="input" data-a-text placeholder="${esc(t("au_text_ph"))}" value="${esc(a.text || "")}" />`;
}

function openAutomationEditor(rule, triggers, stages, done, docs) {
  const r = rule || { name: "", trigger: "order.status_changed", conditions: [], actions: [{ type: "notify" }] };
  openModal(rule ? t("au_edit") : t("au_new"), `
    <label class="field"><span>${esc(t("au_name"))} *</span>
      <input class="input" id="auName" maxlength="80" value="${esc(r.name)}" /></label>
    <label class="field"><span>${esc(t("au_when"))}</span>
      <select class="select" id="auTrigger">${triggers.map((tg) =>
        `<option value="${esc(tg)}"${r.trigger === tg ? " selected" : ""}>${esc(t("au_t_" + tg.replace(/\./g, "_")))}</option>`).join("")}</select></label>
    <div class="field"><span>${esc(t("au_if"))}</span>
      <div id="auConds">${(r.conditions || []).map(autoCondRow).join("")}</div>
      <button class="btn ghost sm" id="auAddCond" type="button">＋ ${esc(t("au_add_cond"))}</button>
      <div class="dim" style="font-size:11.5px;margin-top:6px">${esc(t("au_cond_hint"))}</div></div>
    <div class="field"><span>${esc(t("au_then"))} *</span>
      <div id="auActs">${(r.actions || []).map((a) => autoActRow(a, stages, docs)).join("")}</div>
      <button class="btn ghost sm" id="auAddAct" type="button">＋ ${esc(t("au_add_act"))}</button>
      <div class="dim" style="font-size:11.5px;margin-top:6px">${esc(t("au_text_hint"))}</div></div>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
     <button class="btn primary" id="auSave">${esc(t("save"))}</button>`);

  const wireRows = () => {
    $$("[data-c-del]").forEach((b) => b.onclick = () => b.closest("[data-cond]").remove());
    $$("[data-a-del]").forEach((b) => b.onclick = () => b.closest("[data-act]").remove());
    $$("[data-a-type]").forEach((s) => s.onchange = () => {
      s.parentElement.querySelector("[data-a-arg]").innerHTML = autoActArg(s.value, {}, stages, docs);
    });
  };
  wireRows();
  $("#auAddCond").onclick = () => { $("#auConds").insertAdjacentHTML("beforeend", autoCondRow()); wireRows(); };
  $("#auAddAct").onclick = () => { $("#auActs").insertAdjacentHTML("beforeend", autoActRow(null, stages, docs)); wireRows(); };

  $("#auSave").onclick = async () => {
    const payload = {
      name: $("#auName").value.trim(),
      trigger: $("#auTrigger").value,
      conditions: $$("[data-cond]").map((row) => ({
        field: row.querySelector("[data-c-field]").value.trim(),
        op: row.querySelector("[data-c-op]").value,
        value: row.querySelector("[data-c-value]").value.trim(),
      })).filter((c) => c.field),
      actions: $$("[data-act]").map((row) => {
        const a = { type: row.querySelector("[data-a-type]").value };
        const pick = (sel, key) => { const el = row.querySelector(sel); if (el && el.value.trim()) a[key] = el.value.trim(); };
        pick("[data-a-text]", "text"); pick("[data-a-status]", "status");
        pick("[data-a-level]", "level"); pick("[data-a-email]", "email");
        pick("[data-a-url]", "url"); pick("[data-a-doc]", "doc");
        return a;
      }),
      active: rule ? rule.active : true,
    };
    try {
      if (rule) await API.automationUpdate(rule.id, payload);
      else await API.automationCreate(payload);
      closeModal(); toast(t("saved"), "ok"); done();
    } catch (err) { toast(errMsg(err), "err"); }
  };
}

function automationSection(au, stages) {
  if (!au) return "";
  const rows = au.automations.length ? au.automations.map((r) => `
    <div class="au-rule">
      <div class="grow">
        <b>${esc(r.name)}</b>
        <div class="dim" style="font-size:11.5px">${esc(t("au_t_" + r.trigger.replace(/\./g, "_")))} → ${esc(autoRuleSummary(r))}</div>
        <div class="dim" style="font-size:11px">${esc(t("au_runs", { n: r.runs }))}${
          r.last_status ? " · " + esc(r.last_status === "ok" ? t("au_ok") : t("au_err")) : ""}${
          r.recent.length ? " · " + esc(r.recent[0].detail || "") : ""}</div>
      </div>
      <span class="badge ${r.active ? "ok" : ""}">${esc(r.active ? t("au_on") : t("au_off"))}</span>
      <button class="btn ghost sm" data-au-toggle="${r.id}">${esc(r.active ? t("au_disable") : t("au_enable"))}</button>
      <button class="btn ghost sm" data-au-edit="${r.id}">${esc(t("au_edit_btn"))}</button>
      <button class="btn ghost sm" data-au-del="${r.id}">${esc(t("int_revoke"))}</button>
    </div>`).join("") : `<p class="muted" style="font-size:13px">${esc(t("au_empty"))}</p>`;

  return `<div class="section-title">${esc(t("au_title"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("au_hint"))}</p>
      <div class="mt12">${rows}</div>
      <button class="btn primary sm mt12" id="auNew">＋ ${esc(t("au_new"))}</button>
    </div>`;
}

function wireAutomations(au, stages, reload) {
  if (!au) return;
  const nb = $("#auNew");
  if (nb) nb.addEventListener("click", () => openAutomationEditor(null, au.triggers, stages, reload, au.docs));
  $$("[data-au-edit]").forEach((b) => b.addEventListener("click", () => {
    const r = au.automations.find((x) => x.id == b.dataset.auEdit);
    openAutomationEditor(r, au.triggers, stages, reload, au.docs);
  }));
  $$("[data-au-toggle]").forEach((b) => b.addEventListener("click", async () => {
    const r = au.automations.find((x) => x.id == b.dataset.auToggle);
    try { await API.automationUpdate(r.id, { active: !r.active }); reload(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-au-del]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.automationDelete(b.dataset.auDel); reload(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
}

/* Qualified electronic signature: which trust service provider this company
   signs through. The list is driven by the company's country, because the law
   that gives the signature its effect is the law of that country. */
function qtspSection(q) {
  if (!q) return "";
  const cfg = q.config || null;
  const home = q.providers.filter((p) => p.home);
  const cross = q.providers.filter((p) => !p.home);
  const apiLabel = { csc: t("qt_api_csc"), native: t("qt_api_native"), bridge: t("qt_api_bridge") };
  const opt = (p) => `<option value="${esc(p.key)}"${cfg && cfg.provider === p.key ? " selected" : ""}>${esc(p.name)} · ${esc(apiLabel[p.api] || p.api)}</option>`;
  const sel = `<select class="select" id="qtProvider">
      <option value="">${esc(t("qt_none"))}</option>
      ${home.length ? `<optgroup label="${esc(t("qt_grp_home"))}">${home.map(opt).join("")}</optgroup>` : ""}
      ${cross.length ? `<optgroup label="${esc(t("qt_grp_eu"))}">${cross.map(opt).join("")}</optgroup>` : ""}
    </select>`;

  const badge = cfg && cfg.ready
    ? `<span class="badge ok dot">${esc(t("qt_ready"))}</span>`
    : cfg ? `<span class="badge warn dot">${esc(t("qt_incomplete"))}</span>`
          : `<span class="badge">${esc(t("qt_unset"))}</span>`;

  const law = q.legal ? `<div class="dim mt8" style="font-size:11.5px;line-height:1.6">
      <b>${esc(t("qt_law"))}:</b> ${esc(q.legal.law)}<br>
      <b>${esc(t("qt_supervisor"))}:</b> ${esc(q.legal.supervisor || "—")}
      ${q.legal.list ? ` · <a href="${esc(q.legal.list)}" target="_blank" rel="noopener">${esc(t("qt_trusted_list"))} ↗</a>` : ""}
      ${q.legal.note ? `<br>${esc(q.legal.note)}` : ""}
    </div>` : "";

  return `<div class="section-title">${esc(t("qt_title"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("qt_hint"))}</p>
      <div class="flex gap8 center wrap mt12">${sel}${badge}</div>
      <div id="qtFields" class="mt12"></div>
      <div class="flex gap8 mt12">
        <button class="btn primary sm" id="qtSave">${esc(t("save"))}</button>
        <button class="btn ghost sm" id="qtTest"${cfg && cfg.ready ? "" : " disabled"}>${esc(t("qt_test"))}</button>
      </div>
      ${law}
      <div class="dim mt8" style="font-size:11px">${esc(t("qt_verified"))}: ${esc(q.verified)} ·
        <a href="${esc(q.trusted_list)}" target="_blank" rel="noopener">${esc(t("qt_eu_list"))} ↗</a> ·
        <a href="${esc(q.csc_spec)}" target="_blank" rel="noopener">CSC API v2 ↗</a></div>
    </div>`;
}

function wireQtsp(q) {
  const selEl = $("#qtProvider");
  if (!selEl) return;
  const FIELD_LABEL = {
    base_url: t("qt_f_base"), client_id: t("qt_f_cid"), client_secret: t("qt_f_secret"),
    api_key: t("qt_f_apikey"), relying_party_id: t("qt_f_rpid"),
    client_cert: t("qt_f_cert"), client_key: t("qt_f_key"),
  };
  const draw = () => {
    const p = q.providers.find((x) => x.key === selEl.value);
    const box = $("#qtFields");
    if (!p) { box.innerHTML = ""; return; }
    const set = ((q.config || {}).secrets_set) || {};
    const stored = q.config && q.config.provider === p.key;
    box.innerHTML = `
      ${p.note ? `<p class="dim" style="font-size:12px;margin-bottom:10px">${esc(p.note)}</p>` : ""}
      ${p.needs.map((f) => `<label class="field"><span>${esc(FIELD_LABEL[f] || f)}</span>
        <input class="input" data-qtf="${esc(f)}" type="${f.includes("secret") || f === "api_key" ? "password" : "text"}"
          autocomplete="off" value="${stored && !set[f] ? esc((q.config || {})[f] || "") : ""}"
          placeholder="${set[f] ? esc(t("qt_stored")) : ""}" /></label>`).join("")}
      ${p.needs.length === 0 ? `<p class="dim" style="font-size:12px">${esc(t("qt_bridge_hint"))}</p>` : ""}
      <div class="dim" style="font-size:11.5px">
        <a href="${esc(p.signup || p.site)}" target="_blank" rel="noopener">${esc(t("qt_signup"))} ↗</a>
        ${p.docs ? ` · <a href="${esc(p.docs)}" target="_blank" rel="noopener">${esc(t("qt_docs"))} ↗</a>` : ""}
      </div>`;
  };
  selEl.addEventListener("change", draw);
  draw();

  $("#qtSave").addEventListener("click", async () => {
    const payload = { provider: selEl.value };
    $$("[data-qtf]").forEach((i) => { payload[i.dataset.qtf] = i.value.trim(); });
    try { await API.qtspSave(payload); toast(t("saved"), "ok"); viewIntegrations(); }
    catch (err) { toast(errMsg(err), "err"); }
  });
  const tb = $("#qtTest");
  if (tb) tb.addEventListener("click", async () => {
    tb.disabled = true;
    try { const r = await API.qtspTest(); toast(t("qt_test_ok") + (r.credentials != null ? ` (${r.credentials})` : ""), "ok"); }
    catch (err) { toast(errMsg(err), "err"); }
    tb.disabled = false;
  });
}

/* ---- The network -------------------------------------------------------- *
 * Reference, introductions and needs share one screen because they are one
 * idea: reaching companies you do not yet work with, without giving away the
 * ones you already do.                                                        */
const REF_FIELD_KEYS = ["since", "counterparties", "records", "completion",
  "agreement_speed", "on_time", "signatures", "documents", "value_band"];

function refUnit(k) {
  return k === "completion" || k === "on_time" ? "%" : (k === "agreement_speed" ? " " + t("days") : "");
}

function referenceSection(ref) {
  const p = (ref && ref.preview) || {};
  const rows = REF_FIELD_KEYS.map((k) => {
    const on = ref && ref.fields ? ref.fields[k] : false;
    const val = p[k] === null || p[k] === undefined || p[k] === "" ? "—" : p[k] + refUnit(k);
    return `<label class="ref-field">
      <input type="checkbox" data-reffield="${k}"${on ? " checked" : ""} />
      <span class="rf-l">${esc(t("nr_f_" + k))}</span>
      <b class="rf-v">${esc(String(val))}</b></label>`;
  }).join("");

  return `<div class="section-title">${esc(t("nr_title"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("nr_sub"))}</p>
      <div class="ref-fields mt12">${rows}</div>
      <div class="note-box mt12">${esc(t("nr_note"))}</div>
      ${ref && ref.published ? `
        <div class="hashrow mt12"><code id="refUrl">${esc(location.origin + ref.url.replace(location.origin, ""))}</code>
          <button id="refCopy">${esc(t("nr_copy"))}</button></div>
        <div class="dim mt8" style="font-size:11.5px">${esc(t("nr_published_at"))}: ${esc(ref.published_at || "")} · ${esc(t("nr_views", { n: ref.views || 0 }))}</div>
        <div class="flex gap8 wrap mt12">
          <a class="btn ghost sm" href="${esc(ref.url)}" target="_blank" rel="noopener">${esc(t("nr_open"))} ↗</a>
          <button class="btn primary sm" id="refSave">${esc(t("nr_update"))}</button>
          <button class="btn ghost sm" id="refRevoke">${esc(t("nr_revoke"))}</button>
        </div>` : `
        <div class="dim mt12" style="font-size:12.5px">${esc(t("nr_not_published"))}</div>
        <button class="btn primary sm mt12" id="refSave">${esc(t("nr_publish"))}</button>`}
    </div>`;
}

function wireReference(reload) {
  const fields = () => {
    const out = {};
    $$("[data-reffield]").forEach((c) => { out[c.dataset.reffield] = c.checked; });
    return out;
  };
  const cp = $("#refCopy");
  if (cp) cp.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#refUrl").textContent); toast(t("int_copied"), "ok"); }
    catch (e) { toast(errMsg(e), "err"); }
  });
  const sv = $("#refSave");
  if (sv) sv.addEventListener("click", async () => {
    sv.disabled = true;
    try { await API.referencePublish(fields()); toast(t("nr_published_toast"), "ok"); reload(); }
    catch (err) { toast(errMsg(err), "err"); sv.disabled = false; }
  });
  const rv = $("#refRevoke");
  if (rv) rv.addEventListener("click", async () => {
    if (!confirm(t("nr_confirm_revoke"))) return;
    try { await API.referenceRevoke(); toast(t("nr_revoked_toast"), "ok"); reload(); }
    catch (err) { toast(errMsg(err), "err"); }
  });
}

/* An identity already attached to this account. */
function eidLinkRows(links) {
  if (!links.length) return "";
  return links.map((l) => `
    <div class="net-row">
      <div class="grow"><b>${esc(l.name)}</b>
        <div class="dim" style="font-size:11.5px">${esc(l.label || "")}${
          l.last_used ? " · " + esc(t("sec_last_seen")) + " " + relTime(l.last_used) : ""}</div></div>
      <div class="net-side"><button class="btn ghost sm" data-eiddrop="${l.id}">${esc(t("eid_detach"))}</button></div>
    </div>`).join("");
}

/* What the reader's country has. A scheme that is configured here can be
   attached now; one that exists but is not configured says so and links to the
   place an administrator would register, rather than pretending to be absent. */
function eidOfferRows(cat) {
  if (!cat.providers || !cat.providers.length) {
    return `<p class="muted mt8" style="font-size:12.5px">${esc(t("eid_none_country"))}</p>`;
  }
  return cat.providers.map((p) => `
    <div class="net-row ${p.configured ? "" : "off"}">
      <div class="grow"><b>${esc(p.name)}</b>
        <div class="dim" style="font-size:11.5px">${esc(p.authority || "")}${
          p.usable ? "" : " · " + esc(t("eid_not_oidc"))}</div></div>
      <div class="net-side">${p.configured
        ? `<button class="btn primary sm" data-eidlink="${esc(p.key)}">${esc(t("eid_attach"))}</button>`
        : `<a class="btn ghost sm" href="${esc(p.portal)}" target="_blank" rel="noopener">${esc(t("eid_register"))} ↗</a>`}</div>
    </div>`).join("");
}

function orgLine(o) {
  if (!o) return "";
  return `<b>${esc(o.name)}</b>${o.verified ? ` <span class="badge ok dot">${esc(t("account_verified"))}</span>` : ""}
    <div class="dim" style="font-size:11.5px">${esc([o.country, o.reg_number].filter(Boolean).join(" · "))}${
      o.reference_url ? ` · <a href="${esc(o.reference_url)}" target="_blank" rel="noopener">${esc(t("nr_title"))} ↗</a>` : ""}</div>`;
}

function contactBlock(o) {
  if (!o || (!o.emails && !o.contacts)) return "";
  const c = o.contacts || {};
  const bits = (o.emails || []).concat(["phone", "whatsapp", "viber", "telegram", "alt_email"]
    .map((k) => c[k]).filter(Boolean));
  if (!bits.length) return "";
  return `<div class="note-box mt8"><b>${esc(t("ni_contacts"))}</b><br>${bits.map(esc).join(" · ")}</div>`;
}

function introSection(net) {
  const sent = net.introductions.sent, inbox = net.introductions.inbox;
  const st = (s) => `<span class="badge ${s === "accepted" ? "ok" : (s === "declined" ? "warn" : "")} dot">${esc(t("ni_st_" + (s === "forwarded" ? "requested" : s)))}</span>`;

  const sentRows = sent.length ? sent.map((r) => `
    <div class="net-row">
      <div class="grow">${orgLine(r.target)}
        <div class="dim mt4" style="font-size:11.5px">${esc(t("ni_asked_via"))} ${esc((r.via || {}).name || "")}</div>
        ${r.message ? `<div class="net-msg">${esc(r.message)}</div>` : ""}
        ${r.status === "accepted" ? contactBlock(r.target) : ""}</div>
      <div class="net-side">${st(r.status)}
        ${r.status === "requested" ? `<button class="btn ghost sm" data-introdel="${r.id}">${esc(t("ni_withdraw"))}</button>` : ""}</div>
    </div>`).join("") : `<p class="muted" style="font-size:13px">${esc(t("ni_none"))}</p>`;

  const inboxRows = inbox.length ? inbox.map((r) => {
    // Either way the company on the card is the one asking to be introduced.
    const via = r.role === "via";
    const pending = (via && r.status === "requested") || (!via && r.status === "forwarded");
    return `<div class="net-row">
      <div class="grow">${orgLine(r.asker)}
        <div class="dim mt4" style="font-size:11.5px">${esc(via ? t("ni_role_via") : t("ni_role_target"))} ${esc(via ? (r.target || {}).name || "" : (r.via || {}).name || "")}</div>
        ${r.message ? `<div class="net-msg">${esc(r.message)}</div>` : ""}
        ${r.status === "accepted" && !via ? contactBlock(r.asker) : ""}</div>
      <div class="net-side">${pending ? `
        <button class="btn primary sm" data-introyes="${r.id}">${esc(via ? t("ni_pass") : t("ni_accept"))}</button>
        <button class="btn ghost sm" data-introno="${r.id}">${esc(t("ni_decline"))}</button>` : st(r.status)}</div>
    </div>`;
  }).join("") : `<p class="muted" style="font-size:13px">${esc(t("ni_none"))}</p>`;

  return `<div class="section-title">${esc(t("ni_title"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("ni_sub"))}</p>
      <div class="note-box mt12">${esc(t("ni_privacy"))}</div>
      ${net.partners.length
        ? `<button class="btn primary sm mt12" id="introAsk">＋ ${esc(t("ni_ask"))}</button>`
        : `<div class="dim mt12" style="font-size:12.5px">${esc(t("ni_no_partners"))}</div>`}
      <div class="section-sub mt16">${esc(t("ni_inbox"))}</div>
      ${inboxRows}
      <div class="section-sub mt16">${esc(t("ni_sent"))}</div>
      ${sentRows}
    </div>`;
}

function openIntroDialog(net, reload) {
  let picked = null;
  const m = openModal(t("ni_ask"), `
    <label class="field"><span>${esc(t("ni_via"))}</span>
      <select class="select" id="inVia">${net.partners.map((p) =>
        `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select></label>
    <label class="field"><span>${esc(t("ni_target"))}</span>
      <input class="input" id="inQ" placeholder="${esc(t("ni_search"))}" autocomplete="off" /></label>
    <div id="inResults" class="net-results"></div>
    <div class="dim" style="font-size:11.5px">${esc(t("ni_search_hint"))}</div>
    <label class="field mt12"><span>${esc(t("ni_message"))}</span>
      <textarea class="input" id="inMsg" rows="3" maxlength="600"></textarea></label>`,
    `<button class="btn primary" id="inSend">${esc(t("ni_send"))}</button>`);

  let timer = null;
  m.querySelector("#inQ").addEventListener("input", (e) => {
    clearTimeout(timer);
    const q = e.target.value.trim();
    picked = null;
    if (q.length < 2) { m.querySelector("#inResults").innerHTML = ""; return; }
    timer = setTimeout(async () => {
      try {
        const r = await API.networkSearch(q);
        m.querySelector("#inResults").innerHTML = r.results.map((o) =>
          `<button type="button" class="net-hit" data-pick="${o.id}" data-name="${esc(o.name)}">${orgLine(o)}</button>`).join("")
          || `<p class="muted" style="font-size:12.5px">${esc(t("ni_none"))}</p>`;
        m.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", () => {
          picked = +b.dataset.pick;
          m.querySelectorAll("[data-pick]").forEach((x) => x.classList.remove("on"));
          b.classList.add("on");
        }));
      } catch (err) { toast(errMsg(err), "err"); }
    }, 300);
  });

  m.querySelector("#inSend").addEventListener("click", async (e) => {
    if (!picked) { toast(t("ni_pick_target"), "err"); return; }
    e.target.disabled = true;
    try {
      await API.introCreate({ via_org_id: +m.querySelector("#inVia").value,
        target_org_id: picked, message: m.querySelector("#inMsg").value.trim() });
      closeModal(); toast(t("ni_ok"), "ok"); reload();
    } catch (err) { toast(errMsg(err), "err"); e.target.disabled = false; }
  });
}

function needsSection(net) {
  const card = (n) => `<div class="net-row">
    <div class="grow"><b>${esc(n.title)}</b>
      ${n.detail ? `<div class="net-msg">${esc(n.detail)}</div>` : ""}
      <div class="dim mt4" style="font-size:11.5px">${
        n.mine ? `${esc(t(n.status === "open" ? "nn_open" : "nn_closed"))} · ${esc(t("nn_replies", { n: n.reply_count }))}`
               : `${esc(n.org ? n.org.name : "")} · ${esc(n.distance === 1 ? t("nn_dist1") : t("nn_dist2"))}${n.country ? " · " + esc(n.country) : ""}`}</div>
      ${n.mine && n.replies && n.replies.length ? n.replies.map((r) => `
        <div class="net-reply">${orgLine(r.org)}
          ${r.message ? `<div class="net-msg">${esc(r.message)}</div>` : ""}
          ${contactBlock(r.org)}</div>`).join("") : ""}</div>
    <div class="net-side">${
      n.mine ? (n.status === "open" ? `<button class="btn ghost sm" data-needclose="${n.id}">${esc(t("nn_close"))}</button>` : "")
             : (n.replied ? `<span class="badge ok dot">${esc(t("nn_replied"))}</span>`
                          : `<button class="btn primary sm" data-needreply="${n.id}">${esc(t("nn_reply"))}</button>`)}</div>
  </div>`;

  const feed = net.needs.feed.length ? net.needs.feed.map(card).join("")
    : `<p class="muted" style="font-size:13px">${esc(t("nn_none"))}</p>`;
  const mine = net.needs.mine.length ? net.needs.mine.map(card).join("")
    : `<p class="muted" style="font-size:13px">${esc(t("nn_none"))}</p>`;

  return `<div class="section-title">${esc(t("nn_title"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("nn_sub"))}</p>
      <div class="note-box mt12">${esc(t("nn_privacy"))}</div>
      <div class="flex gap8 center wrap mt12">
        <button class="btn primary sm" id="needNew">＋ ${esc(t("nn_post"))}</button>
        <span class="dim" style="font-size:11.5px">${esc(t("nn_reach", { n: net.reach }))}</span>
      </div>
      <div class="section-sub mt16">${esc(t("nn_feed"))}</div>
      ${feed}
      <div class="section-sub mt16">${esc(t("nn_mine"))}</div>
      ${mine}
    </div>`;
}

function openNeedDialog(reload) {
  const m = openModal(t("nn_post"), `
    <label class="field"><span>${esc(t("nn_ntitle"))}</span>
      <input class="input" id="ndTitle" maxlength="160" /></label>
    <label class="field"><span>${esc(t("nn_detail"))}</span>
      <textarea class="input" id="ndDetail" rows="4" maxlength="1200"></textarea></label>
    <label class="field"><span>${esc(t("nn_country"))}</span>
      <input class="input" id="ndCountry" maxlength="2" style="max-width:110px" placeholder="BG" /></label>`,
    `<button class="btn primary" id="ndSave">${esc(t("nn_publish"))}</button>`);
  m.querySelector("#ndSave").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      await API.needCreate({ title: m.querySelector("#ndTitle").value.trim(),
        detail: m.querySelector("#ndDetail").value.trim(),
        country: m.querySelector("#ndCountry").value.trim() });
      closeModal(); toast(t("nn_ok"), "ok"); reload();
    } catch (err) { toast(errMsg(err), "err"); e.target.disabled = false; }
  });
}

function openReplyDialog(id, reload) {
  const m = openModal(t("nn_reply"), `
    <label class="field"><span>${esc(t("nn_msg"))}</span>
      <textarea class="input" id="rpMsg" rows="4" maxlength="800"></textarea></label>`,
    `<button class="btn primary" id="rpSend">${esc(t("nn_reply"))}</button>`);
  m.querySelector("#rpSend").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await API.needReply(id, m.querySelector("#rpMsg").value.trim());
      closeModal(); toast(t("saved"), "ok"); reload(); }
    catch (err) { toast(errMsg(err), "err"); e.target.disabled = false; }
  });
}

async function viewNetwork() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("net_title") }]);
  let ref = null, net = null;
  try { [ref, net] = await Promise.all([API.reference(), API.network()]); }
  catch (err) { toast(errMsg(err), "err"); }
  if (!net) { shell(cr, `<div class="page"><div class="empty"><p>${esc(t("ni_none"))}</p></div></div>`); return; }

  shell(cr, `<div class="page">
    <div class="page-head"><div><h1>${esc(t("net_title"))}</h1>
      <div class="sub">${esc(t("net_sub"))}</div></div></div>
    ${referenceSection(ref)}
    ${introSection(net)}
    ${needsSection(net)}
  </div>`);

  wireReference(viewNetwork);
  const ask = $("#introAsk");
  if (ask) ask.addEventListener("click", () => openIntroDialog(net, viewNetwork));
  $$("[data-introyes]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await API.introDecide(b.dataset.introyes, "approve"); viewNetwork(); }
    catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
  }));
  $$("[data-introno]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await API.introDecide(b.dataset.introno, "decline"); viewNetwork(); }
    catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
  }));
  $$("[data-introdel]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.introWithdraw(b.dataset.introdel); viewNetwork(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  const nn = $("#needNew");
  if (nn) nn.addEventListener("click", () => openNeedDialog(viewNetwork));
  $$("[data-needreply]").forEach((b) => b.addEventListener("click", () => openReplyDialog(b.dataset.needreply, viewNetwork)));
  $$("[data-needclose]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.needClose(b.dataset.needclose); viewNetwork(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
}

async function viewIntegrations() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("nav_integrations") }]);
  let wh = { hooks: [], events: [] }, keys = [], qt = null, au = null;
  try { [wh, keys] = await Promise.all([API.webhooks(), API.integrationKeys()]); }
  catch (err) { toast(errMsg(err), "err"); }
  try { qt = await API.qtsp(); } catch { qt = null; }
  try { au = await API.automations(); } catch { au = null; }
  let ch = null, cal = null, cloud = null;
  try { ch = await API.chains(); } catch { ch = null; }
  try { cal = await API.calendar(); } catch { cal = null; }
  try { cloud = await API.cloud(); } catch { cloud = null; }

  // Stage options for the "set status" action, gathered across the templates
  // this company actually uses so the picker only offers reachable stages.
  const stages = [];
  const seen = {};
  Object.keys(State.templates || {}).forEach((k) => {
    (((State.templates || {})[k] || {}).stages || []).forEach((s) => {
      const key = s.key || s;
      if (!seen[key]) { seen[key] = 1; stages.push({ key, label: tplStage(k, key, s.label || key) }); }
    });
  });

  const dir = CONNECTORS.map(([cat, ico, items]) => `
    <div class="conn-group">
      <div class="conn-head"><span class="conn-ico">${ico}</span><b>${esc(t(cat))}</b></div>
      <div class="conn-chips">${items.map((n) => `<span class="conn-chip">${esc(connName(n))}</span>`).join("")}</div>
    </div>`).join("");

  const hookRows = wh.hooks.length ? wh.hooks.map((h) => `
    <div class="hook-row">
      <div class="grow"><b>${esc(h.label || h.url)}</b>
        <div class="dim mono" style="font-size:11.5px;word-break:break-all">${esc(h.url)}</div>
        <div class="dim" style="font-size:11.5px">${esc(h.events.join(", "))}${h.last_status ? " · " + esc(h.last_status) : ""}</div></div>
      <button class="btn ghost sm" data-wh-test="${h.id}">${esc(t("wh_test"))}</button>
      <button class="btn ghost sm" data-wh-del="${h.id}">${esc(t("int_revoke"))}</button>
    </div>`).join("") : `<p class="muted" style="font-size:13px">${esc(t("wh_empty"))}</p>`;

  const activeKey = keys.filter((k) => !k.revoked && k.kind === "secret")[0];

  shell(cr, `<div class="page">
    <div class="page-head"><div><h1>${esc(t("integrations_title"))}</h1>
      <div class="sub">${esc(t("integrations_sub"))}</div></div></div>

    ${automationSection(au, stages)}
    ${chainSection(ch)}
    ${connectorsSection(cal, cloud)}

    <div class="section-title">${esc(t("conn_directory"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px;margin-bottom:12px">${esc(t("conn_hint2"))}</p>
      <div class="conn-grid">${dir}</div>
    </div>

    <div class="section-title">${esc(t("wh_title"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("wh_hint"))}</p>
      <div class="mt12">${hookRows}</div>
      <div class="hook-add mt12">
        <input class="input grow" id="whUrl" placeholder="${esc(t("wh_url_ph"))}" />
        <input class="input" id="whLabel" style="max-width:170px" placeholder="${esc(t("wh_label"))}" />
        <button class="btn primary sm" id="whAdd">＋ ${esc(t("wh_add"))}</button>
      </div>
      <div class="dim mt8" style="font-size:11.5px">${esc(t("wh_events"))}: ${wh.events.map(esc).join(" · ")}</div>
    </div>

    <div class="section-title">${esc(t("wh_inbound"))}</div>
    <div class="card pad">
      <p class="dim" style="font-size:12.5px">${esc(t("int_sub"))}</p>
      ${activeKey ? `<div class="net-row mt8">
        <div class="grow"><b>${esc(activeKey.label || t("int_key_active"))}</b>
          <div class="dim" style="font-size:11.5px">${esc(t("int_key_created"))} ${relTime(activeKey.created_at)}${
            activeKey.last_used_at ? " · " + esc(t("sec_last_seen")) + " " + relTime(activeKey.last_used_at) : ""}</div></div>
        <div class="net-side"><button class="btn ghost sm" data-newkey="1">${esc(t("int_key_replace"))}</button></div>
      </div>` : `<button class="btn primary sm mt8" data-newkey="1">${esc(t("int_key_create"))}</button>`}
      <div class="note-box mt8">${esc(t("int_key_once"))}</div>
      <a class="btn ghost sm mt12" href="#/store">${esc(t("nav_store"))} → ${esc(t("st_tab_integration"))} ↗</a>
    </div>

    ${qtspSection(qt)}
  </div>`);

  wireAutomations(au, stages, viewIntegrations);
  wireChains(ch, viewIntegrations);
  wireConnectors(cal, cloud, viewIntegrations);
  wireQtsp(qt);

  const add = $("#whAdd");
  if (add) add.addEventListener("click", async () => {
    const url = $("#whUrl").value.trim();
    if (!url) return;
    add.disabled = true;
    try { await API.webhookCreate({ url, label: $("#whLabel").value.trim(), events: ["*"] }); viewIntegrations(); }
    catch (err) { toast(errMsg(err), "err"); add.disabled = false; }
  });
  $$("[data-wh-del]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.webhookDelete(b.dataset.whDel); viewIntegrations(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-wh-test]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.webhookTest(b.dataset.whTest); toast(t("wh_sent"), "ok"); setTimeout(viewIntegrations, 1500); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-copykey]").forEach((b) => b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copykey); toast(t("int_copied"), "ok"); }
    catch (e) { toast(errMsg(e), "err"); }
  }));
  $$("[data-newkey]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const r = await API.integrationCreateKey({ kind: "secret" });
      if (r.key) showSecretKeyOnce(r.key, viewIntegrations);
    } catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
  }));
}

/* =========================================================================
   ANALYTICS (roadmap 11)

   Charts are hand-built SVG - the app ships no chart library, and these forms
   do not need one. Rules followed throughout: one axis per chart (never two
   scales), thin marks with 4px rounded ends anchored to the baseline, a 2px
   gap between adjacent fills, recessive grid, a legend whenever two series
   share a chart, and hover tooltips on every mark.
   ======================================================================== */
const CHART = { w: 560, h: 190, padL: 40, padR: 10, padT: 12, padB: 26, radius: 4, gap: 2 };

function anTooltip() {
  let el = $(".an-tt");
  if (!el) {
    el = document.createElement("div");
    el.className = "an-tt";
    document.body.appendChild(el);
  }
  return el;
}

/* Attach hover tooltips to any [data-tip] mark inside a chart. */
function wireChartTips(root) {
  const tip = anTooltip();
  $$("[data-tip]", root).forEach((m) => {
    m.addEventListener("mousemove", (e) => {
      tip.innerHTML = m.dataset.tip;
      tip.classList.add("on");
      const r = tip.getBoundingClientRect();
      tip.style.left = Math.min(window.innerWidth - r.width - 8, e.clientX + 12) + "px";
      tip.style.top = Math.max(8, e.clientY - r.height - 10) + "px";
    });
    m.addEventListener("mouseleave", () => tip.classList.remove("on"));
  });
}

function niceMax(v) {
  if (v <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  return Math.ceil(v / mag) * mag;
}

function monthLabel(period) {
  const [y, m] = period.split("-");
  const d = new Date(Number(y), Number(m) - 1, 1);
  try { return d.toLocaleDateString(LANG, { month: "short" }); } catch { return period; }
}

/* Vertical bars over time. One series, so no legend - the title names it. */
function barChartTime(series, fmt) {
  const { w, h, padL, padR, padT, padB, radius } = CHART;
  const max = niceMax(Math.max(...series.map((p) => p.value), 0));
  const iw = w - padL - padR, ih = h - padT - padB;
  const bw = Math.max(6, iw / series.length - 10);
  const y = (v) => padT + ih - (v / max) * ih;
  const ticks = [0, max / 2, max];
  return `<svg class="an-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(t("an_revenue"))}">
    ${ticks.map((tk) => `<line class="grid" x1="${padL}" x2="${w - padR}" y1="${y(tk)}" y2="${y(tk)}"/>
      <text class="axis" x="${padL - 6}" y="${y(tk) + 3.5}" text-anchor="end">${esc(fmt(tk, true))}</text>`).join("")}
    ${series.map((p, i) => {
      const cx = padL + (i + 0.5) * (iw / series.length);
      const bh = Math.max(p.value > 0 ? 2 : 0, padT + ih - y(p.value));
      return `<rect class="hit" x="${cx - bw / 2 - 5}" y="${padT}" width="${bw + 10}" height="${ih}"
                data-tip="<b>${esc(monthLabel(p.period))}</b> · ${esc(fmt(p.value))}"/>
        <rect class="bar" x="${cx - bw / 2}" y="${y(p.value)}" width="${bw}" height="${bh}"
              rx="${radius}" ry="${radius}" fill="var(--chart-1)"/>
        <text class="axis" x="${cx}" y="${h - 8}" text-anchor="middle">${esc(monthLabel(p.period))}</text>`;
    }).join("")}
  </svg>`;
}

/* Two series over time, grouped. Two series means a legend is mandatory. */
function groupedBars(series, aKey, bKey, aLabel, bLabel) {
  const { w, h, padL, padR, padT, padB, radius, gap } = CHART;
  const max = niceMax(Math.max(...series.map((p) => Math.max(p[aKey], p[bKey])), 0));
  const iw = w - padL - padR, ih = h - padT - padB;
  const slot = iw / series.length;
  const bw = Math.max(5, (slot - 14 - gap) / 2);
  const y = (v) => padT + ih - (v / max) * ih;
  const ticks = [0, max / 2, max];
  const bar = (x, v, fill, label, period) => {
    const bh = Math.max(v > 0 ? 2 : 0, padT + ih - y(v));
    return `<rect class="hit" x="${x}" y="${padT}" width="${bw}" height="${ih}"
              data-tip="<b>${esc(monthLabel(period))}</b> · ${esc(label)}: ${v}"/>
      <rect class="bar" x="${x}" y="${y(v)}" width="${bw}" height="${bh}"
            rx="${radius}" ry="${radius}" fill="${fill}"/>`;
  };
  return `<svg class="an-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(t("an_flow"))}">
    ${ticks.map((tk) => `<line class="grid" x1="${padL}" x2="${w - padR}" y1="${y(tk)}" y2="${y(tk)}"/>
      <text class="axis" x="${padL - 6}" y="${y(tk) + 3.5}" text-anchor="end">${Math.round(tk)}</text>`).join("")}
    ${series.map((p, i) => {
      const left = padL + i * slot + 7;
      return bar(left, p[aKey], "var(--chart-1)", aLabel, p.period) +
             bar(left + bw + gap, p[bKey], "var(--chart-2)", bLabel, p.period) +
             `<text class="axis" x="${left + bw + gap / 2}" y="${h - 8}" text-anchor="middle">${esc(monthLabel(p.period))}</text>`;
    }).join("")}
  </svg>`;
}

/* Horizontal bars: category labels are long text, so they belong on the axis. */
function barChartH(rows, labelOf) {
  const w = 560, rowH = 30, padL = 150, padR = 46;
  const h = Math.max(rowH, rows.length * rowH) + 6;
  const max = niceMax(Math.max(...rows.map((r) => r.count), 0));
  const iw = w - padL - padR;
  return `<svg class="an-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(t("an_by_stage"))}">
    ${rows.map((r, i) => {
      const yy = i * rowH + 6, bw = Math.max(r.count > 0 ? 3 : 0, (r.count / max) * iw);
      return `<text class="axis" x="${padL - 10}" y="${yy + 13}" text-anchor="end">${esc(labelOf(r))}</text>
        <rect class="hit" x="${padL}" y="${yy}" width="${iw}" height="${rowH - 8}"
              data-tip="<b>${esc(labelOf(r))}</b> · ${r.count}"/>
        <rect class="bar" x="${padL}" y="${yy}" width="${bw}" height="${rowH - 8}"
              rx="4" ry="4" fill="var(--chart-1)"/>
        <text class="val" x="${padL + bw + 7}" y="${yy + 15}">${r.count}</text>`;
    }).join("")}
  </svg>`;
}

async function viewAnalytics() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("nav_analytics") }]);
  const days = State.anDays || 90;
  let a = null, tradeFigures = null;
  try {
    [a, tradeFigures] = await Promise.all([
      API.analytics(days),
      API.addonMetrics(days).catch(() => null),
    ]);
  } catch (err) { toast(errMsg(err), "err"); }
  if (!a) return shell(cr, `<div class="page"><div class="an-empty">${esc(t("an_empty"))}</div></div>`);

  if (a.empty) {
    return shell(cr, `<div class="page">
      <div class="page-head"><div><h1>${esc(t("an_title"))}</h1>
        <div class="sub">${esc(t("an_sub"))}</div></div></div>
      <div class="card pad"><div class="an-empty">${esc(t("an_empty"))}</div></div>
    </div>`);
  }

  const p = a.productivity, rev = a.revenue;
  const eur = (raw) => displayMoneyText(String(raw));
  const money = (v, short) => short
    ? (v >= 1000 ? Math.round(v / 1000) + "k" : String(Math.round(v)))
    : eur("EUR " + Number(v).toFixed(2));
  const nd = (v) => (v == null ? "—" : t("an_days_n", { n: v }));

  const tiles = [
    { n: eur(rev.total), l: t("an_total_value"), s: t("an_total_value_s") },
    { n: eur(rev.open), l: t("an_open_value"), s: t("an_open_value_s") },
    { n: p.completion_rate + "%", l: t("an_completion"), s: t("an_completion_s", { c: p.closed, o: p.orders_total }) },
    { n: nd(p.avg_cycle_days), l: t("an_cycle"), s: t("an_cycle_s"), sm: true },
    { n: nd(p.avg_approval_days), l: t("an_approval"), s: t("an_approval_s"), sm: true },
  ];

  const attention = a.attention.length ? a.attention.map((o) => `
    <div class="an-row"><div class="grow"><b>${esc(o.ref)} · ${esc(o.title)}</b>
      <div class="dim" style="font-size:11.5px">${esc(o.status_label)}</div></div>
      <span class="badge ${o.days >= 30 ? "danger" : "warn"}">${esc(t("an_days_n", { n: o.days }))}</span>
      <a class="btn ghost sm" href="#/o/${o.id}">${esc(t("an_open_btn"))}</a></div>`).join("")
    : `<p class="muted" style="font-size:13px">${esc(t("an_attention_none"))}</p>`;

  shell(cr, `<div class="page">
    <div class="page-head">
      <div><h1>${esc(t("an_title"))}</h1><div class="sub">${esc(t("an_sub"))}</div></div>
      <div class="an-ranges">${a.range.options.map((d) => `
        <button class="btn ${d === days ? "primary" : "ghost"} sm" data-an-range="${d}">${esc(t("an_days_n", { n: d }))}</button>`).join("")}</div>
    </div>

    <div class="an-tiles">${tiles.map((x) => `
      <div class="an-tile"><div class="n ${x.sm ? "sm" : ""}">${esc(x.n)}</div>
        <div class="l">${esc(x.l)}</div><div class="s">${esc(x.s)}</div></div>`).join("")}</div>

    <div class="section-title">${esc(t("an_trends"))}</div>
    <div class="an-grid">
      <div class="card pad">
        <div class="an-head"><span class="an-title">${esc(t("an_revenue"))}</span>
          <span class="an-sub">${esc(t("an_revenue_sub"))}</span></div>
        ${barChartTime(rev.series, money)}
      </div>
      <div class="card pad">
        <div class="an-head"><span class="an-title">${esc(t("an_flow"))}</span>
          <span class="an-sub">${esc(t("an_flow_sub"))}</span></div>
        ${groupedBars(p.series, "created", "closed", t("an_created"), t("an_closed"))}
        <div class="an-legend">
          <span><i style="background:var(--chart-1)"></i>${esc(t("an_created"))}</span>
          <span><i style="background:var(--chart-2)"></i>${esc(t("an_closed"))}</span>
        </div>
      </div>
    </div>

    <div class="section-title">${esc(t("an_by_stage"))}</div>
    <div class="card pad">
      ${a.status.length
        ? barChartH(a.status, (r) => tplStage(r.template, r.key, r.label))
        : `<div class="an-empty">${esc(t("an_empty"))}</div>`}
    </div>

    <div class="an-grid" style="margin-top:16px">
      <div class="card pad">
        <div class="an-head"><span class="an-title">${esc(t("an_customers"))}</span></div>
        <div class="an-rows">${a.customers.map((c) => `
          <div class="an-row"><div class="grow"><b>${esc(c.name)}</b>
            <div class="dim" style="font-size:11.5px">${esc(t("an_orders_n", { n: c.orders }))} · ${esc(t("an_open_n", { n: c.open }))}</div></div>
            <span class="num">${esc(eur(c.value))}</span>
            <a class="btn ghost sm" href="#/passport/${c.org_id}">${esc(t("an_passport"))}</a></div>`).join("")}
        </div>
      </div>
      <div class="card pad">
        <div class="an-head"><span class="an-title">${esc(t("an_verticals"))}</span></div>
        <div class="an-rows">${a.verticals.map((v) => `
          <div class="an-row"><div class="grow"><b>${esc(tplLabel(v.key, v.label))}</b></div>
            <span class="dim">${esc(t("an_orders_n", { n: v.orders }))}</span>
            <span class="num">${esc(eur(v.value))}</span></div>`).join("")}
        </div>
        <div class="an-legend" style="margin-top:14px">
          <span>${esc(t("an_act_events"))}: <b>${a.activity.events}</b></span>
          <span>${esc(t("an_act_docs"))}: <b>${a.activity.documents}</b></span>
          <span>${esc(t("an_act_msgs"))}: <b>${a.activity.messages}</b></span>
          <span>${esc(t("an_act_sigs"))}: <b>${a.activity.signatures}</b></span>
        </div>
      </div>
    </div>

    ${tradeSection(tradeFigures)}

    <div class="section-title">${esc(t("an_attention"))}</div>
    <div class="card pad"><p class="dim" style="font-size:12.5px">${esc(t("an_attention_hint"))}</p>
      <div class="an-rows">${attention}</div></div>
  </div>`);

  wireChartTips(document);
  $$("[data-an-range]").forEach((b) => b.addEventListener("click", () => {
    State.anDays = Number(b.dataset.anRange);
    viewAnalytics();
  }));
}

/* =========================================================================
   GETTING PAID

   An invoice used to be a page that was generated and forgotten. Here it is a
   record that chases itself, notices the money arriving on the bank statement,
   and afterwards can prove how fast this company settles what it is invoiced.
   ======================================================================== */
let moneyTab = "issued";

function invStatusBadge(i) {
  if (i.status === "paid") return `<span class="badge ok dot">${esc(t("mn_st_paid"))}</span>`;
  if (i.status === "part") return `<span class="badge warn dot">${esc(t("mn_st_part"))}</span>`;
  if (i.status === "draft") return `<span class="badge">${esc(t("mn_st_draft"))}</span>`;
  if (i.overdue_days > 0)
    return `<span class="badge danger dot">${esc(t("mn_st_late", { n: i.overdue_days }))}</span>`;
  return `<span class="badge accent dot">${esc(t("mn_st_sent"))}</span>`;
}

async function viewMoney() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("mn_title") }]);
  let d;
  try { d = await API.invoices(); }
  catch (err) { toast(errMsg(err), "err"); d = { issued: [], received: [], totals: {} }; }
  const list = moneyTab === "received" ? d.received : d.issued;
  const s = d.settings || {};

  const row = (i) => `
    <div class="an-row">
      <div class="grow">
        <b>${esc(i.number)}</b> ${invStatusBadge(i)}
        <div class="dim" style="font-size:11.5px">${esc(i.customer)}${
          i.due_date ? " · " + esc(t("d_due")) + " " + esc(i.due_date) : ""}${
          i.paid_amount && i.status !== "paid"
            ? " · " + esc(displayMoneyText(i.currency + " " + i.paid_amount)) + " " + esc(t("mn_of"))
            : ""}</div>
      </div>
      <b style="white-space:nowrap">${esc(displayMoneyText(i.currency + " " + i.gross))}</b>
      ${i.mine ? `
        ${i.status === "draft" || i.status === "sent" || i.status === "part"
          ? `<button class="btn ghost sm" data-inv-send="${i.id}">${esc(t("mn_send"))}</button>` : ""}
        ${i.status !== "paid"
          ? `<button class="btn ghost sm" data-inv-paid="${i.id}">${esc(t("mn_mark_paid"))}</button>` : ""}` : ""}
      <button class="btn ghost sm" data-inv-log="${i.id}">${esc(t("mn_log"))}</button>
    </div>`;

  const tile = (n, label, cls) => `<div class="an-tile"><div class="n sm ${cls || ""}">${
    esc(displayMoneyText("EUR " + (n || 0)))}</div><div class="l">${esc(label)}</div></div>`;

  shell(cr, `<div class="page">
    <div class="page-head"><div><h1>${esc(t("mn_title"))}</h1>
      <div class="sub">${esc(t("mn_sub"))}</div></div>
      <div class="flex gap8 wrap">
        <button class="btn ghost" id="mnSettings">${esc(t("mn_settings"))}</button>
        <button class="btn primary" id="mnReconcile">${esc(t("mn_reconcile"))}</button>
      </div></div>

    ${!s.iban ? `<div class="quota-banner">
      <span>⚠ ${esc(t("mn_no_iban"))}</span>
      <button class="btn primary sm" id="mnSetup">${esc(t("mn_settings"))}</button></div>` : ""}
    ${!s.mail_on ? `<div class="quota-banner">
      <span>✉ ${esc(t("mn_no_mail"))}</span>
      <button class="btn primary sm" id="mnMail">${esc(t("sm_title"))}</button></div>` : ""}

    <div class="an-tiles">
      ${tile(d.totals.owed, t("mn_owed"))}
      ${tile(d.totals.overdue, t("mn_overdue"))}
      ${tile(d.totals.owing, t("mn_owing"))}
    </div>

    <div class="flex gap8 wrap" style="margin:18px 0 10px">
      <button class="btn ${moneyTab === "issued" ? "primary" : "ghost"} sm" data-mn-tab="issued">${
        esc(t("mn_issued"))} (${d.issued.length})</button>
      <button class="btn ${moneyTab === "received" ? "primary" : "ghost"} sm" data-mn-tab="received">${
        esc(t("mn_received"))} (${d.received.length})</button>
    </div>
    <div class="an-rows">${list.length ? list.map(row).join("")
      : `<p class="muted" style="font-size:13px">${esc(t("mn_none"))}</p>`}</div>
    <p class="dim mt16" style="font-size:12px">${esc(t("mn_note"))}</p>
  </div>`);

  $$("[data-mn-tab]").forEach((b) => b.addEventListener("click", () => {
    moneyTab = b.dataset.mnTab; viewMoney();
  }));
  const go2 = (id, fn) => { const e = $(id); if (e) e.addEventListener("click", fn); };
  go2("#mnSettings", openPaySettings);
  go2("#mnSetup", openPaySettings);
  go2("#mnMail", openMail);
  go2("#mnReconcile", openReconcile);

  $$("[data-inv-send]").forEach((b) => b.addEventListener("click", async () => {
    const inv = list.find((x) => String(x.id) === b.dataset.invSend) || {};
    const to = prompt(t("mn_send_to"), inv.customer_email || "");
    if (to === null) return;
    b.disabled = true;
    try { await API.invoiceSend(b.dataset.invSend, to.trim()); toast(t("mn_sent"), "ok"); viewMoney(); }
    catch (err) { toast(errMsg(err), "err"); viewMoney(); }
  }));
  $$("[data-inv-paid]").forEach((b) => b.addEventListener("click", async () => {
    const inv = list.find((x) => String(x.id) === b.dataset.invPaid) || {};
    const raw = prompt(t("mn_amount_ask"), String(inv.outstanding || ""));
    if (raw === null) return;
    try { await API.invoicePaid(b.dataset.invPaid, Number(raw)); toast(t("saved"), "ok"); viewMoney(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-inv-log]").forEach((b) => b.addEventListener("click", () => openInvoiceLog(b.dataset.invLog)));
}

async function openInvoiceLog(id) {
  let d;
  try { d = await API.invoiceEvents(id); } catch (err) { return toast(errMsg(err), "err"); }
  const i = d.invoice;
  const label = { sent: t("mn_ev_sent"), rem: t("mn_ev_rem"), due: t("mn_ev_due"),
                  late: t("mn_ev_late"), paid: t("mn_ev_paid") };
  openModal(i.number, `
    <div class="an-rows">
      <div class="an-row"><div class="grow"><b>${esc(t("mn_ref"))}</b>
        <div class="dim mono">${esc(i.pay_ref)}</div></div>
        <b>${esc(displayMoneyText(i.currency + " " + i.gross))}</b></div>
    </div>
    <p class="dim mt8" style="font-size:12px">${esc(t("mn_ref_note"))}</p>
    <div class="section-title" style="margin:20px 0 10px">${esc(t("mn_log"))}</div>
    <div class="an-rows">${(d.events || []).length ? d.events.map((e) => `
      <div class="an-row"><div class="grow">
        <b>${esc(label[e.kind] || e.kind)}</b>
        <div class="dim" style="font-size:11.5px">${esc(e.detail || "")} · ${relTime(e.at)}</div>
      </div>${e.amount ? `<b>${esc(displayMoneyText(i.currency + " " + e.amount))}</b>` : ""}</div>`).join("")
      : `<p class="muted" style="font-size:13px">${esc(t("mn_no_events"))}</p>`}</div>`,
    `<button class="btn ghost" data-close>${esc(t("close"))}</button>`);
}

async function openPaySettings() {
  let s;
  try { s = await API.paySettings(); } catch (err) { return toast(errMsg(err), "err"); }
  const days = [-7, -3, -1, 0, 1, 3, 7, 14, 30];
  openModal(t("mn_settings"), `
    <p class="dim" style="font-size:12.5px">${esc(t("mn_settings_hint"))}</p>
    <label class="field mt12"><span>IBAN</span>
      <input class="input mono" id="psIban" maxlength="40" value="${esc(s.iban || "")}" ${
        s.can_edit ? "" : "readonly"}></label>
    <div class="row2">
      <label class="field"><span>BIC</span>
        <input class="input mono" id="psBic" maxlength="16" value="${esc(s.bic || "")}"></label>
      <label class="field"><span>${esc(t("d_bank"))}</span>
        <input class="input" id="psBank" maxlength="80" value="${esc(s.bank || "")}"></label>
    </div>
    <div class="row2">
      <label class="field"><span>${esc(t("d_terms"))}</span>
        <input class="input" id="psTerms" type="number" min="0" max="365" value="${esc(String(s.terms || 14))}"></label>
      <label class="field"><span>${esc(t("mn_pay_link"))}</span>
        <input class="input" id="psLink" maxlength="300" placeholder="https://" value="${esc(s.link || "")}"></label>
    </div>
    <div class="section-title" style="margin:18px 0 8px">${esc(t("mn_reminders"))}</div>
    <p class="dim" style="font-size:12px;margin-bottom:8px">${esc(t("mn_reminders_hint"))}</p>
    <div class="sector-filter">${days.map((n) => `
      <button type="button" class="sec-chip ${(s.reminders || []).indexOf(n) !== -1 ? "on" : ""}"
        data-rem="${n}">${esc(n < 0 ? t("mn_before", { n: -n }) : n === 0 ? t("mn_on_due") : t("mn_after", { n }))}</button>`).join("")}</div>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
     ${s.can_edit ? `<button class="btn primary" id="psSave">${esc(t("save"))}</button>` : ""}`);

  const chosen = new Set(s.reminders || []);
  $$("[data-rem]").forEach((b) => b.addEventListener("click", () => {
    const n = Number(b.dataset.rem);
    if (chosen.has(n)) chosen.delete(n); else chosen.add(n);
    b.classList.toggle("on", chosen.has(n));
  }));
  const save = $("#psSave");
  if (save) save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      await API.paySettingsSave({
        iban: $("#psIban").value.trim(), bic: $("#psBic").value.trim(),
        bank: $("#psBank").value.trim(), terms: $("#psTerms").value.trim(),
        link: $("#psLink").value.trim(),
        reminders: [...chosen].sort((a, b2) => a - b2),
      });
      toast(t("saved"), "ok"); closeModal(); render();
    } catch (err) { toast(errMsg(err), "err"); save.disabled = false; }
  });
}

/* Paste a bank statement, see what it would settle, then commit. Never the
   other way round: a machine that moves money on a guess is worse than one
   that asks. */
async function openReconcile() {
  openModal(t("mn_reconcile"), `
    <p class="dim" style="font-size:12.5px">${esc(t("mn_reconcile_hint"))}</p>
    <label class="field mt12"><span>${esc(t("mn_statement"))}</span>
      <textarea class="input mono" id="rcText" rows="7" placeholder="CAMT.053 / MT940"></textarea></label>
    <div id="rcOut"></div>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
     <button class="btn ghost" id="rcPreview">${esc(t("mn_check"))}</button>
     <button class="btn primary" id="rcApply" disabled>${esc(t("mn_apply"))}</button>`);

  const show = (r) => {
    const rows = (r.matches || []).map((m) => `
      <div class="an-row">
        <div class="grow"><b>${esc(m.transaction.counterparty || "-")}</b>
          <div class="dim mono" style="font-size:11px">${esc(m.transaction.reference || "")}</div></div>
        <b>${esc(displayMoneyText((m.transaction.currency || "EUR") + " " + m.transaction.amount))}</b>
        ${m.invoice_id
          ? `<span class="badge ok dot">${esc(m.number)} · ${esc(t("mn_why_" + m.reason))}</span>`
          : `<span class="badge ${m.reason === "ambiguous" ? "warn" : ""}">${
              esc(m.reason === "ambiguous" ? t("mn_ambiguous") : t("mn_unmatched"))}</span>`}
      </div>`).join("");
    $("#rcOut").innerHTML = `
      <div class="section-title" style="margin:18px 0 10px">${esc(t("mn_found", {
        n: r.transactions, f: r.format || "" }))}</div>
      <div class="an-rows" style="max-height:260px;overflow:auto">${rows ||
        `<p class="muted" style="font-size:13px">${esc(t("mn_no_credits"))}</p>`}</div>`;
    $("#rcApply").disabled = !(r.matches || []).some((m) => m.invoice_id);
  };

  $("#rcPreview").addEventListener("click", async () => {
    try { show(await API.reconcile($("#rcText").value, false)); }
    catch (err) { toast(errMsg(err), "err"); }
  });
  $("#rcApply").addEventListener("click", async () => {
    $("#rcApply").disabled = true;
    try {
      const r = await API.reconcile($("#rcText").value, true);
      toast(t("mn_applied", { n: r.applied }), "ok");
      closeModal(); viewMoney();
    } catch (err) { toast(errMsg(err), "err"); }
  });
}

/* =========================================================================
   PLANS & CAPABILITIES (blueprint section 7: partner side always free)
   ======================================================================== */
async function viewPlans() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("nav_plans") }]);
  let ENT = { features: {}, plans: {}, feature_prices: {} };
  try { ENT = await API.entitlements(); } catch (e) { /* draw what we can */ }
  const cur = (State.pricing && State.pricing.plan) || "starter";
  const label = (k) => (cur === k ? t("plan_current") : (cur === "pro_pending" && k === "pro") ? t("tpl_st_pending") : (k === "ent" ? t("plan_contact") : t("plan_choose")));
  // The prices come from entitlements.py through /api/entitlements, so the
  // screen and the gate cannot disagree about what something costs.
  const P = (ENT.plans || {});
  const eur = (n) => displayMoneyText("EUR " + n);
  const plans = [
    { key: "starter", name: t("ent_plan_starter"), price: t("plan_free"), d: t("plan_starter_d"), cta: label("starter"), cls: cur === "starter" ? "current" : "" },
    { key: "pro", name: t("ent_plan_pro"), price: eur((P.pro || {}).price) + " " + t("plan_month"), d: t("plan_pro_d"), cta: label("pro"), cls: cur === "pro" ? "featured current" : "featured" },
    { key: "half", name: t("ent_plan_half"), price: eur((P.half || {}).price), d: t("plan_pro_d"), cta: label("half"), cls: cur === "half" ? "featured current" : "featured", free: (P.half || {}).free_months },
    { key: "year", name: t("ent_plan_year"), price: eur((P.year || {}).price), d: t("plan_pro_d"), cta: label("year"), cls: cur === "year" ? "featured current" : "featured", free: (P.year || {}).free_months },
    { key: "life", name: t("ent_plan_life"), price: eur((P.life || {}).price), d: t("ent_forever"), cta: label("life"), cls: cur === "life" ? "current" : "" },
    { key: "ent", name: t("plan_ent"), price: t("plan_custom_price"), d: t("plan_ent_d"), cta: label("ent"), cls: cur === "ent" ? "current" : "" },
  ];
  const addons = [["🧩", t("addon_tpl"), displayMoneyText("EUR 12") + " " + t("plan_month")],
                  ["✨", t("addon_ai"), t("plan_free")],
                  ["🔌", t("addon_int"), t("plan_free")]];
  shell(cr, `<div class="page">
    <div class="page-head"><div><h1>${esc(t("plans_title"))}</h1><div class="sub">${esc(t("plans_sub"))}</div></div></div>
    <div class="plans-grid">${plans.map((p) => `<div class="card plan-card ${p.cls}">
      <div class="pl-name">${esc(p.name)}</div>
      <div class="pl-price">${esc(p.price)}</div>
      ${p.free ? `<div class="pl-free">${esc(t("ent_free_months", { n: p.free }))}</div>` : ""}
      <p class="pl-desc">${esc(p.d)}</p>
      <button class="btn ${p.cls ? "primary" : "ghost"} block" data-plan-cta="${p.key}">${esc(p.cta)}</button>
    </div>`).join("")}</div>
    <div class="section-title">${esc(t("ent_parts"))}</div>
    <div class="card pad">
      ${Object.entries(ENT.features || {}).map(([k, v]) => {
        const pr = (ENT.feature_prices || {})[k];
        const why = v.allowed
          ? (v.why === "free" ? t("ent_free") : v.why === "plan" ? t("ent_by_plan") : t("ent_by_purchase"))
          : t("ent_locked");
        return `<div class="ent-row">
          <div><b>${esc(t("ent_f_" + k))}</b>
            <div class="dim" style="font-size:12px">${esc(why)}</div></div>
          ${v.allowed
            ? `<span class="badge ok dot">${esc(t("ent_open"))}</span>`
            : (pr ? `<div class="flex gap8 center wrap">
                <button class="btn ghost sm" data-buy="${k}" data-kind="month">${esc(eur(pr.month))} ${esc(t("ent_buy_month"))}</button>
                <button class="btn ghost sm" data-buy="${k}" data-kind="half"
                  title="${esc(t("ent_free_months", { n: pr.free_half }))}">${esc(eur(pr.half))} ${esc(t("ent_buy_half"))}</button>
                <button class="btn ghost sm" data-buy="${k}" data-kind="year"
                  title="${esc(t("ent_free_months", { n: pr.free_year }))}">${esc(eur(pr.year))} ${esc(t("ent_buy_year"))}</button>
                <button class="btn sm" data-buy="${k}" data-kind="once">${esc(eur(pr.once))} ${esc(t("ent_buy_once"))}</button>
              </div>` : `<span class="badge dim">${esc(t("ent_locked"))}</span>`)}
        </div>`;
      }).join("")}
      <p class="dim mt8" style="font-size:12px">${esc(t("ent_transfer"))}</p>
    </div>
    <p class="dim mt16" style="font-size:12px">${esc(t("plans_note"))}</p>
  </div>`);
  if (cur === "pro_pending") {
    const host = $(".plans-grid");
    const waiting = State.pricing && State.pricing.claim_pending;
    if (host) host.insertAdjacentHTML("afterend", waiting
      // Already told us. Saying so beats a button that quietly does nothing
      // the second time it is pressed.
      ? `<div class="card pad mt12"><b>⏳ ${esc(t("bill_awaiting"))}</b>
          <p class="dim mt8" style="font-size:12.5px">${esc(t("bill_awaiting_hint"))}</p></div>`
      : `<div class="card pad mt12 flex between center wrap gap8">
          <div><b>⏳ ${esc(t("tpl_st_pending"))}</b>
            <div class="dim" style="font-size:12px">${esc(t("bill_claim_hint"))}</div></div>
          <button class="btn primary sm" id="planPaid">${esc(t("bill_i_paid"))}</button></div>`);
    const pd = $("#planPaid");
    if (pd) pd.addEventListener("click", async () => {
      pd.disabled = true;
      try {
        const r = await API.planConfirm();
        State.pricing = await API.pricing();
        toast(t(r.status === "active" ? "toast_payment_ok" : "bill_claim_sent"), "ok");
        viewPlans();
      } catch (err) { toast(errMsg(err), "err"); pd.disabled = false; }
    });
  }
  $$("[data-buy]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const r = await API.buyFeature(b.dataset.buy, b.dataset.kind);
      if (r && r.url) { location.href = r.url; return; }
      toast(t("bill_claim_sent"), "ok");
      viewPlans();
    } catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
  }));
  $$("[data-plan-cta]").forEach((b) => b.addEventListener("click", async () => {
    const plan = b.dataset.planCta;
    if (plan === cur) return;
    b.disabled = true;
    try {
      const r = await API.planCheckout(plan);
      if (r.status === "contact") { window.location.href = "mailto:" + r.email; b.disabled = false; return; }
      State.pricing = await API.pricing();
      if (r.payment_url) { window.open(r.payment_url, "_blank", "noopener"); toast(t("tpl_st_pending"), "ok"); }
      else if (r.proforma) toast(t("proforma_issued"), "ok");
      else toast(t("toast_payment_ok"), "ok");
      viewPlans();
    } catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
  }));
}

/* =========================================================================
   STORE / FULFILLMENT (physical + digital keys)
   ======================================================================== */
let storeTab = "orders";

async function viewStore() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("nav_store") }]);
  shell(cr, `<div class="page"><div class="loading-full"><div class="spin"></div></div></div>`);
  let summary = {}, products = [], orders = [], keys = [], invbg = { enabled: false };
  try { [summary, products, orders, keys] = await Promise.all([API.storeSummary(), API.storeProducts(), API.storeOrders(), API.integrationKeys()]); }
  catch (err) { toast(errMsg(err), "err"); }
  try { invbg = await API.invbgStatus(); } catch { invbg = { enabled: false }; }
  State._invbg = invbg;
  State._storeProducts = products;

  const metric = (ico, n, l, cls) => `<div class="metric ${cls || ""}">
      <div class="m-ico">${ico}</div><div><div class="m-n">${esc(String(n))}</div><div class="m-l">${esc(l)}</div></div></div>`;
  const low = (summary.low || []).length
    ? `<div class="store-low">⚠ ${esc(t("store_low"))}: ${summary.low.map((x) => esc(x.name) + " (" + x.available + ")").join(" · ")}</div>` : "";
  const strip = `<div class="metric-row">
      ${metric("💰", summary.revenue ? displayMoneyText(summary.revenue) : "—", t("store_revenue"), "wide")}
      ${metric("📥", summary.incoming || 0, t("store_incoming"), summary.incoming ? "alert" : "")}
      ${metric("✅", summary.completed || 0, t("store_completed"))}
      ${metric("📈", (summary.fulfil_rate || 0) + "%", t("store_fulfil_rate"))}
      ${metric("📦", summary.products || 0, t("store_products"))}
    </div>${low ? `<div class="card pad mt12">${low}</div>` : ""}`;

  const tabs = `<div class="tabs">
    <button data-stab="orders" class="${storeTab === "orders" ? "active" : ""}">${esc(t("st_tab_orders"))}</button>
    <button data-stab="products" class="${storeTab === "products" ? "active" : ""}">${esc(t("st_tab_products"))}</button>
    <button data-stab="integration" class="${storeTab === "integration" ? "active" : ""}">${esc(t("st_tab_integration"))}</button>
  </div>`;

  const prodThumb = (p) => p.image
    ? `<div class="p-thumb"><img src="${esc(p.image)}" loading="lazy" alt="" onerror="this.parentNode.innerHTML='<span class=&quot;ph&quot;>${p.type === "digital" ? "🔑" : "📦"}</span>'"></div>`
    : `<div class="p-thumb"><span class="ph">${p.type === "digital" ? "🔑" : "📦"}</span></div>`;

  let bodyHTML, newBtn;
  if (storeTab === "orders") {
    newBtn = `<div class="flex gap8 wrap">
      <a class="btn ghost" href="/api/store/orders.csv" download>⤓ ${esc(t("store_export_csv"))}</a>
      <button class="btn primary" id="stNewOrder"><span class="ico">＋</span> ${esc(t("st_new_order"))}</button></div>`;
    const stChip = (o) => o.status === "fulfilled"
      ? `<span class="badge ok dot">${esc(t("st_status_fulfilled"))}</span>`
      : `<span class="badge warn dot">${esc(t("st_status_new"))}</span>`;
    const srcChip = (o) => o.source && o.source !== "manual"
      ? `<span class="badge src">${esc(o.source === "api" ? t("st_source_api") : t("st_source_form"))}</span>` : "";
    bodyHTML = orders.length ? `<div class="card pad0"><table class="tbl store-tbl"><thead><tr>
        <th>${esc(t("col_ref"))}</th><th>${esc(t("st_product"))}</th><th>${esc(t("st_customer"))}</th>
        <th>${esc(t("st_qty"))}</th><th>${esc(t("col_status"))}</th><th></th></tr></thead><tbody>
      ${orders.map((o) => `<tr>
        <td class="mono dim">${esc(o.ref)} ${srcChip(o)}</td>
        <td><div class="ord-prod">${prodThumb(o.product || { type: "physical" })}
          <div><div>${esc(o.product ? o.product.name : "-")}</div>
          ${o.product ? `<span class="badge ${o.product.type === "digital" ? "partner" : "accent"}" style="font-size:10px">${esc(o.product.type === "digital" ? t("st_digital") : t("st_physical"))}</span>` : ""}</div></div></td>
        <td>${esc(o.customer || "-")}</td>
        <td class="mono">${o.qty}</td>
        <td>${stChip(o)}</td>
        <td><div class="flex gap8 wrap center">
          ${invbg.enabled ? `<button class="btn ghost sm" data-invbg="${o.id}" title="${esc(t("invbg_send"))}">🧾</button>` : ""}
          ${o.product && o.product.type !== "digital" ? `<a class="btn ghost sm" href="/label/${o.id}" target="_blank" rel="noopener" title="${esc(t("st_label"))}">🏷️</a>` : ""}
          ${o.status === "new" ? `<button class="btn primary sm" data-fulfill="${o.id}">${esc(t("st_fulfill"))}</button>`
          : (o.keys && o.keys.length ? `<button class="btn ghost sm" data-showkeys='${esc(JSON.stringify(o.keys))}'>🔑 ${esc(t("st_delivered_keys"))}</button>` : "")}
        </div></td>
      </tr>`).join("")}</tbody></table></div>`
      : emptyState("🛒", t("st_no_orders"), t("store_sub"), `<button class="btn primary" id="stNewOrderE"><span class="ico">＋</span> ${esc(t("st_new_order"))}</button>`);
  } else if (storeTab === "products") {
    newBtn = `<button class="btn primary" id="stNewProduct"><span class="ico">＋</span> ${esc(t("st_new_product"))}</button>`;
    bodyHTML = products.length ? `<div class="prod-grid">${products.map((p) => {
      const lowStock = p.available <= 3;
      return `<div class="card prod-card">
        ${prodThumb(p)}
        <div class="p-body">
          <div class="flex between center gap8"><b class="p-name">${esc(p.name)}</b>
            <span class="badge ${p.type === "digital" ? "partner" : "accent"}">${esc(p.type === "digital" ? t("st_digital") : t("st_physical"))}</span></div>
          <div class="muted" style="font-size:12.5px;margin-top:2px">${p.sku ? esc(p.sku) : "—"}</div>
          <div class="flex between center mt8">
            <span class="p-price">${p.price ? esc(displayMoneyText(p.price)) : "—"}</span>
            <span class="badge ${lowStock ? "danger" : "ok"} dot">${p.available} ${esc(p.type === "digital" ? t("st_keys") : t("st_in_stock"))}</span>
          </div>
          <div class="flex gap8 mt12" style="border-top:1px solid var(--border);padding-top:10px">
            <a class="btn ghost sm" href="#/passport/product/${p.id}">🛡 ${esc(t("pr_title"))}</a>
            ${p.type === "digital" ? `<button class="btn ghost sm" data-addkeys="${p.id}">＋ ${esc(t("st_add_keys"))}</button>` : ""}
            <button class="btn ghost sm" data-delprod="${p.id}">${esc(t("tpl_delete"))}</button>
          </div>
        </div>
      </div>`; }).join("")}</div>`
      : emptyState("📦", t("st_no_products"), t("store_sub"), `<button class="btn primary" id="stNewProductE"><span class="ico">＋</span> ${esc(t("st_new_product"))}</button>`);
  } else {
    newBtn = "";
    const active = keys.filter((k) => !k.revoked);
    const sk = active.find((k) => k.kind === "secret");
    const pk = active.find((k) => k.kind === "public");
    const origin = location.origin;
    // A secret key comes back only at the moment it is created; afterwards the
    // row identifies it by label and use, because only its hash is stored.
    const keyList = keys.length ? keys.map((k) => `
      <div class="key-row2 ${k.revoked ? "revoked" : ""}">
        <span class="badge ${k.kind === "secret" ? "buyer" : "partner"}">${esc(k.kind === "secret" ? t("int_kind_secret") : t("int_kind_public"))}</span>
        ${k.key ? `<code class="key-code grow">${esc(k.key)}</code>`
          : `<span class="grow dim" style="font-size:12.5px">${esc(k.label || t("int_key_active"))} · ${esc(t("int_key_created"))} ${relTime(k.created_at)}</span>`}
        ${k.revoked ? `<span class="badge danger dot">${esc(t("int_revoked"))}</span>`
          : `${k.key ? `<button class="btn ghost sm" data-copykey="${esc(k.key)}">${esc(t("int_copy"))}</button>` : ""}
             <button class="btn ghost sm" data-revoke="${k.id}">${esc(t("int_revoke"))}</button>`}
      </div>`).join("") : `<p class="muted">${esc(t("int_no_keys"))}</p>`;
    const curl = `curl -X POST ${origin}/api/inbound/orders \\
  -H "Content-Type: application/json" \\
  -H "X-Seam-Key: YOUR_API_KEY" \\
  -d '{"product": "CBL-2M", "qty": 2, "customer": "client@example.com"}'`;
    const formUrl = pk ? `${origin}/order/${pk.key}` : null;
    bodyHTML = `
      <div class="card pad">
        <div class="flex between center wrap gap8"><b>${esc(t("int_keys_title"))}</b>
          <div class="flex gap8 wrap">
            <button class="btn ghost sm" id="intNewPk">＋ ${esc(t("int_new_public"))}</button>
            <button class="btn primary sm" id="intNewSk">＋ ${esc(t("int_new_secret"))}</button>
          </div></div>
        <p class="dim" style="font-size:12.5px;margin:6px 0 0">${esc(t("int_sub"))}</p>
        <div class="mt12">${keyList}</div>
      </div>
      <div class="card pad mt16">
        <b>${esc(t("int_webhook_hint"))}</b>
        <pre class="code-block" id="curlBlock">${esc(curl)}</pre>
        <button class="btn ghost sm" data-copytext="curlBlock">${esc(t("int_copy"))}</button>
      </div>
      ${formUrl ? `<div class="card pad mt16">
        <b>${esc(t("int_form_hint"))}</b>
        <div class="flex gap8 mt8 wrap center"><code class="key-code grow">${esc(formUrl)}</code>
          <button class="btn ghost sm" data-copykey="${esc(formUrl)}">${esc(t("int_copy"))}</button>
          <a class="btn ghost sm" href="${esc(formUrl)}" target="_blank" rel="noopener">${esc(t("int_open"))} ↗</a></div>
        <div class="mt12"><span class="dim" style="font-size:12.5px">${esc(t("int_embed"))}</span>
          <pre class="code-block" id="embedBlock">${esc(`<iframe src="${formUrl}" style="width:100%;max-width:520px;height:640px;border:0"></iframe>`)}</pre>
          <button class="btn ghost sm" data-copytext="embedBlock">${esc(t("int_copy"))}</button></div>
      </div>` : ""}`;
  }

  shell(cr, `<div class="page">
    <div class="page-head"><div><h1>${esc(t("store_title"))}</h1><div class="sub">${esc(t("store_sub"))}</div></div>${newBtn}</div>
    ${strip}
    <div class="mt16">${tabs}${bodyHTML}</div>
  </div>`);

  $$(".tabs [data-stab]").forEach((b) => b.addEventListener("click", () => { storeTab = b.dataset.stab; viewStore(); }));
  $$("#stNewOrder, #stNewOrderE").forEach((b) => b && b.addEventListener("click", openStoreOrder));
  $$("#stNewProduct, #stNewProductE").forEach((b) => b && b.addEventListener("click", openStoreProduct));
  $$("[data-fulfill]").forEach((b) => b.addEventListener("click", async () => {
    try {
      const o = await API.storeFulfill(b.dataset.fulfill);
      toast(t("toast_fulfilled"), "ok");
      if (o.keys && o.keys.length) showKeysModal(o.keys);
      viewStore();
    } catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-showkeys]").forEach((b) => b.addEventListener("click", () => { try { showKeysModal(JSON.parse(b.dataset.showkeys)); } catch {} }));
  $$("[data-addkeys]").forEach((b) => b.addEventListener("click", () => openAddKeys(b.dataset.addkeys)));
  $$("[data-delprod]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.storeDelProduct(b.dataset.delprod); viewStore(); } catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-invbg]").forEach((b) => b.addEventListener("click", () => {
    const o = orders.find((x) => x.id == b.dataset.invbg);
    if (o) openInvbgModal(o);
  }));

  // integration tab
  const copyText = async (txt) => {
    try { await navigator.clipboard.writeText(txt); toast(t("int_copied"), "ok"); }
    catch (e) { toast(errMsg(e), "err"); }
  };
  const nk = (kind) => async () => {
    try {
      const r = await API.integrationCreateKey({ kind });
      // A secret key exists in readable form exactly once, in this response.
      if (kind === "secret" && r.key) showSecretKeyOnce(r.key);
      else viewStore();
    } catch (err) { toast(errMsg(err), "err"); }
  };
  const sk2 = $("#intNewSk"); if (sk2) sk2.addEventListener("click", nk("secret"));
  const pk2 = $("#intNewPk"); if (pk2) pk2.addEventListener("click", nk("public"));
  $$("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
    try { await API.integrationRevoke(b.dataset.revoke); viewStore(); } catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-copykey]").forEach((b) => b.addEventListener("click", () => copyText(b.dataset.copykey)));
  $$("[data-copytext]").forEach((b) => b.addEventListener("click", () => {
    const el = document.getElementById(b.dataset.copytext);
    if (el) copyText(el.textContent);
  }));
}

/* Shown once, then gone: the server keeps only a hash of it, so this dialog is
   the single moment the key is readable. */
function showSecretKeyOnce(key, after) {
  openModal(t("int_key_new"), `
    <div class="note-box">${esc(t("int_key_once"))}</div>
    <div class="hashrow mt12"><code id="skNew">${esc(key)}</code>
      <button id="skCopy">${esc(t("int_copy"))}</button></div>`,
    `<button class="btn primary" id="skDone">${esc(t("close"))}</button>`);
  $("#skCopy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(key); toast(t("int_copied"), "ok"); }
    catch (e) { toast(errMsg(e), "err"); }
  });
  $("#skDone").addEventListener("click", () => { closeModal(); (after || viewStore)(); });
}

function showKeysModal(keys) {
  openModal(t("st_delivered_keys"), `<div class="keys-list">${keys.map((k) => `<div class="key-row mono">${esc(k)}</div>`).join("")}</div>`,
    `<button class="btn primary" data-close>OK</button>`);
}

function openStoreProduct() {
  openModal(t("st_new_product"), `
    <label class="field"><span>${esc(t("st_product"))} *</span><input class="input" id="pName" /></label>
    <label class="field"><span>${esc(t("st_type"))}</span><select class="select" id="pType"><option value="physical">${esc(t("st_physical"))}</option><option value="digital">${esc(t("st_digital"))}</option></select></label>
    <div class="row2">
      <label class="field"><span>${esc(t("st_sku"))}</span><input class="input" id="pSku" /></label>
      <label class="field"><span>${esc(t("st_price"))}</span><input class="input" id="pPrice" placeholder="EUR 0" /></label>
    </div>
    <label class="field" id="pStockF"><span>${esc(t("st_stock"))}</span><input class="input" id="pStock" type="number" value="0" /></label>
    <label class="field"><span>${esc(t("st_image"))}</span><input class="input" id="pImage" placeholder="https://… ${esc(t("st_image_hint"))}" /></label>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="pSave">${esc(t("create"))}</button>`);
  const sync = () => { $("#pStockF").classList.toggle("hide", $("#pType").value !== "physical"); };
  $("#pType").addEventListener("change", sync); sync();
  $("#pSave").addEventListener("click", async () => {
    const name = $("#pName").value.trim(); if (!name) { toast(t("enter_name"), "err"); return; }
    try {
      await API.storeAddProduct({ name, type: $("#pType").value, sku: $("#pSku").value.trim(), price: $("#pPrice").value.trim(), stock: $("#pStock").value, image: $("#pImage").value.trim() });
      closeModal(); toast(t("toast_product_added"), "ok"); viewStore();
    } catch (err) { toast(errMsg(err), "err"); }
  });
}

/* ---- inv.bg: push a store order to the Bulgarian invoicing service ------- */
function openInvbgModal(o) {
  const types = ((State._invbg || {}).types) || ["dan", "prof"];
  const typeLabel = { dan: t("invbg_type_dan"), prof: t("invbg_type_prof"), deb: t("invbg_type_deb"), cred: t("invbg_type_cred") };
  openModal(t("invbg_title") + " · " + o.ref, `
    <p class="muted" style="margin-bottom:4px">${esc(t("invbg_hint"))}</p>
    <label class="field"><span>${esc(t("invbg_doc_type"))}</span><select class="select" id="ibType">
      ${types.map((k) => `<option value="${esc(k)}">${esc(typeLabel[k] || k)}</option>`).join("")}</select></label>
    <div class="row2">
      <label class="field"><span>${esc(t("invbg_to_name"))} *</span><input class="input" id="ibName" value="${esc(o.customer || "")}" /></label>
      <label class="field"><span>${esc(t("invbg_to_mol"))}</span><input class="input" id="ibMol" /></label>
    </div>
    <label class="field"><span>${esc(t("invbg_to_address"))}</span><input class="input" id="ibAddr" /></label>
    <div class="row2">
      <label class="field"><span>${esc(t("invbg_to_bulstat"))}</span><input class="input" id="ibBulstat" placeholder="205643632" /></label>
      <label class="field"><span>${esc(t("invbg_to_vat"))}</span><input class="input" id="ibVat" placeholder="BG205643632" /></label>
    </div>
    <label class="field checkbox-field"><input type="checkbox" id="ibRegVat" /> <span>${esc(t("invbg_reg_vat"))}</span></label>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="ibSend">${esc(t("invbg_send"))}</button>`);
  $("#ibSend").addEventListener("click", async () => {
    const btn = $("#ibSend"); btn.disabled = true;
    try {
      const r = await API.invbgCreate({
        order_id: o.id, type: $("#ibType").value,
        to_name: $("#ibName").value.trim(), to_mol: $("#ibMol").value.trim(),
        to_address: $("#ibAddr").value.trim(), to_bulstat: $("#ibBulstat").value.trim(),
        to_vat_number: $("#ibVat").value.trim(), to_is_reg_vat: $("#ibRegVat").checked,
      });
      closeModal();
      toast(t("invbg_created") + " " + (r.number || r.id), "ok");
      if (r.pdf) window.open(r.pdf, "_blank", "noopener");
    } catch (err) { toast(errMsg(err), "err"); btn.disabled = false; }
  });
}

function openAddKeys(pid) {
  openModal(t("st_add_keys"), `<label class="field"><span>${esc(t("st_paste_keys"))}</span><textarea class="textarea" id="kCodes" rows="7" placeholder="XXXX-YYYY-ZZZZ\nAAAA-BBBB-CCCC"></textarea></label>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="kSave">${esc(t("b_add"))}</button>`);
  $("#kSave").addEventListener("click", async () => {
    const codes = $("#kCodes").value; if (!codes.trim()) return;
    try { const r = await API.storeAddKeys(pid, codes); closeModal(); toast(t("toast_keys_added") + " (" + r.added + ")", "ok"); viewStore(); }
    catch (err) { toast(errMsg(err), "err"); }
  });
}

function openStoreOrder() {
  const prods = State._storeProducts || [];
  if (!prods.length) { toast(t("st_no_products"), "err"); return; }
  const opts = prods.map((p) => `<option value="${p.id}">${esc(p.name)} · ${esc(p.type === "digital" ? t("st_digital") : t("st_physical"))} (${p.available})</option>`).join("");
  openModal(t("st_new_order"), `
    <label class="field"><span>${esc(t("st_product"))} *</span><select class="select" id="oProd">${opts}</select></label>
    <div class="row2">
      <label class="field"><span>${esc(t("st_customer"))}</span><input class="input" id="oCust" /></label>
      <label class="field"><span>${esc(t("st_qty"))}</span><input class="input" id="oQty" type="number" value="1" min="1" /></label>
    </div>
    <label class="field"><span>${esc(t("st_note"))}</span><input class="input" id="oNote" /></label>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="oSave">${esc(t("create"))}</button>`);
  $("#oSave").addEventListener("click", async () => {
    try {
      await API.storeCreateOrder({ product_id: $("#oProd").value, customer: $("#oCust").value.trim(), qty: $("#oQty").value, note: $("#oNote").value.trim() });
      closeModal(); toast(t("created"), "ok"); viewStore();
    } catch (err) { toast(errMsg(err), "err"); }
  });
}

/* =========================================================================
   AI ASSISTANT (Seam Assist)
   ======================================================================== */
const AI_PROVIDERS_UI = {
  groq: { name: "Groq", tag: "free", model: "llama-3.3-70b-versatile", base: "", url: "https://console.groq.com/keys", needsKey: true, showBase: false, ph: "gsk_…" },
  gemini: { name: "Google Gemini", tag: "free", model: "gemini-2.0-flash", base: "", url: "https://aistudio.google.com/app/apikey", needsKey: true, showBase: false, ph: "AIza…" },
  anthropic: { name: "Claude (Anthropic)", tag: "paid", model: "claude-sonnet-4-6", base: "", url: "https://console.anthropic.com/settings/keys", needsKey: true, showBase: false, ph: "sk-ant-…" },
  openai: { name: "Ollama / OpenAI-compat", tag: "local", model: "llama3.1", base: "http://localhost:11434/v1", url: "https://ollama.com/download", needsKey: false, showBase: true, ph: "§ai_key_optional" },
};

async function viewAssistant() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("nav_assistant") }]);
  shell(cr, `<div class="page"><div class="loading-full"><div class="spin"></div></div></div>`);
  let status = { enabled: false };
  try { status = await API.assistantStatus(); } catch (e) {}
  if (!status.enabled) {
    const provs = AI_PROVIDERS_UI;
    const tagT = (t2) => t2 === "free" ? t("ai_free") : t2 === "local" ? t("ai_local") : t("ai_paid");
    const provOpts = Object.keys(provs).map((k) => `<option value="${k}">${esc(provs[k].name)} · ${esc(tagT(provs[k].tag))}</option>`).join("");
    shell(cr, `<div class="page">
      <div class="card pad assist-setup">
        <div class="assist-hero">✨</div>
        <h1>${esc(t("assistant_setup_title"))}</h1>
        <p class="muted">${esc(t("assistant_setup_text"))}</p>
        <p class="dim" style="font-size:12.5px">${esc(t("ai_free_hint"))}</p>
        <div style="text-align:left;margin-top:14px">
          <label class="field"><span>${esc(t("ai_provider"))}</span><select class="select" id="aiProv">${provOpts}</select></label>
          <label class="field" id="aiKeyF"><span>API key · <a id="aiLink" href="#" target="_blank" rel="noopener">${esc(t("ai_get_key"))}</a></span><input class="input" id="aiKey" type="password" autocomplete="off" /></label>
          <label class="field"><span>${esc(t("ai_model"))}</span><input class="input" id="aiModel" /></label>
          <label class="field hide" id="aiBaseF"><span>${esc(t("ai_baseurl"))}</span><input class="input" id="aiBase" /></label>
          <button class="btn primary block" id="aiSave">${esc(t("assistant_key_save"))}</button>
        </div>
      </div></div>`);
    const applyProv = () => {
      const p = provs[$("#aiProv").value];
      $("#aiKey").placeholder = connName(p.ph);
      $("#aiModel").value = p.model;
      $("#aiBase").value = p.base;
      $("#aiLink").href = p.url;
      $("#aiKeyF").classList.toggle("hide", !p.needsKey);
      $("#aiBaseF").classList.toggle("hide", !p.showBase);
    };
    $("#aiProv").addEventListener("change", applyProv);
    applyProv();
    $("#aiSave").addEventListener("click", async () => {
      const b = $("#aiSave"); b.disabled = true;
      try {
        await API.assistantKey({ provider: $("#aiProv").value, key: $("#aiKey").value.trim(), model: $("#aiModel").value.trim(), base_url: $("#aiBase").value.trim() });
        State.assistantHistory = []; viewAssistant();
      } catch (err) { toast(errMsg(err), "err"); b.disabled = false; }
    });
    return;
  }
  if (!State.assistantHistory) State.assistantHistory = [];
  renderAssistant(cr);
}

function assistantBubble(role, text) {
  const av = role === "user" ? initials(State.user.name) : "✨";
  return `<div class="abub ${role}"><div class="ab-av">${esc(av)}</div><div class="ab-tx">${esc(text)}</div></div>`;
}

function renderAssistant(cr) {
  const h = State.assistantHistory || [];
  const body = h.length ? h.map((m) => assistantBubble(m.role, m.content)).join("")
    : `<div class="assist-empty"><div class="big">✨</div><p>${esc(t("assistant_empty"))}</p>
        <div class="sugs">${["sug_1", "sug_2", "sug_3"].map((s) => `<button class="chip-toggle sug" data-sug="${s}">${esc(t(s))}</button>`).join("")}</div></div>`;
  shell(cr, `<div class="page assist-page">
    <div class="page-head"><div><h1>${esc(t("assistant_title"))}</h1><div class="sub">${esc(t("assistant_intro"))}</div></div></div>
    <div class="card pad assist-chat">
      <div id="aMsgs" class="a-msgs">${body}</div>
      <div class="cmt-box"><input class="input" id="aInput" placeholder="${esc(t("assistant_ph"))}" autocomplete="off" /><button class="btn primary" id="aSend">${esc(t("assistant_send"))}</button></div>
    </div></div>`);

  const list = () => $("#aMsgs");
  const scroll = () => { const l = list(); if (l) l.scrollTop = l.scrollHeight; };
  const append = (html) => { const l = list(); if (l) { l.insertAdjacentHTML("beforeend", html); scroll(); } };

  let busy = false;
  const send = async (text) => {
    if (busy) return;
    const inp = $("#aInput");
    const q = (text !== undefined ? text : (inp ? inp.value : "")).trim();
    if (!q) return;
    busy = true;
    const empty = $("#aMsgs .assist-empty"); if (empty) empty.remove();
    if (inp) inp.value = "";
    State.assistantHistory.push({ role: "user", content: q });
    append(assistantBubble("user", q));
    append(`<div class="abub assistant thinking" id="aThink"><div class="ab-av">✨</div><div class="ab-tx"><span class="spin" style="width:14px;height:14px"></span> ${esc(t("assistant_thinking"))}</div></div>`);
    try {
      const r = await API.assistantAsk(q, State.assistantHistory.slice(0, -1));
      const reply = r.empty ? t("ai_empty_reply") : r.reply;
      State.assistantHistory.push({ role: "assistant", content: reply });
      const th = $("#aThink"); if (th) th.remove();
      append(assistantBubble("assistant", reply));
    } catch (err) {
      const th = $("#aThink"); if (th) th.remove();
      append(assistantBubble("assistant", errMsg(err)));
    }
    busy = false;
    const i2 = $("#aInput"); if (i2) i2.focus();
  };
  $("#aSend").addEventListener("click", () => send());
  $("#aInput").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  $$(".sug").forEach((b) => b.addEventListener("click", () => send(t(b.dataset.sug))));
  $("#aInput").focus();
  scroll();
}

/* =========================================================================
   WORKSPACE
   ======================================================================== */
let wsTab = "board";
let boardView = "board";
let boardFilter = { q: "", status: "", awaiting: false, fields: {} };

async function viewWorkspace(id) {
  stopFeed();
  if (State._wsId !== id) { State._wsId = id; wsTab = "board"; boardFilter = { q: "", status: "", awaiting: false, fields: {} }; }
  let ws;
  try { ws = await API.workspace(id); } catch (err) { toast(errMsg(err), "err"); go("/"); return; }
  setIndustry(ws.template);
  const noun = tplNoun(ws.template, ws.item_noun);
  const crumbs = crumb([{ label: t("home"), href: "/" }, { label: ws.name }]);

  const tabs = `<div class="tabs">
    <button data-tab="board" class="${wsTab === "board" ? "active" : ""}">${esc(t("tab_orders"))}</button>
    <button data-tab="comms" class="${wsTab === "comms" ? "active" : ""}">${esc(t("tab_comms"))}</button>
    <button data-tab="files" class="${wsTab === "files" ? "active" : ""}">${esc(t("ar_tab"))}</button>
    <button data-tab="payouts" class="${wsTab === "payouts" ? "active" : ""}">${esc(t("po_tab"))}</button>
    <button data-tab="activity" class="${wsTab === "activity" ? "active" : ""}">${esc(t("tab_activity"))}</button>
    <button data-tab="settings" class="${wsTab === "settings" ? "active" : ""}">${esc(t("tab_settings"))}</button>
  </div>`;

  const header = `<div class="page-head">
    <div><h1>${esc(ws.name)}</h1>
      <div class="sub flex center gap8 wrap">
        <span class="badge accent">${esc(tplLabel(ws.template, ws.template_label))}</span>
        <span>${esc(ws.buyer.name)} <span class="dim">↔</span> ${ws.partner ? esc(ws.partner.name) : `<span class="dim">${esc(ws.pending_invite ? t("awaiting_partner") : t("no_partner"))}</span>`}</span>
        <span class="badge ${ws.side}">${esc(ws.side === "buyer" ? t("you_are_business") : t("you_are_partner"))}</span>
      </div></div>
    ${wsTab === "board" ? `<button class="btn primary" id="newOrder"><span class="ico">＋</span> ${esc(noun)}</button>` : ""}
  </div>`;

  shell(crumbs, `<div class="page">${header}${tabs}<div id="tabbody"><div class="loading-full"><div class="spin"></div></div></div></div>`);

  $$(".tabs button").forEach((b) => b.addEventListener("click", () => { wsTab = b.dataset.tab; viewWorkspace(id); }));
  const no = $("#newOrder"); if (no) no.addEventListener("click", () => openCreateOrder(ws));

  if (wsTab === "board") await renderBoard(ws);
  else if (wsTab === "comms") await renderComms(ws);
  else if (wsTab === "files") await renderFiles(ws);
  else if (wsTab === "payouts") await renderPayouts(ws);
  else if (wsTab === "activity") await renderActivity(ws);
  else renderSettings(ws);
}

/* ---- the relationship's shelf -------------------------------------------
 * A framework contract, an NDA, an insurance certificate: paperwork that
 * belongs to the relationship rather than to any one order inside it. What is
 * shared here both sides can open; what is not stays with the company that
 * filed it. */
async function renderFiles(ws) {
  let d;
  try { d = await API.files("?workspace_id=" + ws.id); }
  catch (err) { $("#tabbody").innerHTML = `<div class="empty"><p>${esc(errMsg(err))}</p></div>`; return; }

  const row = (f) => `
    <div class="an-row">
      <span style="font-size:20px">${fileIcon(f.mime, f.name)}</span>
      <div class="grow"><b>${esc(f.title)}</b>
        ${f.shared ? `<span class="badge ok dot">${esc(t("ar_shared"))}</span>`
                   : `<span class="badge">${esc(t("ar_private"))}</span>`}
        ${!f.mine ? `<span class="badge partner">${esc(t("ar_from_them"))}</span>` : ""}
        <div class="dim" style="font-size:11.5px">${esc(f.name)} · ${esc(fileSize(f.size))} · ${relTime(f.at)}</div>
        ${f.note ? `<div class="dim" style="font-size:11.5px">${esc(f.note)}</div>` : ""}</div>
      <a class="btn ghost sm" href="${esc(f.url)}">${esc(t("ar_download"))}</a>
      ${f.mine ? `<button class="btn ghost sm" data-wsf-share="${f.id}" data-on="${f.shared ? 1 : 0}">${
        esc(f.shared ? t("ar_unshare") : t("ar_share"))}</button>
        <button class="btn ghost sm" data-wsf-del="${f.id}">${esc(t("tm_remove"))}</button>` : ""}
    </div>`;

  $("#tabbody").innerHTML = `
    <div class="card pad">
      <div class="flex between center wrap gap8">
        <div><b>${esc(t("ar_tab"))}</b>
          <div class="dim" style="font-size:12.5px">${esc(t("ar_ws_hint"))}</div></div>
        <button class="btn primary sm" id="wsfAdd">${esc(t("ar_add"))}</button>
      </div>
      <div class="an-rows mt12">${d.files.length ? d.files.map(row).join("")
        : `<p class="muted" style="font-size:13px">${esc(t("ar_none_ws"))}</p>`}</div>
    </div>`;

  $("#wsfAdd").addEventListener("click", () =>
    openFileUpload({ workspace_id: ws.id, shared: true }, () => renderFiles(ws)));
  $$("[data-wsf-share]").forEach((b) => b.addEventListener("click", async () => {
    try {
      await API.fileUpdate(b.dataset.wsfShare, { shared: b.dataset.on !== "1" });
      toast(t("saved"), "ok"); renderFiles(ws);
    } catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-wsf-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(t("ar_delete_ask"))) return;
    try { await API.fileDelete(b.dataset.wsfDel); toast(t("saved"), "ok"); renderFiles(ws); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
}

/* ---- payouts (roadmap 5) ------------------------------------------------- *
 * Seam records what is owed and when it was settled; it moves no money. The
 * payer records and settles, the payee sees their own - so "was that one
 * paid?" stops being a question asked in a chat thread. */
async function renderPayouts(ws) {
  let p;
  try { p = await API.payouts(ws.id); }
  catch (err) { $("#tabbody").innerHTML = `<div class="empty"><p>${esc(errMsg(err))}</p></div>`; return; }

  let orders = [];
  try { orders = await API.orders(ws.id); } catch {}
  const method = (m) => t("po_m_" + m);
  const st = (s) => ({ due: "warn", paid: "ok", cancelled: "" }[s] || "");

  const rows = p.payouts.length ? p.payouts.map((x) => `
    <div class="po-row">
      <div class="grow">
        <b>${esc(displayMoneyText(x.amount))}</b>
        ${x.description ? ` · ${esc(x.description)}` : ""}
        <div class="dim" style="font-size:11.5px">
          ${esc(x.i_pay ? t("po_to", { n: x.payee }) : t("po_from", { n: x.payer }))} ·
          ${esc(method(x.method))}${x.reference ? " · " + esc(x.reference) : ""}
          ${x.order_ref ? ` · <a href="#/o/${x.order_id}">${esc(x.order_ref)}</a>` : ""}
        </div>
        <div class="dim" style="font-size:11px">
          ${x.status === "paid" && x.paid_at ? esc(t("po_paid_on", { d: relTime(x.paid_at) }))
            : x.due_date ? esc(t("po_due_on", { d: x.due_date })) : ""}
          ${x.note ? " · " + esc(quoted(x.note)) : ""}
        </div>
      </div>
      <span class="badge ${st(x.status)}">${esc(t("po_st_" + x.status))}</span>
      ${x.i_pay ? `<div class="flex gap8">
        ${x.status !== "paid" ? `<button class="btn primary sm" data-po-paid="${x.id}">${esc(t("po_mark_paid"))}</button>` : ""}
        ${x.status !== "cancelled" && x.status !== "paid" ? `<button class="btn ghost sm" data-po-cancel="${x.id}">${esc(t("po_cancel"))}</button>` : ""}
        ${x.status === "paid" ? `<button class="btn ghost sm" data-po-undo="${x.id}">${esc(t("po_undo"))}</button>` : ""}
      </div>` : ""}
    </div>`).join("") : `<div class="empty"><p>${esc(t("po_empty"))}</p></div>`;

  $("#tabbody").innerHTML = `
    <div class="an-tiles mt8">
      <div class="an-tile"><div class="n">${esc(displayMoneyText(p.totals.due))}</div>
        <div class="l">${esc(t("po_total_due"))}</div></div>
      <div class="an-tile"><div class="n">${esc(displayMoneyText(p.totals.paid))}</div>
        <div class="l">${esc(t("po_total_paid"))}</div></div>
    </div>
    <div class="card pad mt16">
      <p class="dim" style="font-size:12.5px">${esc(t("po_hint"))}</p>
      <div class="mt12">${rows}</div>
      ${ws.partner ? `<button class="btn primary sm mt12" id="poNew">＋ ${esc(t("po_new"))}</button>` : ""}
    </div>`;

  const reload = () => renderPayouts(ws);
  const nb = $("#poNew");
  if (nb) nb.addEventListener("click", () => {
    openModal(t("po_new"), `
      <div class="grid2">
        <label class="field"><span>${esc(t("po_amount"))} *</span>
          <input class="input" id="poAmt" placeholder="1250" /></label>
        <label class="field"><span>${esc(t("po_method"))}</span>
          <select class="select" id="poMethod">${p.methods.map((m) =>
            `<option value="${m}">${esc(method(m))}</option>`).join("")}</select></label>
      </div>
      <label class="field"><span>${esc(t("po_description"))}</span>
        <input class="input" id="poDesc" maxlength="200" /></label>
      <div class="grid2">
        <label class="field"><span>${esc(t("po_reference"))}</span>
          <input class="input" id="poRef" maxlength="80" /></label>
        <label class="field"><span>${esc(t("po_due"))}</span>
          <input class="input" id="poDue" type="date" /></label>
      </div>
      <label class="field"><span>${esc(t("po_order"))}</span>
        <select class="select" id="poOrder"><option value="">${esc(t("po_no_order"))}</option>
          ${orders.map((o) => `<option value="${o.id}">${esc(o.ref)} · ${esc(o.title)}</option>`).join("")}
        </select></label>`,
      `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
       <button class="btn primary" id="poSave">${esc(t("save"))}</button>`);
    $("#poSave").addEventListener("click", async () => {
      try {
        await API.payoutCreate(ws.id, {
          amount: $("#poAmt").value.trim(), method: $("#poMethod").value,
          description: $("#poDesc").value.trim(), reference: $("#poRef").value.trim(),
          due_date: $("#poDue").value, order_id: Number($("#poOrder").value) || null,
        });
        closeModal(); toast(t("saved"), "ok"); reload();
      } catch (err) { toast(errMsg(err), "err"); }
    });
  });

  const setStatus = async (id, status, note) => {
    try { await API.payoutUpdate(id, status, note); reload(); }
    catch (err) { toast(errMsg(err), "err"); }
  };
  $$("[data-po-paid]").forEach((b) => b.addEventListener("click", () =>
    setStatus(b.dataset.poPaid, "paid", prompt(t("po_note_ask")) || "")));
  $$("[data-po-cancel]").forEach((b) => b.addEventListener("click", () =>
    setStatus(b.dataset.poCancel, "cancelled")));
  $$("[data-po-undo]").forEach((b) => b.addEventListener("click", () =>
    setStatus(b.dataset.poUndo, "due")));
}

/* ---- workspace communication channel ------------------------------------ */
function msgBubble(author, when, bodyText, showTr) {
  const enc = encodeURIComponent(bodyText);
  return `<div class="cmt">
      <div class="av" style="background:${colorFor(author)}">${initials(author)}</div>
      <div class="bd"><div class="meta"><b>${esc(author)}</b> · ${relTime(when)}</div>
      <div class="body">${esc(bodyText)}</div>
      ${showTr ? `<button class="tr-btn" data-tr="${enc}">🌐 ${esc(t("translate"))}</button><div class="tr-out"></div>` : ""}
      </div></div>`;
}

function wireTranslate(scope) {
  (scope || document).querySelectorAll("[data-tr]").forEach((b) => {
    if (b._wired) return; b._wired = true;
    b.addEventListener("click", async () => {
      const out = b.nextElementSibling;
      if (b.dataset.open === "1") { out.innerHTML = ""; b.dataset.open = "0"; b.textContent = "🌐 " + t("translate"); return; }
      const text = decodeURIComponent(b.dataset.tr);
      b.disabled = true; out.innerHTML = `<span class="dim">${esc(t("translating"))}</span>`;
      try {
        const r = await API.translate(text, LANG);
        out.innerHTML = `<div class="tr-res">${esc(r.text)}<div class="tr-tag">🌐 ${esc(t("translated_to"))} ${esc(langLabel(LANG) || LANG)}</div></div>`;
        b.dataset.open = "1"; b.textContent = "✕ " + t("hide_translation");
      } catch (err) { out.innerHTML = `<span class="err-inline">${esc(errMsg(err))}</span>`; }
      finally { b.disabled = false; }
    });
  });
}

const EMERGENCY_LINKS = {
  phone: (v) => "tel:" + v.replace(/[^\d+]/g, ""),
  whatsapp: (v) => "https://wa.me/" + v.replace(/[^\d]/g, ""),
  viber: (v) => "viber://chat?number=" + encodeURIComponent(v.replace(/[^\d+]/g, "")),
  telegram: (v) => (v.startsWith("@") ? "https://t.me/" + v.slice(1) : (/^\+?\d+$/.test(v) ? "https://t.me/" + v.replace(/[^\d]/g, "") : "https://t.me/" + v)),
  alt_email: (v) => "mailto:" + v,
};
const EMERGENCY_ICONS = { phone: "📞", whatsapp: "🟢", viber: "🟣", telegram: "✈️", alt_email: "✉️", note: "📝" };

function emergencyPanel(c, otherName) {
  const items = ["phone", "whatsapp", "viber", "telegram", "alt_email"].filter((k) => c && c[k])
    .map((k) => `<a class="emg-chip" href="${esc(EMERGENCY_LINKS[k](c[k]))}" target="_blank" rel="noopener">
      ${EMERGENCY_ICONS[k]} ${esc(t("emg_" + k))}: <b>${esc(c[k])}</b></a>`).join("");
  const note = c && c.note ? `<div class="emg-note">📝 ${esc(c.note)}</div>` : "";
  return `<div class="card pad emg-card">
    <div class="flex between center wrap gap8"><b>🚨 ${esc(t("emg_title"))}</b>
      <button class="btn ghost sm" id="emgEdit">${esc(t("emg_edit"))}</button></div>
    <p class="dim" style="font-size:12px;margin:4px 0 8px">${esc(t("emg_hint"))}</p>
    ${items ? `<div class="emg-list"><span class="dim" style="font-size:12px">${esc(otherName)}:</span>${items}</div>${note}`
      : `<p class="muted" style="font-size:13px">${esc(t("emg_empty"))}</p>`}
  </div>`;
}

async function renderComms(ws) {
  let msgs = [], contacts = { other: {}, mine: {}, other_name: "" };
  try { [msgs, contacts] = await Promise.all([API.wsMessages(ws.id), API.wsContacts(ws.id)]); }
  catch (err) { toast(errMsg(err), "err"); }
  const list = msgs.length ? msgs.map((m) => msgBubble(m.author, m.created_at, m.body, !m.mine)).join("")
    : `<p class="muted" style="font-size:13px">${esc(t("no_messages"))}</p>`;
  $("#tabbody").innerHTML = `<div style="max-width:760px">
    ${emergencyPanel(contacts.other, contacts.other_name || t("partner_label"))}
    <div class="card pad mt16">
      <p class="dim" style="font-size:12px;margin-bottom:8px">${esc(t("comms_hint"))}</p>
      <div class="tr-banner">🌐 ${esc(t("comms_translate_hint"))}</div>
      <div id="wsMsgs">${list}</div>
      <div class="cmt-box"><input class="input" id="wsMsgInput" placeholder="${esc(t("comms_ph"))}" />
        <button class="btn primary" id="wsMsgSend">${esc(t("send"))}</button></div>
    </div>
  </div>`;
  const send = async () => {
    const i = $("#wsMsgInput"); const v = i.value.trim(); if (!v) return;
    try { await API.wsMessagePost(ws.id, v); renderComms(ws); }
    catch (err) { toast(errMsg(err), "err"); }
  };
  $("#wsMsgSend").addEventListener("click", send);
  $("#wsMsgInput").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  const eb = $("#emgEdit"); if (eb) eb.addEventListener("click", () => openEmergencyEdit(contacts.mine, ws));
  wireTranslate($("#wsMsgs"));
  const box = $("#wsMsgs"); if (box) box.scrollTop = box.scrollHeight;
}

function openEmergencyEdit(mine, ws) {
  mine = mine || {};
  const f = (k, ph) => `<label class="field"><span>${EMERGENCY_ICONS[k]} ${esc(t("emg_" + k))}</span>
    <input class="input" id="emg_${k}" value="${esc(mine[k] || "")}" placeholder="${esc(ph || "")}" /></label>`;
  openModal(t("emg_edit_title"), `
    <p class="muted" style="margin-bottom:4px">${esc(t("emg_edit_hint"))}</p>
    <div class="row2">${f("phone", "+359…")}${f("whatsapp", "+359…")}</div>
    <div class="row2">${f("viber", "+359…")}${f("telegram", "@handle")}</div>
    ${f("alt_email", "ops@…")}
    <label class="field"><span>${EMERGENCY_ICONS.note} ${esc(t("emg_note"))}</span><input class="input" id="emg_note" value="${esc(mine.note || "")}" placeholder="${esc(t("emg_note_ph"))}" /></label>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="emgSave">${esc(t("save"))}</button>`);
  $("#emgSave").addEventListener("click", async () => {
    const d = {}; ["phone", "whatsapp", "viber", "telegram", "alt_email", "note"].forEach((k) => { d[k] = ($("#emg_" + k).value || "").trim(); });
    try { await API.orgContactsSet(d); closeModal(); toast(t("saved"), "ok"); renderComms(ws); }
    catch (err) { toast(errMsg(err), "err"); }
  });
}
function applyFilters(orders) {
  const q = boardFilter.q.toLowerCase();
  return orders.filter((o) => {
    if (boardFilter.status && o.status !== boardFilter.status) return false;
    if (boardFilter.awaiting && !o.awaiting_you) return false;
    for (const k in boardFilter.fields) {
      const v = boardFilter.fields[k];
      if (v && String(o.fields[k] || "") !== v) return false;
    }
    if (q) {
      const hay = (o.title + " " + o.ref + " " + JSON.stringify(o.fields)).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

async function renderBoard(ws) {
  const tpl = State.templates[ws.template];
  const noun = tplNoun(ws.template, ws.item_noun);
  let all = [];
  try { all = await API.orders(ws.id); } catch (err) { toast(errMsg(err), "err"); }
  const orders = applyFilters(all);

  // branch-specific field filters, from distinct values present in the data
  const fieldSelects = (tpl.filters || []).map((fk) => {
    const vals = [...new Set(all.map((o) => o.fields[fk]).filter(Boolean))];
    if (!vals.length) return "";
    return `<select class="select bt-select" data-ffield="${fk}">
      <option value="">${esc(tplField(ws.template, fk))}: ${esc(t("f_all"))}</option>
      ${vals.map((v) => `<option ${boardFilter.fields[fk] === String(v) ? "selected" : ""}>${esc(v)}</option>`).join("")}
    </select>`;
  }).join("");

  const statusSelect = `<select class="select bt-select" id="fStatus">
    <option value="">${esc(t("f_status"))}: ${esc(t("f_all"))}</option>
    ${tpl.stages.map((s) => `<option value="${s.key}" ${boardFilter.status === s.key ? "selected" : ""}>${esc(tplStage(ws.template, s.key, s.label))}</option>`).join("")}
  </select>`;

  const active = boardFilter.q || boardFilter.status || boardFilter.awaiting || Object.values(boardFilter.fields).some(Boolean);
  const toolbar = `<div class="board-toolbar">
    <div class="search"><span class="ico">⌕</span><input class="input" id="search" placeholder="${esc(t("search_ph", { noun: noun.toLowerCase() }))}" value="${esc(boardFilter.q)}" /></div>
    ${statusSelect}
    ${fieldSelects}
    <button class="chip-toggle ${boardFilter.awaiting ? "active" : ""}" id="fAwait">${esc(t("f_awaiting"))}</button>
    ${active ? `<button class="btn ghost sm" id="fClear">✕ ${esc(t("f_clear"))}</button>` : ""}
    <div class="tb-spacer"></div>
    <div class="seg">
      <button data-bv="board" class="${boardView === "board" ? "active" : ""}">${esc(t("view_stages"))}</button>
      <button data-bv="table" class="${boardView === "table" ? "active" : ""}">${esc(t("view_table"))}</button>
    </div>
  </div>`;

  let bodyHTML;
  if (!all.length) {
    bodyHTML = emptyState("📋", t("empty_orders_title", { noun: noun.toLowerCase() }),
      t("empty_orders_text"),
      `<button class="btn primary" id="firstOrder"><span class="ico">＋</span> ${esc(t("new_item", { noun: noun.toLowerCase() }))}</button>`);
  } else if (!orders.length) {
    bodyHTML = emptyState("🔍", t("no_matches"), "",
      `<button class="btn ghost" id="fClear2">✕ ${esc(t("f_clear"))}</button>`);
  } else if (boardView === "board") {
    bodyHTML = `<div class="board">${tpl.stages.map((st, i) => {
      const inCol = orders.filter((o) => o.status === st.key);
      return `<div class="col" data-stage="${st.key}">
        <div class="col-head"><span class="name">${esc(tplStage(ws.template, st.key, st.label))}</span><span class="cnt">${inCol.length}</span></div>
        <div class="col-body" data-drop="${st.key}">${inCol.map((o) => orderMiniCard(o, ws, i)).join("")}</div>
      </div>`;
    }).join("")}</div>`;
  } else {
    bodyHTML = orderTable(orders, ws);
  }

  $("#tabbody").innerHTML = toolbar + bodyHTML;

  $$(".seg [data-bv]").forEach((b) => b.addEventListener("click", () => { boardView = b.dataset.bv; renderBoard(ws); }));
  const search = $("#search");
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { boardFilter.q = search.value.trim(); renderBoard(ws); }, 240); });
  const st = $("#fStatus"); if (st) st.addEventListener("change", () => { boardFilter.status = st.value; renderBoard(ws); });
  $$("[data-ffield]").forEach((s) => s.addEventListener("change", () => { boardFilter.fields[s.dataset.ffield] = s.value; renderBoard(ws); }));
  const aw = $("#fAwait"); if (aw) aw.addEventListener("click", () => { boardFilter.awaiting = !boardFilter.awaiting; renderBoard(ws); });
  const clear = () => { boardFilter = { q: "", status: "", awaiting: false, fields: {} }; renderBoard(ws); };
  ["#fClear", "#fClear2"].forEach((s) => { const b = $(s); if (b) b.addEventListener("click", clear); });
  const fo = $("#firstOrder"); if (fo) fo.addEventListener("click", () => openCreateOrder(ws));
  $$(".ocard, .tbl tr.clickable").forEach((c) => c.addEventListener("click", () => go(`/o/${c.dataset.id}`)));

  if (boardView === "board") setupDragDrop(ws);
}

function stageIndexClass(tplKey, statusKey) {
  const stages = (State.templates[tplKey] || {}).stages || [];
  const idx = stages.findIndex((s) => s.key === statusKey);
  return `stpill-${Math.min(idx < 0 ? 0 : idx, 6)}`;
}

function orderMiniCard(o, ws) {
  const tpl = State.templates[ws.template];
  const chips = (tpl.fields || []).slice(0, 3).map((f) => {
    const v = o.fields[f.key]; if (!v) return "";
    return `<span class="kv">${esc(String(v).slice(0, 22))}</span>`;
  }).filter(Boolean).join("");
  return `<div class="ocard" draggable="true" data-id="${o.id}" data-status="${o.status}">
    <div class="ref mono">${esc(o.ref)}</div>
    <div class="ttl">${esc(o.title)}</div>
    <div class="meta">${chips || `<span class="dim">${esc(t("no_details"))}</span>`}</div>
    ${o.awaiting_you ? `<div class="flag"><span class="badge warn dot">${esc(t("awaiting_you"))}</span></div>` : ""}
  </div>`;
}

function orderTable(orders, ws) {
  const noun = tplNoun(ws.template, ws.item_noun);
  return `<table class="tbl"><thead><tr>
    <th>${esc(t("col_ref"))}</th><th>${esc(noun)}</th><th>${esc(t("col_status"))}</th><th>${esc(t("col_updated"))}</th><th></th>
  </tr></thead><tbody>
    ${orders.map((o) => `<tr class="clickable" data-id="${o.id}">
      <td class="mono dim">${esc(o.ref)}</td>
      <td><b>${esc(o.title)}</b></td>
      <td><span class="stpill ${stageIndexClass(ws.template, o.status)}">${esc(tplStage(ws.template, o.status, o.status_label))}</span></td>
      <td class="dim">${relTime(o.updated_at)}</td>
      <td>${o.awaiting_you ? `<span class="badge warn dot">${esc(t("for_you_short"))}</span>` : ""}</td>
    </tr>`).join("")}
  </tbody></table>`;
}

function setupDragDrop(ws) {
  let dragId = null;
  $$(".ocard").forEach((card) => {
    card.addEventListener("dragstart", () => { dragId = card.dataset.id; card.classList.add("dragging"); });
    card.addEventListener("dragend", () => { card.classList.remove("dragging"); dragId = null; });
  });
  $$(".col-body").forEach((col) => {
    col.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("dragover"); });
    col.addEventListener("dragleave", () => col.classList.remove("dragover"));
    col.addEventListener("drop", async (e) => {
      e.preventDefault(); col.classList.remove("dragover");
      if (!dragId) return;
      const newStatus = col.dataset.drop;
      const card = $(`.ocard[data-id="${dragId}"]`);
      if (card && card.dataset.status === newStatus) return;
      try { await API.setStatus(dragId, newStatus); toast(t("status_updated"), "ok"); renderBoard(ws); }
      catch (err) { toast(errMsg(err), "err"); }
    });
  });
}

/* ---- activity (workspace audit) ---------------------------------------- */
async function renderActivity(ws) {
  let events = [];
  try { events = await API.activity(ws.id); } catch (err) { toast(errMsg(err), "err"); }
  if (!events.length) { $("#tabbody").innerHTML = emptyState("🧾", t("activity_empty_title"), t("activity_empty_text")); return; }
  $("#tabbody").innerHTML = `<div class="card pad"><div class="timeline">
    ${events.map((e) => timelineItem(e, true)).join("")}
  </div></div>`;
  $$("#tabbody [data-go-order]").forEach((a) => a.addEventListener("click", () => go(`/o/${a.dataset.goOrder}`)));
}

/* ---- settings / partner ------------------------------------------------- */
function renderSettings(ws) {
  const partnerBlock = ws.partner ? `
    <div class="kvrow"><div class="k">${esc(t("partner_label"))}</div><div class="v">${esc(ws.partner.name)} <span class="badge partner">${esc(t("free_access"))}</span>${ws.partner.verified_company ? ` <span class="badge ok dot">${esc(t("company_verified"))}</span>` : ""}</div><div><a class="btn ghost sm" href="#/passport/${ws.partner.id}">🪪 ${esc(t("passport_open"))}</a></div></div>` :
    (ws.side === "buyer" ? `
    <div class="card pad" style="background:var(--surface-2)">
      <b>${esc(t("invite_partner_title"))}</b>
      <p class="muted" style="font-size:13px">${esc(t("invite_partner_text"))}</p>
      <div class="flex gap8 mt8"><input class="input" id="invEmail" type="email" placeholder="partner@example.com" value="${esc(ws.pending_invite || "")}" />
        <button class="btn primary" id="invBtn">${esc(t("invite_send"))}</button></div>
      ${ws.pending_invite ? `<p class="dim mt8" style="font-size:12.5px">${esc(t("waiting_for", { email: ws.pending_invite }))}</p>` : ""}
    </div>` : `<p class="muted">${esc(t("partner_not_added"))}</p>`);

  $("#tabbody").innerHTML = `<div class="card pad" style="max-width:640px">
    <div class="kvlist">
      <div class="kvrow"><div class="k">${esc(t("business_pays"))}</div><div class="v">${esc(ws.buyer.name)} <span class="badge buyer">${esc(t("role_business"))}</span>${ws.buyer.verified_company ? ` <span class="badge ok dot">${esc(t("company_verified"))}</span>` : ""}</div><div><a class="btn ghost sm" href="#/passport/${ws.buyer.id}">🪪 ${esc(t("passport_open"))}</a></div></div>
      ${partnerBlock}
      <div class="kvrow"><div class="k">${esc(t("template_label"))}</div><div class="v">${esc(tplLabel(ws.template, ws.template_label))} - <span class="muted">${esc(tplTagline(ws.template, ws.tagline))}</span></div><div></div></div>
      <div class="kvrow"><div class="k">${esc(t("your_role"))}</div><div class="v"><span class="badge ${ws.side}">${esc(ws.side === "buyer" ? t("role_business") : t("role_partner"))}</span></div><div></div></div>
    </div>
    <p class="dim mt16" style="font-size:12.5px">${esc(t("model_note"))}</p>
  </div>`;
  const ib = $("#invBtn");
  if (ib) ib.addEventListener("click", async () => {
    const email = $("#invEmail").value.trim();
    if (!email) { toast(t("enter_email"), "err"); return; }
    try { await API.invite(ws.id, email); toast(t("invite_sent"), "ok"); viewWorkspace(ws.id); }
    catch (err) { toast(errMsg(err), "err"); }
  });
}

/* ---- create order modal ------------------------------------------------- */
function openCreateOrder(ws) {
  const tpl = State.templates[ws.template];
  const noun = tplNoun(ws.template, ws.item_noun);
  const fieldHTML = (tpl.fields || []).map((f) => fieldInput(f, undefined, ws.template)).join("");
  openModal(t("corder_title", { noun: noun.toLowerCase() }), `
    <label class="field"><span>${esc(t("corder_title_label"))} *</span><input class="input" id="oTitle" placeholder="${esc(t("corder_title_ph", { noun }))}" /></label>
    <div class="row2" id="oFields">${fieldHTML}</div>
    <p class="dim" style="font-size:12.5px">${esc(t("corder_terms_hint"))}</p>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="oCreate">${esc(t("create"))}</button>`);
  $("#oCreate").addEventListener("click", async () => {
    const title = $("#oTitle").value.trim();
    if (!title) { toast(t("enter_title"), "err"); return; }
    const fields = collectFields("#oFields");
    const btn = $("#oCreate"); btn.disabled = true;
    try { const o = await API.createOrder(ws.id, { title, fields }); closeModal(); toast(t("created"), "ok"); go(`/o/${o.id}`); }
    catch (err) { toast(err.message, "err"); btn.disabled = false; }
  });
}

function fieldInput(f, value, tplKey) {
  const v = value == null ? "" : value;
  const req = f.required ? " *" : "";
  const label = tplKey ? tplField(tplKey, f.key, f.label) : f.label;
  let input;
  if (f.type === "textarea") input = `<textarea class="textarea" data-fk="${f.key}" placeholder="${esc(f.placeholder || "")}">${esc(v)}</textarea>`;
  else if (f.type === "select") input = `<select class="select" data-fk="${f.key}"><option value="">-</option>${(f.options || []).map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  else { const type = f.type === "number" || f.type === "money" ? "number" : (f.type === "date" ? "date" : "text");
    input = `<input class="input" data-fk="${f.key}" type="${type}" step="any" placeholder="${esc(f.placeholder || "")}" value="${esc(v)}" />`; }
  const full = f.type === "textarea" ? ' style="grid-column:1/-1"' : "";
  return `<label class="field"${full}><span>${esc(label)}${req}</span>${input}</label>`;
}
function collectFields(root) {
  const out = {};
  $$(`${root} [data-fk]`).forEach((el) => { const v = el.value.trim(); if (v !== "") out[el.dataset.fk] = v; });
  return out;
}

/* =========================================================================
   ORDER DETAIL
   ======================================================================== */
async function viewOrder(id) {
  stopFeed();
  let o;
  try { o = await API.order(id); } catch (err) { toast(errMsg(err), "err"); go("/"); return; }
  let ws;
  try { ws = await API.workspace(o.workspace_id); } catch {}
  try { State._sigs = await API.signatures(id); } catch { State._sigs = []; }
  try { State._appr = await API.approvals(id); } catch { State._appr = null; }
  try { State._chains = await API.chains(); } catch { State._chains = null; }
  renderOrder(o, ws);
  startFeed(id, o);
}

function renderOrder(o, ws) {
  setIndustry(o.template);
  const crumbs = crumb([{ label: t("home"), href: "/" }, { label: ws ? ws.name : "-", href: `/w/${o.workspace_id}` }, { label: o.ref }]);
  const stages = o.stages;
  const curIdx = stages.findIndex((s) => s.key === o.status);

  const pipeline = `<div class="pipeline">${stages.map((s, i) => `
    <div class="pip ${i < curIdx ? "done" : ""} ${i === curIdx ? "current" : ""}" data-stage="${s.key}" title="${esc(tplStage(o.template, s.key, s.label))}">
      <div class="bar"></div><div class="lab">${esc(tplStage(o.template, s.key, s.label))}</div>
    </div>`).join("")}</div>`;

  const awaiting = o.terms.some((tm) => tm.awaiting_you);

  shell(crumbs, `<div class="page">
    <div class="page-head">
      <div>
        <div class="flex center gap8"><span class="mono dim">${esc(o.ref)}</span>${awaiting ? `<span class="badge warn dot">${esc(t("awaiting_you"))}</span>` : ""}</div>
        <h1 style="margin-top:2px">${esc(o.title)}</h1>
      </div>
      <div class="flex gap8 center wrap">
        <a class="btn ghost sm" href="#/passport/order/${o.id}">🛡 ${esc(t("pr_title"))}</a>
        <a class="btn ghost sm" href="/protocol/${o.id}" target="_blank" rel="noopener">🖨 ${esc(t("protocol_btn"))}</a>
        <span class="badge ${o.side}">${esc(o.side === "buyer" ? t("you_business") : t("you_partner"))}</span>
      </div>
    </div>

    <div class="card pad mt8">
      <div class="flex between center" style="margin-bottom:12px"><b>${esc(t("status"))}</b><span class="dim live-dot">● ${esc(t("live"))}</span></div>
      ${pipeline}
      <p class="dim mt8" style="font-size:12.5px">${esc(t("status_hint"))}</p>
    </div>

    <div class="detail-grid mt16">
      <div>${renderFieldsCard(o)}${renderApprovalCard(o)}${renderMediaCard(o)}${renderSignCard(o)}${renderCommentsCard(o)}</div>
      <div>${renderTermsCard(o)}${renderTimelineCard(o)}</div>
    </div>
  </div>`);

  $$(".pip").forEach((p) => p.addEventListener("click", async () => {
    if (p.dataset.stage === o.status) return;
    try { const upd = await API.setStatus(o.id, p.dataset.stage); toast(t("status_updated"), "ok"); renderOrder(upd, ws); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  wireFieldsCard(o, ws);
  wireApprovalCard(o, ws);
  wireMediaCard(o, ws);
  wireSignCard(o, ws);
  wireTermsCard(o, ws);
  wireCommentsCard(o, ws);
}

/* ---- multi-level approvals ---------------------------------------------- *
 * Steps unlock one at a time. The card shows the whole chain, not only the
 * step you can act on, so everyone can see where a decision is waiting. */
function renderApprovalCard(o) {
  const ap = State._appr;
  const chains = (State._chains || {}).chains || [];
  if (!ap) return "";
  const icon = { approved: "✓", rejected: "✕", skipped: "–", pending: "○" };
  const cls = { approved: "ok", rejected: "danger", skipped: "", pending: "warn" };

  if (!ap.approvals.length) {
    const usable = chains.filter((ch) => ch.active && (!ch.template || ch.template === o.template));
    if (!usable.length) return "";
    return `<div class="card pad mt16">
      <div class="flex between center"><b>${esc(t("ap_title"))}</b></div>
      <p class="dim mt8" style="font-size:12.5px">${esc(t("ap_none_hint"))}</p>
      <div class="flex gap8 center wrap mt12">
        <select class="select" id="apChain">${usable.map((ch) =>
          `<option value="${ch.id}">${esc(ch.name)} · ${esc(t("ap_steps_n", { n: ch.steps.length }))}</option>`).join("")}</select>
        <button class="btn primary sm" id="apStart">${esc(t("ap_start"))}</button>
      </div></div>`;
  }

  const rows = ap.approvals.map((a) => `
    <div class="ap-step ${a.status} ${a.is_current ? "current" : ""}">
      <span class="ap-ic ${cls[a.status] || ""}">${icon[a.status] || "○"}</span>
      <div class="grow">
        <b>${esc(a.label)}</b>
        <div class="dim" style="font-size:11.5px">${
          a.status === "pending"
            ? esc(a.approver_name ? t("ap_waits_for", { n: a.approver_name }) : t("ap_waits_any"))
            : a.status === "skipped"
              ? esc(t("ap_skipped_hint"))
              : esc(t("ap_by", { n: a.decided_by || "-" }) + (a.decided_at ? " · " + relTime(a.decided_at) : ""))
        }</div>
        ${a.note ? `<div class="dim" style="font-size:11.5px">${esc(quoted(a.note))}</div>` : ""}
        ${a.ip ? `<div class="dim" style="font-size:11px">${esc(a.ip)} · ${esc(a.device)}</div>` : ""}
      </div>
      ${a.can_decide ? `<div class="flex gap8">
        <button class="btn primary sm" data-ap-ok="${a.id}">${esc(t("ap_approve"))}</button>
        <button class="btn ghost sm" data-ap-no="${a.id}">${esc(t("ap_reject"))}</button></div>` : ""}
    </div>`).join("");

  const badge = { pending: "warn", approved: "ok", rejected: "danger" }[ap.state] || "";
  return `<div class="card pad mt16">
    <div class="flex between center">
      <b>${esc(t("ap_title"))}</b>
      <span class="badge ${badge}">${esc(t("ap_state_" + ap.state))} · ${ap.progress.done}/${ap.progress.total}</span>
    </div>
    <p class="dim mt8" style="font-size:12.5px">${esc(t("ap_hint"))}</p>
    <div class="mt12">${rows}</div>
  </div>`;
}

function wireApprovalCard(o, ws) {
  const reload = async () => {
    try { State._appr = await API.approvals(o.id); } catch {}
    renderOrder(o, ws);
  };
  const start = $("#apStart");
  if (start) start.addEventListener("click", async () => {
    start.disabled = true;
    try { State._appr = await API.approvalsStart(o.id, Number($("#apChain").value)); renderOrder(o, ws); }
    catch (err) { toast(errMsg(err), "err"); start.disabled = false; }
  });
  const decide = async (id, decision) => {
    const note = decision === "rejected" ? (prompt(t("ap_note_ask")) || "") : "";
    try { State._appr = await API.approvalDecide(id, decision, note); toast(t("saved"), "ok"); renderOrder(o, ws); }
    catch (err) { toast(errMsg(err), "err"); reload(); }
  };
  $$("[data-ap-ok]").forEach((b) => b.addEventListener("click", () => decide(b.dataset.apOk, "approved")));
  $$("[data-ap-no]").forEach((b) => b.addEventListener("click", () => decide(b.dataset.apNo, "rejected")));
}

/* ---- electronic signatures ---------------------------------------------- */
function renderSignCard(o) {
  const sigs = State._sigs || [];
  const lvl = (l) => ({ simple: t("sig_l_simple"), advanced: t("sig_l_advanced"), qualified: t("sig_l_qualified") }[l] || l);
  const rows = sigs.length ? sigs.map((s) => `<div class="sig-row">
      <div class="grow">
        <b>${esc(s.signer_name || t("sig_awaiting"))}</b>${s.signer_role ? ` <span class="dim">· ${esc(s.signer_role)}</span>` : ""}
        <div class="dim" style="font-size:11.5px">${esc(lvl(s.level))}${s.signed_at ? ` · ${relTime(s.signed_at)}` : ""}${s.ip ? ` · ${esc(s.device || "")} ${esc(s.ip)}` : ""}</div>
      </div>
      ${s.status === "signed"
        ? `<span class="badge ok dot">${esc(t("sig_signed"))}</span>
           <a class="btn ghost sm" href="/signature/${s.id}" target="_blank" rel="noopener">${esc(t("sig_cert"))} ↗</a>`
        : `<span class="badge warn dot">${esc(t("sig_pending"))}</span>
           <button class="btn ghost sm" data-sig-copy="${esc(s.sign_url || "")}">${esc(t("int_copy"))}</button>
           <a class="btn primary sm" href="${esc(s.sign_url || "#")}" target="_blank" rel="noopener">${esc(t("sig_open"))} ↗</a>`}
    </div>`).join("") : `<p class="muted" style="font-size:13px">${esc(t("sig_empty"))}</p>`;
  return `<div class="card pad mt16">
    <div class="flex between center wrap gap8"><b>✍️ ${esc(t("sig_title"))}</b>
      <button class="btn ghost sm" id="sigNew">＋ ${esc(t("sig_request"))}</button></div>
    <p class="dim" style="font-size:12px;margin:4px 0 8px">${esc(t("sig_hint"))}</p>
    ${rows}
  </div>`;
}

function wireSignCard(o, ws) {
  const b = $("#sigNew");
  if (b) b.addEventListener("click", () => openSignRequest(o, ws));
  $$("[data-sig-copy]").forEach((x) => x.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(location.origin + x.dataset.sigCopy); toast(t("int_copied"), "ok"); }
    catch (e) { toast(errMsg(e), "err"); }
  }));
}

function openSignRequest(o, ws) {
  openModal(t("sig_request"), `
    <p class="muted">${esc(t("sig_req_hint"))}</p>
    <label class="field"><span>${esc(t("sig_level"))}</span><select class="select" id="sgLevel">
      <option value="simple">${esc(t("sig_l_simple"))}</option>
      <option value="advanced">${esc(t("sig_l_advanced"))}</option>
      <option value="qualified">${esc(t("sig_l_qualified"))}</option>
    </select></label>
    <div class="row2">
      <label class="field"><span>${esc(t("sig_signer"))}</span><input class="input" id="sgName" /></label>
      <label class="field"><span>${esc(t("sig_role"))}</span><input class="input" id="sgRole" /></label>
    </div>
    <label class="field"><span>${esc(t("email"))}</span><input class="input" id="sgEmail" type="email" /></label>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="sgGo">${esc(t("sig_create"))}</button>`);
  $("#sgGo").addEventListener("click", async () => {
    const btn = $("#sgGo"); btn.disabled = true;
    try {
      const r = await API.signatureRequest({
        order_id: o.id, doc_kind: "protocol", level: $("#sgLevel").value,
        signer_name: $("#sgName").value.trim(), signer_role: $("#sgRole").value.trim(),
        signer_email: $("#sgEmail").value.trim(),
      });
      closeModal(); toast(t("sig_created"), "ok");
      if (r.sign_url) window.open(r.sign_url, "_blank", "noopener");
      const upd = await API.order(o.id); renderOrder(upd, ws);
    } catch (err) { toast(errMsg(err), "err"); btn.disabled = false; }
  });
}

/* ---- progress evidence (photos / video) --------------------------------- */
function renderMediaCard(o) {
  const items = (o.attachments || []).map((a) => a.kind === "image"
    ? `<div class="med" data-att="${a.id}"><img src="${esc(a.url)}" loading="lazy" alt="" /></div>`
    : `<div class="med vid" data-att="${a.id}"><video src="${esc(a.url)}" preload="metadata" muted></video><span class="med-play">▶</span></div>`
  ).join("");
  return `<div class="card pad mt16">
    <div class="flex between center" style="margin-bottom:4px"><b>📷 ${esc(t("media_title"))}</b>
      <button class="btn ghost sm" id="attAdd">＋ ${esc(t("att_add"))}</button></div>
    <p class="dim" style="font-size:12px;margin-bottom:4px">${esc(t("media_hint"))}</p>
    ${items ? `<div class="med-grid">${items}</div>` : `<p class="muted" style="font-size:13px">${esc(t("att_empty"))}</p>`}
    <input type="file" id="attInput" class="hide" accept="image/*,video/*" multiple />
  </div>`;
}

function wireMediaCard(o, ws) {
  const btn = $("#attAdd"), inp = $("#attInput");
  if (!btn || !inp) return;
  btn.addEventListener("click", () => inp.click());
  inp.addEventListener("change", async () => {
    if (!inp.files.length) return;
    btn.disabled = true;
    try { const upd = await API.uploadAttachments(o.id, inp.files); toast(t("att_uploaded"), "ok"); renderOrder(upd, ws); }
    catch (err) { toast(errMsg(err), "err"); btn.disabled = false; }
  });
  $$(".med[data-att]").forEach((m) => m.addEventListener("click", () => {
    const a = (o.attachments || []).find((x) => x.id == m.dataset.att);
    if (a) openMediaLightbox(o, ws, a);
  }));
}

function openMediaLightbox(o, ws, a) {
  const media = a.kind === "image"
    ? `<img src="${esc(a.url)}" alt="" />`
    : `<video src="${esc(a.url)}" controls></video>`;
  const mine = State.user && a.uploader_id === State.user.id;
  const m = openModal(a.name, `<div class="lightbox">${media}</div>
    <div class="med-meta"><span>${esc(a.uploader)} · ${relTime(a.created_at)}</span>
      ${mine ? `<button class="btn ghost sm" id="attDel">🗑 ${esc(t("att_delete"))}</button>` : ""}</div>`);
  m.classList.add("wide");
  const del = $("#attDel");
  if (del) del.addEventListener("click", async () => {
    try { const upd = await API.deleteAttachment(a.id); closeModal(); renderOrder(upd, ws); }
    catch (err) { toast(errMsg(err), "err"); }
  });
}

function renderFieldsCard(o) {
  const rows = (o.field_specs || []).map((f) => {
    const v = o.fields[f.key];
    return `<div class="kvrow"><div class="k">${esc(tplField(o.template, f.key, f.label))}</div>
      <div class="v ${v ? "" : "empty"}">${v ? esc(v) : "-"}</div><div></div></div>`;
  }).join("");
  return `<div class="card pad">
    <div class="flex between center" style="margin-bottom:6px"><b>${esc(t("details"))}</b>
      <button class="btn ghost sm" id="editFields">✎ ${esc(t("edit"))}</button></div>
    <div class="kvlist" id="fieldsView">${rows || `<p class="muted">${esc(t("no_fields"))}</p>`}</div>
  </div>`;
}
function wireFieldsCard(o, ws) {
  const btn = $("#editFields"); if (!btn) return;
  btn.addEventListener("click", () => {
    const view = $("#fieldsView");
    const form = (o.field_specs || []).map((f) => fieldInput(f, o.fields[f.key], o.template)).join("");
    view.innerHTML = `<div class="row2" id="fieldsEdit">${form}</div>
      <div class="flex gap8 mt8"><button class="btn primary sm" id="saveFields">${esc(t("save"))}</button><button class="btn ghost sm" id="cancelFields">${esc(t("cancel"))}</button></div>`;
    btn.classList.add("hide");
    $("#cancelFields").addEventListener("click", () => renderOrder(o, ws));
    $("#saveFields").addEventListener("click", async () => {
      const fields = collectFields("#fieldsEdit");
      try { const upd = await API.updateOrder(o.id, { fields }); toast(t("saved"), "ok"); renderOrder(upd, ws); }
      catch (err) { toast(errMsg(err), "err"); }
    });
  });
}

function termValueHTML(tm) {
  if (!tm.value) return `<div class="value empty">${esc(t("term_not_set_value"))}</div>`;
  if (tm.type === "money") {
    const dm = displayMoney(tm.value);
    return `<div class="value mono"${dm.title ? ` title="${esc(t("currency"))}: ${esc(dm.title)}"` : ""}>${esc(dm.text)}</div>`;
  }
  return `<div class="value">${esc(tm.value)}</div>`;
}

function renderTermsCard(o) {
  const rows = o.terms.map((tm) => {
    let badge = "";
    if (tm.state === "agreed") badge = `<span class="badge ok dot">${esc(t("term_agreed"))}</span>`;
    else if (tm.value && tm.awaiting_you) badge = `<span class="badge warn dot">${esc(t("term_for_you"))}</span>`;
    else if (tm.value && tm.proposed_by_me) badge = `<span class="badge dot">${esc(t("term_waiting_other"))}</span>`;
    else if (tm.value) badge = `<span class="badge dot">${esc(t("term_proposed"))}</span>`;
    else badge = `<span class="badge dim">${esc(t("term_unset"))}</span>`;

    const actions = [];
    if (tm.value && tm.awaiting_you) actions.push(`<button class="btn ok sm" data-accept="${tm.key}">✓ ${esc(t("accept"))}</button>`);
    actions.push(`<button class="btn ghost sm" data-edit="${tm.key}">${tm.value ? esc(t("change")) : esc(t("propose"))}</button>`);

    // Only an agreed term can be due another look, and only there is the
    // question worth asking. Offering it on a proposal would be asking when to
    // review something nobody has agreed to yet.
    const review = tm.state === "agreed" ? `<label class="term-review">
      <span>${esc(t("agr_review_label"))}</span>
      <select class="select sm" data-review="${tm.key}">
        <option value="">${esc(t("agr_review_off"))}</option>
        ${[30, 90, 180, 365].map((d) =>
          `<option value="${d}" ${Number(tm.review_every_days) === d ? "selected" : ""}>${esc(t("agr_d_" + d))}</option>`).join("")}
      </select></label>` : "";

    return `<div class="term" data-term="${tm.key}">
      <div class="head"><span class="label">${esc(tplField(o.template, tm.key, tm.label))}</span>${badge}</div>
      ${termValueHTML(tm)}
      <div class="state-line">${actions.join("")}</div>
      ${review}
      <div class="editzone"></div>
    </div>`;
  }).join("");
  return `<div class="card pad">
    <div class="flex between center" style="margin-bottom:4px"><b>${esc(t("terms_title"))}</b><span class="dim" style="font-size:12px">${esc(t("two_sided"))}</span></div>
    <p class="dim" style="font-size:12px;margin-bottom:4px">${esc(t("terms_subtitle"))}</p>
    ${rows}
  </div>`;
}
function wireTermsCard(o, ws) {
  $$("[data-review]").forEach((sel) => sel.addEventListener("change", async () => {
    try {
      await API.setTermReview(o.id, sel.dataset.review, sel.value);
      toast(t("agr_review_hint"), "ok");
    } catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-accept]").forEach((b) => b.addEventListener("click", async () => {
    try { const upd = await API.acceptTerm(o.id, b.dataset.accept); toast(t("term_agreed_toast"), "ok"); renderOrder(upd, ws); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-edit]").forEach((b) => b.addEventListener("click", () => {
    const key = b.dataset.edit;
    const term = o.terms.find((x) => x.key === key);
    const zone = $(`.term[data-term="${key}"] .editzone`);
    const spec = { key, type: term.type, options: term.options, placeholder: term.placeholder };
    zone.innerHTML = `<div class="editrow">${termEditInput(spec, term.value)}
      <button class="btn primary sm" id="saveTerm">${esc(t("propose"))}</button></div>`;
    const inp = zone.querySelector("[data-tk]"); inp.focus();
    const readVal = () => {
      if (term.type === "money") {
        const amt = (inp.value || "").trim();
        const cur = (zone.querySelector("[data-tkc]") || {}).value || CCY;
        return amt ? (cur + " " + amt) : "";
      }
      return inp.value.trim();
    };
    $("#saveTerm").addEventListener("click", async () => {
      try { const upd = await API.proposeTerm(o.id, key, readVal()); toast(t("proposal_sent"), "ok"); renderOrder(upd, ws); }
      catch (err) { toast(errMsg(err), "err"); }
    });
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && spec.type !== "textarea") $("#saveTerm").click(); });
  }));
}
function termEditInput(spec, value) {
  const v = value || "";
  if (spec.type === "textarea") return `<textarea class="textarea" data-tk="${spec.key}" placeholder="${esc(spec.placeholder || "")}">${esc(v)}</textarea>`;
  if (spec.type === "select") return `<select class="select" data-tk="${spec.key}"><option value="">-</option>${(spec.options || []).map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  if (spec.type === "money") {
    const p = parseMoney(v) || { cur: CCY, amt: "" };
    return `<input class="input money-amt" data-tk="${spec.key}" type="number" step="any" placeholder="0" value="${esc(p.amt)}" />
      <select class="select money-cur" data-tkc="${spec.key}" style="max-width:96px">${CURRENCIES.map((c) => `<option value="${c.code}" ${c.code === (p.cur || CCY) ? "selected" : ""}>${esc(c.code)}</option>`).join("")}</select>`;
  }
  const type = spec.type === "number" ? "number" : (spec.type === "date" ? "date" : "text");
  return `<input class="input" data-tk="${spec.key}" type="${type}" step="any" placeholder="${esc(spec.placeholder || "")}" value="${esc(v)}" />`;
}

function eventIcon(kind) {
  return ({ order_created: "🆕", status_changed: "→", field_updated: "✎", title_changed: "✎",
    term_proposed: "✎", term_agreed: "✓", workspace_created: "🧩", partner_joined: "🤝",
    attachment_added: "📷", attachment_removed: "🗑", signature_requested: "✍️", signature_signed: "✅" }[kind]) || "•";
}
function sideWord(side) { return side === "buyer" ? t("side_business") : side === "partner" ? t("side_partner") : t("side_system"); }

function isMoneyTerm(tk, termKey) {
  const tpl = State.templates[tk]; if (!tpl) return false;
  const sp = (tpl.terms || []).find((x) => x.key === termKey);
  return !!(sp && sp.type === "money");
}
function moneyMaybe(tk, termKey, val) {
  return (val && isMoneyTerm(tk, termKey)) ? displayMoneyText(val) : val;
}
function eventText(e) {
  let meta = {}; try { meta = JSON.parse(e.meta_json || "{}"); } catch {}
  const tk = meta.tpl;
  switch (e.kind) {
    case "order_created": return { text: t("ev_order_created", { title: meta.title || "", ref: meta.ref || "" }) };
    case "status_changed": return { text: t("ev_status_changed"), diff: `${tplStage(tk, meta.from, meta.from)} → ${tplStage(tk, meta.to, meta.to)}` };
    case "field_updated": return { text: t("ev_field_updated", { field: tplField(tk, meta.field, meta.field) }), diff: `${meta.old || "-"} → ${meta.new || "-"}` };
    case "title_changed": return { text: t("ev_title_changed"), diff: `${meta.old || "-"} → ${meta.new || "-"}` };
    case "term_proposed": return { text: t("ev_term_proposed", { term: tplField(tk, meta.term, meta.term) }), diff: `${moneyMaybe(tk, meta.term, meta.old) || "-"} → ${moneyMaybe(tk, meta.term, meta.new) || "-"}` };
    case "term_agreed": return { text: t("ev_term_agreed", { term: tplField(tk, meta.term, meta.term), value: moneyMaybe(tk, meta.term, meta.value) || "" }) };
    case "workspace_created": return { text: t("ev_workspace_created", { name: meta.name || "" }) };
    case "partner_joined": return { text: t("ev_partner_joined") };
    case "attachment_added": return { text: t("ev_attachment_added", { name: meta.name || "" }) };
    case "signature_requested": return { text: t("ev_signature_requested", { level: meta.level || "" }) };
    case "signature_signed": return { text: t("ev_signature_signed", { by: meta.by || "" }) };
    case "attachment_removed": return { text: t("ev_attachment_removed", { name: meta.name || "" }) };
    case "document_filed": return { text: t("ev_document_filed"), diff: meta.title || "" };
    case "approval_started": return { text: t("ev_approval_started", { n: meta.steps || 0 }) };
    case "document_ready": return { text: t("ev_document_ready", { doc: meta.doc ? t("doc_" + meta.doc) : "" }) };
    case "approval_decided": return { text: t(meta.decision === "approved" ? "ev_approval_ok" : "ev_approval_no", { label: meta.label || "" }), diff: meta.note || "" };
    // The amount is shown exactly as it was entered: reformatting a figure
    // that appears in an audit trail would be inventing a number.
    case "payout_recorded": return { text: t(meta.status === "paid" ? "ev_payout_paid" : "ev_payout_recorded", { amount: meta.amount || "" }) };
    // `summary` is the stored description, written when the event happened and
    // in one language. It must never be the thing on screen.
    default: return { text: t("ev_generic") };
  }
}

function timelineItem(e, withOrderLink) {
  const side = e.actor_side || "system";
  const info = eventText(e);
  const diffHTML = info.diff ? `<div class="diff">${esc(info.diff)}</div>` : "";
  const link = withOrderLink && e.order_ref ? ` <a href="#/o/${e.order_id}" data-go-order="${e.order_id}" onclick="event.preventDefault()" class="mono dim">${esc(e.order_ref)}</a>` : "";
  return `<div class="tl-item">
    <div class="tl-dot ${side}">${eventIcon(e.kind)}</div>
    <div class="txt"><span class="who">${esc(e.actor_name || t("side_system"))}</span> ${esc(info.text)}${link}</div>
    <div class="when">${relTime(e.created_at)} · ${esc(sideWord(side))}${e.device || e.ip
      ? ` · <span class="tl-origin" title="${esc(t("audit_origin"))}">${esc([e.device, e.ip].filter(Boolean).join(" · "))}</span>` : ""}</div>
    ${diffHTML}
  </div>`;
}
function renderTimelineCard(o) {
  const items = o.events.slice().reverse();
  return `<div class="card pad mt16">
    <b>${esc(t("timeline_title"))}</b>
    <p class="dim" style="font-size:12px;margin-bottom:8px">${esc(t("timeline_hint"))}</p>
    <div class="timeline">${items.length ? items.map((e) => timelineItem(e, false)).join("") : `<p class="muted">${esc(t("no_events"))}</p>`}</div>
  </div>`;
}

function renderCommentsCard(o) {
  const me = State.user && State.user.name;
  const list = o.comments.map((c) => msgBubble(c.author, c.created_at, c.body, c.author !== me)).join("");
  return `<div class="card pad mt16">
    <b>${esc(t("discuss_title"))}</b>
    <p class="dim" style="font-size:12px;margin-bottom:6px">${esc(t("discuss_hint"))}</p>
    <div id="cmtList">${list || `<p class="muted" style="font-size:13px">${esc(t("no_comments"))}</p>`}</div>
    <div class="cmt-box"><input class="input" id="cmtInput" placeholder="${esc(t("comment_ph"))}" /><button class="btn primary" id="cmtSend">${esc(t("send"))}</button></div>
  </div>`;
}
function wireCommentsCard(o, ws) {
  const send = async () => {
    const inp = $("#cmtInput"); const v = inp.value.trim(); if (!v) return;
    try { const upd = await API.comment(o.id, v); renderOrder(upd, ws); }
    catch (err) { toast(errMsg(err), "err"); }
  };
  $("#cmtSend").addEventListener("click", send);
  $("#cmtInput").addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  wireTranslate($("#cmtList"));
}

/* ---- live polling on order detail -------------------------------------- */
function stopFeed() { if (State._feedTimer) { clearInterval(State._feedTimer); State._feedTimer = null; } }
function startFeed(id, snapshot) {
  let last = { ev: maxId(snapshot.events), cm: maxId(snapshot.comments), status: snapshot.status };
  State._feedTimer = setInterval(async () => {
    if (!currentRoute().startsWith("/o/")) { stopFeed(); return; }
    try {
      const f = await API.feed(id);
      if (f.last_event !== last.ev || f.last_comment !== last.cm || f.status !== last.status) {
        last = { ev: f.last_event, cm: f.last_comment, status: f.status };
        const o = await API.order(id);
        let ws; try { ws = await API.workspace(o.workspace_id); } catch {}
        renderOrder(o, ws);
        startFeed(id, o);
      }
    } catch {}
  }, 5000);
}
function maxId(arr) { return (arr && arr.length) ? Math.max.apply(null, arr.map((x) => x.id)) : 0; }

/* =========================================================================
   BOOT + ROUTER
   ======================================================================== */
async function boot() {
  const me = await API.me();
  State.user = me.user; State.org = me.org; State._verifyToken = me.verify_token || null;
  try { State.templates = await API.templates(); } catch {}
  State.builtinKeys = Object.keys(State.templates);
  try {
    State.customTemplates = await API.customTemplates();
    State.customTemplates.forEach((ct) => { State.templates[ct.key] = ct.config; });
  } catch { State.customTemplates = []; }
  try { State.pricing = await API.pricing(); } catch { State.pricing = {}; }
  try { State.sectors = await API.sectors(); } catch { State.sectors = { sectors: [], asked: true }; }
  // Opening on the company's own trades is the point of asking.
  docSector = mySectors().length ? "mine" : "all";
  try {
    const ns = await API.notifySettings();
    State.pushOn = !!ns.channels.push && ("Notification" in window) &&
                   Notification.permission === "granted";
    if (State.pushOn) registerSW();
  } catch { State.pushOn = false; }
  await refreshNotif();
  startNotifPolling();
}
function startNotifPolling() {
  if (State._notifTimer) clearInterval(State._notifTimer);
  State._notifTimer = setInterval(async () => {
    if (!State.user) return;
    await refreshNotif(); refreshBadge();
  }, 20000);
}

async function render() {
  const route = currentRoute();
  if (!(route.startsWith("/w/") || route.startsWith("/o/"))) setIndustry(null);
  if (!State.user) {
    if (State._resetToken) return viewReset(State._resetToken);
    if (route === "/register") return viewRegister();
    return viewLogin();
  }
  // Asked once, on the first sign-in that can answer it. A colleague without
  // the right to set it is not shown a question they cannot resolve.
  if (State.sectors && !State.sectors.asked && State.sectors.can_set && !State._sectorAsked) {
    State._sectorAsked = true;
    openSectorChooser(true);
  }
  if (route === "/" || route === "") return viewDashboard();
  if (route === "/assistant") return viewAssistant();
  if (route === "/store") return viewStore();
  if (route === "/templates") return viewTemplates();
  if (route === "/vehicles") return viewVehicles();
  if (route.startsWith("/passport/vehicle/")) return viewVehiclePassport(route.split("/")[3]);
  if (route.startsWith("/passport/order/")) return viewRecordPassport(route.split("/")[3]);
  if (route.startsWith("/passport/product/")) return viewProductPassport(route.split("/")[3]);
  if (route.startsWith("/passport/")) return viewPassport(route.split("/")[2]);
  if (route === "/docs") return viewDocs();
  if (route === "/money") return viewMoney();
  if (route === "/finance") return viewFinance();
  if (route === "/analytics") return viewAnalytics();
  if (route === "/network") return viewNetwork();
  if (route === "/directory") return viewDirectory();
  if (route === "/messages") return viewInbox();
  if (route === "/addons") return viewAddons();
  if (route === "/integrations") return viewIntegrations();
  if (route === "/plans") return viewPlans();
  let m;
  if ((m = route.match(/^\/w\/(\d+)/))) return viewWorkspace(+m[1]);
  if ((m = route.match(/^\/o\/(\d+)/))) return viewOrder(+m[1]);
  return viewDashboard();
}

(async function init() {
  const q = new URLSearchParams(location.search);
  let verified = false;
  const vt = q.get("verify");
  if (vt) { try { await API.verify(vt); verified = true; } catch {} }
  const rt = q.get("reset");
  if (rt) State._resetToken = rt;
  try { await boot(); }
  catch { State.user = null; }
  if (verified) toast(t("verify_done"), "ok");
  const sid = q.get("session_id"), stpl = q.get("stripe_tpl");
  if (State.user && sid && stpl) {
    try { await API.confirmStripe(stpl, sid); toast(t("toast_payment_ok"), "ok"); location.hash = "#/templates"; }
    catch (err) { toast(errMsg(err), "err"); }
  } else if (State.user && sid && q.get("plan")) {
    // Back from the card checkout. The session id is handed to the server,
    // which asks Stripe whether it was actually paid - the browser is not
    // trusted to answer that about itself.
    try {
      const r = await API.planConfirm({ session_id: sid });
      State.pricing = await API.pricing();
      toast(t(r.status === "active" ? "toast_payment_ok" : "bill_claim_sent"), "ok");
    } catch (err) { toast(errMsg(err), "err"); }
    location.hash = "#/plans";
  }
  if (vt || sid) history.replaceState({}, "", location.pathname + location.hash);
  render();
})();
