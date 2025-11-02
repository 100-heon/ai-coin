from fastmcp import FastMCP
import os
from dotenv import load_dotenv
load_dotenv()

import uuid
import time
import json
import hashlib
from urllib.parse import urlencode
from typing import Dict, Any, Tuple

import requests
import jwt
import sys

# Add project root directory to Python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from tools.general_tools import get_config_value, write_config_value


mcp = FastMCP("TradeTools")


UPBIT_API_BASE = os.environ.get("UPBIT_API_BASE", "https://api.upbit.com")
QUOTE_CCY = os.environ.get("UPBIT_QUOTE", "KRW").upper()
DRY_RUN = os.environ.get("UPBIT_DRY_RUN", "true").lower() not in ("false", "0", "no")
try:
    FEE_RATE = float(os.environ.get("FEE_RATE", "0.0005"))  # 0.05% default
except Exception:
    FEE_RATE = 0.0005


def _creds() -> Tuple[str, str]:
    access_key = os.environ.get("UPBIT_ACCESS_KEY")
    secret_key = os.environ.get("UPBIT_SECRET_KEY")
    if not access_key or not secret_key:
        raise ValueError("Missing UPBIT_ACCESS_KEY/UPBIT_SECRET_KEY in environment")
    return access_key, secret_key


def _normalize_market(symbol: str) -> str:
    s = symbol.strip().upper()
    if "-" in s:
        return s
    return f"{QUOTE_CCY}-{s}"


def _auth_headers(method: str, path: str, params: Dict[str, Any] | None) -> Dict[str, str]:
    access_key, secret_key = _creds()
    payload: Dict[str, Any] = {
        "access_key": access_key,
        "nonce": str(uuid.uuid4()),
    }

    if params:
        query_string = urlencode(params).encode()
        m = hashlib.sha512()
        m.update(query_string)
        query_hash = m.hexdigest()
        payload["query_hash"] = query_hash
        payload["query_hash_alg"] = "SHA512"

    token = jwt.encode(payload, secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _get(url: str, params: Dict[str, Any] | None = None, auth: bool = False) -> requests.Response:
    headers = {}
    if auth:
        headers.update(_auth_headers("GET", url, params))
    return requests.get(url, headers=headers, params=params, timeout=10)


def _post(url: str, params: Dict[str, Any]) -> requests.Response:
    headers = _auth_headers("POST", url, params)
    return requests.post(url, headers=headers, params=params, timeout=10)


def _accounts() -> Dict[str, float]:
    url = f"{UPBIT_API_BASE}/v1/accounts"
    resp = _get(url, auth=True)
    resp.raise_for_status()
    data = resp.json()
    balances: Dict[str, float] = {}
    for item in data:
        currency = item.get("currency")
        bal = float(item.get("balance", 0))
        if currency == QUOTE_CCY:
            balances["CASH"] = bal
        else:
            balances[currency] = bal
    return balances


def _submit_order(market: str, side: str, volume: str | None, price: str | None, ord_type: str) -> Dict[str, Any]:
    url = f"{UPBIT_API_BASE}/v1/orders"
    params: Dict[str, Any] = {
        "market": market,
        "side": side,
        "ord_type": ord_type,
    }
    if volume is not None:
        params["volume"] = volume
    if price is not None:
        params["price"] = price

    if DRY_RUN:
        return {"dry_run": True, "request": params}

    resp = _post(url, params)
    if resp.status_code >= 400:
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text}
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


