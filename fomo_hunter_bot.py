# -*- coding: utf-8 -*-
import os
import asyncio
import json
import logging
import re
import aiohttp
from datetime import datetime, timedelta, UTC
from collections import deque
from telegram import Bot, ParseMode, ReplyKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# =============================================================================
# --- الإعدادات الرئيسية ---
# =============================================================================
# --- Main Settings ---

# --- Telegram Configuration ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# --- Periodic FOMO Scan Criteria ---
VOLUME_SPIKE_MULTIPLIER = 10
MIN_USDT_VOLUME = 500000
PRICE_VELOCITY_THRESHOLD = 30.0
RUN_FOMO_SCAN_EVERY_MINUTES = 15
TOP_GAINERS_CANDIDATE_LIMIT = 200

# --- Real-time (WebSocket) Monitoring Criteria ---
INSTANT_TIMEFRAME_SECONDS = 10
INSTANT_VOLUME_THRESHOLD_USDT = 50000
INSTANT_TRADE_COUNT_THRESHOLD = 20

# --- New Listings Sniper Settings ---
RUN_LISTING_SCAN_EVERY_SECONDS = 60

# --- Performance Tracker Settings ---
RUN_PERFORMANCE_TRACKER_EVERY_MINUTES = 5
PERFORMANCE_TRACKING_DURATION_HOURS = 24

# --- Manual Momentum Detector Settings ---
MOMENTUM_MAX_PRICE = 0.10
MOMENTUM_MIN_VOLUME_24H = 50000
MOMENTUM_MAX_VOLUME_24H = 2000000
MOMENTUM_VOLUME_INCREASE = 1.8
MOMENTUM_PRICE_INCREASE = 4.0
MOMENTUM_KLINE_INTERVAL = '5m'
MOMENTUM_KLINE_LIMIT = 12

# --- Advanced Settings ---
MEXC_API_BASE_URL = "https://api.mexc.com"
COINGECKO_API_BASE_URL = "https://api.coingecko.com/api/v3"
MEXC_WS_URL = "wss://wbs.mexc.com/ws"
COOLDOWN_PERIOD_HOURS = 2
HTTP_TIMEOUT = 15

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة وتهيئة البوت ---
# =============================================================================
bot = Bot(token=TELEGRAM_BOT_TOKEN)
recently_alerted_fomo = {}
recently_alerted_instant = {}
known_symbols = set()
active_hunts = {}
performance_tracker = {}
activity_tracker = {}
activity_lock = asyncio.Lock()

# =============================================================================
# 1. قسم الشبكة والوظائف الأساسية (Async)
# =============================================================================
async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None):
    try:
        async with session.get(url, params=params, timeout=HTTP_TIMEOUT, headers={'User-Agent': 'Mozilla/5.0'}) as response:
            response.raise_for_status()
            return await response.json()
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None

async def get_market_data(session: aiohttp.ClientSession):
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr")

async def get_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int):
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/klines", params=params)

async def get_current_price(session: aiohttp.ClientSession, symbol: str) -> float | None:
    data = await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/price", {'symbol': symbol})
    return float(data['price']) if data and 'price' in data else None

def format_price(price_str):
    try:
        return f"{float(price_str):.8g}"
    except (ValueError, TypeError):
        return str(price_str)

# =============================================================================
# 2. قسم الرصد اللحظي (WebSocket)
# =============================================================================
async def handle_websocket_message(message):
    try:
        data = json.loads(message)
        if data.get("method") == "PONG": return
        if data.get('d', {}).get('e') == 'spot@public.deals.v3.api':
            symbol = data['s']
            for deal in data['d']['D']:
                if deal['S'] == 1:
                    volume_usdt = float(deal['p']) * float(deal['q'])
                    timestamp = float(deal['t']) / 1000.0
                    async with activity_lock:
                        if symbol not in activity_tracker:
                            activity_tracker[symbol] = deque(maxlen=200)
                        activity_tracker[symbol].append({'v': volume_usdt, 't': timestamp})
    except Exception: pass

