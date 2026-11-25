"""丰富的数据可视化图表组件（基于 Plotly）：
1. 活动传播价值雷达图 & 分组柱状图（横向对比）
2. 媒体评级占比 & 覆盖国家分布图（活动监测）
3. B2B客户线索人群画像分布图（线索分层）
4. 周报事项闭环状态仪表盘（闭环周报）
"""
from __future__ import annotations
import plotly.graph_objects as go
import plotly.express as px
from collections import Counter


# 现代科技感调色盘（智元蓝/橙/紫）
THEME_COLORS = ["#1E60FF", "#FF5C00", "#7928CA", "#00DFD8", "#F5A623", "#50E3C2"]
BG_COLOR = "rgba(0,0,0,0)"
GRID_COLOR = "rgba(128,128,128,0.15)"


def plot_radar_compare(events: list[dict], group_scores: dict, groups: list[tuple[str, str]]) -> go.Figure:
    """多活动综合传播价值雷达图"""
    categories = [gname for _, gname in groups]
    categories.append(categories[0])  # 闭合雷达图

    fig = go.Figure()
    for idx, e in enumerate(events):
        s = e["short"]
        scores = group_scores.get(s, {})
        values = [scores.get(g, 0) for g, _ in groups]
        values.append(values[0])  # 闭合雷达图
        color = THEME_COLORS[idx % len(THEME_COLORS)]
        
        fig.add_trace(go.Scatterpolar(
            r=values,
            theta=categories,
            fill='toself',
            name=e["name"],
            line=dict(color=color, width=2.5),
            opacity=0.65
        ))

    fig.update_layout(
        polar=dict(
            radialaxis=dict(
                visible=True,
                range=[0, 100],
                showline=False,
                gridcolor=GRID_COLOR,
                tickfont=dict(size=10, color="#888")
            ),
            angularaxis=dict(
                gridcolor=GRID_COLOR,
                tickfont=dict(size=12, family="sans-serif", color="#333")
            ),
            bgcolor=BG_COLOR
        ),
        margin=dict(l=40, r=40, t=30, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=-0.2, xanchor="center", x=0.5),
        height=380,
        paper_bgcolor=BG_COLOR,
    )
    return fig


def plot_bar_compare(events: list[dict], group_scores: dict, totals: dict, groups: list[tuple[str, str]]) -> go.Figure:
    """活动各维度得分与可比总分对比柱状图"""
    x_labels = [gname for _, gname in groups] + ["★ 可比总分"]
    
    fig = go.Figure()
    for idx, e in enumerate(events):
        s = e["short"]
        scores = group_scores.get(s, {})
        y_vals = [scores.get(g, 0) for g, _ in groups]
        y_vals.append(totals.get(s, 0) or 0)
        color = THEME_COLORS[idx % len(THEME_COLORS)]

        fig.add_trace(go.Bar(
            name=e["name"],
            x=x_labels,
            y=y_vals,
            text=[f"{v:.1f}" for v in y_vals],
            textposition='auto',
            marker_color=color,
            opacity=0.88
        ))

    fig.update_layout(
        barmode='group',
        yaxis=dict(title="得分 (0-100)", range=[0, 105], gridcolor=GRID_COLOR),
        xaxis=dict(gridcolor=GRID_COLOR),
        margin=dict(l=20, r=20, t=30, b=30),
        legend=dict(orientation="h", yanchor="bottom", y=-0.25, xanchor="center", x=0.5),
        height=380,
        paper_bgcolor=BG_COLOR,
        plot_bgcolor=BG_COLOR,
    )
    return fig


