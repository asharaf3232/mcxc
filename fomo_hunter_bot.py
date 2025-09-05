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
# تم دمج هذا الجزء هنا مباشرة
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
DATABASE_FILE = "users.db" # اسم ملف قاعدة البيانات

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

# --- إعدادات وحدة القناص (Sniper Module) ---
SNIPER_RADAR_RUN_EVERY_MINUTES = 30
SNIPER_TRIGGER_RUN_EVERY_SECONDS = 60
SNIPER_COMPRESSION_PERIOD_HOURS = 8
SNIPER_MAX_VOLATILITY_PERCENT = 8.0
SNIPER_BREAKOUT_VOLUME_MULTIPLIER = 4.0
SNIPER_MIN_USDT_VOLUME = 200000

# --- إعدادات صائد الجواهر (Gem Hunter Settings) ---
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
SCALP_KLINE_LIMIT = 50
PRO_SCAN_MIN_SCALP_SCORE = 3
# الحد الأدنى من النقاط لإظهار العملة في الفحص الاحترافي
PRO_SCAN_MIN_SCORE = 5 

# --- إعدادات عامة ---
HTTP_TIMEOUT = 15
API_CONCURRENCY_LIMIT = 8
TELEGRAM_MESSAGE_LIMIT = 4096

# --- إعدادات تسجيل الأخطاء ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة ---
# =============================================================================
api_semaphore = asyncio.Semaphore(API_CONCURRENCY_LIMIT)
PLATFORMS = ["MEXC", "Gate.io", "Binance", "Bybit", "KuCoin", "OKX"]
performance_tracker = {p: {} for p in PLATFORMS}
active_hunts = {p: {} for p in PLATFORMS}
known_symbols = {p: set() for p in PLATFORMS}
recently_alerted_fomo = {p: {} for p in PLATFORMS}
sniper_watchlist = {p: {} for p in PLATFORMS}
# --- الإضافة الجديدة: متتبع نتائج القناص ---
sniper_tracker = {p: {} for p in PLATFORMS}


# =============================================================================
# --- قسم إدارة المستخدمين (باستخدام SQLite) ---
# =============================================================================
def setup_database():
    """إنشاء قاعدة البيانات والجدول إذا لم يكونا موجودين"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    logger.info("Database is set up and ready.")

def load_user_ids():
    """تحميل قائمة معرفات المستخدمين من قاعدة البيانات"""
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
    """حفظ معرف مستخدم جديد في قاعدة البيانات"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        conn.close()
        logger.info(f"User with chat_id: {chat_id} has been saved or already exists.")
    except sqlite3.Error as e:
        logger.error(f"Database error while saving user {chat_id}: {e}")

