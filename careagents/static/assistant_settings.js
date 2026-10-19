/* CareAgents settings: an assistant's tone and what it focuses on (one
   assistant per person spec, section 7). The server checks both against
   its fixed lists; neither changes what the assistant can read. */
(function () {
  const forms = document.querySelectorAll("form.assistant-settings");
  forms.forEach((form) => {
    const msg = form.querySelector(".inline-msg");
    function say(text, ok) {
      msg.textContent = text;
      msg.classList.toggle("is-ok", ok);
      msg.hidden = false;
      // Below the fold at 375px, a "Saved" nobody sees (patient walk).
      msg.scrollIntoView({ block: "nearest" });
    }
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const tone = form.querySelector("input[type=radio]:checked");
      const routines = Array.from(
        form.querySelectorAll("input[type=checkbox]:checked"),
        (box) => box.value);
      const body = { routines };
      if (tone) body.persona = tone.value;
      let ok = false;
      try {
        const r = await fetch(
          `/api/agents/${encodeURIComponent(form.dataset.agent)}/settings`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
        ok = r.ok;
      } catch (e) {
        ok = false;
      }
      say(ok ? "Saved." : "That didn't save. Try again.", ok);
    });

    // An extra assistant on an account from before (owner decision D5):
    // Remove asks first, in place, then deletes only that assistant.
    const remove = form.querySelector(".assistant-remove");
    const confirm = form.querySelector(".assistant-remove-confirm");
    if (!remove || !confirm) return;
    remove.addEventListener("click", () => {
      confirm.hidden = false;
      remove.hidden = true;
      confirm.querySelector(".assistant-remove-no").focus();
    });
    confirm.querySelector(".assistant-remove-no").addEventListener(
      "click", () => {
        confirm.hidden = true;
        remove.hidden = false;
        remove.focus();
      });
    confirm.querySelector(".assistant-remove-yes").addEventListener(
      "click", async () => {
        let ok = false;
        try {
          const r = await fetch(
            `/api/agents/${encodeURIComponent(form.dataset.agent)}`,
            { method: "DELETE" });
          ok = r.ok;
        } catch (e) {
          ok = false;
        }
        if (ok) return location.reload();
        say("That didn't remove it. Try again.", false);
      });
  });
})();
