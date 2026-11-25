# -*- coding: utf-8 -*-
"""智元机器人 · 卓越高管工作汇报与商务演示 Skill (Executive Work Report & Presentation Skill)
基于世界一流战略咨询与科技大厂高管汇报标准打造：
1. 失败与缺口前置原则 (Failures & Gaps Front-loaded)：未检出媒体、数据缺口、风险卡点置顶，优先暴露问题。
2. 战果与亮点后置展开 (Confirmed Hits Back-loaded)：权威报道、高光数据与深度评论紧随其后。
3. 金字塔原理与结论先行 (BLUF)：每节先有一句话核心定调，再跟支撑数据。
4. 商业推演与下一步闭环：指标必须绑定责任人与倒排完成标准 (Done Criteria)。
"""

from __future__ import annotations
from typing import Any

# 高管汇报去套话与排版强化规则
EXECUTIVE_REPORT_PROMPT_ADDON = """
【高管工作汇报与商务展示黄金规范 - Skill 增强指南】
1. 【结构必须“失败与缺口前置，成果在后”】：
   - 第一优先级：必须第一时间呈现“待核实/未检出重点媒体”与“数据缺口(Data Gaps)”，标明阻碍原因与公关/商务补救措施，方便管理层迅速决策与资源协调。
   - 第二优先级：再呈现已确认命中的权威报道、媒体原声引语、现场参与战果与商业线索。
2. 【结论先行，言之有物】：
   - 杜绝“各方反响热烈”、“取得圆满成功”等模糊套话，一律使用“覆盖 12 个国家”、“触达 7 万人次”、“促成 7 家现场签约”等量化事实。
3. 【媒体引语真实精准】：
   - 引用福布斯、路透社、新华社时，必须提炼具有行业深度的专业评述（如对人形机器人供应链、具身智能量产、全球化布局的犀利分析），避免摘录口号式空话。
4. 【行动项必须闭环】：
   - 后续计划必须具备明确的“完成标准 (Done Criteria)”与“卡点排除路径”。
"""

def reorder_report_sections_for_readability(sections: dict[str, Any], mon: dict[str, Any] | None = None) -> dict[str, Any]:
    """将报告数据按照『失败/未检出/缺口前置，成果战果后置』的原则重排并结构化优化。"""
    reordered = {}
    
    # 基础概况（结论先行）
    reordered["basic"] = sections.get("basic", {})
    
    # 1. 前置抽取失败与数据缺口
    gaps = []
    if sections.get("data_gaps"):
        gaps.extend(sections["data_gaps"])
    
    unfound_media = []
    if mon and mon.get("watchlist"):
        for w in mon["watchlist"]:
            if w.get("status") != "hit" and not w.get("hits"):
                unfound_media.append({
                    "name": w["name"],
                    "status": "not_found",
                    "reason": "搜索引擎公开索引未检出或需付费订阅/外媒命名不一致",
                    "action": "建议由欧洲 PR 团队定向联系媒体记者或提供专访通稿"
                })
    
    reordered["frontloaded_issues"] = {
        "title": "待核实与未检出排查（优先暴露问题）",
        "unfound_media": unfound_media,
        "data_gaps": gaps,
        "problems": sections.get("review", {}).get("problems", [])
    }
    
    # 2. 后置成果与已确认报道
    reordered["participation"] = sections.get("participation", {})
    reordered["exposure"] = sections.get("exposure", {})
    
    # 3. 战略复盘与高管决策下一步
    reordered["strategic_next"] = {
        "title": "高管决策与下一步行动闭环",
        "next_steps": sections.get("review", {}).get("next_steps", [])
    }
    
    return reordered
