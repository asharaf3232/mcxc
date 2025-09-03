# -*- coding: utf-8 -*-
import os
import asyncio
import json
import logging
import aiohttp
import time
from datetime import datetime, timedelta, UTC
from collections import deque
from telegram import Bot, ParseMode, ReplyKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# =============================================================================
# --- الإعدادات الرئيسية ---
# =============================================================================

# --- Telegram Configuration ---
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
MOMENTUM_LOSS_THRESHOLD_PERCENT = -5.0 # !جديد: نسبة انخفاض السعر من القمة لإرسال تنبيه

# --- إعدادات المهام الدورية ---
RUN_FOMO_SCAN_EVERY_MINUTES = 15
TOP_GAINERS_CANDIDATE_LIMIT = 200
RUN_LISTING_SCAN_EVERY_SECONDS = 60
RUN_PERFORMANCE_TRACKER_EVERY_MINUTES = 5
PERFORMANCE_TRACKING_DURATION_HOURS = 24
PRICE_VELOCITY_THRESHOLD = 30.0
VOLUME_SPIKE_MULTIPLIER = 10
MIN_USDT_VOLUME = 500000


# --- إعدادات عامة ---
HTTP_TIMEOUT = 15
API_CONCURRENCY_LIMIT = 8 # قيمة آمنة لـ 3 منصات

# --- إعدادات تسجيل الأخطاء ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة وتهيئة البوت ---
# =============================================================================
bot = Bot(token=TELEGRAM_BOT_TOKEN)
api_semaphore = asyncio.Semaphore(API_CONCURRENCY_LIMIT)

# --- إدارة الحالة متعددة المنصات ---
PLATFORMS = ["MEXC", "Gate.io", "Binance"]
performance_tracker = {p: {} for p in PLATFORMS}
active_hunts = {p: {} for p in PLATFORMS}
known_symbols = {p: set() for p in PLATFORMS}
background_tasks = {} # لتخزين المهام الخلفية والتحكم بها
recently_alerted_fomo = {p: {} for p in PLATFORMS}

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
            'priceChangePercent': float(item.get('priceChangePercent', 0))
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
            'priceChangePercent': float(item.get('change_percentage', '0')) / 100
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

def get_exchange_client(exchange_name, session):
    clients = {'mexc': MexcClient, 'gate.io': GateioClient, 'binance': BinanceClient}
    return clients[exchange_name.lower()](session)

