from fastmcp import FastMCP
import os
from dotenv import load_dotenv
load_dotenv()

import uuid
import time
import json
import hashlib
from urllib.parse import urlencode
from typing import Dict, Any, Tuple, Optional

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

# Optional guard: cap KRW per market buy (disabled when 0)
try:
    MAX_MARKET_BUY_KRW = float(os.environ.get("UPBIT_MAX_BUY_KRW", "0") or "0")
except Exception:
    MAX_MARKET_BUY_KRW = 0.0


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


def _read_last_ext(signature: str) -> Tuple[Dict[str, float], Dict[str, float], float, int]:
    """Return (positions, avg_costs, realized_pnl, max_id) from last position record, or defaults."""
    position_file_path = os.path.join(project_root, "data", "agent_data", signature, "position", "position.jsonl")
    positions: Dict[str, float] = {}
    avg_costs: Dict[str, float] = {}
    realized_pnl: float = 0.0
    max_id = -1
    if not os.path.exists(position_file_path):
        return positions, avg_costs, realized_pnl, max_id
    try:
        with open(position_file_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                doc = json.loads(line)
                current_id = int(doc.get("id", -1))
                if current_id > max_id:
                    max_id = current_id
                    positions = doc.get("positions", {}) or {}
                    avg_costs = doc.get("avg_costs", {}) or {}
                    realized_pnl = float(doc.get("realized_pnl", 0.0) or 0.0)
    except Exception:
        pass
    return positions, avg_costs, realized_pnl, max_id


def _write_position_snapshot(
    signature: str,
    today_date: str,
    positions: Dict[str, float],
    this_action: Dict[str, Any],
    avg_costs: Optional[Dict[str, float]] = None,
    realized_pnl: Optional[float] = None,
) -> Dict[str, Any]:
    position_file_path = os.path.join(project_root, "data", "agent_data", signature, "position", "position.jsonl")
    os.makedirs(os.path.dirname(position_file_path), exist_ok=True)

    _, _, _, last_id = _read_last_ext(signature)
    record = {
        "date": today_date,
        "id": last_id + 1,
        "this_action": this_action,
        "positions": positions,
    }
    if isinstance(avg_costs, dict):
        record["avg_costs"] = avg_costs
    if isinstance(realized_pnl, (int, float)):
        record["realized_pnl"] = realized_pnl

    with open(position_file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


@mcp.tool()
def buy(symbol: str, amount: Optional[float] = None, price: float | None = None, market_order: bool = True) -> Dict[str, Any]:
    """Place a buy order on Upbit.

    Args:
        symbol: e.g. 'BTC' or 'KRW-BTC'. If no '-' is present, QUOTE_CCY is prefixed.
        amount: For limit orders, the volume in coins. Ignored for market buy when ord_type='price'.
        price: optional limit price (QUOTE_CCY). For market buy by KRW funds, set market_order=True and pass price as total KRW to spend.
        market_order: True for market order. Upbit uses ord_type 'price' for market buy (price=KRW amount), 'market' for market sell.

    Returns: API response or error info. Also writes a position snapshot and sets IF_TRADE=true on success.
    """
    signature = get_config_value("SIGNATURE")
    if signature is None:
        raise ValueError("SIGNATURE environment variable is not set")
    today_date = get_config_value("TODAY_DATE") or time.strftime("%Y-%m-%d")

    market = _normalize_market(symbol)

    # Pre-trade balances (to infer deltas and effective price)
    try:
        pre_bal = _accounts()
    except Exception:
        pre_bal = {}

    try:
        if market_order:
            if price is None:
                return {"error": "For market buy, 'price' must be the KRW amount to spend"}
            # Safety cap (if configured)
            try:
                req_krw = float(price)
            except Exception:
                return {"error": "price must be numeric KRW for market buy", "price": price}
            if MAX_MARKET_BUY_KRW and req_krw > MAX_MARKET_BUY_KRW:
                return {
                    "error": "requested KRW exceeds UPBIT_MAX_BUY_KRW",
                    "requested_krw": req_krw,
                    "limit": MAX_MARKET_BUY_KRW,
                }
            result = _submit_order(market, side="bid", volume=None, price=str(req_krw), ord_type="price")
        else:
            if price is None:
                return {"error": "Limit buy requires 'price'"}
            result = _submit_order(market, side="bid", volume=str(amount), price=str(price), ord_type="limit")
    except Exception as e:
        return {"error": str(e)}

    # Post-trade balances
    try:
        post_bal = _accounts()
    except Exception:
        post_bal = {}

    write_config_value("IF_TRADE", True)

    # Infer deltas and update avg costs (only if balances changed)
    coin = symbol.strip().upper().split("-")[-1]
    pre_qty = float(pre_bal.get(coin, 0.0) or 0.0)
    post_qty = float(post_bal.get(coin, 0.0) or 0.0)
    delta_qty = post_qty - pre_qty
    pre_cash = float(pre_bal.get("CASH", 0.0) or 0.0)
    post_cash = float(post_bal.get("CASH", 0.0) or 0.0)
    delta_cash = post_cash - pre_cash  # expect negative on buy

    prev_positions, prev_avg_costs, prev_realized, _ = _read_last_ext(signature)
    avg_costs = dict(prev_avg_costs)
    realized_pnl = float(prev_realized)

    requested_krw = None
    krw_spent = None
    coin_delta = None
    if market_order:
        # For market buy, 'amount' is not used by the exchange; KRW is taken from 'price'.
        try:
            requested_krw = float(price) if price is not None else None
        except Exception:
            requested_krw = None

    if delta_qty > 0 and delta_cash < 0:
        krw_spent = -delta_cash  # KRW spent including fee
        coin_delta = delta_qty
        effective_price = krw_spent / delta_qty if delta_qty else 0.0
        prev_avg = float(prev_avg_costs.get(coin, 0.0) or 0.0)
        new_qty = post_qty
        if new_qty > 0:
            avg_costs[coin] = (prev_avg * pre_qty + effective_price * delta_qty) / new_qty
        else:
            avg_costs[coin] = effective_price

    this_action = {
        "action": "buy",
        "symbol": coin,
        # For market orders, 'amount' is not meaningful; keep for compatibility, else None
        "amount": (float(amount) if (amount is not None and not market_order) else None),
        "market_order": bool(market_order),
        "requested_krw": requested_krw,
        "krw_spent": krw_spent,
        "coin_delta": coin_delta,
        "fee_rate": FEE_RATE,
    }

    snapshot = _write_position_snapshot(
        signature,
        today_date,
        positions=post_bal or pre_bal,
        this_action=this_action,
        avg_costs=avg_costs if avg_costs else None,
        realized_pnl=realized_pnl,
    )

    return {
        "order_result": result,
        "snapshot": snapshot,
        "dry_run": DRY_RUN,
        "fee_rate": FEE_RATE,
        "avg_costs": avg_costs if avg_costs else None,
        "realized_pnl": realized_pnl,
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

    # Pre-trade balances
    try:
        pre_bal = _accounts()
    except Exception:
        pre_bal = {}

    try:
        if market_order:
            result = _submit_order(market, side="ask", volume=str(amount), price=None, ord_type="market")
        else:
            if price is None:
                return {"error": "Limit sell requires 'price'"}
            result = _submit_order(market, side="ask", volume=str(amount), price=str(price), ord_type="limit")
    except Exception as e:
        return {"error": str(e)}

    # Post-trade balances
    try:
        post_bal = _accounts()
    except Exception:
        post_bal = {}

    write_config_value("IF_TRADE", True)

    coin = symbol.strip().upper().split("-")[-1]
    pre_qty = float(pre_bal.get(coin, 0.0) or 0.0)
    post_qty = float(post_bal.get(coin, 0.0) or 0.0)
    delta_qty = pre_qty - post_qty  # shares sold
    pre_cash = float(pre_bal.get("CASH", 0.0) or 0.0)
    post_cash = float(post_bal.get("CASH", 0.0) or 0.0)
    delta_cash = post_cash - pre_cash  # expect positive on sell

    prev_positions, prev_avg_costs, prev_realized, _ = _read_last_ext(signature)
    avg_costs = dict(prev_avg_costs)
    realized_pnl = float(prev_realized)

    proceeds_krw = None
    coin_delta = None
    if delta_qty > 0 and delta_cash > 0:
        proceeds_krw = delta_cash
        effective_price = proceeds_krw / delta_qty if delta_qty else 0.0
        prev_avg = float(prev_avg_costs.get(coin, 0.0) or 0.0)
        # Realized PnL uses avg cost; proceeds already net of fees
        realized_pnl += proceeds_krw - prev_avg * delta_qty
        coin_delta = delta_qty
        # Update avg cost for remaining qty
        if post_qty <= 0:
            avg_costs[coin] = 0.0
        else:
            avg_costs[coin] = prev_avg  # unchanged for remaining

    this_action = {
        "action": "sell",
        "symbol": coin,
        "amount": float(amount) if amount is not None else None,
        "market_order": bool(market_order),
        "proceeds_krw": proceeds_krw,
        "coin_delta": coin_delta,
        "fee_rate": FEE_RATE,
    }

    snapshot = _write_position_snapshot(
        signature,
        today_date,
        positions=post_bal or pre_bal,
        this_action=this_action,
        avg_costs=avg_costs if avg_costs else None,
        realized_pnl=realized_pnl,
    )

    return {
        "order_result": result,
        "snapshot": snapshot,
        "dry_run": DRY_RUN,
        "fee_rate": FEE_RATE,
        "avg_costs": avg_costs if avg_costs else None,
        "realized_pnl": realized_pnl,
    }


@mcp.tool()
def get_balance() -> Dict[str, Any]:
    """Return account balances from Upbit.

    Returns
    - balances: dict of coin amounts with CASH for KRW
    - avg_costs: last recorded average costs per coin (if available)
    - realized_pnl: cumulative realized PnL from local records (if available)
    """
    try:
        balances = _accounts()
    except Exception as e:
        return {"error": str(e)}

    try:
        signature = get_config_value("SIGNATURE")
        if signature:
            _, avg_costs, realized_pnl, _ = _read_last_ext(signature)
        else:
            avg_costs, realized_pnl = {}, 0.0
    except Exception:
        avg_costs, realized_pnl = {}, 0.0

    # Convenience fields for LLMs
    try:
        cash = float(balances.get("CASH", 0.0) or 0.0)
    except Exception:
        cash = 0.0
    held_coins = []
    try:
        for k, v in (balances or {}).items():
            if k == "CASH":
                continue
            try:
                if float(v or 0.0) > 0.0:
                    held_coins.append(k)
            except Exception:
                continue
    except Exception:
        held_coins = []

    return {
        "balances": balances,
        "cash": cash,
        "held_coins": held_coins,
        "avg_costs": avg_costs,
        "realized_pnl": realized_pnl,
    }


if __name__ == "__main__":
    port = int(os.getenv("TRADE_HTTP_PORT", "8002"))
    mcp.run(transport="streamable-http", port=port)

