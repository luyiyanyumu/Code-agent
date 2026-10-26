"""Provider-scoped proxy selection and safe connection-error guidance."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

import httpx

PROXY_MODES = ("system", "direct")


def normalize_proxy_mode(value: object = None) -> str:
    mode = "system" if value in (None, "") else str(value).strip()
    if mode not in PROXY_MODES:
        raise ValueError("网络代理必须选择跟随系统（system）或直接连接（direct）")
    return mode


def connection_error_detail(exc: Exception, base_url: str, proxy_mode: str = "system") -> str:
    if not isinstance(exc, (httpx.ConnectError, httpx.ProxyError)):
        return f"{type(exc).__name__}: {exc}"
    if proxy_mode == "system":
        try:
            target = urlsplit(base_url)
            proxies = getproxies()
            proxy = proxies.get(target.scheme) or proxies.get("all")
            if proxy and not proxy_bypass(target.hostname or ""):
                endpoint = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
                host = endpoint.hostname or ""
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local = host.lower() == "localhost"
                if local:
                    port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
                    return (f"连接失败，当前系统代理为本机 {host}:{port}。请确认代理已启动，"
                            "或在模型档案的「网络代理」中选择「直接连接」并保存。")
        except (ValueError, OSError):
            pass
    mode = "直接连接" if proxy_mode == "direct" else "系统代理连接"
    return f"{mode}失败（{type(exc).__name__}），请检查网络、Base URL 与 TLS 证书。"
