// Copy the install command. Progressive enhancement: the page works without it.
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (!b || !navigator.clipboard) return;
  navigator.clipboard.writeText(document.querySelector(b.dataset.copy).textContent.trim()).then(() => {
    b.classList.add("done");
    b.querySelector(".copy-label").textContent = "COPIED";
    setTimeout(() => { b.classList.remove("done"); b.querySelector(".copy-label").textContent = "COPY"; }, 1800);
  });
});
