import os
import time
import threading
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import requests
import talib
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s'
)
logger = logging.getLogger('lrc-bot')
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error(f"❌ Telegram error: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"💥 Telegram exception: {e}")


class LRCBybitBot:
    def __init__(self, api_key, api_secret, symbol='DOGEUSDT', testnet=True):
        self.session = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)
        self.symbol = symbol
        self.lrc_period = 20
        self.dev_mult = 2.0
        self.rsi_period = 14
        self.risk_per_trade = 0.01
        self.stop_event = threading.Event()
        self.last_signal_time = 0
        self.last_position_size = 0.0
        self.trade_log = []          # ← для сбора статистики
        self.last_report_time = 0    # ← для ежечасного отчёта
        self.logger = logging.getLogger(f'lrc-bot.{self.symbol}')

        # Настройки по символу — оптимизировано под DOGE
        if self.symbol == "APTUSDT":
            self.min_qty = 50.0
            self.sl_mult = 1.0
            self.tp_mult = 1.0
        elif self.symbol in ["TONUSDT", "DOGEUSDT"]:
            self.min_qty = 100.0
            self.sl_mult = 1.0
            self.tp_mult = 0.9
        elif self.symbol == "ETHUSDT":
            self.min_qty = 0.1
            self.sl_mult = 1.0
            self.tp_mult = 0.9
        else:
            self.min_qty = 1.0
            self.sl_mult = 1.0
            self.tp_mult = 0.9

    def get_klines(self, interval='5', limit=250):
        klines = self.session.get_kline(category="linear", symbol=self.symbol, interval=interval, limit=limit)
        df = pd.DataFrame(
            klines['result']['list'],
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover']
        )
        df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        df = df.iloc[::-1].reset_index(drop=True)
        return df

    def calculate_ema_filter(self):
        df = self.get_klines(limit=250)
        if len(df) < 201:
            return 'neutral', df['close'].iloc[-1], None, None
        close = df['close'].iloc[-1]
        ema50 = talib.EMA(df['close'], timeperiod=50).iloc[-1]
        ema200 = talib.EMA(df['close'], timeperiod=200).iloc[-1]
        if ema50 > ema200 * 1.001 and close > ema50:
            return 'up', close, ema50, ema200
        elif ema50 < ema200 * 0.999 and close < ema50:
            return 'down', close, ema50, ema200
        else:
            return 'neutral', close, ema50, ema200

    def calculate_lrc(self, highs, lows, closes, period):
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
        return float(usdt['equity']) if usdt else 1000.0

    def get_position_size(self, sl_distance_usd):
        if sl_distance_usd <= 0:
            return 0
        balance = self.get_usdt_balance()
        risk_amount = balance * self.risk_per_trade
        qty_raw = risk_amount / sl_distance_usd
        ticker = self.session.get_tickers(category="linear", symbol=self.symbol)['result']['list'][0]
        price = float(ticker['lastPrice'])
        qty = qty_raw / price
        if qty < self.min_qty:
            return 0
        if qty * price > balance * 0.1:
            qty = (balance * 0.1) / price
        if self.symbol == "BTCUSDT":
            return round(qty, 3)
        elif self.symbol == "APTUSDT":
            return int(qty)
        elif self.symbol in ["TONUSDT", "DOGEUSDT"]:
