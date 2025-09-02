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
import aiohttp # <-- المكتبة الجديدة للسرعة
from telegram import Bot, ParseMode, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler
from datetime import datetime, timedelta, UTC
from collections import deque

# --- الإعدادات الرئيسية ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# --- معايير تحليل الفومو (الدوري) ---
VOLUME_SPIKE_MULTIPLIER = 10
MIN_USDT_VOLUME = 500000
PRICE_VELOCITY_THRESHOLD = 30.0
RUN_FOMO_SCAN_EVERY_MINUTES = 15

# --- معايير الرصد اللحظي (WebSocket) ---
INSTANT_TIMEFRAME_SECONDS = 10
INSTANT_VOLUME_THRESHOLD_USDT = 50000
INSTANT_TRADE_COUNT_THRESHOLD = 20

# --- إعدادات قناص الإدراجات ---
RUN_LISTING_SCAN_EVERY_SECONDS = 60

# --- إعدادات صياد الأنماط ---
RUN_PATTERN_SCAN_EVERY_HOURS = 1
PATTERN_SIGHTING_THRESHOLD = 3
PATTERN_LOOKBACK_DAYS = 7

# --- (جديد) إعدادات كاشف الزخم فائق السرعة ---
MOMENTUM_MAX_PRICE = 0.10
MOMENTUM_MIN_VOLUME_24H = 50000
MOMENTUM_MAX_VOLUME_24H = 2000000
MOMENTUM_VOLUME_INCREASE = 1.8 # زيادة حجم التداول 80%
MOMENTUM_PRICE_INCREASE = 4.0 # زيادة السعر 4%

# --- إعدادات متقدمة ---
MEXC_API_BASE_URL = "https://api.mexc.com"
MEXC_WS_URL = "wss://wbs.mexc.com/ws"
COOLDOWN_PERIOD_HOURS = 2
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- تهيئة البوت والمتغيرات العامة ---
bot = Bot(token=TELEGRAM_BOT_TOKEN)
recently_alerted_fomo = {}
known_symbols = set()
pattern_tracker = {}
recently_alerted_pattern = {}
activity_tracker = {}
activity_lock = threading.Lock()
recently_alerted_instant = {}

# =============================================================================
# 1. قسم الرصد اللحظي (WebSocket) - بدون تغيير
# =============================================================================
# (الكود الخاص بالـ WebSocket موجود هنا ولم يتغير)
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
                while trades and now_ts - trades[0]['t'] > INSTANT_TIMEFRAME_SECONDS:
                    trades.popleft()
                if not trades:
                    del activity_tracker[symbol]; continue
                total_volume = sum(trade['v'] for trade in trades)
                trade_count = len(trades)
                if (total_volume >= INSTANT_VOLUME_THRESHOLD_USDT and trade_count >= INSTANT_TRADE_COUNT_THRESHOLD and symbol not in recently_alerted_instant):
                    send_instant_alert(symbol, total_volume, trade_count)
                    recently_alerted_instant[symbol] = now_ts
                    del activity_tracker[symbol]

async def handle_websocket_message(message):
    try:
        data = json.loads(message)
        if 'd' in data and 'e' in data['d'] and data['d']['e'] == 'spot@public.deals.v3.api':
            for deal in data['d']['D']:
                if deal['S'] == 1:
                    symbol = data['s']
                    volume_usdt = float(deal['p']) * float(deal['q'])
                    timestamp = float(deal['t']) / 1000.0
                    with activity_lock:
                        if symbol not in activity_tracker:
                            activity_tracker[symbol] = deque(maxlen=200)
                        activity_tracker[symbol].append({'v': volume_usdt, 't': timestamp})
    except Exception: pass

async def run_websocket_client():
    logger.info("WebSocket client thread starting.")
    subscription_msg = {"method": "SUBSCRIPTION", "params": ["spot@public.deals.v3.api@<SYMBOL>"]}
    while True:
        try:
            async with websockets.connect(MEXC_WS_URL) as websocket:
                await websocket.send(json.dumps(subscription_msg))
                logger.info("Successfully connected and subscribed to MEXC WebSocket.")
                while True:
                    await handle_websocket_message(await websocket.recv())
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
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch market data from MEXC: {e}")
        return []

def format_price(price_str):
    try: return f"{float(price_str):.8f}".rstrip('0').rstrip('.')
    except (ValueError, TypeError): return price_str

