# -*- coding: utf--8 -*-
import os
import requests
import time
import schedule
import logging
import threading
import asyncio
import json
import aiohttp
from telegram import Bot, ParseMode, ReplyKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
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

# --- إعدادات كاشف الزخم (اليدوي) ---
MOMENTUM_MAX_PRICE = 0.10
MOMENTUM_MIN_VOLUME_24H = 50000
MOMENTUM_MAX_VOLUME_24H = 2000000
MOMENTUM_VOLUME_INCREASE = 1.8
MOMENTUM_PRICE_INCREASE = 4.0

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
active_hunts = {} # <-- لإدارة الصفقات النشطة

# =============================================================================
# 1. قسم الرصد اللحظي (WebSocket)
# =============================================================================
async def handle_websocket_message(message):
    try:
        data = json.loads(message)
        if 'd' in data and 'e' in data['d'] and data['d']['e'] == 'spot@public.deals.v3.api':
            for deal in data['d']['D']:
                if deal['S'] == 1:
                    symbol = data['s']
                    logger.info(f"WebSocket: Buy trade received for {symbol}") 
                    volume_usdt = float(deal['p']) * float(deal['q'])
                    timestamp = float(deal['t']) / 1000.0
                    with activity_lock:
                        if symbol not in activity_tracker:
                            activity_tracker[symbol] = deque(maxlen=200)
                        activity_tracker[symbol].append({'v': volume_usdt, 't': timestamp})
    except Exception:
        pass

def send_instant_alert(symbol, total_volume, trade_count):
    # ... الكود هنا ...
    pass # (الكود لم يتغير)

def periodic_activity_checker():
    # ... الكود هنا ...
    pass # (الكود لم يتغير)

async def run_websocket_client():
    # ... الكود هنا ...
    pass # (الكود لم يتغير)

def start_asyncio_loop(loop):
    # ... الكود هنا ...
    pass # (الكود لم يتغير)

# =============================================================================
# 2. الوظائف العامة والأساسية
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
    try:
        return f"{float(price_str):.8f}".rstrip('0').rstrip('.')
    except (ValueError, TypeError):
        return price_str

def analyze_symbol(symbol):
    # (هذه الدالة تبقى كما هي لتخدم fomo_hunter_job)
    try:
        klines_url = f"{MEXC_API_BASE_URL}/api/v3/klines"
        headers = {'User-Agent': 'Mozilla/5.0'}
        daily_params = {'symbol': symbol, 'interval': '1d', 'limit': 2}
        daily_res = requests.get(klines_url, params=daily_params, headers=headers, timeout=10)
        daily_res.raise_for_status()
        daily_data = daily_res.json()
        if len(daily_data) < 2: return None
        
        previous_day_volume = float(daily_data[0][7])
        current_day_volume = float(daily_data[1][7])

        if current_day_volume < MIN_USDT_VOLUME: return None
        is_volume_spike = current_day_volume > (previous_day_volume * VOLUME_SPIKE_MULTIPLIER)
        if not is_volume_spike: return None

        hourly_params = {'symbol': symbol, 'interval': '1h', 'limit': 4}
        hourly_res = requests.get(klines_url, params=hourly_params, headers=headers, timeout=10)
        hourly_res.raise_for_status()
        hourly_data = hourly_res.json()
        if len(hourly_data) < 4: return None

        initial_price = float(hourly_data[0][1])
        latest_high_price = float(hourly_data[-1][2])
        if initial_price == 0: return None
        
        price_increase_percent = ((latest_high_price - initial_price) / initial_price) * 100
        is_strong_pump = price_increase_percent >= PRICE_VELOCITY_THRESHOLD
        if not is_strong_pump: return None

        ticker_url = f"{MEXC_API_BASE_URL}/api/v3/ticker/price"
        price_res = requests.get(ticker_url, params={'symbol': symbol}, headers=headers, timeout=10)
        price_res.raise_for_status()
        current_price = float(price_res.json()['price'])
        
        volume_increase_percent = ((current_day_volume - previous_day_volume) / previous_day_volume) * 100 if previous_day_volume > 0 else float('inf')
        
        # إضافة حجم الشمعة الأخيرة للبيانات المرجعة
        last_candle_vol_res = requests.get(klines_url, params={'symbol': symbol, 'interval': '1h', 'limit': 1}, headers=headers, timeout=10)
        last_candle_vol_res.raise_for_status()
        peak_volume = float(last_candle_vol_res.json()[0][5])

        return {
            'symbol': symbol,
            'volume_increase': f"+{volume_increase_percent:,.2f}%",
            'price_pattern': f"صعود بنسبة +{price_increase_percent:,.2f}% في آخر 4 ساعات",
            'current_price': format_price(current_price),
            'peak_volume': peak_volume
        }
    except Exception:
        return None

