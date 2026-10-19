#!/usr/bin/env python3
"""Myloz Binance Spot scanner. Safe default: signals only; no real orders."""
import json, os, time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from binance.client import Client

API_KEY = os.getenv('BINANCE_API_KEY', '')
API_SECRET = os.getenv('BINANCE_API_SECRET', '')
LIVE = os.getenv('LIVE_TRADING', '0') == '1'
BUY_USDT = Decimal(os.getenv('BUY_USDT', '0'))
SCAN_SECONDS = 15
MAX_SYMBOLS = 30
MIN_VOLUME = Decimal('1000000')
STOP_LOSS = Decimal('0.025')
TAKE_PROFIT = Decimal('0.04')
STATE_FILE = Path.home() / 'myloz_ai_empire' / 'myloz_live_state.json'
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

if not API_KEY or not API_SECRET:
    raise SystemExit('Missing BINANCE_API_KEY / BINANCE_API_SECRET. Do not paste keys into chat.')
client = Client(API_KEY, API_SECRET, requests_params={"timeout": 20})
try:
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {'positions': {}}
except Exception:
    state = {'positions': {}}
state.setdefault('positions', {})

def save():
    STATE_FILE.write_text(json.dumps(state, indent=2))

def ema(vals, period):
    alpha = 2 / (period + 1)
    out = vals[0]
    for v in vals[1:]: out = alpha * v + (1 - alpha) * out
    return out

def rsi(vals, period=14):
    changes = [b-a for a,b in zip(vals[-period-1:-1], vals[-period:])]
    gains = sum(max(x, 0) for x in changes) / period
    losses = sum(max(-x, 0) for x in changes) / period
    if losses == 0: return 100.0 if gains else 50.0
    return 100 - 100 / (1 + gains/losses)

def rules_for(symbol):
    info = client.get_symbol_info(symbol)
    if not info:
        return None

    fs = {f['filterType']: f for f in info['filters']}
    nf = fs.get('NOTIONAL') or fs.get('MIN_NOTIONAL') or {}

    lot = fs.get('MARKET_LOT_SIZE') or {}
    step = Decimal(lot.get('stepSize', '0'))
    min_qty = Decimal(lot.get('minQty', '0'))

    if step <= 0 or min_qty <= 0:
        lot = fs.get('LOT_SIZE') or {}
        step = Decimal(lot.get('stepSize', '0'))
        min_qty = Decimal(lot.get('minQty', '0'))

    if step <= 0 or min_qty <= 0:
        print(f'  SKIP {symbol}: valid quantity rules unavailable.')
        return None

    return {
        'min_notional': Decimal(nf.get('minNotional', '0')),
        'step': step,
        'min_qty': min_qty
    }

def floor_step(q, step):
    return (q / step).to_integral_value(rounding=ROUND_DOWN) * step if step > 0 else q

def format_qty(q, step):
    precision = max(0, -step.normalize().as_tuple().exponent)
    return format(q, f".{precision}f")

def free_asset(asset):
    a = client.get_account()
    return Decimal(next((b['free'] for b in a['balances'] if b['asset'] == asset), '0'))


def buy(symbol, price, rules):
    available_usdt = free_asset("USDT")
    allocation = Decimal("0.80") if state["positions"] else Decimal("0.70")
    max_trade = Decimal(os.getenv("MAX_TRADE_USDT", "0"))
    spend_cap = min(available_usdt, max_trade)
    spend = min(available_usdt * allocation, spend_cap)

    # If the preferred allocation is below Binance's minimum,
    # use more available USDT only when the configured cap allows it.
    if spend < rules["min_notional"] and spend_cap >= rules["min_notional"]:
        spend = spend_cap

    if spend <= 0 or spend < rules["min_notional"]:
        print(f"BUY SKIPPED: amount {spend}; minimum {rules['min_notional']} USDT")
        return
    if not LIVE:
        print(f"SIGNAL ONLY: would spend about {spend} USDT; no order sent")
        return
    if not client.get_account().get("canTrade", False):
        print("STOP: Spot trading permission disabled")
        return
    quote_precision = int(client.get_symbol_info(symbol).get("quoteAssetPrecision", 8))
    quantum = Decimal("1").scaleb(-quote_precision)
    spend = spend.quantize(quantum, rounding=ROUND_DOWN)
    if spend < rules["min_notional"]:
        print(f"BUY SKIPPED: rounded amount {spend} is below minimum")
        return
    result = client.order_market_buy(symbol=symbol, quoteOrderQty=format(spend, "f"))
    executed = Decimal(result.get("executedQty", "0"))
    fills = result.get("fills", [])
    if executed <= 0:
        print(f"BUY WARNING: no confirmed fill; status={result.get('status')}")
        return
    total = sum((Decimal(f["qty"]) for f in fills), Decimal("0"))
    avg = sum((Decimal(f["qty"]) * Decimal(f["price"]) for f in fills), Decimal("0")) / total if total else price
    base = symbol[:-4]
    fee = sum((Decimal(f.get("commission", "0")) for f in fills if f.get("commissionAsset") == base), Decimal("0"))
    sellable = executed - fee
    if sellable <= 0:
        print("BUY WARNING: executed, but sellable quantity needs manual review")
        return
    state["positions"][symbol] = {"qty": str(sellable), "entry": str(avg)}
    save()
    print(f"BUY RECORDED: status={result.get('status')}; executed={executed}; tracked={sellable}")

