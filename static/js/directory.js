/* Profiles, portfolio, the directory and the message inbox.
 *
 * Kept out of app.js because it is a whole surface of its own: who a company
 * says it is, what it has done, and how a stranger reaches it. Loaded before
 * app.js, so every function here is available to the router.
 *
 * The rule that shapes all of it: a published card shows what its owner chose
 * to publish and nothing else. No counterparties, no order history, no
 * telephone number handed to somebody they have never worked with.
 */

/* ---- small shared pieces ------------------------------------------------ */

function postureBadge(p) {
  const cls = p === "hiring" ? "buyer" : p === "seeking" ? "partner" : "ok";
  return `<span class="badge ${cls}">${esc(t("pf_posture_" + p))}</span>`;
}

function tierBadge(tier) {
  const cls = tier === "verified" ? "ok" : tier === "established" ? "buyer" : "";
  return `<span class="badge ${cls} dot">${esc(t("pf_tier_" + tier))}</span>`;
}

/* A dialable link, or nothing. Mirrors profiles.phone_href on the server:
   people write numbers with brackets and dots, and a tel: with those in it
   fails silently on half the phones that follow it. */
function telHref(v) {
  const raw = String(v || "").trim();
  if (!raw) return "";
  const plus = raw.startsWith("+") || raw.startsWith("00");
  let digits = raw.replace(/[^0-9+]/g, "").replace(/^\++/, "");
  if (raw.startsWith("00")) digits = digits.slice(2);
  if (digits.length < 6 || digits.length > 15) return "";
  return "tel:" + (plus ? "+" : "") + digits;
}

function mailHref(v) {
  const raw = String(v || "").trim();
  return /^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$/.test(raw) && raw.length <= 254 ? "mailto:" + raw : "";
}

/* Every way to reach a company, as links that actually dial or open a
   message. A number that has to be copied by hand is a number nobody calls. */
function contactChips(c, email) {
  const out = [];
  const push = (href, icon, label) => {
    if (href) out.push(`<a class="emg-chip" href="${esc(href)}"${
      href.startsWith("http") ? ' target="_blank" rel="noopener noreferrer"' : ""
    }>${icon} ${esc(label)}</a>`);
  };
  if (c) {
    push(telHref(c.phone), "📞", c.phone || "");
    if (c.whatsapp) push("https://wa.me/" + String(c.whatsapp).replace(/[^\d]/g, ""), "🟢", c.whatsapp);
    if (c.viber) push("viber://chat?number=" + encodeURIComponent(String(c.viber).replace(/[^\d+]/g, "")), "🟣", c.viber);
    if (c.telegram) {
      const h = String(c.telegram);
      push("https://t.me/" + (h.startsWith("@") ? h.slice(1) : h.replace(/[^\w\d]/g, "")), "✈️", h);
    }
    push(mailHref(c.alt_email), "✉️", c.alt_email || "");
  }
  push(mailHref(email), "✉️", email || "");
  return out.join("");
}

/* ---- my own profile: how complete it is, and whether it is published ---- */

function standingPanel(c) {
  const pct = c.completeness || 0;
  const todo = (c.checklist || []).filter((r) => !r.ok);
  return `<div class="card pf-standing">
    <div class="flex between wrap gap8">
      <div><div class="section-title">${esc(t("pf_standing"))}</div>
        <div class="dim" style="font-size:12.5px">${esc(t("pf_standing_hint"))}</div></div>
      <div class="flex gap8">${tierBadge(c.tier || "draft")}<b>${pct}%</b></div>
    </div>
    <div class="pf-bar" role="img" aria-label="${esc(t("pf_pct", { n: pct }))}">
      <span style="width:${pct}%"></span></div>
    ${todo.length ? `<ul class="pf-todo">${todo.map((r) =>
      `<li>${esc(t("pf_need_" + r.field))}</li>`).join("")}</ul>`
      : `<p class="dim mt8" style="font-size:12.5px">${esc(t("pf_standing_done"))}</p>`}
  </div>`;
}

/* ---- the directory ------------------------------------------------------ */

const dirState = { posture: "", country: "", sector: "", q: "", page: 0 };

