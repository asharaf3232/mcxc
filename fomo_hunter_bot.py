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

# --- Pattern Hunter Settings ---
RUN_PATTERN_SCAN_EVERY_HOURS = 1
PATTERN_SIGHTING_THRESHOLD = 3
PATTERN_LOOKBACK_DAYS = 7

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
# Using sets for faster lookups
recently_alerted_fomo = {}
recently_alerted_instant = {}
recently_alerted_pattern = {}
known_symbols = set()
pattern_tracker = {}
active_hunts = {}

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
            logger.info("WebSocket: Pong received.")
            return

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
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"Error processing WebSocket message: {e}")

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
                        logger.info("WebSocket: Ping sent.")
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}. Reconnecting in 10 seconds...")
            await asyncio.sleep(10)

async def periodic_activity_checker():
    """Periodically checks the tracked activity from WebSocket for instant alerts."""
    logger.info("Activity checker background task started.")
    while True:
        await asyncio.sleep(5)
        now_ts = datetime.now(UTC).timestamp()

        # Cleanup old alerts first
        for sym in list(recently_alerted_instant.keys()):
            if now_ts - recently_alerted_instant[sym] > COOLDOWN_PERIOD_HOURS * 3600:
                del recently_alerted_instant[sym]

        symbols_to_check = list(activity_tracker.keys())
        async with activity_lock:
            for symbol in symbols_to_check:
                if symbol not in activity_tracker: continue

                trades = activity_tracker[symbol]
                # Efficiently remove old trades from the left of the deque
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
                    del activity_tracker[symbol] # Clear after alerting to prevent re-triggering

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

# --- Keyboard Buttons ---
BTN_MOMENTUM = "🚀 كاشف الزخم (فائق السرعة)"
BTN_GAINERS = "📈 الأكثر ارتفاعاً"
BTN_LOSERS = "📉 الأكثر انخفاضاً"
BTN_VOLUME = "💰 الأعلى سيولة"

def build_menu():
    keyboard = [[BTN_MOMENTUM], [BTN_GAINERS, BTN_LOSERS], [BTN_VOLUME]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def start_command(update, context):
    welcome_message = ("✅ **بوت التداول الذكي (v7) جاهز!**\n\n"
                       "- تم تحديث البنية التحتية بالكامل لتعمل بشكل غير متزامن (`async`) لسرعة وكفاءة أعلى.\n"
                       "- **الراصد اللحظي** يعمل الآن. تحقق من حالته عبر الأمر /status.\n"
                       "- **مساعد إدارة الصفقات** فعال ويراقب الفرص المكتشفة.\n"
                       "- استخدم لوحة الأزرار أدناه للتحليل الفوري.")
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def status_command(update, context):
    tracked_symbols_count = len(activity_tracker)
    active_hunts_count = len(active_hunts)
    message = "📊 **حالة البوت** 📊\n\n"
    message += "**1. الراصد اللحظي (WebSocket):**\n"
    if tracked_symbols_count > 0:
        message += f"   - ✅ نشط، يتم تتبع **{tracked_symbols_count}** عملة.\n"
    else:
        message += f"   - ✅ متصل، السوق هادئ لحظياً.\n"
    message += f"\n**2. مساعد إدارة الصفقات:**\n"
    if active_hunts_count > 0:
        message += f"   - ✅ نشط، يراقب **{active_hunts_count}** فرصة حالياً."
    else:
        message += f"   - ⏳ ينتظر فرصة جديدة لمراقبتها."
    update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update, context):
    """
    Handles button presses from the user.
    This function runs in the PTB thread and safely schedules async tasks
    on the main asyncio event loop.
    """
    button_text = update.message.text
    chat_id = update.message.chat_id
    
    # Get the running asyncio loop and the shared aiohttp session from bot_data
    loop = context.bot_data['loop']
    session = context.bot_data['session']

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
    
    if task:
        asyncio.run_coroutine_threadsafe(task, loop)

