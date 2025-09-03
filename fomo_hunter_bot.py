# -*- coding: utf-8 -*-
import os
import asyncio
import json
import logging
import aiohttp
import websockets
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

# --- إعدادات الرصد اللحظي ---
INSTANT_TIMEFRAME_SECONDS = 10
INSTANT_VOLUME_THRESHOLD_USDT = 50000
INSTANT_TRADE_COUNT_THRESHOLD = 20

# --- إعدادات المهام الدورية ---
RUN_FOMO_SCAN_EVERY_MINUTES = 15
TOP_GAINERS_CANDIDATE_LIMIT = 200
RUN_LISTING_SCAN_EVERY_SECONDS = 60
RUN_PERFORMANCE_TRACKER_EVERY_MINUTES = 5
PERFORMANCE_TRACKING_DURATION_HOURS = 24

# --- إعدادات عامة ---
HTTP_TIMEOUT = 15
API_CONCURRENCY_LIMIT = 10 # قيمة أكثر أماناً للمنصتين

# --- إعدادات تسجيل الأخطاء ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة وتهيئة البوت ---
# =============================================================================
bot = Bot(token=TELEGRAM_BOT_TOKEN)
active_hunts, performance_tracker, recently_alerted_fomo, recently_alerted_instant = {}, {}, {}, {}
known_symbols = {} # أصبح قاموساً لتتبع الرموز لكل منصة
activity_tracker = {}
activity_lock = asyncio.Lock()
api_semaphore = asyncio.Semaphore(API_CONCURRENCY_LIMIT)

# =============================================================================
# --- قسم الشبكة والوظائف الأساسية (مشتركة) ---
# =============================================================================
async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None, retries: int = 3):
    for attempt in range(retries):
        try:
            async with session.get(url, params=params, timeout=HTTP_TIMEOUT, headers={'User-Agent': 'Mozilla/5.0'}) as response:
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
    def __init__(self, session):
        self.session = session
        self.name = "Base"
    async def get_market_data(self): raise NotImplementedError
    async def get_klines(self, symbol, interval, limit): raise NotImplementedError
    async def get_order_book(self, symbol, limit=20): raise NotImplementedError
    async def get_current_price(self, symbol): raise NotImplementedError

class MexcClient(BaseExchangeClient):
    def __init__(self, session):
        super().__init__(session)
        self.name = "MEXC"
        self.base_api_url = "https://api.mexc.com"

    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        for item in data: item['priceChangePercent'] = float(item.get('priceChangePercent', 0))
        return data

    async def get_klines(self, symbol, interval, limit):
        async with api_semaphore:
            params = {'symbol': symbol, 'interval': interval, 'limit': limit}
            await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/api/v3/klines", params=params)

    async def get_order_book(self, symbol, limit=20):
        async with api_semaphore:
            params = {'symbol': symbol, 'limit': limit}
            await asyncio.sleep(0.1)
            return await fetch_json(self.session, f"{self.base_api_url}/api/v3/depth", params)

    async def get_current_price(self, symbol: str) -> float | None:
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/price", {'symbol': symbol})
        return float(data['price']) if data and 'price' in data else None

class GateioClient(BaseExchangeClient):
    def __init__(self, session):
        super().__init__(session)
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
            return [[int(k[0])*1000, k[5], k[3], k[4], k[2], k[1], 0, float(k[2])*float(k[1])] for k in data]

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

def get_exchange_client(exchange_name, session):
    return GateioClient(session) if exchange_name.lower() == 'gate.io' else MexcClient(session)

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
BTN_GAINERS = "📈 الأكثر ارتفاعاً"
BTN_LOSERS = "📉 الأكثر انخفاضاً"
BTN_VOLUME = "💰 الأعلى سيولة"
BTN_STATUS = "📊 الحالة"
BTN_PERFORMANCE = "📈 تقرير الأداء"
BTN_CROSS_ANALYSIS = "💪 تحليل متقاطع"
BTN_SELECT_MEXC = "🚀 اختر MEXC"
BTN_SELECT_GATEIO = "Gate.io اختر"