async function viewDirectory() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("dir_title") }]);
  let d = null, me = null;
  try { [d, me] = await Promise.all([API.directory(dirState), API.profile()]); }
  catch (err) { toast(errMsg(err), "err"); }
  if (!d) { shell(cr, `<div class="page"><div class="empty"><p>${esc(t("dir_none"))}</p></div></div>`); return; }
  const c = (me && me.company) || {};

  const pages = Math.max(1, Math.ceil(d.total / d.per_page));
  shell(cr, `<div class="page">
    <div class="page-head"><div><h1>${esc(t("dir_title"))}</h1>
      <div class="sub">${esc(t("dir_sub"))}</div></div>
      <div class="flex gap8 wrap">
        <button class="btn ghost" id="dirMine">${esc(t("pf_my_profile"))}</button>
        <button class="btn ghost" id="dirReq">${esc(t("cr_title"))}</button>
      </div></div>

    ${c.id ? `<div class="grid2">${standingPanel(c)}
      <div class="card">
        <div class="section-title">${esc(t("pf_publish"))}</div>
        <p class="dim" style="font-size:12.5px;margin:6px 0 12px">${esc(t("pf_publish_hint"))}</p>
        <label class="check"><input type="checkbox" id="dirListed"${c.listed ? " checked" : ""}${
          c.may_list ? "" : " disabled"}><span>${esc(t("pf_listed"))}</span></label>
        ${c.may_list ? "" : `<p class="dim mt8" style="font-size:12px">${esc(t("pf_publish_blocked"))}</p>`}
      </div></div>` : ""}

    <div class="card mt16">
      <div class="dir-filters">
        <label class="field"><span>${esc(t("dir_looking"))}</span>
          <select class="input" id="dirPosture">
            <option value="">${esc(t("dir_any"))}</option>
            ${(d.postures || []).map((p) =>
              `<option value="${p}"${dirState.posture === p ? " selected" : ""}>${esc(t("pf_posture_" + p))}</option>`).join("")}
          </select></label>
        <label class="field"><span>${esc(t("dir_sector"))}</span>
          <select class="input" id="dirSector">
            <option value="">${esc(t("dir_any"))}</option>
            ${(d.sectors || []).map((s) =>
              `<option value="${s}"${dirState.sector === s ? " selected" : ""}>${esc(tplLabel(s))}</option>`).join("")}
          </select></label>
        <label class="field"><span>${esc(t("dir_country"))}</span>
          <input class="input" id="dirCountry" maxlength="2" placeholder="BG" value="${esc(dirState.country)}"></label>
        <label class="field"><span>${esc(t("dir_search"))}</span>
          <input class="input" id="dirQ" maxlength="80" placeholder="${esc(t("dir_search_ph"))}" value="${esc(dirState.q)}"></label>
        <button class="btn primary" id="dirGo">${esc(t("dir_apply"))}</button>
      </div>
    </div>

    ${d.items.length ? `<div class="dir-grid mt16">${d.items.map(dirCard).join("")}</div>
      ${pages > 1 ? `<div class="flex gap8 center mt16">
        <button class="btn ghost sm" id="dirPrev"${dirState.page <= 0 ? " disabled" : ""}>‹</button>
        <span class="dim">${esc(t("dir_page", { n: dirState.page + 1, total: pages }))}</span>
        <button class="btn ghost sm" id="dirNext"${dirState.page + 1 >= pages ? " disabled" : ""}>›</button>
      </div>` : ""}`
    : `<div class="empty mt16"><p>${esc(t("dir_empty"))}</p></div>`}
  </div>`);

  const apply = () => {
    dirState.posture = $("#dirPosture").value;
    dirState.sector = $("#dirSector").value;
    dirState.country = $("#dirCountry").value.trim().toUpperCase();
    dirState.q = $("#dirQ").value.trim();
    dirState.page = 0;
    viewDirectory();
  };
  $("#dirGo").addEventListener("click", apply);
  $("#dirQ").addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
  const prev = $("#dirPrev"), next = $("#dirNext");
  if (prev) prev.addEventListener("click", () => { dirState.page--; viewDirectory(); });
  if (next) next.addEventListener("click", () => { dirState.page++; viewDirectory(); });
  $("#dirMine").addEventListener("click", () => openProfile("company"));
  $("#dirReq").addEventListener("click", openContactRequests);
  const sw = $("#dirListed");
  if (sw) sw.addEventListener("change", async () => {
    sw.disabled = true;
    try { await API.profileCompanySet({ listed: sw.checked }); toast(t("saved"), "ok"); viewDirectory(); }
    catch (err) { toast(errMsg(err), "err"); viewDirectory(); }
  });
  $$("[data-dir-org]").forEach((b) =>
    b.addEventListener("click", () => openCompanyCard(+b.dataset.dirOrg)));
}

