/* CareAgents settings: copy or share your invite link. The link is the
   whole message; nothing is sent to us. */
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

  // Phones share through their own sheet; elsewhere the button stays hidden.
  const share = $("invite-share");
  if (share && navigator.share) {
    share.hidden = false;
    share.addEventListener("click", async () => {
      try {
        await navigator.share({
          title: "CareAgents",
          text: "Try CareAgents with me. It starts on made-up records.",
          url: link,
        });
      } catch (e) {
        // Closing the sheet is not an error worth a line.
      }
    });
  }

  const copy = $("invite-copy");
  if (copy) {
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(link);
        say("Link copied.");
      } catch (e) {
        // No clipboard access: select the link so a long press copies it.
        const range = document.createRange();
        range.selectNodeContents(url);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        say("Press and hold the link to copy it.");
      }
    });
  }
})();
