from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types import InlineKeyboardMarkup as KB
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.states import Form
from bot.keyboards import (
    I_BOLT,
    I_BOT,
    I_CHAT,
    I_CHART,
    I_DOC,
    I_EYE,
    I_GEAR,
    I_HOME,
    I_INFO,
    I_JSON,
    I_KEY,
    I_LINK,
    I_LOCK,
    I_LOTS,
    I_MONEY,
    I_NO,
    I_OK,
    I_ON,
    I_ORDERS,
    I_PLUS,
    I_STOCK,
    I_UP,
    I_USER,
    I_WARN,
    I_WRITE,
    back_to,
    btn,
    cfg_menu,
    em,
    llm_gate_kb,
    llm_menu,
    llm_train_kb,
    menu,
    shop_menu,
)

from core.config import ADMIN_ID, AUTO_RAISE, STOCK_LOW, TG_TOKEN
from core.errors import short_error
from funpay.service import FunPayService
import models.llm as llm_mod
from funpay.lots import create_from_items, parse_payload
from core.proxy import for_aiohttp, from_fields, mask_proxy
from core.storage import (
    append_stock,
    fp_commands,
    llm_cfg,
    llm_system,
    lot_map,
    match_command,
    match_reply,
    pop_stock,
    proxy_fields,
    proxy_url,
    read_stock,
    save_fp_commands,
    save_settings,
    settings,
    stock_low,
    write_stock,
)

log = logging.getLogger("tg")
router = Router()
fp = FunPayService()
bot: Bot | None = None
loop: asyncio.AbstractEventLoop | None = None


def only_admin(handler):
    async def wrap(event, *a, **kw):
        uid = event.from_user.id if event.from_user else 0
        if uid != ADMIN_ID:
            with suppress(Exception):
                if isinstance(event, Message):
                    await event.answer(f"{em(I_NO, '❌')} Нет доступа.")
            return
        return await handler(event, *a, **kw)

    return wrap


def dashboard(snap: dict) -> str:
    nick = html.escape(str(snap.get("username") or "—"))
    bal = html.escape(str(snap.get("balance") or "—"))
    return (
        f"{em(I_USER, '👤')} <b>{nick}</b>  <code>#{snap.get('id')}</code>\n"
        f"<blockquote>"
        f"{em(I_MONEY, '💰')} баланс     {bal}\n"
        f"{em(I_CHART, '📈')} продажи    {snap.get('sales')}\n"
        f"{em(I_ORDERS, '📦')} покупки    {snap.get('purchases')}"
        f"</blockquote>"
    )


async def notify(text: str, kb: KB | None = None) -> None:
    if not bot:
        return
    with suppress(Exception):
        await bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="HTML")


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return await m.answer(f"{em(I_NO, '❌')} Нет доступа.")
    await state.clear()
    snap = await asyncio.to_thread(fp.snapshot)
    await m.answer(dashboard(snap), parse_mode="HTML", reply_markup=menu())


@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.clear()
    snap = await asyncio.to_thread(fp.snapshot)
    await c.message.edit_text(dashboard(snap), parse_mode="HTML", reply_markup=menu())
    await c.answer()


