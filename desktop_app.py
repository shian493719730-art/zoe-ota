
from __future__ import annotations
"""AGIBOT 商业智能与活动复盘 Agent - 原生桌面客户端主程序
使用 pywebview 构建真正的桌面窗口，彻底摒弃网页地址栏与浏览器标签页。
全量支持：
1. 单一主对话框驱动 + 六大快捷功能卡片
2. 历史会话与生成文件 100% 本地留痕与溯源
3. 成果文件库直接调用系统原生软件（PowerPoint/Excel/Finder）打开
4. 默认置顶接入顶格最高档位：Claude Opus 5.5
"""
import os
import sys
import json
import time
import uuid
import shutil
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path
import http.server
import socketserver
import threading
import webview
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("desktop_app")

ROOT = Path(os.getenv("ZOE_BUNDLE_ROOT") or Path(__file__).resolve().parent)
_live_patch = Path.home() / ".zoe" / "live_patch"
if _live_patch.exists():
    sys.path.insert(0, str(_live_patch))
sys.path.insert(0, str(ROOT))

from agent.config import load_config, events_dir, get_data_root
from agent.llm import LLM, LLMError
from agent.search import Searcher
from agent.web import Fetcher
from agent.file_parser import parse_uploaded_file
from agent import weekly, monitor, report, compare, customers
from agent.event_updater import get_events_catalog, add_custom_event
from agent.conversational_editor import is_edit_or_supplement_request, apply_conversational_edit
from agent.skills.de_ai_tone import DE_AI_RULES, sanitize_ai_cliches

DATA_DIR = Path.home() / ".agibot"
DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR = DATA_DIR / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
DATA_ROOT = get_data_root()
UPLOAD_DIR = DATA_ROOT / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_FILE = HISTORY_DIR / "sessions.json"
VAULT_FILE = HISTORY_DIR / "file_vault.json"


def _seed_workspace(code_root: Path, data_root: Path) -> None:
    """将随安装包附带的初始展会、预置交付物同步到用户可写工作区 ~/.zoe/workspace/（绝不覆盖已有数据）。"""
    for folder in ("events", "outputs", "weekly"):
        src_dir = code_root / folder
        dst_dir = data_root / folder
        dst_dir.mkdir(parents=True, exist_ok=True)
        if not src_dir.exists():
            continue
        for item in src_dir.rglob("*"):
            rel = item.relative_to(src_dir)
            target = dst_dir / rel
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.is_file() and not target.exists():
                if folder == "weekly" and item.name == "state.json":
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(item, target)
                except Exception:
                    pass


_seed_workspace(ROOT, DATA_ROOT)

def load_sessions() -> list[dict]:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    for old in [ROOT / "history" / "sessions.json", Path("/Users/mima0000/Desktop/zoe/event-agent/history/sessions.json")]:
        if old.exists():
            try:
                data = json.loads(old.read_text(encoding="utf-8"))
                save_sessions(data)
                return data
            except Exception:
                pass
    return []

def save_sessions(sessions: list[dict]):
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSIONS_FILE.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"保存会话失败: {e}")

def format_error_msg(err: Exception) -> str:
    s = str(err)
    if "没有配置 API Key" in s or "API Key" in s or "api_key" in s:
        return (
            "**提示：尚未配置大模型 API Key**\n\n"
            "请点击软件界面左下角 **【Claude Opus 5.5 就绪】** 设置弹窗，填入您手头的 API 网关地址与 Key（系统已默认锁定顶格旗舰 **Claude Opus 5.5**）。\n\n"
            "• 若您当前处于无 Key 离线演示状态，也可在设置中将接口类型切换为 **【本地离线演练 Mock】**，即可零额度无限制体验完整功能与真实成果文件生成！"
        )
    return f"任务执行异常: {s}"

def detect_event_and_task(msg: str, chosen_event: str | None = None) -> tuple[str, str, str, str]:
    """返回 (event_short, task_category, event_label, task_label)
    绝不强行将所有任务都默认判定为 websummit2026！
    """
    m = (msg or "").lower()
    
    # 1. 任务类型判定
    if any(k in m for k in ("竞品", "同行", "宇树", "逐际", "星动纪元", "达闼", "参展证据", "展品机型", "同行矩阵", "机器人展区", "机型")):
        task = "competitor"
        task_label = "机器人同行排查"
    elif any(k in m for k in ("周报", "闭环", "凭证", "进度", "待办", "追问", "卡点", "催办")):
        task = "weekly"
        task_label = "事项闭环周报"
    elif any(k in m for k in ("对比", "哪场", "更值", "roi", "雷达图", "横向")):
        task = "compare"
        task_label = "跨展会横向对比"
    elif any(k in m for k in ("监测", "舆情", "媒体报道", "外媒", "福布斯", "路透社")):
        task = "monitor"
        task_label = "全网媒体声量监测"
    elif any(k in m for k in ("ppt", "报告", "总结", "四段式", "幻灯片", "演示文稿")):
        task = "report"
        task_label = "展会复盘与商务PPT"
    else:
        task = "general"
        task_label = "深度咨询与工作流"

    # 2. 活动归属判定（从每日自动更新的目标活动库中动态匹配）
    cat = get_events_catalog(root_dir=DATA_ROOT)
    catalog_events = cat.get("events", [])
    ev_map = {e["short"]: e["name"] for e in catalog_events}
    ev_map["weekly"] = "事项闭环周报管理"
    ev_map["competitor"] = "机器人同行与竞品调研"
    ev_map["compare"] = "跨展会横向对比"
    ev_map["general"] = "通用市场商业任务"

    # 优先检测用户消息中是否明确提及了特定展会（如 "是berlin那个", "看下CES的", "IFA展会"）
    import re as _re
    explicit_msg_event = None
    for e in catalog_events:
        short = e["short"]
        kws = [short.lower(), e["name"].lower()] + [k.lower() for k in e.get("keywords", [])] + [k.lower() for k in e.get("keywords_cn", [])]
        # 提取自定义活动名中的关键识别词（如 "AGIBOT | UNIDO" -> "unido"）
        for tok in _re.split(r"[\s|｜/()（）\-—_]+", e["name"].lower()):
            if len(tok) >= 3 and tok not in ("agibot", "event", "summit", "expo", "2026", "2027"):
                kws.append(tok)
        # 特别支持常见缩写与口语简称
        if "ifa" in short:
            kws += ["berlin", "柏林", "ifa"]
        elif "ces" in short:
            kws += ["ces", "拉斯维加斯"]
        elif "websummit" in short:
            kws += ["websummit", "web summit", "里斯本"]

        if any(k in m for k in kws if k):
            explicit_msg_event = short
            break

    event = explicit_msg_event or (chosen_event if (chosen_event and chosen_event not in ("auto", "", "undefined")) else None)
    if not event:
        if task == "weekly":
            event = "weekly"
        elif task == "competitor":
            event = "competitor"
        elif task == "compare":
            event = "compare"
        else:
            event = "websummit2026"

    ev_label = ev_map.get(event, event)
    return event, task, ev_label, task_label


