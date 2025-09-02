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

# --- إعدادات كاشف الزخم (Momentum Detector) ---
MOMENTUM_MAX_PRICE = 0.10
MOMENTUM_MIN_VOLUME_24H = 50000
MOMENTUM_MAX_VOLUME_24H = 2000000
MOMENTUM_VOLUME_INCREASE = 2.0
MOMENTUM_PRICE_INCREASE = 5.0

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
# الوظائف العامة والأساسية
# =============================================================================
def get_market_data():
    """يجلب بيانات السوق الكاملة (قائمة بجميع العملات وأسعارها وحجم تداولها)."""
    url = f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"فشل في جلب بيانات السوق من MEXC: {e}")
        return []

def format_price(price_str):
    """تنسيق السعر لإزالة الأصفار غير الضرورية."""
    try:
        price_float = float(price_str)
        return f"{price_float:.8f}".rstrip('0').rstrip('.')
    except (ValueError, TypeError):
        return price_str

def analyze_symbol(symbol):
    """
    (دالة مساعدة)
    يحلل عملة واحدة للبحث عن نمط الفومو المحدد (حجم + سرعة سعر).
    """
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
            'current_price': format_price(current_price)
        }
    except Exception:
        return None

# =============================================================================
# الوظائف التفاعلية (الأزرار والأوامر)
# =============================================================================
def build_menu():
    keyboard = [
        [InlineKeyboardButton("📈 الأكثر ارتفاعاً", callback_data='top_gainers'),
         InlineKeyboardButton("📉 الأكثر انخفاضاً", callback_data='top_losers')],
        [InlineKeyboardButton("💰 الأعلى سيولة (عام)", callback_data='top_volume')],
        [InlineKeyboardButton("🚀 كاشف الزخم (سريع)", callback_data='momentum_detector')]
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
    query = update.callback_query
    query.answer()
    
    if query.data == 'top_gainers': get_top_10_list(context, query.message.chat_id, 'gainers')
    elif query.data == 'top_losers': get_top_10_list(context, query.message.chat_id, 'losers')
    elif query.data == 'top_volume': get_top_10_list(context, query.message.chat_id, 'volume')
    elif query.data == 'momentum_detector': send_momentum_detector_report(context, query.message.chat_id, query.message.message_id)

def get_top_10_list(context, chat_id, list_type):
    """دالة موحدة لجلب قوائم الأكثر ارتفاعاً، انخفاضاً، وسيولة."""
    type_map = {
        'gainers': {'key': 'priceChangePercent', 'title': '🔥 الأكثر ارتفاعاً', 'reverse': True, 'prefix': '%'},
        'losers': {'key': 'priceChangePercent', 'title': '📉 الأكثر انخفاضاً', 'reverse': False, 'prefix': '%'},
        'volume': {'key': 'quoteVolume', 'title': '💰 الأعلى سيولة', 'reverse': True, 'prefix': '$'}
    }
    config = type_map[list_type]
    
    sent_message = context.bot.send_message(chat_id=chat_id, text=f"🔍 جارِ جلب بيانات {config['title']}...")
    try:
        data = get_market_data()
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['sort_key'] = float(pair[config['key']])
        
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['sort_key'], reverse=config['reverse'])
        
        message = f"**{config['title']} في آخر 24 ساعة**\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            value = pair['sort_key'] * 100 if list_type != 'volume' else pair['sort_key']
            value_str = f"{value:+.2f}" if list_type != 'volume' else f"{value:,.0f}"
            
            message += f"{i+1}. **${pair['symbol'].replace('USDT', '')}**\n"
            message += f"   - النسبة: `{config['prefix']}{value_str}`\n" if list_type != 'volume' else f"   - حجم التداول: `{config['prefix']}{value_str}`\n"
            message += f"   - السعر الحالي: `${format_price(pair['lastPrice'])}`\n\n"
            
        context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_top_10_list for {list_type}: {e}")
        context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text="حدث خطأ أثناء جلب البيانات.")