async def run_websocket_client():
    logger.info("Starting WebSocket client...")
    subscription_msg = {"method": "SUBSCRIPTION", "params": ["spot@public.deals.v3.api"]}
    while True:
        try:
            async with websockets.connect(MEXC_WS_URL) as websocket:
                await websocket.send(json.dumps(subscription_msg))
                logger.info("Successfully subscribed to MEXC WebSocket.")
                while True:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=20.0)
                        await handle_websocket_message(message)
                    except asyncio.TimeoutError:
                        await websocket.send(json.dumps({"method": "PING"}))
        except Exception as e:
            logger.error(f"WebSocket error: {e}. Reconnecting...")
            await asyncio.sleep(10)

async def periodic_activity_checker():
    logger.info("Activity checker started.")
    while True:
        await asyncio.sleep(5)
        now_ts = datetime.now(UTC).timestamp()
        
        # Clear old alerts to allow re-alerting after cooldown
        for sym in list(recently_alerted_instant.keys()):
            if now_ts - recently_alerted_instant[sym] > COOLDOWN_PERIOD_HOURS * 3600:
                del recently_alerted_instant[sym]

        symbols_to_check = list(activity_tracker.keys())
        async with activity_lock:
            for symbol in symbols_to_check:
                trades = activity_tracker.get(symbol)
                if not trades: continue
                while trades and now_ts - trades[0]['t'] > INSTANT_TIMEFRAME_SECONDS:
                    trades.popleft()
                if not trades:
                    if symbol in activity_tracker: del activity_tracker[symbol]
                    continue

                total_volume = sum(trade['v'] for trade in trades)
                trade_count = len(trades)

                if (total_volume >= INSTANT_VOLUME_THRESHOLD_USDT and
                    trade_count >= INSTANT_TRADE_COUNT_THRESHOLD and
                    symbol not in recently_alerted_instant):
                    send_instant_alert(symbol, total_volume, trade_count)
                    recently_alerted_instant[symbol] = now_ts
                    if symbol in activity_tracker: del activity_tracker[symbol]

def send_instant_alert(symbol, total_volume, trade_count):
    message = (f"⚡️ **رصد نشاط شراء مفاجئ! (لحظي)** ⚡️\n\n"
               f"**العملة:** `${symbol}`\n"
               f"**حجم الشراء (آخر {INSTANT_TIMEFRAME_SECONDS} ثوانٍ):** `${total_volume:,.0f} USDT`\n"
               f"**عدد الصفقات:** `{trade_count}`\n\n"
               f"*(إشارة عالية المخاطر)*")
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"INSTANT ALERT for {symbol}.")
    except Exception as e:
        logger.error(f"Failed to send instant alert for {symbol}: {e}")

# =============================================================================
# 3. الوظائف التفاعلية (أوامر البوت)
# =============================================================================
BTN_MOMENTUM = "🚀 كاشف الزخم (فائق السرعة)"
BTN_GAINERS = "📈 الأكثر ارتفاعاً"
BTN_LOSERS = "📉 الأكثر انخفاضاً"
BTN_VOLUME = "💰 الأعلى سيولة"
BTN_STATUS = "📊 الحالة"
BTN_PERFORMANCE = "📈 تقرير الأداء"