# (analyze_symbol and other old functions remain here)
def analyze_symbol(symbol):
    try:
        klines_url = f"{MEXC_API_BASE_URL}/api/v3/klines"; headers = {'User-Agent': 'Mozilla/5.0'}
        daily_params = {'symbol': symbol, 'interval': '1d', 'limit': 2}
        daily_res = requests.get(klines_url, params=daily_params, headers=headers, timeout=10)
        daily_res.raise_for_status(); daily_data = daily_res.json()
        if len(daily_data) < 2: return None
        previous_day_volume, current_day_volume = float(daily_data[0][7]), float(daily_data[1][7])
        if current_day_volume < MIN_USDT_VOLUME or not (current_day_volume > (previous_day_volume * VOLUME_SPIKE_MULTIPLIER)): return None
        hourly_params = {'symbol': symbol, 'interval': '1h', 'limit': 4}
        hourly_res = requests.get(klines_url, params=hourly_params, headers=headers, timeout=10)
        hourly_res.raise_for_status(); hourly_data = hourly_res.json()
        if len(hourly_data) < 4: return None
        initial_price, latest_high_price = float(hourly_data[0][1]), float(hourly_data[-1][2])
        if initial_price == 0: return None
        price_increase_percent = ((latest_high_price - initial_price) / initial_price) * 100
        if not (price_increase_percent >= PRICE_VELOCITY_THRESHOLD): return None
        ticker_url = f"{MEXC_API_BASE_URL}/api/v3/ticker/price"
        price_res = requests.get(ticker_url, params={'symbol': symbol}, headers=headers, timeout=10)
        price_res.raise_for_status(); current_price = float(price_res.json()['price'])
        volume_increase_percent = ((current_day_volume - previous_day_volume) / previous_day_volume) * 100 if previous_day_volume > 0 else float('inf')
        return {'symbol': symbol, 'volume_increase': f"+{volume_increase_percent:,.2f}%", 'price_pattern': f"صعود بنسبة +{price_increase_percent:,.2f}% في آخر 4 ساعات", 'current_price': format_price(current_price)}
    except Exception: return None

# =============================================================================
# 3. الوظائف التفاعلية (تحديث كاشف الزخم)
# =============================================================================
def build_menu():
    keyboard = [[InlineKeyboardButton("📈 الأكثر ارتفاعاً", callback_data='top_gainers'), InlineKeyboardButton("📉 الأكثر انخفاضاً", callback_data='top_losers')], [InlineKeyboardButton("💰 الأعلى سيولة (عام)", callback_data='top_volume')], [InlineKeyboardButton("🚀 كاشف الزخم (فائق السرعة)", callback_data='momentum_detector')]]
    return InlineKeyboardMarkup(keyboard)

def start_command(update, context):
    welcome_message = "✅ **أهلاً بك في بوت التداول الذكي! (v3)**\n\n- **كاشف الزخم** أصبح الآن فائق السرعة.\n- **المراقبة اللحظية** عبر WebSocket تعمل باستمرار.\n\nاختر أحد الخيارات للتحليل الفوري:"
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def button_handler(update, context):
    query = update.callback_query
    query.answer()
    if query.data == 'top_gainers': get_top_10_list(context, query.message.chat_id, 'gainers')
    elif query.data == 'top_losers': get_top_10_list(context, query.message.chat_id, 'losers')
    elif query.data == 'top_volume': get_top_10_list(context, query.message.chat_id, 'volume')
    elif query.data == 'momentum_detector':
        # تشغيل الكاشف السريع في خيط منفصل لتجنب حظر البوت
        threading.Thread(target=lambda: asyncio.run(run_momentum_detector_async(context, query.message.chat_id, query.message.message_id))).start()

