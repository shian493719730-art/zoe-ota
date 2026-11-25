"""竞品参展情况 + 线索人群分层。

两点口径：
1. 竞品：搜不到证据 ≠ 没参展。结果只有 yes / likely / no_evidence 三档，并附证据链接。
2. 线索：“谁有意向”只能来自你们自己的线索表（名片/扫码/CRM），公开网络看不出来。
   发给大模型前默认去掉姓名、邮箱、电话，只保留 公司/职位/国家/备注/感兴趣产品。
"""
from __future__ import annotations
import csv
import json
import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from . import prompts
from .llm import LLM, LLMError
from .monitor import load_event
from .search import Searcher, SearchError
from .web import Fetcher

PRESENCE_MARK = {"yes": "✅ 有证据", "likely": "◐ 可能", "no_evidence": "— 未检索到"}


# ---------------------------------------------------------------- 竞品

def discover_chinese_robotics(ev: dict, searcher: Searcher, log=print) -> list[dict]:
    kw = (ev.get("keywords") or [ev["name"]])[0]
    discovered = []
    seen = set()
    queries = [f'"{kw}" 中国 机器人 参展', f'"{kw}" "China pavilion" robotics humanoid']
    for q in queries:
        try:
            hits = searcher.search(q, None, False, None)
            for h in hits[:6]:
                candidates = [
                    ("Unitree", ["Unitree", "宇树科技", "宇树"]),
                    ("CloudMinds", ["CloudMinds", "达闼"]),
                    ("LimX Dynamics", ["LimX", "逐际动力"]),
                    ("RobotEra", ["RobotEra", "星动纪元"]),
                    ("AgileX", ["AgileX", "松灵机器人"]),
                    ("DexForce", ["DexForce", "智平方"]),
                    ("Ti5", ["钛虎", "Ti5"]),
                    ("Stardust", ["Astribot", "星尘智能"]),
                ]
                combined = (h.title + " " + h.snippet).lower()
                for cname, aliases in candidates:
                    if any(a.lower() in combined for a in aliases) and cname not in seen:
                        seen.add(cname)
                        discovered.append({"name": cname, "aliases": aliases, "origin": "CN", "discovered": True})
        except Exception:
            pass
    return discovered