async def get_top_10_list(context, chat_id, message_id, list_type, session: aiohttp.ClientSession):
    """Fetches and displays top 10 lists (gainers, losers, volume)."""
    type_map = {
        'gainers': {'key': 'priceChangePercent', 'title': '🔥 الأكثر ارتفاعاً', 'reverse': True},
        'losers': {'key': 'priceChangePercent', 'title': '📉 الأكثر انخفاضاً', 'reverse': False},
        'volume': {'key': 'quoteVolume', 'title': '💰 الأعلى سيولة', 'reverse': True}
    }
    config = type_map[list_type]
    
    try:
        data = await get_market_data(session)
        if not data:
            raise ValueError("No market data received.")
            
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        
        # Safely convert sort key to float, default to 0 if missing/invalid
        for pair in usdt_pairs:
            try:
                pair['sort_key'] = float(pair[config['key']])
            except (ValueError, KeyError):
                pair['sort_key'] = 0.0

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
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"Could not edit initial momentum message: {e}")

    market_data = await get_market_data(session)
    if not market_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق."); return

    potential_coins = [
        pair for pair in market_data
        if pair.get('symbol', '').endswith('USDT') and
           float(pair.get('lastPrice', '1')) <= MOMENTUM_MAX_PRICE and
           MOMENTUM_MIN_VOLUME_24H <= float(pair.get('quoteVolume', '0')) <= MOMENTUM_MAX_VOLUME_24H
    ]

    if not potential_coins:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لم يتم العثور على عملات ضمن المعايير الأولية."); return

    tasks = [get_klines(session, pair['symbol'], MOMENTUM_KLINE_INTERVAL, MOMENTUM_KLINE_LIMIT) for pair in potential_coins]
    all_klines_data = await asyncio.gather(*tasks)

    momentum_coins = []
    for i, klines in enumerate(all_klines_data):
        if not klines or len(klines) < MOMENTUM_KLINE_LIMIT: continue
        try:
            split_point = MOMENTUM_KLINE_LIMIT // 2
            old_klines, new_klines = klines[:split_point], klines[split_point:]
            
            old_volume = sum(float(k[5]) for k in old_klines)
            if old_volume == 0: continue

            new_volume = sum(float(k[5]) for k in new_klines)
            start_price = float(new_klines[0][1])
            end_price = float(new_klines[-1][4])
            if start_price == 0: continue

            price_change = ((end_price - start_price) / start_price) * 100
            
            if new_volume > old_volume * MOMENTUM_VOLUME_INCREASE and price_change > MOMENTUM_PRICE_INCREASE:
                momentum_coins.append({
                    'symbol': potential_coins[i]['symbol'],
                    'price_change': price_change,
                    'current_price': end_price
                })
        except (ValueError, IndexError) as e:
            logger.warning(f"Could not process klines for {potential_coins[i]['symbol']}: {e}")
            continue
    
    if not momentum_coins:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **الفحص السريع اكتمل:** لا يوجد زخم حقيقي حالياً."); return
        
    sorted_coins = sorted(momentum_coins, key=lambda x: x['price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم الفوري - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\nأفضل الأهداف التي تظهر بداية زخم الآن:\n\n"
    for i, coin in enumerate(sorted_coins[:10]):
        message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n"
                    f"   - السعر: `${format_price(coin['current_price'])}`\n"
                    f"   - **زخم آخر 30 دقيقة: `%{coin['price_change']:+.2f}`**\n\n")
    
    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

    # Add detected coins to the active hunt monitor
    now = datetime.now(UTC)
    for coin in sorted_coins[:10]:
        symbol = coin['symbol']
        if symbol not in active_hunts:
             active_hunts[symbol] = {
                'alert_price': float(coin['current_price']),
                'alert_time': now
            }
             logger.info(f"MONITORING STARTED for (manual) {symbol}")


# =============================================================================
# 4. المهام الآلية الدورية
# =============================================================================
# --- 4. Automated Periodic Tasks ---

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
                try:
                    pair['priceChangePercent_float'] = float(pair.get('priceChangePercent', 0))
                except (ValueError, KeyError):
                    pair['priceChangePercent_float'] = 0.0

            potential_coins = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=True)[:TOP_GAINERS_CANDIDATE_LIMIT]
            
            for pair_data in potential_coins:
                symbol = pair_data['symbol']
                if symbol in recently_alerted_fomo and (now - recently_alerted_fomo[symbol]) < timedelta(hours=COOLDOWN_PERIOD_HOURS):
                    continue

                alert_data = await analyze_fomo_symbol(session, symbol)
                if alert_data:
                    message = (f"🚨 **تنبيه فومو آلي: يا أشرف انتبه!** 🚨\n\n"
                               f"**العملة:** `${alert_data['symbol']}`\n"
                               f"**منصة:** `MEXC`\n\n"
                               f"📈 *زيادة حجم التداول:* `{alert_data['volume_increase']}`\n"
                               f"🕯️ *نمط السعر:* `{alert_data['price_pattern']}`\n"
                               f"💰 *السعر الحالي:* `{alert_data['current_price']}` USDT\n\n"
                               f"*(تحذير: هذا تنبيه آلي. قم ببحثك الخاص.)*")
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                    logger.info(f"Fomo alert sent for {alert_data['symbol']}")
                    recently_alerted_fomo[symbol] = now
                    if symbol not in active_hunts:
                        active_hunts[symbol] = {
                            'alert_price': float(alert_data['current_price']),
                            'alert_time': now
                        }
                        logger.info(f"MONITORING STARTED for (auto) {symbol}")
                    await asyncio.sleep(1) # Small delay to avoid rate limiting
        
        except Exception as e:
            logger.error(f"An unexpected error occurred in fomo_hunter_loop: {e}")
        
        logger.info("===== Fomo Hunter: Scan Finished =====")
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)

