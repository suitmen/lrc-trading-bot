# backtest.py
import os
import pandas as pd
import numpy as np
import talib
import matplotlib.pyplot as plt
from datetime import datetime
import logging
from pybit.unified_trading import HTTP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backtest")

# === Параметры стратегии ===
LRC_PERIOD = 20
DEV_MULT = 2.0
RSI_PERIOD = 14
RISK_PER_TRADE = 0.01  # 1% баланса
TP_RATIO = 2.0
INITIAL_BALANCE = 1000.0
FEE_TAKER = 0.0006  # Bybit taker fee

# ===================================================================
# Вспомогательные функции — как в main.py
# ===================================================================
def calculate_lrc(df, period=LRC_PERIOD, dev_mult=DEV_MULT):
    closes = df['close']
    linreg = talib.LINEARREG(closes, timeperiod=period)
    slope = talib.LINEARREG_SLOPE(closes, timeperiod=period)
    std = talib.STDDEV(closes, timeperiod=period)
    upper = linreg + dev_mult * std
    lower = linreg - dev_mult * std
    return linreg, upper, lower, slope

def calculate_rsi(df, period=RSI_PERIOD):
    return talib.RSI(df['close'], timeperiod=period)

def calculate_atr(df, period=14):
    return talib.ATR(df['high'], df['low'], df['close'], timeperiod=period)

# ===================================================================
# Сигналы — ОРИГИНАЛЬНАЯ (breakout) и НОВАЯ (mean-revert)
# ===================================================================
def signal_breakout(df, i):
    """Как в main.py — пробойные сигналы"""
    if i < LRC_PERIOD: 
        return None
    close = df['close'].iloc[i]
    prev_close = df['close'].iloc[i-1]
    high = df['high'].iloc[i]
    low = df['low'].iloc[i]
    volume = df['volume'].iloc[i]
    avg_vol = df['volume'].rolling(20).mean().iloc[i]

    linreg, upper, lower, slope = calculate_lrc(df.iloc[:i+1])
    rsi = calculate_rsi(df.iloc[:i+1]).iloc[i]
    
    # Long
    if (prev_close <= upper.iloc[i-1] and close > upper.iloc[i] and
        slope.iloc[i] > 0 and rsi > 50 and volume > avg_vol * 1.2):
        return 'long'
    # Short
    if (prev_close >= lower.iloc[i-1] and close < lower.iloc[i] and
        slope.iloc[i] < 0 and rsi < 50 and volume > avg_vol * 1.2):
        return 'short'
    return None

def signal_meanrevert(df, i):
    """Новая логика — mean-revert"""
    if i < LRC_PERIOD: 
        return None
    close = df['close'].iloc[i]
    prev_close = df['close'].iloc[i-1]
    high = df['high'].iloc[i]
    low = df['low'].iloc[i]

    linreg, upper, lower, slope = calculate_lrc(df.iloc[:i+1])
    rsi = calculate_rsi(df.iloc[:i+1]).iloc[i]
    atr_series = calculate_atr(df.iloc[:i+1])
    atr = atr_series.iloc[i]
    
    margin = atr * 0.2

    # Long: касание нижней границы + отскок
    if (low <= lower.iloc[i] + margin and
        close > prev_close and
        slope.iloc[i] > -0.5 * atr and
        rsi < 52):
        return 'long'

    # Short: касание верхней границы + отскок
    if (high >= upper.iloc[i] - margin and
        close < prev_close and
        slope.iloc[i] < 0.5 * atr and
        rsi > 48):
        return 'short'

    return None

