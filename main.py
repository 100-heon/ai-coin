import os
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
from dotenv import load_dotenv
try:
    # Ensure we load the .env next to this script regardless of CWD
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    load_dotenv()

# Import tools and prompts
from tools.general_tools import get_config_value, write_config_value
from prompts.agent_prompt import all_nasdaq_100_symbols
try:
    from prompts.crypto_symbols import all_upbit_krw_symbols
except Exception:
    all_upbit_krw_symbols = None
try:
    from tools.date_utils import latest_trading_date_kst, sleep_until_next_bar_kst, get_kst_today_str
except Exception:
    latest_trading_date_kst = None
    sleep_until_next_bar_kst = None
    get_kst_today_str = None


def _resolve_bar_minutes_env(default: int = 60) -> int:
    """Resolve bar size from env (UPBIT_BAR or UPBIT_BAR_MINUTES)."""
    raw = os.getenv("UPBIT_BAR")
    if raw:
        v = raw.strip().lower()
        if v.endswith("m") and v[:-1].isdigit():
            return max(1, int(v[:-1]))
        if v.endswith("h") and v[:-1].isdigit():
            return max(1, int(v[:-1]) * 60)
        if v.isdigit():
            return max(1, int(v))
    v2 = os.getenv("UPBIT_BAR_MINUTES")
    if v2 and v2.isdigit():
        return max(1, int(v2))
    return default
try:
    from tools.upbit_universe import get_all_krw_symbols, get_top_krw_symbols_by_24h_value
except Exception:
    get_all_krw_symbols = None
    get_top_krw_symbols_by_24h_value = None


# Agent class mapping table - for dynamic import and instantiation
AGENT_REGISTRY = {
    "BaseAgent": {
        "module": "agent.base_agent.base_agent",
        "class": "BaseAgent"
    },
}


def get_agent_class(agent_type):
    """
    Dynamically import and return the corresponding class based on agent type name
    
    Args:
        agent_type: Agent type name (e.g., "BaseAgent")
        
    Returns:
        Agent class
        
    Raises:
        ValueError: If agent type is not supported
        ImportError: If unable to import agent module
    """
    if agent_type not in AGENT_REGISTRY:
        supported_types = ", ".join(AGENT_REGISTRY.keys())
        raise ValueError(
            f"❌ Unsupported agent type: {agent_type}\n"
            f"   Supported types: {supported_types}"
        )
    
    agent_info = AGENT_REGISTRY[agent_type]
    module_path = agent_info["module"]
    class_name = agent_info["class"]
    
    try:
        # Dynamic import module
        import importlib
        module = importlib.import_module(module_path)
        agent_class = getattr(module, class_name)
        print(f"✅ Successfully loaded Agent class: {agent_type} (from {module_path})")
        return agent_class
    except ImportError as e:
        raise ImportError(f"❌ Unable to import agent module {module_path}: {e}")
    except AttributeError as e:
        raise AttributeError(f"❌ Class {class_name} not found in module {module_path}: {e}")


def load_config(config_path=None):
    """
    Load configuration file from configs directory
    
    Args:
        config_path: Configuration file path, if None use default config
        
    Returns:
        dict: Configuration dictionary
    """
    if config_path is None:
        # Default configuration file path
        config_path = Path(__file__).parent / "configs" / "default_config.json"
    else:
        config_path = Path(config_path)
    
    if not config_path.exists():
        print(f"❌ Configuration file does not exist: {config_path}")
        exit(1)
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        print(f"✅ Successfully loaded configuration file: {config_path}")
        return config
    except json.JSONDecodeError as e:
        print(f"❌ Configuration file JSON format error: {e}")
        exit(1)
    except Exception as e:
        print(f"❌ Failed to load configuration file: {e}")
        exit(1)


