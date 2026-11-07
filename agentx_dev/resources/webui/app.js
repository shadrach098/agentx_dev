/* agentx app window. Events come from the agentx process over /events (server-sent events) and are drawn here;
   what you do (send, answer, stop, speak) goes back as small JSON posts. Nothing runs in the page itself. */
"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const column = $("column"), log = $("log"), input = $("input"), palette = $("palette");

  // -- the token of this run: from the address once, then kept for reloads -----------------------------------
  const params = new URLSearchParams(location.search);
  let token = params.get("t") || sessionStorage.getItem("agentx-token") || "";
  if (params.get("t")) {
    sessionStorage.setItem("agentx-token", token);
    history.replaceState(null, "", "/");
  }

  async function post(path, body) {
    const res = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Agentx-Token": token},
                                   body: JSON.stringify(body || {})});
    return res.json().catch(() => ({}));
  }
  async function get(path) {
    const res = await fetch(path, {headers: {"X-Agentx-Token": token}});
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
  function dropWelcome() { const w = column.querySelector(".welcome"); if (w) w.remove(); }
  function showWelcome() {
    if (!welcome || column.children.length) return;
    const w = el("section", "welcome");
    const tips = (welcome.tips || []).map((t) => `<li>${esc(t)}</li>`).join("");
    const news = (welcome.news || []).map((t) => `<li>${esc(t)}</li>`).join("");
    w.innerHTML = `<h1>${esc(welcome.greeting || "agentx")}</h1>
      <p class="meta">${esc(welcome.model || "")}${welcome.folder ? " in " + esc(welcome.folder) : ""}</p>
      ${tips ? `<ul>${tips}</ul>` : ""}${news ? `<div class="news"><h2>What's new</h2><ul>${news}</ul></div>` : ""}`;
    column.append(w);
  }

  function addUser(text) {
    dropWelcome();
    turn = null; group = null; lastPrint = null;
    const u = el("div", "user");
    u.textContent = text;
    column.append(u);
    if (!$("topTitle").dataset.fixed) $("topTitle").textContent = text.split("\n")[0].slice(0, 90) || "Conversation";
  }

  function makeStep(ev) {
    const s = el("div", "step running");
    s.dataset.name = ev.name;
    s.innerHTML = `<div class="step-head"><span class="step-name">${esc(ev.name)}</span><span class="step-subject">${esc(ev.subject || "")}</span></div>`;
    return s;
  }
  function groupSummary(g) {
    const steps = g.querySelectorAll(".inner .step");
    const counts = {};
    steps.forEach((s) => { const n = s.dataset.name; counts[n] = (counts[n] || 0) + 1; });
    const words = {read_path: "read", grep: "search", find_files: "search", list_directory: "listing", web_search: "web search",
                   web_fetch: "page fetched", recall: "notes searched", use_skill: "skill"};
    const parts = Object.entries(counts).map(([n, c]) => `${c} ${words[n] || n}${c > 1 && !/ed$/.test(words[n] || "") ? "s" : ""}`);
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
        group.innerHTML = `<summary class="step-head"><span class="step-name">Explored</span><span class="step-subject"></span></summary><div class="inner"></div>`;
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
    const g = step.closest(".group");
    if (g) groupSummary(g);
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
    if (ev.level === "callout") n.innerHTML = `<b>${esc(ev.text)}</b>${esc(ev.hint || "")}`;
    else n.innerHTML = esc(ev.text) + (ev.hint ? ` <span class="hint">· ${esc(ev.hint)}</span>` : "");
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
    if (kind !== "diff") return esc(body);
    return String(body).split("\n").map((line) => {
      const cls = line.startsWith("+++") || line.startsWith("---") ? "diff-meta" : line.startsWith("@@") ? "diff-hunk"
        : line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-del" : "";
      return cls ? `<span class="${cls}">${esc(line) || " "}</span>` : esc(line);
    }).join("\n").replace(/<\/span>\n/g, "</span>");
  }
  function finishCard(card, outcome) {
    card.classList.remove("live");
    card.classList.add("done");
    if (outcome) card.append(el("div", "card-outcome", esc(outcome)));
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
    card.innerHTML = `<div class="card-head"><div class="card-tool">${esc(ev.tool)}${ev.helper ? ` · asked by ${esc(ev.helper)}` : ""}</div>
      <div class="card-title">${esc(ev.title)}</div>${sameSubject ? "" : `<div class="card-sub">${esc(ev.subject)}</div>`}</div>
      ${ev.body ? `<div class="card-body"><pre>${diffHtml(ev.body, ev.body_kind)}</pre></div>` : ""}
      <div class="card-actions">
        <button type="button" class="btn primary" data-a="y">Yes <kbd>1</kbd></button>
        ${ev.always ? `<button type="button" class="btn" data-a="a">Yes, and don't ask again: ${esc(ev.grant)} <kbd>2</kbd></button>` : ""}
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

  let ready = null;          // the app's open "message" request: your next message answers it
  function onAsk(ev) {
    if (ready === ev.id || cards.has(ev.id)) return;          // already shown (a reload plays it twice)
    if (ev.kind === "message") {
      ready = ev.id;
      if (ev.default) { input.value = (input.value ? input.value + " " : "") + ev.default; grow(); }
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
  }
  function onAnswered(ev) {
    if (ev.id === ready) { ready = null; setComposer(); }
    const card = cards.get(ev.id);
    if (card && !card.classList.contains("done")) finishCard(card, "");
  }
  function liveCard() {
    for (const card of [...cards.values()].reverse()) if (!card.classList.contains("done")) return card;
    return null;
  }

  // -- the status, team and waiting messages under the conversation -----------------------------------------------
  let status = {}, startedAt = 0;
  function onStatus(ev) {
    status = ev;
    const box = $("status");
    const open = liveCard();
    box.hidden = !ev.working && !open;
    $("statusLabel").textContent = ev.working ? (ev.label || "Working") + "..." : open ? "Waiting for your answer" : "";
    startedAt = ev.working ? ev.startedAt || 0 : 0;
    tickTime();
    const team = $("team");
    if (ev.team && ev.team.rows && ev.team.rows.length) {
      team.hidden = false;
      team.innerHTML = `<div class="team-head">Team · ${ev.team.rows.length} running · ${ev.team.done} of ${ev.team.total} done</div>` +
        ev.team.rows.map((r) => `<div class="team-row"><span class="who">${esc(r.helper)}${r.label && r.label !== r.helper ? ` · ${esc(r.label)}` : ""}</span>` +
          `<span class="stats">${esc(r.stats)}</span>${r.activity ? `<span class="act">${esc(r.activity)}</span>` : ""}</div>`).join("");
    } else { team.hidden = true; team.innerHTML = ""; }
    const waiting = $("waiting");
    const items = ev.waiting || [];
    waiting.hidden = !items.length;
    waiting.innerHTML = items.map((w, n) => `<li><span class="w-text">${items.length > 1 ? `${n + 1}. ` : ""}${esc(w.text)}</span>` +
      `<span class="w-where">${esc(w.where)}</span><button type="button" data-unsend="${n + 1}" aria-label="Take back this message">✕</button></li>`).join("");
    $("micBtn").classList.toggle("recording", !!ev.recording);
    $("micBtn").setAttribute("aria-label", ev.recording ? "Stop recording" : "Speak instead of typing");
    if (ev.mode) $("mode").value = ev.mode;
    if (ev.model) setModel(ev.model);
    setComposer();
  }
  function tickTime() { $("statusTime").textContent = startedAt ? shortTime(Date.now() / 1000 - startedAt) : ""; }
  setInterval(tickTime, 1000);
  $("waiting").addEventListener("click", (e) => {
    const b = e.target.closest("[data-unsend]");
    if (b) post("/api/send", {text: `/unsend ${b.dataset.unsend}`});
  });
  $("stopBtn").addEventListener("click", () => post("/api/stop"));

  // -- the composer -------------------------------------------------------------------------------------------
  function setComposer() {
    input.placeholder = ready ? "Ask agentx to do something in this folder"
      : status.working ? "Add to the task while it works, or / for a command" : "Waiting for agentx...";
    $("sendBtn").disabled = !ready && !status.working;
  }
  function grow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + "px"; }
  async function submit() {
    const text = input.value.replace(/\s+$/, "");
    if (!text.trim()) return;
    input.value = ""; grow(); hidePalette();
    await post("/api/send", {text});
  }
  $("composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  input.addEventListener("input", () => { grow(); updatePalette(); });
  input.addEventListener("keydown", (e) => {
    if (!palette.hidden && ["ArrowDown", "ArrowUp", "Tab", "Escape"].includes(e.key)) { paletteKey(e); return; }
    if (!palette.hidden && e.key === "Enter" && !e.shiftKey && paletteItems.length) { e.preventDefault(); choosePalette(); return; }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
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
    if (res.path) { input.value = (input.value ? input.value.replace(/\s*$/, " ") : "Look at ") + res.path + " "; grow(); input.focus(); }
    else addNotice({level: "error", text: res.error || "The picture could not be added."});
  }
  input.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); files.forEach(addPicture); }
  });
  document.addEventListener("dragover", (e) => { if ([...e.dataTransfer.items].some((i) => i.kind === "file")) e.preventDefault(); });
  document.addEventListener("drop", (e) => { const files = [...(e.dataTransfer?.files || [])]; if (files.length) { e.preventDefault(); files.forEach(addPicture); } });

  $("micBtn").addEventListener("click", () => post("/api/voice"));

  // keys for the open card: 1, 2, 3 when you are not typing; Esc stops
  document.addEventListener("keydown", (e) => {
    const typing = /^(TEXTAREA|INPUT|SELECT)$/.test(document.activeElement?.tagName || "");
    if (e.key === "Escape" && !typing && (liveCard() || status.working)) { post("/api/stop"); return; }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    const card = liveCard();
    if (card && card.keyHandler && /^[1-9]$/.test(e.key)) { e.preventDefault(); card.keyHandler(e.key); }
  });

  // -- the top bar and the sidebar ------------------------------------------------------------------------------
  function setModel(model) {
    const sel = $("model");
    if (![...sel.options].some((o) => o.value === model)) sel.prepend(new Option(model, model));
    sel.value = model;
  }
  $("model").addEventListener("change", () => {
    let name = $("model").value;
    if (name === "__other") {
      name = (prompt("Model name, for example claude-sonnet-5-5 or gpt-6.1-sol") || "").trim();
      if (!name) { setModel(status.model || ""); return; }
    }
    post("/api/send", {text: `/model ${name}`});
  });
  $("mode").addEventListener("change", () => post("/api/send", {text: `/mode ${$("mode").value}`}));
  $("newChat").addEventListener("click", () => post("/api/send", {text: "/clear"}));
  $("usageBtn").addEventListener("click", () => post("/api/send", {text: "/usage"}));
  $("settingsBtn").addEventListener("click", () => post("/api/send", {text: "/settings"}));
  $("sideToggle").addEventListener("click", () => $("side").classList.toggle("open"));

  const savedTheme = localStorage.getItem("agentx-theme");
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;
  $("themeBtn").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    localStorage.setItem("agentx-theme", document.documentElement.dataset.theme);
  });

  async function loadState() {
    try {
      const s = await get("/api/state");
      $("folder").textContent = s.folder || "";
      $("folder").title = s.folder || "";
      document.title = `agentx · ${s.folderName || ""}`;
      commands = s.commands || {};
      skills = s.skills || {};
      const sel = $("model");
      sel.innerHTML = (s.models || []).map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("") +
        '<option value="__other">Other model…</option>';
      if (s.model) setModel(s.model);
      if (s.mode) $("mode").value = s.mode;
    } catch (_) { /* the app is still starting: the next status brings it */ }
  }
  async function loadSessions() {
    try {
      const list = await get("/api/sessions");
      const nav = $("sessions");
      if (!list.length) { nav.innerHTML = '<div class="sessions-empty">Your conversations here will be listed once you send a message.</div>'; return; }
      nav.innerHTML = list.map((s) => `<button type="button" class="session" data-id="${esc(s.id)}" aria-current="${s.current}">` +
        `<span class="session-title">${esc(s.title)}</span><span class="session-date">${esc(String(s.updated).replace("T", " ").slice(0, 16))}</span></button>`).join("");
      const current = list.find((s) => s.current);
      if (current) { $("topTitle").textContent = current.title; $("topTitle").dataset.fixed = "1"; }
      else { delete $("topTitle").dataset.fixed; }
    } catch (_) { /* ignore */ }
  }
  $("sessions").addEventListener("click", (e) => {
    const b = e.target.closest("[data-id]");
    if (b && b.getAttribute("aria-current") !== "true") post("/api/send", {text: `/resume ${b.dataset.id}`});
    $("side").classList.remove("open");
  });

  // -- events -----------------------------------------------------------------------------------------------------
  let loading = 0, queue = [], skipUpTo = 0;
  function handle(ev) {
    if (ev.seq <= skipUpTo) return;
    const stick = nearBottom();
    switch (ev.type) {
      case "welcome": welcome = ev; showWelcome(); break;
      case "user": addUser(ev.text); break;
      case "thought": agentTurn().append(el("div", "note", esc(ev.text))); group = null; break;
      case "tool_call": dropWelcome(); toolCall(ev); break;
      case "tool_result": toolResult(ev); break;
      case "final": group = null; lastPrint = null; agentTurn().append(el("div", "answer", markdown(ev.text))); loadSessions(); break;
      case "print": dropWelcome(); addPrint(ev); break;
      case "notice": addNotice(ev); break;
      case "echo": addEcho(ev); break;
      case "team_plan": addPlan(ev); break;
      case "ask": onAsk(ev); break;
      case "answered": onAnswered(ev); break;
      case "status": onStatus(ev); break;
      case "insert": input.value = (input.value ? input.value.replace(/\s*$/, " ") : "") + ev.text; grow(); input.focus(); break;
      case "conversation": switchConversation(ev); break;
      case "reset": reloadConversation(); break;
      case "closed": showClosed(); break;
    }
    keepBottom(stick || ev.type === "user" || ev.type === "ask");
  }
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
  function showClosed() {
    closed = true;
    $("overlayBox").innerHTML = "<h2>agentx has stopped</h2><p>Start it again from your terminal with <code>agentx --ui</code>. " +
      "Your conversation is saved; <code>agentx --ui --continue</code> picks it up.</p>";
    $("overlay").hidden = false;
  }

  function connect() {
    if (!token) {
      $("overlayBox").innerHTML = "<h2>Open agentx from your terminal</h2><p>Run <code>agentx --ui</code> in a project folder; it opens this window with the right address.</p>";
      $("overlay").hidden = false;
      return;
    }
    const source = new EventSource(`/events?t=${encodeURIComponent(token)}`);
    source.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (_) { /* a broken event is skipped */ } };
    source.onerror = () => { if (closed) source.close(); };
  }

  loadState();
  loadSessions();
  setComposer();
  connect();
  input.focus();
})();
