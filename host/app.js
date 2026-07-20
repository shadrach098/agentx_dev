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

  function renderSidebar(activeSlug) {
    const nav = $("nav");
    if (!nav) return;
    // Keep the active-marker element; rebuild the rest.
    const marker = $("active-marker");
    nav.innerHTML = "";
    if (marker) nav.appendChild(marker);

    NAV.forEach((group, gIdx) => {
      const wrap = document.createElement("div");
      wrap.className = "nav-group";
      const title = document.createElement("div");
      title.className = "nav-group-title";
      title.textContent = group.name;
      wrap.appendChild(title);
      group.items.forEach((it) => {
        const a = document.createElement("a");
        a.href = "#" + it.slug;
        a.className = "nav-item" + (it.slug === activeSlug ? " active" : "");
        a.textContent = it.title;
        a.setAttribute("data-slug", it.slug);
        wrap.appendChild(a);
      });
      nav.appendChild(wrap);
    });

    // Position the sliding accent marker at the active item.
    updateActiveMarker();
  }

  function updateActiveMarker() {
    const marker = $("active-marker");
    const nav = $("nav");
    if (!marker || !nav) return;
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
    const headings = container.querySelectorAll("h2, h3");
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
        doc.innerHTML = marked.parse(md);
        addHeadingIds(doc);
        enhanceCodeBlocks(doc);
      }

      if (typeof Prism !== "undefined") {
        doc.querySelectorAll("pre code").forEach((el) => {
          try { Prism.highlightElement(el); } catch (e) {}
        });
      }

      renderToc(doc);

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

    doc.innerHTML = `
      <section class="hero">
        <h1>${heroTitle}<span class="cursor">|</span></h1>
        <p class="hero-tagline">A small, production-ready Python framework for building LLM agents.</p>
        <p class="hero-sub">// think LangChain, minus the sprawl</p>
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

  function initSearch() {
    const box = $("search");
    if (!box) return;
    let timer;
    box.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const q = box.value;
        if (q.trim()) renderSearch(q);
        else route();
      }, 130);
    });
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        box.focus();
        box.select();
      }
      if (e.key === "Escape" && document.activeElement === box) {
        box.value = "";
        box.blur();
        route();
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
    initSearch();
    initTheme();
    route();
    // Reposition the sidebar marker on window resize (fonts can reflow items).
    window.addEventListener("resize", () => updateActiveMarker(), { passive: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