def build_menu():
    keyboard = [[BTN_MOMENTUM], [BTN_GAINERS, BTN_LOSERS, BTN_VOLUME], [BTN_STATUS, BTN_PERFORMANCE]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def start_command(update: Update, context: CallbackContext):
    msg = ("✅ **بوت التداول الذكي (v11.2) جاهز!**\n\n"
           "**ميزة جديدة:** أرسل رمز أي عملة (مثلاً `BTC`) لتحصل على تقرير فني ومعلوماتي فوري عنها.\n\n"
           "استخدم الأزرار أدناه للوصول إلى الوظائف الأخرى.")
    update.message.reply_text(msg, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def status_command(update: Update, context: CallbackContext):
    msg = (f"📊 **حالة البوت** 📊\n\n"
           f"**الراصد اللحظي:** ✅ متصل، يتبع {len(activity_tracker)} عملة.\n"
           f"**مساعد الصفقات:** ✅ نشط، يراقب {len(active_hunts)} فرصة.\n"
           f"**متتبع الأداء:** ✅ نشط، يراقب {len(performance_tracker)} عملة.")
    update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update: Update, context: CallbackContext):
    text = update.message.text
    if text == BTN_STATUS:
        status_command(update, context)
        return
    
    chat_id = update.message.chat_id
    loop = context.bot_data['loop']
    session = context.bot_data['session']
    sent_message = context.bot.send_message(chat_id=chat_id, text="🔍 جارِ تنفيذ طلبك...")
    
    task_map = {
        BTN_GAINERS: get_top_10_list(context, chat_id, sent_message.message_id, 'gainers', session),
        BTN_LOSERS: get_top_10_list(context, chat_id, sent_message.message_id, 'losers', session),
        BTN_VOLUME: get_top_10_list(context, chat_id, sent_message.message_id, 'volume', session),
        BTN_MOMENTUM: run_momentum_detector(context, chat_id, sent_message.message_id, session),
        BTN_PERFORMANCE: get_performance_report(context, chat_id, sent_message.message_id, session),
    }
    task = task_map.get(text)
    if task:
        asyncio.run_coroutine_threadsafe(task, loop)

async def get_top_10_list(context, chat_id, msg_id, list_type, session):
    type_map = {
        'gainers': {'key': 'priceChangePercent', 'title': '🔥 الأكثر ارتفاعاً', 'rev': True},
        'losers': {'key': 'priceChangePercent', 'title': '📉 الأكثر انخفاضاً', 'rev': False},
        'volume': {'key': 'quoteVolume', 'title': '💰 الأعلى سيولة', 'rev': True}
    }
    config = type_map[list_type]
    try:
        data = await get_market_data(session)
        if not data: raise ValueError("No market data.")
        pairs = sorted([s for s in data if s['symbol'].endswith('USDT')], 
                       key=lambda x: float(x.get(config['key'], 0)), reverse=config['rev'])
        
        msg = f"**{config['title']} (آخر 24 ساعة)**\n\n"
        for i, p in enumerate(pairs[:10]):
            val = float(p.get(config['key'], 0))
            val_str = f"{val * 100:+.2f}%" if list_type != 'volume' else f"${val:,.0f}"
            msg += (f"{i+1}. **${p['symbol'].replace('USDT', '')}**\n"
                    f"   - {'النسبة' if list_type != 'volume' else 'الحجم'}: `{val_str}`\n"
                    f"   - السعر: `${format_price(p.get('lastPrice'))}`\n\n")
        context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_top_10_list: {e}")
        context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="حدث خطأ.")

async def run_momentum_detector(context, chat_id, msg_id, session):
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="🚀 **كاشف الزخم:** جارِ الفحص...")
    except Exception: pass
    
    market_data = await get_market_data(session)
    if not market_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="⚠️ تعذر جلب بيانات السوق."); return

    p_coins = [p for p in market_data if p.get('symbol','').endswith('USDT') and
               float(p.get('lastPrice','1')) <= MOMENTUM_MAX_PRICE and
               MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= MOMENTUM_MAX_VOLUME_24H]
    
    klines_tasks = [get_klines(session, p['symbol'], MOMENTUM_KLINE_INTERVAL, MOMENTUM_KLINE_LIMIT) for p in p_coins]
    all_klines = await asyncio.gather(*klines_tasks)
    
    momentum_coins = []
    for i, klines in enumerate(all_klines):
        if not klines or len(klines) < MOMENTUM_KLINE_LIMIT: continue
        try:
            sp = MOMENTUM_KLINE_LIMIT // 2
            old_v = sum(float(k[5]) for k in klines[:sp])
            new_v = sum(float(k[5]) for k in klines[sp:])
            start_p = float(klines[sp][1])
            if old_v == 0 or start_p == 0: continue
            end_p = float(klines[-1][4])
            price_chg = ((end_p - start_p) / start_p) * 100
            
            if new_v > old_v * MOMENTUM_VOLUME_INCREASE and price_chg > MOMENTUM_PRICE_INCREASE:
                momentum_coins.append({'sym': p_coins[i]['symbol'], 'chg': price_chg, 'pr': end_p})
        except Exception: continue
        
    if not momentum_coins:
        context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="✅ **الفحص اكتمل:** لا يوجد زخم حقيقي حالياً."); return
        
    sorted_coins = sorted(momentum_coins, key=lambda x: x['chg'], reverse=True)
    msg = f"🚀 **تقرير الزخم الفوري** 🚀\n\n"
    for i, coin in enumerate(sorted_coins[:10]):
        msg += (f"**{i+1}. ${coin['sym'].replace('USDT', '')}**\n"
                f"   - السعر: `${format_price(coin['pr'])}`\n"
                f"   - **زخم آخر 30 دقيقة: `%{coin['chg']:+.2f}`**\n\n")
    context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    
    now = datetime.now(UTC)
    for coin in sorted_coins[:10]:
        add_to_monitoring(coin['sym'], float(coin['pr']), 0, now, "الزخم اليدوي")

