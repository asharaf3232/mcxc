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

# --- إعدادات قناص الإدراجات ---
RUN_LISTING_SCAN_EVERY_SECONDS = 60

# --- إعدادات صياد الأنماط ---
RUN_PATTERN_SCAN_EVERY_HOURS = 1
PATTERN_SIGHTING_THRESHOLD = 3
PATTERN_LOOKBACK_DAYS = 7

# --- [جديد] إعدادات صياد الجواهر ---
GEM_MAX_PRICE = 0.10          # أقصى سعر للعملة لتعتبر "جوهرة"
GEM_MIN_VOLUME = 50000        # أقل حجم تداول مقبول
GEM_MAX_VOLUME = 2000000      # أقصى حجم تداول (لتجنب العملات المشهورة)

# --- إعدادات متقدمة ---
MEXC_API_BASE_URL = "https://api.mexc.com"
COOLDOWN_PERIOD_HOURS = 2
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- تهيئة البوت والمتغيرات العامة ---
bot = Bot(token=TELEGRAM_BOT_TOKEN)
recently_alerted_fomo = {}
known_symbols = set()
pattern_tracker = {}
recently_alerted_pattern = {}

# =============================================================================
# الوظائف التفاعلية (الأزرار والأوامر)
# =============================================================================
def build_menu():
    """Builds the main menu keyboard with the new Gem Hunter button."""
    keyboard = [
        [InlineKeyboardButton("📈 الأكثر ارتفاعاً", callback_data='top_gainers'),
         InlineKeyboardButton("📉 الأكثر انخفاضاً", callback_data='top_losers')],
        [InlineKeyboardButton("💰 الأعلى سيولة (عام)", callback_data='top_volume')],
        [InlineKeyboardButton("💎 تقرير صياد الجواهر", callback_data='gem_hunter_report')] # [تغيير]
    ]
    return InlineKeyboardMarkup(keyboard)

def start_command(update, context):
    welcome_message = "✅ **أهلاً بك في بوت التداول الذكي!**\n\n"
    welcome_message += "يقوم هذا البوت الآن بالمراقبة الآلية بحثاً عن:\n"
    welcome_message += "1- **تنبيهات الفومو** (ضخ سيولة وسعر).\n"
    welcome_message += "2- **الإدراجات الجديدة** (قنص فوري).\n"
    welcome_message += "3- **الأنماط المتكررة** (عملات ترتفع بشكل متكرر).\n\n"
    welcome_message += "اختر أحد الخيارات أدناه للتحليل الفوري:"
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def button_handler(update, context):
    """Handles all button presses."""
    query = update.callback_query
    query.answer()
    context.bot.send_message(chat_id=query.message.chat_id, text=f"🔍 جارِ تنفيذ طلبك...")
    
    # [تحديث] تغيير معالج الزر
    if query.data == 'top_gainers':
        get_top_10_gainers(context, query.message.chat_id)
    elif query.data == 'top_losers':
        get_top_10_losers(context, query.message.chat_id)
    elif query.data == 'top_volume':
        get_top_10_volume(context, query.message.chat_id)
    elif query.data == 'gem_hunter_report':
        send_gem_hunter_report(context, query.message.chat_id)

def get_market_data():
    """Helper function to get all market data from MEXC."""
    url = f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr"
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()

def format_price(price_str):
    """Helper to format price strings cleanly."""
    price_float = float(price_str)
    return f"{price_float:.8f}".rstrip('0').rstrip('.')

