// Cracked Code site: copy buttons, the terminal playback, the benchmark switcher, install tabs, reveal on scroll.
// Everything is readable without this file; it only adds motion and interaction.
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
document.documentElement.classList.add("js");

// Copy: the button copies data-copy's target, or the <code> next to it.
document.querySelectorAll(".copy").forEach((btn) => {
  btn.setAttribute("aria-live", "polite");
  btn.addEventListener("click", async () => {
    const el = btn.dataset.copy ? document.querySelector(btn.dataset.copy) : btn.parentElement.querySelector("code");
    const text = el.textContent.trim();
    try { await navigator.clipboard.writeText(text); }
    catch {
      const t = Object.assign(document.createElement("textarea"), { value: text });
      document.body.append(t); t.select(); document.execCommand("copy"); t.remove();
    }
    btn.textContent = "Copied ✓"; btn.classList.add("done");
    setTimeout(() => { btn.textContent = "Copy"; btn.classList.remove("done"); }, 1800);
  });
});

// Terminal: show the session line by line, typing the first line, once it scrolls into view.
const term = document.getElementById("term");
if (term && !still) {
  const lines = [...term.querySelectorAll(".l")];
  const first = lines[0];
  const firstHTML = first.innerHTML;
  const typed = " add a --verbose flag to cli.py and test it";
  const delays = [0, 700, 500, 450, 900, 120, 350, 250, 120, 500];
  let timers = [];
  const sleep = (ms) => new Promise((r) => timers.push(setTimeout(r, ms)));
  async function play() {
    timers.forEach(clearTimeout); timers = [];
    lines.forEach((l) => l.classList.add("hide"));
    first.classList.remove("hide");
    first.innerHTML = '<span class="g b">&gt;_</span><span class="t"></span>';
    const out = first.querySelector(".t");
    out.classList.add("caret");
    await sleep(400);
    for (const ch of typed) { out.textContent += ch; await sleep(28); }
    await sleep(350);
    out.classList.remove("caret");
    first.innerHTML = firstHTML;
    for (let i = 1; i < lines.length; i++) { await sleep(delays[i]); lines[i].classList.remove("hide"); }
  }
  new IntersectionObserver((entries, obs) => {
    if (entries[0].isIntersecting) { play(); obs.disconnect(); }
  }, { threshold: 0.35 }).observe(term);
  document.querySelector(".replay").addEventListener("click", play);
} else {
  document.querySelector(".replay")?.remove();
}

// Benchmarks: switch model; bar widths are relative to the slowest harness.
const DATA = {
  local: { rows: [[404, "174k"], [592, "1.43M"], [825, "1.74M"]], note: "<b>2.04× faster</b> than Qwen Code, <b>1.47×</b> faster than Claude Code." },
  glm: { rows: [[126, "134k"], [144, "1.27M"], [359, "1.30M"]], note: "<b>2.85× faster</b> than Qwen Code. The closest race: <b>1.14×</b> faster than Claude Code." },
  ds: { rows: [[80, "159k"], [137, "1.10M"], [167, "1.31M"]], note: "<b>2.09× faster</b> than Qwen Code, <b>1.71×</b> faster than Claude Code. 80 s for all 11 tasks." },
};
const bars = [...document.querySelectorAll("#chart .bar")];
document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  const d = DATA[b.dataset.set];
  const max = Math.max(...d.rows.map((r) => r[0]));
  d.rows.forEach(([s, tok], i) => {
    bars[i].querySelector(".fill").style.setProperty("--w", `${Math.round((s / max) * 100)}%`);
    bars[i].querySelector(".t").textContent = `${s} s`;
    bars[i].querySelector(".tok").textContent = `${tok} tok`;
  });
  document.getElementById("callout").innerHTML = d.note;
}));

// Install tabs (arrow keys move between them, as a tablist should).
const tabs = [...document.querySelectorAll('[role="tab"]')];
function pick(tab) {
  tabs.forEach((t) => {
    const on = t === tab;
    t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
  });
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => pick(t));
  t.addEventListener("keydown", (e) => {
    const n = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (n) { const next = tabs[(i + n + tabs.length) % tabs.length]; pick(next); next.focus(); }
  });
});
if (/Windows/.test(navigator.userAgent)) pick(document.getElementById("t-win"));

// Reveal sections as they scroll in.
if (!still && "IntersectionObserver" in window) {
  const io = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }), { threshold: 0.12 });
  document.querySelectorAll(".stats, .block > *").forEach((el) => { el.classList.add("reveal"); io.observe(el); });
}
