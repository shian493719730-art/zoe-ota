
"""可切换的联网搜索：ddg(免Key) / tavily / serper / brave / claude(用Claude自带联网搜索)。
所有结果带本地缓存，重复运行不重复消耗你的搜索额度。"""
from __future__ import annotations

# 兼容 macOS LibreSSL 环境下的 ddgs
def _patch_ddgs_ssl():
    try:
        import ddgs.http_client2 as h2_mod
        orig_fn = h2_mod._get_random_ssl_context
        def safe_ssl_context(verify):
            try:
                return orig_fn(verify)
            except ValueError:
                import ssl
                return ssl.create_default_context()
        h2_mod._get_random_ssl_context = safe_ssl_context
    except Exception:
        pass

_patch_ddgs_ssl()

import hashlib
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
import requests


class SearchError(Exception):
    pass


@dataclass
class Hit:
    title: str
    url: str
    snippet: str = ""
    date: str = ""
    query: str = ""
    provider: str = ""

    def to_dict(self):
        return asdict(self)


class FileCache:
    def __init__(self, root: Path, hours: float):
        self.root = Path(root)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.root = Path.home() / ".agibot" / ".cache" / "search"
            self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = hours * 3600

    def _p(self, key: str) -> Path:
        return self.root / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")

    def get(self, key: str):
        p = self._p(key)
        if p.exists() and time.time() - p.stat().st_mtime < self.ttl:
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def set(self, key: str, value):
        try:
            self._p(key).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def _with_sites(query: str, domains) -> str:
    if not domains:
        return query
    return f"{query} (" + " OR ".join(f"site:{d}" for d in domains[:5]) + ")"


def _span(days):
    """把天数映射为各家搜索的时间窗口代码。"""
    if not days:
        return None
    if days <= 1:
        return "d"
    if days <= 7:
        return "w"
    if days <= 31:
        return "m"
    return "y"


