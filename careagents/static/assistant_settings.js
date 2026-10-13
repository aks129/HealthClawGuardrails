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
  });
})();