def remove_user_id(chat_id):
    """إزالة معرف مستخدم من قاعدة البيانات"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        conn.commit()
        conn.close()
        logger.warning(f"User {chat_id} has been removed from the database.")
    except sqlite3.Error as e:
        logger.error(f"Database error while removing user {chat_id}: {e}")

async def broadcast_message(bot: Bot, message_text: str, parse_mode=ParseMode.MARKDOWN):
    """إرسال رسالة إلى جميع المستخدمين المسجلين"""
    user_ids = load_user_ids()
    if not user_ids:
        logger.warning("Broadcast requested, but no users are registered.")
        return

    for user_id in user_ids:
        try:
            await bot.send_message(chat_id=user_id, text=message_text, parse_mode=parse_mode)
        except Forbidden:
            remove_user_id(user_id)
        except BadRequest as e:
            logger.error(f"Failed to send message to {user_id}: {e}")
            if "chat not found" in str(e):
                remove_user_id(user_id)
        except Exception as e:
            logger.error(f"An unexpected error occurred while sending to {user_id}: {e}")

# =============================================================================
# --- قسم الشبكة والوظائف الأساسية (مشتركة) ---
# =============================================================================
async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None, headers: dict = None, retries: int = 3):
    request_headers = {'User-Agent': 'Mozilla/5.0'}
    if headers: request_headers.update(headers)

    for attempt in range(retries):
        try:
            async with session.get(url, params=params, timeout=HTTP_TIMEOUT, headers=request_headers) as response:
                if response.status == 429:
                    wait_time = 2 ** (attempt + 1)
                    logger.warning(f"Rate limit hit (429) for {url}. Retrying after {wait_time}s...")
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
        if price < 1e-4:
            return f"{price:.10f}".rstrip('0')
        return f"{price:.8g}"
    except (ValueError, TypeError): return price_str

# =============================================================================
# --- ⚙️ قسم عملاء المنصات (Exchange Clients) ⚙️ ---
# =============================================================================
class BaseExchangeClient:
    def __init__(self, session, api_key=None, api_secret=None):
        self.session = session
        self.name = "Base"
    async def get_market_data(self): raise NotImplementedError
    async def get_klines(self, symbol, interval, limit): raise NotImplementedError
    async def get_order_book(self, symbol, limit=20): raise NotImplementedError
    async def get_current_price(self, symbol): raise NotImplementedError

    async def get_processed_klines(self, symbol, interval, limit):
        klines = await self.get_klines(symbol, interval, limit)
        if not klines: return None
        klines.sort(key=lambda x: int(x[0]))
        return klines

class MexcClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name = "MEXC"
        self.base_api_url = "https://api.mexc.com"
        self.interval_map = {'1m': '1m', '5m': '5m', '15m': '15m', '1h': '1h', '4h': '4h', '1d': '1d'}

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        return [{'symbol': i['symbol'], 'quoteVolume': i.get('quoteVolume') or '0', 'lastPrice': i.get('lastPrice') or '0', 'priceChangePercent': float(i.get('priceChangePercent','0'))*100} for i in data if i.get('symbol','').endswith("USDT")]

    async def get_klines(self, symbol, interval, limit):
        api_interval = self.interval_map.get(interval, interval)
        async with api_semaphore:
            params = {'symbol': symbol, 'interval': api_interval, 'limit': limit}
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
        self.interval_map = {'1m': '1m', '5m': '5m', '15m': '15m', '1h': '1h', '4h': '4h', '1d': '1d'}

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/spot/tickers")
        if not data: return []
        return [{'symbol': i['currency_pair'].replace('_',''), 'quoteVolume': i.get('quote_volume') or '0', 'lastPrice': i.get('last') or '0', 'priceChangePercent': float(i.get('change_percentage','0'))} for i in data if i.get('currency_pair','').endswith("_USDT")]

    async def get_klines(self, symbol, interval, limit):
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"
        api_interval = self.interval_map.get(interval, interval)
        async with api_semaphore:
            params = {'currency_pair': gateio_symbol, 'interval': api_interval, 'limit': limit}
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
        self.interval_map = {'1m': '1m', '5m': '5m', '15m': '15m', '1h': '1h', '4h': '4h', '1d': '1d'}

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        return [{'symbol': i['symbol'], 'quoteVolume': i.get('quoteVolume') or '0', 'lastPrice': i.get('lastPrice') or '0', 'priceChangePercent': float(i.get('priceChangePercent','0'))} for i in data if i.get('symbol','').endswith("USDT")]

    async def get_klines(self, symbol, interval, limit):
        api_interval = self.interval_map.get(interval, interval)
        async with api_semaphore:
            params = {'symbol': symbol, 'interval': api_interval, 'limit': limit}
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
        self.interval_map = {'1m': '1', '5m': '5', '15m': '15', '1h': '60', '4h': '240', '1d': 'D'}

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/tickers", params={'category': 'spot'})
        if not data or not data.get('result') or not data['result'].get('list'): return []
        return [{'symbol': i['symbol'], 'quoteVolume': i.get('turnover24h') or '0', 'lastPrice': i.get('lastPrice') or '0', 'priceChangePercent': float(i.get('price24hPcnt','0'))*100} for i in data['result']['list'] if i['symbol'].endswith("USDT")]

    async def get_klines(self, symbol, interval, limit):
        async with api_semaphore:
            api_interval = self.interval_map.get(interval, '5')
            params = {'category': 'spot', 'symbol': symbol, 'interval': api_interval, 'limit': limit}
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/kline", params=params)
            if not data or not data.get('result') or not data['result'].get('list'): return None
            return [[int(k[0]), k[1], k[2], k[3], k[4], k[5]] for k in data['result']['list']]

    async def get_order_book(self, symbol, limit=20):
        async with api_semaphore:
            params = {'category': 'spot', 'symbol': symbol, 'limit': limit}
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/orderbook", params)
            if not data or not data.get('result'): return None
            return {'bids': data['result'].get('bids', []), 'asks': data['result'].get('asks', [])}

    async def get_current_price(self, symbol: str) -> float | None:
        data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/tickers", params={'category': 'spot', 'symbol': symbol})
        if not data or not data.get('result') or not data['result'].get('list'): return None
        return float(data['result']['list'][0]['lastPrice'])

class KucoinClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs)
        self.name = "KuCoin"
        self.base_api_url = "https://api.kucoin.com"
        self.interval_map = {'1m':'1min', '5m':'5min', '15m':'15min', '1h': '1hour', '4h': '4hour', '1d': '1day'}

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/allTickers")
        if not data or not data.get('data') or not data['data'].get('ticker'): return []
        return [{'symbol': i['symbol'].replace('-',''), 'quoteVolume': i.get('volValue') or '0', 'lastPrice': i.get('last') or '0', 'priceChangePercent': float(i.get('changeRate','0'))*100} for i in data['data']['ticker'] if i.get('symbol','').endswith("-USDT")]

    async def get_klines(self, symbol, interval, limit):
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        api_interval = self.interval_map.get(interval, '5min')
        async with api_semaphore:
            params = {'symbol': kucoin_symbol, 'type': api_interval}
            await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/candles", params=params)
            if not data or not data.get('data'): return None
            return [[int(k[0])*1000, k[2], k[3], k[4], k[1], k[5]] for k in data['data']]

    async def get_order_book(self, symbol, limit=20):
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        async with api_semaphore:
            params = {'symbol': kucoin_symbol}
            await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/orderbook/level2_20", params)

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
        self.interval_map = {'1m': '1m', '5m': '5m', '15m': '15m', '1h': '1H', '4h': '4H', '1d': '1D'}

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/tickers", params={'instType': 'SPOT'})
        if not data or not data.get('data'): return []
        results = []
        for i in data['data']:
            if i.get('instId','').endswith("-USDT"):
                try:
                    lp, op = float(i.get('last') or '0'), float(i.get('open24h') or '0')
                    cp = ((lp-op)/op)*100 if op > 0 else 0.0
                    results.append({'symbol': i['instId'].replace('-',''), 'quoteVolume': i.get('volCcy24h') or '0', 'lastPrice': i.get('last') or '0', 'priceChangePercent': cp})
                except (ValueError, TypeError): continue
        return results

    async def get_klines(self, symbol, interval, limit):
        api_interval = self.interval_map.get(interval, '5m')
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        async with api_semaphore:
            params = {'instId': okx_symbol, 'bar': api_interval, 'limit': limit}
            await asyncio.sleep(0.25)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/candles", params=params)
            if not data or not data.get('data'): return None
            return [[int(k[0]), k[1], k[2], k[3], k[4], k[5]] for k in data['data']]

    async def get_order_book(self, symbol, limit=20):
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        async with api_semaphore:
            params = {'instId': okx_symbol, 'sz': str(limit)}
            await asyncio.sleep(0.25)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/books", params)
            if not data or not data.get('data'): return None
            book_data = data['data'][0]
            return {'bids': book_data.get('bids',[]), 'asks': book_data.get('asks',[])}

    async def get_current_price(self, symbol: str) -> float | None:
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/tickers", params={'instId': okx_symbol})
        if not data or not data.get('data'): return None
        return float(data['data'][0]['last'])

def get_exchange_client(exchange_name, session):
    clients = {'mexc': MexcClient, 'gate.io': GateioClient, 'binance': BinanceClient, 'bybit': BybitClient, 'kucoin': KucoinClient, 'okx': OkxClient}
    client_class = clients.get(exchange_name.lower())
    return client_class(session) if client_class else None

# =============================================================================
# --- 🔬 قسم التحليل الفني (TA Section) 🔬 ---
# =============================================================================
def calculate_poc(klines, num_bins=50):
    """
    يحسب نقطة التحكم (POC) من بيانات الشموع.
    POC هو مستوى السعر الذي حظي بأعلى حجم تداول.
    """
    if not klines or len(klines) < 10:
        return None

    try:
        high_prices = np.array([float(k[2]) for k in klines])
        low_prices = np.array([float(k[3]) for k in klines])
        volumes = np.array([float(k[5]) for k in klines])

        min_price = np.min(low_prices)
        max_price = np.max(high_prices)

        if max_price == min_price:
            return min_price

        price_bins = np.linspace(min_price, max_price, num_bins)
        volume_per_bin = np.zeros(num_bins)

        # توزيع حجم التداول على مستويات الأسعار
        for i in range(len(klines)):
            # نستخدم متوسط السعر للتقريب
            avg_price = (high_prices[i] + low_prices[i]) / 2
            bin_index = np.searchsorted(price_bins, avg_price) -1
            if 0 <= bin_index < num_bins:
                volume_per_bin[bin_index] += volumes[i]

        # العثور على البين ذو الحجم الأعلى
        if np.sum(volume_per_bin) == 0: return None # لا يوجد حجم تداول
        poc_index = np.argmax(volume_per_bin)
        poc_price = price_bins[poc_index]

        return poc_price
    except Exception as e:
        logger.error(f"Error calculating POC: {e}")
        return None

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
        if high_prices[i] == max(high_prices[i-window:i+window+1]):
            resistances.append(high_prices[i])
        if low_prices[i] == min(low_prices[i-window:i+window+1]):
            supports.append(low_prices[i])
    return sorted(list(set(supports)), reverse=True), sorted(list(set(resistances)), reverse=True)

def calculate_fibonacci_retracement(high_prices, low_prices, period=FIBONACCI_PERIOD):
    if len(high_prices) < period:
        recent_highs = high_prices
        recent_lows = low_prices
    else:
        recent_highs = high_prices[-period:]
        recent_lows = low_prices[-period:]

    max_price = np.max(recent_highs)
    min_price = np.min(recent_lows)
    difference = max_price - min_price
    if difference == 0: return {}

    levels = {
        'level_0.382': max_price - (difference * 0.382),
        'level_0.5': max_price - (difference * 0.5),
        'level_0.618': max_price - (difference * 0.618),
    }
    return levels

def analyze_trend(current_price, ema21, ema50, sma100):
    if ema21 and ema50 and sma100:
        if current_price > ema21 > ema50 > sma100:
            return "🟢 اتجاه صاعد قوي.", 2
        if current_price > ema50 and current_price > ema21:
            return "🟢 اتجاه صاعد.", 1
        if current_price < ema21 < ema50 < sma100:
            return "🔴 اتجاه هابط قوي.", -2
        if current_price < ema50 and current_price < ema21:
             return "🔴 اتجاه هابط.", -1
    return "🟡 جانبي / غير واضح.", 0

# =============================================================================
# --- الوظائف المساعدة للتحليل ---
# =============================================================================
async def helper_get_momentum_symbols(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {}
    potential_coins = [p for p in market_data if float(p.get('lastPrice','1')) <= MOMENTUM_MAX_PRICE and MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= MOMENTUM_MAX_VOLUME_24H]
    if not potential_coins: return {}
    tasks = [client.get_processed_klines(p['symbol'], MOMENTUM_KLINE_INTERVAL, MOMENTUM_KLINE_LIMIT) for p in potential_coins]
    all_klines_data = await asyncio.gather(*tasks)
    momentum_coins_data = {}
    for i, klines in enumerate(all_klines_data):
        if not klines or len(klines) < MOMENTUM_KLINE_LIMIT: continue
        try:
            klines = klines[-MOMENTUM_KLINE_LIMIT:]
            sp = MOMENTUM_KLINE_LIMIT // 2
            old_v = sum(float(k[5]) for k in klines[:sp]); new_v = sum(float(k[5]) for k in klines[sp:])
            start_p = float(klines[sp][1])
            if old_v == 0 or start_p == 0: continue
            end_p = float(klines[-1][4])
            price_change = ((end_p - start_p) / start_p) * 100
            if new_v > old_v * MOMENTUM_VOLUME_INCREASE and price_change > MOMENTUM_PRICE_INCREASE:
                coin_symbol = potential_coins[i]['symbol']
                momentum_coins_data[coin_symbol] = {'symbol': coin_symbol, 'price_change': price_change, 'current_price': end_p, 'peak_volume': new_v}
        except (ValueError, IndexError, TypeError): continue
    return momentum_coins_data

async def helper_get_whale_activity(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {}
    potential_gems = [p for p in market_data if float(p.get('lastPrice','999')) <= WHALE_GEM_MAX_PRICE and WHALE_GEM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= WHALE_GEM_MAX_VOLUME_24H]
    if not potential_gems: return {}
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
    signals = []
    if not book or not book.get('bids') or not book.get('asks'): return signals
    try:
        bids = sorted([(float(item[0]), float(item[1])) for item in book['bids'] if len(item) >= 2], key=lambda x: x[0], reverse=True)
        asks = sorted([(float(item[0]), float(item[1])) for item in book['asks'] if len(item) >= 2], key=lambda x: x[0])
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

async def helper_get_scalp_score(client: BaseExchangeClient, symbol: str) -> int:
    overall_score = 0
    timeframes = {'15m': 2, '5m': 1} 

    for tf_interval, weight in timeframes.items():
        klines = await client.get_processed_klines(symbol, tf_interval, SCALP_KLINE_LIMIT)
        if not klines or len(klines) < 20: continue

        volumes = np.array([float(k[5]) for k in klines])
        close_prices = np.array([float(k[4]) for k in klines])

        avg_volume = np.mean(volumes[-20:-1])
        last_volume = volumes[-1]

        if avg_volume > 0 and last_volume > avg_volume * 1.5:
            overall_score += 1 * weight

        if len(close_prices) >= 5:
            price_change_5_candles = ((close_prices[-1] - close_prices[-5]) / close_prices[-5]) * 100 if close_prices[-5] > 0 else 0
            if price_change_5_candles > 2.0:
                 overall_score += 1 * weight
    return overall_score

# =============================================================================
# --- 4. الوظائف التفاعلية (أوامر البوت) ---
# =============================================================================
BTN_TA_PRO = "🔬 محلل فني"
BTN_SCALP_SCAN = "⚡️ تحليل سريع"
BTN_PRO_SCAN = "🎯 فحص احترافي"
BTN_SNIPER_LIST = "🔭 قائمة القنص"
BTN_GEM_HUNTER = "💎 صائد الجواهر"
BTN_WHALE_RADAR = "🐋 رادار الحيتان"
BTN_MOMENTUM = "🚀 كاشف الزخم"
BTN_STATUS = "📊 الحالة"
BTN_PERFORMANCE = "📈 تقرير الأداء"
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
BTN_ABOUT = "ℹ️ عن البوت"

def build_menu(context: ContextTypes.DEFAULT_TYPE):
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
        [BTN_PRO_SCAN, BTN_MOMENTUM, BTN_WHALE_RADAR],
        [BTN_TA_PRO, BTN_GEM_HUNTER, BTN_SNIPER_LIST],
        [BTN_TOP_GAINERS, BTN_TOP_VOLUME, BTN_TOP_LOSERS],
        [BTN_PERFORMANCE, BTN_STATUS, BTN_ABOUT],
        [toggle_tasks_btn],
        [mexc_btn, gate_btn, binance_btn],
        [bybit_btn, kucoin_btn, okx_btn]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        save_user_id(update.message.chat_id)

    context.user_data['exchange'] = 'mexc'
    context.bot_data.setdefault('background_tasks_enabled', True)
    welcome_message = (
        "أهلاً بك في بوت الصياد الذكي!\n\n"
        "أنا لست مجرد بوت تنبيهات، أنا مساعدك الاستراتيجي في عالم العملات الرقمية. مهمتي هي أن أمنحك ميزة على بقية السوق عبر ثلاث قدرات أساسية:\n\n"
        "**🎯 1. أقتنص الفرص قبل الجميع (وحدة القناص):**\n"
        "أراقب السوق بصمت وأبحث عن العملات التي تستعد للانفجار (الاختراق)، وأرسل لك تنبيهاً في لحظة الانطلاق المحتملة، مما يمنحك فرصة \"زيرو انعكاس\".\n\n"
        "**🚀 2. أرصد الزخم الحالي (الكواشف):**\n"
        "أخبرك بالعملات التي تشهد زخماً قوياً، أو نشاط حيتان، أو أنماطاً متكررة الآن، لتكون على دراية كاملة بما يحدث في السوق لحظة بلحظة.\n\n"
        "**🔬 3. أحلل أي عملة تطلبها (المحلل الفني):**\n"
        "أرسل لي رمز أي عملة (مثل BTC)، وسأقدم لك تقريراً فنياً مفصلاً عنها على عدة أطر زمنية في ثوانٍ, لمساعدتك في اتخاذ قراراتك.\n\n"
        "**كيف تبدأ؟**\n"
        "استخدم لوحة الأزرار أدناه لاستكشاف السوق والبدء في الصيد.\n\n"
        "⚠️ **تنبيه هام:** أنا أداة للمساعدة والتحليل فقط، ولست مستشاراً مالياً. التداول ينطوي على مخاطر عالية، وقراراتك تقع على عاتقك بالكامل."
    )
    if update.message:
        await update.message.reply_text(welcome_message, reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

async def set_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE, exchange_name: str):
    context.user_data['exchange'] = exchange_name.lower()
    await update.message.reply_text(f"✅ تم تحويل المنصة بنجاح. المنصة النشطة الآن هي: **{exchange_name}**", reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

async def toggle_background_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    context.bot_data['background_tasks_enabled'] = not tasks_enabled
    status = "تفعيل" if not tasks_enabled else "إيقاف"
    await update.message.reply_text(f"✅ تم **{status}** المهام الخلفية.", reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    about_text = (
        "ℹ️ **دليل المستخدم الشامل لبوت الصياد الذكي** ℹ️\n\n"
        "---\n\n"
        "### **الفلسفة العامة: كيف يفكر البوت؟**\n\n"
        "أهلاً بك في غرفة عمليات الصياد الذكي. من المهم أن تفهم أن هذا البوت ليس مجرد \"ناقل\" للتوصيات من قنوات أو خبراء. هو **محلل فني آلي مستقل**، وعقله هو الكود الذي يعمل به. مهمته ليست إعطاءك \"توصيات مضمونة\"، بل تزويدك **بأفضلية استراتيجية** عبر تحليل بيانات السوق الحية بشكل أسرع وأوسع من أي إنسان.\n\n"
        "يعمل البوت على ثلاثة محاور رئيسية:\n"
        "1.  **الصيد الاستباقي (Proactive Hunting):** يبحث 24/7 عن أنماط وفرص محددة مسبقاً ويرسل لك تنبيهات تلقائية عند العثور عليها (مثل تنبيهات القناص).\n"
        "2.  **المسح عند الطلب (On-Demand Scanning):** يمنحك أدوات قوية لفحص السوق في أي لحظة تريدها بناءً على استراتيجيات مختلفة (مثل صائد الجواهر والفحص الاحترافي).\n"
        "3.  **التحليل الفردي (Individual Analysis):** يسمح لك بطلب تحليل فني عميق لأي عملة تختارها في ثوانٍ.\n\n"
        "---\n\n"
        "### **شرح أدواتك: وظيفة كل زر في القائمة**\n\n"
        "#### **[ الصف الأول: أدوات البحث عن الفرص ]**\n\n"
        "* **🎯 فحص احترافي (Pro Scan):**\n"
        "    * **الغرض:** يجيب على سؤال: \"ما هي أقوى الفرص الواعدة في السوق *الآن*؟\"\n"
        "    * **الوظيفة:** عند الضغط عليه، يقوم البوت بمسح عشرات العملات المرشحة ويمنح كل واحدة \"نقاط قوة\" بناءً على عدة مؤشرات فنية (مثل قوة الاتجاه، الزخم، سيطرة المشترين، والتقلبات).\n"
        "    * **النتيجة:** يعرض لك قائمة بأفضل العملات التي حصلت على أعلى نقاط. عملة ذات نقاط عالية (6+) تعتبر فرصة قوية للدراسة الفورية.\n\n"
        "* **🚀 كاشف الزخم (Momentum Detector):**\n"
        "    * **الغرض:** يجيب على سؤال: \"ما هي العملات الأكثر سخونة الآن والتي تشهد ضخاً سعرياً مع فوليوم؟\"\n"
        "    * **الوظيفة:** يبحث عن العملات منخفضة السعر التي شهدت ارتفاعاً كبيراً في السعر وحجماً تداولياً متزايداً خلال آخر 30-60 دقيقة.\n"
        "    * **النتيجة:** قائمة بالعملات التي تشهد \"FOMO\" حالياً. مناسبة للمضاربة السريعة ولكنها تتطلب حذراً.\n\n"
        "* **🐋 رادار الحيتان (Whale Radar):**\n"
        "    * **الغرض:** يجيب على سؤال: \"أين يضع كبار اللاعبين (الحيتان) أموالهم الآن؟\"\n"
        "    * **الوظيفة:** يمسح سجل الأوامر (Order Book) للعملات منخفضة السعر بحثاً عن جدران شراء أو بيع ضخمة، أو ضغط شرائي/بيعي غير طبيعي.\n"
        "    * **النتيجة:** تقرير عن أماكن تمركز الحيتان، مما يعطيك فكرة عن مستويات الدعم والمقاومة القوية جداً.\n\n"
        "#### **[ الصف الثاني: الأدوات الاستراتيجية والتحليلية ]**\n\n"
        "* **🔬 محلل فني (Technical Analyst):**\n"
        "    * **الغرض:** يجيب على سؤال: \"ما هو الوضع الفني لعملة معينة؟\"\n"
        "    * **الوظيفة:** بعد الضغط عليه، تـُرسل له رمز أي عملة (مثل `BTC`). سيقوم البوت بتحليلها على عدة أطر زمنية (يومي، 4 ساعات، ساعة) ويقدم لك تقريراً شاملاً عن الاتجاه، مؤشرات MACD و RSI، وأهم مناطق الدعم والمقاومة.\n"
        "    * **النتيجة:** أداة أساسية للتحقق من أي فرصة تجدها أو لدراسة عملة تهتم بها.\n\n"
        "* **💎 صائد الجواهر (Gem Hunter):**\n"
        "    * **الغرض:** يجيب على سؤال: \"ما هي العملات الجديدة التي انهارت سعرياً بعد إدراجها، والآن تظهر عليها علامات تعافي قوية؟\"\n"
        "    * **الوظيفة:** هذه استراتيجية استثمارية قوية. يقوم البوت بفلترة العملات المدرجة منذ بداية 2024، ويختار فقط تلك التي صححت بأكثر من 70%، ثم يفلترها مرة أخرى ليجد التي بدأت فعلاً في التعافي (ارتفعت عن قاعها وبدأت اتجاهاً صاعداً).\n"
        "    * **النتيجة:** قائمة قصيرة من \"الجواهر المخفية\" المحتملة التي لديها القدرة على تحقيق عشرات الأضعاف (X's) لمجرد العودة إلى قمتها السابقة.\n\n"
        "* **🔭 قائمة القنص (Sniper Watchlist):**\n"
        "    * **الغرض:** يجيب على سؤال: \"ما هي العملات التي يراقبها القناص حالياً والتي تستعد للانفجار؟\"\n"
        "    * **الوظيفة:** يعرض لك قائمة العملات التي اكتشفها \"رادار القناص\" (الذي يعمل في الخلفية) والتي تمر بمرحلة تجميع قوية (نمط الزنبرك المضغوط). هذه العملات هي المرشحة الأولى لإرسال تنبيه \"اختراق مؤكد\".\n"
        "    * **النتيجة:** نظرة خاطفة على أهداف القناص قبل أن يتم إطلاق النار.\n\n"
        "#### **[ الصفوف الأخرى: أدوات المراقبة والإدارة ]**\n\n"
        "* **📈 تقرير الأداء (Performance Report):**\n"
        "    * يعرض لك أداء جميع الإشارات السابقة التي أطلقها البوت (من كاشف الزخم والقناص)، موضحاً سعر التنبيه، السعر الحالي، وأعلى سعر وصلت إليه. أداة ممتازة لقياس فعالية استراتيجيات البوت.\n\n"
        "* **📊 الحالة (Status):**\n"
        "    * يعطيك نظرة سريعة على حالة البوت، عدد المستخدمين، حالة المهام الخلفية، وعدد العملات الموجودة في قوائم المراقبة لكل منصة.\n\n"
        "* **🔴/🟢 إيقاف/تفعيل المهام:**\n"
        "    * يسمح لك بإيقاف أو إعادة تشغيل جميع المهام التي تعمل في الخلفية (مثل رادار القناص وصياد الفومو).\n\n"
        "---\n\n"
        "### **الصياد الخفي: المهام التي تعمل في الخلفية (24/7)**\n\n"
        "حتى عندما لا تستخدم البوت، هناك جيش من المراقبين يعملون بلا توقف لخدمتك:\n\n"
        "1.  **رادار القناص (Sniper Radar):**\n"
        "    * يعمل كل 30 دقيقة. مهمته الوحيدة هي مسح السوق بحثاً عن عملات تدخل في نطاق تجميعي ضيق وبتقلبات منخفضة (نمط الزنبرك المضغوط). عند العثور على هدف، يضيفه إلى **\"قائمة القنص\"**.\n\n"
        "2.  **زناد القناص (Sniper Trigger):**\n"
        "    * يعمل كل 60 ثانية. مهمته هي مراقبة العملات الموجودة في **\"قائمة القنص\"** فقط. ينتظر بصبر اللحظة التي تخترق فيها إحدى هذه العملات نطاقها للأعلى مع حجم تداول ضخم وتأكيدات أخرى. عند حدوث ذلك، يرسل لك تنبيه **\"🎯 اختراق مؤكد!\"** عالي الدقة.\n\n"
        "3.  **صياد الفومو (Fomo Hunter):**\n"
        "    * يعمل كل 15 دقيقة. يقوم بتشغيل \"كاشف الزخم\" تلقائياً ويرسل تنبيهاً لجميع المستخدمين إذا وجد فرصاً جديدة وقوية.\n\n"
        "4.  **متتبع الأداء (Performance Tracker):**\n"
        "    * يعمل كل 5 دقائق. يقوم بتحديث أسعار العملات التي تم التنبيه عليها ليحافظ على دقة **\"تقرير الأداء\"**.\n\n"
        "5.  **كاشف الإدراجات الجديدة (New Listings Sniper):**\n"
        "    * يعمل كل 60 ثانية. يراقب قائمة العملات في المنصة، وفي حال ظهور عملة جديدة لم تكن موجودة من قبل، يرسل تنبيهاً فورياً.\n\n"
        "---\n\n"
        "**تذكر دائماً، البوت صياد ماهر ومساعد ذكي، لكن القرار الأخير يبقى لك. استخدم هذه الأدوات لتعزيز تحليلك الخاص. تداول سعيد!**"
    )
    await update.message.reply_text(about_text, parse_mode=ParseMode.MARKDOWN)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    registered_users = len(load_user_ids())
    message = f"📊 **حالة البوت** 📊\n\n"
    message += f"**- المستخدمون المسجلون:** `{registered_users}`\n"
    message += f"**- المهام الخلفية:** {'🟢 نشطة' if tasks_enabled else '🔴 متوقفة'}\n\n"

    for platform in PLATFORMS:
        hunts_count = len(active_hunts.get(platform, {}))
        perf_count = len(performance_tracker.get(platform, {}))
        sniper_count = len(sniper_watchlist.get(platform, {}))
        sniper_tracked_count = len(sniper_tracker.get(platform, {}))
        message += f"**منصة {platform}:**\n"
        message += f"    - 🎯 الصفقات المراقبة: {hunts_count}\n"
        message += f"    - 📈 الأداء المتتبع: {perf_count}\n"
        message += f"    - 🔭 أهداف القناص: {sniper_count}\n"
        message += f"    - 🔫 نتائج القناص: {sniper_tracked_count}\n\n"
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return

    text = update.message.text.strip()

    # --- معالجة المدخلات المعلقة ---
    if context.user_data.get('awaiting_symbol_for_ta'):
        symbol = text.upper()
        if not symbol.endswith("USDT"): symbol += "USDT"
        context.user_data['awaiting_symbol_for_ta'] = False
        context.args = [symbol]
        await run_full_technical_analysis(update, context)
        return

    if context.user_data.get('awaiting_symbol_for_scalp'):
        symbol = text.upper()
        if not symbol.endswith("USDT"): symbol += "USDT"
        context.user_data['awaiting_symbol_for_scalp'] = False
        context.args = [symbol]
        await run_scalp_analysis(update, context)
        return

    # --- معالجة الأزرار ---
    button_text = text.replace("✅ ", "")

    if button_text == BTN_TA_PRO:
        context.user_data['awaiting_symbol_for_ta'] = True
        await update.message.reply_text("🔬 يرجى إرسال رمز العملة للتحليل المعمق (مثال: `BTC` أو `SOLUSDT`)", parse_mode=ParseMode.MARKDOWN)
        return

    if button_text == BTN_SCALP_SCAN:
        context.user_data['awaiting_symbol_for_scalp'] = True
        await update.message.reply_text("⚡️ يرجى إرسال رمز العملة للتحليل السريع (مثال: `PEPE` أو `WIFUSDT`)", parse_mode=ParseMode.MARKDOWN)
        return

    if button_text == BTN_SNIPER_LIST:
        await show_sniper_watchlist(update, context)
        return

    if button_text == BTN_ABOUT:
        await about_command(update, context)
        return

    if button_text in [BTN_SELECT_MEXC, BTN_SELECT_GATEIO, BTN_SELECT_BINANCE, BTN_SELECT_BYBIT, BTN_SELECT_KUCOIN, BTN_SELECT_OKX]:
        exchange_name = button_text
        await set_exchange(update, context, exchange_name)
        return
    if button_text in [BTN_TASKS_ON, BTN_TASKS_OFF]:
        await toggle_background_tasks(update, context)
        return
    if button_text == BTN_STATUS:
        await status_command(update, context)
        return

    # --- معالجة الأوامر التي تتطلب استجابة ---
    chat_id = update.message.chat_id
    session = context.application.bot_data['session']
    current_exchange = context.user_data.get('exchange', 'mexc')
    client = get_exchange_client(current_exchange, session)
    if not client:
        await update.message.reply_text("حدث خطأ في اختيار المنصة. يرجى المحاولة مرة أخرى.")
        return

    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"🔍 جارِ تنفيذ طلبك على منصة {client.name}...")

    task_coro = None
    if button_text == BTN_MOMENTUM: task_coro = run_momentum_detector(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_WHALE_RADAR: task_coro = run_whale_radar_scan(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_PRO_SCAN: task_coro = run_pro_scan(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_PERFORMANCE: task_coro = get_performance_report(context, chat_id, sent_message.message_id)
    elif button_text == BTN_TOP_GAINERS: task_coro = run_top_gainers(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_TOP_LOSERS: task_coro = run_top_losers(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_TOP_VOLUME: task_coro = run_top_volume(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_GEM_HUNTER: task_coro = run_gem_hunter_scan(context, chat_id, sent_message.message_id, client)

    if task_coro:
        asyncio.create_task(task_coro)

async def send_long_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return

    parts = []
    current_part = ""
    for line in text.split('\n'):
        if len(current_part) + len(line) + 1 > TELEGRAM_MESSAGE_LIMIT:
            parts.append(current_part)
            current_part = ""
        current_part += line + '\n'
    if current_part:
        parts.append(current_part)

    for part in parts:
        await context.bot.send_message(chat_id=chat_id, text=part, **kwargs)
        await asyncio.sleep(0.5)

async def run_full_technical_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    symbol = context.args[0]

    current_exchange = context.user_data.get('exchange', 'mexc')
    client = get_exchange_client(current_exchange, context.application.bot_data['session'])

    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"🔬 جارِ إجراء تحليل فني شامل لـ ${symbol} على {client.name}...")

    try:
        timeframes = {'يومي': '1d', '4 ساعات': '4h', 'ساعة': '1h'}
        tf_weights = {'يومي': 3, '4 ساعات': 2, 'ساعة': 1}
        report_parts = []
        header = f"📊 **التحليل الفني المفصل لـ ${symbol}** ({client.name})\n_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_\n\n"
        overall_score = 0

        for tf_name, tf_interval in timeframes.items():
            klines = await client.get_processed_klines(symbol, tf_interval, TA_KLINE_LIMIT)
            tf_report = f"--- **إطار {tf_name}** ---\n"

            if not klines or len(klines) < TA_MIN_KLINE_COUNT:
                tf_report += "لا توجد بيانات كافية للتحليل.\n\n"; report_parts.append(tf_report); continue

            close_prices = np.array([float(k[4]) for k in klines[-TA_KLINE_LIMIT:]])
            high_prices = np.array([float(k[2]) for k in klines[-TA_KLINE_LIMIT:]])
            low_prices = np.array([float(k[3]) for k in klines[-TA_KLINE_LIMIT:]])
            current_price = close_prices[-1]
            report_lines = []
            weight = tf_weights[tf_name]

            ema21, ema50, sma100 = calculate_ema(close_prices, 21), calculate_ema(close_prices, 50), calculate_sma(close_prices, 100)
            trend_text, trend_score = analyze_trend(current_price, ema21, ema50, sma100)
            report_lines.append(f"**الاتجاه:** {trend_text}"); overall_score += trend_score * weight

            macd_line, signal_line = calculate_macd(close_prices)
            if macd_line is not None and signal_line is not None:
                if macd_line > signal_line: report_lines.append(f"🟢 **MACD:** إيجابي."); overall_score += 1 * weight
                else: report_lines.append(f"🔴 **MACD:** سلبي."); overall_score -= 1 * weight

            rsi = calculate_rsi(close_prices)
            if rsi:
                if rsi > 70: report_lines.append(f"🔴 **RSI ({rsi:.1f}):** تشبع شرائي."); overall_score -= 1 * weight
                elif rsi < 30: report_lines.append(f"🟢 **RSI ({rsi:.1f}):** تشبع بيعي."); overall_score += 1 * weight
                else: report_lines.append(f"🟡 **RSI ({rsi:.1f}):** محايد.")

            supports, resistances = find_support_resistance(high_prices, low_prices)
            next_res = min([r for r in resistances if r > current_price], default=None)
            if next_res: report_lines.append(f"🛡️ **أقرب مقاومة:** {format_price(next_res)}")
            next_sup = max([s for s in supports if s < current_price], default=None)
            if next_sup: report_lines.append(f"💰 **أقرب دعم:** {format_price(next_sup)}")
            else: report_lines.append("💰 **أقرب دعم:** لا يوجد دعم واضح أدناه.")

            fib_levels = calculate_fibonacci_retracement(high_prices, low_prices)
            if fib_levels:
                report_lines.append(f"🎚️ **فيبوناتشي:** 0.5: `{format_price(fib_levels['level_0.5'])}` | 0.618: `{format_price(fib_levels['level_0.618'])}`")

            tf_report += "\n".join(report_lines) + f"\n*السعر الحالي: {format_price(current_price)}*\n\n"
            report_parts.append(tf_report)

        summary_report = "--- **ملخص التحليل الذكي** ---\n"
        if overall_score >= 5: summary_report += f"🟢 **التحليل العام يميل للإيجابية بقوة (النقاط: {overall_score}).**"
        elif overall_score > 0: summary_report += f"🟢 **التحليل العام يميل للإيجابية (النقاط: {overall_score}).**"
        elif overall_score <= -5: summary_report += f"🔴 **التحليل العام يميل للسلبية بقوة (النقاط: {overall_score}).**"
        elif overall_score < 0: summary_report += f"🔴 **التحليل العام يميل للسلبية (النقاط: {overall_score}).**"
        else: summary_report += f"🟡 **السوق في حيرة، الإشارات متضاربة (النقاط: {overall_score}).**"
        report_parts.append(summary_report)

        await context.bot.delete_message(chat_id=chat_id, message_id=sent_message.message_id)
        full_message = header + "".join(report_parts)
        await send_long_message(context, chat_id, full_message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in full technical analysis for {symbol}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=f"حدث خطأ فادح أثناء تحليل {symbol}.")

async def run_scalp_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    symbol = context.args[0]

    current_exchange = context.user_data.get('exchange', 'mexc')
    client = get_exchange_client(current_exchange, context.application.bot_data['session'])

    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"⚡️ جارِ إجراء تحليل سريع لـ ${symbol} على {client.name}...")

    try:
        timeframes = {'15 دقيقة': '15m', '5 دقائق': '5m', 'دقيقة': '1m'}
        report_parts = []
        header = f"⚡️ **التحليل السريع لـ ${symbol}** ({client.name})\n_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_\n\n"
        overall_score = 0

        for tf_name, tf_interval in timeframes.items():
            klines = await client.get_processed_klines(symbol, tf_interval, SCALP_KLINE_LIMIT)
            tf_report = f"--- **إطار {tf_name}** ---\n"
            report_lines = [] 

            if not klines or len(klines) < 20:
                tf_report += "لا توجد بيانات كافية.\n\n"; report_parts.append(tf_report); continue

            volumes = np.array([float(k[5]) for k in klines])
            close_prices = np.array([float(k[4]) for k in klines])

            avg_volume = np.mean(volumes[-20:-1])
            last_volume = volumes[-1]

            if avg_volume > 0:
                if last_volume > avg_volume * 3:
                    report_lines.append(f"🟢 **الفوليوم:** عالٍ جداً (أقوى من المتوسط بـ {last_volume/avg_volume:.1f}x)."); overall_score += 2
                elif last_volume > avg_volume * 1.5:
                    report_lines.append(f"🟢 **الفوليوم:** جيد (أقوى من المتوسط بـ {last_volume/avg_volume:.1f}x)."); overall_score += 1
                else: report_lines.append("🟡 **الفوليوم:** عادي.")
            else: report_lines.append("🟡 **الفوليوم:** لا توجد بيانات.")

            price_change_5_candles = ((close_prices[-1] - close_prices[-5]) / close_prices[-5]) * 100 if close_prices[-5] > 0 else 0
            if price_change_5_candles > 2.0:
                 report_lines.append(f"🟢 **السعر:** حركة صاعدة قوية (`%{price_change_5_candles:+.1f}`)."); overall_score += 1
            elif price_change_5_candles < -2.0:
                 report_lines.append(f"🔴 **السعر:** حركة هابطة قوية (`%{price_change_5_candles:+.1f}`)."); overall_score -= 1
            else: report_lines.append("🟡 **السعر:** حركة عادية.")

            tf_report += "\n".join(report_lines) + f"\n*السعر الحالي: {format_price(close_prices[-1])}*\n\n"
            report_parts.append(tf_report)

        summary_report = "--- **ملخص الزخم** ---\n"
        if overall_score >= 4: summary_report += "🟢 **الزخم الحالي قوي جداً ومستمر.**"
        elif overall_score >= 2: summary_report += "🟢 **يوجد زخم إيجابي جيد.**"
        elif overall_score <= -2: summary_report += "🔴 **يوجد زخم سلبي واضح.**"
        else: summary_report += "🟡 **الزخم الحالي ضعيف أو غير واضح.**"
        report_parts.append(summary_report)

        await context.bot.delete_message(chat_id=chat_id, message_id=sent_message.message_id)
        full_message = header + "".join(report_parts)
        await send_long_message(context, chat_id, full_message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in scalp analysis for {symbol}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=f"حدث خطأ فادح أثناء تحليل {symbol}.")

async def run_pro_scan(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🎯 **الفحص الاحترافي المطور ({client.name})**\n\n🔍 جارِ تحليل السوق وتصنيف العملات... هذه العملية قد تستغرق دقيقة."
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception:
        pass

    try:
        market_data = await client.get_market_data()
        if not market_data:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق.")
            return

        candidates = [
            p['symbol'] for p in market_data 
            if MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume', '0')) <= MOMENTUM_MAX_VOLUME_24H
            and float(p.get('lastPrice', '1')) <= MOMENTUM_MAX_PRICE
        ]

        if not candidates:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص الاحترافي على {client.name} اكتمل:**\n\nلا توجد عملات مرشحة ضمن نطاق السعر والحجم المطلوبين حالياً.")
            return

        tasks = [calculate_pro_score(client, symbol) for symbol in candidates[:100]]
        results = await asyncio.gather(*tasks)

        strong_opportunities = []
        for i, (score, details) in enumerate(results):
            if score >= PRO_SCAN_MIN_SCORE:
                details['symbol'] = candidates[i]
                strong_opportunities.append(details)

        if not strong_opportunities:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص الاحترافي على {client.name} اكتمل:**\n\nلم يتم العثور على فرص قوية تتجاوز حد النقاط المطلوب ({PRO_SCAN_MIN_SCORE}).")
            return

        sorted_ops = sorted(strong_opportunities, key=lambda x: x['Final Score'], reverse=True)

        message = f"🎯 **أفضل الفرص حسب النقاط ({client.name})** 🎯\n\n"
        for i, coin_details in enumerate(sorted_ops[:5]):
            message += (f"**{i+1}. ${coin_details['symbol'].replace('USDT', '')}**\n"
                        f"    - **النقاط:** `{coin_details['Final Score']}` ⭐\n"
                        f"    - **السعر:** `${format_price(coin_details.get('Price', 'N/A'))}`\n"
                        f"    - **الاتجاه:** `{coin_details.get('Trend', 'N/A')}`\n"
                        f"    - **الزخم:** `{coin_details.get('Momentum', 'N/A')}`\n"
                        f"    - **التقلب:** `{coin_details.get('Volatility', 'N/A')}`\n\n")
        message += "*(تم تحليل وتقييم هذه العملات بناءً على عدة مؤشرات فنية)*"
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in pro_scan on {client.name}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ فادح أثناء الفحص الاحترافي.")


async def run_momentum_detector(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🚀 **كاشف الزخم ({client.name})**\n\n🔍 جارِ الفحص المنظم للسوق..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    momentum_coins_data = await helper_get_momentum_symbols(client)
    if not momentum_coins_data:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص على {client.name} اكتمل:** لا يوجد زخم حالياً."); return
    sorted_coins = sorted(momentum_coins_data.values(), key=lambda x: x['price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم ({client.name}) - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\n"
    for i, coin in enumerate(sorted_coins[:10]):
        message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n    - السعر: `${format_price(coin['current_price'])}`\n    - **زخم آخر 30 دقيقة: `%{coin['price_change']:+.2f}`**\n\n")
    message += "*(تمت إضافة هذه العملات إلى متتبع الأداء.)*"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    now = datetime.now(UTC)
    for coin in sorted_coins[:10]:
        add_to_monitoring(coin['symbol'], float(coin['current_price']), coin.get('peak_volume', 0), now, f"الزخم ({client.name})", client.name)

async def run_whale_radar_scan(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🐋 **رادار الحيتان ({client.name})**\n\n🔍 جارِ الفحص العميق..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    whale_signals_by_symbol = await helper_get_whale_activity(client)
    if not whale_signals_by_symbol:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **فحص الرادار على {client.name} اكتمل:** لا يوجد نشاط حيتان واضح."); return
    all_signals = [signal for signals_list in whale_signals_by_symbol.values() for signal in signals_list]
    sorted_signals = sorted(all_signals, key=lambda x: x.get('value', 0), reverse=True)
    message = f"🐋 **تقرير رادار الحيتان ({client.name}) - {datetime.now().strftime('%H:%M:%S')}** 🐋\n\n"
    for signal in sorted_signals:
        symbol_name = signal['symbol'].replace('USDT', '')
        if signal['type'] == 'Buy Wall': message += (f"🟢 **حائط شراء ضخم على ${symbol_name}**\n    - **الحجم:** `${signal['value']:,.0f}` USDT\n    - **عند سعر:** `{format_price(signal['price'])}`\n\n")
        elif signal['type'] == 'Sell Wall': message += (f"🔴 **حائط بيع ضخم على ${symbol_name}**\n    - **الحجم:** `${signal['value']:,.0f}` USDT\n    - **عند سعر:** `{format_price(signal['price'])}`\n\n")
        elif signal['type'] == 'Buy Pressure': message += (f"📈 **ضغط شراء عالٍ على ${symbol_name}**\n    - **النسبة:** الشراء يفوق البيع بـ `{signal['value']:.1f}x`\n\n")
        elif signal['type'] == 'Sell Pressure': message += (f"📉 **ضغط بيع عالٍ على ${symbol_name}**\n    - **النسبة:** البيع يفوق الشراء بـ `{signal['value']:.1f}x`\n\n")
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def get_performance_report(context, chat_id, message_id):
    try:
        no_fomo_data = not any(performance_tracker.values())
        no_sniper_data = not any(sniper_tracker.values())

        if no_fomo_data and no_sniper_data:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="ℹ️ لا توجد عملات قيد تتبع الأداء حالياً.")
            return

        message = "📊 **تقرير الأداء الشامل** 📊\n\n"

        if not no_fomo_data:
            message += "📈 **أداء عملات الزخم** 📈\n\n"
            all_tracked_items = []
            for platform_name, symbols_data in performance_tracker.items():
                for symbol, data in symbols_data.items():
                    data_copy = data.copy(); data_copy['exchange'] = platform_name
                    all_tracked_items.append((symbol, data_copy))

            sorted_symbols = sorted(all_tracked_items, key=lambda item: item[1]['alert_time'], reverse=True)
            for symbol, data in sorted_symbols:
                if data.get('status') == 'Archived': continue
                alert_price = data.get('alert_price',0)
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
                                f"    - سعر التنبيه: `${format_price(alert_price)}`\n"
                                f"    - السعر الحالي: `${format_price(current_price)}` (**{current_change:+.2f}%**)\n"
                                f"    - أعلى سعر: `${format_price(high_price)}` (**{peak_change:+.2f}%**)\n\n")

        if not no_sniper_data:
            message += "\n\n🔫 **متابعة نتائج القناص** 🔫\n\n"
            all_sniper_items = []
            for platform_name, symbols_data in sniper_tracker.items():
                for symbol, data in symbols_data.items():
                    if data.get('status') != 'Tracking': continue
                    data_copy = data.copy(); data_copy['exchange'] = platform_name
                    all_sniper_items.append((symbol, data_copy))

            sorted_sniper_items = sorted(all_sniper_items, key=lambda item: item[1]['alert_time'], reverse=True)

            for symbol, data in sorted_sniper_items:
                time_since_alert = datetime.now(UTC) - data['alert_time']
                hours, remainder = divmod(time_since_alert.total_seconds(), 3600)
                minutes, _ = divmod(remainder, 60)
                time_str = f"{int(hours)} س و {int(minutes)} د"
                message += (
                    f"🎯 **${symbol.replace('USDT','')}** ({data.get('exchange', 'N/A')}) (منذ {time_str})\n"
                    f"    - **الحالة:** `قيد المتابعة`\n"
                    f"    - **الهدف:** `{format_price(data['target_price'])}`\n"
                    f"    - **نقطة الفشل:** `{format_price(data['invalidation_price'])}`\n\n"
                )

        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        await send_long_message(context, chat_id, message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in get_performance_report: {e}", exc_info=True)
        try:
             await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ أثناء جلب تقرير الأداء.")
        except Exception:
             logger.error(f"Could not edit message to show error for chat_id {chat_id}")


async def run_top_gainers(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"📈 **الأعلى ربحاً ({client.name})**\n\n🔍 جارِ جلب البيانات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    market_data = await client.get_market_data()
    if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق."); return
    valid_data = [item for item in market_data if float(item.get('quoteVolume','0')) > MARKET_MOVERS_MIN_VOLUME]
    sorted_data = sorted(valid_data, key=lambda x: x.get('priceChangePercent',0), reverse=True)[:10]
    message = f"📈 **الأعلى ربحاً على {client.name}** 📈\n\n"
    for i, coin in enumerate(sorted_data):
        message += f"**{i+1}. ${coin['symbol'].replace('USDT','')}:** `%{coin.get('priceChangePercent',0):+.2f}` (السعر: ${format_price(coin['lastPrice'])})\n"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def run_top_losers(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"📉 **الأعلى خسارة ({client.name})**\n\n🔍 جارِ جلب البيانات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    market_data = await client.get_market_data()
    if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق."); return
    valid_data = [item for item in market_data if float(item.get('quoteVolume','0')) > MARKET_MOVERS_MIN_VOLUME]
    sorted_data = sorted(valid_data, key=lambda x: x.get('priceChangePercent',0))[:10]
    message = f"📉 **الأعلى خسارة على {client.name}** 📉\n\n"
    for i, coin in enumerate(sorted_data):
        message += f"**{i+1}. ${coin['symbol'].replace('USDT','')}:** `%{coin.get('priceChangePercent',0):+.2f}` (السعر: ${format_price(coin['lastPrice'])})\n"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def run_top_volume(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"💰 **الأعلى تداولاً ({client.name})**\n\n🔍 جارِ جلب البيانات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    market_data = await client.get_market_data()
    if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق."); return
    for item in market_data: item['quoteVolume_f'] = float(item.get('quoteVolume','0'))
    sorted_data = sorted(market_data, key=lambda x: x['quoteVolume_f'], reverse=True)[:10]
    message = f"💰 **الأعلى تداولاً على {client.name}** 💰\n\n"
    for i, coin in enumerate(sorted_data):
        volume, volume_str = coin['quoteVolume_f'], ""
        if volume > 1_000_000: volume_str = f"{volume/1_000_000:.2f}M"
        else: volume_str = f"{volume/1_000:.1f}K"
        message += f"**{i+1}. ${coin['symbol'].replace('USDT','')}:** (الحجم: `${volume_str}`)\n"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def show_sniper_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = "🔭 **قائمة مراقبة القناص** 🔭\n\n"
    any_watched = False
    for platform, watchlist in sniper_watchlist.items():
        if watchlist:
            any_watched = True
            message += f"--- **{platform}** ---\n"
            for symbol, data in list(watchlist.items())[:5]:
                poc_str = f", POC: `{format_price(data['poc'])}`" if 'poc' in data else ""
                message += (f"- `${symbol.replace('USDT','')}` (نطاق: "
                            f"`{format_price(data['low'])}` - `{format_price(data['high'])}`{poc_str})\n")
            if len(watchlist) > 5:
                message += f"    *... و {len(watchlist) - 5} عملات أخرى.*\n"
            message += "\n"

    if not any_watched:
        message += "لا توجد أهداف حالية في قائمة المراقبة. يقوم الرادار بالبحث..."

    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def run_gem_hunter_scan(context, chat_id, message_id, client: BaseExchangeClient):
    """Executes the 'Hidden Gem Hunter' strategy."""
    initial_text = f"💎 **صائد الجواهر ({client.name})**\n\n🔍 جارِ تنفيذ مسح عميق للسوق... هذه العملية قد تستغرق عدة دقائق."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    try:
        market_data = await client.get_market_data()
        if not market_data:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق."); return
        
        volume_candidates = [c for c in market_data if float(c.get('quoteVolume', '0')) > GEM_MIN_24H_VOLUME_USDT]
        
        final_gems = []
        for coin in volume_candidates:
            symbol = coin['symbol']
            try:
                klines = await client.get_processed_klines(symbol, '1d', 1000) # Fetch up to ~3 years of daily data
                if not klines or len(klines) < 10: continue

                # Stage 1: Modernity Filter
                first_candle_ts = int(klines[0][0]) / 1000
                listing_date = datetime.fromtimestamp(first_candle_ts, tz=UTC)
                if listing_date < GEM_LISTING_SINCE_DATE: continue

                high_prices = np.array([float(k[2]) for k in klines])
                low_prices = np.array([float(k[3]) for k in klines])
                close_prices = np.array([float(k[4]) for k in klines])
                
                ath = np.max(high_prices)
                atl = np.min(low_prices)
                current_price = close_prices[-1]

                if ath == 0 or current_price == 0: continue

                # Stage 2: Deep Correction Filter
                correction_percent = ((current_price - ath) / ath) * 100
                if correction_percent > GEM_MIN_CORRECTION_PERCENT: continue

                # Stage 4: Recovery Filter
                rise_from_atl = ((current_price - atl) / atl) * 100 if atl > 0 else float('inf')
                if rise_from_atl < GEM_MIN_RISE_FROM_ATL_PERCENT: continue
                
                if len(close_prices) >= 20 and current_price < np.mean(close_prices[-20:]): continue

                # Stage 5: Potential Calculation
                potential_x = ath / current_price
                final_gems.append({
                    'symbol': symbol, 'potential_x': potential_x, 'correction_percent': correction_percent
                })
            except Exception as e:
                logger.warning(f"Could not process gem candidate {symbol}: {e}")
                continue


        if not final_gems:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **بحث الجواهر اكتمل:**\n\nلم يتم العثور على عملات تتوافق مع جميع الشروط الصارمة حالياً."); return

        sorted_gems = sorted(final_gems, key=lambda x: x['potential_x'], reverse=True)
        message = f"💎 **تقرير الجواهر المخفية ({client.name})** 💎\n\n*أفضل الفرص التي تتوافق مع استراتيجية التصحيح العميق والتعافي:*\n\n"
        for gem in sorted_gems[:5]:
            message += (f"**${gem['symbol'].replace('USDT', '')}**\n"
                        f"  - 🚀 **العودة للقمة: {gem['potential_x']:.1f}X**\n"
                        f"  - 🩸 مصححة بنسبة: {gem['correction_percent']:.1f}%\n"
                        f"  - 📈 الحالة: تظهر بوادر تعافي\n\n")
        
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Error in run_gem_hunter_scan: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ فادح أثناء البحث عن الجواهر.")

# =============================================================================
# --- 5. المهام الآلية الدورية (تم التحديث) ---
# =============================================================================
def add_to_monitoring(symbol, alert_price, peak_volume, alert_time, source, exchange_name):
    platform_name = exchange_name
    if platform_name not in PLATFORMS: return
    if symbol not in active_hunts[platform_name]:
        active_hunts[platform_name][symbol] = {'alert_price': alert_price, 'alert_time': alert_time}
    if symbol not in performance_tracker[platform_name]:
        performance_tracker[platform_name][symbol] = {'alert_price': alert_price, 'alert_time': alert_time, 'source': source, 'current_price': alert_price, 'high_price': alert_price, 'status': 'Tracking', 'momentum_lost_alerted': False}
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol} on {exchange_name}")

async def fomo_hunter_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    if not client: return
    logger.info(f"Fomo Hunter background task started for {client.name}.")
    while True:
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)
        if not bot_data.get('background_tasks_enabled', True): continue
        logger.info(f"===== Fomo Hunter ({client.name}): Starting Automatic Scan =====")
        try:
            momentum_coins_data = await helper_get_momentum_symbols(client)
            if not momentum_coins_data:
                logger.info(f"Fomo Hunter ({client.name}): No significant momentum detected."); continue
            now = datetime.now(UTC)
            new_alerts = []
            for symbol, data in momentum_coins_data.items():
                last_alert_time = recently_alerted_fomo[client.name].get(symbol)
                if not last_alert_time or (now - last_alert_time) > timedelta(minutes=RUN_FOMO_SCAN_EVERY_MINUTES * 4):
                     new_alerts.append(data); recently_alerted_fomo[client.name][symbol] = now
            if not new_alerts:
                logger.info(f"Fomo Hunter ({client.name}): Found momentum coins, but they were alerted recently."); continue
            sorted_coins = sorted(new_alerts, key=lambda x: x['price_change'], reverse=True)
            message = f"🚨 **تنبيه تلقائي من صياد الفومو ({client.name})** 🚨\n\n"
            for i, coin in enumerate(sorted_coins[:5]):
                message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n    - السعر: `${format_price(coin['current_price'])}`\n    - **زخم آخر 30 دقيقة: `%{coin['price_change']:+.2f}`**\n\n")

            await broadcast_message(bot, message)

            for coin in sorted_coins[:5]:
                add_to_monitoring(coin['symbol'], float(coin['current_price']), coin.get('peak_volume', 0), now, f"صياد الفومو ({client.name})", client.name)
        except Exception as e:
            logger.error(f"Error in fomo_hunter_loop for {client.name}: {e}", exc_info=True)

async def new_listings_sniper_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    if not client: return
    logger.info(f"New Listings Sniper background task started for {client.name}.")
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
                    await broadcast_message(bot, message)
                known_symbols[client.name].update(newly_listed)
        except Exception as e:
            logger.error(f"An unexpected error in new_listings_sniper_loop for {client.name}: {e}")

async def performance_tracker_loop(session: aiohttp.ClientSession, bot: Bot):
    logger.info("Performance Tracker background task started.")
    while True:
        await asyncio.sleep(RUN_PERFORMANCE_TRACKER_EVERY_MINUTES * 60)
        now = datetime.now(UTC)
        for platform in PLATFORMS:
            for symbol, data in list(performance_tracker[platform].items()):
                if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                    if performance_tracker[platform].get(symbol):
                         performance_tracker[platform][symbol]['status'] = 'Archived'
                    continue
                if data.get('status') == 'Archived':
                    if performance_tracker[platform].get(symbol):
                        del performance_tracker[platform][symbol]
                    continue
                try:
                    client = get_exchange_client(platform, session)
                    if not client: continue
                    current_price = await client.get_current_price(symbol)
                    if not current_price: continue

                    tracker = performance_tracker[platform].get(symbol)
                    if tracker:
                        tracker['current_price'] = current_price
                        if current_price > tracker.get('high_price', 0):
                            tracker['high_price'] = current_price

                        high_price = tracker['high_price']
                        is_momentum_source = "الزخم" in data.get('source', '') or "الفومو" in data.get('source', '')
                        if is_momentum_source and not tracker.get('momentum_lost_alerted', False) and high_price > 0:
                            price_drop_percent = ((current_price - high_price) / high_price) * 100
                            if price_drop_percent <= MOMENTUM_LOSS_THRESHOLD_PERCENT:
                                message = (f"⚠️ **تنبيه: فقدان الزخم لعملة ${symbol.replace('USDT','')}** ({platform})\n\n"
                                           f"    - أعلى سعر: `${format_price(high_price)}`\n"
                                           f"    - السعر الحالي: `${format_price(current_price)}`\n"
                                           f"    - **الهبوط من القمة: `{price_drop_percent:.2f}%`**")
                                await broadcast_message(bot, message)
                                tracker['momentum_lost_alerted'] = True
                                logger.info(f"MOMENTUM LOSS ALERT sent for {symbol} on {platform}")
                except Exception as e:
                    logger.error(f"Error updating price for {symbol} on {platform}: {e}")

            for symbol, data in list(sniper_tracker[platform].items()):
                if data['status'] != 'Tracking': continue

                if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                    if sniper_tracker[platform].get(symbol):
                        del sniper_tracker[platform][symbol]
                    continue

                try:
                    client = get_exchange_client(platform, session)
                    if not client: continue
                    current_price = await client.get_current_price(symbol)
                    if not current_price: continue

                    if current_price >= data['target_price']:
                        success_message = (
                            f"✅ **القناص: نجاح!** ✅\n\n"
                            f"**العملة:** `${symbol.replace('USDT', '')}` ({platform})\n"
                            f"**النتيجة:** وصلت إلى الهدف المحدد عند `{format_price(data['target_price'])}` بنجاح."
                        )
                        await broadcast_message(bot, success_message)
                        logger.info(f"SNIPER TRACKER ({platform}): {symbol} SUCCEEDED.")
                        del sniper_tracker[platform][symbol]

                    elif current_price <= data['invalidation_price']:
                        failure_message = (
                            f"❌ **القناص: فشل.** ❌\n\n"
                            f"**العملة:** `${symbol.replace('USDT', '')}` ({platform})\n"
                            f"**النتيجة:** فشل الاختراق وعاد السعر تحت نقطة الإبطال عند `{format_price(data['invalidation_price'])}`."
                        )
                        await broadcast_message(bot, failure_message)
                        logger.info(f"SNIPER TRACKER ({platform}): {symbol} FAILED.")
                        del sniper_tracker[platform][symbol]

                except Exception as e:
                     logger.error(f"Error in Sniper Tracker for {symbol} on {platform}: {e}")


async def coiled_spring_radar_loop(client: BaseExchangeClient, bot_data: dict):
    if not client: return
    logger.info(f"Sniper Radar background task started for {client.name}.")
    while True:
        await asyncio.sleep(SNIPER_RADAR_RUN_EVERY_MINUTES * 60)
        if not bot_data.get('background_tasks_enabled', True): continue
        logger.info(f"===== Sniper Radar ({client.name}): Searching for coiled springs =====")
        try:
            market_data = await client.get_market_data()
            if not market_data: continue

            candidates = [p for p in market_data if float(p.get('quoteVolume', '0')) > SNIPER_MIN_USDT_VOLUME]

            async def check_candidate(symbol):
                klines = await client.get_processed_klines(symbol, '15m', int(SNIPER_COMPRESSION_PERIOD_HOURS * 4))
                if not klines or len(klines) < int(SNIPER_COMPRESSION_PERIOD_HOURS * 4): return

                high_prices = np.array([float(k[2]) for k in klines])
                low_prices = np.array([float(k[3]) for k in klines])
                volumes = np.array([float(k[5]) for k in klines])

                highest_high = np.max(high_prices)
                lowest_low = np.min(low_prices)

                if lowest_low == 0: return
                volatility = ((highest_high - lowest_low) / lowest_low) * 100

                if volatility <= SNIPER_MAX_VOLATILITY_PERCENT:
                    poc = calculate_poc(klines)
                    if not poc: return

                    avg_volume = np.mean(volumes)
                    if symbol not in sniper_watchlist[client.name]:
                        sniper_watchlist[client.name][symbol] = {
                            'high': highest_high, 'low': lowest_low,
                            'poc': poc,
                            'avg_volume': avg_volume, 'duration_hours': SNIPER_COMPRESSION_PERIOD_HOURS
                        }
                        logger.info(f"SNIPER RADAR ({client.name}): Added {symbol} to watchlist. POC: {poc:.8g}, Volatility: {volatility:.2f}%")

            tasks = [check_candidate(p['symbol']) for p in candidates]
            await asyncio.gather(*tasks)

        except Exception as e:
            logger.error(f"Error in coiled_spring_radar_loop for {client.name}: {e}", exc_info=True)

async def breakout_trigger_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    """
    IMPROVED SNIPER TRIGGER with Confirmation Candle Strategy.
    """
    if not client: return
    logger.info(f"Sniper Trigger (v25 - Confirmation) background task started for {client.name}.")
    while True:
        await asyncio.sleep(SNIPER_TRIGGER_RUN_EVERY_SECONDS)
        if not bot_data.get('background_tasks_enabled', True): continue

        watchlist_copy = list(sniper_watchlist[client.name].items())
        if not watchlist_copy: continue

        for symbol, data in watchlist_copy:
            try:
                klines = await client.get_processed_klines(symbol, '5m', 5) # Get last 5 candles
                if not klines or len(klines) < 3: continue

                # Define candles: current (in progress), confirmation (just closed), trigger (before confirmation)
                confirmation_candle = klines[-2]
                trigger_candle = klines[-3]
                
                breakout_level = data['high']
                
                # --- NEW CONFIRMATION LOGIC ---
                # 1. Check if the breakout happened on the trigger candle
                trigger_high = float(trigger_candle[2])
                if trigger_high < breakout_level:
                    continue # No breakout happened yet

                # 2. Check if the confirmation candle CLOSED above the breakout level
                confirmation_close = float(confirmation_candle[4])
                if confirmation_close < breakout_level:
                    continue # Failed to confirm, it was a fakeout wick

                # 3. Check for high volume on either candle
                trigger_volume = float(trigger_candle[5])
                confirmation_volume = float(confirmation_candle[5])
                avg_volume_5m = data['avg_volume'] / (15 / 5) # Approximate 5m avg volume
                
                is_breakout_volume = (trigger_volume > avg_volume_5m * SNIPER_BREAKOUT_VOLUME_MULTIPLIER or
                                      confirmation_volume > avg_volume_5m * SNIPER_BREAKOUT_VOLUME_MULTIPLIER)

                if not is_breakout_volume:
                    continue # Breakout lacked volume power

                # --- All conditions met, it's a confirmed breakout ---
                
                alert_price = confirmation_close # Use the closing price of the confirmation candle
                invalidation_price = breakout_level
                range_height = data['high'] - data['low']
                target_price = data['high'] + range_height

                message = (
                    f"🎯 **قناص (إشارة مؤكدة): اختراق حقيقي!** 🎯\n\n"
                    f"**العملة:** `${symbol.replace('USDT', '')}` ({client.name})\n"
                    f"**النمط:** اختراق نطاق تجميعي بعد إغلاق شمعة تأكيد.\n"
                    f"**سعر التأكيد:** `{format_price(alert_price)}`\n\n"
                    f"📝 **خطة المراقبة:**\n"
                    f"- **يفشل الاختراق بالإغلاق تحت:** `{format_price(invalidation_price)}` (قمة النطاق)\n"
                    f"- **هدف أولي محتمل:** `{format_price(target_price)}`\n\n"
                    f"*(تم فلترة الإشارة لزيادة الموثوقية. راقب الخطة جيداً)*"
                )
                await broadcast_message(bot, message)
                logger.info(f"SNIPER TRIGGER (CONFIRMED) ({client.name}): Confirmed breakout for {symbol}!")

                sniper_tracker[client.name][symbol] = {
                    'alert_time': datetime.now(UTC),
                    'target_price': target_price,
                    'invalidation_price': invalidation_price,
                    'status': 'Tracking' 
                }
                logger.info(f"SNIPER TRACKER ({client.name}): Started tracking CONFIRMED breakout for {symbol}.")

                if symbol in sniper_watchlist[client.name]:
                    del sniper_watchlist[client.name][symbol]

            except Exception as e:
                 logger.error(f"Error in breakout_trigger_loop for {symbol} on {client.name}: {e}", exc_info=True)
                 if symbol in sniper_watchlist[client.name]:
                     del sniper_watchlist[client.name][symbol]


# =============================================================================
# --- 6. تشغيل البوت (تم التحديث) ---
# =============================================================================
async def send_startup_message(bot: Bot):
    try:
        message = "✅ **بوت الصياد الذكي (v25.0 - قناص التأكيد) متصل الآن!**\n\nأرسل /start لعرض القائمة."
        await broadcast_message(bot, message)
        logger.info("Startup message sent successfully to all users.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

async def post_init(application: Application):
    """دالة يتم تشغيلها بعد تهيئة البوت لتشغيل المهام الخلفية."""
    logger.info("Bot initialized. Starting background tasks...")
    session = aiohttp.ClientSession()
    application.bot_data["session"] = session

    bot_instance = application.bot
    bot_data = application.bot_data

    # بدء المهام الخلفية
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
    """Start the bot."""
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN:
        logger.critical("FATAL ERROR: Bot token is not set.")
        return

    setup_database()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.bot_data['background_tasks_enabled'] = True

    # إضافة المعالجات
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # ربط دالة المهام الخلفية
    application.post_init = post_init

    # تشغيل البوت
    logger.info("Telegram bot is starting...")
    application.run_polling()


if __name__ == '__main__':
    main()