def send_momentum_detector_report(context, chat_id, message_id):
    initial_text = "🚀 **كاشف الزخم (النسخة السريعة)**\n\n🔍 جارِ فلترة السوق وتحديد الأهداف الواعدة..."
    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text, parse_mode=ParseMode.MARKDOWN)
        
    try:
        market_data = get_market_data()
        usdt_pairs = [s for s in market_data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['priceChangePercent_float'] = float(pair['priceChangePercent']) * 100
        potential_coins = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=True)[:200]

        if not potential_coins:
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لم يتم العثور على عملات نشطة للفحص. حاول لاحقاً.")
            return

        momentum_coins = []
        for i, pair_data in enumerate(potential_coins):
            if (i + 1) % 25 == 0:
                progress_text = f"⏳ يتم فحص الهدف رقم {i+1} من {len(potential_coins)}..."
                try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=progress_text)
                except Exception: pass
            
            symbol = pair_data['symbol']
            try:
                price = float(pair_data['lastPrice']); volume_24h = float(pair_data['quoteVolume'])
                if not (price <= MOMENTUM_MAX_PRICE and MOMENTUM_MIN_VOLUME_24H <= volume_24h <= MOMENTUM_MAX_VOLUME_24H): continue
                
                klines_url = f"{MEXC_API_BASE_URL}/api/v3/klines"; params = {'symbol': symbol, 'interval': '15m', 'limit': 16}; headers = {'User-Agent': 'Mozilla/5.0'}
                res = requests.get(klines_url, params=params, headers=headers, timeout=10); res.raise_for_status(); klines = res.json()
                if len(klines) < 16: continue

                old_klines, new_klines = klines[:8], klines[8:]
                old_volume = sum(float(k[5]) for k in old_klines) / 8 if old_klines else 0
                new_volume = sum(float(k[5]) for k in new_klines) / 8 if new_klines else 0
                start_price, end_price = float(new_klines[0][1]), float(new_klines[-1][4])
                if start_price == 0: continue
                price_change = ((end_price - start_price) / start_price) * 100
                
                if new_volume > old_volume * MOMENTUM_VOLUME_INCREASE and price_change > MOMENTUM_PRICE_INCREASE:
                    momentum_coins.append({'symbol': symbol, 'price_change': price_change, 'current_price': end_price})
                time.sleep(0.1)
            except Exception: continue
        
        if not momentum_coins:
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="🚀 لم تنجح أي من الأهداف الواعدة في اختبار الزخم. السوق هادئ حالياً.")
            return
            
        sorted_coins = sorted(momentum_coins, key=lambda x: x['price_change'], reverse=True)
        message = f"🚀 **تقرير كاشف الزخم (فوري) - {datetime.now().strftime('%H:%M')}** 🚀\n\nقائمة أهداف تظهر بداية زخم لحظي:\n\n"
        for i, coin in enumerate(sorted_coins[:10]):
            symbol_name = coin['symbol'].replace('USDT', ''); price_str = format_price(coin['current_price'])
            message += f"**{i+1}. ${symbol_name}**\n   - السعر الحالي: `${price_str}`\n   - **صعود آخر ساعتين: `%{coin['price_change']:+.2f}`**\n\n"
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in send_momentum_detector_report: {e}")
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ فني أثناء تشغيل كاشف الزخم.")

# =============================================================================
# المهام الآلية (التي تعمل في الخلفية)
# =============================================================================

def fomo_hunter_job():
    """
    (النسخة المطورة والسريعة)
    الوظيفة الآلية التي تعمل في الخلفية لرصد الفومو بشكل استباقي وسريع.
    """
    logger.info("===== Fomo Hunter (FAST SCAN): Starting Scan =====")
    now = datetime.now(UTC)
    
    try:
        market_data = get_market_data()
        usdt_pairs = [s for s in market_data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['priceChangePercent_float'] = float(pair['priceChangePercent']) * 100
        potential_coins = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=True)[:200]
        
        if not potential_coins:
            logger.info("Fomo Hunter: No active coins found in the hot zone.")
            return

    except Exception as e:
        logger.error(f"Fomo Hunter: Error during initial filtering: {e}")
        return

    for pair_data in potential_coins:
        symbol = pair_data['symbol']
        
        if symbol in recently_alerted_fomo and (now - recently_alerted_fomo[symbol]) < timedelta(hours=COOLDOWN_PERIOD_HOURS):
            continue

        alert_data = analyze_symbol(symbol)
        
        if alert_data:
            message = f"""
🚨 **تنبيه فومو آلي: يا أشرف انتبه!** 🚨

**العملة:** `${alert_data['symbol']}`
**منصة:** `MEXC`

📈 *زيادة حجم التداول:* `{alert_data['volume_increase']}`
🕯️ *نمط السعر:* `{alert_data['price_pattern']}`
💰 *السعر الحالي:* `{alert_data['current_price']}` USDT

*(تحذير: هذا تنبيه آلي. قم ببحثك الخاص.)*
            """
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
            logger.info(f"Fomo alert sent automatically for {alert_data['symbol']}")
            recently_alerted_fomo[symbol] = now
            time.sleep(1)

    logger.info("===== Fomo Hunter (FAST SCAN): Scan Finished =====")

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
                message = f"🎯 **تنبيه قناص: إدراج جديد!** 🎯\n\nتم للتو إدراج عملة جديدة على منصة MEXC:\n\n**العملة:** `${symbol}`\n\n*(مخاطر عالية! قم ببحثك بسرعة فائقة.)*"
                bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
            known_symbols.update(newly_listed)
    except Exception as e:
        logger.error(f"Sniper: Error checking for new listings: {e}")

