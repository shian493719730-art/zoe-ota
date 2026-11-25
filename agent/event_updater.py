"""目标活动与展会每日自动更新引擎 (Daily Target Events Auto-Updater)
支持：
1. 每天自动检测并更新一次最新目标活动与行业展会日程
2. 动态计算展会倒计时、展会状态（进行中 / 近期筹备 / 已复盘归档）
3. 自动同步初始化各活动的 event.yaml 与底层文档目录
4. 支持手动即时检测与自动每日零点刷新
"""
from __future__ import annotations
import os
import json
import logging
from datetime import datetime, date
from pathlib import Path
import yaml

logger = logging.getLogger("event_updater")

DATA_DIR = Path.home() / ".agibot"
CALENDAR_FILE = DATA_DIR / "events_calendar.json"

DEFAULT_TARGET_EVENTS = [
    {
        "short": "websummit2026",
        "name": "Web Summit Lisbon 2026",
        "icon": "PT",
        "country": "葡萄牙",
        "city": "里斯本",
        "venue": "MEO Arena, Lisboa",
        "start": "2026-11-09",
        "end": "2026-11-12",
        "booth": "E114 (主场展位)",
        "tag": "整机参展",
        "focus": "智元 A3 人形机器人 9 台开幕群舞与主旨演讲，外媒广泛报道",
        "keywords": ["Web Summit", "Web Summit Lisbon 2026"],
        "keywords_cn": ["Web Summit 里斯本"]
    },
    {
        "short": "ifa2026",
        "name": "IFA Berlin 2026",
        "icon": "DE",
        "country": "德国",
        "city": "柏林",
        "venue": "Messe Berlin",
        "start": "2026-09-04",
        "end": "2026-09-08",
        "booth": "欧洲合作展位",
        "tag": "对标展会",
        "focus": "欧洲家电与消费电子生态对标、现场意向客户签约",
        "keywords": ["IFA Berlin 2026", "IFA 2026"],
        "keywords_cn": ["IFA 2026 柏林"]
    },
    {
        "short": "ces2027",
        "name": "CES 2027 (拉斯维加斯消费电子展)",
        "icon": "US",
        "country": "美国",
        "city": "拉斯维加斯",
        "venue": "LVCC (Las Vegas Convention Center)",
        "start": "2027-01-05",
        "end": "2027-01-08",
        "booth": "待定展位",
        "tag": "近期筹备",
        "focus": "全球消费电子与具身智能主场、北美市场核心突破",
        "keywords": ["CES 2027", "CES Las Vegas"],
        "keywords_cn": ["CES 2027 消费电子展"]
    },
    {
        "short": "mwc2027",
        "name": "MWC Barcelona 2027 (世界移动通信大会)",
        "icon": "ES",
        "country": "西班牙",
        "city": "巴塞罗那",
        "venue": "Fira Gran Via",
        "start": "2027-03-01",
        "end": "2027-03-04",
        "booth": "海外政企生态区",
        "tag": "全球拓展",
        "focus": "欧洲运营商与政企客户对接，5G+具身智能工业运维",
        "keywords": ["MWC 2027", "MWC Barcelona"],
        "keywords_cn": ["MWC 2027 世界移动通信大会"]
    },
    {
        "short": "hannover2027",
        "name": "Hannover Messe 2027 (汉诺威工博会)",
        "icon": "DE",
        "country": "德国",
        "city": "汉诺威",
        "venue": "Hannover Exhibition Grounds",
        "start": "2027-04-12",
        "end": "2027-04-16",
        "booth": "工业智造馆",
        "tag": "工业落地",
        "focus": "汽车产线料框分拣、变电站巡检双足机器人工业场景",
        "keywords": ["Hannover Messe 2027", "汉诺威工博会"],
        "keywords_cn": ["汉诺威工业博览会 2027"]
    },
    {
        "short": "wrc2027",
        "name": "世界机器人大会 WRC 2027 (北京)",
        "icon": "CN",
        "country": "中国",
        "city": "北京",
        "venue": "北京亦创国际会展中心",
        "start": "2027-08-18",
        "end": "2027-08-22",
        "booth": "智元特装展区",
        "tag": "国内主场",
        "focus": "中国具身智能同行（宇树/逐际/星动等）正面对比与新品发布",
        "keywords": ["世界机器人大会 2027", "WRC 2027"],
        "keywords_cn": ["2027 世界机器人大会"]
    }
]