async def analyze_fomo_symbol(session: aiohttp.ClientSession, symbol: str):
    """Analyzes a single symbol based on FOMO criteria."""
    try:
        daily_data = await get_klines(session, symbol, '1d', 2)
        if not daily_data or len(daily_data) < 2: return None

        previous_day_volume = float(daily_data[0][7])
        current_day_volume = float(daily_data[1][7])

        if not (current_day_volume > MIN_USDT_VOLUME and current_day_volume > (previous_day_volume * VOLUME_SPIKE_MULTIPLIER)):
            return None
        
        hourly_data = await get_klines(session, symbol, '1h', 4)
        if not hourly_data or len(hourly_data) < 4: return None

        initial_price = float(hourly_data[0][1])
        latest_high_price = float(hourly_data[-1][2])
        if initial_price == 0: return None

        price_increase_percent = ((latest_high_price - initial_price) / initial_price) * 100
        if not (price_increase_percent >= PRICE_VELOCITY_THRESHOLD):
            return None

        # Fetch the very latest price for the alert
        price_data = await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/price", {'symbol': symbol})
        if not price_data: return None
        current_price = float(price_data['price'])

        volume_increase_percent = ((current_day_volume - previous_day_volume) / previous_day_volume) * 100 if previous_day_volume > 0 else float('inf')

        return {
            'symbol': symbol,
            'volume_increase': f"+{volume_increase_percent:,.2f}%",
            'price_pattern': f"صعود بنسبة +{price_increase_percent:,.2f}% في آخر 4 ساعات",
            'current_price': format_price(current_price)
        }
    except (KeyError, IndexError, ValueError) as e:
        logger.warning(f"Data parsing error during FOMO analysis for {symbol}: {e}")
        return None


async def new_listings_sniper_loop(session: aiohttp.ClientSession):
    """Periodically checks for new USDT pairs."""
    global known_symbols
    logger.info("New Listings Sniper background task started.")
    
    # Initial population
    initial_data = await get_market_data(session)
    if initial_data:
        known_symbols = {s['symbol'] for s in initial_data if s.get('symbol','').endswith('USDT')}
        logger.info(f"Sniper: Initialized with {len(known_symbols)} USDT symbols.")
    else:
        logger.warning("Sniper: Could not initialize symbols, will retry.")

    while True:
        try:
            data = await get_market_data(session)
            if not data:
                await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS)
                continue

            current_symbols = {s['symbol'] for s in data if s.get('symbol','').endswith('USDT')}
            
            # If known_symbols is empty (initial fetch failed), populate it now
            if not known_symbols:
                known_symbols = current_symbols
                logger.info(f"Sniper: Re-initialized with {len(known_symbols)} symbols.")
                continue

            newly_listed = current_symbols - known_symbols
            if newly_listed:
                for symbol in newly_listed:
                    logger.info(f"Sniper: NEW LISTING DETECTED: {symbol}")
                    message = (f"🎯 **تنبيه قناص: إدراج جديد!** 🎯\n\n"
                               f"تم للتو إدراج عملة جديدة على منصة MEXC:\n\n"
                               f"**العملة:** `${symbol}`")
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                known_symbols.update(newly_listed)
        except Exception as e:
            logger.error(f"An unexpected error occurred in new_listings_sniper_loop: {e}")
        
        await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS)

