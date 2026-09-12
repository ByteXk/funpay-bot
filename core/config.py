from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TG_TOKEN = os.getenv("TG_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
GOLDEN_KEY = os.getenv("GOLDEN_KEY", "")
PHPSESSID = os.getenv("PHPSESSID", "").strip()
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)
FUNPAY_DELAY = float(os.getenv("FUNPAY_DELAY", "4"))
AUTO_RAISE = os.getenv("AUTO_RAISE", "1") not in {"0", "false", "False"}
STOCK_LOW = int(os.getenv("STOCK_LOW", "3"))

# Один прокси на Telegram + FunPay + нейронку
PROXY_USER = os.getenv("PROXY_USER", "").strip()
PROXY_PASS = os.getenv("PROXY_PASS", "").strip()
PROXY_IP = os.getenv("PROXY_IP", "").strip()  # 1.2.3.4 или 1.2.3.4:1080
PROXY_PORT = os.getenv("PROXY_PORT", "").strip()
PROXY_TYPE = os.getenv("PROXY_TYPE", "socks5").strip()  # http | https | socks5

LLM_URL = os.getenv("LLM_URL", "").strip()
LLM_KEY = os.getenv("LLM_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()
LLM_PROTOCOL = os.getenv("LLM_PROTOCOL", "auto").strip()  # auto | openai | anthropic

DATA = ROOT / "data"
STOCK_DIR = DATA / "stock"
REPLIES_FILE = DATA / "replies.yaml"
SETTINGS_FILE = DATA / "settings.json"
LOT_MAP_FILE = DATA / "lot_map.json"

STOCK_DIR.mkdir(parents=True, exist_ok=True)
