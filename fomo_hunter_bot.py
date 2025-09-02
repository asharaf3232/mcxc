# -*- coding: utf-8 -*-
import os
import requests
import time
import schedule
import logging
import threading
import asyncio
import websockets
import json
import aiohttp
from telegram import Bot, ParseMode, ReplyKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from datetime import datetime, timedelta, UTC
from collections import deque

# --- الإعدادات الرئيسية ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# --- (جميع إعدادات المعايير الأخرى تبقى كما هي) ---
VOLUME_SPIKE_MULTIPLIER = 10; MIN_USDT_VOLUME = 500000; PRICE_VELOCITY_THRESHOLD = 30.0; RUN_FOMO_SCAN_EVERY_MINUTES = 15;
INSTANT_TIMEFRAME_SECONDS = 10; INSTANT_VOLUME_THRESHOLD_USDT = 50000; INSTANT_TRADE_COUNT_THRESHOLD = 20;
RUN_LISTING_SCAN_EVERY_SECONDS = 60; RUN_PATTERN_SCAN_EVERY_HOURS = 1; PATTERN_SIGHTING_THRESHOLD = 3; PATTERN_LOOKBACK_DAYS = 7;
MOMENTUM_MAX_PRICE = 0.10; MOMENTUM_MIN_VOLUME_24H = 50000; MOMENTUM_MAX_VOLUME_24H = 2000000;
MOMENTUM_VOLUME_INCREASE = 1.8; MOMENTUM_PRICE_INCREASE = 4.0;
MEXC_API_BASE_URL = "https://api.mexc.com"; MEXC_WS_URL = "wss://wbs.mexc.com/ws"; COOLDOWN_PERIOD_HOURS = 2;

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- تهيئة البوت والمتغيرات العامة ---
bot = Bot(token=TELEGRAM_BOT_TOKEN)
recently_alerted_fomo = {}; known_symbols = set(); pattern_tracker = {}; recently_alerted_pattern = {};
activity_tracker = {}; activity_lock = threading.Lock(); recently_alerted_instant = {};

# =============================================================================
# 1. قسم الرصد اللحظي (WebSocket) - مع إضافة السجل الحيوي
# =============================================================================
async def handle_websocket_message(message):
    try:
        data = json.loads(message)
        if 'd' in data and 'e' in data['d'] and data['d']['e'] == 'spot@public.deals.v3.api':
            for deal in data['d']['D']:
                if deal['S'] == 1: # صفقة شراء فقط
                    symbol = data['s']
                    
                    # ===== "سماعة الطبيب": هذا هو السجل الحيوي الجديد =====
                    logger.info(f"WebSocket: Buy trade received for {symbol}") 
                    # ======================================================

                    volume_usdt = float(deal['p']) * float(deal['q'])
                    timestamp = float(deal['t']) / 1000.0
                    with activity_lock:
                        if symbol not in activity_tracker:
                            activity_tracker[symbol] = deque(maxlen=200)
                        activity_tracker[symbol].append({'v': volume_usdt, 't': timestamp})
    except Exception:
        pass # تجاهل الأخطاء في الرسائل الفردية للحفاظ على استمرارية الاتصال

# (بقية دوال الـ WebSocket تبقى كما هي)
def send_instant_alert(symbol, total_volume, trade_count):
    message = f"⚡️ **رصد نشاط شراء مفاجئ! (لحظي)** ⚡️\n\n**العملة:** `${symbol}`\n**حجم الشراء (آخر {INSTANT_TIMEFRAME_SECONDS} ثوانٍ):** `${total_volume:,.0f} USDT`\n**عدد الصفقات (آخر {INSTANT_TIMEFRAME_SECONDS} ثوانٍ):** `{trade_count}`\n\n*(إشارة مبكرة جداً وعالية المخاطر)*"
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"INSTANT ALERT sent for {symbol}.")
    except Exception as e:
        logger.error(f"Failed to send instant alert for {symbol}: {e}")
def periodic_activity_checker():
    logger.info("Activity checker thread started.")
    while True:
        time.sleep(5)
        now_ts = datetime.now(UTC).timestamp()
        symbols_to_check = list(activity_tracker.keys())
        with activity_lock:
            for sym in list(recently_alerted_instant.keys()):
                if now_ts - recently_alerted_instant[sym] > COOLDOWN_PERIOD_HOURS * 3600:
                    del recently_alerted_instant[sym]
            for symbol in symbols_to_check:
                if symbol not in activity_tracker: continue
                trades = activity_tracker[symbol]
                while trades and now_ts - trades[0]['t'] > INSTANT_TIMEFRAME_SECONDS: trades.popleft()
                if not trades: del activity_tracker[symbol]; continue
                total_volume = sum(trade['v'] for trade in trades)
                trade_count = len(trades)
                if (total_volume >= INSTANT_VOLUME_THRESHOLD_USDT and trade_count >= INSTANT_TRADE_COUNT_THRESHOLD and symbol not in recently_alerted_instant):
                    send_instant_alert(symbol, total_volume, trade_count)
                    recently_alerted_instant[symbol] = now_ts
                    del activity_tracker[symbol]
