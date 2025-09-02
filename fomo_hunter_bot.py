# -*- coding: utf-8 -*-
import os
import asyncio
import json
import logging
import aiohttp
from datetime import datetime, timedelta, UTC
from collections import deque
from telegram import Bot, ParseMode, ReplyKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters

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
TOP_GAINERS_CANDIDATE_LIMIT = 200 # How many top gainers to analyze in each scan

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
MOMENTUM_KLINE_LIMIT = 12 # Represents 1 hour of data with a 5m interval

# --- Advanced Settings ---
MEXC_API_BASE_URL = "https://api.mexc.com"
MEXC_WS_URL = "wss://wbs.mexc.com/ws"
COOLDOWN_PERIOD_HOURS = 2
HTTP_TIMEOUT = 15

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة وتهيئة البوت ---
# =============================================================================
# --- Global Variables & Bot Initialization ---
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# --- State Management ---
recently_alerted_fomo = {}
recently_alerted_instant = {}
known_symbols = set()
active_hunts = {}
performance_tracker = {} # NEW: For tracking coin performance after alerts

# This deque is shared between the WebSocket thread and the checker thread
activity_tracker = {}
activity_lock = asyncio.Lock() # Using asyncio.Lock for async context

# =============================================================================
# 1. قسم الشبكة والوظائف الأساسية (Async)
# =============================================================================
# --- 1. Core Networking and Helper Functions (Async) ---

async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None):
    """A robust wrapper for making GET requests and parsing JSON with aiohttp."""
    try:
        async with session.get(url, params=params, timeout=HTTP_TIMEOUT, headers={'User-Agent': 'Mozilla/5.0'}) as response:
            response.raise_for_status()
            return await response.json()
    except aiohttp.ClientError as e:
        logger.error(f"AIOHTTP error fetching {url}: {e}")
    except asyncio.TimeoutError:
        logger.error(f"Timeout error fetching {url}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error for {url}: {e}")
    return None

async def get_market_data(session: aiohttp.ClientSession):
    """Fetches 24hr ticker data for all symbols."""
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr")

async def get_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int):
    """Fetches k-line (candlestick) data for a specific symbol."""
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/klines", params=params)

async def get_current_price(session: aiohttp.ClientSession, symbol: str) -> float | None:
    """Fetches the latest price for a single symbol."""
    data = await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/price", {'symbol': symbol})
    if data and 'price' in data:
        return float(data['price'])
    return None

def format_price(price_str):
    """Safely formats a price string to a readable format, removing trailing zeros."""
    try:
        price_float = float(price_str)
        # Use 'g' for general format which automatically handles decimal points
        return f"{price_float:.8g}"
    except (ValueError, TypeError):
        return price_str

# =============================================================================
# 2. قسم الرصد اللحظي (WebSocket)
# =============================================================================
# --- 2. Real-time Monitoring Section (WebSocket) ---

async def handle_websocket_message(message):
    """Processes incoming WebSocket messages."""
    try:
        data = json.loads(message)
        if "method" in data and data["method"] == "PONG":
            return # No need to log pongs, they are frequent

        if 'd' in data and 'e' in data['d'] and data['d']['e'] == 'spot@public.deals.v3.api':
            symbol = data['s']
            for deal in data['d']['D']:
                if deal['S'] == 1:  # Side 1 is for BUY
                    volume_usdt = float(deal['p']) * float(deal['q'])
                    timestamp = float(deal['t']) / 1000.0
                    async with activity_lock:
                        if symbol not in activity_tracker:
                            activity_tracker[symbol] = deque(maxlen=200)
                        activity_tracker[symbol].append({'v': volume_usdt, 't': timestamp})
    except (json.JSONDecodeError, KeyError, ValueError):
        pass # Ignore minor processing errors from the stream

