from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

import requests

from core.errors import short_error
from core.proxy import requests_proxies

log = logging.getLogger("llm")

DEFAULT_SYSTEM = (
    "Ты помощник продавца на FunPay. Отвечай кратко, по делу, на языке покупателя. "
    "Не выдумывай выдачу товара и ключи. Если не уверен — попроси подождать продавца."
)


def _origin(url: str) -> str:
    u = url.strip()
    if not u.startswith("http"):
        u = "https://" + u
    p = urlparse(u)
    host = f"{p.scheme}://{p.netloc}"
    path = (p.path or "").rstrip("/")
    # https://varfungateway.com/v1  или  .../v1/chat/completions
    if "/v1/" in path + "/":
        # keep up to /v1
        idx = path.find("/v1")
        return host + path[: idx + 3]
    if path in {"", "/"}:
        return host + "/v1"
    if path.endswith("/v1"):
        return host + path
    return host + "/v1"


def resolve_protocol(url: str, model: str, protocol: str) -> str:
    p = (protocol or "auto").lower().strip()
    if p in {"openai", "oa", "gpt"}:
        return "openai"
    if p in {"anthropic", "claude", "ant"}:
        return "anthropic"
    path = urlparse(url if "://" in url else "https://" + url).path.lower()
    if "/messages" in path:
        return "anthropic"
    if "chat" in path:
        return "openai"
    m = (model or "").lower()
    if "claude" in m:
        return "anthropic"
    return "openai"


def chat(
    url: str,
    api_key: str,
    user_text: str,
    *,
    model: str = "",
    system: str = DEFAULT_SYSTEM,
    proxy_url: str | None = None,
    timeout: int = 90,
    protocol: str = "auto",
) -> str:
    base = _origin(url)
    proto = resolve_protocol(url, model, protocol)
    order = [proto]
    other = "anthropic" if proto == "openai" else "openai"
    if (protocol or "auto").lower() in {"auto", "", "both"}:
        order.append(other)

    last: Exception | None = None
    for pr in order:
        try:
            data = _post(base, api_key, user_text, model=model, system=system, proxy_url=proxy_url, timeout=timeout, protocol=pr)
            return _extract(data)
        except Exception as e:
            last = e
            log.warning("%s: %s", pr, short_error(e))
    raise last or RuntimeError("нейронка не ответила")


def _post(
    base: str,
    api_key: str,
    user_text: str,
    *,
    model: str,
    system: str,
    proxy_url: str | None,
    timeout: int,
    protocol: str,
) -> Any:
    proxies = requests_proxies(proxy_url) or {}
    if protocol == "anthropic":
        endpoint = base.rstrip("/") + "/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
        }
        payload: dict[str, Any] = {
            "model": model or "claude-sonnet-5",
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
        }
    else:
        endpoint = base.rstrip("/") + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        payload = {
            "model": model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.4,
            "max_tokens": 500,
        }
    r = requests.post(endpoint, headers=headers, json=payload, timeout=timeout, proxies=proxies)
    if r.status_code == 401:
        raise RuntimeError("ключ неверный или отозван (401)")
    if r.status_code == 402:
        raise RuntimeError("нет лицензии / квота (402)")
    if r.status_code == 403:
        raise RuntimeError("модель недоступна тарифу (403)")
    if r.status_code == 429:
        raise RuntimeError("лимит RPM (429)")
    if r.status_code >= 400:
        body = r.text[:300]
        raise RuntimeError(f"{protocol} {r.status_code}: {body}")
    return r.json()


def _extract(data: Any) -> str:
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)[:2000]
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] or {}
        msg = c0.get("message") or {}
        if isinstance(msg, dict) and msg.get("content"):
            return str(msg["content"]).strip()
        if c0.get("text"):
            return str(c0["text"]).strip()
    content = data.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        if parts:
            return "".join(parts).strip()
    for key in ("output", "output_text", "response", "text", "answer"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise ValueError(f"нет текста в ответе: {json.dumps(data, ensure_ascii=False)[:300]}")
