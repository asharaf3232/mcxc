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
    يحسب مؤشر متوسط النطاق الحقيقي (ATR) بصيغته المرجعية.
    TR = max(H-L, |H-C_prev|, |L-C_prev|)
    """
    if len(close_prices) < period + 1:
        return None
    
    tr_values = []
    for i in range(1, len(high_prices)):
        high = high_prices[i]
        low = low_prices[i]
        prev_close = close_prices[i-1]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        tr_values.append(tr)

    return np.mean(tr_values[-period:]) if len(tr_values) >= period else None

def calculate_vwap(close_prices, volumes, period=14):
    """
    يحسب مؤشر متوسط السعر المرجح بالحجم (VWAP).
    VWAP = Σ(Price * Volume) / Σ(Volume)
    """
    if len(close_prices) < period:
        return None
        
    prices = np.array(close_prices[-period:])
    volumes = np.array(volumes[-period:])
    
    if np.sum(volumes) == 0:
        return np.mean(prices)

    return np.sum(prices * volumes) / np.sum(volumes)

def analyze_momentum_consistency(close_prices, volumes, period=10):
    if len(close_prices) < period:
        return 0

    recent_closes = np.array(close_prices[-period:])
    recent_volumes = np.array(volumes[-period:])
    price_increases = np.sum(np.diff(recent_closes) > 0)
    
    score = 0
    if (price_increases / period) >= 0.6:
        score += 1

    half_period = period // 2
    first_half_volume_avg = np.mean(recent_volumes[:half_period])
    second_half_volume_avg = np.mean(recent_volumes[half_period:])

    if first_half_volume_avg > 0 and second_half_volume_avg > (first_half_volume_avg * 1.2):
        score += 1
        
    return score

async def calculate_pro_score(client, symbol: str):
    score = 0
    analysis_details = {}
    try:
        klines = await client.get_processed_klines(symbol, '15m', 100)
        if not klines or len(klines) < 50:
            return 0, {}

        close_prices = np.array([float(k[4]) for k in klines])
        high_prices = np.array([float(k[2]) for k in klines])
        low_prices = np.array([float(k[3]) for k in klines])
        volumes = np.array([float(k[5]) for k in klines])
        current_price = close_prices[-1]

        # 1. Trend Analysis
        ema20 = np.mean(close_prices[-20:])
        ema50 = np.mean(close_prices[-50:])
        if current_price > ema20 > ema50: score += 2; analysis_details['Trend'] = "🟢 Strong Up"
        elif current_price > ema20: score += 1; analysis_details['Trend'] = "🟢 Up"
        elif current_price < ema20 < ema50: score -= 2; analysis_details['Trend'] = "🔴 Strong Down"
        elif current_price < ema20: score -= 1; analysis_details['Trend'] = "🔴 Down"
        else: analysis_details['Trend'] = "🟡 Sideways"

        # 2. Momentum
        momentum_score = analyze_momentum_consistency(close_prices, volumes)
        score += momentum_score
        analysis_details['Momentum'] = f"{'🟢' * momentum_score}{'🟡' * (2-momentum_score)} ({momentum_score}/2)"

        # 3. Volatility (ATR)
        atr = calculate_atr(high_prices, low_prices, close_prices)
        if atr:
            volatility_percent = (atr / current_price) * 100 if current_price > 0 else 0
            analysis_details['Volatility'] = f"{volatility_percent:.2f}%"
            if volatility_percent > 7.0 or volatility_percent < 1.0: score -=1
            else: score += 1
        
        # 4. RSI
        rsi = calculate_rsi(close_prices)
        if rsi:
            analysis_details['RSI'] = f"{rsi:.1f}"
            if rsi > 75: score -= 1
            elif rsi < 25: score += 1
            
        # 5. VWAP
        vwap = calculate_vwap(close_prices, volumes, period=20)
        if vwap:
            analysis_details['VWAP'] = f"{vwap:.8g}"
            if current_price > vwap * 1.01: score += 2
            elif current_price > vwap: score += 1

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
WHALE_OBI_LEVELS = 10 

# --- إعدادات كاشف الزخم ---
MOMENTUM_MAX_PRICE = 0.10
MOMENTUM_MIN_VOLUME_24H = 50000
MOMENTUM_MAX_VOLUME_24H = 2000000
MOMENTUM_VOLUME_INCREASE = 1.8
MOMENTUM_PRICE_INCREASE = 4.0
MOMENTUM_KLINE_INTERVAL = '5m'
MOMENTUM_KLINE_LIMIT = 12
MOMENTUM_MIN_SCORE = 3 # ⭐ الحد الأدنى لنقاط الزخم لإطلاق تنبيه
MOMENTUM_LOSS_THRESHOLD_PERCENT = -10.0 # نسبة الهبوط من القمة التي تعتبر "فقدان للزخم"

# --- [NEW] إعدادات كاشف الانفراج ---
DIVERGENCE_TIMEFRAME = '4h'
DIVERGENCE_KLINE_LIMIT = 150
DIVERGENCE_PEAK_WINDOW = 5 # عدد الشموع على كل جانب لتحديد القمة/القاع

# --- إعدادات وحدة القناص (Sniper Module) v31 ---
SNIPER_RADAR_RUN_EVERY_MINUTES = 30
SNIPER_TRIGGER_RUN_EVERY_SECONDS = 60
SNIPER_COMPRESSION_PERIOD_HOURS = 6 
SNIPER_MAX_VOLATILITY_PERCENT = 18.0 
SNIPER_BREAKOUT_VOLUME_MULTIPLIER = 3.5 
SNIPER_MIN_USDT_VOLUME = 200000
SNIPER_MIN_TARGET_PERCENT = 3.0 
SNIPER_TREND_TIMEFRAME = '1h'
SNIPER_TREND_PERIOD = 50
SNIPER_OBI_THRESHOLD = 0.15 
SNIPER_ATR_STOP_MULTIPLIER = 2.0 
# [NEW] إعدادات صياد التأكيدات
SNIPER_RETEST_RUN_EVERY_MINUTES = 2
SNIPER_RETEST_TIMEOUT_HOURS = 4
SNIPER_RETEST_PROXIMITY_PERCENT = 0.75 # مدى القرب من منطقة الاختراق لاعتبارها إعادة اختبار

# --- إعدادات صائد الجواهر (Gem Hunter Settings) ---
GEM_MIN_CORRECTION_PERCENT = -70.0
GEM_MIN_24H_VOLUME_USDT = 200000
GEM_MIN_RISE_FROM_ATL_PERCENT = 50.0
GEM_LISTING_SINCE_DATE = datetime(2024, 1, 1, tzinfo=UTC)

# --- إعدادات المهام الدورية ---
RUN_FOMO_SCAN_EVERY_MINUTES = 15
RUN_LISTING_SCAN_EVERY_SECONDS = 60
RUN_PERFORMANCE_TRACKER_EVERY_MINUTES = 5
RUN_DIVERGENCE_SCAN_EVERY_HOURS = 1 # [NEW]
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
SNIPER_EXCLUDED_SUBSTRINGS = ['USD', 'DAI', 'TUSD', 'BUSD']
def is_excluded_symbol(symbol: str) -> bool:
    if len(symbol) > 2 and symbol[-1] in 'LS' and symbol[-2].isdigit():
        return True
    for sub in SNIPER_EXCLUDED_SUBSTRINGS:
        if sub in symbol:
            return True
    return False

sniper_tracker = {p: {} for p in PLATFORMS}
# [NEW] متغيرات عامة للميزات الجديدة
sniper_retest_watchlist = {p: {} for p in PLATFORMS}
recently_alerted_divergence = {}

# =============================================================================
# --- [UPDATED] قسم إدارة المستخدمين وقاعدة البيانات ---
# =============================================================================
def setup_database():
    """إنشاء قاعدة البيانات والجداول إذا لم تكن موجودة"""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    # جدول المستخدمين
    cursor.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)")
    # [NEW] جدول أداء الاستراتيجيات
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS strategy_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            exchange TEXT NOT NULL,
            strategy_type TEXT NOT NULL,
            alert_price REAL NOT NULL,
            alert_timestamp INTEGER NOT NULL,
            status TEXT NOT NULL,
            peak_profit_percent REAL,
            final_profit_percent_24h REAL
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Database is set up and ready.")

def log_strategy_result(symbol, exchange, strategy_type, alert_price, status, peak_profit, final_profit):
    """[NEW] تسجيل نتيجة تنبيه مكتمل في قاعدة البيانات"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO strategy_performance 
            (symbol, exchange, strategy_type, alert_price, alert_timestamp, status, peak_profit_percent, final_profit_percent_24h)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, exchange, strategy_type, alert_price, int(datetime.now(UTC).timestamp()), status, peak_profit, final_profit))
        conn.commit()
        conn.close()
        logger.info(f"STRATEGY LOGGED: {strategy_type} for {symbol} on {exchange} with status {status}.")
    except sqlite3.Error as e:
        logger.error(f"Database error while logging strategy result for {symbol}: {e}")