# [جديد] الدالة الخاصة بتقرير صياد الجواهر
def send_gem_hunter_report(context, chat_id):
    """Filters and sends a report of potential 'gem' coins."""
    try:
        data = get_market_data()
        
        potential_gems = []
        for pair in data:
            if not pair['symbol'].endswith('USDT'): continue

            try:
                price = float(pair['lastPrice'])
                volume = float(pair['quoteVolume'])
                change = float(pair['priceChangePercent']) * 100

                # تطبيق الفلاتر الذكية
                if (price <= GEM_MAX_PRICE and
                    GEM_MIN_VOLUME <= volume <= GEM_MAX_VOLUME and
                    change > 0):
                    potential_gems.append(pair)
            except (ValueError, TypeError):
                continue # تجاهل أي عملة ببيانات غير صالحة

        if not potential_gems:
            context.bot.send_message(chat_id=chat_id, text="💎 لم يتم العثور على عملات تطابق معايير 'الجواهر' حالياً. حاول مرة أخرى لاحقاً.")
            return

        # فرز الجواهر المحتملة حسب نسبة الارتفاع
        sorted_gems = sorted(potential_gems, key=lambda x: float(x['priceChangePercent']), reverse=True)
        
        message = f"💎 **تقرير صياد الجواهر - {datetime.now().strftime('%d.%m')}** 💎\n\n"
        message += "قائمة عملات واعدة ذات سعر منخفض وحجم تداول متزايد:\n\n"
        
        for i, pair in enumerate(sorted_gems[:10]):
            symbol = pair['symbol'].replace('USDT', '')
            price = format_price(pair['lastPrice'])
            change_percent = float(pair['priceChangePercent']) * 100
            volume = float(pair['quoteVolume'])
            
            message += f"**{i+1}. ${symbol}**\n"
            message += f"   - السعر: `${price}`\n"
            message += f"   - الارتفاع: `%{change_percent:+.2f}`\n"
            message += f"   - السيولة: `${volume:,.0f}`\n\n"
            
        context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in send_gem_hunter_report: {e}")
        context.bot.send_message(chat_id=chat_id, text="حدث خطأ أثناء البحث عن الجواهر.")

# ... (بقية دوال الأزرار تبقى كما هي)
def get_top_10_gainers(context, chat_id):
    try:
        data = get_market_data(); usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['priceChangePercent_float'] = float(pair['priceChangePercent']) * 100
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=True)
        message = "🔥 **أكثر 10 عملات ارتفاعاً في آخر 24 ساعة** 🔥\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            message += f"{i+1}. **${pair['symbol'].replace('USDT', '')}**\n   - نسبة الارتفاع: `%{pair['priceChangePercent_float']:+.2f}`\n   - السعر الحالي: `${format_price(pair['lastPrice'])}`\n\n"
        context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Error in get_top_10_gainers: {e}"); context.bot.send_message(chat_id=chat_id, text="حدث خطأ أثناء جلب البيانات.")
def get_top_10_losers(context, chat_id):
    try:
        data = get_market_data(); usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['priceChangePercent_float'] = float(pair['priceChangePercent']) * 100
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=False)
        message = "📉 **أكثر 10 عملات انخفاضاً في آخر 24 ساعة** 📉\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            message += f"{i+1}. **${pair['symbol'].replace('USDT', '')}**\n   - نسبة الانخفاض: `%{pair['priceChangePercent_float']:+.2f}`\n   - السعر الحالي: `${format_price(pair['lastPrice'])}`\n\n"
        context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Error in get_top_10_losers: {e}"); context.bot.send_message(chat_id=chat_id, text="حدث خطأ أثناء جلب البيانات.")
def get_top_10_volume(context, chat_id):
    try:
        data = get_market_data(); usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['quoteVolume_float'] = float(pair['quoteVolume'])
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['quoteVolume_float'], reverse=True)
        message = "💰 **أكثر 10 عملات سيولة (فوليوم) في آخر 24 ساعة** 💰\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            message += f"{i+1}. **${pair['symbol'].replace('USDT', '')}**\n   - حجم التداول: `${pair['quoteVolume_float']:,.0f}`\n   - السعر الحالي: `${format_price(pair['lastPrice'])}`\n\n"
        context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e: logger.error(f"Error in get_top_10_volume: {e}"); context.bot.send_message(chat_id=chat_id, text="حدث خطأ أثناء جلب البيانات.")

