/* Farm-style pickers: every <select> on the farm's pages opens a pixel-art list instead of the system's menu.
 *
 * The <select> stays the source of truth: it is still in the form, its label still names it, code still reads and sets
 * its value and listens for `change`. Only its opening is taken over (one set of listeners on the document, so a
 * select added later works too): pointer, Enter, Space, Alt+Down or the arrows open the list, the arrows and letters
 * move in it, Enter or a click picks, Escape or a click outside closes it. An option with data-icon shows that image.
 * The list is a popover inside the select's own dialog, so it sits above everything and stays clickable in a modal.
 * A <select multiple>, a select with class "native", and a browser without popovers keep the system's menu.
 */
(() => {
  if (!HTMLElement.prototype.hasOwnProperty("popover")) return;
  let open = null; // { sel, pop, items, active }

  const usable = (el) => el instanceof HTMLSelectElement && !el.multiple && !el.disabled && !el.classList.contains("native");

  function close(focus = true) {
    if (!open) return;
    const { sel, pop } = open;
    open = null;
    try { pop.hidePopover(); } catch { /* already gone */ }
    pop.remove();
    sel.setAttribute("aria-expanded", "false");
    sel.removeAttribute("aria-activedescendant");
    if (focus) sel.focus({ preventScroll: true });
  }

  function pick(i) {
    const { sel, items } = open, it = items[i];
    if (!it || it.opt.disabled) return;
    const changed = sel.value !== it.opt.value || sel.selectedIndex !== it.index;
    sel.selectedIndex = it.index;
    close();
    if (changed) {
      sel.dispatchEvent(new Event("input", { bubbles: true }));
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  function setActive(i) {
    const { items, sel } = open;
    if (!items.length) return;
    i = Math.max(0, Math.min(items.length - 1, i));
    if (open.active >= 0) items[open.active].el.classList.remove("on");
    open.active = i;
    const el = items[i].el;
    el.classList.add("on");
    sel.setAttribute("aria-activedescendant", el.id);
    el.scrollIntoView({ block: "nearest" });
  }

  function place() {
    if (!open) return;
    const { sel, pop } = open, r = sel.getBoundingClientRect(), gap = 6, vh = innerHeight;
    const below = vh - r.bottom - gap - 8, above = r.top - gap - 8;
    const up = below < 180 && above > below;
    const max = Math.max(120, Math.min(340, up ? above : below));
    pop.style.maxHeight = `${max}px`;
    pop.style.minWidth = `${Math.max(r.width, 180)}px`;
    pop.style.left = `${Math.max(8, Math.min(r.left, innerWidth - Math.max(r.width, 180) - 8))}px`;
    pop.style.top = up ? "" : `${r.bottom + gap}px`;
    pop.style.bottom = up ? `${vh - r.top + gap}px` : "";
  }

  let n = 0;
  function show(sel) {
    if (open?.sel === sel) return close();
    close(false);
    const pop = document.createElement("div");
    pop.className = "fsel-pop";
    pop.setAttribute("popover", "manual");
    pop.setAttribute("role", "listbox");
    pop.id = `fsel-${++n}`;
    if (sel.getAttribute("aria-label") || sel.labels?.[0]) pop.setAttribute("aria-label", sel.getAttribute("aria-label") || sel.labels[0].textContent.trim());
    const items = [];
    let group = null;
    [...sel.options].forEach((opt, index) => {
      if (opt.hidden) return;
      const g = opt.parentElement instanceof HTMLOptGroupElement ? opt.parentElement : null;
      if (g && g !== group) pop.append(Object.assign(document.createElement("div"), { className: "fsel-group", textContent: g.label }));
      group = g;
      const el = document.createElement("div");
      el.className = "fsel-opt" + (index === sel.selectedIndex ? " picked" : "") + (opt.disabled ? " off" : "");
      el.id = `${pop.id}-${index}`;
      el.setAttribute("role", "option");
      el.setAttribute("aria-selected", String(index === sel.selectedIndex));
      if (opt.disabled) el.setAttribute("aria-disabled", "true");
      const tick = document.createElement("span");
      tick.className = "fsel-tick";
      tick.setAttribute("aria-hidden", "true");
      el.append(tick);
      if (opt.dataset.icon) {
        const img = document.createElement("img");
        img.src = opt.dataset.icon; img.alt = ""; img.className = "fsel-ic";
        el.append(img);
      }
      el.append(Object.assign(document.createElement("span"), { className: "fsel-txt", textContent: opt.textContent }));
      const k = items.length;
      el.addEventListener("pointerenter", () => setActive(k));
      el.addEventListener("click", (e) => { e.preventDefault(); pick(k); });
      items.push({ opt, index, el });
      pop.append(el);
    });
    (sel.closest("dialog[open]") || document.body).append(pop);
    open = { sel, pop, items, active: -1, typed: "", typedAt: 0 };
    sel.setAttribute("aria-expanded", "true");
    sel.setAttribute("aria-controls", pop.id);
    pop.showPopover();
    place();
    setActive(Math.max(0, items.findIndex(it => it.index === sel.selectedIndex)));
  }

  function typeahead(ch) {
    const now = Date.now();
    open.typed = (now - open.typedAt < 700 ? open.typed : "") + ch.toLowerCase();
    open.typedAt = now;
    const { items } = open, from = open.active + (open.typed.length === 1 ? 1 : 0);
    for (let j = 0; j < items.length; j++) {
      const it = items[(from + j) % items.length];
      if (!it.opt.disabled && it.opt.textContent.trim().toLowerCase().startsWith(open.typed)) return setActive((from + j) % items.length);
    }
  }

  document.addEventListener("pointerdown", (e) => {
    const sel = e.target;
    if (usable(sel)) {
      if (e.button !== 0) return;
      e.preventDefault(); // no system menu
      sel.focus({ preventScroll: true });
      show(sel);
      return;
    }
    if (!open) return;
    if (open.pop.contains(e.target)) e.preventDefault(); // focus stays on the select while you pick
    else close(false);
  }, true);
  // a touch browser that opens its menu on the click itself
  document.addEventListener("click", (e) => { if (usable(e.target)) e.preventDefault(); }, true);
  document.addEventListener("mousedown", (e) => { if (usable(e.target)) e.preventDefault(); }, true);

  document.addEventListener("keydown", (e) => {
    const sel = e.target;
    if (!usable(sel)) return;
    if (!open || open.sel !== sel) {
      if (e.key === " " || e.key === "Enter" || e.key === "ArrowDown" || e.key === "ArrowUp" || (e.altKey && e.key === "ArrowDown")) {
        e.preventDefault();
        show(sel);
      }
      return;
    }
    const last = open.items.length - 1;
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); setActive(open.active + 1); break;
      case "ArrowUp": e.preventDefault(); setActive(open.active - 1); break;
      case "Home": e.preventDefault(); setActive(0); break;
      case "End": e.preventDefault(); setActive(last); break;
      case "PageDown": e.preventDefault(); setActive(open.active + 8); break;
      case "PageUp": e.preventDefault(); setActive(open.active - 8); break;
      case "Enter": case " ": e.preventDefault(); pick(open.active); break;
      case "Escape": e.preventDefault(); e.stopPropagation(); close(); break; // not the dialog's Escape too
      case "Tab": close(false); break;
      default:
        if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); typeahead(e.key); }
    }
  }, true);

  addEventListener("resize", () => close(false));
  document.addEventListener("scroll", (e) => { if (open && !open.pop.contains(e.target)) place(); }, true);
  document.addEventListener("focusout", (e) => {
    if (open && e.target === open.sel && !open.pop.contains(e.relatedTarget)) setTimeout(() => { if (open && document.activeElement !== open.sel) close(false); }, 0);
  });
  document.addEventListener("close", () => close(false), true); // its dialog closed
})();