async def main(config_path=None):
    """Run trading experiment using BaseAgent class
    
    Args:
        config_path: Configuration file path, if None use default config
    """
    # Load configuration file
    config = load_config(config_path)
    
    # Get Agent type
    agent_type = config.get("agent_type", "BaseAgent")
    try:
        AgentClass = get_agent_class(agent_type)
    except (ValueError, ImportError, AttributeError) as e:
        print(str(e))
        exit(1)
    
    # Get date range from configuration file
    INIT_DATE = config["date_range"]["init_date"]
    END_DATE = config["date_range"]["end_date"]

    # Optional: use today's date (KST) for live mode
    use_today_flag = str(os.getenv("USE_TODAY", str(config.get("use_today", "false")))).lower() in ("1", "true", "yes")
    if use_today_flag:
        include_weekends = str(os.getenv("INCLUDE_WEEKENDS", "false")).lower() in ("1", "true", "yes")
        # If weekends are allowed (e.g., crypto), use calendar today in KST
        if include_weekends and get_kst_today_str is not None:
            today_kst = get_kst_today_str()
            INIT_DATE = today_kst
            END_DATE = today_kst
            print(f"📅 Using KST calendar date (weekends allowed): {today_kst}")
        elif latest_trading_date_kst is not None:
            today_trading = latest_trading_date_kst(include_today=True)
            INIT_DATE = today_trading
            END_DATE = today_trading
            print(f"📅 Using KST latest trading date: {today_trading}")
    
    # Environment variables can override dates in configuration file
    if os.getenv("INIT_DATE"):
        INIT_DATE = os.getenv("INIT_DATE")
        print(f"⚠️  Using environment variable to override INIT_DATE: {INIT_DATE}")
    if os.getenv("END_DATE"):
        END_DATE = os.getenv("END_DATE")
        print(f"⚠️  Using environment variable to override END_DATE: {END_DATE}")
    
    # Validate date range
    INIT_DATE_obj = datetime.strptime(INIT_DATE, "%Y-%m-%d").date()
    END_DATE_obj = datetime.strptime(END_DATE, "%Y-%m-%d").date()
    if INIT_DATE_obj > END_DATE_obj:
        print("❌ INIT_DATE is greater than END_DATE")
        exit(1)
 
    # Get model list from configuration file (only select enabled models)
    enabled_models = [
        model for model in config["models"] 
        if model.get("enabled", True)
    ]
    
    # Get agent configuration
    agent_config = config.get("agent_config", {})
    log_config = config.get("log_config", {})
    max_steps = agent_config.get("max_steps", 10)
    max_retries = agent_config.get("max_retries", 3)
    base_delay = agent_config.get("base_delay", 0.5)
    initial_cash = agent_config.get("initial_cash", 10000.0)
    
    # Display enabled model information
    model_names = [m.get("name", m.get("signature")) for m in enabled_models]
    
    print("🚀 Starting trading experiment")
    print(f"🤖 Agent type: {agent_type}")
    print(f"📅 Date range: {INIT_DATE} to {END_DATE}")
    print(f"🤖 Model list: {model_names}")
    print(f"⚙️  Agent config: max_steps={max_steps}, max_retries={max_retries}, base_delay={base_delay}, initial_cash={initial_cash}")
                    
    # Choose symbol universe
    symbols_override = config.get("symbols")
    universe = os.getenv("UPBIT_UNIVERSE", config.get("universe", "nasdaq100"))
    # Optional cap on number of symbols (env overrides config)
    try:
        max_symbols = int(os.getenv("MAX_SYMBOLS", str(config.get("max_symbols", 0)) or "0"))
    except Exception:
        max_symbols = 0
    # Whether to rank by 24h traded value
    top_by_24h_env = os.getenv("UPBIT_TOP_BY_24H", str(config.get("top_by_24h_value", "false")))
    top_by_24h = str(top_by_24h_env).lower() in ("1", "true", "yes")

    if symbols_override and isinstance(symbols_override, list) and len(symbols_override) > 0:
        symbol_universe = symbols_override
    else:
        u = universe.lower()
        if u in ("upbit_all_krw", "upbit_all"):
            # Prefer top by 24h traded value when requested
            if top_by_24h and get_top_krw_symbols_by_24h_value is not None:
                fetched = get_top_krw_symbols_by_24h_value(max_symbols if max_symbols > 0 else 20)
            elif get_all_krw_symbols is not None:
                fetched = get_all_krw_symbols(max_symbols=max_symbols if max_symbols > 0 else None)
            else:
                fetched = []
            if fetched:
                symbol_universe = fetched
            elif all_upbit_krw_symbols:
                symbol_universe = all_upbit_krw_symbols
            else:
                symbol_universe = all_nasdaq_100_symbols
        elif u == "upbit_krw" and all_upbit_krw_symbols:
            symbol_universe = all_upbit_krw_symbols
        else:
            symbol_universe = all_nasdaq_100_symbols

    # Debug: print current watchlist each run (does not affect LLM tokens)
    try:
        watch_count = len(symbol_universe) if isinstance(symbol_universe, list) else 0
        if isinstance(symbol_universe, list):
            if watch_count <= 30:
                names = ", ".join(symbol_universe)
                print(f"👀 현재 top {watch_count} 코인 주시 중입니다: {names}")
            else:
                preview = ", ".join(symbol_universe[:10])
                print(f"👀 현재 top {watch_count} 코인 주시 중입니다. 예: {preview} …")
        else:
            print("👀 현재 코인 워치리스트를 불러오지 못했습니다.")
    except Exception:
        pass

    # If Upbit universe, try to compute and print current equity and PnL (no LLM tokens, uses Upbit public API)
    try:
        is_upbit = universe.lower() in ("upbit_krw", "upbit_all_krw", "upbit_all")
    except Exception:
        is_upbit = False

    if is_upbit:
        try:
            import requests
            from pathlib import Path

            # Read latest position record for this signature (if exists)
            sig = enabled_models[0].get("signature", "") if enabled_models else ""
            pos_file = Path(__file__).resolve().parent / "data" / "agent_data" / sig / "position" / "position.jsonl"
            latest = None
            if pos_file.exists():
                with pos_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            doc = json.loads(line)
                            latest = doc
                        except Exception:
                            continue

            if latest and isinstance(latest, dict):
                positions = latest.get("positions", {}) or {}
                avg_costs = latest.get("avg_costs", {}) or {}
                realized_pnl = float(latest.get("realized_pnl", 0.0) or 0.0)

                # Build markets list for coins we hold
                coins = [k for k in positions.keys() if k not in ("CASH",) and isinstance(positions.get(k), (int, float)) and positions.get(k, 0) > 0]
                markets = [f"KRW-{c}" for c in coins]
                prices: dict[str, float] = {}
                if markets:
                    base = os.getenv("UPBIT_API_BASE", "https://api.upbit.com")
                    url = f"{base}/v1/ticker"
                    try:
                        resp = requests.get(url, params={"markets": ",".join(markets)}, timeout=5)
                        if resp.status_code == 200:
                            for item in resp.json() if isinstance(resp.json(), list) else []:
                                mkt = item.get("market", "")
                                if mkt.startswith("KRW-"):
                                    coin = mkt.split("-", 1)[1]
                                    prices[coin] = float(item.get("trade_price") or 0.0)
                    except Exception:
                        pass

                cash = float(positions.get("CASH", 0.0) or 0.0)
                equity = cash
                unreal = 0.0
                for c in coins:
                    qty = float(positions.get(c, 0.0) or 0.0)
                    px = float(prices.get(c, 0.0) or 0.0)
                    equity += qty * px
                    avgc = float(avg_costs.get(c, 0.0) or 0.0)
                    if qty > 0 and avgc > 0 and px > 0:
                        unreal += (px - avgc) * qty

                # Baseline for profit rate: initial cash from config
                init_cash = float(agent_config.get("initial_cash", 10000.0) or 10000.0)
                pnl_total = equity - init_cash
                rate = (equity / init_cash - 1.0) * 100.0 if init_cash > 0 else 0.0
                print(f"📊 현재 평가액: {equity:,.0f} KRW | 손익: {pnl_total:,.0f} KRW (수익률 {rate:.2f}%) | 실현손익 {realized_pnl:,.0f} KRW")

                # Per-coin PnL similar to Upbit app (only non-zero holdings)
                for c in coins:
                    qty = float(positions.get(c, 0.0) or 0.0)
                    if qty <= 0:
                        continue
                    px = float(prices.get(c, 0.0) or 0.0)
                    avgc = float(avg_costs.get(c, 0.0) or 0.0)
                    eval_val = qty * px
                    cost_val = qty * avgc
                    upl = eval_val - cost_val
                    rate_c = ((px / avgc) - 1.0) * 100.0 if avgc > 0 and px > 0 else 0.0
                    print(f"  • {c}: 수량 {qty:.6f}, 평균가 {avgc:,.0f}, 현재가 {px:,.0f}, 평가손익 {upl:,.0f} KRW ({rate_c:+.2f}%)")
            else:
                print("📊 현재 포지션 기록이 없어 평가액/수익률을 계산할 수 없습니다.")
        except Exception:
            # Soft-fail; keep running
            pass

    for model_config in enabled_models:
        # Read basemodel and signature directly from configuration file
        model_name = model_config.get("name", "unknown")
        basemodel = model_config.get("basemodel")
        signature = model_config.get("signature")
        openai_base_url = model_config.get("openai_base_url",None)
        openai_api_key = model_config.get("openai_api_key",None)

        # Validate required fields
        if not basemodel:
            print(f"❌ Model {model_name} missing basemodel field")
            continue
        if not signature:
            print(f"❌ Model {model_name} missing signature field")
            continue
        
        print("=" * 60)
        print(f"🤖 Processing model: {model_name}")
        print(f"📝 Signature: {signature}")
        print(f"🔧 BaseModel: {basemodel}")
        
        # Initialize runtime configuration
        write_config_value("SIGNATURE", signature)
        write_config_value("TODAY_DATE", END_DATE)
        write_config_value("IF_TRADE", False)


        # Get log path configuration
        log_path = log_config.get("log_path", "./data/agent_data")

        try:
            # Dynamically create Agent instance
            agent = AgentClass(
                signature=signature,
                basemodel=basemodel,
                stock_symbols=symbol_universe,
                log_path=log_path,
                openai_base_url=openai_base_url,
                openai_api_key=openai_api_key,
                max_steps=max_steps,
                max_retries=max_retries,
                base_delay=base_delay,
                initial_cash=initial_cash,
                init_date=INIT_DATE,
                prompt_mode=("upbit" if universe.lower() in ("upbit_krw", "upbit_all_krw", "upbit_all") else "stocks")
            )
            
            print(f"✅ {agent_type} instance created successfully: {agent}")
            
            # Initialize MCP connection and AI model
            await agent.initialize()
            print("✅ Initialization successful")
            # Run all trading days in date range
            await agent.run_date_range(INIT_DATE, END_DATE)
            
            # Display final position summary
            summary = agent.get_position_summary()
            print(f"📊 Final position summary:")
            print(f"   - Latest date: {summary.get('latest_date')}")
            print(f"   - Total records: {summary.get('total_records')}")
            print(f"   - Cash balance: ${summary.get('positions', {}).get('CASH', 0):.2f}")
            
        except Exception as e:
            print(f"❌ Error processing model {model_name} ({signature}): {str(e)}")
            print(f"📋 Error details: {e}")
            # Can choose to continue processing next model, or exit
            # continue  # Continue processing next model
            exit()  # Or exit program
        
        print("=" * 60)
        print(f"✅ Model {model_name} ({signature}) processing completed")
        print("=" * 60)
    
    print("🎉 All models processing completed!")
    