function dirCard(c) {
  return `<button class="dir-card" data-dir-org="${c.id}">
    <div class="dir-top">${avatarBox(c.logo, c.name, 48)}
      <div class="dir-id"><div class="dir-name">${esc(c.name)}</div>
        <div class="dir-meta">${esc(countryName(c.country) || c.country || "")}${
          c.size_band ? " · " + esc(t("pf_size_" + c.size_band)) : ""}</div></div></div>
    <div class="dir-badges">${postureBadge(c.posture)}${c.verified
      ? `<span class="badge ok dot">${esc(t("account_verified"))}</span>` : ""}${
      c.known ? `<span class="badge">${esc(t("dir_known"))}</span>` : ""}</div>
    <div class="dir-line">${esc(c.headline || "")}</div>
    <div class="dir-tags">${(c.sectors || []).slice(0, 4).map((s) =>
      `<span class="doc-tag">${sectorChip(s)}</span>`).join("")}</div>
    <div class="dir-foot dim">${esc(t("pf_works_n", { n: c.portfolio_count || 0 }))}</div>
  </button>`;
}

/* ---- one company's card ------------------------------------------------- */

async function openCompanyCard(orgId) {
  let c;
  try { c = await API.directoryCard(orgId); } catch (err) { return toast(errMsg(err), "err"); }
  const reach = c.reach || {};
  const links = (c.links || []).filter((l) => l.url);

  const contact = c.known
    ? `<div class="emg-wrap">${contactChips(c.contacts, c.website ? "" : "")}</div>`
    : reach.withheld || reach.can_request
      ? `<p class="dim" style="font-size:12.5px">${esc(t("dir_reach_hidden"))}</p>
         <button class="btn primary sm mt8" id="ccAsk">${esc(t("dir_ask_contact"))}</button>`
      : `<p class="dim" style="font-size:12.5px">${esc(t("dir_reach_none"))}</p>`;

  openModal(c.name, `
    <div class="pf-head">
      ${avatarBox(c.logo, c.name, 76)}
      <div>
        <div class="flex gap8 wrap">${postureBadge(c.posture)}${tierBadge(c.tier)}${
          c.verified ? `<span class="badge ok dot">${esc(t("account_verified"))}</span>` : ""}</div>
        <div class="dim mt8" style="font-size:12.5px">
          ${esc(countryName(c.country) || c.country || "")}${
            c.founded ? " · " + esc(t("pf_since", { y: c.founded })) : ""}${
            c.size_band ? " · " + esc(t("pf_size_" + c.size_band)) : ""}${
            c.reg_number ? " · " + esc(c.reg_number) : ""}</div>
      </div>
    </div>
    ${c.headline ? `<p class="pf-headline">${esc(c.headline)}</p>` : ""}
    ${c.about ? `<p class="pf-about">${esc(c.about)}</p>` : ""}
    ${(c.sectors || []).length ? `<div class="pf-sectors mt12">${(c.sectors || []).map((s) =>
      `<span class="doc-tag">${sectorChip(s)}</span>`).join("")}</div>` : ""}
    ${(c.areas || []).length ? `<div class="dim mt8" style="font-size:12.5px">${
      esc(t("pf_areas"))}: ${esc((c.areas || []).join(", "))}</div>` : ""}

    <div class="section-title" style="margin:18px 0 8px">${esc(t("prof_contacts"))}</div>
    ${contact}
    ${c.website || links.length ? `<div class="emg-wrap mt8">${
      c.website ? `<a class="emg-chip" href="${esc(c.website)}" target="_blank" rel="noopener noreferrer">🌐 ${esc(c.website)}</a>` : ""}${
      links.map((l) => `<a class="emg-chip" href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">🔗 ${
        esc(l.label || l.url)}</a>`).join("")}</div>` : ""}

    <div class="section-title" style="margin:18px 0 8px">${esc(t("pf_portfolio"))}</div>
    ${(c.portfolio || []).length
      ? `<div class="pf-works">${c.portfolio.map(workRow).join("")}</div>`
      : `<p class="dim" style="font-size:12.5px">${esc(t("pf_works_none"))}</p>`}

    ${(c.people || []).length ? `<div class="section-title" style="margin:18px 0 8px">${
      esc(t("pf_people"))}</div><div class="pf-people">${c.people.map((p) =>
      `<div class="pf-person">${avatarBox(p.avatar, p.name, 34)}
        <div><b>${esc(p.name)}</b><div class="dim" style="font-size:12px">${esc(p.title || "")}</div></div></div>`).join("")}</div>` : ""}
  `, `<button class="btn ghost" data-close>${esc(t("close"))}</button>`);

  const ask = $("#ccAsk");
  if (ask) ask.addEventListener("click", () => openContactDialog(c));
}

