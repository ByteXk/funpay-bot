from __future__ import annotations


def short_error(exc: BaseException) -> str:
    sc = getattr(exc, "status_code", None)
    resp = getattr(exc, "response", None)
    snippet = ""
    if resp is not None:
        try:
            snippet = (resp.text or "")[:180].replace("\n", " ").strip()
        except Exception:
            snippet = ""
    if sc and sc != 200:
        extra = f" · {snippet}" if snippet else ""
        return f"FunPay HTTP {sc}{extra}"
    name = exc.__class__.__name__
    if name == "UnauthorizedError":
        return "FunPay не пустил — проверь golden_key"
    s = str(exc) or name
    low = s.lower()
    if "10061" in s or "отклонил" in s or "refused" in low:
        return "прокси закрыт"
    if "timed out" in low or "timeout" in low:
        return "таймаут FunPay"
    if "json" in low:
        return "FunPay отдал не JSON"
    cut = s.split("Метод:")[0].split("Статус-код")[0].split("Заголовки")[0]
    cut = cut.replace("Ошибка запроса к", "").strip(" .\n")
    return (cut[:140] if cut else name)
