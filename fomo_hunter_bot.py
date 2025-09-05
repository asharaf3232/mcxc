# -*- coding: utf-8 -*-
import os
import asyncio
import sqlite3
import json
import logging
import aiohttp
import time
import numpy as np
from datetime import datetime, timedelta, UTC
from collections import deque
from telegram import Bot, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# =============================================================================
# --- 🔬 وحدة التحليل الفني المحسّنة (Analysis Module) 🔬 ---
# =============================================================================

def calculate_atr(high_prices, low_prices, close_prices, period=14):
    """
    يحسب مؤشر متوسط النطاق الحقيقي (ATR) لقياس التقلبات.
    """
    if len(high_prices) < period:
        return None

    tr_values = []
    for i in range(1, len(high_prices)):
        high = high_prices[i]
        low = low_prices[i]
        prev_close = close_prices[i-1]

        tr1 = high - low
        tr2 = abs(high - prev_close)
        tr3 = abs(low - prev_close)

        true_range = max(tr1, tr2, tr3)
        tr_values.append(true_range)

    atr = np.mean(tr_values[-period:])
    return atr

def calculate_vwap(close_prices, volumes, period=14):
    """
    يحسب مؤشر متوسط السعر المرجح بالحجم (VWAP).
    """
    if len(close_prices) < period:
        return None

    prices = np.array(close_prices[-period:])
    volumes = np.array(volumes[-period:])

    if np.sum(volumes) == 0:
        return np.mean(prices) # Fallback to SMA if volume is zero

    return np.sum(prices * volumes) / np.sum(volumes)

def analyze_momentum_consistency(close_prices, volumes, period=10):
    """
    يحلل استمرارية الزخم بدلاً من الاعتماد على قفزة واحدة.
    - يتأكد من أن 60% من الشموع الأخيرة إيجابية.
    - يتأكد من أن الحجم يتزايد بشكل عام.
    """
    if len(close_prices) < period:
        return 0 # Neutral score

    recent_closes = np.array(close_prices[-period:])
    recent_volumes = np.array(volumes[-period:])

    price_increases = np.sum(np.diff(recent_closes) > 0)

    score = 0
    # تحقق من استمرارية صعود السعر
    if (price_increases / period) >= 0.6:
        score += 1

    # تحقق من تزايد حجم التداول
    # نقسم الفترة إلى نصفين ونقارن متوسط الحجم
    half_period = period // 2
    first_half_volume_avg = np.mean(recent_volumes[:half_period])
    second_half_volume_avg = np.mean(recent_volumes[half_period:])

    if first_half_volume_avg > 0 and second_half_volume_avg > (first_half_volume_avg * 1.2):
        score += 1

    return score

async def calculate_pro_score(client, symbol: str):
    """
    الوظيفة التحليلية الجديدة: تحسب نقاطاً للعملة بناءً على معايير متعددة.
    هذا هو قلب المحرك التحليلي المطور.
    """
    score = 0
    analysis_details = {}

    try:
        # استخدام إطار زمني متوسط (15 دقيقة) للتحليل الشامل
        klines = await client.get_processed_klines(symbol, '15m', 100)
        if not klines or len(klines) < 50:
            return 0, {} # لا يمكن التحليل ببيانات غير كافية

        close_prices = np.array([float(k[4]) for k in klines])
        high_prices = np.array([float(k[2]) for k in klines])
        low_prices = np.array([float(k[3]) for k in klines])
        volumes = np.array([float(k[5]) for k in klines])
        current_price = close_prices[-1]

        # 1. تحليل الاتجاه العام (نقاط: -2 إلى +2)
        ema20 = np.mean(close_prices[-20:])
        ema50 = np.mean(close_prices[-50:])
        if current_price > ema20 > ema50:
            score += 2
            analysis_details['Trend'] = "🟢 Strong Up"
        elif current_price > ema20:
            score += 1
            analysis_details['Trend'] = "🟢 Up"
        elif current_price < ema20 < ema50:
            score -= 2
            analysis_details['Trend'] = "🔴 Strong Down"
        elif current_price < ema20:
            score -= 1
            analysis_details['Trend'] = "🔴 Down"
        else:
            analysis_details['Trend'] = "🟡 Sideways"

        # 2. تحليل الزخم القريب (نقاط: 0 إلى +2)
        momentum_score = analyze_momentum_consistency(close_prices, volumes)
        score += momentum_score
        analysis_details['Momentum'] = f"{'🟢' * momentum_score}{'🟡' * (2-momentum_score)} ({momentum_score}/2)"

        # 3. تحليل السيولة والتقلبات (نقاط: -1 إلى +1)
        atr = calculate_atr(high_prices, low_prices, close_prices)
        if atr:
            # تقيس التقلبات كنسبة مئوية من السعر الحالي
            volatility_percent = (atr / current_price) * 100 if current_price > 0 else 0
            analysis_details['Volatility'] = f"{volatility_percent:.2f}%"
            if volatility_percent > 7.0: # تقلبات عالية جداً قد تكون خطيرة
                score -= 1
            elif volatility_percent < 1.0: # تقلبات منخفضة جداً (لا توجد فرصة)
                score -= 1
            else: # تقلبات صحية
                score += 1

        # 4. تحليل القوة النسبية (RSI) (نقاط: -1 إلى +1)
        deltas = np.diff(close_prices)
        gains = deltas[deltas >= 0]
        losses = -deltas[deltas < 0]
        if len(gains) > 0 and len(losses) > 0:
            avg_gain = np.mean(gains)
            avg_loss = np.mean(losses)
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            analysis_details['RSI'] = f"{rsi:.1f}"
            if rsi > 75: score -= 1 # تشبع شرائي
            elif rsi < 25: score += 1 # تشبع بيعي

        # 5. تحليل VWAP (نقاط: 0 إلى +2)
        vwap = calculate_vwap(close_prices, volumes, period=20)
        if vwap:
            analysis_details['VWAP'] = f"{vwap:.8g}"
            if current_price > vwap * 1.01: # إذا كان السعر أعلى من VWAP بنسبة 1%
                score += 2 # إشارة قوية جداً على سيطرة المشترين
            elif current_price > vwap:
                score += 1

        analysis_details['Final Score'] = score
        analysis_details['Price'] = f"{current_price:.8g}"
        return score, analysis_details

    except Exception as e:
        print(f"Error in pro_score for {symbol}: {e}")
        return 0, {"Error": str(e)}

# =============================================================================
# --- الإعدادات الرئيسية ---
# =============================================================================

# --- Telegram Configuration ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
DATABASE_FILE = "users.db"

# --- Exchange API Keys ---
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

# --- إعدادات وحدة القناص الاحترافية (v28) ---
SNIPER_BLACKLIST = {'USDCUSDT', 'USDTUSDT', 'TUSDUSDT', 'FDUSDUSDT', 'DAIUSDT', 'USD1USDT'}
SNIPER_MIN_VOLATILITY_PERCENT = 0.75
SNIPER_RADAR_RUN_EVERY_MINUTES = 30
SNIPER_TRIGGER_RUN_EVERY_SECONDS = 60
SNIPER_COMPRESSION_PERIOD_HOURS = 8
SNIPER_MAX_VOLATILITY_PERCENT = 8.0
SNIPER_BREAKOUT_VOLUME_MULTIPLIER = 4.0
SNIPER_MIN_USDT_VOLUME = 200000

# --- إعدادات صائد الجواهر ---
GEM_MIN_CORRECTION_PERCENT = -70.0
GEM_MIN_24H_VOLUME_USDT = 200000
GEM_MIN_RISE_FROM_ATL_PERCENT = 50.0
GEM_LISTING_SINCE_DATE = datetime(2024, 1, 1, tzinfo=UTC)

