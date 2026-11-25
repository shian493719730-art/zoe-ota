from __future__ import annotations

def _normalize_model(model: str, provider: str) -> str:
    m = (model or "").strip()
    # 默认置顶最高旗舰档位：Claude Opus 5.5
    if not m or m in ("default", "opus", "opus-5.5", "oppus 5，5", "oppus 5.5", "oppus5.5", "claude-opus-5-5", "claude-opus-5.5", "claude-sonnet-5-5"):
        return "claude-opus-5-5" if provider == "openai_compat" else "claude-opus-5-5"
    if m in ("claude-3-7", "claude-3.7-sonnet", "claude-3-7-sonnet", "sonnet-3.7"):
        return "claude-3-7-sonnet-20250219" if provider == "anthropic" else "claude-3-7-sonnet"
    if m in ("claude-3-5", "claude-3.5-sonnet", "claude-3-5-sonnet"):
        return "claude-3-5-sonnet-20241022" if provider == "anthropic" else "claude-3-5-sonnet"
    if m in ("claude-opus", "claude-3-opus"):
        return "claude-3-opus-20240229" if provider == "anthropic" else "claude-3-opus"
    return m

"""统一的大模型调用层：Anthropic / OpenAI 兼容接口 / mock。用你自己的 Key，不依赖任何官方 SDK。"""
import json
import re
import time
import requests


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


def _clean_json_text(s: str) -> str:
    """修复模型常犯的未转义换行/制表符、字符串内双引号与尾部多余逗号。"""
    out = []
    in_str = False
    escape = False
    n = len(s)
    i = 0
    while i < n:
        ch = s[i]
        if escape:
            out.append(ch)
            escape = False
            i += 1
            continue
        if ch == '\\':
            out.append(ch)
            escape = True
            i += 1
            continue
        if ch == '"':
            if not in_str:
                in_str = True
                out.append(ch)
            else:
                # 向前跳过空白，判断是否为合法的 JSON 字符串闭合引号
                j = i + 1
                while j < n and s[j] in (' ', '\t', '\r', '\n'):
                    j += 1
                if j >= n or s[j] in (',', ':', '}', ']'):
                    in_str = False
                    out.append(ch)
                else:
                    # 字符串内部未转义的中文/英文引述双引号
                    out.append('\\"')
            i += 1
            continue
        if in_str:
            if ch == '\n':
                out.append('\\n')
            elif ch == '\r':
                out.append('\\r')
            elif ch == '\t':
                out.append('\\t')
            else:
                out.append(ch)
        else:
            out.append(ch)
        i += 1
    res = "".join(out)
    res = re.sub(r",(\s*[}\]])", r"\1", res)
    return res


def _try_parse_json(s: str):
    for candidate in (s, re.sub(r",(\s*[}\]])", r"\1", s)):
        try:
            return json.loads(candidate, strict=False)
        except Exception:
            pass
    cleaned = _clean_json_text(s)
    try:
        return json.loads(cleaned, strict=False)
    except Exception:
        return None


def _repair_truncated_json(sub: str):
    sub = _clean_json_text(sub).rstrip()
    while sub.endswith("\\"):
        sub = sub[:-1]
    stack = []
    in_str = False
    escape = False
    for ch in sub:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if not in_str:
            if ch in ('{', '['):
                stack.append('}' if ch == '{' else ']')
            elif ch in ('}', ']'):
                if stack and stack[-1] == ch:
                    stack.pop()
    if in_str:
        sub += '"'
    sub = sub.rstrip()
    if sub.endswith(":"):
        sub += "null"
    elif sub.endswith(","):
        sub = sub[:-1]
    # 若在字典末尾只写了 key 而没有冒号（如 {"a": 1, "incomplete_key"），补 : null
    if stack and stack[-1] == '}' and re.search(r'(?:^|[,{])\s*"(?:[^"\\]|\\.)*"\s*$', sub):
        sub += ": null"
    while stack:
        sub += stack.pop()
    sub = re.sub(r",(\s*[}\]])", r"\1", sub)
    try:
        return json.loads(sub, strict=False)
    except Exception:
        return None


