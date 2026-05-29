from src.binance_client import BinanceClient
import json
c = BinanceClient()
orders = c.get_open_orders('BARDUSDT')
for o in orders:
    print(json.dumps({"type": o["type"], "side": o["side"], "price": o["price"], "stopPrice": o.get("stopPrice","N/A"), "qty": o["origQty"]}))