# Other periodic tasks like 'pattern_hunter' and 'monitor_active_hunts' can be converted similarly.
# For brevity, I'll add the hunt monitor as it's a key feature.

async def monitor_active_hunts_loop(session: aiohttp.ClientSession):
    """Periodically checks monitored symbols for signs of weakness."""
    logger.info("Active Hunts Monitor background task started.")
    while True:
        logger.info(f"Active Hunts Monitor: Checking {len(active_hunts)} active hunt(s)...")
        now = datetime.now(UTC)
        
        # Create a copy of keys to safely delete from the original dict while iterating
        for symbol in list(active_hunts.keys()):
            # Timeout check
            if now - active_hunts[symbol]['alert_time'] > timedelta(hours=2):
                del active_hunts[symbol]
                logger.info(f"MONITORING STOPPED for {symbol} (timeout).")
                continue
            
            try:
                klines = await get_klines(session, symbol, '5m', 2)
                if not klines or len(klines) < 2: continue
                
                last_candle = klines[-1]
                open_price, close_price = float(last_candle[1]), float(last_candle[4])
                
                if close_price < open_price:
                    price_drop_percent = ((close_price - open_price) / open_price) * 100 if open_price > 0 else 0
                    if price_drop_percent <= -3.0:
                        send_weakness_alert(symbol, "شمعة حمراء قوية (5 دقائق)", close_price)
                        del active_hunts[symbol]
            except Exception as e:
                logger.error(f"Error monitoring {symbol}: {e}")
                
        await asyncio.sleep(60) # Check every minute

def send_weakness_alert(symbol, reason, current_price):
    message = (f"⚠️ **تحذير: الزخم في ${symbol.replace('USDT', '')} بدأ يضعف!** ⚠️\n\n"
               f"- **تم رصد:** `{reason}`\n"
               f"- **السعر الحالي:** `${format_price(current_price)}`\n\n"
               f"قد يكون هذا مؤشراً على بداية انعكاس الاتجاه.")
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
    logger.info(f"WEAKNESS ALERT sent for {symbol}. Reason: {reason}")


# =============================================================================
# 5. تشغيل البوت
# =============================================================================
# --- 5. Bot Startup and Main Execution ---

def send_startup_message():
    try:
        message = "✅ **بوت التداول الذكي (v7 - Async) متصل الآن!**\n\nأرسل /start لعرض القائمة."
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

async def main():
    """Main function to initialize and run all async tasks."""
    # Strict check for credentials
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set in your environment variables.")
        return

    # Create a single aiohttp session for the application's lifetime
    async with aiohttp.ClientSession() as session:
        # --- Initialize Telegram Bot ---
        updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
        dp = updater.dispatcher

        # Share the asyncio loop and session with the bot handlers
        loop = asyncio.get_running_loop()
        dp.bot_data['loop'] = loop
        dp.bot_data['session'] = session

        # Add handlers
        dp.add_handler(CommandHandler("start", start_command))
        dp.add_handler(CommandHandler("status", status_command))
        dp.add_handler(MessageHandler(Filters.text([BTN_MOMENTUM, BTN_GAINERS, BTN_LOSERS, BTN_VOLUME]), handle_button_press))

        # --- Create and run all background tasks ---
        tasks = [
            asyncio.create_task(run_websocket_client()),
            asyncio.create_task(periodic_activity_checker()),
            asyncio.create_task(fomo_hunter_loop(session)),
            asyncio.create_task(new_listings_sniper_loop(session)),
            asyncio.create_task(monitor_active_hunts_loop(session)),
        ]

        # Run the polling in a separate thread so it doesn't block the event loop
        loop.run_in_executor(None, updater.start_polling)
        logger.info("Telegram bot is now polling for commands and messages...")
        
        send_startup_message()

        # Keep the main function alive to run the tasks
        await asyncio.gather(*tasks)

if __name__ == '__main__':
    try:
        import websockets
        # This will be used by the WebSocket client
    except ImportError:
        print("Please install required libraries: pip install python-telegram-bot aiohttp websockets")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")
