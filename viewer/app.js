/* AgentX trace viewer -- reads FileHook JSONL, renders a timeline. */

(function () {
  "use strict";

  let events = [];              // parsed events
  let hiddenTypes = new Set();  // event types the user toggled off
  let filterText = "";

  const $ = (id) => document.getElementById(id);

  function classify(evt) {
    const t = String(evt.type || "").toLowerCase();
    if (t.includes("agent")) return "agent";
    if (t.includes("llm") || t.includes("call")) return "llm";
    if (t.includes("tool") || t.includes("cache") || t.includes("circuit")) return "tool";
    return "other";
  }

  function isError(evt) {
    const t = String(evt.type || "").toLowerCase();
    return t.includes("error") || t.includes("circuit_open") ||
           (evt.data && evt.data.is_error);
  }

  function fmtDuration(ms) {
    if (ms == null) return "";
    if (ms < 1000) return ms.toFixed(0) + "ms";
    return (ms / 1000).toFixed(2) + "s";
  }

  function fmtRelative(startTs, ts) {
    if (!ts || !startTs) return "";
    const delta = ts - startTs;
    if (delta < 1) return delta.toFixed(3) + "s";
    return delta.toFixed(2) + "s";
  }

  function parseJsonl(text) {
    const out = [];
    for (const line of text.split(/\r?\n/)) {
      const s = line.trim();
      if (!s) continue;
      try { out.push(JSON.parse(s)); }
      catch (e) { /* skip malformed line */ }
    }
    return out;
  }

  function loadFile(file) {
    const reader = new FileReader();
    reader.onload = (e) => {
      events = parseJsonl(String(e.target.result));
      // Normalize timestamp field: accept `ts`, `timestamp`, `time`.
      events.forEach((ev) => {
        const t = ev.ts ?? ev.timestamp ?? ev.time;
        ev._ts = typeof t === "number" ? t : (t ? Date.parse(t) / 1000 : null);
      });
      render();
    };
    reader.readAsText(file);
  }

  function render() {
    if (!events.length) {
      $("empty-state").style.display = "flex";
      $("timeline").innerHTML = "";
      $("summary").innerHTML = "";
      $("type-filter").innerHTML = "";
      return;
    }
    $("empty-state").style.display = "none";

    // Compute summary
    const summary = {
      total: events.length,
      types: new Map(),
      errors: 0,
      totalTokensIn: 0,
      totalTokensOut: 0,
      cacheReads: 0,
      totalToolCalls: 0,
      agentRuns: 0,
    };
    let startTs = Infinity, endTs = -Infinity;
    for (const ev of events) {
      summary.types.set(ev.type, (summary.types.get(ev.type) || 0) + 1);
      if (isError(ev)) summary.errors++;
      if (ev._ts) {
        if (ev._ts < startTs) startTs = ev._ts;
        if (ev._ts > endTs) endTs = ev._ts;
      }
      const d = ev.data || {};
      if (typeof d.input_tokens === "number") summary.totalTokensIn += d.input_tokens;
      if (typeof d.output_tokens === "number") summary.totalTokensOut += d.output_tokens;
      if (typeof d.cache_read_tokens === "number") summary.cacheReads += d.cache_read_tokens;
      const t = String(ev.type || "").toLowerCase();
      if (t.includes("tool_call_start") || t === "tool_call") summary.totalToolCalls++;
      if (t.includes("agent_start") || t === "agent_run") summary.agentRuns++;
    }
    if (!isFinite(startTs)) startTs = null;
    if (!isFinite(endTs)) endTs = null;

    // Render summary
    const wallSec = (startTs != null && endTs != null) ? (endTs - startTs) : null;
    $("summary").innerHTML = `
      <div class="stat"><span class="label">events</span><span class="value">${summary.total}</span></div>
      <div class="stat"><span class="label">agent runs</span><span class="value">${summary.agentRuns}</span></div>
      <div class="stat"><span class="label">tool calls</span><span class="value">${summary.totalToolCalls}</span></div>
      <div class="stat"><span class="label">errors</span><span class="value" style="color:${summary.errors ? 'var(--err)' : ''}">${summary.errors}</span></div>
      <div class="stat"><span class="label">wall clock</span><span class="value">${wallSec != null ? fmtDuration(wallSec * 1000) : '-'}</span></div>
      <div class="stat"><span class="label">input tokens</span><span class="value">${summary.totalTokensIn.toLocaleString()}</span></div>
      <div class="stat"><span class="label">output tokens</span><span class="value">${summary.totalTokensOut.toLocaleString()}</span></div>
      ${summary.cacheReads ? `<div class="stat"><span class="label">cache reads</span><span class="value">${summary.cacheReads.toLocaleString()}</span></div>` : ""}
    `;

    // Type filter
    const types = Array.from(summary.types.keys()).sort();
    $("type-filter").innerHTML = types.map((t) => {
      const count = summary.types.get(t);
      const off = hiddenTypes.has(t) ? "off" : "";
      return `<div class="type-btn ${off}" data-type="${escapeHtml(t)}">
        <span>${escapeHtml(t)}</span><span class="count">${count}</span>
      </div>`;
    }).join("");
    $("type-filter").querySelectorAll(".type-btn").forEach((el) => {
      el.addEventListener("click", () => {
        const t = el.getAttribute("data-type");
        if (hiddenTypes.has(t)) hiddenTypes.delete(t);
        else hiddenTypes.add(t);
        render();
      });
    });

    // Timeline
    const tl = $("timeline");
    tl.innerHTML = "";
    const filterLower = filterText.toLowerCase();
    let shown = 0;
    events.forEach((ev, idx) => {
      if (hiddenTypes.has(ev.type)) return;
      if (filterLower && !matches(ev, filterLower)) return;
      shown++;
      const cls = classify(ev);
      const errCls = isError(ev) ? " error" : "";
      const rel = ev._ts != null ? fmtRelative(startTs, ev._ts) : "";
      const dur = ev.data && ev.data.duration_ms != null
        ? fmtDuration(ev.data.duration_ms) : "";
      const title = summarize(ev);
      const div = document.createElement("div");
      div.className = `event ${cls}${errCls}`;
      div.innerHTML = `
        <span class="time">${rel}</span>
        <span class="dot ${cls}"></span>
        <span><span class="type">${escapeHtml(ev.type || "?")}</span>${escapeHtml(title)}</span>
        <span class="duration">${dur}</span>
        <div class="details"><pre>${escapeHtml(JSON.stringify(ev, null, 2))}</pre></div>
      `;
      div.addEventListener("click", () => div.classList.toggle("expanded"));
      tl.appendChild(div);
    });

    if (shown === 0) {
      tl.innerHTML = `<div style="color:var(--text-mute);padding:20px;text-align:center">
        No events match the current filter.
      </div>`;
    }
  }

  function matches(ev, needle) {
    const hay = (ev.type + " " + JSON.stringify(ev.data || {})).toLowerCase();
    return hay.includes(needle);
  }

  function summarize(ev) {
    const d = ev.data || {};
    const t = String(ev.type || "").toLowerCase();
    if (t.includes("tool_call_start") || t.includes("tool_call_end")) {
      return d.tool_name || d.name || "";
    }
    if (t.includes("agent_start")) return d.query ? `"${String(d.query).slice(0, 80)}"` : "";
    if (t.includes("agent_end")) return d.final_answer ? `"${String(d.final_answer).slice(0, 80)}"` : "";
    if (t.includes("llm")) {
      const parts = [];
      if (d.input_tokens != null) parts.push(`in=${d.input_tokens}`);
      if (d.output_tokens != null) parts.push(`out=${d.output_tokens}`);
      return parts.join(" ");
    }
    return "";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("file").addEventListener("change", (e) => {
      const f = e.target.files[0];
      if (f) loadFile(f);
    });
    const dz = $("dropzone");
    ["dragenter", "dragover"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragover"); })
    );
    ["dragleave", "drop"].forEach((ev) =>
      dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("dragover"); })
    );
    dz.addEventListener("drop", (e) => {
      const f = e.dataTransfer.files[0];
      if (f) loadFile(f);
    });
    $("filter").addEventListener("input", (e) => {
      filterText = e.target.value;
      render();
    });
    $("clear").addEventListener("click", () => {
      events = []; hiddenTypes.clear(); filterText = "";
      $("filter").value = "";
      render();
    });
    render();
  });
})();