# ===================================================================
# Backtest engine
# ===================================================================
def backtest(df, signal_func, symbol="SOLUSDT"):
    balance = INITIAL_BALANCE
    equity_curve = [balance]
    trades = []
    position = None  # {'type': 'long', 'entry': ..., 'sl': ..., 'tp': ..., 'qty': ..., 'entry_idx': i}
    
    for i in range(LRC_PERIOD, len(df)):
        timestamp = df.index[i]
        price = df['close'].iloc[i]

        # Обработка выхода по TP/SL
        if position:
            if position['type'] == 'long':
                if price <= position['sl']:
                    pnl = (position['sl'] - position['entry']) * position['qty']
                    fee = (position['entry'] + position['sl']) * position['qty'] * FEE_TAKER
                    balance += pnl - fee
                    trades.append({
                        'symbol': symbol,
                        'type': 'long',
                        'entry': position['entry'],
                        'exit': position['sl'],
                        'qty': position['qty'],
                        'pnl': pnl - fee,
                        'timestamp': timestamp,
                        'reason': 'SL'
                    })
                    position = None
                elif price >= position['tp']:
                    pnl = (position['tp'] - position['entry']) * position['qty']
                    fee = (position['entry'] + position['tp']) * position['qty'] * FEE_TAKER
                    balance += pnl - fee
                    trades.append({
                        'symbol': symbol,
                        'type': 'long',
                        'entry': position['entry'],
                        'exit': position['tp'],
                        'qty': position['qty'],
                        'pnl': pnl - fee,
                        'timestamp': timestamp,
                        'reason': 'TP'
                    })
                    position = None
            elif position['type'] == 'short':
                if price >= position['sl']:
                    pnl = (position['entry'] - position['sl']) * position['qty']
                    fee = (position['entry'] + position['sl']) * position['qty'] * FEE_TAKER
                    balance += pnl - fee
                    trades.append({
                        'symbol': symbol,
                        'type': 'short',
                        'entry': position['entry'],
                        'exit': position['sl'],
                        'qty': position['qty'],
                        'pnl': pnl - fee,
                        'timestamp': timestamp,
                        'reason': 'SL'
                    })
                    position = None
                elif price <= position['tp']:
                    pnl = (position['entry'] - position['tp']) * position['qty']
                    fee = (position['entry'] + position['tp']) * position['qty'] * FEE_TAKER
                    balance += pnl - fee
                    trades.append({
                        'symbol': symbol,
                        'type': 'short',
                        'entry': position['entry'],
                        'exit': position['tp'],
                        'qty': position['qty'],
                        'pnl': pnl - fee,
                        'timestamp': timestamp,
                        'reason': 'TP'
                    })
                    position = None

        # Генерация нового сигнала (если нет открытой позиции)
        if not position:
            sig = signal_func(df.iloc[:i+1], i)
            if sig:
                atr = calculate_atr(df.iloc[:i+1]).iloc[i]
                risk_usd = balance * RISK_PER_TRADE

                if sig == 'long':
                    entry = price
                    sl = df['lower'].iloc[i] - atr * 1.0
                    tp = df['linreg'].iloc[i] + atr * 0.3
                    sl = min(sl, entry - atr * 1.2)
                    tp = max(tp, entry + atr * 0.8)
                    qty = risk_usd / (entry - sl)
                    position = {
                        'type': 'long', 'entry': entry, 'sl': sl, 'tp': tp,
                        'qty': qty, 'entry_idx': i
                    }

                elif sig == 'short':
                    entry = price
                    sl = df['upper'].iloc[i] + atr * 1.0
                    tp = df['linreg'].iloc[i] - atr * 0.3
                    sl = max(sl, entry + atr * 1.2)
                    tp = min(tp, entry - atr * 0.8)
                    qty = risk_usd / (sl - entry)
                    position = {
                        'type': 'short', 'entry': entry, 'sl': sl, 'tp': tp,
                        'qty': qty, 'entry_idx': i
                    }

        equity_curve.append(balance)

    return pd.DataFrame(trades), np.array(equity_curve)

# ===================================================================
# Подготовка данных (пример для SOLUSDT)
# ===================================================================
def fetch_bybit_5m(symbol, days=7):
    session = HTTP()
    limit = 2000
    klines = session.get_kline(
        category="linear",
        symbol=symbol,
        interval="5",
        limit=limit
    )['result']['list']
    df = pd.DataFrame(klines, columns=['ts','open','high','low','close','vol','turn'])
    df['timestamp'] = pd.to_datetime(df['ts'].astype(int), unit='ms')
    df.set_index('timestamp', inplace=True)
    df = df[['open','high','low','close','vol']].astype(float).iloc[::-1]
    df.columns = ['open','high','low','close','volume']
    # добавить LRC
    linreg, upper, lower, _ = calculate_lrc(df)
    df['linreg'] = linreg
    df['upper'] = upper
    df['lower'] = lower
    return df
# ===================================================================
# Запуск
# ===================================================================
if __name__ == "__main__":
    SYMBOL = "SOLUSDT"
    df = fetch_bybit_5m(SYMBOL)

    logger.info("Running original (breakout) strategy...")
    trades_orig, equity_orig = backtest(df, signal_breakout, SYMBOL)

    logger.info("Running new (mean-revert) strategy...")
    trades_new, equity_new = backtest(df, signal_meanrevert, SYMBOL)

    # Статистика
    def stats(trades, equity):
        if len(trades) == 0:
            return {"total": 0, "win_rate": 0, "profit_factor": 0}
        wins = trades[trades['pnl'] > 0]
        loss = trades[trades['pnl'] < 0]
        gross_profit = wins['pnl'].sum()
        gross_loss = abs(loss['pnl'].sum())
        pf = gross_profit / gross_loss if gross_loss > 0 else np.inf
        return {
            "total": len(trades),
            "win_rate": len(wins) / len(trades),
            "profit_factor": pf,
            "net_pnl": trades['pnl'].sum(),
            "max_drawdown": (np.maximum.accumulate(equity) - equity).max()
        }

    print("\n=== BACKTEST RESULTS ===")
    print(f"Initial balance: {INITIAL_BALANCE:.2f} USDT\n")
    print("🔹 ORIGINAL (breakout):")
    s1 = stats(trades_orig, equity_orig)
    for k, v in s1.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.3f}")
        else:
            print(f"  {k}: {v}")

    print("\n🔹 NEW (mean-revert):")
    s2 = stats(trades_new, equity_new)
    for k, v in s2.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.3f}")
        else:
            print(f"  {k}: {v}")

    # Plot
    plt.figure(figsize=(12, 6))
    plt.plot(equity_orig, label='Original (breakout)', alpha=0.7)
    plt.plot(equity_new, label='New (mean-revert)', linewidth=2)
    plt.axhline(INITIAL_BALANCE, color='gray', linestyle='--', linewidth=1)
    plt.title(f'Equity Curve: {SYMBOL} (5m, 7 days)')
    plt.xlabel('Candle index')
    plt.ylabel('Balance, USDT')
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig("backtest_equity.png", dpi=150)
    logger.info("Equity curve saved to backtest_equity.png")
    plt.show()