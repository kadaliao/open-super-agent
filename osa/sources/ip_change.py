"""公网 IP 变了：这台机器的公网 IP 换了时，Bot 收到一个事件（原来的 → 现在的）。

家里的宽带重新拨号、路由器重启后 IP 常常会换。在家里跑服务、用 DDNS、防火墙按 IP 放行的，建一个这样的事件，IP 一换就知道该去改哪了。
在 Bot 的“事件”里选“公网 IP 变了”。默认 10 分钟看一次，问的是 ipify、icanhazip、ifconfig.me 这几个查 IP 的服务，
一个不通换下一个；发现变了会再问另一家确认一次，免得某一家抽风误报。
IPv6 看的是前缀（前 64 位）：电脑出门用的临时地址每天都会换，前缀变了才是运营商换了地址。
走了代理（HTTP_PROXY 之类的环境变量）的话，查到的是代理出口的 IP。
"""
import ipaddress
import time
from ..i18n import N_
from ..registry import POLL, Source
from . import _util

SERVICES = {
    4: ["https://api.ipify.org", "https://ipv4.icanhazip.com", "https://ifconfig.me/ip"],
    6: ["https://api6.ipify.org", "https://ipv6.icanhazip.com"],
}


def parse(text: str, version: int) -> str | None:
    """服务返回的文字 → IP；不是这个版本的公网地址（返回了网页、内网地址）就是 None。"""
    try:
        ip = ipaddress.ip_address((text or "").strip())
    except ValueError:
        return None
    return str(ip) if ip.version == version and ip.is_global else None


async def lookup(version: int, skip: str = "") -> tuple[str | None, str, list[str]]:
    """按顺序问查 IP 的服务，第一个答对的为准。返回 (IP, 哪家答的, 没答上的原因)。skip：确认时跳过刚才那家。"""
    errors = []
    for url in SERVICES[version]:
        if url == skip:
            continue
        try:
            r = await _util.get(url, timeout=10, max_bytes=10_000)
        except Exception as e:
            errors.append(f"{url}：{type(e).__name__} {e}".strip())
            continue
        if ip := parse(r.text, version):
            return ip, url, errors
        errors.append(f"{url}：返回的不是 IPv{version} 地址")
    return None, "", errors


def same(old: str, new: str, version: int) -> bool:
    if version == 6:   # 只比前缀，临时地址换了不算
        net = lambda ip: ipaddress.ip_network(f"{ip}/64", strict=False)
        return net(old) == net(new)
    return old == new


def event(version: int, old: str, new: str, by: str, confirm: str | None) -> dict:
    what = "IPv6 前缀" if version == 6 else "IPv4"
    when = time.strftime("%Y-%m-%d %H:%M")
    how = by.removeprefix("https://") + " 查到，" + (
        "别的几家没连上、没能确认" if not confirm else "换了一家确认过" if same(confirm, new, version) else f"另一家查到的是 {confirm}")
    return {"id": f"ipv{version}:{old}->{new}", "title": f"公网 {what} 变了：{old} → {new}",
            "text": f"原来：{old}\n现在：{new}\n{when} 发现的（{how}）。\n"
                    "有域名解析到这里（DDNS）、防火墙或别的服务按 IP 放行、端口转发写死了地址的，记得改。"}


async def check(params: dict, state):
    versions = [4, 6] if params.get("ipv6") else [4]
    old = dict(state or {})
    new, events = dict(old), []
    for v in versions:
        ip, by, errors = await lookup(v)
        if not ip:
            if v == 4:   # IPv4 查不到说明网断了或者几家都挂了，报出来；没有 IPv6 很常见，跳过
                raise RuntimeError("几个查 IP 的服务都没答上：" + "；".join(errors[:3]))
            continue
        key = f"v{v}"
        if key in old and not same(old[key], ip, v):
            confirm, _, _ = await lookup(v, skip=by)
            if confirm and same(confirm, old[key], v):
                continue   # 另一家说没变：大概是刚才那家抽风，下次再看
            events.append(event(v, old[key], ip, by, confirm))
        new[key] = ip
    return events, new


IP_CHANGE = Source(
    id="ip_change", mode=POLL, category=N_("系统"), icon="🌐",
    title=N_("公网 IP 变了"),
    description=N_("这台机器的公网 IP 换了时触发（家里宽带重新拨号之类），方便你去改 DNS、防火墙白名单。"),
    fields=[{"key": "ipv6", "label": N_("也看 IPv6"), "type": "bool", "default": False,
             "help": N_("IPv6 只看前缀（前 64 位），临时地址换了不算")}],
    check=check, every=10, min_every=5,
    summary=lambda p: "IPv4 和 IPv6" if p.get("ipv6") else "",
    sample={"title": "公网 IPv4 变了：203.0.113.7 → 198.51.100.24",
            "text": "原来：203.0.113.7\n现在：198.51.100.24\n2026-11-09 08:10 发现的（api.ipify.org 查到，换了一家确认过）。\n"
                    "有域名解析到这里（DDNS）、防火墙或别的服务按 IP 放行、端口转发写死了地址的，记得改。"},
    scenarios=[
        {"title": N_("家里公网 IP 变了提醒我（改 DNS）"), "description": N_("宽带重新拨号换了 IP，提醒你去改域名解析、防火墙白名单"),
         "name": N_("公网 IP 变了"),
         "prompt": N_("这台机器的公网 IP 变了。告诉我原来的和现在的 IP，提醒我检查：域名解析（DDNS 有没有自动更新）、"
                      "云服务器安全组或防火墙里按 IP 放行的规则、别的服务里写死的地址。简短一点。")},
    ])

SOURCES = [IP_CHANGE]
