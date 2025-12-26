import os
import time
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from pybit.unified_trading import HTTP
import requests
import talib
from datetime import datetime
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

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
        self.tp_ratio = 2.0
        self.position = None
        
    def get_klines(self, interval='5', limit=100):
        """Получить OHLCV данные"""
        klines = self.session.get_kline(
            category="linear",
            symbol=self.symbol,
            interval=interval,
            limit=limit
        )
#        print(f"** {klines['result']['list']}")

        df = pd.DataFrame(klines['result']['list'], 
                         columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 
                                'turnover'])
        df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        df = df.iloc[::-1].reset_index(drop=True)  # Новые сверху
        return df
    
    def calculate_lrc(self, highs, lows, closes, period):
        """Вычисление Linear Regression Channel"""
        # Линейная регрессия для close
        linreg = talib.LINEARREG(closes, timeperiod=period)
        linreg_slope = talib.LINEARREG_SLOPE(closes, timeperiod=period)
        
        # Стандартное отклонение
        std = talib.STDDEV(closes, timeperiod=period)
        
        upper = linreg + (std * self.dev_mult)
        lower = linreg - (std * self.dev_mult)
        
        return linreg.iloc[-1], upper.iloc[-1], lower.iloc[-1], linreg_slope.iloc[-1]
    
    def calculate_rsi(self, closes):
        """RSI для фильтра"""
        return talib.RSI(closes, timeperiod=self.rsi_period).iloc[-1]

    def get_usdt_balance(self):
        resp = self.session.get_wallet_balance(accountType="UNIFIED")
        coins = resp['result']['list'][0]['coin']  # список монет
        usdt = next((c for c in coins if c['coin'] == 'USDT'), None)
        if usdt:
            # Используем доступный баланс или equity по USDT
           return float(usdt['equity'])  # или usdt['availableToWithdraw']
        return 0.0

    
    def get_position_size(self):
        """Размер позиции по риску"""
        balance = self.get_usdt_balance()
        balance = float(balance) if balance else 1000
        risk_amount = balance * self.risk_per_trade
        ticker = self.session.get_tickers(category="linear", symbol=self.symbol)['result']['list'][0]
        price = float(ticker['lastPrice'])
        atr = self.calculate_atr()
        sl_distance = atr * 1.5
        qty = (risk_amount / sl_distance) / price
        return round(qty, 3)
    
    def calculate_atr(self):
        """ATR для стопов"""
        df = self.get_klines(limit=50)
        return talib.ATR(df['high'], df['low'], df['close'], timeperiod=14).iloc[-1]
    
    def get_position(self):
        """Текущая позиция"""
        positions = self.session.get_positions(category="linear", symbol=self.symbol)

        pos = positions['result']['list']
        if pos and float(pos[0]['size']) > 0:
            return {
                'side': pos[0]['side'],
                'size': float(pos[0]['size']),
                'entryPrice': float(pos[0]['avgPrice'])
            }
        return None
    
    def place_order(self, side, qty, tp_price=None, sl_price=None):
        """Разместить ордер с TP/SL"""
        order = self.session.place_order(
            category="linear",
            symbol=self.symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            takeProfit=tp_price,
            stopLoss=sl_price
        )
        logger.info(f"Order placed: {side} {qty} {self.symbol} TP:{tp_price} SL:{sl_price}")
        return order
    
    def check_signals(self):
        """Основная логика сигналов LRC 5m"""
        df = self.get_klines(limit=self.lrc_period + 10)
        if len(df) < self.lrc_period:
            return
            
        close = df['close'].iloc[-1]
        prev_close = df['close'].iloc[-2]
        high = df['high'].iloc[-1]
        low = df['low'].iloc[-1]
        
        # LRC
        linreg, upper, lower, slope = self.calculate_lrc(
            df['high'], df['low'], df['close'], self.lrc_period
        )
        
        rsi = self.calculate_rsi(df['close'])
        volume = df['volume'].iloc[-1]
        avg_volume = df['volume'].rolling(20).mean().iloc[-1]
        
        self.position = self.get_position()

        print(f"{linreg} {upper} {lower} {close}")

     
        # Long сигнал: пробой верхней границы
        if (not self.position and 
            prev_close <= upper and close > upper and 
            slope > 0 and rsi > 50 and volume > avg_volume * 1.2):
 
            send_telegram(f"*LONG signal detected on {self.symbol}*\nPrice: {close}\nTime: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")         
            qty = self.get_position_size()
            atr = self.calculate_atr()
            sl_price = lower - atr * 0.5
            tp_price = close + (close - sl_price) * self.tp_ratio
            
            self.place_order("Buy", qty, tp_price, sl_price)
            
        # Short сигнал: пробой нижней границы
        elif (not self.position and 
              prev_close >= lower and close < lower and 
              slope < 0 and rsi < 50 and volume > avg_volume * 1.2):

            send_telegram(f"*SHORT signal detected on {self.symbol}*\nPrice: {close}\nTime: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")         
            qty = self.get_position_size()
            atr = self.calculate_atr()
            sl_price = upper + atr * 0.5
            tp_price = close - (sl_price - close) * self.tp_ratio
            
            self.place_order("Sell", qty, tp_price, sl_price)
    
    def run(self):
        """Главный цикл"""
        logger.info(f"Starting LRC 5m bot for {self.symbol}")
        while True:
            try:
                self.check_signals()
                time.sleep(30)  # Проверка каждые 30 сек
            except Exception as e:
                logger.error(f"Error: {e}")
                time.sleep(60)



def send_telegram(text: str):
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


if __name__ == "__main__":
    # Замените на свои ключи Bybit

    API_KEY = os.getenv('BYBIT_API_KEY')
    API_SECRET = os.getenv('BYBIT_API_SECRET')
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

#    print(f"BYBIT_API_KEY {API_KEY}")
    
    if not API_KEY or not API_SECRET:
        raise ValueError("Set BYBIT_API_KEY and BYBIT_SECRET_KEY environment variables")
    
    bot = LRCBybitBot(
        api_key=API_KEY,
        api_secret=API_SECRET,
        symbol='SOLUSDT',
        testnet=False  # testnet=True для тестов
    )
    bot.run()
