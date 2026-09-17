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

  const taskDetail = document.querySelector("#task-detail[data-task-id]");
  if (taskDetail) {
    const taskId = taskDetail.dataset.taskId;
    const statusNode = document.querySelector("#task-status");
    const phaseNode = document.querySelector("#task-phase");
    const messageNode = document.querySelector("#task-message");
    const updatedNode = document.querySelector("#task-updated");
    const bytesNode = document.querySelector("#task-bytes");
    const progressNode = document.querySelector("#task-progress");
    const errorNode = document.querySelector("#task-error");
    const terminal = new Set(["succeeded", "failed", "cancelled", "intervention_required"]);
    const labels = {
      approved: "En cola",
      assigned: "Agente asignado",
      running: "En curso",
      cancelling: "Cancelando",
      verifying: "Verificando",
      succeeded: "Completada",
      failed: "Fallida",
      cancelled: "Cancelada",
      intervention_required: "Requiere intervención",
    };
    const bytes = (value) => {
      if (value === null || value === undefined) return "Sin datos";
      let amount = Number(value);
      for (const unit of ["B", "KiB", "MiB", "GiB", "TiB"]) {
        if (amount < 1024 || unit === "TiB") return `${amount.toLocaleString("es-AR", { maximumFractionDigits: 1 })} ${unit}`;
        amount /= 1024;
      }
      return "Sin datos";
    };
    const refreshTask = async () => {
      try {
        const response = await fetch(`/tasks/${taskId}/status`, {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
        });
        if (!response.ok) return false;
        const data = await response.json();
        if (statusNode) {
          statusNode.textContent = labels[data.status] || data.status;
          statusNode.dataset.status = data.status;
          statusNode.classList.remove("ready", "pending", "muted");
          statusNode.classList.add(data.status === "succeeded" ? "ready" : terminal.has(data.status) ? "muted" : "pending");
        }
        if (phaseNode) phaseNode.textContent = data.phase;
        if (messageNode) messageNode.textContent = data.message || data.failure_reason || "Esperando actualizaciones.";
        if (updatedNode && data.updated_at) {
          updatedNode.textContent = `${new Date(data.updated_at).toLocaleString("es-AR")} UTC`;
        }
        if (errorNode) {
          errorNode.textContent = data.failure_reason || "";
          errorNode.hidden = !data.failure_reason;
        }
        if (progressNode) {
          progressNode.max = data.total_bytes || 1;
          progressNode.value = Math.min(data.bytes_processed || 0, data.total_bytes || 1);
        }
        if (bytesNode) bytesNode.textContent = data.total_bytes ? `${bytes(data.bytes_processed)} de ${bytes(data.total_bytes)}` : "Todavía no hay datos de transferencia.";
        const finished = terminal.has(data.status);
        if (finished) taskDetail.dataset.taskTerminal = "true";
        return !finished;
      } catch (_error) {
        return true;
      }
    };
    refreshTask().then((keepPolling) => {
      if (keepPolling) {
        const poll = window.setInterval(async () => {
          if (!(await refreshTask())) window.clearInterval(poll);
        }, 3000);
      }
    });
  }
})();
