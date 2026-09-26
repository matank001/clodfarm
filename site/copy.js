// Copy the install command. Progressive enhancement: the page works without it.
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b || !navigator.clipboard) return;
  navigator.clipboard.writeText(document.querySelector(b.dataset.copy).textContent.trim()).then(() => {
    b.classList.add("done");
    b.setAttribute("aria-label", "Copied");
    setTimeout(() => { b.classList.remove("done"); b.setAttribute("aria-label", "Copy install command"); }, 1800);
  });
});
