# -*- coding: utf-8 -*-
import os
import asyncio
import json
import logging
import aiohttp
import time
import numpy as np
from datetime import datetime, timedelta, UTC
from collections import deque
from telegram import Bot, ParseMode, ReplyKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# =============================================================================
# --- الإعدادات الرئيسية ---
# =============================================================================

# --- Telegram Configuration ---
# Note: It's highly recommended to use environment variables for security.
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# --- Exchange API Keys (للتطوير المستقبلي) ---
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '')
BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '')

# --- إعدادات رادار الحيتان ---
WHALE_GEM_MAX_PRICE = 0.50
WHALE_GEM_MIN_VOLUME_24H = 100000
WHALE_GEM_MAX_VOLUME_24H = 5000000
WHALE_WALL_THRESHOLD_USDT = 25000
WHALE_PRESSURE_RATIO = 3.0
WHALE_SCAN_CANDIDATE_LIMIT = 50

# --- إعدادات كاشف الزخم ---
MOMENTUM_MAX_PRICE = 0.10
MOMENTUM_MIN_VOLUME_24H = 50000
MOMENTUM_MAX_VOLUME_24H = 2000000
MOMENTUM_VOLUME_INCREASE = 1.8
MOMENTUM_PRICE_INCREASE = 4.0
MOMENTUM_KLINE_INTERVAL = '5m'
MOMENTUM_KLINE_LIMIT = 12
MOMENTUM_LOSS_THRESHOLD_PERCENT = -5.0

# --- إعدادات التوصيات الآلية ---
RECOMMENDATION_KLINE_INTERVAL = '5m'
RECOMMENDATION_KLINE_LIMIT = 20
RECOMMENDATION_TAKE_PROFIT_PERCENT = 7.0
RECOMMENDATION_STOP_LOSS_PERCENT = -3.5

# --- إعدادات المهام الدورية ---
RUN_FOMO_SCAN_EVERY_MINUTES = 15
RUN_LISTING_SCAN_EVERY_SECONDS = 60
RUN_PERFORMANCE_TRACKER_EVERY_MINUTES = 5
PERFORMANCE_TRACKING_DURATION_HOURS = 24
MARKET_MOVERS_MIN_VOLUME = 50000 # حد أدنى للفوليوم لفلترة النتائج

# --- إعدادات عامة ---
HTTP_TIMEOUT = 15
API_CONCURRENCY_LIMIT = 8

# --- إعدادات تسجيل الأخطاء ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة وتهيئة البوت ---
# =============================================================================
bot = Bot(token=TELEGRAM_BOT_TOKEN)
api_semaphore = asyncio.Semaphore(API_CONCURRENCY_LIMIT)

# --- إدارة الحالة متعددة المنصات ---
PLATFORMS = ["MEXC", "Gate.io", "Binance", "Bybit", "KuCoin", "OKX"]
performance_tracker = {p: {} for p in PLATFORMS}
active_hunts = {p: {} for p in PLATFORMS}
known_symbols = {p: set() for p in PLATFORMS}
background_tasks = {}
recently_alerted_fomo = {p: {} for p in PLATFORMS}

# =============================================================================
# --- قسم الشبكة والوظائف الأساسية (مشتركة) ---
# =============================================================================
async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None, headers: dict = None, retries: int = 3):
    """
    جلب البيانات من واجهة برمجة التطبيقات مع معالجة الأخطاء وإعادة المحاولة.
    """
    request_headers = {'User-Agent': 'Mozilla/5.0'}
    if headers: request_headers.update(headers)
    
    for attempt in range(retries):
        try:
            async with session.get(url, params=params, timeout=HTTP_TIMEOUT, headers=request_headers) as response:
                if response.status == 429:
                    logger.warning(f"Rate limit hit (429) for {url}. Retrying after {2 ** attempt}s...")
                    await asyncio.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                return await response.json()
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            if attempt >= retries - 1: return None
            await asyncio.sleep(1)
    return None

def format_price(price_str):
    """تنسيق السعر لعرضه بشكل أوضح."""
    try: return f"{float(price_str):.8g}"
    except (ValueError, TypeError): return price_str

# =============================================================================
# --- ⚙️ قسم عملاء المنصات (Exchange Clients) ⚙️ ---
# =============================================================================
class BaseExchangeClient:
    def __init__(self, session, api_key=None, api_secret=None):
        self.session = session
        self.name = "Base"
        self.api_key = api_key
        self.api_secret = api_secret
    async def get_market_data(self): raise NotImplementedError
    async def get_klines(self, symbol, interval, limit): raise NotImplementedError
    async def get_order_book(self, symbol, limit=20): raise NotImplementedError
    async def get_current_price(self, symbol): raise NotImplementedError

class MexcClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name = "MEXC"
        self.base_api_url = "https://api.mexc.com"

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        return [{
            'symbol': item['symbol'],
            'quoteVolume': item.get('quoteVolume', '0'),
            'lastPrice': item.get('lastPrice', '0'),
            'priceChangePercent': float(item.get('priceChangePercent', '0')) * 100
        } for item in data if item.get('symbol','').endswith("USDT")]

    async def get_klines(self, symbol, interval, limit):
        async with api_semaphore:
            params = {'symbol': symbol, 'interval': interval, 'limit': limit}
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/klines", params=params)
            return [[item[0], item[1], item[2], item[3], item[4], item[5]] for item in data] if data else None

    async def get_order_book(self, symbol, limit=20):
        async with api_semaphore:
            params = {'symbol': symbol, 'limit': limit}
            await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/api/v3/depth", params)

    async def get_current_price(self, symbol: str) -> float | None:
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/price", {'symbol': symbol})
        return float(data['price']) if data and 'price' in data else None

class GateioClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name = "Gate.io"
        self.base_api_url = "https://api.gateio.ws/api/v4"

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/spot/tickers")
        if not data: return []
        return [{
            'symbol': item['currency_pair'].replace('_', ''),
            'quoteVolume': item.get('quote_volume', '0'),
            'lastPrice': item.get('last', '0'),
            'priceChangePercent': float(item.get('change_percentage', '0'))
        } for item in data if item.get('currency_pair', '').endswith("_USDT")]

    async def get_klines(self, symbol, interval, limit):
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"
        async with api_semaphore:
            params = {'currency_pair': gateio_symbol, 'interval': interval, 'limit': limit}
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/spot/candlesticks", params=params)
            if not data: return None
            return [[int(k[0])*1000, k[5], k[3], k[4], k[2], k[1]] for k in data]

    async def get_order_book(self, symbol, limit=20):
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"
        async with api_semaphore:
            params = {'currency_pair': gateio_symbol, 'limit': limit}
            await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/spot/order_book", params)

    async def get_current_price(self, symbol: str) -> float | None:
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"
        data = await fetch_json(self.session, f"{self.base_api_url}/spot/tickers", {'currency_pair': gateio_symbol})
        return float(data[0]['last']) if data and isinstance(data, list) and len(data) > 0 and 'last' in data[0] else None

class BinanceClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name = "Binance"
        self.base_api_url = "https://api.binance.com"

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        return [{
            'symbol': item['symbol'],
            'quoteVolume': item.get('quoteVolume', '0'),
            'lastPrice': item.get('lastPrice', '0'),
            'priceChangePercent': float(item.get('priceChangePercent', '0'))
        } for item in data if item.get('symbol','').endswith("USDT")]

    async def get_klines(self, symbol, interval, limit):
        async with api_semaphore:
            params = {'symbol': symbol, 'interval': interval, 'limit': limit}
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/klines", params=params)
            return [[item[0], item[1], item[2], item[3], item[4], item[5]] for item in data] if data else None

    async def get_order_book(self, symbol, limit=20):
        async with api_semaphore:
            params = {'symbol': symbol, 'limit': limit}
            await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/api/v3/depth", params)

    async def get_current_price(self, symbol: str) -> float | None:
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/price", {'symbol': symbol})
        return float(data['price']) if data and 'price' in data else None

class BybitClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name = "Bybit"
        self.base_api_url = "https://api.bybit.com"

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/tickers", params={'category': 'spot'})
        if not data or not data.get('result') or not data['result'].get('list'): return []
        return [{
            'symbol': item['symbol'],
            'quoteVolume': item.get('turnover24h', '0'),
            'lastPrice': item.get('lastPrice', '0'),
            'priceChangePercent': float(item.get('price24hPcnt', '0')) * 100
        } for item in data['result']['list'] if item['symbol'].endswith("USDT")]

    async def get_klines(self, symbol, interval, limit):
        async with api_semaphore:
            bybit_interval_map = {'1m': '1', '5m': '5', '15m': '15', '30m': '30', '1h': '60', '4h': '240', '1d': 'D'}
            bybit_interval = bybit_interval_map.get(interval, '5')
            params = {'category': 'spot', 'symbol': symbol, 'interval': bybit_interval, 'limit': limit}
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/kline", params=params)
            if not data or not data.get('result') or not data['result'].get('list'): return None
            
            # <-- FIX: Bybit returns klines in descending order (newest first). We must reverse it.
            klines_list = data['result']['list']
            klines_list.reverse()
            
            return [[int(k[0]), k[1], k[2], k[3], k[4], k[5]] for k in klines_list]

    async def get_order_book(self, symbol, limit=20):
        async with api_semaphore:
            params = {'category': 'spot', 'symbol': symbol, 'limit': limit}
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/orderbook", params)
            if not data or not data.get('result'): return None
            return {
                'bids': data['result'].get('b', []),
                'asks': data['result'].get('a', [])
            }

    async def get_current_price(self, symbol: str) -> float | None:
        params = {'category': 'spot', 'symbol': symbol}
        data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/tickers", params=params)
        if data and data.get('result') and data['result'].get('list'):
            return float(data['result']['list'][0]['lastPrice'])
        return None

class KucoinClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name = "KuCoin"
        self.base_api_url = "https://api.kucoin.com"

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/allTickers")
        if not data or not data.get('data') or not data['data'].get('ticker'): return []
        return [{
            'symbol': item['symbol'].replace('-', ''), # Normalize symbol
            'quoteVolume': item.get('volValue', '0'),
            'lastPrice': item.get('last', '0'),
            'priceChangePercent': float(item.get('changeRate', '0')) * 100
        } for item in data['data']['ticker'] if item.get('symbol','').endswith("-USDT")]

    async def get_klines(self, symbol, interval, limit):
        # KuCoin uses hyphenated symbols for API calls
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        kucoin_interval = {'5m': '5min', '15m': '15min'}.get(interval, '5min')
        async with api_semaphore:
            params = {'symbol': kucoin_symbol, 'type': kucoin_interval, 'limit': limit}
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/candles", params=params)
            if not data or not data.get('data'): return None
            # Data is returned as: [time, open, close, high, low, volume, turnover]
            # Re-arrange to standard: [time, open, high, low, close, volume]
            return [[int(k[0])*1000, k[1], k[3], k[4], k[2], k[5]] for k in data['data']]

    async def get_order_book(self, symbol, limit=20):
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        async with api_semaphore:
            params = {'symbol': kucoin_symbol}
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/orderbook/level2_20", params)
            return data.get('data') if data else None

    async def get_current_price(self, symbol: str) -> float | None:
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/orderbook/level1", {'symbol': kucoin_symbol})
        if not data or not data.get('data'): return None
        return float(data['data']['price'])

class OkxClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name = "OKX"
        self.base_api_url = "https://www.okx.com"

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/tickers", params={'instType': 'SPOT'})
        if not data or not data.get('data'): return []
        
        results = []
        for item in data['data']:
            # <-- FIX: OKX uses hyphenated symbols like "BTC-USDT". Filter correctly.
            if not item.get('instId','').endswith("-USDT"):
                continue
            
            try:
                # <-- IMPROVEMENT: Calculate 24h change more accurately
                open_24h = float(item.get('open24h', '0'))
                last_price = float(item.get('last', '0'))
                change_percent = ((last_price - open_24h) / open_24h) * 100 if open_24h > 0 else 0
                
                results.append({
                    # <-- FIX: Normalize symbol by removing hyphen for consistency
                    'symbol': item['instId'].replace('-', ''),
                    'quoteVolume': item.get('volCcy24h', '0'),
                    'lastPrice': last_price,
                    'priceChangePercent': change_percent
                })
            except (ValueError, TypeError):
                continue # Skip if data is invalid
        return results

    async def get_klines(self, symbol, interval, limit):
        # OKX uses hyphenated symbols for API calls
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        okx_interval = {'5m': '5m', '15m': '15m'}.get(interval, '5m')
        async with api_semaphore:
            await asyncio.sleep(0.2)
            params = {'instId': okx_symbol, 'bar': okx_interval, 'limit': limit}
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/candles", params=params)
            if not data or not data.get('data'): return None
            
            # <-- FIX: OKX returns klines in descending order (newest first). We must reverse it.
            klines_list = data['data']
            klines_list.reverse()
            
            return [[int(k[0]), k[1], k[2], k[3], k[4], k[5]] for k in klines_list]

    async def get_order_book(self, symbol, limit=20):
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        async with api_semaphore:
            await asyncio.sleep(0.2)
            params = {'instId': okx_symbol, 'sz': limit}
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/books", params)
            if not data or not data.get('data'): return None
            return data['data'][0]

    async def get_current_price(self, symbol: str) -> float | None:
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        params = {'instId': okx_symbol}
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/ticker", params=params)
        if not data or not data.get('data'): return None
        return float(data['data'][0]['last'])

def get_exchange_client(exchange_name, session):
    clients = {
        'mexc': MexcClient,
        'gate.io': GateioClient,
        'binance': BinanceClient,
        'bybit': BybitClient,
        'kucoin': KucoinClient,
        'okx': OkxClient
    }
    return clients.get(exchange_name.lower())(session)

# =============================================================================
# --- الوظائف المساعدة للتحليل ---
# =============================================================================
async def helper_get_momentum_symbols(client: BaseExchangeClient):
    """يحدد العملات التي تشهد زخماً عالياً بناءً على تغيرات السعر والحجم."""
    market_data = await client.get_market_data()
    if not market_data:
        logger.warning(f"Momentum ({client.name}): No market data received from API.")
        return {}
    
    potential_coins = [p for p in market_data if float(p.get('lastPrice','1')) <= MOMENTUM_MAX_PRICE and MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= MOMENTUM_MAX_VOLUME_24H]
    
    if not potential_coins:
        logger.info(f"Momentum ({client.name}): Found {len(market_data)} coins, but 0 met the price/volume criteria.")
        return {}

    logger.info(f"Momentum ({client.name}): Analyzing {len(potential_coins)} potential coins.")
    tasks = [client.get_klines(p['symbol'], MOMENTUM_KLINE_INTERVAL, MOMENTUM_KLINE_LIMIT) for p in potential_coins]
    all_klines_data = await asyncio.gather(*tasks)
    
    momentum_coins_data = {}
    for i, klines in enumerate(all_klines_data):
        if not klines or len(klines) < MOMENTUM_KLINE_LIMIT: continue
        try:
            sp = MOMENTUM_KLINE_LIMIT // 2
            old_v = sum(float(k[5]) for k in klines[:sp]); new_v = sum(float(k[5]) for k in klines[sp:])
            start_p = float(klines[sp][1]) # Use open price of first candle in second half
            if old_v == 0 or start_p == 0: continue
            
            end_p = float(klines[-1][4]) # Use close price of the last candle
            price_change = ((end_p - start_p) / start_p) * 100
            
            if new_v > old_v * MOMENTUM_VOLUME_INCREASE and price_change > MOMENTUM_PRICE_INCREASE:
                coin_symbol = potential_coins[i]['symbol']
                momentum_coins_data[coin_symbol] = {'symbol': coin_symbol, 'price_change': price_change, 'current_price': end_p, 'peak_volume': new_v}
        except (ValueError, IndexError, TypeError) as e:
            logger.error(f"Error analyzing klines for {potential_coins[i]['symbol']} on {client.name}: {e}")
            continue
    return momentum_coins_data

async def helper_get_whale_activity(client: BaseExchangeClient):
    """يحدد العملات التي تشهد نشاط حيتان بناءً على حوائط الأوامر والضغط."""
    market_data = await client.get_market_data()
    if not market_data: 
        logger.warning(f"Whale ({client.name}): No market data received from API.")
        return {}

    potential_gems = [p for p in market_data if float(p.get('lastPrice','999')) <= WHALE_GEM_MAX_PRICE and WHALE_GEM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= WHALE_GEM_MAX_VOLUME_24H]
    
    if not potential_gems: 
        logger.info(f"Whale ({client.name}): Found {len(market_data)} coins, but 0 met the price/volume criteria.")
        return {}
    
    logger.info(f"Whale ({client.name}): Analyzing {len(potential_gems)} potential gems.")
    for p in potential_gems: p['change_float'] = p.get('priceChangePercent', 0)
    
    top_gems = sorted(potential_gems, key=lambda x: x['change_float'], reverse=True)[:WHALE_SCAN_CANDIDATE_LIMIT]
    tasks = [client.get_order_book(p['symbol']) for p in top_gems]
    all_order_books = await asyncio.gather(*tasks)
    
    whale_signals_by_symbol = {}
    for i, book in enumerate(all_order_books):
        symbol = top_gems[i]['symbol']
        signals = await analyze_order_book_for_whales(book, symbol)
        if signals:
            if symbol not in whale_signals_by_symbol: whale_signals_by_symbol[symbol] = []
            for signal in signals:
                signal['symbol'] = symbol
                whale_signals_by_symbol[symbol].append(signal)
    return whale_signals_by_symbol

