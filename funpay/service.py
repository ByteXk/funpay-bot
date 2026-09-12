from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup
from FunPayAPI.account import Account
from FunPayAPI.common import exceptions as fp_exc
from FunPayAPI.types import LotFields
from FunPayAPI.updater.runner import Runner
from requests.exceptions import ConnectionError as ReqConnectionError
from requests.exceptions import ProxyError, SSLError, Timeout

from core.config import FUNPAY_DELAY, GOLDEN_KEY, PHPSESSID, USER_AGENT
from core.errors import short_error
from core.proxy import mask_proxy, requests_proxies
from core.storage import proxy_url as cfg_proxy_url

log = logging.getLogger("fp")

# Нейронка только на сообщения не старше этого. Старая история — молчим.
LLM_MAX_AGE = 5 * 60

_MONTHS = {
    "январ": 1,
    "феврал": 2,
    "март": 3,
    "апрел": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "август": 8,
    "сентябр": 9,
    "октябр": 10,
    "ноябр": 11,
    "декабр": 12,
}


def _unix_from_value(val) -> float | None:
    if val is None or val is False:
        return None
    if isinstance(val, (int, float)):
        n = float(val)
        if n > 1e12:
            n /= 1000.0
        return n if n > 1e9 else None
    s = str(val).strip()
    if s.isdigit():
        return _unix_from_value(int(s))
    return None


def parse_msg_time(html: str, extra: dict | None = None) -> float | None:
    """Unix-время сообщения FunPay из JSON или HTML (title / «15:04» / «вчера»)."""
    if extra:
        for k in ("time", "date", "timestamp", "created", "created_at"):
            ts = _unix_from_value(extra.get(k))
            if ts:
                return ts
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(".chat-msg-date, .message-date, .media-meta, time")
    raw = ""
    if el is not None:
        raw = (el.get("title") or el.get("datetime") or el.get_text(" ", strip=True) or "").strip()
    if not raw:
        m = re.search(r"title=\"([^\"]{4,40})\"", html)
        raw = (m.group(1) if m else "").strip()
    return _parse_fp_clock(raw)


def _parse_fp_clock(raw: str) -> float | None:
    if not raw:
        return None
    s = re.sub(r"\s+", " ", raw.strip().lower())
    now = datetime.now()
    hm = re.search(r"(\d{1,2}):(\d{2})", s)
    h = int(hm.group(1)) if hm else 0
    mi = int(hm.group(2)) if hm else 0

    if s in {"только что", "сейчас"} or s.startswith("только что"):
        return time.time()
    if "вчера" in s:
        dt = now - timedelta(days=1)
        return dt.replace(hour=h, minute=mi, second=0, microsecond=0).timestamp() if hm else (now - timedelta(days=1)).timestamp()

    # «13 сентября 2026, 15:04» / «13 сентября, 15:04»
    named = re.search(r"(\d{1,2})\s+([а-яё]+)", s)
    if named:
        day = int(named.group(1))
        mon_raw = named.group(2)
        month = None
        for prefix, num in _MONTHS.items():
            if mon_raw.startswith(prefix):
                month = num
                break
        if month:
            year_m = re.search(r"(20\d{2})", s)
            year = int(year_m.group(1)) if year_m else now.year
            try:
                return datetime(year, month, day, h, mi).timestamp()
            except ValueError:
                return None

    # «13.09.2026» / «13.09»
    dotted = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(20\d{2}))?", s)
    if dotted:
        day, month = int(dotted.group(1)), int(dotted.group(2))
        year = int(dotted.group(3)) if dotted.group(3) else now.year
        try:
            return datetime(year, month, day, h, mi).timestamp()
        except ValueError:
            return None

    if hm and re.fullmatch(r"\d{1,2}:\d{2}", s):
        dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if dt > now + timedelta(minutes=5):
            dt -= timedelta(days=1)
        return dt.timestamp()
    return None