def pattern_hunter_job():
    global pattern_tracker, recently_alerted_pattern
    logger.info("Pattern Hunter: Starting scan...")
    now = datetime.now(UTC)
    
    # Cleanup old entries
    for symbol in list(pattern_tracker.keys()):
        if now - pattern_tracker[symbol]['last_seen'] > timedelta(days=PATTERN_LOOKBACK_DAYS):
            del pattern_tracker[symbol]
    for symbol in list(recently_alerted_pattern.keys()):
        if now - recently_alerted_pattern[symbol] > timedelta(days=PATTERN_LOOKBACK_DAYS):
            del recently_alerted_pattern[symbol]
            
    try:
        data = get_market_data()
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['priceChangePercent_float'] = float(pair['priceChangePercent']) * 100
        top_gainers = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=True)[:30]
        
        for coin in top_gainers:
            symbol = coin['symbol']
            if symbol in pattern_tracker:
                if now.date() > pattern_tracker[symbol]['last_seen'].date():
                    pattern_tracker[symbol]['count'] += 1
                    pattern_tracker[symbol]['last_seen'] = now
            else:
                pattern_tracker[symbol] = {'count': 1, 'last_seen': now}
        
        for symbol, data in pattern_tracker.items():
            if data['count'] >= PATTERN_SIGHTING_THRESHOLD and symbol not in recently_alerted_pattern:
                logger.info(f"Pattern Hunter: PATTERN DETECTED for {symbol}!")
                message = f"🧠 **تنبيه صياد الأنماط: تم رصد سلوك متكرر!** 🧠\n\nالعملة **${symbol.replace('USDT', '')}** ظهرت في قائمة الأعلى ارتفاعاً **{data['count']} مرات** خلال الأيام القليلة الماضية.\n\nقد يشير هذا إلى اهتمام مستمر وقوة شرائية متواصلة.\n\n*(هذا ليس تنبيه فومو لحظي، بل ملاحظة لنمط متكرر.)*"
                bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                recently_alerted_pattern[symbol] = now
    except Exception as e:
        logger.error(f"Pattern Hunter: Error during scan: {e}")

# =============================================================================
# تشغيل البوت والجدولة
# =============================================================================
### ===== تم نقل دالة بدء التشغيل هنا لتكون معرّفة قبل استدعائها =====
def send_startup_message():
    """يرسل رسالة تأكيدية عند بدء تشغيل البوت."""
    try:
        message = "✅ **بوت التداول الذكي متصل الآن!**\n\nأرسل /start لعرض قائمة التحليل الفوري."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info("تم إرسال رسالة بدء التشغيل بنجاح.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

def run_scheduler():
    logger.info("Scheduler thread started.")
    schedule.every(RUN_FOMO_SCAN_EVERY_MINUTES).minutes.do(fomo_hunter_job)
    schedule.every(RUN_LISTING_SCAN_EVERY_SECONDS).seconds.do(new_listings_sniper_job)
    schedule.every(RUN_PATTERN_SCAN_EVERY_HOURS).hours.do(pattern_hunter_job)
    
    # Run jobs once at startup to populate initial data without sending alerts
    # This helps initialize the 'known_symbols' and 'pattern_tracker'
    threading.Thread(target=new_listings_sniper_job).start()
    time.sleep(2) # Small delay
    threading.Thread(target=pattern_hunter_job).start()
    time.sleep(2) # Small delay
    threading.Thread(target=fomo_hunter_job).start()
    
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
    
    # استدعاء دالة بدء التشغيل بعد التأكد من أنها معرّفة
    send_startup_message()
    
    # تشغيل المهام المجدولة في خيط منفصل
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    updater.start_polling()
    logger.info("Bot is now polling for commands and button clicks...")
    updater.idle()

if __name__ == '__main__':
    main()