# =============================================================================
# 4. ميزة المدقق (The Verifier)
# =============================================================================
def verifier_command_handler(update: Update, context: CallbackContext):
    symbol = update.message.text.strip().upper()
    chat_id = update.message.chat_id
    loop = context.bot_data['loop']
    session = context.bot_data['session']
    sent_message = context.bot.send_message(chat_id=chat_id, text=f"🕵️‍♂️ **المدقق:** جارِ فحص `{symbol}`...")
    task = run_verifier(context, chat_id, sent_message.message_id, symbol, session)
    asyncio.run_coroutine_threadsafe(task, loop)

async def run_verifier(context, chat_id, msg_id, base_symbol, session):
    symbol_usdt = f"{base_symbol}USDT"
    mexc_task = get_mexc_market_snapshot(session, symbol_usdt)
    tech_task = analyze_technical_data(session, symbol_usdt)
    cg_task = get_coingecko_data(session, base_symbol)
    mexc_data, tech_data, cg_data = await asyncio.gather(mexc_task, tech_task, cg_task)

    if not mexc_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=f"لم يتم العثور على `{symbol_usdt}` في MEXC.", parse_mode=ParseMode.MARKDOWN)
        return

    report = await format_verifier_report(base_symbol, mexc_data, tech_data, cg_data)
    context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=report, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def get_mexc_market_snapshot(session, symbol_usdt):
    data = await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr", {'symbol': symbol_usdt})
    if not data: return None
    return {'price': data.get('lastPrice'), 'change_24h': float(data.get('priceChangePercent', 0)) * 100, 'volume_24h': float(data.get('quoteVolume', 0))}

def calculate_rsi(prices: list, period: int = 14):
    if len(prices) < period + 1: return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

async def analyze_technical_data(session, symbol_usdt):
    try:
        klines = await get_klines(session, symbol_usdt, '1h', 100)
        if not klines or len(klines) < 50: return None
        closes = [float(k[4]) for k in klines]
        rsi = calculate_rsi(closes, 14)
        sma_50 = sum(closes[-50:]) / 50
        trend = "صاعد" if closes[-1] > sma_50 else "هابط"
        return {'trend': trend, 'rsi': rsi, 'basis': "فوق متوسط 50 شمعة" if trend == "صاعد" else "تحت متوسط 50 شمعة"}
    except Exception: return None

async def get_coingecko_data(session, base_symbol):
    try:
        search = await fetch_json(session, f"{COINGECKO_API_BASE_URL}/search", {'query': base_symbol})
        if not search or not search.get('coins'): return None
        coin_id = next((c['id'] for c in search['coins'] if c['symbol'].upper() == base_symbol), search['coins'][0]['id'])

        data = await fetch_json(session, f"{COINGECKO_API_BASE_URL}/coins/{coin_id}")
        if not data: return None
        desc = data.get('description', {}).get('en', 'N/A').split('. ')[0]
        links = data.get('links', {})
        return {
            'desc': desc[:200] + '...' if len(desc) > 200 else desc,
            'web': links.get('homepage', [''])[0],
            'x': f"https://twitter.com/{links.get('twitter_screen_name')}" if links.get('twitter_screen_name') else '',
            'tg': links.get('telegram_channel_identifier', '')
        }
    except Exception: return None