def _cookie_val(jar, name: str) -> str | None:
    """requests CookieJar.get() падает, если одно имя с разными domain/path."""
    found = None
    try:
        cookies = list(jar)
    except Exception:
        return None
    for c in cookies:
        if getattr(c, "name", None) != name:
            continue
        found = c.value
        domain = (getattr(c, "domain", None) or "").lstrip(".").lower()
        if domain.endswith("funpay.com"):
            return c.value
    return found


def _put_cookie(jar, name: str, value: str | None, domain: str = "funpay.com") -> None:
    for c in list(jar):
        if getattr(c, "name", None) == name:
            try:
                jar.clear(c.domain, c.path, c.name)
            except Exception:
                pass
    if value:
        jar.set(name, value, domain=domain, path="/")


_OURS_PREVIEW = re.compile(r"^(вы|you)\s*:\s*", re.IGNORECASE)


def preview_from_us(preview: str) -> bool:
    """В списке чатов FunPay свои сообщения помечает «Вы: …»."""
    p = (preview or "").lstrip()
    if not p:
        return False
    if p.startswith("\u2064"):
        return True
    return bool(_OURS_PREVIEW.match(p))


def chat_preview_fresh(when: str, max_age: int = LLM_MAX_AGE) -> bool:
    """Время из списка чатов: «15:04» сегодня. «12 сен» / «вчера» — уже старое."""
    s = (when or "").strip()
    if not s:
        return True
    if not re.fullmatch(r"\d{1,2}:\d{2}", s):
        return False
    ts = _parse_fp_clock(s)
    if ts is None:
        return True
    age = time.time() - ts
    return -60 <= age <= max_age


