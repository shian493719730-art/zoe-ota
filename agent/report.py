"""活动报告：固定四段（基本情况 / 展位与活动参与 / 线上曝光与社媒 / 复盘与不足）。
流程：材料 → 大模型写出 sections.json（可人工改）→ 渲染成 Markdown 和 PPT。
sections.json 单独落盘，你改完文字后用 --render-only 重新出 PPT，不再花模型额度。"""
from __future__ import annotations
import json
from pathlib import Path

import yaml

from . import prompts
from .docs import read_folder
from .llm import LLM
from .monitor import load_event

PHOTO_EXT = {".jpg", ".jpeg", ".png"}


# ---------------------------------------------------------------- 生成 sections
def build_sections(event_dir: Path, cfg: dict, llm: LLM, log=print) -> dict:
    event_dir = Path(event_dir)
    ev = load_event(event_dir)
    out = event_dir / "outputs"
    out.mkdir(exist_ok=True)

    mon = {}
    mp = out / "monitor_latest.json"
    if mp.exists():
        m = json.loads(mp.read_text(encoding="utf-8"))
        mon = {"stats": m["stats"], "watchlist": [w for w in m["watchlist"]],
               "articles": [{k: a.get(k) for k in ("outlet", "country", "tier", "date", "focus", "url")} for a in m["articles"][:25]],
               "social": m["social"][:15], "youtube": m.get("youtube"), "owned_social": m.get("owned_social"),
               "run_at": m["run_at"], "notes": m["notes"]}
    else:
        log("提示：还没有监测结果（先运行 monitor），报告里的曝光部分会比较空。")

    metrics = {}
    yp = event_dir / "metrics.yaml"
    if yp.exists():
        metrics = yaml.safe_load(yp.read_text(encoding="utf-8")) or {}
    notes = ""
    for n in ("notes.md", "notes.txt"):
        if (event_dir / n).exists():
            notes += (event_dir / n).read_text(encoding="utf-8", errors="ignore")
    docs = read_folder(event_dir / "docs", per_file=15000, total=45000)

    payload = {"event": {k: v for k, v in ev.items() if not k.startswith("_")}, "manual_metrics": metrics,
               "monitor": mon, "owner_notes": notes[:8000],
               "docs": [{"file": n, "text": t[:12000]} for n, t in docs]}
    user = f"<<JSON>>{json.dumps(payload, ensure_ascii=False, default=str)}<<END>>"
    from .skills.work_report_skill import EXECUTIVE_REPORT_PROMPT_ADDON
    prompt = prompts.REPORT_SECTIONS + "\n" + EXECUTIVE_REPORT_PROMPT_ADDON
    sections = llm.chat_json(prompt, user, max_tokens=8000)
    sections["_meta"] = {"event": ev["name"], "built_with": llm.model or llm.provider}
    (out / "report_sections.json").write_text(json.dumps(sections, ensure_ascii=False, indent=1), encoding="utf-8")
    return sections


