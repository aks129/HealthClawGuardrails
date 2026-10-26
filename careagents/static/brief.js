/* CareAgents /brief — Print or save as PDF. The browser's own print
   dialog offers both; brief-print.css lays the page out for paper. The
   button ships hidden and is shown here, so a page without script never
   offers a button that does nothing. */
(function () {
  const btn = document.getElementById("brief-print");
  if (!btn || typeof window.print !== "function") return;
  btn.hidden = false;
  btn.addEventListener("click", () => window.print());
})();
