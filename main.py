import os
import time
import threading
import re
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import requests
import talib
from datetime import datetime
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')
logger = logging.getLogger('lrc-bot')
load_dotenv()

def load_symbols():
    """Загрузить SYMBOLS из окружения или из .env (поддерживает список и CSV)."""
    env = os.getenv('SYMBOLS')
    if env:
        s = env.strip().strip('[]').replace('"', '').replace("'", "")
        parts = [p.strip() for p in s.split(',') if p.strip()]
        return [p.upper() for p in parts]
    # fallback: parse .env file
    try:
        with open('.env', 'r', encoding='utf-8') as f:
            text = f.read()
            m = re.search(r'SYMBOLS\s*=\s*\[(.*?)\]', text, re.S)
            if m:
                content = m.group(1)
                parts = re.findall(r'["\'](.*?)["\']', content)
                return [p.upper() for p in parts]
    except FileNotFoundError:
        pass
    return []

def send_telegram(text: str):
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Ошибка отправки в Telegram: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Исключение при отправке в Telegram: {e}")


class LRCBybitBot:
    def __init__(self, api_key, api_secret, symbol='SOLUSDT', testnet=True):
        self.session = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret
        )
        self.symbol = symbol
        self.lrc_period = 20
        self.dev_mult = 2.0
        self.rsi_period = 14
        self.risk_per_trade = 0.01  # 1%
        self.position = None
        self.stop_event = threading.Event()
        self.last_signal_time = 0
        self.logger = logging.getLogger(f'lrc-bot.{self.symbol}')

    def get_klines(self, interval='5', limit=100):
        """Получить OHLCV данные"""
        klines = self.session.get_kline(
            category="linear",
            symbol=self.symbol,
            interval=interval,
            limit=limit
        )
        df = pd.DataFrame(klines['result']['list'],
                          columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
        df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        df = df.iloc[::-1].reset_index(drop=True)
        return df

    def calculate_lrc(self, highs, lows, closes, period):
        """Вычисление Linear Regression Channel"""
        linreg = talib.LINEARREG(closes, timeperiod=period)
        slope = talib.LINEARREG_SLOPE(closes, timeperiod=period)
        std = talib.STDDEV(closes, timeperiod=period)
        upper = linreg + (std * self.dev_mult)
        lower = linreg - (std * self.dev_mult)
        return linreg.iloc[-1], upper.iloc[-1], lower.iloc[-1], slope.iloc[-1]

    def calculate_rsi(self, closes):
        return talib.RSI(closes, timeperiod=self.rsi_period).iloc[-1]

    def calculate_atr(self):
        df = self.get_klines(limit=50)
        return talib.ATR(df['high'], df['low'], df['close'], timeperiod=14).iloc[-1]

    def get_usdt_balance(self):
        resp = self.session.get_wallet_balance(accountType="UNIFIED")
        coins = resp['result']['list'][0]['coin']
        usdt = next((c for c in coins if c['coin'] == 'USDT'), None)
        return float(usdt['equity']) if usdt else 0.0

    def get_position_size(self, sl_distance):
        balance = self.get_usdt_balance() or 1000.0
        risk_amount = balance * self.risk_per_trade
        ticker = self.session.get_tickers(category="linear", symbol=self.symbol)['result']['list'][0]
        price = float(ticker['lastPrice'])
        qty_raw = risk_amount / sl_distance
        qty = qty_raw / price
        # Минимальные размеры (по Bybit specs для USDT-фьючерсов)
        if self.symbol.startswith('BTC'):
            return round(qty, 3)
        elif self.symbol.startswith(('ETH', 'SOL')):
            return round(qty, 2)
        else:
            return round(qty, 1)

    def get_position(self):
        positions = self.session.get_positions(category="linear", symbol=self.symbol)
        pos_list = positions['result']['list']
        if pos_list and float(pos_list[0]['size']) > 0:
            return {
                'side': pos_list[0]['side'],
                'size': float(pos_list[0]['size']),
                'entryPrice': float(pos_list[0]['avgPrice'])
            }
        return None

    def place_order(self, side, qty, tp_price=None, sl_price=None):
        try:
            order = self.session.place_order(
                category="linear",
                symbol=self.symbol,
                side=side,
                orderType="Market",
                qty=str(qty),
                takeProfit=tp_price,
                stopLoss=sl_price,
                reduceOnly=False
            )
            logger.info(f"Order placed: {side} {qty} {self.symbol} TP:{tp_price} SL:{sl_price}")
            return order
        except Exception as e:
            logger.error(f"Ошибка размещения ордера: {e}")
            return None

    def check_signals(self):
        now = time.time()
        # Cooldown 15 минут между сигналами
        if now - self.last_signal_time < 15 * 60:
            return

        df = self.get_klines(limit=self.lrc_period + 20)
        if len(df) < self.lrc_period + 5:
            return

        close = df['close'].iloc[-1]
        prev_close = df['close'].iloc[-2]
        high = df['high'].iloc[-1]
        low = df['low'].iloc[-1]

        linreg, upper, lower, slope = self.calculate_lrc(
            df['high'], df['low'], df['close'], self.lrc_period
        )
        rsi = self.calculate_rsi(df['close'])
        atr = self.calculate_atr()
        atr_pct = atr / close * 100

        # 🔴 Фильтр волатильности: не торговать, если ATR < 0.5%
        if atr_pct < 0.5:
            logger.info(f"{self.symbol}: низкая волатильность ({atr_pct:.2f}%) — пропуск")
            return

        self.position = self.get_position()
        # 🔴 НЕ входить, если уже в позиции
        if self.position:
            return

        margin = atr * 0.2
        qty = None

        # ✅ LONG: касание нижней границы + отскок
        if (low <= lower + margin and
            close > prev_close and
            slope > -0.5 * atr and
            30 < rsi < 52):

            sl_price = lower - atr * 1.2
            tp_price = linreg + atr * 0.6
            # Защита от слишком близких уровней
            sl_price = min(sl_price, close - atr * 1.0)
            tp_price = max(tp_price, close + atr * 0.4)
            sl_distance = close - sl_price
            qty = self.get_position_size(sl_distance)

            if qty > 0:
                send_telegram(f"*🔄 LONG (Mean-Revert) on {self.symbol}*\n"
                              f"Price: {close:.5f} | Lower: {lower:.5f}\n"
                              f"Slope: {slope:.6f} | RSI: {rsi:.1f}\n"
                              f"ATR: {atr:.4f} ({atr_pct:.2f}%)\n"
                              f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
                self.place_order("Buy", qty, tp_price=f"{tp_price:.8f}", sl_price=f"{sl_price:.8f}")
                self.last_signal_time = now

        # ✅ SHORT: касание верхней границы + отскок
        elif (high >= upper - margin and
              close < prev_close and
              slope < 0.5 * atr and
              48 < rsi < 70):

            sl_price = upper + atr * 1.2
            tp_price = linreg - atr * 0.6
            sl_price = max(sl_price, close + atr * 1.0)
            tp_price = min(tp_price, close - atr * 0.4)
            sl_distance = sl_price - close
            qty = self.get_position_size(sl_distance)

            if qty > 0:
                send_telegram(f"*🔄 SHORT (Mean-Revert) on {self.symbol}*\n"
                              f"Price: {close:.5f} | Upper: {upper:.5f}\n"
                              f"Slope: {slope:.6f} | RSI: {rsi:.1f}\n"
                              f"ATR: {atr:.4f} ({atr_pct:.2f}%)\n"
                              f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
                self.place_order("Sell", qty, tp_price=f"{tp_price:.8f}", sl_price=f"{sl_price:.8f}")
                self.last_signal_time = now

    def run(self):
        self.logger.info(f"Starting LRC Mean-Revert Bot for {self.symbol}")
        while not self.stop_event.is_set():
            try:
                self.check_signals()
                time.sleep(30)
            except Exception as e:
                self.logger.exception(f"Error in {self.symbol}: {e}")
                time.sleep(60)
        self.logger.info(f"Bot for {self.symbol} stopped")

    def stop(self):
        self.stop_event.set()


if __name__ == "__main__":
    API_KEY = os.getenv('BYBIT_API_KEY')
    API_SECRET = os.getenv('BYBIT_API_SECRET')
    if not API_KEY or not API_SECRET:
        raise ValueError("Set BYBIT_API_KEY and BYBIT_API_SECRET environment variables")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    trading_mode = os.getenv('TRADING_MODE', 'testnet').lower()
    testnet_flag = trading_mode != 'live'

    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "APTUSDT", "TONUSDT", "UNIUSDT"]
    logger.info(f"Запуск ботов для: {symbols}")

    bots = {}
    threads = []

    def start_bot(sym):
        bot = LRCBybitBot(api_key=API_KEY, api_secret=API_SECRET, symbol=sym, testnet=testnet_flag)
        bots[sym] = bot
        try:
            bot.run()
        except Exception:
            logger.exception(f"Unhandled exception in bot {sym}")

    for sym in symbols:
        t = threading.Thread(target=start_bot, args=(sym,), name=f"Bot-{sym}")
        t.start()
        threads.append(t)
        time.sleep(1)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C...")
        for b in bots.values():
            b.stop()
        for t in threads:
            t.join(timeout=10)
        logger.info("Все боты остановлены.")