document.querySelectorAll(".copy-btn").forEach((button) => {
  button.addEventListener("click", () => {
    const pre = button.parentElement.querySelector("pre:not(.hidden)");
    const text = pre.textContent.replace(/^\$\s*/, "");
    navigator.clipboard.writeText(text).then(() => {
      button.textContent = "copied ✓";
      setTimeout(() => (button.textContent = "copy"), 1500);
    });
  });
});