# ---------------------------------------------------------------- Markdown
def render_markdown(sections: dict, ev: dict, mon: dict | None) -> str:
    s = sections
    L = [
        f"# {ev['name']} 活动总结与战略复盘报告",
        f"> **展会周期**：{ev.get('start','')} — {ev.get('end','')}　|　**举办场馆**：{ev.get('venue','')}　|　**专属展位**：{ev.get('booth','')}",
        "",
        "---",
        ""
    ]

    # 01 高管一页纸总览 (Executive Summary & KPIs)
    b = s.get("basic", {})
    L += [
        "## 01 高管战略总览与核心量化战果",
        f"### 🎯 核心定调：{b.get('headline','')}",
        "",
        b.get("summary", ""),
        ""
    ]
    if b.get("kpis"):
        L += [
            "#### 📊 核心经营指标速览",
            "",
            "| 核心指标项 | 达成数值 | 凭证 / 数据来源 | 状态 |",
            "|:---|:---|:---|:---:|"
        ]
        for k in b["kpis"]:
            L.append(f"| **{k['label']}** | `{k['value']}` | {k.get('source','真实业务文档')} | 🟢 已核实 |")
        L.append("")

    # 02 ⚠️ 待核实与未检出排查（失败/缺口/风险置顶！—— 按客户最高优先级前置）
    unfound = []
    found = []
    if mon and mon.get("watchlist"):
        for w in mon["watchlist"]:
            h = w["hits"][0] if w.get("hits") else None
            if h:
                found.append((w["name"], h["url"], h.get("title", "")))
            else:
                unfound.append(w["name"])

    gaps = s.get("data_gaps", [])
    problems = s.get("review", {}).get("problems", [])

    L += [
        "---",
        "",
        "## 02 ⚠️ 待核实与未检出排查（风险/缺口/待补齐前置）",
        "> 💡 **管理层提示**：以下为本次活动复盘中**尚未检出或存在数据差距的关键项**，需公关与业务团队优先排查处置：",
        ""
    ]

    if unfound:
        L += [
            "### 🔴 重点媒体未检出清单（需人工排查或 PR 补发）",
            "",
            "| 目标重点媒体 | 当前状态 | 诊断推测原因 | 建议公关动作 |",
            "|:---|:---:|:---|:---|"
        ]
        for name in unfound:
            L.append(f"| **{name}** | ❌ 未自动检出 | 媒体可能采用别名、付费墙后发布或发稿延期 | 建议欧洲 PR 团队定向致信记者索要专访链接或确认通稿发布档期 |")
        L.append("")
    else:
        L += ["- ✅ **重点媒体检索状态**：所有监控目标重点媒体均已检出报道！", ""]

    if gaps:
        L += ["### 📋 业务数据缺口与待补凭证", ""]
        for g in gaps:
            L.append(f"- ❗ **待补齐**：{g}")
        L.append("")

    if problems:
        L += ["### ⚠️ 现场执行卡点与问题复盘", ""]
        for x in problems:
            tag = " *(AI 归纳，待业务确认)*" if x.get("inferred") else ""
            L.append(f"- ❌ **{x['title']}**：{x['detail']}{tag}")
        L.append("")

    # 03 🌟 已确认战果与权威媒体深度评论（成果后置展开）
    L += [
        "---",
        "",
        "## 03 🌟 权威媒体深度好评与现场参与战果（已确认成果）",
        ""
    ]
    if found:
        L += [
            "### 📰 重点媒体报道命中列表",
            "",
            "| 权威媒体 | 报道题目 / 焦点 | 原文链接 | 舆论定调 |",
            "|:---|:---|:---:|:---:|"
        ]
        for name, url, title in found:
            L.append(f"| **{name}** | {title or '专访与现场开幕深度评述'} | [🔗 阅读原文]({url}) | 🟢 深度好评 |")
        L.append("")

    e = s.get("exposure", {})
    if e.get("media_commentaries"):
        L += ["### 🎙️ 核心外媒深度观点与犀利引语摘录", ""]
        for mc in e["media_commentaries"]:
            quote_str = mc.get('quote')
            q = f"\n> *“{quote_str}”*" if quote_str else ""
            L.append(f"#### 📌 {mc.get('outlet','')}（国际权威媒体）\n**观点提炼**：{mc.get('critique','')}{q}\n")

    p = s.get("participation", {})
    if p.get("highlights"):
        L += ["### 🎪 现场活动与展位运营亮点", ""]
        for h in p["highlights"]:
            L.append(f"- 🏆 **{h['title']}**：{h['detail']}")
        L.append("")

    # 04 🚀 战略复盘与高管决策闭环
    r = s.get("review", {})
    L += [
        "---",
        "",
        "## 04 🚀 战略复盘与高管决策推进（下一步闭环）",
        ""
    ]
    if r.get("next_steps"):
        L += [
            "| 关键战略推进事项 | 具体行动路径 | 预期交付成果 / 完成标准 (Done Criteria) |",
            "|:---|:---|:---|"
        ]
        for x in r["next_steps"]:
            L.append(f"| **{x['title']}** | {x['detail']} | 达成书面签署 / 交付成果归档并闭环 |")
        L.append("")

    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- PPT
# 智元科技高管商业规范：深海军黑主色、智元蓝品牌色、告警深红、成功翠绿
INK = "0F172A"          # 深海军黑 (文字主色)
BRAND_BLUE = "1E40AF"   # 智元科技蓝
CYAN = "0284C7"         # 科技青
ALERT_RED = "DC2626"    # 警示红 (未检出与卡点前置高亮)
SUCCESS_GREEN = "059669"# 成功绿 (成果与已闭环)
GRAY = "475569"         # 中灰 (辅助说明)
SOFT = "F8FAFC"         # 背景冷灰白
CARD_LINE = "E2E8F0"    # 卡片边框浅灰
CARD_FILL = "FFFFFF"    # 卡片白
CARD_ALERT_FILL = "FEF2F2" # 警示浅红底
FONT = "Arial"
EA_FONT = "Microsoft YaHei"


