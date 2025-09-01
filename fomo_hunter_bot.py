# -*- coding: utf-8 -*-
import os
import requests
import time
import schedule
import logging
from telegram import Bot
from datetime import datetime, timedelta, UTC

# --- الإعدادات الرئيسية ---
# سيتم جلب هذه المتغيرات من منصة النشر
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# --- معايير التحليل (يمكنك تعديل هذه القيم لتناسب استراتيجيتك) ---
VOLUME_SPIKE_MULTIPLIER = 10  # حجم التداول الحالي يجب أن يكون 10 أضعاف المتوسط (يعني 1000%)
PRICE_ACTION_CANDLES = 6      # عدد الشموع التي سيتم تحليلها (على إطار ساعة)
GREEN_CANDLE_THRESHOLD = 4    # الحد الأدنى لعدد الشموع الخضراء من إجمالي الشموع المحللة
MIN_USDT_VOLUME = 500000      # تجاهل العملات ذات حجم تداول أقل من 500 ألف دولار لتجنب العملات الميتة
RUN_EVERY_MINUTES = 15        # تشغيل الفحص كل 15 دقيقة

# --- إعدادات متقدمة ---
MEXC_API_BASE_URL = "https://api.mexc.com"
COOLDOWN_PERIOD_HOURS = 2     # فترة الانتظار (بالساعات) قبل إرسال تنبيه جديد لنفس العملة
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- تهيئة البوت وقاعدة البيانات المؤقتة ---
bot = Bot(token=TELEGRAM_BOT_TOKEN)
recently_alerted = {} # لتخزين العملات التي تم التنبيه عنها مؤخراً

def send_startup_message():
    """يرسل رسالة تأكيدية عند بدء تشغيل البوت."""
    try:
        message = "✅ **بوت صياد الفومو متصل الآن!**\n\nسأقوم بمراقبة السوق وإرسال التنبيهات عند العثور على فرصة محتملة."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')
        logging.info("تم إرسال رسالة بدء التشغيل بنجاح.")
    except Exception as e:
        logging.error(f"فشل في إرسال رسالة بدء التشغيل. يرجى التحقق من TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID. الخطأ: {e}")

def get_usdt_pairs_from_mexc():
    """جلب جميع أزواج التداول التي تنتهي بـ USDT من منصة MEXC."""
    try:
        url = f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        if not isinstance(data, list) or not data:
            logging.warning("API Ticker 24hr استجابت بنجاح ولكنها لم ترجع أي بيانات للعملات.")
            return []
            
        usdt_pairs = [
            s['symbol'] for s in data 
            if s['symbol'].endswith('USDT')
        ]
        logging.info(f"تم العثور على {len(usdt_pairs)} زوج تداول مقابل USDT عبر Ticker API.")
        return usdt_pairs
    except requests.exceptions.RequestException as e:
        logging.error(f"خطأ في جلب قائمة العملات من MEXC عبر Ticker API: {e}")
        return []