# =============================================================================
# المهام الآلية (تعمل في الخلفية - لا تغيير هنا)
# =============================================================================
def pattern_hunter_job():
    global pattern_tracker, recently_alerted_pattern; logger.info("Pattern Hunter: Starting scan...")
    now = datetime.now(UTC)
    for symbol in list(pattern_tracker.keys()):
        if now - pattern_tracker[symbol]['last_seen'] > timedelta(days=PATTERN_LOOKBACK_DAYS): del pattern_tracker[symbol]
    for symbol in list(recently_alerted_pattern.keys()):
        if now - recently_alerted_pattern[symbol] > timedelta(days=PATTERN_LOOKBACK_DAYS): del recently_alerted_pattern[symbol]
    try:
        data = get_market_data(); usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['priceChangePercent_float'] = float(pair['priceChangePercent']) * 100
        top_gainers = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=True)[:30]
        for coin in top_gainers:
            symbol = coin['symbol']
            if symbol in pattern_tracker:
                if now.date() > pattern_tracker[symbol]['last_seen'].date():
                    pattern_tracker[symbol]['count'] += 1; pattern_tracker[symbol]['last_seen'] = now
            else: pattern_tracker[symbol] = {'count': 1, 'last_seen': now}
        for symbol, data in pattern_tracker.items():
            if data['count'] >= PATTERN_SIGHTING_THRESHOLD and symbol not in recently_alerted_pattern:
                logger.info(f"Pattern Hunter: PATTERN DETECTED for {symbol}!")
                message = f"🧠 **تنبيه صياد الأنماط: تم رصد سلوك متكرر!** 🧠\n\nالعملة **${symbol.replace('USDT', '')}** ظهرت في قائمة الأعلى ارتفاعاً **{data['count']} مرات** خلال الأيام القليلة الماضية.\n\nقد يشير هذا إلى اهتمام مستمر وقوة شرائية متواصلة.\n\n*(هذا ليس تنبيه فومو لحظي، بل ملاحظة لنمط متكرر.)*"
                bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                recently_alerted_pattern[symbol] = now
    except Exception as e: logger.error(f"Pattern Hunter: Error during scan: {e}")
def new_listings_sniper_job():
    global known_symbols; logger.info("Sniper: Checking for new listings...")
    try:
        url = f"{MEXC_API_BASE_URL}/api/v3/exchangeInfo"; headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=15); response.raise_for_status()
        current_symbols = {s['symbol'] for s in response.json()['symbols'] if s['symbol'].endswith('USDT') and s['status'] == 'ENABLED'}
        if not known_symbols: known_symbols = current_symbols; logger.info(f"Sniper: Initialized with {len(known_symbols)} symbols."); return
        newly_listed = current_symbols - known_symbols
        if newly_listed:
            for symbol in newly_listed:
                logger.info(f"Sniper: NEW LISTING DETECTED: {symbol}")
                message = f"🎯 **تنبيه قناص: إدراج جديد!** 🎯\n\nتم للتو إدراج عملة جديدة على منصة MEXC:\n\n**العملة:** `${symbol}`\n\n*(مخاطر عالية! قم ببحثك بسرعة فائقة.)*"
                bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
            known_symbols.update(newly_listed)
    except Exception as e: logger.error(f"Sniper: Error checking for new listings: {e}")
def fomo_hunter_job():
    logger.info("===== Fomo Hunter: Starting Scan ====="); now = datetime.now(UTC)
    for symbol, timestamp in list(recently_alerted_fomo.items()):
        if now - timestamp > timedelta(hours=COOLDOWN_PERIOD_HOURS): del recently_alerted_fomo[symbol]
    symbols_to_check = [s['symbol'] for s in get_market_data() if s['symbol'].endswith('USDT')]
    if not symbols_to_check: return
    for i, symbol in enumerate(symbols_to_check):
        if (i + 1) % 300 == 0: logger.info(f"Fomo scan progress: {i+1}/{len(symbols_to_check)}")
        if symbol in recently_alerted_fomo: continue
        alert_data = analyze_symbol(symbol)
        if alert_data: 
            message = f"🚨 *تنبيه فومو محتمل!* 🚨\n\n*العملة:* `${alert_data['symbol']}`\n*منصة:* `MEXC`\n\n📈 *زيادة حجم التداول:* `{alert_data['volume_increase']}`\n🕯️ *نمط السعر:* `{alert_data['price_pattern']}`\n💰 *السعر الحالي:* `{alert_data['current_price']}` USDT\n\n*(تحذير: هذا تنبيه آلي.)*"
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
            logger.info(f"Fomo alert sent for {alert_data['symbol']}"); recently_alerted_fomo[symbol] = now; time.sleep(1)
    logger.info("===== Fomo Hunter: Scan Finished =====")