def _write_position_snapshot(signature: str, today_date: str) -> Dict[str, Any]:
    try:
        positions = _accounts()
    except Exception as e:
        return {"error": f"Failed to fetch accounts: {e}"}

    position_file_path = os.path.join(project_root, "data", "agent_data", signature, "position", "position.jsonl")
    os.makedirs(os.path.dirname(position_file_path), exist_ok=True)

    max_id = -1
    if os.path.exists(position_file_path):
        try:
            with open(position_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    doc = json.loads(line)
                    if doc.get("date") == today_date:
                        max_id = max(max_id, int(doc.get("id", -1)))
        except Exception:
            pass

    record = {
        "date": today_date,
        "id": max_id + 1,
        "this_action": {"action": "snapshot", "symbol": "", "amount": 0},
        "positions": positions,
    }
    with open(position_file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


@mcp.tool()
def buy(symbol: str, amount: float, price: float | None = None, market_order: bool = True) -> Dict[str, Any]:
    """Place a buy order on Upbit.

    Args:
        symbol: e.g. 'BTC' or 'KRW-BTC'. If no '-' is present, QUOTE_CCY is prefixed.
        amount: coin amount to buy for market order; for limit order, this is the volume in coins.
        price: optional limit price (QUOTE_CCY). For market buy by KRW funds, set market_order=True and pass price as total KRW to spend.
        market_order: True for market order. Upbit uses ord_type 'price' for market buy (price=KRW amount), 'market' for market sell.

    Returns: API response or error info. Also writes a position snapshot and sets IF_TRADE=true on success.
    """
    signature = get_config_value("SIGNATURE")
    if signature is None:
        raise ValueError("SIGNATURE environment variable is not set")
    today_date = get_config_value("TODAY_DATE") or time.strftime("%Y-%m-%d")

    market = _normalize_market(symbol)

    try:
        if market_order:
            if price is None:
                return {"error": "For market buy, 'price' must be the KRW amount to spend"}
            result = _submit_order(market, side="bid", volume=None, price=str(price), ord_type="price")
        else:
            if price is None:
                return {"error": "Limit buy requires 'price'"}
            result = _submit_order(market, side="bid", volume=str(amount), price=str(price), ord_type="limit")
    except Exception as e:
        return {"error": str(e)}

    write_config_value("IF_TRADE", True)
    snapshot = _write_position_snapshot(signature, today_date)
    # Estimated fee/cost note (Upbit applies fees on execution; this is an estimate)
    est_fee = None
    est_total_cost = None
    if market_order and price is not None:
        try:
            est_fee = float(price) * FEE_RATE
            est_total_cost = float(price) + est_fee
        except Exception:
            pass
    return {
        "order_result": result,
        "snapshot": snapshot,
        "dry_run": DRY_RUN,
        "fee_rate": FEE_RATE,
        "estimated_fee": est_fee,
        "estimated_total_cost": est_total_cost,
    }


@mcp.tool()
def sell(symbol: str, amount: float, price: float | None = None, market_order: bool = True) -> Dict[str, Any]:
    """Place a sell order on Upbit.

    Args:
        symbol: e.g. 'BTC' or 'KRW-BTC'. If no '-' is present, QUOTE_CCY is prefixed.
        amount: coin amount to sell for market order; for limit order, this is the volume in coins.
        price: optional limit price (QUOTE_CCY). For market sell, leave price=None.
        market_order: True for market order. Upbit uses ord_type='market' for market sell.

    Returns: API response or error info. Also writes a position snapshot and sets IF_TRADE=true on success.
    """
    signature = get_config_value("SIGNATURE")
    if signature is None:
        raise ValueError("SIGNATURE environment variable is not set")
    today_date = get_config_value("TODAY_DATE") or time.strftime("%Y-%m-%d")

    market = _normalize_market(symbol)

    try:
        if market_order:
            result = _submit_order(market, side="ask", volume=str(amount), price=None, ord_type="market")
        else:
            if price is None:
                return {"error": "Limit sell requires 'price'"}
            result = _submit_order(market, side="ask", volume=str(amount), price=str(price), ord_type="limit")
    except Exception as e:
        return {"error": str(e)}

    write_config_value("IF_TRADE", True)
    snapshot = _write_position_snapshot(signature, today_date)
    # Estimated fee/proceeds (approximation)
    est_fee = None
    est_net_proceeds = None
    if not market_order and price is not None:
        try:
            gross = float(price) * float(amount)
            est_fee = gross * FEE_RATE
            est_net_proceeds = gross - est_fee
        except Exception:
            pass
    return {
        "order_result": result,
        "snapshot": snapshot,
        "dry_run": DRY_RUN,
        "fee_rate": FEE_RATE,
        "estimated_fee": est_fee,
        "estimated_net_proceeds": est_net_proceeds,
    }


@mcp.tool()
def get_balance() -> Dict[str, Any]:
    """Return account balances from Upbit. CASH corresponds to the quote currency balance (e.g., KRW)."""
    try:
        balances = _accounts()
        return balances
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    port = int(os.getenv("TRADE_HTTP_PORT", "8002"))
    mcp.run(transport="streamable-http", port=port)