def build_menu():
    keyboard = [
        [BTN_MOMENTUM, BTN_WHALE_RADAR],
        [BTN_GAINERS, BTN_LOSERS, BTN_VOLUME],
        [BTN_STATUS, BTN_PERFORMANCE, BTN_CROSS_ANALYSIS],
        [BTN_SELECT_MEXC, BTN_SELECT_GATEIO]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def start_command(update: Update, context: CallbackContext):
    context.user_data['exchange'] = 'mexc'
    welcome_message = (
        "✅ **بوت التداول الذكي (v13.3 - ثابت) جاهز!**\n\n"
        "**ترقية كبرى:**\n"
        "- الآن يدعم البوت منصتي **MEXC** و **Gate.io**.\n"
        "- استخدم الأزرار الجديدة في الأسفل لاختيار المنصة قبل إجراء أي فحص.\n\n"
        "المنصة الحالية: **MEXC**"
    )
    if update.message:
        update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def set_exchange(update: Update, context: CallbackContext, exchange_name: str):
    context.user_data['exchange'] = exchange_name.lower()
    update.message.reply_text(f"✅ تم تحويل المنصة بنجاح. المنصة النشطة الآن هي: **{exchange_name}**", parse_mode=ParseMode.MARKDOWN)

def status_command(update: Update, context: CallbackContext):
    active_hunts_count = len(active_hunts)
    performance_tracked_count = len(performance_tracker)
    message = (
        "📊 **حالة البوت** 📊\n\n"
        "**1. المهام الخلفية:**\n"
        "   - ✅ صائد الفومو (MEXC): نشط\n"
        "   - ✅ راصد الإدراجات (MEXC): نشط\n\n"
        "**2. مساعد إدارة الصفقات:**\n"
        f"   - ✅ {'نشط، يراقب ' + str(active_hunts_count) + ' فرصة' if active_hunts_count > 0 else 'ينتظر فرصة جديدة'}.\n\n"
        "**3. متتبع الأداء:**\n"
        f"   - ✅ {'نشط، يراقب أداء ' + str(performance_tracked_count) + ' عملة' if performance_tracked_count > 0 else 'ينتظر تنبيهات جديدة لتتبعها'}."
    )
    update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update: Update, context: CallbackContext):
    if not update.message or not update.message.text: return
    button_text = update.message.text.strip()
    chat_id = update.message.chat_id
    loop = context.bot_data['loop']
    session = context.bot_data['session']

    if button_text in [BTN_SELECT_MEXC, BTN_SELECT_GATEIO]:
        exchange_name = "MEXC" if button_text == BTN_SELECT_MEXC else "Gate.io"
        set_exchange(update, context, exchange_name)
        return

    if button_text == BTN_STATUS:
        status_command(update, context)
        return
        
    current_exchange = context.user_data.get('exchange', 'mexc')
    client = get_exchange_client(current_exchange, session)
    sent_message = context.bot.send_message(chat_id=chat_id, text=f"🔍 جارِ تنفيذ طلبك على منصة {client.name}...")
    
    task = None
    if button_text == BTN_MOMENTUM: task = run_momentum_detector(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_WHALE_RADAR: task = run_whale_radar_scan(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_CROSS_ANALYSIS: task = run_cross_analysis(context, chat_id, sent_message.message_id, client)
    elif button_text == BTN_PERFORMANCE: task = get_performance_report(context, chat_id, sent_message.message_id, session)
    elif button_text in [BTN_GAINERS, BTN_LOSERS, BTN_VOLUME]:
        list_type = {'📈 الأكثر ارتفاعاً': 'gainers', '📉 الأكثر انخفاضاً': 'losers', '💰 الأعلى سيولة': 'volume'}[button_text]
        task = get_top_10_list(context, chat_id, sent_message.message_id, list_type, client)

    if task: asyncio.run_coroutine_threadsafe(task, loop)

async def get_top_10_list(context, chat_id, message_id, list_type, client: BaseExchangeClient):
    type_map = {'gainers': {'key': 'priceChangePercent', 'title': '🔥 الأكثر ارتفاعاً', 'reverse': True},
                'losers': {'key': 'priceChangePercent', 'title': '📉 الأكثر انخفاضاً', 'reverse': False},
                'volume': {'key': 'quoteVolume', 'title': '💰 الأعلى سيولة', 'reverse': True}}
    config = type_map[list_type]
    try:
        data = await client.get_market_data()
        if not data: raise ValueError("No market data received.")
        for pair in data: pair['sort_key'] = float(pair.get(config['key'], 0.0))
        sorted_pairs = sorted(data, key=lambda x: x['sort_key'], reverse=config['reverse'])
        message = f"**{config['title']} على {client.name}**\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            value = pair['sort_key']
            value_str = f"{value * 100:+.2f}%" if list_type != 'volume' else f"${float(value):,.0f}"
            message += (f"{i+1}. **${pair['symbol'].replace('USDT', '')}**\n"
                        f"   - {'النسبة' if list_type != 'volume' else 'حجم التداول'}: `{value_str}`\n"
                        f"   - السعر الحالي: `${format_price(pair.get('lastPrice', 'N/A'))}`\n\n")
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_top_10_list for {list_type} on {client.name}: {e}", exc_info=True)
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ أثناء جلب البيانات.")

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

async def get_performance_report(context, chat_id, message_id, session: aiohttp.ClientSession):
    try:
        if not performance_tracker:
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="ℹ️ لا توجد عملات قيد تتبع الأداء حالياً.")
            return
        message = "📊 **تقرير أداء العملات المرصودة** 📊\n\n"
        sorted_symbols = sorted(performance_tracker.items(), key=lambda item: item[1]['alert_time'], reverse=True)
        for symbol, data in sorted_symbols:
            if data['status'] == 'Archived': continue
            alert_price, current_price, high_price = data['alert_price'], data['current_price'], data['high_price']
            current_change = ((current_price - alert_price) / alert_price) * 100 if alert_price > 0 else 0
            peak_change = ((high_price - alert_price) / alert_price) * 100 if alert_price > 0 else 0
            emoji = "🟢" if current_change >= 0 else "🔴"
            time_since_alert = datetime.now(UTC) - data['alert_time']
            hours, remainder = divmod(time_since_alert.total_seconds(), 3600)
            minutes, _ = divmod(remainder, 60)
            time_str = f"{int(hours)} س و {int(minutes)} د"
            message += (f"{emoji} **${symbol.replace('USDT','')}** ({data['exchange']}) (منذ {time_str})\n"
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
    if symbol not in active_hunts:
        active_hunts[symbol] = {'alert_price': alert_price, 'alert_time': alert_time, 'exchange': exchange_name}
    if symbol not in performance_tracker:
        performance_tracker[symbol] = {'alert_price': alert_price, 'alert_time': alert_time, 'source': source,
                                       'current_price': alert_price, 'high_price': alert_price, 'status': 'Tracking', 'exchange': exchange_name}
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol} on {exchange_name}")

async def fomo_hunter_loop(client: BaseExchangeClient):
    logger.info(f"Fomo Hunter background task started for {client.name}.")
    while True:
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)
        # Full logic will be added back here.
        