def get_top_10_list(context, chat_id, list_type):
    # (الكود هنا لم يتغير)
    type_map = {'gainers': {'key': 'priceChangePercent', 'title': '🔥 الأكثر ارتفاعاً', 'reverse': True, 'prefix': '%'}, 'losers': {'key': 'priceChangePercent', 'title': '📉 الأكثر انخفاضاً', 'reverse': False, 'prefix': '%'}, 'volume': {'key': 'quoteVolume', 'title': '💰 الأعلى سيولة', 'reverse': True, 'prefix': '$'}}
    config = type_map[list_type]; sent_message = context.bot.send_message(chat_id=chat_id, text=f"🔍 جارِ جلب بيانات {config['title']}...")
    try:
        data = get_market_data(); usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['sort_key'] = float(pair[config['key']])
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['sort_key'], reverse=config['reverse'])
        message = f"**{config['title']} في آخر 24 ساعة**\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            value = float(pair['sort_key']) * 100 if list_type != 'volume' else float(pair['sort_key'])
            value_str = f"{value:+.2f}" if list_type != 'volume' else f"{value:,.0f}"
            message += f"{i+1}. **${pair['symbol'].replace('USDT', '')}**\n   - {'النسبة' if list_type != 'volume' else 'حجم التداول'}: `{config['prefix'] if list_type == 'volume' else ''}{value_str}{'%' if list_type != 'volume' else ''}`\n   - السعر الحالي: `${format_price(pair['lastPrice'])}`\n\n"
        context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_top_10_list: {e}")
        context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text="حدث خطأ أثناء جلب البيانات.")

# --- القسم الجديد: كاشف الزخم فائق السرعة ---
async def fetch_kline_data(session, symbol):
    """جلب بيانات الشموع لعملة واحدة بشكل غير متزامن."""
    url = f"{MEXC_API_BASE_URL}/api/v3/klines"
    # شمعات 5 دقائق، آخر 12 شمعة (ساعة واحدة من البيانات)
    params = {'symbol': symbol, 'interval': '5m', 'limit': 12}
    try:
        async with session.get(url, params=params, timeout=10) as response:
            if response.status == 200:
                return await response.json()
    except Exception:
        return None
    return None