async def run_websocket_client():
    """Manages the WebSocket connection and subscription."""
    logger.info("Starting WebSocket client...")
    subscription_msg = {"method": "SUBSCRIPTION", "params": ["spot@public.deals.v3.api"]}
    while True:
        try:
            async with websockets.connect(MEXC_WS_URL) as websocket:
                await websocket.send(json.dumps(subscription_msg))
                logger.info("Successfully connected and subscribed to MEXC WebSocket.")
                while True:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=20.0)
                        await handle_websocket_message(message)
                    except asyncio.TimeoutError:
                        await websocket.send(json.dumps({"method": "PING"}))
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}. Reconnecting in 10 seconds...")
            await asyncio.sleep(10)

async def periodic_activity_checker():
    """Periodically checks the tracked activity from WebSocket for instant alerts."""
    logger.info("Activity checker background task started.")
    while True:
        await asyncio.sleep(5)
        now_ts = datetime.now(UTC).timestamp()

        for sym in list(recently_alerted_instant.keys()):
            if now_ts - recently_alerted_instant[sym] > COOLDOWN_PERIOD_HOURS * 3600:
                del recently_alerted_instant[sym]

        symbols_to_check = list(activity_tracker.keys())
        async with activity_lock:
            for symbol in symbols_to_check:
                if symbol not in activity_tracker: continue
                trades = activity_tracker[symbol]
                while trades and now_ts - trades[0]['t'] > INSTANT_TIMEFRAME_SECONDS:
                    trades.popleft()
                if not trades:
                    del activity_tracker[symbol]
                    continue

                total_volume = sum(trade['v'] for trade in trades)
                trade_count = len(trades)

                if (total_volume >= INSTANT_VOLUME_THRESHOLD_USDT and
                        trade_count >= INSTANT_TRADE_COUNT_THRESHOLD and
                        symbol not in recently_alerted_instant):
                    
                    send_instant_alert(symbol, total_volume, trade_count)
                    recently_alerted_instant[symbol] = now_ts
                    del activity_tracker[symbol]

def send_instant_alert(symbol, total_volume, trade_count):
    """Sends a formatted alert for instant buy activity."""
    message = (f"⚡️ **رصد نشاط شراء مفاجئ! (لحظي)** ⚡️\n\n"
               f"**العملة:** `${symbol}`\n"
               f"**حجم الشراء (آخر {INSTANT_TIMEFRAME_SECONDS} ثوانٍ):** `${total_volume:,.0f} USDT`\n"
               f"**عدد الصفقات (آخر {INSTANT_TIMEFRAME_SECONDS} ثوانٍ):** `{trade_count}`\n\n"
               f"*(إشارة مبكرة جداً وعالية المخاطر)*")
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"INSTANT ALERT sent for {symbol}.")
    except Exception as e:
        logger.error(f"Failed to send instant alert for {symbol}: {e}")

# =============================================================================
# 3. الوظائف التفاعلية (أوامر البوت)
# =============================================================================
# --- 3. Interactive Functions (Bot Commands) ---

BTN_MOMENTUM = "🚀 كاشف الزخم (فائق السرعة)"
BTN_GAINERS = "📈 الأكثر ارتفاعاً"
BTN_LOSERS = "📉 الأكثر انخفاضاً"
BTN_VOLUME = "💰 الأعلى سيولة"
BTN_STATUS = "📊 الحالة"
BTN_PERFORMANCE = "📈 تقرير الأداء"