def detect_event_from_text(msg: str, default: str = "websummit2026") -> str:
    ev, _, _, _ = detect_event_and_task(msg, chosen_event=None)
    return ev if ev not in ("general", "weekly", "competitor") else default


def classify_file(path: Path, tag: str = "") -> dict | None:
    """对单个文件进行【输入材料 / 输出交付物】与【业务任务】精细分类，并标注精准时间与高级业务标题。忽略 .json 等中间文件。"""
    p = Path(path)
    name = p.name
    lower_name = name.lower()
    p_str = str(p).lower()
    ext = p.suffix.lower()

    # 业务层彻底过滤中间数据与元数据文件，不给管理层展示冰冷的 JSON
    if ext in (".json", ".tmp", ".cache") or lower_name.startswith((".git", ".ds_store")):
        return None

    # 1. 判定方向：输入材料 (input) vs 输出交付物 (output)
    is_input = False
    if "upload" in p_str or "/docs/" in p_str or "manual_metrics" in p_str or name in ("metrics.yaml", "notes.md", "notes.txt"):
        is_input = True
    elif ext in (".pdf", ".docx", ".doc", ".yaml", ".yml") and not lower_name.startswith(("周报_", "report_", "compare_", "monitor_", "竞品")):
        is_input = True
    elif any(k in tag for k in ("输入", "原始", "附件", "材料", "底表", "备忘")):
        is_input = True
    direction = "input" if is_input else "output"

    # 2. 判定所属活动 event
    file_event = "all"
    if "/events/" in p_str:
        try:
            parts = Path(path).parts
            if "events" in parts:
                idx = parts.index("events")
                if idx + 1 < len(parts):
                    file_event = parts[idx + 1]
        except Exception:
            pass

    # 3. 判定业务任务分类
    if any(k in lower_name for k in ("ppt", "report", "四段式", "复盘", "总结报告")):
        task = "展会复盘与汇报"
        icon_type = "presentation" if ext.startswith(".ppt") else "document"
    elif any(k in lower_name for k in ("monitor", "监测", "舆情", "媒体")):
        task = "全网媒体监测"
        icon_type = "radar"
    elif any(k in lower_name for k in ("compare", "对比", "雷达", "roi")):
        task = "跨展会横向对比"
        icon_type = "chart" if ext.startswith(".xls") else "document"
    elif any(k in lower_name for k in ("周报", "weekly", "闭环", "事项", "待办")):
        task = "事项闭环周报"
        icon_type = "clipboard"
    elif any(k in lower_name for k in ("竞品", "同行", "矩阵", "competitor", "宇树", "逐际", "星动纪元")):
        task = "中国机器人同行排查"
        icon_type = "bot"
    elif ext == ".zip":
        title = f"全套业务交付成果离线归档包 ({file_event.upper()})"
    elif is_input:
        task = "原始业务输入资料"
        icon_type = "document" if ext == ".pdf" else "paperclip"
    else:
        task = "综合商业分析成果"
        icon_type = "document"

    # 4. 生成专业、人类易读的高级业务标题名 (Human-Readable Business Deliverable Title)
    if ext == ".pptx":
        title = "16:9 展会商业复盘与战略决策 PPT"
    elif "report" in lower_name and ext == ".md":
        title = "四段式高管复盘深度分析报告 (基本盘/高光/卡点/动作)"
    elif any(k in lower_name for k in ("competitor", "竞品")):
        title = "中国机器人同行参展机型与实证全景矩阵"
    elif "monitor" in lower_name and ext == ".md":
        title = "全网媒体声量与海外主流权威评述监测报告"
    elif "leads" in lower_name and ext in (".xlsx", ".xls"):
        title = "展会海外买家 10 类画像分层与高意向跟进表 (已物理脱敏)"
    elif "leads" in lower_name and ext == ".md":
        title = "买家画像与高意向客户跟进策略总结报告"
    elif "compare" in lower_name and ext in (".xlsx", ".xls"):
        title = "跨展会多维对数 ROI 横向对比表 (含动态可调公式)"
    elif "compare" in lower_name and ext == ".md":
        title = "跨展会商业投资回报与媒体性价比综合分析报告"
    elif "周报" in lower_name:
        sub = p.stem.replace("周报_", "")
        title = f"事项闭环与跨部门责任人推进看板 ({sub})"
    elif ext == ".zip":
        title = f"全套业务交付成果离线归档包 ({file_event.upper()})"
    elif is_input:
        if ext == ".pdf":
            clean_stem = p.stem.replace("_", " ").replace("-", " ")
            title = f"展会输入材料: {clean_stem}"
        elif ext in (".xlsx", ".csv"):
            clean_stem = p.stem.replace("_", " ").replace("-", " ")
            title = f"原始线索底表: {clean_stem}"
        else:
            title = f"现场输入资料: {p.name}"
    else:
        title = p.stem.replace("_", " ").title()

    # 5. 标签精细化
    final_tag = tag
    if not final_tag or final_tag in ("成果文件", "交付物"):
        if ext.startswith(".ppt"):
            final_tag = "16:9商务PPT"
        elif ext.startswith(".xls"):
            final_tag = "动态公式Excel"
        elif "monitor" in lower_name:
            final_tag = "全网监测报告"
        elif "竞品" in lower_name:
            final_tag = "同行参展矩阵"
        elif "周报" in lower_name:
            final_tag = "闭环周报看板"
        elif "report" in lower_name:
            final_tag = "四段式复盘"
        elif ext == ".pdf":
            final_tag = "原始PDF材料"
        else:
            final_tag = "交付文档"

    mtime = p.stat().st_mtime if p.exists() else datetime.now().timestamp()
    dt = datetime.fromtimestamp(mtime)
    time_str = dt.strftime("%Y-%m-%d %H:%M")
    short_time = dt.strftime("%m-%d %H:%M")

    return {
        "id": p.stem + "_" + uuid.uuid4().hex[:4],
        "name": name,
        "title": title,
        "path": str(p),
        "ext": ext,
        "event": file_event,
        "direction": direction,
        "direction_label": "输入材料" if direction == "input" else "交付成果",
        "task": task,
        "tag": final_tag,
        "icon_type": icon_type,
        "size": f"{p.stat().st_size / 1024:.1f} KB" if p.exists() else "0 KB",
        "time": time_str,
        "short_time": short_time,
        "mtime": mtime
    }