# --- إعدادات المهام الدورية ---
RUN_FOMO_SCAN_EVERY_MINUTES = 15
RUN_LISTING_SCAN_EVERY_SECONDS = 60
RUN_PERFORMANCE_TRACKER_EVERY_MINUTES = 5
PERFORMANCE_TRACKING_DURATION_HOURS = 24
MARKET_MOVERS_MIN_VOLUME = 50000

# --- إعدادات التحليل الفني ---
TA_KLINE_LIMIT = 200
TA_MIN_KLINE_COUNT = 50
FIBONACCI_PERIOD = 90
PRO_SCAN_MIN_SCORE = 5 

# --- إعدادات عامة ---
HTTP_TIMEOUT = 15
API_CONCURRENCY_LIMIT = 8
TELEGRAM_MESSAGE_LIMIT = 4096

# --- إعدادات تسجيل الأخطاء ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة وقواعد البيانات ---
# =============================================================================
api_semaphore = asyncio.Semaphore(API_CONCURRENCY_LIMIT)
PLATFORMS = ["MEXC", "Gate.io", "Binance", "Bybit", "KuCoin", "OKX"]
performance_tracker = {p: {} for p in PLATFORMS}
active_hunts = {p: {} for p in PLATFORMS}
known_symbols = {p: set() for p in PLATFORMS}
recently_alerted_fomo = {p: {} for p in PLATFORMS}
sniper_watchlist = {p: {} for p in PLATFORMS}
sniper_tracker = {p: {} for p in PLATFORMS}

def setup_database():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.cursor().execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    logger.info("Database is set up.")

def load_user_ids():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM users")
        user_ids = {row[0] for row in cursor.fetchall()}
        conn.close()
        return user_ids
    except sqlite3.Error as e:
        logger.error(f"Database error while loading users: {e}")
        return set()