def build_menu():
    keyboard = [
        [BTN_MOMENTUM],
        [BTN_GAINERS, BTN_LOSERS, BTN_VOLUME],
        [BTN_STATUS, BTN_PERFORMANCE]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def start_command(update, context):
    welcome_message = ("✅ **بوت التداول الذكي (v10) جاهز!**\n\n"
                       "**تحسينات رئيسية:**\n"
                       "- تم تحسين `تحذير ضعف الزخم` ليصبح أكثر دقة ويتجنب الإنذارات الكاذبة.\n"
                       "- استخدم زر `📈 تقرير الأداء` لتقييم أداء الإشارات.\n\n"
                       "- جميع الأوامر متاحة الآن عبر لوحة الأزرار.")
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def status_command(update, context):
    tracked_symbols_count = len(activity_tracker)
    active_hunts_count = len(active_hunts)
    performance_tracked_count = len(performance_tracker)
    message = "📊 **حالة البوت** 📊\n\n"
    message += f"**1. الراصد اللحظي (WebSocket):**\n"
    message += f"   - ✅ متصل، {'يتم تتبع ' + str(tracked_symbols_count) + ' عملة' if tracked_symbols_count > 0 else 'السوق هادئ'}.\n"
    message += f"\n**2. مساعد إدارة الصفقات:**\n"
    message += f"   - ✅ {'نشط، يراقب ' + str(active_hunts_count) + ' فرصة' if active_hunts_count > 0 else 'ينتظر فرصة جديدة'}.\n"
    message += f"\n**3. متتبع الأداء:**\n"
    message += f"   - ✅ {'نشط، يراقب أداء ' + str(performance_tracked_count) + ' عملة' if performance_tracked_count > 0 else 'ينتظر تنبيهات جديدة لتتبعها'}."
    update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update, context):
    """Handles all button presses from the main keyboard."""
    button_text = update.message.text
    chat_id = update.message.chat_id
    loop = context.bot_data['loop']
    session = context.bot_data['session']

    # Handle synchronous commands directly without a "loading" message
    if button_text == BTN_STATUS:
        status_command(update, context)
        return

    # Handle asynchronous commands with a "loading" message
    sent_message = context.bot.send_message(chat_id=chat_id, text="🔍 جارِ تنفيذ طلبك...")
    
    task = None
    if button_text == BTN_GAINERS:
        task = get_top_10_list(context, chat_id, sent_message.message_id, 'gainers', session)
    elif button_text == BTN_LOSERS:
        task = get_top_10_list(context, chat_id, sent_message.message_id, 'losers', session)
    elif button_text == BTN_VOLUME:
        task = get_top_10_list(context, chat_id, sent_message.message_id, 'volume', session)
    elif button_text == BTN_MOMENTUM:
        task = run_momentum_detector(context, chat_id, sent_message.message_id, session)
    elif button_text == BTN_PERFORMANCE:
        task = get_performance_report(context, chat_id, sent_message.message_id, session)

    if task:
        asyncio.run_coroutine_threadsafe(task, loop)

async def get_top_10_list(context, chat_id, message_id, list_type, session: aiohttp.ClientSession):
    """Fetches and displays top 10 lists."""
    type_map = {
        'gainers': {'key': 'priceChangePercent', 'title': '🔥 الأكثر ارتفاعاً', 'reverse': True},
        'losers': {'key': 'priceChangePercent', 'title': '📉 الأكثر انخفاضاً', 'reverse': False},
        'volume': {'key': 'quoteVolume', 'title': '💰 الأعلى سيولة', 'reverse': True}
    }
    config = type_map[list_type]
    try:
        data = await get_market_data(session)
        if not data: raise ValueError("No market data received.")
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs:
            pair['sort_key'] = float(pair.get(config['key'], 0.0))
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['sort_key'], reverse=config['reverse'])
        
        message = f"**{config['title']} في آخر 24 ساعة**\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            value = pair['sort_key']
            value_str = f"{value * 100:+.2f}%" if list_type != 'volume' else f"${value:,.0f}"
            message += (f"{i+1}. **${pair['symbol'].replace('USDT', '')}**\n"
                        f"   - {'النسبة' if list_type != 'volume' else 'حجم التداول'}: `{value_str}`\n"
                        f"   - السعر الحالي: `${format_price(pair.get('lastPrice', 'N/A'))}`\n\n")
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_top_10_list for {list_type}: {e}")
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ أثناء جلب البيانات.")