def plot_media_distribution(articles: list[dict]) -> tuple[go.Figure, go.Figure]:
    """活动监测：媒体国家分布与媒体级别环形图"""
    # 1. 媒体级别分布
    tiers = [f"T{a.get('tier', 3)} 媒体" for a in articles]
    tier_counts = Counter(tiers)
    fig_tier = go.Figure(data=[go.Pie(
        labels=list(tier_counts.keys()),
        values=list(tier_counts.values()),
        hole=0.45,
        marker=dict(colors=["#1E60FF", "#FF5C00", "#7928CA", "#A0AEC0"]),
        textinfo='label+percent',
    )])
    fig_tier.update_layout(
        title="媒体级别分布 (T1核心/T2垂直/T3其他)",
        margin=dict(l=20, r=20, t=40, b=20),
        height=300,
        paper_bgcolor=BG_COLOR,
        showlegend=False
    )

    # 2. 媒体所属国家分布
    countries = [a.get("country") or "未知" for a in articles]
    cnt_counts = Counter(countries).most_common(8)
    fig_country = go.Figure(data=[go.Bar(
        x=[k for k, _ in cnt_counts],
        y=[v for _, v in cnt_counts],
        text=[v for _, v in cnt_counts],
        textposition='auto',
        marker_color="#1E60FF",
        opacity=0.85
    )])
    fig_country.update_layout(
        title="报道媒体国家分布 TOP 8",
        yaxis=dict(title="报道篇数", gridcolor=GRID_COLOR),
        xaxis=dict(gridcolor=GRID_COLOR),
        margin=dict(l=20, r=20, t=40, b=20),
        height=300,
        paper_bgcolor=BG_COLOR,
        plot_bgcolor=BG_COLOR,
    )
    return fig_tier, fig_country


def plot_leads_charts(results: list[dict]) -> tuple[go.Figure, go.Figure]:
    """线索分层：客户人群类型与意向级别图表"""
    segs = Counter(x.get("segment", "其他") for x in results)
    intents = Counter(x.get("intent", "unknown") for x in results)
    
    intent_map = {"high": "🔥 高意向", "medium": "💬 中意向", "low": "👀 泛了解", "unknown": "❓ 未知"}
    intent_colors = {"🔥 高意向": "#FF5C00", "💬 中意向": "#1E60FF", "👀 泛了解": "#A0AEC0", "❓ 未知": "#E2E8F0"}

    # 人群分类环形图
    fig_seg = go.Figure(data=[go.Pie(
        labels=list(segs.keys()),
        values=list(segs.values()),
        hole=0.45,
        marker=dict(colors=THEME_COLORS),
        textinfo='label+percent',
    )])
    fig_seg.update_layout(
        title="意向客户群体分类画像",
        margin=dict(l=20, r=20, t=40, b=20),
        height=320,
        paper_bgcolor=BG_COLOR
    )

    # 意向层级柱状图
    mapped_intents = {intent_map.get(k, k): v for k, v in intents.items()}
    fig_intent = go.Figure(data=[go.Bar(
        x=list(mapped_intents.keys()),
        y=list(mapped_intents.values()),
        text=list(mapped_intents.values()),
        textposition='auto',
        marker_color=[intent_colors.get(k, "#1E60FF") for k in mapped_intents.keys()],
    )])
    fig_intent.update_layout(
        title="客户采购意向层级分布",
        yaxis=dict(title="线索数量", gridcolor=GRID_COLOR),
        xaxis=dict(gridcolor=GRID_COLOR),
        margin=dict(l=20, r=20, t=40, b=20),
        height=320,
        paper_bgcolor=BG_COLOR,
        plot_bgcolor=BG_COLOR
    )
    return fig_seg, fig_intent


def plot_weekly_progress(items: list[dict]) -> go.Figure:
    """周报事项闭环进度统计图"""
    status_map = {"done": "已闭环", "in_progress": "推进中", "blocked": "受阻/卡点", "new": "新启动"}
    status_counts = Counter(status_map.get(it.get("status"), "推进中") for it in items)
    
    color_map = {"已闭环": "#10B981", "推进中": "#3B82F6", "受阻/卡点": "#EF4444", "新启动": "#F59E0B"}
    
    fig = go.Figure(data=[go.Pie(
        labels=list(status_counts.keys()),
        values=list(status_counts.values()),
        hole=0.55,
        marker=dict(colors=[color_map.get(k, "#A0AEC0") for k in status_counts.keys()]),
        textinfo='label+value',
    )])
    fig.update_layout(
        title="本周事项闭环状态概览",
        margin=dict(l=10, r=10, t=40, b=10),
        height=260,
        paper_bgcolor=BG_COLOR,
        showlegend=True
    )
    return fig