def load_vault(event_short: str | None = None) -> dict:
    """全面扫描并分类汇总所有【输入材料】与【输出交付成果】，并按【业务任务】分组与时间排序。"""
    scanned_paths = set()

    # 1. 扫描 outputs/
    for folder in [DATA_ROOT / "outputs", ROOT / "outputs"]:
        if folder.exists():
            for p in folder.glob("*"):
                if p.is_file() and not p.name.startswith("."):
                    scanned_paths.add(p.resolve())

    # 2. 扫描 events/*/outputs/ 和输入材料 events/*/docs/
    for events_folder in (DATA_ROOT / "events",):
        if events_folder.exists():
            for ev in events_folder.iterdir():
                if ev.is_dir():
                    out_p = ev / "outputs"
                    if out_p.exists():
                        for p in out_p.glob("*"):
                            if p.is_file() and not p.name.startswith("."):
                                scanned_paths.add(p.resolve())
                    docs_p = ev / "docs"
                    if docs_p.exists():
                        for p in docs_p.glob("*"):
                            if p.is_file() and not p.name.startswith("."):
                                scanned_paths.add(p.resolve())
                    for spec in ("metrics.yaml", "notes.md", "notes.txt"):
                        fp = ev / spec
                        if fp.exists():
                            scanned_paths.add(fp.resolve())

    # 4. 扫描 uploads/ 目录
    for up in [UPLOAD_DIR, DATA_DIR / "uploads", DATA_ROOT / "uploads"]:
        if up.exists():
            for p in up.glob("*"):
                if p.is_file() and not p.name.startswith("."):
                    scanned_paths.add(p.resolve())

    # 5. 扫描工作区根目录下的业务 PDF / 周报 / 说明
    for p in [DATA_ROOT, ROOT]:
        if p.exists():
            for f in p.glob("*"):
                if f.is_file() and f.suffix.lower() in (".pdf", ".docx", ".xlsx", ".md") and not f.name.startswith("."):
                    if any(kw in f.name for kw in ("AGIBOT", "Web Summit", "Weekly", "周报", "总结")):
                        scanned_paths.add(f.resolve())

    # 6. 读取已记录的 VAULT_FILE 中的文件
    if VAULT_FILE.exists():
        try:
            saved = json.loads(VAULT_FILE.read_text(encoding="utf-8"))
            for s in saved:
                if s.get("path") and Path(s["path"]).exists():
                    scanned_paths.add(Path(s["path"]).resolve())
        except Exception:
            pass

    # 分类打包
    inputs = []
    outputs = []
    seen_names = set()
    for p in scanned_paths:
        try:
            if not p.is_file():
                continue
            item = classify_file(p)
            if not item:
                continue
            dedup_key = f"{item['direction']}_{item['name']}_{item['size']}"
            if dedup_key in seen_names:
                continue
            seen_names.add(dedup_key)
            if item["direction"] == "input":
                inputs.append(item)
            else:
                outputs.append(item)
        except Exception:
            pass

    # 严格按时间倒序排列 (最新的在最前，带精准时间戳)
    inputs.sort(key=lambda x: x["mtime"], reverse=True)
    outputs.sort(key=lambda x: x["mtime"], reverse=True)

    # 按照任务分组
    by_task = {}
    for item in (outputs + inputs):
        t = item["task"]
        if t not in by_task:
            by_task[t] = {"outputs": [], "inputs": []}
        if item["direction"] == "output":
            by_task[t]["outputs"].append(item)
        else:
            by_task[t]["inputs"].append(item)

    # 如果指定了活动，额外提供特定活动的筛选集
    event_inputs = [i for i in inputs if i["event"] in (event_short, "all")] if event_short and event_short not in ("all", "auto") else inputs
    event_outputs = [o for o in outputs if o["event"] in (event_short, "all")] if event_short and event_short not in ("all", "auto") else outputs

    return {
        "inputs": inputs,
        "outputs": outputs,
        "event_inputs": event_inputs,
        "event_outputs": event_outputs,
        "by_task": by_task,
        "total_inputs": len(inputs),
        "total_outputs": len(outputs),
        "flat": outputs + inputs
    }