# =============================================================================
# 3. الوظائف التفاعلية
# =============================================================================
BTN_MOMENTUM = "🚀 كاشف الزخم (فائق السرعة)"
BTN_GAINERS = "📈 الأكثر ارتفاعاً"
BTN_LOSERS = "📉 الأكثر انخفاضاً"
BTN_VOLUME = "💰 الأعلى سيولة"

def build_menu():
    keyboard = [[BTN_MOMENTUM], [BTN_GAINERS, BTN_LOSERS], [BTN_VOLUME]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def start_command(update, context):
    welcome_message = "✅ **بوت التداول الذكي (v5) جاهز!**\n\n- **الراصد اللحظي** يعمل الآن ويمكنك التحقق من حالته عبر الأمر /status.\n- **مساعد إدارة الصفقات** فعال الآن.\n- استخدم لوحة الأزرار أدناه للتحليل الفوري."
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def status_command(update, context):
    with activity_lock:
        tracked_symbols_count = len(activity_tracker)
    active_hunts_count = len(active_hunts)
    
    message = "📊 **حالة البوت** 📊\n\n"
    message += f"**1. الراصد اللحظي (WebSocket):**\n"
    if tracked_symbols_count > 0:
        message += f"   - ✅ نشط، يتم تتبع **{tracked_symbols_count}** عملة.\n"
    else:
        message += f"   - ⚠️ متصل، السوق هادئ لحظياً.\n"
    message += f"\n**2. مساعد إدارة الصفقات:**\n"
    if active_hunts_count > 0:
        message += f"   - ✅ نشط، يراقب **{active_hunts_count}** فرصة حالياً."
    else:
        message += f"   -  standby, ينتظر فرصة جديدة لمراقبتها."
        
    update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update, context):
    button_text = update.message.text
    chat_id = update.message.chat_id
    sent_message = context.bot.send_message(chat_id=chat_id, text="🔍 جارِ تنفيذ طلبك...")

    if button_text == BTN_GAINERS: get_top_10_list(context, chat_id, 'gainers', sent_message.message_id)
    elif button_text == BTN_LOSERS: get_top_10_list(context, chat_id, 'losers', sent_message.message_id)
    elif button_text == BTN_VOLUME: get_top_10_list(context, chat_id, 'volume', sent_message.message_id)
    elif button_text == BTN_MOMENTUM:
        threading.Thread(target=lambda: asyncio.run(run_momentum_detector_async(context, chat_id, sent_message.message_id))).start()

def get_top_10_list(context, chat_id, list_type, message_id):
    # ... الكود هنا لم يتغير ...
    pass

async def fetch_kline_data(session, symbol):
    # ... الكود هنا لم يتغير ...
    pass

async def run_momentum_detector_async(context, chat_id, message_id):
    # ... (الكود هنا لم يتغير حتى نهاية الدالة)
    
    # ===== التعديل الأخير: إضافة النتائج للمراقبة النشطة =====
    logger.info(f"Adding {len(sorted_coins)} manually detected coin(s) to the active hunt monitor.")
    now = datetime.now(UTC)
    for coin in sorted_coins[:10]:
        symbol = coin['symbol']
        if symbol not in active_hunts:
             active_hunts[symbol] = {
                'alert_price': float(coin['current_price']),
                'peak_volume': coin.get('peak_volume', 0), # استخدام .get للأمان
                'alert_time': now
            }
             logger.info(f"MONITORING STARTED for (manual) {symbol}")
    # ========================================================

