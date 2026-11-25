"""活动监测：围绕一场活动，自动检索媒体报道与社媒公开信号，抽取成结构化数据。

数据口径（重要）：
- 这里得到的“报道篇数/国家数”是【公开可检索到的下限】，不是全量；付费墙、搜索引擎未收录的会漏。
- LinkedIn / Instagram 的登录后数据抓不到，也不去抓。自家账号的真实曝光请把后台导出的数字
  填进 events/<活动>/social_manual.csv，Agent 会合并进报告。
"""
from __future__ import annotations
import csv
import json
import re
from collections import Counter
from datetime import datetime, date
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
import yaml

from . import prompts
from .search import Searcher, SearchError, Hit
from .web import Fetcher, domain_of, can_fetch
from .llm import LLM, LLMError, BudgetExceeded

TLD_COUNTRY = {"cn": "CN", "pt": "PT", "de": "DE", "fr": "FR", "it": "IT", "es": "ES", "uk": "GB",
               "nl": "NL", "pl": "PL", "be": "BE", "ch": "CH", "at": "AT", "ie": "IE", "se": "SE"}
SOCIAL_PLATFORMS = ("linkedin", "instagram", "youtube", "x", "facebook", "tiktok", "weibo")


# ---------------------------------------------------------------- 基础
def load_event(event_dir: Path) -> dict:
    p = Path(event_dir) / "event.yaml"
    if not p.exists():
        raise FileNotFoundError(f"缺少 {p}")
    ev = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    ev.setdefault("short", Path(event_dir).name)
    ev.setdefault("name", ev["short"])
    ev["_dir"] = str(event_dir)
    return ev


def norm_url(u: str) -> str:
    try:
        p = urlparse(u.strip())
        q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid", "igsh", "igshid"))]
        host = p.netloc.lower()
        host = host[4:] if host.startswith("www.") else host
        return urlunparse((p.scheme.lower() or "https", host, p.path.rstrip("/"), "", urlencode(q), ""))
    except Exception:
        return u.strip()


def classify_domain(domain: str, cfg: dict) -> dict:
    for e in cfg.get("media_registry", []):
        if domain == e["d"] or domain.endswith("." + e["d"]):
            return {"name": e["n"], "tier": e["t"], "country": e["c"], "known": True}
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    return {"name": domain, "tier": 3, "country": TLD_COUNTRY.get(tld), "known": False}


def platform_of(url: str, cfg: dict) -> str | None:
    d = domain_of(url)
    for plat, dom in cfg.get("social_domains", {}).items():
        if d == dom or d.endswith("." + dom):
            return plat
    if d == "youtu.be":
        return "youtube"
    return None


def brand_terms(cfg: dict) -> list[str]:
    return [b.lower() for b in cfg["brand"]["names"]]


def mentions_brand(text: str, cfg: dict) -> bool:
    t = text.lower()
    return any(b in t for b in brand_terms(cfg))


# ---------------------------------------------------------------- 查询计划
def plan_queries(ev: dict, cfg: dict, max_queries: int = 30) -> list[dict]:
    brands = cfg["brand"]["names"]
    primary = brands[0]
    cn_brand = next((b for b in brands if re.search(r"[\u4e00-\u9fff]", b)), None)
    kws = ev.get("keywords") or [ev["name"]]
    kws_cn = ev.get("keywords_cn") or []
    plan: list[dict] = []

    def add(q, group, kind="web", domains=None, platform=None, watch=None):
        plan.append({"q": q, "group": group, "kind": kind, "domains": domains, "platform": platform, "watch": watch})

    for kw in kws[:2]:
        add(f'"{primary}" "{kw}"', "core", "news")
    if cn_brand:
        add(f'"{cn_brand}" {(kws_cn or kws)[0]}', "core_cn", "news")
    for p in cfg["brand"].get("products", [])[:3]:
        add(f'"{primary}" {p} "{kws[0]}"', "product")
    for q in ev.get("extra_queries", []):
        add(q, "extra")
    for w in cfg.get("watchlist", []):
        is_cn = w.get("region") == "CN"
        brand = cn_brand if (is_cn and cn_brand) else primary
        kw = kws_cn[0] if (is_cn and kws_cn) else kws[0]
        add(f"{brand} {kw}", "watch", "web", [w["domain"]], watch=w["name"])
    loc = ev.get("local_media_domains", []) or []
    for i in range(0, len(loc), 5):
        add(f'"{primary}" "{kws[0]}"', "local", "web", loc[i:i + 5])
    for plat in ("linkedin", "instagram", "youtube", "x"):
        dom = cfg.get("social_domains", {}).get(plat)
        if dom:
            add(f'"{primary}" "{kws[0]}"', "social", "web", [dom], plat)
    return plan[:max_queries]


