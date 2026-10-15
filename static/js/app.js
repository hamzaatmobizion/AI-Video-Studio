// Small vanilla helpers: CSRF-protected POSTs, the studio form, card polling, folder picker.
(function () {
  const csrf = document.querySelector('meta[name="csrf-token"]').content;

  async function post(url, body) {
    const opts = { method: "POST", headers: { "X-CSRF-Token": csrf } };
    if (body instanceof FormData) opts.body = body;
    else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body || {}); }
    const r = await fetch(url, opts);
    let data = {};
    try { data = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(data.detail || `Request failed (${r.status})`);
    return data;
  }

  function showError(el, msg) { if (!el) return alert(msg); el.textContent = msg; el.hidden = !msg; }

  // Generic [data-post] buttons
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-post]");
    if (!btn) return;
    e.preventDefault();
    if (btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
    btn.disabled = true;
    try {
      const data = await post(btn.dataset.post);
      if (btn.dataset.then) location.href = btn.dataset.then;
      else if (btn.dataset.reload !== undefined) location.reload();
      else if (btn.dataset.show) document.getElementById(btn.dataset.show).textContent = data.message || "OK";
      else refresh();
    } catch (err) { alert(err.message); }
    finally { btn.disabled = false; }
  });

  // Show/hide toggle on every password field
  const EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>';
  const EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.6 5.1A10.4 10.4 0 0 1 12 5c6.5 0 10 7 10 7a17.6 17.6 0 0 1-2.4 3.4M6.6 6.6A17.4 17.4 0 0 0 2 12s3.5 7 10 7a9.7 9.7 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="M2 2l20 20"/></svg>';
  function setShown(input, btn, shown) {
    input.type = shown ? "text" : "password";
    btn.innerHTML = shown ? EYE_OFF : EYE;
    btn.setAttribute("aria-label", shown ? "Hide password" : "Show password");
    btn.setAttribute("aria-pressed", String(shown));
  }
  document.querySelectorAll('input[type="password"]').forEach((input) => {
    const wrap = document.createElement("span");
    wrap.className = "pw";
    input.replaceWith(wrap);
    wrap.append(input);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pw-toggle";
    setShown(input, btn, false);
    btn.addEventListener("click", () => { setShown(input, btn, input.type === "password"); input.focus(); });
    wrap.append(btn);
    // hide again when the form is submitted or cleared
    if (input.form) {
      input.form.addEventListener("submit", () => setShown(input, btn, false));
      input.form.addEventListener("reset", () => setShown(input, btn, false));
    }
  });

  // Settings: while the Gemini sign-in window is open or a check runs, reload when the worker's state changes
  const watch = document.getElementById("gemini-watch");
  if (watch) {
    const poll = async () => {
      try {
        const st = await (await fetch("/settings/gemini/state")).json();
        if (st.key !== watch.dataset.key) return location.reload();
      } catch (_) {}
      setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
  }

  // Login
  const login = document.getElementById("login");
  if (login) login.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(login);
    try { await post("/login", { email: f.get("email"), password: f.get("password") }); location.href = "/"; }
    catch (err) { showError(document.getElementById("login-error"), err.message); }
  });

  // Sign up
  const signup = document.getElementById("signup");
  if (signup) signup.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("signup-error");
    if (signup.password.value !== signup.repeat.value) return showError(err, "Passwords don't match.");
    try { await post("/signup", { email: signup.email.value, password: signup.password.value }); location.href = "/"; }
    catch (ex) { showError(err, ex.message); }
  });

  // Change password
  const pw = document.getElementById("password-form");
  if (pw) pw.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("password-error"), msg = document.getElementById("password-msg");
    showError(err, ""); msg.textContent = "";
    if (pw.new.value !== pw.repeat.value) return showError(err, "New passwords don't match.");
    try {
      const data = await post("/account/password", { current_password: pw.current.value, new_password: pw.new.value });
      pw.reset(); msg.textContent = data.message;
    } catch (ex) { showError(err, ex.message); }
  });

  // New video
  const form = document.getElementById("new-video");
  if (form) form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("new-error");
    showError(err, "");
    const files = form.images.files;
    if (files.length > 3) return showError(err, "Attach at most 3 reference images.");
    for (const f of files) if (f.size > 10 * 1024 * 1024) return showError(err, `${f.name} is larger than 10 MB.`);
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try { await post("/generations", new FormData(form)); form.prompt.value = ""; form.images.value = ""; await refresh(); }
    catch (ex) { showError(err, ex.message); }
    finally { btn.disabled = false; }
  });

  // Card polling: every 5 s while anything is in progress
  const cards = document.getElementById("cards");
  let timer = null;
  async function refresh() {
    if (!cards) return;
    let data;
    try { data = await (await fetch("/api/generations")).json(); } catch (_) { return schedule(true); }
    const seen = new Set();
    let prev = null;
    for (const c of data.cards) {
      seen.add(String(c.id));
      let wrap = cards.querySelector(`.card-wrap[data-id="${c.id}"]`);
      if (!wrap) {
        wrap = document.createElement("div");
        wrap.className = "card-wrap"; wrap.dataset.id = c.id;
        prev ? prev.after(wrap) : cards.prepend(wrap);
      }
      if (wrap.dataset.hash !== c.hash) { wrap.innerHTML = c.html; wrap.dataset.hash = c.hash; }
      prev = wrap;
    }
    cards.querySelectorAll(".card-wrap").forEach((w) => { if (!seen.has(w.dataset.id)) w.remove(); });
    schedule(data.active);
  }
  function schedule(active) { clearTimeout(timer); if (active) timer = setTimeout(refresh, 5000); }
  if (cards && window.STUDIO_ACTIVE) schedule(true);

  // Drive folder picker
  const load = document.getElementById("load-folders");
  if (load) {
    const picker = document.getElementById("folder-picker"), select = document.getElementById("folder-select");
    load.addEventListener("click", async () => {
      try {
        const { folders } = await post("/settings/google/folders");
        select.innerHTML = "";
        for (const f of folders) select.add(new Option(f.name, f.id));
        if (!folders.length) select.add(new Option("(no folders created by this app yet)", ""));
        picker.hidden = false;
      } catch (err) { alert(err.message); }
    });
    document.getElementById("folder-use").addEventListener("click", async () => {
      if (!select.value) return;
      try { await post("/settings/google/folder", { folder_id: select.value }); location.reload(); } catch (err) { alert(err.message); }
    });
    document.getElementById("folder-create").addEventListener("click", async () => {
      const name = document.getElementById("folder-new").value.trim();
      if (!name) return;
      try { await post("/settings/google/folder", { new_name: name }); location.reload(); } catch (err) { alert(err.message); }
    });
  }
})();