async def run_momentum_detector(context, chat_id, message_id, session: aiohttp.ClientSession):
    """The core logic for the on-demand momentum detector."""
    initial_text = "🚀 **كاشف الزخم (فائق السرعة)**\n\n🔍 جارِ الفحص المتوازي للسوق..."
    try:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    market_data = await get_market_data(session)
    if not market_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق."); return

    potential_coins = [p for p in market_data if p.get('symbol','').endswith('USDT') and
                       float(p.get('lastPrice','1')) <= MOMENTUM_MAX_PRICE and
                       MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= MOMENTUM_MAX_VOLUME_24H]

    if not potential_coins:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لم يتم العثور على عملات ضمن المعايير الأولية."); return

    tasks = [get_klines(session, p['symbol'], MOMENTUM_KLINE_INTERVAL, MOMENTUM_KLINE_LIMIT) for p in potential_coins]
    all_klines_data = await asyncio.gather(*tasks)

    momentum_coins = []
    for i, klines in enumerate(all_klines_data):
        if not klines or len(klines) < MOMENTUM_KLINE_LIMIT: continue
        try:
            sp = MOMENTUM_KLINE_LIMIT // 2
            old_v = sum(float(k[5]) for k in klines[:sp])
            if old_v == 0: continue
            new_v = sum(float(k[5]) for k in klines[sp:])
            start_p = float(klines[sp][1])
            if start_p == 0: continue
            end_p = float(klines[-1][4])
            price_change = ((end_p - start_p) / start_p) * 100
            
            if new_v > old_v * MOMENTUM_VOLUME_INCREASE and price_change > MOMENTUM_PRICE_INCREASE:
                momentum_coins.append({'symbol': potential_coins[i]['symbol'], 'price_change': price_change,
                                       'current_price': end_p, 'peak_volume': new_v})
        except (ValueError, IndexError): continue
    
    if not momentum_coins:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **الفحص السريع اكتمل:** لا يوجد زخم حقيقي حالياً."); return
        
    sorted_coins = sorted(momentum_coins, key=lambda x: x['price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم الفوري - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\n"
    for i, coin in enumerate(sorted_coins[:10]):
        message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n"
                    f"   - السعر: `${format_price(coin['current_price'])}`\n"
                    f"   - **زخم آخر 30 دقيقة: `%{coin['price_change']:+.2f}`**\n\n")
    message += "*(تمت إضافة هذه العملات إلى متتبع الأداء.)*"
    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

    now = datetime.now(UTC)
    for coin in sorted_coins[:10]:
        add_to_monitoring(coin['symbol'], float(coin['current_price']), coin.get('peak_volume', 0), now, "الزخم اليدوي")

# =============================================================================
# 4. المهام الآلية الدورية
# =============================================================================
# --- 4. Automated Periodic Tasks ---

def add_to_monitoring(symbol, alert_price, peak_volume, alert_time, source):
    """A centralized function to add a coin to active hunts and performance tracker."""
    if symbol not in active_hunts:
        active_hunts[symbol] = {
            'alert_price': alert_price,
            'alert_time': alert_time,
            'peak_volume': peak_volume
        }
        logger.info(f"MONITORING STARTED for ({source}) {symbol}")
    
    if symbol not in performance_tracker:
        performance_tracker[symbol] = {
            'alert_price': alert_price,
            'alert_time': alert_time,
            'source': source,
            'current_price': alert_price,
            'high_price': alert_price,
            'status': 'Tracking'
        }
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol}")

