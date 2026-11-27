"""Keyless web search and page fetch. Stdlib only."""

from __future__ import annotations

import urllib.parse
import urllib.request
from html.parser import HTMLParser

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0"


def get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


_SKIP = ("script", "style", "noscript", "svg", "head", "template")
_BLOCK = ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "pre", "section", "article")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def to_text(page: str) -> str:
    import re

    parser = _Text()
    parser.feed(page)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n+", "\n\n", text)
    return text.strip()


def _uddg(href: str) -> str:
    """DuckDuckGo wraps result links in /l/?uddg=<encoded>."""
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        if "uddg" in q:
            return q["uddg"][0]
    return href


class _Results(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items: list[dict] = []
        self._kind: str | None = None
        self._href = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")
        if tag == "a" and "result__a" in cls:
            self._kind, self._href, self._buf = "title", a.get("href", ""), []
        elif tag in ("a", "div") and "result__snippet" in cls:
            self._kind, self._buf = "snippet", []

    def handle_data(self, data):
        if self._kind:
            self._buf.append(data)

    def handle_endtag(self, tag):
        # shortcut: a nested tag inside a snippet div ends it early, so a snippet can
        # be cut short. Upgrade: track tag depth if snippets prove too lossy.
        if self._kind == "title" and tag == "a":
            self.items.append(
                {"title": "".join(self._buf).strip(), "url": _uddg(self._href), "snippet": ""}
            )
            self._kind = None
        elif self._kind == "snippet" and tag in ("a", "div"):
            if self.items:
                self.items[-1]["snippet"] = "".join(self._buf).strip()
            self._kind = None


def search(query: str, max_results: int = 5) -> list[dict]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    parser = _Results()
    parser.feed(get(url))
    return parser.items[:max_results]