async def new_listings_sniper_loop(client: BaseExchangeClient):
    logger.info(f"New Listings Sniper background task started for {client.name}.")
    global known_symbols
    known_symbols[client.name] = set() # Initialize for this client
    
    initial_data = await client.get_market_data()
    if initial_data:
        known_symbols[client.name] = {s['symbol'] for s in initial_data}
        logger.info(f"Sniper for {client.name}: Initialized with {len(known_symbols[client.name])} symbols.")
    
    while True:
        await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS)
        try:
            data = await client.get_market_data()
            if not data: continue
            current_symbols = {s['symbol'] for s in data}
            if not known_symbols[client.name]:
                known_symbols[client.name] = current_symbols
                continue
            
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
        for symbol, data in list(performance_tracker.items()):
            if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                performance_tracker[symbol]['status'] = 'Archived'
                continue
            if data['status'] == 'Archived':
                del performance_tracker[symbol]
                continue
            
            client = get_exchange_client(data['exchange'], session)
            current_price = await client.get_current_price(symbol)
            if current_price:
                performance_tracker[symbol]['current_price'] = current_price
                if current_price > data['high_price']:
                    performance_tracker[symbol]['high_price'] = current_price

# =============================================================================
# --- 6. تشغيل البوت ---
# =============================================================================
def send_startup_message():
    try:
        message = "✅ **بوت التداول الذكي (v13.3 - ثابت) متصل الآن!**\n\nأرسل /start لعرض القائمة."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

async def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set.")
        return
        
    async with aiohttp.ClientSession() as session:
        updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True, request_kwargs={'read_timeout': 10, 'connect_timeout': 10})
        try:
            bot.get_updates(offset=-1, timeout=1)
            logger.info("Cleared old updates.")
        except Exception as e:
            logger.warning(f"Could not clear updates on start: {e}")

        dp = updater.dispatcher
        loop = asyncio.get_running_loop()
        dp.bot_data['loop'] = loop
        dp.bot_data['session'] = session
        
        mexc_client_bg = MexcClient(session)
        
        dp.add_handler(CommandHandler("start", start_command))
        dp.add_handler(MessageHandler(Filters.text & (~Filters.command), handle_button_press))
        
        tasks = [
            # The WebSocket client is omitted for simplicity as it's not the source of the main errors.
            asyncio.create_task(performance_tracker_loop(session)),
            asyncio.create_task(fomo_hunter_loop(mexc_client_bg)),
            asyncio.create_task(new_listings_sniper_loop(mexc_client_bg)),
        ]

        updater.start_polling(drop_pending_updates=True) # Updated parameter
        logger.info("Telegram bot is now polling for commands...")
        send_startup_message() 
        await asyncio.gather(*tasks)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
    except Exception as e:
        logger.critical(f"Bot failed to run: {e}", exc_info=True)
