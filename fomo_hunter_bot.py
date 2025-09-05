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
        logger.error(f"Error in pro_score for {symbol}: {e}")
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

# --- إعدادات وحدة القناص (Sniper Module) ---
SNIPER_RADAR_RUN_EVERY_MINUTES = 30
SNIPER_TRIGGER_RUN_EVERY_SECONDS = 60
SNIPER_COMPRESSION_PERIOD_HOURS = 8
SNIPER_MAX_VOLATILITY_PERCENT = 8.0
SNIPER_BREAKOUT_VOLUME_MULTIPLIER = 4.0
SNIPER_MIN_USDT_VOLUME = 200000

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
# --- قسم التحليل الفني والوظائف المساعدة ---
# =============================================================================
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
        if current_price > ema21 > ema50 > sma100: return "🟢 اتجاه صاعد قوي.", 2
        if current_price > ema50 and current_price > ema21: return "🟢 اتجاه صاعد.", 1
        if current_price < ema21 < ema50 < sma100: return "🔴 اتجاه هابط قوي.", -2
        if current_price < ema50 and current_price < ema21: return "🔴 اتجاه هابط.", -1
    return "🟡 جانبي / غير واضح.", 0

# =============================================================================
# --- 4. الوظائف التفاعلية (أوامر البوت) ---
# =============================================================================
BTN_TA_PRO = "🔬 محلل فني"
BTN_SCALP_SCAN = "⚡️ تحليل سريع"
BTN_PRO_SCAN = "🎯 فحص احترافي"
BTN_SNIPER_LIST = "🔭 قائمة القنص"
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
        [BTN_TA_PRO, BTN_SCALP_SCAN, BTN_SNIPER_LIST],
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
    welcome_message = (
        "أهلاً بك في بوت الصياد الذكي!\n\n"
        "مهمتي هي أن أمنحك ميزة على بقية السوق عبر ثلاث قدرات أساسية:\n\n"
        "**🎯 1. أقتنص الفرص (وحدة القناص):**\n"
        "أبحث عن العملات التي تستعد للانفجار، وأرسل لك تنبيهاً في لحظة الانطلاق المحتملة مع خطة مراقبة واضحة.\n\n"
        "**🚀 2. أرصد الزخم (الكواشف):**\n"
        "أخبرك بالعملات التي تشهد زخماً قوياً أو نشاط حيتان، لتكون على دراية بما يحدث في السوق.\n\n"
        "**🔬 3. أحلل أي عملة (المحلل الفني):**\n"
        "أرسل لي رمز أي عملة (مثل BTC)، وسأقدم لك تقريراً فنياً مفصلاً لمساعدتك في اتخاذ قراراتك.\n\n"
        "استخدم الأزرار أدناه للبدء.\n\n"
        "⚠️ **تنبيه:** أنا أداة للمساعدة والتحليل فقط، ولست مستشاراً مالياً. قراراتك تقع على عاتقك بالكامل."
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

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    registered_users = len(load_user_ids())
    message = f"📊 **حالة البوت** 📊\n\n"
    message += f"**- المستخدمون:** `{registered_users}`\n"
    message += f"**- المهام:** {'🟢 نشطة' if tasks_enabled else '🔴 متوقفة'}\n\n"
    for platform in PLATFORMS:
        message += f"**{platform}:**\n"
        message += f"    - 📈 الأداء المتتبع: {len(performance_tracker.get(platform, {}))}\n"
        message += f"    - 🔭 أهداف القناص: {len(sniper_watchlist.get(platform, {}))}\n"
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.strip()

    if context.user_data.get('awaiting_symbol_for_ta'):
        symbol = text.upper().replace("USDT", "") + "USDT"
        context.user_data['awaiting_symbol_for_ta'] = False
        await run_full_technical_analysis(update, context, symbol)
        return
    
    if context.user_data.get('awaiting_symbol_for_scalp'):
        symbol = text.upper().replace("USDT", "") + "USDT"
        context.user_data['awaiting_symbol_for_scalp'] = False
        await run_scalp_analysis(update, context, symbol)
        return

    button_text = text.replace("✅ ", "")
    
    button_map = {
        BTN_TA_PRO: ("awaiting_symbol_for_ta", "🔬 يرجى إرسال رمز العملة للتحليل المعمق (مثال: `BTC`)"),
        BTN_SCALP_SCAN: ("awaiting_symbol_for_scalp", "⚡️ يرجى إرسال رمز العملة للتحليل السريع (مثال: `PEPE`)"),
    }

    if button_text in button_map:
        key, reply_text = button_map[button_text]
        context.user_data[key] = True
        await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)
        return
        
    if button_text == BTN_SNIPER_LIST:
        await show_sniper_watchlist(update, context); return
    if button_text in [BTN_SELECT_MEXC, BTN_SELECT_GATEIO, BTN_SELECT_BINANCE, BTN_SELECT_BYBIT, BTN_SELECT_KUCOIN, BTN_SELECT_OKX]:
        await set_exchange(update, context, button_text); return
    if button_text in [BTN_TASKS_ON, BTN_TASKS_OFF]:
        await toggle_background_tasks(update, context); return
    if button_text == BTN_STATUS:
        await status_command(update, context); return

    chat_id = update.message.chat_id
    session = context.application.bot_data['session']
    client = get_exchange_client(context.user_data.get('exchange', 'mexc'), session)
    if not client:
        await update.message.reply_text("حدث خطأ في اختيار المنصة."); return

    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"🔍 جارِ تنفيذ طلبك على {client.name}...")
    
    task_map = {
        BTN_PRO_SCAN: run_pro_scan,
        BTN_PERFORMANCE: get_performance_report,
        BTN_TOP_GAINERS: run_top_gainers,
        BTN_TOP_LOSERS: run_top_losers,
        BTN_TOP_VOLUME: run_top_volume,
    }
    
    if button_text in task_map:
        # These tasks might need the client object
        if button_text in [BTN_TOP_GAINERS, BTN_TOP_LOSERS, BTN_TOP_VOLUME, BTN_PRO_SCAN]:
             task_coro = task_map[button_text](context, chat_id, sent_message.message_id, client)
        else:
             task_coro = task_map[button_text](context, chat_id, sent_message.message_id)
        if task_coro: asyncio.create_task(task_coro)

