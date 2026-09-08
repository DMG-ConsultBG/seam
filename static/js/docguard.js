/* Free-plan document guard: discourages casual copying of a watermarked sheet.
   External rather than inline because the document pages run under a strict
   Content-Security-Policy (script-src 'self'), which blocks inline scripts.
   A screenshot cannot be prevented by any web page - the watermark is the
   control that makes a leaked copy identifiable. */
document.addEventListener("contextmenu", function (e) { e.preventDefault(); });
document.addEventListener("copy", function (e) { e.preventDefault(); });
