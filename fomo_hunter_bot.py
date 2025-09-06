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
# --- الإعدادات الرئيسية ---
# =============================================================================

# --- Telegram Configuration ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
# [CRITICAL] ضع مفتاح Gemini API الخاص بك هنا
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', 'PASTE_YOUR_GEMINI_API_KEY_HERE')
DATABASE_FILE = "users_v33.db"

# --- إعدادات كاشف الزخم ---
MOMENTUM_MAX_PRICE = 0.10
MOMENTUM_MIN_VOLUME_24H = 50000
MOMENTUM_MAX_VOLUME_24H = 2000000
MOMENTUM_PRICE_INCREASE = 4.0
MOMENTUM_VOLUME_INCREASE = 1.8
MOMENTUM_MIN_SCORE = 3
MOMENTUM_LOSS_THRESHOLD_PERCENT = -10.0

# --- إعدادات كاشف الانفراج ---
DIVERGENCE_TIMEFRAME = '4h'
DIVERGENCE_KLINE_LIMIT = 150
DIVERGENCE_PEAK_WINDOW = 5

# --- إعدادات وحدة القناص (Sniper Module) v33 ---
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
SNIPER_RETEST_RUN_EVERY_MINUTES = 2
SNIPER_RETEST_TIMEOUT_HOURS = 4
SNIPER_RETEST_PROXIMITY_PERCENT = 0.75

# --- إعدادات صائد الجواهر ---
GEM_MIN_CORRECTION_PERCENT = -70.0
GEM_MIN_24H_VOLUME_USDT = 200000
GEM_MIN_RISE_FROM_ATL_PERCENT = 50.0
GEM_LISTING_SINCE_DATE = datetime(2024, 1, 1, tzinfo=UTC)

# --- إعدادات المهام الدورية ---
RUN_FOMO_SCAN_EVERY_MINUTES = 15
RUN_LISTING_SCAN_EVERY_SECONDS = 60
RUN_PERFORMANCE_TRACKER_EVERY_MINUTES = 5
RUN_DIVERGENCE_SCAN_EVERY_HOURS = 1
PERFORMANCE_TRACKING_DURATION_HOURS = 24
MARKET_MOVERS_MIN_VOLUME = 50000

# --- إعدادات عامة ---
HTTP_TIMEOUT = 15
API_CONCURRENCY_LIMIT = 8
TELEGRAM_MESSAGE_LIMIT = 4096
# [FIXED] تم تحديث اسم النموذج إلى الإصدار الصحيح والمستقر
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-preview-05-20:generateContent?key={GEMINI_API_KEY}"

# --- إعدادات تسجيل الأخطاء ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة ---
# =============================================================================
api_semaphore = asyncio.Semaphore(API_CONCURRENCY_LIMIT)
PLATFORMS = ["MEXC", "Gate.io", "Binance", "Bybit", "KuCoin", "OKX"]
performance_tracker = {p: {} for p in PLATFORMS}
known_symbols = {p: set() for p in PLATFORMS}
recently_alerted_fomo = {p: {} for p in PLATFORMS}
sniper_watchlist = {p: {} for p in PLATFORMS}
sniper_tracker = {p: {} for p in PLATFORMS}
sniper_retest_watchlist = {p: {} for p in PLATFORMS}
recently_alerted_divergence = {}
SNIPER_EXCLUDED_SUBSTRINGS = ['USD', 'DAI', 'TUSD', 'BUSD']

def is_excluded_symbol(symbol: str) -> bool:
    if len(symbol) > 2 and symbol[-1] in 'LS' and symbol[-2].isdigit(): return True
    for sub in SNIPER_EXCLUDED_SUBSTRINGS:
        if sub in symbol: return True
    return False

# =============================================================================
# --- 🧠 طبقة الذكاء الاصطناعي (Gemini AI Layer) 🧠 ---
# =============================================================================

PROMPT_TEMPLATES = {
    "PRO_SCAN": """
    **الدور:** أنت محلل فني خبير تقوم بتقديم خلاصة "الفحص الاحترافي" للمتداولين.
    **المهمة:**
    1.  ابدأ بعنوان "🎯 تحليل الفحص الاحترافي 🎯".
    2.  اشرح أن هذا الفحص يقوم بتقييم العملات بناءً على عدة مؤشرات فنية ويعطيها "نقاط" تعبر عن قوتها الحالية.
    3.  حلل "البيانات" المقدمة، وركز على أفضل عملتين من حيث النقاط (Final Score).
    4.  لكل من العملتين، اذكر بإيجاز نقاط قوتها الرئيسية (مثلاً: "تتميز باتجاه صاعد قوي وزخم متزايد").
    5.  اختتم بنصيحة "العملات ذات النقاط العالية تستحق المراقبة، ولكن تذكر أن تقوم ببحثك الخاص."
    **البيانات:**\n{data}
    """,
    "MOMENTUM_SCAN": """
    **الدور:** أنت خبير تحليل فني ومهمتك هي شرح تقرير "كاشف الزخم" لمستخدمي بوت تداول.
    **المهمة:**
    1.  ابدأ بعنوان جذاب مثل "🚀 تحليل تقرير الزخم الكمّي 🚀".
    2.  اشرح بأسلوب بسيط ماذا يعني "الزخم" (ارتفاع سريع في السعر مع حجم تداول عالٍ).
    3.  حلل "البيانات" المقدمة وركز على العملة التي حصلت على أعلى نقاط (score).
    4.  اذكر لماذا تبدو هذه العملة مثيرة للاهتمام (مثلاً: نقاطها العالية تدل على قوة شرائية كبيرة).
    5.  اختتم بنصيحة عامة مثل "هذه العملات تشهد اهتمامًا عاليًا الآن، ولكن الزخم قد يكون متقلبًا. كن حذرًا."
    **البيانات:**\n{data}
    """,
    "WHALE_RADAR": """
    **الدور:** أنت خبير في تحليل سيولة السوق ودفتر الأوامر.
    **المهمة:**
    1.  ابدأ بعنوان جذاب مثل "🐋 تحليل رادار الحيتان 🐋".
    2.  اشرح بأسلوب بسيط مفاهيم "حائط الشراء" (دعم قوي محتمل)، "حائط البيع" (مقاومة قوية محتملة)، و"ضغط الشراء/البيع" (سيطرة المشترين أو البائعين).
    3.  حلل "البيانات" المقدمة واختر أهم إشارة تم رصدها (مثلاً أكبر حائط شراء).
    4.  اشرح التأثير المحتمل لهذه الإشارة على سعر العملة.
    5.  اختتم بتحذير "نشاط الحيتان قد يكون تلاعبيًا أحيانًا، لا تعتمد عليه وحده."
    **البيانات:**\n{data}
    """,
    "GEM_HUNTER": """
    **الدور:** أنت محلل استثماري متخصص في إيجاد الفرص طويلة الأمد.
    **المهمة:**
    1.  ابدأ بعنوان جذاب مثل "💎 تحليل تقرير الجواهر المخفية 💎".
    2.  اشرح استراتيجية "صائد الجواهر" ببساطة (البحث عن مشاريع قوية صححت بقوة وبدأت بالتعافي).
    3.  حلل "البيانات" المقدمة، وركز على العملة ذات أعلى عائد محتمل (potential_x).
    4.  اشرح ماذا تعني الأرقام (مثلاً: "تصحيح 90% يعني أن السعر الحالي رخيص جدًا مقارنة بقمته، والعودة للقمة 10X هو العائد المحتمل إذا استعاد المشروع زخمه").
    5.  اختتم بنصيحة "هذه استراتيجية للمدى الطويل وتتطلب صبرًا وبحثًا إضافيًا في أساسيات المشاريع."
    **البيانات:**\n{data}
    """,
    "SNIPER_TRIGGER": """
    **الدور:** أنت قناص فني (Technical Sniper) متخصص في الاختراقات.
    **المهمة:**
    1.  ابدأ بعنوان حماسي مثل "🎯 قناص: تأكيد اختراق عالي الجودة! 🎯".
    2.  اشرح أن هذه الإشارة جاءت بعد أن نجحت العملة في اجتياز عدة فلاتر صارمة (انضغاط سعري، حجم تداول، اتجاه عام، ضغط شراء).
    3.  اعرض تفاصيل "البيانات" بوضوح (العملة، سعر التأكيد، الهدف، وقف الخسارة).
    4.  اشرح أهمية الالتزام بخطة المراقبة المذكورة.
    5.  اختتم بـ "تمت فلترة الإشارة بدقة. راقب الخطة جيدًا وقم بإدارة المخاطر."
    **البيانات:**\n{data}
    """,
    "SNIPER_RETEST": """
    **الدور:** أنت محلل فني متخصص في تأكيد الاختراقات.
    **المهمة:**
    1.  ابدأ بعنوان مشجع مثل "🎯 قناص (تأكيد): فرصة إعادة اختبار ناجحة! 🎯".
    2.  اشرح للمستخدم ماذا يعني "إعادة الاختبار" (السعر يخترق مقاومة ثم يعود ليختبرها كدعم جديد).
    3.  وضح أن هذا النموذج يعتبره الكثيرون "إشارة دخول ثانية أكثر أمانًا" من الاختراق الأول.
    4.  اعرض تفاصيل "البيانات" (العملة، منطقة الدعم التي تم الارتداد منها، السعر الحالي).
    5.  اختتم بـ "الارتداد الناجح من هذه المنطقة يعزز النظرة الإيجابية للعملة."
    **البيانات:**\n{data}
    """,
    "DIVERGENCE_SCAN": """
    **الدور:** أنت محلل فني خبير في المؤشرات الاستباقية.
    **المهمة:**
    1.  ابدأ بعنوان تحذيري أو إيجابي حسب نوع الانفراج، مثل "⚠️ تحليل: رصد انفراج سلبي!" أو "✅ تحليل: رصد انفراج إيجابي!".
    2.  اشرح ببساطة مفهوم "الانفراج" (عندما يتعارض اتجاه السعر مع اتجاه مؤشر القوة النسبية RSI).
    3.  وضح أن هذه "إشارة استباقية" قد تتنبأ بانعكاس السعر القادم.
    4.  اعرض تفاصيل "البيانات" (العملة، نوع الانفراج، الإطار الزمني).
    5.  اشرح ما يعنيه هذا النوع من الانفراج (السلبي قد يسبق هبوطًا، والإيجابي قد يسبق صعودًا).
    6.  اختتم بـ "الانفراج ليس إشارة مؤكدة 100%، ولكنه دعوة قوية لمراقبة حركة السعر عن كثب."
    **البيانات:**\n{data}
    """,
    "STRATEGY_STATS": """
    **الدور:** أنت محلل بيانات متخصص في أداء استراتيجيات التداول.
    **المهمة:**
    1.  ابدأ بعنوان "📊 تحليل أداء الاستراتيجيات (آخر 30 يومًا) 📊".
    2.  اشرح للمستخدم أن هذا التقرير يعرض نتائج التنبيهات السابقة بشكل شفاف لمساعدته على معرفة أي الاستراتيجيات كانت الأكثر فعالية.
    3.  لكل استراتيجية في "البيانات"، قم بعرض النتائج بطريقة واضحة: إجمالي التنبيهات، نسبة النجاح، ومتوسط أقصى ربح.
    4.  يمكنك إضافة تعليق موجز على أداء كل استراتيجية (مثال: "استراتيجية القناص تظهر دقة عالية، بينما استراتيجية الزخم توفر فرصًا أكثر ولكن بنسبة نجاح أقل").
    5.  اختتم بـ "هذه البيانات تساعدك على بناء ثقة في تنبيهات البوت وفهم نقاط قوة كل استراتيجية."
    **البيانات:**\n{data}
    """
}

