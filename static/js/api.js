// Thin API client. Every call returns parsed JSON or throws {message}.
const API = (() => {
  let CSRF = null;

  async function req(method, url, data) {
    const opts = { method, headers: {}, credentials: "same-origin" };
    if (CSRF) opts.headers["X-CSRF"] = CSRF;
    if (data !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(data);
    }
    const res = await fetch(url, opts);
    let payload = null;
    const text = await res.text();
    if (text) {
      try { payload = JSON.parse(text); } catch { payload = { error: text }; }
    }
    if (payload && payload.csrf) CSRF = payload.csrf;
    if (!res.ok) {
      const err = new Error((payload && payload.error) || ("Error " + res.status));
      err.status = res.status;
      err.code = payload && payload.code;
      err.info = (payload && payload.info) || {};
      throw err;
    }
    return payload;
  }

  return {
    get: (u) => req("GET", u),
    post: (u, d) => req("POST", u, d || {}),
    patch: (u, d) => req("PATCH", u, d || {}),

    // auth
    me: () => req("GET", "/api/me"),
    register: (d) => req("POST", "/api/auth/register", d),
    login: (d) => req("POST", "/api/auth/login", d),
    logout: () => req("POST", "/api/auth/logout", {}),
    verify: (token) => req("POST", `/api/auth/verify/${token}`, {}),

    // templates
    templates: () => req("GET", "/api/templates"),
    captcha: () => req("GET", "/api/captcha"),
    pricing: () => req("GET", "/api/pricing"),
    customTemplates: () => req("GET", "/api/custom-templates"),
    createCustomTemplate: (d) => req("POST", "/api/custom-templates", d),
    checkoutTemplate: (id, d) => req("POST", `/api/custom-templates/${id}/checkout`, d),
    confirmStripe: (id, session_id) => req("POST", `/api/custom-templates/${id}/confirm-stripe`, { session_id }),
    confirmInvoice: (id) => req("POST", `/api/custom-templates/${id}/confirm-invoice`, {}),
    deleteCustomTemplate: (id) => req("DELETE", `/api/custom-templates/${id}`),

    // assistant
    assistantStatus: () => req("GET", "/api/assistant/status"),
    assistantKey: (cfg) => req("POST", "/api/assistant/key", cfg),
    assistantAsk: (message, history) => req("POST", "/api/assistant", { message, history: history || [] }),

    // store / fulfillment
    storeSummary: () => req("GET", "/api/store/summary"),
    storeProducts: () => req("GET", "/api/store/products"),
    storeAddProduct: (d) => req("POST", "/api/store/products", d),
    storeAddKeys: (id, codes) => req("POST", `/api/store/products/${id}/keys`, { codes }),
    storeDelProduct: (id) => req("DELETE", `/api/store/products/${id}`),
    storeOrders: (status) => req("GET", `/api/store/orders${status ? "?status=" + status : ""}`),
    storeCreateOrder: (d) => req("POST", "/api/store/orders", d),
    storeFulfill: (id) => req("POST", `/api/store/orders/${id}/fulfill`, {}),

    // productivity + communication
    pending: () => req("GET", "/api/pending"),
    wsMessages: (id) => req("GET", `/api/workspaces/${id}/messages`),
    wsMessagePost: (id, body) => req("POST", `/api/workspaces/${id}/messages`, { body }),

    // inbound integration (API keys / hosted form)
    integrationKeys: () => req("GET", "/api/integration/keys"),
    integrationCreateKey: (d) => req("POST", "/api/integration/keys", d),
    integrationRevoke: (id) => req("DELETE", `/api/integration/keys/${id}`),
    webhooks: () => req("GET", "/api/integration/webhooks"),
    webhookCreate: (d) => req("POST", "/api/integration/webhooks", d),
    webhookDelete: (id) => req("DELETE", `/api/integration/webhooks/${id}`),
    webhookTest: (id) => req("POST", `/api/integration/webhooks/${id}/test`, {}),
    planCheckout: (plan) => req("POST", "/api/plans/checkout", { plan }),
    entitlements: () => req("GET", "/api/entitlements"),
    buyFeature: (feature, kind) =>
      req("POST", "/api/features/checkout", { feature, kind }),
    // Coming back from checkout with a session id proves the payment; without
    // one this records a claim for the operator to approve, and grants nothing.
    planConfirm: (d) => req("POST", "/api/plans/confirm", d || {}),
    billingClaims: (status) => req("GET", "/api/admin/billing?status=" + (status || "pending")),
    billingDecide: (id, decision, note) =>
      req("POST", `/api/admin/billing/${id}/${decision}`, { note: note || "" }),
    // Which trades this company works in, and the profile cards behind them.
    sectors: () => req("GET", "/api/org/sectors"),
    sectorsSet: (sectors) => req("PUT", "/api/org/sectors", { sectors }),
    profile: () => req("GET", "/api/profile"),
    profileSet: (d) => req("PUT", "/api/profile", d),
    profileCompanySet: (d) => req("PUT", "/api/profile/company", d),
    profileOf: (uid) => req("GET", `/api/profile/${uid}`),
    // What a company has done, and the evidence hung on it. Files come from
    // the document shelf, filed once and referenced here.
    portfolio: (orgId) => req("GET", "/api/portfolio" + (orgId ? `?org_id=${orgId}` : "")),
    portfolioAdd: (d) => req("POST", "/api/portfolio", d),
    portfolioSet: (id, d) => req("PATCH", `/api/portfolio/${id}`, d),
    portfolioDel: (id) => req("DELETE", `/api/portfolio/${id}`),
    portfolioAttach: (id, docIds) => req("POST", `/api/portfolio/${id}/docs`, { doc_ids: docIds }),
    portfolioDetach: (id, docId) => req("DELETE", `/api/portfolio/${id}/docs/${docId}`),
    // The published profiles. Filters are all optional.
    directory: (q) => req("GET", "/api/directory?" + new URLSearchParams(q || {}).toString()),
    directoryCard: (orgId) => req("GET", `/api/directory/${orgId}`),
    directoryContact: (orgId, d) => req("POST", `/api/directory/${orgId}/contact`, d),
    contactRequests: () => req("GET", "/api/contact-requests"),
    inbox: () => req("GET", "/api/messages"),
    // Depth per relationship template: what a trade actually needs, switched
    // on by the company that needs it.
    addons: (tpl) => req("GET", "/api/addons" + (tpl ? "?template=" + encodeURIComponent(tpl) : "")),
    addonsSet: (template, list) => req("PUT", "/api/addons", { template, addons: list }),
    addonsPreview: (template, list) => req("POST", "/api/addons/preview", { template, addons: list }),
    // Figures the switched-on add-ons make measurable. Fetched apart from
    // /api/analytics so a company with no add-ons still gets the rest.
    addonMetrics: (days) => req("GET", "/api/analytics/metrics?days=" + (days || 90)),
    profilePhotoClear: (of) => req("DELETE", `/api/profile/photo?of=${of || "person"}`),
    // Multipart, so it cannot go through req(): the CSRF token stays in here
    // rather than being handed out to callers.
    profilePhoto: async (of, file) => {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`/api/profile/photo?of=${of || "person"}`,
        { method: "POST", body: fd, credentials: "same-origin", headers: CSRF ? { "X-CSRF": CSRF } : {} });
      let payload = null;
      const text = await res.text();
      if (text) { try { payload = JSON.parse(text); } catch { payload = { error: text }; } }
      if (!res.ok) {
        const err = new Error((payload && payload.error) || ("Error " + res.status));
        err.status = res.status; err.code = payload && payload.code; err.info = (payload && payload.info) || {};
        throw err;
      }
      return payload;
    },
    compliance: () => req("GET", "/api/compliance"),
    docsEntitlement: () => req("GET", "/api/docs/entitlement"),
    forgotPassword: (email) => req("POST", "/api/auth/forgot", { email }),
    resetPassword: (token, password) => req("POST", "/api/auth/reset", { token, password }),
    changePassword: (current, password) => req("POST", "/api/auth/password", { current, password }),
    passwordRules: () => req("GET", "/api/auth/password-rules"),
    // The shelf
    files: (q) => req("GET", "/api/files" + (q || "")),
    fileUpdate: (id, d) => req("PATCH", `/api/files/${id}`, d),
    fileDelete: (id) => req("DELETE", `/api/files/${id}`),
    fileArchive: (d) => req("POST", "/api/files/archive", d),
    fileUpload: async (fileList, opts) => {
      const fd = new FormData();
      Array.from(fileList).forEach((f) => fd.append("files", f));
      if (opts && opts.title) fd.append("title", opts.title);
      if (opts && opts.note) fd.append("note", opts.note);
      const q = new URLSearchParams();
      if (opts && opts.workspace_id) q.set("workspace_id", opts.workspace_id);
      if (opts && opts.order_id) q.set("order_id", opts.order_id);
      if (opts && opts.shared) q.set("shared", "1");
      const res = await fetch("/api/files" + (q.toString() ? "?" + q : ""),
        { method: "POST", body: fd, credentials: "same-origin", headers: CSRF ? { "X-CSRF": CSRF } : {} });
      let payload = null;
      const text = await res.text();
      if (text) { try { payload = JSON.parse(text); } catch { payload = { error: text }; } }
      if (!res.ok) {
        const err = new Error((payload && payload.error) || ("Error " + res.status));
        err.status = res.status; err.code = payload && payload.code; err.info = (payload && payload.info) || {};
        throw err;
      }
      return payload;
    },

    // Getting paid
    paySettings: () => req("GET", "/api/pay-settings"),
    paySettingsSave: (d) => req("PUT", "/api/pay-settings", d),
    invoices: () => req("GET", "/api/invoices"),
    invoiceCreate: (d) => req("POST", "/api/invoices", d),
    invoiceSend: (id, to) => req("POST", `/api/invoices/${id}/send`, { to }),
    invoicePaid: (id, amount) => req("POST", `/api/invoices/${id}/paid`, { amount }),
    invoiceEvents: (id) => req("GET", `/api/invoices/${id}/events`),
    reconcile: (content, commit) => req("POST", "/api/invoices/reconcile", { content, commit }),
    remindNow: () => req("POST", "/api/invoices/remind", {}),

    smtp: () => req("GET", "/api/admin/smtp"),
    smtpSave: (d) => req("PUT", "/api/admin/smtp", d),
    smtpTest: (to) => req("POST", "/api/admin/smtp/test", { to }),
    translate: (text, to) => req("POST", "/api/translate", { text, to }),
    invbgStatus: () => req("GET", "/api/invbg/status"),
    invbgCreate: (d) => req("POST", "/api/invbg/invoice", d),

    analytics: (days) => req("GET", "/api/analytics?days=" + (days || 90)),

    // Bringing existing work in, and keeping the instance healthy
    importPreview: (kind, content) => req("POST", "/api/import/preview", { kind, content }),
    importCommit: (kind, content) => req("POST", "/api/import/commit", { kind, content }),
    health: () => req("GET", "/api/admin/health"),
    preflight: () => req("GET", "/api/admin/preflight"),
    errors: () => req("GET", "/api/admin/errors"),
    errorsSeen: () => req("POST", "/api/admin/errors/seen", {}),
    backup: (to_cloud) => req("POST", "/api/admin/backup", { to_cloud }),

    // The team inside one company
    team: () => req("GET", "/api/team"),
    teamInvite: (d) => req("POST", "/api/team", d),
    teamRole: (id, role) => req("PATCH", "/api/team/" + id, { role }),
    teamRemove: (id) => req("DELETE", "/api/team/" + id),

    // Vehicles
    vehicles: (q) => req("GET", "/api/vehicles" + (q ? "?q=" + encodeURIComponent(q) : "")),
    vehicleCreate: (d) => req("POST", "/api/vehicles", d),
    vehicleUpdate: (id, d) => req("PATCH", "/api/vehicles/" + id, d),
    vehicleLink: (id, order_id, role) => req("POST", "/api/vehicles/" + id + "/link", { order_id, role }),
    passportVehicle: (id) => req("GET", "/api/passport/vehicle/" + id),

    // Native connectors: calendar, banking, cloud storage
    calendar: () => req("GET", "/api/calendar"),
    calendarRotate: () => req("POST", "/api/calendar/rotate", {}),
    cloud: () => req("GET", "/api/cloud"),
    cloudSave: (d) => req("POST", "/api/cloud", d),
    cloudTest: () => req("POST", "/api/cloud/test", {}),
    archiveToCloud: (wsId) => req("POST", "/api/workspaces/" + wsId + "/archive/cloud", {}),
    bankStatement: (content) => req("POST", "/api/banking/statement", { content }),
    bankReconcile: (payout_ids, note) => req("POST", "/api/banking/reconcile", { payout_ids, note }),

    // Sign-in security: second factor and open sessions
    twoFactor: () => req("GET", "/api/auth/2fa"),
    twoFactorSetup: () => req("POST", "/api/auth/2fa/setup", {}),
    twoFactorEnable: (code) => req("POST", "/api/auth/2fa/enable", { code }),
    twoFactorDisable: (password) => req("POST", "/api/auth/2fa/disable", { password }),
    twoFactorVerify: (code, recovery_code) => req("POST", "/api/auth/2fa/verify", { code, recovery_code }),
    // National electronic identity (OpenID Connect)
    eidProviders: () => req("GET", "/api/auth/eid/providers"),
    eidCatalogue: (country) => req("GET", "/api/eid/catalogue" + (country ? "?country=" + encodeURIComponent(country) : "")),
    eidLinks: () => req("GET", "/api/auth/eid"),
    eidUnlink: (id) => req("DELETE", "/api/auth/eid/" + id),
    eidStart: (key) => req("POST", "/api/auth/eid/" + key + "/start", {}),
    eidConfig: () => req("GET", "/api/eid/config"),
    eidConfigSave: (d) => req("POST", "/api/eid/config", d),

    sessions: () => req("GET", "/api/auth/sessions"),
    sessionRevoke: (id) => req("DELETE", "/api/auth/sessions/" + id),
    sessionsRevokeOthers: () => req("POST", "/api/auth/sessions/revoke-others", {}),

    // What this installation does with data, and the two rights over it
    privacy: () => req("GET", "/api/privacy"),
    closeEffect: () => req("GET", "/api/account/close-effect"),
    closeAccount: (password) => req("POST", "/api/account/close", { password }),

    // The network: reference, introductions, needs
    reference: () => req("GET", "/api/reference"),
    referencePublish: (fields) => req("POST", "/api/reference", { fields }),
    referenceRevoke: () => req("POST", "/api/reference/revoke", {}),
    network: () => req("GET", "/api/network"),
    networkSearch: (q) => req("GET", "/api/network/search?q=" + encodeURIComponent(q)),
    introCreate: (d) => req("POST", "/api/network/introductions", d),
    introDecide: (id, decision, note) => req("POST", "/api/network/introductions/" + id + "/decide", { decision, note }),
    introWithdraw: (id) => req("DELETE", "/api/network/introductions/" + id),
    needCreate: (d) => req("POST", "/api/network/needs", d),
    needClose: (id) => req("POST", "/api/network/needs/" + id + "/close", {}),
    needReply: (id, message) => req("POST", "/api/network/needs/" + id + "/reply", { message }),

    passportOrder: (oid) => req("GET", "/api/passport/order/" + oid),
    passportProduct: (pid) => req("GET", "/api/passport/product/" + pid),

    // Payouts to the other side
    payouts: (wsId) => req("GET", "/api/workspaces/" + wsId + "/payouts"),
    payoutCreate: (wsId, d) => req("POST", "/api/workspaces/" + wsId + "/payouts", d),
    payoutUpdate: (id, status, note) => req("PATCH", "/api/payouts/" + id, { status, note }),
    payoutDelete: (id) => req("DELETE", "/api/payouts/" + id),

    // Notification channels
    notifySettings: () => req("GET", "/api/notify/settings"),
    notifySettingsSave: (d) => req("PUT", "/api/notify/settings", d),
    smsConfig: (d) => req("POST", "/api/notify/sms-config", d),
    smsTest: (to) => req("POST", "/api/notify/sms-test", { to }),
    pushKey: () => req("GET", "/api/notify/push-key"),
    pushSubscribe: (d) => req("POST", "/api/notify/push-subscribe", d),
    pushUnsubscribe: (endpoint) => req("POST", "/api/notify/push-unsubscribe", { endpoint }),
    pushTest: () => req("POST", "/api/notify/push-test", {}),

    // Multi-level approvals
    chains: () => req("GET", "/api/approval-chains"),
    chainCreate: (d) => req("POST", "/api/approval-chains", d),
    chainToggle: (id, active) => req("PATCH", "/api/approval-chains/" + id, { active }),
    chainDelete: (id) => req("DELETE", "/api/approval-chains/" + id),
    approvals: (oid) => req("GET", "/api/orders/" + oid + "/approvals"),
    approvalsStart: (oid, chain_id) => req("POST", "/api/orders/" + oid + "/approvals/start", { chain_id }),
    approvalDecide: (id, decision, note) => req("POST", "/api/approvals/" + id + "/decide", { decision, note }),

    // Workflow automation rules
    automations: () => req("GET", "/api/automations"),
    automationCreate: (d) => req("POST", "/api/automations", d),
    automationUpdate: (id, d) => req("PATCH", "/api/automations/" + id, d),
    automationDelete: (id) => req("DELETE", "/api/automations/" + id),

    // Qualified electronic signature providers
    qtsp: () => req("GET", "/api/qtsp"),
    qtspSave: (d) => req("POST", "/api/qtsp", d),
    qtspTest: () => req("POST", "/api/qtsp/test", {}),
    orgContacts: () => req("GET", "/api/org/contacts"),
    orgContactsSet: (d) => req("PUT", "/api/org/contacts", d),
    wsContacts: (id) => req("GET", `/api/workspaces/${id}/contacts`),
    passport: (orgId) => req("GET", `/api/passport/${orgId}`),
    signatures: (orderId) => req("GET", `/api/signatures${orderId ? "?order_id=" + orderId : ""}`),
    signatureRequest: (d) => req("POST", "/api/signatures", d),

    // workspaces
    workspaces: () => req("GET", "/api/workspaces"),
    createWorkspace: (d) => req("POST", "/api/workspaces", d),
    workspace: (id) => req("GET", `/api/workspaces/${id}`),
    invite: (id, email) => req("POST", `/api/workspaces/${id}/invite`, { email }),
    activity: (id, since) => req("GET", `/api/workspaces/${id}/activity${since ? "?since=" + since : ""}`),

    // invites
    invites: () => req("GET", "/api/invites"),
    acceptInvite: (token) => req("POST", `/api/invites/${token}/accept`, {}),

    // orders
    orders: (wsId, params) => {
      const qs = params ? "?" + new URLSearchParams(params).toString() : "";
      return req("GET", `/api/workspaces/${wsId}/orders${qs}`);
    },
    createOrder: (wsId, d) => req("POST", `/api/workspaces/${wsId}/orders`, d),
    order: (id) => req("GET", `/api/orders/${id}`),
    updateOrder: (id, d) => req("PATCH", `/api/orders/${id}`, d),
    setStatus: (id, status) => req("POST", `/api/orders/${id}/status`, { status }),
    proposeTerm: (id, key, value) => req("POST", `/api/orders/${id}/terms`, { key, value }),
    acceptTerm: (id, key) => req("POST", `/api/orders/${id}/terms/${key}/accept`, {}),
    finance: (q) => req("GET", `/api/finance?${q}`),
    financeSettings: () => req("GET", "/api/finance/settings"),
    saveFinanceSettings: (d) => req("POST", "/api/finance/settings", d),
    expenses: () => req("GET", "/api/expenses"),
    addExpense: (d) => req("POST", "/api/expenses", d),
    deleteExpense: (id) => req("DELETE", `/api/expenses/${id}`),
    setTermReview: (id, key, days) =>
      req("POST", `/api/orders/${id}/terms/${key}/review`, { every_days: days || null }),
    agreements: (days) => req("GET", `/api/agreements${days ? "?days=" + days : ""}`),
    comment: (id, body) => req("POST", `/api/orders/${id}/comments`, { body }),
    feed: (id) => req("GET", `/api/orders/${id}/feed`),
    uploadAttachments: async (id, fileList) => {
      const fd = new FormData();
      Array.from(fileList).forEach((f) => fd.append("files", f));
      const res = await fetch(`/api/orders/${id}/attachments`, { method: "POST", body: fd, credentials: "same-origin", headers: CSRF ? { "X-CSRF": CSRF } : {} });
      let payload = null;
      const text = await res.text();
      if (text) { try { payload = JSON.parse(text); } catch { payload = { error: text }; } }
      if (!res.ok) {
        const err = new Error((payload && payload.error) || ("Error " + res.status));
        err.status = res.status; err.code = payload && payload.code; err.info = (payload && payload.info) || {};
        throw err;
      }
      return payload;
    },
    deleteAttachment: (aid) => req("DELETE", `/api/attachments/${aid}`),

    // notifications
    notifications: () => req("GET", "/api/notifications"),
    markRead: (ids) => req("POST", "/api/notifications/read", ids ? { ids } : {}),
  };
})();
