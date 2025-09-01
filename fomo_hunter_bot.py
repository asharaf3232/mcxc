# -*- coding: utf-8 -*-
import os
import requests
import time
import schedule
import logging
import threading
from telegram import Bot, ParseMode, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler
from datetime import datetime, timedelta, UTC

# --- الإعدادات الرئيسية ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# --- معايير تحليل الفومو ---
VOLUME_SPIKE_MULTIPLIER = 10
MIN_USDT_VOLUME = 500000
PRICE_VELOCITY_THRESHOLD = 30.0 
RUN_FOMO_SCAN_EVERY_MINUTES = 15

# --- [جديد] إعدادات قناص الإدراجات ---
RUN_LISTING_SCAN_EVERY_SECONDS = 60 # فحص الإدراجات الجديدة كل 60 ثانية

# --- إعدادات متقدمة ---
MEXC_API_BASE_URL = "https://api.mexc.com"
COOLDOWN_PERIOD_HOURS = 2
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- تهيئة البوت والمتغيرات العامة ---
bot = Bot(token=TELEGRAM_BOT_TOKEN)
recently_alerted_fomo = {}
# [جديد] ذاكرة قناص الإدراجات لتخزين العملات الحالية
known_symbols = set() 

# =============================================================================
# الوظائف التفاعلية (الأزرار والأوامر) - لا تغيير هنا
# =============================================================================
def build_menu():
    keyboard = [
        [InlineKeyboardButton("📈 الأكثر ارتفاعاً", callback_data='top_gainers')],
        [InlineKeyboardButton("📉 الأكثر انخفاضاً", callback_data='top_losers')],
        [InlineKeyboardButton("💰 الأعلى سيولة (فوليوم)", callback_data='top_volume')],
    ]
    return InlineKeyboardMarkup(keyboard)

def start_command(update, context):
    welcome_message = "✅ **أهلاً بك في بوت القنص المتطور!**\n\n"
    welcome_message += "يقوم هذا البوت الآن بمراقبة السوق بشكل آلي بحثاً عن:\n"
    welcome_message += "1- **تنبيهات الفومو** (ارتفاع حجم التداول والسعر).\n"
    welcome_message += "2- **الإدراجات الجديدة** (قنص العملات لحظة إضافتها).\n\n"
    welcome_message += "اختر أحد الخيارات من القائمة أدناه للتحليل الفوري:"
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

# ... (بقية دوال الأزرار تبقى كما هي بدون تغيير)
def button_handler(update, context):
    query = update.callback_query
    query.answer()
    context.bot.send_message(chat_id=query.message.chat_id, text=f"🔍 جارِ تنفيذ طلبك...")
    if query.data == 'top_gainers': get_top_10_gainers(context, query.message.chat_id)
    elif query.data == 'top_losers': get_top_10_losers(context, query.message.chat_id)
    elif query.data == 'top_volume': get_top_10_volume(context, query.message.chat_id)

def get_market_data():
    url = f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr"
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()

def get_top_10_gainers(context, chat_id):
    try:
        data = get_market_data()
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['priceChangePercent_float'] = float(pair['priceChangePercent']) * 100
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=True)
        message = "🔥 **أكثر 10 عملات ارتفاعاً في آخر 24 ساعة** 🔥\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            symbol, change, price = pair['symbol'].replace('USDT', ''), pair['priceChangePercent_float'], f"{float(pair['lastPrice']):.8f}".rstrip('0').rstrip('.')
            message += f"{i+1}. **${symbol}**\n   - نسبة الارتفاع: `%{change:+.2f}`\n   - السعر الحالي: `${price}`\n\n"
        context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_top_10_gainers: {e}")
        context.bot.send_message(chat_id=chat_id, text="حدث خطأ أثناء جلب البيانات.")

def get_top_10_losers(context, chat_id):
    try:
        data = get_market_data()
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['priceChangePercent_float'] = float(pair['priceChangePercent']) * 100
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=False)
        message = "📉 **أكثر 10 عملات انخفاضاً في آخر 24 ساعة** 📉\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            symbol, change, price = pair['symbol'].replace('USDT', ''), pair['priceChangePercent_float'], f"{float(pair['lastPrice']):.8f}".rstrip('0').rstrip('.')
            message += f"{i+1}. **${symbol}**\n   - نسبة الانخفاض: `%{change:+.2f}`\n   - السعر الحالي: `${price}`\n\n"
        context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_top_10_losers: {e}")
        context.bot.send_message(chat_id=chat_id, text="حدث خطأ أثناء جلب البيانات.")

