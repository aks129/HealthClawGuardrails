/* CareAgents /brief — Print or save as PDF. The browser's own print
   dialog offers both; brief-print.css lays the page out for paper. The
   button ships hidden and is shown here, so a page without script never
   offers a button that does nothing. */
(function () {
  // "As of": the server wrote the instant it read the records, and the date
  // it shows is UTC's. Here it becomes the viewer's own date for that same
  // instant. Never the browser's "now": the page can be printed days later.
  const asOf = document.querySelector(".brief-asof[data-as-of]");
  if (asOf) {
    const when = new Date(asOf.getAttribute("data-as-of"));
    if (!isNaN(when.getTime())) {
      asOf.textContent = "As of " + when.toLocaleDateString("en-US", {
        month: "short", day: "numeric", year: "numeric" });
    }
  }

  const btn = document.getElementById("brief-print");
  if (!btn || typeof window.print !== "function") return;
  btn.hidden = false;
  btn.addEventListener("click", () => window.print());
})();