async def analyze_order_book_for_whales(book, symbol):
    """يحلل دفتر الأوامر للكشف عن نشاط الحيتان."""
    signals = []
    if not book or not book.get('bids') or not book.get('asks'): return signals
    try:
        # Data from different exchanges can have price/qty as strings or floats
        bids = sorted([(float(p), float(q)) for p, q, *_ in book['bids']], key=lambda x: x[0], reverse=True)
        asks = sorted([(float(p), float(q)) for p, q, *_ in book['asks']], key=lambda x: x[0])
        
        for price, qty in bids[:5]:
            value = price * qty
            if value >= WHALE_WALL_THRESHOLD_USDT:
                signals.append({'type': 'Buy Wall', 'value': value, 'price': price}); break
        for price, qty in asks[:5]:
            value = price * qty
            if value >= WHALE_WALL_THRESHOLD_USDT:
                signals.append({'type': 'Sell Wall', 'value': value, 'price': price}); break
        
        bids_value = sum(p * q for p, q in bids[:10])
        asks_value = sum(p * q for p, q in asks[:10])
        
        if asks_value > 0 and (bids_value / asks_value) >= WHALE_PRESSURE_RATIO:
            signals.append({'type': 'Buy Pressure', 'value': bids_value / asks_value})
        elif bids_value > 0 and (asks_value / bids_value) >= WHALE_PRESSURE_RATIO:
            signals.append({'type': 'Sell Pressure', 'value': asks_value / bids_value})
    except Exception as e:
        logger.warning(f"Could not analyze order book for {symbol}: {e}")
    return signals

# =============================================================================
# --- 4. الوظائف التفاعلية (أوامر البوت) ---
# =============================================================================
BTN_WHALE_RADAR = "🐋 رادار الحيتان"
BTN_MOMENTUM = "🚀 كاشف الزخم"
BTN_RECOMMENDATIONS = "💡 توصيات آلية"
BTN_STATUS = "📊 الحالة"
BTN_PERFORMANCE = "📈 تقرير الأداء"
BTN_CROSS_ANALYSIS = "💪 تحليل متقاطع"
BTN_TOP_GAINERS = "📈 الأعلى ربحاً"
BTN_TOP_LOSERS = "📉 الأعلى خسارة"
BTN_TOP_VOLUME = "💰 الأعلى تداولاً"
BTN_SELECT_MEXC = "MEXC"
BTN_SELECT_GATEIO = "Gate.io"
BTN_SELECT_BINANCE = "Binance"
BTN_SELECT_BYBIT = "Bybit"
BTN_SELECT_KUCOIN = "KuCoin"
BTN_SELECT_OKX = "OKX"
BTN_TASKS_ON = "🔴 إيقاف المهام"
BTN_TASKS_OFF = "🟢 تفعيل المهام"