async def fomo_hunter_loop(session: aiohttp.ClientSession):
    """Main async loop for the Fomo Hunter job."""
    logger.info("Fomo Hunter background task started.")
    while True:
        logger.info("===== Fomo Hunter: Starting Scan =====")
        now = datetime.now(UTC)
        try:
            market_data = await get_market_data(session)
            if not market_data:
                await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)
                continue

            usdt_pairs = [s for s in market_data if s.get('symbol','').endswith('USDT')]
            for pair in usdt_pairs:
                pair['priceChangePercent_float'] = float(pair.get('priceChangePercent', 0))
            potential_coins = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=True)[:TOP_GAINERS_CANDIDATE_LIMIT]
            
            for pair_data in potential_coins:
                symbol = pair_data['symbol']
                if symbol in recently_alerted_fomo and (now - recently_alerted_fomo[symbol]) < timedelta(hours=COOLDOWN_PERIOD_HOURS):
                    continue

                alert_data = await analyze_fomo_symbol(session, symbol)
                if alert_data:
                    message = (f"🚨 **تنبيه فومو آلي** 🚨\n\n"
                               f"**العملة:** `${alert_data['symbol']}`\n\n"
                               f"📈 *زيادة حجم التداول:* `{alert_data['volume_increase']}`\n"
                               f"🕯️ *نمط السعر:* `{alert_data['price_pattern']}`\n"
                               f"💰 *السعر الحالي:* `{alert_data['current_price']}` USDT")
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                    logger.info(f"Fomo alert sent for {alert_data['symbol']}")
                    recently_alerted_fomo[symbol] = now
                    add_to_monitoring(symbol, float(alert_data['current_price']), alert_data.get('peak_volume', 0), now, "فومو آلي")
                    await asyncio.sleep(1)
        
        except Exception as e:
            logger.error(f"An unexpected error in fomo_hunter_loop: {e}")
        
        logger.info("===== Fomo Hunter: Scan Finished =====")
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)

async def analyze_fomo_symbol(session: aiohttp.ClientSession, symbol: str):
    """Analyzes a single symbol based on FOMO criteria."""
    try:
        daily_data = await get_klines(session, symbol, '1d', 2)
        if not daily_data or len(daily_data) < 2: return None

        prev_vol = float(daily_data[0][7])
        curr_vol = float(daily_data[1][7])

        if not (curr_vol > MIN_USDT_VOLUME and curr_vol > (prev_vol * VOLUME_SPIKE_MULTIPLIER)):
            return None
        
        hourly_data = await get_klines(session, symbol, '1h', 4)
        if not hourly_data or len(hourly_data) < 4: return None

        initial_price = float(hourly_data[0][1])
        if initial_price == 0: return None
        price_increase_percent = ((float(hourly_data[-1][2]) - initial_price) / initial_price) * 100
        if not (price_increase_percent >= PRICE_VELOCITY_THRESHOLD):
            return None

        current_price = await get_current_price(session, symbol)
        if not current_price: return None

        vol_increase_percent = ((curr_vol - prev_vol) / prev_vol) * 100 if prev_vol > 0 else float('inf')
        last_hour_kline = await get_klines(session, symbol, '1h', 1)
        peak_volume = float(last_hour_kline[0][5]) if last_hour_kline else 0

        return {'symbol': symbol, 'volume_increase': f"+{vol_increase_percent:,.2f}%",
                'price_pattern': f"صعود +{price_increase_percent:,.2f}% في 4 ساعات",
                'current_price': format_price(current_price), 'peak_volume': peak_volume}
    except (KeyError, IndexError, ValueError) as e:
        logger.warning(f"Data parsing error during FOMO analysis for {symbol}: {e}")
        return None