async def get_ai_analysis(report_data: dict, session: aiohttp.ClientSession):
    if not GEMINI_API_KEY or 'PASTE_YOUR' in GEMINI_API_KEY:
        logger.warning("Gemini API Key is not set. Skipping AI analysis.")
        return "⚠️ لم يتم تكوين مفتاح الذكاء الاصطناعي. سيتم عرض البيانات الأولية."

    report_type = report_data.get("report_type")
    prompt_template = PROMPT_TEMPLATES.get(report_type)
    if not prompt_template:
        logger.error(f"No prompt template found for report type: {report_type}")
        return "حدث خطأ: لم يتم العثور على قالب تحليل لهذا النوع من التقارير."

    data_str = json.dumps(report_data.get("data"), indent=2, ensure_ascii=False)
    final_prompt = prompt_template.format(data=data_str)

    payload = {
      "contents": [{"parts":[{"text": final_prompt}]}],
      "generationConfig": {"temperature": 0.4, "topK": 1, "topP": 1, "maxOutputTokens": 2048}
    }

    try:
        async with session.post(GEMINI_API_URL, json=payload, timeout=HTTP_TIMEOUT) as response:
            if response.status == 429: return "عذرًا، نواجه ضغطًا عاليًا على خدمة التحليل حاليًا. يرجى المحاولة مرة أخرى بعد دقيقة."
            response.raise_for_status()
            result = await response.json()
            if 'candidates' in result and result['candidates']:
                content = result['candidates'][0].get('content', {})
                if 'parts' in content and content['parts']:
                    return content['parts'][0].get('text', "لم يتمكن الذكاء الاصطناعي من توليد رد.")
            return "استجابة غير متوقعة من خدمة التحليل."
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}")
        return f"حدث خطأ أثناء التواصل مع خدمة التحليل الذكي: {e}"

# =============================================================================
# --- قسم إدارة قاعدة البيانات ---
# =============================================================================
def setup_database():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS strategy_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, exchange TEXT NOT NULL,
            strategy_type TEXT NOT NULL, alert_price REAL NOT NULL, alert_timestamp INTEGER NOT NULL,
            status TEXT NOT NULL, peak_profit_percent REAL, final_profit_percent_24h REAL
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Database is set up and ready.")

def log_strategy_result(symbol, exchange, strategy_type, alert_price, status, peak_profit, final_profit):
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
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        thirty_days_ago = int((datetime.now(UTC) - timedelta(days=30)).timestamp())
        cursor.execute("SELECT strategy_type, status, peak_profit_percent FROM strategy_performance WHERE alert_timestamp >= ?", (thirty_days_ago,))
        records = cursor.fetchall()
        conn.close()
        return records
    except sqlite3.Error as e:
        logger.error(f"Database error while fetching strategy stats: {e}")
        return []

def load_user_ids():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM users")
        user_ids = {row[0] for row in cursor.fetchall()}
        conn.close()
        return user_ids
    except sqlite3.Error as e: logger.error(f"Database error while loading users: {e}"); return set()

