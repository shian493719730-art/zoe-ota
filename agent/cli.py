"""命令行入口。常用：
  python -m agent.cli doctor                    # 自检：Key 通不通、缺什么
  python -m agent.cli init-event myexpo2026 --name "My Expo 2026" --start 2026-10-01 --end 2026-10-03
  python -m agent.cli monitor myexpo2026        # 联网监测
  python -m agent.cli report myexpo2026         # 生成四段式报告(md + pptx)
  python -m agent.cli compare                   # 多场活动横向对比
  python -m agent.cli weekly --notes this_week.txt
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from .config import load_config, event_path, events_dir
from .llm import LLM, LLMError
from .search import Searcher
from .web import Fetcher


def _mask(k: str) -> str:
    return "未设置" if not k else f"已设置（…{k[-4:]}）"


def _clients(cfg):
    llm = LLM(cfg)
    return llm, Searcher(cfg, llm), Fetcher(cfg)


def cmd_doctor(a, cfg):
    ok = True

    def line(good, msg, fix=""):
        nonlocal ok
        print(("  ✓ " if good else "  ✗ ") + msg + (f"\n      → {fix}" if (fix and not good) else ""))
        ok = ok and good
    print("== 环境 ==")
    line(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}", "需要 Python 3.9 或更高")
    for mod, pipname in [("requests", "requests"), ("yaml", "pyyaml"), ("pptx", "python-pptx"), ("openpyxl", "openpyxl"),
                         ("pypdf", "pypdf"), ("docx", "python-docx")]:
        try:
            __import__(mod)
            line(True, f"依赖 {pipname}")
        except ImportError:
            line(False, f"缺少依赖 {pipname}", f"pip install {pipname}")
    for mod, pipname in [("trafilatura", "trafilatura"), ("ddgs", "ddgs"), ("streamlit", "streamlit")]:
        try:
            __import__(mod)
            line(True, f"可选依赖 {pipname}")
        except ImportError:
            print(f"  · 可选依赖 {pipname} 未安装（pip install {pipname}）")
    print("== 大模型 ==")
    l = cfg["llm"]
    print(f"  provider={l['provider']}  model={l['model'] or '(未设置)'}  base_url={l['base_url'] or '(默认)'}  key={_mask(l['api_key'])}")
    llm = LLM(cfg)
    if l["provider"] == "mock":
        line(True, "试运行模式（mock）：不调用接口。要用真实模型，请改 .env 的 LLM_PROVIDER 并填 Key")
    elif not l["api_key"]:
        line(False, "没有 API Key", "在 .env 里填 LLM_API_KEY")
    elif l["provider"] == "openai_compat" and not l["base_url"]:
        line(False, "openai_compat 需要 LLM_BASE_URL", "例如 https://你的网关地址/v1")
    elif not l["model"]:
        line(False, "没有指定模型名", "在 .env 里填 LLM_MODEL")
    else:
        try:
            t0 = time.time()
            r = llm.chat("你是连通性测试。", "只回复两个字：收到", max_tokens=20)
            line(True, f"大模型连通，返回：{r.strip()[:20]}（{time.time() - t0:.1f}s）")
        except LLMError as e:
            line(False, f"大模型调用失败：{str(e)[:160]}", "检查 Key / 模型名 / 网络(代理、VPN)；内网网关需连公司网络")
    print("== 联网搜索 ==")
    s = cfg["search"]
    print(f"  provider={s['provider']}  key={_mask(s['api_key'])}")
    if s["provider"] in ("tavily", "serper", "brave") and not s["api_key"]:
        line(False, f"{s['provider']} 需要 SEARCH_API_KEY", "到对应官网申请，或先用 SEARCH_PROVIDER=ddg（免 Key）")
    elif a.skip_search:
        print("  · 已跳过搜索连通测试")
    else:
        try:
            hits = Searcher(cfg, llm).search("AGIBOT robot", None, False, None)
            line(bool(hits), f"搜索返回 {len(hits)} 条", "搜索源没返回结果，换一个 provider 试试")
        except Exception as e:
            line(False, f"搜索失败：{str(e)[:160]}", "免费源(ddg)常被限流；建议改用 tavily/serper/brave")
    print("== 可选 ==")
    print(f"  YouTube API Key：{_mask(cfg.get('youtube_api_key', ''))}（用于读取视频播放量，可不配）")
    print("\n结果：" + ("全部通过" if ok else "有未通过项，按提示处理后重新运行 doctor"))
    return 0 if ok else 1


def cmd_init_event(a, cfg):
    d = events_dir(cfg) / a.short
    if d.exists():
        print(f"{d} 已存在")
        return 1
    for sub in ("docs", "photos", "outputs"):
        (d / sub).mkdir(parents=True)
    (d / "event.yaml").write_text(
        f"name: {a.name}\nshort: {a.short}\nstart: {a.start or ''}\nend: {a.end or ''}\nvenue: {a.venue or ''}\nbooth: {a.booth or ''}\n"
        f"keywords: [\"{a.name}\"]\nkeywords_cn: []\nextra_queries: []\nlocal_media_domains: []\nknown_links: []\n"
        f"# exhibitor_list_url: https://...   # 有展商名录页就填，竞品检索会更准\n", encoding="utf-8")
    (d / "notes.md").write_text("# 负责人笔记（复盘与不足、现场情况、展位安排…随便写，AI 只依据这里和 docs/ 里的文档）\n", encoding="utf-8")
    (d / "social_manual.csv").write_text("platform,url,impressions,likes,comments,shares,views,followers_gain,note\n"
                                         "# linkedin,https://...,12000,300,20,15,,120,示例行：以#开头的行会被忽略\n", encoding="utf-8")
    print(f"已创建 {d}\n  下一步：把周报/总结报告放进 docs/，运行 monitor 与 extract-metrics")
    return 0


def cmd_plan(a, cfg):
    from .monitor import plan_queries, load_event
    ev = load_event(event_path(cfg, a.event))
    for i, q in enumerate(plan_queries(ev, cfg, a.max_queries), 1):
        sites = f"  [限定 {','.join(q['domains'])}]" if q["domains"] else ""
        print(f"{i:>2}. [{q['group']}] {q['q']}{sites}")
    return 0


def cmd_monitor(a, cfg):
    from .monitor import run_monitor
    llm, se, fe = _clients(cfg)
    r = run_monitor(event_path(cfg, a.event), cfg, llm, se, fe, a.max_queries, a.max_articles)
    st = r["stats"]
    print(f"\n相关报道 {st['articles_total']} 篇（海外 {st['articles_overseas']} / 国内 {st['articles_domestic']}），覆盖 {st['countries_count']} 个国家；重点媒体命中 {sum(1 for w in r['watchlist'] if w['status']=='hit')}/{len(r['watchlist'])}")
    for n in r["notes"][:6]:
        print("  ·", n)
    print(llm.usage_line(), f"；搜索 {se.calls} 次")
    print(f"结果：{event_path(cfg, a.event) / 'outputs' / 'monitor_latest.md'}")
    return 0


def cmd_watch(a, cfg):
    from .monitor import run_monitor
    d = event_path(cfg, a.event)
    print(f"每 {a.every} 小时监测一次，Ctrl+C 停止。（也可用系统定时任务代替）")
    while True:
        llm, se, fe = _clients(cfg)
        r = run_monitor(d, cfg, llm, se, fe, a.max_queries, a.max_articles, log=lambda *x: None)
        print(f"[{r['run_at']}] 报道 {r['stats']['articles_total']} 篇，新增 {len(r['new_since_last'])} 条；{llm.usage_line()}")
        for u in r["new_since_last"][:10]:
            print("   +", u)
        time.sleep(a.every * 3600)


def cmd_report(a, cfg):
    from .report import build_sections, render_all
    d = event_path(cfg, a.event)
    sections = None
    if not a.render_only:
        llm = LLM(cfg)
        sections = build_sections(d, cfg, llm)
        print(llm.usage_line())
    res = render_all(d, cfg, sections)
    print(f"已生成：\n  {res['md']}\n  {res['pptx']}\n  （文字想改：编辑 outputs/report_sections.json 后加 --render-only 重新出 PPT，不花额度）")
    return 0


def cmd_extract(a, cfg):
    from .compare import extract_metrics
    llm = LLM(cfg)
    p = extract_metrics(event_path(cfg, a.event), cfg, llm)
    print(f"已生成草稿 {p}\n请逐项核对后改名为 metrics.yaml，并把 status 改成 reviewed")
    return 0


def cmd_compare(a, cfg):
    from .compare import run_compare, export_all, ranking
    llm = None if a.no_llm else LLM(cfg)
    out = run_compare(events_dir(cfg), cfg, llm, a.events or None, narrate=not a.no_llm)
    files = export_all(out, events_dir(cfg).parent / "outputs")
    for n, s in ranking(out):
        print(f"  {n}: 可比总分 {s:.0f}")
    for w in out["warnings"]:
        print("  ⚠", w)
    print(f"已生成：{files['md']}\n        {files['xlsx']}")
    return 0


def cmd_competitors(a, cfg):
    from .customers import scan_competitors, competitor_matrix_md
    llm, se, fe = _clients(cfg)
    results = [scan_competitors(event_path(cfg, e), cfg, llm, se, fe) for e in a.events]
    md = competitor_matrix_md(results)
    out = events_dir(cfg).parent / "outputs"
    out.mkdir(exist_ok=True)
    p = out / f"competitors_{date.today():%Y%m%d}.md"
    p.write_text(md, encoding="utf-8")
    print(md)
    print(llm.usage_line(), f"；搜索 {se.calls} 次\n已保存 {p}")
    return 0


def cmd_leads(a, cfg):
    from .customers import segment_leads
    llm = LLM(cfg)
    r = segment_leads(event_path(cfg, a.event), Path(a.file), cfg, llm)
    print(r["md"].read_text(encoding="utf-8"))
    print(f"明细：{r['xlsx']}\n{llm.usage_line()}")
    return 0


def cmd_weekly(a, cfg):
    from .weekly import run_weekly
    llm = LLM(cfg)
    notes = Path(a.notes).read_text(encoding="utf-8") if a.notes else sys.stdin.read()
    wdir = Path(cfg["_root"]) / "weekly"
    o = run_weekly(notes, cfg, llm, wdir, a.label)
    print(o["markdown"])
    print(llm.usage_line())
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent", description="活动复盘 Agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("doctor", help="自检"); s.add_argument("--skip-search", action="store_true"); s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("init-event", help="新建活动目录"); s.add_argument("short"); s.add_argument("--name", required=True)
    s.add_argument("--start"); s.add_argument("--end"); s.add_argument("--venue"); s.add_argument("--booth"); s.set_defaults(fn=cmd_init_event)
    s = sub.add_parser("plan", help="只看检索计划，不联网"); s.add_argument("event"); s.add_argument("--max-queries", type=int, default=30); s.set_defaults(fn=cmd_plan)
    for name, fn in (("monitor", cmd_monitor),):
        s = sub.add_parser(name, help="联网监测"); s.add_argument("event")
        s.add_argument("--max-queries", type=int, default=30); s.add_argument("--max-articles", type=int, default=40); s.set_defaults(fn=fn)
    s = sub.add_parser("watch", help="定时监测"); s.add_argument("event"); s.add_argument("--every", type=float, default=6)
    s.add_argument("--max-queries", type=int, default=30); s.add_argument("--max-articles", type=int, default=40); s.set_defaults(fn=cmd_watch)
    s = sub.add_parser("report", help="生成四段式报告"); s.add_argument("event"); s.add_argument("--render-only", action="store_true"); s.set_defaults(fn=cmd_report)
    s = sub.add_parser("extract-metrics", help="从文档抽取指标草稿"); s.add_argument("event"); s.set_defaults(fn=cmd_extract)
    s = sub.add_parser("compare", help="横向对比"); s.add_argument("events", nargs="*"); s.add_argument("--no-llm", action="store_true"); s.set_defaults(fn=cmd_compare)
    s = sub.add_parser("competitors", help="竞品参展矩阵"); s.add_argument("events", nargs="+"); s.set_defaults(fn=cmd_competitors)
    s = sub.add_parser("leads", help="线索人群分层"); s.add_argument("event"); s.add_argument("--file", required=True); s.set_defaults(fn=cmd_leads)
    s = sub.add_parser("weekly", help="闭环周报"); s.add_argument("--notes"); s.add_argument("--label"); s.set_defaults(fn=cmd_weekly)
    a = p.parse_args(argv)
    cfg = load_config()
    try:
        return a.fn(a, cfg) or 0
    except (LLMError, FileNotFoundError, ValueError) as e:
        print(f"出错：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