async def new_listings_sniper_loop(session: aiohttp.ClientSession):
    """Periodically checks for new USDT pairs."""
    global known_symbols
    logger.info("New Listings Sniper background task started.")
    
    initial_data = await get_market_data(session)
    if initial_data:
        known_symbols = {s['symbol'] for s in initial_data if s.get('symbol','').endswith('USDT')}
        logger.info(f"Sniper: Initialized with {len(known_symbols)} symbols.")
    else:
        logger.warning("Sniper: Could not initialize symbols.")

    while True:
        try:
            data = await get_market_data(session)
            if not data:
                await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS)
                continue

            current_symbols = {s['symbol'] for s in data if s.get('symbol','').endswith('USDT')}
            if not known_symbols:
                known_symbols = current_symbols; continue

            newly_listed = current_symbols - known_symbols
            if newly_listed:
                for symbol in newly_listed:
                    logger.info(f"Sniper: NEW LISTING DETECTED: {symbol}")
                    message = f"🎯 **إدراج جديد:** `${symbol}`"
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                known_symbols.update(newly_listed)
        except Exception as e:
            logger.error(f"An unexpected error in new_listings_sniper_loop: {e}")
        
        await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS)

async def monitor_active_hunts_loop(session: aiohttp.ClientSession):
    """Periodically checks monitored symbols for signs of weakness using a more robust pattern-based approach."""
    logger.info("Active Hunts Monitor background task started.")
    while True:
        await asyncio.sleep(60)
        now = datetime.now(UTC)
        for symbol in list(active_hunts.keys()):
            if now - active_hunts[symbol]['alert_time'] > timedelta(hours=2):
                del active_hunts[symbol]
                logger.info(f"MONITORING STOPPED for {symbol} (timeout).")
                continue
            
            try:
                # Fetch last 3 candles to check for a pattern of weakness
                klines = await get_klines(session, symbol, '5m', 3)
                if not klines or len(klines) < 3: continue
                
                last_candle = klines[-1]
                prev_candle = klines[-2]

                # Condition 1: Check for two consecutive red candles
                is_last_red = float(last_candle[4]) < float(last_candle[1])
                is_prev_red = float(prev_candle[4]) < float(prev_candle[1])

                weakness_reason = None
                if is_last_red and is_prev_red:
                    weakness_reason = "شمعتان حمراوان متتاليتان"
                    
                    # Condition 2 (Optional confirmation): Check if volume is also declining
                    last_volume = float(last_candle[5])
                    prev_volume = float(prev_candle[5])
                    if last_volume < prev_volume:
                        weakness_reason += " مع انخفاض في السيولة"
                
                if weakness_reason:
                    current_price = float(last_candle[4])
                    send_weakness_alert(symbol, weakness_reason, current_price)
                    del active_hunts[symbol]

            except Exception as e:
                logger.error(f"Error monitoring {symbol}: {e}")

def send_weakness_alert(symbol, reason, current_price):
    message = (f"⚠️ **تحذير ضعف الزخم: ${symbol.replace('USDT', '')}** ⚠️\n\n"
               f"- **السبب:** `{reason}`\n"
               f"- **السعر الحالي:** `${format_price(current_price)}`")
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
    logger.info(f"WEAKNESS ALERT sent for {symbol}. Reason: {reason}")

