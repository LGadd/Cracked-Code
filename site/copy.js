// Copy buttons: copy the code block next to the button, then say "Copied ✓" for 2 s (announced to screen readers).
document.querySelectorAll(".copy").forEach((btn) => {
  btn.setAttribute("aria-live", "polite");
  btn.addEventListener("click", async () => {
    const text = btn.parentElement.querySelector("pre").innerText.trim();
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = Object.assign(document.createElement("textarea"), { value: text });
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    btn.textContent = "Copied ✓";
    btn.classList.add("done");
    setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("done"); }, 2000);
  });
});