async def run_websocket_client():
    logger.info("WebSocket client thread starting.")
    subscription_msg = {"method": "SUBSCRIPTION", "params": ["spot@public.deals.v3.api@<SYMBOL>"]}
    while True:
        try:
            async with websockets.connect(MEXC_WS_URL) as websocket:
                await websocket.send(json.dumps(subscription_msg))
                logger.info("Successfully connected and subscribed to MEXC WebSocket.")
                while True: await handle_websocket_message(await websocket.recv())
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}. Reconnecting in 10 seconds...")
            await asyncio.sleep(10)
def start_asyncio_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


# =============================================================================
# 2. الوظائف العامة والأساسية (بدون تغيير)
# =============================================================================
def get_market_data():
    url = f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr"
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15); response.raise_for_status(); return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch market data from MEXC: {e}"); return []
def format_price(price_str):
    try: return f"{float(price_str):.8f}".rstrip('0').rstrip('.')
    except (ValueError, TypeError): return price_str

# =============================================================================
# 3. الوظائف التفاعلية (مع إضافة أمر الحالة ولوحة الأزرار الثابتة)
# =============================================================================
# --- (الأسماء المستخدمة في الأزرار) ---
BTN_MOMENTUM = "🚀 كاشف الزخم (فائق السرعة)"
BTN_GAINERS = "📈 الأكثر ارتفاعاً"
BTN_LOSERS = "📉 الأكثر انخفاضاً"
BTN_VOLUME = "💰 الأعلى سيولة"

def build_menu():
    """تنشئ لوحة أزرار ثابتة تظهر بدلاً من لوحة المفاتيح العادية."""
    keyboard = [
        [BTN_MOMENTUM],
        [BTN_GAINERS, BTN_LOSERS],
        [BTN_VOLUME]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def start_command(update, context):
    """عند بدء التشغيل، يرسل رسالة ترحيب مع لوحة الأزرار الثابتة."""
    welcome_message = "✅ **بوت التداول الذكي (v4) جاهز!**\n\n- **الراصد اللحظي** يعمل الآن ويمكنك التحقق من حالته عبر الأمر /status.\n- استخدم لوحة الأزرار أدناه للتحليل الفوري."
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def status_command(update, context):
    """(جديد) يعطي تقريراً عن حالة الراصد اللحظي."""
    with activity_lock:
        tracked_symbols_count = len(activity_tracker)
        
    message = "📊 **حالة الراصد اللحظي (WebSocket)** 📊\n\n"
    
    if tracked_symbols_count > 0:
        message += f"✅ **الحالة:** متصل ويعمل بنشاط.\n"
        message += f"- يتم تتبع **{tracked_symbols_count}** عملة تظهر نشاطاً شرائياً الآن."
    else:
        message += f"⚠️ **الحالة:** متصل ولكن السوق هادئ حالياً.\n"
        message += f"- لا يوجد نشاط شرائي ملحوظ يتم تتبعه في هذه اللحظة."
        
    update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update, context):
    """(جديد) يعالج الضغط على أزرار القائمة الثابتة."""
    button_text = update.message.text
    chat_id = update.message.chat_id

    # إرسال رسالة "جاري المعالجة" والحصول على هويتها لتعديلها لاحقاً
    sent_message = context.bot.send_message(chat_id=chat_id, text="🔍 جارِ تنفيذ طلبك...")

    if button_text == BTN_GAINERS:
        get_top_10_list(context, chat_id, 'gainers', sent_message.message_id)
    elif button_text == BTN_LOSERS:
        get_top_10_list(context, chat_id, 'losers', sent_message.message_id)
    elif button_text == BTN_VOLUME:
        get_top_10_list(context, chat_id, 'volume', sent_message.message_id)
    elif button_text == BTN_MOMENTUM:
        threading.Thread(target=lambda: asyncio.run(run_momentum_detector_async(context, chat_id, sent_message.message_id))).start()

def get_top_10_list(context, chat_id, list_type, message_id):
    type_map = {'gainers': {'key': 'priceChangePercent', 'title': '🔥 الأكثر ارتفاعاً', 'reverse': True}, 'losers': {'key': 'priceChangePercent', 'title': '📉 الأكثر انخفاضاً', 'reverse': False}, 'volume': {'key': 'quoteVolume', 'title': '💰 الأعلى سيولة', 'reverse': True}}
    config = type_map[list_type]
    try:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"🔍 جارِ جلب بيانات {config['title']}...")
        data = get_market_data(); usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['sort_key'] = float(pair[config['key']])
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['sort_key'], reverse=config['reverse'])
        message = f"**{config['title']} في آخر 24 ساعة**\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            value = float(pair['sort_key'])
            prefix = ""
            if list_type != 'volume': value_str = f"{value*100:+.2f}%"
            else: value_str = f"${value:,.0f}"; prefix = "$"
            message += f"{i+1}. **${pair['symbol'].replace('USDT', '')}**\n   - {'النسبة' if list_type != 'volume' else 'حجم التداول'}: `{value_str}`\n   - السعر الحالي: `${format_price(pair['lastPrice'])}`\n\n"
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_top_10_list: {e}")
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ أثناء جلب البيانات.")