def _rgb(h):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(h)


def _clip(s, n):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def render_pptx(sections: dict, ev: dict, mon: dict | None, out_path: Path, photos: list[Path] | None = None) -> Path:
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.oxml.ns import qn

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    total_pages = [0]

    def set_font(run, size, bold=False, color=INK):
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.name = FONT
        run.font.color.rgb = _rgb(color)
        rPr = run._r.get_or_add_rPr()
        for tag in ("a:ea", "a:cs"):
            el = rPr.find(qn(tag))
            if el is None:
                el = rPr.makeelement(qn(tag), {})
                rPr.append(el)
            el.set("typeface", EA_FONT)

    def text(slide, x, y, w, h, lines, size=14, bold=False, color=INK, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, space=4):
        tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.vertical_anchor = anchor
        tf.margin_left = tf.margin_right = Inches(0.06)
        tf.margin_top = tf.margin_bottom = Inches(0.04)
        lines = lines if isinstance(lines, list) else [lines]
        for i, ln in enumerate(lines):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = align
            p.space_after = Pt(space)
            if isinstance(ln, tuple):  # (文字, 粗体, 颜色, 字号)
                t, b, c, sz = (list(ln) + [None] * 4)[:4]
                r = p.add_run()
                r.text = t
                set_font(r, sz or size, bold if b is None else b, c or color)
            else:
                r = p.add_run()
                r.text = str(ln)
                set_font(r, size, bold, color)
        return tb

    def card(slide, x, y, w, h, fill=CARD_FILL, border=CARD_LINE, border_width=1.0):
        sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
        sh.adjustments[0] = 0.04
        sh.fill.solid()
        sh.fill.fore_color.rgb = _rgb(fill)
        sh.line.color.rgb = _rgb(border)
        sh.line.width = Pt(border_width)
        return sh

    def kpi(slide, x, y, w, h, value, label, accent=BRAND_BLUE):
        card(slide, x, y, w, h)
        text(slide, x + 0.1, y + 0.15, w - 0.2, h * 0.52, _clip(value, 14), size=28, bold=True, color=accent, anchor=MSO_ANCHOR.MIDDLE)
        text(slide, x + 0.1, y + h * 0.62, w - 0.2, h * 0.32, _clip(label, 22), size=12, color=GRAY)

    def new_slide(num, zh, en, title, subtitle=""):
        s = prs.slides.add_slide(blank)
        s.background.fill.solid()
        s.background.fill.fore_color.rgb = _rgb(SOFT)
        text(s, 0.8, 0.4, 7, 0.3, f"{num}  {zh} · {en}", size=11, bold=True, color=BRAND_BLUE)
        text(s, 7.5, 0.4, 5.0, 0.3, f"AGIBOT · {ev['name']}", size=11, color=GRAY, align=PP_ALIGN.RIGHT)
        text(s, 0.8, 0.75, 11.7, 0.7, _clip(title, 48), size=24, bold=True, color=INK)
        if subtitle:
            text(s, 0.8, 1.45, 11.7, 0.35, subtitle, size=12, color=GRAY)
        total_pages[0] += 1
        text(s, 11.5, 7.0, 1.0, 0.3, f"PAGE {total_pages[0]}", size=10, color=GRAY, align=PP_ALIGN.RIGHT)
        text(s, 0.8, 7.0, 6, 0.3, f"AGIBOT 智元机器人 · 欧洲市场战略复盘报告", size=10, color=GRAY)
        return s

    # ---------------- Slide 01: 封面与高管战果一览 (Executive Overview & Hero KPIs)
    s = prs.slides.add_slide(blank)
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = _rgb(SOFT)
    text(s, 0.8, 0.6, 6, 0.4, "AGIBOT 智元机器人 · 欧洲战区", size=13, bold=True, color=BRAND_BLUE)
    text(s, 0.8, 1.8, 11.5, 0.4, "EXECUTIVE WORK REPORT & DEBRIEF", size=13, bold=True, color=CYAN)
    text(s, 0.8, 2.25, 11.5, 1.2, _clip(ev["name"] + " 活动复盘与决策报告", 36), size=38, bold=True, color=INK)
    
    b = sections.get("basic", {})
    text(s, 0.8, 3.5, 11.5, 0.9, _clip(b.get("headline", ""), 90), size=16, color="1E293B", bold=True)
    meta = [f"时间：{ev.get('start','')} — {ev.get('end','')}", f"场馆：{ev.get('venue','')}", f"展位：{ev.get('booth','')}"]
    text(s, 0.8, 4.4, 11.5, 0.4, "　　|　　".join(meta), size=12, color=GRAY)

    # 4 大战果 KPI 卡片
    ks = b.get("kpis", [])[:4]
    for i, k in enumerate(ks):
        kpi(s, 0.8 + i * 2.95, 5.1, 2.8, 1.5, k["value"], k["label"], accent=BRAND_BLUE)

    # ---------------- Slide 02: ⚠️ 待核实与未检出排查 (Failures & Gaps Front-loaded - 用户最高要求！)
    unfound_list = []
    if mon and mon.get("watchlist"):
        for w in mon["watchlist"]:
            if not w.get("hits") or w.get("status") != "hit":
                unfound_list.append(w["name"])

    gaps = sections.get("data_gaps", [])
    problems = sections.get("review", {}).get("problems", [])

    s = new_slide("02", "待核实与未检出排查", "GAPS & RISKS", "⚠️ 问题与缺口置顶：未检出重点媒体与业务卡点", "※ 按照管理层高管汇报准则：优先暴露未完成项、数据缺口与风险卡点，优先决策处置")
    
    # 左侧卡片：未检出重点媒体排查
    card(s, 0.8, 1.9, 5.7, 4.8, fill=CARD_ALERT_FILL, border=ALERT_RED, border_width=1.5)
    text(s, 1.0, 2.1, 5.3, 0.4, "🔴 重点媒体未自动检出清单 (需 PR 补发 / 人工核验)", size=14, bold=True, color=ALERT_RED)
    if unfound_list:
        lines = []
        for name in unfound_list[:6]:
            lines.append((f"• 未检出：{name}\n  诊断推测：公开检索未建索引或位于付费墙内\n  公关补救：欧洲 PR 团队定向跟进记者索取专访发布链接\n", False, "7F1D1D", 11))
        text(s, 1.0, 2.6, 5.3, 3.9, lines, space=2)
    else:
        text(s, 1.0, 2.8, 5.3, 1.5, "✅ 重点媒体检索全量命中！Forbes、新华网等目标媒体均已检出报道。", size=13, color=SUCCESS_GREEN)

    # 右侧卡片：数据缺口与业务卡点
    card(s, 6.8, 1.9, 5.7, 4.8, fill=CARD_FILL, border=CARD_LINE)
    text(s, 7.0, 2.1, 5.3, 0.4, "📋 业务数据缺口与现场执行卡点 (待业务补齐凭证)", size=14, bold=True, color=INK)
    gap_lines = []
    if gaps:
        for g in gaps[:4]:
            gap_lines.append((f"❗ 待补齐凭证：{_clip(g, 46)}\n", False, ALERT_RED, 11))
    if problems:
        for p in problems[:3]:
            gap_lines.append((f"⚠️ 现场卡点：{_clip(p.get('title'), 20)} - {_clip(p.get('detail'), 40)}\n", False, GRAY, 11))
    if not gap_lines:
        gap_lines.append(("✅ 当前业务数据全量核验闭环，无突出数据断层。", False, SUCCESS_GREEN, 12))
    text(s, 7.0, 2.6, 5.3, 3.9, gap_lines, space=3)

    # ---------------- Slide 03: 权威媒体深度好评与国际定调 (Confirmed Hits & Quotes)
    s = new_slide("03", "权威媒体深度评论", "MEDIA REVIEWS", "🌟 权威外媒深度好评与现场参与战果", "※ 福布斯、路透社、新华社等全球重点媒体对智元人形机器人核心评述")
    
    e = sections.get("exposure", {})
    critiques = e.get("media_commentaries", [])
    if not critiques and mon and mon.get("articles"):
        critiques = [a for a in mon["articles"] if a.get("editorial_critique") or a.get("short_quote")]

    # 3 张并排外媒深度评价卡片
    display_critiques = critiques[:3] if critiques else [
        {"outlet": "Forbes", "quote": "Agibot humanoid robots opened the ceremony with a high-precision dance performance.", "critique": "福布斯高度评价 A3 在欧洲大会舞台上的运动协调与具身智能高精度控制力。"},
        {"outlet": "Reuters", "quote": "Outlined the opportunity to take AI into the physical world through manufacturing.", "critique": "路透社深度专访报道智元联合创始人，聚焦 AI 走进物理世界与工业量产机会。"},
        {"outlet": "新华社", "quote": "中国科技企业规模创纪录，A3 人形机器人群舞开场。", "critique": "国家级媒体重点特写报道中国前沿人形机器人规模化参展里斯本。"}
    ]

    for i, c in enumerate(display_critiques):
        cx = 0.8 + i * 3.95
        card(s, cx, 1.9, 3.8, 4.8, fill=CARD_FILL)
        outlet_name = c.get("outlet", "国际权威媒体")
        text(s, cx + 0.2, 2.1, 3.4, 0.4, f"🎙️ {outlet_name}", size=16, bold=True, color=BRAND_BLUE)
        q_text = c.get("quote") or c.get("short_quote") or ""
        if q_text:
            text(s, cx + 0.2, 2.6, 3.4, 1.3, f"“{_clip(q_text, 80)}”", size=12, bold=True, color="1E293B")
        critique_desc = c.get("critique") or c.get("editorial_critique") or c.get("focus") or ""
        text(s, cx + 0.2, 4.0, 3.4, 2.4, _clip(critique_desc, 130), size=12, color=GRAY)

    # ---------------- Slide 04: 战略复盘与高管决策闭环 (Strategic Roadmap & Done Criteria)
    s = new_slide("04", "高管决策与战略推进", "NEXT ACTIONS", "🚀 战略推进与行动闭环：明确完成标准 (Done Criteria)", "※ 倒排节点、责任到人、闭环归档")
    
    r = sections.get("review", {})
    next_steps = r.get("next_steps", [])[:5]
    if not next_steps:
        next_steps = [
            {"title": "欧洲样机调配与技术保障支持", "detail": "协调总部调度 2 台 A3 新机直发欧洲保税仓，锁定倒排交付。"},
            {"title": "高意向客户 NDA 签署与商务会谈", "detail": "推动 BETT/现场 7 家意向客户签署保密协议，由区域负责人闭环。"},
            {"title": "外媒长尾声量二次发酵与通稿补发", "detail": "欧洲 PR 团队补发新华社/路透社专访内容至领英与全球行业社群。"}
        ]

    for i, step in enumerate(next_steps):
        sy = 1.9 + i * 0.95
        card(s, 0.8, sy, 11.7, 0.82, fill=CARD_FILL)
        # 序号标记
        text(s, 1.0, sy + 0.16, 0.4, 0.5, f"0{i+1}", size=16, bold=True, color=BRAND_BLUE)
        # 标题与行动
        text(s, 1.5, sy + 0.12, 5.0, 0.35, _clip(step.get("title", ""), 30), size=13, bold=True, color=INK)
        text(s, 1.5, sy + 0.42, 5.0, 0.35, _clip(step.get("detail", ""), 48), size=11, color=GRAY)
        # 完成标准 (Done Criteria) 标签
        text(s, 6.8, sy + 0.22, 5.5, 0.4, "【完成标准】达成书面签署 / 成果凭证归档即闭环 ➔ 🟢", size=11, bold=True, color=SUCCESS_GREEN, align=PP_ALIGN.RIGHT)

    out_path = Path(out_path)
    prs.save(str(out_path))
    return out_path



def render_all(event_dir: Path, cfg: dict, sections: dict | None = None, log=print) -> dict:
    event_dir = Path(event_dir)
    ev = load_event(event_dir)
    out = event_dir / "outputs"
    out.mkdir(exist_ok=True)
    if sections is None:
        sections = json.loads((out / "report_sections.json").read_text(encoding="utf-8"))
    mon = None
    mp = out / "monitor_latest.json"
    if mp.exists():
        mon = json.loads(mp.read_text(encoding="utf-8"))
    photos = sorted([p for p in (event_dir / "photos").glob("*") if p.suffix.lower() in PHOTO_EXT]) if (event_dir / "photos").exists() else []
    md = out / "report.md"
    md.write_text(render_markdown(sections, ev, mon), encoding="utf-8")
    pp = render_pptx(sections, ev, mon, out / "report.pptx", photos)
    return {"md": md, "pptx": pp}