def record_vault_file(path: Path, tag: str = "成果文件") -> dict:
    p = Path(path)
    entry = classify_file(p, tag=tag)
    if not entry:
        return {}
    existing = []
    if VAULT_FILE.exists():
        try:
            existing = json.loads(VAULT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing.insert(0, entry)
    seen = set()
    dedup = []
    for item in existing:
        ipath = item.get("path")
        if ipath and ipath not in seen and Path(ipath).exists():
            seen.add(ipath)
            dedup.append(item)
    try:
        VAULT_FILE.write_text(json.dumps(dedup, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"保存资产归档失败: {e}")
    return entry


# ---------------- 核心 Agent 业务调度
def is_weekly_input(text: str) -> bool:
    t = text.strip()
    if "周报" in t:
        return True
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    bullet_lines = sum(1 for ln in lines if ln.startswith(("-", "*", "•", "1.", "2.", "3.", "4.", "5.")))
    weekly_keywords = ["验收", "跟进", "审批", "签署", "合同", "物料", "节点", "待办", "对接", "闭环", "凭证", "进度", "推进", "备件", "发票", "名单"]
    keyword_hits = sum(1 for kw in weekly_keywords if kw in t)
    return (bullet_lines >= 2 and keyword_hits >= 1) or keyword_hits >= 3


def should_search_web(query: str) -> bool:
    q = query.lower()
    search_triggers = ["搜索", "检索", "外网", "报道", "最新", "新闻", "舆情", "媒体", "新华社", "福布斯", "查一下", "找一下", "社媒", "评价", "谁参展", "法规", "动态", "怎么样"]
    return any(w in q for w in search_triggers)


class AgentDispatcher:
    def __init__(self):
        self.reload_config()

    def reload_config(self):
        self.cfg = load_config()
        self.cfg["_data_root"] = str(DATA_ROOT)

    def process_message(
        self,
        message: str,
        action: str | None = None,
        event_short: str = "auto",
        files: list[dict] | None = None,
        web_search_enabled: bool = True,
        history: list[dict] | None = None
    ) -> dict:
        event_short, detected_task, ev_label, task_label = detect_event_and_task(message, chosen_event=event_short)
        self.reload_config()
        llm = LLM(self.cfg)
        actual_event = event_short if (DATA_ROOT / "events" / event_short).exists() else ("ifa2026" if ("ifa" in event_short or "berlin" in event_short) else "websummit2026")
        ev_dir = DATA_ROOT / "events" / actual_event
        ev_dir.mkdir(parents=True, exist_ok=True)
        (ev_dir / "docs").mkdir(exist_ok=True)
        (ev_dir / "outputs").mkdir(exist_ok=True)
        msg_clean = message.strip()
        lower_msg = msg_clean.lower()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 解析用户拖拽或上传的附件（支持 PDF / Word / PPT / Excel / CSV / Markdown 等）
        parsed_files = []
        uploaded_vault_entries = []
        files_text_block = ""
        if files:
            import base64
            for f in files:
                try:
                    data_str = f.get("data", "")
                    raw_bytes = base64.b64decode(data_str.split(",")[-1] if "," in data_str else data_str)
                    parsed = parse_uploaded_file(f.get("name", "未命名文件"), raw_bytes, UPLOAD_DIR)
                    parsed_files.append(parsed)
                    v_entry = record_vault_file(Path(parsed["saved_path"]), tag="已解析上传材料")
                    uploaded_vault_entries.append(v_entry)
                except Exception as e:
                    print(f"解析附件出错: {e}")
            if parsed_files:
                files_text_block = "\n\n".join([
                    f"【已解析上传材料: {p['filename']} ({p['preview']})】\n{p['text']}"
                    for p in parsed_files
                ])

        # 组合消息与附件上下文
        combined_text = (msg_clean + ("\n\n" + files_text_block if files_text_block else "")).strip()
        if not combined_text and parsed_files:
            combined_text = f"请基于我刚刚上传的 {len(parsed_files)} 份业务材料进行深度解析，提炼关键数据、事实与后续推进建议。"

        # ---------------- 意图智能分发
        # 0. 对话式精准修订与补充 (用户在对话框中指令修改 PPT/报告/竞品/周报，或拖入文件进行增补)
        is_edit, edit_domain = is_edit_or_supplement_request(msg_clean, uploaded_vault_entries)
        if is_edit and (not action or action in ("report", "competitors", "weekly")):
            try:
                res_edit = apply_conversational_edit(
                    ev_dir,
                    msg_clean,
                    uploaded_vault_entries,
                    llm,
                    self.cfg,
                    record_vault_file
                )
                return res_edit
            except Exception as e:
                logger.warning(f"对话式修订执行异常: {e}")

        # 1. 闭环周报（无论是否包含“周报”字样，只要输入多条事项或勾选周报动作，立即执行闭环梳理）
        if action == "weekly" or is_weekly_input(combined_text):
            try:
                res = weekly.run_weekly(combined_text, self.cfg, llm, DATA_ROOT / "weekly")
                md_path = DATA_ROOT / "outputs" / f"周报_{weekly.default_week_label()}.md"
                md_path.parent.mkdir(exist_ok=True)
                md_path.write_text(res["markdown"], encoding="utf-8")
                v_entry = record_vault_file(md_path, tag="闭环周报")
                all_files = uploaded_vault_entries + [v_entry]

                # 提取追问与建议
                suggestions = [
                    "为未闭环事项补齐验收凭证或单号",
                    "在本地 Markdown 中打开周报全文",
                    "把维也纳/BETT 下一步行动加入催办日程"
                ]
                return {
                    "text": res["markdown"],
                    "items": res.get("items", []),
                    "asks": res.get("asks", []),
                    "files": all_files,
                    "input_files": uploaded_vault_entries,
                    "output_files": [v_entry],
                    "event": event_short,
                    "event_label": ev_label,
                    "task_type": "weekly",
                    "task_label": "事项闭环周报",
                    "suggestions": suggestions,
                    "time": now_str,
                    "model": llm.model
                }
            except Exception as e:
                return {"text": format_error_msg(e), "time": now_str}

        # 2. 全网活动监测（联网深度抓取媒体深度评论）
        elif action == "monitor" or ("监测" in msg_clean or "福布斯" in msg_clean or "舆情" in msg_clean or "媒体" in msg_clean or "报道" in msg_clean):
            try:
                searcher = Searcher(self.cfg, llm)
                fetcher = Fetcher(self.cfg)
                r = monitor.run_monitor(ev_dir, self.cfg, llm, searcher, fetcher, max_queries=16, max_articles=20)
                md_path = ev_dir / "outputs" / "monitor_latest.md"
                v_entry = record_vault_file(md_path, tag="全网监测报告")
                all_files = uploaded_vault_entries + [v_entry]

                s = r["stats"]
                watchlist = r.get("watchlist", [])
                unfound = [w for w in watchlist if w.get("status") != "hit" and not w.get("hits")]
                found = [w for w in watchlist if w.get("status") == "hit" or w.get("hits")]

                summary = f"### {r['event']['name']} 全网媒体监测已完成\n\n"
                summary += f"- **总计检索并确认相关报道**：`{s['articles_total']}` 篇（海外 `{s['articles_overseas']}` 篇 / 国内 `{s['articles_domestic']}` 篇）\n"
                summary += f"- **覆盖国家**：`{s['countries_count']}` 个国家\n"
                summary += f"- **目标重点媒体检索命中**：`{len(found)} / {len(watchlist)}` 家\n\n"

                # 按照用户高管工作汇报最高准则：【待核实与未检出排查必须前置置顶，暴露问题与公关补救】
                if unfound:
                    summary += "#### 待核实与未检出排查清单（需人工排查 / 欧洲 PR 补发）\n"
                    for w in unfound:
                        summary += f"- **{w['name']}**：未自动检出公开报道（推测原因：外媒采用别名/付费墙后发布/本地语种检索差异；**建议公关动作**：欧洲 PR 团队定向致信记者索要专访链接或确认通稿发布档期）\n"
                    summary += "\n"

                # 战果与已确认报道后置展开
                critiques = [a for a in r["articles"] if a.get("editorial_critique") or a.get("short_quote")]
                if critiques:
                    summary += "#### 权威媒体深度评论与已确认战果（后置展开）\n"
                    for a in critiques[:6]:
                        q_str = f" *“{a['short_quote']}”*" if a.get("short_quote") else ""
                        critique_text = a.get('editorial_critique') or a.get('focus') or ''
                        summary += f"- **{a['outlet']}** ({a.get('country','全球')}): {critique_text}{q_str}\n"

                suggestions = [
                    "一键编译 16:9 展会总结商务 PPT",
                    "横向对比 Web Summit 与 IFA 的媒体声量",
                    "提炼外媒核心好评用于国内新闻通稿"
                ]
                return {
                    "text": summary,
                    "articles": r["articles"],
                    "stats": s,
                    "files": all_files,
                    "input_files": uploaded_vault_entries,
                    "output_files": [v_entry],
                    "event": event_short,
                    "event_label": ev_label,
                    "task_type": "monitor",
                    "task_label": "全网媒体声量监测",
                    "suggestions": suggestions,
                    "time": now_str,
                    "model": llm.model
                }
            except Exception as e:
                return {"text": format_error_msg(e), "time": now_str}

        # 3. 四段式报告 (PPTX + MD) - 严格遵循高管汇报 Skill (Failures Front-loaded)
        elif action == "report" or ("报告" in msg_clean or "ppt" in lower_msg or "幻灯片" in msg_clean or "四段式" in msg_clean):
            try:
                sections = report.build_sections(ev_dir, self.cfg, llm)
                files_out = report.render_all(ev_dir, self.cfg, sections)
                v1 = record_vault_file(files_out["pptx"], tag="16:9商务PPT")
                v2 = record_vault_file(files_out["md"], tag="四段式完整报告")
                all_files = uploaded_vault_entries + [v1, v2]

                gaps_count = len(sections.get("data_gaps", []))
                text = f"### {ev_dir.name} 高管决策与战略复盘报告已编译生成\n\n"
                text += f"- **01 基本情况与核心量化战果**：{sections.get('basic',{}).get('headline','科技峰会首秀')}\n"
                text += f"- **02 待核实与未检出排查（前置暴露）**：优先暴露 {gaps_count} 项待补齐数据凭证与未检出重点媒体排查清单，便于管理层迅速决策\n"
                text += f"- **03 权威外媒深度评述与现场高光**：收录 Forbes、路透社原声引语及 9 台 A3 群舞现场特写\n"
                text += f"- **04 战略复盘与高管决策闭环**：按 Minto 金字塔原理输出绑定完成标准 (Done Criteria) 的下一步推演\n\n"
                text += f"已编译生成 **16:9 商务演示级 PPT (Failures Front-loaded 结构)** 及完整 Markdown 文档，点击下方卡片可在本地 PowerPoint/Word 中直接打开！"

                suggestions = [
                    "在本地 PowerPoint 中直接打开幻灯片",
                    "在访达 (Finder) 中显示 PPT 演示文稿",
                    "查看复盘第 2 部分需人工核对的未检出项"
                ]
                return {
                    "text": text,
                    "files": all_files,
                    "input_files": uploaded_vault_entries,
                    "output_files": [v1, v2],
                    "event": event_short,
                    "event_label": ev_label,
                    "task_type": "report",
                    "task_label": "展会复盘与商务PPT",
                    "suggestions": suggestions,
                    "time": now_str,
                    "model": llm.model
                }
            except Exception as e:
                return {"text": format_error_msg(e), "time": now_str}

        # 4. 横向对比 (对数归一化 + 雷达图 + Excel 动态公式)
        elif action == "compare" or ("对比" in msg_clean or "哪场" in msg_clean or "更值" in msg_clean or "roi" in lower_msg or "雷达图" in msg_clean):
            try:
                out = compare.run_compare(DATA_ROOT / "events", self.cfg, llm, ["websummit2026", "ifa2026"], narrate=True)
                exp = compare.export_all(out, DATA_ROOT / "outputs")
                v1 = record_vault_file(exp["xlsx"], tag="动态公式Excel对比表")
                v2 = record_vault_file(exp["md"], tag="横向对比结论")
                all_files = uploaded_vault_entries + [v1, v2]

                text = f"### 活动传播与商业价值横向对比（对数归一化公平评估）\n\n"
                text += f"**核心结论**：{out.get('narrative', '对比分析已完成')}\n\n"
                text += f"- **已导出 Excel**：得分为原生 Excel 动态公式，随时可调整触达/质量/转化权重。\n"
                text += f"- **雷达图维度**：对数尺度消除百万曝光对万级专业展的量级压制。\n"

                suggestions = [
                    "在本地 Excel 中直接打开公式对比表",
                    "调整商业转化与政企采购权重重新打分",
                    "查看 Web Summit 独有高光事件清单"
                ]
                return {
                    "text": text,
                    "compare_result": out["result"],
                    "events": out["events"],
                    "files": all_files,
                    "input_files": uploaded_vault_entries,
                    "output_files": [v1, v2],
                    "event": event_short,
                    "event_label": ev_label,
                    "task_type": "compare",
                    "task_label": "跨展会横向对比",
                    "suggestions": suggestions,
                    "time": now_str,
                    "model": llm.model
                }
            except Exception as e:
                return {"text": format_error_msg(e), "time": now_str}

        # 5. 中国机器人竞品排查
        elif action == "competitors" or any(k in msg_clean for k in ("竞品", "同行", "宇树", "逐际", "星动纪元", "机器人展区", "参展证据", "展品机型", "同行矩阵", "机型")):
            try:
                searcher = Searcher(self.cfg, llm)
                fe = Fetcher(self.cfg)
                res = customers.scan_competitors(ev_dir, self.cfg, llm, searcher, fe)
                out_p = DATA_ROOT / "outputs" / f"竞品矩阵_{ev_dir.name}.md"
                md_content = customers.competitor_matrix_md([res])
                out_p.write_text(md_content, encoding="utf-8")
                v_entry = record_vault_file(out_p, tag="同行参展矩阵")
                all_files = uploaded_vault_entries + [v_entry]

                suggestions = [
                    "在本地 Markdown 中打开完整竞品矩阵",
                    "深入检索宇树与逐际在欧洲参展的最新动态",
                    "提炼 A3/X2 对比竞品的差异化优势"
                ]
                return {
                    "text": md_content,
                    "files": all_files,
                    "input_files": uploaded_vault_entries,
                    "output_files": [v_entry],
                    "event": event_short,
                    "event_label": ev_label,
                    "task_type": "competitor",
                    "task_label": "中国机器人同行排查",
                    "suggestions": suggestions,
                    "time": now_str,
                    "model": llm.model
                }
            except Exception as e:
                return {"text": format_error_msg(e), "time": now_str}

        # 6. 客户线索画像分层与买家筛选
        is_negation_or_clarification = any(neg in msg_clean for neg in ("不是", "不要", "不对", "别", "无需", "并不", "换成", "转到", "我是说", "我说的是", "是那个", "那个"))
        
        # 检查是否有上传的线索附件表格
        leads_file = None
        if uploaded_vault_entries:
            for f in uploaded_vault_entries:
                if Path(f["path"]).suffix.lower() in (".xlsx", ".xls", ".csv", ".txt"):
                    leads_file = Path(f["path"])
                    break

        has_pasted_leads = any(k in lower_msg for k in ("公司", "职位", "职务", "国家", "巡检", "采购", "意向", "装配", "测试", "bosch", "siemens", "university", "tech")) and len(msg_clean.splitlines()) >= 2
        wants_demo = any(k in lower_msg for k in ("样板", "演示", "demo", "继续", "体验", "先跑", "试一下", "好的", "开始"))
        
        # 严格判断是否执行线索数据清洗（否定句或澄清句绝不误触发）
        is_leads_task = (not is_negation_or_clarification) and (
            (action == "leads") or 
            has_pasted_leads or 
            (leads_file is not None) or 
            (any(k in msg_clean for k in ("线索分层", "买家画像", "线索清洗", "客户脱敏")) and (wants_demo or has_pasted_leads))
        )

        if is_leads_task:
            # 如果用户明确触发了分层动作，但既未上传文件也未粘贴手记：实时调用 Claude Opus 5.5 进行专业引导，绝不返回死板硬编码模板
            if not leads_file and not has_pasted_leads and not wants_demo:
                lead_sys = f"""你是 Zoe 欧洲市场部的活动复盘与商业智能决策总监级助理。
用户是欧洲市场部主管 Zoe，她当前在【{ev_label}】工作区内想要推进客户线索画像分层。
但当前尚未上传名单表格文件，也未粘贴现场手记。

请以专业、干练、务实的高级专家口吻回复 Zoe：
1. 明确表明系统已就绪为【{ev_label}】进行本地物理脱敏（严格符合德国 DSGVO 与欧盟法规，姓名/邮箱/电话在本地抹除）与 10 类画像分层。
2. 告知 Zoe 可以直接将现场沟通笔记或名片备忘粘贴在对话框，或直接拖入 Excel/CSV 表格。
3. 若手头暂无现成名单，也可直接点击下方快捷建议或回复“跑样板”，一键使用该展会的实战标杆买家数据先行体验分层成果。
严禁使用假大空AI套话。"""
                text = llm.chat(lead_sys, msg_clean or "请对我上传的展会买家名单进行本地脱敏，并划分 10 类画像与高意向客户跟进策略。")
                text = sanitize_ai_cliches(text)
                suggestions = [
                    f"使用 {ev_label} 标杆买家数据立即分层",
                    "我直接粘贴 5 条客户现场洽谈手记",
                    "查看 10 类客户画像分类标准与跟进策略"
                ]
                return {
                    "text": text,
                    "files": uploaded_vault_entries,
                    "input_files": uploaded_vault_entries,
                    "output_files": [],
                    "event": event_short,
                    "event_label": ev_label,
                    "task_type": "leads",
                    "task_label": "客户线索画像分层",
                    "suggestions": suggestions,
                    "time": now_str,
                    "model": llm.model
                }

            try:
                if has_pasted_leads and not leads_file:
                    res = customers.classify_leads_from_text(ev_dir, msg_clean, self.cfg, llm)
                else:
                    res = customers.classify_leads(ev_dir, self.cfg, llm, leads_file=leads_file)

                v1 = record_vault_file(res["xlsx"], tag="客户线索画像与分层表")
                v2 = record_vault_file(res["md"], tag="客户线索分层总结")
                all_files = uploaded_vault_entries + [v1, v2]
                text = f"### {ev_label} 客户线索智能画像分层已完成\n\n"
                text += f"- **已清洗线索数**：`{res['count']}` 条（本地脱敏，姓名/邮箱/电话已物理抹除后安全分析）\n"
                text += f"- **高意向买家**：`{res.get('high_count', 0)}` 家（明确提出试用/采购时间表/报价需求）\n"
                text += f"- **已生成交付成果**：已自动导出标准 10 类画像分层 Excel 与总结 Markdown，可在本地直接打开查看。\n\n"
                if res.get("md") and Path(res["md"]).exists():
                    text += Path(res["md"]).read_text(encoding="utf-8")

                suggestions = [
                    "在本地 Excel 中打开高意向客户名单",
                    "按高校与科研采购拆分销售跟进动作",
                    "核对本地线索 PII 隐私脱敏日志"
                ]
                return {
                    "text": text,
                    "files": all_files,
                    "input_files": uploaded_vault_entries,
                    "output_files": [v1, v2],
                    "event": event_short,
                    "event_label": ev_label,
                    "task_type": "leads",
                    "task_label": "客户线索画像分层",
                    "suggestions": suggestions,
                    "time": now_str,
                    "model": llm.model
                }
            except Exception as e:
                return {"text": format_error_msg(e), "time": now_str}

        # 7. 通用智能商业决策对话（内置 Claude Opus 5.5 旗舰大模型 + 实时外网检索 + 去AI味实战文风）
        else:
            try:
                web_context = ""
                # 如果开启了外网检索且问题包含新闻/外部/法规/竞品动态
                if web_search_enabled and should_search_web(combined_text):
                    try:
                        searcher = Searcher(self.cfg, llm)
                        hits = searcher.search(msg_clean, max_results=4)
                        if hits:
                            web_context = "\n\n【实时外网联网检索结果参考】：\n" + "\n".join([
                                f"- [{h.title}]({h.url}): {h.snippet}" for h in hits
                            ])
                    except Exception as err:
                        print(f"联网检索跳过: {err}")

                sys_prompt = f"""你是 Zoe 欧洲市场部的活动复盘与商业智能决策总监级助理。
你熟悉机器人产品矩阵（A3、X2、G2、D1、Luka）以及欧洲各场展会活动（Web Summit Lisbon、IFA Berlin、CES、MWC、Hannover 等）。
你正在协助欧洲市场部主管 Zoe。

【当前聚焦的工作区视角】：
- 当前活动：{ev_label} ({event_short})
- 举办地与场馆：{ev_dir.name}

【Zoe 实战文风 · 严禁AI套话规范】：
{DE_AI_RULES}

请以专业、务实、干练的高级市场运营专家口吻回答。
1. 如果用户在纠正、澄清、否定上一轮话题或指代特定展会（例如“不是...，是...那个”、“切换到柏林/IFA”），请立即明确确认理解，并迅速将焦点切换到用户所指的目标展会或主题上，绝不机械重复之前的模板。
2. 结合实际产品与欧洲海外商业化场景（如德国 DACH 区域制造业客户如 Miele、Brose、Siemens，或展会复盘/媒体声量）给出切实建议。
3. 结论与硬核事实先行，多用实词动词与数字，绝不使用空洞排比、抒情与假大空套话。
4. 提供 3 条针对该展会或下一步行动的专业推进建议。"""

                history_block = ""
                if history:
                    history_block = "\n\n【本轮工作区近期上下文对话回顾】：\n"
                    for h in history[-4:]:
                        role = "用户" if h.get("role") == "user" else "AI助手"
                        c_snip = (h.get("content") or "").strip()[:500]
                        history_block += f"- {role}: {c_snip}\n"

                user_prompt = f"【用户最新指令】：\n{combined_text}\n{history_block}{web_context}"
                ans = llm.chat(sys_prompt, user_prompt)
                ans = sanitize_ai_cliches(ans)

                # 生成下一步智能补充建议
                suggestions = [
                    f"查看 {ev_label} 商业复盘报告与 PPT",
                    f"启动针对 {ev_label} 的全网媒体与竞品排查",
                    f"梳理 {ev_label} 的意向买家与跟进策略"
                ]
                return {
                    "text": ans,
                    "files": uploaded_vault_entries,
                    "input_files": uploaded_vault_entries,
                    "output_files": [],
                    "event": event_short,
                    "event_label": ev_label,
                    "task_type": detected_task,
                    "task_label": task_label,
                    "suggestions": suggestions,
                    "time": now_str,
                    "model": llm.model
                }
            except Exception as e:
                return {"text": format_error_msg(e), "time": now_str}


# ---------------- 桌面 HTTP API 服务
dispatcher = AgentDispatcher()

class DesktopApiHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        live_ui = Path.home() / ".zoe" / "live_patch" / "ui"
        ui_dir = live_ui if (live_ui / "index.html").exists() else (ROOT / "ui")
        super().__init__(*args, directory=str(ui_dir), **kwargs)

    def log_message(self, format, *args):
        pass  # 保持终端静默

    def do_POST(self):
        try:
            length = int(self.headers.get("content-length", 0))
            body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            data = json.loads(body) if body else {}

            if self.path == "/api/chat":
                msg = data.get("message", "")
                act = data.get("action")
                ev = data.get("event", "websummit2026")
                files = data.get("files", [])
                web_search = data.get("web_search", True)
                history = data.get("history", [])
                res = dispatcher.process_message(
                    msg,
                    action=act,
                    event_short=ev,
                    files=files,
                    web_search_enabled=web_search,
                    history=history
                )
                self._send_json(res)

            elif self.path == "/api/open_file":
                import subprocess
                filepath = data.get("path", "")
                if filepath and os.path.exists(filepath):
                    if sys.platform == "darwin":
                        subprocess.Popen(["open", str(filepath)])
                    elif sys.platform == "win32":
                        os.startfile(filepath)
                    self._send_json({"ok": True, "message": "已在系统原生应用中打开"})
                else:
                    self._send_json({"ok": False, "message": "文件不存在"})

            elif self.path == "/api/reveal_file":
                import subprocess
                filepath = data.get("path", "")
                if filepath and os.path.exists(filepath):
                    if sys.platform == "darwin":
                        subprocess.Popen(["open", "-R", str(filepath)])
                    elif sys.platform == "win32":
                        subprocess.Popen(["explorer", f"/select,{filepath}"])
                    self._send_json({"ok": True, "message": "已在系统访达/文件资源管理器中定位"})
                else:
                    self._send_json({"ok": False, "message": "文件不存在"})

            elif self.path == "/api/open_folder":
                import subprocess
                ev = data.get("event")
                if ev and (DATA_ROOT / "events" / ev / "outputs").exists():
                    folder = str(DATA_ROOT / "events" / ev / "outputs")
                else:
                    folder = data.get("path") or str(DATA_ROOT / "outputs")
                if sys.platform == "darwin":
                    subprocess.Popen(["open", str(folder)])
                elif sys.platform == "win32":
                    os.startfile(folder)
                self._send_json({"ok": True, "path": folder})

            elif self.path == "/api/package_outputs":
                import subprocess, zipfile
                ev = data.get("event") or "websummit2026"
                downloads_dir = Path.home() / "Downloads"
                downloads_dir.mkdir(parents=True, exist_ok=True)
                zip_filename = f"Zoe_{ev}_deliverables.zip"
                target_zip = downloads_dir / zip_filename
                outputs_zip = DATA_ROOT / "outputs" / zip_filename

                files_packed = []
                with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                    ev_dir = DATA_ROOT / "events" / ev / "outputs"
                    if ev_dir.exists():
                        for f in ev_dir.iterdir():
                            if f.is_file() and not f.name.startswith(".") and not f.name.endswith(".zip"):
                                zf.write(f, arcname=f"{ev}/{f.name}")
                                files_packed.append(f.name)
                    docs_dir = DATA_ROOT / "events" / ev / "docs"
                    if docs_dir.exists() and not files_packed:
                        for f in docs_dir.iterdir():
                            if f.is_file() and not f.name.startswith("."):
                                zf.write(f, arcname=f"{ev}/inputs/{f.name}")
                                files_packed.append(f.name)
                    gen_dir = DATA_ROOT / "outputs"
                    if gen_dir.exists():
                        for f in gen_dir.iterdir():
                            if f.is_file() and not f.name.startswith(".") and not f.name.endswith(".zip"):
                                if ev in f.name or "compare" in f.name or "周报" in f.name:
                                    zf.write(f, arcname=f"global/{f.name}")
                                    files_packed.append(f.name)

                try:
                    shutil.copy2(target_zip, outputs_zip)
                except Exception:
                    pass

                if sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", str(target_zip)])
                elif sys.platform == "win32":
                    subprocess.Popen(["explorer", f"/select,{target_zip}"])

                size_mb = round(target_zip.stat().st_size / (1024 * 1024), 2)
                self._send_json({
                    "ok": True,
                    "path": str(target_zip),
                    "filename": zip_filename,
                    "size_mb": size_mb,
                    "count": len(files_packed),
                    "files": files_packed[:8],
                    "message": f"成果包已生成至访达并在『下载』文件夹中高亮定位！"
                })

            elif self.path == "/api/save_config":
                upd = data.get("config", {})
                for k, v in upd.items():
                    os.environ[k] = str(v)

                for cfg_dir in [Path.home() / ".zoe", Path.home() / ".agibot"]:
                    try:
                        cfg_dir.mkdir(parents=True, exist_ok=True)
                        p_env = cfg_dir / ".env"
                        lines = p_env.read_text(encoding="utf-8").splitlines() if p_env.exists() else []
                        seen = set()
                        for i, ln in enumerate(lines):
                            k = ln.split("=", 1)[0].strip()
                            if k in upd:
                                lines[i] = f"{k}={upd[k]}"
                                seen.add(k)
                        lines += [f"{k}={v}" for k, v in upd.items() if k not in seen]
                        p_env.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    except Exception as e:
                        logger.warning(f"保存 {cfg_dir}/.env 异常: {e}")

                for candidate in [ROOT / ".env", Path("/Users/mima0000/Desktop/zoe/event-agent/.env")]:
                    try:
                        if candidate.exists() and os.access(candidate, os.W_OK):
                            lines = candidate.read_text(encoding="utf-8").splitlines()
                            seen = set()
                            for i, ln in enumerate(lines):
                                k = ln.split("=", 1)[0].strip()
                                if k in upd:
                                    lines[i] = f"{k}={upd[k]}"
                                    seen.add(k)
                            lines += [f"{k}={v}" for k, v in upd.items() if k not in seen]
                            candidate.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    except Exception:
                        pass

                dispatcher.reload_config()
                self._send_json({"ok": True, "message": "配置已成功保存！"})

            elif self.path == "/api/save_sessions":
                sess_list = data if isinstance(data, list) else (data.get("sessions", []) if isinstance(data, dict) else [])
                save_sessions(sess_list)
                self._send_json({"ok": True})

            elif self.path == "/api/refresh_events":
                cat = get_events_catalog(root_dir=DATA_ROOT, force_refresh=True)
                self._send_json({"ok": True, "catalog": cat, "message": "目标活动日历已同步更新至最新状态"})

            elif self.path in ("/api/add_custom_event", "/api/add_event"):
                name = (data.get("name") or "").strip()
                start = (data.get("start") or data.get("start_date") or "").strip()
                end = (data.get("end") or data.get("end_date") or "").strip()
                venue = (data.get("venue") or "").strip()
                tag = (data.get("tag") or "重点展会").strip()
                files = data.get("files") or []
                if name:
                    try:
                        res = add_custom_event(name, start, end, venue=venue, tag=tag, root_dir=DATA_ROOT, files=files)
                        self._send_json({
                            "ok": True,
                            "catalog": res.get("catalog"),
                            "short": res.get("short"),
                            "event_short": res.get("short"),
                            "name": res.get("name"),
                            "saved_files": res.get("saved_files", []),
                            "message": f"活动 {name} 添加成功并已初始化专属工作区"
                        })
                    except OSError as oe:
                        self._send_json({
                            "ok": False,
                            "message": f"无法写入工作区目录（请先将 Zoe 拖入「应用程序」或桌面后再运行）：{oe}"
                        })
                else:
                    self._send_json({"ok": False, "message": "活动名称不能为空"})

            elif self.path == "/api/apply_update":
                from agent.updater import apply_update
                res = apply_update(ROOT)
                self._send_json(res)

            elif self.path == "/api/restart_app":
                import subprocess
                self._send_json({"ok": True, "message": "正在重新启动 Zoe..."})

                def _do_restart():
                    time.sleep(0.5)
                    # 若在 .app 内部运行，直接通过 launcher 重启；否则用当前 python 重启
                    app_launcher = ROOT.parent.parent / "MacOS" / "launcher"
                    if app_launcher.exists():
                        subprocess.Popen([str(app_launcher)], start_new_session=True)
                    else:
                        subprocess.Popen([sys.executable, str(ROOT / "desktop_app.py")], cwd=str(ROOT), start_new_session=True)
                    try:
                        for w in list(webview.windows):
                            w.destroy()
                    except Exception:
                        pass
                    os._exit(0)

                threading.Thread(target=_do_restart, daemon=True).start()

            else:
                self.send_error(404)
        except Exception as e:
            logger.exception(f"HTTP do_POST 异常: {e}")
            try:
                self._send_json({
                    "text": f"服务处理异常: {e}",
                    "time": datetime.now().strftime("%H:%M")
                })
            except Exception:
                pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/sessions":
            self._send_json(load_sessions())
        elif self.path.startswith("/api/vault"):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            ev = params.get("event", [None])[0]
            self._send_json(load_vault(event_short=ev))
        elif self.path == "/api/events_calendar":
            self._send_json(get_events_catalog(root_dir=DATA_ROOT))
        elif self.path.startswith("/api/workspace_status"):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            ev = params.get("event", ["websummit2026"])[0]
            from agent.workspace_hub import get_workspace_task_summary
            self._send_json(get_workspace_task_summary(DATA_ROOT, event_short=ev))
        elif self.path.startswith("/api/download_file"):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            rel_path = params.get("path", [""])[0]
            if not rel_path:
                self.send_error(400, "Missing path parameter")
                return
            target_path = Path(rel_path)
            if not target_path.is_absolute():
                target_path = DATA_ROOT / target_path
            if not target_path.exists() or not target_path.is_file():
                self.send_error(404, "File not found")
                return

            filename = target_path.name
            file_size = target_path.stat().st_size

            ext = target_path.suffix.lower()
            content_types = {
                ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".pdf": "application/pdf",
                ".md": "text/markdown; charset=utf-8",
                ".txt": "text/plain; charset=utf-8",
                ".json": "application/json",
                ".zip": "application/zip",
            }
            ctype = content_types.get(ext, "application/octet-stream")

            self.send_response(200)
            self.send_header("Content-Type", ctype)
            quoted_filename = urllib.parse.quote(filename)
            self.send_header("Content-Disposition", f'attachment; filename="{quoted_filename}"; filename*=UTF-8\'\'{quoted_filename}')
            self.send_header("Content-Length", str(file_size))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            with open(target_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 256)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        elif self.path.startswith("/api/download_all_outputs"):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            ev = params.get("event", ["websummit2026"])[0]
            import io, zipfile
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                ev_dir = DATA_ROOT / "events" / ev / "outputs"
                if ev_dir.exists():
                    for f in ev_dir.iterdir():
                        if f.is_file() and not f.name.startswith("."):
                            zf.write(f, arcname=f"events/{ev}/{f.name}")
                gen_dir = DATA_ROOT / "outputs"
                if gen_dir.exists():
                    for f in gen_dir.iterdir():
                        if f.is_file() and not f.name.startswith("."):
                            zf.write(f, arcname=f"outputs/{f.name}")

            val = zip_buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            zip_name = f"Zoe_{ev}_deliverables.zip"
            self.send_header("Content-Disposition", f'attachment; filename="{zip_name}"')
            self.send_header("Content-Length", str(len(val)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(val)
            return
        elif self.path == "/api/check_update":
            from agent.updater import check_for_update
            self._send_json(check_for_update(ROOT))
        elif self.path == "/api/get_config":
            from agent.updater import get_local_version
            dispatcher.reload_config()
            cfg = dispatcher.cfg
            ver = get_local_version(ROOT)
            safe_cfg = {
                "provider": cfg["llm"]["provider"],
                "model": cfg["llm"]["model"],
                "base_url": cfg["llm"]["base_url"],
                "has_key": bool(cfg["llm"]["api_key"]),
                "search_provider": cfg["search"]["provider"],
                "version": ver["version"],
                "build": ver["build"],
            }
            self._send_json(safe_cfg)
        else:
            super().do_GET()

    def _send_json(self, payload: dict | list):
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(blob)


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

def start_server() -> int:
    httpd = ThreadedTCPServer(("127.0.0.1", 0), DesktopApiHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return port


# ---------------- 启动原生桌面窗口
def launch_window(url: str):
    import subprocess
    # 1. 优先使用 pywebview (macOS Cocoa + WebKit 原生架构)：
    # 与 .app 完美融合，Dock 显示专属图标，菜单栏显示应用名称，完全脱离终端与第三方浏览器依赖
    try:
        window = webview.create_window(
            title="Zoe · 商业智能与活动决策平台",
            url=url,
            width=1280,
            height=860,
            min_size=(1024, 680),
            text_select=True,
            zoomable=True
        )
        webview.start(debug=False)
        os._exit(0)
    except Exception as e:
        logger.warning(f"pywebview 启动受阻: {e}，尝试 Chrome/Edge 独立应用模式")

    # 2. 降级备用：Chrome/Edge --app 原生独立窗口模式
    if sys.platform == "darwin":
        chrome_app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(chrome_app):
            try:
                proc = subprocess.Popen([
                    chrome_app,
                    f"--app={url}",
                    "--user-data-dir=/tmp/agibot_chrome_profile",
                    "--window-size=1280,860",
                    "--window-position=100,50"
                ])
                proc.wait()
                os._exit(0)
            except Exception:
                pass
    elif sys.platform == "win32":
        for browser in [r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]:
            if os.path.exists(browser):
                try:
                    subprocess.Popen([browser, f"--app={url}", "--window-size=1280,860"])
                    return
                except Exception:
                    pass

    # 3. 最终兜底
    import webbrowser
    webbrowser.open(url)

def start_daily_updater():
    def _worker():
        while True:
            try:
                get_events_catalog(root_dir=DATA_ROOT)
            except Exception as e:
                logger.warning(f"每日目标活动自动更新异常: {e}")
            time.sleep(3600)
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def main():
    start_daily_updater()
    port = start_server()
    ui_url = f"http://127.0.0.1:{port}/index.html"
    launch_window(ui_url)
    # 保持主线程常驻
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