async def send_long_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs):
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return
    parts = [text[i:i+TELEGRAM_MESSAGE_LIMIT] for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT)]
    for part in parts:
        await context.bot.send_message(chat_id=chat_id, text=part, **kwargs)
        await asyncio.sleep(0.5)

async def run_full_technical_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    chat_id = update.message.chat_id
    client = get_exchange_client(context.user_data.get('exchange', 'mexc'), context.application.bot_data['session'])
    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"🔬 جارِ تحليل ${symbol} على {client.name}...")

    try:
        # ... (Implementation of full TA remains the same)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=f"التحليل الكامل لـ ${symbol} جاهز.")
    except Exception as e:
        logger.error(f"Error in full TA for {symbol}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=f"حدث خطأ أثناء تحليل {symbol}.")

async def run_scalp_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    chat_id = update.message.chat_id
    client = get_exchange_client(context.user_data.get('exchange', 'mexc'), context.application.bot_data['session'])
    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"⚡️ جارِ تحليل سريع لـ ${symbol} على {client.name}...")

    try:
        # ... (Implementation of scalp analysis remains the same)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=f"التحليل السريع لـ ${symbol} جاهز.")
    except Exception as e:
        logger.error(f"Error in scalp analysis for {symbol}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=f"حدث خطأ أثناء تحليل {symbol}.")

async def run_pro_scan(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🎯 **الفحص الاحترافي المطور ({client.name})**\n\n🔍 جارِ تحليل وتصنيف السوق..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except: pass
    try:
        market_data = await client.get_market_data()
        if not market_data:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="خطأ في جلب بيانات السوق.")
            return

        candidates = [p['symbol'] for p in market_data if MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume', '0')) <= MOMENTUM_MAX_VOLUME_24H and float(p.get('lastPrice', '1')) <= MOMENTUM_MAX_PRICE]

        if not candidates:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص على {client.name} اكتمل:**\n\nلا توجد عملات مرشحة ضمن الشروط حالياً."); return

        tasks = [calculate_pro_score(client, symbol) for symbol in candidates[:100]]
        results = await asyncio.gather(*tasks)

        strong_opportunities = [{'symbol': candidates[i], **details} for i, (score, details) in enumerate(results) if score >= PRO_SCAN_MIN_SCORE]
        
        if not strong_opportunities:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص على {client.name} اكتمل:**\n\nلم يتم العثور على فرص قوية تتجاوز حد النقاط المطلوب."); return

        sorted_ops = sorted(strong_opportunities, key=lambda x: x['Final Score'], reverse=True)
        
        message = f"🎯 **أفضل الفرص حسب النقاط ({client.name})** 🎯\n\n"
        for i, coin in enumerate(sorted_ops[:5]):
            message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n"
                        f"    - **النقاط:** `{coin['Final Score']}` ⭐\n"
                        f"    - **السعر:** `${format_price(coin.get('Price', 'N/A'))}`\n"
                        f"    - **الاتجاه:** `{coin.get('Trend', 'N/A')}`\n"
                        f"    - **الزخم:** `{coin.get('Momentum', 'N/A')}`\n\n")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in pro_scan on {client.name}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ فادح أثناء الفحص.")

# =============================================================================
# --- 5. المهام الآلية الدورية (تم التحديث) ---
# =============================================================================
def add_to_monitoring(symbol, alert_price, source, exchange_name):
    platform_name = exchange_name
    if symbol not in performance_tracker[platform_name]:
        performance_tracker[platform_name][symbol] = {
            'alert_price': alert_price, 'alert_time': datetime.now(UTC), 
            'source': source, 'current_price': alert_price, 'high_price': alert_price, 
            'status': 'Tracking', 'momentum_lost_alerted': False
        }
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol} on {exchange_name}")