def save_user_id(chat_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.error(f"Database error while saving user {chat_id}: {e}")

def remove_user_id(chat_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        conn.commit()
        conn.close()
        logger.warning(f"User {chat_id} has been removed.")
    except sqlite3.Error as e:
        logger.error(f"Database error while removing user {chat_id}: {e}")

async def broadcast_message(bot: Bot, message_text: str, parse_mode=ParseMode.MARKDOWN):
    user_ids = load_user_ids()
    if not user_ids: return
    for user_id in user_ids:
        try:
            await bot.send_message(chat_id=user_id, text=message_text, parse_mode=parse_mode)
        except Forbidden: remove_user_id(user_id)
        except BadRequest as e:
            if "chat not found" in str(e): remove_user_id(user_id)
            else: logger.error(f"Failed to send to {user_id}: {e}")
        except Exception as e: logger.error(f"Unexpected error sending to {user_id}: {e}")

# =============================================================================
# --- الشبكة وعملاء المنصات ---
# =============================================================================
async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None, headers: dict = None, retries: int = 3):
    request_headers = {'User-Agent': 'Mozilla/5.0'}
    if headers: request_headers.update(headers)
    for attempt in range(retries):
        try:
            async with session.get(url, params=params, timeout=HTTP_TIMEOUT, headers=request_headers) as response:
                if response.status == 429:
                    wait_time = 2 ** (attempt + 1)
                    logger.warning(f"Rate limit hit for {url}. Retrying in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                    continue
                response.raise_for_status()
                return await response.json()
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            if attempt >= retries - 1: return None
            await asyncio.sleep(1)
    return None

def format_price(price_str):
    try:
        price = float(price_str)
        return f"{price:.10f}".rstrip('0') if price < 1e-4 else f"{price:.8g}"
    except (ValueError, TypeError): return price_str

class BaseExchangeClient:
    def __init__(self, session, **kwargs):
        self.session, self.name = session, "Base"
    async def get_market_data(self): raise NotImplementedError
    async def get_klines(self, symbol, interval, limit): raise NotImplementedError
    async def get_order_book(self, symbol, limit=20): raise NotImplementedError
    async def get_current_price(self, symbol): raise NotImplementedError
    async def get_processed_klines(self, symbol, interval, limit):
        klines = await self.get_klines(symbol, interval, limit)
        if klines: klines.sort(key=lambda x: int(x[0]))
        return klines

class MexcClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name, self.base_api_url = "MEXC", "https://api.mexc.com"
        self.interval_map = {'1m':'1m','5m':'5m','15m':'15m','1h':'1h','4h':'4h','1d':'1d'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        return [{'symbol':i['symbol'],'quoteVolume':i.get('quoteVolume','0'),'lastPrice':i.get('lastPrice','0'),'priceChangePercent':float(i.get('priceChangePercent','0'))*100} for i in data if i.get('symbol','').endswith("USDT")]
    async def get_klines(self, symbol, interval, limit):
        params = {'symbol':symbol,'interval':self.interval_map.get(interval,interval),'limit':limit}
        async with api_semaphore:
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/klines", params=params)
        return [[item[0],item[1],item[2],item[3],item[4],item[5]] for item in data] if data else None
    async def get_order_book(self, symbol, limit=20):
        params = {'symbol':symbol,'limit':limit}
        async with api_semaphore:
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
        self.interval_map = {'1m':'1m','5m':'5m','15m':'15m','1h':'1h','4h':'4h','1d':'1d'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/spot/tickers")
        if not data: return []
        return [{'symbol':i['currency_pair'].replace('_',''),'quoteVolume':i.get('quote_volume','0'),'lastPrice':i.get('last','0'),'priceChangePercent':float(i.get('change_percentage','0'))} for i in data if i.get('currency_pair','').endswith("_USDT")]
    async def get_klines(self, symbol, interval, limit):
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"
        params = {'currency_pair':gateio_symbol,'interval':self.interval_map.get(interval,interval),'limit':limit}
        async with api_semaphore:
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/spot/candlesticks", params=params)
        return [[int(k[0])*1000,k[5],k[3],k[4],k[2],k[1]] for k in data] if data else None
    async def get_order_book(self, symbol, limit=20):
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"
        params = {'currency_pair':gateio_symbol,'limit':limit}
        async with api_semaphore:
            await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/spot/order_book", params)
    async def get_current_price(self, symbol: str) -> float | None:
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"
        data = await fetch_json(self.session, f"{self.base_api_url}/spot/tickers", {'currency_pair': gateio_symbol})
        return float(data[0]['last']) if data and isinstance(data, list) and len(data) > 0 and 'last' in data[0] else None

class BinanceClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name, self.base_api_url = "Binance", "https://api.binance.com"
        self.interval_map = {'1m':'1m','5m':'5m','15m':'15m','1h':'1h','4h':'4h','1d':'1d'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        return [{'symbol':i['symbol'],'quoteVolume':i.get('quoteVolume','0'),'lastPrice':i.get('lastPrice','0'),'priceChangePercent':float(i.get('priceChangePercent','0'))} for i in data if i.get('symbol','').endswith("USDT")]
    async def get_klines(self, symbol, interval, limit):
        params = {'symbol':symbol,'interval':self.interval_map.get(interval,interval),'limit':limit}
        async with api_semaphore:
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/klines", params=params)
        return [[item[0],item[1],item[2],item[3],item[4],item[5]] for item in data] if data else None
    async def get_order_book(self, symbol, limit=20):
        params = {'symbol':symbol,'limit':limit}
        async with api_semaphore:
            await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/api/v3/depth", params)
    async def get_current_price(self, symbol: str) -> float | None:
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/price", {'symbol': symbol})
        return float(data['price']) if data and 'price' in data else None

class BybitClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name, self.base_api_url = "Bybit", "https://api.bybit.com"
        self.interval_map = {'1m':'1','5m':'5','15m':'15','1h':'60','4h':'240','1d':'D'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/tickers", params={'category':'spot'})
        if not data or not data.get('result') or not data['result'].get('list'): return []
        return [{'symbol':i['symbol'],'quoteVolume':i.get('turnover24h','0'),'lastPrice':i.get('lastPrice','0'),'priceChangePercent':float(i.get('price24hPcnt','0'))*100} for i in data['result']['list'] if i['symbol'].endswith("USDT")]
    async def get_klines(self, symbol, interval, limit):
        params = {'category':'spot','symbol':symbol,'interval':self.interval_map.get(interval,'5'),'limit':limit}
        async with api_semaphore:
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/kline", params=params)
        return [[int(k[0]),k[1],k[2],k[3],k[4],k[5]] for k in data['result']['list']] if data and data.get('result') and data['result'].get('list') else None
    async def get_order_book(self, symbol, limit=20):
        params = {'category':'spot','symbol':symbol,'limit':limit}
        async with api_semaphore:
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/orderbook", params)
        return {'bids':data['result'].get('bids',[]),'asks':data['result'].get('asks',[])} if data and data.get('result') else None
    async def get_current_price(self, symbol: str) -> float | None:
        data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/tickers", params={'category':'spot','symbol':symbol})
        return float(data['result']['list'][0]['lastPrice']) if data and data.get('result') and data['result'].get('list') else None

class KucoinClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name, self.base_api_url = "KuCoin", "https://api.kucoin.com"
        self.interval_map = {'1m':'1min','5m':'5min','15m':'15min','1h':'1hour','4h':'4hour','1d':'1day'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/allTickers")
        if not data or not data.get('data') or not data['data'].get('ticker'): return []
        return [{'symbol':i['symbol'].replace('-',''),'quoteVolume':i.get('volValue','0'),'lastPrice':i.get('last','0'),'priceChangePercent':float(i.get('changeRate','0'))*100} for i in data['data']['ticker'] if i.get('symbol','').endswith("-USDT")]
    async def get_klines(self, symbol, interval, limit):
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        params = {'symbol':kucoin_symbol,'type':self.interval_map.get(interval,'5min')}
        async with api_semaphore:
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/candles", params=params)
        return [[int(k[0])*1000,k[2],k[3],k[4],k[1],k[5]] for k in data['data']] if data and data.get('data') else None
    async def get_order_book(self, symbol, limit=20):
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        async with api_semaphore:
            await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/orderbook/level2_20", {'symbol':kucoin_symbol})
    async def get_current_price(self, symbol: str) -> float | None:
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/orderbook/level1", {'symbol': kucoin_symbol})
        return float(data['data']['price']) if data and data.get('data') else None

class OkxClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name, self.base_api_url = "OKX", "https://www.okx.com"
        self.interval_map = {'1m':'1m','5m':'5m','15m':'15m','1h':'1H','4h':'4H','1d':'1D'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/tickers", params={'instType':'SPOT'})
        if not data or not data.get('data'): return []
        results = []
        for i in data['data']:
            if i.get('instId','').endswith("-USDT"):
                try:
                    lp,op=float(i.get('last','0')),float(i.get('open24h','0'))
                    cp=((lp-op)/op)*100 if op > 0 else 0.0
                    results.append({'symbol':i['instId'].replace('-',''),'quoteVolume':i.get('volCcy24h','0'),'lastPrice':i.get('last','0'),'priceChangePercent':cp})
                except (ValueError,TypeError): continue
        return results
    async def get_klines(self, symbol, interval, limit):
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        params = {'instId':okx_symbol,'bar':self.interval_map.get(interval,'5m'),'limit':limit}
        async with api_semaphore:
            await asyncio.sleep(0.25)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/candles", params=params)
        return [[int(k[0]),k[1],k[2],k[3],k[4],k[5]] for k in data['data']] if data and data.get('data') else None
    async def get_order_book(self, symbol, limit=20):
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        params = {'instId':okx_symbol,'sz':str(limit)}
        async with api_semaphore:
            await asyncio.sleep(0.25)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/books", params)
        return {'bids':data['data'][0].get('bids',[]),'asks':data['data'][0].get('asks',[])} if data and data.get('data') else None
    async def get_current_price(self, symbol: str) -> float | None:
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/tickers", params={'instId': okx_symbol})
        return float(data['data'][0]['last']) if data and data.get('data') else None

def get_exchange_client(exchange_name, session):
    clients = {'mexc': MexcClient, 'gate.io': GateioClient, 'binance': BinanceClient, 'bybit': BybitClient, 'kucoin': KucoinClient, 'okx': OkxClient}
    client_class = clients.get(exchange_name.lower())
    return client_class(session) if client_class else None

# =============================================================================
# --- 🔬 قسم التحليل الفني (TA Section) 🔬 ---
# =============================================================================
def calculate_poc(klines, num_bins=50):
    if not klines or len(klines) < 10: return None
    try:
        high_prices=np.array([float(k[2]) for k in klines]); low_prices=np.array([float(k[3]) for k in klines]); volumes=np.array([float(k[5]) for k in klines])
        min_price,max_price = np.min(low_prices),np.max(high_prices)
        if max_price == min_price: return min_price
        price_bins=np.linspace(min_price,max_price,num_bins); volume_per_bin=np.zeros(num_bins)
        for i in range(len(klines)):
            avg_price=(high_prices[i]+low_prices[i])/2
            bin_index=np.searchsorted(price_bins,avg_price)-1
            if 0<=bin_index<num_bins: volume_per_bin[bin_index]+=volumes[i]
        if np.sum(volume_per_bin)==0: return None
        return price_bins[np.argmax(volume_per_bin)]
    except Exception as e: logger.error(f"Error calculating POC: {e}"); return None

def calculate_ema_series(prices, period):
    if len(prices) < period: return []
    ema = []
    sma = sum(prices[:period]) / period
    ema.append(sma)
    multiplier = 2 / (period + 1)
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema

def calculate_ema(prices, period):
    if len(prices) < period: return None
    return calculate_ema_series(prices, period)[-1]

def calculate_sma(prices, period):
    if len(prices) < period: return None
    return np.mean(prices[-period:])

def calculate_macd(prices, fast_period=12, slow_period=26, signal_period=9):
    if len(prices) < slow_period: return None, None
    ema_fast = calculate_ema_series(prices, fast_period)
    ema_slow = calculate_ema_series(prices, slow_period)
    if not ema_fast or not ema_slow: return None, None
    ema_fast = ema_fast[len(ema_fast) - len(ema_slow):]
    macd_line_series = np.array(ema_fast) - np.array(ema_slow)
    signal_line_series = calculate_ema_series(macd_line_series.tolist(), signal_period)
    if not signal_line_series: return None, None
    return macd_line_series[-1], signal_line_series[-1]

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return None
    deltas = np.diff(prices)
    gains = deltas[deltas >= 0]
    losses = -deltas[deltas < 0]
    if len(gains) == 0: avg_gain = 0
    else: avg_gain = np.mean(gains)
    if len(losses) == 0: avg_loss = 1e-10
    else: avg_loss = np.mean(losses)
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_bollinger_bands(prices, period=20, num_std_dev=2):
    if len(prices) < period: return None, None, None
    middle_band = calculate_sma(prices, period)
    if middle_band is None: return None, None, None
    std_dev = np.std(prices[-period:])
    upper_band = middle_band + (std_dev * num_std_dev)
    lower_band = middle_band - (std_dev * num_std_dev)
    return upper_band, middle_band, lower_band

def find_support_resistance(high_prices, low_prices, window=10):
    supports, resistances = [], []
    for i in range(window, len(high_prices) - window):
        if high_prices[i] == max(high_prices[i-window:i+window+1]): resistances.append(high_prices[i])
        if low_prices[i] == min(low_prices[i-window:i+window+1]): supports.append(low_prices[i])
    return sorted(list(set(supports)), reverse=True), sorted(list(set(resistances)), reverse=True)

def calculate_fibonacci_retracement(high_prices, low_prices, period=90):
    if len(high_prices) < period: recent_highs, recent_lows = high_prices, low_prices
    else: recent_highs, recent_lows = high_prices[-period:], low_prices[-period:]
    max_price, min_price = np.max(recent_highs), np.min(recent_lows)
    difference = max_price - min_price
    if difference == 0: return {}
    return {'level_0.382': max_price-(difference*0.382),'level_0.5': max_price-(difference*0.5),'level_0.618': max_price-(difference*0.618)}

def analyze_trend(current_price, ema21, ema50, sma100):
    if ema21 and ema50 and sma100:
        if current_price > ema21 > ema50 > sma100: return "🟢 اتجاه صاعد قوي.", 2
        if current_price > ema50 and current_price > ema21: return "🟢 اتجاه صاعد.", 1
        if current_price < ema21 < ema50 < sma100: return "🔴 اتجاه هابط قوي.", -2
        if current_price < ema50 and current_price < ema21: return "🔴 اتجاه هابط.", -1
    return "🟡 جانبي / غير واضح.", 0

# =============================================================================
# --- الوظائف المساعدة للتحليل ---
# =============================================================================
async def helper_get_momentum_symbols(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {}
    potential_coins = [p for p in market_data if float(p.get('lastPrice','1')) <= MOMENTUM_MAX_PRICE and 50000 <= float(p.get('quoteVolume','0')) <= 2000000]
    if not potential_coins: return {}
    tasks = [client.get_processed_klines(p['symbol'], '5m', 12) for p in potential_coins]
    all_klines_data = await asyncio.gather(*tasks)
    momentum_coins_data = {}
    for i, klines in enumerate(all_klines_data):
        if not klines or len(klines) < 12: continue
        try:
            klines = klines[-12:]
            old_v = sum(float(k[5]) for k in klines[:6]); new_v = sum(float(k[5]) for k in klines[6:])
            start_p = float(klines[6][1])
            if old_v == 0 or start_p == 0: continue
            end_p = float(klines[-1][4])
            price_change = ((end_p - start_p) / start_p) * 100
            if new_v > old_v * 1.8 and price_change > 4.0:
                coin_symbol = potential_coins[i]['symbol']
                momentum_coins_data[coin_symbol] = {'symbol': coin_symbol, 'price_change': price_change, 'current_price': end_p}
        except (ValueError, IndexError, TypeError): continue
    return momentum_coins_data

async def helper_get_whale_activity(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {}
    potential_gems = [p for p in market_data if float(p.get('lastPrice','999')) <= WHALE_GEM_MAX_PRICE and 100000 <= float(p.get('quoteVolume','0')) <= 5000000]
    if not potential_gems: return {}
    for p in potential_gems: p['change_float'] = p.get('priceChangePercent', 0)
    top_gems = sorted(potential_gems, key=lambda x: x['change_float'], reverse=True)[:50]
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
    signals = []
    if not book or not book.get('bids') or not book.get('asks'): return signals
    try:
        bids = sorted([(float(item[0]), float(item[1])) for item in book['bids'] if len(item) >= 2], key=lambda x: x[0], reverse=True)
        asks = sorted([(float(item[0]), float(item[1])) for item in book['asks'] if len(item) >= 2], key=lambda x: x[0])
        for price, qty in bids[:5]:
            if price * qty >= 25000: signals.append({'type': 'Buy Wall', 'value': price*qty, 'price': price}); break
        for price, qty in asks[:5]:
            if price * qty >= 25000: signals.append({'type': 'Sell Wall', 'value': price*qty, 'price': price}); break
        bids_value, asks_value = sum(p*q for p,q in bids[:10]), sum(p*q for p,q in asks[:10])
        if asks_value > 0 and (bids_value/asks_value) >= 3.0: signals.append({'type': 'Buy Pressure', 'value': bids_value/asks_value})
        elif bids_value > 0 and (asks_value/bids_value) >= 3.0: signals.append({'type': 'Sell Pressure', 'value': asks_value/bids_value})
    except Exception as e: logger.warning(f"Could not analyze order book for {symbol}: {e}")
    return signals

# =============================================================================
# --- 4. الوظائف التفاعلية (أوامر البوت) ---
# =============================================================================
BTN_TA_PRO = "🔬 محلل فني"
BTN_PRO_SCAN = "🎯 فحص احترافي"
BTN_GEM_HUNTER = "💎 صائد الجواهر"
BTN_SNIPER_LIST = "🔭 قائمة القنص"
BTN_MOMENTUM = "🚀 كاشف الزخم"
BTN_WHALE_RADAR = "🐋 رادار الحيتان"
BTN_PERFORMANCE = "📈 تقرير الأداء"
BTN_STATUS = "📊 الحالة"
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

def build_menu(context: ContextTypes.DEFAULT_TYPE):
    selected_exchange = context.user_data.get('exchange', 'mexc')
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    
    mexc_btn = f"✅ {BTN_SELECT_MEXC}" if selected_exchange == 'mexc' else BTN_SELECT_MEXC
    gate_btn = f"✅ {BTN_SELECT_GATEIO}" if selected_exchange == 'gate.io' else BTN_SELECT_GATEIO
    binance_btn = f"✅ {BTN_SELECT_BINANCE}" if selected_exchange == 'binance' else BTN_SELECT_BINANCE
    bybit_btn = f"✅ {BTN_SELECT_BYBIT}" if selected_exchange == 'bybit' else BTN_SELECT_BYBIT
    kucoin_btn = f"✅ {BTN_SELECT_KUCOIN}" if selected_exchange == 'kucoin' else BTN_SELECT_KUCOIN
    okx_btn = f"✅ {BTN_SELECT_OKX}" if selected_exchange == 'okx' else BTN_SELECT_OKX
    toggle_tasks_btn = BTN_TASKS_ON if tasks_enabled else BTN_TASKS_OFF
    
    keyboard = [
        [BTN_PRO_SCAN, BTN_MOMENTUM, BTN_WHALE_RADAR],
        [BTN_TA_PRO, BTN_GEM_HUNTER, BTN_SNIPER_LIST],
        [BTN_TOP_GAINERS, BTN_TOP_VOLUME, BTN_TOP_LOSERS],
        [BTN_PERFORMANCE, BTN_STATUS, toggle_tasks_btn],
        [mexc_btn, gate_btn, binance_btn],
        [bybit_btn, kucoin_btn, okx_btn]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message: save_user_id(update.message.chat_id)
    context.user_data['exchange'] = 'mexc'
    context.bot_data.setdefault('background_tasks_enabled', True)
    welcome_message = "أهلاً بك في بوت الصياد الذكي!\n\nاستخدم لوحة الأزرار أدناه لاستكشاف السوق والبدء في الصيد.\n\n⚠️ **تنبيه هام:** أنا أداة للمساعدة والتحليل فقط، ولست مستشاراً مالياً."
    if update.message: await update.message.reply_text(welcome_message, reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

async def set_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE, exchange_name: str):
    context.user_data['exchange'] = exchange_name.lower()
    await update.message.reply_text(f"✅ تم تحويل المنصة إلى: **{exchange_name}**", reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

async def toggle_background_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks_enabled = not context.bot_data.get('background_tasks_enabled', True)
    context.bot_data['background_tasks_enabled'] = tasks_enabled
    status = "تفعيل" if tasks_enabled else "إيقاف"
    await update.message.reply_text(f"✅ تم **{status}** المهام الخلفية.", reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    registered_users = len(load_user_ids())
    message = f"📊 **حالة البوت** 📊\n\n"
    message += f"**- المستخدمون:** `{registered_users}` | **المهام:** {'🟢 نشطة' if tasks_enabled else '🔴 متوقفة'}\n\n"
    for platform in PLATFORMS:
        sniper_count = len(sniper_watchlist.get(platform, {}))
        tracked_count = len(sniper_tracker.get(platform, {}))
        message += f"**{platform}:** 🔭 {sniper_count} | 🔫 {tracked_count}\n"
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.strip()
    if context.user_data.get('awaiting_symbol_for_ta'):
        symbol = text.upper()
        if not symbol.endswith("USDT"): symbol += "USDT"
        context.user_data['awaiting_symbol_for_ta'] = False
        context.args = [symbol]
        await run_full_technical_analysis(update, context)
        return
    
    button_text = text.replace("✅ ", "")
    if button_text == BTN_TA_PRO:
        context.user_data['awaiting_symbol_for_ta'] = True
        await update.message.reply_text("🔬 يرجى إرسال رمز العملة للتحليل (مثال: `BTC`)", parse_mode=ParseMode.MARKDOWN)
        return
    if button_text == BTN_SNIPER_LIST: await show_sniper_watchlist(update, context); return
    if button_text == BTN_STATUS: await status_command(update, context); return
    
    exchange_map = {BTN_SELECT_MEXC: "MEXC", BTN_SELECT_GATEIO: "Gate.io", BTN_SELECT_BINANCE: "Binance", BTN_SELECT_BYBIT: "Bybit", BTN_SELECT_KUCOIN: "KuCoin", BTN_SELECT_OKX: "OKX"}
    if button_text in exchange_map: await set_exchange(update, context, exchange_map[button_text]); return
    
    if button_text in [BTN_TASKS_ON, BTN_TASKS_OFF]: await toggle_background_tasks(update, context); return
    
    chat_id = update.message.chat_id
    session = context.application.bot_data['session']
    current_exchange = context.user_data.get('exchange', 'mexc')
    client = get_exchange_client(current_exchange, session)
    if not client: await update.message.reply_text("خطأ في اختيار المنصة."); return
    
    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"🔍 جارِ تنفيذ طلبك على {client.name}...")
    
    task_map = {
        BTN_MOMENTUM: run_momentum_detector, BTN_WHALE_RADAR: run_whale_radar_scan,
        BTN_PRO_SCAN: run_pro_scan, BTN_PERFORMANCE: get_performance_report,
        BTN_TOP_GAINERS: run_top_gainers, BTN_TOP_LOSERS: run_top_losers,
        BTN_TOP_VOLUME: run_top_volume, BTN_GEM_HUNTER: run_gem_hunter_scan
    }
    if button_text in task_map: asyncio.create_task(task_map[button_text](context, chat_id, sent_message.message_id, client))

async def send_long_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    if len(text) <= 4096:
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return
    parts = [text[i:i+4096] for i in range(0, len(text), 4096)]
    for part in parts:
        await context.bot.send_message(chat_id=chat_id, text=part, **kwargs)
        await asyncio.sleep(0.5)

async def run_full_technical_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    symbol = context.args[0]
    current_exchange = context.user_data.get('exchange', 'mexc')
    client = get_exchange_client(current_exchange, context.application.bot_data['session'])
    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"🔬 جارِ إجراء تحليل فني شامل لـ ${symbol}...")
    try:
        timeframes = {'يومي': '1d', '4 ساعات': '4h', 'ساعة': '1h'}
        tf_weights = {'يومي': 3, '4 ساعات': 2, 'ساعة': 1}
        report_parts = []
        header = f"📊 **التحليل الفني المفصل لـ ${symbol}** ({client.name})\n_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_\n\n"
        overall_score = 0
        for tf_name, tf_interval in timeframes.items():
            klines = await client.get_processed_klines(symbol, tf_interval, 200)
            tf_report = f"--- **إطار {tf_name}** ---\n"
            if not klines or len(klines) < 50:
                tf_report += "لا توجد بيانات كافية للتحليل.\n\n"; report_parts.append(tf_report); continue
            close_prices=np.array([float(k[4]) for k in klines[-200:]]); high_prices=np.array([float(k[2]) for k in klines[-200:]]); low_prices=np.array([float(k[3]) for k in klines[-200:]])
            current_price=close_prices[-1]; report_lines=[]; weight=tf_weights[tf_name]
            ema21,ema50,sma100=calculate_ema(close_prices,21),calculate_ema(close_prices,50),calculate_sma(close_prices,100)
            trend_text,trend_score = analyze_trend(current_price,ema21,ema50,sma100)
            report_lines.append(f"**الاتجاه:** {trend_text}"); overall_score+=trend_score*weight
            macd_line,signal_line=calculate_macd(close_prices)
            if macd_line is not None and signal_line is not None:
                if macd_line > signal_line: report_lines.append("🟢 **MACD:** إيجابي."); overall_score+=1*weight
                else: report_lines.append("🔴 **MACD:** سلبي."); overall_score-=1*weight
            rsi=calculate_rsi(close_prices)
            if rsi:
                if rsi > 70: report_lines.append(f"🔴 **RSI ({rsi:.1f}):** تشبع شرائي."); overall_score-=1*weight
                elif rsi < 30: report_lines.append(f"🟢 **RSI ({rsi:.1f}):** تشبع بيعي."); overall_score+=1*weight
                else: report_lines.append(f"🟡 **RSI ({rsi:.1f}):** محايد.")
            supports,resistances=find_support_resistance(high_prices,low_prices)
            next_res=min([r for r in resistances if r>current_price],default=None)
            if next_res: report_lines.append(f"🛡️ **أقرب مقاومة:** {format_price(next_res)}")
            next_sup=max([s for s in supports if s<current_price],default=None)
            if next_sup: report_lines.append(f"💰 **أقرب دعم:** {format_price(next_sup)}")
            fib_levels=calculate_fibonacci_retracement(high_prices,low_prices)
            if fib_levels: report_lines.append(f"🎚️ **فيبوناتشي:** 0.5: `{format_price(fib_levels['level_0.5'])}` | 0.618: `{format_price(fib_levels['level_0.618'])}`")
            tf_report += "\n".join(report_lines) + f"\n*السعر الحالي: {format_price(current_price)}*\n\n"
            report_parts.append(tf_report)
        summary_report = "--- **ملخص التحليل** ---\n"
        if overall_score >= 5: summary_report += f"🟢 **إيجابي بقوة (النقاط: {overall_score}).**"
        elif overall_score > 0: summary_report += f"🟢 **إيجابي (النقاط: {overall_score}).**"
        elif overall_score <= -5: summary_report += f"🔴 **سلبي بقوة (النقاط: {overall_score}).**"
        elif overall_score < 0: summary_report += f"🔴 **سلبي (النقاط: {overall_score}).**"
        else: summary_report += f"🟡 **محايد (النقاط: {overall_score}).**"
        report_parts.append(summary_report)
        await context.bot.delete_message(chat_id=chat_id, message_id=sent_message.message_id)
        await send_long_message(context, chat_id, header + "".join(report_parts), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in TA for {symbol}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=f"حدث خطأ أثناء تحليل {symbol}.")

async def run_pro_scan(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🎯 **الفحص الاحترافي المطور ({client.name})**\n\n🔍 جارِ تحليل السوق وتصنيف العملات... هذه العملية قد تستغرق دقيقة."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    try:
        market_data = await client.get_market_data()
        if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق."); return
        candidates = [p['symbol'] for p in market_data if 50000 <= float(p.get('quoteVolume', '0')) <= 2000000 and float(p.get('lastPrice', '1')) <= 0.10]
        if not candidates: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص الاحترافي على {client.name} اكتمل:**\n\nلا توجد عملات مرشحة حالياً."); return
        results = await asyncio.gather(*[calculate_pro_score(client, symbol) for symbol in candidates[:100]])
        strong_opportunities = []
        for i, (score, details) in enumerate(results):
            if score >= PRO_SCAN_MIN_SCORE: details['symbol'] = candidates[i]; strong_opportunities.append(details)
        if not strong_opportunities: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص الاحترافي على {client.name} اكتمل:**\n\nلم يتم العثور على فرص قوية."); return
        sorted_ops = sorted(strong_opportunities, key=lambda x: x['Final Score'], reverse=True)
        message = f"🎯 **أفضل الفرص حسب النقاط ({client.name})** 🎯\n\n"
        for i, coin_details in enumerate(sorted_ops[:5]):
            message += (f"**{i+1}. ${coin_details['symbol'].replace('USDT', '')}**\n"
                        f"    - **النقاط:** `{coin_details['Final Score']}` ⭐\n"
                        f"    - **السعر:** `${format_price(coin_details.get('Price', 'N/A'))}`\n"
                        f"    - **الاتجاه:** `{coin_details.get('Trend', 'N/A')}`\n"
                        f"    - **الزخم:** `{coin_details.get('Momentum', 'N/A')}`\n\n")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in pro_scan on {client.name}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ فادح أثناء الفحص الاحترافي.")

async def run_momentum_detector(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🚀 **كاشف الزخم ({client.name})**\n\n🔍 جارِ الفحص..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    momentum_coins_data = await helper_get_momentum_symbols(client)
    if not momentum_coins_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **فحص الزخم على {client.name} اكتمل:** لا يوجد زخم حالياً."); return
    sorted_coins = sorted(momentum_coins_data.values(), key=lambda x: x['price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم ({client.name}) - {datetime.now().strftime('%H:%M')}** 🚀\n\n"
    for i, coin in enumerate(sorted_coins[:10]):
        message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n    - السعر: `${format_price(coin['current_price'])}`\n    - **الزخم: `%{coin['price_change']:+.2f}`**\n\n")
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    for coin in sorted_coins[:10]: add_to_monitoring(coin['symbol'], float(coin['current_price']), 0, datetime.now(UTC), f"الزخم ({client.name})", client.name)

async def run_whale_radar_scan(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🐋 **رادار الحيتان ({client.name})**\n\n🔍 جارِ الفحص..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    whale_signals_by_symbol = await helper_get_whale_activity(client)
    if not whale_signals_by_symbol: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **فحص الرادار على {client.name} اكتمل:** لا يوجد نشاط حيتان واضح."); return
    all_signals = [s for sl in whale_signals_by_symbol.values() for s in sl]
    sorted_signals = sorted(all_signals, key=lambda x: x.get('value', 0), reverse=True)
    message = f"🐋 **تقرير رادار الحيتان ({client.name}) - {datetime.now().strftime('%H:%M')}** 🐋\n\n"
    for signal in sorted_signals:
        s_name = signal['symbol'].replace('USDT', '')
        if signal['type'] == 'Buy Wall': message += f"🟢 **حائط شراء على ${s_name}**\n    - **الحجم:** `${signal['value']:,.0f}` USDT عند `{format_price(signal['price'])}`\n\n"
        elif signal['type'] == 'Sell Wall': message += f"🔴 **حائط بيع على ${s_name}**\n    - **الحجم:** `${signal['value']:,.0f}` USDT عند `{format_price(signal['price'])}`\n\n"
        elif signal['type'] == 'Buy Pressure': message += f"📈 **ضغط شراء على ${s_name}** (النسبة: `{signal['value']:.1f}x`)\n\n"
        elif signal['type'] == 'Sell Pressure': message += f"📉 **ضغط بيع على ${s_name}** (النسبة: `{signal['value']:.1f}x`)\n\n"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def get_performance_report(context, chat_id, message_id):
    try:
        if not any(performance_tracker.values()) and not any(sniper_tracker.values()):
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="ℹ️ لا توجد عملات قيد تتبع الأداء حالياً."); return
        message = "📊 **تقرير الأداء الشامل** 📊\n\n"
        if any(performance_tracker.values()):
            message += "📈 **أداء عملات الزخم** 📈\n\n"
            all_items = [(s,d) for p,sd in performance_tracker.items() for s,d in sd.items()]
            sorted_symbols = sorted(all_items, key=lambda i: i[1]['alert_time'], reverse=True)
            for symbol, data in sorted_symbols:
                if data.get('status') == 'Archived': continue
                ap,cp,hp = data.get('alert_price',0),data.get('current_price',data.get('alert_price',0)),data.get('high_price',data.get('alert_price',0))
                cc = ((cp-ap)/ap)*100 if ap>0 else 0; pc = ((hp-ap)/ap)*100 if ap>0 else 0
                emoji = "🟢" if cc>=0 else "🔴"; time_str = f"{(datetime.now(UTC)-data['alert_time']).total_seconds()/3600:.1f} س"
                message += f"{emoji} **${symbol.replace('USDT','')}** ({data.get('source','N/A')}) (منذ {time_str})\n    - الحالي: `${format_price(cp)}` (**{cc:+.2f}%**)\n    - الأعلى: `${format_price(hp)}` (**{pc:+.2f}%**)\n\n"
        if any(sniper_tracker.values()):
            message += "\n🔫 **متابعة نتائج القناص** 🔫\n\n"
            all_sniper_items = [(s,d) for p,sd in sniper_tracker.items() for s,d in sd.items() if d.get('status')=='Tracking']
            sorted_sniper_items = sorted(all_sniper_items, key=lambda i: i[1]['alert_time'], reverse=True)
            for symbol, data in sorted_sniper_items:
                time_str = f"{(datetime.now(UTC)-data['alert_time']).total_seconds()/3600:.1f} س"
                message += f"🎯 **${symbol.replace('USDT','')}** (منذ {time_str})\n    - **الهدف:** `{format_price(data['target_price'])}`\n    - **الفشل:** `{format_price(data['invalidation_price'])}`\n\n"
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        await send_long_message(context, chat_id, message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_performance_report: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ أثناء جلب التقرير.")

async def run_top_gainers(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"📈 **الأعلى ربحاً ({client.name})**\n\n🔍 جارِ جلب البيانات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    market_data = await client.get_market_data()
    if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="خطأ في جلب البيانات."); return
    valid_data = [i for i in market_data if float(i.get('quoteVolume','0')) > MARKET_MOVERS_MIN_VOLUME]
    sorted_data = sorted(valid_data, key=lambda x: x.get('priceChangePercent',0), reverse=True)[:10]
    message = f"📈 **الأعلى ربحاً على {client.name}** 📈\n\n"
    for i, coin in enumerate(sorted_data): message += f"**{i+1}. ${coin['symbol'].replace('USDT','')}:** `%{coin.get('priceChangePercent',0):+.2f}`\n"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def run_top_losers(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"📉 **الأعلى خسارة ({client.name})**\n\n🔍 جارِ جلب البيانات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    market_data = await client.get_market_data()
    if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="خطأ في جلب البيانات."); return
    valid_data = [i for i in market_data if float(i.get('quoteVolume','0')) > MARKET_MOVERS_MIN_VOLUME]
    sorted_data = sorted(valid_data, key=lambda x: x.get('priceChangePercent',0))[:10]
    message = f"📉 **الأعلى خسارة على {client.name}** 📉\n\n"
    for i, coin in enumerate(sorted_data): message += f"**{i+1}. ${coin['symbol'].replace('USDT','')}:** `%{coin.get('priceChangePercent',0):+.2f}`\n"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def run_top_volume(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"💰 **الأعلى تداولاً ({client.name})**\n\n🔍 جارِ جلب البيانات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    market_data = await client.get_market_data()
    if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="خطأ في جلب البيانات."); return
    for item in market_data: item['quoteVolume_f'] = float(item.get('quoteVolume','0'))
    sorted_data = sorted(market_data, key=lambda x: x['quoteVolume_f'], reverse=True)[:10]
    message = f"💰 **الأعلى تداولاً على {client.name}** 💰\n\n"
    for i, coin in enumerate(sorted_data):
        vol = coin['quoteVolume_f']
        vol_str = f"{vol/1_000_000:.2f}M" if vol > 1_000_000 else f"{vol/1_000:.1f}K"
        message += f"**{i+1}. ${coin['symbol'].replace('USDT','')}:** (الحجم: `${vol_str}`)\n"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def show_sniper_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = "🔭 **قائمة مراقبة القناص** 🔭\n\n"
    any_watched = any(sniper_watchlist.values())
    if any_watched:
        for platform, watchlist in sniper_watchlist.items():
            if watchlist:
                message += f"--- **{platform}** ---\n"
                for symbol, data in list(watchlist.items())[:5]:
                    poc_str = f", POC: `{format_price(data['poc'])}`" if 'poc' in data else ""
                    message += f"- `${symbol.replace('USDT','')}` (نطاق: `{format_price(data['low'])}` - `{format_price(data['high'])}`{poc_str})\n"
                if len(watchlist) > 5: message += f"    *... و {len(watchlist) - 5} عملات أخرى.*\n\n"
    else: message += "لا توجد أهداف حالية في قائمة المراقبة."
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def run_gem_hunter_scan(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"💎 **صائد الجواهر ({client.name})**\n\n🔍 جارِ تنفيذ مسح عميق للسوق..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    try:
        market_data = await client.get_market_data()
        if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="خطأ في جلب بيانات السوق."); return
        volume_candidates = [c for c in market_data if float(c.get('quoteVolume', '0')) > GEM_MIN_24H_VOLUME_USDT]
        final_gems = []
        for coin in volume_candidates:
            symbol = coin['symbol']
            try:
                klines = await client.get_processed_klines(symbol, '1d', 1000)
                if not klines or len(klines) < 10: continue
                if datetime.fromtimestamp(int(klines[0][0])/1000, tz=UTC) < GEM_LISTING_SINCE_DATE: continue
                high_prices, low_prices, close_prices = np.array([float(k[2]) for k in klines]), np.array([float(k[3]) for k in klines]), np.array([float(k[4]) for k in klines])
                ath, atl, current_price = np.max(high_prices), np.min(low_prices), close_prices[-1]
                if ath==0 or current_price==0: continue
                correction_percent = ((current_price-ath)/ath)*100
                if correction_percent > GEM_MIN_CORRECTION_PERCENT: continue
                rise_from_atl = ((current_price-atl)/atl)*100 if atl > 0 else float('inf')
                if rise_from_atl < GEM_MIN_RISE_FROM_ATL_PERCENT: continue
                if len(close_prices) >= 20 and current_price < np.mean(close_prices[-20:]): continue
                final_gems.append({'symbol':symbol,'potential_x':ath/current_price,'correction_percent':correction_percent})
            except Exception as e: logger.warning(f"Could not process gem candidate {symbol}: {e}"); continue
        if not final_gems: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **بحث الجواهر اكتمل:** لم يتم العثور على عملات تتوافق مع الشروط."); return
        sorted_gems = sorted(final_gems, key=lambda x: x['potential_x'], reverse=True)
        message = f"💎 **تقرير الجواهر المخفية ({client.name})** 💎\n\n"
        for gem in sorted_gems[:5]:
            message += f"**${gem['symbol'].replace('USDT','')}**\n  - 🚀 **العودة للقمة: {gem['potential_x']:.1f}X**\n  - 🩸 مصححة: {gem['correction_percent']:.1f}%\n  - 📈 الحالة: بوادر تعافي\n\n"
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in run_gem_hunter_scan: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ فادح أثناء البحث.")

# =============================================================================
# --- 5. المهام الآلية الدورية ---
# =============================================================================
def add_to_monitoring(symbol, alert_price, peak_volume, alert_time, source, exchange_name):
    if exchange_name not in PLATFORMS: return
    if symbol not in active_hunts[exchange_name]: active_hunts[exchange_name][symbol] = {'alert_price': alert_price, 'alert_time': alert_time}
    if symbol not in performance_tracker[exchange_name]:
        performance_tracker[exchange_name][symbol] = {'alert_price': alert_price,'alert_time': alert_time,'source': source,'current_price': alert_price,'high_price': alert_price,'status': 'Tracking','momentum_lost_alerted': False}
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol} on {exchange_name}")

async def fomo_hunter_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    if not client: return
    while True:
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)
        if not bot_data.get('background_tasks_enabled', True): continue
        logger.info(f"===== Fomo Hunter ({client.name}): Starting Scan =====")
        try:
            momentum_coins = await helper_get_momentum_symbols(client)
            if not momentum_coins: continue
            now = datetime.now(UTC)
            new_alerts = [data for symbol, data in momentum_coins.items() if not recently_alerted_fomo[client.name].get(symbol) or (now - recently_alerted_fomo[client.name][symbol]) > timedelta(minutes=RUN_FOMO_SCAN_EVERY_MINUTES*4)]
            if not new_alerts: continue
            for alert in new_alerts: recently_alerted_fomo[client.name][alert['symbol']] = now
            sorted_coins = sorted(new_alerts, key=lambda x: x['price_change'], reverse=True)
            message = f"🚨 **تنبيه تلقائي من صياد الفومو ({client.name})** 🚨\n\n"
            for i, coin in enumerate(sorted_coins[:5]):
                message += f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n    - السعر: `${format_price(coin['current_price'])}`\n    - **الزخم: `%{coin['price_change']:+.2f}`**\n\n"
            await broadcast_message(bot, message)
            for coin in sorted_coins[:5]: add_to_monitoring(coin['symbol'], float(coin['current_price']), 0, now, f"صياد الفومو ({client.name})", client.name)
        except Exception as e: logger.error(f"Error in fomo_hunter_loop for {client.name}: {e}", exc_info=True)

async def new_listings_sniper_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    if not client: return
    initial_data = await client.get_market_data()
    if initial_data: known_symbols[client.name] = {s['symbol'] for s in initial_data}
    while True:
        await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS)
        if not bot_data.get('background_tasks_enabled', True): continue
        try:
            data = await client.get_market_data()
            if not data: continue
            current_symbols = {s['symbol'] for s in data}
            if not known_symbols[client.name]: known_symbols[client.name] = current_symbols; continue
            newly_listed = current_symbols - known_symbols[client.name]
            if newly_listed:
                for symbol in newly_listed:
                    logger.info(f"Sniper ({client.name}): NEW LISTING: {symbol}")
                    await broadcast_message(bot, f"🎯 **إدراج جديد على {client.name}:** `${symbol}`")
                known_symbols[client.name].update(newly_listed)
        except Exception as e: logger.error(f"Error in new_listings_sniper_loop for {client.name}: {e}")

async def performance_tracker_loop(session: aiohttp.ClientSession, bot: Bot):
    while True:
        await asyncio.sleep(RUN_PERFORMANCE_TRACKER_EVERY_MINUTES * 60)
        now = datetime.now(UTC)
        for platform in PLATFORMS:
            client = get_exchange_client(platform, session)
            if not client: continue
            for symbol, data in list(performance_tracker[platform].items()):
                if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                    if performance_tracker[platform].get(symbol): del performance_tracker[platform][symbol]
                    continue
                try:
                    current_price = await client.get_current_price(symbol)
                    if not current_price: continue
                    tracker = performance_tracker[platform][symbol]
                    tracker['current_price'] = current_price
                    if current_price > tracker.get('high_price', 0): tracker['high_price'] = current_price
                    if "الزخم" in data.get('source','') or "الفومو" in data.get('source',''):
                        if not tracker.get('momentum_lost_alerted',False) and tracker['high_price'] > 0:
                            drop = ((current_price - tracker['high_price'])/tracker['high_price'])*100
                            if drop <= -5.0:
                                await broadcast_message(bot, f"⚠️ **فقدان الزخم لـ ${symbol.replace('USDT','')}** ({platform})\n\n    - الأعلى: `${format_price(tracker['high_price'])}`\n    - الحالي: `${format_price(current_price)}` (**{drop:.2f}%**)")
                                tracker['momentum_lost_alerted'] = True
                except Exception as e: logger.error(f"Error updating price for {symbol} on {platform}: {e}")

            for symbol, data in list(sniper_tracker[platform].items()):
                if data['status'] != 'Tracking': continue
                if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                    if sniper_tracker[platform].get(symbol): del sniper_tracker[platform][symbol]
                    continue
                try:
                    current_price = await client.get_current_price(symbol)
                    if not current_price: continue
                    if current_price >= data['target_price']:
                        await broadcast_message(bot, f"✅ **القناص: نجاح!** ✅\n\n**العملة:** `${symbol.replace('USDT','')}` ({platform})\n**النتيجة:** وصلت للهدف `{format_price(data['target_price'])}`.")
                        del sniper_tracker[platform][symbol]
                    elif current_price <= data['invalidation_price']:
                        await broadcast_message(bot, f"❌ **القناص: فشل.** ❌\n\n**العملة:** `${symbol.replace('USDT','')}` ({platform})\n**النتيجة:** فشل الاختراق وعاد السعر تحت `{format_price(data['invalidation_price'])}`.")
                        del sniper_tracker[platform][symbol]
                except Exception as e: logger.error(f"Error in Sniper Tracker for {symbol} on {platform}: {e}")

async def coiled_spring_radar_loop(client: BaseExchangeClient, bot_data: dict):
    if not client: return
    logger.info(f"Sniper Radar (v28) started for {client.name}.")
    while True:
        await asyncio.sleep(SNIPER_RADAR_RUN_EVERY_MINUTES * 60)
        if not bot_data.get('background_tasks_enabled', True): continue
        logger.info(f"===== Sniper Radar ({client.name}): Searching for coiled springs =====")
        try:
            market_data = await client.get_market_data()
            if not market_data: continue
            
            candidates = [p for p in market_data if float(p.get('quoteVolume', '0')) > SNIPER_MIN_USDT_VOLUME and p['symbol'] not in SNIPER_BLACKLIST]

            async def check_candidate(symbol):
                klines = await client.get_processed_klines(symbol, '15m', int(SNIPER_COMPRESSION_PERIOD_HOURS * 4))
                if not klines or len(klines) < int(SNIPER_COMPRESSION_PERIOD_HOURS * 4): return

                high_prices = np.array([float(k[2]) for k in klines])
                low_prices = np.array([float(k[3]) for k in klines])
                highest_high, lowest_low = np.max(high_prices), np.min(low_prices)

                if lowest_low == 0: return
                volatility = ((highest_high - lowest_low) / lowest_low) * 100
                
                if not (SNIPER_MIN_VOLATILITY_PERCENT < volatility <= SNIPER_MAX_VOLATILITY_PERCENT):
                    return

                poc = calculate_poc(klines)
                if not poc: return

                if symbol not in sniper_watchlist[client.name]:
                    sniper_watchlist[client.name][symbol] = {
                        'high': highest_high, 'low': lowest_low, 'poc': poc,
                        'avg_volume': np.mean(np.array([float(k[5]) for k in klines])),
                        'duration_hours': SNIPER_COMPRESSION_PERIOD_HOURS
                    }
                    logger.info(f"SNIPER RADAR ({client.name}): Added {symbol} to watchlist. Volatility: {volatility:.2f}%")

            tasks = [check_candidate(p['symbol']) for p in candidates]
            await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"Error in coiled_spring_radar_loop for {client.name}: {e}", exc_info=True)

async def breakout_trigger_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    if not client: return
    logger.info(f"Sniper Trigger (v28 - Professional) started for {client.name}.")
    while True:
        await asyncio.sleep(SNIPER_TRIGGER_RUN_EVERY_SECONDS)
        if not bot_data.get('background_tasks_enabled', True): continue

        watchlist_copy = list(sniper_watchlist[client.name].items())
        if not watchlist_copy: continue

        for symbol, data in watchlist_copy:
            try:
                klines_5m = await client.get_processed_klines(symbol, '5m', 5)
                if not klines_5m or len(klines_5m) < 3: continue
                
                confirmation_candle, trigger_candle = klines_5m[-2], klines_5m[-3]
                breakout_level = data['high']
                
                if float(trigger_candle[2]) < breakout_level: continue
                
                confirmation_close = float(confirmation_candle[4])
                if confirmation_close < breakout_level: continue

                klines_1d = await client.get_processed_klines(symbol, '1d', 20)
                if not klines_1d or len(klines_1d) < 20: continue
                
                close_prices_1d = np.array([float(k[4]) for k in klines_1d])
                ema20_1d = np.mean(close_prices_1d[-20:])
                
                if confirmation_close < ema20_1d:
                    continue

                avg_volume_5m = data['avg_volume'] / 3
                is_breakout_volume = (float(trigger_candle[5]) > avg_volume_5m * SNIPER_BREAKOUT_VOLUME_MULTIPLIER or
                                      float(confirmation_candle[5]) > avg_volume_5m * SNIPER_BREAKOUT_VOLUME_MULTIPLIER)

                if not is_breakout_volume: continue
                
                alert_price = confirmation_close
                invalidation_price = breakout_level
                target_price = data['high'] + (data['high'] - data['low'])

                message = (
                    f"🎯 **قناص (إشارة احترافية): اختراق مؤكد!** 🎯\n\n"
                    f"**العملة:** `${symbol.replace('USDT', '')}` ({client.name})\n"
                    f"**النمط:** اختراق نطاق تجميعي + شمعة تأكيد + **اتجاه عام صاعد.**\n"
                    f"**سعر التأكيد:** `{format_price(alert_price)}`\n\n"
                    f"📝 **خطة المراقبة:**\n"
                    f"- **يفشل الاختراق بالإغلاق تحت:** `{format_price(invalidation_price)}`\n"
                    f"- **هدف أولي محتمل:** `{format_price(target_price)}`\n\n"
                    f"*(إشارة عالية الجودة تمت فلترتها. راقب الخطة جيداً)*"
                )
                await broadcast_message(bot, message)
                logger.info(f"SNIPER TRIGGER (PROFESSIONAL) ({client.name}): Confirmed breakout for {symbol}!")

                sniper_tracker[client.name][symbol] = {
                    'alert_time': datetime.now(UTC), 'target_price': target_price,
                    'invalidation_price': invalidation_price, 'status': 'Tracking' 
                }
                
                if symbol in sniper_watchlist[client.name]:
                    del sniper_watchlist[client.name][symbol]

            except Exception as e:
                 logger.error(f"Error in breakout_trigger_loop for {symbol} on {client.name}: {e}", exc_info=True)
                 if symbol in sniper_watchlist[client.name]:
                     del sniper_watchlist[client.name][symbol]

# =============================================================================
# --- 6. تشغيل البوت ---
# =============================================================================
async def send_startup_message(bot: Bot):
    try:
        message = "✅ **بوت الصياد الذكي (v28.0 - القناص الاحترافي) متصل الآن!**\n\nأرسل /start لعرض القائمة."
        await broadcast_message(bot, message)
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

async def post_init(application: Application):
    logger.info("Bot initialized. Starting background tasks...")
    session = aiohttp.ClientSession()
    application.bot_data["session"] = session
    bot_instance, bot_data = application.bot, application.bot_data

    application.bot_data['task_performance'] = asyncio.create_task(performance_tracker_loop(session, bot_instance))
    for platform_name in PLATFORMS:
        client = get_exchange_client(platform_name.lower(), session)
        if client:
            application.bot_data[f'task_fomo_{platform_name}'] = asyncio.create_task(fomo_hunter_loop(client, bot_instance, bot_data))
            application.bot_data[f'task_listings_{platform_name}'] = asyncio.create_task(new_listings_sniper_loop(client, bot_instance, bot_data))
            application.bot_data[f'task_sniper_radar_{platform_name}'] = asyncio.create_task(coiled_spring_radar_loop(client, bot_data))
            application.bot_data[f'task_sniper_trigger_{platform_name}'] = asyncio.create_task(breakout_trigger_loop(client, bot_instance, bot_data))

    await send_startup_message(bot_instance)

def main() -> None:
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN:
        logger.critical("FATAL ERROR: Bot token is not set.")
        return
    setup_database()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.bot_data['background_tasks_enabled'] = True
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.post_init = post_init
    logger.info("Telegram bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()

