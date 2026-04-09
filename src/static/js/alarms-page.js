(() => {
    const overlay = document.getElementById("alarm-export-overlay");
    const panel = document.getElementById("alarm-export-panel");
    const closeBtn = document.getElementById("alarm-export-close");
    const submitBtn = document.getElementById("alarm-export-submit");
    const eventInput = document.getElementById("alarm-export-event-id");
    const rangeHint = document.getElementById("alarm-export-range-hint");
    const outcome = document.getElementById("alarm-export-outcome");
    const form = document.getElementById("alarm-export-form");
    const openButtons = document.querySelectorAll(".alarm-export-open");
    const exportUrl = panel ? (panel.dataset.exportUrl || "").trim() : "";

    if (!overlay || !panel || !closeBtn || !submitBtn || !eventInput || !rangeHint || !outcome || !form || !exportUrl) {
        return;
    }

    const close = () => {
        overlay.classList.remove("open");
        panel.classList.remove("open");
        overlay.setAttribute("aria-hidden", "true");
        panel.setAttribute("aria-hidden", "true");
    };
    const open = (eventId, start, end) => {
        eventInput.value = String(eventId || "");
        rangeHint.textContent = `Диапазон: ${start} - 10 минут, ${end} + 10 минут.`;
        outcome.innerHTML = "";
        overlay.classList.add("open");
        panel.classList.add("open");
        overlay.setAttribute("aria-hidden", "false");
        panel.setAttribute("aria-hidden", "false");
    };
    openButtons.forEach((btn) => {
        btn.addEventListener("click", () => open(btn.dataset.eventId, btn.dataset.eventStart, btn.dataset.eventEnd));
    });
    closeBtn.addEventListener("click", close);
    overlay.addEventListener("click", close);
    submitBtn.addEventListener("click", () => {
        outcome.innerHTML = "";
        fetch(exportUrl, { method: "POST", body: new FormData(form) })
            .then((resp) => resp.text())
            .then((html) => {
                outcome.innerHTML = html;
            })
            .catch((err) => {
                outcome.innerHTML = `<div class="flash error">Ошибка экспорта: ${String(err)}</div>`;
            });
    });
})();