class FunPayService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._meta = threading.Lock()
        self._raise_at = 0.0
        self._seen_ids: dict[int, int] = {}
        self._sent: dict[int, list[tuple[str, float]]] = {}
        self._previews: dict[int, str] = {}
        self.proxy_url = cfg_proxy_url()
        self.account = Account(
            GOLDEN_KEY,
            user_agent=USER_AGENT,
            requests_timeout=25,
            proxy=requests_proxies(self.proxy_url),
        )
        if PHPSESSID:
            self.account.phpsessid = PHPSESSID
        self.session = requests.Session()
        self._bind_session()

    def set_proxy(self, url: str | None) -> None:
        url = url or None
        proxies = requests_proxies(url) or {}
        changed = url != self.proxy_url or self.account.proxy != proxies
        self.proxy_url = url
        self.account.proxy = proxies
        self.session.proxies = proxies
        if changed:
            log.info("FunPay proxy: %s", mask_proxy(self.proxy_url))

    def _bind_session(self) -> None:
        acc = self.account
        self.session.headers.update(
            {
                "user-agent": acc.user_agent or USER_AGENT,
                "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
            }
        )
        self.session.proxies = acc.proxy or {}

        def method(request_method, api_method, headers, payload, exclude_phpsessid=False, raise_not_200=False):
            with self._lock:
                headers = dict(headers or {})
                headers.pop("cookie", None)
                headers.pop("Cookie", None)
                headers.setdefault("accept", "*/*")
                _put_cookie(self.session.cookies, "golden_key", acc.golden_key)
                if acc.phpsessid and not exclude_phpsessid:
                    _put_cookie(self.session.cookies, "PHPSESSID", acc.phpsessid)
                elif exclude_phpsessid:
                    _put_cookie(self.session.cookies, "PHPSESSID", None)
                link = (
                    api_method
                    if str(api_method).startswith("https://funpay.com")
                    else "https://funpay.com/" + str(api_method).lstrip("/")
                )
                response = self.session.request(
                    request_method,
                    link,
                    headers=headers,
                    data=payload,
                    timeout=acc.requests_timeout,
                )
                if acc.golden_key:
                    _put_cookie(self.session.cookies, "golden_key", acc.golden_key)
                sid = _cookie_val(self.session.cookies, "PHPSESSID") or _cookie_val(response.cookies, "PHPSESSID")
                if sid:
                    acc.phpsessid = sid
                    _put_cookie(self.session.cookies, "PHPSESSID", sid)
                if response.status_code == 403:
                    raise fp_exc.UnauthorizedError(response)
                if response.status_code != 200 and raise_not_200:
                    raise fp_exc.RequestFailedError(response)
                return response

        acc.method = method

    def _do_get(self, update_phpsessid: bool = True):
        acc = self.account
        response = acc.method("get", "https://funpay.com", {}, {}, update_phpsessid, True)
        html = response.content.decode("utf-8", "ignore")
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:100]
        app = {}
        body = soup.find("body")
        raw_app = body.get("data-app-data") if body else None
        if raw_app:
            try:
                app = json.loads(raw_app)
            except Exception:
                app = {}
        uid = int(app.get("userId") or 0)
        username = None
        for sel in (
            "div.user-link-name",
            ".user-link-name",
            "a.user-link-name",
            ".user-link-dropdown",
            "div.user-link-dropdown .media-user-name",
        ):
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                username = el.get_text(strip=True)
                break
        if not uid:
            log.warning(
                "funpay не авторизовал (title=%r html=%s байт). проверь GOLDEN_KEY",
                title,
                len(html),
            )
            raise fp_exc.UnauthorizedError(response)
        acc.username = username or f"id{uid}"
        acc.app_data = app
        acc.id = uid
        acc.csrf_token = app.get("csrf-token") or app.get("csrfToken") or app.get("csrf")
        log.info("app keys=%s csrf=%s", list(app.keys()) if app else [], bool(acc.csrf_token))
        sales = soup.select_one("span.badge-trade, span.badge.badge-trade")
        acc.active_sales = int(sales.text) if sales and sales.text.strip().isdigit() else 0
        buys = soup.select_one("span.badge-orders, span.badge.badge-orders")
        acc.active_purchases = int(buys.text) if buys and buys.text.strip().isdigit() else 0
        jar = {c.name: c.value for c in self.session.cookies}
        jar.update(response.cookies.get_dict())
        log.info("cookies=%s", list(jar.keys()))
        sid = jar.get("PHPSESSID") or jar.get("phpsessid")
        if update_phpsessid or not acc.phpsessid:
            acc.phpsessid = sid or acc.phpsessid
        if not acc.phpsessid:
            log.warning("PHPSESSID нет — runner будет 400")
        if not acc.is_initiated:
            acc._Account__setup_categories(html)
        acc.last_update = int(time.time())
        acc.html = html
        acc._Account__initiated = True
        return acc

    def login(self) -> Account:
        from core.proxy import candidates

        preferred = self.proxy_url
        conn_err = (ReqConnectionError, ProxyError, Timeout, SSLError, OSError)
        try:
            self.set_proxy(preferred)
            with self._lock:
                self._do_get(update_phpsessid=not bool(self.account.phpsessid))
        except fp_exc.UnauthorizedError:
            log.warning("прокси доставил страницу, но golden_key не принят")
            raise
        except conn_err as e:
            log.warning("прокси %s не коннектится: %s", mask_proxy(preferred), short_error(e))
            last = e
            rest = [u for u in candidates(preferred) if u != preferred] + [None]
            ok = False
            for u in rest:
                try:
                    self.set_proxy(u)
                    with self._lock:
                        self._do_get(update_phpsessid=not bool(self.account.phpsessid))
                    ok = True
                    last = None
                    break
                except fp_exc.UnauthorizedError:
                    raise
                except Exception as e2:
                    last = e2
                    log.warning("login %s: %s", mask_proxy(u), short_error(e2))
            if not ok:
                raise last
        log.info(
            "FunPay: %s id=%s proxy=%s",
            self.account.username,
            self.account.id,
            mask_proxy(self.proxy_url),
        )
        return self.account

    def call(self, fn: Callable, *args, **kwargs):
        with self._lock:
            return fn(*args, **kwargs)

    def snapshot(self) -> dict[str, Any]:
        acc = self.account
        try:
            self.call(self._do_get, False)
        except Exception as e:
            log.warning("refresh: %s", short_error(e))
        return {
            "username": acc.username or "—",
            "id": acc.id or "—",
            "sales": acc.active_sales if acc.active_sales is not None else "—",
            "purchases": acc.active_purchases if acc.active_purchases is not None else "—",
            "balance": self.read_balance(),
            "proxy": mask_proxy(self.proxy_url),
        }

    def read_balance(self) -> str:
        html = self.account.html or ""
        text = _balance_from_html(html)
        if text:
            return text
        try:
            lots = self.my_lots()
            if lots:
                bal = self.call(self.account.get_balance, int(lots[0].id))
                return f"{bal.available_rub:g} ₽  ·  всего {bal.total_rub:g} ₽"
        except Exception as e:
            log.warning("balance: %s", short_error(e))
        try:
            r = self.call(
                self.account.method,
                "get",
                "account/balance",
                {"accept": "*/*"},
                {},
                False,
                False,
            )
            text = _balance_from_html(r.content.decode("utf-8", "ignore"))
            if text:
                return text
        except Exception as e:
            log.warning("balance page: %s", short_error(e))
        return "—"

    def my_lots(self):
        profile = self.call(self.account.get_user, self.account.id)
        return profile.get_common_lots()

    def find_lot_id(self, title: str | None, subcategory_id: int | None = None) -> int | None:
        if not title:
            return None
        for lot in self.my_lots():
            if subcategory_id and lot.subcategory and lot.subcategory.id != subcategory_id:
                continue
            if (lot.title or lot.description) == title:
                return int(lot.id)
        return None

    def chats(self):
        return list(self.call(self.account.get_chats, True).values())

    def chat_history(self, chat_id: int, last_message_id: int = 99999999999999999):
        try:
            return self.node_messages(chat_id)
        except Exception:
            return self.call(self.account.get_chat_history, chat_id, last_message_id)

    def node_messages(self, chat_id: int) -> list:
        from types import SimpleNamespace

        acc = self.account
        objects = [
            {
                "type": "chat_node",
                "id": int(chat_id),
                "tag": "00000000",
                "data": {"node": int(chat_id), "last_message": -1, "content": ""},
            }
        ]
        payload = {
            "objects": json.dumps(objects),
            "request": False,
            "csrf_token": acc.csrf_token,
        }
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        }

        def _post():
            r = acc.method("post", "runner/", headers, payload, False, True)
            return r.json()

        data = self.call(_post)
        rows = []
        for obj in data.get("objects") or []:
            if obj.get("type") != "chat_node":
                continue
            for m in (obj.get("data") or {}).get("messages") or []:
                aid = int(m.get("author") or 0)
                html = m.get("html") or ""
                soup = BeautifulSoup(html, "html.parser")
                el = soup.select_one("div.message-text, .chat-msg-text, .message-text, .default-text")
                text = el.get_text(" ", strip=True) if el else soup.get_text(" ", strip=True)
                mark = getattr(acc, "bot_character", "\u2064") or ""
                by_bot = bool(mark and (text or "").startswith(mark))
                if by_bot and mark:
                    text = text.replace(mark, "", 1)
                name_el = soup.select_one(".media-user-name a, a.chat-msg-author, .media-user-name")
                parsed = name_el.get_text(" ", strip=True) if name_el else None
                author = acc.username if aid == acc.id else parsed
                rows.append(
                    SimpleNamespace(
                        id=int(m.get("id") or 0),
                        author_id=aid,
                        author=author,
                        text=text,
                        by_bot=by_bot or aid == acc.id,
                        chat_id=int(chat_id),
                        html=html,
                        ts=parse_msg_time(html, m if isinstance(m, dict) else None),
                    )
                )
        return rows

    def last_in_chat(self, chat_id: int):
        hist = self.node_messages(chat_id)
        return hist[-1] if hist else None

    def last_foreign(self, chat_id: int):
        """Последнее сообщение в чате, если оно не наше. Иначе None — не лезем в историю."""
        msg = self.last_in_chat(chat_id)
        if msg is None:
            return None
        my = int(self.account.id or 0)
        aid = int(getattr(msg, "author_id", 0) or 0)
        if not aid or aid == my:
            return None
        return msg

    def remember_msg(self, chat_id: int, msg_id: int) -> None:
        cid, mid = int(chat_id), int(msg_id or 0)
        if mid <= 0:
            return
        with self._meta:
            prev = self._seen_ids.get(cid, 0)
            if mid > prev:
                self._seen_ids[cid] = mid

    def already_seen(self, chat_id: int, msg_id: int) -> bool:
        mid = int(msg_id or 0)
        if mid <= 0:
            return False
        with self._meta:
            return mid <= self._seen_ids.get(int(chat_id), 0)

    def claim_msg(self, chat_id: int, msg_id: int) -> bool:
        """True, если это мы первые увидели id. Иначе другой поток уже отвечает."""
        cid, mid = int(chat_id), int(msg_id or 0)
        if mid <= 0:
            return True
        with self._meta:
            if mid <= self._seen_ids.get(cid, 0):
                return False
            self._seen_ids[cid] = mid
            return True

    def is_fresh(self, msg, max_age: int = LLM_MAX_AGE) -> bool:
        """Свежее входящее — не история час назад. Без часов не режем: live-событие / poll уже проверил список."""
        ts = getattr(msg, "ts", None)
        if ts is None:
            ts = parse_msg_time(getattr(msg, "html", "") or "")
        if ts is None:
            return True
        age = time.time() - float(ts)
        return -60 <= age <= max_age

    def note_sent(self, chat_id: int, text: str) -> None:
        key = (text or "").strip()[:180]
        with self._meta:
            bucket = self._sent.setdefault(int(chat_id), [])
            bucket.append((key, time.time()))
            self._sent[int(chat_id)] = [(t, ts) for t, ts in bucket if time.time() - ts < 180][-20:]

    def we_sent(self, chat_id: int, text: str) -> bool:
        key = (text or "").strip()[:180]
        if not key:
            return False
        with self._meta:
            rows = list(self._sent.get(int(chat_id), []))
        for t, ts in rows:
            if t == key or key.startswith(t[:40]) or t.startswith(key[:40]):
                return True
        return False

    def send_text(self, chat_id: int, text: str):
        acc = self.account
        mark = getattr(acc, "bot_character", "\u2064") or ""
        body = f"{mark}{text}" if text else ""
        request = {
            "action": "chat_message",
            "data": {"node": int(chat_id), "last_message": -1, "content": body},
        }
        objects = [
            {
                "type": "chat_node",
                "id": int(chat_id),
                "tag": "00000000",
                "data": {"node": int(chat_id), "last_message": -1, "content": ""},
            }
        ]
        payload = {
            "objects": json.dumps(objects),
            "request": json.dumps(request),
            "csrf_token": acc.csrf_token,
        }
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        }

        def _post():
            r = acc.method("post", "runner/", headers, payload, False, True)
            data = r.json()
            resp = data.get("response") or {}
            err = resp.get("error")
            if err:
                raise RuntimeError(str(err))
            return data

        try:
            data = self.call(_post)
        except Exception:
            self.refresh_session()
            data = self.call(_post)
        self.note_sent(int(chat_id), text)
        mid = 0
        try:
            objs = data.get("objects") or []
            msgs = ((objs[0].get("data") or {}).get("messages") if objs else None) or []
            if msgs:
                mid = int((msgs[-1] or {}).get("id") or 0)
        except Exception:
            mid = 0
        if mid:
            self.remember_msg(int(chat_id), mid)
        runner = getattr(acc, "runner", None)
        if runner and isinstance(chat_id, int):
            try:
                if mid:
                    runner.mark_as_by_bot(int(chat_id), mid)
                runner.update_last_message(int(chat_id), text)
            except Exception:
                log.debug("runner mark sent", exc_info=True)
        log.info("sent chat %s (%s chars)", chat_id, len(text or ""))
        return data

    def get_order(self, order_id: str):
        return self.call(self.account.get_order, order_id)

    def refund(self, order_id: str):
        return self.call(self.account.refund, order_id)

    def sells(self, **kwargs):
        _next, orders = self.call(self.account.get_sells, **kwargs)
        return orders

    def lot_fields(self, lot_id: int) -> LotFields:
        return self.call(self.account.get_lot_fields, lot_id)

    def save_lot(self, fields: LotFields):
        fields.renew_fields()
        return self.call(self.account.save_lot, fields)

    def delete_lot(self, lot_id: int):
        fields = self.lot_fields(lot_id)
        d = dict(fields.fields)
        d["deleted"] = "1"
        d["csrf_token"] = self.account.csrf_token
        fields.set_fields(d)
        return self.call(self.account.save_lot, fields)

    def set_active(self, lot_id: int, active: bool):
        fields = self.lot_fields(lot_id)
        fields.active = active
        return self.save_lot(fields)

    def set_price(self, lot_id: int, price: float):
        fields = self.lot_fields(lot_id)
        fields.price = price
        return self.save_lot(fields)

    def raise_all(self) -> list[str]:
        notes = []
        cats = {}
        for lot in self.my_lots():
            sub = getattr(lot, "subcategory", None)
            cat = getattr(sub, "category", None) if sub else None
            if cat is None:
                continue
            cats[cat.id] = cat
        for cid, cat in cats.items():
            try:
                ok = self.call(self.account.raise_lots, cid)
                notes.append(f"{cat.name}: {'ок' if ok else 'кулдаун'}")
            except Exception as e:
                notes.append(f"{cat.name}: {short_error(e)}")
            time.sleep(1.2)
        self._raise_at = time.time() + 4 * 3600
        return notes

    def maybe_raise(self) -> list[str] | None:
        if time.time() < self._raise_at:
            return None
        return self.raise_all()

    def create_lot(
        self,
        node_id: int,
        *,
        title_ru: str,
        title_en: str | None = None,
        description_ru: str = "",
        description_en: str | None = None,
        price: float,
        amount: int = 1,
        active: bool = True,
        deactivate_after_sale: bool = False,
        extra: dict | None = None,
    ) -> LotFields:
        fields = {
            "offer_id": "0",
            "node_id": str(node_id),
            "csrf_token": self.account.csrf_token,
            "fields[summary][ru]": title_ru,
            "fields[summary][en]": title_en or title_ru,
            "fields[desc][ru]": description_ru,
            "fields[desc][en]": description_en or description_ru,
            "price": str(price),
            "amount": str(amount),
            "active": "on" if active else "",
            "deactivate_after_sale": "on" if deactivate_after_sale else "",
            "location": "trade",
        }
        if extra:
            fields.update({str(k): str(v) for k, v in extra.items()})
        lot = LotFields(0, fields)
        lot.title_ru = title_ru
        lot.title_en = title_en or title_ru
        lot.description_ru = description_ru
        lot.description_en = description_en or description_ru
        lot.price = price
        lot.amount = amount
        lot.active = active
        lot.deactivate_after_sale = deactivate_after_sale
        lot.renew_fields()
        fd = dict(lot.fields)
        fd["offer_id"] = "0"
        fd["node_id"] = str(node_id)
        fd["csrf_token"] = self.account.csrf_token
        lot.set_fields(fd)
        self.call(self.account.save_lot, lot)
        profile = self.call(self.account.get_user, self.account.id)
        for found in reversed(profile.get_common_lots()):
            if (found.title or found.description) == title_ru:
                lot.lot_id = int(found.id)
                break
        return lot

    def listen(self, on_event):
        self.account.runner = None
        runner = Runner(self.account)
        for event in runner.listen(requests_delay=max(FUNPAY_DELAY, 6), ignore_exceptions=True):
            try:
                on_event(event)
            except Exception:
                log.exception("event handler")

    def refresh_session(self) -> None:
        with self._lock:
            self._do_get(update_phpsessid=not bool(PHPSESSID))

    def maybe_refresh(self, every: int = 40 * 60) -> None:
        last = int(getattr(self.account, "last_update", 0) or 0)
        if time.time() - last < every:
            return
        try:
            self.refresh_session()
        except Exception as e:
            log.warning("session refresh: %s", short_error(e))

    def poll_incoming(self) -> list[tuple]:
        """Только чаты, где сменился превью. Без истории на весь список."""
        if getattr(self, "_backoff_until", 0) > time.time():
            return []
        self.maybe_refresh()
        try:
            chats = self.call(self._list_chats)
        except Exception as e:
            sc = getattr(e, "status_code", None)
            if sc == 429:
                self._backoff_until = time.time() + 45
                log.warning("FunPay 429 — пауза 45 с")
                return []
            if sc in {400, 401, 403}:
                self.refresh_session()
                chats = self.call(self._list_chats)
            else:
                raise
        out: list[tuple] = []
        my = int(self.account.id or 0)
        for cid, name, preview, when in chats:
            old = self._previews.get(cid)
            self._previews[cid] = preview
            if old is None or preview == old:
                continue
            if preview_from_us(preview):
                log.info("чат %s: превью наше (%s), в тг не зеркалю", cid, preview[:40])
                continue
            if not chat_preview_fresh(when):
                log.info("чат %s: превью старое (%s), нейронку не зову", cid, when)
                continue
            try:
                msg = self.last_foreign(cid)
            except Exception as e:
                sc = getattr(e, "status_code", None)
                log.warning("history %s: %s", cid, short_error(e))
                if sc == 429:
                    self._backoff_until = time.time() + 45
                    break
                continue
            if msg is None:
                log.info("чат %s: последнее наше, в тг не зеркалю", cid)
                continue
            if int(getattr(msg, "author_id", 0) or 0) == my:
                continue
            mid = int(getattr(msg, "id", 0) or 0)
            if self.already_seen(cid, mid):
                continue
            if not self.is_fresh(msg):
                log.info("чат %s: сообщение не свежее, нейронку не зову", cid)
                self.remember_msg(cid, mid)
                continue
            if not getattr(msg, "author", None):
                msg.author = name
            out.append((cid, name, msg))
            time.sleep(0.8)
        return out

    def _list_chats(self) -> list[tuple[int, str, str, str]]:
        acc = self.account
        from FunPayAPI.common import utils as fp_utils

        chats_obj = {
            "type": "chat_bookmarks",
            "id": acc.id,
            "tag": fp_utils.random_tag(),
            "data": False,
        }
        payload = {
            "objects": json.dumps([chats_obj]),
            "request": False,
            "csrf_token": acc.csrf_token,
        }
        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        }
        r = acc.method("post", "https://funpay.com/runner/", headers, payload, False, True)
        data = r.json()
        html = ""
        for obj in data.get("objects") or []:
            if obj.get("type") == "chat_bookmarks":
                html = (obj.get("data") or {}).get("html") or ""
        soup = BeautifulSoup(html, "html.parser")
        nodes = soup.select("a.contact-item") or soup.select("a[data-id]")
        rows = []
        for a in nodes:
            try:
                cid = int(a.get("data-id"))
            except (TypeError, ValueError):
                continue
            name_el = a.select_one(".media-user-name, .contact-item-name, .chat-item-title")
            msg_el = a.select_one(".contact-item-message, .chat-item-message, .media-user-status")
            time_el = a.select_one(".contact-item-time, .chat-item-time")
            name = name_el.get_text(strip=True) if name_el else str(cid)
            preview = msg_el.get_text(strip=True) if msg_el else ""
            when = time_el.get_text(strip=True) if time_el else ""
            rows.append((cid, name, preview, when))
        if not rows:
            log.warning("чат-лист пуст, html %s байт", len(html))
        return rows


def _balance_from_html(html: str) -> str | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for sel in (
        "span.badge-balance",
        ".badge-balance",
        "a.user-link-balance",
        ".user-link-balance",
        "span.balance",
        "a[href*='account/balance']",
    ):
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t and any(ch.isdigit() for ch in t):
                return t
    m = re.search(r"([\d\s]+[.,]\d{1,2})\s*₽", html)
    if m:
        return m.group(1).strip() + " ₽"
    return None
