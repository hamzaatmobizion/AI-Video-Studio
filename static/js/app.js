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

  // New video: one prompt + length (Gemini writes the scenes), or the user's own scenes
  const form = document.getElementById("new-video");
  const scenesBox = document.getElementById("scenes"), addScene = document.getElementById("add-scene");
  const autoMode = document.getElementById("auto-mode"), manualMode = document.getElementById("manual-mode");
  const MAX_SCENES = 12, SCENE_SECONDS = 10;
  const manual = () => autoMode && autoMode.hidden;
  // Mirrors duration_from_prompt in app/routes/api.py (the server's answer is the one used)
  const WORDS = { a: 1, an: 1, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8,
    nine: 9, ten: 10, eleven: 11, twelve: 12, "half a": 0.5, "half an": 0.5 };
  function durationFromPrompt(text) {
    const found = [];
    const length = /\b(\d+(?:\.\d+)?|half an?|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)[\s-]*(seconds?|secs?|minutes?|mins?)\b/gi;
    for (const [, num, unit] of text.matchAll(length)) {
      const key = num.toLowerCase().split(/\s+/).join(" ");
      const n = key in WORDS ? WORDS[key] : Number(num);
      found.push(unit.toLowerCase().startsWith("m") ? n * 60 : n);
    }
    for (const [, m, s] of text.matchAll(/\b([0-2]):([0-5]\d)\b(?!\s*[ap]\.?m\b)/gi)) found.push(Number(m) * 60 + Number(s));
    const longest = Math.max(0, ...found);
    if (!longest) return null;
    return Math.min(Math.max(Math.ceil(longest / SCENE_SECONDS), 1), MAX_SCENES) * SCENE_SECONDS;
  }
  function chosenDuration() {
    const picked = document.getElementById("duration").value;
    if (picked) return Number(picked);
    return durationFromPrompt(form.querySelector("textarea[name=prompt]").value) || SCENE_SECONDS;
  }
  function setEnabled(box, on) { box.querySelectorAll("textarea, select").forEach((el) => { el.disabled = !on; }); }
  function syncScenes() {
    if (!scenesBox) return;
    const items = [...scenesBox.querySelectorAll(".scene")];
    items.forEach((el, i) => {
      el.querySelector(".scene-label").textContent = `Scene ${i + 1}`;
      el.querySelector(".remove-scene").hidden = items.length === 1;
    });
    addScene.disabled = items.length >= MAX_SCENES;
    const n = manual() ? items.length : chosenDuration() / SCENE_SECONDS;
    const secs = n * SCENE_SECONDS;
    const length = secs < 60 ? `${secs}-second` : `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    document.getElementById("generate").textContent =
      n === 1 || !manual() ? `Generate ${length} video` : `Generate ${length} video (${n} scenes)`;
  }
  if (scenesBox) {
    addScene.addEventListener("click", () => {
      const first = scenesBox.querySelector(".scene"), el = first.cloneNode(true), ta = el.querySelector("textarea");
      ta.value = "";
      scenesBox.append(el); syncScenes(); ta.focus();
    });
    scenesBox.addEventListener("click", (e) => {
      const rm = e.target.closest(".remove-scene");
      if (!rm || scenesBox.querySelectorAll(".scene").length === 1) return;
      rm.closest(".scene").remove(); syncScenes();
    });
    document.getElementById("duration").addEventListener("change", syncScenes);
    form.querySelector("textarea[name=prompt]").addEventListener("input", syncScenes);
    document.getElementById("toggle-mode").addEventListener("click", (e) => {
      const toManual = !manual();
      autoMode.hidden = toManual; manualMode.hidden = !toManual;
      setEnabled(autoMode, !toManual); setEnabled(manualMode, toManual);  // hidden fields aren't sent
      e.target.textContent = toManual ? "Write one prompt instead" : "Write scenes myself";
      syncScenes();
    });
    syncScenes();
  }
  if (form) form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = document.getElementById("new-error");
    showError(err, "");
    const files = form.images.files;
    if (files.length > 3) return showError(err, "Attach at most 3 reference images.");
    for (const f of files) if (f.size > 10 * 1024 * 1024) return showError(err, `${f.name} is larger than 10 MB.`);
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const made = await post("/generations", new FormData(form));
      if (made.review && manual()) { location.href = `/generations/${made.id}/script`; return; }
      scenesBox.querySelectorAll(".scene").forEach((el, i) => { if (i) el.remove(); else el.querySelector("textarea").value = ""; });
      form.prompt.value = ""; syncScenes(); form.images.value = ""; await refresh();
    }
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
  // Characters: one form for adding and editing
  const charForm = document.getElementById("character-form");
  if (charForm) {
    const title = document.getElementById("character-form-title"), save = document.getElementById("character-save");
    const cancelEdit = document.getElementById("character-cancel"), removeRow = document.getElementById("remove-image-row");
    const reset = () => {
      charForm.reset(); charForm.dataset.id = "";
      title.textContent = "New character"; save.textContent = "Add character";
      cancelEdit.hidden = true; removeRow.hidden = true;
    };
    document.querySelectorAll(".edit-character").forEach((btn) => btn.addEventListener("click", () => {
      const c = JSON.parse(btn.closest(".char-card").dataset.character);
      for (const k of ["name", "appearance", "outfit", "voice", "personality"]) charForm[k].value = c[k] || "";
      charForm.dataset.id = c.id; charForm.image.value = ""; charForm.remove_image.checked = false;
      title.textContent = `Edit ${c.name}`; save.textContent = "Save changes";
      cancelEdit.hidden = false; removeRow.hidden = !c.has_image;
      charForm.scrollIntoView({ behavior: "smooth" }); charForm.name.focus();
    }));
    cancelEdit.addEventListener("click", reset);
    charForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = document.getElementById("character-error");
      showError(err, "");
      const f = charForm.image.files[0];
      if (f && f.size > 10 * 1024 * 1024) return showError(err, `${f.name} is larger than 10 MB.`);
      save.disabled = true;
      try {
        await post(charForm.dataset.id ? `/characters/${charForm.dataset.id}` : "/characters", new FormData(charForm));
        location.reload();
      } catch (ex) { showError(err, ex.message); }
      finally { save.disabled = false; }
    });
  }

  // Script editor: per scene the setting, camera, who is where, action, lines and sound
  const editor = document.getElementById("script-editor");
  if (editor) {
    const data = JSON.parse(document.getElementById("script-data").textContent);
    const editable = editor.dataset.editable === "true";
    const people = data.library.map((id, i) => ({ id, name: data.names[i] }));
    const styleIn = document.getElementById("script-style"), langIn = document.getElementById("script-language");
    styleIn.value = data.style; langIn.value = data.language;
    const el = (tag, attrs = {}, ...kids) => {
      const n = document.createElement(tag);
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "text") n.textContent = v;
        else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
        else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
      }
      n.append(...kids);
      return n;
    };
    const blank = () => ({ setting: "", time_of_day: "", action: "", sound: "", shot: {}, characters: [], dialogue: [] });
    let scenes = data.scenes.length ? data.scenes : [blank()];

    // A person is a library character (by id) or a name Gemini made up; the select's value encodes which.
    const key = (p) => (p.id ? `id:${p.id}` : `name:${p.name}`);
    const fromKey = (k) => {
      if (k.startsWith("id:")) { const id = Number(k.slice(3)); return { id, name: (people.find((c) => c.id === id) || {}).name || "" }; }
      return { id: null, name: k.slice(5) };
    };
    function personSelect(current, extra, onchange) {
      const sel = el("select", { disabled: !editable, "aria-label": "Character", onchange: () => onchange(fromKey(sel.value)) });
      const seen = new Set();
      const add = (p, label) => { const k = key(p); if (seen.has(k)) return; seen.add(k); sel.add(new Option(label || p.name, k)); };
      people.forEach((p) => add(p));
      [current, ...extra].filter((p) => p && p.name).forEach((p) => add(p, p.id ? p.name : `${p.name} (not in library)`));
      if (current) sel.value = key(current);
      return sel;
    }
    function field(label, value, onInput, attrs = {}) {
      const input = el(attrs.rows ? "textarea" : "input", { type: attrs.rows ? null : "text", disabled: !editable, ...attrs,
        oninput: (e) => { onInput(e.target.value); changed(); } });
      input.value = value || "";
      return el("label", {}, label, input);
    }
    function words(sc) { return sc.dialogue.reduce((n, d) => n + (d.line.trim() ? d.line.trim().split(/\s+/).length : 0), 0); }

    function card(sc, i) {
      sc.shot = sc.shot || {};
      const castRows = el("div", { class: "rows" }), lineRows = el("div", { class: "rows" });
      const count = el("span", { class: "muted small" });
      const updateCount = () => {
        const n = words(sc);
        count.textContent = n ? `${n} spoken word${n === 1 ? "" : "s"}${n > data.max_words ? ` (about ${data.max_words} fit in 10 seconds)` : ""}` : "";
        count.className = n > data.max_words ? "warn" : "muted small";
      };
      function drawCast() {
        castRows.replaceChildren(...sc.characters.map((p, j) => el("div", { class: "line" },
          personSelect(p, [], (np) => { sc.characters[j] = { ...np, position: p.position }; changed(); drawLines(); }),
          (() => {
            const pos = el("select", { disabled: !editable, "aria-label": "Position in frame",
              onchange: (e) => { p.position = e.target.value; changed(); } });
            pos.add(new Option("anywhere in frame", ""));
            data.positions.forEach((o) => pos.add(new Option(o, o)));
            if (p.position && !data.positions.includes(p.position)) pos.add(new Option(p.position, p.position));
            pos.value = p.position || "";
            return pos;
          })(),
          editable ? el("button", { type: "button", class: "link", text: "Remove",
            onclick: () => { sc.characters.splice(j, 1); drawCast(); changed(); } }) : "")));
      }
      function drawLines() {
        lineRows.replaceChildren(...sc.dialogue.map((d, j) => el("div", { class: "line" },
          personSelect(d, sc.characters, (np) => { Object.assign(d, np); changed(); }),
          (() => {
            const input = el("input", { type: "text", maxlength: 400, disabled: !editable, placeholder: "What they say",
              "aria-label": "Line", oninput: (e) => { d.line = e.target.value; updateCount(); changed(); } });
            input.value = d.line;
            return input;
          })(),
          editable ? el("button", { type: "button", class: "link", text: "Remove",
            onclick: () => { sc.dialogue.splice(j, 1); drawLines(); updateCount(); changed(); } }) : "")));
      }
      drawCast(); drawLines(); updateCount();
      const firstPerson = () => sc.characters[0] || people[0] || { id: null, name: "Narrator" };
      const prompt = el("pre", { class: "prompt-text", text: data.prompts[i] || "" });
      const warns = el("div", { class: "warnings" });
      return el("article", { class: "shot-card" },
        el("header", {}, el("h2", { text: `Scene ${i + 1}` }), el("span", { class: "muted small", text: `${i * 10}–${i * 10 + 10} s` }),
          editable && scenes.length > 1 ? el("button", { type: "button", class: "link", text: "Remove scene",
            onclick: () => { if (confirm(`Remove scene ${i + 1}?`)) { scenes.splice(i, 1); draw(); changed(); } } }) : ""),
        el("div", { class: "grid2" },
          field("Setting", sc.setting, (v) => { sc.setting = v; }, { maxlength: 400, placeholder: "Old Lahore rooftop, kites in the sky" }),
          field("Time of day", sc.time_of_day, (v) => { sc.time_of_day = v; }, { maxlength: 60, list: "time-options" })),
        el("h3", { class: "sub", text: "Camera" }),
        el("div", { class: "grid4" }, ...["framing", "angle", "movement", "lens"].map((k) =>
          field(k[0].toUpperCase() + k.slice(1), sc.shot[k], (v) => { sc.shot[k] = v; }, { maxlength: 60, list: `${k}-options` }))),
        el("h3", { class: "sub", text: "In the shot" }), castRows,
        editable ? el("button", { type: "button", class: "link", text: "+ Add character to shot",
          onclick: () => { sc.characters.push({ ...(people.find((p) => !sc.characters.some((c) => c.id === p.id)) || firstPerson()), position: "" }); drawCast(); changed(); } }) : "",
        field("Action", sc.action, (v) => { sc.action = v; }, { rows: 3, maxlength: 1500, placeholder: "What happens, how they move and react" }),
        el("h3", { class: "sub", text: "Dialogue" }), lineRows,
        el("div", { class: "line" }, editable ? el("button", { type: "button", class: "link", text: "+ Add line",
          onclick: () => { sc.dialogue.push({ ...firstPerson(), line: "" }); drawLines(); changed(); } }) : "", " ", count),
        field("Sound", sc.sound, (v) => { sc.sound = v; }, { maxlength: 300, placeholder: "Wind, distant traffic, kite strings" }),
        warns,
        el("details", { class: "compiled" }, el("summary", { text: "Clip prompt" }), prompt));
    }
    function draw() { editor.replaceChildren(...scenes.map(card)); }
    draw();

    // Autosave (and refresh the clip prompts) shortly after each change
    const status = document.getElementById("script-status"), err = document.getElementById("script-error");
    let saveTimer = null, saving = Promise.resolve();
    const payload = (approve) => ({ scenes, style: styleIn.value, language: langIn.value, approve });
    function show(result) {
      editor.querySelectorAll(".shot-card").forEach((c, i) => {
        const r = result[i]; if (!r) return;
        c.querySelector(".prompt-text").textContent = r.prompt;
        c.querySelector(".warnings").replaceChildren(...r.warnings.map((w) => el("p", { class: "warn", text: w })));
      });
    }
    async function saveNow() {
      clearTimeout(saveTimer);
      status.textContent = "Saving…";
      try { show((await post(`/generations/${editor.dataset.id}/script`, payload(false))).scenes); status.textContent = "Saved"; }
      catch (ex) { status.textContent = ""; showError(err, ex.message); }
    }
    function changed() {
      if (!editable) return;
      showError(err, ""); status.textContent = "Unsaved changes";
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => { saving = saveNow(); }, 700);
    }
    if (editable) {
      styleIn.addEventListener("input", changed); langIn.addEventListener("input", changed);
      document.getElementById("add-shot").addEventListener("click", () => {
        if (scenes.length >= 12) return showError(err, "A video can have at most 12 scenes.");
        const prev = scenes[scenes.length - 1] || blank();
        scenes.push({ ...blank(), setting: prev.setting, time_of_day: prev.time_of_day, characters: prev.characters.map((p) => ({ ...p })) });
        draw(); changed();
        editor.lastElementChild.scrollIntoView({ behavior: "smooth" });
      });
      document.getElementById("approve-script").addEventListener("click", async (e) => {
        const btn = e.target;
        btn.disabled = true; showError(err, "");
        clearTimeout(saveTimer);
        try {
          await saving;
          await post(`/generations/${editor.dataset.id}/script`, payload(true));
          location.href = "/";
        } catch (ex) { showError(err, ex.message); }
        finally { btn.disabled = false; }
      });
      saving = saveNow();  // shows the warnings and current prompts straight away
    }
  }
})();