# ---------------------------------------------------------------- YouTube / 自有数据
def youtube_ids(urls) -> list[str]:
    ids = []
    for u in urls:
        m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([A-Za-z0-9_-]{11})", u)
        if m:
            ids.append(m.group(1))
    return list(dict.fromkeys(ids))


def youtube_stats(ids: list[str], key: str) -> dict:
    out = {}
    for i in range(0, len(ids), 50):
        r = requests.get("https://www.googleapis.com/youtube/v3/videos",
                         params={"part": "snippet,statistics", "id": ",".join(ids[i:i + 50]), "key": key}, timeout=30)
        if r.status_code != 200:
            raise SearchError(f"YouTube API {r.status_code}: {r.text[:150]}")
        for it in r.json().get("items", []):
            st = it.get("statistics", {})
            out[it["id"]] = {"title": it["snippet"].get("title", ""), "channel": it["snippet"].get("channelTitle", ""),
                             "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)) if "likeCount" in st else None,
                             "comments": int(st.get("commentCount", 0)) if "commentCount" in st else None}
    return out


def load_owned_social(event_dir: Path) -> dict:
    """读取你手工填写的自有账号数据（来自 LinkedIn/Instagram 后台导出）。
    列: platform,url,impressions,likes,comments,shares,views,followers_gain,note"""
    p = Path(event_dir) / "social_manual.csv"
    if not p.exists():
        return {}
    rows = []
    with open(p, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if not (r.get("platform") or "").strip() or (r.get("platform") or "").startswith("#"):
                continue
            rows.append(r)

    def num(v):
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return None
    by = {}
    for r in rows:
        pl = r["platform"].strip().lower()
        d = by.setdefault(pl, {"posts": 0, "impressions": 0, "likes": 0, "comments": 0, "shares": 0, "views": 0, "followers_gain": 0})
        d["posts"] += 1 if (r.get("url") or "").strip() else 0
        for k in ("impressions", "likes", "comments", "shares", "views", "followers_gain"):
            v = num(r.get(k))
            if v is not None:
                d[k] += v
    total = {k: sum(d[k] for d in by.values()) for k in ("posts", "impressions", "likes", "comments", "shares", "views", "followers_gain")}
    return {"by_platform": by, "total": total, "rows": len(rows)}


# ---------------------------------------------------------------- 主流程
def run_monitor(event_dir: Path, cfg: dict, llm: LLM, searcher: Searcher, fetcher: Fetcher | None = None,
                max_queries: int = 30, max_articles: int = 40, log=print) -> dict:
    event_dir = Path(event_dir)
    ev = load_event(event_dir)
    fetcher = fetcher or Fetcher(cfg)
    out_dir = event_dir / "outputs"
    out_dir.mkdir(exist_ok=True)
    notes: list[str] = []

    # 时间窗：活动开始前 14 天 → 今天
    since_days = None
    if ev.get("start"):
        try:
            s = ev["start"] if isinstance(ev["start"], date) else datetime.strptime(str(ev["start"]), "%Y-%m-%d").date()
            since_days = max(7, (date.today() - s).days + 14)
        except Exception:
            pass

    plan = plan_queries(ev, cfg, max_queries)
    log(f"检索计划：{len(plan)} 条查询")
    pool: dict[str, dict] = {}
    consec_fail = 0
    for item in plan:
        try:
            hits = searcher.search(item["q"], item["domains"], item["kind"] == "news", since_days)
            consec_fail = 0
        except SearchError as e:
            notes.append(f"搜索失败「{item['q'][:40]}」：{e}")
            log(f"  ⚠ {e}")
            consec_fail += 1
            if consec_fail >= 2:
                notes.append("外网检索当前网络受限（连续失败已自动熔断跳过），仅保留已配置/已有信息")
                break
            continue
        item["found"] = len(hits)
        for h in hits:
            if not h.url:
                continue
            k = norm_url(h.url)
            rec = pool.setdefault(k, {"hit": h, "groups": set(), "platform_hint": item.get("platform")})
            rec["groups"].add(item["group"])
    for u in ev.get("known_links", []) or []:
        k = norm_url(u)
        pool.setdefault(k, {"hit": Hit("", u, ""), "groups": {"known"}, "platform_hint": None})
    log(f"去重后候选链接：{len(pool)}")

    media_recs, social_recs = [], []
    for k, rec in pool.items():
        plat = platform_of(rec["hit"].url, cfg)
        (social_recs if plat else media_recs).append((k, rec, plat))

    # 媒体：重点级别优先，再按有没有日期
    def rank(r):
        info = classify_domain(domain_of(r[1]["hit"].url), cfg)
        return (info["tier"], 0 if r[1]["hit"].date else 1)
    media_recs.sort(key=rank)

    articles = []
    fetch_fail = 0
    for k, rec, _ in media_recs:
        if len(articles) >= max_articles:
            break
        h: Hit = rec["hit"]
        dom = domain_of(h.url)
        info = classify_domain(dom, cfg)
        page = fetcher.fetch(h.url) if can_fetch(h.url) else {"ok": False, "text": "", "title": ""}
        text = page["text"] if page["ok"] else ""
        if not page["ok"]:
            fetch_fail += 1
        blob = f"{h.title} {h.snippet} {text}"
        if "known" not in rec["groups"] and not mentions_brand(blob, cfg):
            continue  # 页面里压根没提到品牌 → 不浪费大模型额度
        user = (f"活动：{ev['name']}（{ev.get('start','')} ~ {ev.get('end','')}）\n品牌关键词：{', '.join(cfg['brand']['names'])}\n"
                f"<<JSON>>{json.dumps({'url': h.url, 'title': h.title or page.get('title',''), 'date': h.date, 'text': (text or h.snippet)[:5000], 'fetched': page['ok']}, ensure_ascii=False)}<<END>>")
        try:
            x = llm.chat_json(prompts.ARTICLE_EXTRACT, user, max_tokens=900)
        except BudgetExceeded as e:
            notes.append(str(e))
            break
        except LLMError as e:
            notes.append(f"抽取失败 {h.url[:60]}: {e}")
            continue
        if not isinstance(x, dict) or not x.get("relevant"):
            continue
        country = info["country"] or x.get("country")
        articles.append({
            "url": h.url, "title": h.title or page.get("title", ""),
            "outlet": info["name"] if info["known"] else (x.get("outlet") or info["name"]),
            "tier": info["tier"], "country": country, "region": "CN" if country == "CN" else "overseas",
            "date": x.get("published_date") or (h.date[:10] if h.date and re.match(r"\d{4}-\d{2}-\d{2}", h.date) else None),
            "type": x.get("article_type"), "sentiment": x.get("sentiment"), "focus": x.get("focus"),
            "key_facts": x.get("key_facts") or [], "short_quote": x.get("short_quote"),
            "products": x.get("products_mentioned") or [], "confidence": x.get("confidence"),
            "fetched": page["ok"], "groups": sorted(rec["groups"]),
        })
    if fetch_fail:
        notes.append(f"{fetch_fail} 个页面无法抓取正文（付费墙/反爬），这些只能凭搜索摘要判断，可信度较低")

    # 社媒：只用搜索摘要，批量抽取
    social = []
    batch = []
    for k, rec, plat in social_recs:
        h = rec["hit"]
        if not mentions_brand(f"{h.title} {h.snippet} {h.url}", cfg) and "known" not in rec["groups"]:
            continue
        batch.append({"url": h.url, "title": h.title, "snippet": h.snippet, "platform_hint": plat})
    for i in range(0, len(batch), 12):
        part = batch[i:i + 12]
        user = f"<<JSON>>{json.dumps({'items': part}, ensure_ascii=False)}<<END>>"
        try:
            rows = llm.chat_json(prompts.SOCIAL_EXTRACT, user, max_tokens=1500)
        except (LLMError, BudgetExceeded) as e:
            notes.append(f"社媒抽取失败：{e}")
            break
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and r.get("relevant", True) and r.get("url"):
                    r["source"] = "public_snippet"
                    social.append(r)

    # YouTube 官方 API（可选）
    yt = {}
    key = cfg.get("youtube_api_key")
    ids = youtube_ids([s["url"] for s in social if s.get("platform") == "youtube"] + list(ev.get("known_links", []) or [])) + list(ev.get("youtube_ids", []) or [])
    if key and ids:
        try:
            yt = youtube_stats(list(dict.fromkeys(ids)), key)
        except Exception as e:
            notes.append(f"YouTube 数据读取失败：{e}")
    elif ids:
        notes.append("发现 YouTube 视频但未配置 YOUTUBE_API_KEY，播放量未读取")

    owned = load_owned_social(event_dir)
    watch = watchlist_status(articles, cfg)
    stats = summarize(articles, social, yt, owned)

    prev_p = out_dir / "monitor_latest.json"
    prev_urls = set()
    if prev_p.exists():
        try:
            old = json.loads(prev_p.read_text(encoding="utf-8"))
            prev_urls = {norm_url(a["url"]) for a in old.get("articles", [])} | {norm_url(s["url"]) for s in old.get("social", [])}
        except Exception:
            pass
    new_urls = [a["url"] for a in articles if norm_url(a["url"]) not in prev_urls] + \
               [s["url"] for s in social if norm_url(s["url"]) not in prev_urls] if prev_urls else []

    result = {"event": {"name": ev["name"], "short": ev["short"], "start": str(ev.get("start", "")), "end": str(ev.get("end", ""))},
              "run_at": datetime.now().isoformat(timespec="seconds"), "queries": plan,
              "articles": articles, "social": social, "youtube": yt, "owned_social": owned,
              "watchlist": watch, "stats": stats, "new_since_last": new_urls, "notes": notes,
              "usage": {"llm_calls": llm.calls, "search_calls": searcher.calls}}
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    (out_dir / f"monitor_{stamp}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    prev_p.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "monitor_latest.md").write_text(render_monitor_md(result), encoding="utf-8")
    return result


# ---------------------------------------------------------------- 汇总
def watchlist_status(articles: list[dict], cfg: dict) -> list[dict]:
    out = []
    for w in cfg.get("watchlist", []):
        hits = [a for a in articles if domain_of(a["url"]) == w["domain"] or domain_of(a["url"]).endswith("." + w["domain"])]
        out.append({"name": w["name"], "domain": w["domain"], "region": w.get("region"),
                    "status": "hit" if hits else "not_found",
                    "hits": [{"title": a["title"], "url": a["url"], "date": a["date"]} for a in hits[:3]]})
    return out


def summarize(articles, social, yt, owned) -> dict:
    countries = sorted({a["country"] for a in articles if a.get("country")})
    overseas = [a for a in articles if a["region"] == "overseas"]
    domestic = [a for a in articles if a["region"] == "CN"]
    st = {
        "articles_total": len(articles),
        "articles_overseas": len(overseas), "articles_domestic": len(domestic),
        "countries": countries, "countries_count": len(countries),
        "overseas_countries_count": len({a["country"] for a in overseas if a.get("country")}),
        "tier1_count": sum(1 for a in articles if a["tier"] == 1),
        "sentiment": dict(Counter(a.get("sentiment") or "unknown" for a in articles)),
        "social_public_items": len(social),
        "social_by_platform": dict(Counter(s.get("platform") or "other" for s in social)),
        "youtube_views_total": sum(v["views"] for v in yt.values()) if yt else None,
        "owned_impressions": (owned.get("total") or {}).get("impressions") if owned else None,
        "owned_followers_gain": (owned.get("total") or {}).get("followers_gain") if owned else None,
    }
    return st


def render_monitor_md(r: dict) -> str:
    ev, st = r["event"], r["stats"]
    L = [f"# 监测结果｜{ev['name']}", f"运行时间：{r['run_at']}　调用：大模型 {r['usage']['llm_calls']} 次 / 搜索 {r['usage']['search_calls']} 次", ""]
    L.append("> 口径：以下为公开可检索到的**下限**，不是全量。自有账号真实曝光请填 social_manual.csv。")
    L.append("")
    L += ["## 概览",
          f"- 相关报道：**{st['articles_total']}** 篇（海外 {st['articles_overseas']} / 国内 {st['articles_domestic']}），覆盖 **{st['countries_count']}** 个国家/地区：{', '.join(st['countries']) or '—'}",
          f"- 重点媒体（Tier-1）：{st['tier1_count']} 篇　情绪分布：{st['sentiment']}",
          f"- 社媒公开信号：{st['social_public_items']} 条　{st['social_by_platform']}"]
    if st.get("youtube_views_total") is not None:
        L.append(f"- YouTube 已读取视频播放量合计：{st['youtube_views_total']:,}")
    if st.get("owned_impressions") is not None:
        L.append(f"- 自有账号（手工录入）：曝光 {int(st['owned_impressions']):,}，粉丝净增 {int(st['owned_followers_gain'] or 0):,}")
    L += ["", "## 重点媒体命中情况", "| 媒体 | 状态 | 链接 |", "|---|---|---|"]
    for w in r["watchlist"]:
        if w["status"] == "hit":
            h = w["hits"][0]
            L.append(f"| {w['name']} | ✅ 命中 | [{(h['title'] or h['url'])[:50]}]({h['url']}) {h['date'] or ''} |")
        else:
            L.append(f"| {w['name']} | ❔ 未检索到 | 可能被付费墙/搜索引擎未收录，建议人工确认 |")
    L += ["", "## 报道清单", "| 日期 | 媒体 | 国家 | 级别 | 焦点 | 链接 |", "|---|---|---|---|---|---|"]
    for a in sorted(r["articles"], key=lambda x: (x["tier"], x["date"] or "9")):
        L.append(f"| {a['date'] or ''} | {a['outlet']} | {a['country'] or ''} | T{a['tier']} | {a['focus'] or ''} | [链接]({a['url']}) |")
    if r["social"]:
        L += ["", "## 社媒公开信号", "| 平台 | 账号 | 内容 | 赞 | 评论 | 播放 | 链接 |", "|---|---|---|---|---|---|---|"]
        for s in r["social"]:
            L.append(f"| {s.get('platform','')} | {s.get('author') or ''} | {s.get('summary') or ''} | {s.get('likes') or ''} | {s.get('comments') or ''} | {s.get('views') or ''} | [链接]({s['url']}) |")
    if r["new_since_last"]:
        L += ["", f"## 自上次运行新增（{len(r['new_since_last'])}）"] + [f"- {u}" for u in r["new_since_last"]]
    if r["notes"]:
        L += ["", "## 运行提示"] + [f"- {n}" for n in r["notes"]]
    return "\n".join(L) + "\n"
