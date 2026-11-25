"""横向对比：哪场活动的传播价值最高。

原则：打分由程序算，模型只写结论文字。
- 只比较“所有活动都有数据”的指标，缺数据的指标不参与总分（避免把“没记录”当成“0”）。
- 每项指标用“该指标最大值的占比”归一化，再按四个维度加权。
- 导出的 xlsx 里得分是 Excel 公式，改数字/改权重会自动重算。
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path

import yaml

from . import prompts
from .docs import read_folder
from .llm import LLM
from .monitor import load_event

GROUPS = [("reach", "触达"), ("quality", "质量"), ("engagement", "互动"), ("conversion", "转化")]


def load_dictionary(cfg: dict) -> list[dict]:
    p = Path(cfg["_root"]) / "metrics_dict.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or []


def load_metrics(event_dir: Path, dictionary: list[dict]) -> dict:
    """读取 metrics.yaml → {event, status, values{key: float}, meta{key: {...}}, highlights[]}"""
    p = Path(event_dir) / "metrics.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    raw = raw or {}
    values, meta = {}, {}
    for k, v in (raw.get("metrics") or {}).items():
        if isinstance(v, dict):
            val = v.get("value")
            meta[k] = {kk: vv for kk, vv in v.items() if kk != "value"}
        else:
            val, meta[k] = v, {}
        if val is not None:
            try:
                values[k] = float(val)
            except (TypeError, ValueError):
                pass
    # 合计类指标：文档没给总数时，用分项相加（并标注为推算）
    for m in dictionary:
        if m.get("derived") and m["key"] not in values:
            parts = [values.get(x) for x in m["derived"]]
            if all(x is not None for x in parts):
                values[m["key"]] = sum(parts)
                meta[m["key"]] = {"approx": True, "note": "由 " + " + ".join(m["derived"]) + " 推算", "derived": True}
    return {"event": raw.get("event") or Path(event_dir).name, "short": Path(event_dir).name,
            "status": raw.get("status", "unknown"), "values": values, "meta": meta,
            "highlights": raw.get("highlights") or []}


def score_events(events: list[dict], dictionary: list[dict], weights: dict) -> dict:
    """返回 {groups: {g: {metrics:[key], common:[key], ...}}, scores: {short: {...}}, included: [...]}"""
    names = [e["short"] for e in events]
    gscore = {n: {} for n in names}
    included_metrics = []
    common_by_group = {}
    for g, _ in GROUPS:
        ms = [m for m in dictionary if m["group"] == g and m.get("score", True)]
        common = [m for m in ms if all(m["key"] in e["values"] for e in events)]
        common_by_group[g] = [m["key"] for m in common]
        if not common:
            continue
        included_metrics += [m["key"] for m in common]
        import math
        for e in events:
            ratios = []
            for m in common:
                mx = max(x["values"][m["key"]] for x in events)
                val = max(0.0, e["values"][m["key"]])
                # 对触达类大数值指标采用对数归一化，避免单一量级差主导总分
                if m.get("group") == "reach" or mx > 1000:
                    r = 1.0 if mx <= 0 else (math.log10(val + 1) / math.log10(mx + 1))
                else:
                    r = 1.0 if mx == 0 else val / mx
                ratios.append(r)
            gscore[e["short"]][g] = sum(ratios) / len(ratios) * 100
    used = [g for g, _ in GROUPS if common_by_group.get(g)]
    wsum = sum(weights.get(g, 0) for g in used)
    total = {}
    for e in events:
        n = e["short"]
        total[n] = (sum(weights.get(g, 0) * gscore[n][g] for g in used) / wsum) if wsum else None
    all_keys = [m["key"] for m in dictionary if m.get("score", True)]
    coverage = {e["short"]: sum(1 for k in all_keys if k in e["values"]) / max(1, len(all_keys)) for e in events}
    return {"group_scores": gscore, "total": total, "used_groups": used, "common": common_by_group,
            "included_metrics": included_metrics, "coverage": coverage}


# ---------------------------------------------------------------- 主入口
def run_compare(events_root: Path, cfg: dict, llm: LLM | None, shorts: list[str] | None = None, narrate: bool = True, log=print) -> dict:
    events_root = Path(events_root)
    dictionary = load_dictionary(cfg)
    dirs = [events_root / s for s in shorts] if shorts else sorted(p for p in events_root.iterdir() if (p / "metrics.yaml").exists())
    events = [load_metrics(d, dictionary) for d in dirs if (d / "metrics.yaml").exists()]
    if len(events) < 2:
        raise ValueError("至少需要 2 场活动都有 metrics.yaml 才能对比。可先运行 extract-metrics 从文档生成草稿。")
    for d, e in zip(dirs, events):
        try:
            ev = load_event(d)
            e["name"] = ev["name"]
        except Exception:
            e["name"] = e["event"]
    result = score_events(events, dictionary, cfg.get("scoring_weights", {}))
    warnings = []
    label_of = {m["key"]: m["label"] for m in dictionary}
    for m in dictionary:
        if not m.get("derived"):
            continue
        have = [e for e in events if m["key"] in e["values"]]
        derived = [e for e in have if e["meta"].get(m["key"], {}).get("derived")]
        plain = [e for e in have if not e["meta"].get(m["key"], {}).get("derived")]
        if derived and plain:
            warnings.append(
                f"口径提醒｜{label_of[m['key']]}：{'、'.join(e['name'] for e in derived)} 是由海外+国内分项相加得到的，"
                f"而 {'、'.join(e['name'] for e in plain)} 的原始报告没有拆分国内/海外。"
                f"若国内媒体占比很大，两者不可直接比较；建议补录拆分数据后再下结论。")
    for e in events:
        if e["status"] != "reviewed":
            warnings.append(f"「{e['name']}」的 metrics.yaml 状态是 {e['status']}（未人工核对），结论仅供参考")
    out = {"events": events, "dictionary": dictionary, "result": result, "warnings": warnings,
           "weights": cfg.get("scoring_weights", {})}
    if narrate and llm is not None:
        brief = {"events": [{"name": e["name"], "values": e["values"], "meta": e["meta"], "highlights": e["highlights"], "status": e["status"]} for e in events],
                 "scores": result, "labels": {m["key"]: m["label"] for m in dictionary}, "warnings": warnings}
        user = f"<<JSON>>{json.dumps(brief, ensure_ascii=False)}<<END>>"
        out["narrative"] = llm.chat(prompts.COMPARE_NARRATIVE, user, max_tokens=1500)
    return out


def ranking(out: dict) -> list[tuple[str, float]]:
    t = out["result"]["total"]
    names = {e["short"]: e["name"] for e in out["events"]}
    return sorted(((names[k], v) for k, v in t.items() if v is not None), key=lambda x: -x[1])


# ---------------------------------------------------------------- 导出
def _fmt(v):
    if v is None:
        return "—"
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:g}"


def render_markdown(out: dict) -> str:
    ev, res, dic = out["events"], out["result"], out["dictionary"]
    L = ["# 活动传播价值横向对比", f"生成时间：{datetime.now():%Y-%m-%d %H:%M}", ""]
    L += ["## 可比得分（只含所有活动都有数据的指标）", "", "| 活动 | " + " | ".join(n for _, n in GROUPS) + " | **可比总分** | 数据完整度 |", "|---|" + "---|" * (len(GROUPS) + 2)]
    for e in ev:
        gs = res["group_scores"][e["short"]]
        cells = [(f"{gs[g]:.0f}" if g in gs else "—") for g, _ in GROUPS]
        t = res["total"][e["short"]]
        L.append(f"| {e['name']} | " + " | ".join(cells) + f" | **{t:.0f}** | {res['coverage'][e['short']]:.0%} |")
    L += ["", "权重：" + "　".join(f"{n} {out['weights'].get(g, 0):.0%}" for g, n in GROUPS), ""]
    miss = [n for g, n in GROUPS if g not in res["used_groups"]]
    if miss:
        L.append(f"> 「{'、'.join(miss)}」维度因为至少一场活动没有数据，未计入总分。")
    L += ["", "## 全部指标明细", "", "| 指标 | " + " | ".join(e["name"] for e in ev) + " |", "|---|" + "---|" * len(ev)]
    for m in dic:
        if any(m["key"] in e["values"] for e in ev):
            cells = []
            for e in ev:
                v = e["values"].get(m["key"])
                tag = "≈" if e["meta"].get(m["key"], {}).get("approx") else ""
                cells.append(f"{tag}{_fmt(v)}")
            L.append(f"| {m['label']}（{m.get('unit','')}） | " + " | ".join(cells) + " |")
    for e in ev:
        if e["highlights"]:
            L += ["", f"**{e['name']} 的传播亮点**"] + [f"- {h}" for h in e["highlights"]]
    if out.get("narrative"):
        L += ["", "## 结论", out["narrative"]]
    if out["warnings"]:
        L += ["", "## 注意"] + [f"- {w}" for w in out["warnings"]]
    L.append("\n≈ 表示原文为“30+/1W+”这类下限或由分项推算。")
    return "\n".join(L) + "\n"


def write_xlsx(out: dict, path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter as col

    ev, res, dic = out["events"], out["result"], out["dictionary"]
    n = len(ev)
    F = "Arial"
    head_fill = PatternFill("solid", fgColor="F1E3F0")
    input_font = Font(name=F, color="0000FF")
    base = Font(name=F)
    bold = Font(name=F, bold=True)
    thin = Side(style="thin", color="D9C8DE")
    box = Border(top=thin, bottom=thin, left=thin, right=thin)

    wb = Workbook()
    # ---- Sheet 1：可比得分（公式）
    ws = wb.active
    ws.title = "可比得分"
    ws["A1"] = "活动传播价值横向对比（可比得分）"
    ws["A1"].font = Font(name=F, bold=True, size=14)
    ws["A2"] = "蓝色=可修改的输入（数值/权重）；黑色=公式。只纳入所有活动都有数据的指标，每项按“最大值占比”归一化。"
    ws["A2"].font = Font(name=F, italic=True, color="6F6580")
    hdr = ["维度", "指标"] + [e["name"] for e in ev] + [""] + [f"{e['name']} 占比" for e in ev]
    for i, h in enumerate(hdr, 1):
        c = ws.cell(row=4, column=i, value=h)
        c.font, c.fill, c.border = bold, head_fill, box
        c.alignment = Alignment(wrap_text=True, vertical="center")
    r = 5
    group_rows = {}
    for g, gname in GROUPS:
        keys = res["common"].get(g, [])
        if not keys:
            continue
        start = r
        for k in keys:
            m = next(x for x in dic if x["key"] == k)
            ws.cell(row=r, column=1, value=gname).font = base
            ws.cell(row=r, column=2, value=f"{m['label']}（{m.get('unit','')}）").font = base
            for j, e in enumerate(ev):
                c = ws.cell(row=r, column=3 + j, value=e["values"][k])
                c.font, c.number_format = input_font, "#,##0.##"
            first, last = col(3), col(2 + n)
            for j in range(n):
                v = f"{col(3 + j)}{r}"
                ws.cell(row=r, column=4 + n + j, value=f"=IF(MAX(${first}{r}:${last}{r})=0,1,{v}/MAX(${first}{r}:${last}{r}))").number_format = "0%"
                ws.cell(row=r, column=4 + n + j).font = base
            r += 1
        group_rows[g] = (start, r - 1)
    r += 1
    # 维度得分与权重
    ws.cell(row=r, column=1, value="维度得分").font = bold
    ws.cell(row=r, column=2, value="权重").font = bold
    for j, e in enumerate(ev):
        ws.cell(row=r, column=3 + j, value=e["name"]).font = bold
    for i in range(1, 3 + n):
        ws.cell(row=r, column=i).fill = head_fill
    r += 1
    w_cells, g_first = {}, r
    for g, gname in GROUPS:
        if g not in group_rows:
            continue
        s, e_ = group_rows[g]
        ws.cell(row=r, column=1, value=gname).font = base
        wc = ws.cell(row=r, column=2, value=out["weights"].get(g, 0))
        wc.font, wc.number_format = input_font, "0%"
        w_cells[g] = r
        for j in range(n):
            ratio_col = col(4 + n + j)
            c = ws.cell(row=r, column=3 + j, value=f"=AVERAGE({ratio_col}{s}:{ratio_col}{e_})*100")
            c.number_format, c.font = "0.0", base
        r += 1
    g_last = r - 1
    ws.cell(row=r, column=1, value="可比总分").font = bold
    for j in range(n):
        cc = col(3 + j)
        c = ws.cell(row=r, column=3 + j, value=f"=SUMPRODUCT($B${g_first}:$B${g_last},{cc}{g_first}:{cc}{g_last})/SUM($B${g_first}:$B${g_last})")
        c.number_format, c.font = "0.0", bold
    total_row = r
    r += 2
    miss = [nm for g, nm in GROUPS if g not in group_rows]
    if miss:
        ws.cell(row=r, column=1, value=f"「{'、'.join(miss)}」维度：至少一场活动缺数据，未计入总分，见“全部指标”表。").font = Font(name=F, color="9A3B6B")
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 34
    for i in range(3, 4 + 2 * n):
        ws.column_dimensions[col(i)].width = 17
    ws.freeze_panes = "C5"

    # ---- Sheet 2：全部指标
    w2 = wb.create_sheet("全部指标")
    h2 = ["维度", "指标", "单位"] + [e["name"] for e in ev] + ["备注/来源"]
    for i, h in enumerate(h2, 1):
        c = w2.cell(row=1, column=i, value=h)
        c.font, c.fill, c.border = bold, head_fill, box
    gname_of = dict(GROUPS)
    gname_of["input"] = "投入"
    rr = 2
    for m in dic:
        if not any(m["key"] in e["values"] for e in ev):
            continue
        w2.cell(row=rr, column=1, value=gname_of.get(m["group"], m["group"])).font = base
        w2.cell(row=rr, column=2, value=m["label"]).font = base
        w2.cell(row=rr, column=3, value=m.get("unit", "")).font = base
        notes = []
        for j, e in enumerate(ev):
            v = e["values"].get(m["key"])
            c = w2.cell(row=rr, column=4 + j, value=v)
            c.font, c.number_format = input_font, "#,##0.##"
            mt = e["meta"].get(m["key"], {})
            bits = [mt.get("source"), mt.get("note"), "下限/约数" if mt.get("approx") else None]
            bits = [b for b in bits if b]
            if bits:
                notes.append(f"{e['name']}: " + "；".join(bits))
        w2.cell(row=rr, column=4 + n, value="\n".join(notes)).font = Font(name=F, color="6F6580", size=9)
        w2.cell(row=rr, column=4 + n).alignment = Alignment(wrap_text=True, vertical="top")
        rr += 1
    w2.column_dimensions["A"].width = 8
    w2.column_dimensions["B"].width = 30
    w2.column_dimensions["C"].width = 6
    for j in range(n):
        w2.column_dimensions[col(4 + j)].width = 16
    w2.column_dimensions[col(4 + n)].width = 70
    w2.freeze_panes = "D2"

    # ---- Sheet 3：亮点与说明
    w3 = wb.create_sheet("亮点与说明")
    rr = 1
    for e in ev:
        w3.cell(row=rr, column=1, value=e["name"]).font = bold
        rr += 1
        for h in e["highlights"] or ["（未记录）"]:
            w3.cell(row=rr, column=1, value="• " + h).font = base
            rr += 1
        rr += 1
    if out.get("narrative"):
        w3.cell(row=rr, column=1, value="结论").font = bold
        rr += 1
        for line in out["narrative"].splitlines():
            w3.cell(row=rr, column=1, value=line).font = base
            rr += 1
    for wmsg in out["warnings"]:
        rr += 1
        w3.cell(row=rr, column=1, value="注意：" + wmsg).font = Font(name=F, color="9A3B6B")
    w3.column_dimensions["A"].width = 120

    path = Path(path)
    wb.save(str(path))
    return path


def export_all(out: dict, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    md = out_dir / f"compare_{stamp}.md"
    md.write_text(render_markdown(out), encoding="utf-8")
    xp = write_xlsx(out, out_dir / f"compare_{stamp}.xlsx")
    return {"md": md, "xlsx": xp}


# ---------------------------------------------------------------- 从文档抽取指标草稿
def extract_metrics(event_dir: Path, cfg: dict, llm: LLM, log=print) -> Path:
    """读取 events/<活动>/docs 下的文档，抽取指标 → metrics.draft.yaml（需你核对后改名为 metrics.yaml 并标 reviewed）。"""
    event_dir = Path(event_dir)
    dictionary = load_dictionary(cfg)
    ev = load_event(event_dir)
    docs = read_folder(event_dir / "docs", per_file=30000, total=90000)
    if not docs:
        raise FileNotFoundError(f"{event_dir/'docs'} 里没有可读文档，先把周报/总结报告放进去。")
    payload = {"event": ev["name"], "dictionary": [{"key": m["key"], "label": m["label"], "unit": m.get("unit")} for m in dictionary],
               "docs": [{"file": n, "text": t} for n, t in docs]}
    user = f"<<JSON>>{json.dumps(payload, ensure_ascii=False)}<<END>>"
    res = llm.chat_json(prompts.METRICS_EXTRACT, user, max_tokens=3500)
    valid = {m["key"] for m in dictionary}
    res["metrics"] = {k: v for k, v in (res.get("metrics") or {}).items() if k in valid}
    res["status"] = "draft"
    res["event"] = ev["name"]
    out = event_dir / "metrics.draft.yaml"
    out.write_text("# 由 AI 从文档抽取的草稿 —— 请逐项核对 source/evidence，确认后改名为 metrics.yaml 并把 status 改为 reviewed\n"
                   + yaml.safe_dump(res, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return out
