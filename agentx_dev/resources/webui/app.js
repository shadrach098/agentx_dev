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
  try { token = token || sessionStorage.getItem("agentx-token") || ""; } catch (_) { /* no storage */ }
  function keepKey() {
    try { sessionStorage.setItem("agentx-token", token); } catch (_) { /* no storage */ }
    if (token) history.replaceState(null, "", `/#t=${encodeURIComponent(token)}`);
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

  async function post(path, body) {
    const res = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Agentx-Token": token,
                                                             "X-Agentx-Device": DEVICE},
                                   body: JSON.stringify(body || {})});
    return res.json().catch(() => ({}));
  }
  async function get(path) {
    const res = await fetch(path, {headers: {"X-Agentx-Token": token, "X-Agentx-Device": DEVICE}});
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
  function inline(text) {
    let s = esc(text);
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>").replace(/(^|[\s(])_([^_\s][^_]*)_/g, "$1<em>$2</em>");
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
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
  const MARK = '<svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><path d="M6 8 16 16 6 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><rect x="21" y="8" width="3" height="16" rx=".5" fill="currentColor"/></svg>';
  const STARTERS = [
    ["Explain how this project fits together", "Explain how this project fits together"],
    ["Find a bug and fix it", "Find a bug in this project and fix it"],
    ["Write notes for agentx <code>/init</code>", "/init"],
  ];
  let folderName = "";
  function showWelcome() {
    if (!welcome || column.children.length) return;
    const w = el("section", "welcome");
    const tips = (welcome.tips || []).map((t) => `<li>${esc(t)}</li>`).join("");
    const news = (welcome.news || []).map((t) => `<li>${esc(t)}</li>`).join("");
    const where = folderName || welcome.folder || "";
    w.innerHTML = `${MARK}<h1>What should we work on${where ? ` in ${esc(where)}` : ""}?</h1>
      <p class="meta">${esc(welcome.model || "")}</p>
      <div class="starters">${STARTERS.map(([label], n) => `<button type="button" class="starter" data-starter="${n}">${label}</button>`).join("")}</div>
      ${tips || news ? `<details><summary>Tips and what's new</summary>${tips ? `<ul>${tips}</ul>` : ""}` +
        `${news ? `<h2>What's new</h2><ul>${news}</ul>` : ""}</details>` : ""}`;
    w.addEventListener("click", (e) => {
      const b = e.target.closest("[data-starter]");
      if (!b) return;
      input.value = STARTERS[Number(b.dataset.starter)][1];
      grow(); setComposer(); input.focus();
    });
    column.append(w);
    syncView();
  }

  function addUser(text) {
    dropWelcome();
    turn = null; group = null; lastPrint = null;
    const command = /^\/\w/.test(text);              // a command (often from a menu) is not a chat message
    const u = el("div", command ? "user command" : "user");
    u.textContent = text;
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
    let live = column.querySelector(".thinking-now");
    const show = !!status.working && !pending.length && !liveCard() && !column.querySelector(".welcome");
    if (!show) { if (live) live.remove(); return; }
    if (!live) live = el("div", "thinking-now", '<span class="spinner" aria-hidden="true"></span><span class="thinking-now-label">Thinking…</span>');
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
  function addNotice(ev) {
    lastPrint = null; turn = null;
    const n = el("div", `notice ${ev.level || "info"}`);
    const flush = (s) => String(s || "").replace(/^[ \t]+/gm, "").trim();      // the terminal's indent is not needed here
    if (ev.level === "callout") n.innerHTML = `<b>${esc(flush(ev.text))}</b>${esc(flush(ev.hint))}`;
    else n.innerHTML = `<span>${esc(flush(ev.text))}${ev.hint ? ` <span class="hint">${esc(flush(ev.hint))}</span>` : ""}</span>`;
    // "2 MCP servers need sign-in: flightclaw, supabase": a button for each, named
    const signin = String(ev.text || "").match(/MCP servers? needs? sign-in:\s*([\w, -]+)/i);
    if (signin) {
      const row = el("span", "notice-actions");
      signin[1].split(",").map((s) => s.trim()).filter((s) => /^[\w-]{1,40}$/.test(s)).forEach((name) => {
        const b = el("button", "btn small", `Sign in to ${esc(name)}`);
        b.type = "button";
        b.addEventListener("click", () => post("/api/send", {text: `/mcp login ${name}`}));
        row.append(b);
      });
      n.firstElementChild.append(row);
    }
    column.append(n);
  }
  function addEcho(ev) {
    lastPrint = null; turn = null;
    const e = el("div", `echo${ev.done ? "" : " failed"}`);
    e.innerHTML = `<span class="echo-text">${esc(ev.text)}</span><span class="echo-out">${ev.done ? "✓" : "✗"} ${esc(ev.outcome)}</span>`;
    column.append(e);
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

  let ready = null;          // the app's open "message" request: your next message answers it
  function onAsk(ev) {
    if (ready === ev.id || cards.has(ev.id)) return;          // already shown (a reload plays it twice)
    if (ev.kind === "message") {
      ready = ev.id;
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
    requestAnimationFrame(() => (card.querySelector(".btn.primary, input") || card).focus({preventScroll: true}));
    setComposer();
  }
  function onAnswered(ev) {
    if (ev.id === ready) ready = null;
    const card = cards.get(ev.id);
    if (card && !card.classList.contains("done")) finishCard(card, ev.outcome ? `${ev.outcome} in the terminal` : "");
    setComposer();
  }
  function liveCard() {
    for (const card of [...cards.values()].reverse()) if (!card.classList.contains("done")) return card;
    return null;
  }

  // -- the status, team and waiting messages under the conversation -----------------------------------------------
  let status = {}, startedAt = 0, sentAt = 0;   // sentAt: a message went off and agentx has not picked it up yet
  function drawStatus() {
    const open = liveCard();
    const starting = sentAt && !status.working && !open && !ready && Date.now() - sentAt < 60000;
    if (!starting) sentAt = 0;
    $("status").hidden = !status.working && !open && !starting;
    $("statusLabel").textContent = status.working ? friendlyLabel(status.label) + "…"
      : open ? "Waiting for your answer" : starting ? "Starting…" : "";
    $("composer").classList.toggle("busy", !!(status.working || starting));
    tickTime();
  }
  function onStatus(ev) {
    status = ev;
    if (ev.working) sentAt = 0;
    startedAt = ev.working ? ev.startedAt || 0 : 0;
    drawStatus();
    const team = $("team");
    if (ev.team && ev.team.rows && ev.team.rows.length) {
      team.hidden = false;
      team.innerHTML = `<div class="team-head">Team · ${ev.team.rows.length} running · ${ev.team.done} of ${ev.team.total} done</div>` +
        ev.team.rows.map((r) => `<div class="team-row"><span class="who">${esc(r.helper)}${r.label && r.label !== r.helper ? ` · ${esc(r.label)}` : ""}</span>` +
          `<span class="stats">${esc(r.stats)}</span>${r.activity ? `<span class="act">${esc(r.activity)}</span>` : ""}</div>`).join("");
    } else { team.hidden = true; team.innerHTML = ""; }
    const others = Number(ev.othersWorking || 0);         // other devices' conversations working in this folder
    $("others").hidden = !others;
    $("others").textContent = others === 1
      ? "Another conversation is working in this folder: it may change the same files."
      : `${others} other conversations are working in this folder: they may change the same files.`;
    const waiting = $("waiting");
    const items = ev.waiting || [];
    waiting.hidden = !items.length;
    waiting.innerHTML = items.map((w, n) => `<li><span class="w-text">${items.length > 1 ? `${n + 1}. ` : ""}${esc(w.text)}</span>` +
      `<span class="w-where">${esc(w.where)}</span><button type="button" data-unsend="${n + 1}" aria-label="Take back this message">✕</button></li>`).join("");
    $("micBtn").classList.toggle("recording", !!ev.recording);
    $("micBtn").setAttribute("aria-label", ev.recording ? "Stop recording" : "Speak instead of typing");
    if (ev.mode) setMode(ev.mode);
    if (ev.model) setModel(ev.model);
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
  }
  setInterval(tickTime, 1000);
  $("waiting").addEventListener("click", (e) => {
    const b = e.target.closest("[data-unsend]");
    if (b) post("/api/send", {text: `/unsend ${b.dataset.unsend}`});
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
    if ($("sendBtn").classList.contains("stopping")) { post("/api/stop"); return; }
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
    if (e.key === "Escape" && !typing && (liveCard() || status.working)) { post("/api/stop"); return; }
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
  function setMode(mode) {
    currentMode = mode;
    const m = MODES.find((x) => x[0] === mode);
    $("modeLabel").textContent = m ? m[1] : mode;
  }
  function setModel(model) { currentModel = model || ""; $("modelLabel").textContent = currentModel || "Model"; }

  const menu = $("menu");
  function closeMenu(refocus) {
    if (!menuFor) return;
    menu.hidden = true;
    menuFor.setAttribute("aria-expanded", "false");
    if (refocus) menuFor.focus();
    menuFor = null; menuPick = null;
  }
  function openMenu(button, items, pick) {
    if (menuFor === button) { closeMenu(true); return; }
    closeMenu(); hidePalette();
    menuFor = button; menuPick = pick;
    menu.innerHTML = items.map((it, n) => `<div class="menu-item" role="option" tabindex="-1" data-n="${n}" aria-selected="${!!it.current}">` +
      `<span class="menu-text"><span class="menu-label">${esc(it.label)}</span>${it.hint ? `<span class="menu-hint">${esc(it.hint)}</span>` : ""}</span>` +
      `${it.current ? CHECK : ""}</div>`).join("");
    menu.items = items;
    menu.hidden = false;
    const b = button.getBoundingClientRect();           // above its button, kept inside the window
    menu.style.left = Math.max(8, Math.min(b.left, window.innerWidth - menu.offsetWidth - 8)) + "px";
    menu.style.bottom = Math.max(8, window.innerHeight - b.top + 8) + "px";
    button.setAttribute("aria-expanded", "true");
    const start = Math.max(0, items.findIndex((it) => it.current));
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
  $("modelBtn").addEventListener("click", () => {
    const names = models.includes(currentModel) || !currentModel ? models : [currentModel, ...models];
    openMenu($("modelBtn"), [...names.map((m) => ({value: m, label: m, current: m === currentModel})),
                             {value: "", label: "Other model…", hint: "Type any model name"}],
      async (it) => {
        let name = it.value;
        if (!name) {
          name = await askText({title: "Use another model", text: "Any model your keys can reach, by its exact name.",
                                placeholder: "for example claude-sonnet-5-5 or gpt-6.1-sol", ok: "Switch"});
          if (!name) return;
        }
        if (name !== currentModel) { setModel(name); post("/api/send", {text: `/model ${name}`}); }
      });
  });
  $("newChat").addEventListener("click", () => { if (live.joined) joinLive(false, "new"); else post("/api/send", {text: "/clear"}); });
  // -- the side panel: context, tokens, team work, MCP servers, memory ----------------------------------------------
  const num = (n) => Number(n || 0).toLocaleString();
  let detailsOpen = false, detailsTimer = 0;
  function setDetails(open) {
    detailsOpen = open;
    $("details").hidden = !open;
    $("detailsScrim").hidden = !open;
    document.querySelector(".app").classList.toggle("with-details", open);
    $("detailsBtn").setAttribute("aria-expanded", String(open));
    try { localStorage.setItem("agentx-details", open ? "open" : "closed"); } catch (_) { /* no storage */ }
    if (open) loadDetails();
  }
  $("detailsBtn").addEventListener("click", () => setDetails(!detailsOpen));
  $("detailsClose").addEventListener("click", () => setDetails(false));
  // below a wide window the panel lies over the chat: tapping beside it, or Escape, closes it
  const detailsOver = () => matchMedia("(max-width: 1199px)").matches;
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
    $("detailsBtn").title = ctx ? `Context ${pct}% full` : "Details";
    if (!detailsOpen) return;
    const out = [];
    if (ctx) {
      out.push(section("Context", `<div class="meter"><span style="width:${pct}%"></span></div>
        <p class="d-note">${pct}% full · ${num(ctx.messages)} messages. At 100%, older parts are summarised so the
        conversation can go on.</p>`));
    }
    const u = d.usage;
    if (u) {
      const last = u.lastTurn && u.lastTurn.length ? u.lastTurn : null;
      out.push(section("Tokens this session", `<dl class="d-grid">
        <dt>Sent</dt><dd>${num(u.input)}</dd><dt>Received</dt><dd>${num(u.output)}</dd>
        <dt>From cache</dt><dd>${num(u.cached)}</dd><dt>Requests</dt><dd>${num(u.requests)}</dd>
        ${last ? `<dt>Last message</dt><dd>${num(last[1] + last[2])}</dd>` : ""}
        <dt>Limit per message</dt><dd>${u.tokenLimit ? num(u.tokenLimit) : "none"}</dd></dl>`,
        `<button type="button" class="link" data-cmd="/usage">Full report</button>`));
    }
    const t = d.team;
    out.push(section("Team work", t && t.steps.length ? t.steps.map((s) => `<details class="d-step"${s.done ? "" : " open"}>
        <summary><span class="dot ${s.done ? "ok" : "run"}"></span><span class="d-who">${esc(s.helper)}${s.label && s.label !== s.helper ? ` · ${esc(s.label)}` : ""}</span>
        <span class="d-num">${s.seconds}s · ${num(s.tokens)} tok</span></summary>
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

  // -- the Settings sheet: saved to the settings files, for every folder or for this one ----------------------------
  let settingsData = null, scope = "user";
  async function openSettings() {
    try { settingsData = await get("/api/settings"); } catch (_) { return; }
    $("settingsWrap").hidden = false;
    $("settingsMsg").textContent = "";
    $("scopeFolder").disabled = !settingsData.canFolder;
    drawSettings();
    setTimeout(() => $("settingsForm").querySelector("input, textarea, button")?.focus(), 0);
  }
  function closeSettings() { $("settingsWrap").hidden = true; $("settingsBtn").focus(); }
  function drawSettings() {
    const file = settingsData[scope], v = file.values || {}, eff = settingsData.effective;
    document.querySelectorAll(".scope [data-scope]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.scope === scope)));
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
      ${settingsData.problems.length ? `<div class="s-problems"><b>Problems in the settings files</b>${settingsData.problems.map((p) => `<p>${esc(p)}</p>`).join("")}</div>` : ""}
      <p class="s-hint">Rules and limits take effect at once. The model and mode are used from the next start.</p>`;
  }
  async function saveSettings() {
    const f = new FormData($("settingsForm"));
    const split = (s) => String(s || "").split("\n").map((x) => x.trim()).filter(Boolean);
    const values = {model: f.get("model") || "", mode: f.get("mode") || "", allow: split(f.get("allow")), deny: split(f.get("deny")),
                    maxSteps: f.get("maxSteps") || "", tokenLimit: f.get("tokenLimit") || ""};
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
  $("settingsForm").addEventListener("submit", (e) => { e.preventDefault(); saveSettings(); });
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
    {value: "copy", label: "Copy this folder's path", hint: $("folder").title},
  ], async (it) => {
    if (it.value === "open") openAnotherFolder();
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
      folderName = s.folderName || "";
      $("folderName").textContent = folderName;
      $("folder").title = s.folder || "";
      const h = column.querySelector(".welcome h1");
      if (h && folderName) h.textContent = `What should we work on in ${folderName}?`;
      document.title = `agentx · ${s.folderName || ""}`;
      commands = s.commands || {};
      skills = s.skills || {};
      models = (s.models || []).map(String);
      if (s.model) setModel(s.model);
      if (s.mode) setMode(s.mode);
      live = s.live || {};
      drawSessions();
    } catch (_) { /* the app is still starting: the next status brings it */ }
  }
  // Each device has its own conversation. The one running on the computer (or in the first window) is listed on
  // top as "Live": opening it shares it with whoever has it; "Your own conversation" goes back.
  let sessionList = [], live = {};
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
    const rows = sessionList.map((s) => `<button type="button" class="session" data-id="${esc(s.id)}" aria-current="${s.current}" ` +
      `title="${esc(String(s.updated).replace("T", " ").slice(0, 16))}"><span class="session-title">${esc(s.title)}</span></button>`).join("");
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
  async function joinLive(on, then) {
    await post("/api/join", {live: on, then: then || ""});
    location.reload();
  }
  $("sessions").addEventListener("click", (e) => {
    const l = e.target.closest("[data-live]");
    if (l) { joinLive(l.dataset.live === "join"); return; }
    const b = e.target.closest("[data-id]");
    if (b && b.getAttribute("aria-current") !== "true") {
      if (live.joined) joinLive(false, `resume:${b.dataset.id}`);
      else post("/api/send", {text: `/resume ${b.dataset.id}`});
    }
    $("side").classList.remove("open");
  });

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
  // Once a conversation has started, its top carries the agentx header, like the box the terminal opens with.
  function syncBanner() {
    const has = column.querySelector(":scope > .banner");
    const empty = !column.children.length || (has && column.children.length === 1);
    if (!welcome || column.querySelector(".welcome") || empty) { if (has) has.remove(); return; }
    if (has && column.firstElementChild === has) return;
    const b = has || el("header", "banner");
    const where = folderName || welcome.folder || "";
    b.innerHTML = `${MARK}<div class="banner-text"><div class="banner-name">agentx` +
      `${welcome.version ? ` <span class="banner-version">${esc(welcome.version)}</span>` : ""}</div>` +
      `<div class="banner-meta">${[welcome.model, where].filter(Boolean).map(esc).join(" · ")}</div></div>`;
    column.prepend(b);
  }
  function syncView() {
    syncBanner();
    updateLive();
    document.querySelector(".main").classList.toggle("empty", !!column.querySelector(".welcome"));
    $("jump").hidden = nearBottom();
  }
  log.addEventListener("scroll", () => { $("jump").hidden = nearBottom(); }, {passive: true});
  $("jump").addEventListener("click", () => { log.scrollTop = log.scrollHeight; $("jump").hidden = true; });
  function onEvent(ev) { if (loading) queue.push(ev); else handle(ev); }

  // A conversation opened (start, /resume, /clear): its earlier messages come from /api/history; what happens in it
  // from now on comes as events. The first one keeps what is already shown (startup notices) below its history.
  let knownConversation = null;
  function resetView() { column.innerHTML = ""; cards.clear(); turn = null; group = null; pending = []; lastPrint = null; ready = null; }
  async function switchConversation(ev) {
    const mine = ++loading;
    let data = null;
    try {
      data = await get(`/api/history?upto=${Number(ev.base) || 0}`);
      if (mine !== loading) return;
      const first = knownConversation === null;
      knownConversation = ev.path;
      if (first) {
        const shown = [...column.childNodes];
        shown.forEach((n) => n.remove());
        turn = null; group = null; lastPrint = null;
        for (const item of data.items) handle({...item, seq: Infinity});
        if (data.items.length) dropWelcome();
        shown.forEach((n) => { if (!(data.items.length && n.classList && n.classList.contains("welcome"))) column.append(n); });
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
      later.forEach(handle);
      // questions still open that were asked before this conversation's event (the rest came above, once each)
      for (const open of (data && data.pending) || []) handle({...open, seq: Infinity});
      if (data && data.status) handle({...data.status, type: "status", seq: Infinity});
      keepBottom(true);
      loadSessions(); loadState();
    }
  }

  async function reloadConversation() {
    const mine = ++loading;
    try {
      const data = await get("/api/history");
      if (mine !== loading) return;
      column.innerHTML = ""; cards.clear(); turn = null; group = null; pending = []; lastPrint = null; ready = null;
      for (const item of data.items) handle({...item, seq: Infinity});
      skipUpTo = data.seq;
      for (const ev of data.pending || []) handle({...ev, seq: Infinity});
      if (data.status) handle({...data.status, type: "status", seq: Infinity});
      if (!column.children.length) showWelcome();
      loading = 0;
      const later = queue.filter((ev) => ev.seq > skipUpTo);
      queue = [];
      later.forEach(handle);
      keepBottom(true);
      loadSessions(); loadState();
    } catch (_) {
      if (mine === loading) { loading = 0; queue.splice(0).forEach(handle); }
    }
  }

  let closed = false;
  function showClosed(ev) {
    closed = true;
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

  async function connect() {
    if (!token) {
      askForKey("This page has no key", "It was opened without the end of its link (the part with ?t=). Open the " +
        "whole link agentx printed in the terminal, or paste it here.");
      return;
    }
    const check = await fetch("/api/state", {headers: {"X-Agentx-Token": token, "X-Agentx-Device": DEVICE}}).catch(() => null);
    if (check && check.status === 403) {
      const why = await check.json().catch(() => ({}));
      if (why.error === "forbidden") {
        try { sessionStorage.removeItem("agentx-token"); } catch (_) { /* no storage */ }
        askForKey("This link is from an earlier start", "Every start of agentx makes a new link. Open the one the " +
          "terminal shows now, or paste it here.");
        return;
      }
    }
    const source = new EventSource(`/events?t=${encodeURIComponent(token)}&d=${DEVICE}`);
    source.onmessage = (m) => {
      try { onEvent(JSON.parse(m.data)); } catch (error) { console.error("agentx: an event could not be shown", error); }
    };
    source.onerror = () => { if (closed) source.close(); };
  }

  loadState();
  loadSessions();
  setComposer();
  connect();
  input.focus();
})();
