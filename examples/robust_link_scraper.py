"""Robust link scraping with a Supervisor — handles JS-rendered sites.

The naive approach ("urllib.urlopen + regex for https?://") fails on two
extremely common cases:

  1. It only matches ABSOLUTE URLs, missing every relative link
     (href="/products", href="about.html").
  2. On a JavaScript-rendered site (React/Next/Vue SPA), the raw HTML is
     an almost-empty shell — the links are injected client-side, so
     there is nothing in the source to parse no matter how good the regex.
     rosyrec.com is exactly this: its homepage HTML has ZERO <a> tags,
     but rosyrec.com/sitemap.xml lists all 15 pages.

This example fixes both with:

  * SCRAPER_ADDENDUM — a system_addendum that teaches python_agent to
    parse relative + absolute links with html.parser AND to fall back to
    sitemap.xml when the HTML yields nothing (the JS-rendered case).
  * a Supervisor `subtask_success_check` that rejects an empty links.txt
    and retries with the reason fed back — so "0 links" doesn't get
    silently accepted as done.

`extract_links_reference` below is a standalone, dependency-free
implementation of the same strategy you can run directly to see it work.
"""

from __future__ import annotations

import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Set


# --------------------------------------------------------------------------
# Reference implementation (stdlib only) — proves the strategy works.
# --------------------------------------------------------------------------

class _AnchorParser(HTMLParser):
    """Collect every href from <a> tags — relative and absolute alike."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v)


def _fetch(url: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _links_from_html(base_url: str, html: str) -> Set[str]:
    """Parse anchors and resolve every href to an absolute URL."""
    parser = _AnchorParser()
    parser.feed(html)
    out: Set[str] = set()
    for href in parser.hrefs:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.add(urllib.parse.urljoin(base_url, href))
    return out


def _links_from_sitemap(base_url: str) -> Set[str]:
    """Fall back to <base>/sitemap.xml — works even for JS-rendered sites,
    since the sitemap is static XML the server hands out directly. Handles
    both a flat urlset and a sitemap index that points to child sitemaps."""
    sitemap_url = urllib.parse.urljoin(base_url, "/sitemap.xml")
    try:
        xml = _fetch(sitemap_url)
    except Exception:
        return set()
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return set()
    # Namespace-agnostic: match any tag ending in 'loc'.
    locs = {
        e.text.strip()
        for e in root.iter()
        if e.tag.endswith("}loc") or e.tag == "loc"
        if e.text and e.text.strip()
    }
    # Sitemap index → each <loc> is another sitemap; recurse one level.
    if root.tag.endswith("}sitemapindex") or root.tag == "sitemapindex":
        child_links: Set[str] = set()
        for child_sitemap in locs:
            try:
                child_xml = _fetch(child_sitemap)
                child_root = ET.fromstring(child_xml)
                child_links |= {
                    e.text.strip() for e in child_root.iter()
                    if (e.tag.endswith("}loc") or e.tag == "loc")
                    and e.text and e.text.strip()
                }
            except Exception:
                continue
        return child_links
    return locs


def extract_links_reference(url: str) -> List[str]:
    """Best-effort link extraction: try the HTML, fall back to the sitemap
    when the HTML is an empty (JS-rendered) shell. Returns sorted URLs."""
    links: Set[str] = set()
    try:
        html = _fetch(url)
        links = _links_from_html(url, html)
    except Exception:
        links = set()
    if not links:
        # HTML gave nothing — very likely a JS-rendered SPA. The sitemap
        # is the reliable fallback.
        links = _links_from_sitemap(url)
    return sorted(links)


# --------------------------------------------------------------------------
# The Supervisor wiring.
# --------------------------------------------------------------------------

SCRAPER_ADDENDUM = """WEB-SCRAPING RULES (follow these exactly when extracting links):

1. Parse links with html.parser, NOT a bare regex. Collect the href of
   every <a> tag, then resolve each to an absolute URL with
   urllib.parse.urljoin(base_url, href). This captures BOTH relative
   links (href="/products") and absolute ones (href="https://...").
   A regex like r'href="https?://...' silently drops every relative link.

2. If the parsed HTML yields ZERO links, DO NOT conclude "the site has no
   links." A near-empty HTML body with a JS bundle means the page is
   JavaScript-rendered (React/Next/Vue) — the links exist but are injected
   in the browser, so they are not in the raw HTML you fetched. urllib
   cannot run JavaScript.

3. In that case, FALL BACK to the sitemap: fetch <base>/sitemap.xml and
   read every <loc> entry (handle a <sitemapindex> by following each child
   sitemap one level). This is static XML the server returns directly, so
   it works even for JS-rendered sites. Most sites publish one.

4. Only after BOTH the HTML parse and the sitemap fallback come back empty
   should you report that no links were found — and say WHICH methods you
   tried and that the site appears to be JS-rendered.

5. Write the final absolute URLs (one per line, de-duplicated) to the
   requested output file, and report the COUNT and the first several URLs
   verbatim in your reply."""


def links_file_is_non_empty(result) -> "bool | str":
    """Supervisor success check: the scrape only counts as done if
    ./workspace/links.txt exists and has at least one link. Otherwise
    reject with a reason that nudges the sitemap fallback."""
    p = Path("./workspace/links.txt")
    if p.exists() and p.stat().st_size > 0 and p.read_text(encoding="utf-8").strip():
        return True
    return ("links.txt is empty or missing — no links were extracted. The "
            "site is probably JavaScript-rendered; fetch its sitemap.xml and "
            "extract the <loc> URLs instead of scraping the HTML.")


def build_supervisor():
    """Wire a Supervisor whose python_agent knows how to scrape robustly
    and whose success check refuses an empty result."""
    from agentx_dev import AgentRunner, AgentType, GPT, Permissions, Supervisor

    llm = GPT()  # or Claude()

    python_agent = AgentRunner(
        model=llm,
        agent=AgentType.ReAct,
        permissions=Permissions(
            read_files=True, write_files=True, execute_python=True,
            allowed_paths=["./workspace"], workspace="./workspace",
        ),
        max_iterations=15,
        system_addendum=SCRAPER_ADDENDUM,   # <-- the scraping know-how
    )

    return Supervisor(
        model=llm,
        agents={"python_agent": ("Python code execution + web scraping", python_agent)},
        max_subtasks=5,
        max_subtask_retries=2,                        # give the fallback a chance
        subtask_success_check=links_file_is_non_empty,  # reject empty results
        verbose=True,
    )


if __name__ == "__main__":
    # Standalone proof the strategy works — no API key needed.
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "https://rosyrec.com"
    found = extract_links_reference(target)
    print(f"{len(found)} links found for {target}:")
    for link in found:
        print(" ", link)

    # To run the full agent flow (needs OPENAI_API_KEY), uncomment:
    # sup = build_supervisor()
    # result = sup.run(
    #     f"Scrape {target} for ALL links and save them to "
    #     f"./workspace/links.txt. Make sure every link is pulled."
    # )
    # print(result.content)