def analyze_symbol(symbol):
    """تحليل عملة واحدة بناءً على حجم التداول وحركة السعر."""
    try:
        klines_url = f"{MEXC_API_BASE_URL}/api/v3/klines"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }

        daily_params = {'symbol': symbol, 'interval': '1d', 'limit': 2}
        daily_res = requests.get(klines_url, params=daily_params, headers=headers, timeout=10)
        daily_res.raise_for_status()
        daily_data = daily_res.json()

        if len(daily_data) < 2: return None

        previous_day_volume = float(daily_data[0][7]) 
        current_day_volume = float(daily_data[1][7])

        if current_day_volume < MIN_USDT_VOLUME: return None

        if previous_day_volume == 0:
            volume_increase_percent = float('inf')
        else:
            volume_increase_percent = ((current_day_volume - previous_day_volume) / previous_day_volume) * 100

        is_volume_spike = current_day_volume > (previous_day_volume * VOLUME_SPIKE_MULTIPLIER)
        if not is_volume_spike: return None

        hourly_params = {'symbol': symbol, 'interval': '1h', 'limit': PRICE_ACTION_CANDLES}
        hourly_res = requests.get(klines_url, params=hourly_params, headers=headers, timeout=10)
        hourly_res.raise_for_status()
        hourly_data = hourly_res.json()

        if len(hourly_data) < PRICE_ACTION_CANDLES: return None

        green_candles = sum(1 for c in hourly_data if float(c[4]) > float(c[1]))
        
        is_strong_uptrend = green_candles >= GREEN_CANDLE_THRESHOLD
        if not is_strong_uptrend: return None

        ticker_url = f"{MEXC_API_BASE_URL}/api/v3/ticker/price"
        price_res = requests.get(ticker_url, params={'symbol': symbol}, headers=headers, timeout=10)
        price_res.raise_for_status()
        current_price = float(price_res.json()['price'])

        return {
            'symbol': symbol,
            'volume_increase': f"+{volume_increase_percent:,.2f}%",
            'price_pattern': f"{green_candles} شمعة خضراء من آخر {PRICE_ACTION_CANDLES} شموع (إطار ساعة)",
            'current_price': f"{current_price:.8f}".rstrip('0').rstrip('.')
        }
    except requests.exceptions.RequestException:
        return None
    except Exception as e:
        logging.error(f"خطأ غير متوقع عند تحليل {symbol}: {e}")
        return None

def send_telegram_alert(alert_data):
    """إرسال رسالة تنبيه إلى تليجرام."""
    message = f"""
🚨 *تنبيه فومو محتمل!* 🚨

*العملة:* `${alert_data['symbol']}`
*منصة:* `MEXC`

📈 *زيادة حجم التداول (24 ساعة):* `{alert_data['volume_increase']}`
🕯️ *نمط السعر:* `{alert_data['price_pattern']}`
💰 *السعر الحالي:* `{alert_data['current_price']}` USDT

*(تحذير: هذا تنبيه آلي. قم بأبحاثك الخاصة قبل اتخاذ أي قرار.)*
    """
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown')
        logging.info(f"تم إرسال تنبيه بنجاح للعملة: {alert_data['symbol']}")
    except Exception as e:
        logging.error(f"فشل إرسال رسالة تليجرام: {e}")

def main_job():
    """الوظيفة الرئيسية التي يتم تشغيلها بشكل دوري."""
    logging.info("===== بدء جولة فحص جديدة =====")
    
    now = datetime.now(UTC)
    for symbol, timestamp in list(recently_alerted.items()):
        if now - timestamp > timedelta(hours=COOLDOWN_PERIOD_HOURS):
            del recently_alerted[symbol]
            
    symbols_to_check = get_usdt_pairs_from_mexc()
    if not symbols_to_check:
        logging.warning("لم يتم العثور على عملات للفحص. سيتم إعادة المحاولة لاحقاً.")
        return

    alert_count = 0
    for i, symbol in enumerate(symbols_to_check):
        if symbol in recently_alerted: continue
        if (i + 1) % 100 == 0: logging.info(f"تقدم الفحص: {i+1}/{len(symbols_to_check)}")

        alert_data = analyze_symbol(symbol)
        
        if alert_data:
            send_telegram_alert(alert_data)
            recently_alerted[symbol] = datetime.now(UTC)
            alert_count += 1
            time.sleep(1)
    
    logging.info(f"===== انتهاء جولة الفحص. تم العثور على {alert_count} تنبيه جديد. =====")

if __name__ == "__main__":
    logging.info("تم تشغيل بوت 'صياد الفومو'.")
    logging.info(f"سيتم إجراء الفحص كل {RUN_EVERY_MINUTES} دقيقة.")
    
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logging.error("خطأ فادح: لم يتم تعيين توكن التليجرام أو معرف المحادثة. يرجى إضافتهم كمتغيرات بيئة.")
    else:
        # **الإضافة الجديدة: إرسال رسالة بدء التشغيل**
        send_startup_message()
        
        schedule.every(RUN_EVERY_MINUTES).minutes.do(main_job)
        while True:
            schedule.run_pending()
            time.sleep(1)