function workRow(w) {
  const meta = [w.role, w.place, w.year, w.value_band].filter(Boolean).join(" · ");
  return `<div class="pf-work">
    <div class="flex between wrap gap8">
      <b>${esc(w.title)}</b>
      <span class="doc-tag">${esc(t("pf_kind_" + w.kind))}</span></div>
    ${meta ? `<div class="dim" style="font-size:12px">${esc(meta)}</div>` : ""}
    ${w.client ? `<div class="dim" style="font-size:12px">${esc(t("pf_client"))}: ${esc(w.client)}</div>` : ""}
    ${w.summary ? `<p class="pf-work-sum">${esc(w.summary)}</p>` : ""}
    ${w.url ? `<a class="emg-chip" href="${esc(w.url)}" target="_blank" rel="noopener noreferrer">🔗 ${esc(t("pf_work_link"))}</a>` : ""}
    ${(w.docs || []).length ? `<div class="emg-wrap mt8">${w.docs.map((d) =>
      `<a class="emg-chip" href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">📎 ${
        esc(d.title || d.name)}</a>`).join("")}</div>` : ""}
  </div>`;
}

/* ---- asking a company to make contact ----------------------------------- */

function openContactDialog(c) {
  const me = (State.user && State.user.email) || "";
  openModal(t("dir_ask_contact"), `
    <p class="dim" style="font-size:12.5px">${esc(t("dir_ask_hint", { name: c.name }))}</p>
    <label class="field mt12"><span>${esc(t("prof_phone"))}</span>
      <input class="input" id="crPhone" maxlength="40" placeholder="+359 ..."></label>
    <label class="field"><span>${esc(t("prof_email"))}</span>
      <input class="input" id="crEmail" maxlength="254" value="${esc(me)}"></label>
    <label class="field"><span>${esc(t("dir_ask_note"))}</span>
      <textarea class="input" id="crNote" rows="4" maxlength="600" placeholder="${esc(t("dir_ask_note_ph"))}"></textarea></label>
  `, `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
      <button class="btn primary" id="crSend">${esc(t("dir_ask_send"))}</button>`);
  $("#crSend").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await API.directoryContact(c.id, {
        phone: $("#crPhone").value, email: $("#crEmail").value, note: $("#crNote").value,
      });
      closeModal();
      toast(r.delivered ? t("dir_ask_sent") : t("dir_ask_stored"), "ok");
    } catch (err) { toast(errMsg(err), "err"); e.target.disabled = false; }
  });
}

async function openContactRequests() {
  let d;
  try { d = await API.contactRequests(); } catch (err) { return toast(errMsg(err), "err"); }
  const rows = (d.items || []).map((r) => `
    <div class="pf-work">
      <div class="flex between wrap gap8"><b>${esc(r.from || t("cr_unknown"))}</b>
        <span class="dim" style="font-size:12px">${esc(relTime(r.at))}</span></div>
      <div class="emg-wrap mt8">
        ${r.phone_href ? `<a class="emg-chip" href="${esc(r.phone_href)}">📞 ${esc(r.phone)}</a>` : ""}
        ${r.email_href ? `<a class="emg-chip" href="${esc(r.email_href)}">✉️ ${esc(r.email)}</a>` : ""}
      </div>
      ${r.note ? `<p class="pf-work-sum">${esc(r.note)}</p>` : ""}
      ${r.sent ? "" : `<div class="dim mt8" style="font-size:12px">${esc(t("cr_not_mailed"))}</div>`}
    </div>`).join("");
  openModal(t("cr_title"), rows
    ? `<p class="dim" style="font-size:12.5px;margin-bottom:12px">${esc(t("cr_hint"))}</p>
       <div class="pf-works">${rows}</div>`
    : `<div class="empty"><p>${esc(t("cr_none"))}</p></div>`,
    `<button class="btn ghost" data-close>${esc(t("close"))}</button>`);
}

