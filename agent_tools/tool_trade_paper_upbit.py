from fastmcp import FastMCP
import os
import json
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Tuple, Optional
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

mcp = FastMCP("TradeTools")

# Project root for data storage
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UPBIT_API_BASE = os.environ.get("UPBIT_API_BASE", "https://api.upbit.com")
QUOTE_CCY = os.environ.get("UPBIT_QUOTE", "KRW").upper()
try:
    FEE_RATE = float(os.environ.get("FEE_RATE", "0.0005"))
except Exception:
    FEE_RATE = 0.0005
try:
    START_CASH_KRW = float(os.environ.get("START_CASH_KRW", "100000000"))
except Exception:
    START_CASH_KRW = 100000000.0


def _normalize_market(symbol: str) -> str:
    s = symbol.strip().upper()
    if "-" in s:
        return s
    return f"{QUOTE_CCY}-{s}"


def _ticker_price(symbol: str) -> float:
    market = _normalize_market(symbol)
    url = f"{UPBIT_API_BASE}/v1/ticker"
    try:
        resp = requests.get(url, params={"markets": market}, timeout=10)
        if resp.status_code != 200:
            return 0.0
        data = resp.json()
        if isinstance(data, list) and data:
            return float(data[0].get("trade_price") or 0.0)
    except Exception:
        return 0.0
    return 0.0


def _position_path(signature: str) -> str:
    return os.path.join(project_root, "data", "agent_data", signature, "position", "position.jsonl")


def _read_last_ext(signature: str) -> Tuple[Dict[str, float], Dict[str, float], float, int]:
    positions: Dict[str, float] = {}
    avg_costs: Dict[str, float] = {}
    realized_pnl = 0.0
    max_id = -1
    path = _position_path(signature)
    if not os.path.exists(path):
        return positions, avg_costs, realized_pnl, max_id
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    doc = json.loads(line)
                except Exception:
                    continue
                current_id = int(doc.get("id", -1))
                if current_id > max_id:
                    max_id = current_id
                    positions = doc.get("positions", {}) or {}
                    avg_costs = doc.get("avg_costs", {}) or {}
                    realized_pnl = float(doc.get("realized_pnl", 0.0) or 0.0)
    except Exception:
        pass
    return positions, avg_costs, realized_pnl, max_id


def _write_snapshot(
    signature: str,
    today_date: str,
    positions: Dict[str, float],
    this_action: Dict[str, Any],
    avg_costs: Optional[Dict[str, float]] = None,
    realized_pnl: Optional[float] = None,
) -> Dict[str, Any]:
    path = _position_path(signature)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _, _, _, last_id = _read_last_ext(signature)
    record = {
        "date": today_date,
        "timestamp": _current_timestamp_kst(),
        "id": last_id + 1,
        "this_action": this_action,
        "positions": positions,
    }
    if isinstance(avg_costs, dict):
        record["avg_costs"] = avg_costs
    if isinstance(realized_pnl, (int, float)):
        record["realized_pnl"] = realized_pnl
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _current_timestamp_kst() -> str:
    kst = timezone(timedelta(hours=9))
    return datetime.now(tz=kst).strftime("%Y-%m-%dT%H:%M:%S")


def _bootstrap_if_missing(signature: str, today_date: str) -> None:
    path = _position_path(signature)
    if os.path.exists(path):
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    init_positions = {"CASH": START_CASH_KRW}
    _write_snapshot(
        signature,
        today_date,
        positions=init_positions,
        this_action={"action": "init", "symbol": "", "amount": 0, "note": "paper trading init"},
        avg_costs={},
        realized_pnl=0.0,
    )


def _get_config_value(key: str, default=None):
    # local helper to avoid dependency on tools.general_tools
    env_path = os.environ.get("RUNTIME_ENV_PATH")
    if env_path and os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and key in data:
                    return data[key]
        except Exception:
            pass
    return os.getenv(key, default)


def _write_config_value(key: str, value: Any):
    env_path = os.environ.get("RUNTIME_ENV_PATH")
    if not env_path:
        return
    current = {}
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                current = json.load(f) if f.readable() else {}
        except Exception:
            current = {}
    current[key] = value
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=4)
    except Exception:
        pass


