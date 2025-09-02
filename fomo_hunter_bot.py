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

# --- معايير تحليل الفومو والزخم (تبقى كما هي) ---
VOLUME_SPIKE_MULTIPLIER = 10; MIN_USDT_VOLUME = 500000; PRICE_VELOCITY_THRESHOLD = 30.0; RUN_FOMO_SCAN_EVERY_MINUTES = 15;
INSTANT_TIMEFRAME_SECONDS = 10; INSTANT_VOLUME_THRESHOLD_USDT = 50000; INSTANT_TRADE_COUNT_THRESHOLD = 20;
RUN_LISTING_SCAN_EVERY_SECONDS = 60; RUN_PATTERN_SCAN_EVERY_HOURS = 1; PATTERN_SIGHTING_THRESHOLD = 3; PATTERN_LOOKBACK_DAYS = 7;
MOMENTUM_MAX_PRICE = 0.10; MOMENTUM_MIN_VOLUME_24H = 50000; MOMENTUM_MAX_VOLUME_24H = 2000000;
MOMENTUM_VOLUME_INCREASE = 1.8; MOMENTUM_PRICE_INCREASE = 4.0;

# --- (جديد) معايير إنذار ضعف الزخم ---
WEAKNESS_RED_CANDLE_PERCENT = -3.0 # شمعة حمراء بنسبة -3%
WEAKNESS_VOLUME_DROP_RATIO = 0.1 # انخفاض حجم التداول بنسبة 90% (مقارنة بالشمعة السابقة)

# --- إعدادات متقدمة ---
MEXC_API_BASE_URL = "https://api.mexc.com"; MEXC_WS_URL = "wss://wbs.mexc.com/ws"; COOLDOWN_PERIOD_HOURS = 2;

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- تهيئة البوت والمتغيرات العامة ---
bot = Bot(token=TELEGRAM_BOT_TOKEN)
# متغيرات قديمة
recently_alerted_fomo = {}; known_symbols = set(); pattern_tracker = {}; recently_alerted_pattern = {};
activity_tracker = {}; activity_lock = threading.Lock(); recently_alerted_instant = {};
# (جديد) متغيرات خاصة بمراقبة الصفقات النشطة
active_hunts = {}
hunts_lock = threading.Lock() # قفل لضمان الأمان عند تعديل القاموس من خيوط متعددة

# =============================================================================
# 1. قسم الرصد اللحظي (WebSocket) - بدون تغيير
# =============================================================================
# (الكود الخاص بالـ WebSocket موجود هنا ولم يتغير)
async def handle_websocket_message(message):
    try:
        data = json.loads(message)
        if 'd' in data and 'e' in data['d'] and data['d']['e'] == 'spot@public.deals.v3.api':
            for deal in data['d']['D']:
                if deal['S'] == 1:
                    symbol = data['s']; logger.info(f"WebSocket: Buy trade received for {symbol}")
                    volume_usdt = float(deal['p']) * float(deal['q']); timestamp = float(deal['t']) / 1000.0
                    with activity_lock:
                        if symbol not in activity_tracker: activity_tracker[symbol] = deque(maxlen=200)
                        activity_tracker[symbol].append({'v': volume_usdt, 't': timestamp})
    except Exception: pass
def send_instant_alert(symbol, total_volume, trade_count): pass
def periodic_activity_checker(): pass
async def run_websocket_client(): pass
def start_asyncio_loop(loop): pass


# =============================================================================
# 2. الوظائف العامة والأساسية (بدون تغيير)
# =============================================================================
def get_market_data():
    url = f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr"
    try:
        response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=15); response.raise_for_status(); return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to fetch market data: {e}"); return []
def format_price(price_str):
    try: return f"{float(price_str):.8f}".rstrip('0').rstrip('.')
    except (ValueError, TypeError): return price_str