/* ---- portfolio editor --------------------------------------------------- */

async function openPortfolio() {
  let d;
  try { d = await API.portfolio(); } catch (err) { return toast(errMsg(err), "err"); }
  const rows = (d.items || []).map((w) => `
    <div class="pf-work" data-work="${w.id}">
      ${workRow(w)}
      <div class="flex gap8 wrap mt8">
        <button class="btn ghost sm" data-work-edit="${w.id}">${esc(t("edit"))}</button>
        <button class="btn ghost sm" data-work-docs="${w.id}">${esc(t("pf_attach"))}</button>
        <button class="btn ghost sm" data-work-hide="${w.id}">${esc(w.visible ? t("pf_hide") : t("pf_show"))}</button>
        <button class="btn ghost sm danger" data-work-del="${w.id}">${esc(t("delete"))}</button>
      </div>
    </div>`).join("");
  openModal(t("pf_portfolio"), `
    <p class="dim" style="font-size:12.5px">${esc(t("pf_portfolio_hint"))}</p>
    <button class="btn primary sm mt12" id="pfWorkNew">+ ${esc(t("pf_work_add"))}</button>
    <div class="pf-works mt12">${rows || `<div class="empty"><p>${esc(t("pf_works_none"))}</p></div>`}</div>
  `, `<button class="btn ghost" data-close>${esc(t("close"))}</button>`);

  $("#pfWorkNew").addEventListener("click", () => openWorkDialog(null, d.kinds));
  $$("[data-work-edit]").forEach((b) => b.addEventListener("click", () =>
    openWorkDialog((d.items || []).find((w) => w.id === +b.dataset.workEdit), d.kinds)));
  $$("[data-work-docs]").forEach((b) => b.addEventListener("click", () =>
    openAttachDialog((d.items || []).find((w) => w.id === +b.dataset.workDocs))));
  $$("[data-work-hide]").forEach((b) => b.addEventListener("click", async () => {
    const w = (d.items || []).find((x) => x.id === +b.dataset.workHide);
    try { await API.portfolioSet(w.id, { visible: !w.visible }); openPortfolio(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
  $$("[data-work-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(t("pf_work_del_sure"))) return;
    try { await API.portfolioDel(b.dataset.workDel); openPortfolio(); }
    catch (err) { toast(errMsg(err), "err"); }
  }));
}

function openWorkDialog(w, kinds) {
  const v = w || { kind: "project" };
  openModal(w ? t("pf_work_edit") : t("pf_work_add"), `
    <label class="field"><span>${esc(t("pf_work_title"))}</span>
      <input class="input" id="wkTitle" maxlength="160" value="${esc(v.title || "")}"></label>
    <label class="field"><span>${esc(t("pf_work_kind"))}</span>
      <select class="input" id="wkKind">${(kinds || []).map((k) =>
        `<option value="${k}"${v.kind === k ? " selected" : ""}>${esc(t("pf_kind_" + k))}</option>`).join("")}</select></label>
    <label class="field"><span>${esc(t("pf_work_sum"))}</span>
      <textarea class="input" id="wkSum" rows="4" maxlength="1200" placeholder="${esc(t("pf_work_sum_ph"))}">${esc(v.summary || "")}</textarea></label>
    <div class="grid2">
      <label class="field"><span>${esc(t("pf_work_role"))}</span>
        <input class="input" id="wkRole" maxlength="120" value="${esc(v.role || "")}"></label>
      <label class="field"><span>${esc(t("pf_work_place"))}</span>
        <input class="input" id="wkPlace" maxlength="120" value="${esc(v.place || "")}"></label>
      <label class="field"><span>${esc(t("pf_work_year"))}</span>
        <input class="input" id="wkYear" maxlength="4" inputmode="numeric" value="${esc(v.year || "")}"></label>
      <label class="field"><span>${esc(t("pf_work_band"))}</span>
        <input class="input" id="wkBand" maxlength="24" placeholder="${esc(t("pf_work_band_ph"))}" value="${esc(v.value_band || "")}"></label>
    </div>
    <label class="field"><span>${esc(t("pf_client"))}</span>
      <input class="input" id="wkClient" maxlength="120" value="${esc(v.client || "")}"></label>
    <p class="dim" style="font-size:12px;margin:-4px 0 12px">${esc(t("pf_client_hint"))}</p>
    <label class="field"><span>${esc(t("pf_work_url"))}</span>
      <input class="input" id="wkUrl" maxlength="300" placeholder="https://" value="${esc(v.url || "")}"></label>
  `, `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
      <button class="btn primary" id="wkSave">${esc(t("save"))}</button>`);
  $("#wkSave").addEventListener("click", async (e) => {
    e.target.disabled = true;
    const d = {
      title: $("#wkTitle").value, kind: $("#wkKind").value, summary: $("#wkSum").value,
      role: $("#wkRole").value, place: $("#wkPlace").value, year: $("#wkYear").value,
      value_band: $("#wkBand").value, client: $("#wkClient").value, url: $("#wkUrl").value,
    };
    try {
      if (w) await API.portfolioSet(w.id, d); else await API.portfolioAdd(d);
      closeModal(); toast(t("saved"), "ok"); openPortfolio();
    } catch (err) { toast(errMsg(err), "err"); e.target.disabled = false; }
  });
}

/* Evidence comes off the document shelf: filed once, with its checksum and its
   date, and reused wherever it belongs. Nothing is uploaded twice. */
async function openAttachDialog(w) {
  let files = { files: [] };
  try { files = await API.files(); } catch (err) { return toast(errMsg(err), "err"); }
  const own = (files.files || []).filter((f) => f.mine);
  const on = new Set((w.docs || []).map((d) => d.id));
  openModal(t("pf_attach"), own.length ? `
    <p class="dim" style="font-size:12.5px">${esc(t("pf_attach_hint"))}</p>
    <div class="pf-picklist mt12">${own.map((f) => `
      <label class="pf-pick"><input type="checkbox" value="${f.id}"${on.has(f.id) ? " checked" : ""}>
        <span><b>${esc(f.title || f.name)}</b>
        <span class="dim" style="font-size:12px">${esc(f.name)}</span></span></label>`).join("")}</div>
  ` : `<div class="empty"><p>${esc(t("pf_attach_none"))}</p></div>`,
    `<button class="btn ghost" data-close>${esc(t("cancel"))}</button>
     ${own.length ? `<button class="btn primary" id="pfAttSave">${esc(t("save"))}</button>` : ""}`);
  const save = $("#pfAttSave");
  if (save) save.addEventListener("click", async (e) => {
    e.target.disabled = true;
    const want = $$(".pf-picklist input:checked").map((i) => +i.value);
    try {
      for (const id of Array.from(on)) if (want.indexOf(id) === -1) await API.portfolioDetach(w.id, id);
      const add = want.filter((id) => !on.has(id));
      if (add.length) await API.portfolioAttach(w.id, add);
      closeModal(); toast(t("saved"), "ok"); openPortfolio();
    } catch (err) { toast(errMsg(err), "err"); e.target.disabled = false; }
  });
}

/* ---- the message inbox -------------------------------------------------- */

async function viewInbox() {
  const cr = crumb([{ label: t("home"), href: "/" }, { label: t("inbox_title") }]);
  let d;
  try { d = await API.inbox(); } catch (err) { toast(errMsg(err), "err"); }
  if (!d) { shell(cr, `<div class="page"><div class="empty"><p>${esc(t("inbox_none"))}</p></div></div>`); return; }

  shell(cr, `<div class="page">
    <div class="page-head"><div><h1>${esc(t("inbox_title"))}</h1>
      <div class="sub">${esc(t("inbox_sub"))}</div></div></div>
    ${d.items.length ? `<div class="inbox">${d.items.map((r) => `
      <button class="inbox-row" data-ws="${r.workspace_id}">
        ${avatarBox(r.logo, r.org, 40)}
        <div class="inbox-mid">
          <div class="flex between gap8"><b>${esc(r.org)}</b>
            <span class="dim" style="font-size:12px">${esc(r.at ? relTime(r.at) : "")}</span></div>
          <div class="dim inbox-ws">${r.pending
            ? esc(t("inbox_pending")) : esc(r.workspace)}</div>
          <div class="inbox-last">${r.last
            ? (r.last.mine ? `<span class="dim">${esc(t("inbox_you"))}: </span>` : "") + esc(r.last.body)
            : `<span class="dim">${esc(t("inbox_empty_thread"))}</span>`}</div>
        </div>
        ${r.unread ? `<span class="nav-count">${r.unread > 9 ? "9+" : r.unread}</span>` : ""}
      </button>`).join("")}</div>`
    : `<div class="empty"><p>${esc(t("inbox_empty"))}</p></div>`}
  </div>`);

  $$("[data-ws]").forEach((b) => b.addEventListener("click", () => go("/w/" + b.dataset.ws)));
}

/* ---- add-ons: the depth a trade needs ----------------------------------- *
 * The base templates are deliberately thin, which is what makes the first
 * record easy to open. Everything a particular trade actually needs - acting
 * under образец 19, an MRN, a returns window - is switched on here, per
 * template, by the company that needs it.
 */

const ADDON_ICON = {
  commerce: "🛒", logistics: "🚚", construction: "🏗", vehicles: "🚗",
  finance: "💰", quality: "✅", legal: "⚖", people: "👷",
};

async function viewAddons() {
  const cr = crumb([{ label: t("home"), href: "/" },
                    { label: t("nav_templates"), href: "/templates" },
                    { label: t("ad_title") }]);
  let d;
  try { d = await API.addons(); } catch (err) { toast(errMsg(err), "err"); }
  if (!d) { shell(cr, `<div class="page"><div class="empty"><p>${esc(t("ad_none"))}</p></div></div>`); return; }

  const keys = Object.keys(d.templates);
  if (!addonTpl || keys.indexOf(addonTpl) === -1) addonTpl = keys[0];
  const cfg = d.templates[addonTpl];
  const on = new Set(cfg.on);

  shell(cr, `<div class="page">
    <div class="page-head"><div><h1>${esc(t("ad_title"))}</h1>
      <div class="sub">${esc(t("ad_sub"))}</div></div></div>

    <div class="card">
      <label class="field" style="margin:0"><span>${esc(t("ad_for"))}</span>
        <select class="input" id="adTpl">${keys.map((k) =>
          `<option value="${k}"${k === addonTpl ? " selected" : ""}>${esc(tplLabel(k, d.templates[k].label))}</option>`).join("")}</select></label>
      <p class="dim mt8" style="font-size:12.5px">${esc(t("ad_scope_hint"))}</p>
    </div>

    ${cfg.groups.map((g) => `<div class="card mt16">
      <div class="section-title">${ADDON_ICON[g.group] || "◆"} ${esc(t("ad_g_" + g.group))}</div>
      <div class="ad-grid mt12">${g.items.map((a) => `
        <label class="ad-card${a.on ? " on" : ""}${a.mine ? " mine" : ""}">
          <input type="checkbox" data-addon="${a.key}"${a.on ? " checked" : ""}>
          <div class="ad-body">
            <div class="ad-name">${esc(addonLabel(a.key, a.label))}${
              a.mine ? `<span class="ad-mine" title="${esc(t("ad_your_trade"))}">◆</span>` : ""}</div>
            <div class="ad-tag">${esc(addonTagline(a.key, a.tagline))}</div>
            <div class="ad-meta">${[
              a.fields ? t("ad_n_fields", { n: a.fields }) : "",
              a.terms ? t("ad_n_terms", { n: a.terms }) : "",
              a.metrics ? t("ad_n_metrics", { n: a.metrics }) : "",
            ].filter(Boolean).map(esc).join(" · ")}</div>
            ${a.stages.length ? `<div class="ad-stage">${esc(t("ad_adds_stage"))}: ${
              a.stages.map((s) => esc(tplStage(addonTpl, s.key, s.label))).join(", ")}</div>` : ""}
          </div>
        </label>`).join("")}</div>
    </div>`).join("")}

    <div class="flex gap8 wrap mt16">
      <button class="btn primary" id="adSave">${esc(t("save"))}</button>
      <button class="btn ghost" id="adPreview">${esc(t("ad_preview"))}</button>
      <span class="dim" id="adCount" style="align-self:center">${
        esc(t("ad_on_now", { n: on.size }))}</span>
    </div>
  </div>`);

  $("#adTpl").addEventListener("change", (e) => { addonTpl = e.target.value; viewAddons(); });
  const picked = () => $$("[data-addon]:checked").map((i) => i.dataset.addon);
  $$("[data-addon]").forEach((i) => i.addEventListener("change", () => {
    i.closest(".ad-card").classList.toggle("on", i.checked);
    $("#adCount").textContent = t("ad_on_now", { n: picked().length });
  }));
  $("#adSave").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      await API.addonsSet(addonTpl, picked());
      toast(t("saved"), "ok");
      viewAddons();
    } catch (err) { toast(errMsg(err), "err"); e.target.disabled = false; }
  });
  $("#adPreview").addEventListener("click", () => openAddonPreview(addonTpl, picked()));
}

