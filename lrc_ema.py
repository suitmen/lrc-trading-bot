# strategy/lrc_ema.py
import talib
import time
import logging
from bybit import BybitClient
from telegram import send_telegram
from monitoring import TradeMonitor

logger = logging.getLogger(__name__)

class LRCEMABot:
    def __init__(self, client: BybitClient, symbol: str):
        self.client = client
        self.symbol = symbol
        self.monitor = TradeMonitor(symbol)
        self.last_signal_time = 0
        self.last_position_size = 0.0

        # Параметры по символу
        if symbol == "APTUSDT":
            self.min_qty = 50.0
            self.sl_mult = 1.0
            self.tp_mult = 1.0
        elif symbol in ["TONUSDT", "DOGEUSDT"]:
            self.min_qty = 100.0
            self.sl_mult = 1.0
            self.tp_mult = 0.9
        elif symbol == "ETHUSDT":
            self.min_qty = 0.1
            self.sl_mult = 1.0
            self.tp_mult = 0.9
        else:
            self.min_qty = 1.0
            self.sl_mult = 1.0
            self.tp_mult = 0.9

    def calculate_ema_filter(self, df):
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

    def get_position_size(self, sl_distance_usd, balance, price):
        if sl_distance_usd <= 0:
            return 0
        risk_amount = balance * 0.01
        qty_raw = risk_amount / sl_distance_usd
        qty = qty_raw / price
        if qty < self.min_qty:
            return 0
        if qty * price > balance * 0.1:
            qty = (balance * 0.1) / price
        if self.symbol in ["TONUSDT", "DOGEUSDT"]:
            return round(qty, 1)
        elif self.symbol == "ETHUSDT":
            return round(qty, 2)
        elif self.symbol == "APTUSDT":
            return int(qty)
        else:
            return round(qty, 2)

    def run_once(self):
        now = time.time()
        if now - self.last_signal_time < 15 * 60:
            return

        df = self.client.get_klines(self.symbol, limit=250)
        if len(df) < 30:
            print("too low data...")
            return

        real_pos = self.client.get_position(self.symbol)
        current_size = real_pos['size'] if real_pos else 0.0

        # Логирование закрытия позиции
        if self.last_position_size > 0 and current_size == 0 and (now - self.last_signal_time) > 60:
            try:
                closed_pnl = self.client.get_closed_pnl(self.symbol, limit=1)
                if closed_pnl['result']['list']:
                    pnl = float(closed_pnl['result']['list'][0]['closedPnl'])
                    self.monitor.log_trade(pnl)
                    logger.info(f"📊 Закрыта сделка: PnL={pnl:.4f}")
            except Exception as e:
                logger.error(f"Ошибка получения PnL: {e}")
            logger.info(f"🔔 {self.symbol}: TP/SL сработал!")
            send_telegram(f"✅ TP/SL сработал по {self.symbol}!")
            self.last_position_size = 0
        else:
            self.last_position_size = current_size

        if real_pos:
            return

        close = df['close'].iloc[-1]
        prev_close = df['close'].iloc[-2]
        high = df['high'].iloc[-1]
        low = df['low'].iloc[-1]
        linreg, upper, lower, slope = talib.LINEARREG(df['close'], timeperiod=20).iloc[-1], \
                                      talib.LINEARREG(df['close'], timeperiod=20).iloc[-1] + \
                                      talib.STDDEV(df['close'], timeperiod=20).iloc[-1] * 2.0, \
                                      talib.LINEARREG(df['close'], timeperiod=20).iloc[-1] - \
                                      talib.STDDEV(df['close'], timeperiod=20).iloc[-1] * 2.0, \
                                      talib.LINEARREG_SLOPE(df['close'], timeperiod=20).iloc[-1]
        rsi = talib.RSI(df['close'], timeperiod=14).iloc[-1]
        atr = talib.ATR(df['high'], df['low'], df['close'], timeperiod=14).iloc[-1]

        atr_pct = atr / close * 100
        short_range = (df['high'].iloc[-5:].max() - df['low'].iloc[-5:].min()) / close * 100
        roc_15m = (close / df['close'].iloc[-4] - 1) * 100 if len(df) >= 5 else 0
        effective_vol = max(atr_pct, short_range * 0.7)
        volatility_override = abs(roc_15m) >= 0.8

        vol_threshold = 0.28 if self.symbol in ["DOGEUSDT", "TONUSDT"] else 0.30
        if not volatility_override and effective_vol < vol_threshold:
            return

        trend, _, ema50, ema200 = self.calculate_ema_filter(df)
        allow_long = (trend != 'down')
        allow_short = (trend != 'up')

        margin = atr * 0.2

        balance = self.client.get_usdt_balance()
        ticker = self.client.session.get_tickers(category="linear", symbol=self.symbol)['result']['list'][0]
        price = float(ticker['lastPrice'])

        # ✅ LONG
        if allow_long and (low <= lower + margin and close > prev_close and slope > -0.5 * atr and (35 < rsi < 52 if self.symbol == 'APTUSDT' else 30 < rsi < 52)):
            sl_price = lower - atr * self.sl_mult
            tp_price = linreg + atr * self.tp_mult
            sl_price = min(sl_price, close - atr * max(0.8, self.sl_mult))
            tp_price = max(tp_price, close + atr * max(0.5, self.tp_mult))
            sl_dist_usd = close - sl_price
            qty = self.get_position_size(sl_dist_usd, balance, price)
            if qty > 0:
                send_telegram(
                    f"*🔄 LONG on {self.symbol}*\n"
                    f"Price: {close:.5f} | Lower: {lower:.5f}\n"
                    f"Slope: {slope:+.6f} | RSI: {rsi:.1f}\n"
                    f"Trend: {trend} | ATR%: {effective_vol:.2f}%\n"
                    f"TP: {tp_price:.5f} | SL: {sl_price:.5f}"
                )
                self.client.place_order(self.symbol, "Buy", qty, tp_price=f"{tp_price:.8f}", sl_price=f"{sl_price:.8f}")
                self.last_signal_time = now

        # ✅ SHORT
        elif allow_short and (high >= upper - margin and close < prev_close and slope < 0.5 * atr and (48 < rsi < 65 if self.symbol == 'APTUSDT' else 48 < rsi < 70)):
            sl_price = upper + atr * self.sl_mult
            tp_price = linreg - atr * self.tp_mult
            sl_price = max(sl_price, close + atr * max(0.8, self.sl_mult))
            tp_price = min(tp_price, close - atr * max(0.5, self.tp_mult))
            sl_dist_usd = sl_price - close
            qty = self.get_position_size(sl_dist_usd, balance, price)
            if qty > 0:
                send_telegram(
                    f"*🔄 SHORT on {self.symbol}*\n"
                    f"Price: {close:.5f} | Upper: {upper:.5f}\n"
                    f"Slope: {slope:+.6f} | RSI: {rsi:.1f}\n"
                    f"Trend: {trend} | ATR%: {effective_vol:.2f}%\n"
                    f"TP: {tp_price:.5f} | SL: {sl_price:.5f}"
                )
                self.client.place_order(self.symbol, "Sell", qty, tp_price=f"{tp_price:.8f}", sl_price=f"{sl_price:.8f}")
                self.last_signal_time = now

        self.monitor.maybe_send_hourly_report(now)

    def stop(self):
        self.stop_event.set()