def save_user_id(chat_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        conn.commit()
        conn.close()
        logger.info(f"User with chat_id: {chat_id} has been saved or already exists.")
    except sqlite3.Error as e: logger.error(f"Database error while saving user {chat_id}: {e}")

def remove_user_id(chat_id):
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        conn.commit()
        conn.close()
        logger.warning(f"User {chat_id} has been removed from the database.")
    except sqlite3.Error as e: logger.error(f"Database error while removing user {chat_id}: {e}")

async def broadcast_message(bot: Bot, message_text: str, parse_mode=ParseMode.MARKDOWN):
    user_ids = load_user_ids()
    if not user_ids: logger.warning("Broadcast requested, but no users are registered."); return
    for user_id in user_ids:
        try: await bot.send_message(chat_id=user_id, text=message_text, parse_mode=parse_mode)
        except Forbidden: remove_user_id(user_id)
        except BadRequest as e:
            if "chat not found" in str(e): remove_user_id(user_id)
            else: logger.error(f"Failed to send message to {user_id}: {e}")
        except Exception as e: logger.error(f"An unexpected error occurred while sending to {user_id}: {e}")

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
            if attempt >= retries - 1: logger.error(f"Error fetching {url} after {retries} retries: {e}"); return None
            await asyncio.sleep(1)
    return None

def format_price(price_str):
    try:
        price = float(price_str)
        if price < 1e-4: return f"{price:.10f}".rstrip('0')
        return f"{price:.8g}"
    except (ValueError, TypeError): return price_str

# =============================================================================
# --- قسم عملاء المنصات (Exchange Clients) ---
# =============================================================================
class BaseExchangeClient:
    def __init__(self, session, api_key=None, api_secret=None): self.session = session; self.name = "Base"
    async def get_market_data(self): raise NotImplementedError
    async def get_klines(self, symbol, interval, limit): raise NotImplementedError
    async def get_order_book(self, symbol, limit=20): raise NotImplementedError
    async def get_current_price(self, symbol): raise NotImplementedError
    async def get_processed_klines(self, symbol, interval, limit):
        klines = await self.get_klines(symbol, interval, limit)
        if not klines: return None
        klines.sort(key=lambda x: int(x[0])); return klines
class MexcClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs); self.name = "MEXC"; self.base_api_url = "https://api.mexc.com"; self.interval_map = {'1m': '1m', '5m': '5m', '15m': '15m', '1h': '1h', '4h': '4h', '1d': '1d'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        return [{'symbol': i['symbol'], 'quoteVolume': i.get('quoteVolume') or '0', 'lastPrice': i.get('lastPrice') or '0', 'priceChangePercent': float(i.get('priceChangePercent','0'))*100} for i in data if i.get('symbol','').endswith("USDT")]
    async def get_klines(self, symbol, interval, limit):
        api_interval = self.interval_map.get(interval, interval)
        async with api_semaphore:
            params = {'symbol': symbol, 'interval': api_interval, 'limit': limit}; await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/klines", params=params)
            return [[item[0], item[1], item[2], item[3], item[4], item[5]] for item in data] if data else None
    async def get_order_book(self, symbol, limit=20):
        async with api_semaphore:
            params = {'symbol': symbol, 'limit': limit}; await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/api/v3/depth", params)
    async def get_current_price(self, symbol: str):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/price", {'symbol': symbol})
        return float(data['price']) if data and 'price' in data else None
class GateioClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs); self.name = "Gate.io"; self.base_api_url = "https://api.gateio.ws/api/v4"; self.interval_map = {'1m': '1m', '5m': '5m', '15m': '15m', '1h': '1h', '4h': '4h', '1d': '1d'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/spot/tickers")
        if not data: return []
        return [{'symbol': i['currency_pair'].replace('_',''), 'quoteVolume': i.get('quote_volume') or '0', 'lastPrice': i.get('last') or '0', 'priceChangePercent': float(i.get('change_percentage','0'))} for i in data if i.get('currency_pair','').endswith("_USDT")]
    async def get_klines(self, symbol, interval, limit):
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"; api_interval = self.interval_map.get(interval, interval)
        async with api_semaphore:
            params = {'currency_pair': gateio_symbol, 'interval': api_interval, 'limit': limit}; await asyncio.sleep(0.2)
            data = await fetch_json(self.session, f"{self.base_api_url}/spot/candlesticks", params=params)
            if not data: return None
            return [[int(k[0])*1000, k[5], k[3], k[4], k[2], k[1]] for k in data]
    async def get_order_book(self, symbol, limit=20):
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"
        async with api_semaphore:
            params = {'currency_pair': gateio_symbol, 'limit': limit}; await asyncio.sleep(0.2)
            return await fetch_json(self.session, f"{self.base_api_url}/spot/order_book", params)
    async def get_current_price(self, symbol: str):
        gateio_symbol = f"{symbol[:-4]}_{symbol[-4:]}"
        data = await fetch_json(self.session, f"{self.base_api_url}/spot/tickers", {'currency_pair': gateio_symbol})
        return float(data[0]['last']) if data and isinstance(data, list) and len(data) > 0 and 'last' in data[0] else None
class BinanceClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs); self.name = "Binance"; self.base_api_url = "https://api.binance.com"; self.interval_map = {'1m': '1m', '5m': '5m', '15m': '15m', '1h': '1h', '4h': '4h', '1d': '1d'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        return [{'symbol': i['symbol'], 'quoteVolume': i.get('quoteVolume') or '0', 'lastPrice': i.get('lastPrice') or '0', 'priceChangePercent': float(i.get('priceChangePercent','0'))} for i in data if i.get('symbol','').endswith("USDT")]
    async def get_klines(self, symbol, interval, limit):
        api_interval = self.interval_map.get(interval, interval)
        async with api_semaphore:
            params = {'symbol': symbol, 'interval': api_interval, 'limit': limit}; await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/klines", params=params)
            return [[item[0], item[1], item[2], item[3], item[4], item[5]] for item in data] if data else None
    async def get_order_book(self, symbol, limit=20):
        async with api_semaphore:
            params = {'symbol': symbol, 'limit': limit}; await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/api/v3/depth", params)
    async def get_current_price(self, symbol: str):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/price", {'symbol': symbol})
        return float(data['price']) if data and 'price' in data else None
class BybitClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs); self.name = "Bybit"; self.base_api_url = "https://api.bybit.com"; self.interval_map = {'1m': '1', '5m': '5', '15m': '15', '1h': '60', '4h': '240', '1d': 'D'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/tickers", params={'category': 'spot'})
        if not data or not data.get('result') or not data['result'].get('list'): return []
        return [{'symbol': i['symbol'], 'quoteVolume': i.get('turnover24h') or '0', 'lastPrice': i.get('lastPrice') or '0', 'priceChangePercent': float(i.get('price24hPcnt','0'))*100} for i in data['result']['list'] if i['symbol'].endswith("USDT")]
    async def get_klines(self, symbol, interval, limit):
        async with api_semaphore:
            api_interval = self.interval_map.get(interval, '5'); params = {'category': 'spot', 'symbol': symbol, 'interval': api_interval, 'limit': limit}; await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/kline", params=params)
            if not data or not data.get('result') or not data['result'].get('list'): return None
            return [[int(k[0]), k[1], k[2], k[3], k[4], k[5]] for k in data['result']['list']]
    async def get_order_book(self, symbol, limit=20):
        async with api_semaphore:
            params = {'category': 'spot', 'symbol': symbol, 'limit': limit}; await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/orderbook", params)
            if not data or not data.get('result'): return None
            return {'bids': data['result'].get('bids', []), 'asks': data['result'].get('asks', [])}
    async def get_current_price(self, symbol: str):
        data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/tickers", params={'category': 'spot', 'symbol': symbol})
        if not data or not data.get('result') or not data['result'].get('list'): return None
        return float(data['result']['list'][0]['lastPrice'])
class KucoinClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs); self.name = "KuCoin"; self.base_api_url = "https://api.kucoin.com"; self.interval_map = {'1m':'1min', '5m':'5min', '15m':'15min', '1h': '1hour', '4h': '4hour', '1d': '1day'}
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/allTickers")
        if not data or not data.get('data') or not data['data'].get('ticker'): return []
        return [{'symbol': i['symbol'].replace('-',''), 'quoteVolume': i.get('volValue') or '0', 'lastPrice': i.get('last') or '0', 'priceChangePercent': float(i.get('changeRate','0'))*100} for i in data['data']['ticker'] if i.get('symbol','').endswith("-USDT")]
    async def get_klines(self, symbol, interval, limit):
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"; api_interval = self.interval_map.get(interval, '5min')
        async with api_semaphore:
            params = {'symbol': kucoin_symbol, 'type': api_interval}; await asyncio.sleep(0.1)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/candles", params=params)
            if not data or not data.get('data'): return None
            return [[int(k[0])*1000, k[2], k[3], k[4], k[1], k[5]] for k in data['data']]
    async def get_order_book(self, symbol, limit=20):
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        async with api_semaphore:
            params = {'symbol': kucoin_symbol}; await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/orderbook/level2_20", params)
    async def get_current_price(self, symbol: str):
        kucoin_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/orderbook/level1", {'symbol': kucoin_symbol})
        if not data or not data.get('data'): return None
        return float(data['data']['price'])
class OkxClient(BaseExchangeClient):
    def __init__(self, session, **kwargs):
        super().__init__(session, **kwargs); self.name = "OKX"; self.base_api_url = "https://www.okx.com"; self.interval_map = {'1m': '1m', '5m': '5m', '15m': '15m', '1h': '1H', '4h': '4H', '1d': '1D'}
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
        api_interval = self.interval_map.get(interval, '5m'); okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        async with api_semaphore:
            params = {'instId': okx_symbol, 'bar': api_interval, 'limit': limit}; await asyncio.sleep(0.25)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/candles", params=params)
            if not data or not data.get('data'): return None
            return [[int(k[0]), k[1], k[2], k[3], k[4], k[5]] for k in data['data']]
    async def get_order_book(self, symbol, limit=20):
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        async with api_semaphore:
            params = {'instId': okx_symbol, 'sz': str(limit)}; await asyncio.sleep(0.25)
            data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/books", params)
            if not data or not data.get('data'): return None
            book_data = data['data'][0]
            return {'bids': book_data.get('bids',[]), 'asks': book_data.get('asks',[])}
    async def get_current_price(self, symbol: str):
        okx_symbol = f"{symbol[:-4]}-{symbol[-4:]}"
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/tickers", params={'instId': okx_symbol})
        if not data or not data.get('data'): return None
        return float(data['data'][0]['last'])

def get_exchange_client(exchange_name, session):
    clients = {'mexc': MexcClient, 'gate.io': GateioClient, 'binance': BinanceClient, 'bybit': BybitClient, 'kucoin': KucoinClient, 'okx': OkxClient}
    client_class = clients.get(exchange_name.lower())
    return client_class(session) if client_class else None

# =============================================================================
# --- قسم التحليل الفني والكمي (TA & Quant Section) ---
# =============================================================================
def calculate_poc(klines, num_bins=50):
    if not klines or len(klines) < 10: return None
    try:
        high_prices = np.array([float(k[2]) for k in klines]); low_prices = np.array([float(k[3]) for k in klines]); volumes = np.array([float(k[5]) for k in klines])
        min_price, max_price = np.min(low_prices), np.max(high_prices)
        if max_price == min_price: return min_price
        price_bins = np.linspace(min_price, max_price, num_bins); volume_per_bin = np.zeros(num_bins)
        for i in range(len(klines)):
            avg_price = (high_prices[i] + low_prices[i]) / 2; bin_index = np.searchsorted(price_bins, avg_price) -1
            if 0 <= bin_index < num_bins: volume_per_bin[bin_index] += volumes[i]
        if np.sum(volume_per_bin) == 0: return None
        return price_bins[np.argmax(volume_per_bin)]
    except Exception as e: logger.error(f"Error calculating POC: {e}"); return None
def calculate_ema_series(prices, period):
    if len(prices) < period: return []
    ema, sma = [], sum(prices[:period]) / period; ema.append(sma); multiplier = 2 / (period + 1)
    for price in prices[period:]: ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema
def calculate_ema(prices, period):
    if len(prices) < period: return None
    return calculate_ema_series(prices, period)[-1]
def calculate_sma(prices, period):
    if len(prices) < period: return None
    return np.mean(prices[-period:])
def calculate_macd(prices, fast_period=12, slow_period=26, signal_period=9):
    if len(prices) < slow_period: return None, None
    ema_fast = calculate_ema_series(prices, fast_period); ema_slow = calculate_ema_series(prices, slow_period)
    if not ema_fast or not ema_slow: return None, None
    ema_fast = ema_fast[len(ema_fast) - len(ema_slow):]; macd_line_series = np.array(ema_fast) - np.array(ema_slow)
    signal_line_series = calculate_ema_series(macd_line_series.tolist(), signal_period)
    if not signal_line_series: return None, None
    return macd_line_series[-1], signal_line_series[-1]