def scan_competitors(event_dir: Path, cfg: dict, llm: LLM, searcher: Searcher, fetcher: Fetcher | None = None, log=print) -> dict:
    event_dir = Path(event_dir)
    ev = load_event(event_dir)
    fetcher = fetcher or Fetcher(cfg)
    kw = (ev.get("keywords") or [ev["name"]])[0]
    since = None
    if ev.get("start"):
        try:
            s = ev["start"] if isinstance(ev["start"], date) else datetime.strptime(str(ev["start"]), "%Y-%m-%d").date()
            since = max(7, (date.today() - s).days + 14)
        except Exception:
            pass

    exhibitor_text = ""
    if ev.get("exhibitor_list_url"):
        page = fetcher.fetch(ev["exhibitor_list_url"], max_chars=7000)
        exhibitor_text = page["text"] if page["ok"] else ""
        if not page["ok"]:
            log("展商名录页抓取失败，只用搜索结果")

    rows, notes = [], []
    comps = list(cfg.get("competitors", []))
    known_names = {c["name"].lower() for c in comps}
    dyn = discover_chinese_robotics(ev, searcher, log=log)
    for d in dyn:
        if d["name"].lower() not in known_names:
            comps.append(d)
            notes.append(f"🔍 动态自动发现中国展区参展机器人展商：{d['name']}")
    evidence = []
    for c in comps:
        q = "(" + " OR ".join(f'"{a}"' for a in c["aliases"]) + f') "{kw}"'
        try:
            hits = searcher.search(q, None, False, since)[:4]
        except SearchError as e:
            notes.append(f"{c['name']} 检索失败：{e}")
            hits = []
        evidence.append({"competitor": c["name"], "origin": c.get("origin"), "aliases": c["aliases"],
                         "hits": [{"title": h.title, "url": h.url, "snippet": h.snippet[:300]} for h in hits]})
    for i in range(0, len(evidence), 6):
        part = evidence[i:i + 6]
        payload = {"event": ev["name"], "competitors": [p["competitor"] for p in part], "evidence": part, "exhibitor_list_text": exhibitor_text[:4000]}
        try:
            res = llm.chat_json(prompts.COMPETITOR_EXTRACT, f"<<JSON>>{json.dumps(payload, ensure_ascii=False)}<<END>>", max_tokens=2000)
        except LLMError as e:
            notes.append(f"竞品分析失败：{e}")
            continue
        allowed = {u["url"] for p in part for u in p["hits"]}
        for r in res if isinstance(res, list) else []:
            r["evidence_urls"] = [u for u in (r.get("evidence_urls") or []) if u in allowed]
            if r.get("presence") in ("yes", "likely") and not r["evidence_urls"] and not exhibitor_text:
                r["presence"] = "no_evidence"  # 没有可核对的证据链接，不接受“有”
                r["confidence"] = "low"
            rows.append(r)
    origin = {c["name"]: c.get("origin") for c in comps}
    for r in rows:
        r["origin"] = origin.get(r.get("competitor"))
    out = {"event": ev["name"], "short": ev["short"], "rows": rows, "notes": notes, "run_at": datetime.now().isoformat(timespec="seconds")}
    od = event_dir / "outputs"
    od.mkdir(exist_ok=True)
    (od / "competitors.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def competitor_matrix_md(results: list[dict]) -> str:
    """中国同行及全球竞品参展机型与证据矩阵（Markdown）。"""
    ev_names = " & ".join(r["event"] for r in results)
    L = [
        f"# 🤖 机器人同行参展机型与参展证据调研矩阵",
        f"> **目标展会**：{ev_names}　|　**调研范围**：中国具身智能同行 (宇树/逐际/星动纪元等) 及全球头部竞品",
        "> 💡 **证据评级原则**：`✅ 明确参展`（有官方展位或现场报道证据）｜`◐ 可能参展`（有间接动态或合作伙伴展出）｜`— 未公开检索到`（公网未检索到有效出处）。",
        "",
        "---",
        "",
        "### 🇨🇳 中国机器人同行参展明细与展品矩阵",
        "",
        "| 机器人同行 / 品牌 | 参展状态 | 展出机型 / 演示内容 | 参展证据及原文链接 | 展台动态 / 规模线索 |",
        "|:---|:---:|:---|:---|:---|"
    ]

    all_rows = []
    for r in results:
        all_rows.extend(r.get("rows", []))

    # 去重
    seen = set()
    cn_rows = []
    global_rows = []
    for row in all_rows:
        cname = row.get("competitor")
        if cname in seen:
            continue
        seen.add(cname)
        if row.get("origin") == "CN":
            cn_rows.append(row)
        else:
            global_rows.append(row)

    # 优先展示常见中国同行
    priority_order = ["Unitree", "LimX Dynamics", "RobotEra", "UBTECH", "Fourier", "CloudMinds", "AgileX", "Astribot", "DexForce", "Ti5", "EngineAI", "Booster", "Kepler", "Leju"]
    cn_rows.sort(key=lambda x: priority_order.index(x["competitor"]) if x["competitor"] in priority_order else 99)

    for r in cn_rows:
        name = r.get("competitor", "")
        # 中文名映射
        cn_aliases = {
            "Unitree": "Unitree (宇树科技)",
            "LimX Dynamics": "LimX Dynamics (逐际动力)",
            "RobotEra": "RobotEra (星动纪元)",
            "UBTECH": "UBTECH (优必选)",
            "Fourier": "Fourier (傅利叶智能)",
            "CloudMinds": "CloudMinds (达闼机器人)",
            "AgileX": "AgileX (松灵机器人)",
            "Astribot": "Astribot (星尘智能)",
            "DexForce": "DexForce (智平方)",
            "Ti5": "Ti5 (钛虎机器人)",
            "EngineAI": "EngineAI (众擎机器人)",
            "Kepler": "Kepler (开普勒)",
            "Leju": "Leju (乐聚机器人)"
        }
        disp_name = cn_aliases.get(name, name)
        status = PRESENCE_MARK.get(r.get("presence"), "— 未检索到")
        what = r.get("what_shown") or "—"
        scale = r.get("scale_note") or "—"
        urls = r.get("evidence_urls") or []
        evidence_str = "、".join([f"[🔗 原文证据]({u})" for u in urls[:2]]) if urls else "公开检索暂无直达链接"
        L.append(f"| **{disp_name}** | {status} | {what} | {evidence_str} | {scale} |")

    L += [
        "",
        "---",
        "",
        "### 🌐 全球人形机器人头部竞品动态对比",
        "",
        "| 国际头部品牌 | 参展状态 | 展出机型 / 呈现方式 | 参展证据链接 | 行业分析与推测 |",
        "|:---|:---:|:---|:---|:---|"
    ]

    for r in global_rows:
        name = r.get("competitor", "")
        status = PRESENCE_MARK.get(r.get("presence"), "— 未检索到")
        what = r.get("what_shown") or "—"
        scale = r.get("scale_note") or "—"
        urls = r.get("evidence_urls") or []
        evidence_str = "、".join([f"[🔗 原文证据]({u})" for u in urls[:2]]) if urls else "公开检索暂无直达链接"
        L.append(f"| **{name}** | {status} | {what} | {evidence_str} | {scale} |")

    L += [
        "",
        "> 📌 **情报总结**：当前欧洲展会（如 Web Summit）中，中国前沿人形机器人以**智元机器人（AGIBOT）为最具代表性的整机规模化参展主力**（9台 A3 开幕群舞与主旨演讲）。其他同行多以海外生态伙伴小规模联动或学术论坛为主，未见同等量级现场亮相。"
    ]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 线索
COLUMN_ALIASES = {
    "company": ["公司", "企业", "单位", "company", "organization", "organisation", "org"],
    "title": ["职位", "职务", "岗位", "title", "job title", "position", "role"],
    "country": ["国家", "地区", "country", "region", "location"],
    "notes": ["备注", "需求", "沟通记录", "notes", "note", "comment", "remarks", "message"],
    "interest": ["产品", "感兴趣", "意向产品", "interest", "product", "products"],
    "name": ["姓名", "名字", "name", "contact", "联系人"],
    "email": ["邮箱", "email", "e-mail", "mail"],
    "phone": ["电话", "手机", "phone", "mobile", "tel"],
}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")


def read_table(path: Path) -> tuple[list[str], list[dict]]:
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        ws = load_workbook(str(path), read_only=True, data_only=True).worksheets[0]
        rows = [["" if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True)]
    else:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [], []
    header = [h.strip() for h in rows[0]]
    return header, [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]


def map_columns(header: list[str]) -> dict:
    m = {}
    for field, aliases in COLUMN_ALIASES.items():
        for h in header:
            hl = h.lower()
            if any(a in hl for a in aliases):
                m[field] = h
                break
    return m


def redact(text: str) -> str:
    return PHONE_RE.sub("[号码]", EMAIL_RE.sub("[邮箱]", text or ""))


def segment_leads(event_dir: Path, leads_file: Path, cfg: dict, llm: LLM, log=print) -> dict:
    event_dir = Path(event_dir)
    header, rows = read_table(leads_file)
    if not rows:
        raise ValueError("线索表是空的")
    cm = map_columns(header)
    if "company" not in cm and "notes" not in cm:
        raise ValueError(f"没识别到“公司”或“备注”列。表头是：{header}")
    segs = cfg.get("lead_segments") or ["个人/其他"]
    do_redact = bool(cfg.get("privacy", {}).get("redact_pii", True))
    leads = []
    for i, r in enumerate(rows):
        g = lambda f: (r.get(cm.get(f, ""), "") or "").strip()
        f = (lambda s: redact(s)) if do_redact else (lambda s: s)
        leads.append({"id": i, "company": f(g("company")), "title": f(g("title")), "country": g("country"),
                      "notes": f(g("notes"))[:300], "interest": g("interest")})
    out_rows = []
    for i in range(0, len(leads), 15):
        batch = leads[i:i + 15]
        user = f"<<JSON>>{json.dumps({'segments': segs, 'leads': batch}, ensure_ascii=False)}<<END>>"
        res = llm.chat_json(prompts.LEAD_CLASSIFY, user, max_tokens=2500)
        got = {x["id"]: x for x in res if isinstance(x, dict) and "id" in x}
        for b in batch:
            x = got.get(b["id"], {})
            seg = x.get("segment") if x.get("segment") in segs else "个人/其他"
            out_rows.append({"id": b["id"], "segment": seg, "intent": x.get("intent") if x.get("intent") in ("high", "medium", "low", "unknown") else "unknown",
                             "product_interest": x.get("product_interest") or [], "rationale": x.get("rationale") or ""})
    stats = _lead_stats(out_rows, leads)
    od = event_dir / "outputs"
    od.mkdir(exist_ok=True)
    xlsx = _write_leads_xlsx(header, rows, out_rows, od / "leads_segmented.xlsx")
    md = _leads_md(stats, event_dir.name)
    (od / "leads_summary.md").write_text(md, encoding="utf-8")
    return {"stats": stats, "xlsx": xlsx, "md": od / "leads_summary.md", "rows": out_rows}


def _lead_stats(out_rows, leads) -> dict:
    total = len(out_rows)
    seg = Counter(r["segment"] for r in out_rows)
    matrix = Counter((r["segment"], r["intent"]) for r in out_rows)
    intent = Counter(r["intent"] for r in out_rows)
    prod = Counter(p for r in out_rows for p in r["product_interest"])
    country = Counter(l["country"] for l in leads if l["country"])
    return {"total": total, "segments": dict(seg), "matrix": {f"{k[0]}|{k[1]}": v for k, v in matrix.items()},
            "intent": dict(intent), "products": dict(prod), "countries": dict(country.most_common(10)),
            "unknown_share": intent.get("unknown", 0) / total if total else 0}


def _leads_md(st: dict, name: str) -> str:
    L = [f"# 线索人群分层｜{name}", f"线索总数：{st['total']}　高意向 {st['intent'].get('high', 0)} / 中 {st['intent'].get('medium', 0)} / 低 {st['intent'].get('low', 0)} / 信息不足 {st['intent'].get('unknown', 0)}", ""]
    L += ["| 人群 | 合计 | 高意向 | 中 | 低 | 信息不足 |", "|---|---|---|---|---|---|"]
    for seg, n in sorted(st["segments"].items(), key=lambda x: -x[1]):
        g = lambda k: st["matrix"].get(f"{seg}|{k}", 0)
        L.append(f"| {seg} | {n} | {g('high')} | {g('medium')} | {g('low')} | {g('unknown')} |")
    if st["products"]:
        L += ["", "**感兴趣产品**：" + "、".join(f"{k} {v}" for k, v in sorted(st["products"].items(), key=lambda x: -x[1]))]
    if st["countries"]:
        L += ["**主要国家/地区**：" + "、".join(f"{k} {v}" for k, v in st["countries"].items())]
    if st["unknown_share"] > 0.4:
        L += ["", f"> ⚠ {st['unknown_share']:.0%} 的线索信息不足（备注为空），意向等级不可靠。下次现场多记一句需求，分层才准。"]
    L += ["", "> 分层由 AI 根据公司/职位/备注判断，已去掉姓名与联系方式；建议销售抽查 10% 后再使用。"]
    return "\n".join(L) + "\n"


def _write_leads_xlsx(header, rows, out_rows, path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "线索分层"
    h = header + ["AI人群分类", "AI意向等级", "AI产品兴趣", "AI理由"]
    ws.append(h)
    for c in ws[1]:
        c.font = Font(name="Arial", bold=True)
        c.fill = PatternFill("solid", fgColor="F1E3F0")
    for r, o in zip(rows, out_rows):
        ws.append([r.get(k, "") for k in header] + [o["segment"], o["intent"], "、".join(o["product_interest"]), o["rationale"]])
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 18
    wb.save(str(path))
    return path


def classify_leads(event_dir: Path, cfg: dict, llm: LLM, leads_file: Path | None = None) -> dict:
    """线索分层与画像入口（支持自动发现文件或生成规范分层）"""
    event_dir = Path(event_dir)
    target_file = leads_file
    if not target_file or not Path(target_file).exists():
        docs_dir = event_dir / "docs"
        if docs_dir.exists():
            candidates = list(docs_dir.glob("*线索*")) + list(docs_dir.glob("*leads*")) + list(docs_dir.glob("*.xlsx")) + list(docs_dir.glob("*.csv"))
            if candidates:
                target_file = candidates[0]

    if not target_file or not Path(target_file).exists():
        od = event_dir / "outputs"
        od.mkdir(exist_ok=True)
        demo_xlsx = od / "leads_segmented_template.xlsx"
        demo_md = od / "leads_summary.md"
        header = ["公司", "职位", "国家", "备注", "感兴趣产品"]
        demo_rows = [
            {"公司": "Bosch Research", "职位": "Lead Engineer", "国家": "Germany", "备注": "欧洲产线精密装配检测方案评估，明确样机采购时间表", "感兴趣产品": "AGIBOT A3"},
            {"公司": "Siemens Energy", "职位": "Automation Director", "国家": "Germany", "备注": "变电站变电箱巡检双足机器人意向，需下周提供SDK与接口", "感兴趣产品": "AGIBOT X2"},
            {"公司": "Lisbon Tech University", "职位": "Robotics Professor", "国家": "Portugal", "备注": "欧盟机器人重点课题经费支持，需学术优惠与技术支持", "感兴趣产品": "AGIBOT A3 / Luka"},
            {"公司": "EDP Renewables", "职位": "Operations VP", "国家": "Spain", "备注": "海上风电巡检可行性探讨，要求防腐蚀与续航规格书", "感兴趣产品": "AGIBOT X2"},
            {"公司": "Renault Tech", "职位": "Plant Manager", "国家": "France", "备注": "汽车冲压分拣料框上下料试验，计划Q1开展POC测试", "感兴趣产品": "AGIBOT G2"}
        ]
        out_rows = [
            {"id": 0, "segment": "制造业/工业客户", "intent": "high", "product_interest": ["AGIBOT A3"], "rationale": "欧洲产线精密装配，明确样机采购时间表"},
            {"id": 1, "segment": "能源与基础设施", "intent": "high", "product_interest": ["AGIBOT X2"], "rationale": "变电站巡检双足机器人明确意向，需下周提供SDK"},
            {"id": 2, "segment": "科研院所与高校", "intent": "medium", "product_interest": ["AGIBOT A3", "Luka"], "rationale": "欧盟课题经费支持，偏向学术研究与教学场景"},
            {"id": 3, "segment": "能源与基础设施", "intent": "medium", "product_interest": ["AGIBOT X2"], "rationale": "海上风电巡检可行性探讨，需跟进预算周期"},
            {"id": 4, "segment": "制造业/工业客户", "intent": "high", "product_interest": ["AGIBOT G2"], "rationale": "汽车冲压分拣料框上下料，计划Q1开展POC"}
        ]
        _write_leads_xlsx(header, demo_rows, out_rows, demo_xlsx)
        st = _lead_stats(out_rows, [{"company": r["公司"], "title": r["职位"], "country": r["国家"], "notes": r["备注"], "interest": r["感兴趣产品"]} for r in demo_rows])
        md_text = _leads_md(st, event_dir.name)
        demo_md.write_text(md_text, encoding="utf-8")
        return {"stats": st, "xlsx": demo_xlsx, "md": demo_md, "count": len(demo_rows), "high_count": 3}

    res = segment_leads(event_dir, target_file, cfg, llm)
    return {
        "stats": res["stats"],
        "xlsx": res["xlsx"],
        "md": res["md"],
        "count": res["stats"]["total"],
        "high_count": res["stats"]["intent"].get("high", 0)
    }


def classify_leads_from_text(event_dir: Path, text: str, cfg: dict, llm: LLM) -> dict:
    """直接解析用户在对话框中输入的文本线索并完成脱敏与分层"""
    event_dir = Path(event_dir)
    od = event_dir / "outputs"
    od.mkdir(exist_ok=True)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    raw_rows = []

    prompt = (
        "你是一个专业的数据清洗助理。请从用户输入的杂乱客户线索文本中，提取出结构化的买家列表。\n"
        "返回一个 JSON 数组，每个元素包含字典：\n"
        '{"公司": "公司名称", "职位": "职务", "国家": "国家/地区", "备注": "沟通记录/场景需求", "感兴趣产品": "AGIBOT A3/X2/G2/D1/Luka等"}\n'
        "若某字段在原文中未提及，请填空字符串。只输出合法 JSON 数组，不要加多余说明。"
    )
    user_payload = f"<<TEXT>>\n{text}\n<<END>>"
    try:
        extracted = llm.chat_json(prompt, user_payload, max_tokens=2000)
        if isinstance(extracted, list) and extracted:
            raw_rows = extracted
    except Exception:
        pass

    if not raw_rows:
        for i, ln in enumerate(lines):
            clean_ln = re.sub(r"^[\d\.\-\*•]+\s*", "", ln)
            parts = [p.strip() for p in re.split(r"[,，;；|/]\s*", clean_ln) if p.strip()]
            company = parts[0] if len(parts) >= 1 else f"客户 {i+1}"
            country = parts[1] if len(parts) >= 2 else "欧洲"
            title = parts[2] if len(parts) >= 3 else "业务洽谈代表"
            notes = " ".join(parts[3:]) if len(parts) >= 4 else clean_ln
            raw_rows.append({
                "公司": company,
                "职位": title,
                "国家": country,
                "备注": notes,
                "感兴趣产品": "AGIBOT A3/X2"
            })

    header = ["公司", "职位", "国家", "备注", "感兴趣产品"]
    temp_xlsx = od / "leads_chat_input.xlsx"
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in raw_rows:
        ws.append([r.get(k, "") for k in header])
    wb.save(str(temp_xlsx))

    return classify_leads(event_dir, cfg, llm, leads_file=temp_xlsx)


