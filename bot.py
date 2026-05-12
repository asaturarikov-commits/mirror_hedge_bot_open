#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 Simple Mirror Hedge Bot v8.1 (P&L + баланс USDT)
"""
import requests, time, hashlib, hmac, json, os, uuid, logging
from urllib.parse import urlencode
from datetime import datetime
from typing import Optional, Dict, List

# ================== НАСТРОЙКИ ==================
API_KEY = "SFjjUI2d80zAfDmxeq"
API_SECRET = "sL8rVoteMCfv7DH1jIomkchS1cWCN0AiBdRK"
AUTO_TRADE = True
STATE_FILE = "active_bundles.json"
CHECK_INTERVAL = 60
LOT = 0.2
PROFIT_PERCENT = 15  # <--- ИЗМЕНЕНО НА 15%
TARGET_OTM = 400
OTM_TOLERANCE = 50
EXP_DAYS_MIN = 21
EXP_DAYS_MAX = 56
HEDGE_ORDERS = 4
HEDGE_FIRST_DISTANCE = 120
HEDGE_STEP = 40
BYBIT_BASE_URL = "https://api.bybit.com"
RECV_WINDOW = 30000
REQUEST_TIMEOUT = 30

# ================== ЛОГИРОВАНИЕ ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def log(msg: str, level: str = "INFO"):
    icons = {"INFO":"ℹ️","SUCCESS":"✅","ERROR":"❌","WARN":"⚠️","TRADE":"💹","SYS":"⚙️"}
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {icons.get(level, '•')} {msg}")
    if level in ("ERROR", "WARN", "TRADE", "SUCCESS"):
        logging.info(f"{level} | {msg}")

# ================== API ==================
def send_request(method: str, endpoint: str, params: Optional[Dict] = None, body: Optional[Dict] = None) -> Dict:
    ts = str(int(time.time() * 1000))
    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": str(RECV_WINDOW),
        "Content-Type": "application/json"
    }
    if method == "POST":
        body_str = json.dumps(body, separators=(',', ':')) if body else ""
        sign_str = ts + API_KEY + str(RECV_WINDOW) + body_str
        headers["X-BAPI-SIGN"] = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        r = requests.post(f"{BYBIT_BASE_URL}{endpoint}", data=body_str, headers=headers, timeout=REQUEST_TIMEOUT)
    else:
        p = params or {}
        q = urlencode(sorted(p.items()))
        sign_str = ts + API_KEY + str(RECV_WINDOW) + q
        headers["X-BAPI-SIGN"] = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        r = requests.get(f"{BYBIT_BASE_URL}{endpoint}", params=p, headers=headers, timeout=REQUEST_TIMEOUT)
    res = r.json()
    if res.get("retCode") != 0:
        log(f"Bybit: {res.get('retMsg')} (code: {res.get('retCode')})", "ERROR")
    return res

# ================== БАЛАНС ДЕПОЗИТА ==================
def get_usdt_balance() -> float:
    try:
        res = send_request("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED"})
        if res.get("retCode") == 0:
            for coin in res["result"]["list"][0]["coin"]:
                if coin["coin"] == "USDT":
                    return float(coin.get("equity", 0))
    except Exception:
        pass
    return 0.0

# ================== HEDGE MODE ==================
def ensure_hedge_mode():
    log("🔄 Проверяем Hedge Mode...", "SYS")
    res = send_request("POST", "/v5/position/switch-mode", body={"category": "linear", "symbol": "ETHUSDT", "mode": 1})
    if res.get("retCode") in (0, 10001):
        log("✅ Hedge Mode активен", "SUCCESS")
    else:
        log("⚠️ Hedge Mode не переключён", "WARN")

# ================== ЛОГИКА СТРАТЕГИИ ==================
def get_current_price() -> Optional[float]:
    r = requests.get(f"{BYBIT_BASE_URL}/v5/market/tickers", params={"category": "linear", "symbol": "ETHUSDT"}, timeout=10).json()
    return float(r["result"]["list"][0]["lastPrice"]) if r.get("retCode") == 0 and r["result"]["list"] else None

def get_eth_options() -> List[Dict]:
    r = requests.get(f"{BYBIT_BASE_URL}/v5/market/instruments-info", params={"category": "option", "baseCoin": "ETH", "limit": 1000}, timeout=15).json()
    return r["result"]["list"] if r.get("retCode") == 0 else []

def parse_option(symbol: str) -> Optional[Dict]:
    try:
        clean = symbol.replace("-USDT", "").replace("USDT", "").rstrip("-")
        parts = clean.split("-")
        if len(parts) < 4: return None
        strike = int(parts[2])
        opt_type = "Put" if parts[3] == "P" else "Call"
        exp = datetime.strptime(parts[1], "%d%b%y")
        if exp.year < 2000: exp = exp.replace(year=exp.year + 100)
        return {"symbol": symbol, "strike": strike, "type": opt_type, "days_to_exp": (exp - datetime.now()).days}
    except Exception as e:
        log(f"Parse error for {symbol}: {e}", "ERROR")
        return None

def find_best_setup(side: str, price: float) -> Optional[Dict]:
    cands = []
    for opt in get_eth_options():
        info = parse_option(opt["symbol"])
        if not info or not (EXP_DAYS_MIN <= info["days_to_exp"] <= EXP_DAYS_MAX): continue
        dist = (info["strike"] - price) if side == "call" else (price - info["strike"])
        if side == "call" and info["type"] != "Call": continue
        if side == "put" and info["type"] != "Put": continue
        if TARGET_OTM - OTM_TOLERANCE <= dist <= TARGET_OTM + OTM_TOLERANCE:
            cands.append({**info, "distance": dist, "deviation": abs(dist - TARGET_OTM)})
    return min(cands, key=lambda x: x["deviation"]) if cands else None

def get_option_price(symbol: str) -> Optional[float]:
    r = requests.get(f"{BYBIT_BASE_URL}/v5/market/tickers", params={"category": "option", "symbol": symbol}, timeout=15).json()
    return float(r["result"]["list"][0]["markPrice"]) if r.get("retCode") == 0 and r["result"].get("list") else None

def calc_profit(entry: float, current: float) -> float:
    return round((entry - current) / entry * 100, 2) if entry > 0 and current > 0 else 0.0

def place_option_order(symbol: str, side: str, qty: float) -> Dict:
    order_link_id = f"opt-{uuid.uuid4().hex[:12]}"
    body = {"category": "option", "symbol": symbol, "side": side, "orderType": "Market", "qty": str(qty), "orderLinkId": order_link_id}
    log(f"📤 Ордер опцион: {symbol} | {side} {qty} @ Market", "TRADE")
    return send_request("POST", "/v5/order/create", body=body)

def place_hedge_order(side: str, qty: float, price: float, trigger_price: float, trigger_direction: int):
    order_link_id = f"hedge-{uuid.uuid4().hex[:12]}"
    position_idx = 1 if side == "Buy" else 2
    body = {
        "category": "linear", "symbol": "ETHUSDT", "side": side, "orderType": "Limit",
        "price": str(price), "qty": str(qty), "timeInForce": "GTC",
        "orderLinkId": order_link_id, "positionIdx": position_idx,
        "triggerPrice": str(trigger_price), "triggerDirection": trigger_direction, "triggerBy": "LastPrice"
    }
    log(f"🛡️ УСЛОВНЫЙ хедж: {side} {qty} @ {price} | trigger={trigger_price}", "TRADE")
    return send_request("POST", "/v5/order/create", body=body), order_link_id

def create_hedge_orders(setup: Dict):
    strike, opt_type = setup["strike"], setup["type"]
    hedge_qty = max(0.01, round(LOT / HEDGE_ORDERS, 2))
    hedge_ids = []
    log(f"🛡️ Выставляем {HEDGE_ORDERS} УСЛОВНЫХ хеджей (каждый = {hedge_qty})", "SYS")
    for i in range(HEDGE_ORDERS):
        dist = HEDGE_FIRST_DISTANCE - (i * HEDGE_STEP)
        if opt_type == "Put":
            hedge_price = strike + dist
            res, link_id = place_hedge_order("Sell", hedge_qty, hedge_price, hedge_price, 2)
        else:
            hedge_price = strike - dist
            res, link_id = place_hedge_order("Buy", hedge_qty, hedge_price, hedge_price, 1)
        if res.get("retCode") == 0:
            hedge_ids.append(link_id)
        time.sleep(0.8)
    log("✅ УСЛОВНЫЕ ХЕДЖИ РАЗМЕЩЕНЫ", "SUCCESS")
    return hedge_ids

def cancel_hedge_orders(hedge_ids: List[str]):
    log(f"🗑️ Отмена {len(hedge_ids)} хеджей по ID...", "SYS")
    count = 0
    for link_id in hedge_ids:
        res = send_request("POST", "/v5/order/cancel", body={"category": "linear", "symbol": "ETHUSDT", "orderLinkId": link_id})
        if res.get("retCode") == 0:
            count += 1
    log(f"✅ Отменено {count}/{len(hedge_ids)} хеджей", "SUCCESS")

def open_new_bundle(setup: Dict) -> Optional[Dict]:
    log(f"🎯 Открываем связку: {setup['symbol']} LOT={LOT}", "TRADE")
    if not AUTO_TRADE:
        log(f"🧪 [ТЕСТ] Продали бы {LOT} шт.", "INFO")
        return {"symbol": setup["symbol"], "strike": setup["strike"], "type": setup["type"], "qty": LOT, "entry_price": 0, "entry_time": datetime.now().isoformat(), "hedge_ids": []}
    res = place_option_order(setup["symbol"], "Sell", LOT)
    if res.get("retCode") != 0: return None
    time.sleep(2)
    entry_price = get_option_price(setup["symbol"]) or 0
    log(f"✅ Вход @ {entry_price}", "SUCCESS")
    hedge_ids = create_hedge_orders(setup)
    return {"symbol": setup["symbol"], "strike": setup["strike"], "type": setup["type"], "qty": LOT, "entry_price": entry_price, "entry_time": datetime.now().isoformat(), "hedge_ids": hedge_ids}

def close_bundle(bundle: Dict):
    log(f" Закрываем связку: {bundle['symbol']}", "TRADE")
    if not AUTO_TRADE: return log(f"🧪 [ТЕСТ] Закрыли бы", "INFO")
    place_option_order(bundle["symbol"], "Buy", bundle["qty"])
    time.sleep(2)
    if bundle.get("hedge_ids"): cancel_hedge_orders(bundle["hedge_ids"])
    log("✅ Связка закрыта + её хеджи отменены", "SUCCESS")

def check_profit_exit(bundle: Dict) -> bool:
    cur = get_option_price(bundle["symbol"])
    if not cur or not bundle.get("entry_price"): return False
    pnl = calc_profit(bundle["entry_price"], cur)
    opt_type = bundle["type"].upper()
    log(f"📊 P&L {opt_type}: {pnl}%")
    if pnl >= PROFIT_PERCENT:
        log(f"🎯 Цель {PROFIT_PERCENT}% достигнута ({pnl}%) — закрываем {opt_type}", "SUCCESS")
        return True
    return False

def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try: return json.load(open(STATE_FILE, "r", encoding="utf-8"))
        except Exception: pass
    return {"call": None, "put": None}

def save_state(state: Dict):
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

# ====================== ЗАПУСК ======================
def main():
    # ✅ КРИТИЧЕСКИ ВАЖНО: \n вместо реальных переносов строк
    header = "\n" + "═" * 50 + f"\n Simple Mirror Hedge Bot v8.1 (P&L + баланс)\n" + "═" * 50
    print(header)
    log(f"Лот: {LOT} | Прибыль: {PROFIT_PERCENT}% | AUTO_TRADE: {AUTO_TRADE}", "SYS")
    ensure_hedge_mode()
    state = load_state()
    while True:
        try:
            log("🔄 Цикл...", "INFO")
            balance = get_usdt_balance()
            log(f"💰 Депозит USDT: {balance:.2f}", "SYS")
            price = get_current_price()
            if not price:
                log("⚠️ Не удалось получить цену ETHUSDT, пропуск цикла", "WARN")
                time.sleep(CHECK_INTERVAL)
                continue
            for side in ["call", "put"]:
                bundle = state.get(side)
                if bundle is None:
                    setup = find_best_setup(side, price)
                    if setup:
                        nb = open_new_bundle(setup)
                        if nb:
                            state[side] = nb
                            save_state(state)
                else:
                    if check_profit_exit(bundle):
                        close_bundle(bundle)
                        state[side] = None
                        save_state(state)
                        log(f"🔄 {side.upper()} позиция закрыта. Ожидание новой точки входа...", "SYS")
                    else:
                        log(f"⏳ {side.upper()} активна. Ждём профита {PROFIT_PERCENT}%", "INFO")
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            log("🛑 Остановлен пользователем", "WARN")
            break
        except Exception as e:
            log(f"❌ Критическая ошибка цикла: {e}", "ERROR")
            time.sleep(10)

if __name__ == "__main__":
    main()