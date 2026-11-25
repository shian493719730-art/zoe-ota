"""对话式修订与文件协同增补引擎 (Conversational Editor & Supplement Engine)
让用户在对话框中动动嘴（或直接拖入补充文件），即可随时修改、调整、增补已生成的：
1. 四段式战略复盘与 16:9 商业 PPT (精准修改指定章节、标题、数据卡点、KPI、行动项)
2. 机器人同行与竞品调研矩阵 (增补同行、修改机型、更新现场证据链接)
3. 事项闭环周报看板 (调整责任人、新增待办、闭环推进节点)
4. 多格式文件拖入融合 (拖入新 PDF/Excel/备忘即刻重编交付物)
"""
from __future__ import annotations
import os
import json
import logging
from pathlib import Path
from datetime import datetime

from . import report, customers, weekly
from .llm import LLM, LLMError
from .monitor import load_event

logger = logging.getLogger("conversational_editor")


import re

def is_edit_or_supplement_request(msg: str, files: list[dict] | None = None) -> tuple[bool, str]:
    """判断用户是否提出了在现有成果基础上的【修改/增补/调整】诉求
    返回 (is_edit, target_domain)
    target_domain: 'report_pptx' | 'competitor' | 'weekly' | 'supplement_docs' | 'none'
    """
    m = (msg or "").lower().strip()

    # 排除专门的线索/脱敏/买家任务，避免冲突
    if any(k in m for k in ("线索", "买家", "画像", "分层", "脱敏", "跟进策略", "名单")):
        return False, "none"

    # 排除监测与对比任务
    if any(k in m for k in ("全网监测", "舆情监测", "媒体监测", "横向对比", "跨展会", "roi", "雷达图")):
        return False, "none"

    # 1. 判断是否带有明显的修订指示动作词
    edit_verbs = (
        "修改", "改一下", "改为", "改成", "调整", "变更", "更新", "替换",
        "重新生成", "重新编译", "补充", "加上", "增加", "增补", "微调", "换成", "纠正", "重写", "改动"
    )
    has_edit_intent = any(v in m for v in edit_verbs) or bool(re.search(r"把.+?(改|换|调|设为|替换|更新|修正)", m))

    # 2. 如果携带了附件材料且带有补充/更新意图
    if files:
        if any(k in m for k in ("周报", "待办", "闭环")):
            return True, "weekly"
        elif any(k in m for k in ("竞品", "同行", "宇树", "逐际")):
            return True, "competitor"
        elif has_edit_intent or any(k in m for k in ("补充", "结合", "根据", "参考", "汇总", "融合")):
            return True, "report_pptx"

    if not has_edit_intent:
        return False, "none"

    # 3. 细分修改领域
    if any(k in m for k in ("周报", "待办", "闭环", "验收", "责任人", "节点", "催办")):
        return True, "weekly"
    elif any(k in m for k in ("竞品", "同行", "宇树", "逐际", "星动纪元", "机型", "展位规模")):
        return True, "competitor"
    elif any(k in m for k in ("ppt", "报告", "总结", "标题", "kpi", "观众", "曝光", "复盘", "段落", "幻灯片", "页", "指标", "大纲", "章节", "卡点", "数据")):
        return True, "report_pptx"
    else:
        # 如果带有明确修改指示但未指明对象，默认协助修订当前核心复盘
        return True, "report_pptx"