#            return round(qty, 1)
            if qty == 0.0: 
                return 0
            else: return 900.0
        elif self.symbol == "ETHUSDT":
            return round(qty, 2)
        else:
            return round(qty, 2)

    def get_position(self):
        positions = self.session.get_positions(category="linear", symbol=self.symbol)
        pos_list = positions['result']['list']
        if pos_list and float(pos_list[0]['size']) > 0:
            return {'side': pos_list[0]['side'], 'size': float(pos_list[0]['size'])}
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
            logger.info(f"✅ {side} {qty} {self.symbol} → TP:{tp_price} SL:{sl_price}")
            return order
        except Exception as e:
            logger.error(f"🛑 Order failed: {e}")
            return None

    def check_signals(self):
        now = time.time()
        if now - self.last_signal_time < 15 * 60:
            return

        df = self.get_klines(limit=self.lrc_period + 30)
        if len(df) < self.lrc_period + 10:
            return

        # 🔔 Логирование закрытия позиции + сбор статистики
        real_pos = self.get_position()
        current_size = real_pos['size'] if real_pos else 0.0
        if self.last_position_size > 0 and current_size == 0 and (now - self.last_signal_time) > 60:
            # Получаем последний закрытый PnL
            try:
                closed_pnl = self.session.get_closed_pnl(category="linear", symbol=self.symbol, limit=1)
                if closed_pnl['result']['list']:
                    pnl = float(closed_pnl['result']['list'][0]['closedPnl'])
                    is_win = pnl > 0
                    self.trade_log.append({'pnl': pnl, 'is_win': is_win})
                    logger.info(f"📊 Закрыта сделка: PnL={pnl:.4f}, Win={is_win}")
            except Exception as e:
                logger.error(f"Ошибка получения PnL: {e}")

            logger.info(f"🔔 {self.symbol}: TP/SL сработал!")
            send_telegram(f"✅ TP/SL сработал по {self.symbol}!")
            self.last_position_size = 0
        else:
            self.last_position_size = current_size

        # 🔒 Никаких сигналов при открытой позиции
        if real_pos:
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
        short_range = (df['high'].iloc[-5:].max() - df['low'].iloc[-5:].min()) / close * 100
        roc_15m = (close / df['close'].iloc[-4] - 1) * 100 if len(df) >= 5 else 0
        effective_vol = max(atr_pct, short_range * 0.7)
        volatility_override = abs(roc_15m) >= 0.8

        # 📉 Порог волатильности
        vol_threshold = 0.28 if self.symbol in ["DOGEUSDT", "TONUSDT"] else 0.30
        if not volatility_override and effective_vol < vol_threshold:
            logger.info(f"⏸ {self.symbol}: vol={effective_vol:.2f}%, ROC15={roc_15m:+.2f}% → skip")
            return

        # 📈 EMA TREND FILTER
        trend, _, ema50, ema200 = self.calculate_ema_filter()
        allow_long = (trend != 'down')
        allow_short = (trend != 'up')

        margin = atr * 0.2
        logger.info(f"📈 {self.symbol} Trend: {trend}, allow_long={allow_long}, allow_short={allow_short}")

        # ✅ LONG
        if allow_long and (low <= lower + margin and
                           close > prev_close and
                           slope > -0.5 * atr and
                           (35 < rsi < 52 if self.symbol == 'APTUSDT' else 30 < rsi < 52)):

            sl_price = lower - atr * self.sl_mult
            tp_price = linreg + atr * self.tp_mult
            sl_price = min(sl_price, close - atr * max(0.8, self.sl_mult))
            tp_price = max(tp_price, close + atr * max(0.5, self.tp_mult))
            sl_dist_usd = close - sl_price
            qty = self.get_position_size(sl_dist_usd)
            if qty > 0:
                send_telegram(
                    f"*🔄 LONG on {self.symbol}*\n"
                    f"Price: {close:.5f} | Lower: {lower:.5f}\n"
                    f"Slope: {slope:+.6f} | RSI: {rsi:.1f}\n"
                    f"Trend: {trend} | ATR%: {effective_vol:.2f}%\n"
                    f"TP: {tp_price:.5f} | SL: {sl_price:.5f}"
                )
                self.place_order("Buy", qty, tp_price=f"{tp_price:.8f}", sl_price=f"{sl_price:.8f}")
                self.last_signal_time = now

        # ✅ SHORT
        elif allow_short and (high >= upper - margin and
                              close < prev_close and
                              slope < 0.5 * atr and
                              (48 < rsi < 65 if self.symbol == 'APTUSDT' else 48 < rsi < 70)):

            sl_price = upper + atr * self.sl_mult
            tp_price = linreg - atr * self.tp_mult
            sl_price = max(sl_price, close + atr * max(0.8, self.sl_mult))
            tp_price = min(tp_price, close - atr * max(0.5, self.tp_mult))
            sl_dist_usd = sl_price - close
            qty = self.get_position_size(sl_dist_usd)
            if qty > 0:
                send_telegram(
                    f"*🔄 SHORT on {self.symbol}*\n"
                    f"Price: {close:.5f} | Upper: {upper:.5f}\n"
                    f"Slope: {slope:+.6f} | RSI: {rsi:.1f}\n"
                    f"Trend: {trend} | ATR%: {effective_vol:.2f}%\n"
                    f"TP: {tp_price:.5f} | SL: {sl_price:.5f}"
                )
                self.place_order("Sell", qty, tp_price=f"{tp_price:.8f}", sl_price=f"{sl_price:.8f}")
                self.last_signal_time = now

        # 📊 ЕЖЕЧАСНЫЙ ОТЧЁТ
        if now - self.last_report_time > 3600 and self.trade_log:
            total_trades = len(self.trade_log)
            wins = sum(1 for t in self.trade_log if t['is_win'])
            win_rate = wins / total_trades * 100
            total_pnl = sum(t['pnl'] for t in self.trade_log)
            avg_pnl = total_pnl / total_trades

            report = (
                f"*📊 Отчёт по {self.symbol} (последний час)*\n"
                f"Сделок: {total_trades}\n"
                f"Win Rate: {win_rate:.1f}%\n"
                f"Общий PnL: {total_pnl:.4f} USDT\n"
                f"Средний PnL: {avg_pnl:.4f} USDT"
            )
            send_telegram(report)
            logger.info(f"📤 Отправлен отчёт по {self.symbol}: {report}")

            self.trade_log.clear()
            self.last_report_time = now

    def run(self):
        self.logger.info(f"🚀 Starting EMA-filtered LRC Bot for {self.symbol}")
        while not self.stop_event.is_set():
            try:
                self.check_signals()
                time.sleep(30)
            except Exception as e:
                self.logger.exception(f"💥 Crash in {self.symbol}: {e}")
                time.sleep(60)
        self.logger.info(f"⏹ Bot for {self.symbol} stopped")

    def stop(self):
        self.stop_event.set()


if __name__ == "__main__":
    API_KEY = os.getenv('BYBIT_API_KEY')
    API_SECRET = os.getenv('BYBIT_API_SECRET')
    if not API_KEY or not API_SECRET:
        raise ValueError("Set BYBIT_API_KEY and BYBIT_API_SECRET in .env")

    testnet = os.getenv('TRADING_MODE', 'testnet').lower() != 'live'
    symbols = ["DOGEUSDT","APTUSDT","TONUSDT"]  # рекомендуется начать только с DOGE
    logger.info(f"▶ Starting EMA-filtered bots for: {symbols}")

    bots = {}
    threads = []

    def start_bot(sym):
        bot = LRCBybitBot(api_key=API_KEY, api_secret=API_SECRET, symbol=sym, testnet=testnet)
        bots[sym] = bot
        bot.run()

    for sym in symbols:
        t = threading.Thread(target=start_bot, args=(sym,), name=f"Bot-{sym}")
        t.start()
        threads.append(t)
        time.sleep(1)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Stopping all bots (Ctrl+C)...")
        for b in bots.values():
            b.stop()
        for t in threads:
            t.join(timeout=10)
        logger.info("✅ All bots stopped gracefully.")