let addonTpl = "";

/* What the form and the pipeline will actually look like once this is saved.
   Switching on five add-ons blind and discovering a form of forty fields is
   how a good feature becomes one nobody trusts. */
async function openAddonPreview(tplKey, picked) {
  let r;
  try { r = await API.addonsPreview(tplKey, picked); }
  catch (err) { return toast(errMsg(err), "err"); }
  const row = (f) => `<tr><td>${esc(tplField(tplKey, f.key, f.label))}</td>
    <td class="dim">${esc(t("ad_type_" + f.type))}</td>
    <td class="dim">${f.from ? esc(addonLabel(f.from)) : esc(t("ad_base"))}</td></tr>`;
  openModal(t("ad_preview"), `
    <div class="section-title">${esc(t("ad_pipeline"))}</div>
    <div class="ad-pipe mt8">${r.stages.map((s) =>
      `<span class="ad-step${s.from ? " new" : ""}">${esc(tplStage(tplKey, s.key, s.label))}</span>`).join("")}</div>
    <div class="section-title" style="margin:18px 0 8px">${esc(t("ad_fields"))} (${r.fields.length})</div>
    <div class="ad-tablewrap"><table class="ad-table">${r.fields.map(row).join("")}</table></div>
    <div class="section-title" style="margin:18px 0 8px">${esc(t("ad_terms"))} (${r.terms.length})</div>
    <div class="ad-tablewrap"><table class="ad-table">${r.terms.map(row).join("")}</table></div>
  `, `<button class="btn ghost" data-close>${esc(t("close"))}</button>`);
}

