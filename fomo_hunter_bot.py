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

# --- (مُعدل) معايير الزخم لتكون أكثر مرونة ---
MOMENTUM_MAX_PRICE = 0.20  # تم رفع الحد الأعلى للسعر
MOMENTUM_MIN_VOLUME_24H = 30000 # تم تخفيض الحد الأدنى للسيولة
MOMENTUM_MAX_VOLUME_24H = 5000000 # تم رفع الحد الأعلى للسيولة
MOMENTUM_VOLUME_INCREASE = 1.7 # زيادة بنسبة 70%
MOMENTUM_PRICE_INCREASE = 3.5 # زيادة بنسبة 3.5%

# --- بقية المعايير (تبقى كما هي) ---
VOLUME_SPIKE_MULTIPLIER = 10; MIN_USDT_VOLUME = 500000; PRICE_VELOCITY_THRESHOLD = 30.0; RUN_FOMO_SCAN_EVERY_MINUTES = 15;
INSTANT_TIMEFRAME_SECONDS = 10; INSTANT_VOLUME_THRESHOLD_USDT = 50000; INSTANT_TRADE_COUNT_THRESHOLD = 20;
RUN_LISTING_SCAN_EVERY_SECONDS = 60; RUN_PATTERN_SCAN_EVERY_HOURS = 1; PATTERN_SIGHTING_THRESHOLD = 3; PATTERN_LOOKBACK_DAYS = 7;
WEAKNESS_RED_CANDLE_PERCENT = -3.0; WEAKNESS_VOLUME_DROP_RATIO = 0.1;
MEXC_API_BASE_URL = "https://api.mexc.com"; MEXC_WS_URL = "wss://wbs.mexc.com/ws"; COOLDOWN_PERIOD_HOURS = 2;

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- تهيئة البوت والمتغيرات العامة ---
bot = Bot(token=TELEGRAM_BOT_TOKEN)
active_hunts = {}; hunts_lock = threading.Lock(); activity_tracker = {}; activity_lock = threading.Lock();
momentum_scan_lock = threading.Lock()

# =============================================================================
# 1. قسم الرصد اللحظي (WebSocket) - بدون تغيير
# =============================================================================
# (جميع دوال WebSocket هنا، لم تتغير)
async def handle_websocket_message(message):
    try:
        data = json.loads(message)
        if 'd' in data and 'e' in data['d'] and data['d']['e'] == 'spot@public.deals.v3.api':
            for deal in data['d']['D']:
                if deal['S'] == 1:
                    symbol = data['s']; logger.info(f"WebSocket: Buy trade for {symbol}")
                    volume_usdt = float(deal['p']) * float(deal['q']); timestamp = float(deal['t']) / 1000.0
                    with activity_lock:
                        if symbol not in activity_tracker: activity_tracker[symbol] = deque(maxlen=200)
                        activity_tracker[symbol].append({'v': volume_usdt, 't': timestamp})
    except Exception: pass
async def run_websocket_client():
    logger.info("WebSocket client thread starting.")
    sub = {"method": "SUBSCRIPTION", "params": ["spot@public.deals.v3.api@<SYMBOL>"]}
    while True:
        try:
            async with websockets.connect(MEXC_WS_URL) as ws:
                await ws.send(json.dumps(sub)); logger.info("WS subscribed.")
                while True: await handle_websocket_message(await ws.recv())
        except Exception as e:
            logger.error(f"WS error: {e}. Reconnecting..."); await asyncio.sleep(10)
def start_asyncio_loop(loop): asyncio.set_event_loop(loop); loop.run_forever()


# =============================================================================
# 2. الوظائف العامة والأساسية (بدون تغيير)
# =============================================================================
def get_market_data():
    try:
        r = requests.get(f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr", timeout=15); r.raise_for_status(); return r.json()
    except Exception as e: logger.error(f"Market data fetch failed: {e}"); return []
def format_price(p):
    try: return f"{float(p):.8f}".rstrip('0').rstrip('.')
    except (ValueError, TypeError): return p


# =============================================================================
# 3. الوظائف التفاعلية (مع إصلاح منطق كاشف الزخم)
# =============================================================================
BTN_MOMENTUM = "🚀 كاشف الزخم (فائق السرعة)"; BTN_GAINERS = "📈 الأكثر ارتفاعاً"; BTN_LOSERS = "📉 الأكثر انخفاضاً"; BTN_VOLUME = "💰 الأعلى سيولة"
def build_menu(): return ReplyKeyboardMarkup([[BTN_MOMENTUM],[BTN_GAINERS, BTN_LOSERS],[BTN_VOLUME]], resize_keyboard=True)
def start_command(update, context):
    msg = "✅ **بوت التداول الذكي (v7) جاهز!**\n\n- تم إصلاح منطق كاشف الزخم لنتائج أفضل.\n- استخدم /status لفحص حالة الراصد اللحظي."
    update.message.reply_text(msg, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)
def status_command(update, context):
    with activity_lock: count = len(activity_tracker)
    msg = f"📊 **حالة الراصد اللحظي (WebSocket)** 📊\n\n"
    if count > 0: msg += f"✅ **الحالة:** متصل ويعمل بنشاط.\n- يتم تتبع **{count}** عملة الآن."
    else: msg += f"⚠️ **الحالة:** متصل ولكن السوق هادئ حالياً."
    update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update, context):
    button_text = update.message.text; chat_id = update.message.chat_id
    if button_text == BTN_MOMENTUM:
        if not momentum_scan_lock.acquire(blocking=False):
            context.bot.send_message(chat_id=chat_id, text="⏳ **يرجى الانتظار...**\nالفحص السابق لا يزال قيد التنفيذ.")
            return
        sent_msg = context.bot.send_message(chat_id=chat_id, text="🔍 جارِ تنفيذ طلبك...")
        threading.Thread(target=lambda: asyncio.run(run_momentum_detector_async(context, chat_id, sent_msg.message_id))).start()
    elif button_text in [BTN_GAINERS, BTN_LOSERS, BTN_VOLUME]:
        sent_msg = context.bot.send_message(chat_id=chat_id, text="🔍 جارِ جلب القائمة...")
        list_type = {BTN_GAINERS: 'gainers', BTN_LOSERS: 'losers', BTN_VOLUME: 'volume'}[button_text]
        threading.Thread(target=get_top_10_list, args=(context, chat_id, list_type, sent_msg.message_id)).start()