def calculate_rsi_series(prices, period=14):
    if len(prices) < period + 1: return np.array([])
    deltas = np.diff(prices); seed = deltas[:period]; up = seed[seed >= 0].sum()/period
    down = -seed[seed < 0].sum()/period; rs = up / down if down != 0 else 0
    rsi = np.zeros_like(prices); rsi[:period] = 100. - 100./(1.+rs)
    for i in range(period, len(prices)):
        delta = deltas[i-1]
        if delta > 0: upval = delta; downval = 0.
        else: upval = 0.; downval = -delta
        up = (up*(period-1) + upval)/period; down = (down*(period-1) + downval)/period
        rs = up/down if down != 0 else 0; rsi[i] = 100. - 100./(1.+rs)
    return rsi
def calculate_rsi(prices, period=14):
    series = calculate_rsi_series(prices, period)
    return series[-1] if len(series) > 0 else None
def find_support_resistance(high_prices, low_prices, window=10):
    supports, resistances = [], []
    for i in range(window, len(high_prices) - window):
        if high_prices[i] == max(high_prices[i-window:i+window+1]): resistances.append(high_prices[i])
        if low_prices[i] == min(low_prices[i-window:i+window+1]): supports.append(low_prices[i])
    return sorted(list(set(supports)), reverse=True), sorted(list(set(resistances)), reverse=True)
def analyze_trend(current_price, ema21, ema50, sma100):
    if ema21 and ema50 and sma100:
        if current_price > ema21 > ema50 > sma100: return "🟢 اتجاه صاعد قوي.", 2
        if current_price > ema50 and current_price > ema21: return "🟢 اتجاه صاعد.", 1
        if current_price < ema21 < ema50 < sma100: return "🔴 اتجاه هابط قوي.", -2
        if current_price < ema50 and current_price < ema21: return "🔴 اتجاه هابط.", -1
    return "🟡 جانبي / غير واضح.", 0
def order_book_imbalance(bids, asks, top_n=10):
    try:
        b = sum(float(p) * float(q) for p, q in bids[:top_n]); a = sum(float(p) * float(q) for p, q in asks[:top_n])
        denom = (b + a); return (b - a) / denom if denom > 0 else 0.0
    except (ValueError, TypeError): return 0.0
def find_peaks(data, window): return [i for i in range(window, len(data) - window) if data[i] == max(data[i-window:i+window+1])]
def find_troughs(data, window): return [i for i in range(window, len(data) - window) if data[i] == min(data[i-window:i+window+1])]
def find_rsi_divergence(klines):
    if not klines or len(klines) < 50: return None
    prices = np.array([float(k[4]) for k in klines]); rsi_values = calculate_rsi_series(prices, period=14)
    if rsi_values is None or len(rsi_values) < 30: return None
    prices = prices[-len(rsi_values):]; price_peaks = find_peaks(prices, DIVERGENCE_PEAK_WINDOW); rsi_peaks = find_peaks(rsi_values, DIVERGENCE_PEAK_WINDOW)
    if len(price_peaks) >= 2 and len(rsi_peaks) >= 2:
        p1_idx, p2_idx, r1_idx, r2_idx = price_peaks[-2], price_peaks[-1], rsi_peaks[-2], rsi_peaks[-1]
        if abs(p2_idx - r2_idx) < DIVERGENCE_PEAK_WINDOW:
            if prices[p2_idx] > prices[p1_idx] and rsi_values[r2_idx] < rsi_values[r1_idx]:
                return {'type': 'Bearish', 'price_peak': prices[p2_idx]}
    price_troughs = find_troughs(prices, DIVERGENCE_PEAK_WINDOW); rsi_troughs = find_troughs(rsi_values, DIVERGENCE_PEAK_WINDOW)
    if len(price_troughs) >= 2 and len(rsi_troughs) >= 2:
        p1_idx, p2_idx, r1_idx, r2_idx = price_troughs[-2], price_troughs[-1], rsi_troughs[-2], rsi_troughs[-1]
        if abs(p2_idx - r2_idx) < DIVERGENCE_PEAK_WINDOW:
            if prices[p2_idx] < prices[p1_idx] and rsi_values[r2_idx] > rsi_values[r1_idx]:
                return {'type': 'Bullish', 'price_trough': prices[p2_idx]}
    return None

# =============================================================================
# --- 4. الوظائف التفاعلية (أوامر البوت) ---
# =============================================================================
BTN_TA_PRO = "🔬 محلل فني"; BTN_SCALP_SCAN = "⚡️ تحليل سريع"; BTN_PRO_SCAN = "🎯 فحص احترافي"; BTN_SNIPER_LIST = "🔭 قائمة القنص"; BTN_GEM_HUNTER = "💎 صائد الجواهر"; BTN_WHALE_RADAR = "🐋 رادار الحيتان"; BTN_MOMENTUM = "🚀 كاشف الزخم"; BTN_STATUS = "📊 الحالة"; BTN_PERFORMANCE = "📈 تقرير الأداء"; BTN_STRATEGY_STATS = "📊 أداء الاستراتيجيات"; BTN_TOP_GAINERS = "📈 الأعلى ربحاً"; BTN_TOP_LOSERS = "📉 الأعلى خسارة"; BTN_TOP_VOLUME = "💰 الأعلى تداولاً"; BTN_SELECT_MEXC = "MEXC"; BTN_SELECT_GATEIO = "Gate.io"; BTN_SELECT_BINANCE = "Binance"; BTN_SELECT_BYBIT = "Bybit"; BTN_SELECT_KUCOIN = "KuCoin"; BTN_SELECT_OKX = "OKX"; BTN_TASKS_ON = "🔴 إيقاف المهام"; BTN_TASKS_OFF = "🟢 تفعيل المهام"

