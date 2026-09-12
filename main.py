from __future__ import annotations

import asyncio
import logging
import sys

from core.config import ADMIN_ID, GOLDEN_KEY, TG_TOKEN


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("FunPayAPI.runner").setLevel(logging.CRITICAL)
    logging.getLogger("FunPayAPI.account").setLevel(logging.WARNING)
    missing = []
    if not TG_TOKEN:
        missing.append("TG_TOKEN")
    if not ADMIN_ID:
        missing.append("ADMIN_ID")
    if not GOLDEN_KEY:
        missing.append("GOLDEN_KEY")
    if missing:
        raise SystemExit(f"Заполни .env: {', '.join(missing)} (см. .env.example)")
    from bot.app import run

    asyncio.run(run())


if __name__ == "__main__":
    main()
