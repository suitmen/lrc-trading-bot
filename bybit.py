# exchanges/bybit.py
from pybit.unified_trading import HTTP
import pandas as pd

class BybitClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool):
        print(f"session strated: {api_key} {api_secret} {testnet}")
        self.session = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)

    def get_klines(self, symbol: str, interval='5', limit=250):
        print(f"get_klines {symbol}")
        klines = self.session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(
            klines['result']['list'],
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover']
        )
        df = df[['open', 'high', 'low', 'close', 'volume']].astype(float)
        return df.iloc[::-1].reset_index(drop=True)

    def get_position(self, symbol: str):
        positions = self.session.get_positions(category="linear", symbol=symbol)
        pos_list = positions['result']['list']
        if pos_list and float(pos_list[0]['size']) > 0:
            return {'side': pos_list[0]['side'], 'size': float(pos_list[0]['size'])}
        return None

    def get_usdt_balance(self):
        resp = self.session.get_wallet_balance(accountType="UNIFIED")
        coins = resp['result']['list'][0]['coin']
        usdt = next((c for c in coins if c['coin'] == 'USDT'), None)
        return float(usdt['equity']) if usdt else 1000.0

    def place_order(self, symbol: str, side: str, qty: float, tp_price=None, sl_price=None):
        return self.session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            takeProfit=tp_price,
            stopLoss=sl_price,
            reduceOnly=False
        )

    def get_closed_pnl(self, symbol: str, limit=1):
        return self.session.get_closed_pnl(category="linear", symbol=symbol, limit=limit)