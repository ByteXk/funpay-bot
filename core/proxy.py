from __future__ import annotations

from urllib.parse import quote, urlparse

TYPES = {"http", "https", "socks5", "socks5h", "socks4"}


def _is_ipv6(host: str) -> bool:
    h = host.strip("[]")
    return ":" in h and "." not in h.split("%")[0]


def parse_host_port(ip: str, port: str = "") -> tuple[str, str]:
    """IPv4, IPv6 ([addr]:port или addr + отдельный port)."""
    host = (ip or "").strip().strip("[]")
    p = str(port).strip() if port else ""
    if ip.startswith("["):
        inside, _, rest = ip[1:].partition("]")
        host = inside
        if rest.startswith(":") and not p:
            p = rest[1:]
    elif host.count(":") == 1 and not p:
        a, b = host.rsplit(":", 1)
        if b.isdigit():
            host, p = a, b
    elif _is_ipv6(host) and not p:
        last = host.rsplit(":", 1)
        if len(last) == 2 and last[1].isdigit() and last[0].count(":") >= 2:
            host, p = last[0], last[1]
    return host.strip("[]"), p


def host_for_url(host: str) -> str:
    return f"[{host}]" if _is_ipv6(host) else host


def from_fields(
    *,
    user: str = "",
    password: str = "",
    ip: str = "",
    type_: str = "socks5",
    port: str | int = "",
) -> str | None:
    ip = (ip or "").strip()
    if not ip:
        return None
    kind = (type_ or "socks5").strip().lower().replace("socks 5", "socks5")
    if kind == "socks":
        kind = "socks5"
    if kind not in TYPES:
        raise ValueError(f"type: http, https или socks5 (не «{kind}»)")

    host = ip
    p = str(port).strip() if port else ""
    if "://" in host:
        parsed = urlparse(host)
        host = parsed.hostname or host
        p = p or (str(parsed.port) if parsed.port else "")
        user = user or (parsed.username or "")
        password = password if password else (parsed.password or "")
    else:
        host, parsed_port = parse_host_port(host, p)
        p = parsed_port or p

    if not p:
        p = "1080" if kind.startswith("socks") else "8080"

    if kind == "socks5":
        kind = "socks5h"

    auth = ""
    if user:
        auth = quote(str(user), safe="")
        if password:
            auth += ":" + quote(str(password), safe="")
        auth += "@"
    return f"{kind}://{auth}{host_for_url(host)}:{p}"


def normalize_proxy(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().strip('"').strip("'")
    if not s:
        return None
    if "://" not in s:
        parts = [x.strip() for x in s.split(":")]
        if len(parts) == 2:
            return from_fields(ip=parts[0], port=parts[1])
        if len(parts) == 4:
            return from_fields(ip=parts[0], port=parts[1], user=parts[2], password=parts[3])
        raise ValueError("Не понял строку прокси")
    parsed = urlparse(s)
    return from_fields(
        user=parsed.username or "",
        password=parsed.password or "",
        ip=parsed.hostname or "",
        type_=parsed.scheme or "http",
        port=parsed.port or "",
    )


def for_aiohttp(url: str | None) -> str | None:
    """python-socks не знает socks5h — для Telegram нужен socks5."""
    if not url:
        return None
    return url.replace("socks5h://", "socks5://").replace("socks4a://", "socks4://")


def with_scheme(url: str, scheme: str) -> str:
    p = urlparse(url)
    rest = url.split("://", 1)[-1]
    if scheme == "socks5":
        scheme = "socks5h"
    return f"{scheme}://{rest}" if "://" in url else url


def candidates(url: str | None) -> list[str]:
    """Сначала указанный тип, потом http/socks5 — у панелей часто путают."""
    if not url:
        return []
    parsed = urlparse(url)
    first = (parsed.scheme or "http").lower()
    if first == "socks5h":
        first = "socks5"
    order = [first] + [x for x in ("http", "socks5", "https") if x != first]
    out, seen = [], set()
    for sch in order:
        u = with_scheme(url, sch)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def requests_proxies(url: str | None) -> dict[str, str] | None:
    if not url:
        return None
    n = url if "://" in url else normalize_proxy(url)
    if not n:
        return None
    return {"http": n, "https": n}


def mask_proxy(url: str | None) -> str:
    if not url:
        return "нет"
    try:
        n = normalize_proxy(url) or url
        p = urlparse(n)
        user = p.username or ""
        if user:
            user = f"{user}:***@"
        return f"{p.scheme}://{user}{p.hostname}:{p.port}"
    except Exception:
        return "***"