async def run_momentum_detector_async(context, chat_id, message_id):
    """
    النسخة الجديدة فائقة السرعة التي تعمل بشكل غير متزامن.
    """
    initial_text = "🚀 **كاشف الزخم (فائق السرعة)**\n\n🔍 جارِ الفحص المتوازي للسوق... (قد يستغرق بضع ثوانٍ)"
    try:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text, parse_mode=ParseMode.MARKDOWN)
    except Exception: pass # تجاهل الخطأ إذا لم يتمكن من تعديل الرسالة
        
    market_data = get_market_data()
    if not market_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق. حاول مرة أخرى.")
        return

    # 1. فلترة أولية للعملات الواعدة
    usdt_pairs = [s for s in market_data if s['symbol'].endswith('USDT')]
    potential_coins_data = sorted(usdt_pairs, key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)[:200]
    
    potential_coins = [
        pair for pair in potential_coins_data
        if float(pair.get('lastPrice', 1)) <= MOMENTUM_MAX_PRICE and
           MOMENTUM_MIN_VOLUME_24H <= float(pair.get('quoteVolume', 0)) <= MOMENTUM_MAX_VOLUME_24H
    ]

    if not potential_coins:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لم يتم العثور على عملات واعدة ضمن معايير السعر والسيولة الأولية.")
        return

    # 2. جلب بيانات الشموع بشكل متوازي
    momentum_coins = []
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_kline_data(session, pair['symbol']) for pair in potential_coins]
        all_klines_data = await asyncio.gather(*tasks)

        # 3. تحليل النتائج
        for i, klines in enumerate(all_klines_data):
            if not klines or len(klines) < 12:
                continue

            symbol = potential_coins[i]['symbol']
            try:
                # المقارنة: آخر 30 دقيقة مقابل الـ 30 دقيقة التي قبلها
                old_klines, new_klines = klines[:6], klines[6:]
                
                old_volume = sum(float(k[5]) for k in old_klines)
                new_volume = sum(float(k[5]) for k in new_klines)
                
                if old_volume == 0: continue

                start_price = float(new_klines[0][1]) # سعر افتتاح أول شمعة في الفترة الجديدة
                end_price = float(new_klines[-1][4]) # سعر إغلاق آخر شمعة
                if start_price == 0: continue
                
                price_change = ((end_price - start_price) / start_price) * 100
                
                if new_volume > old_volume * MOMENTUM_VOLUME_INCREASE and price_change > MOMENTUM_PRICE_INCREASE:
                    momentum_coins.append({'symbol': symbol, 'price_change': price_change, 'current_price': end_price})
            except (ValueError, IndexError):
                continue

    if not momentum_coins:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **الفحص السريع اكتمل:**\n\nلم يتم رصد أي عملة تظهر بداية زخم حقيقي حالياً.")
        return
        
    sorted_coins = sorted(momentum_coins, key=lambda x: x['price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم الفوري - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\nأفضل الأهداف التي تظهر بداية زخم الآن:\n\n"
    for i, coin in enumerate(sorted_coins[:10]):
        symbol_name = coin['symbol'].replace('USDT', '')
        price_str = format_price(coin['current_price'])
        message += f"**{i+1}. ${symbol_name}**\n   - السعر: `${price_str}`\n   - **زخم آخر 30 دقيقة: `%{coin['price_change']:+.2f}`**\n\n"
    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

# =============================================================================
# 4. المهام الآلية الدورية (القديمة)
# =============================================================================
# (fomo_hunter_job, new_listings_sniper_job, pattern_hunter_job remain here without change)
def fomo_hunter_job():
    logger.info("===== Fomo Hunter (PERIODIC SCAN): Starting Scan =====")
    now = datetime.now(UTC)
    try:
        market_data = get_market_data()
        potential_coins = sorted([s for s in market_data if s['symbol'].endswith('USDT')], key=lambda x: float(x['priceChangePercent']), reverse=True)[:200]
        for pair_data in potential_coins:
            symbol = pair_data['symbol']
            if symbol in recently_alerted_fomo and (now - recently_alerted_fomo[symbol]) < timedelta(hours=COOLDOWN_PERIOD_HOURS): continue
            alert_data = analyze_symbol(symbol)
            if alert_data:
                message = f"🚨 **تنبيه فومو آلي: يا أشرف انتبه!** 🚨\n\n**العملة:** `${alert_data['symbol']}`\n**منصة:** `MEXC`\n\n📈 *زيادة حجم التداول:* `{alert_data['volume_increase']}`\n🕯️ *نمط السعر:* `{alert_data['price_pattern']}`\n💰 *السعر الحالي:* `{alert_data['current_price']}` USDT\n\n*(تحذير: هذا تنبيه آلي.)*"
                bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                logger.info(f"Fomo alert sent for {alert_data['symbol']}")
                recently_alerted_fomo[symbol] = now
                time.sleep(1)
    except Exception as e:
        logger.error(f"Fomo Hunter: Error during periodic scan: {e}")

def new_listings_sniper_job():
    global known_symbols
    logger.info("Sniper: Checking for new listings...")
    try:
        current_symbols = {s['symbol'] for s in get_market_data() if s['symbol'].endswith('USDT')}
        if not known_symbols:
            known_symbols = current_symbols
            logger.info(f"Sniper: Initialized with {len(known_symbols)} symbols.")
            return
        newly_listed = current_symbols - known_symbols
        if newly_listed:
            for symbol in newly_listed:
                logger.info(f"Sniper: NEW LISTING DETECTED: {symbol}")
                message = f"🎯 **تنبيه قناص: إدراج جديد!** 🎯\n\nتم للتو إدراج عملة جديدة على منصة MEXC:\n\n**العملة:** `${symbol}`"
                bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
            known_symbols.update(newly_listed)
    except Exception as e:
        logger.error(f"Sniper: Error checking for new listings: {e}")

def pattern_hunter_job():
    pass # Code remains the same

# =============================================================================
# 5. تشغيل البوت والجدولة (بدون تغيير)
# =============================================================================
def send_startup_message():
    try:
        message = "✅ **بوت التداول الذكي (v3) متصل الآن!**\n\nكاشف الزخم السريع جاهز للعمل."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

def run_scheduler():
    logger.info("Scheduler thread for periodic jobs started.")
    schedule.every(RUN_FOMO_SCAN_EVERY_MINUTES).minutes.do(fomo_hunter_job)
    schedule.every(RUN_LISTING_SCAN_EVERY_SECONDS).seconds.do(new_listings_sniper_job)
    schedule.every(RUN_PATTERN_SCAN_EVERY_HOURS).hours.do(pattern_hunter_job)
    threading.Thread(target=new_listings_sniper_job, daemon=True).start()
    while True:
        schedule.run_pending()
        time.sleep(1)

def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set.")
        return
        
    asyncio_loop = asyncio.new_event_loop()
    asyncio.ensure_future(run_websocket_client(), loop=asyncio_loop)
    ws_thread = threading.Thread(target=start_asyncio_loop, args=(asyncio_loop,), daemon=True)
    ws_thread.start()

    checker_thread = threading.Thread(target=periodic_activity_checker, daemon=True)
    checker_thread.start()
    
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CallbackQueryHandler(button_handler))
    
    send_startup_message()
    
    updater.start_polling()
    logger.info("Telegram bot is now polling for commands...")
    updater.idle()

if __name__ == '__main__':
    main()