def get_top_10_volume(context, chat_id):
    try:
        data = get_market_data()
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['quoteVolume_float'] = float(pair['quoteVolume'])
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['quoteVolume_float'], reverse=True)
        message = "💰 **أكثر 10 عملات سيولة (فوليوم) في آخر 24 ساعة** 💰\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            symbol, volume, price = pair['symbol'].replace('USDT', ''), pair['quoteVolume_float'], f"{float(pair['lastPrice']):.8f}".rstrip('0').rstrip('.')
            message += f"{i+1}. **${symbol}**\n   - حجم التداول: `${volume:,.0f}`\n   - السعر الحالي: `${price}`\n\n"
        context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_top_10_volume: {e}")
        context.bot.send_message(chat_id=chat_id, text="حدث خطأ أثناء جلب البيانات.")

# =============================================================================
# [جديد] وظائف قناص الإدراجات الجديدة
# =============================================================================
def new_listings_sniper_job():
    """
    Checks for newly listed USDT pairs on MEXC.
    This function runs frequently (e.g., every minute).
    """
    global known_symbols
    logger.info("Sniper: Checking for new listings...")
    
    try:
        # We use exchangeInfo as it's the most reliable source for the full list
        url = f"{MEXC_API_BASE_URL}/api/v3/exchangeInfo"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        current_symbols = {s['symbol'] for s in data['symbols'] if s['symbol'].endswith('USDT') and s['status'] == 'ENABLED'}
        
        # If this is the first run, just populate the known symbols and exit
        if not known_symbols:
            known_symbols = current_symbols
            logger.info(f"Sniper: Initialized with {len(known_symbols)} symbols.")
            return

        # Find the difference between the new list and the old list
        newly_listed = current_symbols - known_symbols
        
        if newly_listed:
            for symbol in newly_listed:
                logger.info(f"Sniper: NEW LISTING DETECTED: {symbol}")
                message = f"🎯 **تنبيه قناص: إدراج جديد!** 🎯\n\n"
                message += f"تم للتو إدراج عملة جديدة على منصة MEXC:\n\n"
                message += f"**العملة:** `${symbol}`\n\n"
                message += f"*(مخاطر عالية! قم ببحثك بسرعة فائقة.)*"
                bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
            
            # Update the known symbols list with the new findings
            known_symbols.update(newly_listed)

    except Exception as e:
        logger.error(f"Sniper: Error checking for new listings: {e}")


# =============================================================================
# وظائف صياد الفومو (تعمل في الخلفية) - لا تغيير هنا
# =============================================================================
def send_startup_message():
    try:
        message = "✅ **بوت القنص المتطور متصل الآن!**\n\nأرسل /start لعرض قائمة التحليل الفوري."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')
        logger.info("تم إرسال رسالة بدء التشغيل بنجاح.")
    except Exception as e: logger.error(f"Failed to send startup message: {e}")

def get_usdt_pairs_for_fomo():
    try:
        usdt_pairs = [s['symbol'] for s in get_market_data() if s['symbol'].endswith('USDT')]
        logger.info(f"Fomo Hunter: Found {len(usdt_pairs)} USDT pairs.")
        return usdt_pairs
    except Exception as e:
        logger.error(f"Fomo Hunter: Failed to get pairs: {e}")
        return []

