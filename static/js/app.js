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

  // "Copy" buttons: put the text of the element their data-copy names on the clipboard
  document.querySelectorAll("[data-copy]").forEach((btn) => btn.addEventListener("click", async () => {
    const text = document.querySelector(btn.dataset.copy).innerText;
    try { await navigator.clipboard.writeText(text); } catch (_) {
      const area = Object.assign(document.createElement("textarea"), { value: text });
      document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
    }
    const was = btn.textContent;
    btn.textContent = "Copied ✓";
    setTimeout(() => { btn.textContent = was; }, 1500);
  }));

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

  // Settings: while a sign-in window (Gemini or Flow) is open or a check runs, reload when the worker's state changes
  document.querySelectorAll("[data-watch]").forEach((watch) => {
    const poll = async () => {
      try {
        const st = await (await fetch(`/settings/${watch.dataset.watch}/state`)).json();
        if (st.key !== watch.dataset.key) return location.reload();
      } catch (_) {}
      setTimeout(poll, 3000);
    };
    setTimeout(poll, 3000);
  });

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
  // One scene = one clip: 10 s with Gemini, 8 s with Google Flow (SCENE_SECONDS in app/enums.py)
  const MAX_SCENES = 12, SCENE_SECONDS = form ? Number(form.dataset.step) || 10 : 10;
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
  // At most max_cast characters (MAX_CAST in app/script.py): the rest can't be ticked once the cast is full
  const castList = document.querySelector(".cast-list");
  if (castList) {
    const boxes = [...castList.querySelectorAll("input[name=cast]")], max = Number(castList.dataset.max) || 8;
    const syncCast = () => {
      const full = boxes.filter((b) => b.checked).length >= max;
      boxes.forEach((b) => { b.disabled = full && !b.checked; });
    };
    castList.addEventListener("change", syncCast); syncCast();
  }
  // A note when more characters are picked than the length can show with their portraits (3 per clip)
  const PER_CLIP = 3;
  const lengthFor = (clips) => `${clips * SCENE_SECONDS} seconds`;
  function syncNotes() {
    if (!scenesBox) return;
    const clips = manual() ? scenesBox.querySelectorAll(".scene").length : chosenDuration() / SCENE_SECONDS;
    const castNote = document.getElementById("cast-note");
    if (castNote) {
      const picked = form.querySelectorAll("input[name=cast]:checked").length;
      const need = Math.ceil(picked / PER_CLIP);
      castNote.hidden = picked <= clips * PER_CLIP;
      castNote.textContent = `${picked} characters in ${lengthFor(clips)}: each clip shows about ${PER_CLIP} with ` +
        `their portrait, so some won't look like theirs. Choose ${lengthFor(need)} or more to give everyone a scene.`;
    }
  }
  // A note when the lines written in the prompt (in quotes) need more time than the length gives them
  const WORDS_PER_CLIP = Math.round(25 * SCENE_SECONDS / 10);  // words_per_clip in app/script.py: 25 per 10 s
  function syncTalk() {
    const note = document.getElementById("talk-note");
    if (!note || manual()) { if (note) note.hidden = true; return; }
    const text = form.querySelector("textarea[name=prompt]").value;
    const words = [...text.matchAll(/["“”]([^"“”\n]{2,})["“”]/g)].reduce((n, m) => n + m[1].trim().split(/\s+/).length, 0);
    const clips = chosenDuration() / SCENE_SECONDS, need = Math.ceil(words / WORDS_PER_CLIP);
    note.hidden = need <= clips;
    note.textContent = `Your lines have about ${words} spoken words, but ${lengthFor(clips)} fits about ` +
      `${clips * WORDS_PER_CLIP}, so they'll be rushed or some may be cut. Choose ${lengthFor(Math.min(need, MAX_SCENES))} ` +
      "or more to keep every line.";
  }
  // A format named in the prompt wins over the picked one (mirrors aspect_from_prompt in app/script.py)
  const ASPECTS = {
    "9:16": /\b9\s*[:x×/]\s*16\b|\bvertical\b|\bportrait (?:video|format|mode|orientation)\b|\b(?:reels?|shorts|tiktok|stories)\b/gi,
    "16:9": /\b16\s*[:x×/]\s*9\b|\bhorizontal\b|\blandscape (?:video|format|mode|orientation)\b|\bwide-?screen\b/gi,
  };
  function syncAspect() {
    const note = document.getElementById("aspect-note");
    if (!note) return;
    const text = [...form.querySelectorAll("textarea:not(:disabled)")].map((t) => t.value).join("\n");
    const found = [];
    for (const [aspect, re] of Object.entries(ASPECTS))
      for (const m of text.matchAll(re)) found.push([/^\d/.test(m[0]) ? 0 : 1, m.index, aspect]);
    found.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    note.hidden = !found.length;
    if (found.length) form.querySelector(`input[name=aspect][value="${found[0][2]}"]`).checked = true;
  }
  // Notes from the server on things known to make Gemini refuse or ignore the prompt (app/checks.py)
  let checkTimer = null, checkSeq = 0;
  function syncChecks() {
    const box = document.getElementById("prompt-notes");
    if (!box) return;
    clearTimeout(checkTimer);
    checkTimer = setTimeout(async () => {
      const seq = ++checkSeq;
      const text = [...form.querySelectorAll("textarea:not(:disabled)")].map((t) => t.value).join("\n");
      const field = (n) => (form.elements.namedItem(n) || {}).value || "";
      const body = { prompt: text, style: field("style"), language: field("language"),
        seconds: manual() ? 0 : chosenDuration(), provider: form.dataset.provider || "",
        cast: [...form.querySelectorAll("input[name=cast]:checked")].map((b) => Number(b.value)) };
      let notes = [];
      try { notes = text.trim() ? (await post("/api/prompt-check", body)).notes : []; } catch (_) { return; }
      if (seq !== checkSeq) return;  // a newer check is on its way
      // red: Gemini will likely refuse or fail; yellow: may spoil the video; green: nothing found
      const others = ["talk-note", "cast-note"].some((id) => { const el = document.getElementById(id); return el && !el.hidden; });
      const lines = notes.map((n) => Object.assign(document.createElement("p"),
        { className: n.level === "error" ? "note-error" : "warn", textContent: n.text }));
      if (!lines.length && !others && text.trim())
        lines.push(Object.assign(document.createElement("p"), { className: "note-ok",
          textContent: "✓ Prompt looks good: nothing found that's known to make Gemini refuse or spoil the video." }));
      box.replaceChildren(...lines);
      box.hidden = !lines.length;
    }, 600);
  }
  // "click" covers adding or removing a scene and switching between one prompt and own scenes
  if (form) ["input", "change", "click"].forEach((t) => form.addEventListener(t, () => { syncNotes(); syncTalk(); syncAspect(); syncChecks(); }));
  syncNotes(); if (form) { syncTalk(); syncAspect(); syncChecks(); }
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
      form.prompt.value = ""; syncScenes(); form.images.value = ""; form.elements.namedItem("music").value = ""; await refresh();
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
    const provider = cards.dataset.provider ? `?provider=${encodeURIComponent(cards.dataset.provider)}` : "";
    try { data = await (await fetch(`/api/generations${provider}`)).json(); } catch (_) { return schedule(true); }
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
  // Characters: the editor panel (fields, voice chips, live clip preview) and the portrait studio
  const charForm = document.getElementById("character-form");
  if (charForm) {
    const panel = document.getElementById("cp-editor"), title = document.getElementById("cp-editor-title");
    const save = document.getElementById("character-save"), err = document.getElementById("character-error");
    const frame = document.getElementById("cp-frame"), frameImg = document.getElementById("cp-frame-img");
    const badge = document.getElementById("cp-frame-badge"), fileIn = document.getElementById("cp-file");
    const portraitIn = document.getElementById("cp-portrait-id"), removeIn = document.getElementById("cp-remove-image");
    const removeBtn = document.getElementById("cp-remove-pic"), drawBtn = document.getElementById("cp-draw");
    const drawErr = document.getElementById("cp-draw-error"), drawHint = document.getElementById("cp-draw-hint");
    const attempts = document.getElementById("cp-attempts"), attemptsRow = document.getElementById("cp-attempts-row");
    const f = (name) => charForm.elements[name];
    let editing = null, localUrl = null, polling = null, drawing = false;
    const canDraw = !drawBtn.disabled;

    function showPicture(url, label) {
      frame.dataset.state = url ? "image" : "empty";
      frameImg.hidden = !url;
      if (url) frameImg.src = url; else frameImg.removeAttribute("src");
      badge.hidden = !label; badge.textContent = label || "";
      removeBtn.hidden = !url;
      syncPreview();
    }
    function choosePortrait(p) {
      fileIn.value = ""; portraitIn.value = p.id; removeIn.value = "";
      showPicture(p.image_url, "AI portrait");
      attemptsRow.querySelectorAll(".cp-attempt").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.id === String(p.id))));
    }
    function addAttempt(p) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "cp-attempt"; b.dataset.id = p.id; b.setAttribute("aria-label", "Use this portrait");
      const img = document.createElement("img"); img.src = p.image_url; img.alt = "";
      b.append(img); b.addEventListener("click", () => choosePortrait(p));
      attemptsRow.prepend(b); attempts.hidden = false;
    }

    // counters, chips and the live preview
    const counters = [...charForm.querySelectorAll(".cp-count")];
    function syncCounts() {
      for (const c of counters) {
        const n = document.getElementById(c.dataset.for).value.length, max = Number(c.dataset.max);
        c.textContent = `${n} / ${max}`; c.classList.toggle("near", n > max * 0.9);
      }
    }
    const parts = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
    function syncChips() {
      charForm.querySelectorAll(".cp-chips").forEach((box) => {
        const have = parts(document.getElementById(box.dataset.target).value.toLowerCase());
        box.querySelectorAll(".cp-chip").forEach((c) => c.setAttribute("aria-pressed", String(have.includes(c.textContent.toLowerCase()))));
      });
    }
    charForm.querySelectorAll(".cp-chips").forEach((box) => box.addEventListener("click", (e) => {
      const chip = e.target.closest(".cp-chip");
      if (!chip) return;
      const input = document.getElementById(box.dataset.target), word = chip.textContent;
      const list = parts(input.value), i = list.findIndex((x) => x.toLowerCase() === word.toLowerCase());
      if (i >= 0) list.splice(i, 1); else list.push(word);
      input.value = list.join(", ").slice(0, Number(input.maxLength) || 400);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }));
    const sentence = (t) => (/[.!?]$/.test(t) ? t : `${t}.`);
    function syncPreview() {
      const name = f("name").value.trim() || "Name", look = [f("appearance").value.trim(), f("outfit").value.trim()].filter(Boolean).join("; ");
      const voice = f("voice").value.trim(), locked = f("locked").value.trim().replace(/[ .]+$/, "");
      const never = f("never").value.trim().replace(/[ .]+$/, "");
      const always = locked ? `; always ${locked}` : "";
      // As app/script.py compile_prompt: a pictured character is described by their picture and locked traits.
      const who = frame.dataset.state === "image"
        ? `${name}, exactly as in reference image 1 (same face, hair, outfit and style)${always}.`
        : sentence(name + (look ? ` (${look})` : "") + always);
      document.getElementById("cp-preview").textContent =
        `Characters: ${who}\n${name}${voice ? ` (voice: ${voice})` : ""} says: '…'${never ? `\nAvoid: ${never}.` : ""}`;
      if (canDraw && !drawing) {
        drawBtn.disabled = !f("appearance").value.trim();
        drawHint.textContent = drawBtn.disabled ? "Describe their appearance first, then let Gemini draw them." : "Gemini draws them from the Look section.";
      }
    }
    // One portrait of a group can't keep every face the same from clip to clip
    const GROUP = /\b(group|friends|team|family|couple|crowd|gang|squad|\d+\s+(boys|girls|men|women|people|kids|children|friends))\b/i;
    const syncGroup = () => { document.getElementById("cp-group-warn").hidden = !GROUP.test(f("appearance").value); };
    charForm.addEventListener("input", () => { syncCounts(); syncChips(); syncPreview(); syncGroup(); });

    function open(c) {
      clearInterval(polling); drawing = false; frame.dataset.state = "empty";
      charForm.reset(); editing = c ? c.id : null;
      for (const k of ["name", "appearance", "outfit", "voice", "personality", "locked", "never"]) f(k).value = (c && c[k]) || "";
      portraitIn.value = ""; removeIn.value = ""; attemptsRow.replaceChildren(); attempts.hidden = true;
      showError(err, ""); showError(drawErr, "");
      showPicture(c ? c.image_url : null, null);
      title.textContent = c ? `Edit ${c.name}` : "New character";
      save.textContent = c ? "Save changes" : "Create character";
      panel.hidden = false; syncCounts(); syncChips(); syncPreview(); syncGroup();
      const empty = document.getElementById("cp-empty"); if (empty) empty.hidden = true;
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
      f("name").focus({ preventScroll: true });
    }
    function close() {
      clearInterval(polling); drawing = false; panel.hidden = true;
      const empty = document.getElementById("cp-empty"); if (empty) empty.hidden = false;
    }
    document.querySelectorAll("#cp-new, .cp-new-btn").forEach((b) => b.addEventListener("click", () => open(null)));
    document.querySelectorAll(".edit-character").forEach((b) => b.addEventListener("click", () => open(JSON.parse(b.closest(".cp-card").dataset.character))));
    document.getElementById("cp-close").addEventListener("click", close);
    document.getElementById("character-cancel").addEventListener("click", close);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) close(); });

    // upload: click or drop a photo onto the frame
    function useFile(file) {
      if (!file) return;
      if (!/^image\/(jpeg|png|webp)$/.test(file.type)) return showError(drawErr, `${file.name} isn't a JPG, PNG or WebP image.`);
      if (file.size > 10 * 1024 * 1024) return showError(drawErr, `${file.name} is larger than 10 MB.`);
      showError(drawErr, "");
      if (localUrl) URL.revokeObjectURL(localUrl);
      localUrl = URL.createObjectURL(file);
      portraitIn.value = ""; removeIn.value = "";
      attemptsRow.querySelectorAll(".cp-attempt").forEach((b) => b.setAttribute("aria-pressed", "false"));
      showPicture(localUrl, "Your photo");
    }
    fileIn.addEventListener("change", () => useFile(fileIn.files[0]));
    frame.addEventListener("dragover", (e) => { e.preventDefault(); frame.classList.add("dragover"); });
    frame.addEventListener("dragleave", () => frame.classList.remove("dragover"));
    frame.addEventListener("drop", (e) => {
      e.preventDefault(); frame.classList.remove("dragover");
      const file = e.dataTransfer.files[0];
      if (!file || drawing) return;
      const dt = new DataTransfer(); dt.items.add(file); fileIn.files = dt.files;
      useFile(file);
    });
    removeBtn.addEventListener("click", () => {
      fileIn.value = ""; portraitIn.value = ""; removeIn.value = editing ? "1" : "";
      attemptsRow.querySelectorAll(".cp-attempt").forEach((b) => b.setAttribute("aria-pressed", "false"));
      showPicture(null, null);
    });

    // AI portrait: Gemini draws from the Look fields; poll until it's ready
    drawBtn.addEventListener("click", async () => {
      showError(drawErr, "");
      const before = { url: frameImg.hidden ? null : frameImg.src, badge: badge.hidden ? null : badge.textContent };
      drawing = true; drawBtn.disabled = true; frame.dataset.state = "busy";
      document.getElementById("cp-busy-text").textContent = `Gemini is drawing ${f("name").value.trim() || "your character"}…`;
      const done = (msg) => {
        clearInterval(polling); drawing = false; syncPreview();
        if (msg) { showError(drawErr, msg); showPicture(before.url, before.badge); }
      };
      try {
        const p = await post("/portraits", { appearance: f("appearance").value, outfit: f("outfit").value,
          style: document.getElementById("cp-style").value });
        polling = setInterval(async () => {
          let st;
          try { st = await (await fetch(`/portraits/${p.id}`)).json(); } catch (_) { return; }
          if (st.status === "ready") { done(); addAttempt(st); choosePortrait(st); }
          else if (st.status === "failed") done(st.error || "Gemini couldn't draw the portrait.");
          else if (st.detail) done(st.detail);
        }, 3000);
      } catch (ex) { done(ex.message); }
    });

    charForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      showError(err, "");
      if (!f("name").value.trim()) { f("name").focus(); return showError(err, "Give the character a name."); }
      if (!f("appearance").value.trim()) { f("appearance").focus(); return showError(err, "Describe how the character looks."); }
      if (drawing) return showError(err, "Wait for the portrait to finish, or close and reopen the editor.");
      save.disabled = true;
      try {
        await post(editing ? `/characters/${editing}` : "/characters", new FormData(charForm));
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
        count.textContent = n ? `${n} spoken word${n === 1 ? "" : "s"}${n > data.max_words ? ` (about ${data.max_words} fit in ${data.seconds || 10} seconds)` : ""}` : "";
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
        el("header", {}, el("h2", { text: `Scene ${i + 1}` }), el("span", { class: "muted small", text: `${i * (data.seconds || 10)}–${(i + 1) * (data.seconds || 10)} s` }),
          editable && !data.failed && scenes.length > 1 ? el("button", { type: "button", class: "link", text: "Remove scene",
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
        // the finished clip, to watch it and remake just this scene (the others are kept)
        data.clips && data.clips[i] ? el("div", { class: "scene-clip" },
          el("video", { controls: true, preload: "metadata", playsinline: true,
            src: `/generations/${editor.dataset.id}/scenes/${i + 1}/clip` }),
          data.checks && data.checks[i] ? el("p", { class: "muted small", text: `Clip check: ${data.checks[i]}` }) : "",
          data.can_redo ? el("button", { type: "button", class: "secondary", text: "Remake this scene",
            onclick: async (e) => {
              if (!confirm(`Make scene ${i + 1} again? The other scenes are kept, and the video is joined again.`)) return;
              e.target.disabled = true;
              try { await post(`/generations/${editor.dataset.id}/scenes/${i + 1}/redo`); location.href = "/"; }
              catch (ex) { e.target.disabled = false; alert(ex.message); }
            } }) : "") : "",
        el("details", { class: "compiled" }, el("summary", { text: "Clip prompt" }), prompt));
    }
    function draw() { editor.replaceChildren(...scenes.map(card)); }
    draw();

    // Story bible: what every scene shares (app/script.py normalize_bible)
    const bible = { title: "", style: "", palette: "", setting: "", motif: "", characters: [], cast: [], ...data.bible };
    const genreIn = document.getElementById("script-genre");
    genreIn.value = data.genre;
    for (const k of ["title", "palette", "setting", "motif"]) {
      const input = document.getElementById(`bible-${k}`);
      input.value = bible[k] || "";
      input.addEventListener("input", () => { bible[k] = input.value; changed(); });
    }
    const bibleCast = document.getElementById("bible-characters");
    function drawBibleCast() {
      // Invented characters: their fixed look. Picked characters: what the prompt says, which wins over their
      // saved details field by field (app/script.py merged_sheet); an empty field keeps the saved one.
      bibleCast.replaceChildren(
        ...bible.characters.map((c) => el("div", { class: "grid2" },
          field(`${c.name}: look`, c.look, (v) => { c.look = v; }, { maxlength: 400 }),
          field(`${c.name}: never`, c.never, (v) => { c.never = v; }, { maxlength: 200, placeholder: "e.g. upright ears, brown fur" }))),
        ...bible.cast.map((c) => el("div", { class: "grid4" },
          ...[["age", 80], ["look", 300], ["outfit", 200], ["voice", 150]].map(([k, n]) =>
            field(`${c.name} (from your prompt): ${k}`, c[k], (v) => { c[k] = v; }, { maxlength: n, placeholder: "saved one" })))));
    }
    drawBibleCast();

    // Autosave (and refresh the clip prompts) shortly after each change
    const status = document.getElementById("script-status"), err = document.getElementById("script-error");
    let saveTimer = null, saving = Promise.resolve();
    const payload = (approve) => ({ scenes, style: styleIn.value, language: langIn.value, approve, bible, genre: genreIn.value });
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
      genreIn.addEventListener("change", changed);
      const addShot = document.getElementById("add-shot");
      if (addShot) addShot.addEventListener("click", () => {
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
