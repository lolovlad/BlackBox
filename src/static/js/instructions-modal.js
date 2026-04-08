/* global marked */
(function () {
  async function loadMarkdownToNode(url, node) {
    try {
      const response = await fetch(url, { headers: { Accept: "text/plain" } });
      const text = await response.text();
      if (!response.ok) {
        node.innerHTML = "<p>Не удалось загрузить инструкцию.</p>";
        return;
      }
      if (typeof marked === "undefined" || typeof marked.parse !== "function") {
        node.textContent = text;
        return;
      }
      node.innerHTML = marked.parse(text, { breaks: true, gfm: true });
    } catch (error) {
      node.innerHTML = "<p>Ошибка загрузки инструкции.</p>";
    }
  }

  window.InstructionsModal = {
    loadMarkdownToNode,
  };
})();