def analyze_symbol(symbol): # Helper for fomo_hunter_job
    try:
        klines_url = f"{MEXC_API_BASE_URL}/api/v3/klines"; headers = {'User-Agent': 'Mozilla/5.0'}
        daily_params = {'symbol': symbol, 'interval': '1d', 'limit': 2}; daily_res = requests.get(klines_url, params=daily_params, headers=headers, timeout=10); daily_res.raise_for_status(); daily_data = daily_res.json()
        if len(daily_data) < 2: return None
        previous_day_volume, current_day_volume = float(daily_data[0][7]), float(daily_data[1][7])
        if current_day_volume < MIN_USDT_VOLUME: return None
        if not current_day_volume > (previous_day_volume * VOLUME_SPIKE_MULTIPLIER): return None
        hourly_params = {'symbol': symbol, 'interval': '1h', 'limit': 4}; hourly_res = requests.get(klines_url, params=hourly_params, headers=headers, timeout=10); hourly_res.raise_for_status(); hourly_data = hourly_res.json()
        if len(hourly_data) < 4: return None
        initial_price, latest_high_price = float(hourly_data[0][1]), float(hourly_data[-1][2])
        if initial_price == 0: return None
        price_increase_percent = ((latest_high_price - initial_price) / initial_price) * 100
        if not price_increase_percent >= PRICE_VELOCITY_THRESHOLD: return None
        ticker_url = f"{MEXC_API_BASE_URL}/api/v3/ticker/price"; price_res = requests.get(ticker_url, params={'symbol': symbol}, headers=headers, timeout=10); price_res.raise_for_status(); current_price = float(price_res.json()['price'])
        volume_increase_percent = ((current_day_volume - previous_day_volume) / previous_day_volume) * 100 if previous_day_volume > 0 else float('inf')
        return {'symbol': symbol, 'volume_increase': f"+{volume_increase_percent:,.2f}%", 'price_pattern': f"صعود بنسبة +{price_increase_percent:,.2f}% في آخر 4 ساعات", 'current_price': format_price(current_price)}
    except Exception: return None

# =============================================================================
# تشغيل البوت والجدولة
# =============================================================================
def send_startup_message():
    try:
        message = "✅ **بوت التداول الذكي متصل الآن!**\n\nأرسل /start لعرض قائمة التحليل الفوري."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')
        logger.info("تم إرسال رسالة بدء التشغيل بنجاح.")
    except Exception as e: logger.error(f"Failed to send startup message: {e}")

def run_scheduler():
    logger.info("Scheduler thread started.")
    schedule.every(RUN_FOMO_SCAN_EVERY_MINUTES).minutes.do(fomo_hunter_job)
    schedule.every(RUN_LISTING_SCAN_EVERY_SECONDS).seconds.do(new_listings_sniper_job)
    schedule.every(RUN_PATTERN_SCAN_EVERY_HOURS).hours.do(pattern_hunter_job)
    threading.Thread(target=fomo_hunter_job).start()
    threading.Thread(target=new_listings_sniper_job).start()
    threading.Thread(target=pattern_hunter_job).start()
    while True: schedule.run_pending(); time.sleep(1)

def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.error("FATAL ERROR: Bot token or chat ID are not set."); return
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CallbackQueryHandler(button_handler))
    send_startup_message()
    scheduler_thread = threading.Thread(target=run_scheduler); scheduler_thread.daemon = True; scheduler_thread.start()
    updater.start_polling(); logger.info("Bot is now polling for commands and button clicks..."); updater.idle()

if __name__ == '__main__':
    main()