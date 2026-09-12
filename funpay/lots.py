from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from funpay.service import FunPayService
from core.storage import set_lot_stock, write_stock


def parse_payload(raw: str | bytes | dict) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        data = raw
    else:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
    if isinstance(data, list):
        lots = data
    else:
        lots = data.get("lots") or data.get("items") or data.get("offers")
    if not isinstance(lots, list) or not lots:
        raise ValueError("В JSON нужен массив lots / items / offers или сам массив лотов.")
    return lots


def _node_id(item: dict) -> int:
    for key in ("node_id", "subcategory_id", "category_id", "lot_category"):
        if key in item:
            return int(item[key])
    raise ValueError("У лота нет node_id (id подкатегории FunPay).")


def create_from_items(fp: FunPayService, items: list[dict], pause: float = 1.2) -> list[str]:
    import time

    log: list[str] = []
    for i, item in enumerate(items, 1):
        try:
            node = _node_id(item)
            title = item.get("title_ru") or item.get("title") or item.get("name")
            if not title:
                raise ValueError("нет title_ru / title")
            price = float(item.get("price") or item.get("sum") or 0)
            amount = int(item.get("amount") or item.get("qty") or item.get("quantity") or 1)
            stock = item.get("stock") or item.get("keys") or item.get("secrets") or []
            if isinstance(stock, str):
                stock = [s.strip() for s in stock.splitlines() if s.strip()]
            extra = item.get("fields") if isinstance(item.get("fields"), dict) else None
            lot = fp.create_lot(
                node,
                title_ru=str(title),
                title_en=item.get("title_en"),
                description_ru=item.get("description_ru") or item.get("description") or "",
                description_en=item.get("description_en"),
                price=price,
                amount=amount,
                active=bool(item.get("active", True)),
                deactivate_after_sale=bool(item.get("deactivate_after_sale", False)),
                extra=extra,
            )
            lid = lot.lot_id
            fname = item.get("stock_file") or f"{lid}.txt"
            set_lot_stock(lid, fname)
            if stock:
                write_stock(lid, [str(x) for x in stock])
            log.append(f"{i}. ок · node {node} · лот {lid} · {title} · {amount} шт. · сток {len(stock)}")
        except Exception as e:
            log.append(f"{i}. ошибка · {e}")
        time.sleep(pause)
    return log