/* ---- figures the add-ons make measurable -------------------------------- *
 * Only what the company actually switched on, grouped by relationship: a
 * return rate and a certification cycle are not comparable and do not belong
 * in one row. Every figure carries the number of records behind it, and a
 * figure the engine refused to compute says so rather than showing a zero.
 */

function tradeSection(d) {
  if (!d || d.none_on) {
    return `<div class="section-title">${esc(t("tf_title"))}</div>
      <div class="card pad"><p class="dim" style="font-size:12.5px">${esc(t("tf_none_on"))}</p>
        <a class="btn ghost sm mt8" href="#/addons">${esc(t("ad_title"))}</a></div>`;
  }
  if (!d.groups || !d.groups.length) {
    return `<div class="section-title">${esc(t("tf_title"))}</div>
      <div class="card pad"><p class="dim" style="font-size:12.5px">${esc(t("tf_empty"))}</p></div>`;
  }
  return `<div class="section-title">${esc(t("tf_title"))}</div>
    <p class="dim" style="font-size:12.5px;margin:-6px 0 10px">${
      esc(t("tf_hint", { n: d.min_sample }))}</p>
    ${d.groups.map((g) => `<div class="card pad mt12">
      <div class="flex between wrap gap8">
        <b>${esc(tplLabel(g.template, g.label))}</b>
        <span class="dim" style="font-size:12px">${esc(t("tf_records", { n: g.records }))}</span>
      </div>
      <div class="tf-grid mt12">${g.metrics.map(tradeTile).join("")}</div>
    </div>`).join("")}`;
}

function tradeTile(m) {
  const known = m.value !== null && m.value !== undefined;
  // Same convention as the rest of the analytics screen: hand the money module
  // a canonical "EUR 0.00" and let it render in the reader's own currency.
  const shown = !known ? "—"
    : m.unit === "money" ? displayMoneyText("EUR " + Number(m.value).toFixed(2))
    : m.unit === "%" ? m.value + "%"
    : m.unit === "days" ? t("tf_days", { n: m.value })
    : String(m.value);
  return `<div class="tf-tile${known ? "" : " thin"}">
    <div class="tf-val">${esc(shown)}</div>
    <div class="tf-lab">${esc(tplMetric(m.key, m.label))}</div>
    <div class="tf-meta">${esc(addonLabel(m.addon))} · ${
      known ? esc(t("tf_n", { n: m.n })) : esc(t("tf_too_few", { n: m.n }))}</div>
  </div>`;
}
