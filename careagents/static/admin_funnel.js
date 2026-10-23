/* /admin/funnel: copy a Morning nudge. The words are already on the page;
   nothing is sent to us. */
(function () {
  document.querySelectorAll(".morning-copy").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = btn.getAttribute("data-copy") || "";
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "Copied";
      } catch (e) {
        // No clipboard access: select the words so a long press copies them.
        const words = btn.parentElement.querySelector(".morning-nudge-text");
        const range = document.createRange();
        range.selectNodeContents(words);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        btn.textContent = "Press and hold";
      }
    });
  });
})();