def sell(symbol, price, rules, pos, reason):
    tracked = Decimal(pos["qty"])
    base = symbol[:-4]
    available = free_asset(base)
    qty = floor_step(min(tracked, available), rules["step"])
    if qty < rules["min_qty"] or qty * price < rules["min_notional"]:
        print(f"SELL SKIPPED: {symbol} below minimum or quantity unavailable; position retained")
        return
    if not LIVE:
        print(f"SIGNAL ONLY: would sell {symbol} ({reason}); no order sent")
        return
    result = client.order_market_sell(symbol=symbol, quantity=format_qty(qty, rules["step"]))
    executed = Decimal(result.get("executedQty", "0"))
    if executed <= 0:
        print(f"SELL WARNING: no confirmed fill; status={result.get('status')}; position retained")
        return
    remaining = min(max(Decimal("0"), tracked - executed), max(Decimal("0"), available - executed))
    if remaining == 0:
        state["positions"].pop(symbol, None)
    else:
        state["positions"][symbol]["qty"] = str(remaining)
    save()
    print(f"SELL RESULT: status={result.get('status')}; executed={executed}; remaining={remaining}; reason={reason}")

def attempt_bnb_to_usdt():
    """Attempt to sell free BNB only when live mode and explicit confirmation are enabled."""
    try:
        bnb = free_asset('BNB')
        if bnb <= 0:
            print('BNB conversion: no free BNB in Spot.')
            return
        price = Decimal(client.get_symbol_ticker(symbol='BNBUSDT')['price'])
        rules = rules_for('BNBUSDT')
        if not rules:
            print('BNB conversion: BNBUSDT rules unavailable; no order sent.')
            return
        qty = floor_step(bnb, rules['step'])
        value = qty * price
        print(f'BNB available: {bnb}; estimated value: {value:.4f} USDT; market minimum: {rules["min_notional"]} USDT')
        if qty < rules['min_qty'] or value < rules['min_notional']:
            print('BNB sale skipped: Binance market minimum not met. No order sent.')
            return
        if not LIVE:
            print('Signal-only mode: BNB sale not sent.')
            return
        if os.getenv('CONFIRM_BNB_SELL') != 'YES':
            print('BNB sale not sent: explicit confirmation is missing.')
            return
        result = client.order_market_sell(symbol='BNBUSDT', quantity=format(qty, 'f'))
        print(f'BNB/USDT market sell: {result.get("status")}; verify Binance order history.')
    except Exception as e:
        print(f'BNB sale not completed: {type(e).__name__}: {e}')
def show_holdings():
    print("\\n========== MYLOZ PORTFOLIO DASHBOARD ==========")
    total_value = Decimal("0")
    total_cost = Decimal("0")
    account = client.get_account()
    balances = {
        item["asset"]: Decimal(item["free"]) + Decimal(item["locked"])
        for item in account["balances"]
    }
    usdt = balances.get("USDT", Decimal("0"))

    print(f"Available USDT: {usdt:.8f}")
    print("\\n--- Tracked Positions ---")

    for symbol, pos in list(state["positions"].items()):
        try:
            qty = Decimal(pos["qty"])
            entry = Decimal(pos["entry"])
            price = Decimal(client.get_symbol_ticker(symbol=symbol)["price"])
            value = qty * price
            cost = qty * entry
            pnl = value - cost
            pnl_pct = ((price - entry) / entry * 100) if entry else Decimal("0")

            total_value += value
            total_cost += cost

            print(f"\\nCoin: {symbol}")
            print(f"Quantity tracked: {qty}")
            print(f"Entry price: {entry}")
            print(f"Current price: {price}")
            print(f"Market value: {value:.4f} USDT")
            print(f"Unrealized P/L: {pnl:+.4f} USDT ({pnl_pct:+.2f}%)")
        except Exception as e:
            print(f"{symbol}: Price lookup failed: {e}")

    portfolio_value = total_value + usdt
    total_pnl = total_value - total_cost
    pnl_pct = (total_pnl / total_cost * 100) if total_cost else Decimal("0")

    print("\\n---------------- Portfolio Summary ----------------")
    print(f"Tracked coin value: {total_value:.4f} USDT")
    print(f"Available USDT: {usdt:.8f}")
    print(f"Tracked value + USDT: {portfolio_value:.4f} USDT")
    print(f"Tracked positions P/L: {total_pnl:+.4f} USDT ({pnl_pct:+.2f}%)")
    print("===================================================")

def run_scan():
    show_holdings()
    account = client.get_account()
    usdt = next((Decimal(b['free']) for b in account['balances'] if b['asset']=='USDT'), Decimal('0'))
    print('\n=== MYLOZ SPOT SCANNER ===')
    print('API: connected | Spot trading:', account.get('canTrade'), '| Free USDT:', usdt)
    print('Mode:', 'LIVE ORDERS ENABLED' if LIVE else 'SAFE SIGNAL-ONLY MODE')