# =============================================================================
# 4. المهام الآلية الدورية (مع إضافة المساعد الجديد)
# =============================================================================
def fomo_hunter_job():
    logger.info("===== Fomo Hunter (FAST SCAN): Starting Scan =====")
    now = datetime.now(UTC)
    # ... (بقية الكود لم يتغير، لكن تأكد من أنه يضيف لـ active_hunts كما فعلنا سابقاً)
    # The important part is adding to active_hunts after an alert
    alert_data = analyze_symbol(symbol)
    if alert_data:
        # (send alert code...)
        if symbol not in active_hunts:
            active_hunts[symbol] = {
                'alert_price': float(alert_data['current_price']),
                'peak_volume': alert_data['peak_volume'],
                'alert_time': now
            }
            logger.info(f"MONITORING STARTED for {symbol}")

# ----- الدالة الجديدة بالكامل -----
def monitor_active_hunts_job():
    logger.info(f"Active Hunts Monitor: Checking {len(active_hunts)} active hunt(s)...")
    now = datetime.now(UTC)
    for symbol in list(active_hunts.keys()):
        if now - active_hunts[symbol]['alert_time'] > timedelta(hours=2):
            del active_hunts[symbol]
            logger.info(f"MONITORING STOPPED for {symbol} (timeout).")
            continue
        try:
            klines_url = f"{MEXC_API_BASE_URL}/api/v3/klines"
            params = {'symbol': symbol, 'interval': '5m', 'limit': 2}
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(klines_url, params=params, headers=headers, timeout=10)
            res.raise_for_status()
            klines = res.json()
            if len(klines) < 2: continue
            
            last_candle = klines[-1]
            open_price, close_price, volume = float(last_candle[1]), float(last_candle[4]), float(last_candle[5])
            weakness_reason = None
            
            if close_price < open_price:
                price_drop_percent = ((close_price - open_price) / open_price) * 100
                if price_drop_percent <= -3.0:
                    weakness_reason = "شمعة حمراء قوية (5 دقائق)"
            
            peak_volume = active_hunts[symbol]['peak_volume']
            if peak_volume > 0 and volume < (peak_volume * 0.1):
                if weakness_reason is None:
                    weakness_reason = "انخفاض حاد في السيولة"
            
            if weakness_reason:
                send_weakness_alert(symbol, weakness_reason, close_price)
                del active_hunts[symbol]
        except Exception as e:
            logger.error(f"Error monitoring {symbol}: {e}")

def send_weakness_alert(symbol, reason, current_price):
    message = f"⚠️ **تحذير: الزخم في ${symbol.replace('USDT', '')} بدأ يضعف!** ⚠️\n\n- **تم رصد:** `{reason}`\n- **السعر الحالي:** `${format_price(current_price)}`\n\nقد يكون هذا مؤشراً على بداية انعكاس الاتجاه."
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
    logger.info(f"WEAKNESS ALERT sent for {symbol}. Reason: {reason}")
# ------------------------------------

def new_listings_sniper_job():
    # ... الكود لم يتغير ...
    pass
def pattern_hunter_job():
    # ... الكود لم يتغير ...
    pass

# =============================================================================
# 5. تشغيل البوت والجدولة (مع إضافة المهمة الجديدة)
# =============================================================================
def send_startup_message():
    # ... الكود لم يتغير ...
    pass

def run_scheduler():
    logger.info("Scheduler thread for periodic jobs started.")
    schedule.every(RUN_FOMO_SCAN_EVERY_MINUTES).minutes.do(fomo_hunter_job)
    schedule.every(RUN_LISTING_SCAN_EVERY_SECONDS).seconds.do(new_listings_sniper_job)
    schedule.every(RUN_PATTERN_SCAN_EVERY_HOURS).hours.do(pattern_hunter_job)
    
    # ===== المهمة الجديدة لمراقبة الصفقات النشطة =====
    schedule.every(1).minutes.do(monitor_active_hunts_job)
    # ===============================================
    
    # ... (بقية الكود لم يتغير)
    while True:
        schedule.run_pending(); time.sleep(1)

def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set."); return
        
    # --- تشغيل خيوط الخلفية ---
    # (هذا القسم لم يتغير)

    # --- إعداد معالجات التليجرام ---
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    
    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CommandHandler("status", status_command)) # <-- إضافة أمر الحالة
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_button_press))
    
    send_startup_message()
    
    # ... (بقية الكود لم يتغير)
    updater.idle()

if __name__ == '__main__':
    main()