@mcp.tool()
def buy(symbol: str, amount: Optional[float] = None, price: float | None = None, market_order: bool = True) -> Dict[str, Any]:
    """Paper buy order using local ledger and Upbit public prices."""
    signature = _get_config_value("SIGNATURE")
    if signature is None:
        return {"error": "SIGNATURE is not set"}
    today_date = _get_config_value("TODAY_DATE") or time.strftime("%Y-%m-%d")

    _bootstrap_if_missing(signature, today_date)
    positions, avg_costs, realized_pnl, _ = _read_last_ext(signature)
    cash = float(positions.get("CASH", 0.0) or 0.0)
    coin = symbol.strip().upper().split("-")[-1]
    market = _normalize_market(symbol)

    fill_price = _ticker_price(symbol)
    if fill_price <= 0:
        return {"error": "Failed to fetch ticker price", "symbol": symbol}

    krw_requested = None
    qty = None
    if market_order:
        if price is None:
            return {"error": "For market buy, price must be KRW amount to spend"}
        try:
            krw_requested = float(price)
        except Exception:
            return {"error": "price must be numeric KRW for market buy", "price": price}
        qty = krw_requested / fill_price
        gross_cost = krw_requested
    else:
        if price is None or amount is None:
            return {"error": "Limit buy needs both price and amount"}
        try:
            qty = float(amount)
            limit_price = float(price)
        except Exception:
            return {"error": "Invalid amount/price for limit buy"}
        gross_cost = qty * limit_price
        fill_price = limit_price

    fee_cost = gross_cost * FEE_RATE
    total_cost = gross_cost + fee_cost
    if total_cost > cash:
        return {
            "error": "Insufficient cash",
            "cash": cash,
            "required": total_cost,
            "fee": fee_cost,
            "fee_rate": FEE_RATE,
        }

    positions["CASH"] = cash - total_cost
    prev_qty = float(positions.get(coin, 0.0) or 0.0)
    positions[coin] = prev_qty + qty

    prev_avg = float(avg_costs.get(coin, 0.0) or 0.0)
    new_qty = positions[coin]
    if new_qty > 0:
        avg_costs[coin] = (prev_avg * prev_qty + fill_price * qty) / new_qty
    else:
        avg_costs[coin] = fill_price

    _write_config_value("IF_TRADE", True)
    snapshot = _write_snapshot(
        signature,
        today_date,
        positions=positions,
        this_action={
            "action": "buy",
            "symbol": coin,
            "amount": qty,
            "market_order": bool(market_order),
            "krw_spent": gross_cost,
            "fee": fee_cost,
            "fee_rate": FEE_RATE,
            "fill_price": fill_price,
        },
        avg_costs=avg_costs,
        realized_pnl=realized_pnl,
    )
    return {
        "order_result": {"status": "filled", "market": market, "fill_price": fill_price, "filled_qty": qty},
        "snapshot": snapshot,
        "dry_run": True,
        "avg_costs": avg_costs,
        "realized_pnl": realized_pnl,
        "fee_rate": FEE_RATE,
    }


@mcp.tool()
def sell(symbol: str, amount: float, price: float | None = None, market_order: bool = True) -> Dict[str, Any]:
    """Paper sell order using local ledger and Upbit public prices."""
    signature = _get_config_value("SIGNATURE")
    if signature is None:
        return {"error": "SIGNATURE is not set"}
    today_date = _get_config_value("TODAY_DATE") or time.strftime("%Y-%m-%d")

    _bootstrap_if_missing(signature, today_date)
    positions, avg_costs, realized_pnl, _ = _read_last_ext(signature)
    coin = symbol.strip().upper().split("-")[-1]
    market = _normalize_market(symbol)

    holding = float(positions.get(coin, 0.0) or 0.0)
    if amount > holding:
        return {"error": "Insufficient position", "have": holding, "want": amount, "symbol": coin}

    fill_price = _ticker_price(symbol)
    if fill_price <= 0:
        return {"error": "Failed to fetch ticker price", "symbol": symbol}

    qty = float(amount)
    if not market_order:
        if price is None:
            return {"error": "Limit sell requires price"}
        try:
            fill_price = float(price)
        except Exception:
            return {"error": "Invalid limit price"}

    gross_proceeds = qty * fill_price
    fee_cost = gross_proceeds * FEE_RATE
    net_proceeds = gross_proceeds - fee_cost

    positions[coin] = holding - qty
    positions["CASH"] = float(positions.get("CASH", 0.0) or 0.0) + net_proceeds

    prev_avg = float(avg_costs.get(coin, 0.0) or 0.0)
    realized_pnl += (fill_price - prev_avg) * qty
    if positions[coin] <= 0:
        avg_costs[coin] = 0.0

    _write_config_value("IF_TRADE", True)
    snapshot = _write_snapshot(
        signature,
        today_date,
        positions=positions,
        this_action={
            "action": "sell",
            "symbol": coin,
            "amount": qty,
            "market_order": bool(market_order),
            "proceeds_krw": gross_proceeds,
            "fee": fee_cost,
            "fee_rate": FEE_RATE,
            "fill_price": fill_price,
        },
        avg_costs=avg_costs,
        realized_pnl=realized_pnl,
    )
    return {
        "order_result": {"status": "filled", "market": market, "fill_price": fill_price, "filled_qty": qty},
        "snapshot": snapshot,
        "dry_run": True,
        "avg_costs": avg_costs,
        "realized_pnl": realized_pnl,
        "fee_rate": FEE_RATE,
    }


@mcp.tool()
def get_balance() -> Dict[str, Any]:
    """Return paper ledger balances (no real API calls)."""
    signature = _get_config_value("SIGNATURE")
    if signature is None:
        return {"error": "SIGNATURE is not set"}
    today_date = _get_config_value("TODAY_DATE") or time.strftime("%Y-%m-%d")
    _bootstrap_if_missing(signature, today_date)
    positions, avg_costs, realized_pnl, _ = _read_last_ext(signature)
    cash = float(positions.get("CASH", 0.0) or 0.0)
    held_coins = []
    for k, v in (positions or {}).items():
        if k == "CASH":
            continue
        try:
            if float(v or 0.0) > 0.0:
                held_coins.append(k)
        except Exception:
            continue
    return {
        "balances": positions,
        "cash": cash,
        "held_coins": held_coins,
        "avg_costs": avg_costs,
        "realized_pnl": realized_pnl,
    }


if __name__ == "__main__":
    port = int(os.getenv("TRADE_HTTP_PORT", "8002"))
    mcp.run(transport="streamable-http", port=port)
