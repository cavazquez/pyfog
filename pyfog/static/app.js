(() => {
  const status = document.querySelector("#submit-status");

  const restore = (form) => {
    form.removeAttribute("aria-busy");
    delete form.dataset.submitting;
    form.querySelectorAll("button[data-original-html]").forEach((button) => {
      button.innerHTML = button.dataset.originalHtml;
      delete button.dataset.originalHtml;
      button.disabled = false;
    });
    if (status) status.textContent = "";
  };

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }
    if (!form.checkValidity()) return;

    form.dataset.submitting = "true";
    form.setAttribute("aria-busy", "true");
    const submitter = event.submitter instanceof HTMLButtonElement ? event.submitter : null;
    form.querySelectorAll('button[type="submit"], button:not([type])').forEach((button) => {
      button.dataset.originalHtml = button.innerHTML;
      button.disabled = true;
      button.textContent =
        button === submitter ? submitter?.dataset.loadingText || "Guardando…" : "En espera…";
    });
    if (status) status.textContent = submitter?.dataset.loadingText || "Guardando cambios…";
  });

  window.addEventListener("pageshow", () => {
    document.querySelectorAll('form[aria-busy="true"]').forEach(restore);
  });
})();