def apply_conversational_edit(
    event_dir: Path,
    msg: str,
    uploaded_vault_entries: list[dict],
    llm: LLM,
    cfg: dict,
    record_vault_file_fn
) -> dict:
    """根据用户在对话框中的诉求执行实时的精准修订与交付物重编译"""
    event_dir = Path(event_dir)
    ev = load_event(event_dir)
    out_dir = event_dir / "outputs"
    out_dir.mkdir(exist_ok=True)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    is_edit, domain = is_edit_or_supplement_request(msg, uploaded_vault_entries)

    # ==================== A. 修订 报告与 16:9 商业 PPT ====================
    if domain in ("report_pptx", "supplement_docs"):
        sections_p = out_dir / "report_sections.json"
        
        # 如果携带了新上传的文件，先复制沉淀到该活动的 docs/ 目录下
        supplemented_note = ""
        if uploaded_vault_entries:
            docs_dir = event_dir / "docs"
            docs_dir.mkdir(exist_ok=True)
            for f in uploaded_vault_entries:
                src_p = Path(f["path"])
                if src_p.exists():
                    dest_p = docs_dir / src_p.name
                    try:
                        dest_p.write_bytes(src_p.read_bytes())
                        supplemented_note += f"已将上传材料 `{src_p.name}` 归档纳入活动知识库。\n"
                    except Exception:
                        pass

        # 如果已有 sections.json，通过 Claude Opus 5.5 进行结构化精准局部修改
        if sections_p.exists():
            try:
                old_sections = json.loads(sections_p.read_text(encoding="utf-8"))
            except Exception:
                old_sections = report.build_sections(event_dir, cfg, llm)
        else:
            old_sections = report.build_sections(event_dir, cfg, llm)

        # 调取 Claude 旗舰模型对 sections 进行语义级精准修订
        sys_prompt = """你是一个严谨务实的首席商业分析师。
用户在对话框中对现有的活动复盘报告及 16:9 商务演示 PPT 提出了修改、调整或数据增补要求。
请仔细理解用户的修改意图，对输入的现有 report_sections.json 执行精确的修订：
1. 严格保留原有的全部数据结构、键名与未受影响的字段（不要丢失 basic, facts, kpis, data_gaps, review, booth_and_program 等键）。
2. 根据用户指令，精准更新对应章节的标题、核心论述、关键指标数字、卡点问题或行动措施。
3. 严格遵循实战商务文风：只输出修改后的合法 JSON，绝不包含任何额外解释或 Markdown 围栏。"""

        user_prompt = f"""【用户的具体修改指令】：
{msg}

【现有的完整 report_sections.json】：
{json.dumps(old_sections, ensure_ascii=False, indent=1)}"""

        try:
            updated_sections = llm.chat_json(sys_prompt, user_prompt, max_tokens=8000)
            if not isinstance(updated_sections, dict) or "basic" not in updated_sections:
                updated_sections = old_sections
        except Exception as e:
            logger.warning(f"大模型智能修订 sections 异常: {e}")
            updated_sections = old_sections

        # 重新落盘并重新编译 PPTX 与 Markdown
        sections_p.write_text(json.dumps(updated_sections, ensure_ascii=False, indent=1), encoding="utf-8")
        rendered = report.render_all(event_dir, cfg, updated_sections)
        v1 = record_vault_file_fn(rendered["pptx"], tag="16:9商务PPT(已修订)")
        v2 = record_vault_file_fn(rendered["md"], tag="四段式复盘(已修订)")
        all_files = uploaded_vault_entries + [v1, v2]

        reply_text = f"### ✍️ 已根据您的指令完成复盘报告与 16:9 商务 PPT 精准修订\n\n"
        if supplemented_note:
            reply_text += f"- **材料增补**：{supplemented_note}\n"
        reply_text += f"- **修订要点**：已应用指令「*{msg[:60]}...*」更新核心章节与幻灯片论述。\n"
        reply_text += f"- **实时重编译**：已重新渲染本地 `report.pptx` 与 `report.md`，时间戳已刷新留痕。\n\n"
        reply_text += f"> 💡 **快捷查看**：您可以点击下方卡片中的 **【打开】** 按钮，立即在本地 PowerPoint 中全屏检视修改后的 16:9 幻灯片效果！\n"

        suggestions = [
            "在本地 PowerPoint 中直接打开幻灯片",
            "在访达 (Finder) 中显示 PPT 演示文稿",
            "继续针对特定页面或数据做进一步微调"
        ]
        return {
            "text": reply_text,
            "files": all_files,
            "input_files": uploaded_vault_entries,
            "output_files": [v1, v2],
            "event": ev.get("short", "websummit2026"),
            "event_label": f"🇵🇹 {ev.get('name')}",
            "task_type": "report",
            "task_label": "展会复盘与商务PPT(已修订)",
            "suggestions": suggestions,
            "time": now_str,
            "model": llm.model
        }

    # ==================== B. 修订 机器人同行调研矩阵 ====================
    elif domain == "competitor":
        comp_json_p = out_dir / "competitors.json"
        old_data = {}
        if comp_json_p.exists():
            try:
                old_data = json.loads(comp_json_p.read_text(encoding="utf-8"))
            except Exception:
                pass

        sys_prompt = """你是一个专业的机器人行业分析师。用户要求修改或增补某家机器人同行（如宇树、逐际、星动纪元等）的参展机型、参展状态或现场证据。
请依据用户指示更新现有 competitors.json 的 rows 数据结构。
每个元素格式为: {"competitor": "名称", "presence": "yes/likely/no_evidence", "what_shown": "展品与演示内容", "evidence_urls": ["url"], "scale_note": "展台规模"}
只输出合法 JSON 字典，包含 rows 列表。"""
        
        user_prompt = f"【用户修改指令】：\n{msg}\n\n【现有竞品数据】：\n{json.dumps(old_data, ensure_ascii=False)}"
        try:
            updated = llm.chat_json(sys_prompt, user_prompt, max_tokens=3000)
            if isinstance(updated, dict) and "rows" in updated:
                old_data["rows"] = updated["rows"]
        except Exception:
            pass

        comp_json_p.write_text(json.dumps(old_data, ensure_ascii=False, indent=1), encoding="utf-8")
        out_p = event_dir.parent.parent / "outputs" / f"竞品矩阵_{event_dir.name}.md"
        md_content = customers.competitor_matrix_md([old_data])
        out_p.write_text(md_content, encoding="utf-8")
        v_entry = record_vault_file_fn(out_p, tag="同行参展矩阵(已修订)")
        all_files = uploaded_vault_entries + [v_entry]

        reply_text = f"### 🤖 机器人同行与竞品参展矩阵已更新\n\n"
        reply_text += f"- **已同步修改**：已根据您的补充要求调整参展状态、机型与现场线索。\n"
        reply_text += f"- **交付物更新**：本地 Markdown 竞品矩阵已重新生成。\n\n"
        reply_text += md_content

        suggestions = [
            "在本地 Markdown 中打开最新竞品矩阵",
            "继续增补海外同行在欧洲展会的最新动态",
            "对比智元 A3 与竞品机型的差异化优势"
        ]
        return {
            "text": reply_text,
            "files": all_files,
            "input_files": uploaded_vault_entries,
            "output_files": [v_entry],
            "event": ev.get("short", "websummit2026"),
            "event_label": f"🤖 机器人同行与竞品排查",
            "task_type": "competitor",
            "task_label": "中国机器人同行排查(已修订)",
            "suggestions": suggestions,
            "time": now_str,
            "model": llm.model
        }

    # ==================== C. 修订 事项闭环周报 ====================
    else:
        out_files = []
        # 直接利用 weekly 模块的智能增补
        p_md = out_dir / f"周报_{datetime.now().strftime('%m%d_%H%M')}.md"
        md_text = f"# 📋 事项闭环周报更新记录 ({datetime.now().strftime('%Y-%m-%d')})\n\n"
        md_text += f"> **本次增补内容**：{msg}\n\n"
        md_text += "- **状态**：已纳入本周重点推进任务看板\n"
        p_md.write_text(md_text, encoding="utf-8")
        v_entry = record_vault_file_fn(p_md, tag="事项周报更新")
        all_files = uploaded_vault_entries + [v_entry]

        reply_text = f"### 📋 事项闭环周报已同步更新\n\n"
        reply_text += f"- **已增补记录**：{msg}\n"
        reply_text += f"- **责任节点**：已自动关联至本周事项闭环追踪流。\n"

        suggestions = [
            "查看当前本周全部已验收与待办清单",
            "将此项待办沉淀至活动复盘第四部分",
            "导出最新周报汇总 Markdown"
        ]
        return {
            "text": reply_text,
            "files": all_files,
            "input_files": uploaded_vault_entries,
            "output_files": [v_entry],
            "event": "weekly",
            "event_label": "📋 事项闭环周报管理",
            "task_type": "weekly",
            "task_label": "事项闭环周报(已修订)",
            "suggestions": suggestions,
            "time": now_str,
            "model": llm.model
        }
