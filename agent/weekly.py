"""闭环周报：把零散记录整理成“事项 × 阶段 × 方法 × 闭环凭证 × 下一步”，并自动带入上周遗留。

写得少的办法：你只需要丢进来本周的零散记录（甚至是微信式的碎句），
模型负责归并事项、判断阶段；记录里没写的“方法/凭证”它不会编，而是列成几个 10 秒能答完的小问题。
闭环检查（程序做，不靠模型）：标成“已闭环”却没有凭证 → 自动追问；上周计划里的事项本周没出现 → 列为遗留。
"""
from __future__ import annotations
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

from . import prompts
from .llm import LLM

STATUS_CN = {"done": "已闭环", "in_progress": "进行中", "blocked": "受阻", "new": "新启动"}


def default_week_label(today: date | None = None) -> str:
    t = today or date.today()
    mon = t - timedelta(days=t.weekday())
    fri = mon + timedelta(days=4)
    return f"{mon.month}.{mon.day}-{fri.month}.{fri.day}"


def _item_id(name: str) -> str:
    return "i" + hashlib.md5(name.strip().lower().encode("utf-8")).hexdigest()[:6]


def load_state(wdir: Path) -> dict:
    p = Path(wdir) / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": []}


def save_state(wdir: Path, state: dict) -> None:
    Path(wdir).mkdir(parents=True, exist_ok=True)
    (Path(wdir) / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def loop_checks(item: dict) -> list[str]:
    """程序化的闭环检查，返回需要追问的点。"""
    qs = []
    if item.get("status") == "done" and not item.get("evidence"):
        qs.append(f"「{item['name']}」标记为已闭环，但没有闭环凭证（验收单/审批单号/发布链接/汇款回单）——补一个？")
    nxt = item.get("next") or []
    if item.get("status") in ("in_progress", "blocked", "new") and not nxt:
        qs.append(f"「{item['name']}」还没闭环，但没有下一步动作。下一步是什么？")
    for a in nxt:
        if not a.get("done_when"):
            qs.append(f"「{item['name']}」下一步“{a.get('action','')[:20]}”怎样算完成？（一句话）")
            break
    if item.get("status") == "blocked" and not item.get("blockers"):
        qs.append(f"「{item['name']}」受阻，卡在哪里、需要谁的支持？")
    return qs


def run_weekly(notes: str, cfg: dict, llm: LLM, wdir: Path, label: str | None = None) -> dict:
    wdir = Path(wdir)
    label = label or default_week_label()
    stages = cfg.get("weekly", {}).get("stages") or ["持续运营"]
    state = load_state(wdir)
    open_items = [{"id": i["id"], "name": i["name"], "stage": i.get("stage"), "status": i.get("status"),
                   "next": i.get("next")} for i in state["items"] if i.get("status") != "done"]
    payload = {"notes": notes, "open_items": open_items, "stages": stages, "week": label}
    user = f"<<JSON>>{json.dumps(payload, ensure_ascii=False)}<<END>>"
    from .llm import LLMError
    try:
        res = llm.chat_json(prompts.WEEKLY_UPDATE, user, max_tokens=8000)
    except LLMError as err:
        if "JSON" not in str(err):
            raise
        # 若模型输入过长或未严格按 JSON 返回，优雅降级为直接输出高管级结构化周报 Markdown
        md_sys = (
            "你是欧洲市场部资深运营总监级周报润色与闭环助理。请直接输出一份结构清晰、结论先行的 Markdown 周报：\n"
            "1. 【风险卡点与需决策事项】前置置顶；\n"
            "2. 【本周核心成果与交付进展】逐条按「事项 / 核心进展 / 闭环凭证或待补项 / 下一步行动」专业润色；\n"
            "3. 严禁虚构数字或人名，忠于原文，去除空话套话。"
        )
        md_user = f"【本周周期：{label}】\n请对以下工作记录进行专业润色与闭环整理：\n\n{notes}"
        md = llm.chat(md_sys, md_user, max_tokens=6000)
        wdir.mkdir(parents=True, exist_ok=True)
        safe = label.replace("/", "-")
        (wdir / f"weekly_{safe}.md").write_text(md, encoding="utf-8")
        return {"label": label, "summary": {}, "items": [], "carry": [], "asks": [], "conflicts": [], "markdown": md}

    by_id = {i["id"]: i for i in state["items"]}
    touched = set()
    asks, conflicts = [], []
    items_out = []
    for it in res.get("items", []):
        if not isinstance(it, dict) or not it.get("name"):
            continue
        mid = it.get("match_id") if it.get("match_id") in by_id else None
        iid = mid or _item_id(it["name"])
        if iid in by_id and not mid:
            mid = iid
        old = by_id.get(iid, {})
        stage = it.get("stage") if it.get("stage") in stages else (old.get("stage") or stages[-1])
        merged = {
            "id": iid, "name": old.get("name") or it["name"], "stage": stage,
            "status": it.get("status") if it.get("status") in STATUS_CN else old.get("status", "in_progress"),
            "goal": it.get("goal") or old.get("goal"),
            "did": it.get("did") or [], "methods": it.get("methods") or [],
            "evidence": list(dict.fromkeys((old.get("evidence") or []) + (it.get("evidence") or []))),
            "blockers": it.get("blockers"), "next": it.get("next") or [], "reusable": it.get("reusable"),
        }
        hist = old.get("history", [])
        hist.append({"week": label, "status": merged["status"], "did": merged["did"]})
        merged["history"] = hist[-12:]
        by_id[iid] = merged
        touched.add(iid)
        items_out.append(merged)
        asks += [f"「{merged['name']}」{q}" for q in (it.get("ask_user") or [])]
        asks += loop_checks(merged)
        conflicts += [f"「{merged['name']}」{c}" for c in (it.get("conflicts") or [])]

    carry = [i for i in by_id.values() if i["id"] not in touched and i.get("status") != "done"]
    state["items"] = list(by_id.values())
    state["last_week"] = label
    save_state(wdir, state)

    out = {"label": label, "summary": res.get("summary") or {}, "items": items_out, "carry": carry,
           "asks": list(dict.fromkeys(asks)), "conflicts": conflicts}
    md = render_weekly(out)
    safe = label.replace("/", "-")
    (wdir / f"weekly_{safe}.md").write_text(md, encoding="utf-8")
    out["markdown"] = md
    return out


def render_weekly(o: dict) -> str:
    s = o["summary"]
    L = [
        f"# 周报｜{o['label']} · 高管决策闭环版",
        "> 💡 **高管汇报原则**：风险/卡点/缺凭证置顶前置暴露，战果与闭环进展后置展开。",
        "",
        "## 01 ⚠️ 风险卡点与决策排查（优先暴露问题）",
        ""
    ]

    # 1. 风险、卡点与决策前置
    risks = s.get("risks") or []
    decisions = s.get("decisions_needed") or []
    if risks:
        L.append("**🔴 当前阻塞卡点与风险预警 (需管理层协调支持)**")
        L.extend([f"- ❗ {x}" for x in risks])
        L.append("")
    if decisions:
        L.append("**⚡ 待管理层拍板决策项**")
        L.extend([f"- ❓ {x}" for x in decisions])
        L.append("")
    if o.get("asks"):
        L.append("**📋 待业务人员补齐闭环凭证 (10秒答复)**")
        L.extend([f"- [ ] {q}" for q in o["asks"]])
        L.append("")
    if o.get("carry"):
        L.append("**⏳ 上周遗留但本周未汇报事项 (需核实是否停滞)**")
        for it in o["carry"]:
            nxt = "；".join(a.get("action", "") for a in (it.get("next") or [])[:2]) or "无下一步记录"
            L.append(f"- ⚠️ {it['name']}（{it.get('stage')}｜{STATUS_CN.get(it.get('status'), '')}）：{nxt}")
        L.append("")

    # 2. 成果与进展后置
    L += [
        "---",
        "",
        "## 02 🌟 本周关键战果与已闭环成果（已确认战果）",
        ""
    ]
    top_results = s.get("top_results") or []
    if top_results:
        L.extend([f"- 🏆 **已达成**：{x}" for x in top_results])
    else:
        L.append("- 暂无新增已闭环关键战果")
    L.append("")

    if s.get("workflow_highlight"):
        L += ["**💡 工作流亮点与可复用方法**", f"- {s.get('workflow_highlight')}", ""]

    # 3. 事项看板
    L += [
        "---",
        "",
        "## 03 事项闭环看板与详情",
        "",
        "| 事项 | 阶段 | 状态 | 闭环凭证 | 剩余动作（完成标准） |",
        "|:---|:---:|:---:|:---|:---|"
    ]
    for it in o["items"]:
        ev = "；".join(it["evidence"][:2]) if it["evidence"] else ("—" if it["status"] != "done" else "⚠ 缺凭证")
        rest = "；".join(f"{a.get('action','')}" + (f"（完成标准：{a['done_when']}）" if a.get("done_when") else "") for a in it["next"][:2]) or "—"
        L.append(f"| **{it['name']}** | {it['stage']} | {STATUS_CN.get(it['status'], it['status'])} | {ev} | {rest} |")

    # 4. 详情
    L += ["", "### 事项推进明细与下一步责任人"]
    for it in o["items"]:
        L += ["", f"#### {it['name']}｜{it['stage']} · {STATUS_CN.get(it['status'], it['status'])}"]
        if it.get("goal"):
            L.append(f"- **目标**：{it['goal']}")
        L.append("- **本周进展**：" + ("；".join(it["did"]) if it["did"] else "—"))
        L.append("- **方法**：" + ("；".join(it["methods"]) if it["methods"] else "（待补）"))
        L.append("- **闭环凭证**：" + ("；".join(it["evidence"]) if it["evidence"] else "（待补）"))
        if it.get("blockers"):
            L.append(f"- ❗ **卡点/需支持**：{it['blockers']}")
        if it["next"]:
            L.append("- **下周计划**：")
            for a in it["next"]:
                meta = "｜".join(x for x in [a.get("owner"), a.get("due"), ("完成标准：" + a["done_when"]) if a.get("done_when") else None] if x)
                L.append(f"  - [ ] {a.get('action','')}" + (f"（{meta}）" if meta else ""))
        if it.get("reusable"):
            L.append(f"- **可沉淀资产**：{it['reusable']}")
    if o["conflicts"]:
        L += ["", "### ⚠️ 事实矛盾项待核对"] + [f"- {c}" for c in o["conflicts"]]

    return "\n".join(L) + "\n"
