"""试运行模式：不调用任何接口，按任务返回示例结果，用来熟悉流程、测试界面。
结果是占位示例，不是真实分析。"""
from __future__ import annotations
import json
import re


def payload(user: str):
    """各模块把结构化输入放在 <<JSON>> ... <<END>> 之间。"""
    m = re.search(r"<<JSON>>\s*(.*?)\s*<<END>>", user, flags=re.S)
    return json.loads(m.group(1)) if m else {}


def mock_complete(system: str, user: str) -> str:
    m = re.search(r"\[\[task:(\w+)\]\]", system)
    task = m.group(1) if m else ""
    fn = _TASKS.get(task)
    if fn is None:
        return "（mock）"
    out = fn(payload(user))
    return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


def _article(p):
    url = p.get("url", "")
    dom = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    return {"relevant": True, "outlet": dom or "示例媒体", "published_date": p.get("date") or None,
            "country": "CN" if dom.endswith(".cn") or dom.endswith(".com.cn") else "PT",
            "language": "示例", "article_type": "news", "sentiment": "positive",
            "focus": "【试运行示例】报道了品牌在活动现场的亮相", "key_facts": ["【示例】这是试运行模式生成的占位内容"],
            "short_quote": None, "products_mentioned": ["A3"], "confidence": "low"}


def _social(p):
    out = []
    for it in p.get("items", []):
        out.append({"url": it["url"], "platform": it.get("platform_hint") or "other", "author": None, "date": None,
                    "summary": "【试运行示例】", "likes": None, "comments": None, "shares": None, "views": None, "relevant": True})
    return out


def _report(p):
    ev = p.get("event", {})
    name = ev.get("name", "示例活动")
    return {
        "basic": {"headline": f"{name}：【试运行示例】全球科技峰会，品牌首次亮相主舞台",
                  "summary": "这是试运行模式生成的占位内容。换成真实模型后，这里会根据你的文档和监测结果生成。",
                  "facts": [{"label": "日期", "value": f"{ev.get('start','')} — {ev.get('end','')}"},
                            {"label": "地点", "value": ev.get("venue", "")}, {"label": "展位", "value": ev.get("booth", "")}],
                  "kpis": [{"label": "参会者", "value": "70,000", "source": "示例"}, {"label": "国家与地区", "value": "164", "source": "示例"}]},
        "participation": {"headline": "展台与主舞台双线呈现", "summary": "【示例】",
                          "highlights": [{"title": "开场群舞", "detail": "【示例】9 台 A3 编队登台", "inferred": False},
                                         {"title": "主旨演讲", "detail": "【示例】机器人主讲", "inferred": False}],
                          "schedule": [{"date": ev.get("start", ""), "item": "开幕夜"}]},
        "exposure": {"headline": "公开检索到的媒体与社媒信号", "summary": "【示例】",
                     "kpis": [{"label": "公开检索到的报道", "value": "示例", "source": "监测"}],
                     "social_notes": ["【示例】"]},
        "review": {"problems": [{"title": "（示例）物料与音响准备", "detail": "高峰人流下需加强", "inferred": True}],
                   "next_steps": [{"title": "（示例）按高峰备展", "detail": "提前联调彩排", "inferred": True}]},
        "data_gaps": ["这是试运行占位内容，请切换到真实模型"],
    }


def _metrics(p):
    return {"event": p.get("event", "示例活动"),
            "metrics": {"media_countries": {"value": 10, "approx": True, "source": "示例", "evidence": "", "note": ""}},
            "highlights": ["【试运行示例】"]}


def _compare(p):
    return "【试运行示例】对比结论会在这里生成。可比得分由程序计算，结论文字由模型根据得分表撰写。"


def _competitors(p):
    return [{"competitor": c, "presence": "no_evidence", "evidence_urls": [], "what_shown": None,
             "scale_note": None, "confidence": "low"} for c in p.get("competitors", [])]


def _leads(p):
    segs = p.get("segments", ["个人/其他"])
    return [{"id": x["id"], "segment": segs[x["id"] % len(segs)], "intent": "unknown", "product_interest": [],
             "rationale": "试运行占位"} for x in p.get("leads", [])]


def _weekly(p):
    notes = [l.strip("-•* \t") for l in p.get("notes", "").splitlines() if l.strip()]
    first = notes[0] if notes else "示例事项"
    return {"summary": {"top_results": [first], "risks": [], "decisions_needed": [], "workflow_highlight": None},
            "items": [{"name": first[:20], "match_id": None, "stage": p.get("stages", ["持续运营"])[0], "status": "in_progress",
                       "goal": None, "did": [first], "methods": [], "evidence": [], "blockers": None,
                       "next": [{"action": "（试运行）继续推进", "owner": None, "due": None, "done_when": None}],
                       "reusable": None, "ask_user": ["这件事的闭环凭证是什么？（验收单/审批单号/发布链接）"], "conflicts": []}]}


_TASKS = {"article_extract": _article, "social_extract": _social, "report_sections": _report,
          "metrics_extract": _metrics, "compare_narrative": _compare, "competitor_extract": _competitors,
          "lead_classify": _leads, "weekly_update": _weekly}
