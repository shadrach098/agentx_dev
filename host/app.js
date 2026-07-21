/* AgentX docs SPA -- editorial, dark-first, deliberate motion. */

(function () {
  "use strict";

  const NAV = window.AGENTX_NAV || [];
  const DOCS = window.AGENTX_DOCS || {};

  /* ------------------------------------------------------------------ Helpers */

  const $ = (id) => document.getElementById(id);

  function slugify(s) {
    return String(s || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function escapeRegex(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
  function safeCssId(s) {
    if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }
  function prefersReducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  /* ------------------------------------------------------------------ Marked */

  if (typeof marked !== "undefined") {
    marked.setOptions({ gfm: true, breaks: false, headerIds: false });
  }

  function addHeadingIds(container) {
    const seen = new Map();
    container.querySelectorAll("h1, h2, h3, h4, h5, h6").forEach((h) => {
      const base = slugify(h.textContent) || "section";
      const n = (seen.get(base) || 0) + 1;
      seen.set(base, n);
      h.id = n > 1 ? `${base}-${n}` : base;
    });
  }

  /* -------- Breadcrumbs -------- */

  function renderBreadcrumbs(slug) {
    // Locate the slug in NAV to figure out which group it belongs to.
    let groupName = null;
    let itemTitle = null;
    for (const g of NAV) {
      for (const it of g.items) {
        if (it.slug === slug) {
          groupName = g.name;
          itemTitle = it.title;
          break;
        }
      }
      if (groupName) break;
    }
    if (!groupName) return "";
    return `
      <div class="breadcrumbs">
        <span class="group">${escapeHtml(groupName)}</span>
        <span class="sep">/</span>
        <span>${escapeHtml(itemTitle)}</span>
      </div>
    `;
  }

  /* -------- Header anchor links -------- */

  function addAnchorLinks(container, slug) {
    container.querySelectorAll("h2, h3").forEach((h) => {
      if (!h.id) return;
      const a = document.createElement("a");
      a.className = "anchor-link";
      a.href = `#${slug}#${h.id}`;
      a.textContent = "#";
      a.setAttribute("aria-label", `Permalink to ${h.textContent}`);
      h.insertBefore(a, h.firstChild);
    });
  }

  /* -------- Reading progress bar -------- */

  function updateProgressBar() {
    const bar = $("progress-bar");
    if (!bar) return;
    const doc = $("doc");
    if (!doc || currentSlug() === "index") {
      bar.style.width = "0%";
      return;
    }
    const rect = doc.getBoundingClientRect();
    const total = doc.offsetHeight - window.innerHeight + 100; // padding buffer
    if (total <= 0) {
      bar.style.width = "100%";
      return;
    }
    const scrolled = -rect.top;
    const pct = Math.max(0, Math.min(100, (scrolled / total) * 100));
    bar.style.width = pct.toFixed(1) + "%";
  }

  function enhanceCodeBlocks(container) {
    container.querySelectorAll("pre").forEach((pre) => {
      const code = pre.querySelector("code");
      if (!code) return;
      // Language label from `language-xxx` class.
      const clsList = Array.from(code.classList);
      const lang = clsList.find((c) => c.startsWith("language-"));
      if (lang) pre.setAttribute("data-lang", lang.slice("language-".length));
      // Copy button.
      const btn = document.createElement("button");
      btn.className = "copy-btn";
      btn.textContent = "copy";
      btn.setAttribute("aria-label", "Copy code");
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const text = code.textContent || "";
        navigator.clipboard.writeText(text).then(() => {
          btn.textContent = "copied";
          btn.classList.add("copied");
          setTimeout(() => {
            btn.textContent = "copy";
            btn.classList.remove("copied");
          }, 1400);
        }).catch(() => {
          btn.textContent = "err";
        });
      });
      pre.appendChild(btn);
    });
  }

  /* ------------------------------------------------------------------ Order */

  const ORDER = [];
  NAV.forEach((g) => g.items.forEach((it) => ORDER.push(it.slug)));

  function currentSlug() {
    const raw = (location.hash || "#").slice(1);
    if (!raw) return "index";
    return raw.split("#")[0] || "index";
  }
  function currentAnchor() {
    const parts = (location.hash || "#").slice(1).split("#");
    return parts.length > 1 ? parts.slice(1).join("#") : "";
  }

  /* ------------------------------------------------------------------ Sidebar */

  function loadCollapsed() {
    try { return new Set(JSON.parse(localStorage.getItem("agentx-collapsed") || "[]")); }
    catch { return new Set(); }
  }
  function saveCollapsed(set) {
    try { localStorage.setItem("agentx-collapsed", JSON.stringify(Array.from(set))); }
    catch {}
  }

  function renderSidebar(activeSlug) {
    const nav = $("nav");
    if (!nav) return;
    const marker = $("active-marker");
    nav.innerHTML = "";
    if (marker) nav.appendChild(marker);

    const collapsed = loadCollapsed();
    let activeFound = false;
    NAV.forEach((group) => {
      const wrap = document.createElement("div");
      wrap.className = "nav-group" + (collapsed.has(group.name) ? " collapsed" : "");
      const title = document.createElement("div");
      title.className = "nav-group-title";
      // SVG chevron (down when expanded, rotated -90deg when collapsed
      // via CSS). Crisper than a Unicode arrow at 10px.
      title.innerHTML =
        `<span>${escapeHtml(group.name)}</span>` +
        `<span class="caret" aria-hidden="true">` +
          `<svg viewBox="0 0 10 10" xmlns="http://www.w3.org/2000/svg">` +
            `<path d="M 2 3.5 L 5 6.5 L 8 3.5" fill="none" ` +
              `stroke="currentColor" stroke-width="1.5" ` +
              `stroke-linecap="round" stroke-linejoin="round"/>` +
          `</svg>` +
        `</span>`;
      title.setAttribute("data-group", group.name);
      title.addEventListener("click", () => {
        wrap.classList.toggle("collapsed");
        const isCollapsed = wrap.classList.contains("collapsed");
        const set = loadCollapsed();
        if (isCollapsed) set.add(group.name);
        else set.delete(group.name);
        saveCollapsed(set);
        // Marker may need to re-position now that item heights changed.
        requestAnimationFrame(() => updateActiveMarker());
      });
      wrap.appendChild(title);
      group.items.forEach((it) => {
        const a = document.createElement("a");
        a.href = "#" + it.slug;
        const isActive = !activeFound && it.slug === activeSlug;
        if (isActive) activeFound = true;
        a.className = "nav-item" + (isActive ? " active" : "");
        a.textContent = it.title;
        a.setAttribute("data-slug", it.slug);
        wrap.appendChild(a);
      });
      nav.appendChild(wrap);
    });

    // Double-rAF so getBoundingClientRect reads a settled layout.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => updateActiveMarker());
    });
  }

  function updateActiveMarker() {
    const marker = $("active-marker");
    const nav = $("nav");
    if (!marker || !nav) return;
    // Only one .active item should exist (guaranteed by renderSidebar).
    // Query all just in case, and take the first.
    const active = nav.querySelector(".nav-item.active");
    if (!active) {
      marker.classList.remove("visible");
      return;
    }
    const rect = active.getBoundingClientRect();
    const navRect = nav.getBoundingClientRect();
    const y = rect.top - navRect.top + (rect.height - 22) / 2;
    marker.style.transform = `translateY(${y}px)`;
    marker.classList.add("visible");
  }

  /* ------------------------------------------------------------------ TOC */

  function renderToc(container) {
    const toc = $("toc");
    if (!toc) return;
    // Article headings only -- skip headings inside cards / lists / other
    // UI chrome that shouldn't appear in the "on this page" navigation.
    const raw = container.querySelectorAll("h2, h3");
    const headings = Array.from(raw).filter((h) => {
      return !h.closest(".card, .search-result, .prev-next, .whats-new-list");
    });
    if (!headings.length) {
      toc.innerHTML = "";
      return;
    }
    let html = `<div class="toc-title">On this page</div><ul class="toc-list">`;
    headings.forEach((h) => {
      const depth = h.tagName === "H2" ? 2 : 3;
      html += `<li class="toc-item depth-${depth}" data-target="${h.id}">${escapeHtml(h.textContent)}</li>`;
    });
    html += `</ul>`;
    toc.innerHTML = html;

    toc.querySelectorAll(".toc-item").forEach((li) => {
      li.addEventListener("click", () => {
        const id = li.getAttribute("data-target");
        const target = document.getElementById(id);
        if (!target) return;
        target.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
        // Update URL without triggering the router.
        history.replaceState(null, "", "#" + currentSlug() + "#" + id);
      });
    });

    // Scrollspy — set .active on the toc item closest to the top.
    if (window._tocObserver) window._tocObserver.disconnect();
    const items = new Map();
    toc.querySelectorAll(".toc-item").forEach((li) => items.set(li.getAttribute("data-target"), li));
    const observer = new IntersectionObserver(
      (entries) => {
        // Find the entry most visible at the top of the viewport.
        const visible = entries.filter((e) => e.isIntersecting).sort(
          (a, b) => a.boundingClientRect.top - b.boundingClientRect.top,
        );
        if (visible.length === 0) return;
        const id = visible[0].target.id;
        toc.querySelectorAll(".toc-item").forEach((el) => el.classList.remove("active"));
        const li = items.get(id);
        if (li) li.classList.add("active");
      },
      { rootMargin: "-70px 0px -70% 0px", threshold: 0 },
    );
    headings.forEach((h) => observer.observe(h));
    window._tocObserver = observer;
  }

  /* ------------------------------------------------------------------ Rendering */

  function renderDoc(slug) {
    const doc = $("doc");
    if (!doc) return;

    if (slug === "index") {
      renderLanding(doc);
      renderPrevNext(null);
      renderToc(doc);   // hero has no headings; clears the toc
      updateProgressBar();
      return;
    }

    const entry = DOCS[slug];
    if (!entry) {
      doc.innerHTML = `
        <h1>Page not found</h1>
        <p>No page named <code>${escapeHtml(slug)}</code>.</p>
        <p><a href="#">Return home</a></p>`;
      renderPrevNext(null);
      renderToc(doc);
      updateProgressBar();
      return;
    }

    try {
      if (typeof marked === "undefined") {
        doc.innerHTML = `
          <div class="error-note">
            <strong>marked.js failed to load.</strong> Rendering raw markdown.
            Try serving via <code>python -m http.server -d host 8000</code>.
          </div>
          <h1>${escapeHtml(entry.title)}</h1>
          <pre style="white-space:pre-wrap">${escapeHtml(entry.markdown)}</pre>
        `;
      } else {
        const md = rewriteRelativeLinks(entry.markdown, slug);
        const html = marked.parse(md);
        // Prepend breadcrumbs to orient the reader.
        doc.innerHTML = renderBreadcrumbs(slug) + html;
        addHeadingIds(doc);
        addAnchorLinks(doc, slug);
        enhanceCodeBlocks(doc);
      }

      if (typeof Prism !== "undefined") {
        doc.querySelectorAll("pre code").forEach((el) => {
          try { Prism.highlightElement(el); } catch (e) {}
        });
      }

      renderToc(doc);
      updateProgressBar();

      const anchor = currentAnchor();
      if (anchor) {
        const target = doc.querySelector("#" + safeCssId(anchor));
        if (target) target.scrollIntoView({ behavior: "instant", block: "start" });
      } else {
        window.scrollTo({ top: 0 });
      }
    } catch (err) {
      console.error("[agentx docs] render error", err);
      doc.innerHTML = `
        <h1>Render error</h1>
        <p>Something broke rendering <code>${escapeHtml(slug)}</code>. See the browser console.</p>
        <pre style="white-space:pre-wrap">${escapeHtml(err && (err.stack || err.message) || String(err))}</pre>
      `;
    }
    renderPrevNext(slug);
  }

  /* ------------------------------------------------------------------ Landing */

  function renderLanding(doc) {
    const cards = [
      { slug: "getting-started",          title: "getting-started",         desc: "Install, first agent, mental model." },
      { slug: "use-cases",                title: "use-cases",               desc: "12 concrete scenarios with runnable code." },
      { slug: "concepts/models",          title: "chat-models",             desc: "GPT, Claude, retries, budgets, streaming." },
      { slug: "concepts/tools",           title: "tools",                    desc: "Standard, Structured, async variants." },
      { slug: "concepts/agents",          title: "agents",                   desc: "Types, parsers, runner loop, modes." },
      { slug: "concepts/permissions",     title: "permissions",              desc: "Sandbox, DefaultTools, capability model." },
      { slug: "advanced/memory",          title: "memory",                   desc: "Six strategies incl. SemanticMemory (3.1)." },
      { slug: "advanced/rag",             title: "rag",                      desc: "Embeddings, VectorStore, vector_search (3.1)." },
      { slug: "advanced/prompt-caching",  title: "prompt-caching",           desc: "Anthropic ephemeral cache (3.1)." },
      { slug: "advanced/parallel-tools",  title: "parallel-tool-calls",      desc: "bind_tools_natively (3.1)." },
      { slug: "advanced/supervisor",      title: "supervisor",               desc: "Decompose > dispatch > synthesize." },
      { slug: "advanced/handoffs",        title: "handoffs",                 desc: "Peer-to-peer routing (3.1)." },
      { slug: "advanced/evals",           title: "evals",                    desc: "EvalRunner, assertions, CI (3.1)." },
      { slug: "advanced/production-controls", title: "production",           desc: "Budgets, breakers, caches." },
      { slug: "reference/api-summary",    title: "api-reference",            desc: "Every exported symbol at a glance." },
      { slug: "cookbook/patterns",        title: "patterns",                 desc: "Reusable multi-agent shapes." },
      { slug: "cookbook/troubleshooting", title: "troubleshooting",          desc: "Common errors + fixes." },
      { slug: "cookbook/faq",             title: "faq",                      desc: "Model choice, mode choice, integrations." },
    ];

    // Compose the hero title with per-character animation. The name "agentx_dev"
    // has 10 chars; each gets its own delay so the reveal reads left-to-right.
    const titleChars = "agentx_dev";
    const heroTitle = titleChars
      .split("")
      .map((c, i) => `<span class="glyph" style="animation-delay:${i * 45}ms">${c}</span>`)
      .join("");

    // Hero code snippet -- the smallest working example, with hand-tinted
    // syntax coloring so it doesn't need Prism to look right at first
    // paint. The framework's proposition is "small enough to fit in the
    // hero" -- proving it here is stronger than describing it.
    const heroSnippet = `<span class="kw">from</span> <span class="cls">agentx_dev</span> <span class="kw">import</span> AgentRunner, AgentType, Claude

runner = <span class="fn">AgentRunner</span>(model=<span class="fn">Claude</span>(), agent=AgentType.ReAct, tools=[])
result = runner.<span class="fn">invoke</span>(<span class="str">"What is MVCC?"</span>)
<span class="fn">print</span>(result.content)`;

    doc.innerHTML = `
      <section class="hero">
        <div class="hero-mark" aria-hidden="true">
          <svg viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg">
            <path d="M 6 8 L 16 16 L 6 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
            <rect x="21" y="8" width="3" height="16" rx="0.5" fill="currentColor"/>
          </svg>
        </div>
        <h1>${heroTitle}<span class="cursor">|</span></h1>
        <p class="hero-tagline">A production-grade Python framework for building LLM agents.</p>
        <p class="hero-sub">// nothing sprawls</p>
        <div class="hero-code"><pre><code>${heroSnippet}</code></pre></div>
        <div class="hero-cta">
          <a href="#getting-started" class="primary">get started <span class="arrow">→</span></a>
          <a href="#reference/api-summary">api reference</a>
          <a href="https://github.com/shadrach098/Bruce_framework">source</a>
        </div>
      </section>

      <h2 class="section-title">Browse</h2>
      <div class="card-grid">
        ${cards.map(c => `
          <a class="card" href="#${c.slug}">
            <h3>${escapeHtml(c.title)}</h3>
            <p>${escapeHtml(c.desc)}</p>
          </a>`).join("")}
      </div>

      <h2 class="section-title">What's new in 3.1</h2>
      <ul class="whats-new-list">
        <li>
          <strong>Prompt caching</strong>
          <div class="desc"><code>Claude(enable_prompt_cache=True)</code> caches system prompt + tool schemas + long history. ~90% cheaper on cached input.</div>
        </li>
        <li>
          <strong>Parallel tools</strong>
          <div class="desc"><code>bind_tools_natively=True</code> dispatches multiple <code>tool_use</code> blocks concurrently on a bounded thread pool.</div>
        </li>
        <li>
          <strong>Semantic memory</strong>
          <div class="desc"><code>SemanticMemory</code> embeds every turn and pulls back top-K on <code>set_query</code>.</div>
        </li>
        <li>
          <strong>Retrieval (RAG)</strong>
          <div class="desc"><code>VectorStore</code> + <code>vector_search_tool()</code>. Zero-dep <code>HashEmbeddings</code>, or <code>OpenAIEmbeddings</code> for production.</div>
        </li>
        <li>
          <strong>Handoffs</strong>
          <div class="desc"><code>HandoffCoordinator</code> routes between peer agents mid-run (Swarm-style).</div>
        </li>
        <li>
          <strong>Evals harness</strong>
          <div class="desc"><code>EvalRunner</code> + assertion helpers + JSON case loader + CLI.</div>
        </li>
      </ul>
    `;
  }

  /* ------------------------------------------------------------------ Prev/next */

  function renderPrevNext(slug) {
    const wrap = $("doc-nav-prev-next");
    if (!wrap) return;
    wrap.innerHTML = "";
    if (!slug) return;
    const idx = ORDER.indexOf(slug);
    const prev = idx > 0 ? ORDER[idx - 1] : null;
    const next = idx >= 0 && idx < ORDER.length - 1 ? ORDER[idx + 1] : null;
    const spacer = () => {
      const d = document.createElement("div");
      d.className = "prev-next empty";
      return d;
    };
    if (prev && DOCS[prev]) {
      const a = document.createElement("a");
      a.className = "prev-next prev";
      a.href = "#" + prev;
      a.innerHTML = `<div class="label">← previous</div><div class="title">${escapeHtml(DOCS[prev].title)}</div>`;
      wrap.appendChild(a);
    } else wrap.appendChild(spacer());
    if (next && DOCS[next]) {
      const a = document.createElement("a");
      a.className = "prev-next next";
      a.href = "#" + next;
      a.innerHTML = `<div class="label">next →</div><div class="title">${escapeHtml(DOCS[next].title)}</div>`;
      wrap.appendChild(a);
    } else wrap.appendChild(spacer());
  }

  /* ------------------------------------------------------------------ Links */

  function rewriteRelativeLinks(md, currentSlug) {
    const currentDir = currentSlug.includes("/")
      ? currentSlug.slice(0, currentSlug.lastIndexOf("/"))
      : "";
    return md.replace(/\]\(([^)]+)\)/g, (match, href) => {
      if (/^(https?:|mailto:|#|\/)/.test(href)) return match;
      if (!/\.md(#.*)?$/.test(href)) return match;
      const [path, anchor] = href.split("#");
      const resolved = resolvePath(currentDir, path).replace(/\.md$/, "");
      return `](#${resolved}${anchor ? "#" + anchor : ""})`;
    });
  }
  function resolvePath(baseDir, relPath) {
    const parts = (baseDir ? baseDir.split("/") : []).concat(relPath.split("/"));
    const out = [];
    for (const p of parts) {
      if (p === "" || p === ".") continue;
      if (p === "..") out.pop();
      else out.push(p);
    }
    return out.join("/");
  }

  /* ------------------------------------------------------------------ Search */

  function runSearch(query) {
    const q = query.trim().toLowerCase();
    if (!q) return null;
    const results = [];
    for (const [slug, entry] of Object.entries(DOCS)) {
      if (!entry || !entry.markdown) continue;
      const hay = (entry.title + "\n" + entry.markdown).toLowerCase();
      const idx = hay.indexOf(q);
      if (idx !== -1) {
        const start = Math.max(0, idx - 60);
        const end = Math.min(entry.markdown.length, idx + query.length + 120);
        const snippet =
          (start > 0 ? "... " : "") +
          entry.markdown.slice(start, end) +
          (end < entry.markdown.length ? " ..." : "");
        results.push({ slug, title: entry.title, snippet, score: hay.indexOf(q) });
      }
    }
    results.sort((a, b) => a.score - b.score);
    return results.slice(0, 20);
  }

  function renderSearch(query) {
    const doc = $("doc");
    if (!doc) return;
    const results = runSearch(query);
    if (!results) { route(); return; }
    if (results.length === 0) {
      doc.innerHTML = `<h1>search</h1><p>No matches for <code>${escapeHtml(query)}</code>.</p>`;
      renderPrevNext(null);
      renderToc(doc);
      return;
    }
    const eq = escapeRegex(query);
    const hi = (s) => escapeHtml(s).replace(new RegExp(eq, "gi"), (m) => `<mark>${m}</mark>`);
    doc.innerHTML = `
      <h1>search</h1>
      <p style="color:var(--text-dim);font-size:13.5px;margin-bottom:24px;font-family:var(--font-mono)">
        ${results.length} match${results.length === 1 ? "" : "es"} for <code>${escapeHtml(query)}</code>
      </p>
      <div class="search-results">
        ${results.map(r => `
          <a class="search-result" href="#${r.slug}">
            <div class="title">${hi(r.title)}</div>
            <div class="snippet">${hi(r.snippet).replace(/\n/g, " ")}</div>
          </a>`).join("")}
      </div>
    `;
    renderPrevNext(null);
    renderToc(doc);
  }

  /* ------------------------------------------------------------------ Router */

  function route() {
    const slug = currentSlug();
    renderSidebar(slug);
    renderDoc(slug);
  }

  window.addEventListener("hashchange", route);

  /* ------------------------------------------------------------------ Init */

  function initTopbarScrollBorder() {
    const topbar = $("topbar");
    if (!topbar) return;
    const onScroll = () => {
      topbar.classList.toggle("scrolled", window.scrollY > 12);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  /* -------- Command palette (Cmd+K) -------- */

  let paletteResults = [];
  let paletteIdx = 0;

  function openPalette() {
    const bd = $("palette-backdrop");
    const inp = $("palette-input");
    if (!bd || !inp) return;
    bd.classList.add("open");
    bd.setAttribute("aria-hidden", "false");
    inp.value = "";
    renderPalette("");
    // Focus after the transition starts so the caret lands cleanly.
    requestAnimationFrame(() => inp.focus());
  }

  function closePalette() {
    const bd = $("palette-backdrop");
    if (!bd) return;
    bd.classList.remove("open");
    bd.setAttribute("aria-hidden", "true");
  }

  function renderPalette(query) {
    const list = $("palette-list");
    if (!list) return;
    const q = query.trim().toLowerCase();

    // Empty state: show ALL pages grouped by section (the palette becomes
    // a keyboard navigator when you haven't typed anything yet).
    if (!q) {
      const items = [];
      NAV.forEach((g) => {
        items.push({ groupHeader: g.name });
        g.items.forEach((it) => {
          if (it.slug === "index") return;
          items.push({ slug: it.slug, title: it.title, snippet: "", group: g.name });
        });
      });
      paletteResults = items.filter((x) => !x.groupHeader);
      paletteIdx = 0;
      list.innerHTML = items.map((x, i) => {
        if (x.groupHeader) return `<div class="palette-group">${escapeHtml(x.groupHeader)}</div>`;
        const idx = paletteResults.indexOf(x);
        return `<div class="palette-item ${idx === 0 ? "selected" : ""}" data-idx="${idx}" data-slug="${escapeHtml(x.slug)}">
          <div class="palette-item-title">${escapeHtml(x.title)}</div>
        </div>`;
      }).join("");
      wirePaletteClicks();
      return;
    }

    // Search: substring match, ranked by earliest occurrence.
    const results = [];
    for (const [slug, entry] of Object.entries(DOCS)) {
      if (!entry || !entry.markdown) continue;
      if (slug === "index") continue;
      const hay = (entry.title + "\n" + entry.markdown).toLowerCase();
      const idx = hay.indexOf(q);
      if (idx !== -1) {
        const start = Math.max(0, idx - 40);
        const end = Math.min(entry.markdown.length, idx + q.length + 80);
        const snippet = (start > 0 ? "..." : "") +
                        entry.markdown.slice(start, end) +
                        (end < entry.markdown.length ? "..." : "");
        results.push({ slug, title: entry.title, snippet, score: idx });
      }
    }
    results.sort((a, b) => a.score - b.score);
    const trimmed = results.slice(0, 30);
    paletteResults = trimmed;
    paletteIdx = 0;

    if (trimmed.length === 0) {
      list.innerHTML = `<div class="palette-empty">no matches for "${escapeHtml(query)}"</div>`;
      return;
    }

    const eq = escapeRegex(query);
    const hi = (s) => escapeHtml(s).replace(new RegExp(eq, "gi"), (m) => `<mark>${m}</mark>`);
    list.innerHTML = trimmed.map((r, i) => `
      <div class="palette-item ${i === 0 ? "selected" : ""}" data-idx="${i}" data-slug="${escapeHtml(r.slug)}">
        <div class="palette-item-title">${hi(r.title)}</div>
        <div class="palette-item-snippet">${hi(r.snippet.replace(/\n/g, " "))}</div>
      </div>
    `).join("");
    wirePaletteClicks();
  }

  function wirePaletteClicks() {
    $("palette-list").querySelectorAll(".palette-item").forEach((el) => {
      el.addEventListener("click", () => {
        const slug = el.getAttribute("data-slug");
        location.hash = "#" + slug;
        closePalette();
      });
      el.addEventListener("mouseenter", () => {
        const i = parseInt(el.getAttribute("data-idx"), 10);
        if (!Number.isNaN(i)) selectPaletteItem(i);
      });
    });
  }

  function selectPaletteItem(idx) {
    const list = $("palette-list");
    if (!list) return;
    const items = list.querySelectorAll(".palette-item");
    if (!items.length) return;
    idx = ((idx % items.length) + items.length) % items.length;
    paletteIdx = idx;
    items.forEach((el, i) => el.classList.toggle("selected", i === idx));
    items[idx].scrollIntoView({ block: "nearest" });
  }

  function initPalette() {
    const bd = $("palette-backdrop");
    const inp = $("palette-input");
    const trigger = $("palette-trigger");
    if (!bd || !inp) return;

    if (trigger) trigger.addEventListener("click", openPalette);
    bd.addEventListener("click", (e) => {
      if (e.target === bd) closePalette();
    });

    // Debounced input
    let timer;
    inp.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => renderPalette(inp.value), 60);
    });

    // Keyboard navigation
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { closePalette(); return; }
      if (e.key === "ArrowDown") { e.preventDefault(); selectPaletteItem(paletteIdx + 1); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); selectPaletteItem(paletteIdx - 1); return; }
      if (e.key === "Enter") {
        e.preventDefault();
        const r = paletteResults[paletteIdx];
        if (r && r.slug) {
          location.hash = "#" + r.slug;
          closePalette();
        }
      }
    });

    // Global Cmd/Ctrl+K to open
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        openPalette();
      }
    });
  }

  function initTheme() {
    let saved = null;
    try { saved = localStorage.getItem("agentx-theme"); } catch {}
    document.documentElement.setAttribute("data-theme", saved || "dark");
    const btn = $("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("agentx-theme", next); } catch {}
    });
  }

  function init() {
    initTopbarScrollBorder();
    initPalette();
    initTheme();
    route();
    // Reposition the sidebar marker when layout can shift under it:
    //  1. Window resize.
    //  2. Web fonts finishing load -- Inter/JetBrains Mono lazy-load and
    //     shift each nav item's height slightly.
    window.addEventListener("resize", () => {
      updateActiveMarker();
      updateProgressBar();
    }, { passive: true });
    // Reading progress: cheap enough to update on every scroll frame.
    window.addEventListener("scroll", updateProgressBar, { passive: true });
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(() => updateActiveMarker()).catch(() => {});
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