def get_strategy_stats():
    """[NEW] جلب إحصائيات الأداء من قاعدة البيانات"""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        # جلب البيانات من آخر 30 يومًا فقط
        thirty_days_ago = int((datetime.now(UTC) - timedelta(days=30)).timestamp())
        cursor.execute(
            "SELECT strategy_type, status, peak_profit_percent FROM strategy_performance WHERE alert_timestamp >= ?",
            (thirty_days_ago,)
        )
        records = cursor.fetchall()
        conn.close()
        return records
    except sqlite3.Error as e:
        logger.error(f"Database error while fetching strategy stats: {e}")
        return []

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
            await asyncio.sleep(0.2)
            data = await fetch_json(self.session, f"{self.base_api_url}/spot/candlesticks", params=params)
            if not data: return None
            return [[int(k[0])*1000, k[5], k[3], k[4], k[2], k[1]] for k in data]

    async def get_order_book(self, symbol, limit=20):
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"
        async with api_semaphore:
            params = {'currency_pair': gateio_symbol, 'limit': limit}
            await asyncio.sleep(0.2)
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
# --- 🔬 قسم التحليل الفني والكمي (Quantitative & TA Section) 🔬 ---
# =============================================================================
def calculate_poc(klines, num_bins=50):
    if not klines or len(klines) < 10: return None
    try:
        high_prices = np.array([float(k[2]) for k in klines])
        low_prices = np.array([float(k[3]) for k in klines])
        volumes = np.array([float(k[5]) for k in klines])
        min_price, max_price = np.min(low_prices), np.max(high_prices)
        if max_price == min_price: return min_price
        price_bins = np.linspace(min_price, max_price, num_bins)
        volume_per_bin = np.zeros(num_bins)
        for i in range(len(klines)):
            avg_price = (high_prices[i] + low_prices[i]) / 2
            bin_index = np.searchsorted(price_bins, avg_price) -1
            if 0 <= bin_index < num_bins:
                volume_per_bin[bin_index] += volumes[i]
        if np.sum(volume_per_bin) == 0: return None
        poc_index = np.argmax(volume_per_bin)
        return price_bins[poc_index]
    except Exception as e:
        logger.error(f"Error calculating POC: {e}")
        return None

def calculate_ema_series(prices, period):
    if len(prices) < period: return []
    ema, sma = [], sum(prices[:period]) / period
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
    if len(gains) == 0: return 0
    if len(losses) == 0: return 100
    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)
    if avg_loss == 0: return 100
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

def bollinger_bandwidth(prices, period=20):
    bands = calculate_bollinger_bands(prices, period)
    if not all(bands): return None
    upper, middle, lower = bands
    return ((upper - lower) / middle) * 100 if middle > 0 else None

def find_support_resistance(high_prices, low_prices, window=10):
    supports, resistances = [], []
    for i in range(window, len(high_prices) - window):
        if high_prices[i] == max(high_prices[i-window:i+window+1]): resistances.append(high_prices[i])
        if low_prices[i] == min(low_prices[i-window:i+window+1]): supports.append(low_prices[i])
    return sorted(list(set(supports)), reverse=True), sorted(list(set(resistances)), reverse=True)

def calculate_fibonacci_retracement(high_prices, low_prices, period=FIBONACCI_PERIOD):
    if len(high_prices) < period:
        recent_highs, recent_lows = high_prices, low_prices
    else:
        recent_highs, recent_lows = high_prices[-period:], low_prices[-period:]
    max_price, min_price = np.max(recent_highs), np.min(recent_lows)
    difference = max_price - min_price
    if difference == 0: return {}
    return {
        'level_0.382': max_price - (difference * 0.382),
        'level_0.5': max_price - (difference * 0.5),
        'level_0.618': max_price - (difference * 0.618),
    }

def analyze_trend(current_price, ema21, ema50, sma100):
    if ema21 and ema50 and sma100:
        if current_price > ema21 > ema50 > sma100: return "🟢 اتجاه صاعد قوي.", 2
        if current_price > ema50 and current_price > ema21: return "🟢 اتجاه صاعد.", 1
        if current_price < ema21 < ema50 < sma100: return "🔴 اتجاه هابط قوي.", -2
        if current_price < ema50 and current_price < ema21: return "🔴 اتجاه هابط.", -1
    return "🟡 جانبي / غير واضح.", 0

def order_book_imbalance(bids, asks, top_n=10):
    try:
        b = sum(float(p) * float(q) for p, q in bids[:top_n])
        a = sum(float(p) * float(q) for p, q in asks[:top_n])
        denom = (b + a)
        return (b - a) / denom if denom > 0 else 0.0
    except (ValueError, TypeError):
        return 0.0

