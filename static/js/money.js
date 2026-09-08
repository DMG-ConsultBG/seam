/* ===========================================================================
   Seam - currency / FX
   A display currency the user picks, independent of the UI language. Money is
   stored canonically as "<CUR> <amount>" (e.g. "EUR 12400"); we convert to the
   chosen currency and format it with Intl for proper locale + symbol.
   Rates are indicative (EUR base) and live in one place for easy updating.
   ========================================================================== */
"use strict";

// Rates relative to EUR, indicative mid-market.
//
// BGN is still here and is deliberately not in the list below. Bulgaria's
// statutory dual display ended on 8 August 2026, so the lev is no longer a
// currency anyone can choose - but amounts agreed before then are stored as
// "BGN <n>", and dropping the rate would render those records as raw text
// instead of money. The rate is the one fixed by law, so those figures convert
// exactly and for good.
const BGN_FIXED = 1.95583;
const FX = {
  EUR: 1, USD: 1.08, GBP: 0.85, BGN: BGN_FIXED, RON: 5.0, TRY: 35,
  RUB: 100, RSD: 117, UAH: 45, PLN: 4.30, CHF: 0.95,
};

const CURRENCIES = [
  { code: "EUR", sym: "€" }, { code: "USD", sym: "$" }, { code: "GBP", sym: "£" },
  { code: "RON", sym: "lei" }, { code: "TRY", sym: "₺" },
  { code: "RUB", sym: "₽" }, { code: "RSD", sym: "дин" }, { code: "UAH", sym: "₴" },
  { code: "PLN", sym: "zł" }, { code: "CHF", sym: "Fr" },
];

function offered(code) { return CURRENCIES.some((c) => c.code === code); }

function defaultCcyForLang(lang) {
  return { bg: "EUR", de: "EUR", ro: "RON", el: "EUR", tr: "TRY", it: "EUR", ru: "RUB", en: "EUR" }[lang] || "EUR";
}

let CCY = (function () {
  const fallback = defaultCcyForLang(typeof LANG !== "undefined" ? LANG : "en");
  try {
    const saved = localStorage.getItem("seam_ccy");
    // Somebody who chose the lev before it was withdrawn still has it saved.
    return saved && offered(saved) ? saved : fallback;
  } catch (e) { return "EUR"; }
})();

function setCcy(code) {
  if (!offered(code)) return;
  CCY = code;
  try { localStorage.setItem("seam_ccy", code); } catch (e) {}
  if (typeof render === "function") render();
}
function currentCcy() { return CURRENCIES.find((c) => c.code === CCY) || CURRENCIES[0]; }

// "EUR 12400" / "EUR 12400.50" -> { cur, amt } ; null if not money-shaped
function parseMoney(s) {
  if (typeof s !== "string") return null;
  const m = s.trim().match(/^([A-Z]{3})\s+([0-9]+(?:\.[0-9]+)?)$/);
  if (!m || !FX[m[1]]) return null;
  return { cur: m[1], amt: parseFloat(m[2]) };
}

function convertAmount(amt, from, to) {
  if (!FX[from] || !FX[to]) return amt;
  return (amt / FX[from]) * FX[to];
}

function fmtMoney(amount, ccy, lang) {
  try {
    return new Intl.NumberFormat(lang || "en", {
      style: "currency", currency: ccy,
      minimumFractionDigits: Number.isInteger(amount) ? 0 : 2, maximumFractionDigits: 2,
    }).format(amount);
  } catch (e) {
    const sym = (CURRENCIES.find((c) => c.code === ccy) || {}).sym || ccy;
    return sym + " " + amount.toFixed(2);
  }
}

// Public: render a stored money string in the chosen display currency.
// Returns { text, title } - title carries the original for transparency.
function displayMoney(canonical) {
  const p = parseMoney(canonical);
  if (!p) return { text: canonical, title: "" };
  const lang = typeof LANG !== "undefined" ? LANG : "en";
  const conv = convertAmount(p.amt, p.cur, CCY);
  const text = fmtMoney(conv, CCY, lang);
  const orig = fmtMoney(p.amt, p.cur, lang);
  return { text: text, title: p.cur === CCY ? "" : orig };
}
function displayMoneyText(canonical) { return displayMoney(canonical).text; }
