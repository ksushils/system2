import os
os.environ.setdefault("FMP_API_KEY", "q8RajUBegYvlnJHIe366CBqk4dK2OKd7")
from fmp_api import get

endpoints = [
    "stable/stock-news",
    "stock-news",
    "v3/stock_news",
    "v4/stock-news",
    "stable/upgrades-downgrades",
    "upgrades-downgrades",
    "v4/upgrades-downgrades",
]

for ep in endpoints:
    r = get(ep, {"symbol": "AAPL", "limit": "1"})
    status = f"list[{len(r)}]" if isinstance(r, list) else str(r)[:80]
    print(f"{ep}: {status}")