def calculate_status(start_str: str, end_str: str, today: date) -> tuple[str, str, int | None]:
    """计算活动最新状态与倒计时
    返回 (status_code, status_label, countdown_days)
    """
    try:
        s_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        e_date = datetime.strptime(end_str, "%Y-%m-%d").date()
    except Exception:
        return "active", "进行中", None

    if today > e_date:
        days_ago = (today - e_date).days
        return "completed", f"已复盘 (结束{days_ago}天)", None
    elif s_date <= today <= e_date:
        return "ongoing", "正在进行中", 0
    else:
        days_left = (s_date - today).days
        if days_left <= 60:
            return "upcoming_urgent", f"近期筹备 (倒计时 {days_left} 天)", days_left
        else:
            return "upcoming", f"日程规划 (距开展 {days_left} 天)", days_left


def ensure_event_directory(event_info: dict, root_dir: Path):
    """确保活动的底层目录结构及 event.yaml 存在"""
    short = event_info["short"]
    ev_dir = root_dir / "events" / short
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "docs").mkdir(exist_ok=True)
    (ev_dir / "outputs").mkdir(exist_ok=True)

    yaml_p = ev_dir / "event.yaml"
    if not yaml_p.exists():
        data = {
            "name": event_info["name"],
            "short": short,
            "start": event_info.get("start", ""),
            "end": event_info.get("end", ""),
            "venue": event_info.get("venue", ""),
            "booth": event_info.get("booth", ""),
            "keywords": event_info.get("keywords", [event_info["name"]]),
            "keywords_cn": event_info.get("keywords_cn", [event_info["name"]]),
            "tag": event_info.get("tag", "常规展会"),
            "focus": event_info.get("focus", ""),
            "known_links": []
        }
        yaml_p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def get_events_catalog(root_dir: Path | None = None, force_refresh: bool = False) -> dict:
    """获取目标活动日历。
    如果今天尚未更新过，则自动执行每日最新活动更新检测！
    """
    if root_dir is None:
        root_dir = Path(__file__).resolve().parent.parent

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today()
    today_str = today.isoformat()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    existing_data = None
    if CALENDAR_FILE.exists() and not force_refresh:
        try:
            cached = json.loads(CALENDAR_FILE.read_text(encoding="utf-8"))
            if cached.get("last_updated_date") == today_str:
                return cached
            existing_data = cached
        except Exception:
            pass

    # 执行每日最新活动更新逻辑
    events_list = []
    registered_shorts = set()

    # 1. 载入系统内置目标展会排程
    for ev in DEFAULT_TARGET_EVENTS:
        ev_copy = dict(ev)
        code, label, days = calculate_status(ev_copy["start"], ev_copy["end"], today)
        ev_copy["status_code"] = code
        ev_copy["status_label"] = label
        ev_copy["countdown_days"] = days
        events_list.append(ev_copy)
        registered_shorts.add(ev_copy["short"])
        ensure_event_directory(ev_copy, root_dir)

    # 2. 扫描用户自定义创建或本地已有的 events 目录
    events_root = root_dir / "events"
    if events_root.exists():
        for d in events_root.iterdir():
            if d.is_dir() and d.name not in registered_shorts and not d.name.startswith("."):
                yaml_p = d / "event.yaml"
                ev_name = d.name
                s_date = ""
                e_date = ""
                venue = ""
                tag = "自定义活动"
                focus = ""
                if yaml_p.exists():
                    try:
                        yd = yaml.safe_load(yaml_p.read_text(encoding="utf-8")) or {}
                        ev_name = yd.get("name", d.name)
                        s_date = str(yd.get("start", ""))
                        e_date = str(yd.get("end", ""))
                        venue = yd.get("venue", "")
                        tag = yd.get("tag", "自定义活动")
                        focus = yd.get("focus", "")
                    except Exception:
                        pass
                
                code, label, days = calculate_status(s_date, e_date, today) if (s_date and e_date) else ("active", "自定义目标活动", None)
                custom_item = {
                    "short": d.name,
                    "name": ev_name,
                    "icon": "CUSTOM",
                    "country": "自定",
                    "city": "",
                    "venue": venue,
                    "start": s_date,
                    "end": e_date,
                    "booth": "",
                    "tag": tag,
                    "focus": focus,
                    "status_code": code,
                    "status_label": label,
                    "countdown_days": days,
                    "is_custom": True
                }
                events_list.append(custom_item)
                registered_shorts.add(d.name)

    # 排序：进行中 > 近期筹备 (倒计时由近及远) > 已复盘 (由近及远)
    def sort_key(x):
        code = x["status_code"]
        if code == "ongoing":
            return (0, 0)
        elif code == "upcoming_urgent":
            return (1, x["countdown_days"] or 999)
        elif code == "upcoming":
            return (2, x["countdown_days"] or 999)
        else:
            return (3, 0)

    events_list.sort(key=sort_key)

    catalog = {
        "events": events_list,
        "total": len(events_list),
        "last_updated": now_str,
        "last_updated_date": today_str,
        "today": today_str,
        "auto_daily": True,
        "note": "目标活动日程由 Agent 每日自动校准与同步更新"
    }

    try:
        CALENDAR_FILE.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"写入 events_calendar.json 异常: {e}")

    return catalog