class Searcher:
    def __init__(self, cfg: dict, llm=None):
        s = cfg["search"]
        self.provider = s["provider"]
        self.key = s.get("api_key", "")
        self.n = int(s.get("results_per_query", 8))
        self.llm = llm
        root = Path(cfg.get("_data_root") or cfg["_root"]) / ".cache" / "search"
        self.cache = FileCache(root, float(cfg.get("limits", {}).get("cache_hours", 12)))
        self.calls = 0

    def search(self, query: str, domains=None, news: bool = False, since_days=None, max_results: int | None = None) -> list[Hit]:
        ck = json.dumps([self.provider, query, domains, news, since_days, self.n], ensure_ascii=False)
        cached = self.cache.get(ck)
        if cached is not None:
            res = [Hit(**h) for h in cached]
            return res[:max_results] if max_results else res
        fn = getattr(self, "_" + self.provider, None)
        if fn is None:
            raise SearchError(f"未知搜索 provider: {self.provider}")
        self.calls += 1
        hits = fn(query, domains, news, since_days)

        # 严格过滤域名限制，杜绝搜索引擎偶发返回的无关域或广告垃圾链接
        if domains:
            from urllib.parse import urlparse
            allowed = [d.lower().lstrip("www.") for d in domains]
            filtered = []
            for h in hits:
                try:
                    dom = urlparse(h.url).netloc.split(":")[0].lower().lstrip("www.")
                    if any(dom == a or dom.endswith("." + a) for a in allowed):
                        filtered.append(h)
                except Exception:
                    pass
            hits = filtered

        for h in hits:
            h.query = query
            h.provider = self.provider
        self.cache.set(ck, [h.to_dict() for h in hits])
        return hits[:max_results] if max_results else hits

    # ---------- 免 Key ----------
    def _ddg(self, query, domains, news, since_days):
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS  # 旧包名
        except ImportError:
            raise SearchError("未安装 ddgs：pip install ddgs")
        _patch_ddgs_ssl()
        q = _with_sites(query, domains)
        tl = _span(since_days)
        is_cn = any('一' <= ch <= '鿿' for ch in query) or any(str(d).endswith('.cn') for d in (domains or []))
        reg = "cn-zh" if is_cn else "wt-wt"
        try:
            with DDGS() as d:
                if news:
                    rows = list(d.news(q, max_results=self.n, timelimit=tl, region=reg)) if tl else list(d.news(q, max_results=self.n, region=reg))
                    return [Hit(r.get("title", ""), r.get("url", ""), r.get("body", ""), r.get("date", "")) for r in rows]
                rows = list(d.text(q, max_results=self.n, timelimit=tl, region=reg)) if tl else list(d.text(q, max_results=self.n, region=reg))
                return [Hit(r.get("title", ""), r.get("href", ""), r.get("body", "")) for r in rows]
        except Exception as e:
            raise SearchError(f"ddg 搜索失败（免费源易被限流，建议换 tavily/serper/brave）: {e}")

    # ---------- Tavily ----------
    def _tavily(self, query, domains, news, since_days):
        if not self.key:
            raise SearchError("缺少 SEARCH_API_KEY (tavily)")
        body = {"query": query, "max_results": self.n, "search_depth": "basic"}
        if domains:
            body["include_domains"] = domains
        if news:
            body["topic"] = "news"
        if since_days:
            body["days" if news else "time_range"] = since_days if news else {"d": "day", "w": "week", "m": "month", "y": "year"}[_span(since_days)]
        r = requests.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {self.key}"}, json=body, timeout=40)
        if r.status_code != 200:
            raise SearchError(f"tavily {r.status_code}: {r.text[:200]}")
        return [Hit(x.get("title", ""), x.get("url", ""), x.get("content", ""), x.get("published_date", "") or "")
                for x in r.json().get("results", [])]

    # ---------- Serper (Google) ----------
    def _serper(self, query, domains, news, since_days):
        if not self.key:
            raise SearchError("缺少 SEARCH_API_KEY (serper)")
        body = {"q": _with_sites(query, domains), "num": self.n}
        sp = _span(since_days)
        if sp:
            body["tbs"] = f"qdr:{sp}"
        ep = "news" if news else "search"
        r = requests.post(f"https://google.serper.dev/{ep}", headers={"X-API-KEY": self.key, "Content-Type": "application/json"}, json=body, timeout=40)
        if r.status_code != 200:
            raise SearchError(f"serper {r.status_code}: {r.text[:200]}")
        rows = r.json().get("news" if news else "organic", [])
        return [Hit(x.get("title", ""), x.get("link", ""), x.get("snippet", ""), x.get("date", "") or "") for x in rows]

    # ---------- Brave ----------
    def _brave(self, query, domains, news, since_days):
        if not self.key:
            raise SearchError("缺少 SEARCH_API_KEY (brave)")
        params = {"q": _with_sites(query, domains), "count": min(self.n, 20)}
        sp = _span(since_days)
        if sp:
            params["freshness"] = "p" + sp
        ep = "news/search" if news else "web/search"
        r = requests.get(f"https://api.search.brave.com/res/v1/{ep}", headers={"X-Subscription-Token": self.key, "Accept": "application/json"}, params=params, timeout=40)
        if r.status_code != 200:
            raise SearchError(f"brave {r.status_code}: {r.text[:200]}")
        d = r.json()
        rows = d.get("results", []) if news else d.get("web", {}).get("results", [])
        return [Hit(x.get("title", ""), x.get("url", ""), x.get("description", ""), x.get("age", "") or x.get("page_age", "") or "") for x in rows]

    # ---------- Claude 自带联网搜索（消耗你的 Claude 额度；需在 Anthropic 控制台启用 web search）----------
    def _claude(self, query, domains, news, since_days):
        if self.llm is None or self.llm.provider != "anthropic" or not self.llm.api_key:
            raise SearchError("search.provider=claude 需要 LLM_PROVIDER=anthropic 且已配置 Key")
        tool = {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
        if domains:
            tool["allowed_domains"] = domains
        body = {"model": self.llm.model, "max_tokens": 1200,
                "messages": [{"role": "user", "content": f"Search the web for: {query}\nReply with one short line only."}],
                "tools": [tool]}
        url = (self.llm.base_url or "https://api.anthropic.com") + "/v1/messages"
        r = requests.post(url, headers={"x-api-key": self.llm.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}, json=body, timeout=120)
        if r.status_code != 200:
            raise SearchError(f"claude 搜索失败 {r.status_code}: {r.text[:200]}")
        self.llm.calls += 1
        hits = []
        for b in r.json().get("content", []):
            if b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
                for x in b["content"]:
                    if x.get("type") == "web_search_result":
                        hits.append(Hit(x.get("title", ""), x.get("url", ""), "", x.get("page_age", "") or ""))
        return hits[: self.n]