"""网页正文抓取。社媒/登录墙站点一律不抓（遵守各平台条款，也抓不到），只用搜索摘要。"""
from __future__ import annotations
import re
from pathlib import Path
from urllib.parse import urlparse
import requests
from .search import FileCache

NO_FETCH = ("linkedin.com", "instagram.com", "facebook.com", "x.com", "twitter.com",
            "tiktok.com", "weibo.com", "xiaohongshu.com", "youtube.com", "youtu.be")
UA = "Mozilla/5.0 (compatible; EventAgent/0.1; +local-use)"


def domain_of(url: str) -> str:
    try:
        h = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def can_fetch(url: str) -> bool:
    d = domain_of(url)
    return bool(d) and not any(d == n or d.endswith("." + n) for n in NO_FETCH)


class Fetcher:
    def __init__(self, cfg: dict):
        root = Path(cfg["_root"]) / ".cache" / "pages"
        self.cache = FileCache(root, float(cfg.get("limits", {}).get("cache_hours", 12)))

    def fetch(self, url: str, max_chars: int = 9000) -> dict:
        """返回 {ok, status, title, text}。失败时 ok=False，调用方退回用搜索摘要。"""
        if not can_fetch(url):
            return {"ok": False, "status": "skipped", "title": "", "text": ""}
        c = self.cache.get(url)
        if c is not None:
            return c
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en,zh;q=0.8"}, timeout=20)
        except requests.RequestException as e:
            return {"ok": False, "status": f"error:{type(e).__name__}", "title": "", "text": ""}
        if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
            out = {"ok": False, "status": str(r.status_code), "title": "", "text": ""}
            self.cache.set(url, out)
            return out
        title, text = _extract(r.text)
        out = {"ok": bool(text.strip()), "status": "200", "title": title, "text": text[:max_chars]}
        self.cache.set(url, out)
        return out


def _extract(html: str):
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
    try:
        import trafilatura
        t = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
        if t:
            return title, t
    except ImportError:
        pass
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        return title, re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    except ImportError:
        return title, re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
