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

# --- معايير التحليل ---
VOLUME_SPIKE_MULTIPLIER = 10
MIN_USDT_VOLUME = 500000
PRICE_VELOCITY_THRESHOLD = 30.0 
RUN_EVERY_MINUTES = 15

# --- إعدادات متقدمة ---
MEXC_API_BASE_URL = "https://api.mexc.com"
COOLDOWN_PERIOD_HOURS = 2
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- تهيئة البوت ---
bot = Bot(token=TELEGRAM_BOT_TOKEN)
recently_alerted = {}

# =============================================================================
# الوظائف التفاعلية (الأزرار والأوامر)
# =============================================================================

def build_menu():
    """Builds the main menu keyboard."""
    keyboard = [
        [InlineKeyboardButton("📈 الأكثر ارتفاعاً", callback_data='top_gainers')],
        [InlineKeyboardButton("📉 الأكثر انخفاضاً", callback_data='top_losers')],
        [InlineKeyboardButton("💰 الأعلى سيولة (فوليوم)", callback_data='top_volume')],
    ]
    return InlineKeyboardMarkup(keyboard)

def start_command(update, context):
    """Handler for /start command, shows the main menu."""
    welcome_message = "✅ **أهلاً بك في بوت صياد الفومو!**\n\nاختر أحد الخيارات من القائمة أدناه:"
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def button_handler(update, context):
    """Handles all button presses."""
    query = update.callback_query
    query.answer() # Acknowledge the button press

    # Show a "loading" message
    context.bot.send_message(chat_id=query.message.chat_id, text=f"🔍 جارِ تنفيذ طلبك...")

    if query.data == 'top_gainers':
        get_top_10_gainers(context, query.message.chat_id)
    elif query.data == 'top_losers':
        get_top_10_losers(context, query.message.chat_id)
    elif query.data == 'top_volume':
        get_top_10_volume(context, query.message.chat_id)

def get_market_data():
    """Helper function to get all market data from MEXC."""
    url = f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr"
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()

def get_top_10_gainers(context, chat_id):
    """Fetches and sends the top 10 gaining coins."""
    try:
        data = get_market_data()
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        
        for pair in usdt_pairs:
            pair['priceChangePercent_float'] = float(pair['priceChangePercent']) * 100

        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=True)
        top_10 = sorted_pairs[:10]

        message = "🔥 **أكثر 10 عملات ارتفاعاً في آخر 24 ساعة** 🔥\n\n"
        for i, pair in enumerate(top_10):
            symbol = pair['symbol'].replace('USDT', '')
            change = pair['priceChangePercent_float']
            price = f"{float(pair['lastPrice']):.8f}".rstrip('0').rstrip('.')
            message += f"{i+1}. **${symbol}**\n   - نسبة الارتفاع: `%{change:+.2f}`\n   - السعر الحالي: `${price}`\n\n"
        
        context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in get_top_10_gainers: {e}")
        context.bot.send_message(chat_id=chat_id, text="حدث خطأ أثناء جلب البيانات.")

def get_top_10_losers(context, chat_id):
    """[جديد] Fetches and sends the top 10 losing coins."""
    try:
        data = get_market_data()
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        
        for pair in usdt_pairs:
            pair['priceChangePercent_float'] = float(pair['priceChangePercent']) * 100

        # The only change is sorting in ascending order (reverse=False)
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=False)
        top_10 = sorted_pairs[:10]

        message = "📉 **أكثر 10 عملات انخفاضاً في آخر 24 ساعة** 📉\n\n"
        for i, pair in enumerate(top_10):
            symbol = pair['symbol'].replace('USDT', '')
            change = pair['priceChangePercent_float']
            price = f"{float(pair['lastPrice']):.8f}".rstrip('0').rstrip('.')
            message += f"{i+1}. **${symbol}**\n   - نسبة الانخفاض: `%{change:+.2f}`\n   - السعر الحالي: `${price}`\n\n"
        
        context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in get_top_10_losers: {e}")
        context.bot.send_message(chat_id=chat_id, text="حدث خطأ أثناء جلب البيانات.")