def analyze_symbol(symbol):
    try:
        klines_url = f"{MEXC_API_BASE_URL}/api/v3/klines"; headers = {'User-Agent': 'Mozilla/5.0'}
        daily_params = {'symbol': symbol, 'interval': '1d', 'limit': 2}
        daily_res = requests.get(klines_url, params=daily_params, headers=headers, timeout=10); daily_res.raise_for_status()
        daily_data = daily_res.json()
        if len(daily_data) < 2: return None
        previous_day_volume, current_day_volume = float(daily_data[0][7]), float(daily_data[1][7])
        if current_day_volume < MIN_USDT_VOLUME: return None
        if not current_day_volume > (previous_day_volume * VOLUME_SPIKE_MULTIPLIER): return None
        hourly_params = {'symbol': symbol, 'interval': '1h', 'limit': 4}
        hourly_res = requests.get(klines_url, params=hourly_params, headers=headers, timeout=10); hourly_res.raise_for_status()
        hourly_data = hourly_res.json()
        if len(hourly_data) < 4: return None
        initial_price, latest_high_price = float(hourly_data[0][1]), float(hourly_data[-1][2])
        if initial_price == 0: return None
        price_increase_percent = ((latest_high_price - initial_price) / initial_price) * 100
        if not price_increase_percent >= PRICE_VELOCITY_THRESHOLD: return None
        ticker_url = f"{MEXC_API_BASE_URL}/api/v3/ticker/price"
        price_res = requests.get(ticker_url, params={'symbol': symbol}, headers=headers, timeout=10); price_res.raise_for_status()
        current_price = float(price_res.json()['price'])
        volume_increase_percent = ((current_day_volume - previous_day_volume) / previous_day_volume) * 100 if previous_day_volume > 0 else float('inf')
        return {'symbol': symbol, 'volume_increase': f"+{volume_increase_percent:,.2f}%", 'price_pattern': f"صعود بنسبة +{price_increase_percent:,.2f}% في آخر 4 ساعات", 'current_price': f"{current_price:.8f}".rstrip('0').rstrip('.')}
    except Exception: return None

def send_fomo_alert(alert_data):
    message = f"🚨 *تنبيه فومو محتمل!* 🚨\n\n*العملة:* `${alert_data['symbol']}`\n*منصة:* `MEXC`\n\n📈 *زيادة حجم التداول:* `{alert_data['volume_increase']}`\n🕯️ *نمط السعر:* `{alert_data['price_pattern']}`\n💰 *السعر الحالي:* `{alert_data['current_price']}` USDT\n\n*(تحذير: هذا تنبيه آلي.)*"
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN); logger.info(f"Fomo alert sent for {alert_data['symbol']}")

def fomo_hunter_job():
    logger.info("===== بدء جولة فحص فومو جديدة =====")
    now = datetime.now(UTC)
    for symbol, timestamp in list(recently_alerted_fomo.items()):
        if now - timestamp > timedelta(hours=COOLDOWN_PERIOD_HOURS): del recently_alerted_fomo[symbol]
    symbols_to_check = get_usdt_pairs_for_fomo()
    if not symbols_to_check: return
    for i, symbol in enumerate(symbols_to_check):
        if (i + 1) % 200 == 0: logger.info(f"Fomo scan progress: {i+1}/{len(symbols_to_check)}")
        if symbol in recently_alerted_fomo: continue
        alert_data = analyze_symbol(symbol)
        if alert_data: send_fomo_alert(alert_data); recently_alerted_fomo[symbol] = datetime.now(UTC); time.sleep(1)
    logger.info("===== انتهاء جولة فحص فومو =====")

# =============================================================================
# تشغيل البوت والجدولة
# =============================================================================
def run_scheduler():
    logger.info("Scheduler thread started.")
    # جدولة المهام المختلفة
    schedule.every(RUN_FOMO_SCAN_EVERY_MINUTES).minutes.do(fomo_hunter_job)
    schedule.every(RUN_LISTING_SCAN_EVERY_SECONDS).seconds.do(new_listings_sniper_job)

    # تشغيل المهام لأول مرة فوراً
    fomo_hunter_job()
    new_listings_sniper_job()
    
    while True:
        schedule.run_pending()
        time.sleep(1)

def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.error("FATAL ERROR: Bot token or chat ID are not set.")
        return

    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CallbackQueryHandler(button_handler))

    send_startup_message()

    scheduler_thread = threading.Thread(target=run_scheduler)
    scheduler_thread.daemon = True
    scheduler_thread.start()
    
    updater.start_polling()
    logger.info("Bot is now polling for commands and button clicks...")
    updater.idle()

if __name__ == '__main__':
    main()