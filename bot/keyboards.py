from __future__ import annotations

from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup as KB

# custom emoji из tg prem.txt (pack tgmacicons / tonundrwrld)
I_CHAT = "5258215846450305872"
I_ORDERS = "5258134813302332906"
I_LOTS = "5258514780469075716"
I_STOCK = "5257965810634202885"
I_JSON = "5258477770735885832"
I_UP = "5260652420052032852"
I_LOCK = "5258476306152038031"
I_BOT = "5258093637450866522"
I_GEAR = "5258096772776991776"
I_USER = "5258362837411045098"
I_HOME = "5257963315258204021"
I_WRITE = "5258331647358540449"
I_OK = "5260726538302660868"
I_NO = "5260342697075416641"
I_MONEY = "5258204546391351475"
I_PLUS = "5258108352008823107"
I_LINK = "5257991477358763590"
I_BOLT = "5258152182150077732"
I_STAR = "5258185631355378853"
I_BACK = "5258236805890710909"
I_KEY = "5258476306152038031"
I_DOC = "5258477770735885832"
I_ON = "5260416304224936047"
I_PEOPLE = "5258513401784573443"
I_INFO = "5258503720928288433"
I_WARN = "5258474669769497337"
I_EYE = "5260341314095947411"
I_CHART = "5258391025281408576"
I_CLOCK = "5258419835922030550"


def em(icon: str, fallback: str = "•") -> str:
    """Премиум-эмодзи в тексте сообщения (HTML parse_mode)."""
    return f'<tg-emoji emoji-id="{icon}">{fallback}</tg-emoji>'


def btn(text: str, data: str, icon: str | None = None, style: str | None = None) -> B:
    kw: dict = {"text": text, "callback_data": data}
    if icon:
        kw["icon_custom_emoji_id"] = str(icon)
    if style:
        kw["style"] = style
    try:
        return B(**kw)
    except TypeError:
        return B(text=text, callback_data=data)


def menu() -> KB:
    return KB(
        inline_keyboard=[
            [btn("чаты", "chats", I_CHAT), btn("заказы", "orders", I_ORDERS)],
            [btn("лоты", "cat_shop", I_LOTS), btn("бот", "llm", I_BOT)],
            [btn("настройки", "cfg", I_GEAR, "primary")],
        ]
    )


def shop_menu() -> KB:
    return KB(
        inline_keyboard=[
            [btn("список", "lots", I_LOTS), btn("сток", "stock", I_STOCK)],
            [btn("json", "json", I_JSON), btn("поднять", "raise", I_UP, "success")],
            [btn("меню", "home", I_HOME, "primary")],
        ]
    )


def llm_menu(on: bool) -> KB:
    return KB(
        inline_keyboard=[
            [btn("шлюз", "llm_gate", I_LINK), btn("обучение", "llm_train", I_DOC)],
            [btn("выкл в чатах" if on else "вкл в чатах", "llm_tgl", I_NO if on else I_ON, "danger" if on else "success")],
            [btn("тест", "llm_test", I_BOLT, "success")],
            [btn("меню", "home", I_HOME, "primary")],
        ]
    )


def llm_gate_kb() -> KB:
    return KB(
        inline_keyboard=[
            [btn("URL", "llm_url", I_LINK), btn("ключ", "llm_key", I_KEY)],
            [btn("модель", "llm_model", I_BOT)],
            [
                btn("auto", "llm_proto:auto", I_GEAR),
                btn("openai", "llm_proto:openai", I_STAR),
                btn("anthropic", "llm_proto:anthropic", I_BOT),
            ],
            [btn("назад", "llm", I_BACK)],
        ]
    )


def llm_train_kb() -> KB:
    return KB(
        inline_keyboard=[
            [btn("промпт", "llm_prompt", I_WRITE), btn("знания", "llm_notes", I_DOC)],
            [btn("пример+", "llm_ex", I_PLUS), btn("сброс примеров", "llm_exclear", I_NO, "danger")],
            [btn("назад", "llm", I_BACK)],
        ]
    )


def cfg_menu() -> KB:
    return KB(
        inline_keyboard=[
            [btn("прокси", "proxy", I_LOCK), btn("команды", "cmds", I_BOLT)],
            [btn("поднятие", "tgl:auto_raise", I_UP), btn("ответы", "tgl:auto_reply", I_CHAT)],
            [btn("меню", "home", I_HOME, "primary")],
        ]
    )


def back_home() -> KB:
    return KB(inline_keyboard=[[btn("меню", "home", I_HOME, "primary")]])


def back_to(data: str, title: str = "назад") -> KB:
    return KB(inline_keyboard=[[btn(title, data, I_BACK)]])