async def format_verifier_report(base_symbol, mexc, tech, cg):
    report = f"🕵️‍♂️ **تقرير المدقق لعملة: ${base_symbol}** 🕵️‍♂️\n\n"
    report += "--- (1) **بيانات السوق (MEXC)** ---\n"
    report += f"  - السعر: `${format_price(mexc['price'])}`\n"
    report += f"  - التغير (24س): `{mexc['change_24h']:+.2f}%` {'🟢' if mexc['change_24h'] >= 0 else '🔴'}\n"
    report += f"  - الحجم (24س): `${mexc['volume_24h']:,.0f}`\n\n"
    report += "--- (2) **تحليل فني (ساعة)** ---\n"
    if tech:
        rsi_level = "تشبع شرائي" if tech['rsi'] > 70 else "تشبع بيعي" if tech['rsi'] < 30 else "طبيعي"
        report += f"  - الاتجاه: `{tech['trend']}` ({tech['basis']}).\n"
        report += f"  - الزخم (RSI): `{tech['rsi']:.1f}` (مستوى `{rsi_level}`).\n\n"
    else: report += "  - تعذر التحليل (بيانات غير كافية).\n\n"
    report += "--- (3) **هوية المشروع (CoinGecko)** ---\n"
    if cg:
        report += f"  - الوصف: {cg.get('desc', 'N/A')}\n"
        if cg.get('web'): report += f"  - الموقع: [اضغط هنا]({cg['web']})\n"
        if cg.get('x'): report += f"  - تويتر (X): [اضغط هنا]({cg['x']})\n"
        if cg.get('tg'): report += f"  - تليجرام: [اضغط هنا](https://t.me/{cg['tg']})\n\n"
    else: report += "  - لم يتم العثور على بيانات المشروع.\n\n"
    report += "--- (4) **خلاصة المدقق** ---\n"
    s, w = [], []
    if tech and tech['trend'] == 'صاعد': s.append("اتجاه صاعد")
    if mexc['volume_24h'] > 500000: s.append("حجم تداول جيد")
    if tech and tech['rsi'] > 75: w.append("RSI مرتفع (خطر تصحيح)")
    if not cg: w.append("مشروع غير معروف")
    report += f"  - القوة: {', '.join(s) if s else 'لا توجد نقاط قوة واضحة'}.\n"
    report += f"  - الضعف: {', '.join(w) if w else 'لا توجد نقاط ضعف واضحة'}.\n"
    return report

# =============================================================================
# 5. المهام الآلية الدورية (الكود القديم بالكامل)
# =============================================================================
def add_to_monitoring(symbol, alert_price, peak_volume, alert_time, source):
    if symbol not in active_hunts:
        active_hunts[symbol] = {'alert_price': alert_price, 'alert_time': alert_time}
        logger.info(f"MONITORING STARTED for ({source}) {symbol}")
    if symbol not in performance_tracker:
        performance_tracker[symbol] = {'alert_price': alert_price, 'alert_time': alert_time, 'source': source, 'current_price': alert_price, 'high_price': alert_price, 'status': 'Tracking'}
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol}")

async def fomo_hunter_loop(session: aiohttp.ClientSession):
    logger.info("Fomo Hunter started.")
    while True:
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)
        logger.info("===== Fomo Hunter: Starting Scan =====")
        now = datetime.now(UTC)
        try:
            market_data = await get_market_data(session)
            if not market_data: continue
            
            potential = sorted([s for s in market_data if s.get('symbol','').endswith('USDT')], 
                               key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)[:TOP_GAINERS_CANDIDATE_LIMIT]
            
            for pair in potential:
                symbol = pair['symbol']
                if symbol in recently_alerted_fomo and (now - recently_alerted_fomo[symbol]) < timedelta(hours=COOLDOWN_PERIOD_HOURS):
                    continue
                
                alert_data = await analyze_fomo_symbol(session, symbol)
                if alert_data:
                    msg = (f"🚨 **تنبيه فومو آلي** 🚨\n\n"
                           f"**العملة:** `${symbol}`\n"
                           f"📈 *زيادة الحجم:* `{alert_data['vol_inc']}`\n"
                           f"🕯️ *نمط السعر:* `{alert_data['price_ptrn']}`\n"
                           f"💰 *السعر:* `{alert_data['price']}` USDT")
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)
                    logger.info(f"Fomo alert for {symbol}")
                    recently_alerted_fomo[symbol] = now
                    add_to_monitoring(symbol, float(alert_data['price']), 0, now, "فومو آلي")
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error in fomo_hunter_loop: {e}")
        logger.info("===== Fomo Hunter: Scan Finished =====")