if __name__ == "__main__":
    import sys
    
    # Support specifying configuration file through command line arguments
    # Usage: python livebaseagent_config.py [config_path]
    # Example: python livebaseagent_config.py configs/my_config.json
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    
    if config_path:
        print(f"📄 Using specified configuration file: {config_path}")
    else:
        print(f"📄 Using default configuration file: configs/default_config.json")
    
    # Optional internal scheduler: ENABLE_SCHEDULER=true to loop and align to bar size
    enable_scheduler = str(os.getenv("ENABLE_SCHEDULER", "false")).lower() in ("1", "true", "yes")
    if enable_scheduler and sleep_until_next_bar_kst is not None:
        # Force today-only safe execution in loop unless explicitly overridden
        os.environ.setdefault("USE_TODAY", "true")
        os.environ.setdefault("ONLY_TODAY", "true")
        os.environ.setdefault("INCLUDE_WEEKENDS", "true")

        # Derive interval from bar minutes unless SCHEDULE_INTERVAL_MINUTES is provided
        try:
            interval_min = int(os.getenv("SCHEDULE_INTERVAL_MINUTES", "0"))
        except Exception:
            interval_min = 0
        if interval_min <= 0:
            interval_min = _resolve_bar_minutes_env(60)

        immediate = str(os.getenv("SCHEDULE_IMMEDIATE_RUN", "true")).lower() in ("1", "true", "yes")
        align = str(os.getenv("SCHEDULE_ALIGN_TO_BAR", "true")).lower() in ("1", "true", "yes")

        try:
            first = True
            while True:
                if not immediate and first and align:
                    # Wait until next bar boundary before first run
                    sleep_until_next_bar_kst(interval_min)
                asyncio.run(main(config_path))
                first = False
                if align:
                    sleep_until_next_bar_kst(interval_min)
                else:
                    # Simple fixed sleep (in seconds)
                    import time
                    time.sleep(max(60, interval_min * 60))
        except KeyboardInterrupt:
            pass
    else:
        asyncio.run(main(config_path))

