#!/usr/bin/env python3
"""
Quick diagnostic: can this machine reach Binance WebSocket?

Run:  python check_binance.py
"""

import asyncio
import json
import ssl
import sys

# Endpoints to try (international, alt port, futures, Binance US)
ENDPOINTS = [
    "wss://stream.binance.com:9443",
    "wss://stream.binance.com:443",
    "wss://fstream.binance.com",
    "wss://stream.binance.us:9443",
]

STREAM = "btcusdt@miniTicker"


async def test_endpoint(base_url: str, timeout: float = 8.0) -> bool:
    try:
        import websockets
    except ImportError:
        print("[!] 'websockets' package not installed.")
        print("    Run:  pip install websockets")
        sys.exit(1)

    url = f"{base_url}/stream?streams={STREAM}"
    print(f"  Trying {url} ...", end=" ", flush=True)

    ssl_ctx = ssl.create_default_context()
    try:
        import certifi
        ssl_ctx.load_verify_locations(certifi.where())
    except Exception:
        pass

    try:
        async with websockets.connect(
            url, ssl=ssl_ctx, open_timeout=timeout, close_timeout=3
        ) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(raw)
            payload = data.get("data", data)
            price = payload.get("c")
            if price:
                print(f"OK  (BTC = ${float(price):,.2f})")
                return True
            else:
                print(f"OK  (connected, but unexpected payload)")
                return True
    except asyncio.TimeoutError:
        print("TIMEOUT")
    except OSError as e:
        print(f"FAILED  ({e})")
    except Exception as e:
        print(f"ERROR  ({type(e).__name__}: {e})")
    return False


async def main():
    print()
    print("=" * 56)
    print("  Binance WebSocket Connectivity Check")
    print("=" * 56)
    print()

    # Basic DNS check first
    import socket
    for host in ["stream.binance.com", "stream.binance.us"]:
        try:
            ip = socket.getaddrinfo(host, 443)[0][4][0]
            print(f"  DNS: {host} -> {ip}")
        except socket.gaierror:
            print(f"  DNS: {host} -> FAILED (cannot resolve)")
    print()

    found = False
    for ep in ENDPOINTS:
        ok = await test_endpoint(ep)
        if ok:
            found = True
            print(f"\n  >>> Working endpoint: {ep}")
            print(
                f"\n  To use this endpoint, set in your .env file:"
                f"\n    BINANCE_WS_URL={ep}\n"
            )
            break

    if not found:
        print()
        print("  !! None of the Binance endpoints are reachable.")
        print()
        print("  Common causes:")
        print("    1. Binance is blocked in your country (e.g. US)")
        print("       -> Try a VPN, or the bot will use Binance.US automatically")
        print("    2. Corporate/school firewall blocking port 9443")
        print("       -> The port-443 fallback should fix this; if not, try VPN")
        print("    3. Python SSL certificates are outdated")
        print("       -> Run: pip install --upgrade certifi")
        print("    4. No internet connection")
        print()

    print("=" * 56)
    print()


if __name__ == "__main__":
    asyncio.run(main())