async def analyze_fomo_symbol(session, symbol):
    try:
        daily_klines = await get_klines(session, symbol, '1d', 2)
        if not daily_klines or len(daily_klines) < 2: return None
        prev_vol, curr_vol = float(daily_klines[0][7]), float(daily_klines[1][7])
        if not (curr_vol > MIN_USDT_VOLUME and curr_vol > (prev_vol * VOLUME_SPIKE_MULTIPLIER)):
            return None

        hourly_klines = await get_klines(session, symbol, '1h', 4)
        if not hourly_klines or len(hourly_klines) < 4: return None
        initial_price = float(hourly_klines[0][1])
        if initial_price == 0: return None
        price_inc = ((float(hourly_klines[-1][2]) - initial_price) / initial_price) * 100
        if not (price_inc >= PRICE_VELOCITY_THRESHOLD):
            return None

        current_price = await get_current_price(session, symbol)
        if not current_price: return None
        
        vol_inc_str = f"+{((curr_vol - prev_vol) / prev_vol) * 100:,.2f}%" if prev_vol > 0 else "∞"
        return {'vol_inc': vol_inc_str, 'price_ptrn': f"صعود +{price_inc:,.2f}% في 4 ساعات", 'price': format_price(current_price)}
    except Exception: return None

async def new_listings_sniper_loop(session: aiohttp.ClientSession):
    global known_symbols
    logger.info("New Listings Sniper started.")
    initial_data = await get_market_data(session)
    if initial_data:
        known_symbols = {s['symbol'] for s in initial_data if s.get('symbol','').endswith('USDT')}
        logger.info(f"Sniper: Initialized with {len(known_symbols)} symbols.")
    
    while True:
        await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS)
        try:
            data = await get_market_data(session)
            if not data: continue
            current_symbols = {s['symbol'] for s in data if s.get('symbol','').endswith('USDT')}
            if not known_symbols: known_symbols = current_symbols; continue
            
            newly_listed = current_symbols - known_symbols
            if newly_listed:
                for symbol in newly_listed:
                    logger.info(f"Sniper: NEW LISTING: {symbol}")
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🎯 **إدراج جديد:** `${symbol}`", parse_mode=ParseMode.MARKDOWN)
                known_symbols.update(newly_listed)
        except Exception as e:
            logger.error(f"Error in new_listings_sniper_loop: {e}")

async def monitor_active_hunts_loop(session: aiohttp.ClientSession):
    logger.info("Active Hunts Monitor started.")
    while True:
        await asyncio.sleep(60)
        now = datetime.now(UTC)
        for symbol in list(active_hunts.keys()):
            if now - active_hunts[symbol]['alert_time'] > timedelta(hours=2):
                del active_hunts[symbol]
                logger.info(f"MONITORING STOPPED for {symbol} (timeout).")
                continue
            try:
                klines = await get_klines(session, symbol, '5m', 3)
                if not klines or len(klines) < 3: continue
                last_c, prev_c = klines[-1], klines[-2]
                is_last_red = float(last_c[4]) < float(last_c[1])
                is_prev_red = float(prev_c[4]) < float(prev_c[1])
                
                if is_last_red and is_prev_red:
                    reason = "شمعتان حمراوان متتاليتان"
                    if float(last_c[5]) < float(prev_c[5]): reason += " مع انخفاض السيولة"
                    send_weakness_alert(symbol, reason, float(last_c[4]))
                    del active_hunts[symbol]
            except Exception as e:
                logger.error(f"Error monitoring {symbol}: {e}")

def send_weakness_alert(symbol, reason, current_price):
    msg = (f"⚠️ **تحذير ضعف الزخم: ${symbol.replace('USDT', '')}** ⚠️\n\n"
           f"- السبب: `{reason}`\n"
           f"- السعر: `${format_price(current_price)}`")
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)
    logger.info(f"WEAKNESS ALERT for {symbol}. Reason: {reason}")

