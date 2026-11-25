/* agentx app window. Events come from the agentx process over /events (server-sent events) and are drawn here;
   what you do (send, answer, stop, speak) goes back as small JSON posts. Nothing runs in the page itself. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const column = $("column"), log = $("log"), input = $("input"), palette = $("palette");

  // -- the key of this run: from the link (?t=...), then kept in the address after a # (never sent to the server,
  // but it goes along with "Open in Safari", a new tab, sharing to another browser or a bookmark) ------------
  const keyFrom = (text) => {
    const s = String(text || "").trim();
    const m = s.match(/[?#&]t=([^&#\s]+)/);
    return m ? decodeURIComponent(m[1]) : /^[\w-]{16,}$/.test(s) ? s : "";
  };
  let token = keyFrom(location.search) || keyFrom(location.hash);
  // drawn as a side of another agentx window (the split view): no sidebar of its own
  const EMBED = /[?#&]embed=1(?:&|$)/.test(location.search + location.hash);
  if (EMBED) document.documentElement.classList.add("embedded");
  // The window agentx opens has a one-time code instead (?c=...): another account on the computer could read the
  // key from the browser's command line. The page trades the code for the key once (claimLaunch, below).
  const launchCode = token ? "" : ((location.search.match(/[?&]c=([\w-]+)/) || [])[1] || "");
  try { token = token || (launchCode ? "" : sessionStorage.getItem("agentx-token")) || ""; } catch (_) { /* no storage */ }
  function keepKey() {
    try { sessionStorage.setItem("agentx-token", token); } catch (_) { /* no storage */ }
    if (token) history.replaceState(null, "", `/#t=${encodeURIComponent(token)}${EMBED ? "&embed=1" : ""}`);
  }
  keepKey();

  // -- this device: kept in the browser, so each device has its own conversation (a reload or a new tab keeps it) --
  const DEVICE = (() => {
    const fresh = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
    try {
      let id = localStorage.getItem("agentx-device");
      if (!id || !/^[A-Za-z0-9_-]{8,64}$/.test(id)) { id = fresh(); localStorage.setItem("agentx-device", id); }
      return id;
    } catch (_) { return fresh(); }
  })();

  // -- the conversation this window shows: the device's own, or one it runs beside it (a lane, l1, l2...) --------
  // Clicking another conversation while one works opens it in a lane and shows it; the first goes on working and
  // Recent marks it. The lane is kept for this tab only, so a reload shows the same conversation.
  const LANE = (() => {
    if (EMBED) return "";
    try { const lane = sessionStorage.getItem("agentx-lane") || ""; return /^l[0-9]{1,4}$/.test(lane) ? lane : ""; }
    catch (_) { return ""; }
  })();
  function showLane(lane) {
    try { if (lane) sessionStorage.setItem("agentx-lane", lane); else sessionStorage.removeItem("agentx-lane"); }
    catch (_) { /* no storage: the window shows the device's own conversation */ }
    // the page held here is not on screen after the reload (before the Browser tab is set up, nothing is held)
    try { giveBackHeld(); } catch (_) { /* nothing to give back */ }
    location.reload();
  }

  async function post(path, body) {
    const res = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Agentx-Token": token,
                                                             "X-Agentx-Device": DEVICE, "X-Agentx-Lane": LANE},
                                   body: JSON.stringify(body || {})});
    return res.json().catch(() => ({}));
  }
  const authHeaders = () => ({"X-Agentx-Token": token, "X-Agentx-Device": DEVICE, "X-Agentx-Lane": LANE});
  async function get(path) {
    const res = await fetch(path, {headers: authHeaders()});
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

  // -- small helpers ------------------------------------------------------------------------------------------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
  function nearBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 160; }
  function keepBottom(stick) { if (stick) log.scrollTop = log.scrollHeight; }
  function shortTime(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ${String(sec % 60).padStart(2, "0")}s`;
    return `${Math.floor(sec / 3600)}h ${String(Math.floor(sec % 3600 / 60)).padStart(2, "0")}m`;
  }

  // -- markdown, for the agent's answers (escaped first, so nothing in an answer can become live HTML) -----------
  // Each link is built once and put aside (like the code spans) before the next pass, so no pass ever looks inside
  // an href already made: model text could otherwise end an href early and add its own attributes (a style).
  // A link's address stops at a space, ( ) < > and a quote (escaped by then as &quot; or &#39;).
  const URL_PART = String.raw`https?:\/\/(?:(?!&quot;|&#39;)[^\s()<>"'\u0000\u0001])+`;
  const LINK = new RegExp(String.raw`\[([^\]]+)\]\((${URL_PART})\)`, "g"), BARE = new RegExp(String.raw`(^|[\s(])(${URL_PART})`, "g");
  function inline(text) {
    let s = esc(text).replace(/[\u0000\u0001]/g, "");
    const codes = [], links = [];
    const link = (url, label) => { links.push(`<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`); return `\u0001${links.length - 1}\u0001`; };
    s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>").replace(/(^|[\s(])_([^_\s][^_]*)_/g, "$1<em>$2</em>");
    s = s.replace(LINK, (_, label, url) => link(url, label));
    s = s.replace(BARE, (_, before, url) => before + link(url, url));
    s = s.replace(/\u0001(\d+)\u0001/g, (_, i) => links[+i]);
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
  }
  function markdown(src) {
    const lines = String(src ?? "").replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let i = 0;
    const listOf = (re) => {
      const ordered = re === OL, items = [];
      while (i < lines.length && re.test(lines[i])) {
        let item = lines[i].replace(re, "");
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !UL.test(lines[i].trim() ? lines[i] : "x") ) { item += " " + lines[i].trim(); i++; }
        items.push(`<li>${inline(item)}</li>`);
      }
      return `<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`;
    };
    const UL = /^\s*[-*+]\s+/, OL = /^\s*\d+[.)]\s+/;
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^\s*(```|~~~)\s*([\w+-]*)/);
      if (fence) {
        const body = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) { body.push(lines[i]); i++; }
        i++;
        out.push(`<pre><code>${esc(body.join("\n"))}</code></pre>`);
        continue;
      }
      if (!line.trim()) { i++; continue; }
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { const n = Math.min(4, h[1].length); out.push(`<h${n}>${inline(h[2])}</h${n}>`); i++; continue; }
      if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) { out.push("<hr>"); i++; continue; }
      if (/^\s*>/.test(line)) {
        const body = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) { body.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
        out.push(`<blockquote>${markdown(body.join("\n"))}</blockquote>`);
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
        out.push(`<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>` +
                 rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("") + "</tbody></table>");
        continue;
      }
      if (UL.test(line)) { out.push(listOf(UL)); continue; }
      if (OL.test(line)) { out.push(listOf(OL)); continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*```|\s*~~~|\s*>)/.test(lines[i]) && !UL.test(lines[i]) && !OL.test(lines[i])) {
        para.push(lines[i]); i++;
      }
      out.push(`<p>${inline(para.join("\n")).replace(/\n/g, "<br>")}</p>`);
    }
    return out.join("");
  }

  // -- ANSI colours from the app's console, for what it prints as terminal text ----------------------------------
  const BASIC = ["#3b3f46", "#ff7a7a", "#9fe08a", "#f2c14e", "#7cc7ff", "#d7a6ff", "#6fe3e3", "#d6d8dc"];
  const BRIGHT = ["#7a808a", "#ff9a9a", "#b8ff3e", "#ffd76e", "#a6dbff", "#e6c4ff", "#9ff0f0", "#ffffff"];
  function c256(n) {
    if (n < 8) return BASIC[n];
    if (n < 16) return BRIGHT[n - 8];
    if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
    n -= 16;
    const f = (x) => (x ? 55 + x * 40 : 0);
    return `rgb(${f(Math.floor(n / 36))},${f(Math.floor(n / 6) % 6)},${f(n % 6)})`;
  }
  function ansi(text) {
    let st = {}, out = "", open = false;
    const parts = String(text).split(/\x1b\[([0-9;]*)m/);
    for (let k = 0; k < parts.length; k++) {
      if (k % 2 === 0) {
        const chunk = parts[k].replace(/\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07/g, "");
        if (!chunk) continue;
        const cls = [st.b && "b", st.d && "d", st.i && "i", st.u && "u"].filter(Boolean).join(" ");
        const css = [st.fg && `color:${st.fg}`, st.bg && `background:${st.bg}`].filter(Boolean).join(";");
        out += cls || css ? `<span${cls ? ` class="${cls}"` : ""}${css ? ` style="${css}"` : ""}>${esc(chunk)}</span>` : esc(chunk);
        continue;
      }
      const codes = parts[k] === "" ? [0] : parts[k].split(";").map(Number);
      for (let j = 0; j < codes.length; j++) {
        const c = codes[j];
        if (c === 0) st = {};
        else if (c === 1) st.b = 1; else if (c === 2) st.d = 1; else if (c === 3) st.i = 1; else if (c === 4) st.u = 1;
        else if (c === 22) { st.b = 0; st.d = 0; } else if (c === 23) st.i = 0; else if (c === 24) st.u = 0;
        else if (c >= 30 && c <= 37) st.fg = BASIC[c - 30]; else if (c >= 90 && c <= 97) st.fg = BRIGHT[c - 90];
        else if (c === 39) st.fg = null; else if (c >= 40 && c <= 47) st.bg = BASIC[c - 40];
        else if (c >= 100 && c <= 107) st.bg = BRIGHT[c - 100]; else if (c === 49) st.bg = null;
        else if ((c === 38 || c === 48) && codes[j + 1] === 2) { const v = `rgb(${codes[j + 2]},${codes[j + 3]},${codes[j + 4]})`; if (c === 38) st.fg = v; else st.bg = v; j += 4; }
        else if ((c === 38 || c === 48) && codes[j + 1] === 5) { const v = c256(codes[j + 2]); if (c === 38) st.fg = v; else st.bg = v; j += 2; }
      }
    }
    return out;
  }

  // -- the conversation -----------------------------------------------------------------------------------------
  let turn = null, group = null, pending = [], lastPrint = null, welcome = null;
  const cards = new Map();

  function agentTurn() {
    if (!turn || column.lastElementChild !== turn) { turn = el("div", "agent"); column.append(turn); group = null; }
    return turn;
  }
  function dropWelcome() { const w = column.querySelector(".welcome"); if (w) { w.remove(); syncView(); } }
  const STARTERS = [
    ["Explain how this project fits together", "Explain how this project fits together"],
    ["Find a bug and fix it", "Find a bug in this project and fix it"],
    ["Write notes for agentx <code>/init</code>", "/init"],
  ];
  let folderName = "";
  // The opening (as on the docs site): the mark, then "agentx" typed letter by letter, then a blinking cursor.
  let heroShown = false;                       // it plays once per page; later welcomes show it still
  const HERO = () => '<div class="hero-name" aria-label="agentx">' +
    "agentx".split("").map((c, n) => `<span class="glyph" aria-hidden="true" style="animation-delay:${200 + n * 45}ms">${c}</span>`).join("") +
    '<span class="hero-cursor" aria-hidden="true">|</span></div>';
  // the orb on the opening, large, under the question (the model and the mode are already on the box below)
  const HERO_ORB = (delay) => `<div class="hero-orb hero-in" style="animation-delay:${delay}ms">` +
    `<canvas class="orb" data-size="${window.innerHeight < 860 ? 150 : 184}" aria-hidden="true"></canvas></div>`;
  function showStarting() {                    // before agentx has said hello: the opening and "Starting..."
    if (column.children.length) return;
    const s = el("section", "welcome splash");
    s.innerHTML = HERO() + '<p class="starting hero-in" style="animation-delay:600ms" role="status">Starting' +
      '<span class="tick">.</span><span class="tick">.</span><span class="tick">.</span></p>' + HERO_ORB(700);
    heroShown = true;
    column.append(s);
    mountOrbs(s, () => "thinking", {interactive: true});
    syncView();
  }
  function showWelcome() {
    const splash = column.querySelector(".welcome.splash");
    if (splash) splash.remove();                 // the welcome grows out of the starting screen
    if (startView && startView.show) { showStartIfAsked(); return; }
    if (!welcome || column.children.length) return;
    const w = el("section", heroShown ? "welcome still" : "welcome");
    const tips = (welcome.tips || []).map((t) => `<li>${esc(t)}</li>`).join("");
    const news = (welcome.news || []).map((t) => `<li>${esc(t)}</li>`).join("");
    const where = folderName || welcome.folder || "";
    const late = heroShown ? 0 : 520;            // after the name has typed itself, the first time
    w.innerHTML = `${HERO()}<h1 class="hero-in" style="animation-delay:${late}ms">What should we work on${where ? ` in ${esc(where)}` : ""}?</h1>
      ${HERO_ORB(heroShown && !late ? 0 : late + 100)}
      <div class="starters hero-in" style="animation-delay:${late + 160}ms">${STARTERS.map(([label], n) => `<button type="button" class="starter" data-starter="${n}">${label}</button>`).join("")}</div>
      ${tips || news ? `<details class="hero-in" style="animation-delay:${late + 240}ms"><summary>Tips and what's new</summary>${tips ? `<ul>${tips}</ul>` : ""}` +
        `${news ? `<h2>What's new</h2><ul>${news}</ul>` : ""}</details>` : ""}`;
    heroShown = true;
    w.addEventListener("click", (e) => {
      const b = e.target.closest("[data-starter]");
      if (!b) return;
      input.value = STARTERS[Number(b.dataset.starter)][1];
      grow(); setComposer(); input.focus();
    });
    column.append(w);
    if (modeNote && !column.querySelector(":scope > .notice-chip-row")) column.append(modeChip());   // a new conversation
    mountOrbs(w, orbState, {interactive: true});   // once on the page, so it reads the theme's lime
    syncView();
  }

  // -- the start screen: agentx started outside a project (the home folder, a drive root) ------------------------
  // The server decides (GET /api/start) and keeps the choice, so a reload shows it again until one is made. A folder
  // picked here gets agentx of its own, in a new window, and this one ends; "Chat here anyway" shows the welcome.
  let startView = null;
  async function loadStart() {
    if (EMBED || LANE) return;                   // only the main window, at start
    try { startView = await get("/api/start"); } catch (_) { startView = null; }
  }
  const FOLDER_EXAMPLE = "for example C:\\Users\\you\\projects\\shop";
  function middle(text, room) {                  // a long path keeps its start and its end
    const s = String(text || "");
    if (s.length <= room) return s;
    const head = Math.ceil((room - 1) * 0.45);
    return s.slice(0, head) + "…" + s.slice(s.length - (room - 1 - head));
  }
  function showStartIfAsked() {                 // the server holds the app until a choice: nothing else comes first
    if (!(startView && startView.show) || column.querySelector(".start-screen")) return;
    const splash = column.querySelector(".welcome.splash");
    if (splash) splash.remove();
    showStart();
  }
  function showStart() {
    const s = el("section", "start-screen");
    s.setAttribute("aria-labelledby", "startTitle");
    const h = el("h1"); h.id = "startTitle"; h.textContent = "Choose a folder to work in";
    const why = el("p", "start-why"); why.textContent = startView.line || "";
    s.append(h, why);
    const recent = Array.isArray(startView.recent) ? startView.recent : [];
    if (recent.length) {
      const list = el("ul", "start-list");
      list.setAttribute("aria-label", "Recent folders");
      for (const f of recent) {
        const li = el("li");
        const b = el("button", "start-row");
        b.type = "button";
        b.title = String(f.path || "");
        b.dataset.path = String(f.path || "");
        const name = el("span", "start-name"); name.textContent = String(f.name || f.path || "");
        const where = el("span", "start-path"); where.dataset.full = String(f.path || "");
        const ago = el("span", "start-ago"); ago.textContent = String(f.ago || "");
        b.append(name, where, ago);
        li.append(b);
        list.append(li);
      }
      s.append(list);
    } else {
      const none = el("p", "start-none");
      none.textContent = "No recent folders yet. Open one to start.";
      s.append(none);
    }
    const acts = el("div", "start-actions");
    const open = el("button", "btn primary"); open.type = "button"; open.dataset.start = "open"; open.textContent = "Open a folder…";
    const here = el("button", "btn quiet"); here.type = "button"; here.dataset.start = "here"; here.textContent = "Chat here anyway";
    acts.append(open, here);
    const msg = el("p", "start-msg"); msg.setAttribute("role", "status");
    s.append(acts, msg);
    s.addEventListener("click", async (e) => {
      const row = e.target.closest(".start-row"), act = e.target.closest("[data-start]");
      if (row) chooseFolder(row.dataset.path, s);
      else if (act && act.dataset.start === "open") {
        const path = await askText({title: "Open a folder", text: "agentx starts there in a window of its own, and this one closes.",
                                    placeholder: FOLDER_EXAMPLE, ok: "Open"});
        if (path) chooseFolder(path, s);
      } else if (act && act.dataset.start === "here") chatHere(s);
    });
    column.append(s);
    syncView();
    fitPaths();
    const first = s.querySelector(".start-row, [data-start]");
    if (first) first.focus({preventScroll: true});
  }
  function fitPaths() {                         // each path as long as its row has room for, cut in the middle
    for (const n of column.querySelectorAll(".start-screen .start-path")) {
      const full = n.dataset.full || "";
      let room = full.length;
      n.textContent = full;
      while (room > 12 && n.scrollWidth > n.clientWidth) { room -= 2; n.textContent = middle(full, room); }
    }
  }
  let fitLater = 0;
  window.addEventListener("resize", () => { clearTimeout(fitLater); fitLater = setTimeout(fitPaths, 80); });
  async function chooseFolder(path, s) {
    const msg = s.querySelector(".start-msg");
    const buttons = [...s.querySelectorAll("button")];
    buttons.forEach((b) => { b.disabled = true; });
    msg.textContent = "";
    let res = {};
    try { res = await post("/api/start", {action: "open", path}); } catch (_) { res = {}; }
    if (res.ok) { showHandedOff(res.message || ""); return; }
    buttons.forEach((b) => { b.disabled = false; });
    msg.textContent = res.message || "The folder could not be opened.";
  }
  async function chatHere(s) {
    try { await post("/api/start", {action: "here"}); } catch (_) { /* the welcome shows either way */ }
    if (startView) startView.show = false;
    const rest = [...column.children].filter((n) => n !== s);   // what came meanwhile stays, under the welcome
    rest.forEach((n) => n.remove());
    s.remove();
    if (welcome) showWelcome();                  // agentx starts here now: its welcome (or trust question) follows
    else showStarting();
    rest.forEach((n) => column.append(n));
    syncView();
    input.focus();
  }
  function showHandedOff(text) {
    closed = true;
    const box = $("overlayBox");
    box.innerHTML = "";
    const h = el("h2"); h.textContent = "Opened in a new window";
    const p = el("p"); p.textContent = text;
    box.append(h, p);
    $("overlay").hidden = false;
  }

  function addUser(text) {
    dropWelcome();
    const start = column.querySelector(".start-screen");
    if (start) { start.remove(); if (startView) startView.show = false; }
    turn = null; group = null; lastPrint = null;
    const command = /^\/\w/.test(text);              // a command (often from a menu) is not a chat message
    const u = el("div", command ? "user command" : "user");
    u.textContent = text;
    if (!command && !EMBED) {                        // hover: Edit, to change it and send it again
      const b = el("button", "u-edit");
      b.type = "button";
      b.title = "Edit this message and send it again";
      b.setAttribute("aria-label", "Edit this message");
      b.innerHTML = `<svg viewBox="0 0 16 16" aria-hidden="true">${ICONS.edit}</svg>`;
      u.append(b);
    }
    // once you write, the orb takes the top: the mode note goes (Details keeps it)
    for (const chip of column.querySelectorAll(":scope > .notice-chip-row")) chip.remove();
    column.append(u);
    if (!command && !$("topTitle").dataset.fixed) $("topTitle").textContent = text.split("\n")[0].slice(0, 90) || "Conversation";
  }

  // each tool as a plain verb and a small icon
  const ICONS = {
    read: '<path d="M4 2.5h5l3 3v8H4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M9 2.5v3h3M6 8.5h4M6 11h3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" fill="none"/>',
    search: '<circle cx="7" cy="7" r="4" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="m10 10 3.5 3.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
    folder: '<path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.6l1.4 1.5h5A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
    edit: '<path d="M10.5 2.8 13.2 5.5 6 12.7H3.3V10z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
    write: '<path d="M4 2.5h5l3 3v8H4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M8 7.5v4M6 9.5h4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
    run: '<rect x="2" y="3" width="12" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="m5 6.5 2 1.5-2 1.5M8.5 10H11" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>',
    web: '<circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M2.5 8h11M8 2.5c1.8 2 1.8 9 0 11M8 2.5c-1.8 2-1.8 9 0 11" fill="none" stroke="currentColor" stroke-width="1.3"/>',
    remove: '<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>',
    move: '<path d="M3 8h10M10 5l3 3-3 3" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>',
    skill: '<path d="M8 2.5 9.4 6.6 13.5 8 9.4 9.4 8 13.5 6.6 9.4 2.5 8 6.6 6.6z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
    note: '<path d="M4.5 2.5h7v11L8 11l-3.5 2.5z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
    other: '<circle cx="8" cy="8" r="2.5" fill="currentColor"/>',
    explore: '<path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
  };
  const TOOLS = {
    read_path: ["Read", "read"], grep: ["Search", "search"], find_files: ["Find files", "search"], list_directory: ["List", "folder"],
    edit_file: ["Edit", "edit"], write_file: ["Write", "write"], delete_path: ["Delete", "remove"], move_file: ["Move", "move"],
    run_shell: ["Run", "run"], run_python: ["Run Python", "run"], web_search: ["Search the web", "web"], web_fetch: ["Fetch", "web"],
    recall: ["Recall notes", "note"], use_skill: ["Use skill", "skill"],
    start_background: ["Start in the background", "run"], send_to_terminal: ["Type in terminal", "run"],
    stop_other_task: ["Stop task", "run"], stop_background: ["Stop task", "run"], list_background: ["Tasks", "run"],
    background_output: ["Task output", "run"], browser_open: ["Open page", "web"], browser_click: ["Click", "web"],
    browser_type: ["Type", "web"], browser_press: ["Press", "web"], browser_back: ["Go back", "web"],
    browser_read: ["Read page", "web"], browser_screenshot: ["Screenshot", "web"], browser_console: ["Console", "web"],
  };
  const icon = (kind, cls) => `<svg class="${cls || ""}" viewBox="0 0 16 16" aria-hidden="true">${ICONS[kind] || ICONS.other}</svg>`;
  function toolLook(name) {
    const known = TOOLS[name];
    if (known) return known;
    const words = String(name || "tool").replace(/^mcp__/, "").replace(/__/g, " · ").replace(/_/g, " ");
    return [words.charAt(0).toUpperCase() + words.slice(1), "other"];
  }
  const DOING = {read_path: "Reading", grep: "Searching", find_files: "Finding files", list_directory: "Listing a folder",
                 edit_file: "Editing", write_file: "Writing a file", delete_path: "Deleting", move_file: "Moving a file",
                 run_shell: "Running a command", run_python: "Running Python", web_search: "Searching the web",
                 web_fetch: "Fetching a page", recall: "Recalling notes", use_skill: "Using a skill"};
  function friendlyLabel(label) {
    const text = String(label || "Working").replace(/\.+$|…$/, "");
    const m = text.match(/^Running (\w+)(.*)$/);
    return m && DOING[m[1]] ? DOING[m[1]] + m[2] : text;
  }
  function makeStep(ev) {
    const s = el("div", "step running");
    s.dataset.name = ev.name;
    const [verb, kind] = toolLook(ev.name);
    s.innerHTML = `<div class="step-head" title="${esc(ev.name)}"><span class="step-icon">${icon(kind)}</span>` +
      `<span class="step-verb">${esc(verb)}</span><span class="step-subject">${esc(ev.subject || "")}</span></div>`;
    return s;
  }
  function groupSummary(g) {
    const steps = g.querySelectorAll(".inner .step");
    const counts = {};
    steps.forEach((s) => { const n = s.dataset.name; counts[n] = (counts[n] || 0) + 1; });
    const words = {read_path: ["file", "files"], grep: ["search", "searches"], find_files: ["search", "searches"],
                   list_directory: ["folder", "folders"], web_search: ["web search", "web searches"], web_fetch: ["page", "pages"],
                   recall: ["note search", "note searches"], use_skill: ["skill", "skills"]};
    const parts = Object.entries(counts).map(([n, c]) => `${c} ${(words[n] || [n, n])[c > 1 ? 1 : 0]}`);
    g.querySelector(".step-subject").textContent = parts.join(", ");
    const running = g.querySelector(".inner .step.running");
    const failed = g.querySelector(".inner .step.failed");
    g.className = `step group ${running ? "running" : failed ? "failed" : "done"}`;
  }
  function toolCall(ev) {
    lastPrint = null;
    const step = makeStep(ev);
    const here = agentTurn();                      // first: a new turn resets `group`
    if (ev.look) {
      if (!group || !group.isConnected || group.parentElement !== here) {
        group = el("details", "step group running");
        group.innerHTML = `<summary class="step-head"><span class="step-icon">${icon("explore", "chev")}</span>` +
          `<span class="step-verb">Explored</span><span class="step-subject"></span></summary><div class="inner"></div>`;
        here.append(group);
      }
      group.querySelector(".inner").append(step);
      pending.push(step);
      groupSummary(group);
    } else {
      group = null;
      agentTurn().append(step);
      pending.push(step);
    }
  }
  function toolResult(ev) {
    let k = pending.findIndex((s) => s.dataset.name === ev.name);
    if (k < 0) k = pending.length - 1;
    const step = k >= 0 ? pending.splice(k, 1)[0] : null;
    if (!step) return;
    step.classList.remove("running");
    step.classList.add(ev.failed ? "failed" : "done");
    const text = ev.failed ? (ev.detail || ev.summary || "failed") + (ev.more ? `\n... ${ev.more} more lines` : "") : ev.summary;
    if (text) step.append(el("div", "step-result", ev.failed ? ansi(text) : esc(text)));
    if (ev.output) addOutput(step, ev.output, ev.outputMore);
    const g = step.closest(".group");
    if (g) groupSummary(g);
  }
  // a step you can open to see what the tool gave back
  function addOutput(step, output, more) {
    const head = step.querySelector(":scope > .step-head");
    if (!head) return;
    step.classList.add("openable");
    head.setAttribute("role", "button");
    head.tabIndex = 0;
    head.setAttribute("aria-expanded", "false");
    head.insertAdjacentHTML("beforeend", `<svg class="step-more" viewBox="0 0 16 16" aria-hidden="true">${ICONS.explore}</svg>`);
    const box = el("pre", "step-output", ansi(output) + (more > 0 ? `\n<span class="d">… ${more} more line${more > 1 ? "s" : ""}</span>` : ""));
    box.hidden = true;
    step.append(box);
    const toggle = () => {
      box.hidden = !box.hidden;
      step.classList.toggle("open", !box.hidden);
      head.setAttribute("aria-expanded", String(!box.hidden));
    };
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  }
  // what the model says it is doing between steps: the newest one open, older ones folded to their first line
  function addThought(text) {
    if (/^\s*(thinking|thought|working)?\s*(\.{3}|…)?\s*$/i.test(String(text || ""))) return;   // filler, nothing said
    const here = agentTurn();
    group = null;
    column.querySelectorAll("details.thought[open]").forEach((d) => { d.open = false; });
    const t = el("details", "thought");
    t.open = true;
    const first = String(text || "").trim().split("\n")[0].slice(0, 160);
    t.innerHTML = `<summary><span class="thought-label">Thinking</span><span class="thought-peek">${esc(first)}</span></summary>` +
      `<div class="thought-body">${markdown(text)}</div>`;
    here.append(t);
  }
  // a live "Thinking…" line while the model works and no step is running yet
  function updateLive() {
    if (window.AgentxOrb && $("stage")) drawStage();
    let live = column.querySelector(".thinking-now");
    const show = !!status.working && !pending.length && !liveCard() && !column.querySelector(".welcome");
    if (!show) { if (live) live.remove(); return; }
    if (!live) { live = el("div", "thinking-now", '<canvas class="orb" data-size="16" aria-hidden="true"></canvas><span class="thinking-now-label">Thinking…</span>'); mountOrbs(live, orbState); }
    if (column.lastElementChild !== live) column.append(live);
  }

  // the answer, with a copy button for it and for each code block
  const COPY = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10.5 3.5v-.5A1.5 1.5 0 0 0 9 1.5H4A1.5 1.5 0 0 0 2.5 3v5A1.5 1.5 0 0 0 4 9.5h.5" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>';
  const DONE = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  function copyButton(getText, label) {
    const b = el("button", "copy", COPY + (label ? `<span>${esc(label)}</span>` : ""));
    b.type = "button";
    b.setAttribute("aria-label", "Copy");
    b.title = "Copy";
    b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(getText()); } catch (_) { return; }
      b.innerHTML = DONE + (label ? "<span>Copied</span>" : "");
      setTimeout(() => { b.innerHTML = COPY + (label ? `<span>${esc(label)}</span>` : ""); }, 1500);
    });
    return b;
  }
  function addAnswer(text) {
    const a = el("div", "answer", markdown(text));
    a.querySelectorAll("pre").forEach((pre) => {
      const wrap = el("div", "code");
      pre.replaceWith(wrap);
      wrap.append(pre, copyButton(() => pre.innerText));
    });
    const tools = el("div", "answer-tools");
    tools.append(copyButton(() => String(text), "Copy"));
    agentTurn().append(a, tools);
  }
  function addPrint(ev) {
    if (!ev.ansi || !ev.ansi.trim()) return;
    if (lastPrint && column.lastElementChild === lastPrint) { lastPrint.innerHTML += "\n" + ansi(ev.ansi); return; }
    lastPrint = el("pre", "term", ansi(ev.ansi));
    turn = null;
    column.append(lastPrint);
  }
  // The conversation keeps room for the work: the mode note ("Auto mode is on.") is a small chip whose full text
  // shows on hover and in Details, and MCP servers that need signing in are listed in Details (a dot on its button).
  let modeNote = null;
  function addNotice(ev) {
    lastPrint = null; turn = null;
    const flush = (s) => String(s || "").replace(/^[ \t]+/gm, "").trim();      // the terminal's indent is not needed here
    if (/MCP servers? needs? sign-in:/i.test(String(ev.text || ""))) {
      $("detailsBtn").classList.add("attention");
      if (detailsOpen) loadDetails();
      return;
    }
    // "Sign in to X in your browser. If it did not open, go to:" and the address, in one notice or as two
    const said = flush(ev.text);
    const signin = said.match(/^Sign in to (.+?) in your browser\. If it did not open, go to:\s*(https?:\/\/\S+)?\s*$/s);
    if (signin) {
      if (signin[2]) localSigninCard(signin[1], signin[2]); else { signinFor = signin[1]; signingIn = signin[1]; drawStage(); }
      return;
    }
    if (signinFor && /^https?:\/\/\S+$/.test(said)) { const who = signinFor; signinFor = ""; localSigninCard(who, said); return; }
    signinFor = "";
    if (ev.level === "callout") {
      modeNote = {title: flush(ev.text), body: flush(ev.hint)};
      for (const old of column.querySelectorAll(":scope > .notice-chip-row")) old.remove();   // the latest mode only
      if (!column.querySelector(":scope > .user")) column.append(modeChip());   // a conversation you wrote in: Details keeps it
      if (detailsOpen) loadDetails();
      return;
    }
    const n = el("div", `notice ${ev.level || "info"}`);
    n.innerHTML = `<span>${esc(flush(ev.text))}${ev.hint ? ` <span class="hint">${esc(flush(ev.hint))}</span>` : ""}</span>`;
    const actions = (Array.isArray(ev.actions) ? ev.actions : []).filter((a) => a && a.label && a.send);
    if (actions.length) {                 // a question with its answers (the fallback offer): each sends its text
      const row = el("span", "notice-actions");
      for (const a of actions) {
        const b = el("button", "btn small");
        b.type = "button";
        b.textContent = a.label;
        b.addEventListener("click", () => {
          row.querySelectorAll("button").forEach((x) => { x.disabled = true; });
          b.classList.add("chosen");
          post("/api/send", {text: a.send});
        });
        row.append(b);
      }
      n.firstElementChild.append(row);
    }
    column.append(n);
  }
  // the mode note as a small chip: hover to read it, click to open Details at the mode; a new conversation shows it
  // again, and it goes once you write
  function modeChip() {
    const row = el("div", "notice-chip-row");
    const chip = el("button", "notice-chip", `<span class="chip-dot" aria-hidden="true"></span>${esc(modeNote.title)}` +
      `<span class="chip-pop" role="tooltip">${esc(modeNote.body)}<em>Click for details</em></span>`);
    chip.type = "button";
    const place = () => {                          // the card opens above the chip when there is no room below
      const pop = chip.querySelector(".chip-pop");
      chip.classList.toggle("up", chip.getBoundingClientRect().bottom + (pop.offsetHeight || 96) + 16 > log.getBoundingClientRect().bottom);
    };
    chip.addEventListener("mouseenter", place);
    chip.addEventListener("focus", place);
    chip.addEventListener("click", () => {
      setDetails(true);
      setTimeout(() => { const s = $("detailsBody").querySelector(".d-mode"); if (s) s.scrollIntoView({block: "nearest"}); }, 120);
    });
    row.append(chip);
    return row;
  }
  function addEcho(ev) {
    lastPrint = null; turn = null;
    const e = el("div", `echo${ev.done ? "" : " failed"}`);
    const long = String(ev.text || "").length > 400 || String(ev.text || "").split("\n").length > 6;
    e.innerHTML = `<span class="echo-text${long ? " long" : ""}"${long ? ' title="Click to show all"' : ""}>${esc(ev.text)}</span><span class="echo-out">${ev.done ? "✓" : "✗"} ${esc(ev.outcome)}</span>`;
    if (long) e.querySelector(".echo-text").addEventListener("click", (c) => c.currentTarget.classList.add("open"));
    column.append(e);
  }
  // A team job, live in the conversation: one block per job, drawn like the terminal's dark block. Each row is
  // updated in place and keeps its height: one line while it waits or runs (what it does now, its tool count and
  // time), two once it ended (the terminal's ended line and the start of its report). Click a row for its details.
  // A big team scrolls inside the block, the running steps kept in view; after the job the block is its record.
  const teamBlocks = new Map();                    // job -> its block
  const teamOpen = new Set();                      // "job row" of the rows whose details are open
  const TEAM_TONES = ["green", "red", "yellow", "dim"];
  const TEAM_STATES = ["waiting", "running", "done", "failed", "skipped", "interrupted", "unknown"];
  function teamLine(r) {
    const who = `${r.helper || ""} · ${r.label || r.helper || ""}`;
    if (r.mark) return {mark: r.mark, who, rest: `${r.word || ""}${r.stats ? ` (${r.stats})` : ""}`,
                        tone: TEAM_TONES.includes(r.tone) ? r.tone : "dim"};
    if (r.state === "running") {
      const doing = r.inFlight && r.action ? `${DOING[r.action] || toolLook(r.action)[0]}${r.path ? ` ${r.path}` : ""}`
        : r.activity || "starting";
      const tools = Number(r.toolUses || 0);
      return {mark: "●", who, rest: [doing, tools ? `${tools} tool${tools === 1 ? "" : "s"}` : "", r.time].filter(Boolean).join(" · "), tone: "run"};
    }
    return {mark: "○", who, rest: "waiting", tone: "wait"};
  }
  function teamDetail(r) {
    const out = [];
    if (r.model) out.push(`<div class="d">on ${esc(r.model)}</div>`);
    if (r.inFlight && r.action) out.push(`<div><span class="b">Now · ${esc(DOING[r.action] || toolLook(r.action)[0])}</span>${r.path ? ` ${esc(r.path)}` : ""}</div>`);
    for (const a of r.actions || []) {
      out.push(`<div class="${a.failed ? "tb-failed" : ""}"><span class="b">${esc(toolLook(a.name)[0])}</span>${a.subject ? ` ${esc(a.subject)}` : ""}` +
        `${a.summary ? `<span class="d"> ⎿ ${esc(a.summary)}</span>` : ""}</div>`);
    }
    const counts = [r.activity, r.stats].filter(Boolean).join(" · ");
    if (counts) out.push(`<div class="d">${esc(counts)}</div>`);
    if (r.fallback) out.push(`<div class="tb-fallback">↺ on ${esc(r.fallback)} (fallback${r.fallbackFrom || r.fallbackReason ? `: ${esc([r.fallbackFrom, r.fallbackReason].filter(Boolean).join(" "))}` : ""})</div>`);
    return out.join("") || `<div class="d">Nothing yet.</div>`;
  }
  function setText(node, text) { if (node.textContent !== text) node.textContent = text; }
  function teamRow() {
    const n = el("div", "tb-row", '<div class="tb-line"><span class="tb-mark"></span> <span class="tb-who b"></span>  <span class="tb-rest"></span></div>');
    n.tabIndex = 0;
    n.setAttribute("role", "button");
    n.setAttribute("aria-expanded", "false");
    return n;
  }
  function toggleTeamRow(block, row) {
    const key = `${block.dataset.job} ${row.dataset.row}`;
    if (teamOpen.has(key)) teamOpen.delete(key); else teamOpen.add(key);
    drawTeamRow(block, row, row._data);
  }
  function drawTeamRow(block, node, r) {
    node._data = r;
    const state = TEAM_STATES.includes(r.state) ? r.state : "unknown";
    const look = teamLine(r);
    const cls = `tb-row is-${state} tone-${look.tone}`;
    if (node.className !== cls) node.className = cls;
    setText(node.querySelector(".tb-mark"), look.mark);
    setText(node.querySelector(".tb-who"), look.who);
    setText(node.querySelector(".tb-rest"), look.rest);
    node.title = look.rest;
    let report = node.querySelector(".tb-report");
    if (r.mark && r.report) {
      if (!report) { report = el("div", "tb-report"); node.querySelector(".tb-line").after(report); }
      setText(report, `  ⎿ ${r.report}`);
    } else if (report) report.remove();
    const open = teamOpen.has(`${block.dataset.job} ${r.id}`);
    node.setAttribute("aria-expanded", String(open));
    let detail = node.querySelector(".tb-detail");
    if (open) {
      if (!detail) { detail = el("div", "tb-detail"); node.append(detail); }
      const html = teamDetail(r);
      if (detail._html !== html) { detail.innerHTML = html; detail._html = html; }
    } else if (detail) detail.remove();
  }
  function teamBlock(team, live) {
    const rows = team && Array.isArray(team.rows) ? team.rows.filter((r) => r && typeof r === "object") : [];
    if (!rows.length) return;
    const job = String(team.job || "live");
    const now = rows.filter((r) => r.state === "running").length;
    const going = now || rows.some((r) => r.state === "waiting");
    let block = teamBlocks.get(job);
    if (!block || !block.isConnected) {
      if (live && !going) return;                 // a job that ended is drawn from the conversation's history
      block = el("div", "term team-block", '<div class="tb-head"><span class="tb-title b">Team</span><span class="tb-counts d"></span></div><div class="tb-rows"></div>');
      block.dataset.job = job;
      block.setAttribute("role", "group");
      block.setAttribute("aria-label", "Team job");
      block.addEventListener("click", (e) => {
        const row = e.target.closest(".tb-row");
        if (row && !e.target.closest(".tb-detail")) toggleTeamRow(block, row);
      });
      block.addEventListener("keydown", (e) => {
        const row = e.target.closest(".tb-row");
        if (row && e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleTeamRow(block, row); }
      });
      const box = block.querySelector(".tb-rows");
      for (const kind of ["wheel", "touchstart", "pointerdown", "keydown"]) {
        box.addEventListener(kind, () => { block._touched = Date.now(); }, {passive: true});   // the reader scrolls it
      }
      lastPrint = null; turn = null; group = null;
      column.append(block);
      teamBlocks.set(job, block);
    }
    const done = Number(team.done || 0), total = Number(team.total || rows.length);
    setText(block.querySelector(".tb-counts"), going ? ` · ${now} running · ${done} of ${total} done` : ` · ${done} of ${total} done`);
    block.classList.toggle("live", !!going);
    const box = block.querySelector(".tb-rows");
    const nodes = new Map([...box.children].map((n) => [n.dataset.row, n]));
    rows.forEach((r, k) => {
      const id = String(r.id || `row-${k}`);
      let node = nodes.get(id);
      if (!node) { node = teamRow(); node.dataset.row = id; }
      if (box.children[k] !== node) box.insertBefore(node, box.children[k] || null);
      drawTeamRow(block, node, {...r, id});
    });
    // the running steps stay in view while the block scrolls inside, unless the reader just scrolled it
    if (now && box.scrollHeight > box.clientHeight + 1 && Date.now() - (block._touched || 0) > 8000) {
      const running = box.querySelectorAll(".tb-row.is-running");
      const top = running[0].offsetTop, last = running[running.length - 1];
      const bottom = last.offsetTop + last.offsetHeight;
      if (top < box.scrollTop || bottom > box.scrollTop + box.clientHeight) {   // in the middle: done above, waiting below
        box.scrollTop = bottom - top <= box.clientHeight ? Math.round((top + bottom - box.clientHeight) / 2) : top;
      }
    }
  }
  function addPlan(ev) {
    lastPrint = null; turn = null;
    const p = el("section", "plan");
    p.innerHTML = `<h3>Team plan</h3><ol>${(ev.steps || []).map((s) => `<li><span class="who">${esc(s.agent)}</span>` +
      `${s.tools ? ` <span class="what">(new helper: ${esc(s.tools.join(", "))})</span>` : ""}` +
      `${s.label ? ` <span class="what">· ${esc(s.label)}</span>` : ""}<div class="what">${esc(s.query)}</div></li>`).join("")}</ol>`;
    column.append(p);
  }

  // -- cards: approvals and the app's questions ------------------------------------------------------------------
  function diffHtml(body, kind) {
    return String(body).split("\n").map((line) => {
      const cls = kind !== "diff" ? "plain" : line.startsWith("+++") || line.startsWith("---") ? "diff-meta"
        : line.startsWith("@@") ? "diff-hunk" : line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-del" : "diff-ctx";
      return `<span class="${cls}">${esc(line) || " "}</span>`;
    }).join("");
  }
  function finishCard(card, outcome) {
    card.classList.remove("live");
    card.classList.add("done");
    if (outcome) card.append(el("div", `card-outcome${/^(Declined|Cancelled)/.test(outcome) ? " no" : ""}`, esc(outcome)));
  }
  function answer(id, value, outcome) {
    const card = cards.get(id);
    if (card && !card.classList.contains("done")) finishCard(card, outcome);
    post("/api/answer", {id, value});
  }
  function approvalCard(ev) {
    const card = el("section", "card live approval");
    const flat = (s) => String(s || "").replace(/\s+/g, " ").trim();
    const sameSubject = !ev.subject || ev.subject === ev.title || flat(ev.subject) === flat(ev.body);
    card.innerHTML = `<div class="card-head"><div class="card-tool">Needs your OK${ev.helper ? ` · asked by ${esc(ev.helper)}` : ""}</div>
      <div class="card-title">${esc(ev.title)}</div>${sameSubject ? "" : `<div class="card-sub">${esc(ev.subject)}</div>`}</div>
      ${ev.body ? `<div class="card-body"><pre>${diffHtml(ev.body, ev.body_kind)}</pre></div>` : ""}
      <div class="card-actions">
        <button type="button" class="btn primary" data-a="y">Yes <kbd>1</kbd></button>
        ${ev.always ? `<button type="button" class="btn" data-a="a">Yes, don't ask again for ${esc(ev.grant)} <kbd>2</kbd></button>` : ""}
        <button type="button" class="btn danger" data-a="n">No <kbd>${ev.always ? 3 : 2}</kbd></button>
      </div>`;
    const actions = card.querySelector(".card-actions");
    card.addEventListener("click", (e) => {
      const b = e.target.closest("[data-a]");
      if (!b || card.classList.contains("done")) return;
      const a = b.dataset.a;
      if (a === "y") answer(ev.id, {choice: "y"}, "Approved");
      else if (a === "a") answer(ev.id, {choice: "a"}, `Approved, and won't ask again: ${ev.grant}`);
      else if (a === "n") {
        actions.innerHTML = `<input type="text" placeholder="What should it do instead? (optional)" aria-label="Reason">
          <button type="button" class="btn danger" data-a="decline">Decline</button>`;
        const box = actions.querySelector("input");
        box.focus();
        box.addEventListener("keydown", (k) => { if (k.key === "Enter") { k.preventDefault(); actions.querySelector("[data-a=decline]").click(); } });
      } else if (a === "decline") {
        const reason = actions.querySelector("input").value.trim();
        answer(ev.id, {choice: "n", reason}, reason ? `Declined: ${reason}` : "Declined");
      }
    });
    card.keyHandler = (key) => {
      const order = ev.always ? ["y", "a", "n"] : ["y", "n"];
      const pick = order[Number(key) - 1];
      if (pick) card.querySelector(`[data-a="${pick}"]`)?.click();
    };
    return card;
  }
  function questionCard(ev) {
    const card = el("section", "card live question");
    if (ev.kind === "select") {
      card.innerHTML = `<div class="options">${ev.options.map((o, n) => `<button type="button" class="btn${n === ev.default ? " primary" : ""}" data-n="${n}">` +
        `<kbd>${n + 1}</kbd>${esc(o)}</button>`).join("")}</div>`;
      card.addEventListener("click", (e) => {
        const b = e.target.closest("[data-n]");
        if (b && !card.classList.contains("done")) answer(ev.id, Number(b.dataset.n), ev.options[Number(b.dataset.n)]);
      });
      card.keyHandler = (key) => { const n = Number(key) - 1; if (n >= 0 && n < ev.options.length) card.querySelector(`[data-n="${n}"]`).click(); };
      return card;
    }
    const recording = /Recording/.test(ev.prompt || "");
    card.innerHTML = `<div class="card-head"><div class="card-title">${esc((ev.prompt || "").trim())}</div></div>
      <div class="card-actions">${recording
        ? `<button type="button" class="btn primary" data-q="send">Stop recording</button><button type="button" class="btn" data-q="cancel">Cancel</button>`
        : `<input type="text" aria-label="Answer"><button type="button" class="btn primary" data-q="send">Answer</button><button type="button" class="btn" data-q="skip">Skip</button>`}</div>`;
    const box = card.querySelector("input");
    const send = () => { const v = box ? box.value : ""; answer(ev.id, v, recording ? "Stopped recording" : v ? `Answered: ${v}` : "Skipped"); };
    card.addEventListener("click", (e) => {
      const b = e.target.closest("[data-q]");
      if (!b || card.classList.contains("done")) return;
      if (b.dataset.q === "send") send();
      else if (b.dataset.q === "skip") answer(ev.id, "", "Skipped");
      else { finishCard(card, "Cancelled"); post("/api/stop"); }
    });
    if (box) box.addEventListener("keydown", (k) => { if (k.key === "Enter") { k.preventDefault(); send(); } });
    return card;
  }

  // an MCP server's sign-in, from a device that is not the computer agentx runs on
  function signinCard(ev) {
    if (!/^https?:\/\//i.test(ev.url || "")) return;
    lastPrint = null; turn = null;
    signingIn = ev.server || "the MCP server"; drawStage();
    const who = ev.server ? esc(ev.server) : "the MCP server";
    const card = el("section", "card signin");
    card.innerHTML = `<div class="card-head"><div class="card-tool">Sign in</div><div class="card-title">Sign in to ${who}</div></div>
      <ol class="signin-steps">
        <li><a class="btn primary" href="${esc(ev.url)}" target="_blank" rel="noopener noreferrer">Open the sign-in page</a></li>
        <li>Sign in there. Afterwards a page opens that cannot load (it was meant for the computer agentx runs on).
          That is expected: copy its whole address from the address bar.</li>
        <li><div class="signin-paste"><input type="url" placeholder="http://localhost:…/callback?code=…" aria-label="The address of the page that could not load">
          <button type="button" class="btn">Finish sign-in</button></div></li>
      </ol><div class="card-outcome" hidden></div>`;
    const box = card.querySelector("input"), out = card.querySelector(".card-outcome"), finish = card.querySelector(".signin-paste .btn");
    const send = async () => {
      if (!box.value.trim()) return;
      finish.disabled = true;
      const res = await post("/api/mcp-signin", {url: box.value.trim()});
      finish.disabled = false;
      out.hidden = false;
      out.textContent = res.message || (res.ok ? "Signed in." : "That did not work.");
      out.classList.toggle("no", !res.ok);
      if (res.ok) card.querySelector(".signin-steps").remove();
    };
    finish.addEventListener("click", send);
    box.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); send(); } });
    column.append(card);
  }

  // an MCP sign-in on this computer: the browser opened (or did not); the orb waits for you until agentx is back
  let signingIn = "", signinFor = "";             // signinFor: the sentence came, its address is the next notice
  function localSigninCard(server, url) {
    lastPrint = null; turn = null;
    signingIn = server || "the MCP server";
    const card = el("section", "card signin local");
    card.innerHTML = `<div class="card-head"><div class="card-tool">Sign in</div><div class="card-title">Sign in to ${esc(signingIn)}</div></div>
      <p class="signin-note">Your browser should have opened the sign-in page. Finish there and agentx carries on by itself.</p>
      <div class="signin-actions"><a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open the sign-in page</a>
        <span class="signin-wait">Esc cancels</span></div>`;
    column.append(card);
    drawStage();
  }
  function endSignin() {
    if (!signingIn) return;
    signingIn = "";
    for (const card of column.querySelectorAll(".card.signin.local:not(.done)")) {
      card.classList.add("done");
      const wait = card.querySelector(".signin-wait"); if (wait) wait.remove();
    }
    drawStage();
  }
  let ready = null;          // the app's open "message" request: your next message answers it
  // typing: in a text field (the message box, a card's answer box, a dialog), or a message is in the box
  const typingNow = () => /^(TEXTAREA|INPUT|SELECT)$/.test(document.activeElement?.tagName || "") || !!input.value.trim();
  function onAsk(ev) {
    if (ready === ev.id || cards.has(ev.id)) return;          // already shown (a reload plays it twice)
    if (ev.kind === "message") {
      ready = ev.id;
      endSignin();                                             // a sign-in ended (done, failed or cancelled)
      if (ev.default) { input.value = (input.value ? input.value + " " : "") + ev.default; grow(); }
      drawStatus();                                            // agentx is back for the next message
      setComposer();
      return;
    }
    lastPrint = null; turn = null;
    const card = ev.kind === "approval" ? approvalCard(ev) : questionCard(ev);
    cards.set(ev.id, card);
    // an approval sits under the step it is about, so its result comes after it
    const step = ev.kind === "approval" ? [...pending].reverse().find((s) => s.dataset.name === ev.tool && s.isConnected) : null;
    if (step && !step.closest(".group")) step.append(card); else column.append(card);
    // Focus moves to the card only when you are not typing: a key meant for the message box (Enter, Space) must
    // never answer a card that opened under it. The default answer the app gives is the highlighted button.
    // From an empty message box focus goes to the card itself, not a button: 1, 2, 3 answer it, Space and Enter don't.
    requestAnimationFrame(() => {
      const fromEmptyBox = document.activeElement === input && !input.value.trim();
      if (fromEmptyBox && !card.querySelector("input")) { card.tabIndex = -1; card.focus({preventScroll: true}); }
      else if (!typingNow()) (card.querySelector(".btn.primary, input") || card).focus({preventScroll: true});
    });
    setComposer();
    drawStage();
    showCard(card);
  }
  // A card waiting for you is scrolled into view from its top once the stage has folded to make room, so the
  // question or the change it asks about is what you see, not only its answer box.
  function showCard(card) {
    setTimeout(() => {
      if (!card.isConnected || card.classList.contains("done")) return;
      card.scrollIntoView({block: card.offsetHeight > log.clientHeight ? "start" : "nearest", behavior: "smooth"});
    }, 480);
  }
  function onAnswered(ev) {
    if (ev.id === ready) ready = null;
    const card = cards.get(ev.id);
    if (card && !card.classList.contains("done")) finishCard(card, ev.outcome ? `${ev.outcome} in the terminal` : "");
    setComposer();
    drawStage();
  }
  function liveCard() {
    for (const card of [...cards.values()].reverse()) if (!card.classList.contains("done")) return card;
    return null;
  }

  // -- the status, team and waiting messages under the conversation -----------------------------------------------
  let status = {}, startedAt = 0, sentAt = 0;   // sentAt: a message went off and agentx has not picked it up yet
  let doneAt = 0;                                // a turn just finished: the orb rests with a glow for a moment
  const DONE_MS = 6000;
  // a team job's helper steps, for the orb: counted when a status comes, read by the orb every frame
  let orbHelpers = {running: 0, finished: 0, waiting: 0};
  function countHelpers(team) {
    const n = {running: 0, finished: 0, waiting: 0};
    for (const r of (team && team.rows) || []) {
      if (r.state === "running") n.running++;
      else if (r.state === "waiting") n.waiting++;
      else if (r.state !== "skipped") n.finished++;         // done, failed, stopped or ended without word
    }
    return n;
  }
  const orbTeam = () => orbHelpers;
  // what the orb shows: stuck on a question or approval, a team at work, working on a step, thinking, done, or resting.
  // A team job shows from its first running helper until none is running or waiting (then it gathers back)
  function orbState() {
    if (liveCard() || signingIn) return "stuck";
    const h = orbHelpers;
    if (status.working && (h.running || (h.finished && h.waiting))) return "team";
    if (status.working) return pending.length || !/^thinking/i.test(String(status.label || "")) ? "working" : "thinking";
    if (sentAt) return "thinking";
    return doneAt && Date.now() - doneAt < DONE_MS ? "success" : "idle";
  }
  function mountOrbs(root, stateOf, options) {
    if (!window.AgentxOrb) return;
    for (const canvas of root.querySelectorAll("canvas.orb:not([data-on])")) {
      canvas.dataset.on = "1";
      window.AgentxOrb.create(canvas, stateOf, {teamOf: orbTeam, ...options});
    }
  }
  // the stage: once you have written, the orb large at the top, with a small note under it naming what the orb
  // shows now: thinking, the step it runs, waiting for you, done (while it glows), then ready; it stays after a turn
  let doneTook = "", stoppingAt = 0;               // stoppingAt: Esc (or Stop) was pressed and the turn has not ended yet
  const busyNow = () => !!(status.working || liveCard() || sentAt || signingIn);
  function stopNow() {
    if (status.working && !liveCard()) { stoppingAt = Date.now(); drawStage(); }
    post("/api/stop");
  }
  function drawStage() {
    const stage = $("stage"), state = orbState();
    const on = !column.querySelector(".welcome") && (state !== "idle" || !!column.querySelector(":scope > .user"));
    if (on !== stage.classList.contains("on")) {
      const stick = nearBottom();                 // the conversation keeps its end in view as the stage opens or folds
      stage.classList.toggle("on", on);
      stage.setAttribute("aria-hidden", on ? "false" : "true");
      if (stick) { const until = performance.now() + 520; const hold = () => { log.scrollTop = log.scrollHeight; if (performance.now() < until) requestAnimationFrame(hold); }; requestAnimationFrame(hold); }
    }
    // While a card waits for you the stage folds to a small orb beside its note, so the card has the room
    const waiting = !!liveCard();
    if (waiting !== stage.classList.contains("compact")) stage.classList.toggle("compact", waiting);
    if (state === "stuck" && stage.dataset.state !== "stuck") {   // what it waits for stays in view as the stage opens
      const card = liveCard();
      if (card) showCard(card);
    }
    stage.dataset.state = state;
    if (!on) return;
    if (stoppingAt && (!status.working || Date.now() - stoppingAt > 30000)) stoppingAt = 0;
    $("stageLabel").textContent = stoppingAt && state !== "stuck" ? "Stopping…"
      : state === "stuck" ? (signingIn && !liveCard() ? `Waiting for you to sign in to ${signingIn}` : "Waiting for your answer")
      : state === "success" ? "Done"
      : state === "idle" ? "Ready" : status.working ? friendlyLabel(status.label) + "…" : "Starting…";
    $("stageTime").textContent = state === "success" ? doneTook : startedAt ? [shortTime(Date.now() / 1000 - startedAt),
      status.working && status.tokens ? `${shortTokens(status.tokens)} tokens` : ""].filter(Boolean).join(" · ") : "";
  }
  function drawStatus() {
    const open = liveCard();
    const starting = sentAt && !status.working && !open && !ready && Date.now() - sentAt < 60000;
    if (!starting) sentAt = 0;
    const done = !status.working && !open && !starting && doneAt && Date.now() - doneAt < DONE_MS;
    $("status").hidden = !status.working && !open && !starting && !done;
    $("status").classList.toggle("done", !!done);
    $("statusLabel").textContent = open ? "Waiting for your answer" : status.working ? friendlyLabel(status.label) + "…"
      : starting ? "Starting…" : done ? "Done" : "";
    drawStage();
    $("composer").classList.toggle("busy", !!(status.working || starting));
    tickTime();
  }
  function onStatus(ev) {
    if (status.working && !ev.working) {
      doneAt = Date.now();
      doneTook = startedAt ? shortTime(Date.now() / 1000 - startedAt) : "";
      setTimeout(drawStatus, DONE_MS + 50);
    }
    else if (ev.working) doneAt = 0;
    status = ev;
    orbHelpers = countHelpers(ev.team);
    if (ev.working) sentAt = 0;
    startedAt = ev.working ? ev.startedAt || 0 : 0;
    drawStatus();
    if (ev.team) teamBlock(ev.team, true);           // the team job's block in the conversation
    const others = Number(ev.othersWorking || 0);         // other devices' conversations working in this folder
    $("others").hidden = !others;
    $("others").textContent = others === 1
      ? "Another conversation is working in this folder: it may change the same files."
      : `${others} other conversations are working in this folder: they may change the same files.`;
    drawWaiting(ev.waiting || []);
    $("micBtn").classList.toggle("recording", !!ev.recording);
    $("micBtn").setAttribute("aria-label", ev.recording ? "Stop recording" : "Speak instead of typing");
    setTasksBadge(ev.tasks);
    if (ev.mode) setMode(ev.mode);
    if ("fallback_from" in ev) fallbackFrom = ev.fallback_from || null;
    if (ev.model) setModel(ev.model); else setModel(currentModel);
    setComposer();
    const stick = nearBottom();
    updateLive();
    keepBottom(stick);
  }
  function shortTokens(n) { return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n); }
  function tickTime() {
    const parts = [];
    if (startedAt) parts.push(shortTime(Date.now() / 1000 - startedAt));
    if (status.working && status.tokens) parts.push(`${shortTokens(status.tokens)} tokens`);
    $("statusTime").textContent = parts.join(" · ");
    drawStage();
  }
  setInterval(tickTime, 1000);
  // Messages typed while agentx works, waiting to be read. Each wraps (three lines, then "Show all"), and has Send
  // now (stop what it does, as Esc does, and make this the next message it reads), Edit (back into the message box)
  // and Remove.
  const openWaiting = new Set();
  let waitingShown = "";
  function drawWaiting(items) {
    const waiting = $("waiting");
    const key = JSON.stringify(items) + [...openWaiting].join("|");
    if (key === waitingShown) return;                // the status comes often: redraw only on a change
    waitingShown = key;
    waiting.hidden = !items.length;
    waiting.innerHTML = items.map((w, n) => {
      const long = w.text.length > 220 || w.text.split("\n").length > 3;
      const open = openWaiting.has(w.text);
      const message = ["agent", "team", "later"].includes(w.kind);
      return `<li class="w-item${open ? " open" : ""}" data-n="${n + 1}">` +
        `<div class="w-body"><span class="w-text">${items.length > 1 ? `${n + 1}. ` : ""}${esc(w.text)}</span>` +
        `<span class="w-where">${esc(w.where)}${long ? ` · <button type="button" class="w-more" data-wact="more">${open ? "Show less" : "Show all"}</button>` : ""}</span></div>` +
        `<div class="w-acts">${message ? `<button type="button" class="w-now" data-wact="now" title="Stop what agentx is doing (what it did is kept) and send this now">Send now</button>` +
        `<button type="button" data-wact="edit" title="Take it back into the message box">Edit</button>` : ""}` +
        `<button type="button" class="w-remove" data-wact="remove" aria-label="Take back this message" title="Take it back">✕</button></div></li>`;
    }).join("");
  }
  $("waiting").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-wact]");
    const li = e.target.closest("[data-n]");
    if (!b || !li) return;
    const n = Number(li.dataset.n);
    const text = li.querySelector(".w-text").textContent.replace(/^\d+\. /, "");
    if (b.dataset.wact === "more") {
      if (openWaiting.has(text)) openWaiting.delete(text); else openWaiting.add(text);
      li.classList.toggle("open");
      b.textContent = li.classList.contains("open") ? "Show less" : "Show all";
      return;
    }
    b.disabled = true;
    const res = await post("/api/waiting", {action: b.dataset.wact, n}).catch(() => null);
    if (res && res.ok && typeof res.edit === "string") {          // into the message box, ready to change
      input.value = input.value.trim() ? `${input.value.replace(/\s*$/, "")}\n${res.edit}` : res.edit;
      grow(); setComposer(); input.focus();
    } else if (res && !res.ok) {
      addNotice({level: "error", text: `Could not ${b.dataset.wact === "now" ? "send it now" : "take it back"}: ${res.text || "it was read already"}.`});
    }
    b.disabled = false;
  });

  // -- the composer -------------------------------------------------------------------------------------------
  // While agentx works and the box is empty, the send button is the Stop button.
  function setComposer() {
    input.placeholder = ready ? "Ask agentx to do something, or type / for commands"
      : status.working || sentAt ? "Add to the task while it works, or / for a command" : "Waiting for agentx…";
    const send = $("sendBtn");
    const stopping = !!(status.working || liveCard()) && !input.value.trim();
    send.classList.toggle("stopping", stopping);
    send.setAttribute("aria-label", stopping ? "Stop" : "Send");
    send.title = stopping ? "Stop (Esc)" : "Send (Enter)";
    send.disabled = stopping ? false : !(ready || status.working) || !input.value.trim();
  }
  function grow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px"; }
  async function submit() {
    if ($("sendBtn").classList.contains("stopping")) { stopNow(); return; }
    const text = input.value.replace(/\s+$/, "");
    if (!text.trim()) return;
    input.value = ""; grow(); hidePalette();
    if (ready && !status.working) { ready = null; sentAt = Date.now(); drawStatus(); }   // moving at once, not "Waiting…"
    await post("/api/send", {text});
  }
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  input.addEventListener("input", () => { grow(); updatePalette(); setComposer(); });
  input.addEventListener("keydown", (e) => {
    if (!palette.hidden && ["ArrowDown", "ArrowUp", "Tab", "Escape"].includes(e.key)) { paletteKey(e); return; }
    // Esc stops agentx from the message box too (what you typed stays)
    if (e.key === "Escape" && !e.isComposing && busyNow()) { e.preventDefault(); stopNow(); return; }
    if (!palette.hidden && e.key === "Enter" && !e.shiftKey && paletteItems.length) { e.preventDefault(); choosePalette(); return; }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (input.value.trim()) submit(); }
  });

  // the `/` menu: commands, then skills
  let commands = {}, skills = {}, paletteItems = [], paletteIndex = 0;
  function hidePalette() { palette.hidden = true; paletteItems = []; }
  function updatePalette() {
    const m = input.value.match(/^\/([\w.-]*)$/);
    if (!m) return hidePalette();
    const q = m[1].toLowerCase();
    paletteItems = [...Object.entries(commands).map(([n, d]) => ({n, d, skill: false})),
                    ...Object.entries(skills).map(([n, d]) => ({n, d: d.replace(/^\(skill\)\s*/, ""), skill: true}))]
      .filter((x) => x.n.slice(1).toLowerCase().startsWith(q)).slice(0, 40);
    if (!paletteItems.length) return hidePalette();
    paletteIndex = Math.min(paletteIndex, paletteItems.length - 1);
    palette.innerHTML = paletteItems.map((x, n) => `<div class="palette-item" role="option" data-n="${n}" aria-selected="${n === paletteIndex}">` +
      `<span class="palette-name${x.skill ? " skill" : ""}">${esc(x.n)}</span><span class="palette-desc">${esc(x.d)}</span></div>`).join("");
    palette.hidden = false;
  }
  function paletteKey(e) {
    e.preventDefault();
    if (e.key === "Escape") return hidePalette();
    if (e.key === "Tab") return choosePalette();
    paletteIndex = (paletteIndex + (e.key === "ArrowDown" ? 1 : -1) + paletteItems.length) % paletteItems.length;
    updatePalette();
  }
  function choosePalette(n) {
    const item = paletteItems[n ?? paletteIndex];
    if (!item) return;
    input.value = item.n + " ";
    hidePalette(); grow(); input.focus();
  }
  palette.addEventListener("mousedown", (e) => { const it = e.target.closest("[data-n]"); if (it) { e.preventDefault(); choosePalette(Number(it.dataset.n)); } });

  // pictures pasted or dropped: saved in the project, then named in the message
  async function addPicture(file) {
    if (!file || !/^image\/(png|jpeg|gif|webp)$/.test(file.type)) return;
    const data = await new Promise((ok) => { const r = new FileReader(); r.onload = () => ok(String(r.result).split(",")[1] || ""); r.readAsDataURL(file); });
    const res = await post("/api/upload", {name: file.name || "screenshot", type: file.type, data});
    if (res.path) { input.value = (input.value ? input.value.replace(/\s*$/, " ") : "Look at ") + res.path + " "; grow(); setComposer(); input.focus(); }
    else addNotice({level: "error", text: res.error || "The picture could not be added."});
  }
  input.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); files.forEach(addPicture); }
  });
  document.addEventListener("dragover", (e) => { if ([...e.dataTransfer.items].some((i) => i.kind === "file")) e.preventDefault(); });
  document.addEventListener("drop", (e) => { const files = [...(e.dataTransfer?.files || [])]; if (files.length) { e.preventDefault(); files.forEach(addPicture); } });

  $("fileInput").addEventListener("change", () => { [...$("fileInput").files].forEach(addPicture); $("fileInput").value = ""; });
  $("micBtn").addEventListener("click", () => post("/api/voice"));

  // keys for the open card: 1, 2, 3 when you are not typing; Esc stops
  document.addEventListener("keydown", (e) => {
    const typing = /^(TEXTAREA|INPUT|SELECT)$/.test(document.activeElement?.tagName || "");
    if (e.key === "Escape" && !typing && busyNow()) { stopNow(); return; }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    const card = liveCard();
    if (card && card.keyHandler && /^[1-9]$/.test(e.key)) { e.preventDefault(); card.keyHandler(e.key); }
  });

  // -- the window's own dialog (never the browser's) ---------------------------------------------------------------
  // askText({title, text, placeholder, ok}) → the typed text, or null when cancelled (Esc, Cancel, a click outside).
  function askText({title, text, placeholder, ok}) {
    return new Promise((resolve) => {
      if (closed) { resolve(null); return; }
      const overlay = $("overlay"), box = $("overlayBox"), back = document.activeElement;
      box.innerHTML = `<h2 id="dlgTitle">${esc(title)}</h2>${text ? `<p>${esc(text)}</p>` : ""}` +
        `<input type="text" class="dlg-input" placeholder="${esc(placeholder || "")}" aria-labelledby="dlgTitle" spellcheck="false" autocomplete="off">` +
        `<div class="dlg-actions"><button type="button" class="btn" data-d="cancel">Cancel</button>` +
        `<button type="button" class="btn primary" data-d="ok" disabled>${esc(ok || "OK")}</button></div>`;
      box.setAttribute("aria-labelledby", "dlgTitle");
      overlay.hidden = false;
      const field = box.querySelector("input"), okBtn = box.querySelector('[data-d="ok"]');
      const done = (value) => {
        overlay.hidden = true;
        box.onclick = box.onkeydown = overlay.onmousedown = null;
        resolve(value);
        if (back && back.focus) back.focus();
      };
      field.addEventListener("input", () => { okBtn.disabled = !field.value.trim(); });
      box.onclick = (e) => { const b = e.target.closest("[data-d]"); if (b && !b.disabled) done(b.dataset.d === "ok" ? field.value.trim() : null); };
      overlay.onmousedown = (e) => { if (e.target === overlay) done(null); };
      box.onkeydown = (e) => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(null); }
        else if (e.key === "Enter" && e.target === field && field.value.trim()) { e.preventDefault(); done(field.value.trim()); }
        else if (e.key === "Tab") {                     // keep the keys inside the dialog
          const stops = [...box.querySelectorAll("input, button:not(:disabled)")];
          const at = stops.indexOf(document.activeElement);
          e.preventDefault();
          stops[(at + (e.shiftKey ? -1 : 1) + stops.length) % stops.length].focus();
        }
      };
      field.focus();
    });
  }

  // -- the mode and model menus under the message box --------------------------------------------------------------
  const MODES = [
    ["default", "Ask before changes", "Edits, commands and web lookups ask first"],
    ["accept-edits", "Accept edits", "Edits run without asking; commands still ask"],
    ["plan", "Plan only", "Reads and plans; changes nothing"],
    ["auto", "Auto", "Nothing asks. Only in folders you trust"],
  ];
  const CHECK = '<svg class="menu-check" viewBox="0 0 16 16" aria-hidden="true"><path d="m3.5 8.5 3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  let models = [], currentModel = "", currentMode = "default", menuFor = null, menuPick = null;
  // the models the local server lists (marked "· local" in the menu), and while the conversation runs on the
  // fallback model, the one that failed and why ({model, reason}; the chip says "· fallback")
  let localModels = [], fallbackFrom = null;
  function setMode(mode) {
    currentMode = mode;
    const m = MODES.find((x) => x[0] === mode);
    $("modeLabel").textContent = m ? m[1] : mode;
  }
  function setModel(model) {
    currentModel = model || "";
    const onFallback = !!(fallbackFrom && currentModel);
    $("modelLabel").textContent = currentModel ? `${currentModel}${onFallback ? " · fallback" : ""}` : "Model";
    $("modelBtn").classList.toggle("on-fallback", onFallback);
    $("modelBtn").title = onFallback
      ? `${fallbackFrom.model || "The chosen model"} failed${fallbackFrom.reason ? ` (${fallbackFrom.reason})` : ""}, so this conversation runs on ${currentModel}. Choose a model to try it again.`
      : "";
  }
  const menu = $("menu");
  function closeMenu(refocus) {
    if (!menuFor) return;
    menu.hidden = true;
    menuFor.setAttribute("aria-expanded", "false");
    if (refocus) menuFor.focus();
    menuFor = null; menuPick = null;
  }
  function drawMenu(button, items) {
    menu.innerHTML = items.map((it, n) => `<div class="menu-item" role="option" tabindex="-1" data-n="${n}" aria-selected="${!!it.current}">` +
      `<span class="menu-text"><span class="menu-label">${esc(it.label)}${it.tag ? `<span class="menu-tag"> · ${esc(it.tag)}</span>` : ""}</span>${it.hint ?`<span class="menu-hint">${esc(it.hint)}</span>` : ""}</span>` +
      `${it.current ? CHECK : ""}</div>`).join("");
    menu.items = items;
    menu.hidden = false;
    const b = button.getBoundingClientRect();           // above its button, kept inside the window
    menu.style.left = Math.max(8, Math.min(b.left, window.innerWidth - menu.offsetWidth - 8)) + "px";
    menu.style.bottom = Math.max(8, window.innerHeight - b.top + 8) + "px";
  }
  function openMenu(button, items, pick) {
    if (menuFor === button) { closeMenu(true); return; }
    closeMenu(); hidePalette();
    menuFor = button; menuPick = pick;
    drawMenu(button, items);
    button.setAttribute("aria-expanded", "true");
    const start = Math.max(0, items.findIndex((it) => it.current));
    menu.querySelector(`[data-n="${start}"]`).focus();
  }
  // The open menu's items replaced in place (a list that came in after it opened); the focused item keeps focus.
  function refreshMenu(button, items) {
    if (menuFor !== button) return;
    const same = (a, b) => a.value === b.value && a.label === b.label && a.tag === b.tag && !!a.current === !!b.current;
    if (items.length === menu.items.length && items.every((it, n) => same(it, menu.items[n]))) return;
    const at = menu.contains(document.activeElement) ? menu.items[Number(document.activeElement.dataset.n)] : null;
    drawMenu(button, items);
    const keep = at ? items.findIndex((it) => it.value === at.value && it.label === at.label) : -1;
    const start = keep >= 0 ? keep : Math.max(0, items.findIndex((it) => it.current));
    menu.querySelector(`[data-n="${start}"]`).focus();
  }
  function choose(n) {
    const it = menu.items && menu.items[n], pick = menuPick;
    closeMenu(true);
    if (it && pick) pick(it);
  }
  menu.addEventListener("click", (e) => { const it = e.target.closest("[data-n]"); if (it) choose(Number(it.dataset.n)); });
  menu.addEventListener("mousemove", (e) => { const it = e.target.closest("[data-n]"); if (it && document.activeElement !== it) it.focus(); });
  menu.addEventListener("keydown", (e) => {
    const all = [...menu.querySelectorAll("[data-n]")];
    const at = all.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") all[(at + (e.key === "ArrowDown" ? 1 : -1) + all.length) % all.length].focus();
    else if (e.key === "Home") all[0].focus();
    else if (e.key === "End") all[all.length - 1].focus();
    else if (e.key === "Enter" || e.key === " ") { if (at >= 0) choose(Number(all[at].dataset.n)); }
    else if (e.key === "Escape") closeMenu(true);
    else if (e.key === "Tab") { closeMenu(true); return; }
    else return;
    e.preventDefault(); e.stopPropagation();
  });
  document.addEventListener("mousedown", (e) => {
    if (menuFor && !menu.contains(e.target) && !menuFor.contains(e.target)) closeMenu();
  });
  window.addEventListener("resize", () => closeMenu());

  $("modeBtn").addEventListener("click", () => openMenu($("modeBtn"),
    MODES.map(([value, label, hint]) => ({value, label, hint, current: value === currentMode})),
    (it) => { if (it.value !== currentMode) { setMode(it.value); post("/api/send", {text: `/mode ${it.value}`}); } }));
  function modelItems() {
    const names = models.includes(currentModel) || !currentModel ? models : [currentModel, ...models];
    return [...names.map((m) => ({value: m, label: m, tag: localModels.includes(m) ? "local" : "",
                                  current: m === currentModel})),
            {value: "", label: "Other model…", hint: "Type any model name"}];
  }
  async function pickModel(it) {
    let name = it.value;
    if (!name) {
      name = await askText({title: "Use another model", text: "Any model your keys can reach, by its exact name.",
                            placeholder: "for example claude-sonnet-5-5 or gpt-6.1-sol", ok: "Switch"});
      if (!name) return;
    }
    if (name !== currentModel) { setModel(name); post("/api/send", {text: `/model ${name}`}); }
  }
  // Opens at once from what is known (a second click closes it, as any menu); the local server's models may have
  // come in since, so the list is asked again and the open menu updated in place.
  $("modelBtn").addEventListener("click", () => {
    const button = $("modelBtn");
    const opening = menuFor !== button;
    openMenu(button, modelItems(), pickModel);
    if (opening) loadState().then(() => refreshMenu(button, modelItems()));
  });
  $("newChat").addEventListener("click", () => {
    if (live.joined) joinLive(false, "new");
    else if (busyNow() && !EMBED) openLane("");                    // this one works: start the new one beside it
    else post("/api/send", {text: "/clear"});
  });
  // -- the side panel: context, tokens, team work, MCP servers, memory ----------------------------------------------
  const num = (n) => Number(n || 0).toLocaleString();
  let detailsOpen = false, detailsTimer = 0;
  let tasksOpen = false, tasksWidth = 0;       // the Tasks panel (below) shares the right edge: one of them is open
  const TASKS_W = 420;                         // its usual width
  function setDetails(open) {
    if (open && tasksOpen) setTasks(false);
    detailsOpen = open;
    $("details").hidden = !open;
    $("detailsScrim").hidden = !(open || tasksOpen);
    document.querySelector(".app").classList.toggle("with-details", open || tasksOpen);
    $("detailsBtn").setAttribute("aria-expanded", String(open));
    placeDetails();
    try { localStorage.setItem("agentx-details", open ? "open" : "closed"); } catch (_) { /* no storage */ }
    if (open) loadDetails();
  }
  $("detailsBtn").addEventListener("click", () => setDetails(!detailsOpen));
  $("detailsClose").addEventListener("click", () => setDetails(false));
  // below a wide window the panel lies over the chat: tapping beside it, or Escape, closes it
  // a side of the split view is narrower than a window: its Details is a column there too, unless it is narrow
  const detailsOver = () => matchMedia(EMBED ? "(max-width: 639px)" : "(max-width: 1199px)").matches;
  // With a side open, a Details panel that lies over the chat lies over this side only, at its right edge (the side
  // beside has its own Details); as a column it sits between this side and the line (see .app.split in the CSS).
  function placeDetails() {
    const panel = $(tasksOpen ? "tasksPanel" : "details"), scrim = $("detailsScrim");
    const other = $(tasksOpen ? "details" : "tasksPanel");
    other.style.right = other.style.width = "";
    const split = document.querySelector(".app").classList.contains("split") && !matchMedia("(max-width: 860px)").matches;
    if (split && (detailsOpen || tasksOpen) && detailsOver()) {
      const main = document.querySelector(".main").getBoundingClientRect();
      const fromRight = Math.max(0, window.innerWidth - main.right);
      panel.style.right = `${fromRight}px`;
      panel.style.width = `${Math.min((tasksOpen ? tasksWidth || TASKS_W : detailsWidth || 360), main.width)}px`;
      scrim.style.right = `${fromRight}px`;
    } else {
      panel.style.right = panel.style.width = scrim.style.right = "";
    }
  }
  window.addEventListener("resize", placeDetails);
  // Details' width: drag its left edge (or, with it focused, the arrow keys); double-click for the usual width.
  // Kept per window (each side of the split view has its own). 0 means the usual width.
  const DETAILS_MIN = 260;
  let detailsWidth = 0;
  try { const kept = Number(localStorage.getItem("agentx-details-w")); if (kept >= DETAILS_MIN) detailsWidth = kept; } catch (_) { /* no storage */ }
  const detailsMax = () => Math.max(DETAILS_MIN, Math.round(window.innerWidth * 0.6));
  function setDetailsWidth(width, keep) {
    detailsWidth = width ? Math.round(Math.min(detailsMax(), Math.max(DETAILS_MIN, width))) : 0;
    const root = document.documentElement;
    if (detailsWidth) root.style.setProperty("--details-w", `${detailsWidth}px`); else root.style.removeProperty("--details-w");
    placeDetails();
    if (keep) { try { if (detailsWidth) localStorage.setItem("agentx-details-w", String(detailsWidth)); else localStorage.removeItem("agentx-details-w"); } catch (_) { /* no storage */ } }
  }
  setDetailsWidth(detailsWidth, false);
  $("detailsGrip").addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const grip = $("detailsGrip"), app = document.querySelector(".app");
    try { grip.setPointerCapture(e.pointerId); } catch (_) { /* a pointer the page cannot hold: moves still come */ }
    app.classList.add("resizing-details");          // a side's frame must not take the pointer while dragging
    const startX = e.clientX, startW = $("details").getBoundingClientRect().width;
    const move = (ev) => setDetailsWidth(startW + (startX - ev.clientX), false);   // its left edge: left is wider
    const up = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
      grip.removeEventListener("pointercancel", up);
      app.classList.remove("resizing-details");
      setDetailsWidth(detailsWidth, true);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
    grip.addEventListener("pointercancel", up);
  });
  $("detailsGrip").addEventListener("dblclick", () => setDetailsWidth(0, true));
  $("detailsGrip").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const now = $("details").getBoundingClientRect().width;
    setDetailsWidth(now + (e.key === "ArrowLeft" ? 24 : -24), true);
  });
  $("detailsScrim").addEventListener("click", () => setDetails(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && detailsOpen && detailsOver() && $("settingsWrap").hidden) setDetails(false);
  });
  function section(title, body, extra) {
    return `<section class="d-sec"><h3>${esc(title)}${extra ? `<span class="d-extra">${extra}</span>` : ""}</h3>${body}</section>`;
  }
  async function loadDetails() {
    let d;
    try { d = await get("/api/details"); } catch (_) { return; }
    // the ring in the top bar: how close the conversation is to being summarised
    const ctx = d.context;
    const pct = ctx ? Math.min(100, Math.round(ctx.chars * 100 / Math.max(1, ctx.limit))) : 0;
    $("ctxRing").style.setProperty("--p", pct);
    $("ctxRing").classList.toggle("high", pct >= 80);
    const needSignin = (d.mcp || []).filter((m) => m.state === "signin").map((m) => m.name);
    $("detailsBtn").classList.toggle("attention", needSignin.length > 0);
    $("detailsBtn").title = needSignin.length ? `${needSignin.join(", ")}: sign-in needed (MCP)` : ctx ? `Context ${pct}% full` : "Details";
    if (!detailsOpen) return;
    const out = [];
    if (modeNote) {
      out.push(`<section class="d-sec d-mode"><h3>Mode</h3><p class="d-mode-title">${esc(modeNote.title)}</p>` +
        `<p class="d-note">${esc(modeNote.body)}</p></section>`);
    }
    if (ctx) {
      out.push(section("Context", `<div class="meter"><span style="width:${pct}%"></span></div>
        <p class="d-note">${pct}% full · ${num(ctx.messages)} messages. At 100%, older parts are summarised so the
        conversation can go on.</p>`));
    }
    const u = d.usage;
    if (u) {
      const last = u.lastTurn && u.lastTurn.length ? u.lastTurn : null;
      out.push(section("Tokens in this conversation", `<dl class="d-grid">
        <dt>Sent</dt><dd>${num(u.input)}</dd><dt>Received</dt><dd>${num(u.output)}</dd>
        <dt>From cache</dt><dd>${num(u.cached)}</dd><dt>Requests</dt><dd>${num(u.requests)}</dd>
        ${last ? `<dt>Last message</dt><dd>${num(last[1] + last[2])}</dd>` : ""}
        <dt>Limit per message</dt><dd>${u.tokenLimit ? num(u.tokenLimit) : "none"}</dd></dl>`,
        `<button type="button" class="link" data-cmd="/usage">Full report</button>`));
    }
    const t = d.team;
    out.push(section("Team work", t && t.steps.length ? t.steps.map((s, n) => `${s.job && (n === 0 || t.steps[n - 1].job !== s.job) ? `<p class="d-job">${esc(s.job)}</p>` : ""}<details class="d-step"${s.done ? "" : " open"}>
        <summary><span class="dot ${s.state === "failed" ? "failed" : s.done ? "ok" : "run"}"></span><span class="d-who">${esc(s.helper)}${s.label && s.label !== s.helper ? ` · ${esc(s.label)}` : ""}</span>
        <span class="d-num">${s.seconds ? `${s.seconds}s · ` : ""}${s.toolUses && !s.seconds ? `${num(s.toolUses)} steps · ` : ""}${num(s.tokens)} tok</span>
        ${s.fallback ? `<span class="d-fallback">↺ on ${esc(s.fallback.to)} (fallback${s.fallback.from || s.fallback.reason ? `: ${esc([s.fallback.from, s.fallback.reason].filter(Boolean).join(" "))}` : ""})</span>` : ""}</summary>
        <p class="d-note">${esc(s.query)}</p>
        ${s.actions.length ? `<ul class="d-acts">${s.actions.map((a) => `<li class="${a.failed ? "failed" : ""}">${esc(toolLook(a.name)[0])} <code>${esc(a.subject)}</code></li>`).join("")}</ul>` : ""}
      </details>`).join("") : `<p class="d-empty">No team job in this conversation yet. Ask for a big job and agentx can split it across helpers.</p>`,
      t && t.steps.length ? `<button type="button" class="link" data-cmd="/team log">Log</button>` : ""));
    const servers = d.mcp || [];
    const words = {on: "Connected", signin: "Needs sign-in", off: "Not connected", failed: "Failed"};
    out.push(section("MCP servers", (servers.length ? `<ul class="d-mcp">${servers.map((m) => `<li>
        <div class="d-row"><span class="dot ${m.state}"></span><span class="d-name">${esc(m.name)}</span>
        <span class="d-state ${m.state}">${words[m.state] || ""}${m.tools != null ? ` · ${m.tools} tools` : ""}</span></div>
        <div class="d-target">${esc(m.target)}</div>
        ${m.state !== "on" ? `<div class="d-why">${esc(m.status)}</div>` : ""}
        <div class="d-actions">
          ${m.state === "signin" || m.state === "off" || m.state === "failed" ? `<button type="button" class="btn small${m.state === "signin" ? " primary" : ""}" data-cmd="/mcp login ${esc(m.name)}">Sign in to ${esc(m.name)}</button>` : ""}
          <button type="button" class="btn small" data-cmd="/mcp reload ${esc(m.name)}">Reconnect</button>
          ${m.state === "on" ? `<button type="button" class="btn small" data-cmd="/mcp tools ${esc(m.name)}">Tools</button>
          <button type="button" class="btn small" data-cmd="/mcp logout ${esc(m.name)}">Sign out</button>` : ""}
        </div></li>`).join("")}</ul>` : `<p class="d-empty">No MCP servers. Add one with <code>/mcp add &lt;name&gt; &lt;url&gt;</code>.</p>`) +
      (d.mcpProblems || []).map((p) => `<p class="d-why">${esc(p)}</p>`).join("")));
    const mem = d.memory;
    if (mem) {
      out.push(section("Memory", `${mem.files.length ? `<ul class="d-files">${mem.files.map((f) =>
          `<li><code>${esc(f.path)}</code><span class="d-num">${num(Math.round(f.bytes / 100) / 10)} KB</span></li>`).join("")}</ul>`
        : `<p class="d-empty">No AGENTX.md yet. <button type="button" class="link" data-cmd="/init">Write one with /init</button></p>`}
        ${mem.notes != null ? `<p class="d-note">${num(mem.notes)} saved notes · ${num(mem.reports)} team reports${mem.log ? " · a work log" : ""}</p>` : ""}`,
        mem.notes != null ? `<button type="button" class="link" data-cmd="/memory">Browse</button>` : ""));
    }
    const body = $("detailsBody");
    const open = [...body.querySelectorAll("details[open] summary .d-who")].map((n) => n.textContent);
    body.innerHTML = out.join("");
    body.querySelectorAll("details summary .d-who").forEach((n) => { if (open.includes(n.textContent)) n.closest("details").open = true; });
  }
  $("detailsBody").addEventListener("click", (e) => {
    const b = e.target.closest("[data-cmd]");
    if (b) { post("/api/send", {text: b.dataset.cmd}); setTimeout(loadDetails, 1500); }
  });
  setInterval(() => { if (detailsOpen || detailsTimer++ % 5 === 0) loadDetails(); }, 3000);
  setTimeout(() => {
    try { if (localStorage.getItem("agentx-details") === "open" && !detailsOver()) setDetails(true); else loadDetails(); } catch (_) { loadDetails(); }
  }, 0);

  // -- the Tasks panel: background tasks, the page agentx has open, and terminals ---------------------------------
  // It shares the right edge with Details (opening one closes the other) and lies over the chat on a narrow window
  // as Details does. Only what is on screen is asked for: the list every 2 s, an output every second, the page's
  // state every second and its picture about 3 times a second (one request at a time), and nothing while the
  // panel, its tab or the window is hidden. Names, commands and output are always set as text, never as HTML.
  const TP_TABS = ["tasks", "browser", "terminal"], TASKS_MIN = 300, MAX_LINES = 2000;
  const VIEW_W = 1280, VIEW_H = 800;                 // the browser's page size: clicks are scaled to it
  let tpTab = "tasks";
  try { const kept = localStorage.getItem("agentx-tasks-tab"); if (TP_TABS.includes(kept)) tpTab = kept; } catch (_) { /* no storage */ }
  let tpRows = [], terminalsAllowed = true, tpSig = "", termSig = "", termId = "";
  const tpShown = (tab) => tasksOpen && !document.hidden && (!tab || tpTab === tab);
  const refused = (r, fallback) => { if (r && r.ok === false) addNotice({level: "error", text: r.error || fallback}); return !r || r.ok === false; };

  function setTasks(open, focus) {
    if (open && detailsOpen) setDetails(false);
    tasksOpen = open;
    $("tasksPanel").hidden = !open;
    $("detailsScrim").hidden = !(open || detailsOpen);
    const app = document.querySelector(".app");
    app.classList.toggle("with-details", open || detailsOpen);
    app.classList.toggle("with-tasks", open);
    $("tasksBtn").setAttribute("aria-expanded", String(open));
    placeDetails();
    if (open) {
      showTab(tpTab);
      if (focus) $("tasksPanel").querySelector('[role="tab"][aria-selected="true"]').focus();
    } else stopFrames();
  }
  $("tasksBtn").addEventListener("click", () => setTasks(!tasksOpen, !tasksOpen));
  $("tasksClose").addEventListener("click", () => { setTasks(false); $("tasksBtn").focus(); });
  $("detailsScrim").addEventListener("click", () => { if (tasksOpen) setTasks(false); });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !tasksOpen || !detailsOver() || !$("settingsWrap").hidden) return;
    if (bw.user && document.activeElement === $("bwFrame")) return;      // the page has the keys: Escape is its
    setTasks(false);
    $("tasksBtn").focus();
  });
  function setTasksBadge(n) {
    n = Math.max(0, Number(n) || 0);
    const badge = $("tasksBadge"), count = $("tpCount");
    badge.hidden = count.hidden = n <= 0;
    badge.textContent = count.textContent = n > 0 ? String(n) : "";
    $("tasksBtn").setAttribute("aria-label", n > 0 ? `Tasks, ${n} running` : "Tasks");
    $("tpTabTasks").setAttribute("aria-label", n > 0 ? `Tasks, ${n} running` : "Tasks");   // read as that, not "Tasks2"
  }

  // the tabs: a click, or the arrow keys with a tab focused
  function showTab(name) {
    tpTab = TP_TABS.includes(name) ? name : "tasks";
    for (const b of $("tasksPanel").querySelectorAll('.tp-tabs [role="tab"]')) {
      const on = b.dataset.tab === tpTab;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
    }
    for (const body of $("tasksPanel").querySelectorAll('[role="tabpanel"]')) body.hidden = body.dataset.tab !== tpTab;
    try { localStorage.setItem("agentx-tasks-tab", tpTab); } catch (_) { /* no storage */ }
    if (tpTab === "browser") { loadBrowser(); startFrames(); } else { stopFrames(); loadTasks(); }
  }
  $("tasksPanel").querySelector(".tp-tabs").addEventListener("click", (e) => {
    const b = e.target.closest('[role="tab"]');
    if (b) showTab(b.dataset.tab);
  });
  $("tasksPanel").querySelector(".tp-tabs").addEventListener("keydown", (e) => {
    const step = {ArrowRight: 1, ArrowLeft: -1}[e.key];
    if (!step) return;
    e.preventDefault();
    const next = TP_TABS[(TP_TABS.indexOf(tpTab) + step + TP_TABS.length) % TP_TABS.length];
    showTab(next);
    $("tasksPanel").querySelector(`.tp-tabs [data-tab="${next}"]`).focus();
  });
  document.addEventListener("visibilitychange", () => { if (tpShown("browser")) { loadBrowser(); startFrames(); } });

  // the panel's width: its left edge, as for Details (kept per window; 0 is the usual width)
  try { const kept = Number(localStorage.getItem("agentx-tasks-width")); if (kept >= TASKS_MIN) tasksWidth = kept; } catch (_) { /* no storage */ }
  function setTasksWidth(width, keep) {
    tasksWidth = width ? Math.round(Math.min(Math.max(TASKS_MIN, window.innerWidth * 0.7), Math.max(TASKS_MIN, width))) : 0;
    const root = document.documentElement;
    if (tasksWidth) root.style.setProperty("--tasks-w", `${tasksWidth}px`); else root.style.removeProperty("--tasks-w");
    placeDetails();
    if (keep) { try { if (tasksWidth) localStorage.setItem("agentx-tasks-width", String(tasksWidth)); else localStorage.removeItem("agentx-tasks-width"); } catch (_) { /* no storage */ } }
  }
  setTasksWidth(tasksWidth, false);
  $("tasksGrip").addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const grip = $("tasksGrip"), app = document.querySelector(".app");
    try { grip.setPointerCapture(e.pointerId); } catch (_) { /* moves still come */ }
    app.classList.add("resizing-details");
    const startX = e.clientX, startW = $("tasksPanel").getBoundingClientRect().width;
    const move = (ev) => setTasksWidth(startW + (startX - ev.clientX), false);
    const up = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
      grip.removeEventListener("pointercancel", up);
      app.classList.remove("resizing-details");
      setTasksWidth(tasksWidth, true);
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
    grip.addEventListener("pointercancel", up);
  });
  $("tasksGrip").addEventListener("dblclick", () => setTasksWidth(0, true));
  $("tasksGrip").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    setTasksWidth($("tasksPanel").getBoundingClientRect().width + (e.key === "ArrowLeft" ? 24 : -24), true);
  });

  // -- Tasks: Running and Finished, each row with its time and Stop; a click opens its output ---------------------
  const termLabel = (t) => t.name || `Terminal ${String(t.id).replace(/^term/, "")}`;
  const rowName = (t) => t.kind === "terminal" ? termLabel(t) : t.name || t.command || t.id;
  function runTime(t) {
    const end = t.state === "running" || !t.endedAt ? Date.now() / 1000 : t.endedAt;
    return t.startedAt ? shortTime(end - t.startedAt) : "";
  }
  function stateWords(t) {
    if (t.state === "running") return "";
    if (t.state === "stopped") return "Stopped";
    if (t.state === "failed") return t.exitCode != null ? `Failed · exit ${t.exitCode}` : "Failed to start";
    return t.exitCode != null ? `Exited ${t.exitCode}` : "Exited";
  }
  const span = (cls, text) => { const n = el("span", cls); n.textContent = text; return n; };
  function taskRow(t) {
    const finishedBad = t.state === "failed" || (t.state === "exited" && t.exitCode);
    const row = el("div", `tp-row is-${t.state}${finishedBad ? " is-bad" : ""}`);
    row.dataset.id = t.id;
    const open = el("button", "tp-open");
    open.type = "button";
    open.title = t.command || "";
    const main = el("span", "tp-main");
    main.append(span(`tp-name${t.name || t.kind === "terminal" ? "" : " mono"}`, rowName(t)));
    const meta = [stateWords(t), t.kind === "terminal" ? "Terminal" : t.ownerTitle ? `From ${t.ownerTitle}` : ""].filter(Boolean);
    if (t.name && t.command && t.kind !== "terminal") main.append(span("tp-cmd", t.command));
    if (meta.length) main.append(span(`tp-sub${finishedBad ? " bad" : ""}`, meta.join(" · ")));
    open.append(span("tp-dot", ""), main, span("tp-time", runTime(t)));
    open.setAttribute("aria-label", `${rowName(t)}${meta.length ? `, ${meta.join(", ")}` : ", running"}. Show its output`);
    row.append(open);
    if (t.state === "running" && (t.kind !== "terminal" || terminalsAllowed)) {
      const stop = el("button", "btn small tp-stop");
      stop.type = "button";
      stop.textContent = "Stop";
      stop.setAttribute("aria-label", `Stop ${rowName(t)}`);
      row.append(stop);
    }
    return row;
  }
  function drawTasks() {
    const list = $("taskList");
    const sig = JSON.stringify([terminalsAllowed, tpRows.map((t) => [t.id, t.state, t.name, t.command, t.exitCode, t.ownerTitle])]);
    if (sig !== tpSig) {
      tpSig = sig;
      const had = document.activeElement && list.contains(document.activeElement)
        ? [document.activeElement.closest(".tp-row")?.dataset.id, document.activeElement.classList.contains("tp-stop")] : null;
      list.textContent = "";
      const running = tpRows.filter((t) => t.state === "running"), done = tpRows.filter((t) => t.state !== "running");
      if (!tpRows.length) {
        list.append(span("tp-empty", "No background tasks. agentx starts one when it runs a server or a watcher."));
      } else {
        const group = (cls, title, rows, extra) => {
          const g = el("section", `tp-group ${cls}`);
          const h = el("h3", "tp-group-head");
          h.append(span("", title), span("tp-n", String(rows.length)));
          if (extra) h.append(extra);
          g.append(h);
          if (rows.length) rows.forEach((t) => g.append(taskRow(t)));
          else g.append(span("tp-none", "Nothing running."));
          list.append(g);
        };
        group("running", "Running", running);
        if (done.length) {
          const clear = el("button", "link tp-clear");
          clear.type = "button";
          clear.id = "tasksClear";
          clear.textContent = "Clear finished";
          group("finished", "Finished", done.reverse(), clear);
        }
      }
      if (had && had[0]) {                          // the list was redrawn under the keyboard: keep its place
        const row = list.querySelector(`.tp-row[data-id="${CSS.escape(had[0])}"]`);
        const back = row && ((had[1] && row.querySelector(".tp-stop")) || row.querySelector(".tp-open"));
        if (back) back.focus();
      }
    }
    tickTasks();
    drawTaskOut();
  }
  function tickTasks() {
    for (const row of $("taskList").querySelectorAll(".tp-row.is-running")) {
      const t = tpRows.find((r) => r.id === row.dataset.id);
      if (t) row.querySelector(".tp-time").textContent = runTime(t);
    }
    const t = tpRows.find((r) => r.id === feeds.task.id);
    if (t && !$("taskOut").hidden) $("taskOutMeta").textContent = [stateWords(t) || "Running", runTime(t)].filter(Boolean).join(" · ");
  }
  let tpLoading = false, tpAgain = false;
  async function loadTasks() {
    if (tpLoading) { tpAgain = true; return; }        // one at a time; a call meanwhile asks again once it is back
    tpLoading = true;
    try {
      do {
        tpAgain = false;
        let d;
        try { d = await get("/api/tasks"); } catch (_) { break; }
        tpRows = Array.isArray(d.tasks) ? d.tasks : [];
        terminalsAllowed = d.terminalsAllowed !== false;
        setTasksBadge(tpRows.filter((t) => t.state === "running").length);
        drawTasks();
        drawTerminals();
      } while (tpAgain);
    } finally { tpLoading = false; }
  }
  async function stopTask(id, button) {
    const r = await post("/api/tasks/stop", {id});
    if (refused(r, `${id} could not be stopped`) && button) button.disabled = false;   // to try again
    await loadTasks();
  }
  $("taskList").addEventListener("click", async (e) => {
    if (e.target.closest("#tasksClear")) {
      refused(await post("/api/tasks/clear"), "The finished tasks could not be cleared");
      await loadTasks();
      return;
    }
    const row = e.target.closest(".tp-row");
    if (!row) return;
    const t = tpRows.find((r) => r.id === row.dataset.id);
    const stop = e.target.closest(".tp-stop");
    if (stop) { stop.disabled = true; stopTask(row.dataset.id, stop); return; }
    if (!t) return;
    if (t.kind === "terminal") { termId = t.id; termSig = ""; showTab("terminal"); return; }
    openTaskOut(t.id);
  });

  // an output, live: the newest 2,000 lines, kept at the bottom unless you scrolled up to read
  const feeds = {task: {pre: $("taskOutText"), id: "", offset: 0, text: "", busy: false, gen: 0},
                 term: {pre: $("termOut"), id: "", offset: 0, text: "", busy: false, gen: 0}};
  function feedOpen(f, id) {
    if (f.id === id) return;
    Object.assign(f, {id, offset: 0, text: "", gen: f.gen + 1});
    f.pre.textContent = "";
    if (id) feedPoll(f);
  }
  async function feedPoll(f) {
    if (!f.id || f.busy) return;
    f.busy = true;
    const gen = f.gen;
    try {
      const d = await get(`/api/tasks/output?id=${encodeURIComponent(f.id)}&after=${f.offset}`);
      if (gen !== f.gen) return;
      f.offset = Math.max(f.offset, Number(d.offset) || 0);
      if (d.text) feedAppend(f, String(d.text));
    } catch (_) { /* the next second asks again */ } finally { f.busy = false; }
  }
  function feedAppend(f, text) {
    const pre = f.pre;
    const stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    let all = f.text + text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const lines = all.split("\n");
    if (lines.length > MAX_LINES + 1) all = lines.slice(-(MAX_LINES + 1)).join("\n");
    f.text = all;
    pre.textContent = all;
    if (stick) pre.scrollTop = pre.scrollHeight;
  }
  setInterval(() => {
    if (!tpShown()) return;
    if (tpTab === "tasks" && !$("taskOut").hidden) feedPoll(feeds.task);
    if (tpTab === "terminal" && termId) feedPoll(feeds.term);
  }, 1000);
  setInterval(() => { if (tpShown() && tpTab !== "browser") loadTasks(); }, 2000);
  setInterval(() => { if (tpShown("tasks")) tickTasks(); }, 1000);

  function openTaskOut(id) {
    $("taskList").hidden = true;
    $("taskOut").hidden = false;
    feedOpen(feeds.task, id);
    drawTaskOut();
    $("taskBack").focus();
  }
  function closeTaskOut() {
    feedOpen(feeds.task, "");
    $("taskOut").hidden = true;
    $("taskList").hidden = false;
    const back = $("taskList").querySelector(".tp-open");
    if (back) back.focus();
  }
  function drawTaskOut() {
    if ($("taskOut").hidden) return;
    const t = tpRows.find((r) => r.id === feeds.task.id);
    $("taskOutName").textContent = t ? rowName(t) : feeds.task.id;
    $("taskOutName").title = t ? t.command || "" : "";
    $("taskOutStop").hidden = !(t && t.state === "running" && (t.kind !== "terminal" || terminalsAllowed));
    $("taskOutStop").disabled = false;
    $("taskOutMeta").textContent = t ? [stateWords(t) || "Running", runTime(t)].filter(Boolean).join(" · ") : "Cleared";
  }
  $("taskBack").addEventListener("click", closeTaskOut);
  $("taskOutStop").addEventListener("click", () => { $("taskOutStop").disabled = true; stopTask(feeds.task.id, $("taskOutStop")); });

  // -- Terminal: one tab per terminal, New terminal, the output and an input line ---------------------------------
  function drawTerminals() {
    const terms = tpRows.filter((t) => t.kind === "terminal");
    if (!terms.some((t) => t.id === termId)) termId = ((terms.filter((t) => t.state === "running").pop()) || terms[terms.length - 1] || {}).id || "";
    const sig = JSON.stringify([terminalsAllowed, termId, terms.map((t) => [t.id, t.state, t.name])]);
    if (sig !== termSig) {
      termSig = sig;
      const tabs = $("termTabs");
      const focused = document.activeElement && tabs.contains(document.activeElement) ? document.activeElement.closest(".term-tab")?.dataset.id : "";
      tabs.textContent = "";
      for (const t of terms) {
        const on = t.id === termId;
        const tab = el("div", `term-tab${on ? " on" : ""}${t.state === "running" ? "" : " ended"}`);
        tab.dataset.id = t.id;
        const pick = el("button", "term-pick");
        pick.type = "button";
        pick.setAttribute("role", "tab");
        pick.setAttribute("aria-selected", String(on));
        pick.append(span("tp-dot", ""), span("", termLabel(t)));
        if (t.state !== "running") pick.append(span("term-ended", "ended"));
        tab.append(pick);
        if (t.state === "running" && terminalsAllowed) {
          const x = el("button", "term-x", `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`);
          x.type = "button";
          x.setAttribute("aria-label", `Close ${termLabel(t)}`);
          x.title = "Close this terminal";
          tab.append(x);
        }
        tabs.append(tab);
      }
      if (focused) { const back = tabs.querySelector(`.term-tab[data-id="${CSS.escape(focused)}"] .term-pick`); if (back) back.focus(); }
    }
    const t = terms.find((r) => r.id === termId);
    $("termNew").hidden = !terminalsAllowed;
    $("termNote").hidden = terminalsAllowed;
    $("termNote").textContent = "Read only: terminals are off while remote control is on. agentx can still read and use them.";
    $("termEmpty").hidden = terms.length > 0;
    $("termEmpty").textContent = terminalsAllowed ? "No terminals. New terminal opens a shell in this folder." : "No terminals.";
    $("termOut").hidden = !t;
    $("termLine").hidden = !t || !terminalsAllowed;
    const live = !!(t && t.state === "running" && terminalsAllowed);
    const box = $("termInput");
    box.disabled = !live;
    box.placeholder = live ? "Type a command and press Enter · Ctrl+C stops it" : t ? "This terminal has ended" : "";
    feedOpen(feeds.term, termId);
  }
  $("termTabs").addEventListener("click", async (e) => {
    const tab = e.target.closest(".term-tab");
    if (!tab) return;
    if (e.target.closest(".term-x")) {
      refused(await post("/api/terminals/close", {id: tab.dataset.id}), "The terminal could not be closed");
      await loadTasks();
      return;
    }
    termId = tab.dataset.id;
    drawTerminals();
    $("termInput").focus();
  });
  $("termNew").addEventListener("click", async () => {
    $("termNew").disabled = true;
    try {
      const r = await post("/api/terminals");
      if (!refused(r, "The terminal could not start")) termId = r.id;
      await loadTasks();
    } finally { $("termNew").disabled = false; }
    if (!$("termInput").disabled) $("termInput").focus();
  });
  // what you type goes in order, one line at a time (an empty line is "", never nothing)
  let termChain = Promise.resolve(), termHistory = [], termAt = 0;
  $("termLine").addEventListener("submit", (e) => {
    e.preventDefault();
    const box = $("termInput"), id = termId;
    if (!id || box.disabled) return;
    const text = String(box.value || "");
    box.value = "";
    if (text && termHistory[termHistory.length - 1] !== text) termHistory.push(text);
    termHistory = termHistory.slice(-100);
    termAt = termHistory.length;
    termChain = termChain.then(async () => {
      refused(await post("/api/terminals/input", {id, text}), "The terminal did not take the line");
      setTimeout(() => feedPoll(feeds.term), 150);
    });
  });
  $("termInput").addEventListener("keydown", (e) => {
    const box = $("termInput");
    if (e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "c") {   // (Cmd+C stays copy)
      const selected = box.selectionStart !== box.selectionEnd || String(window.getSelection() || "") !== "";
      if (selected || !termId) return;                       // copying what is selected
      e.preventDefault();
      const id = termId;
      termChain = termChain.then(async () => {
        refused(await post("/api/terminals/interrupt", {id}), "The command could not be stopped");
        setTimeout(() => feedPoll(feeds.term), 200);
      });
      return;
    }
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && termHistory.length) {
      e.preventDefault();
      termAt = Math.max(0, Math.min(termHistory.length, termAt + (e.key === "ArrowUp" ? -1 : 1)));
      box.value = termHistory[termAt] || "";
    }
  });

  // -- Browser: the page of the conversation on screen, live; Take over sends your clicks and keys to it ----------
  let bw = {available: true, open: false, url: "", title: "", user: false, frame: 0, takeAllowed: true, error: ""};
  let bwShown = 0, bwBlob = "", bwBusy = false, frameRun = 0, frameSeq = 0;
  // The mark of the conversation whose page is shown (from the state; the frame carries it too). Each conversation's
  // page counts its pictures on its own, so after a switch the old picture could pass for the new one, and a click
  // meant for it could land on the other page: a new mark drops the picture, the take-over and what was queued.
  let bwOwner = null, bwLoading = false, bwAgain = false;
  function pageChanged(owner) {
    bwOwner = owner;
    clearFrame();
    bwQueue.length = 0;
    bw = Object.assign({}, bw, {open: false, user: false, url: "", title: "", frame: 0});
  }
  async function loadBrowser() {
    if (bwLoading) { bwAgain = true; return; }        // one at a time, so answers cannot land out of order
    bwLoading = true;
    try {
      do {
        bwAgain = false;
        let d;
        try { d = await get("/api/browser/state"); } catch (_) { break; }
        if (!d) break;
        if ((d.ownerMark || "") !== bwOwner) pageChanged(d.ownerMark || "");
        bw = d;
        drawBrowser();
      } while (bwAgain);
    } finally { bwLoading = false; }
  }
  // The page you took over goes back to the agent before the window shows another conversation (a switch, a lane,
  // a reload, the window closing): by its mark, since the conversation on screen may have changed already. keepalive
  // lets it finish while the page unloads. (Should it never arrive, the hold lapses once no window shows the page.)
  function giveBackHeld() {
    if (!bw.user || !bwOwner) return;
    const body = JSON.stringify({owner: bwOwner});
    bw = Object.assign({}, bw, {user: false});
    try {
      fetch("/api/browser/give", {method: "POST", keepalive: true, body,
                                  headers: {"Content-Type": "application/json", ...authHeaders()}}).catch(() => {});
    } catch (_) { /* the hold lapses by itself */ }
  }
  window.addEventListener("pagehide", giveBackHeld);
  // the conversation on screen changed (a switch, or it was reloaded): forget the page until the state says whose
  function browserMoved() {
    giveBackHeld();
    pageChanged(null);
    drawBrowser();
    if (tpShown("browser")) loadBrowser();
  }
  setInterval(() => { if (tpShown("browser")) loadBrowser(); }, 1000);
  function clearFrame() {
    const img = $("bwFrame");
    img.hidden = true;
    img.removeAttribute("src");
    delete img.dataset.owner;
    if (bwBlob) URL.revokeObjectURL(bwBlob);
    bwBlob = "";
    bwShown = 0;
  }
  function drawBrowser() {
    const open = !!bw.open && bw.available !== false, mine = !!bw.user, may = bw.takeAllowed !== false;
    if (!open && bwBlob) clearFrame();
    $("bwBack").disabled = $("bwReload").disabled = !open || !may;
    const url = $("bwUrl");
    url.disabled = !may || bw.available === false;
    if (document.activeElement !== url) url.value = open ? bw.url || "" : "";
    url.placeholder = may ? "Type an address and press Enter" : "Watching only";
    $("bwTitle").parentElement.hidden = !open && !mine;
    $("bwTitle").textContent = open ? bw.title || bw.url || "" : "";
    $("bwTitle").title = open ? bw.url || "" : "";
    $("bwTake").hidden = !open || mine || !may;
    $("bwGive").hidden = !mine;
    const note = $("bwNote");
    note.textContent = bw.available === false ? "" : !may ? "Watching only: taking over the browser is off while remote control is on."
      : mine ? "You have the page. Clicks, scrolling and keys on it go to the page; agentx waits until you give it back. Shift+Tab leaves it." : "";
    note.hidden = !note.textContent;
    $("bwStage").classList.toggle("taken", mine && open);
    $("bwFrame").hidden = !open || !bwBlob;
    const empty = $("bwEmpty");
    empty.hidden = open && !!bwBlob;
    empty.textContent = bw.available === false ? `The browser is not installed. ${bw.error || ""}`.trim()
      : open ? "Loading the page…" : may ? "agentx has not opened a page yet. Type an address to open one yourself." : "agentx has not opened a page yet.";
    if (open && tpShown("browser")) startFrames();
  }
  // the picture: one request at a time, about 3 a second, only while the Browser tab is on screen
  function startFrames() {
    if (frameRun) return;
    frameRun = ++frameSeq;
    loopFrames(frameRun);
  }
  function stopFrames() { frameRun = 0; }
  async function loopFrames(me) {
    while (frameRun === me && tpShown("browser")) {
      const started = Date.now();
      if (bw.open) await fetchFrame();
      await new Promise((done) => setTimeout(done, Math.max(100, 333 - (Date.now() - started))));
    }
    if (frameRun === me) frameRun = 0;
  }
  async function fetchFrame() {
    if (bwBusy) return;
    bwBusy = true;
    try {
      const asked = bwOwner;
      const res = await fetch(`/api/browser/frame?since=${bwShown}&owner=${encodeURIComponent(asked || "")}`,
                              {headers: authHeaders(), cache: "no-store"});
      const mark = res.headers.get("X-Frame-Owner") || "";
      if (asked === null || mark !== asked || bwOwner !== asked) {   // another conversation's page: ask whose it is
        loadBrowser();
        return;
      }
      if (res.status !== 200) return;
      const counter = Number(res.headers.get("X-Frame") || 0);
      const blob = await res.blob();
      if (!bw.open || bwOwner !== asked) return;
      const url = URL.createObjectURL(blob), old = bwBlob, img = $("bwFrame");
      bwBlob = url;
      img.src = url;
      img.dataset.owner = mark;
      if (old) URL.revokeObjectURL(old);
      bwShown = counter;
      img.hidden = false;
      $("bwEmpty").hidden = true;
    } catch (_) { /* the next one asks again */ } finally { bwBusy = false; }
  }
  // what you do on the page goes in order; typed characters and wheel turns are joined while one is on its way
  const bwQueue = [];
  let bwPumping = false, wheelDy = 0, wheelAt = null, wheelTimer = 0;
  function bwSend(ev) {
    const last = bwQueue[bwQueue.length - 1], owner = bwOwner;
    const same = last && last.owner === owner && last.ev.type === ev.type;
    if (same && ev.type === "text") last.ev.text += ev.text;
    else if (same && ev.type === "wheel") Object.assign(last.ev, {dy: last.ev.dy + ev.dy, x: ev.x, y: ev.y});
    else bwQueue.push({ev: Object.assign({}, ev), owner});
    pumpBrowser();
  }
  async function pumpBrowser() {
    if (bwPumping) return;
    bwPumping = true;
    try {
      while (bwQueue.length) {
        const {ev, owner} = bwQueue.shift();
        if (owner !== bwOwner) continue;              // meant for a page no longer on screen
        const body = owner ? {event: ev, owner} : {event: ev};
        if (refused(await post("/api/browser/input", body), "The page did not take that") && ev.type !== "navigate") bwQueue.length = 0;
      }
    } finally { bwPumping = false; }
    loadBrowser();
  }
  function framePoint(e) {
    const img = $("bwFrame"), r = img.getBoundingClientRect();
    const nw = img.naturalWidth || VIEW_W, nh = img.naturalHeight || VIEW_H;
    const scale = Math.min(r.width / nw, r.height / nh);          // object-fit: contain, so allow for the bands
    if (!scale) return null;
    const x = (e.clientX - r.left - (r.width - nw * scale) / 2) / scale, y = (e.clientY - r.top - (r.height - nh * scale) / 2) / scale;
    if (x < 0 || y < 0 || x > nw || y > nh) return null;
    return {x: Math.round(x * VIEW_W / nw), y: Math.round(y * VIEW_H / nh)};
  }
  $("bwFrame").addEventListener("click", (e) => {
    $("bwFrame").focus();
    if (!bw.user) return;
    const at = framePoint(e);
    if (at) bwSend({type: "click", x: at.x, y: at.y});
  });
  $("bwFrame").addEventListener("wheel", (e) => {
    if (!bw.user) return;
    e.preventDefault();
    wheelDy += e.deltaMode === 1 ? e.deltaY * 40 : e.deltaMode === 2 ? e.deltaY * VIEW_H : e.deltaY;
    wheelAt = framePoint(e) || wheelAt;
    if (wheelTimer) return;
    wheelTimer = setTimeout(() => {                   // at most one turn of the wheel every 120 ms
      wheelTimer = 0;
      const dy = Math.round(wheelDy);
      wheelDy = 0;
      if (dy) bwSend(Object.assign({type: "wheel", dy}, wheelAt || {}));
    }, 120);
  }, {passive: false});
  const KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "Insert", "Home", "End", "PageUp", "PageDown",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"]);
  $("bwFrame").addEventListener("keydown", (e) => {
    if (!bw.user || e.isComposing) return;
    if (e.key === "Tab" && e.shiftKey) return;        // the way out of the page with the keyboard
    const altGr = e.ctrlKey && e.altKey;                // a character typed with AltGr
    if (e.key.length === 1 && (altGr || (!e.ctrlKey && !e.metaKey && !e.altKey))) {
      e.preventDefault(); e.stopPropagation();
      bwSend({type: "text", text: e.key});
      return;
    }
    const mods = [e.ctrlKey && "Control", e.altKey && "Alt", e.metaKey && "Meta"].filter(Boolean);
    if (KEYS.has(e.key) || (e.key.length === 1 && mods.length)) {
      e.preventDefault(); e.stopPropagation();
      const shift = e.shiftKey && (KEYS.has(e.key) || mods.length) ? ["Shift"] : [];
      bwSend({type: "key", key: [...mods, ...shift, e.key].join("+")});
    }
  });
  function navTarget(text) {
    const s = String(text || "").trim();
    if (!s) return "";
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(s) || /^(about|data|file|javascript):/i.test(s)) return s;
    return (/^(localhost|127\.|\[::1\])/i.test(s) ? "http://" : "https://") + s;
  }
  $("bwBar").addEventListener("submit", (e) => {
    e.preventDefault();
    const url = navTarget($("bwUrl").value);
    if (!url) return;
    $("bwUrl").value = url;
    $("bwUrl").blur();
    $("bwEmpty").textContent = "Opening the page…";
    bwSend({type: "navigate", url});
  });
  $("bwBack").addEventListener("click", () => bwSend({type: "back"}));
  $("bwReload").addEventListener("click", () => bwSend({type: "reload"}));
  $("bwTake").addEventListener("click", async () => {
    const owner = bwOwner;                            // the page on screen: refused if the conversation changed
    if (refused(await post("/api/browser/take", owner ? {owner} : {}), "The page could not be taken over")) return;
    if (owner !== bwOwner) return;
    bw.user = true;
    drawBrowser();
    $("bwFrame").focus();
    loadBrowser();
  });
  $("bwGive").addEventListener("click", async () => {
    refused(await post("/api/browser/give", bwOwner ? {owner: bwOwner} : {}), "The page could not be given back");
    bw.user = false;
    drawBrowser();
    $("bwTake").focus();
    loadBrowser();
  });

  // -- the Settings sheet: saved to the settings files, for every folder or for this one ----------------------------
  let settingsData = null, scope = "user";
  async function openSettings() {
    try { settingsData = await get("/api/settings"); } catch (_) { return; }
    keysData = null;
    $("settingsWrap").hidden = false;
    $("settingsMsg").textContent = "";
    $("scopeFolder").disabled = !settingsData.canFolder;
    drawSettings();
    setTimeout(() => $("settingsForm").querySelector("input, textarea, button")?.focus(), 0);
  }
  function closeSettings() { $("settingsWrap").hidden = true; $("settingsBtn").focus(); }
  function drawSettings() {
    document.querySelectorAll(".scope [data-scope]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.scope === scope)));
    $("settingsSave").hidden = scope === "keys";
    if (scope === "keys") return drawKeys();
    const file = settingsData[scope], v = file.values || {}, eff = settingsData.effective;
    const folderModes = scope === "folder";
    const lines = (list) => esc((list || []).join("\n"));
    $("settingsForm").innerHTML = `
      <p class="s-file">${scope === "user" ? "Used in every folder, unless a folder sets its own." : "Only for this folder; wins over the all-folders settings."}
        <br><code>${esc(file.path)}</code>${file.exists ? "" : " (created when you save)"}${file.error ? ` <span class="s-err">${esc(file.error)}</span>` : ""}</p>
      <label class="s-field"><span class="s-label">Default model</span>
        <input type="text" name="model" value="${esc(v.model || "")}" placeholder="${esc(eff.model || "for example claude-opus-5-5")}" list="modelList" spellcheck="false">
        <span class="s-hint">Used when agentx starts here. The model menu under the message box changes this conversation only.</span></label>
      <datalist id="modelList">${models.map((m) => `<option value="${esc(m)}">`).join("")}</datalist>
      <fieldset class="s-field"><legend class="s-label">Default mode</legend><div class="s-modes">
        <label class="s-mode"><input type="radio" name="mode" value=""${!v.mode ? " checked" : ""}><span><b>Not set</b><small>${scope === "folder" ? "Use the all-folders setting" : "Ask before changes"}</small></span></label>
        ${MODES.map(([value, label, hint]) => `<label class="s-mode${folderModes && (value === "accept-edits" || value === "auto") ? " off" : ""}">
          <input type="radio" name="mode" value="${value}"${v.mode === value ? " checked" : ""}${folderModes && (value === "accept-edits" || value === "auto") ? " disabled" : ""}>
          <span><b>${esc(label)}</b><small>${esc(hint)}</small></span></label>`).join("")}</div>
        ${folderModes ? `<span class="s-hint">A folder's own settings cannot turn on Accept edits or Auto; that stays your choice.</span>` : ""}</fieldset>
      <label class="s-field"><span class="s-label">Always allow</span>
        <textarea name="allow" rows="3" spellcheck="false" placeholder="one rule per line, for example run_shell:npm test*">${lines(v.allow)}</textarea>
        <span class="s-hint">These run without asking. A rule is a tool, optionally with <code>:pattern</code>.</span></label>
      <label class="s-field"><span class="s-label">Never allow</span>
        <textarea name="deny" rows="3" spellcheck="false" placeholder="for example read_path:.env*">${lines(v.deny)}</textarea>
        <span class="s-hint">Refused every time, in every mode.</span></label>
      <div class="s-two">
        <label class="s-field"><span class="s-label">Steps per message</span>
          <input type="number" name="maxSteps" min="1" max="2000" value="${esc(v.maxSteps ?? "")}" placeholder="${esc(eff.maxSteps)}"></label>
        <label class="s-field"><span class="s-label">Token limit per message</span>
          <input type="number" name="tokenLimit" min="0" value="${esc(v.tokenLimit ?? "")}" placeholder="${esc(eff.tokenLimit || 0)}">
          <span class="s-hint">0 means no limit${folderModes ? "; a folder can only lower it" : ""}.</span></label>
      </div>
      ${scope === "user" ? localFields(v, eff) : ""}
      ${settingsData.problems.length ? `<div class="s-problems"><b>Problems in the settings files</b>${settingsData.problems.map((p) => `<p>${esc(p)}</p>`).join("")}</div>` : ""}
      <p class="s-hint">Rules and limits take effect at once. The model and mode${scope === "user" ? " and the local model settings" : ""} are used from the next start.</p>`;
  }
  // Local models (Ollama, LM Studio): only your own settings file may say where they run, so a project you cloned
  // cannot send the conversation elsewhere. The fallback is the local model a conversation continues on when Claude
  // or GPT fails for good.
  function localFields(v, eff) {
    const off = v.fallbackModel === "";
    return `<fieldset class="s-field s-local"><legend class="s-label">Local models</legend>
      <span class="s-hint">Models on this computer, through Ollama or LM Studio. Only these all-folders settings can set them.</span>
      <label class="s-field"><span class="s-sub">Local server</span>
        <input type="text" name="localServer" value="${esc(typeof v.localServer === "string" ? v.localServer : "")}" placeholder="${esc(eff.localServer || "http://localhost:11434/v1")}" spellcheck="false">
        <span class="s-hint">Where Ollama or LM Studio is running. agentx uses a server that is already running; it never starts one.</span></label>
      <div class="s-two">
        <label class="s-field"><span class="s-sub">Fallback model</span>
          <input type="text" name="fallbackModel" value="${esc(typeof v.fallbackModel === "string" ? v.fallbackModel : "")}" placeholder="${off ? "off" : "none"}" list="localModelList" spellcheck="false">
          <span class="s-hint">When Claude or GPT fails for good, the conversation continues on it, marked. Empty is off; <code>/fallback</code> changes it now.</span></label>
        <label class="s-field"><span class="s-sub">Context window</span>
          <input type="number" name="localContext" min="1" value="${esc(v.localContext ?? "")}" placeholder="what the server says">
          <span class="s-hint">Tokens. Set it when the server does not say, or loads models with a larger window.</span></label>
      </div>
      <label class="s-field"><span class="s-sub">Tools on a local model</span>
        <select name="localTools">
          <option value=""${v.localTools !== "lean" && v.localTools !== "all" ? " selected" : ""}>Not set (lean)</option>
          <option value="lean"${v.localTools === "lean" ? " selected" : ""}>Lean: the core tools and a short skill list</option>
          <option value="all"${v.localTools === "all" ? " selected" : ""}>All: the web and MCP tools and every skill too</option>
        </select>
        <span class="s-hint">Lean leaves room in a small window for the conversation; <code>use_skill</code> still opens every skill.</span></label>
      <datalist id="localModelList">${(settingsData.localModels || []).map((m) => `<option value="${esc(m)}">`).join("")}</datalist>
    </fieldset>`;
  }
  // API keys: kept encrypted by the system's credential store; the page only ever sees the last four characters
  let keysData = null;
  async function drawKeys() {
    if (!keysData) { $("settingsForm").innerHTML = '<p class="s-hint">Loading…</p>'; try { keysData = await get("/api/keys"); } catch (_) { return; } }
    if (scope !== "keys") return;
    const where = {saved: "Saved in agentx", environment: "From your environment"};
    $("settingsForm").innerHTML = `
      <p class="s-file">Your keys for the models. They are kept encrypted by your system's credential store
        (Windows Credential Manager), never in a file, and only the last four characters are shown here.
        A key saved here wins over the environment variable, and agentx uses it from your next message.</p>
      ${keysData.store ? "" : `<p class="s-err">Saving keys needs the keyring package: <code>pip install keyring</code></p>`}
      ${keysData.keys.map((k) => `<div class="s-key" data-provider="${esc(k.provider)}">
        <div class="s-key-head"><span class="s-label">${esc(k.label)}</span>
          <span class="s-key-state ${k.set ? "on" : ""}">${k.set ? `${where[k.source] || "Set"}${k.tail ? ` · ends in <code>${esc(k.tail)}</code>` : ""}` : "Not set"}</span></div>
        <div class="s-key-row">
          <input type="password" name="key-${esc(k.provider)}" placeholder="${k.set ? "Paste a new key to replace it" : "Paste your key"}" autocomplete="off" spellcheck="false" aria-label="${esc(k.label)} API key">
          <button type="button" class="btn primary small" data-key-save="${esc(k.provider)}">${k.source === "saved" ? "Replace" : "Save"}</button>
          ${k.source === "saved" ? `<button type="button" class="btn small" data-key-remove="${esc(k.provider)}">Remove</button>` : ""}
        </div>
        <span class="s-hint">${esc(k.variable)}${k.shadows ? " is also set in your environment; the saved key wins." : ""}</span>
      </div>`).join("")}`;
  }
  async function changeKey(provider, remove) {
    const field = $("settingsForm").querySelector(`input[name="key-${provider}"]`);
    const key = remove ? null : (field ? field.value : "");
    if (!remove && !String(key || "").trim()) { $("settingsMsg").textContent = "Paste a key first."; $("settingsMsg").className = "sheet-msg bad"; return; }
    const res = await post("/api/keys", remove ? {provider, remove: true} : {provider, key});
    if (field) field.value = "";                   // the key leaves the page as soon as it is sent
    $("settingsMsg").textContent = res.message || (res.ok ? "Saved" : "Not saved");
    $("settingsMsg").className = `sheet-msg ${res.ok ? "ok" : "bad"}`;
    if (res.ok && res.keys) { keysData = {keys: res.keys, store: res.store}; drawKeys(); }
  }
  $("settingsForm").addEventListener("click", (e) => {
    const save = e.target.closest("[data-key-save]"), drop = e.target.closest("[data-key-remove]");
    if (save) changeKey(save.dataset.keySave, false);
    else if (drop) changeKey(drop.dataset.keyRemove, true);
  });
  $("settingsForm").addEventListener("keydown", (e) => {
    const field = e.target.closest && e.target.closest('input[name^="key-"]');
    if (field && e.key === "Enter") { e.preventDefault(); changeKey(field.name.slice(4), false); }
  });
  async function saveSettings() {
    const f = new FormData($("settingsForm"));
    const split = (s) => String(s || "").split("\n").map((x) => x.trim()).filter(Boolean);
    const values = {model: f.get("model") || "", mode: f.get("mode") || "", allow: split(f.get("allow")), deny: split(f.get("deny")),
                    maxSteps: f.get("maxSteps") || "", tokenLimit: f.get("tokenLimit") || ""};
    if (scope === "user") {                        // your own file only (see localFields)
      values.localServer = String(f.get("localServer") || "").trim();
      values.localContext = f.get("localContext") || "";
      values.localTools = f.get("localTools") || "";
      // an empty field turns the fallback off, which also ends the offer: sent only when it was changed
      const before = settingsData.user.values.fallbackModel, now = String(f.get("fallbackModel") || "").trim();
      if (now !== (typeof before === "string" ? before : "")) values.fallbackModel = now;
    }
    $("settingsSave").disabled = true;
    const res = await post("/api/settings", {scope, values});
    $("settingsSave").disabled = false;
    $("settingsMsg").textContent = res.ok ? "Saved" : (res.message || "Not saved");
    $("settingsMsg").className = `sheet-msg ${res.ok ? "ok" : "bad"}`;
    if (res.ok && res.view) { settingsData = res.view; drawSettings(); }
  }
  $("settingsBtn").addEventListener("click", openSettings);
  $("settingsClose").addEventListener("click", closeSettings);
  $("settingsCancel").addEventListener("click", closeSettings);
  $("settingsSave").addEventListener("click", saveSettings);
  $("settingsForm").addEventListener("submit", (e) => { e.preventDefault(); if (scope !== "keys") saveSettings(); });
  $("settingsWrap").addEventListener("mousedown", (e) => { if (e.target === $("settingsWrap")) closeSettings(); });
  $("settingsWrap").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeSettings(); } });
  document.querySelector(".scope").addEventListener("click", (e) => {
    const b = e.target.closest("[data-scope]");
    if (b && !b.disabled && b.dataset.scope !== scope) { scope = b.dataset.scope; $("settingsMsg").textContent = ""; drawSettings(); }
  });

  // -- the + menu, the folder menu, mentioning a file, another folder ---------------------------------------------
  async function pickFile() {
    if (closed) return null;
    return new Promise((resolve) => {
      const overlay = $("overlay"), box = $("overlayBox"), back = document.activeElement;
      box.innerHTML = `<h2 id="dlgTitle">Mention a file</h2><p>The path goes into your message, so agentx knows which file you mean.</p>
        <input type="text" class="dlg-input" placeholder="Type part of a name" aria-labelledby="dlgTitle" spellcheck="false" autocomplete="off">
        <ul class="pick" role="listbox"></ul><div class="dlg-actions"><button type="button" class="btn" data-d="cancel">Cancel</button></div>`;
      overlay.hidden = false;
      const field = box.querySelector("input"), list = box.querySelector(".pick");
      let hits = [], at = 0, seq = 0;
      const draw = () => { list.innerHTML = hits.length ? hits.map((p, n) => `<li role="option" data-n="${n}" aria-selected="${n === at}">${esc(p)}</li>`).join("")
        : `<li class="none">No file matches.</li>`; };
      const search = async () => {
        const mine = ++seq;
        let found = [];
        try { found = await get(`/api/files?q=${encodeURIComponent(field.value)}`); } catch (_) { /* keep the list */ }
        if (mine !== seq) return;
        hits = found; at = 0; draw();
      };
      const done = (value) => { overlay.hidden = true; box.onclick = box.onkeydown = overlay.onmousedown = null; resolve(value); if (back && back.focus) back.focus(); };
      field.addEventListener("input", search);
      box.onclick = (e) => {
        const it = e.target.closest("[data-n]"); if (it) return done(hits[Number(it.dataset.n)]);
        if (e.target.closest('[data-d="cancel"]')) done(null);
      };
      overlay.onmousedown = (e) => { if (e.target === overlay) done(null); };
      box.onkeydown = (e) => {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(null); }
        else if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); if (hits.length) { at = (at + (e.key === "ArrowDown" ? 1 : -1) + hits.length) % hits.length; draw(); list.children[at].scrollIntoView({block: "nearest"}); } }
        else if (e.key === "Enter" && hits.length) { e.preventDefault(); done(hits[at]); }
      };
      field.focus();
      search();
    });
  }
  async function openAnotherFolder() {
    const path = await askText({title: "Open another folder", text: "agentx starts there in a window of its own; this one keeps running.",
                                placeholder: "for example C:\\Users\\you\\projects\\shop", ok: "Open"});
    if (!path) return;
    const res = await post("/api/open-folder", {path});
    addNotice({level: res.ok ? "info" : "error", text: res.message || "The folder could not be opened."});
  }
  function insertText(text) {
    input.value = (input.value && !/\s$/.test(input.value) ? input.value + " " : input.value) + text + " ";
    grow(); setComposer(); input.focus();
  }
  $("attachBtn").addEventListener("click", () => openMenu($("attachBtn"), [
    {value: "picture", label: "Add a picture", hint: "PNG, JPEG, GIF or WebP; or paste one"},
    {value: "file", label: "Mention a file", hint: "Put a file's path in your message"},
    {value: "folder", label: "Open another folder…", hint: "agentx in another project, in a new window"},
  ], async (it) => {
    if (it.value === "picture") $("fileInput").click();
    else if (it.value === "file") { const p = await pickFile(); if (p) insertText(p); }
    else openAnotherFolder();
  }));
  $("folder").addEventListener("click", () => openMenu($("folder"), [
    {value: "open", label: "Open another folder…", hint: "agentx in another project, in a new window"},
    ...(EMBED ? [] : [{value: "beside", label: "Open a folder beside…", hint: "Another project on the right half of this window"}]),
    {value: "copy", label: "Copy this folder's path", hint: $("folder").title},
  ], async (it) => {
    if (it.value === "open") openAnotherFolder();
    else if (it.value === "beside") openFolderBeside();
    else { try { await navigator.clipboard.writeText($("folder").title); } catch (_) { /* no clipboard */ } }
  }));
  // the sidebar: slides over on a narrow window, folds away on a wide one (remembered)
  const narrow = () => matchMedia("(max-width: 860px)").matches;
  try { if (localStorage.getItem("agentx-side") === "hidden") document.querySelector(".app").classList.add("side-hidden"); } catch (_) { /* no storage */ }
  function toggleSide() {
    if (narrow()) { $("side").classList.toggle("open"); return; }
    const hidden = document.querySelector(".app").classList.toggle("side-hidden");
    try { localStorage.setItem("agentx-side", hidden ? "hidden" : "shown"); } catch (_) { /* no storage */ }
  }
  $("sideToggle").addEventListener("click", toggleSide);
  $("sideClose").addEventListener("click", toggleSide);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") { e.preventDefault(); toggleSide(); }
  });

  // light or dark follows the system, and changes with it (the stylesheet's prefers-color-scheme does that live)
  try { localStorage.removeItem("agentx-theme"); } catch (_) { /* no storage */ }

  async function loadState() {
    try {
      const s = await get("/api/state");
      if (LANE && s.lane !== LANE) { showLane(""); return; }     // that conversation was put away: show the own one
      folderName = s.folderName || "";
      $("folderName").textContent = folderName;
      $("folder").title = s.folder || "";
      if (sessionList.length) drawSessions();                  // the folder row of the list carries its name
      const h = column.querySelector(".welcome h1");
      if (h && folderName) h.textContent = `What should we work on in ${folderName}?`;
      document.title = `agentx · ${s.folderName || ""}`;
      commands = s.commands || {};
      skills = s.skills || {};
      models = (s.models || []).map(String);
      localModels = (s.local_models || []).map(String);
      fallbackFrom = s.fallback_from || null;
      if (s.model) setModel(s.model); else setModel(currentModel);
      if (s.mode) setMode(s.mode);
      live = s.live || {};
      drawSessions();
    } catch (_) { /* the app is still starting: the next status brings it */ }
  }
  // Each device has its own conversation. The one running on the computer (or in the first window) is listed on
  // top as "Live": opening it shares it with whoever has it; "Your own conversation" goes back.
  let sessionList = [], live = {};
  let treeOpen = (() => { try { return localStorage.getItem("agentx-tree") !== "closed"; } catch (_) { return true; } })();
  async function loadSessions() {
    try { sessionList = await get("/api/sessions"); } catch (_) { return; }
    drawSessions();
  }
  function drawSessions() {
    const nav = $("sessions");
    const top = live.joined
      ? `<button type="button" class="session live-entry" data-live="leave"><span class="session-title">Your own conversation</span>` +
        `<span class="session-sub">Back to this device's conversation</span></button>`
      : live.available
        ? `<button type="button" class="session live-entry" data-live="join"><span class="session-title">` +
          `<span class="live-dot${live.working ? " working" : ""}" aria-hidden="true"></span>Live on ${esc(live.where || "the computer")}</span>` +
          `<span class="session-sub">${esc(live.title || "The conversation running there")} · open to share it</span></button>`
        : "";
    // One row per conversation, hanging from the folder (the main branch) on an L-shaped line; a conversation
    // opened while another was working hangs from that one. A dot says it works (or waits for you) behind this one.
    const row = (s) => `<div class="session-row${beside.session === s.id ? " beside-open" : ""}">` +
      `<button type="button" class="session" data-id="${esc(s.id)}" aria-current="${s.current}" draggable="${!s.current && !EMBED}" ` +
      `title="${esc(String(s.updated).replace("T", " ").slice(0, 16))}${s.current || EMBED ? "" : " · drag onto the chat to open it beside"}">` +
      `<span class="session-title">${typeof s.lane === "string" && !s.current && (s.working || s.asking)
        ? `<span class="session-run ${s.asking ? "asking" : "working"}" title="${s.asking ? "Waiting for you" : "Working"}" ` +
          `aria-label="${s.asking ? "waiting for you" : "working"}"></span>` : ""}${esc(s.title)}</span></button>` +
      (EMBED ? "" : `<button type="button" class="session-more" data-more="${esc(s.id)}" title="More: rename, open beside, delete" ` +
        `aria-label="More for ${esc(s.title)}" aria-haspopup="menu">` +
        `<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="3.5" cy="8" r="1.3" fill="currentColor"/><circle cx="8" cy="8" r="1.3" fill="currentColor"/><circle cx="12.5" cy="8" r="1.3" fill="currentColor"/></svg></button>`) +
      `</div>`;
    const ids = new Set(sessionList.map((s) => s.id));
    const under = (parent) => sessionList.filter((s) => parent ? s.from === parent : !(s.from && ids.has(s.from) && s.from !== s.id));
    const branch = (list, depth) => list.length && depth < 6
      ? `<ul class="tree">${list.map((s) => `<li>${row(s)}${branch(under(s.id), depth + 1)}</li>`).join("")}</ul>` : "";
    const rows = sessionList.length
      ? `<button type="button" class="tree-folder" aria-expanded="${treeOpen}" title="${esc($("folder").title || "")}">` +
        `<svg class="tree-chev" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>` +
        `<span class="tree-folder-name">${esc(folderName || "This folder")}</span>` +
        `<span class="tree-count">${sessionList.length}</span></button>` + (treeOpen ? branch(under(""), 0) : "")
      : "";
    nav.innerHTML = top + (rows || '<div class="sessions-empty">Conversations in this folder show up here.</div>');
    const current = sessionList.find((s) => s.current);
    if (live.joined) { $("topTitle").textContent = `Shared · ${current ? current.title : "live conversation"}`; $("topTitle").dataset.fixed = "1"; }
    else if (current) { $("topTitle").textContent = current.title; $("topTitle").dataset.fixed = "1"; }
    else { delete $("topTitle").dataset.fixed; }
  }
  // Opening or leaving the shared conversation changes what this window shows: it reloads (the key and the
  // device stay), and what was asked for (another conversation, a new one) is done in this device's own.
  setInterval(() => {               // other devices' conversations, the live one's title and whether it works
    loadSessions();
    if (live.available || live.joined) loadState();
  }, 10000);
  setInterval(() => {               // a conversation working behind this one: Recent shows when it finishes or asks
    if (sessionList.some((s) => typeof s.lane === "string" && !s.current)) loadSessions();
  }, 2500);
  async function joinLive(on, then) {
    await post("/api/join", {live: on, then: then || ""});
    location.reload();
  }
  // -- edit a message you sent: change it and send it again. The conversation goes back to just before it (the
  // whole conversation as it was is kept in Recent as "... (before edit)"); files agentx changed stay changed. ------
  function userText(u) { return [...u.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join(""); }
  column.addEventListener("click", (e) => {
    const b = e.target.closest(".u-edit");
    if (!b) return;
    const u = b.closest(".user");
    if (!u || u.classList.contains("editing")) return;
    if ($("composer").classList.contains("busy")) {
      addNotice({level: "warn", text: "agentx is working: stop it or wait, then edit a message."});
      return;
    }
    const original = userText(u);
    const n = [...column.querySelectorAll(".user:not(.command)")].indexOf(u) + 1;
    const saved = [...u.childNodes];
    u.classList.add("editing");
    u.textContent = "";
    const box = el("textarea", "u-field");
    box.value = original;
    box.setAttribute("aria-label", "Edit your message");
    const note = el("p", "u-note");
    note.textContent = "Sends this instead, and the conversation goes back to just before it. The conversation as it is now is kept in Recent (“… (before edit)”). Files agentx changed stay changed.";
    const acts = el("div", "u-acts");
    acts.innerHTML = `<button type="button" class="u-cancel">Cancel</button><button type="button" class="u-send">Send</button>`;
    u.append(box, note, acts);
    const fit = () => { box.style.height = "auto"; box.style.height = `${Math.min(box.scrollHeight, window.innerHeight * 0.5)}px`; };
    fit(); box.addEventListener("input", fit);
    box.focus(); box.setSelectionRange(box.value.length, box.value.length);
    const cancel = () => { u.classList.remove("editing"); u.textContent = ""; saved.forEach((c) => u.append(c)); };
    const send = async () => {
      const text = box.value.trim();
      if (!text) return;
      if (text === original.trim()) { cancel(); return; }
      acts.querySelectorAll("button").forEach((x) => { x.disabled = true; });
      const res = await post("/api/edit", {n, text: original}).catch(() => null);
      if (!res || !res.ok) {
        acts.querySelectorAll("button").forEach((x) => { x.disabled = false; });
        addNotice({level: "error", text: `Could not edit it: ${(res && res.error) || "no answer"}.`});
        return;
      }
      await reloadConversation();
      post("/api/send", {text});
    };
    acts.querySelector(".u-cancel").addEventListener("click", cancel);
    acts.querySelector(".u-send").addEventListener("click", send);
    box.addEventListener("keydown", (k) => {
      k.stopPropagation();
      if (k.key === "Escape") { k.preventDefault(); cancel(); }
      else if (k.key === "Enter" && !k.shiftKey && !k.isComposing) { k.preventDefault(); send(); }
    });
  });

  // -- the menu on a conversation in Recent: rename it, open it beside, delete it (it is moved, not erased) -----
  function closeRowMenu() { const m = document.querySelector(".row-menu"); if (m) m.remove(); }
  function rowMenu(button) {
    closeRowMenu();
    const id = button.dataset.more;
    const s = sessionList.find((x) => x.id === id);
    if (!s) return;
    const open = typeof s.lane === "string";
    const menu = el("div", "row-menu");
    menu.setAttribute("role", "menu");
    menu.innerHTML = `<button type="button" role="menuitem" data-rm="rename">Rename</button>` +
      (s.current ? "" : `<button type="button" role="menuitem" data-rm="beside">Open beside</button>`) +
      `<button type="button" role="menuitem" class="danger" data-rm="delete"${open ? ' disabled title="It is open: switch to another first"' : ""}>Delete</button>`;
    const box = button.getBoundingClientRect(), side = $("side").getBoundingClientRect();
    menu.style.top = `${box.bottom - side.top + 4}px`;
    menu.style.right = `${side.right - box.right}px`;
    $("side").append(menu);
    menu.querySelector("button").focus();
    menu.addEventListener("click", async (e) => {
      e.stopPropagation();                         // its content may change below: not a click outside it
      const b = e.target.closest("[data-rm]");
      if (!b || b.disabled) return;
      if (b.dataset.rm === "beside") { closeRowMenu(); openBeside({session: id}); $("side").classList.remove("open"); return; }
      if (b.dataset.rm === "rename") { closeRowMenu(); renameRow(id); return; }
      if (b.dataset.rm === "delete") {
        menu.innerHTML = `<p class="row-menu-ask">Delete “${esc(s.title)}”? It leaves the list; the file is moved to a <code>deleted</code> folder beside your saved conversations, so it can be put back.</p>` +
          `<div class="row-menu-acts"><button type="button" class="danger" data-rm="yes">Delete</button><button type="button" data-rm="no">Cancel</button></div>`;
        menu.querySelector('[data-rm="no"]').focus();
        return;
      }
      if (b.dataset.rm === "no") { closeRowMenu(); return; }
      if (b.dataset.rm === "yes") {
        const res = await post("/api/session", {action: "delete", id}).catch(() => null);
        closeRowMenu();
        if (!res || !res.ok) addNotice({level: "error", text: `Could not delete it: ${(res && res.error) || "no answer"}.`});
        loadSessions();
      }
    });
  }
  function renameRow(id) {
    const button = $("sessions").querySelector(`.session[data-id="${CSS.escape(id)}"]`);
    const s = sessionList.find((x) => x.id === id);
    if (!button || !s) return;
    const title = button.querySelector(".session-title");
    const field = el("input", "rename-field");
    field.value = s.title;
    field.setAttribute("aria-label", "New title");
    title.replaceWith(field);
    field.focus(); field.select();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const value = field.value.trim();
      if (save && value && value !== s.title) {
        const res = await post("/api/session", {action: "rename", id, title: value}).catch(() => null);
        if (!res || !res.ok) addNotice({level: "error", text: `Could not rename it: ${(res && res.error) || "no answer"}.`});
      }
      loadSessions();
    };
    field.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    field.addEventListener("click", (e) => e.stopPropagation());
    field.addEventListener("blur", () => finish(true));
  }
  document.addEventListener("click", (e) => { if (!e.target.closest(".row-menu, .session-more")) closeRowMenu(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeRowMenu(); });

  $("sessions").addEventListener("click", (e) => {
    const more = e.target.closest("[data-more]");
    if (more) { e.stopPropagation(); if (document.querySelector(".row-menu")) closeRowMenu(); else rowMenu(more); return; }
    if (e.target.closest(".rename-field")) return;
    if (e.target.closest(".tree-folder")) {                       // fold or unfold the folder's conversations
      treeOpen = !treeOpen;
      try { localStorage.setItem("agentx-tree", treeOpen ? "open" : "closed"); } catch (_) { /* no storage */ }
      drawSessions();
      return;
    }
    const l = e.target.closest("[data-live]");
    if (l) { joinLive(l.dataset.live === "join"); return; }
    const b = e.target.closest("[data-id]");
    if (b && beside.session === b.dataset.id) { $("besideFrame").focus(); return; }   // it is open beside: go there
    if (b && b.getAttribute("aria-current") !== "true") {
      const open = sessionList.find((s) => s.id === b.dataset.id && typeof s.lane === "string");
      if (open && open.lane !== LANE) showLane(open.lane);          // it runs already: show it
      else if (live.joined) joinLive(false, `resume:${b.dataset.id}`);
      else if (busyNow()) openLane(b.dataset.id);                   // this one works: open that one beside it
      else post("/api/send", {text: `/resume ${b.dataset.id}`});
    }
    $("side").classList.remove("open");
  });
  // Open a conversation (or, with "", a new one) in a lane of its own and show it; the one shown now goes on.
  async function openLane(session) {
    const res = await post("/api/lanes", {session});
    if (res && res.ok && typeof res.lane === "string") { showLane(res.lane); return; }
    addNotice({level: "error", text: `Could not open it beside this one: ${(res && res.error) || "agentx did not answer"}.`});
  }

  // -- the split view: another conversation beside this one, run by an agentx of its own -------------------------
  // Drag a conversation from Recent onto the chat (or use its button), or open another folder beside. Each side
  // has its own history, settings, approvals and orb; closing a side ends its agentx (its conversation is saved).
  const beside = {session: "", busy: false};
  let besideOrb = null;
  function showBeside(res, restoring = false) {
    const url = new URL(res.url);
    if (url.protocol !== location.protocol || url.hostname !== location.hostname || url.origin === location.origin) return;
    beside.session = res.session || "";
    $("besideTitle").textContent = res.title || "Conversation";
    $("besideFolder").textContent = res.folderName || "";
    $("besideFolder").title = res.folder || "";
    const saved = {url: url.origin + "/#embed", session: beside.session,
                   title: res.title, folderName: res.folderName, folder: res.folder};
    // Each child already keeps its key in its own tab storage. Never save launch codes or keys here.
    try { sessionStorage.setItem("agentx-beside", JSON.stringify(saved)); } catch (_) { /* no storage */ }
    $("beside").hidden = false;
    $("splitHandle").hidden = false;
    document.querySelector(".app").classList.add("split");
    applySplit();
    const frame = $("besideFrame");
    frame.onload = () => { $("besideWait").hidden = true; frame.hidden = false; };
    frame.src = restoring ? saved.url : `${res.url}&embed=1`;
  }
  function restoreBeside() {
    if (EMBED) return;
    try {
      const saved = JSON.parse(sessionStorage.getItem("agentx-beside") || "null");
      if (saved && saved.url) showBeside(saved, true);
    } catch (_) { /* stale or unavailable storage */ }
  }
  async function openBeside(what) {
    if (EMBED || beside.busy) return;
    beside.busy = true;
    const frame = $("besideFrame");
    frame.hidden = true;
    frame.src = "about:blank";                       // a side already open here: its agentx quits by itself
    beside.session = "";
    $("besideTitle").textContent = what.title || "Opening…";
    $("besideFolder").textContent = "";
    $("besideWaitText").textContent = what.folder ? "Starting agentx in that folder…" : "Opening the conversation…";
    $("besideWait").hidden = false;
    $("beside").hidden = false;
    $("splitHandle").hidden = false;
    document.querySelector(".app").classList.add("split");
    applySplit();
    if (!besideOrb && window.AgentxOrb) { $("besideOrb").dataset.on = "1"; besideOrb = window.AgentxOrb.create($("besideOrb"), () => "thinking"); }
    const res = await post("/api/pane", what.folder ? {folder: what.folder} : {session: what.session}).catch(() => ({}));
    beside.busy = false;
    if (!res.ok || !res.url) {
      closeBeside();
      addNotice({level: "error", text: res.message || "That conversation could not be opened beside this one."});
      return;
    }
    showBeside(res);
    drawSessions();
  }
  function closeBeside() {
    try { sessionStorage.removeItem("agentx-beside"); } catch (_) { /* no storage */ }
    const frame = $("besideFrame");
    frame.onload = null;
    frame.src = "about:blank";                       // its window is gone: that agentx saves and quits
    frame.hidden = true;
    $("beside").hidden = true;
    $("splitHandle").hidden = true;
    document.querySelector(".app").classList.remove("split");
    placeDetails();
    beside.session = "";
    drawSessions();
    input.focus();
  }
  $("besideClose").addEventListener("click", closeBeside);
  // the line between the two sides: drag it (or use the arrow keys) to give either side more room; double-click
  // for half and half. The left side's share is remembered. Each side keeps at least MIN_SIDE pixels.
  const MIN_SIDE = 320;
  let leftShare = 0.5;
  try { const kept = Number(localStorage.getItem("agentx-split")); if (kept > 0.1 && kept < 0.9) leftShare = kept; } catch (_) { /* no storage */ }
  function applySplit() {
    const app = document.querySelector(".app");
    app.style.setProperty("--split-left", `${leftShare.toFixed(4)}fr`);
    app.style.setProperty("--split-right", `${(1 - leftShare).toFixed(4)}fr`);
    $("splitHandle").setAttribute("aria-valuenow", String(Math.round(leftShare * 100)));
    placeDetails();
  }
  function setShare(share, keep) {
    const main = document.querySelector(".main").getBoundingClientRect(), side = $("beside").getBoundingClientRect();
    const room = main.width + side.width;
    const low = room > 2 * MIN_SIDE ? MIN_SIDE / room : 0.5;
    leftShare = Math.min(1 - low, Math.max(low, share));
    applySplit();
    if (keep) { try { localStorage.setItem("agentx-split", String(leftShare)); } catch (_) { /* no storage */ } }
  }
  $("splitHandle").addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const handle = $("splitHandle"), app = document.querySelector(".app");
    try { handle.setPointerCapture(e.pointerId); } catch (_) { /* a pointer the page cannot hold: moves still come */ }
    app.classList.add("resizing");                 // the side's frame must not take the pointer while dragging
    const grab = e.clientX - handle.getBoundingClientRect().left;   // where on the line it was taken: it stays there
    const move = (ev) => {
      const left = document.querySelector(".main").getBoundingClientRect().left;
      const right = $("beside").getBoundingClientRect().right;
      // this side's Details, as a column, sits between this side and the line: it is not part of either share
      const details = (detailsOpen || tasksOpen) && !detailsOver() ? $(tasksOpen ? "tasksPanel" : "details").getBoundingClientRect().width : 0;
      const room = right - left - details - handle.getBoundingClientRect().width;
      if (room > 0) setShare((ev.clientX - grab - left - details) / room, false);
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      app.classList.remove("resizing");
      setShare(leftShare, true);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  });
  $("splitHandle").addEventListener("dblclick", () => setShare(0.5, true));
  $("splitHandle").addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); setShare(leftShare + (e.key === "ArrowLeft" ? -0.05 : 0.05), true); }
    else if (e.key === "Home" || e.key === "End") { e.preventDefault(); setShare(e.key === "Home" ? 0 : 1, true); }
  });
  // dragging a conversation from Recent: a drop target over the chat (the side's frame would swallow the drop)
  let dragging = "";
  $("sessions").addEventListener("dragstart", (e) => {
    const b = e.target.closest("[data-id]");
    if (!b || EMBED || b.getAttribute("draggable") !== "true") { e.preventDefault(); return; }
    dragging = b.dataset.id;
    e.dataTransfer.effectAllowed = "copy";
    e.dataTransfer.setData("text/plain", b.textContent.trim());
    requestAnimationFrame(() => { $("dropBeside").hidden = false; });
  });
  $("sessions").addEventListener("dragend", () => { dragging = ""; $("dropBeside").hidden = true; $("dropBeside").classList.remove("over"); });
  $("dropBeside").addEventListener("dragover", (e) => { if (dragging) { e.preventDefault(); e.dataTransfer.dropEffect = "copy"; $("dropBeside").classList.add("over"); } });
  $("dropBeside").addEventListener("dragleave", () => $("dropBeside").classList.remove("over"));
  $("dropBeside").addEventListener("drop", (e) => {
    e.preventDefault();
    const id = dragging;
    dragging = "";
    $("dropBeside").hidden = true; $("dropBeside").classList.remove("over");
    const row = sessionList.find((x) => x.id === id);
    if (id) openBeside({session: id, title: row ? row.title : ""});
  });
  async function openFolderBeside() {
    const path = await askText({title: "Open a folder beside this conversation", text: "agentx starts there, on the right half of this window; this conversation stays on the left.",
                                placeholder: "for example C:\\Users\\you\\projects\\shop", ok: "Open beside"});
    if (path) openBeside({folder: path, title: "New conversation"});
  }

  // -- events -----------------------------------------------------------------------------------------------------
  let loading = 0, queue = [], skipUpTo = 0;
  function handle(ev) {
    if (ev.seq <= skipUpTo) return;
    const stick = nearBottom();
    const live = column.querySelector(".thinking-now");          // drawn again after the event, at the very end
    if (live) live.remove();
    switch (ev.type) {
      case "welcome": welcome = ev; showWelcome(); break;
      case "user": addUser(ev.text); break;
      case "thought": addThought(ev.text); break;
      case "signin": signinCard(ev); break;
      case "tool_call": dropWelcome(); toolCall(ev); break;
      case "tool_result": toolResult(ev); break;
      case "final":                                 // the conversation's short title is written just after
        group = null; lastPrint = null; addAnswer(ev.text); loadSessions();
        setTimeout(loadSessions, 4000); setTimeout(loadSessions, 12000); break;
      case "print": dropWelcome(); addPrint(ev); break;
      case "notice": addNotice(ev); break;
      case "echo": addEcho(ev); break;
      case "team_plan": addPlan(ev); break;
      case "team_block": teamBlock(ev, false); break;
      case "ask": onAsk(ev); break;
      case "answered": onAnswered(ev); break;
      case "status": onStatus(ev); break;
      case "insert": input.value = (input.value ? input.value.replace(/\s*$/, " ") : "") + ev.text; grow(); setComposer(); input.focus(); break;
      case "conversation": switchConversation(ev); break;
      case "reset": reloadConversation(); break;
      case "closed": showClosed(ev); break;
    }
    keepBottom(stick || ev.type === "user" || ev.type === "ask");
    syncView();
  }
  // the empty screen puts the message box in the middle; scrolled up, a button jumps back to the latest
  function syncView() {
    updateLive();
    document.querySelector(".main").classList.toggle("empty", !!column.querySelector(".welcome"));
    document.querySelector(".main").classList.toggle("choosing", !!column.querySelector(".start-screen"));
    $("jump").hidden = nearBottom();
  }
  log.addEventListener("scroll", () => { $("jump").hidden = nearBottom(); }, {passive: true});
  $("jump").addEventListener("click", () => { log.scrollTop = log.scrollHeight; $("jump").hidden = true; });
  // The wheel scrolls the conversation wherever the pointer is: over the orb, the title, or a part of the bottom
  // that cannot scroll itself (a list or the message box scroll first while they can).
  document.querySelector(".main").addEventListener("wheel", (e) => {
    if (e.ctrlKey || !e.deltaY || log.contains(e.target)) return;
    for (let n = e.target; n && n !== e.currentTarget; n = n.parentElement) {
      if (n.scrollHeight > n.clientHeight + 1 && /(auto|scroll)/.test(getComputedStyle(n).overflowY)) {
        const room = e.deltaY > 0 ? n.scrollTop + n.clientHeight < n.scrollHeight - 1 : n.scrollTop > 0;
        if (room) return;                         // that one still scrolls this way: leave it to it
      }
    }
    log.scrollTop += e.deltaY * (e.deltaMode === 1 ? 32 : e.deltaMode === 2 ? log.clientHeight : 1);
    e.preventDefault();
  }, {passive: false});
  function onEvent(ev) { if (loading) queue.push(ev); else handle(ev); }

  // A conversation opened (start, /resume, /clear): its earlier messages come from /api/history; what happens in it
  // from now on comes as events. The first one keeps what is already shown (startup notices) below its history.
  let knownConversation = null;
  function resetView() { column.innerHTML = ""; cards.clear(); teamBlocks.clear(); turn = null; group = null; pending = []; lastPrint = null; ready = null; doneAt = 0; doneTook = ""; signingIn = ""; }
  // Events that waited while a conversation loaded, in order. Another conversation among them loads first: the
  // events after it wait for that load (handled now, they were drawn and then wiped by it, so a reloaded window lost
  // the message being worked on).
  function playLater(later) {
    for (let n = 0; n < later.length; n++) {
      if (later[n].type === "conversation") { queue = later.slice(n + 1).concat(queue); handle(later[n]); return; }
      handle(later[n]);
    }
  }
  async function switchConversation(ev) {
    const mine = ++loading;
    browserMoved();                                   // the Browser tab shows this conversation's page from now on
    let data = null;
    try {
      data = await get(`/api/history?upto=${Number(ev.base) || 0}`);
      if (mine !== loading) return;
      const first = knownConversation === null;
      knownConversation = ev.path;
      if (first) {
        const shown = [...column.childNodes];
        const focused = column.contains(document.activeElement) ? document.activeElement : null;
        shown.forEach((n) => n.remove());
        turn = null; group = null; lastPrint = null;
        for (const item of data.items) handle({...item, seq: Infinity});
        if (data.items.length) { dropWelcome(); if (startView) startView.show = false; }
        shown.forEach((n) => { if (!(data.items.length && n.classList && (n.classList.contains("welcome") || n.classList.contains("start-screen")))) column.append(n); });
        if (focused && focused.isConnected) focused.focus({preventScroll: true});   // moving it took the focus away
        turn = null; group = null;
      } else {
        resetView();
        for (const item of data.items) handle({...item, seq: Infinity});
        skipUpTo = ev.seq;
      }
      if (!column.children.length) showWelcome();
    } catch (_) { /* the queue is played below */ }
    if (mine === loading) {
      loading = 0;
      const later = queue.filter((q) => q.seq > skipUpTo);
      queue = [];
      playLater(later);
      // questions still open that were asked before this conversation's event (the rest came above, once each)
      for (const open of (data && data.pending) || []) handle({...open, seq: Infinity});
      if (data && data.status) handle({...data.status, type: "status", seq: Infinity});
      keepBottom(true);
      loadSessions(); loadState();
    }
  }

  async function reloadConversation() {
    const mine = ++loading;
    browserMoved();
    try {
      const data = await get("/api/history");
      if (mine !== loading) return;
      resetView();
      for (const item of data.items) handle({...item, seq: Infinity});
      skipUpTo = data.seq;
      for (const ev of data.pending || []) handle({...ev, seq: Infinity});
      if (data.status) handle({...data.status, type: "status", seq: Infinity});
      if (!column.children.length) showWelcome();
      loading = 0;
      const later = queue.filter((ev) => ev.seq > skipUpTo);
      queue = [];
      playLater(later);
      keepBottom(true);
      loadSessions(); loadState();
    } catch (_) {
      if (mine === loading) { loading = 0; queue.splice(0).forEach(handle); }
    }
  }

  let closed = false;
  function showClosed(ev) {
    closed = true;
    if (ev && ev.handedOff) {                  // the start screen opened agentx in another folder; this one ended
      showHandedOff(`agentx opened ${ev.handedOff} in a new window. You can close this one.`);
      return;
    }
    if (ev && ev.keyChanged) {                 // someone used the start link again: agentx made a new key
      try { sessionStorage.removeItem("agentx-token"); } catch (_) { /* no storage */ }
      askForKey("agentx changed its key", "The one-time link this window was opened with was used again, " +
        "perhaps by another program on this computer, so agentx made a new key. Paste the link the terminal shows now.");
      return;
    }
    if (ev && ev.remoteOff) {                  // remote control was turned off on the computer agentx runs on
      $("overlayBox").innerHTML = "<h2>Remote control is off</h2><p>It was turned off on the computer agentx runs " +
        "on. Your conversation is saved; when remote control is on again, reload this page to carry on.</p>";
      $("overlay").hidden = false;
      return;
    }
    if (ev && ev.room) {                       // this device's own conversation ended; agentx still runs
      $("overlayBox").innerHTML = (ev.idle
        ? "<h2>This conversation was put away</h2><p>No window used it for a while, so agentx closed it. It is saved: open it again to carry on.</p>"
        : "<h2>This conversation ended</h2><p>It is saved in the list. Start a new one, or open any conversation from the list.</p>") +
        `<div class="key-form"><button type="button" class="btn primary" id="reopen">${ev.idle ? "Open it again" : "Start a new conversation"}</button></div>`;
      $("overlay").hidden = false;
      $("reopen").addEventListener("click", () => location.reload());
      return;
    }
    $("overlayBox").innerHTML = "<h2>agentx has stopped</h2><p>Start it again from your terminal with <code>agentx --ui</code>. " +
      "Your conversation is saved; <code>agentx --ui --continue</code> picks it up.</p>";
    $("overlay").hidden = false;
  }

  // No key, or the key of an earlier start: say so, and take the link agentx printed (pasted here) instead.
  function askForKey(title, text) {
    $("overlayBox").innerHTML = `<h2>${esc(title)}</h2><p>${esc(text)}</p>
      <form class="key-form"><input type="text" name="link" autocomplete="off" autocapitalize="off" spellcheck="false"
        placeholder="Paste the link agentx printed" aria-label="The link agentx printed">
        <button type="submit" class="btn primary">Open</button></form><p class="key-msg" role="status"></p>`;
    $("overlay").hidden = false;
    const form = $("overlayBox").querySelector("form");
    form.link.focus();
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const key = keyFrom(form.link.value);
      if (!key) { $("overlayBox").querySelector(".key-msg").textContent = "That is not an agentx link: it has ?t= in it."; return; }
      token = key;
      keepKey();
      location.reload();
    });
  }

  let launchProblem = "";
  async function claimLaunch() {
    if (!launchCode || token) return;
    const res = await fetch("/api/launch", {method: "POST", headers: {"Content-Type": "application/json"},
                                            body: JSON.stringify({code: launchCode})}).catch(() => null);
    const data = res ? await res.json().catch(() => ({})) : {};
    if (data.token) { token = data.token; keepKey(); return; }
    launchProblem = data.error || "unknown";
    history.replaceState(null, "", location.pathname + (EMBED ? "#embed=1" : ""));   // the code is used up either way
  }

  async function connect() {
    if (!token && launchProblem) {
      askForKey(launchProblem === "expired" ? "This start link has expired" : "This start link was already used",
        "The link agentx opened this window with works once, for a short time. Open the link the terminal shows " +
        "(the one with ?t=), or paste it here.");
      return;
    }
    if (!token) {
      askForKey("This page has no key", "It was opened without the end of its link (the part with ?t=). Open the " +
        "whole link agentx printed in the terminal, or paste it here.");
      return;
    }
    const check = await fetch("/api/state", {headers: {"X-Agentx-Token": token, "X-Agentx-Device": DEVICE,
                                                       "X-Agentx-Lane": LANE}}).catch(() => null);
    if (check && check.status === 403) {
      const why = await check.json().catch(() => ({}));
      if (why.error === "forbidden") {
        try { sessionStorage.removeItem("agentx-token"); } catch (_) { /* no storage */ }
        askForKey("This link is from an earlier start", "Every start of agentx makes a new link. Open the one the " +
          "terminal shows now, or paste it here.");
        return;
      }
    }
    openStream(0);
  }

  // The event stream. When remote use is turned off agentx changes its key and sends the new one to windows on
  // this computer: the window keeps it and picks the stream up where it left off.
  let lastSeen = 0;
  function openStream(after) {
    const source = new EventSource(`/events?t=${encodeURIComponent(token)}&d=${DEVICE}${LANE ? `&l=${LANE}` : ""}` +
                                   `${after ? `&after=${after}` : ""}`);
    source.onmessage = (m) => {
      if (m.lastEventId) lastSeen = Math.max(lastSeen, Number(m.lastEventId) || 0);
      let ev;
      try { ev = JSON.parse(m.data); } catch (_) { return; }
      if (ev && ev.type === "key" && ev.token) {
        token = ev.token; keepKey(); source.close(); openStream(lastSeen); return;
      }
      try { onEvent(ev); } catch (error) { console.error("agentx: an event could not be shown", error); }
    };
    source.onerror = () => { if (closed) source.close(); };
  }

  showStarting();                          // the opening, until agentx says hello
  mountOrbs($("status"), orbState);         // the orb next to the status line
  mountOrbs($("stage"), orbState, {interactive: true});   // and large on the stage while agentx works
  setComposer();
  input.focus();
  // the start screen is known before the events come, so the welcome is never drawn first
  claimLaunch().then(async () => { restoreBeside(); loadState(); loadSessions(); await loadStart(); showStartIfAsked(); connect(); });
})();
