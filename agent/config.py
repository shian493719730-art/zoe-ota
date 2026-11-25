from __future__ import annotations
import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    candidates = [
        Path.home() / ".zoe" / ".env",
        Path.home() / ".agibot" / ".env",
        ROOT / ".env",
    ]
    for env in candidates:
        if not env.exists():
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(env, override=False)
        except ImportError:
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: str | os.PathLike | None = None) -> dict:
    _load_env()
    p = Path(path) if path else ROOT / "config.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg["_root"] = str(ROOT)

    llm = cfg.setdefault("llm", {})
    llm["provider"] = os.getenv("LLM_PROVIDER") or llm.get("provider") or "anthropic"
    llm["model"] = os.getenv("LLM_MODEL") or llm.get("model") or ""
    llm["base_url"] = os.getenv("LLM_BASE_URL") or llm.get("base_url") or ""
    key = os.getenv("LLM_API_KEY", "")
    if not key:
        key = os.getenv("ANTHROPIC_API_KEY", "") if llm["provider"] == "anthropic" else os.getenv("OPENAI_API_KEY", "")
    llm["api_key"] = key

    s = cfg.setdefault("search", {})
    s["provider"] = os.getenv("SEARCH_PROVIDER") or s.get("provider") or "ddg"
    s["api_key"] = os.getenv("SEARCH_API_KEY", "")
    cfg["youtube_api_key"] = os.getenv("YOUTUBE_API_KEY", "")
    return cfg


def events_dir(cfg: dict) -> Path:
    return Path(cfg["_root"]) / "events"


def event_path(cfg: dict, short: str) -> Path:
    p = events_dir(cfg) / short
    if not p.exists():
        raise FileNotFoundError(f"找不到活动目录 {p}。先运行: python -m agent.cli init-event {short} --name \"活动名\"")
    return p
