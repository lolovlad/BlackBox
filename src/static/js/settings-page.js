window.settingsPage = function settingsPage() {
    return {
        instructionTitle: "Инструкция",
        async openInstruction(slug, title) {
            this.instructionTitle = title || "Инструкция";
            const modal = this.$refs?.rulesHelpModal || document.getElementById("rules-help-modal");
            if (!modal) {
                return;
            }
            const contentNode = document.getElementById("instruction-content");
            if (!contentNode) {
                return;
            }
            contentNode.innerHTML = "<p>Загрузка...</p>";
            if (typeof modal.showModal === "function") {
                modal.showModal();
            } else {
                modal.setAttribute("open", "open");
            }
            await window.InstructionsModal.loadMarkdownToNode(`/settings/instructions/${slug}`, contentNode);
        },
    };
};