def add_custom_event(name: str, start: str, end: str, venue: str = "", tag: str = "重点展会", root_dir: Path | None = None, files: list = None) -> dict:
    """新增自定义目标活动，支持在创建时直接注入导入的材料文件"""
    if root_dir is None:
        root_dir = Path(__file__).resolve().parent.parent

    import base64
    # 生成唯一的 short id
    clean_name = "".join(c for c in name.lower() if c.isalnum() or c in ("-", "_"))[:16] or "custom"
    short = f"ev_{clean_name}_{datetime.now().strftime('%m%d_%H%M')}"

    ev_dir = root_dir / "events" / short
    ev_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = ev_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "outputs").mkdir(exist_ok=True)

    start_norm = (start or "").strip().replace("/", "-")
    end_norm = (end or "").strip().replace("/", "-")

    # 保存随活动一同导入的文档材料
    saved_files = []
    if files and isinstance(files, list):
        for f in files:
            try:
                fname = f.get("name", "document.pdf")
                # 过滤非法字符
                fname = Path(fname).name
                dest = docs_dir / fname
                raw_b64 = f.get("content") or f.get("data") or ""
                if raw_b64:
                    if isinstance(raw_b64, str) and "," in raw_b64 and raw_b64.startswith("data:"):
                        raw_b64 = raw_b64.split(",", 1)[1]
                    raw = base64.b64decode(raw_b64)
                    dest.write_bytes(raw)
                    saved_files.append(str(dest))
                elif f.get("path") and Path(f["path"]).exists():
                    import shutil
                    shutil.copy2(f["path"], dest)
                    saved_files.append(str(dest))
            except Exception as fe:
                logger.warning(f"导入活动文件 {f.get('name')} 异常: {fe}")

    yaml_data = {
        "name": name.strip(),
        "short": short,
        "start": start_norm,
        "end": end_norm,
        "venue": venue.strip(),
        "tag": tag.strip(),
        "keywords": [name.strip()],
        "keywords_cn": [name.strip()],
        "imported_files": [Path(p).name for p in saved_files],
        "created_at": datetime.now().isoformat()
    }
    (ev_dir / "event.yaml").write_text(yaml.safe_dump(yaml_data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    catalog = get_events_catalog(root_dir=root_dir, force_refresh=True)
    return {
        "catalog": catalog,
        "short": short,
        "name": name.strip(),
        "saved_files": saved_files
    }