def extract_json(text: str):
    """从模型输出里尽量稳地取出 JSON（容忍 ```json 围栏、未转义引号/换行、尾逗号与意外截断自动补全）。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t).strip()

    parsed = _try_parse_json(t)
    if parsed is not None:
        return parsed

    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if not starts:
        raise ValueError("输出中没有 JSON")
    s = min(starts)
    e = max(t.rfind("}"), t.rfind("]"))
    if e > s:
        parsed = _try_parse_json(t[s:e + 1])
        if parsed is not None:
            return parsed

    repaired = _repair_truncated_json(t[s:])
    if repaired is not None:
        return repaired

    raise ValueError("输出中的 JSON 格式异常且无法自动修复")


class LLM:
    def __init__(self, cfg: dict):
        l = cfg["llm"]
        self.provider = l["provider"]
        raw_model = l.get("model") or ""
        self.model = _normalize_model(raw_model, self.provider)
        self.smart_model = _normalize_model(l.get("smart_model") or self.model, self.provider)
        self.fast_model = l.get("fast_model") or ""
        self.api_key = l.get("api_key") or ""
        self.base_url = (l.get("base_url") or "").rstrip("/")
        self.max_tokens = int(l.get("max_tokens", 4000))
        self.temperature = l.get("temperature", 0.2)
        self.max_calls = int(cfg.get("limits", {}).get("max_llm_calls_per_run", 120))
        self.calls = 0
        self.in_tokens = 0
        self.out_tokens = 0
        self.timeout = 180
        if self.provider == "mock":
            from .mock_llm import mock_complete
            self._mock = mock_complete

    # ---------- 对外接口 ----------
    def chat(self, system: str, user: str, max_tokens: int | None = None, tier: str = "smart") -> str:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"已达到单次运行调用上限 {self.max_calls} 次（config.yaml → limits.max_llm_calls_per_run）")
        self.calls += 1
        mt = max_tokens or self.max_tokens
        use_model = self.fast_model if (tier == "fast" and self.fast_model) else self.model
        if self.provider == "mock":
            return self._mock(system, user)
        if not self.api_key:
            raise LLMError("没有配置 API Key。请在 .env 里填 LLM_API_KEY，或把 LLM_PROVIDER 设为 mock 试运行。")
        if self.provider == "anthropic":
            return self._anthropic(system, user, mt, model=use_model)
        if self.provider == "openai_compat":
            return self._openai(system, user, mt, model=use_model)
        raise LLMError(f"未知 provider: {self.provider}")

    def chat_json(self, system: str, user: str, max_tokens: int | None = None, retries: int = 1):
        sys2 = system + "\n\n只输出一个合法 JSON，不要任何解释，不要 Markdown 围栏。"
        last = ""
        for i in range(retries + 1):
            prompt = user if i == 0 else user + "\n\n（上一次输出不是合法 JSON，请严格只输出 JSON。）"
            last = self.chat(sys2, prompt, max_tokens)
            try:
                return extract_json(last)
            except Exception:
                continue
        raise LLMError("模型没有返回可解析的 JSON：" + last[:200])

    def usage_line(self) -> str:
        return f"大模型调用 {self.calls} 次，输入≈{self.in_tokens} tokens，输出≈{self.out_tokens} tokens"

    # ---------- 各家协议 ----------
    def _post(self, url, headers, body):
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        is_internal = (
            host.endswith(".agibot.com")
            or host == "agibot.com"
            or host in ("localhost", "127.0.0.1")
            or host.startswith(("10.", "192.168.", "172."))
        )
        verify_ssl = True
        for attempt in range(4):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=self.timeout, verify=verify_ssl)
            except requests.exceptions.SSLError as e:
                if is_internal and verify_ssl:
                    verify_ssl = False
                    try:
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    except Exception:
                        pass
                    continue
                if attempt == 3:
                    raise LLMError(f"SSL 证书校验失败: {e}")
                time.sleep(2 ** attempt)
                continue
            except requests.RequestException as e:
                if attempt == 3:
                    raise LLMError(f"网络错误: {e}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            return r
        raise LLMError("重试后仍失败")

    def _anthropic(self, system, user, max_tokens, model=None):
        url = (self.base_url or "https://api.anthropic.com") + "/v1/messages"
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        body = {"model": model or self.model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        r = self._post(url, headers, body)
        if r.status_code == 400 and "temperature" in r.text and "temperature" in body:
            body.pop("temperature")
            r = self._post(url, headers, body)
        if r.status_code in (400, 404) and ("model" in r.text.lower() or "not_found" in r.text.lower()):
            for fallback_m in ["claude-opus-4-1-20250805", "claude-sonnet-4-20250514", "claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022"]:
                if body.get("model") == fallback_m:
                    continue
                body["model"] = fallback_m
                r_fb = self._post(url, headers, body)
                if r_fb.status_code == 200:
                    r = r_fb
                    self.model = fallback_m
                    break
        if r.status_code != 200:
            raise LLMError(f"Anthropic 接口错误 {r.status_code}: {r.text[:300]}")
        d = r.json()
        u = d.get("usage", {})
        self.in_tokens += u.get("input_tokens", 0)
        self.out_tokens += u.get("output_tokens", 0)
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")

    def _openai(self, system, user, max_tokens, model=None):
        raw_base = (self.base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{raw_base}/chat/completions" if raw_base.endswith("/v1") else f"{raw_base}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        use_model = model or self.model
        body = {"model": use_model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_tokens}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        r = self._post(url, headers, body)
        if r.status_code == 404:
            alt_url = f"{raw_base}/chat/completions" if "/v1" not in raw_base else f"{raw_base.rsplit('/v1', 1)[0]}/chat/completions"
            r_alt = self._post(alt_url, headers, body)
            if r_alt.status_code == 200:
                r = r_alt
                url = alt_url
        if r.status_code == 400 and "max_completion_tokens" in r.text:
            body["max_completion_tokens"] = body.pop("max_tokens")
            body.pop("temperature", None)
            r = self._post(url, headers, body)
        elif r.status_code == 400 and "max_tokens" in r.text.lower() and body.get("max_tokens", 0) > 4096:
            body["max_tokens"] = 4096
            r = self._post(url, headers, body)
        elif r.status_code == 400 and "temperature" in r.text:
            body.pop("temperature", None)
            r = self._post(url, headers, body)
        if r.status_code in (400, 404) and ("model" in r.text.lower() or "not found" in r.text.lower()) and use_model != "claude-3-5-sonnet":
            for fallback_m in ["claude-3-5-sonnet", "claude-3-7-sonnet", "gpt-4o", "qwen-max"]:
                body["model"] = fallback_m
                r_fb = self._post(url, headers, body)
                if r_fb.status_code == 200:
                    r = r_fb
                    self.model = fallback_m
                    break
        if r.status_code != 200:
            raise LLMError(f"接口错误 {r.status_code}: {r.text[:300]}")
        d = r.json()
        u = d.get("usage", {})
        self.in_tokens += u.get("prompt_tokens", 0)
        self.out_tokens += u.get("completion_tokens", 0)
        return d["choices"][0]["message"]["content"] or ""