async def fomo_hunter_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    # ... (Implementation remains the same)
    pass

async def new_listings_sniper_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    # ... (Implementation remains the same)
    pass
    
async def performance_tracker_loop(session: aiohttp.ClientSession, bot: Bot):
    # ... (Implementation remains the same)
    pass
    
async def coiled_spring_radar_loop(client: BaseExchangeClient, bot_data: dict):
    # ... (Implementation remains the same)
    pass

async def breakout_trigger_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    if not client: return
    logger.info(f"Sniper Trigger background task started for {client.name}.")
    while True:
        await asyncio.sleep(SNIPER_TRIGGER_RUN_EVERY_SECONDS)
        if not bot_data.get('background_tasks_enabled', True): continue

        watchlist_copy = list(sniper_watchlist[client.name].items())
        if not watchlist_copy: continue

        for symbol, data in watchlist_copy:
            try:
                klines = await client.get_processed_klines(symbol, '5m', 20)
                if not klines or len(klines) < 20: continue

                current_price = float(klines[-1][4])
                current_volume = float(klines[-1][5])
                
                close_prices_5m = [float(k[4]) for k in klines]
                volumes_5m = [float(k[5]) for k in klines]
                vwap_5m = calculate_vwap(close_prices_5m, volumes_5m, period=14)

                is_breakout_price = current_price > data['high']
                is_breakout_volume = current_volume > (data['avg_volume'] * SNIPER_BREAKOUT_VOLUME_MULTIPLIER)
                is_above_vwap = vwap_5m and current_price > vwap_5m

                if is_breakout_price and is_breakout_volume and is_above_vwap:
                    
                    # --- الإضافة الجديدة هنا ---
                    invalidation_price = data['high']
                    range_height = data['high'] - data['low']
                    success_target = data['high'] + range_height
                    # --- نهاية الإضافة ---

                    message = (
                        f"🎯 **تنبيه قناص: اختراق مؤكد!** 🎯\n\n"
                        f"**العملة:** `${symbol.replace('USDT', '')}` ({client.name})\n"
                        f"**النمط:** اختراق نطاق تجميعي استمر لـ {data['duration_hours']} ساعات.\n"
                        f"**سعر الاختراق:** `{format_price(current_price)}`\n"
                        f"**التأكيد:** السعر فوق VWAP وحجم التداول عالٍ.\n\n"
                        f"---\n"
                        f"📝 **خطة المراقبة:**\n"
                        f"- **يفشل الاختراق بالإغلاق تحت:** `{format_price(invalidation_price)}`\n"
                        f"- **هدف أولي محتمل (نجاح):** `{format_price(success_target)}`\n"
                        f"---\n\n"
                        f"*(إشارة عالية الدقة، راقب نقاط الخطة جيداً)*"
                    )
                    await broadcast_message(bot, message)
                    logger.info(f"SNIPER TRIGGER ({client.name}): Breakout detected for {symbol}!")

                    if symbol in sniper_watchlist[client.name]:
                        del sniper_watchlist[client.name][symbol]

            except Exception as e:
                 logger.error(f"Error in breakout_trigger_loop for {symbol} on {client.name}: {e}")
                 if symbol in sniper_watchlist[client.name]:
                     del sniper_watchlist[client.name][symbol]

# =============================================================================
# --- 6. تشغيل البوت ---
# =============================================================================
async def post_init(application: Application):
    logger.info("Bot initialized. Starting background tasks...")
    session = aiohttp.ClientSession()
    application.bot_data["session"] = session
    bot_instance = application.bot
    bot_data = application.bot_data

    application.bot_data['task_performance'] = asyncio.create_task(performance_tracker_loop(session, bot_instance))
    for platform_name in PLATFORMS:
        client = get_exchange_client(platform_name.lower(), session)
        if client:
            application.bot_data[f'task_fomo_{platform_name}'] = asyncio.create_task(fomo_hunter_loop(client, bot_instance, bot_data))
            application.bot_data[f'task_sniper_radar_{platform_name}'] = asyncio.create_task(coiled_spring_radar_loop(client, bot_data))
            application.bot_data[f'task_sniper_trigger_{platform_name}'] = asyncio.create_task(breakout_trigger_loop(client, bot_instance, bot_data))

async def main() -> None:
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