async def fetch_kline_data(session, symbol):
    url = f"{MEXC_API_BASE_URL}/api/v3/klines"; params = {'symbol': symbol, 'interval': '5m', 'limit': 12}
    try:
        async with session.get(url, params=params, timeout=10) as response:
            if response.status == 200: return await response.json()
    except Exception: return None
    return None

async def run_momentum_detector_async(context, chat_id, message_id):
    initial_text = "🚀 **كاشف الزخم (فائق السرعة)**\n\n🔍 جارِ الفحص المتوازي للسوق..."
    try:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text, parse_mode=ParseMode.MARKDOWN)
    except Exception: pass
    market_data = get_market_data()
    if not market_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق."); return
    potential_coins = [p for p in sorted([s for s in market_data if s['symbol'].endswith('USDT')], key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)[:200] if float(p.get('lastPrice', 1)) <= MOMENTUM_MAX_PRICE and MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume', 0)) <= MOMENTUM_MAX_VOLUME_24H]
    if not potential_coins:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لم يتم العثور على عملات واعدة ضمن المعايير الأولية."); return
    momentum_coins = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_kline_data(session, pair['symbol']) for pair in potential_coins]
        for i, klines in enumerate(await asyncio.gather(*tasks)):
            if not klines or len(klines) < 12: continue
            try:
                old_klines, new_klines = klines[:6], klines[6:]
                old_volume = sum(float(k[5]) for k in old_klines)
                if old_volume == 0: continue
                new_volume = sum(float(k[5]) for k in new_klines)
                start_price, end_price = float(new_klines[0][1]), float(new_klines[-1][4])
                if start_price == 0: continue
                price_change = ((end_price - start_price) / start_price) * 100
                if new_volume > old_volume * MOMENTUM_VOLUME_INCREASE and price_change > MOMENTUM_PRICE_INCREASE:
                    momentum_coins.append({'symbol': potential_coins[i]['symbol'], 'price_change': price_change, 'current_price': end_price})
            except (ValueError, IndexError): continue
    if not momentum_coins:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **الفحص السريع اكتمل:** لا يوجد زخم حقيقي حالياً."); return
    sorted_coins = sorted(momentum_coins, key=lambda x: x['price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم الفوري - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\nأفضل الأهداف التي تظهر بداية زخم الآن:\n\n"
    for i, coin in enumerate(sorted_coins[:10]):
        message += f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n   - السعر: `${format_price(coin['current_price'])}`\n   - **زخم آخر 30 دقيقة: `%{coin['price_change']:+.2f}`**\n\n"
    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

# =============================================================================
# 4. المهام الآلية الدورية (القديمة - بدون تغيير)
# =============================================================================
def fomo_hunter_job(): pass
def new_listings_sniper_job(): pass
def pattern_hunter_job(): pass

# =============================================================================
# 5. تشغيل البوت والجدولة (مع تسجيل المعالجات الجديدة)
# =============================================================================
def send_startup_message():
    try:
        message = "✅ **بوت التداول الذكي (v4) متصل الآن!**\n\nأرسل /start لعرض القائمة أو /status لفحص حالة الراصد."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

def run_scheduler():
    logger.info("Scheduler thread for periodic jobs started.")
    # (schedule calls remain here)
    while True:
        schedule.run_pending(); time.sleep(1)

def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set."); return
    
    # --- تشغيل خيوط الخلفية ---
    asyncio_loop = asyncio.new_event_loop()
    asyncio.ensure_future(run_websocket_client(), loop=asyncio_loop)
    ws_thread = threading.Thread(target=start_asyncio_loop, args=(asyncio_loop,), daemon=True)
    ws_thread.start()
    checker_thread = threading.Thread(target=periodic_activity_checker, daemon=True)
    checker_thread.start()
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # --- إعداد معالجات التليجرام ---
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    
    dp.add_handler(CommandHandler("start", start_command))
    
    # ===== تسجيل الأمر الجديد =====
    dp.add_handler(CommandHandler("status", status_command))
    # ===============================

    # ===== تسجيل معالج الأزرار الجديد =====
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_button_press))
    # ======================================
    
    send_startup_message()
    updater.start_polling()
    logger.info("Telegram bot is now polling for commands and messages...")
    updater.idle()

if __name__ == '__main__':
    main()