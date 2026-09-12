from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Form(StatesGroup):
    reply_chat = State()
    price = State()
    stock_add = State()
    json_wait = State()
    proxy_fields = State()
    llm_url = State()
    llm_key = State()
    llm_model = State()
    llm_prompt = State()
    llm_notes = State()
    llm_example = State()
    cmd_add = State()