def build_menu(context: CallbackContext):
    """يبني لوحة التحكم التفاعلية للبوت."""
    user_data = context.user_data
    bot_data = context.bot_data
    selected_exchange = user_data.get('exchange', 'mexc')
    tasks_enabled = bot_data.get('background_tasks_enabled', True)
    
    mexc_btn = f"✅ {BTN_SELECT_MEXC}" if selected_exchange == 'mexc' else BTN_SELECT_MEXC
    gate_btn = f"✅ {BTN_SELECT_GATEIO}" if selected_exchange == 'gate.io' else BTN_SELECT_GATEIO
    binance_btn = f"✅ {BTN_SELECT_BINANCE}" if selected_exchange == 'binance' else BTN_SELECT_BINANCE
    bybit_btn = f"✅ {BTN_SELECT_BYBIT}" if selected_exchange == 'bybit' else BTN_SELECT_BYBIT
    kucoin_btn = f"✅ {BTN_SELECT_KUCOIN}" if selected_exchange == 'kucoin' else BTN_SELECT_KUCOIN
    okx_btn = f"✅ {BTN_SELECT_OKX}" if selected_exchange == 'okx' else BTN_SELECT_OKX
    toggle_tasks_btn = BTN_TASKS_ON if tasks_enabled else BTN_TASKS_OFF

    keyboard = [
        [BTN_MOMENTUM, BTN_WHALE_RADAR, BTN_RECOMMENDATIONS],
        [BTN_TOP_GAINERS, BTN_TOP_VOLUME, BTN_TOP_LOSERS],
        [BTN_CROSS_ANALYSIS, BTN_PERFORMANCE, BTN_STATUS],
        [mexc_btn, gate_btn, binance_btn],
        [bybit_btn, kucoin_btn, okx_btn, toggle_tasks_btn]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def start_command(update: Update, context: CallbackContext):
    """الرد على أمر /start وإظهار القائمة الرئيسية."""
    context.user_data['exchange'] = 'mexc'
    context.bot_data.setdefault('background_tasks_enabled', True)
    welcome_message = (
        "✅ **بوت التداول الذكي (v15 - Fixed) جاهز!**\n\n"
        "**ما الجديد؟**\n"
        "- **إصلاح شامل:** تم إصلاح مشكلة عدم عمل رادار الحيتان وكاشف الزخم لمنصتي **OKX** و **Bybit**.\n"
        "- **تحسين دقة البيانات** لجميع المنصات.\n\n"
        "المنصة الحالية: **MEXC**")
    if update.message:
        update.message.reply_text(welcome_message, reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

def set_exchange(update: Update, context: CallbackContext, exchange_name: str):
    """يغير المنصة الحالية التي يعمل عليها البوت."""
    context.user_data['exchange'] = exchange_name.lower()
    update.message.reply_text(f"✅ تم تحويل المنصة بنجاح. المنصة النشطة الآن هي: **{exchange_name}**",
                              reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

def toggle_background_tasks(update: Update, context: CallbackContext):
    """يوقف أو يشغل المهام الخلفية للبوت."""
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    context.bot_data['background_tasks_enabled'] = not tasks_enabled
    status = "تفعيل" if not tasks_enabled else "إيقاف"
    update.message.reply_text(f"✅ تم **{status}** المهام الخلفية.",
                              reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

def status_command(update: Update, context: CallbackContext):
    """يعرض حالة البوت والمهام النشطة."""
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    message = "📊 **حالة البوت** 📊\n\n"
    message += f"**1. المهام الخلفية:** {'🟢 نشطة' if tasks_enabled else '🔴 متوقفة'}\n\n"
    for platform in PLATFORMS:
        perf_count = len(performance_tracker.get(platform, {}))
        message += f"**منصة {platform}:**\n"
        message += f"   - 📈 الأداء المتتبع: {perf_count}\n\n"
    update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update: Update, context: CallbackContext):
    """يعالج ضغط الأزرار المختلفة ويوجهها للوظائف المناسبة."""
    if not update.message or not update.message.text: return
    button_text = update.message.text.strip().replace("✅ ", "")
    chat_id = update.message.chat_id
    loop = context.bot_data['loop']
    session = context.bot_data['session']

    if button_text in [BTN_SELECT_MEXC, BTN_SELECT_GATEIO, BTN_SELECT_BINANCE, BTN_SELECT_BYBIT, BTN_SELECT_KUCOIN, BTN_SELECT_OKX]:
        set_exchange(update, context, button_text); return
    if button_text in [BTN_TASKS_ON, BTN_TASKS_OFF]:
        toggle_background_tasks(update, context); return
    if button_text == BTN_STATUS:
        status_command(update, context); return
        
    current_exchange = context.user_data.get('exchange', 'mexc')
    client = get_exchange_client(current_exchange, session)
    sent_message = context.bot.send_message(chat_id=chat_id, text=f"🔍 جارِ تنفيذ طلبك على منصة {client.name}...")
    
    task = None
    if button_text == BTN_MOMENTUM: task = run_momentum_detector(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_WHALE_RADAR: task = run_whale_radar_scan(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_CROSS_ANALYSIS: task = run_cross_analysis(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_PERFORMANCE: task = get_performance_report(context, chat_id, sent_message.message_id)
    elif button_text == BTN_RECOMMENDATIONS: task = run_automated_recommendations(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_TOP_GAINERS: task = run_top_gainers(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_TOP_LOSERS: task = run_top_losers(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_TOP_VOLUME: task = run_top_volume(context, chat_id, sent_message.message_id, client)

    if task: asyncio.run_coroutine_threadsafe(task, loop)

async def run_momentum_detector(context, chat_id, message_id, client: BaseExchangeClient):
    """يبدأ فحص الزخم اليدوي."""
    initial_text = f"🚀 **كاشف الزخم ({client.name})**\n\n🔍 جارِ الفحص المنظم للسوق..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    
    momentum_coins_data = await helper_get_momentum_symbols(client)
    
    if not momentum_coins_data:
        # <-- IMPROVEMENT: More descriptive message
        msg = f"✅ **الفحص على {client.name} اكتمل:**\n\nلم يتم العثور على عملات تتوافق مع معايير السعر والحجم المحددة حالياً."
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=msg); return

    sorted_coins = sorted(momentum_coins_data.values(), key=lambda x: x['price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم ({client.name}) - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\n"
    for i, coin in enumerate(sorted_coins[:10]):
        message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n   - السعر: `${format_price(coin['current_price'])}`\n   - **زخم آخر 30 دقيقة: `%{coin['price_change']:+.2f}`**\n\n")
    message += "*(تمت إضافة هذه العملات إلى متتبع الأداء.)*"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    
    now = datetime.now(UTC)
    for coin in sorted_coins[:10]:
        add_to_monitoring(coin['symbol'], float(coin['current_price']), coin.get('peak_volume', 0), now, f"الزخم ({client.name})", client.name)

async def run_whale_radar_scan(context, chat_id, message_id, client: BaseExchangeClient):
    """يبدأ فحص نشاط الحيتان اليدوي."""
    initial_text = f"🐋 **رادار الحيتان ({client.name})**\n\n🔍 جارِ الفحص العميق..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    
    whale_signals_by_symbol = await helper_get_whale_activity(client)
    
    if not whale_signals_by_symbol:
        # <-- IMPROVEMENT: More descriptive message
        msg = f"✅ **فحص الرادار على {client.name} اكتمل:**\n\nلم يتم العثور على عملات تظهر نشاط حيتان واضح ضمن معايير البحث."
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=msg); return

    all_signals = [signal for signals_list in whale_signals_by_symbol.values() for signal in signals_list]
    sorted_signals = sorted(all_signals, key=lambda x: x.get('value', 0), reverse=True)
    message = f"🐋 **تقرير رادار الحيتان ({client.name}) - {datetime.now().strftime('%H:%M:%S')}** 🐋\n\n"
    for signal in sorted_signals:
        symbol_name = signal['symbol'].replace('USDT', '')
        if signal['type'] == 'Buy Wall': message += (f"🟢 **حائط شراء ضخم على ${symbol_name}**\n   - **الحجم:** `${signal['value']:,.0f}` USDT\n   - **عند سعر:** `{format_price(signal['price'])}`\n\n")
        elif signal['type'] == 'Sell Wall': message += (f"🔴 **حائط بيع ضخم على ${symbol_name}**\n   - **الحجم:** `${signal['value']:,.0f}` USDT\n   - **عند سعر:** `{format_price(signal['price'])}`\n\n")
        elif signal['type'] == 'Buy Pressure': message += (f"📈 **ضغط شراء عالٍ على ${symbol_name}**\n   - **النسبة:** الشراء يفوق البيع بـ `{signal['value']:.1f}x`\n\n")
        elif signal['type'] == 'Sell Pressure': message += (f"📉 **ضغط بيع عالٍ على ${symbol_name}**\n   - **النسبة:** البيع يفوق الشراء بـ `{signal['value']:.1f}x`\n\n")
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def run_cross_analysis(context, chat_id, message_id, client: BaseExchangeClient):
    """يجمع بين إشارات الزخم والحيتان لتقديم إشارات قوية."""
    initial_text = f"💪 **تحليل متقاطع ({client.name})**\n\n🔍 جارِ إجراء الفحصين بالتوازي..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    try:
        momentum_task = asyncio.create_task(helper_get_momentum_symbols(client))
        whale_task = asyncio.create_task(helper_get_whale_activity(client))
        momentum_coins_data, whale_signals_by_symbol = await asyncio.gather(momentum_task, whale_task)
        
        strong_symbols = set(momentum_coins_data.keys()).intersection(set(whale_signals_by_symbol.keys()))
        
        if not strong_symbols:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **التحليل المتقاطع على {client.name} اكتمل:**\n\nلم يتم العثور على عملات مشتركة حالياً."); return
        
        message = f"💪 **تقرير الإشارات القوية ({client.name}) - {datetime.now().strftime('%H:%M:%S')}** 💪\n\n"
        for symbol in strong_symbols:
            momentum_details = momentum_coins_data[symbol]
            whale_signals = whale_signals_by_symbol[symbol]
            message += f"💎 **${symbol.replace('USDT', '')}** 💎\n"
            message += f"   - **الزخم:** `%{momentum_details['price_change']:+.2f}` في آخر 30 دقيقة.\n"
            whale_info_parts = [f"حائط شراء ({s['value']:,.0f} USDT)" for s in whale_signals if s['type'] == 'Buy Wall'] + \
                               [f"ضغط شراء ({s['value']:.1f}x)" for s in whale_signals if s['type'] == 'Buy Pressure']
            if whale_info_parts: message += f"   - **الحيتان:** " + ", ".join(whale_info_parts) + ".\n\n"
            else: message += f"   - **الحيتان:** تم رصد نشاط.\n\n"
        message += "*(إشارات عالية الجودة تتطلب تحليلك الخاص)*"
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in cross_analysis on {client.name}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ فادح أثناء التحليل المتقاطع.")

async def get_performance_report(context, chat_id, message_id):
    """يقدم تقريراً مفصلاً عن أداء العملات المرصودة."""
    try:
        if not any(performance_tracker.values()):
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="ℹ️ لا توجد عملات قيد تتبع الأداء حالياً.")
            return
        
        message = "📊 **تقرير أداء العملات المرصودة** 📊\n\n"
        
        all_tracked_items = []
        for platform_name, symbols_data in performance_tracker.items():
            for symbol, data in symbols_data.items():
                data_copy = data.copy()
                data_copy['exchange'] = platform_name
                all_tracked_items.append((symbol, data_copy))
        
        sorted_symbols = sorted(all_tracked_items, key=lambda item: item[1]['alert_time'], reverse=True)
        
        for symbol, data in sorted_symbols:
            if data.get('status') == 'Archived': continue
            alert_price = data.get('alert_price', 0)
            current_price = data.get('current_price', alert_price)
            high_price = data.get('high_price', alert_price)
            current_change = ((current_price - alert_price) / alert_price) * 100 if alert_price > 0 else 0
            peak_change = ((high_price - alert_price) / alert_price) * 100 if alert_price > 0 else 0
            emoji = "🟢" if current_change >= 0 else "🔴"
            time_since_alert = datetime.now(UTC) - data['alert_time']
            hours, remainder = divmod(time_since_alert.total_seconds(), 3600)
            minutes, _ = divmod(remainder, 60)
            time_str = f"{int(hours)} س و {int(minutes)} د"
            
            message += (f"{emoji} **${symbol.replace('USDT','')}** ({data.get('exchange', 'N/A')}) (منذ {time_str})\n"
                        f"   - سعر التنبيه: `${format_price(alert_price)}`\n"
                        f"   - السعر الحالي: `${format_price(current_price)}` (**{current_change:+.2f}%**)\n"
                        f"   - أعلى سعر: `${format_price(high_price)}` (**{peak_change:+.2f}%**)\n\n")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_performance_report: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ أثناء جلب تقرير الأداء.")

async def run_automated_recommendations(context, chat_id, message_id, client: BaseExchangeClient):
    """يقدم توصيات تداول آلية بناءً على تحليل الزخم والحيتان."""
    initial_text = f"💡 **التوصيات الآلية ({client.name})**\n\n🔍 جارِ دمج وتحليل الإشارات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    try:
        momentum_task = asyncio.create_task(helper_get_momentum_symbols(client))
        whale_task = asyncio.create_task(helper_get_whale_activity(client))
        momentum_coins, whale_signals = await asyncio.gather(momentum_task, whale_task)

        strong_symbols = set(momentum_coins.keys()).intersection(set(whale_signals.keys()))
        
        if not strong_symbols:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **تحليل التوصيات على {client.name} اكتمل:**\n\nلا توجد فرص قوية تتوافق مع الشروط حالياً."); return
        
        message = f"💡 **أفضل التوصيات الآلية ({client.name})** 💡\n\n"
        
        for symbol in strong_symbols:
            klines = await client.get_klines(symbol, RECOMMENDATION_KLINE_INTERVAL, RECOMMENDATION_KLINE_LIMIT)
            if not klines or len(klines) < 10: continue

            close_prices = np.array([float(k[4]) for k in klines])
            current_price = close_prices[-1]
            
            entry_zone_avg = np.mean(close_prices[-5:])
            entry_price_low = entry_zone_avg * 0.995
            entry_price_high = entry_zone_avg * 1.005

            take_profit = entry_zone_avg * (1 + RECOMMENDATION_TAKE_PROFIT_PERCENT / 100)
            stop_loss = entry_zone_avg * (1 + RECOMMENDATION_STOP_LOSS_PERCENT / 100)
            
            message += (
                f"💎 **${symbol.replace('USDT','')}**\n"
                f"   - **الدخول:** بين `{format_price(entry_price_low)}` - `{format_price(entry_price_high)}`\n"
                f"   - **الهدف 🎯:** `{format_price(take_profit)}` (+{RECOMMENDATION_TAKE_PROFIT_PERCENT}%)\n"
                f"   - **الوقف 🛡️:** `{format_price(stop_loss)}` ({RECOMMENDATION_STOP_LOSS_PERCENT}%)\n"
                f"   - _السعر الحالي: {format_price(current_price)}_\n\n"
            )

        message += "--- \n**إخلاء مسؤولية:** هذه التوصيات تم توليدها آلياً بناءً على بيانات السوق الحالية وهي ليست نصيحة مالية. قم بأبحاثك الخاصة دائماً."
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in automated_recommendations on {client.name}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ فادح أثناء توليد التوصيات.")

async def run_top_gainers(context, chat_id, message_id, client: BaseExchangeClient):
    """يعرض قائمة بأكثر 10 عملات ربحاً في آخر 24 ساعة."""
    initial_text = f"📈 **الأعلى ربحاً ({client.name})**\n\n🔍 جارِ جلب البيانات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    
    market_data = await client.get_market_data()
    if not market_data:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق.")
        return

    valid_data = [item for item in market_data if float(item.get('quoteVolume', '0')) > MARKET_MOVERS_MIN_VOLUME]
    sorted_data = sorted(valid_data, key=lambda x: x.get('priceChangePercent', 0), reverse=True)[:10]

    message = f"📈 **الأعلى ربحاً على {client.name}** 📈\n\n"
    for i, coin in enumerate(sorted_data):
        symbol = coin['symbol'].replace('USDT', '')
        price = format_price(coin['lastPrice'])
        change = coin.get('priceChangePercent', 0)
        message += f"**{i+1}. ${symbol}:** `%{change:+.2f}` (السعر: ${price})\n"
    
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def run_top_losers(context, chat_id, message_id, client: BaseExchangeClient):
    """يعرض قائمة بأكثر 10 عملات خسارة في آخر 24 ساعة."""
    initial_text = f"📉 **الأعلى خسارة ({client.name})**\n\n🔍 جارِ جلب البيانات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    
    market_data = await client.get_market_data()
    if not market_data:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق.")
        return

    valid_data = [item for item in market_data if float(item.get('quoteVolume', '0')) > MARKET_MOVERS_MIN_VOLUME]
    sorted_data = sorted(valid_data, key=lambda x: x.get('priceChangePercent', 0))[:10]

    message = f"📉 **الأعلى خسارة على {client.name}** 📉\n\n"
    for i, coin in enumerate(sorted_data):
        symbol = coin['symbol'].replace('USDT', '')
        price = format_price(coin['lastPrice'])
        change = coin.get('priceChangePercent', 0)
        message += f"**{i+1}. ${symbol}:** `%{change:+.2f}` (السعر: ${price})\n"
        
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def run_top_volume(context, chat_id, message_id, client: BaseExchangeClient):
    """يعرض قائمة بأكثر 10 عملات تداولاً في آخر 24 ساعة."""
    initial_text = f"💰 **الأعلى تداولاً ({client.name})**\n\n🔍 جارِ جلب البيانات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    
    market_data = await client.get_market_data()
    if not market_data:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق.")
        return

    for item in market_data: item['quoteVolume_f'] = float(item.get('quoteVolume', '0'))
    sorted_data = sorted(market_data, key=lambda x: x['quoteVolume_f'], reverse=True)[:10]

    message = f"💰 **الأعلى تداولاً على {client.name}** 💰\n\n"
    for i, coin in enumerate(sorted_data):
        symbol = coin['symbol'].replace('USDT', '')
        price = format_price(coin['lastPrice'])
        volume = coin['quoteVolume_f']
        if volume > 1_000_000: volume_str = f"{volume/1_000_000:.2f}M"
        else: volume_str = f"{volume/1_000:.1f}K"
        message += f"**{i+1}. ${symbol}:** (الحجم: `${volume_str}`)\n"
        
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

# =============================================================================
# --- 5. المهام الآلية الدورية ---
# =============================================================================
def add_to_monitoring(symbol, alert_price, peak_volume, alert_time, source, exchange_name):
    """تضيف العملة إلى قائمة المراقبة لتتبع أدائها."""
    platform_name = exchange_name
    if platform_name not in PLATFORMS: return
        
    if symbol not in performance_tracker.get(platform_name, {}):
        performance_tracker[platform_name][symbol] = {
            'alert_price': alert_price, 'alert_time': alert_time, 'source': source,
            'current_price': alert_price, 'high_price': alert_price, 'status': 'Tracking',
            'momentum_lost_alerted': False
        }
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol} on {exchange_name}")

async def fomo_hunter_loop(client: BaseExchangeClient, bot_data):
    """
    مهمة خلفية للكشف عن عملات الزخم بشكل دوري وإرسال تنبيهات.
    """
    logger.info(f"Fomo Hunter background task started for {client.name}.")
    while True:
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)
        if not bot_data.get('background_tasks_enabled', True): continue
        logger.info(f"===== Fomo Hunter ({client.name}): Starting Automatic Scan =====")
        try:
            momentum_coins_data = await helper_get_momentum_symbols(client)
            if not momentum_coins_data:
                logger.info(f"Fomo Hunter ({client.name}): No significant momentum detected.")
                continue

            now = datetime.now(UTC)
            
            new_alerts = []
            for symbol, data in momentum_coins_data.items():
                last_alert_time = recently_alerted_fomo[client.name].get(symbol)
                if not last_alert_time or (now - last_alert_time) > timedelta(minutes=RUN_FOMO_SCAN_EVERY_MINUTES * 4):
                     new_alerts.append(data)
                     recently_alerted_fomo[client.name][symbol] = now

            if not new_alerts:
                logger.info(f"Fomo Hunter ({client.name}): Found momentum coins, but they were alerted recently.")
                continue

            sorted_coins = sorted(new_alerts, key=lambda x: x['price_change'], reverse=True)
            message = f"🚨 **تنبيه تلقائي من صياد الفومو ({client.name})** 🚨\n\n"
            for i, coin in enumerate(sorted_coins[:5]):
                message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n   - السعر: `${format_price(coin['current_price'])}`\n   - **زخم آخر 30 دقيقة: `%{coin['price_change']:+.2f}`**\n\n")
            
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
            
            for coin in sorted_coins[:5]:
                add_to_monitoring(coin['symbol'], float(coin['current_price']), coin.get('peak_volume', 0), now, f"صياد الفومو ({client.name})", client.name)

        except Exception as e:
            logger.error(f"Error in fomo_hunter_loop for {client.name}: {e}", exc_info=True)
        
async def new_listings_sniper_loop(client: BaseExchangeClient, bot_data):
    """
    مهمة خلفية للكشف عن الإدراحات الجديدة بشكل دوري.
    """
    logger.info(f"New Listings Sniper background task started for {client.name}.")
    global known_symbols
    known_symbols[client.name] = set()
    initial_data = await client.get_market_data()
    if initial_data:
        known_symbols[client.name] = {s['symbol'] for s in initial_data}
        logger.info(f"Sniper for {client.name}: Initialized with {len(known_symbols[client.name])} symbols.")
    
    while True:
        await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS)
        if not bot_data.get('background_tasks_enabled', True): continue
        try:
            data = await client.get_market_data()
            if not data: continue
            current_symbols = {s['symbol'] for s in data}
            if not known_symbols[client.name]:
                known_symbols[client.name] = current_symbols; continue
            
            newly_listed = current_symbols - known_symbols[client.name]
            if newly_listed:
                for symbol in newly_listed:
                    logger.info(f"Sniper ({client.name}): NEW LISTING DETECTED: {symbol}")
                    message = f"🎯 **إدراج جديد على {client.name}:** `${symbol}`"
                    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                known_symbols[client.name].update(newly_listed)
        except Exception as e:
            logger.error(f"An unexpected error in new_listings_sniper_loop for {client.name}: {e}")
            
async def performance_tracker_loop(session: aiohttp.ClientSession, bot_instance: Bot):
    """
    مهمة خلفية لتتبع أداء العملات المرصودة وتنبيه المستخدم عند فقدان الزخم.
    """
    logger.info("Performance Tracker background task started.")
    while True:
        await asyncio.sleep(RUN_PERFORMANCE_TRACKER_EVERY_MINUTES * 60)
        now = datetime.now(UTC)
        platforms_to_process = list(performance_tracker.keys())
        
        for platform in platforms_to_process:
            symbols_to_process = list(performance_tracker.get(platform, {}).items())
            if not symbols_to_process: continue
                
            for symbol, data in symbols_to_process:
                if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                    if performance_tracker.get(platform, {}).get(symbol):
                         performance_tracker[platform][symbol]['status'] = 'Archived'
                         logger.info(f"Archived {symbol} on {platform} after {PERFORMANCE_TRACKING_DURATION_HOURS} hours.")
                    continue
                
                if data.get('status') == 'Archived':
                    if performance_tracker.get(platform, {}).get(symbol):
                        del performance_tracker[platform][symbol]
                    continue
                
                try:
                    client = get_exchange_client(platform, session)
                    current_price = await client.get_current_price(symbol)
                    if current_price is None: continue

                    if performance_tracker.get(platform, {}).get(symbol):
                        tracker_entry = performance_tracker[platform][symbol]
                        tracker_entry['current_price'] = current_price
                        if current_price > tracker_entry.get('high_price', 0):
                            tracker_entry['high_price'] = current_price
                        
                        high_price = tracker_entry['high_price']
                        is_momentum_source = "الزخم" in data.get('source', '') or "الفومو" in data.get('source', '')
                        already_alerted = data.get('momentum_lost_alerted', False)

                        if is_momentum_source and not already_alerted and high_price > 0:
                            price_drop_percent = ((current_price - high_price) / high_price) * 100
                            if price_drop_percent <= MOMENTUM_LOSS_THRESHOLD_PERCENT:
                                message = (
                                    f"⚠️ **تنبيه: فقدان الزخم لعملة ${symbol.replace('USDT','')}** ({platform})\n\n"
                                    f"   - أعلى سعر: `${format_price(high_price)}`\n"
                                    f"   - السعر الحالي: `${format_price(current_price)}`\n"
                                    f"   - **الهبوط من القمة: `{price_drop_percent:.2f}%`**"
                                )
                                await bot_instance.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                                tracker_entry['momentum_lost_alerted'] = True
                                logger.info(f"MOMENTUM LOSS ALERT sent for {symbol} on {platform}")

                except Exception as e:
                    logger.error(f"Error updating price for {symbol} on {platform}: {e}")

# =============================================================================
# --- 6. تشغيل البوت ---
# =============================================================================
async def send_startup_message(bot_instance: Bot):
    """يرسل رسالة بدء التشغيل إلى الدردشة المحددة."""
    try:
        message = "✅ **بوت التداول الذكي (v15 - Fixed) متصل الآن!**\n\nأرسل /start لعرض القائمة."
        await bot_instance.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

async def main():
    """
    وظيفة التشغيل الرئيسية التي تبدأ البوت وتطلق المهام الخلفية.
    """
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set."); return
        
    async with aiohttp.ClientSession() as session:
        bot_instance = Bot(token=TELEGRAM_BOT_TOKEN)
        updater = Updater(bot=bot_instance, use_context=True)
        
        try:
            await bot_instance.get_updates(offset=-1, timeout=1, limit=1)
            logger.info("Cleared old updates.")
        except Exception:
            pass

        dp = updater.dispatcher
        loop = asyncio.get_running_loop()
        dp.bot_data['loop'] = loop
        dp.bot_data['session'] = session
        dp.bot_data['background_tasks_enabled'] = True
        
        dp.add_handler(CommandHandler("start", start_command))
        dp.add_handler(MessageHandler(Filters.text & (~Filters.command), handle_button_press))
        
        background_tasks['performance'] = asyncio.create_task(performance_tracker_loop(session, bot_instance))
        for platform_name in PLATFORMS:
            client = get_exchange_client(platform_name, session)
            background_tasks[f'fomo_{platform_name}'] = asyncio.create_task(fomo_hunter_loop(client, dp.bot_data))
            background_tasks[f'listings_{platform_name}'] = asyncio.create_task(new_listings_sniper_loop(client, dp.bot_data))
        
        updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot is now polling for commands...")
        await send_startup_message(bot_instance)
        
        # Keep the main coroutine alive to let background tasks run
        while True:
            await asyncio.sleep(3600)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
    except Exception as e:
        logger.critical(f"Bot failed to run: {e}", exc_info=True)