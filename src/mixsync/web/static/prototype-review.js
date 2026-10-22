// PROTOTYPE (throwaway): three variants of review-as-window on /review, switchable via ?variant=A|B|C. Lives on branch prototype/review-windows only.
(() => {
  const VARIANTS = [["A", "Modal dialog"], ["B", "Slide-over drawer"], ["C", "Floating windows"]];
  const variant = document.getElementById("proto-root").dataset.variant;
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  };
  const typing = (t) => t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
  const btn = (label, aria, fn) => {
    const b = el("button", "btn quiet", label);
    b.type = "button";
    b.setAttribute("aria-label", aria);
    b.onclick = fn;
    return b;
  };
  const header = (...parts) => {
    const h = el("div", "proto-head");
    h.append(...parts);
    return h;
  };

  // ---- switcher
  const idx = VARIANTS.findIndex(([k]) => k === variant);
  const go = (d) => {
    const n = idx < 0 ? (d > 0 ? 0 : VARIANTS.length - 1) : (idx + d + VARIANTS.length) % VARIANTS.length;
    location.search = "?variant=" + VARIANTS[n][0];
  };
  const sw = el("div", "proto-switcher");
  const prev = el("button", "", "←"), next = el("button", "", "→");
  prev.type = next.type = "button";
  prev.onclick = () => go(-1);
  next.onclick = () => go(1);
  sw.append(prev, el("span", "", idx < 0 ? "Current page (no variant)" : `${VARIANTS[idx][0]} (${VARIANTS[idx][1]})`), next);
  document.body.append(sw);
  addEventListener("keydown", (e) => {
    if (typing(e.target) || e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.key === "ArrowLeft") go(-1);
    else if (e.key === "ArrowRight") go(1);
  });
  if (idx < 0) return;

  // ---- shared
  const rows = () => [...document.querySelectorAll("tbody tr")].filter((r) => r.querySelector('a[href^="/review/"]'));
  const idOf = (r) => r.querySelector('a[href^="/review/"]').textContent.trim();
  const rowFor = (id) => rows().find((r) => idOf(r) === String(id));
  const labelOf = (r) => r.cells[3].textContent.trim().replace(" - ", " — ");
  const toast = (msg) => {
    const t = el("div", "proto-toast", msg);
    t.setAttribute("role", "status");
    document.body.append(t);
    setTimeout(() => t.remove(), 2500);
  };
  const load = async (id, into) => {
    into.textContent = "Loading…";
    into.innerHTML = await (await fetch(`/review/${id}?partial=1`)).text();
  };
  // Stub every action in the window: toast, then `after` (the variant drops the row and moves on).
  const wire = (body, id, after) =>
    body.querySelectorAll("form").forEach((form) =>
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        const act = (e.submitter?.getAttribute("formaction") || form.getAttribute("action")).split("/").pop();
        toast(`PROTOTYPE: would ${act} #${id}`);
        if (!["suggest", "search", "choose"].includes(act)) after(id);
      }),
    );
  const neighbour = (id, d) => {
    const rs = rows(), i = rs.findIndex((r) => idOf(r) === String(id));
    return rs[i + d] ? idOf(rs[i + d]) : null;
  };
  // Remove the row; return the id that followed it (else the one before).
  const consume = (id) => {
    const n = neighbour(id, 1) ?? neighbour(id, -1);
    rowFor(id)?.remove();
    return n;
  };
  const onRowClick = (open) =>
    document.addEventListener("click", (e) => {
      const a = e.target.closest('tbody a[href^="/review/"]');
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
      e.preventDefault();
      open(a.textContent.trim());
    });
  const unselect = () => document.querySelectorAll("tr.proto-selected").forEach((r) => r.classList.remove("proto-selected"));

  // ---- A (modal dialog) and B (drawer): one pane that steps through the list
  if (variant === "A" || variant === "B") {
    const modal = variant === "A";
    const pane = modal ? el("dialog", "proto-dialog") : el("aside", "proto-drawer");
    const title = el("span", "proto-title");
    const body = el("div", "proto-body");
    let cur = null;
    const close = () => {
      cur = null;
      if (modal) pane.close();
      else { pane.classList.remove("open"); document.body.classList.remove("proto-split"); }
      unselect();
    };
    const show = async (id) => {
      if (id == null) return close();
      cur = String(id);
      unselect();
      const r = rowFor(id);
      r?.classList.add("proto-selected");
      title.textContent = `#${id} · ${r ? labelOf(r) : ""}`;
      await load(id, body);
      wire(body, id, (i) => show(consume(i)));
      if (!modal) r?.scrollIntoView({ block: "nearest" });
    };
    const step = (d) => { const n = neighbour(cur, d); if (n) show(n); };
    pane.append(header(btn("←", "Previous review", () => step(-1)), btn("→", "Next review", () => step(1)), title, btn("✕", "Close", close)), body);
    document.body.append(pane);
    if (modal) pane.addEventListener("close", () => { cur = null; unselect(); }); // Esc closes natively
    onRowClick((id) => {
      if (modal) pane.showModal();
      else { pane.classList.add("open"); document.body.classList.add("proto-split"); }
      show(id);
    });
    if (!modal)
      addEventListener("keydown", (e) => {
        if (typing(e.target) || cur == null) return;
        if (e.key === "j") step(1);
        else if (e.key === "k") step(-1);
        else if (e.key === "Escape") close();
      });
    return;
  }

  // ---- C: floating windows
  const tray = el("div", "proto-tray");
  document.body.append(tray);
  const wins = new Map();
  let z = 9000, spawned = 0;
  const raise = (w) => { w.style.zIndex = ++z; };
  const closeWin = (id) => {
    const x = wins.get(id);
    if (!x) return;
    x.w.remove();
    x.chip.remove();
    wins.delete(id);
    rowFor(id)?.classList.remove("proto-selected");
  };
  const openWin = async (id) => {
    id = String(id);
    const old = wins.get(id);
    if (old) { old.w.classList.remove("min"); old.chip.hidden = true; raise(old.w); return; }
    const r = rowFor(id);
    r?.classList.add("proto-selected");
    const label = `#${id} · ${r ? labelOf(r) : ""}`;
    const w = el("div", "proto-win");
    w.style.left = 40 + (spawned % 6) * 28 + "px";
    w.style.top = 70 + (spawned++ % 6) * 28 + "px";
    const body = el("div", "proto-body");
    const chip = btn(label, "Restore " + label, () => { w.classList.remove("min"); chip.hidden = true; raise(w); });
    chip.hidden = true;
    tray.append(chip);
    const head = header(el("span", "proto-title", label), btn("–", "Minimize", () => { w.classList.add("min"); chip.hidden = false; }), btn("✕", "Close", () => closeWin(id)));
    w.append(head, body);
    w.addEventListener("pointerdown", () => raise(w));
    head.addEventListener("pointerdown", (e) => {
      if (e.target.closest("button")) return;
      const dx = e.clientX - w.offsetLeft, dy = e.clientY - w.offsetTop;
      head.setPointerCapture(e.pointerId);
      head.onpointermove = (m) => { w.style.left = m.clientX - dx + "px"; w.style.top = Math.max(0, m.clientY - dy) + "px"; };
      head.onpointerup = () => { head.onpointermove = head.onpointerup = null; };
    });
    document.body.append(w);
    raise(w);
    wins.set(id, { w, chip });
    await load(id, body);
    wire(body, id, (i) => { closeWin(String(i)); rowFor(i)?.remove(); });
  };
  onRowClick(openWin);
})();