def build_menu(context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data; bot_data = context.bot_data; selected_exchange = user_data.get('exchange', 'mexc'); tasks_enabled = bot_data.get('background_tasks_enabled', True)
    mexc_btn = f"✅ {BTN_SELECT_MEXC}" if selected_exchange == 'mexc' else BTN_SELECT_MEXC
    gate_btn = f"✅ {BTN_SELECT_GATEIO}" if selected_exchange == 'gate.io' else BTN_SELECT_GATEIO
    binance_btn = f"✅ {BTN_SELECT_BINANCE}" if selected_exchange == 'binance' else BTN_SELECT_BINANCE
    bybit_btn = f"✅ {BTN_SELECT_BYBIT}" if selected_exchange == 'bybit' else BTN_SELECT_BYBIT
    kucoin_btn = f"✅ {BTN_SELECT_KUCOIN}" if selected_exchange == 'kucoin' else BTN_SELECT_KUCOIN
    okx_btn = f"✅ {BTN_SELECT_OKX}" if selected_exchange == 'okx' else BTN_SELECT_OKX
    toggle_tasks_btn = BTN_TASKS_ON if tasks_enabled else BTN_TASKS_OFF
    keyboard = [[BTN_PRO_SCAN, BTN_MOMENTUM, BTN_WHALE_RADAR], [BTN_TA_PRO, BTN_SCALP_SCAN], [BTN_GEM_HUNTER, BTN_SNIPER_LIST], [BTN_TOP_GAINERS, BTN_TOP_VOLUME, BTN_TOP_LOSERS], [BTN_PERFORMANCE, BTN_STRATEGY_STATS, BTN_STATUS], [toggle_tasks_btn], [mexc_btn, gate_btn, binance_btn], [bybit_btn, kucoin_btn, okx_btn]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message: save_user_id(update.message.chat_id)
    context.user_data['exchange'] = 'mexc'; context.bot_data.setdefault('background_tasks_enabled', True)
    welcome_message = "أهلاً بك في بوت الصياد الذكي!\n\nأنا مساعدك الاستراتيجي في عالم العملات الرقمية. أستخدم الذكاء الاصطناعي لتحليل البيانات وتقديمها لك مع شرح مفصل لمساعدتك في اتخاذ قرارات أفضل.\n\nاستخدم الأزرار أدناه لاستكشاف قدراتي."
    if update.message: await update.message.reply_text(welcome_message, reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.strip()
    button_text = text.replace("✅ ", "")

    if context.user_data.get('awaiting_symbol_for_ta'):
        symbol = text.upper();
        if not symbol.endswith("USDT"): symbol += "USDT"
        context.user_data['awaiting_symbol_for_ta'] = False; context.args = [symbol]
        await run_full_technical_analysis(update, context); return
    if context.user_data.get('awaiting_symbol_for_scalp'):
        symbol = text.upper();
        if not symbol.endswith("USDT"): symbol += "USDT"
        context.user_data['awaiting_symbol_for_scalp'] = False; context.args = [symbol]
        await run_scalp_analysis(update, context); return

    if button_text == BTN_TA_PRO:
        context.user_data['awaiting_symbol_for_ta'] = True; await update.message.reply_text("🔬 يرجى إرسال رمز العملة للتحليل المعمق..."); return
    if button_text == BTN_SCALP_SCAN:
        context.user_data['awaiting_symbol_for_scalp'] = True; await update.message.reply_text("⚡️ يرجى إرسال رمز العملة للتحليل السريع..."); return
    
    # معالجة الأزرار التي لا تتطلب تحليل AI
    if button_text == BTN_SNIPER_LIST: await show_sniper_watchlist(update, context); return
    if button_text in [BTN_SELECT_MEXC, BTN_SELECT_GATEIO, BTN_SELECT_BINANCE, BTN_SELECT_BYBIT, BTN_SELECT_KUCOIN, BTN_SELECT_OKX]:
        context.user_data['exchange'] = button_text.lower(); await update.message.reply_text(f"✅ تم تحويل المنصة إلى: **{button_text}**", reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN); return
    if button_text in [BTN_TASKS_ON, BTN_TASKS_OFF]:
        context.bot_data['background_tasks_enabled'] = not context.bot_data.get('background_tasks_enabled', True); status = "تفعيل" if context.bot_data['background_tasks_enabled'] else "إيقاف"
        await update.message.reply_text(f"✅ تم **{status}** المهام الخلفية.", reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN); return
    if button_text == BTN_STATUS: await status_command(update, context); return
    if button_text == BTN_PERFORMANCE: await get_performance_report(update.message.chat_id, context); return
    
    if button_text in [BTN_TOP_GAINERS, BTN_TOP_LOSERS, BTN_TOP_VOLUME]:
        client = get_exchange_client(context.user_data.get('exchange', 'mexc'), context.application.bot_data['session'])
        sent_message = await update.message.reply_text(f"🔍 جارِ جلب البيانات من {client.name}...")
        await run_top_movers(sent_message.chat_id, sent_message.message_id, client, context, button_text)
        return
    
    session = context.application.bot_data['session']
    client = get_exchange_client(context.user_data.get('exchange', 'mexc'), session)
    if not client: await update.message.reply_text("حدث خطأ في اختيار المنصة."); return
    sent_message = await update.message.reply_text(f"🧠 جارِ جمع البيانات وتحليلها بواسطة الذكاء الاصطناعي على منصة {client.name}...")

    report_data = None
    if button_text == BTN_MOMENTUM: report_data = await get_momentum_data(client)
    elif button_text == BTN_WHALE_RADAR: report_data = await get_whale_data(client)
    elif button_text == BTN_GEM_HUNTER: report_data = await get_gem_data(client)
    elif button_text == BTN_STRATEGY_STATS: report_data = await get_strategy_stats_data()
    elif button_text == BTN_PRO_SCAN: report_data = await get_pro_scan_data(client)
    
    if report_data:
        if not report_data.get("data"):
            await sent_message.edit_text(f"✅ اكتمل البحث على {client.name}: لا توجد نتائج تتوافق مع الشروط حاليًا.")
        else:
            ai_analysis = await get_ai_analysis(report_data, session)
            await sent_message.edit_text(ai_analysis, parse_mode=ParseMode.MARKDOWN)
    else:
        await sent_message.edit_text("حدث خطأ غير متوقع أو أن الميزة غير مدعومة بعد.")

# =============================================================================
# --- دوال جمع البيانات (لتحليل AI) ---
# =============================================================================
async def get_pro_scan_data(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {"report_type": "PRO_SCAN", "data": []}
    candidates = [p['symbol'] for p in market_data if MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume', '0')) and float(p.get('lastPrice', '1')) <= MOMENTUM_MAX_PRICE]
    
    async def calculate_pro_score(symbol):
        score, analysis_details = 0, {}
        try:
            klines = await client.get_processed_klines(symbol, '15m', 100)
            if not klines or len(klines) < 50: return None
            close = np.array([float(k[4]) for k in klines]); vol = np.array([float(k[5]) for k in klines]); current = close[-1]
            ema20 = np.mean(close[-20:]); ema50 = np.mean(close[-50:])
            if current > ema20 > ema50: score += 2; analysis_details['Trend'] = "Strong Up"
            elif current > ema20: score += 1; analysis_details['Trend'] = "Up"
            if (np.sum(np.diff(close[-10:]) > 0) / 10) >= 0.6: score += 1
            if rsi := calculate_rsi(close):
                if 25 < rsi < 75: score +=1
            if vwap := calculate_vwap(close, vol, period=20):
                if current > vwap: score += 1
            analysis_details['Final Score'] = score; analysis_details['Symbol'] = symbol
            return analysis_details if score >= 4 else None
        except Exception: return None
    
    tasks = [calculate_pro_score(symbol) for symbol in candidates[:100]]
    results = [res for res in await asyncio.gather(*tasks) if res is not None]
    strong_opportunities = sorted(results, key=lambda x: x['Final Score'], reverse=True)
    return {"report_type": "PRO_SCAN", "data": strong_opportunities[:5]}

async def get_momentum_data(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {"report_type": "MOMENTUM_SCAN", "data": []}
    potential_coins = [p for p in market_data if float(p.get('lastPrice','1')) <= MOMENTUM_MAX_PRICE and MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= MOMENTUM_MAX_VOLUME_24H]
    async def score_candidate(symbol):
        try:
            klines_5m = await client.get_processed_klines(symbol, '5m', 30)
            if not klines_5m or len(klines_5m) < 20: return None
            score, details = 0, {'symbol': symbol}
            close_prices = np.array([float(k[4]) for k in klines_5m]); current_price = close_prices[-1]
            start_price = float(klines_5m[-12][4])
            if start_price > 0:
                price_change = ((current_price - start_price) / start_price) * 100
                if price_change > MOMENTUM_PRICE_INCREASE: score += 1
                details['price_change_percent_60m'] = round(price_change, 2)
            if len(klines_5m) >= 24:
                old_volume = sum(float(k[5]) for k in klines_5m[-24:-12]); new_volume = sum(float(k[5]) for k in klines_5m[-12:])
                if old_volume > 0 and new_volume > old_volume * MOMENTUM_VOLUME_INCREASE: score += 1
            if sum(1 for k in klines_5m[-5:] if float(k[4]) > float(k[1])) >= 3: score += 1
            if rsi := calculate_rsi(close_prices, period=14):
                if rsi > 60: score += 1
                if rsi > 85: score -= 1
            details['score'] = score; details['current_price'] = current_price
            if score >= MOMENTUM_MIN_SCORE: return details
            return None
        except Exception: return None
    tasks = [score_candidate(p['symbol']) for p in potential_coins[:100]]
    results = [res for res in await asyncio.gather(*tasks) if res is not None]
    momentum_coins = sorted(results, key=lambda x: x['score'], reverse=True)
    for coin in momentum_coins: add_to_monitoring(coin['symbol'], float(coin['current_price']), "Fomo Hunter", client.name)
    return {"report_type": "MOMENTUM_SCAN", "data": momentum_coins[:5]}

async def get_whale_data(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {"report_type": "WHALE_RADAR", "data": []}
    potential_gems = [p for p in market_data if float(p.get('lastPrice','999')) <= 0.50 and 100000 <= float(p.get('quoteVolume','0')) <= 5000000]
    top_gems = sorted(potential_gems, key=lambda x: x.get('priceChangePercent', 0), reverse=True)[:50]
    all_signals = []
    for gem in top_gems:
        try:
            book = await client.get_order_book(gem['symbol'], 10)
            if not book or not book.get('bids') or not book.get('asks'): continue
            bids = [(float(p), float(q)) for p, q in book['bids']]; asks = [(float(p), float(q)) for p, q in book['asks']]
            for price, qty in bids[:5]:
                if (value := price * qty) >= 25000: all_signals.append({'type': 'Buy Wall', 'symbol': gem['symbol'], 'value': round(value), 'price': price}); break
            for price, qty in asks[:5]:
                if (value := price * qty) >= 25000: all_signals.append({'type': 'Sell Wall', 'symbol': gem['symbol'], 'value': round(value), 'price': price}); break
            if (obi := order_book_imbalance(bids, asks, 10)) > SNIPER_OBI_THRESHOLD: all_signals.append({'type': 'Buy Pressure', 'symbol': gem['symbol'], 'value': round(obi, 3)})
            elif obi < -SNIPER_OBI_THRESHOLD: all_signals.append({'type': 'Sell Pressure', 'symbol': gem['symbol'], 'value': round(obi, 3)})
        except Exception: continue
    return {"report_type": "WHALE_RADAR", "data": all_signals[:10]}

async def get_gem_data(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {"report_type": "GEM_HUNTER", "data": []}
    final_gems = []
    candidates = [c for c in market_data if float(c.get('quoteVolume', '0')) > GEM_MIN_24H_VOLUME_USDT]
    for coin in candidates[:200]:
        try:
            klines = await client.get_processed_klines(coin['symbol'], '1d', 1000)
            if not klines or len(klines) < 10 or datetime.fromtimestamp(int(klines[0][0])/1000, tz=UTC) < GEM_LISTING_SINCE_DATE: continue
            highs = [float(k[2]) for k in klines]; lows = [float(k[3]) for k in klines]; closes = [float(k[4]) for k in klines]
            ath, atl, current = np.max(highs), np.min(lows), closes[-1]
            if ath == 0 or current == 0: continue
            correction = ((current - ath) / ath) * 100; rise_from_atl = ((current - atl) / atl) * 100 if atl > 0 else float('inf')
            if correction <= GEM_MIN_CORRECTION_PERCENT and rise_from_atl >= GEM_MIN_RISE_FROM_ATL_PERCENT and current > np.mean(closes[-20:]):
                final_gems.append({'symbol': coin['symbol'], 'potential_x': round(ath / current, 1), 'correction_percent': round(correction, 1)})
        except Exception: continue
    sorted_gems = sorted(final_gems, key=lambda x: x['potential_x'], reverse=True)
    return {"report_type": "GEM_HUNTER", "data": sorted_gems[:5]}

async def get_strategy_stats_data():
    records = get_strategy_stats()
    if not records: return {"report_type": "STRATEGY_STATS", "data": {}}
    stats = {}
    for strategy, status, peak_profit in records:
        if strategy not in stats: stats[strategy] = {'total': 0, 'success': 0, 'profits': []}
        stats[strategy]['total'] += 1; stats[strategy]['profits'].append(peak_profit or 0)
        if status == 'Success': stats[strategy]['success'] += 1
    processed_stats = {}
    for strategy, data in stats.items():
        if strategy == 'Sniper': success_rate = (data['success'] / data['total']) * 100 if data['total'] > 0 else 0
        else: success_rate = (sum(1 for p in data['profits'] if p > 2.0) / len(data['profits'])) * 100 if data['profits'] else 0
        avg_profit = np.mean([p for p in data['profits'] if p > 0]) if any(p > 0 for p in data['profits']) else 0
        processed_stats[strategy] = {'total_alerts': data['total'], 'success_rate_percent': round(success_rate, 1), 'average_peak_profit_percent': round(avg_profit, 2)}
    return {"report_type": "STRATEGY_STATS", "data": processed_stats}

# =============================================================================
# --- دوال عرض البيانات المباشرة (التي لا تستخدم AI) ---
# =============================================================================
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    message = f"📊 **حالة البوت** 📊\n\n- **المستخدمون:** `{len(load_user_ids())}`\n- **المهام الخلفية:** {'🟢 نشطة' if tasks_enabled else '🔴 متوقفة'}\n\n"
    for platform in PLATFORMS:
        message += (f"**منصة {platform}:**\n"
                    f"  - 🔭 أهداف القناص: {len(sniper_watchlist.get(platform, {}))}\n"
                    f"  - 🎯 إعادة الاختبار: {len(sniper_retest_watchlist.get(platform, {}))}\n"
                    f"  - 📈 المتتبعة: {len(performance_tracker.get(platform, {}))}\n\n")
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def show_sniper_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, any_watched = "🔭 **قائمة مراقبة القناص** 🔭\n\n", False
    for platform, watchlist in sniper_watchlist.items():
        if watchlist:
            any_watched = True; message += f"--- **{platform}** ---\n"
            for symbol, data in list(watchlist.items())[:5]:
                message += f"- `${symbol.replace('USDT','')}` (نطاق: `{format_price(data['low'])}` - `{format_price(data['high'])}`)\n"
            if len(watchlist) > 5: message += f"    *... و {len(watchlist) - 5} عملات أخرى.*\n"
            message += "\n"
    if not any_watched: message += "لا توجد أهداف حالية في قائمة المراقبة. يقوم الرادار بالبحث..."
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def get_performance_report(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    sent_message = await context.bot.send_message(chat_id=chat_id, text="📈 جارِ إعداد تقرير الأداء...")
    message = "📊 **تقرير الأداء الشامل (آخر 24 ساعة)** 📊\n\n"; found_any = False
    all_tracked = []
    for platform, symbols in performance_tracker.items():
        for symbol, data in symbols.items():
            if data.get('status') == 'Tracking': all_tracked.append((symbol, dict(data, **{'exchange': platform})))
    if all_tracked:
        found_any = True; message += "--- **متابعة الزخم** ---\n"
        for symbol, data in sorted(all_tracked, key=lambda item: item[1]['alert_time'], reverse=True):
            alert_price = data.get('alert_price',0); current_price = data.get('current_price', alert_price); high_price = data.get('high_price', alert_price)
            current_change = ((current_price - alert_price) / alert_price) * 100 if alert_price > 0 else 0
            peak_change = ((high_price - alert_price) / alert_price) * 100 if alert_price > 0 else 0
            time_since = (datetime.now(UTC) - data['alert_time']).total_seconds(); time_str = f"{int(time_since/3600)} س و {int((time_since % 3600)/60)} د"
            message += (f"{'🟢' if current_change >= 0 else '🔴'} **${symbol.replace('USDT','')}** ({data['exchange']}) (منذ {time_str})\n"
                        f"    - الحالي: `{format_price(current_price)}` (**{current_change:+.2f}%**)\n"
                        f"    - الأعلى: `{format_price(high_price)}` (**{peak_change:+.2f}%**)\n\n")
    all_sniper = []
    for platform, symbols in sniper_tracker.items():
        for symbol, data in symbols.items():
            if data.get('status') == 'Tracking': all_sniper.append((symbol, dict(data, **{'exchange': platform})))
    if all_sniper:
        found_any = True; message += "--- **متابعة القناص** ---\n"
        for symbol, data in sorted(all_sniper, key=lambda item: item[1]['alert_time'], reverse=True):
            time_since = (datetime.now(UTC) - data['alert_time']).total_seconds(); time_str = f"{int(time_since/3600)} س و {int((time_since % 3600)/60)} د"
            message += (f"🎯 **${symbol.replace('USDT','')}** ({data['exchange']}) (منذ {time_str})\n"
                        f"    - الهدف: `{format_price(data['target_price'])}` | الفشل: `{format_price(data['invalidation_price'])}`\n\n")
    if not found_any: message = "ℹ️ لا توجد عملات قيد تتبع الأداء حالياً."
    await sent_message.edit_text(message, parse_mode=ParseMode.MARKDOWN)

async def run_top_movers(chat_id: int, message_id: int, client: BaseExchangeClient, context: ContextTypes.DEFAULT_TYPE, button_text: str):
    try:
        title, sort_key, reverse_sort = "", "", False
        if button_text == BTN_TOP_GAINERS: title, sort_key, reverse_sort = "📈 الأعلى ربحاً", "priceChangePercent", True
        elif button_text == BTN_TOP_LOSERS: title, sort_key, reverse_sort = "📉 الأعلى خسارة", "priceChangePercent", False
        elif button_text == BTN_TOP_VOLUME: title, sort_key, reverse_sort = "💰 الأعلى تداولاً", "quoteVolume", True
        market_data = await client.get_market_data()
        if not market_data: await context.bot.edit_message_text(chat_id, message_id, "حدث خطأ في جلب بيانات السوق."); return
        for item in market_data: item['quoteVolume'] = float(item.get('quoteVolume','0'))
        valid_data = [item for item in market_data if item['quoteVolume'] > MARKET_MOVERS_MIN_VOLUME]
        sorted_data = sorted(valid_data, key=lambda x: x.get(sort_key, 0), reverse=reverse_sort)[:10]
        message = f"{title} على {client.name}\n\n"
        for i, coin in enumerate(sorted_data):
            symbol = coin['symbol'].replace('USDT','')
            if button_text == BTN_TOP_VOLUME:
                vol = coin['quoteVolume']; vol_str = f"{vol/1_000_000:.2f}M" if vol > 1_000_000 else f"{vol/1_000:.1f}K"
                message += f"**{i+1}. ${symbol}:** (الحجم: `${vol_str}`)\n"
            else: message += f"**{i+1}. ${symbol}:** `%{coin.get('priceChangePercent',0):+.2f}`\n"
        await context.bot.edit_message_text(chat_id, message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Error in top movers: {e}")

async def run_full_technical_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id; symbol = context.args[0]
    client = get_exchange_client(context.user_data.get('exchange', 'mexc'), context.application.bot_data['session'])
    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"🔬 جارِ إجراء تحليل فني شامل لـ ${symbol}...")
    try:
        timeframes = {'يومي': '1d', '4 ساعات': '4h', 'ساعة': '1h'}
        report_parts = []; overall_score = 0
        header = f"📊 **التحليل الفني المفصل لـ ${symbol}** ({client.name})\n\n"
        for tf_name, tf_interval in timeframes.items():
            klines = await client.get_processed_klines(symbol, tf_interval, 200)
            tf_report = f"--- **إطار {tf_name}** ---\n"
            if not klines or len(klines) < 100:
                tf_report += "لا توجد بيانات كافية.\n\n"; report_parts.append(tf_report); continue
            close = np.array([float(k[4]) for k in klines]); high = np.array([float(k[2]) for k in klines]); low = np.array([float(k[3]) for k in klines]); current = close[-1]
            report_lines = []
            trend_text, trend_score = analyze_trend(current, calculate_ema(close, 21), calculate_ema(close, 50), calculate_sma(close, 100))
            report_lines.append(f"**الاتجاه:** {trend_text}"); overall_score += trend_score
            if rsi := calculate_rsi(close):
                if rsi > 70: report_lines.append(f"🔴 **RSI ({rsi:.1f}):** تشبع شرائي.")
                elif rsi < 30: report_lines.append(f"🟢 **RSI ({rsi:.1f}):** تشبع بيعي.")
                else: report_lines.append(f"🟡 **RSI ({rsi:.1f}):** محايد.")
            supports, resistances = find_support_resistance(high, low)
            if next_res := min([r for r in resistances if r > current], default=None): report_lines.append(f"🛡️ **مقاومة:** {format_price(next_res)}")
            if next_sup := max([s for s in supports if s < current], default=None): report_lines.append(f"💰 **دعم:** {format_price(next_sup)}")
            tf_report += "\n".join(report_lines) + f"\n*السعر الحالي: {format_price(current)}*\n\n"
            report_parts.append(tf_report)
        summary = "--- **ملخص التحليل** ---\n"
        if overall_score >= 3: summary += "🟢 **النظرة العامة تميل للإيجابية.**"
        elif overall_score <= -3: summary += "🔴 **النظرة العامة تميل للسلبية.**"
        else: summary += "🟡 **الإشارات متضاربة حاليًا.**"
        report_parts.append(summary)
        await sent_message.edit_text(header + "".join(report_parts), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await sent_message.edit_text(f"حدث خطأ أثناء تحليل {symbol}."); logger.error(f"Error in TA for {symbol}: {e}")

async def run_scalp_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id; symbol = context.args[0]
    client = get_exchange_client(context.user_data.get('exchange', 'mexc'), context.application.bot_data['session'])
    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"⚡️ جارِ إجراء تحليل سريع لـ ${symbol}...")
    try:
        timeframes = {'15 دقيقة': '15m', '5 دقائق': '5m'}
        report_parts = []
        header = f"⚡️ **التحليل السريع لـ ${symbol}** ({client.name})\n\n"
        for tf_name, tf_interval in timeframes.items():
            klines = await client.get_processed_klines(symbol, tf_interval, 50)
            tf_report = f"--- **إطار {tf_name}** ---\n"
            if not klines or len(klines) < 20:
                tf_report += "لا توجد بيانات كافية.\n\n"; report_parts.append(tf_report); continue
            volumes = np.array([float(k[5]) for k in klines]); close = np.array([float(k[4]) for k in klines])
            avg_vol = np.mean(volumes[-20:-1]); last_vol = volumes[-1]
            report_lines = []
            if avg_vol > 0:
                if last_vol > avg_vol * 2: report_lines.append(f"🟢 **الفوليوم:** عالٍ ({last_vol/avg_vol:.1f}x).")
                else: report_lines.append("🟡 **الفوليوم:** عادي.")
            price_change = ((close[-1] - close[-5]) / close[-5]) * 100 if close[-5] > 0 else 0
            if price_change > 1.0: report_lines.append(f"🟢 **السعر:** حركة صاعدة (`%{price_change:+.1f}`).")
            elif price_change < -1.0: report_lines.append(f"🔴 **السعر:** حركة هابطة (`%{price_change:+.1f}`).")
            else: report_lines.append("🟡 **السعر:** حركة عادية.")
            tf_report += "\n".join(report_lines) + f"\n*السعر: {format_price(close[-1])}*\n\n"
            report_parts.append(tf_report)
        await sent_message.edit_text(header + "".join(report_parts), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await sent_message.edit_text(f"حدث خطأ أثناء تحليل {symbol}."); logger.error(f"Error in Scalp for {symbol}: {e}")

# =============================================================================
# --- 5. المهام الآلية الدورية ---
# =============================================================================
def add_to_monitoring(symbol, alert_price, source, exchange_name):
    if symbol not in performance_tracker[exchange_name]:
        performance_tracker[exchange_name][symbol] = {
            'alert_price': alert_price, 'alert_time': datetime.now(UTC), 'source': source, 
            'current_price': alert_price, 'high_price': alert_price, 'status': 'Tracking', 
            'momentum_lost_alerted': False
        }
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol} on {exchange_name}")

async def performance_tracker_loop(session: aiohttp.ClientSession, bot: Bot):
    logger.info("Performance Tracker background task started.")
    while True:
        await asyncio.sleep(RUN_PERFORMANCE_TRACKER_EVERY_MINUTES * 60)
        now = datetime.now(UTC); client_cache = {}
        for platform in PLATFORMS:
            for symbol, data in list(performance_tracker[platform].items()):
                if data.get('status') == 'Archived': continue
                try:
                    if platform not in client_cache: client_cache[platform] = get_exchange_client(platform, session)
                    client = client_cache[platform];
                    if not client: continue
                    if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                        alert_price = data['alert_price']; peak_profit = ((data['high_price'] - alert_price) / alert_price) * 100 if alert_price > 0 else 0
                        final_profit = ((data['current_price'] - alert_price) / alert_price) * 100 if alert_price > 0 else 0
                        log_strategy_result(symbol, platform, data['source'], alert_price, 'Expired', peak_profit, final_profit)
                        del performance_tracker[platform][symbol]; continue
                    current_price = await client.get_current_price(symbol)
                    if not current_price: continue
                    data['current_price'] = current_price
                    if current_price > data.get('high_price', 0): data['high_price'] = current_price
                    if not data.get('momentum_lost_alerted', False) and data['high_price'] > 0:
                        price_drop = ((current_price - data['high_price']) / data['high_price']) * 100
                        if price_drop <= MOMENTUM_LOSS_THRESHOLD_PERCENT:
                            await broadcast_message(bot, f"⚠️ **تنبيه: فقدان الزخم لـ ${symbol.replace('USDT','')}** ({platform})\n    - هبط بنسبة `{price_drop:.2f}%` من أعلى قمة.")
                            data['momentum_lost_alerted'] = True
                except Exception as e: logger.error(f"Error updating price for {symbol}: {e}")
        for platform in PLATFORMS:
            for symbol, data in list(sniper_tracker[platform].items()):
                if data['status'] != 'Tracking': continue
                try:
                    if platform not in client_cache: client_cache[platform] = get_exchange_client(platform, session)
                    client = client_cache[platform]
                    if not client: continue
                    if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                        log_strategy_result(symbol, platform, 'Sniper', data['alert_price'], 'Expired', data.get('peak_profit',0), data.get('final_profit',0)); del sniper_tracker[platform][symbol]; continue
                    current_price = await client.get_current_price(symbol)
                    if not current_price: continue
                    alert_price = data['alert_price']
                    data['peak_profit'] = max(data.get('peak_profit',0), ((current_price - alert_price)/alert_price)*100); data['final_profit'] = ((current_price - alert_price)/alert_price)*100
                    if current_price >= data['target_price']:
                        await broadcast_message(bot, f"✅ **القناص: نجاح!** ✅\n\n**العملة:** `${symbol}` ({platform})\n**النتيجة:** وصلت للهدف بنجاح.")
                        log_strategy_result(symbol, platform, 'Sniper', alert_price, 'Success', data['peak_profit'], data['final_profit']); del sniper_tracker[platform][symbol]
                    elif current_price <= data['invalidation_price']:
                        await broadcast_message(bot, f"❌ **القناص: فشل.** ❌\n\n**العملة:** `${symbol}` ({platform})\n**النتيجة:** فشل الاختراق وعاد السعر تحت نقطة الإبطال.")
                        log_strategy_result(symbol, platform, 'Sniper', alert_price, 'Failure', data['peak_profit'], data['final_profit']); del sniper_tracker[platform][symbol]
                except Exception as e: logger.error(f"Error in Sniper Tracker for {symbol}: {e}")

async def fomo_hunter_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict, session: aiohttp.ClientSession):
    logger.info(f"Fomo Hunter (v33) background task started for {client.name}.")
    while True:
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)
        if not bot_data.get('background_tasks_enabled', True): continue
        logger.info(f"===== Fomo Hunter ({client.name}): Starting Scan =====")
        try:
            report_data = await get_momentum_data(client)
            if report_data and report_data.get("data"):
                new_alerts = []
                for coin in report_data["data"]:
                    if not (last_alert := recently_alerted_fomo[client.name].get(coin['symbol'])) or (datetime.now(UTC) - last_alert) > timedelta(hours=2):
                        new_alerts.append(coin)
                        recently_alerted_fomo[client.name][coin['symbol']] = datetime.now(UTC)
                if new_alerts:
                    ai_report_data = {"report_type": "MOMENTUM_SCAN", "data": new_alerts}
                    ai_analysis = await get_ai_analysis(ai_report_data, session)
                    await broadcast_message(bot, ai_analysis)
        except Exception as e: logger.error(f"Error in fomo_hunter_loop for {client.name}: {e}", exc_info=True)

async def new_listings_sniper_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict):
    logger.info(f"New Listings Sniper background task started for {client.name}.")
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
            if newly_listed := current_symbols - known_symbols[client.name]:
                for symbol in newly_listed:
                    logger.info(f"Sniper ({client.name}): NEW LISTING DETECTED: {symbol}")
                    await broadcast_message(bot, f"🎯 **إدراج جديد على {client.name}:** `${symbol}`")
                known_symbols[client.name].update(newly_listed)
        except Exception as e: logger.error(f"Error in new_listings_sniper_loop for {client.name}: {e}")

async def coiled_spring_radar_loop(client: BaseExchangeClient, bot_data: dict):
    logger.info(f"Sniper Radar (v33) background task started for {client.name}.")
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
                highs, lows = [float(k[2]) for k in klines], [float(k[3]) for k in klines]
                highest, lowest = np.max(highs), np.min(lows)
                if lowest > 0 and ((highest - lowest) / lowest) * 100 <= SNIPER_MAX_VOLATILITY_PERCENT:
                    if symbol not in sniper_watchlist[client.name]:
                        sniper_watchlist[client.name][symbol] = {'high': highest, 'low': lowest}
                        logger.info(f"SNIPER RADAR ({client.name}): Added {symbol} to watchlist.")
            await asyncio.gather(*[check_candidate(p['symbol']) for p in candidates])
        except Exception as e: logger.error(f"Error in coiled_spring_radar_loop for {client.name}: {e}", exc_info=True)

async def breakout_trigger_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict, session: aiohttp.ClientSession):
    logger.info(f"Sniper Trigger (v33) background task started for {client.name}.")
    while True:
        await asyncio.sleep(SNIPER_TRIGGER_RUN_EVERY_SECONDS)
        if not bot_data.get('background_tasks_enabled', True) or not sniper_watchlist[client.name]: continue
        for symbol, data in list(sniper_watchlist[client.name].items()):
            try:
                if (data['high'] - data['low']) / data['high'] * 100 < SNIPER_MIN_TARGET_PERCENT: del sniper_watchlist[client.name][symbol]; continue
                trend_klines = await client.get_processed_klines(symbol, SNIPER_TREND_TIMEFRAME, SNIPER_TREND_PERIOD + 5)
                if not trend_klines or len(trend_klines) < SNIPER_TREND_PERIOD: continue
                trend_sma = calculate_sma([float(k[4]) for k in trend_klines], SNIPER_TREND_PERIOD)
                if trend_sma is None or float(trend_klines[-1][4]) < trend_sma: continue
                klines = await client.get_processed_klines(symbol, '5m', 25)
                if not klines or len(klines) < 20: continue
                confirmation_candle, trigger_candle = klines[-2], klines[-3]; breakout_level = data['high']
                if float(trigger_candle[2]) < breakout_level or float(confirmation_candle[4]) < breakout_level: continue
                if float(trigger_candle[5]) < np.mean([float(k[5]) for k in klines[-22:-2]]) * SNIPER_BREAKOUT_VOLUME_MULTIPLIER: continue
                order_book = await client.get_order_book(symbol, 10)
                if not order_book or order_book_imbalance(order_book.get('bids',[]), order_book.get('asks',[])) < SNIPER_OBI_THRESHOLD: continue
                
                alert_price = float(confirmation_candle[4]); range_height = data['high'] - data['low']; target_price = data['high'] + range_height
                atr_klines = await client.get_processed_klines(symbol, '15m', 15)
                if not atr_klines: continue
                atr_val = calculate_atr([float(k[2]) for k in atr_klines], [float(k[3]) for k in atr_klines], [float(k[4]) for k in atr_klines])
                stop_loss = alert_price - (atr_val * SNIPER_ATR_STOP_MULTIPLIER) if atr_val else data['low']
                
                report_data = {"report_type": "SNIPER_TRIGGER", "data": {'symbol': symbol, 'confirmation_price': alert_price, 'target': target_price, 'stop_loss': stop_loss}}
                ai_analysis = await get_ai_analysis(report_data, session)
                await broadcast_message(bot, ai_analysis)
                
                sniper_tracker[client.name][symbol] = {'alert_time': datetime.now(UTC), 'alert_price': alert_price, 'target_price': target_price, 'invalidation_price': stop_loss, 'status': 'Tracking'}
                sniper_retest_watchlist[client.name][symbol] = {'breakout_level': breakout_level, 'timestamp': datetime.now(UTC)}
                del sniper_watchlist[client.name][symbol]
            except Exception as e:
                logger.error(f"Error in breakout_trigger_loop for {symbol}: {e}", exc_info=True)
                if symbol in sniper_watchlist[client.name]: del sniper_watchlist[client.name][symbol]

async def retest_hunter_loop(client: BaseExchangeClient, bot: Bot, bot_data: dict, session: aiohttp.ClientSession):
    logger.info(f"Sniper Retest Hunter (v33) background task started for {client.name}.")
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
                if breakout_level * (1 - SNIPER_RETEST_PROXIMITY_PERCENT/100) <= low_price <= breakout_level * (1 + SNIPER_RETEST_PROXIMITY_PERCENT/100):
                    is_bullish_candle = close_price > open_price; avg_volume = np.mean([float(k[5]) for k in prev_candles])
                    is_high_volume = float(last_candle[5]) > avg_volume * 1.5
                    if is_bullish_candle and is_high_volume:
                        report_data = {"report_type": "SNIPER_RETEST", "data": {'symbol': symbol, 'retest_level': breakout_level, 'current_price': close_price}}
                        ai_analysis = await get_ai_analysis(report_data, session)
                        await broadcast_message(bot, ai_analysis)
                        logger.info(f"SNIPER RETEST ({client.name}): Confirmed re-test for {symbol}!")
                        del sniper_retest_watchlist[client.name][symbol]
            except Exception as e:
                logger.error(f"Error in retest_hunter_loop for {symbol}: {e}")
                if symbol in sniper_retest_watchlist[client.name]: del sniper_retest_watchlist[client.name][symbol]

async def divergence_detector_loop(bot: Bot, bot_data: dict, session: aiohttp.ClientSession):
    logger.info("Divergence Detector (v33) background task started.")
    client = get_exchange_client('binance', session)
    if not client: logger.error("Divergence detector failed: Binance client not available."); return
    while True:
        await asyncio.sleep(RUN_DIVERGENCE_SCAN_EVERY_HOURS * 3600)
        if not bot_data.get('background_tasks_enabled', True): continue
        logger.info("===== Divergence Detector: Starting Market Scan =====")
        try:
            market_data = await client.get_market_data()
            if not market_data: continue
            top_coins = sorted([c for c in market_data if c['symbol'].endswith('USDT')], key=lambda x: float(x.get('quoteVolume', '0')), reverse=True)[:50]
            for coin in top_coins:
                symbol = coin['symbol']
                try:
                    klines = await client.get_processed_klines(symbol, DIVERGENCE_TIMEFRAME, DIVERGENCE_KLINE_LIMIT)
                    if divergence := find_rsi_divergence(klines):
                        alert_key = f"{symbol}_{divergence['type']}"
                        if not (last_alert := recently_alerted_divergence.get(alert_key)) or (datetime.now(UTC) - last_alert) > timedelta(hours=12):
                            report_data = {"report_type": "DIVERGENCE_SCAN", "data": {"symbol": symbol, "timeframe": DIVERGENCE_TIMEFRAME, **divergence}}
                            ai_analysis = await get_ai_analysis(report_data, session)
                            await broadcast_message(bot, ai_analysis)
                            recently_alerted_divergence[alert_key] = datetime.now(UTC)
                            logger.info(f"DIVERGENCE ALERT: {divergence['type']} on {symbol}")
                except Exception: continue
        except Exception as e: logger.error(f"Major error in divergence_detector_loop: {e}", exc_info=True)

# =============================================================================
# --- 6. تشغيل البوت ---
# =============================================================================
async def post_init(application: Application):
    logger.info("Bot initialized with AI Layer. Starting background tasks...")
    session = aiohttp.ClientSession()
    application.bot_data["session"] = session
    bot, bot_data = application.bot, application.bot_data

    application.bot_data['task_performance'] = asyncio.create_task(performance_tracker_loop(session, bot))
    application.bot_data['task_divergence'] = asyncio.create_task(divergence_detector_loop(bot, bot_data, session))
    
    for platform_name in PLATFORMS:
        if client := get_exchange_client(platform_name.lower(), session):
            application.bot_data[f'task_fomo_{platform_name}'] = asyncio.create_task(fomo_hunter_loop(client, bot, bot_data, session))
            application.bot_data[f'task_listings_{platform_name}'] = asyncio.create_task(new_listings_sniper_loop(client, bot, bot_data))
            application.bot_data[f'task_sniper_radar_{platform_name}'] = asyncio.create_task(coiled_spring_radar_loop(client, bot_data))
            application.bot_data[f'task_sniper_trigger_{platform_name}'] = asyncio.create_task(breakout_trigger_loop(client, bot, bot_data, session))
            application.bot_data[f'task_sniper_retest_{platform_name}'] = asyncio.create_task(retest_hunter_loop(client, bot, bot_data, session))

    await send_startup_message(bot)

async def send_startup_message(bot: Bot):
    await broadcast_message(bot, "✅ **بوت الصياد الذكي (v33.1 - إصلاح حاسم) متصل الآن!**\n\nأرسل /start لعرض القائمة.")
    logger.info("Startup message sent successfully to all users.")
    
def main():
    if not TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM_BOT_TOKEN' in TELEGRAM_BOT_TOKEN: 
        logger.critical("FATAL ERROR: Bot token is not set.")
        return
    if not GEMINI_API_KEY or 'PASTE_YOUR_GEMINI_API_KEY_HERE' in GEMINI_API_KEY: 
        logger.critical("FATAL ERROR: Gemini API Key is not set.")
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

