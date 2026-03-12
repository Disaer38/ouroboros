"""
Proxy configuration and health-check for Polymarket bot.

Set POLYMARKET_PROXY_URL in your environment to route all traffic through a proxy.

Supported formats:
  HTTP proxy:  http://user:pass@host:port
  SOCKS5:      socks5://user:pass@host:port
  SOCKS5h:     socks5h://user:pass@host:port  (remote DNS resolution)

Examples:
  POLYMARKET_PROXY_URL=http://my-proxy.example.com:8080
  POLYMARKET_PROXY_URL=socks5://user:secret@1.2.3.4:1080

To test proxy geo-location:
  python -m bots.polymarket.proxy
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GEO_CHECK_URL = "https://ipinfo.io/json"
POLYMARKET_GEO_URL = "https://polymarket.com/api/geoblock"


def get_proxy_url() -> Optional[str]:
    """Return proxy URL from environment, or None if not set."""
    return os.environ.get("POLYMARKET_PROXY_URL") or None


def build_httpx_client(proxy_url: Optional[str] = None, **kwargs) -> httpx.AsyncClient:
    """
    Build an httpx.AsyncClient with optional proxy configuration.

    Args:
        proxy_url: Proxy URL string. If None, reads from POLYMARKET_PROXY_URL env var.
                   Pass empty string "" to force no proxy.
        **kwargs:  Extra args forwarded to httpx.AsyncClient.

    Returns:
        Configured AsyncClient instance.
    """
    if proxy_url is None:
        proxy_url = get_proxy_url()

    if proxy_url:
        logger.info(f"Using proxy: {_mask_proxy(proxy_url)}")
        # httpx uses 'proxies' or 'mounts' API — use mounts for newer versions
        transport = httpx.AsyncHTTPTransport(proxy=proxy_url)
        return httpx.AsyncClient(transport=transport, **kwargs)
    else:
        return httpx.AsyncClient(**kwargs)


def _mask_proxy(url: str) -> str:
    """Mask password in proxy URL for logging."""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url)
        if p.password:
            masked = p._replace(netloc=f"{p.username}:***@{p.hostname}:{p.port}")
            return urlunparse(masked)
    except Exception:
        pass
    return url


async def check_geo(proxy_url: Optional[str] = None) -> dict:
    """
    Check current IP geo-location and Polymarket block status.

    Returns dict with keys: ip, country, city, proxy_used, polymarket_blocked
    """
    result = {
        "ip": None,
        "country": None,
        "city": None,
        "org": None,
        "proxy_used": bool(proxy_url or get_proxy_url()),
        "polymarket_blocked": None,
        "error": None,
    }

    try:
        async with build_httpx_client(proxy_url, timeout=15.0) as client:
            # 1. IP / geo info
            resp = await client.get(GEO_CHECK_URL)
            resp.raise_for_status()
            geo = resp.json()
            result["ip"] = geo.get("ip")
            result["country"] = geo.get("country")
            result["city"] = geo.get("city")
            result["org"] = geo.get("org")

            # 2. Polymarket geoblock check
            try:
                pm_resp = await client.get(POLYMARKET_GEO_URL, timeout=10.0)
                pm_resp.raise_for_status()
                pm_data = pm_resp.json()
                result["polymarket_blocked"] = pm_data.get("blocked", False)
                result["polymarket_country"] = pm_data.get("countryCode")
            except Exception as e:
                result["polymarket_blocked"] = f"error: {e}"

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Geo check failed: {e}")

    return result


if __name__ == "__main__":
    import asyncio

    async def _main():
        proxy = get_proxy_url()
        print(f"\n🌍 Checking geo-location...")
        if proxy:
            print(f"   Using proxy: {_mask_proxy(proxy)}")
        else:
            print("   No proxy configured (set POLYMARKET_PROXY_URL to use one)")
        print()

        info = await check_geo()
        if info["error"]:
            print(f"❌ Error: {info['error']}")
            return

        print(f"📍 IP:      {info['ip']}")
        print(f"🌐 Country: {info['country']} ({info.get('city', '?')})")
        print(f"🏢 ISP:     {info.get('org', '?')}")
        print()

        blocked = info.get("polymarket_blocked")
        if blocked is True:
            print(f"🚫 Polymarket: BLOCKED (country={info.get('polymarket_country')})")
        elif blocked is False:
            print(f"✅ Polymarket: ALLOWED (country={info.get('polymarket_country')})")
        else:
            print(f"⚠️  Polymarket geoblock check: {blocked}")

    asyncio.run(_main())
