/* CareAgents settings and hub: copy or share your invite link. The link is
   the whole message; nothing is sent to us. */
(function () {
  const $ = (id) => document.getElementById(id);
  const url = $("invite-url");
  if (!url) return;
  const link = url.textContent.trim();
  const msg = $("invite-msg");
  // Both lines are news, not errors: the neutral style, never the red.
  function say(text) {
    msg.textContent = text;
    msg.classList.add("is-ok");
    msg.hidden = false;
  }

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(link);
      say("Link copied.");
    } catch (e) {
      // No clipboard access: select the link so a long press copies it,
      // opening the hub's "Show link" first.
      const box = url.closest("details");
      if (box) box.open = true;
      const range = document.createRange();
      range.selectNodeContents(url);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      say("Press and hold the link to copy it.");
    }
  }

  // Phones share through their own sheet. In Settings the button stays
  // hidden elsewhere, beside Copy; the hub's one button (data-fallback)
  // copies instead (#1080).
  const share = $("invite-share");
  const fallback = Boolean(share) && share.dataset.fallback === "copy";
  if (share && (navigator.share || fallback)) {
    share.hidden = false;
    share.addEventListener("click", async () => {
      if (!navigator.share) {
        await copyLink();
        return;
      }
      try {
        await navigator.share({
          title: "CareAgents",
          // The same promise as the hub's line (#1085 patient walk).
          text: "Try CareAgents with me. It's free during the beta, and you start with made-up records.",
          url: link,
        });
      } catch (e) {
        // Closing the sheet is not an error worth a line.
      }
    });
  }

  const copy = $("invite-copy");
  if (copy) copy.addEventListener("click", copyLink);
})();
