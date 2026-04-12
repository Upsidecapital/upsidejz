#!/usr/bin/env python3
"""
Quick diagnostic: which crypto price feeds can this machine reach?

Run:  python check_binance.py

Tests: Binance WS, Coinbase WS, Kraken WS, CoinGecko REST
"""

import asyncio
import json
import ssl
import sys
import urllib.request


def _ssl_ctx():
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


# ── Binance ──────────────────────────────────────────────
async def test_binance():
    import websockets
    endpoints = [
        "wss://stream.binance.com:9443",
        "wss://stream.binance.com:443",
        "wss://stream.binance.us:9443",
    ]
    for ep in endpoints:
        url = f"{ep}/stream?streams=btcusdt@miniTicker"
        print(f"  Binance  {ep} ...", end=" ", flush=True)
        try:
            async with websockets.connect(
                url, ssl=_ssl_ctx(), open_timeout=8, close_timeout=3
            ) as ws:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                data = json.loads(raw)
                price = data.get("data", data).get("c")
                if price:
                    print(f"OK  (BTC ${float(price):,.2f})")
                    return True
        except asyncio.TimeoutError:
            print("TIMEOUT")
        except Exception as e:
            print(f"FAIL  ({e})")
    return False


# ── Coinbase ─────────────────────────────────────────────
async def test_coinbase():
    import websockets
    url = "wss://ws-feed.exchange.coinbase.com"
    print(f"  Coinbase {url} ...", end=" ", flush=True)
    try:
        async with websockets.connect(
            url, ssl=_ssl_ctx(), open_timeout=8, close_timeout=3
        ) as ws:
            await ws.send(json.dumps({
                "type": "subscribe",
                "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}],
            }))
            # Read messages until we get a ticker
            for _ in range(10):
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                data = json.loads(raw)
                if data.get("type") == "ticker":
                    price = data.get("price")
                    if price:
                        print(f"OK  (BTC ${float(price):,.2f})")
                        return True
            print("OK  (connected, no ticker yet)")
            return True
    except asyncio.TimeoutError:
        print("TIMEOUT")
    except Exception as e:
        print(f"FAIL  ({e})")
    return False


# ── Kraken ───────────────────────────────────────────────
async def test_kraken():
    import websockets
    url = "wss://ws.kraken.com"
    print(f"  Kraken   {url} ...", end=" ", flush=True)
    try:
        async with websockets.connect(
            url, ssl=_ssl_ctx(), open_timeout=8, close_timeout=3
        ) as ws:
            await ws.send(json.dumps({
                "event": "subscribe",
                "pair": ["XBT/USD"],
                "subscription": {"name": "ticker"},
            }))
            for _ in range(15):
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                data = json.loads(raw)
                if isinstance(data, list) and len(data) >= 4:
                    price = float(data[1].get("c", [0])[0])
                    if price > 0:
                        print(f"OK  (BTC ${price:,.2f})")
                        return True
            print("OK  (connected, waiting for data)")
            return True
    except asyncio.TimeoutError:
        print("TIMEOUT")
    except Exception as e:
        print(f"FAIL  ({e})")
    return False


# ── CoinGecko REST ───────────────────────────────────────
async def test_coingecko():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    print(f"  CoinGecko REST ...", end=" ", flush=True)
    try:
        loop = asyncio.get_event_loop()
        req = urllib.request.Request(
            url, headers={"User-Agent": "Polytracker/1.0"}
        )
        raw = await loop.run_in_executor(
            None,
            lambda: urllib.request.urlopen(
                req, context=_ssl_ctx(), timeout=10
            ).read(),
        )
        data = json.loads(raw)
        price = data.get("bitcoin", {}).get("usd")
        if price:
            print(f"OK  (BTC ${float(price):,.2f})")
            return True
        print("OK  (connected, unexpected response)")
        return True
    except Exception as e:
        print(f"FAIL  ({e})")
    return False


async def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("[!] 'websockets' not installed. Run: pip install websockets")
        sys.exit(1)

    print()
    print("=" * 60)
    print("  Crypto Price Feed Connectivity Check")
    print("=" * 60)
    print()

    results = {}
    for name, test_fn in [
        ("Binance", test_binance),
        ("Coinbase", test_coinbase),
        ("Kraken", test_kraken),
        ("CoinGecko", test_coingecko),
    ]:
        ok = await test_fn()
        results[name] = ok
        print()

    working = [n for n, ok in results.items() if ok]
    failed = [n for n, ok in results.items() if not ok]

    print("-" * 60)
    if working:
        print(f"  Working:  {', '.join(working)}")
        print()
        print(f"  The bot will use: {working[0]}")
        print("  (it tries each in order and auto-selects the first")
        print("   that connects — no config changes needed)")
    else:
        print("  !! No exchanges reachable.")
        print()
        print("  This likely means:")
        print("    - Your internet is down, or")
        print("    - A firewall/VPN is blocking all crypto APIs")
        print()
        print("  Try: connect to a different network or use a VPN")

    if failed:
        print()
        print(f"  Blocked:  {', '.join(failed)}")

    print()
    print("=" * 60)
    print()


if __name__ == "__main__":
    asyncio.run(main())
