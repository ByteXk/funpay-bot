from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from core.config import (
    LLM_KEY,
    LLM_MODEL,
    LLM_PROTOCOL,
    LLM_URL,
    LOT_MAP_FILE,
    PROXY_IP,
    PROXY_PASS,
    PROXY_PORT,
    PROXY_TYPE,
    PROXY_USER,
    REPLIES_FILE,
    SETTINGS_FILE,
    STOCK_DIR,
    STOCK_LOW,
)
from core.proxy import from_fields


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def settings() -> dict:
    data = _read_json(SETTINGS_FILE, {"auto_raise": True, "auto_reply": True})
    return data


def save_settings(data: dict) -> None:
    _write_json(SETTINGS_FILE, data)


def proxy_fields() -> dict:
    s = settings()
    return {
        "user": s["proxy_user"] if "proxy_user" in s else PROXY_USER,
        "password": s["proxy_pass"] if "proxy_pass" in s else PROXY_PASS,
        "ip": s["proxy_ip"] if "proxy_ip" in s else PROXY_IP,
        "type": s["proxy_type"] if "proxy_type" in s else (PROXY_TYPE or "socks5"),
        "port": s["proxy_port"] if "proxy_port" in s else PROXY_PORT,
    }


def proxy_url() -> str | None:
    f = proxy_fields()
    if not (f.get("ip") or "").strip():
        return None
    return from_fields(
        user=f.get("user") or "",
        password=f.get("password") or "",
        ip=f.get("ip") or "",
        type_=f.get("type") or "socks5",
        port=f.get("port") or "",
    )


def llm_cfg() -> dict:
    s = settings()
    return {
        "url": (s.get("llm_url") or LLM_URL or "").strip(),
        "key": (s.get("llm_key") or LLM_KEY or "").strip(),
        "model": (s.get("llm_model") or LLM_MODEL or "").strip(),
        "protocol": (s.get("llm_protocol") or LLM_PROTOCOL or "auto").strip().lower(),
        "on": s["llm_on"]
        if "llm_on" in s
        else bool((s.get("llm_url") or LLM_URL) and (s.get("llm_key") or LLM_KEY)),
        "prompt": (s.get("llm_prompt") or "").strip(),
        "notes": (s.get("llm_notes") or "").strip(),
        "examples": list(s.get("llm_examples") or []),
    }


def llm_system() -> str:
    from models.llm import DEFAULT_SYSTEM

    c = llm_cfg()
    prompt = c["prompt"] or DEFAULT_SYSTEM
    parts = [prompt]
    if c["notes"]:
        parts.append("Факты о магазине (следуй им):\n" + c["notes"])
    lines = []
    for ex in c["examples"][-20:]:
        q = (ex.get("q") or "").strip()
        a = (ex.get("a") or "").strip()
        if q and a:
            lines.append(f"Покупатель: {q}\nТы: {a}")
    if lines:
        parts.append("Примеры твоих ответов:\n" + "\n\n".join(lines))
    return "\n\n".join(parts)


_DEFAULT_COMMANDS = [
    {"cmd": "вызов", "reply": "Продавца вызвал, скоро ответит."},
]


def fp_commands() -> list[dict]:
    s = settings()
    if "fp_commands" not in s:
        return list(_DEFAULT_COMMANDS)
    out = []
    for item in s.get("fp_commands") or []:
        if isinstance(item, str):
            out.append({"cmd": item, "reply": ""})
        elif isinstance(item, dict) and (item.get("cmd") or "").strip():
            out.append({"cmd": str(item["cmd"]).strip().lstrip("!"), "reply": str(item.get("reply") or "")})
    return out


def save_fp_commands(cmds: list[dict]) -> None:
    s = settings()
    s["fp_commands"] = [
        {"cmd": str(c.get("cmd") or "").strip().lstrip("!"), "reply": str(c.get("reply") or "")}
        for c in cmds
        if str(c.get("cmd") or "").strip()
    ]
    save_settings(s)


def match_command(text: str) -> dict | None:
    low = (text or "").lower()
    if "!" not in low:
        return None
    for c in fp_commands():
        name = (c.get("cmd") or "").strip().lstrip("!").lower()
        if not name:
            continue
        if re.search(rf"(^|[^\w])!{re.escape(name)}($|[^\w])", low, re.IGNORECASE):
            return c
    return None


def lot_map() -> dict[str, str]:
    """lot_id -> stock filename"""
    return {str(k): str(v) for k, v in _read_json(LOT_MAP_FILE, {}).items()}


def set_lot_stock(lot_id: int | str, filename: str) -> None:
    m = lot_map()
    m[str(lot_id)] = filename
    _write_json(LOT_MAP_FILE, m)


def stock_path(lot_id: int | str) -> Path:
    m = lot_map()
    name = m.get(str(lot_id), f"{lot_id}.txt")
    return STOCK_DIR / name


def read_stock(lot_id: int | str) -> list[str]:
    p = stock_path(lot_id)
    if not p.exists():
        return []
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def write_stock(lot_id: int | str, lines: list[str]) -> None:
    p = stock_path(lot_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def pop_stock(lot_id: int | str, n: int = 1) -> list[str] | None:
    items = read_stock(lot_id)
    if len(items) < n:
        return None
    taken, rest = items[:n], items[n:]
    write_stock(lot_id, rest)
    return taken


def append_stock(lot_id: int | str, lines: list[str]) -> int:
    items = read_stock(lot_id)
    items.extend(lines)
    write_stock(lot_id, items)
    return len(items)


def stock_low(lot_id: int | str) -> bool:
    return len(read_stock(lot_id)) <= STOCK_LOW


def load_replies() -> list[dict]:
    if not REPLIES_FILE.exists():
        return []
    data = yaml.safe_load(REPLIES_FILE.read_text(encoding="utf-8")) or []
    return data


def match_reply(text: str) -> str | None:
    if not text:
        return None
    low = text.lower()
    for rule in load_replies():
        kws = rule.get("keywords") or []
        if any(k.lower() in low for k in kws):
            return rule.get("text")
    return None
