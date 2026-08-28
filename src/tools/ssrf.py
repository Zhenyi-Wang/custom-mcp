"""轻量 SSRF 防护(参考 ihor-sokoliuk/mcp-searxng 的双层思路)。

第一层:URL 字面检查(scheme + literal IP)。
第二层:DNS 解析检查,全部解析结果任一命中私网即拒绝(防 rebinding)。
已知残留风险:resolve-then-connect 的 TOCTOU 窗口,个人场景接受。
"""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse

_BLOCKED_V4 = [
    ipaddress.ip_network(c)
    for c in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
        "224.0.0.0/4", "240.0.0.0/4",
    )
]
_BLOCKED_V6 = [
    ipaddress.ip_network(c)
    for c in (
        "::/128",        # 未指定地址,Linux 上等同 0.0.0.0/localhost
        "::1/128",
        "64:ff9b::/96",  # NAT64 合成地址
        "2002::/16",     # 6to4,内嵌 IPv4
        "2001::/32",     # Teredo,内嵌 IPv4
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
]


class SSRFError(ValueError):
    """URL 指向私有/保留地址或无法安全验证。"""


def is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # 非法地址按拒绝处理
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped  # ::ffff:10.0.0.1 归一化为 IPv4 判断
    nets = _BLOCKED_V4 if isinstance(addr, ipaddress.IPv4Address) else _BLOCKED_V6
    return any(addr in n for n in nets)


def assert_url_allowed(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"不支持的协议 {parsed.scheme!r},仅允许 http/https")
    host = parsed.hostname
    if not host:
        raise SSRFError("URL 缺少主机名")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host  # 域名,交给 resolve_and_check
    if is_private_ip(host):
        raise SSRFError(f"拒绝访问私有/保留地址: {host}")
    return host


def resolve_and_check(
    hostname: str,
    resolver: Callable = socket.getaddrinfo,
) -> list[str]:
    try:
        infos = resolver(hostname, None)
    except socket.gaierror as e:
        raise SSRFError(f"域名解析失败: {hostname}") from e
    except OSError as e:  # 网络不可用等,避免裸异常冒泡到 MCP 客户端
        raise SSRFError(f"域名解析网络错误: {hostname}") from e
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise SSRFError(f"域名无解析结果: {hostname}")
    for addr in addresses:
        if is_private_ip(addr):
            raise SSRFError(f"{hostname} 解析到私有/保留地址 {addr},已拦截")
    return addresses