@router.callback_query(F.data == "chats")
async def cb_chats(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    chats = await asyncio.to_thread(fp.chats)
    kb = InlineKeyboardBuilder()
    for ch in (chats or [])[:20]:
        mark = "● " if getattr(ch, "unread", False) else ""
        kb.row(btn(f"{mark}{ch.name}"[:60], f"chat:{ch.id}", I_CHAT))
    kb.row(btn("меню", "home", I_HOME, "primary"))
    await c.message.edit_text(f"{em(I_CHAT, '💬')} <b>чаты</b>", parse_mode="HTML", reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("chat:"))
async def cb_chat(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    cid = int(c.data.split(":")[1])
    hist = await asyncio.to_thread(fp.chat_history, cid)
    lines = []
    for msg in (hist or [])[-15:]:
        who = html.escape(str(getattr(msg, "author", "") or "") or "—")
        text = html.escape((msg.text or "")[:400])
        lines.append(f"<b>{who}</b>: {text}")
    body = "\n".join(lines) or "пусто"
    if len(body) > 3500:
        body = "…\n" + body[-3490:]
    kb = KB(
        inline_keyboard=[
            [btn("ответить", f"rpl:{cid}", I_WRITE, "primary")],
            [btn("чаты", "chats", I_CHAT)],
        ]
    )
    await c.message.edit_text(f"{em(I_CHAT, '💬')} <b>чат</b> <code>{cid}</code>\n\n{body}", parse_mode="HTML", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("rpl:"))
async def cb_rpl(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    cid = int(c.data.split(":")[1])
    await state.set_state(Form.reply_chat)
    await state.update_data(chat_id=cid)
    await c.message.answer(
        f"{em(I_WRITE, '✍️')} Напиши текст для чата <code>{cid}</code>. /cancel — отмена.",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await m.answer(f"{em(I_OK, '✅')} ок", parse_mode="HTML", reply_markup=menu())


@router.message(Form.reply_chat)
async def do_reply(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    await state.clear()
    try:
        await asyncio.to_thread(fp.send_text, int(data["chat_id"]), m.text or "")
        await m.answer(f"{em(I_OK, '✅')} ушло в FunPay", parse_mode="HTML", reply_markup=menu())
    except Exception as e:
        await m.answer(
            f"{em(I_NO, '❌')} не ушло: {html.escape(short_error(e))}",
            parse_mode="HTML",
            reply_markup=menu(),
        )


@router.callback_query(F.data == "orders")
async def cb_orders(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    sells = await asyncio.to_thread(fp.sells)
    kb = InlineKeyboardBuilder()
    for o in (sells or [])[:20]:
        kb.row(
            btn(
                f"{o.id} · {o.price} · {o.buyer_username}"[:64],
                f"ord:{o.id}",
                I_ORDERS,
            )
        )
    kb.row(btn("меню", "home", I_HOME, "primary"))
    await c.message.edit_text(f"{em(I_ORDERS, '📦')} <b>заказы</b>", parse_mode="HTML", reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("ord:"))
async def cb_ord(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    oid = c.data.split(":", 1)[1]
    o = await asyncio.to_thread(fp.get_order, oid)
    text = (
        f"{em(I_ORDERS, '📦')} <b>{html.escape(o.id)}</b>\n"
        f"{html.escape(o.title or o.short_description or '')}\n"
        f"{em(I_MONEY, '💰')} {o.sum} · {html.escape(o.buyer_username)}\n"
        f"{em(I_INFO, 'ℹ️')} статус: {o.status}"
    )
    kb = KB(
        inline_keyboard=[
            [
                btn("выдать", f"give:{oid}", I_OK, "success"),
                btn("написать", f"rpl:{o.buyer_id}", I_WRITE, "primary"),
            ],
            [btn("возврат", f"ref:{oid}", I_NO, "danger")],
            [btn("заказы", "orders", I_ORDERS)],
        ]
    )
    await c.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("give:"))
async def cb_give(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    oid = c.data.split(":", 1)[1]
    o = await asyncio.to_thread(fp.get_order, oid)
    lot_id = _lot_id_from_order(o)
    qty = _order_qty(o)
    items = pop_stock(lot_id, qty) if lot_id else None
    if not items:
        await c.answer("Сток пуст", show_alert=True)
        await notify(
            f"{em(I_WARN, '❗️')} Сток пуст для заказа <code>{html.escape(oid)}</code>. "
            f"Выдай вручную в чат покупателя {html.escape(o.buyer_username)}.",
        )
        return
    payload = "\n".join(items)
    await asyncio.to_thread(
        fp.send_text,
        o.buyer_id,
        f"Ваш заказ {o.id}:\n{payload}",
    )
    await c.answer("Выдано")
    await notify(f"{em(I_OK, '✅')} Выдан заказ <code>{html.escape(oid)}</code> ({len(items)} шт.).")


@router.callback_query(F.data.startswith("ref:"))
async def cb_ref(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    oid = c.data.split(":", 1)[1]
    await asyncio.to_thread(fp.refund, oid)
    await c.answer("Возврат отправлен")
    await c.message.answer(
        f"{em(I_NO, '❌')} Возврат <code>{html.escape(oid)}</code>",
        parse_mode="HTML",
        reply_markup=menu(),
    )


@router.callback_query(F.data == "cat_shop")
async def cb_cat_shop(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await c.message.edit_text(
        f"{em(I_LOTS, '📂')} <b>лоты</b>\nсписок, сток, json, поднятие",
        parse_mode="HTML",
        reply_markup=shop_menu(),
    )
    await c.answer()


@router.callback_query(F.data == "lots")
async def cb_lots(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    lots = await asyncio.to_thread(fp.my_lots)
    kb = InlineKeyboardBuilder()
    for lot in lots[:30]:
        kb.row(
            btn(
                f"#{lot.id} {lot.title or lot.description or ''}"[:60],
                f"lot:{lot.id}",
                I_LOTS,
            )
        )
    kb.row(btn("назад", "cat_shop", I_LOTS))
    await c.message.edit_text(
        f"{em(I_LOTS, '📂')} <b>лоты</b>  {len(lots)}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await c.answer()


@router.callback_query(F.data.startswith("lot:"))
async def cb_lot(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    lid = int(c.data.split(":")[1])
    fields = await asyncio.to_thread(fp.lot_fields, lid)
    st = len(read_stock(lid))
    text = (
        f"{em(I_LOTS, '📂')} <b>лот {lid}</b>\n"
        f"{html.escape(fields.title_ru or '')}\n"
        f"{em(I_MONEY, '💰')} цена {fields.price} · кол-во {fields.amount}\n"
        f"{em(I_ON if fields.active else I_NO, '✅' if fields.active else '❌')} "
        f"{'активен' if fields.active else 'выкл'} · сток {st}"
    )
    kb = KB(
        inline_keyboard=[
            [
                btn("вкл" if not fields.active else "выкл", f"act:{lid}", I_ON if not fields.active else I_NO, "success" if not fields.active else "danger"),
                btn("цена", f"prc:{lid}", I_MONEY),
            ],
            [
                btn("сток+", f"stk:{lid}", I_PLUS),
                btn("удалить", f"del:{lid}", I_NO, "danger"),
            ],
            [btn("список", "lots", I_LOTS)],
        ]
    )
    await c.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data.startswith("act:"))
async def cb_act(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    lid = int(c.data.split(":")[1])
    fields = await asyncio.to_thread(fp.lot_fields, lid)
    await asyncio.to_thread(fp.set_active, lid, not fields.active)
    await c.answer("Сохранено")
    c.data = f"lot:{lid}"
    await cb_lot(c)


@router.callback_query(F.data.startswith("prc:"))
async def cb_prc(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    lid = int(c.data.split(":")[1])
    await state.set_state(Form.price)
    await state.update_data(lot_id=lid)
    await c.message.answer(f"{em(I_MONEY, '💰')} Новая цена числом:", parse_mode="HTML")
    await c.answer()


@router.message(Form.price)
async def do_price(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    try:
        price = float((m.text or "0").replace(",", "."))
    except ValueError:
        await m.answer(f"{em(I_WARN, '❗️')} Нужно число, например <code>150.5</code>.", parse_mode="HTML")
        return
    await state.clear()
    await asyncio.to_thread(fp.set_price, int(data["lot_id"]), price)
    await m.answer(f"{em(I_OK, '✅')} Цена обновлена.", parse_mode="HTML", reply_markup=menu())


@router.callback_query(F.data.startswith("del:"))
async def cb_del(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    lid = int(c.data.split(":")[1])
    await asyncio.to_thread(fp.delete_lot, lid)
    await c.answer("Удалён")
    await cb_lots(c)


@router.callback_query(F.data.startswith("stk:"))
async def cb_stk(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    lid = int(c.data.split(":")[1])
    await state.set_state(Form.stock_add)
    await state.update_data(lot_id=lid)
    await c.message.answer(
        f"{em(I_STOCK, '📁')} Пришли строки стока (каждая с новой строки) или .txt файл.",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(Form.stock_add)
async def do_stock(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    await state.clear()
    lid = int(data["lot_id"])
    lines: list[str] = []
    if m.document:
        f = await m.bot.download(m.document)
        lines = [ln.strip() for ln in f.read().decode("utf-8").splitlines() if ln.strip()]
    elif m.text:
        lines = [ln.strip() for ln in m.text.splitlines() if ln.strip()]
    n = append_stock(lid, lines)
    await m.answer(
        f"{em(I_STOCK, '📁')} Сток лота <code>{lid}</code>: {n} шт.",
        parse_mode="HTML",
        reply_markup=menu(),
    )


@router.callback_query(F.data == "stock")
async def cb_stock(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    m = lot_map()
    if not m:
        lots = await asyncio.to_thread(fp.my_lots)
        lines = [f"#{lot.id}: {len(read_stock(lot.id))} шт." for lot in lots[:40]]
    else:
        lines = [f"лот {k}: {len(read_stock(k))} ({v})" for k, v in m.items()]
    body = "\n".join(lines) or "пусто"
    await c.message.edit_text(
        f"{em(I_STOCK, '📁')} <b>сток</b>\n<code>{html.escape(body)}</code>",
        parse_mode="HTML",
        reply_markup=back_to("cat_shop", "лоты"),
    )
    await c.answer()


@router.callback_query(F.data == "json")
async def cb_json(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.json_wait)
    await c.message.answer(
        f"{em(I_JSON, '📄')} Пришли JSON-файл или текст.\n"
        "Каждый элемент — отдельный лот в своей подкатегории "
        "(<code>node_id</code>), количество (<code>amount</code>) и сток независимы.\n"
        "Пример: examples/lots.json",
        parse_mode="HTML",
        reply_markup=back_to("cat_shop", "лоты"),
    )
    await c.answer()


@router.message(Form.json_wait)
async def do_json(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    raw = ""
    if m.document:
        f = await m.bot.download(m.document)
        raw = f.read().decode("utf-8")
    elif m.text:
        raw = m.text
    try:
        items = parse_payload(raw)
    except Exception as e:
        await m.answer(
            f"{em(I_NO, '❌')} JSON не разобран: {html.escape(str(e))}",
            parse_mode="HTML",
        )
        return
    await m.answer(
        f"{em(I_JSON, '📄')} Создаю {len(items)} лот(ов), пауза ~1 с на лот…",
        parse_mode="HTML",
    )
    log_lines = await asyncio.to_thread(create_from_items, fp, items)
    await state.clear()
    await m.answer(
        f"{em(I_OK, '✅')} " + html.escape("\n".join(log_lines)[:3900]),
        parse_mode="HTML",
        reply_markup=menu(),
    )


@router.message(F.document)
async def any_json_file(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    name = (m.document.file_name or "").lower()
    if not name.endswith(".json"):
        return
    await state.set_state(Form.json_wait)
    await do_json(m, state)


def _apply_proxy() -> str | None:
    url = proxy_url()
    fp.set_proxy(url)
    return url


def _proxy_text() -> str:
    f = proxy_fields()
    return (
        f"{em(I_LOCK, '🔒')} Один прокси на Telegram, FunPay и нейронку.\n"
        f"{em(I_USER, '👤')} user: <code>{html.escape(f.get('user') or '—')}</code>\n"
        f"{em(I_KEY, '🔒')} password: {'***' if f.get('password') else '—'}\n"
        f"{em(I_LINK, '↗️')} ip: <code>{html.escape((f.get('ip') or '—') + ((':' + str(f['port'])) if f.get('port') else ''))}</code>\n"
        f"{em(I_GEAR, '⚙')} type: <code>{html.escape(f.get('type') or 'socks5')}</code>\n"
        f"{em(I_EYE, '👀')} сейчас: {html.escape(mask_proxy(proxy_url()))}\n\n"
        f"{em(I_INFO, 'ℹ️')} Telegram подхватит прокси после перезапуска бота."
    )


@router.callback_query(F.data == "proxy")
async def cb_proxy(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.clear()
    kb = KB(
        inline_keyboard=[
            [btn("задать поля", "proxy_set", I_WRITE, "primary")],
            [btn("выключить", "proxy_off", I_NO, "danger")],
            [btn("назад", "cfg", I_GEAR)],
        ]
    )
    await c.message.edit_text(f"{em(I_LOCK, '🔒')} <b>прокси</b>\n" + _proxy_text(), parse_mode="HTML", reply_markup=kb)
    await c.answer()


@router.callback_query(F.data == "proxy_set")
async def cb_proxy_set(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.proxy_fields)
    await c.message.answer(
        f"{em(I_WRITE, '✍️')} Четыре строки (или через | ):\n"
        "<code>user\n"
        "password\n"
        "ip:port\n"
        "socks5</code>\n\n"
        "type: http / https / socks5\n"
        "Если логина нет — первая строка <code>-</code>",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(Form.proxy_fields)
async def do_proxy_fields(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    raw = (m.text or "").strip()
    lines = [x.strip() for x in raw.replace("|", "\n").splitlines() if x.strip()]
    if len(lines) < 3:
        await m.answer(
            f"{em(I_WARN, '❗️')} Нужно минимум 3 строки: user, password, ip. Четвёртая — type.",
            parse_mode="HTML",
        )
        return
    user, password, ip = lines[0], lines[1], lines[2]
    type_ = lines[3] if len(lines) > 3 else "socks5"
    if user in {"-", "нет", "none"}:
        user = ""
    if password in {"-", "нет", "none"}:
        password = ""
    port = ""
    if ip.startswith("["):
        inside, _, rest = ip[1:].partition("]")
        ip = inside
        if rest.startswith(":"):
            port = rest[1:]
    elif ip.count(":") == 1:
        ip, port = ip.rsplit(":", 1)
    elif ip.count(":") >= 2:
        # IPv6 без скобок: последние цифры после : — порт, если указан
        left, maybe_port = ip.rsplit(":", 1)
        if maybe_port.isdigit() and left.count(":") >= 2:
            ip, port = left, maybe_port
    try:
        url = from_fields(user=user, password=password, ip=ip, type_=type_, port=port)
    except Exception as e:
        await m.answer(f"{em(I_NO, '❌')} Не разобрал: {html.escape(str(e))}", parse_mode="HTML")
        return
    await state.clear()
    s = settings()
    s.update(
        {
            "proxy_user": user,
            "proxy_pass": password,
            "proxy_ip": ip,
            "proxy_port": port,
            "proxy_type": type_,
        }
    )
    save_settings(s)
    fp.set_proxy(url)
    await m.answer(
        f"{em(I_OK, '✅')} Прокси: {html.escape(mask_proxy(url))}\n"
        f"{em(I_INFO, 'ℹ️')} Перезапусти бота, чтобы Telegram пошёл через него.",
        parse_mode="HTML",
        reply_markup=menu(),
    )


@router.callback_query(F.data == "proxy_off")
async def cb_proxy_off(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    s = settings()
    s.update({"proxy_user": "", "proxy_pass": "", "proxy_ip": "", "proxy_port": "", "proxy_type": "socks5"})
    save_settings(s)
    fp.set_proxy(None)
    await cb_proxy(c, state)


def _llm_hub_text() -> str:
    c = llm_cfg()
    return (
        f"{em(I_BOT, '🤖')} <b>бот</b>\n"
        f"<blockquote>"
        f"{em(I_ON if c['on'] else I_NO, '✅' if c['on'] else '❌')} в чатах: {'вкл' if c['on'] else 'выкл'}\n"
        f"{em(I_BOT, '🤖')} модель: {html.escape(c['model'] or '—')}\n"
        f"{em(I_DOC, '📄')} примеры: {len(c.get('examples') or [])}"
        f"</blockquote>"
    )


def _llm_gate_text() -> str:
    c = llm_cfg()
    key = "задан" if c["key"] else "нет"
    return (
        f"{em(I_LINK, '↗️')} <b>шлюз</b>\n"
        f"URL: <code>{html.escape(c['url'] or 'https://varfungateway.com/v1')}</code>\n"
        f"ключ: {key}\n"
        f"model: <code>{html.escape(c['model'] or '—')}</code>\n"
        f"протокол: <code>{html.escape(c.get('protocol') or 'auto')}</code>"
    )


def _llm_train_text() -> str:
    c = llm_cfg()
    prompt = c.get("prompt") or "стандартный"
    notes = c.get("notes") or "нет"
    n_ex = len(c.get("examples") or [])
    return (
        f"{em(I_DOC, '📄')} <b>обучение</b>\n"
        f"промпт: {html.escape(prompt[:120])}\n"
        f"знания: {html.escape(notes[:120])}\n"
        f"примеры: {n_ex}"
    )


@router.callback_query(F.data == "llm")
async def cb_llm(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await c.message.edit_text(_llm_hub_text(), parse_mode="HTML", reply_markup=llm_menu(llm_cfg()["on"]))
    await c.answer()


@router.callback_query(F.data == "llm_gate")
async def cb_llm_gate(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await c.message.edit_text(_llm_gate_text(), parse_mode="HTML", reply_markup=llm_gate_kb())
    await c.answer()


@router.callback_query(F.data == "llm_train")
async def cb_llm_train(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await c.message.edit_text(_llm_train_text(), parse_mode="HTML", reply_markup=llm_train_kb())
    await c.answer()


@router.callback_query(F.data == "llm_url")
async def cb_llm_url(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.llm_url)
    await c.message.answer(
        f"{em(I_LINK, '↗️')} base url шлюза, например:\n"
        "<code>https://varfungateway.com/v1</code>\n"
        "хватит и домена — /chat/completions и /messages допишет сам.",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(Form.llm_url)
async def do_llm_url(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    s = settings()
    s["llm_url"] = (m.text or "").strip()
    save_settings(s)
    await m.answer(f"{em(I_OK, '✅')} URL сохранён.", parse_mode="HTML", reply_markup=menu())


@router.callback_query(F.data == "llm_key")
async def cb_llm_key(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.llm_key)
    await c.message.answer(f"{em(I_KEY, '🔒')} Кинь API-ключ.", parse_mode="HTML")
    await c.answer()


@router.message(Form.llm_key)
async def do_llm_key(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    s = settings()
    s["llm_key"] = (m.text or "").strip()
    save_settings(s)
    with suppress(Exception):
        await m.delete()
    await m.answer(f"{em(I_OK, '✅')} Ключ сохранён.", parse_mode="HTML", reply_markup=menu())


@router.callback_query(F.data == "llm_model")
async def cb_llm_model(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.llm_model)
    await c.message.answer(
        f"{em(I_BOT, '🤖')} id модели из каталога, например:\n"
        "<code>claude-sonnet-5</code> или <code>gpt-5.6-sol</code>",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(Form.llm_model)
async def do_llm_model(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    s = settings()
    s["llm_model"] = (m.text or "").strip()
    save_settings(s)
    await m.answer(f"{em(I_OK, '✅')} Модель сохранена.", parse_mode="HTML", reply_markup=menu())


@router.callback_query(F.data == "llm_prompt")
async def cb_llm_prompt(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.llm_prompt)
    cur = llm_cfg().get("prompt") or llm_mod.DEFAULT_SYSTEM
    await c.message.answer(
        f"{em(I_WRITE, '✍️')} Системный промпт нейронки. Пришли новый текст.\n\n"
        f"сейчас:\n<code>{html.escape(cur[:1500])}</code>",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(Form.llm_prompt)
async def do_llm_prompt(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    s = settings()
    s["llm_prompt"] = (m.text or "").strip()
    save_settings(s)
    await m.answer(f"{em(I_OK, '✅')} Промпт сохранён.", parse_mode="HTML", reply_markup=menu())


@router.callback_query(F.data == "llm_notes")
async def cb_llm_notes(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.llm_notes)
    cur = llm_cfg().get("notes") or "пусто"
    await c.message.answer(
        f"{em(I_DOC, '📄')} Знания магазина (факты, цены, правила). Пришли текст целиком — заменит старый.\n\n"
        f"сейчас:\n<code>{html.escape(cur[:1500])}</code>",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(Form.llm_notes)
async def do_llm_notes(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    await state.clear()
    s = settings()
    s["llm_notes"] = (m.text or "").strip()
    save_settings(s)
    await m.answer(f"{em(I_OK, '✅')} Знания сохранены.", parse_mode="HTML", reply_markup=menu())


@router.callback_query(F.data == "llm_ex")
async def cb_llm_ex(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.llm_example)
    await c.message.answer(
        f"{em(I_PLUS, '➕')} Пример для обучения.\n"
        "Первая строка — что пишет покупатель.\n"
        "Дальше — как должен ответить бот.",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(Form.llm_example)
async def do_llm_ex(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    raw = (m.text or "").strip()
    q, _, a = raw.partition("\n")
    q, a = q.strip(), a.strip()
    if not q or not a:
        await m.answer(
            f"{em(I_WARN, '❗️')} Нужны минимум две строки: вопрос и ответ.",
            parse_mode="HTML",
        )
        return
    await state.clear()
    s = settings()
    ex = list(s.get("llm_examples") or [])
    ex.append({"q": q, "a": a})
    s["llm_examples"] = ex[-40:]
    save_settings(s)
    await m.answer(
        f"{em(I_OK, '✅')} Пример добавлен ({len(s['llm_examples'])} шт.).",
        parse_mode="HTML",
        reply_markup=menu(),
    )


@router.callback_query(F.data == "llm_exclear")
async def cb_llm_exclear(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    s = settings()
    s["llm_examples"] = []
    save_settings(s)
    await cb_llm_train(c, state)


@router.callback_query(F.data.startswith("llm_proto:"))
async def cb_llm_proto(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    proto = c.data.split(":", 1)[1]
    s = settings()
    s["llm_protocol"] = proto
    save_settings(s)
    await cb_llm_gate(c, state)


@router.callback_query(F.data == "llm_tgl")
async def cb_llm_tgl(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    s = settings()
    s["llm_on"] = not llm_cfg()["on"]
    save_settings(s)
    await cb_llm(c, state)


@router.callback_query(F.data == "llm_test")
async def cb_llm_test(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    cfg = llm_cfg()
    if not cfg["url"] or not cfg["key"]:
        await c.answer("Сначала URL и ключ", show_alert=True)
        return
    await c.answer("Спрашиваю…")
    try:
        ans = await asyncio.to_thread(
            llm_mod.chat,
            cfg["url"],
            cfg["key"],
            "Ответь одним словом: ок",
            model=cfg["model"],
            system=llm_system(),
            proxy_url=fp.proxy_url,
            protocol=cfg.get("protocol") or "auto",
        )
        await c.message.answer(
            f"{em(I_BOT, '🤖')} {html.escape(ans[:2000])}",
            parse_mode="HTML",
        )
    except Exception as e:
        await c.message.answer(
            f"{em(I_NO, '❌')} {html.escape(short_error(e))}",
            parse_mode="HTML",
        )


def _cmds_text() -> str:
    rows = fp_commands()
    if not rows:
        return f"{em(I_BOLT, '⚡️')} команд нет. Добавь, например <code>!вызов</code>."
    lines = []
    for c in rows:
        name = html.escape(c.get("cmd") or "")
        reply = html.escape((c.get("reply") or "только в тг")[:80])
        lines.append(f"<code>!{name}</code> → {reply}")
    return (
        f"{em(I_BOLT, '⚡️')} <b>команды FunPay</b>\n"
        "Покупатель пишет <code>!вызов</code> — тебе в Telegram, нейронка молчит.\n\n"
        + "\n".join(lines)
    )


@router.callback_query(F.data == "cmds")
async def cb_cmds(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    for i, item in enumerate(fp_commands()):
        kb.row(btn(f"!{item.get('cmd')}", f"cmdel:{i}", I_NO, "danger"))
    kb.row(btn("добавить", "cmdadd", I_PLUS, "success"))
    kb.row(btn("назад", "cfg", I_GEAR))
    await c.message.edit_text(_cmds_text(), parse_mode="HTML", reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data == "cmdadd")
async def cb_cmdadd(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    await state.set_state(Form.cmd_add)
    await c.message.answer(
        f"{em(I_PLUS, '➕')} Первая строка — команда: <code>!вызов</code>\n"
        "Вторая (необязательно) — что ответить покупателю в FunPay.",
        parse_mode="HTML",
    )
    await c.answer()


@router.message(Form.cmd_add)
async def do_cmd_add(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID:
        return
    lines = [ln.strip() for ln in (m.text or "").splitlines() if ln.strip()]
    if not lines:
        await m.answer(f"{em(I_WARN, '❗️')} Пусто.", parse_mode="HTML")
        return
    cmd = lines[0].lstrip("!").strip()
    if not cmd:
        await m.answer(f"{em(I_WARN, '❗️')} Нужна команда.", parse_mode="HTML")
        return
    reply = "\n".join(lines[1:])
    await state.clear()
    cmds = fp_commands()
    low = cmd.lower()
    cmds = [c for c in cmds if (c.get("cmd") or "").lower() != low]
    cmds.append({"cmd": cmd, "reply": reply})
    save_fp_commands(cmds)
    await m.answer(
        f"{em(I_OK, '✅')} Команда <code>!{html.escape(cmd)}</code> сохранена.",
        parse_mode="HTML",
        reply_markup=menu(),
    )


@router.callback_query(F.data.startswith("cmdel:"))
async def cb_cmdel(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID:
        return
    try:
        idx = int(c.data.split(":", 1)[1])
    except ValueError:
        await c.answer()
        return
    cmds = fp_commands()
    if 0 <= idx < len(cmds):
        cmds.pop(idx)
        save_fp_commands(cmds)
    await cb_cmds(c, state)


@router.callback_query(F.data == "raise")
async def cb_raise(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    await c.answer("Поднимаю…")
    notes = await asyncio.to_thread(fp.raise_all)
    await c.message.answer(
        f"{em(I_UP, '⬆️')} Поднятие:\n" + html.escape("\n".join(notes)),
        parse_mode="HTML",
        reply_markup=menu(),
    )


@router.callback_query(F.data == "cfg")
async def cb_cfg(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    s = settings()
    s.setdefault("auto_raise", AUTO_RAISE)
    s.setdefault("auto_reply", True)
    text = (
        f"{em(I_GEAR, '⚙')} <b>настройки</b>\n"
        f"<blockquote>"
        f"{em(I_UP, '⬆️')} поднятие   {'вкл' if s.get('auto_raise') else 'выкл'}\n"
        f"{em(I_CHAT, '💬')} ответы     {'вкл' if s.get('auto_reply') else 'выкл'}\n"
        f"{em(I_STOCK, '📁')} сток min   {STOCK_LOW}\n"
        f"{em(I_BOT, '🤖')} нейронка   {'вкл' if llm_cfg()['on'] else 'выкл'}"
        f"</blockquote>"
    )
    await c.message.edit_text(text, parse_mode="HTML", reply_markup=cfg_menu())
    await c.answer()


@router.callback_query(F.data.startswith("tgl:"))
async def cb_tgl(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return
    key = c.data.split(":")[1]
    s = settings()
    s[key] = not s.get(key, True)
    save_settings(s)
    await cb_cfg(c)


def _order_qty(order) -> int:
    n = getattr(order, "amount", None)
    try:
        if n is not None and int(n) > 0:
            return int(n)
    except (TypeError, ValueError):
        pass
    for raw in (
        getattr(order, "description", None),
        getattr(order, "short_description", None),
        getattr(order, "title", None),
        getattr(order, "html", None),
    ):
        if not raw:
            continue
        m = re.search(r"(\d+)\s*шт", str(raw), re.I)
        if m:
            return max(1, int(m.group(1)))
    return 1


def _lot_id_from_order(order) -> int | None:
    html_s = getattr(order, "html", "") or ""
    m = re.search(r"offer\?id=(\d+)", html_s)
    if m:
        return int(m.group(1))
    title = getattr(order, "title", None) or getattr(order, "short_description", None) or getattr(
        order, "description", None
    )
    sub = getattr(order, "subcategory", None)
    sid = getattr(sub, "id", None) if sub else None
    return fp.find_lot_id(title, sid)


_seen_msg: dict[tuple, float] = {}


def _mine(msg) -> bool:
    aid = getattr(msg, "author_id", None)
    if aid is not None and fp.account.id is not None and int(aid) == int(fp.account.id):
        return True
    name = (getattr(msg, "author", None) or "").strip().lower()
    mine = (fp.account.username or "").strip().lower()
    return bool(name and mine and name == mine)


def _push_chat(chat_id, author: str, text: str) -> None:
    key = (int(chat_id), (text or "")[:120])
    now = time.time()
    if _seen_msg.get(key, 0) > now - 20:
        return
    _seen_msg[key] = now
    if len(_seen_msg) > 400:
        _seen_msg.clear()
    body = text.strip() if text else "медиа / пусто"
    log.info("fp msg %s %s: %s", chat_id, author, body[:80])
    if not (loop and bot):
        log.warning("нет telegram loop — сообщение не ушло в тг")
        return
    asyncio.run_coroutine_threadsafe(
        notify(
            f"{em(I_BOT if (author or '') == 'бот' else I_CHAT, '🤖' if (author or '') == 'бот' else '💬')} "
            f"<b>{html.escape(str(author or 'покупатель'))}</b>\n"
            f"{html.escape(body[:3500])}",
            KB(
                inline_keyboard=[
                    [btn("ответить", f"rpl:{chat_id}", I_WRITE, "primary")],
                    [btn("чат", f"chat:{chat_id}", I_CHAT)],
                ]
            ),
        ),
        loop,
    )


def _llm_for_chat(chat_id, author: str, text: str, author_id: int | None = None, msg=None) -> None:
    my = int(fp.account.id or 0)
    if author_id is None or int(author_id) == 0 or int(author_id) == my:
        log.info("нейронка молчит: author_id=%s (это мы или неизвестно)", author_id)
        return
    mid = int(getattr(msg, "id", 0) or 0) if msg is not None else 0
    if msg is not None and not fp.is_fresh(msg):
        log.info("нейронка молчит: сообщение не свежее chat=%s id=%s", chat_id, mid)
        if mid:
            fp.remember_msg(int(chat_id), mid)
        return
    if mid and not fp.claim_msg(int(chat_id), mid):
        return
    if fp.we_sent(int(chat_id), text or ""):
        return
    name = (author or "").strip().lower()
    mine = (fp.account.username or "").strip().lower()
    if name and mine and name == mine:
        return
    s = settings()
    if not s.get("auto_reply", True) or not (text or "").strip():
        return
    cmd = match_command(text)
    if cmd:
        if loop and bot:
            asyncio.run_coroutine_threadsafe(
                notify(
                    f"{em(I_BOLT, '⚡️')} команда <code>!{html.escape(cmd.get('cmd') or '')}</code>\n"
                    f"{em(I_USER, '👤')} {html.escape(str(author or 'покупатель'))}\n"
                    f"{html.escape((text or '')[:2000])}",
                    KB(
                        inline_keyboard=[
                            [btn("ответить", f"rpl:{chat_id}", I_WRITE, "primary")],
                            [btn("чат", f"chat:{chat_id}", I_CHAT)],
                        ]
                    ),
                ),
                loop,
            )
        reply = (cmd.get("reply") or "").strip()
        if reply:
            try:
                fp.send_text(int(chat_id), reply)
                if mid:
                    fp.remember_msg(int(chat_id), mid)
                _push_chat(chat_id, "бот", reply)
            except Exception as e:
                log.warning("send cmd: %s", short_error(e))
        elif mid:
            fp.remember_msg(int(chat_id), mid)
        return
    reply = match_reply(text)
    cfg = llm_cfg()
    if not reply and cfg.get("url") and cfg.get("key") and cfg.get("on", True):
        try:
            reply = llm_mod.chat(
                cfg["url"],
                cfg["key"],
                text,
                model=cfg["model"],
                system=llm_system(),
                proxy_url=fp.proxy_url,
                protocol=cfg.get("protocol") or "auto",
            )
        except Exception as e:
            log.warning("llm: %s", short_error(e))
            if loop and bot:
                asyncio.run_coroutine_threadsafe(
                    notify(f"{em(I_NO, '❌')} нейронка не ответила: {html.escape(short_error(e))}"),
                    loop,
                )
            return
    elif not reply:
        log.info("нейронка пропуск: on=%s url=%s", cfg.get("on"), bool(cfg.get("url") and cfg.get("key")))
    if reply:
        try:
            fp.send_text(int(chat_id), reply)
            if mid:
                fp.remember_msg(int(chat_id), mid)
            _push_chat(chat_id, "бот", reply)
        except Exception as e:
            log.warning("send: %s", short_error(e))


def handle_fp_event(event) -> None:
    from FunPayAPI.updater.events import LastChatMessageChangedEvent, NewMessageEvent, NewOrderEvent

    kind = type(event).__name__
    if kind.startswith("Initial") or kind in {"ChatsListChangedEvent", "OrdersListChangedEvent"}:
        return

    log.info("fp event %s", kind)

    if isinstance(event, LastChatMessageChangedEvent):
        # name в этом событии — всегда ник собеседника, не автор.
        # Свои «ку» отсюда улетали в тг как сообщения друга. Автор только из NewMessage / poll.
        return

    if isinstance(event, NewMessageEvent):
        msg = event.message
        text = msg.text or ""
        if _mine(msg) or getattr(msg, "by_bot", False) or fp.we_sent(msg.chat_id, text):
            mid = int(getattr(msg, "id", 0) or 0)
            if mid:
                fp.remember_msg(int(msg.chat_id), mid)
            return
        aid = getattr(msg, "author_id", None)
        if aid is None or int(aid) == 0:
            log.info("чат %s: нет author_id — в тг не зеркалю", msg.chat_id)
            return
        who = (msg.author or "").strip() or "покупатель"
        if who.lower() == (fp.account.username or "").strip().lower():
            return
        _push_chat(msg.chat_id, who, text)
        stack = getattr(event, "stack", None)
        if stack:
            batch = [
                e.message
                for e in stack.get_stack()
                if not _mine(e.message) and not getattr(e.message, "by_bot", False)
            ]
            last = batch[-1] if batch else None
            if last is not None and int(getattr(last, "id", 0) or 0) != int(getattr(msg, "id", 0) or 0):
                return
        _llm_for_chat(msg.chat_id, who, text, aid, msg)
        return
    elif isinstance(event, NewOrderEvent):
        o = event.order
        lot_id = _lot_id_from_order(o) if hasattr(o, "html") else None
        try:
            full = fp.get_order(o.id)
            lot_id = _lot_id_from_order(full) or lot_id
            qty = _order_qty(o)
            if getattr(full, "amount", None):
                qty = _order_qty(full)
            buyer_chat = full.buyer_id
        except Exception:
            full = o
            qty = _order_qty(o)
            buyer_chat = getattr(o, "buyer_id", None)

        delivered = False
        if lot_id:
            items = pop_stock(lot_id, qty)
            if items and buyer_chat:
                fp.send_text(buyer_chat, f"Ваш заказ {o.id}:\n" + "\n".join(items))
                delivered = True
                if stock_low(lot_id) and loop:
                    asyncio.run_coroutine_threadsafe(
                        notify(
                            f"{em(I_WARN, '❗️')} Сток лота <code>{lot_id}</code> на исходе: {len(read_stock(lot_id))} шт."
                        ),
                        loop,
                    )
        if loop:
            extra = "автовыдача ок" if delivered else "сток пуст — выдай вручную"
            asyncio.run_coroutine_threadsafe(
                notify(
                    f"{em(I_ORDERS, '📦')} Новый заказ <code>{html.escape(o.id)}</code>\n"
                    f"{html.escape(getattr(o, 'description', '') or '')}\n"
                    f"{em(I_MONEY, '💰')} {getattr(o, 'price', '')} · {html.escape(str(o.buyer_username))}\n"
                    f"{em(I_OK if delivered else I_WARN, '✅' if delivered else '❗️')} {extra}",
                    KB(
                        inline_keyboard=[
                            [btn("выдать", f"give:{o.id}", I_OK, "success")],
                            [btn("заказ", f"ord:{o.id}", I_ORDERS)],
                        ]
                    ),
                ),
                loop,
            )


def fp_loop():
    import threading
    import time

    while True:
        try:
            _apply_proxy()
            fp.login()
            if loop:
                asyncio.run_coroutine_threadsafe(
                    notify(
                        f"{em(I_OK, '✅')} FunPay онлайн: {html.escape(str(fp.account.username))}"
                    ),
                    loop,
                )
            break
        except Exception as e:
            log.warning("FunPay login: %s", short_error(e))
            time.sleep(12)

    def chat_poll():
        while True:
            try:
                if settings().get("auto_raise", AUTO_RAISE):
                    notes = fp.maybe_raise()
                    if notes and loop:
                        asyncio.run_coroutine_threadsafe(
                            notify(f"{em(I_UP, '⬆️')} Автоподнятие:\n" + html.escape("\n".join(notes))),
                            loop,
                        )
                for cid, name, msg in fp.poll_incoming():
                    text = getattr(msg, "text", None) or ""
                    if _mine(msg) or getattr(msg, "by_bot", False) or fp.we_sent(cid, text):
                        continue
                    _push_chat(cid, getattr(msg, "author", None) or name, text)
                    _llm_for_chat(
                        cid,
                        getattr(msg, "author", None) or name,
                        text,
                        getattr(msg, "author_id", None),
                        msg,
                    )
            except Exception as e:
                log.warning("poll: %s", short_error(e))
            time.sleep(8)

    threading.Thread(target=chat_poll, name="fp-poll", daemon=True).start()
    while True:
        try:
            if settings().get("auto_raise", AUTO_RAISE):
                notes = fp.maybe_raise()
                if notes and loop:
                    asyncio.run_coroutine_threadsafe(
                        notify(f"{em(I_UP, '⬆️')} Автоподнятие:\n" + html.escape("\n".join(notes))),
                        loop,
                    )
            fp.listen(handle_fp_event)
        except Exception as e:
            log.warning("FunPay runner: %s", short_error(e))
            time.sleep(15)


def _tg_bot(**kw) -> Bot:
    return Bot(
        token=TG_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        **kw,
    )


async def _telegram_session():
    from core.proxy import candidates

    px = proxy_url()
    tried = []
    if px:
        for u in candidates(px):
            tried.append(u)
            session = AiohttpSession(proxy=for_aiohttp(u))
            b = _tg_bot(session=session)
            try:
                me = await b.get_me()
                log.info("Telegram через %s (@%s)", mask_proxy(u), me.username)
                return b
            except Exception as e:
                log.warning("Telegram proxy %s: %s", mask_proxy(u), e)
                await session.close()
    log.warning("Прокси к Telegram не открылся, пробую напрямую")
    b = _tg_bot()
    await b.get_me()
    return b


async def run() -> None:
    global bot, loop
    loop = asyncio.get_running_loop()
    bot = await _telegram_session()
    dp = Dispatcher()
    dp.include_router(router)
    import threading

    threading.Thread(target=fp_loop, name="funpay", daemon=True).start()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
