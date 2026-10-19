/* CareAgents: copy the web address from inside an app's built-in browser,
   to open it in Safari or Chrome. The address is the public one. */
(function () {
  const $ = (id) => document.getElementById(id);
  const url = $("in-app-url");
  const copy = $("in-app-copy");
  if (!url || !copy) return;
  const msg = $("in-app-msg");
  function say(text) {
    msg.textContent = text;
    msg.hidden = false;
  }
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(url.textContent.trim());
      say("Link copied. Paste it into Safari or Chrome.");
    } catch (e) {
      // Several built-in browsers block the clipboard: select the address
      // so a long press copies it.
      const range = document.createRange();
      range.selectNodeContents(url);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      say("Press and hold the address to copy it.");
    }
  });
})();