# BNB conversion disabled for safety
    info = client.get_exchange_info()
    eligible = {s['symbol'] for s in info['symbols'] if s.get('status')=='TRADING' and s.get('quoteAsset')=='USDT' and s.get('isSpotTradingAllowed', True)}
    excluded = ('UPUSDT','DOWNUSDT','BULLUSDT','BEARUSDT')
    stablecoin_pairs = {'FDUSDUSDT', 'USDCUSDT', 'TUSDUSDT', 'USDPUSDT', 'BUSDUSDT', 'DAIUSDT', 'USDEUSDT', 'USD1USDT'}
    tickers = client.get_ticker()
    ranked=[]
    for t in tickers:
        sym=t.get('symbol','')
        if sym in eligible and not sym.endswith(excluded):
            try:
                vol=Decimal(t.get('quoteVolume','0'))
                if vol >= MIN_VOLUME and sym not in stablecoin_pairs: ranked.append((sym,vol))
            except Exception: pass
    low_min_pairs = {
        'DOGEUSDT', 'VTHOUSDT', 'SHIBUSDT', 'PEOPLEUSDT',
        'PEPEUSDT', 'FLOKIUSDT', 'MEMEUSDT', '1000SATSUSDT',
        'BONKUSDT', 'WIFUSDT', 'BOMEUSDT', 'DOGSUSDT',
        'NEIROUSDT', 'TURBOUSDT', '1MBABYDOGEUSDT', 'CATIUSDT',
        'PNUTUSDT', 'ACTUSDT', '1000CATUSDT', 'PENGUUSDT',
        'TRUMPUSDT', '1000CHEEMSUSDT', 'TSTUSDT', 'MUBARAKUSDT',
        'TUTUSDT', 'BROCCOLI714USDT', 'BANANAS31USDT',
        'GIGGLEUSDT', '币安人生USDT', '牛来USDT'
    }

    free_usdt = free_asset("USDT")

    if free_usdt < Decimal("5"):
        symbols = [s for s in low_min_pairs if s in eligible and s not in stablecoin_pairs]
        print(f"Balance below $5: scanning {len(symbols)} eligible coins from the low-minimum list.")
    else:
        symbols = [s for s, _ in sorted(ranked, key=lambda x: x[1], reverse=True)[:100]]
        print(f"Balance $5 or more: scanning {len(symbols)} high-volume USDT pairs.")

    print(f"Scanning {len(symbols)} USDT pairs (15-minute candles)...")
    for symbol in symbols:
        try:
            kl=client.get_klines(symbol=symbol, interval='15m', limit=60)
            closes=[float(k[4]) for k in kl[:-1]]
            volumes=[float(k[5]) for k in kl[:-1]]
            if len(closes)<30: continue
            price=Decimal(str(closes[-1])); fast=ema(closes[-30:],9); slow=ema(closes[-50:],21); mom=rsi(closes)
            rules=rules_for(symbol)
            if not rules: continue
            pos=state['positions'].get(symbol)
            if pos:
                entry=Decimal(pos['entry']); change=(price-entry)/entry if entry else Decimal('0')
                if change <= -STOP_LOSS: sell(symbol,price,rules,pos,f'stop-loss {change:.2%}')
                elif change >= TAKE_PROFIT: sell(symbol,price,rules,pos,f'take-profit {change:.2%}')
                elif fast < slow and mom < 45: sell(symbol,price,rules,pos,'trend weakened')
            elif len(state['positions']) < 3:
                recent_vol = sum(volumes[-5:]) / 5 if len(volumes) >= 25 else 0
                previous_vol = sum(volumes[-25:-5]) / 20 if len(volumes) >= 25 else 0
                trend_ok = fast > slow and price > Decimal(str(fast))
                momentum_ok = 48 <= mom <= 62
                volume_ok = previous_vol > 0 and recent_vol >= previous_vol * 1.10
                if trend_ok and momentum_ok and volume_ok:
                    print(f'V2 CANDIDATE {symbol}: price={price}, RSI={mom:.1f}, trend=up, volume=confirmed')
                    buy(symbol,price,rules)
        except Exception as e:
            print(f'Skip {symbol}: {type(e).__name__}: {e}')
    print(f'Scan complete. Waiting {SCAN_SECONDS}s. Press Ctrl+C to stop.')
    save()

print('Starting scanner. No withdrawals, margin, or futures are used.')
if LIVE and Decimal(os.getenv('MAX_TRADE_USDT', '0')) <= 0:
    raise SystemExit('Safety stop: set MAX_TRADE_USDT to a positive limit before live trading.')
try:
    while True:
        run_scan()
        time.sleep(SCAN_SECONDS)
except KeyboardInterrupt:
    print('\nBot stopped safely.')
except Exception as e:
    raise SystemExit(f'Bot stopped: {type(e).__name__}: {e}')