# =============================================================================
# --- الوظائف المساعدة للتحليل ---
# =============================================================================
async def helper_get_advanced_momentum(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return []

    potential_coins = [
        p for p in market_data 
        if float(p.get('lastPrice','1')) <= MOMENTUM_MAX_PRICE 
        and MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= MOMENTUM_MAX_VOLUME_24H
    ]
    if not potential_coins: return []

    async def score_candidate(symbol):
        try:
            klines_5m = await client.get_processed_klines(symbol, '5m', 30)
            if not klines_5m or len(klines_5m) < 20: return None

            score = 0
            details = {'symbol': symbol}

            close_prices = np.array([float(k[4]) for k in klines_5m])
            volumes = np.array([float(k[5]) for k in klines_5m])
            current_price = close_prices[-1]
            
            start_price = float(klines_5m[-12][4])
            if start_price > 0:
                price_change = ((current_price - start_price) / start_price) * 100
                if price_change > MOMENTUM_PRICE_INCREASE: score += 1
                details['price_change'] = price_change

            old_volume = sum(float(k[5]) for k in klines_5m[-24:-12]) if len(klines_5m) >= 24 else 0
            new_volume = sum(float(k[5]) for k in klines_5m[-12:])
            if old_volume > 0 and new_volume > old_volume * MOMENTUM_VOLUME_INCREASE: score += 1

            positive_candles = sum(1 for k in klines_5m[-5:] if float(k[4]) > float(k[1]))
            if positive_candles >= 3: score += 1
            details['positive_candles'] = f"{positive_candles}/5"

            rsi = calculate_rsi(close_prices, period=14)
            if rsi:
                details['rsi'] = rsi
                if rsi > 60: score += 1
                if rsi > 85: score -= 1 

            klines_15m = await client.get_processed_klines(symbol, '15m', 20)
            if klines_15m:
                vwap = calculate_vwap([float(k[4]) for k in klines_15m], [float(k[5]) for k in klines_15m], 14)
                if vwap and current_price > vwap: score += 1
            
            details['score'] = score
            details['current_price'] = current_price
            if score >= MOMENTUM_MIN_SCORE: return details
            return None
        except Exception: return None

    tasks = [score_candidate(p['symbol']) for p in potential_coins]
    results = await asyncio.gather(*tasks)
    momentum_coins = [res for res in results if res is not None]
    return sorted(momentum_coins, key=lambda x: x['score'], reverse=True)

async def helper_get_whale_activity(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {}
    potential_gems = [p for p in market_data if float(p.get('lastPrice','999')) <= WHALE_GEM_MAX_PRICE and WHALE_GEM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= WHALE_GEM_MAX_VOLUME_24H]
    if not potential_gems: return {}
    
    for p in potential_gems: p['change_float'] = p.get('priceChangePercent', 0)
    top_gems = sorted(potential_gems, key=lambda x: x['change_float'], reverse=True)[:WHALE_SCAN_CANDIDATE_LIMIT]
    
    tasks = [client.get_order_book(p['symbol'], WHALE_OBI_LEVELS) for p in top_gems]
    all_order_books = await asyncio.gather(*tasks)
    
    whale_signals_by_symbol = {}
    for i, book in enumerate(all_order_books):
        symbol = top_gems[i]['symbol']
        signals = await analyze_order_book_for_whales(book, symbol)
        if signals:
            if symbol not in whale_signals_by_symbol: whale_signals_by_symbol[symbol] = []
            whale_signals_by_symbol[symbol].extend(signals)
    return whale_signals_by_symbol

async def analyze_order_book_for_whales(book, symbol):
    signals = []
    if not book or not book.get('bids') or not book.get('asks'): return signals
    try:
        bids, asks = [], []
        for item in book.get('bids', []):
            if isinstance(item, list) and len(item) == 2:
                try: bids.append((float(item[0]), float(item[1])))
                except (ValueError, TypeError): continue
        for item in book.get('asks', []):
            if isinstance(item, list) and len(item) == 2:
                try: asks.append((float(item[0]), float(item[1])))
                except (ValueError, TypeError): continue

        for price, qty in bids[:5]:
            value = price * qty
            if value >= WHALE_WALL_THRESHOLD_USDT:
                signals.append({'type': 'Buy Wall', 'symbol': symbol, 'value': value, 'price': price}); break
        for price, qty in asks[:5]:
            value = price * qty
            if value >= WHALE_WALL_THRESHOLD_USDT:
                signals.append({'type': 'Sell Wall', 'symbol': symbol, 'value': value, 'price': price}); break
        
        obi = order_book_imbalance(bids, asks, WHALE_OBI_LEVELS)
        if obi > SNIPER_OBI_THRESHOLD: 
            signals.append({'type': 'Buy Pressure', 'symbol': symbol, 'value': obi})
        elif obi < -SNIPER_OBI_THRESHOLD:
             signals.append({'type': 'Sell Pressure', 'symbol': symbol, 'value': obi})

    except Exception as e:
        logger.warning(f"Could not analyze order book for {symbol}: {e}")
    return signals

def find_peaks(data, window):
    """[NEW] دالة مبسطة لإيجاد القمم"""
    return [i for i in range(window, len(data) - window) if data[i] == max(data[i-window:i+window+1])]

def find_troughs(data, window):
    """[NEW] دالة مبسطة لإيجاد القيعان"""
    return [i for i in range(window, len(data) - window) if data[i] == min(data[i-window:i+window+1])]

def find_rsi_divergence(klines):
    """[NEW] دالة للبحث عن الانفراج الإيجابي والسلبي"""
    if not klines or len(klines) < 50:
        return None
    
    prices = np.array([float(k[4]) for k in klines])
    rsi_values = calculate_rsi(prices, period=14)
    if rsi_values is None:
        # إذا كانت الدالة الأصلية ترجع قيمة واحدة، يجب أن نحسبها على شكل سلسلة
        full_rsi = []
        for i in range(14, len(prices)):
            rsi = calculate_rsi(prices[:i+1])
            if rsi: full_rsi.append(rsi)
        if len(full_rsi) < 30 : return None
        rsi_values = np.array(full_rsi)
        prices = prices[-len(rsi_values):]

    # البحث عن آخر قمتين
    price_peaks = find_peaks(prices, DIVERGENCE_PEAK_WINDOW)
    rsi_peaks = find_peaks(rsi_values, DIVERGENCE_PEAK_WINDOW)

    if len(price_peaks) >= 2 and len(rsi_peaks) >= 2:
        p1_idx, p2_idx = price_peaks[-2], price_peaks[-1]
        r1_idx, r2_idx = rsi_peaks[-2], rsi_peaks[-1]
        
        # التأكد من أن القمم متقاربة زمنياً
        if abs(p2_idx - r2_idx) < DIVERGENCE_PEAK_WINDOW:
            if prices[p2_idx] > prices[p1_idx] and rsi_values[r2_idx] < rsi_values[r1_idx]:
                return {'type': 'Bearish', 'price_peak': prices[p2_idx]}

    # البحث عن آخر قاعين
    price_troughs = find_troughs(prices, DIVERGENCE_PEAK_WINDOW)
    rsi_troughs = find_troughs(rsi_values, DIVERGENCE_PEAK_WINDOW)

    if len(price_troughs) >= 2 and len(rsi_troughs) >= 2:
        p1_idx, p2_idx = price_troughs[-2], price_troughs[-1]
        r1_idx, r2_idx = rsi_troughs[-2], rsi_troughs[-1]

        if abs(p2_idx - r2_idx) < DIVERGENCE_PEAK_WINDOW:
            if prices[p2_idx] < prices[p1_idx] and rsi_values[r2_idx] > rsi_values[r1_idx]:
                return {'type': 'Bullish', 'price_trough': prices[p2_idx]}

    return None
    
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
BTN_STRATEGY_STATS = "📊 أداء الاستراتيجيات" # [NEW]
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
        [BTN_TA_PRO, BTN_SCALP_SCAN],
        [BTN_GEM_HUNTER, BTN_SNIPER_LIST],
        [BTN_TOP_GAINERS, BTN_TOP_VOLUME, BTN_TOP_LOSERS],
        [BTN_PERFORMANCE, BTN_STRATEGY_STATS, BTN_STATUS], # [UPDATED]
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
        "أرسل لي رمز أي عملة (مثل BTC)، وسأقدم لك تقريباً فنياً مفصلاً عنها على عدة أطر زمنية في ثوانٍ, لمساعدتك في اتخاذ قراراتك.\n\n"
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
        retest_count = len(sniper_retest_watchlist.get(platform, {})) # [NEW]
        sniper_tracked_count = len(sniper_tracker.get(platform, {}))
        message += f"**منصة {platform}:**\n"
        message += f"    - 🎯 الصفقات المراقبة: {hunts_count}\n"
        message += f"    - 📈 الأداء المتتبع: {perf_count}\n"
        message += f"    - 🔭 أهداف القناص: {sniper_count}\n"
        message += f"    - 🎯 أهداف إعادة الاختبار: {retest_count}\n" # [NEW]
        message += f"    - 🔫 نتائج القناص: {sniper_tracked_count}\n\n"
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

    if context.user_data.get('awaiting_symbol_for_scalp'):
        symbol = text.upper()
        if not symbol.endswith("USDT"): symbol += "USDT"
        context.user_data['awaiting_symbol_for_scalp'] = False
        context.args = [symbol]
        await run_scalp_analysis(update, context)
        return

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

    if button_text in [BTN_SELECT_MEXC, BTN_SELECT_GATEIO, BTN_SELECT_BINANCE, BTN_SELECT_BYBIT, BTN_SELECT_KUCOIN, BTN_SELECT_OKX]:
        await set_exchange(update, context, button_text)
        return
    if button_text in [BTN_TASKS_ON, BTN_TASKS_OFF]:
        await toggle_background_tasks(update, context)
        return
    if button_text == BTN_STATUS:
        await status_command(update, context)
        return
    if button_text == BTN_STRATEGY_STATS: # [NEW]
        await show_strategy_stats(update, context)
        return

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
        report_parts, overall_score = [], 0
        header = f"📊 **التحليل الفني المفصل لـ ${symbol}** ({client.name})\n_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_\n\n"
        
        for tf_name, tf_interval in timeframes.items():
            klines = await client.get_processed_klines(symbol, tf_interval, TA_KLINE_LIMIT)
            tf_report = f"--- **إطار {tf_name}** ---\n"
            if not klines or len(klines) < TA_MIN_KLINE_COUNT:
                tf_report += "لا توجد بيانات كافية للتحليل.\n\n"; report_parts.append(tf_report); continue
            
            close_prices = np.array([float(k[4]) for k in klines[-TA_KLINE_LIMIT:]])
            high_prices = np.array([float(k[2]) for k in klines[-TA_KLINE_LIMIT:]])
            low_prices = np.array([float(k[3]) for k in klines[-TA_KLINE_LIMIT:]])
            current_price = close_prices[-1]
            report_lines, weight = [], tf_weights[tf_name]

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
        await send_long_message(context, chat_id, header + "".join(report_parts), parse_mode=ParseMode.MARKDOWN)

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
        report_parts, overall_score = [], 0
        header = f"⚡️ **التحليل السريع لـ ${symbol}** ({client.name})\n_{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_\n\n"
        
        for tf_name, tf_interval in timeframes.items():
            klines = await client.get_processed_klines(symbol, tf_interval, SCALP_KLINE_LIMIT)
            tf_report, report_lines = f"--- **إطار {tf_name}** ---\n", []
            if not klines or len(klines) < 20:
                tf_report += "لا توجد بيانات كافية.\n\n"; report_parts.append(tf_report); continue

            volumes, close_prices = np.array([float(k[5]) for k in klines]), np.array([float(k[4]) for k in klines])
            avg_volume, last_volume = np.mean(volumes[-20:-1]), volumes[-1]

            if avg_volume > 0:
                if last_volume > avg_volume * 3: report_lines.append(f"🟢 **الفوليوم:** عالٍ جداً ({last_volume/avg_volume:.1f}x)."); overall_score += 2
                elif last_volume > avg_volume * 1.5: report_lines.append(f"🟢 **الفوليوم:** جيد ({last_volume/avg_volume:.1f}x)."); overall_score += 1
                else: report_lines.append("🟡 **الفوليوم:** عادي.")
            else: report_lines.append("🟡 **الفوليوم:** لا توجد بيانات.")

            price_change_5_candles = ((close_prices[-1] - close_prices[-5]) / close_prices[-5]) * 100 if close_prices[-5] > 0 else 0
            if price_change_5_candles > 2.0: report_lines.append(f"🟢 **السعر:** حركة صاعدة قوية (`%{price_change_5_candles:+.1f}`)."); overall_score += 1
            elif price_change_5_candles < -2.0: report_lines.append(f"🔴 **السعر:** حركة هابطة قوية (`%{price_change_5_candles:+.1f}`)."); overall_score -= 1
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
        await send_long_message(context, chat_id, header + "".join(report_parts), parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in scalp analysis for {symbol}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=f"حدث خطأ فادح أثناء تحليل {symbol}.")

async def run_pro_scan(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🎯 **الفحص الاحترافي المطور ({client.name})**\n\n🔍 جارِ تحليل السوق وتصنيف العملات..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    try:
        market_data = await client.get_market_data()
        if not market_data:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق."); return

        candidates = [p['symbol'] for p in market_data if MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume', '0')) <= MOMENTUM_MAX_VOLUME_24H and float(p.get('lastPrice', '1')) <= MOMENTUM_MAX_PRICE]
        if not candidates:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص الاحترافي على {client.name} اكتمل:**\n\nلا توجد عملات مرشحة حالياً."); return

        tasks = [calculate_pro_score(client, symbol) for symbol in candidates[:100]]
        results = await asyncio.gather(*tasks)
        strong_opportunities = [dict(details, **{'symbol': candidates[i]}) for i, (score, details) in enumerate(results) if score >= PRO_SCAN_MIN_SCORE]

        if not strong_opportunities:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص الاحترافي على {client.name} اكتمل:**\n\nلم يتم العثور على فرص قوية."); return

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
    initial_text = f"🚀 **كاشف الزخم المطور ({client.name})**\n\n🔍 جارِ الفحص الكمّي للسوق..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    
    momentum_coins = await helper_get_advanced_momentum(client)
    if not momentum_coins:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **فحص الزخم على {client.name} اكتمل:** لا يوجد زخم قوي حالياً."); return
    
    message = f"🚀 **تقرير الزخم الكمّي ({client.name})** 🚀\n\n"
    for i, coin in enumerate(momentum_coins[:5]):
        price_change_str = f"`%{coin['price_change']:.2f}`" if 'price_change' in coin else "N/A"
        message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n"
                    f"    - **نقاط الزخم:** `{coin['score']}` ⭐\n"
                    f"    - السعر الحالي: `${format_price(coin['current_price'])}`\n"
                    f"    - التغير (60د): {price_change_str}\n\n")
    message += "*(تمت إضافة هذه العملات إلى متتبع الأداء.)*"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    
    now = datetime.now(UTC)
    for coin in momentum_coins[:5]:
        add_to_monitoring(coin['symbol'], float(coin['current_price']), 0, now, "Fomo Hunter", client.name)

async def run_whale_radar_scan(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🐋 **رادار الحيتان ({client.name})**\n\n🔍 جارِ الفحص العميق..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    whale_signals_by_symbol = await helper_get_whale_activity(client)
    if not whale_signals_by_symbol:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **فحص الرادار على {client.name} اكتمل:** لا يوجد نشاط حيتان واضح."); return
    
    message = f"🐋 **تقرير رادار الحيتان ({client.name}) - {datetime.now().strftime('%H:%M:%S')}** 🐋\n\n"
    for symbol, signals in whale_signals_by_symbol.items():
        for signal in signals:
            if signal['type'] == 'Buy Wall':
                message += f"🟢 **حائط شراء على ${symbol.replace('USDT', '')}**\n    - الحجم: `${signal['value']:,.0f}` عند `{format_price(signal['price'])}`\n\n"
            elif signal['type'] == 'Sell Wall':
                message += f"🔴 **حائط بيع على ${symbol.replace('USDT', '')}**\n    - الحجم: `${signal['value']:,.0f}` عند `{format_price(signal['price'])}`\n\n"
            elif signal['type'] == 'Buy Pressure':
                message += f"📈 **ضغط شراء على ${symbol.replace('USDT', '')}**\n    - اختلال الدفتر: `%{signal['value']:.2f}`\n\n"
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def get_performance_report(context, chat_id, message_id):
    try:
        if not any(performance_tracker.values()) and not any(sniper_tracker.values()):
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="ℹ️ لا توجد عملات قيد تتبع الأداء حالياً."); return

        message = "📊 **تقرير الأداء الشامل** 📊\n\n"
        
        all_tracked_items = []
        for platform_name, symbols_data in performance_tracker.items():
            for symbol, data in symbols_data.items():
                if data.get('status') != 'Archived':
                    all_tracked_items.append((symbol, dict(data, **{'exchange': platform_name})))
        
        if all_tracked_items:
            message += "📈 **أداء عملات الزخم** 📈\n\n"
            sorted_symbols = sorted(all_tracked_items, key=lambda item: item[1]['alert_time'], reverse=True)
            for symbol, data in sorted_symbols:
                alert_price = data.get('alert_price',0)
                current_price = data.get('current_price', alert_price)
                high_price = data.get('high_price', alert_price)
                current_change = ((current_price - alert_price) / alert_price) * 100 if alert_price > 0 else 0
                peak_change = ((high_price - alert_price) / alert_price) * 100 if alert_price > 0 else 0
                emoji = "🟢" if current_change >= 0 else "🔴"
                time_since = (datetime.now(UTC) - data['alert_time']).total_seconds()
                time_str = f"{int(time_since/3600)} س و {int((time_since % 3600)/60)} د"
                message += (f"{emoji} **${symbol.replace('USDT','')}** ({data.get('exchange', 'N/A')}) (منذ {time_str})\n"
                            f"    - الحالي: `${format_price(current_price)}` (**{current_change:+.2f}%**)\n"
                            f"    - الأعلى: `${format_price(high_price)}` (**{peak_change:+.2f}%**)\n\n")

        all_sniper_items = []
        for platform_name, symbols_data in sniper_tracker.items():
            for symbol, data in symbols_data.items():
                if data.get('status') == 'Tracking':
                    all_sniper_items.append((symbol, dict(data, **{'exchange': platform_name})))

        if all_sniper_items:
            message += "\n\n🔫 **متابعة نتائج القناص** 🔫\n\n"
            sorted_sniper = sorted(all_sniper_items, key=lambda item: item[1]['alert_time'], reverse=True)
            for symbol, data in sorted_sniper:
                time_since = (datetime.now(UTC) - data['alert_time']).total_seconds()
                time_str = f"{int(time_since/3600)} س و {int((time_since % 3600)/60)} د"
                message += (f"🎯 **${symbol.replace('USDT','')}** ({data.get('exchange', 'N/A')}) (منذ {time_str})\n"
                            f"    - **الحالة:** `قيد المتابعة`\n"
                            f"    - **الهدف:** `{format_price(data['target_price'])}`\n"
                            f"    - **نقطة الفشل:** `{format_price(data['invalidation_price'])}`\n\n")

        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        await send_long_message(context, chat_id, message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in get_performance_report: {e}", exc_info=True)
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ أثناء جلب تقرير الأداء.")
        except Exception: pass

async def run_top_gainers(context, chat_id, message_id, client: BaseExchangeClient):
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"📈 **الأعلى ربحاً ({client.name})**\n\n🔍 جارِ جلب البيانات...")
        market_data = await client.get_market_data()
        if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق."); return
        valid_data = [item for item in market_data if float(item.get('quoteVolume','0')) > MARKET_MOVERS_MIN_VOLUME]
        sorted_data = sorted(valid_data, key=lambda x: x.get('priceChangePercent',0), reverse=True)[:10]
        message = f"📈 **الأعلى ربحاً على {client.name}** 📈\n\n"
        for i, coin in enumerate(sorted_data):
            message += f"**{i+1}. ${coin['symbol'].replace('USDT','')}:** `%{coin.get('priceChangePercent',0):+.2f}`\n"
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Error in top gainers: {e}")

async def run_top_losers(context, chat_id, message_id, client: BaseExchangeClient):
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"📉 **الأعلى خسارة ({client.name})**\n\n🔍 جارِ جلب البيانات...")
        market_data = await client.get_market_data()
        if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق."); return
        valid_data = [item for item in market_data if float(item.get('quoteVolume','0')) > MARKET_MOVERS_MIN_VOLUME]
        sorted_data = sorted(valid_data, key=lambda x: x.get('priceChangePercent',0))[:10]
        message = f"📉 **الأعلى خسارة على {client.name}** 📉\n\n"
        for i, coin in enumerate(sorted_data):
            message += f"**{i+1}. ${coin['symbol'].replace('USDT','')}:** `%{coin.get('priceChangePercent',0):+.2f}`\n"
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Error in top losers: {e}")

async def run_top_volume(context, chat_id, message_id, client: BaseExchangeClient):
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"💰 **الأعلى تداولاً ({client.name})**\n\n🔍 جارِ جلب البيانات...")
        market_data = await client.get_market_data()
        if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق."); return
        for item in market_data: item['quoteVolume_f'] = float(item.get('quoteVolume','0'))
        sorted_data = sorted(market_data, key=lambda x: x['quoteVolume_f'], reverse=True)[:10]
        message = f"💰 **الأعلى تداولاً على {client.name}** 💰\n\n"
        for i, coin in enumerate(sorted_data):
            volume = coin['quoteVolume_f']
            volume_str = f"{volume/1_000_000:.2f}M" if volume > 1_000_000 else f"{volume/1_000:.1f}K"
            message += f"**{i+1}. ${coin['symbol'].replace('USDT','')}:** (الحجم: `${volume_str}`)\n"
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Error in top volume: {e}")

async def show_sniper_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, any_watched = "🔭 **قائمة مراقبة القناص** 🔭\n\n", False
    for platform, watchlist in sniper_watchlist.items():
        if watchlist:
            any_watched = True
            message += f"--- **{platform}** ---\n"
            for symbol, data in list(watchlist.items())[:5]:
                poc_str = f", POC: `{format_price(data['poc'])}`" if 'poc' in data else ""
                message += (f"- `${symbol.replace('USDT','')}` (نطاق: "
                            f"`{format_price(data['low'])}` - `{format_price(data['high'])}`{poc_str})\n")
            if len(watchlist) > 5: message += f"    *... و {len(watchlist) - 5} عملات أخرى.*\n"
            message += "\n"
    if not any_watched: message += "لا توجد أهداف حالية في قائمة المراقبة. يقوم الرادار بالبحث..."
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def run_gem_hunter_scan(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"💎 **صائد الجواهر ({client.name})**\n\n🔍 جارِ تنفيذ مسح عميق للسوق..."
    try: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    try:
        market_data = await client.get_market_data()
        if not market_data: await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ في جلب بيانات السوق."); return
        
        final_gems = []
        for coin in [c for c in market_data if float(c.get('quoteVolume', '0')) > GEM_MIN_24H_VOLUME_USDT]:
            try:
                klines = await client.get_processed_klines(coin['symbol'], '1d', 1000)
                if not klines or len(klines) < 10: continue
                if datetime.fromtimestamp(int(klines[0][0])/1000, tz=UTC) < GEM_LISTING_SINCE_DATE: continue

                highs, lows, closes = [float(k[2]) for k in klines], [float(k[3]) for k in klines], [float(k[4]) for k in klines]
                ath, atl, current = np.max(highs), np.min(lows), closes[-1]
                if ath == 0 or current == 0: continue

                correction = ((current - ath) / ath) * 100
                rise_from_atl = ((current - atl) / atl) * 100 if atl > 0 else float('inf')
                if correction <= GEM_MIN_CORRECTION_PERCENT and rise_from_atl >= GEM_MIN_RISE_FROM_ATL_PERCENT and current > np.mean(closes[-20:]):
                    final_gems.append({'symbol': coin['symbol'], 'potential_x': ath / current, 'correction_percent': correction})
            except Exception as e: logger.warning(f"Could not process gem candidate {coin['symbol']}: {e}")

        if not final_gems:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **بحث الجواهر اكتمل:** لم يتم العثور على عملات تتوافق مع الشروط."); return

        sorted_gems = sorted(final_gems, key=lambda x: x['potential_x'], reverse=True)
        message = f"💎 **تقرير الجواهر المخفية ({client.name})** 💎\n\n"
        for gem in sorted_gems[:5]:
            message += (f"**${gem['symbol'].replace('USDT', '')}**\n"
                        f"  - 🚀 **العودة للقمة: {gem['potential_x']:.1f}X**\n"
                        f"  - 🩸 مصححة بنسبة: {gem['correction_percent']:.1f}%\n\n")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in run_gem_hunter_scan: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ فادح أثناء البحث عن الجواهر.")

async def show_strategy_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[NEW] يعرض تقرير أداء الاستراتيجيات"""
    sent_message = await update.message.reply_text("📊 جارِ تحليل أداء الاستراتيجيات...")
    
    records = get_strategy_stats()
    if not records:
        await sent_message.edit_text("لا توجد بيانات كافية لعرض الإحصائيات. سيتم تجميع البيانات مع مرور الوقت.")
        return

    stats = {}
    for strategy, status, peak_profit in records:
        if strategy not in stats:
            stats[strategy] = {'total': 0, 'success': 0, 'failure': 0, 'profits': []}
        stats[strategy]['total'] += 1
        stats[strategy]['profits'].append(peak_profit or 0)
        if status == 'Success':
            stats[strategy]['success'] += 1
        elif status == 'Failure':
            stats[strategy]['failure'] += 1

    message = "📊 **تقرير أداء الاستراتيجيات (آخر 30 يومًا)** 📊\n\n"
    for strategy, data in stats.items():
        # تحديد النجاح: للقناص هو وصول الهدف، ولغيره هو تحقيق ربح
        if strategy == 'Sniper':
            success_rate = (data['success'] / data['total']) * 100 if data['total'] > 0 else 0
        else:
            successful_trades = sum(1 for p in data['profits'] if p > 2.0) # اعتبر النجاح > 2% ربح
            success_rate = (successful_trades / len(data['profits'])) * 100 if data['profits'] else 0

        avg_profit = np.mean(data['profits']) if data['profits'] else 0

        message += f"--- **{strategy}** ---\n"
        message += f"- **إجمالي التنبيهات:** {data['total']}\n"
        message += f"- **نسبة النجاح:** {success_rate:.1f}%\n"
        message += f"- **متوسط أقصى ربح:** {avg_profit:.2f}%\n\n"
    
    await sent_message.edit_text(message)

# =============================================================================
# --- 5. المهام الآلية الدورية (تم التحديث) ---
# =============================================================================
def add_to_monitoring(symbol, alert_price, peak_volume, alert_time, source, exchange_name):
    if symbol not in performance_tracker[exchange_name]:
        performance_tracker[exchange_name][symbol] = {'alert_price': alert_price, 'alert_time': alert_time, 'source': source, 'current_price': alert_price, 'high_price': alert_price, 'status': 'Tracking', 'momentum_lost_alerted': False}
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol} on {exchange_name}")

async def fomo_hunter_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    logger.info(f"Fomo Hunter (v31) background task started for {client.name}.")
    while True:
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)
        if not bot_data.get('background_tasks_enabled', True): continue
        logger.info(f"===== Fomo Hunter ({client.name}): Starting Advanced Scan =====")
        try:
            momentum_coins = await helper_get_advanced_momentum(client)
            if not momentum_coins: continue
                
            now, new_alerts = datetime.now(UTC), []
            for coin in momentum_coins:
                symbol = coin['symbol']
                last_alert = recently_alerted_fomo[client.name].get(symbol)
                if not last_alert or (now - last_alert) > timedelta(hours=1):
                     new_alerts.append(coin)
                     recently_alerted_fomo[client.name][symbol] = now
                     
            if not new_alerts: continue
            
            message = f"🚨 **تنبيه تلقائي من صياد الزخم ({client.name})** 🚨\n\n"
            for i, coin in enumerate(new_alerts[:3]):
                message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n"
                            f"    - **نقاط الزخم:** `{coin['score']}` ⭐\n"
                            f"    - السعر: `${format_price(coin['current_price'])}`\n\n")
            await broadcast_message(bot, message)

            for coin in new_alerts[:3]:
                add_to_monitoring(coin['symbol'], float(coin['current_price']), 0, now, "Fomo Hunter", client.name)
        except Exception as e:
            logger.error(f"Error in fomo_hunter_loop for {client.name}: {e}", exc_info=True)

async def new_listings_sniper_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
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
            
            if newly_listed := current_symbols - known_symbols[client.name]:
                for symbol in newly_listed:
                    logger.info(f"Sniper ({client.name}): NEW LISTING DETECTED: {symbol}")
                    await broadcast_message(bot, f"🎯 **إدراج جديد على {client.name}:** `${symbol}`")
                known_symbols[client.name].update(newly_listed)
        except Exception as e:
            logger.error(f"An unexpected error in new_listings_sniper_loop for {client.name}: {e}")

async def performance_tracker_loop(session: aiohttp.ClientSession, bot: Bot):
    logger.info("Performance Tracker background task started.")
    while True:
        await asyncio.sleep(RUN_PERFORMANCE_TRACKER_EVERY_MINUTES * 60)
        now = datetime.now(UTC)
        client_cache = {}

        # [UPDATED] Loop for performance_tracker (Momentum, etc.)
        for platform in PLATFORMS:
            for symbol, data in list(performance_tracker[platform].items()):
                if data.get('status') == 'Archived': continue
                
                try:
                    if platform not in client_cache: client_cache[platform] = get_exchange_client(platform, session)
                    client = client_cache[platform]
                    if not client: continue

                    if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                        # Log to DB before archiving
                        alert_price = data['alert_price']
                        peak_profit = ((data['high_price'] - alert_price) / alert_price) * 100 if alert_price > 0 else 0
                        final_profit = ((data['current_price'] - alert_price) / alert_price) * 100 if alert_price > 0 else 0
                        log_strategy_result(symbol, platform, data['source'], alert_price, 'Expired', peak_profit, final_profit)
                        data['status'] = 'Archived'
                        del performance_tracker[platform][symbol]
                        continue

                    current_price = await client.get_current_price(symbol)
                    if not current_price: continue

                    data['current_price'] = current_price
                    if current_price > data.get('high_price', 0): data['high_price'] = current_price
                    
                    if not data.get('momentum_lost_alerted', False) and data['high_price'] > 0:
                        price_drop = ((current_price - data['high_price']) / data['high_price']) * 100
                        if price_drop <= MOMENTUM_LOSS_THRESHOLD_PERCENT:
                            message = (f"⚠️ **تنبيه: فقدان الزخم لـ ${symbol.replace('USDT','')}** ({platform})\n"
                                       f"    - هبط بنسبة `{price_drop:.2f}%` من أعلى قمة.")
                            await broadcast_message(bot, message)
                            data['momentum_lost_alerted'] = True
                            logger.info(f"MOMENTUM LOSS ALERT sent for {symbol} on {platform}")
                except Exception as e:
                    logger.error(f"Error updating price for {symbol} on {platform}: {e}")

        # [UPDATED] Loop for sniper_tracker
        for platform in PLATFORMS:
            for symbol, data in list(sniper_tracker[platform].items()):
                if data['status'] != 'Tracking': continue
                try:
                    if platform not in client_cache: client_cache[platform] = get_exchange_client(platform, session)
                    client = client_cache[platform]
                    if not client: continue

                    if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                        log_strategy_result(symbol, platform, 'Sniper', data['alert_price'], 'Expired', data.get('peak_profit',0), data.get('final_profit',0))
                        del sniper_tracker[platform][symbol]
                        continue
                    
                    current_price = await client.get_current_price(symbol)
                    if not current_price: continue
                    
                    alert_price = data['alert_price']
                    data['peak_profit'] = max(data.get('peak_profit',0), ((current_price - alert_price)/alert_price)*100)
                    data['final_profit'] = ((current_price - alert_price)/alert_price)*100

                    if current_price >= data['target_price']:
                        await broadcast_message(bot, f"✅ **القناص: نجاح!** ✅\n\n**العملة:** `${symbol}` ({platform})\n**النتيجة:** وصلت للهدف بنجاح.")
                        logger.info(f"SNIPER TRACKER ({platform}): {symbol} SUCCEEDED.")
                        log_strategy_result(symbol, platform, 'Sniper', alert_price, 'Success', data['peak_profit'], data['final_profit'])
                        del sniper_tracker[platform][symbol]
                    elif current_price <= data['invalidation_price']:
                        await broadcast_message(bot, f"❌ **القناص: فشل.** ❌\n\n**العملة:** `${symbol}` ({platform})\n**النتيجة:** فشل الاختراق وعاد السعر تحت نقطة الإبطال.")
                        logger.info(f"SNIPER TRACKER ({platform}): {symbol} FAILED.")
                        log_strategy_result(symbol, platform, 'Sniper', alert_price, 'Failure', data['peak_profit'], data['final_profit'])
                        del sniper_tracker[platform][symbol]

                except Exception as e:
                     logger.error(f"Error in Sniper Tracker for {symbol} on {platform}: {e}")

async def coiled_spring_radar_loop(client: BaseExchangeClient, bot_data: dict):
    logger.info(f"Sniper Radar (v31) background task started for {client.name}.")
    while True:
        await asyncio.sleep(SNIPER_RADAR_RUN_EVERY_MINUTES * 60)
        if not bot_data.get('background_tasks_enabled', True): continue
        logger.info(f"===== Sniper Radar ({client.name}): Searching for coiled springs =====")
        try:
            market_data = await client.get_market_data()
            if not market_data: continue
            
            candidates = [p for p in market_data if float(p.get('quoteVolume', '0')) > SNIPER_MIN_USDT_VOLUME and not is_excluded_symbol(p['symbol'])]

            async def check_candidate(symbol):
                klines = await client.get_processed_klines(symbol, '15m', int(SNIPER_COMPRESSION_PERIOD_HOURS * 4))
                if not klines or len(klines) < int(SNIPER_COMPRESSION_PERIOD_HOURS * 4): return
                highs, lows, vols = [float(k[2]) for k in klines], [float(k[3]) for k in klines], [float(k[5]) for k in klines]
                highest, lowest = np.max(highs), np.min(lows)
                if lowest == 0: return
                
                if ((highest - lowest) / lowest) * 100 <= SNIPER_MAX_VOLATILITY_PERCENT:
                    if poc := calculate_poc(klines):
                        if symbol not in sniper_watchlist[client.name]:
                            sniper_watchlist[client.name][symbol] = {'high': highest, 'low': lowest, 'poc': poc}
                            logger.info(f"SNIPER RADAR ({client.name}): Added {symbol} to watchlist.")

            await asyncio.gather(*[check_candidate(p['symbol']) for p in candidates])
        except Exception as e:
            logger.error(f"Error in coiled_spring_radar_loop for {client.name}: {e}", exc_info=True)

async def breakout_trigger_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    logger.info(f"Sniper Trigger (v31) background task started for {client.name}.")
    while True:
        await asyncio.sleep(SNIPER_TRIGGER_RUN_EVERY_SECONDS)
        if not bot_data.get('background_tasks_enabled', True) or not sniper_watchlist[client.name]: continue

        for symbol, data in list(sniper_watchlist[client.name].items()):
            try:
                if (data['high'] - data['low']) / data['high'] * 100 < SNIPER_MIN_TARGET_PERCENT:
                    del sniper_watchlist[client.name][symbol]; continue
                
                trend_klines = await client.get_processed_klines(symbol, SNIPER_TREND_TIMEFRAME, SNIPER_TREND_PERIOD + 5)
                if not trend_klines or len(trend_klines) < SNIPER_TREND_PERIOD: continue
                trend_sma = calculate_sma([float(k[4]) for k in trend_klines], SNIPER_TREND_PERIOD)
                if trend_sma is None or float(trend_klines[-1][4]) < trend_sma: continue 

                klines = await client.get_processed_klines(symbol, '5m', 25)
                if not klines or len(klines) < 20: continue
                confirmation_candle, trigger_candle = klines[-2], klines[-3]
                breakout_level = data['high']
                
                if float(trigger_candle[2]) < breakout_level or float(confirmation_candle[4]) < breakout_level: continue
                if float(trigger_candle[5]) < np.mean([float(k[5]) for k in klines[-22:-2]]) * SNIPER_BREAKOUT_VOLUME_MULTIPLIER: continue
                
                order_book = await client.get_order_book(symbol, WHALE_OBI_LEVELS)
                if not order_book or order_book_imbalance(order_book.get('bids',[]), order_book.get('asks',[])) < SNIPER_OBI_THRESHOLD: continue
                
                vwap_klines = await client.get_processed_klines(symbol, '15m', 20)
                if not vwap_klines: continue
                vwap_15m = calculate_vwap([float(k[4]) for k in vwap_klines], [float(k[5]) for k in vwap_klines], 14)
                if vwap_15m and float(confirmation_candle[4]) < vwap_15m: continue

                # --- All Filters Passed: Generate Signal ---
                alert_price, range_height = float(confirmation_candle[4]), data['high'] - data['low']
                target_price = data['high'] + range_height
                atr_klines = await client.get_processed_klines(symbol, '15m', 15)
                atr_val = calculate_atr([float(k[2]) for k in atr_klines], [float(k[3]) for k in atr_klines], [float(k[4]) for k in atr_klines])
                stop_loss = alert_price - (atr_val * SNIPER_ATR_STOP_MULTIPLIER) if atr_val else data['low']

                message = (f"🎯 **قناص: اختراق عالي الجودة!** 🎯\n\n"
                           f"**العملة:** `${symbol}` ({client.name})\n"
                           f"**النمط:** اختراق مؤكد بالاتجاه، الحجم، وتدفق الأوامر.\n"
                           f"**سعر التأكيد:** `{format_price(alert_price)}`\n\n"
                           f"📝 **خطة المراقبة:**\n- **وقف الخسارة:** `{format_price(stop_loss)}`\n- **الهدف:** `{format_price(target_price)}`")
                await broadcast_message(bot, message)
                
                sniper_tracker[client.name][symbol] = {'alert_time': datetime.now(UTC), 'alert_price': alert_price, 'target_price': target_price, 'invalidation_price': stop_loss, 'status': 'Tracking'}
                # [NEW] Add to re-test watchlist
                sniper_retest_watchlist[client.name][symbol] = {'breakout_level': breakout_level, 'timestamp': datetime.now(UTC)}
                
                del sniper_watchlist[client.name][symbol]
            except Exception as e:
                 logger.error(f"Error in breakout_trigger_loop for {symbol}: {e}", exc_info=True)
                 if symbol in sniper_watchlist[client.name]: del sniper_watchlist[client.name][symbol]

async def retest_hunter_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    """[NEW] مهمة خلفية لصيد فرص إعادة الاختبار"""
    logger.info(f"Sniper Retest Hunter (v31) background task started for {client.name}.")
    while True:
        await asyncio.sleep(SNIPER_RETEST_RUN_EVERY_MINUTES * 60)
        if not bot_data.get('background_tasks_enabled', True) or not sniper_retest_watchlist[client.name]: continue

        for symbol, data in list(sniper_retest_watchlist[client.name].items()):
            try:
                if datetime.now(UTC) - data['timestamp'] > timedelta(hours=SNIPER_RETEST_TIMEOUT_HOURS):
                    del sniper_retest_watchlist[client.name][symbol]; continue
                
                klines = await client.get_processed_klines(symbol, '5m', 20)
                if not klines or len(klines) < 10: continue

                last_candle, prev_candles = klines[-1], klines[-10:-1]
                low_price, close_price, open_price = float(last_candle[3]), float(last_candle[4]), float(last_candle[1])
                breakout_level = data['breakout_level']

                # هل السعر قريب من منطقة الاختبار؟
                if breakout_level * (1 - SNIPER_RETEST_PROXIMITY_PERCENT/100) <= low_price <= breakout_level * (1 + SNIPER_RETEST_PROXIMITY_PERCENT/100):
                    # هل هناك علامات ارتداد؟
                    is_bullish_candle = close_price > open_price
                    avg_volume = np.mean([float(k[5]) for k in prev_candles])
                    is_high_volume = float(last_candle[5]) > avg_volume * 1.5

                    if is_bullish_candle and is_high_volume:
                        message = (f"🎯 **قناص (تأكيد): فرصة إعادة اختبار!** 🎯\n\n"
                                   f"**العملة:** `${symbol}` ({client.name})\n"
                                   f"**النمط:** ارتداد ناجح من منطقة الاختراق عند `{format_price(breakout_level)}`.\n"
                                   f"**السعر الحالي:** `{format_price(close_price)}`\n\n"
                                   f"*(قد تكون هذه إشارة دخول ثانية أكثر أمانًا)*")
                        await broadcast_message(bot, message)
                        logger.info(f"SNIPER RETEST ({client.name}): Confirmed re-test for {symbol}!")
                        del sniper_retest_watchlist[client.name][symbol] # إزالة بعد إرسال التنبيه
            except Exception as e:
                logger.error(f"Error in retest_hunter_loop for {symbol}: {e}")
                if symbol in sniper_retest_watchlist[client.name]: del sniper_retest_watchlist[client.name][symbol]

async def divergence_detector_loop(bot: Bot, bot_data: dict, session: aiohttp.ClientSession):
    """[NEW] مهمة خلفية لكشف الانفراج"""
    logger.info("Divergence Detector (v31) background task started.")
    # استخدام عميل Binance كمرجع للسوق العام
    client = get_exchange_client('binance', session)
    if not client:
        logger.error("Divergence detector failed to start: Binance client not available.")
        return

    while True:
        await asyncio.sleep(RUN_DIVERGENCE_SCAN_EVERY_HOURS * 3600)
        if not bot_data.get('background_tasks_enabled', True): continue
        logger.info("===== Divergence Detector: Starting Market Scan =====")
        try:
            market_data = await client.get_market_data()
            if not market_data: continue
            
            top_coins_by_vol = sorted([c for c in market_data if c['symbol'].endswith('USDT')], key=lambda x: float(x.get('quoteVolume', '0')), reverse=True)[:50]
            
            for coin in top_coins_by_vol:
                symbol = coin['symbol']
                try:
                    klines = await client.get_processed_klines(symbol, DIVERGENCE_TIMEFRAME, DIVERGENCE_KLINE_LIMIT)
                    divergence = find_rsi_divergence(klines)

                    if divergence:
                        alert_key = f"{symbol}_{divergence['type']}"
                        last_alert_time = recently_alerted_divergence.get(alert_key)
                        
                        # إرسال تنبيه لنفس الانفراج مرة كل 12 ساعة كحد أقصى
                        if not last_alert_time or (datetime.now(UTC) - last_alert_time) > timedelta(hours=12):
                            if divergence['type'] == 'Bearish':
                                message = (f"⚠️ **رصد انفراج سلبي على ${symbol}** ({client.name})\n\n"
                                           f"إطار: `{DIVERGENCE_TIMEFRAME}`. قد يسبق هذا حركة تصحيحية هابطة.")
                            else: # Bullish
                                message = (f"✅ **رصد انفراج إيجابي على ${symbol}** ({client.name})\n\n"
                                           f"إطار: `{DIVERGENCE_TIMEFRAME}`. قد يسبق هذا حركة صاعدة.")
                            
                            await broadcast_message(bot, message)
                            recently_alerted_divergence[alert_key] = datetime.now(UTC)
                            logger.info(f"DIVERGENCE ALERT: {divergence['type']} on {symbol}")
                except Exception as e:
                    logger.warning(f"Could not check divergence for {symbol}: {e}")
        except Exception as e:
            logger.error(f"Major error in divergence_detector_loop: {e}", exc_info=True)

# =============================================================================
# --- 6. تشغيل البوت (تم التحديث) ---
# =============================================================================
async def send_startup_message(bot: Bot):
    try:
        await broadcast_message(bot, "✅ **بوت الصياد الذكي (v31.0 - استراتيجيات متقدمة) متصل الآن!**\n\nأرسل /start لعرض القائمة.")
        logger.info("Startup message sent successfully to all users.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

async def post_init(application: Application):
    logger.info("Bot initialized. Starting background tasks...")
    session = aiohttp.ClientSession()
    application.bot_data["session"] = session
    bot, bot_data = application.bot, application.bot_data

    # بدء المهام الخلفية
    application.bot_data['task_performance'] = asyncio.create_task(performance_tracker_loop(session, bot))
    application.bot_data['task_divergence'] = asyncio.create_task(divergence_detector_loop(bot, bot_data, session)) # [NEW]

    for platform_name in PLATFORMS:
        if client := get_exchange_client(platform_name.lower(), session):
            application.bot_data[f'task_fomo_{platform_name}'] = asyncio.create_task(fomo_hunter_loop(client, bot, bot_data))
            application.bot_data[f'task_listings_{platform_name}'] = asyncio.create_task(new_listings_sniper_loop(client, bot, bot_data))
            application.bot_data[f'task_sniper_radar_{platform_name}'] = asyncio.create_task(coiled_spring_radar_loop(client, bot_data))
            application.bot_data[f'task_sniper_trigger_{platform_name}'] = asyncio.create_task(breakout_trigger_loop(client, bot, bot_data))
            application.bot_data[f'task_sniper_retest_{platform_name}'] = asyncio.create_task(retest_hunter_loop(client, bot, bot_data)) # [NEW]

    await send_startup_message(bot)

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