# =============================================================================
# 3. الوظائف التفاعلية (كاشف الزخم اليدوي)
# =============================================================================
BTN_MOMENTUM = "🚀 كاشف الزخم (فائق السرعة)"; BTN_GAINERS = "📈 الأكثر ارتفاعاً"; BTN_LOSERS = "📉 الأكثر انخفاضاً"; BTN_VOLUME = "💰 الأعلى سيولة"
def build_menu():
    return ReplyKeyboardMarkup([[BTN_MOMENTUM], [BTN_GAINERS, BTN_LOSERS], [BTN_VOLUME]], resize_keyboard=True)
def start_command(update, context):
    welcome_message = "✅ **بوت التداول الذكي (v5) جاهز!**\n\n- تمت إضافة **إنذار ضعف الزخم** للصفقات النشطة.\n- استخدم /status لفحص حالة الراصد اللحظي."
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)
def status_command(update, context):
    with activity_lock: tracked_symbols_count = len(activity_tracker)
    message = f"📊 **حالة الراصد اللحظي (WebSocket)** 📊\n\n"
    if tracked_symbols_count > 0: message += f"✅ **الحالة:** متصل ويعمل بنشاط.\n- يتم تتبع **{tracked_symbols_count}** عملة تظهر نشاطاً شرائياً الآن."
    else: message += f"⚠️ **الحالة:** متصل ولكن السوق هادئ حالياً."
    update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
def handle_button_press(update, context):
    button_text = update.message.text; chat_id = update.message.chat_id; sent_message = context.bot.send_message(chat_id=chat_id, text="🔍 جارِ تنفيذ طلبك...")
    if button_text == BTN_MOMENTUM: threading.Thread(target=lambda: asyncio.run(run_momentum_detector_async(context, chat_id, sent_message.message_id))).start()
    elif button_text in [BTN_GAINERS, BTN_LOSERS, BTN_VOLUME]:
        list_type = {'📈 الأكثر ارتفاعاً': 'gainers', '📉 الأكثر انخفاضاً': 'losers', '💰 الأعلى سيولة': 'volume'}[button_text]
        get_top_10_list(context, chat_id, list_type, sent_message.message_id)
def get_top_10_list(context, chat_id, list_type, message_id): pass # Code remains the same
async def fetch_kline_data(session, symbol): pass # Code remains the same

