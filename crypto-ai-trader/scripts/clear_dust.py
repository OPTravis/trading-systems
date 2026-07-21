#!/usr/bin/env python3
"""Clear remaining dust positions via Binance dust transfer API.

Binance enforces a 1-hour cooldown between dust transfers, so this script
is designed to be run once to clear all remaining dust in a single batch.
"""
import sys, json, time, hmac, hashlib, urllib.parse, requests, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from _binance_sdk_client import BinanceClient


def signed_request(client, method, path, params=None):
    api_key = client.api_key
    api_secret = client.api_secret
    timestamp = int(time.time() * 1000)
    base_params = {"timestamp": timestamp}
    if params:
        base_params.update(params)
    query = urllib.parse.urlencode(base_params)
    signing = hmac.new(
        api_secret.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    url = f"https://api.binance.com{path}?{query}&signature={signing}"
    headers = {"X-MBX-APIKEY": api_key}
    return requests.request(method, url, headers=headers, timeout=10)


def main():
    bc = BinanceClient()
    client = bc.client

    # Get all non-zero, non-stablecoin balances
    account = client.account()
    dust_assets = []
    for b in account["balances"]:
        asset = b["asset"]
        free = float(b["free"])
        if asset in ("USDT", "USDC", "BUSD", "BNB"):  # BNB is dust target, skip
            continue
        if free > 0:
            dust_assets.append(asset)

    if not dust_assets:
        print("✅ No dust to clear.")
        return

    print(f"Found {len(dust_assets)} dust assets: {dust_assets}")

    # Try batch transfer (all at once is allowed after cooldown)
    asset_str = ",".join(dust_assets)
    print(f"Attempting dust transfer for: {asset_str}")
    resp = signed_request(client, "POST", "/sapi/v1/asset/dust", {"asset": asset_str})

    if resp.status_code == 200:
        data = resp.json()
        print(f"✅ Dust transfer SUCCESS!")
        print(f"   Total transferred to BNB: {data.get('totalTransfered', 'N/A')}")
        print(f"   Service charge: {data.get('totalServiceCharge', 'N/A')}")
        for result in data.get("transferResult", []):
            print(
                f"   {result['fromAsset']}: {result['amount']} → "
                f"{result['transferedAmount']} BNB"
            )
    else:
        print(f"❌ Dust transfer failed: {resp.status_code} — {resp.text}")

        # Fallback: try individually
        print("\nTrying individually...")
        for asset in dust_assets:
            resp = signed_request(
                client, "POST", "/sapi/v1/asset/dust", {"asset": asset}
            )
            if resp.status_code == 200:
                data = resp.json()
                print(f"  ✅ {asset}: transferred to {data['totalTransfered']} BNB")
            else:
                print(f"  ❌ {asset}: {resp.text[:150]}")
            time.sleep(0.5)

    # Final balance check
    time.sleep(2)
    account = client.account()
    remaining = []
    for b in account["balances"]:
        asset = b["asset"]
        free = float(b["free"])
        if asset in ("USDT", "USDC", "BUSD"):
            continue
        if free > 0:
            remaining.append(f"{asset}: {free}")

    if remaining:
        print(f"\n⚠️  Remaining non-zero: {', '.join(remaining)}")
    else:
        print("\n✅ All dust cleared!")


if __name__ == "__main__":
    main()
