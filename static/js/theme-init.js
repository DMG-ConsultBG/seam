// Set the theme before first paint to avoid a flash of the wrong theme.
// Kept as an external file so the page can run under a strict CSP (script-src 'self').
(function () {
  try {
    var th = localStorage.getItem("seam_theme");
    if (!th) th = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", th);
  } catch (e) {
    document.documentElement.setAttribute("data-theme", "light");
  }
})();