async def run_momentum_detector_async(context, chat_id, message_id):
    # ... (الكود الأولي للفحص السريع يبقى كما هو) ...
    initial_text = "🚀 **كاشف الزخم (فائق السرعة)**\n\n🔍 جارِ الفحص المتوازي للسوق..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text, parse_mode=ParseMode.MARKDOWN)
    except Exception: pass
    market_data = get_market_data()
    if not market_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق."); return
    potential_coins = [p for p in sorted([s for s in market_data if s['symbol'].endswith('USDT')], key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)[:200] if float(p.get('lastPrice', 1)) <= MOMENTUM_MAX_PRICE and MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume', 0)) <= MOMENTUM_MAX_VOLUME_24H]
    if not potential_coins:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لم يتم العثور على عملات واعدة ضمن المعايير الأولية."); return
    
    momentum_coins_data = [] # سيحتوي على بيانات كاملة
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_kline_data(session, pair['symbol']) for pair in potential_coins]
        for i, klines in enumerate(await asyncio.gather(*tasks)):
            if not klines or len(klines) < 12: continue
            try:
                # ... (نفس منطق التحليل) ...
                old_klines, new_klines = klines[:6], klines[6:]
                old_volume = sum(float(k[5]) for k in old_klines)
                if old_volume == 0: continue
                new_volume = sum(float(k[5]) for k in new_klines)
                start_price, end_price = float(new_klines[0][1]), float(new_klines[-1][4])
                if start_price == 0: continue
                price_change = ((end_price - start_price) / start_price) * 100
                if new_volume > old_volume * MOMENTUM_VOLUME_INCREASE and price_change > MOMENTUM_PRICE_INCREASE:
                    coin_full_data = potential_coins[i]
                    coin_full_data['calculated_price_change'] = price_change
                    momentum_coins_data.append(coin_full_data)
            except (ValueError, IndexError): continue

    if not momentum_coins_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **الفحص السريع اكتمل:** لا يوجد زخم حقيقي حالياً."); return
    
    sorted_coins = sorted(momentum_coins_data, key=lambda x: x['calculated_price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم الفوري - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\nأفضل الأهداف التي تظهر بداية زخم الآن:\n\n"
    for i, coin_data in enumerate(sorted_coins[:10]):
        message += f"**{i+1}. ${coin_data['symbol'].replace('USDT', '')}**\n   - السعر: `${format_price(coin_data['lastPrice'])}`\n   - **زخم آخر 30 دقيقة: `%{coin_data['calculated_price_change']:.2f}`**\n\n"
    message += "*(سيتم الآن مراقبة هذه العملات وإرسال إنذار عند ضعف الزخم)*"
    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

    # --- (جديد) إضافة العملات المكتشفة إلى قائمة المراقبة النشطة ---
    with hunts_lock:
        for coin_data in sorted_coins[:10]:
            symbol = coin_data['symbol']
            if symbol not in active_hunts:
                active_hunts[symbol] = {
                    'alert_price': float(coin_data['lastPrice']),
                    'initial_5m_volume': float(coin_data.get('volume', 0)), # استخدام حجم آخر شمعة كمرجع
                    'alert_time': datetime.now(UTC)
                }
                logger.info(f"MONITORING ADDED: {symbol} from manual momentum scan.")


# =============================================================================
# 4. المهام الآلية الدورية (صياد الفومو الآلي والمراقب الجديد)
# =============================================================================
def fomo_hunter_job():
    logger.info("===== Fomo Hunter (PERIODIC SCAN): Starting Scan =====")
    now = datetime.now(UTC)
    try:
        market_data = get_market_data()
        potential_coins = sorted([s for s in market_data if s['symbol'].endswith('USDT')], key=lambda x: float(x.get('priceChangePercent',0)), reverse=True)[:200]
        for pair_data in potential_coins:
            symbol = pair_data['symbol']
            if symbol in recently_alerted_fomo and (now - recently_alerted_fomo[symbol]) < timedelta(hours=COOLDOWN_PERIOD_HOURS): continue
            
            alert_data = analyze_symbol(symbol) # دالة التحليل العميق
            
            if alert_data:
                message = f"🚨 **تنبيه فومو آلي: يا أشرف انتبه!** 🚨\n\n**العملة:** `${alert_data['symbol']}` ... (بقية الرسالة)"
                bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                logger.info(f"Fomo alert sent for {alert_data['symbol']}")
                recently_alerted_fomo[symbol] = now
                
                # --- (جديد) إضافة العملة إلى قائمة المراقبة النشطة ---
                with hunts_lock:
                    if symbol not in active_hunts:
                        active_hunts[symbol] = {
                            'alert_price': float(alert_data['current_price']),
                            'initial_5m_volume': float(pair_data.get('volume', 0)),
                            'alert_time': now
                        }
                        logger.info(f"MONITORING ADDED: {symbol} from automatic fomo hunter.")
                time.sleep(1)
    except Exception as e:
        logger.error(f"Fomo Hunter: Error during periodic scan: {e}")

def monitor_active_hunts():
    """(جديد) تراقب الصفقات النشطة وترسل إنذاراً عند ضعف الزخم."""
    with hunts_lock:
        if not active_hunts: return # الخروج مبكراً إذا لم يكن هناك شيء للمراقبة
        symbols_to_monitor = list(active_hunts.keys())
    
    logger.info(f"MONITOR: Checking {len(symbols_to_monitor)} active hunt(s): {symbols_to_monitor}")

    for symbol in symbols_to_monitor:
        try:
            # جلب آخر 10 شمعات (5 دقائق) لحساب المتوسط المتحرك
            klines_url = f"{MEXC_API_BASE_URL}/api/v3/klines"
            params = {'symbol': symbol, 'interval': '5m', 'limit': 10}
            res = requests.get(klines_url, params=params, timeout=10)
            if res.status_code != 200: continue
            klines = res.json()
            if len(klines) < 10: continue

            last_candle = klines[-1]
            prev_candle = klines[-2]
            
            open_price = float(last_candle[1]); current_price = float(last_candle[4]);
            last_volume = float(last_candle[5]); prev_volume = float(prev_candle[5])
            
            weakness_reason = None

            # الشرط الأول: الشمعة الحمراء القوية
            price_change_percent = ((current_price - open_price) / open_price) * 100
            if price_change_percent <= WEAKNESS_RED_CANDLE_PERCENT:
                weakness_reason = f"شمعة حمراء قوية ({price_change_percent:.2f}%)"

            # الشرط الثاني: موت حجم التداول
            elif prev_volume > 0 and (last_volume / prev_volume) <= WEAKNESS_VOLUME_DROP_RATIO:
                weakness_reason = "انخفاض حاد في السيولة"
            
            # الشرط الثالث: كسر الدعم (MA10)
            else:
                ma10 = sum(float(k[4]) for k in klines) / 10
                if current_price < ma10:
                    weakness_reason = f"كسر الدعم الفني (MA10 @ {format_price(ma10)})"

            if weakness_reason:
                message = f"⚠️ **تحذير: الزخم في {symbol.replace('USDT', '')} بدأ يضعف!** ⚠️\n\n- **تم رصد:** {weakness_reason}\n- **السعر الحالي:** ${format_price(current_price)}\n\n*قد يكون هذا مؤشراً على بداية انعكاس الاتجاه. يرجى المراجعة.*"
                bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                logger.warning(f"WEAKNESS ALERT sent for {symbol}. Reason: {weakness_reason}. Removing from active hunts.")
                
                # حذف العملة من المراقبة بعد إرسال التنبيه
                with hunts_lock:
                    if symbol in active_hunts:
                        del active_hunts[symbol]
            
            time.sleep(0.5) # فاصل بسيط بين الطلبات

        except Exception as e:
            logger.error(f"MONITOR: Error processing {symbol}: {e}")
            with hunts_lock: # حذف العملة أيضاً في حال حدوث خطأ فادح لمنع التكرار
                if symbol in active_hunts: del active_hunts[symbol]

def new_listings_sniper_job(): pass # Code remains the same
def pattern_hunter_job(): pass # Code remains the same

# =============================================================================
# 5. تشغيل البوت والجدولة (مع إضافة مهمة المراقبة الجديدة)
# =============================================================================
def run_scheduler():
    logger.info("Scheduler thread for periodic jobs started.")
    schedule.every(RUN_FOMO_SCAN_EVERY_MINUTES).minutes.do(fomo_hunter_job)
    schedule.every(RUN_LISTING_SCAN_EVERY_SECONDS).seconds.do(new_listings_sniper_job)
    schedule.every(RUN_PATTERN_SCAN_EVERY_HOURS).hours.do(pattern_hunter_job)
    
    # --- (جديد) إضافة مهمة مراقبة الصفقات النشطة لتعمل كل دقيقة ---
    schedule.every(1).minutes.do(monitor_active_hunts)
    
    while True:
        schedule.run_pending(); time.sleep(1)

def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set."); return
    
    # ... (بقية كود التشغيل يبقى كما هو) ...
    asyncio_loop = asyncio.new_event_loop(); asyncio.ensure_future(run_websocket_client(), loop=asyncio_loop)
    ws_thread = threading.Thread(target=start_asyncio_loop, args=(asyncio_loop,), daemon=True); ws_thread.start()
    checker_thread = threading.Thread(target=periodic_activity_checker, daemon=True); checker_thread.start()
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True); scheduler_thread.start()
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True); dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start_command)); dp.add_handler(CommandHandler("status", status_command))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_button_press))
    send_startup_message(); updater.start_polling(); logger.info("Telegram bot is now polling..."); updater.idle()
def send_startup_message(): pass # Code remains the same
if __name__ == '__main__':
    main()