# =============================================================================
# --- الوظائف المساعدة للتحليل ---
# =============================================================================
async def helper_get_momentum_symbols(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {}
    potential_coins = [p for p in market_data if float(p.get('lastPrice','1')) <= MOMENTUM_MAX_PRICE and MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= MOMENTUM_MAX_VOLUME_24H]
    if not potential_coins: return {}
    tasks = [client.get_klines(p['symbol'], MOMENTUM_KLINE_INTERVAL, MOMENTUM_KLINE_LIMIT) for p in potential_coins]
    all_klines_data = await asyncio.gather(*tasks)
    momentum_coins_data = {}
    for i, klines in enumerate(all_klines_data):
        if not klines or len(klines) < MOMENTUM_KLINE_LIMIT: continue
        try:
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
    for p in potential_gems: p['change_float'] = float(p.get('priceChangePercent', 0))
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
        bids = sorted([(float(p), float(q)) for p, q in book['bids']], key=lambda x: x[0], reverse=True)
        asks = sorted([(float(p), float(q)) for p, q in book['asks']], key=lambda x: x[0])
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
BTN_STATUS = "📊 الحالة"
BTN_PERFORMANCE = "📈 تقرير الأداء"
BTN_CROSS_ANALYSIS = "💪 تحليل متقاطع"
BTN_SELECT_MEXC = "MEXC"
BTN_SELECT_GATEIO = "Gate.io"
BTN_SELECT_BINANCE = "Binance"
BTN_TASKS_ON = "🔴 إيقاف المهام"
BTN_TASKS_OFF = "🟢 تفعيل المهام"

def build_menu(context: CallbackContext):
    user_data = context.user_data
    bot_data = context.bot_data
    selected_exchange = user_data.get('exchange', 'mexc')
    tasks_enabled = bot_data.get('background_tasks_enabled', True)
    
    mexc_btn = f"✅ {BTN_SELECT_MEXC}" if selected_exchange == 'mexc' else BTN_SELECT_MEXC
    gate_btn = f"✅ {BTN_SELECT_GATEIO}" if selected_exchange == 'gate.io' else BTN_SELECT_GATEIO
    binance_btn = f"✅ {BTN_SELECT_BINANCE}" if selected_exchange == 'binance' else BTN_SELECT_BINANCE
    toggle_tasks_btn = BTN_TASKS_ON if tasks_enabled else BTN_TASKS_OFF

    keyboard = [
        [BTN_MOMENTUM, BTN_WHALE_RADAR, BTN_CROSS_ANALYSIS],
        [BTN_PERFORMANCE, BTN_STATUS, toggle_tasks_btn],
        [mexc_btn, gate_btn, binance_btn]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def start_command(update: Update, context: CallbackContext):
    context.user_data['exchange'] = 'mexc'
    context.bot_data.setdefault('background_tasks_enabled', True)
    welcome_message = (
        "✅ **بوت التداول الذكي (v14.3 - Professional) جاهز!**\n\n"
        "**ما الجديد؟**\n"
        "- **تنبيه فقدان الزخم:** سيقوم البوت الآن بإرسال تنبيه عندما تفقد عملة مرصودة زخمها (تهبط من قمتها).\n"
        "- دعم ثلاثي للمنصات (Binance, MEXC, Gate.io).\n"
        "- تحكم كامل في المهام الخلفية.\n\n"
        "المنصة الحالية: **MEXC**")
    if update.message:
        update.message.reply_text(welcome_message, reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

def set_exchange(update: Update, context: CallbackContext, exchange_name: str):
    context.user_data['exchange'] = exchange_name.lower()
    update.message.reply_text(f"✅ تم تحويل المنصة بنجاح. المنصة النشطة الآن هي: **{exchange_name}**",
                              reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

def toggle_background_tasks(update: Update, context: CallbackContext):
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    context.bot_data['background_tasks_enabled'] = not tasks_enabled
    status = "تفعيل" if not tasks_enabled else "إيقاف"
    update.message.reply_text(f"✅ تم **{status}** المهام الخلفية.",
                              reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

def status_command(update: Update, context: CallbackContext):
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    message = "📊 **حالة البوت** 📊\n\n"
    message += f"**1. المهام الخلفية:** {'🟢 نشطة' if tasks_enabled else '🔴 متوقفة'}\n\n"
    for platform in PLATFORMS:
        hunts_count = len(active_hunts.get(platform, {}))
        perf_count = len(performance_tracker.get(platform, {}))
        message += f"**منصة {platform}:**\n"
        message += f"   - 🎯 الصفقات المراقبة: {hunts_count}\n"
        message += f"   - 📈 الأداء المتتبع: {perf_count}\n\n"
    update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update: Update, context: CallbackContext):
    if not update.message or not update.message.text: return
    button_text = update.message.text.strip().replace("✅ ", "")
    chat_id = update.message.chat_id
    loop = context.bot_data['loop']
    session = context.bot_data['session']

    if button_text in [BTN_SELECT_MEXC, BTN_SELECT_GATEIO, BTN_SELECT_BINANCE]:
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

    if task: asyncio.run_coroutine_threadsafe(task, loop)

async def run_momentum_detector(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🚀 **كاشف الزخم ({client.name})**\n\n🔍 جارِ الفحص المنظم للسوق..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    momentum_coins_data = await helper_get_momentum_symbols(client)
    if not momentum_coins_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص على {client.name} اكتمل:** لا يوجد زخم حالياً."); return
    sorted_coins = sorted(momentum_coins_data.values(), key=lambda x: x['price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم ({client.name}) - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\n"
    for i, coin in enumerate(sorted_coins[:10]):
        message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n   - السعر: `${format_price(coin['current_price'])}`\n   - **زخم آخر 30 دقيقة: `%{coin['price_change']:+.2f}`**\n\n")
    message += "*(تمت إضافة هذه العملات إلى متتبع الأداء.)*"
    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    now = datetime.now(UTC)
    for coin in sorted_coins[:10]:
        add_to_monitoring(coin['symbol'], float(coin['current_price']), coin.get('peak_volume', 0), now, f"الزخم ({client.name})", client.name)

async def run_whale_radar_scan(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"🐋 **رادار الحيتان ({client.name})**\n\n🔍 جارِ الفحص العميق..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    whale_signals_by_symbol = await helper_get_whale_activity(client)
    if not whale_signals_by_symbol:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **فحص الرادار على {client.name} اكتمل:** لا يوجد نشاط حيتان واضح."); return
    all_signals = [signal for signals_list in whale_signals_by_symbol.values() for signal in signals_list]
    sorted_signals = sorted(all_signals, key=lambda x: x.get('value', 0), reverse=True)
    message = f"🐋 **تقرير رادار الحيتان ({client.name}) - {datetime.now().strftime('%H:%M:%S')}** 🐋\n\n"
    for signal in sorted_signals:
        symbol_name = signal['symbol'].replace('USDT', '')
        if signal['type'] == 'Buy Wall': message += (f"🟢 **حائط شراء ضخم على ${symbol_name}**\n   - **الحجم:** `${signal['value']:,.0f}` USDT\n   - **عند سعر:** `{format_price(signal['price'])}`\n\n")
        elif signal['type'] == 'Sell Wall': message += (f"🔴 **حائط بيع ضخم على ${symbol_name}**\n   - **الحجم:** `${signal['value']:,.0f}` USDT\n   - **عند سعر:** `{format_price(signal['price'])}`\n\n")
        elif signal['type'] == 'Buy Pressure': message += (f"📈 **ضغط شراء عالٍ على ${symbol_name}**\n   - **النسبة:** الشراء يفوق البيع بـ `{signal['value']:.1f}x`\n\n")
        elif signal['type'] == 'Sell Pressure': message += (f"📉 **ضغط بيع عالٍ على ${symbol_name}**\n   - **النسبة:** البيع يفوق الشراء بـ `{signal['value']:.1f}x`\n\n")
    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def run_cross_analysis(context, chat_id, message_id, client: BaseExchangeClient):
    initial_text = f"💪 **تحليل متقاطع ({client.name})**\n\n🔍 جارِ إجراء الفحصين بالتوازي..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    try:
        momentum_task = asyncio.create_task(helper_get_momentum_symbols(client))
        whale_task = asyncio.create_task(helper_get_whale_activity(client))
        momentum_coins_data, whale_signals_by_symbol = await asyncio.gather(momentum_task, whale_task)
        strong_symbols = set(momentum_coins_data.keys()).intersection(set(whale_signals_by_symbol.keys()))
        if not strong_symbols:
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **التحليل المتقاطع على {client.name} اكتمل:**\n\nلم يتم العثور على عملات مشتركة حالياً."); return
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
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in cross_analysis on {client.name}: {e}", exc_info=True)
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ فادح أثناء التحليل المتقاطع.")

async def get_performance_report(context, chat_id, message_id):
    try:
        if not any(performance_tracker.values()):
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="ℹ️ لا توجد عملات قيد تتبع الأداء حالياً.")
            return
        
        message = "📊 **تقرير أداء العملات المرصودة** 📊\n\n"
        
        all_tracked_items = []
        for platform_name, symbols_data in performance_tracker.items():
            for symbol, data in symbols_data.items():
                data_copy = data.copy()
                data_copy['exchange'] = platform_name # Ensure exchange is in the dict
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
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_performance_report: {e}", exc_info=True)
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ أثناء جلب تقرير الأداء.")

# =============================================================================
# --- 5. المهام الآلية الدورية ---
# =============================================================================
def add_to_monitoring(symbol, alert_price, peak_volume, alert_time, source, exchange_name):
    platform_name = exchange_name
    if platform_name not in PLATFORMS: return
    
    if symbol not in active_hunts[platform_name]:
        active_hunts[platform_name][symbol] = {'alert_price': alert_price, 'alert_time': alert_time}
        
    if symbol not in performance_tracker[platform_name]:
        performance_tracker[platform_name][symbol] = {
            'alert_price': alert_price, 'alert_time': alert_time, 'source': source,
            'current_price': alert_price, 'high_price': alert_price, 'status': 'Tracking',
            'momentum_lost_alerted': False # !جديد: لتتبع إرسال تنبيه فقدان الزخم
        }
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol} on {exchange_name}")

async def fomo_hunter_loop(client: BaseExchangeClient, bot_data):
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
            
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
            
            for coin in sorted_coins[:5]:
                add_to_monitoring(coin['symbol'], float(coin['current_price']), coin.get('peak_volume', 0), now, f"صياد الفومو ({client.name})", client.name)

        except Exception as e:
            logger.error(f"Error in fomo_hunter_loop for {client.name}: {e}", exc_info=True)
        
async def new_listings_sniper_loop(client: BaseExchangeClient, bot_data):
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
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                known_symbols[client.name].update(newly_listed)
        except Exception as e:
            logger.error(f"An unexpected error in new_listings_sniper_loop for {client.name}: {e}")
            
async def performance_tracker_loop(session: aiohttp.ClientSession):
    logger.info("Performance Tracker background task started.")
    while True:
        await asyncio.sleep(RUN_PERFORMANCE_TRACKER_EVERY_MINUTES * 60)
        now = datetime.now(UTC)
        for platform in PLATFORMS:
            for symbol, data in list(performance_tracker[platform].items()):
                # أرشفة وحذف البيانات القديمة
                if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                    if performance_tracker[platform].get(symbol):
                         performance_tracker[platform][symbol]['status'] = 'Archived'
                    continue
                if data.get('status') == 'Archived':
                    if performance_tracker[platform].get(symbol):
                        del performance_tracker[platform][symbol]
                    continue
                
                try:
                    # تحديث السعر الحالي وتسجيل أعلى سعر
                    client = get_exchange_client(platform, session)
                    current_price = await client.get_current_price(symbol)
                    if not current_price: continue

                    if performance_tracker[platform].get(symbol):
                        performance_tracker[platform][symbol]['current_price'] = current_price
                        if current_price > data.get('high_price', 0):
                            performance_tracker[platform][symbol]['high_price'] = current_price
                        
                        # !جديد: التحقق من فقدان الزخم
                        high_price = performance_tracker[platform][symbol]['high_price']
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
                                bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                                performance_tracker[platform][symbol]['momentum_lost_alerted'] = True
                                logger.info(f"MOMENTUM LOSS ALERT sent for {symbol} on {platform}")

                except Exception as e:
                    logger.error(f"Error updating price for {symbol} on {platform}: {e}")

# =============================================================================
# --- 6. تشغيل البوت ---
# =============================================================================
def send_startup_message():
    try:
        message = "✅ **بوت التداول الذكي (v14.3 - Professional) متصل الآن!**\n\nأرسل /start لعرض القائمة."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

async def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set."); return
        
    async with aiohttp.ClientSession() as session:
        updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
        try: bot.get_updates(offset=-1, timeout=1, limit=1); logger.info("Cleared old updates.")
        except Exception: pass

        dp = updater.dispatcher
        loop = asyncio.get_running_loop()
        dp.bot_data['loop'] = loop
        dp.bot_data['session'] = session
        dp.bot_data['background_tasks_enabled'] = True
        
        dp.add_handler(CommandHandler("start", start_command))
        dp.add_handler(MessageHandler(Filters.text & (~Filters.command), handle_button_press))
        
        background_tasks['performance'] = asyncio.create_task(performance_tracker_loop(session))
        for platform_name in PLATFORMS:
            client = get_exchange_client(platform_name, session)
            background_tasks[f'fomo_{platform_name}'] = asyncio.create_task(fomo_hunter_loop(client, dp.bot_data))
            background_tasks[f'listings_{platform_name}'] = asyncio.create_task(new_listings_sniper_loop(client, dp.bot_data))
        
        updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot is now polling for commands...")
        send_startup_message() 
        await asyncio.gather(*background_tasks.values())

if __name__ == '__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Bot stopped manually.")
    except Exception as e: logger.critical(f"Bot failed to run: {e}", exc_info=True)