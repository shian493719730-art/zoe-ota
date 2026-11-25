"""Zoe 轻量 OTA 热更新引擎
支持从 jsDelivr CDN / GitHub / Gitee / 自定义内网更新源拉取代码差量补丁，
严密保护本地 .env（密钥）、events/（展会数据）、history/（会话历史）绝不被覆盖。
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
from pathlib import Path
import requests

DEFAULT_OTA_REPO = "shian493719730-art/zoe-ota"

# 安全白名单：只允许更新程序代码与前端静态资源
ALLOWED_EXACT = {"desktop_app.py", "config.yaml", "version.json"}
ALLOWED_PREFIXES = ("ui/", "agent/")
FORBIDDEN_FRAGMENTS = (".env", "events/", "history/", "uploads/", ".venv/", ".cache/", "..")


def _parse_ver(v: str) -> tuple[int, ...]:
    nums = []
    for part in str(v or "0").lstrip("vV").split("."):
        digits = "".join(c for c in part if c.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _is_safe_relpath(rel: str) -> bool:
    r = rel.strip().lstrip("/")
    if not r or "\\" in r:
        return False
    if any(bad in r for bad in FORBIDDEN_FRAGMENTS):
        return False
    return r in ALLOWED_EXACT or any(r.startswith(p) for p in ALLOWED_PREFIXES)


def get_local_version(root_dir: Path) -> dict:
    vp = Path(root_dir) / "version.json"
    if vp.exists():
        try:
            data = json.loads(vp.read_text(encoding="utf-8"))
            return {
                "version": str(data.get("version", "1.2.1")),
                "build": int(data.get("build", 121)),
                "updated_at": data.get("updated_at", ""),
                "notes": data.get("notes", ""),
            }
        except Exception:
            pass
    return {"version": "1.2.1", "build": 121, "updated_at": "", "notes": ""}


def _resolve_candidate_bases() -> list[str]:
    bases = []
    custom_url = os.getenv("ZOE_UPDATE_URL", "").strip().rstrip("/")
    if custom_url:
        bases.append(custom_url)

    repo = os.getenv("ZOE_OTA_REPO", DEFAULT_OTA_REPO).strip() or DEFAULT_OTA_REPO
    # 1. 通过 jsDelivr 元数据接口实时获取最新 Git Tag（绕过 @main 分支 12 小时缓存）
    try:
        r = requests.get(f"https://data.jsdelivr.com/v1/package/gh/{repo}", timeout=6)
        if r.status_code == 200:
            versions = r.json().get("versions", [])
            if versions:
                versions_sorted = sorted(versions, key=_parse_ver, reverse=True)
                latest_tag = versions_sorted[0]
                bases.append(f"https://cdn.jsdelivr.net/gh/{repo}@{latest_tag}")
    except Exception:
        pass

    # 2. 兜底镜像
    bases.append(f"https://cdn.jsdelivr.net/gh/{repo}@main")
    bases.append(f"https://raw.githubusercontent.com/{repo}/main")
    return bases


def _fetch_remote_manifest(timeout: int = 8) -> tuple[str | None, dict | None, str]:
    last_err = "暂未发现线上更新源"
    for base in _resolve_candidate_bases():
        url = f"{base}/manifest.json"
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "version" in data and "files" in data:
                    return base, data, ""
            elif r.status_code == 404:
                last_err = "更新仓库尚未发布首个热更新包"
        except Exception as e:
            last_err = f"检查更新连接超时或受限: {e}"
    return None, None, last_err


def check_for_update(root_dir: Path) -> dict:
    root = Path(root_dir)
    local_info = get_local_version(root)
    base, manifest, err = _fetch_remote_manifest()
    if not manifest or not base:
        return {
            "ok": False,
            "has_update": False,
            "current_version": local_info["version"],
            "current_build": local_info["build"],
            "message": err,
        }

    rem_ver = str(manifest.get("version", "0.0.0"))
    rem_build = int(manifest.get("build", 0))
    is_newer = (_parse_ver(rem_ver) > _parse_ver(local_info["version"])) or (rem_build > local_info["build"])

    changed = []
    files_map = manifest.get("files", {})
    for rel, expected_sha in files_map.items():
        if not _is_safe_relpath(rel):
            continue
        local_file = root / rel
        if not local_file.exists() or _sha256_file(local_file) != expected_sha:
            changed.append(rel)

    has_update = is_newer and len(changed) > 0
    return {
        "ok": True,
        "has_update": has_update,
        "base_url": base,
        "current_version": local_info["version"],
        "current_build": local_info["build"],
        "latest_version": rem_ver,
        "latest_build": rem_build,
        "notes": manifest.get("notes", ""),
        "changed_count": len(changed),
        "changed_files": changed,
    }


def apply_update(root_dir: Path) -> dict:
    root = Path(root_dir).resolve()
    info = check_for_update(root)
    if not info.get("ok"):
        return {"ok": False, "message": info.get("message", "无法连接更新源")}
    if not info.get("has_update"):
        return {
            "ok": True,
            "updated": False,
            "version": info["current_version"],
            "needs_restart": False,
            "message": f"当前已是最新版本 v{info['current_version']}",
        }

    base = info["base_url"]
    _, manifest, err = _fetch_remote_manifest(timeout=10)
    if not manifest:
        return {"ok": False, "message": err or "拉取更新清单失败"}

    files_map = manifest.get("files", {})
    staging = root / ".update_staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    downloaded: list[tuple[str, Path]] = []
    try:
        for rel in info["changed_files"]:
            if not _is_safe_relpath(rel):
                continue
            expected_sha = files_map.get(rel, "")
            url = f"{base}/{rel}"
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                raise RuntimeError(f"下载 {rel} 失败 (HTTP {r.status_code})")
            content = r.content
            if expected_sha:
                actual_sha = hashlib.sha256(content).hexdigest()
                if actual_sha != expected_sha:
                    raise RuntimeError(f"文件 {rel} 完整性校验失败，已中止更新")
            stage_target = (staging / rel).resolve()
            if not str(stage_target).startswith(str(staging.resolve())):
                raise RuntimeError(f"非法路径拦截: {rel}")
            stage_target.parent.mkdir(parents=True, exist_ok=True)
            stage_target.write_bytes(content)
            downloaded.append((rel, stage_target))

        # 全部下载并通过 SHA256 校验后，再原子写入应用目录
        needs_restart = False
        for rel, src in downloaded:
            dest = (root / rel).resolve()
            if not str(dest).startswith(str(root)):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            if rel.endswith(".py") or rel == "config.yaml":
                needs_restart = True

        # 写入新版 version.json
        new_ver = {
            "version": str(manifest.get("version", info["latest_version"])),
            "build": int(manifest.get("build", info["latest_build"])),
            "updated_at": manifest.get("updated_at", ""),
            "notes": manifest.get("notes", ""),
        }
        (root / "version.json").write_text(json.dumps(new_ver, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "ok": True,
            "updated": True,
            "version": new_ver["version"],
            "build": new_ver["build"],
            "notes": new_ver["notes"],
            "updated_files": [rel for rel, _ in downloaded],
            "needs_restart": needs_restart,
            "message": f"已更新至 v{new_ver['version']}",
        }
    except PermissionError as pe:
        return {
            "ok": False,
            "message": f"更新目录无写入权限（请先将 Zoe 拖入「应用程序」后重试）：{pe}",
        }
    except Exception as e:
        return {"ok": False, "message": f"热更新失败：{e}"}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