async def performance_tracker_loop(session: aiohttp.ClientSession):
    logger.info("Performance Tracker started.")
    while True:
        await asyncio.sleep(RUN_PERFORMANCE_TRACKER_EVERY_MINUTES * 60)
        now = datetime.now(UTC)
        for symbol in list(performance_tracker.keys()):
            data = performance_tracker[symbol]
            if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                data['status'] = 'Archived'
                logger.info(f"PERFORMANCE TRACKING ARCHIVED for {symbol}.")
                continue
            if data['status'] == 'Archived':
                if symbol in performance_tracker: del performance_tracker[symbol]
                continue
            
            price = await get_current_price(session, symbol)
            if price:
                data['current_price'] = price
                if price > data['high_price']: data['high_price'] = price

async def get_performance_report(context, chat_id, msg_id, session):
    try:
        if not performance_tracker:
            context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="ℹ️ لا توجد عملات قيد تتبع الأداء حالياً."); return

        symbols_to_update = [s for s, d in performance_tracker.items() if d.get('status') == 'Tracking']
        prices = await asyncio.gather(*[get_current_price(session, s) for s in symbols_to_update])
        for i, symbol in enumerate(symbols_to_update):
            if prices[i]:
                performance_tracker[symbol]['current_price'] = prices[i]
                if prices[i] > performance_tracker[symbol]['high_price']:
                    performance_tracker[symbol]['high_price'] = prices[i]

        sorted_symbols = sorted(performance_tracker.items(), key=lambda item: item[1]['alert_time'], reverse=True)
        msg = "📊 **تقرير أداء العملات المرصودة** 📊\n\n"
        for symbol, data in sorted_symbols:
            if data['status'] == 'Archived': continue
            alert_p, current_p, high_p = data['alert_price'], data['current_price'], data['high_price']
            curr_chg = ((current_p - alert_p) / alert_p) * 100 if alert_p > 0 else 0
            peak_chg = ((high_p - alert_p) / alert_p) * 100 if alert_p > 0 else 0
            
            time_since_alert = datetime.now(UTC) - data['alert_time']
            hours, remainder = divmod(time_since_alert.total_seconds(), 3600)
            minutes, _ = divmod(remainder, 60)
            time_str = f"{int(hours)} س و {int(minutes)} د"

            msg += (f"{'🟢' if curr_chg >= 0 else '🔴'} **${symbol.replace('USDT','')}** (منذ {time_str})\n"
                    f"   - سعر التنبيه: `${format_price(alert_p)}`\n"
                    f"   - الحالي: `${format_price(current_p)}` (**{curr_chg:+.2f}%**)\n"
                    f"   - الأعلى: `${format_price(high_p)}` (**{peak_chg:+.2f}%**)\n\n")
        context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_performance_report: {e}")
        context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="حدث خطأ.")

# =============================================================================
# 6. تشغيل البوت
# =============================================================================
def send_startup_message():
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="✅ **بوت التداول (v11.2) متصل!**", parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

async def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN:
        logger.critical("FATAL ERROR: Bot token is not set."); return
    
    async with aiohttp.ClientSession() as session:
        updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
        dp = updater.dispatcher
        loop = asyncio.get_running_loop()
        dp.bot_data.update({'loop': loop, 'session': session})

        # --- Handlers ---
        dp.add_handler(CommandHandler("start", start_command))
        dp.add_handler(MessageHandler(Filters.regex(re.compile(r'^[A-Z0-9]{2,10}$', re.IGNORECASE)), verifier_command_handler))
        
        # THIS IS THE FIX: Correctly list the button texts for the filter
        button_texts = [
            BTN_MOMENTUM, BTN_GAINERS, BTN_LOSERS, BTN_VOLUME,
            BTN_STATUS, BTN_PERFORMANCE
        ]
        dp.add_handler(MessageHandler(Filters.text(button_texts), handle_button_press))

        tasks = [
            run_websocket_client(),
            periodic_activity_checker(),
            fomo_hunter_loop(session),
            new_listings_sniper_loop(session),
            monitor_active_hunts_loop(session),
            performance_tracker_loop(session),
        ]
        
        loop.run_in_executor(None, updater.start_polling)
        logger.info("Bot is polling...")
        send_startup_message()
        await asyncio.gather(*[asyncio.create_task(task) for task in tasks])

if __name__ == '__main__':
    try:
        import websockets
    except ImportError:
        print("Please install required libraries: pip install python-telegram-bot aiohttp websockets")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")