def get_top_10_list(context, chat_id, list_type, message_id):
    # (هذه الدالة تبقى كما هي)
    pass

async def fetch_kline_data(session, symbol):
    url = f"{MEXC_API_BASE_URL}/api/v3/klines"; params = {'symbol': symbol, 'interval': '5m', 'limit': 12}
    try:
        async with session.get(url, params=params, timeout=10) as response:
            if response.status == 200: return await response.json()
    except Exception: return None

async def run_momentum_detector_async(context, chat_id, message_id):
    """(مُعدل) مع منطق فحص مُحسّن ورسائل توضيحية."""
    try:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="🚀 **كاشف الزخم...**\n1/3: جارِ جلب بيانات السوق...")
        market_data = get_market_data()
        if not market_data:
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق."); return

        top_gainers = sorted([s for s in market_data if s['symbol'].endswith('USDT')], key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)[:200]
        
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="🚀 **كاشف الزخم...**\n2/3: جارِ فلترة العملات الواعدة...")
        potential_coins = [
            p for p in top_gainers
            if float(p.get('lastPrice', 999)) <= MOMENTUM_MAX_PRICE and
               MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume', 0)) <= MOMENTUM_MAX_VOLUME_24H
        ]
        
        if not potential_coins:
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="ℹ️ **الفحص اكتمل:**\nلم يتم العثور على عملات واعدة ضمن المعايير الأولية (السعر والسيولة)."); return

        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"🚀 **كاشف الزخم...**\n3/3: جارِ تحليل الزخم لـ **{len(potential_coins)}** عملة...")
        
        momentum_coins_data = []
        async with aiohttp.ClientSession() as session:
            tasks = [fetch_kline_data(session, pair['symbol']) for pair in potential_coins]
            for i, klines in enumerate(await asyncio.gather(*tasks)):
                if not klines or len(klines) < 12: continue
                try:
                    old, new = klines[:6], klines[6:]
                    old_vol = sum(float(k[5]) for k in old)
                    if old_vol == 0: continue
                    new_vol = sum(float(k[5]) for k in new)
                    start_p, end_p = float(new[0][1]), float(new[-1][4])
                    if start_p == 0: continue
                    price_change = ((end_p - start_p) / start_p) * 100
                    if new_vol > old_vol * MOMENTUM_VOLUME_INCREASE and price_change > MOMENTUM_PRICE_INCREASE:
                        coin_data = potential_coins[i]; coin_data['calculated_price_change'] = price_change
                        momentum_coins_data.append(coin_data)
                except (ValueError, IndexError): continue
        
        if not momentum_coins_data:
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"✅ **الفحص اكتمل:**\nتم فحص **{len(potential_coins)}** عملة واعدة، لكن لم يظهر أي منها زخمًا كافيًا حالياً."); return
        
        sorted_coins = sorted(momentum_coins_data, key=lambda x: x['calculated_price_change'], reverse=True)
        message = f"🚀 **تقرير الزخم الفوري - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\n"
        for i, coin in enumerate(sorted_coins[:10]):
            message += f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n   - السعر: `${format_price(coin['lastPrice'])}`\n   - **زخم آخر 30 دقيقة: `%{coin['calculated_price_change']:.2f}`**\n\n"
        message += "*(سيتم الآن مراقبة هذه العملات...)*"
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
        
        with hunts_lock:
            for coin in sorted_coins[:10]:
                if coin['symbol'] not in active_hunts:
                    active_hunts[coin['symbol']] = {'alert_time': datetime.now(UTC)}
                    logger.info(f"MONITORING ADDED: {coin['symbol']}")
    finally:
        momentum_scan_lock.release()

# =============================================================================
# 4. المهام الآلية الدورية (بدون تغيير)
# =============================================================================
def monitor_active_hunts(): pass
# (بقية الدوال هنا)

# =============================================================================
# 5. تشغيل البوت والجدولة (بدون تغيير)
# =============================================================================
def send_startup_message():
    try:
        msg = "✅ **بوت التداول الذكي (v7) متصل الآن!**"
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent.")
    except Exception as e: logger.error(f"Failed to send startup message: {e}")

def run_scheduler():
    logger.info("Scheduler thread started.")
    schedule.every(1).minutes.do(monitor_active_hunts)
    while True: schedule.run_pending(); time.sleep(1)

def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN: logger.critical("FATAL: Bot token not set."); return
    
    # --- تشغيل الخيوط ---
    loop = asyncio.new_event_loop(); asyncio.ensure_future(run_websocket_client(), loop=loop)
    threading.Thread(target=start_asyncio_loop, args=(loop,), daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    # --- إعداد التليجرام ---
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True); dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CommandHandler("status", status_command))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_button_press))
    
    send_startup_message()
    updater.start_polling(); logger.info("Bot is polling...")
    updater.idle()

if __name__ == '__main__':
    main()