def get_top_10_volume(context, chat_id):
    """Fetches and sends the top 10 coins by volume."""
    try:
        data = get_market_data()
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        
        for pair in usdt_pairs:
            pair['quoteVolume_float'] = float(pair['quoteVolume'])

        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['quoteVolume_float'], reverse=True)
        top_10 = sorted_pairs[:10]

        message = "💰 **أكثر 10 عملات سيولة (فوليوم) في آخر 24 ساعة** 💰\n\n"
        for i, pair in enumerate(top_10):
            symbol = pair['symbol'].replace('USDT', '')
            volume = pair['quoteVolume_float']
            price = f"{float(pair['lastPrice']):.8f}".rstrip('0').rstrip('.')
            message += f"{i+1}. **${symbol}**\n   - حجم التداول: `${volume:,.0f}`\n   - السعر الحالي: `${price}`\n\n"
        
        context.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in get_top_10_volume: {e}")
        context.bot.send_message(chat_id=chat_id, text="حدث خطأ أثناء جلب البيانات.")

# =============================================================================
# وظائف صياد الفومو (تعمل في الخلفية) - لا تغيير هنا
# =============================================================================
def send_startup_message():
    try:
        message = "✅ **بوت صياد الفومو (واجهة الأزرار) متصل الآن!**\n\nأرسل /start لعرض القائمة."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')
        logger.info("تم إرسال رسالة بدء التشغيل بنجاح.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

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
        return {
            'symbol': symbol,
            'volume_increase': f"+{volume_increase_percent:,.2f}%",
            'price_pattern': f"صعود بنسبة +{price_increase_percent:,.2f}% في آخر 4 ساعات",
            'current_price': f"{current_price:.8f}".rstrip('0').rstrip('.')
        }
    except Exception: return None

def send_fomo_alert(alert_data):
    message = f"🚨 *تنبيه فومو محتمل!* 🚨\n\n*العملة:* `${alert_data['symbol']}`\n*منصة:* `MEXC`\n\n📈 *زيادة حجم التداول:* `{alert_data['volume_increase']}`\n🕯️ *نمط السعر:* `{alert_data['price_pattern']}`\n💰 *السعر الحالي:* `{alert_data['current_price']}` USDT\n\n*(تحذير: هذا تنبيه آلي.)*"
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
    logger.info(f"Fomo alert sent for {alert_data['symbol']}")

def fomo_hunter_job():
    logger.info("===== بدء جولة فحص فومو جديدة =====")
    now = datetime.now(UTC)
    for symbol, timestamp in list(recently_alerted.items()):
        if now - timestamp > timedelta(hours=COOLDOWN_PERIOD_HOURS):
            del recently_alerted[symbol]
    
    symbols_to_check = get_usdt_pairs_for_fomo()
    if not symbols_to_check: return

    for i, symbol in enumerate(symbols_to_check):
        if (i + 1) % 200 == 0: logger.info(f"Fomo scan progress: {i+1}/{len(symbols_to_check)}")
        if symbol in recently_alerted: continue
        alert_data = analyze_symbol(symbol)
        if alert_data:
            send_fomo_alert(alert_data)
            recently_alerted[symbol] = datetime.now(UTC)
            time.sleep(1)
    logger.info("===== انتهاء جولة فحص فومو =====")

# =============================================================================
# تشغيل البوت والجدولة
# =============================================================================
def run_scheduler():
    logger.info("Scheduler thread started. Running first fomo scan...")
    fomo_hunter_job()
    schedule.every(RUN_EVERY_MINUTES).minutes.do(fomo_hunter_job)
    while True:
        schedule.run_pending()
        time.sleep(1)

def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.error("FATAL ERROR: Bot token or chat ID are not set.")
        return

    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # إضافة معالجات الأوامر والأزرار
    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CallbackQueryHandler(button_handler)) # [مهم] هذا المعالج يستمع لكل ضغطات الأزرار

    send_startup_message()

    scheduler_thread = threading.Thread(target=run_scheduler)
    scheduler_thread.daemon = True
    scheduler_thread.start()
    
    updater.start_polling()
    logger.info("Bot is now polling for commands and button clicks...")
    updater.idle()

if __name__ == '__main__':
    main()