# --- NEW Performance Tracker ---
async def performance_tracker_loop(session: aiohttp.ClientSession):
    """Periodically updates the performance data of tracked coins."""
    logger.info("Performance Tracker background task started.")
    while True:
        await asyncio.sleep(RUN_PERFORMANCE_TRACKER_EVERY_MINUTES * 60)
        now = datetime.now(UTC)
        
        for symbol in list(performance_tracker.keys()):
            # Stop tracking after a set duration
            if now - performance_tracker[symbol]['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                performance_tracker[symbol]['status'] = 'Archived'
                logger.info(f"PERFORMANCE TRACKING ARCHIVED for {symbol}.")
                continue # Keep it in the list for one final report, then it will be removed
            
            if performance_tracker[symbol]['status'] == 'Archived':
                del performance_tracker[symbol]
                continue

            current_price = await get_current_price(session, symbol)
            if current_price:
                performance_tracker[symbol]['current_price'] = current_price
                if current_price > performance_tracker[symbol]['high_price']:
                    performance_tracker[symbol]['high_price'] = current_price

async def get_performance_report(context, chat_id, message_id, session: aiohttp.ClientSession):
    """Generates and sends the performance report."""
    try:
        if not performance_tracker:
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="ℹ️ لا توجد عملات قيد تتبع الأداء حالياً.")
            return

        # Fetch latest prices for all tracked symbols to ensure the report is up-to-the-second accurate
        symbols_to_update = [symbol for symbol, data in performance_tracker.items() if data.get('status') == 'Tracking']
        price_tasks = [get_current_price(session, symbol) for symbol in symbols_to_update]
        latest_prices = await asyncio.gather(*price_tasks)

        for i, symbol in enumerate(symbols_to_update):
            if latest_prices[i] is not None:
                performance_tracker[symbol]['current_price'] = latest_prices[i]
                if latest_prices[i] > performance_tracker[symbol]['high_price']:
                    performance_tracker[symbol]['high_price'] = latest_prices[i]

        message = "📊 **تقرير أداء العملات المرصودة** 📊\n\n"
        sorted_symbols = sorted(performance_tracker.items(), key=lambda item: item[1]['alert_time'], reverse=True)

        for symbol, data in sorted_symbols:
            if data['status'] == 'Archived': continue
            
            alert_price = data['alert_price']
            current_price = data['current_price']
            high_price = data['high_price']

            current_change = ((current_price - alert_price) / alert_price) * 100 if alert_price > 0 else 0
            peak_change = ((high_price - alert_price) / alert_price) * 100 if alert_price > 0 else 0

            emoji = "🟢" if current_change >= 0 else "🔴"
            
            time_since_alert = datetime.now(UTC) - data['alert_time']
            hours, remainder = divmod(time_since_alert.total_seconds(), 3600)
            minutes, _ = divmod(remainder, 60)
            time_str = f"{int(hours)} س و {int(minutes)} د"

            message += (f"{emoji} **${symbol.replace('USDT','')}** (منذ {time_str})\n"
                        f"   - سعر التنبيه: `${format_price(alert_price)}`\n"
                        f"   - السعر الحالي: `${format_price(current_price)}` (**{current_change:+.2f}%**)\n"
                        f"   - أعلى سعر: `${format_price(high_price)}` (**{peak_change:+.2f}%**)\n\n")

        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_performance_report: {e}")
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ أثناء جلب تقرير الأداء.")


# =============================================================================
# 5. تشغيل البوت
# =============================================================================
# --- 5. Bot Startup and Main Execution ---

def send_startup_message():
    try:
        message = "✅ **بوت التداول الذكي (v9 - واجهة تفاعلية) متصل الآن!**\n\nأرسل /start لعرض القائمة."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

async def main():
    """Main function to initialize and run all async tasks."""
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set.")
        return

    async with aiohttp.ClientSession() as session:
        updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
        dp = updater.dispatcher
        loop = asyncio.get_running_loop()
        dp.bot_data['loop'] = loop
        dp.bot_data['session'] = session

        dp.add_handler(CommandHandler("start", start_command))
        dp.add_handler(MessageHandler(Filters.text([
            BTN_MOMENTUM, BTN_GAINERS, BTN_LOSERS, BTN_VOLUME,
            BTN_STATUS, BTN_PERFORMANCE
        ]), handle_button_press))

        tasks = [
            asyncio.create_task(run_websocket_client()),
            asyncio.create_task(periodic_activity_checker()),
            asyncio.create_task(fomo_hunter_loop(session)),
            asyncio.create_task(new_listings_sniper_loop(session)),
            asyncio.create_task(monitor_active_hunts_loop(session)),
            asyncio.create_task(performance_tracker_loop(session)), # NEW TASK
        ]

        loop.run_in_executor(None, updater.start_polling)
        logger.info("Telegram bot is now polling for commands...")
        send_startup_message()
        await asyncio.gather(*tasks)

if __name__ == '__main__':
    try:
        import websockets
    except ImportError:
        print("Please install required libraries: pip install python-telegram-bot aiohttp websockets")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")

