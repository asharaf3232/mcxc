# -*- coding: utf-8 -*-
import os
import asyncio
import json
import logging
import aiohttp
import websockets
from datetime import datetime, timedelta, UTC
from collections import deque
from telegram import Bot, ParseMode, ReplyKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters

# =============================================================================
# --- الإعدادات الرئيسية ---
# =============================================================================

# --- Telegram Configuration ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# --- Whale Radar Settings ---
WHALE_WALL_THRESHOLD_USDT = 100000
# For automatic WebSocket monitoring
AUTO_WHALE_WATCH_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
# NEW: For manual scan
MANUAL_WHALE_SCAN_TOP_N = 30 # Scan top N coins by 24h volume

# --- Real-time (WebSocket) Monitoring Criteria ---
INSTANT_TIMEFRAME_SECONDS = 10
INSTANT_VOLUME_THRESHOLD_USDT = 50000
INSTANT_TRADE_COUNT_THRESHOLD = 20

# --- Periodic FOMO Scan Criteria ---
VOLUME_SPIKE_MULTIPLIER = 10
MIN_USDT_VOLUME = 500000
PRICE_VELOCITY_THRESHOLD = 30.0
RUN_FOMO_SCAN_EVERY_MINUTES = 15
TOP_GAINERS_CANDIDATE_LIMIT = 200

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

# --- State Management ---
recently_alerted_fomo = {}
recently_alerted_instant = {}
recently_alerted_whale = {}
known_symbols = set()
active_hunts = {}
performance_tracker = {}
order_books_ws = {} # For WebSocket real-time updates

# --- Locks for thread-safe operations ---
activity_lock = asyncio.Lock()
order_book_lock = asyncio.Lock()

activity_tracker = {}

# =============================================================================
# 1. قسم الشبكة والوظائف الأساسية (Async)
# =============================================================================
async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None):
    try:
        async with session.get(url, params=params, timeout=HTTP_TIMEOUT, headers={'User-Agent': 'Mozilla/5.0'}) as response:
            response.raise_for_status()
            return await response.json()
    except aiohttp.ClientError as e: logger.error(f"AIOHTTP error fetching {url}: {e}")
    except asyncio.TimeoutError: logger.error(f"Timeout error fetching {url}")
    except json.JSONDecodeError as e: logger.error(f"JSON decode error for {url}: {e}")
    return None

async def get_market_data(session: aiohttp.ClientSession):
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr")

async def get_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int):
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/klines", params=params)

async def get_current_price(session: aiohttp.ClientSession, symbol: str) -> float | None:
    data = await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/price", {'symbol': symbol})
    if data and 'price' in data: return float(data['price'])
    return None

# NEW: Function to get a snapshot of the order book via REST API
async def get_order_book(session: aiohttp.ClientSession, symbol: str, limit: int = 20):
    """Fetches a snapshot of the order book for a given symbol."""
    params = {'symbol': symbol, 'limit': limit}
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/depth", params)

def format_price(price_str):
    try:
        price_float = float(price_str)
        return f"{price_float:.8g}"
    except (ValueError, TypeError): return price_str

# =============================================================================
# 2. قسم الرصد اللحظي (WebSocket للصفقات)
# =============================================================================
async def handle_trades_message(message):
    try:
        data = json.loads(message)
        if "method" in data and data["method"] == "PONG": return
        if 'd' in data and 'e' in data['d'] and data['d']['e'] == 'spot@public.deals.v3.api':
            symbol = data['s']
            for deal in data['d']['D']:
                if deal['S'] == 1:
                    volume_usdt = float(deal['p']) * float(deal['q'])
                    timestamp = float(deal['t']) / 1000.0
                    async with activity_lock:
                        if symbol not in activity_tracker: activity_tracker[symbol] = deque(maxlen=200)
                        activity_tracker[symbol].append({'v': volume_usdt, 't': timestamp})
    except (json.JSONDecodeError, KeyError, ValueError): pass

async def run_trades_websocket_client():
    logger.info("Starting Trades WebSocket client...")
    subscription_msg = {"method": "SUBSCRIPTION", "params": ["spot@public.deals.v3.api"]}
    while True:
        try:
            async with websockets.connect(MEXC_WS_URL) as websocket:
                await websocket.send(json.dumps(subscription_msg))
                logger.info("Successfully subscribed to public trades stream.")
                while True:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=20.0)
                        await handle_trades_message(message)
                    except asyncio.TimeoutError:
                        await websocket.send(json.dumps({"method": "PING"}))
        except Exception as e:
            logger.error(f"Trades WebSocket error: {e}. Reconnecting in 10s...")
            await asyncio.sleep(10)

async def periodic_activity_checker():
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
    message = (f"⚡️ **رصد نشاط شراء مفاجئ! (صفقات)** ⚡️\n\n"
               f"**العملة:** `${symbol}`\n"
               f"**حجم الشراء (آخر {INSTANT_TIMEFRAME_SECONDS} ثوانٍ):** `${total_volume:,.0f} USDT`\n"
               f"**عدد الصفقات (آخر {INSTANT_TIMEFRAME_SECONDS} ثوانٍ):** `{trade_count}`\n\n"
               f"*(إشارة مبكرة جداً وعالية المخاطر)*")
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"INSTANT ALERT sent for {symbol}.")
    except Exception as e: logger.error(f"Failed to send instant alert for {symbol}: {e}")

# =============================================================================
# 3. قسم رادار الحيتان (التلقائي واليدوي)
# =============================================================================
def send_whale_watch_alert(symbol, side, price, volume_usdt):
    side_text, side_emoji = ("شراء", "🟢") if side == "bids" else ("بيع", "🔴")
    message = (f"🐋 **رادار الحيتان (تلقائي): رصد حائط {side_text} ضخم!** 🐋\n\n"
               f"**العملة:** `${symbol}`\n\n"
               f"{side_emoji} **حائط {side_text} جديد:**\n"
               f"   - **السعر:** `{format_price(price)}` USDT\n"
               f"   - **الحجم:** `${volume_usdt:,.0f}` USDT\n\n"
               f"*(تنبيه استباقي من المراقب التلقائي)*")
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"AUTO WHALE ALERT for {symbol}: {side} wall of {volume_usdt:,.0f} USDT.")
    except Exception as e: logger.error(f"Failed to send auto whale alert for {symbol}: {e}")

async def analyze_order_book_for_walls_ws(symbol, book_side, data):
    now_ts = datetime.now(UTC).timestamp()
    for sym in list(recently_alerted_whale.keys()):
        if now_ts - recently_alerted_whale[sym] > COOLDOWN_PERIOD_HOURS * 3600:
            del recently_alerted_whale[sym]
    if symbol in recently_alerted_whale: return
    for price_level in data:
        price, quantity = float(price_level['p']), float(price_level['q'])
        volume_usdt = price * quantity
        if volume_usdt >= WHALE_WALL_THRESHOLD_USDT:
            send_whale_watch_alert(symbol, book_side, price, volume_usdt)
            recently_alerted_whale[symbol] = now_ts
            break

async def handle_depth_message(message):
    try:
        data = json.loads(message)
        if "method" in data and data["method"] == "PONG": return
        if 'd' in data and 'e' in data['d'] and data['d']['e'] == 'spot@public.increase.depth.v3.api':
            symbol = data['s']
            depth_data = data['d']
            async with order_book_lock:
                order_books_ws[symbol] = {'bids': depth_data.get('b', []), 'asks': depth_data.get('a', [])}
            await analyze_order_book_for_walls_ws(symbol, 'bids', depth_data.get('b', []))
            await analyze_order_book_for_walls_ws(symbol, 'asks', depth_data.get('a', []))
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"Error processing depth message: {e}")

async def run_depth_websocket_client():
    logger.info("Starting Auto Whale Radar (WebSocket)...")
    params = [f"spot@public.increase.depth.v3.api@{symbol}" for symbol in AUTO_WHALE_WATCH_SYMBOLS]
    subscription_msg = {"method": "SUBSCRIPTION", "params": params}
    while True:
        try:
            async with websockets.connect(MEXC_WS_URL) as websocket:
                await websocket.send(json.dumps(subscription_msg))
                logger.info(f"Auto Whale Radar subscribed to: {', '.join(AUTO_WHALE_WATCH_SYMBOLS)}")
                while True:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=20.0)
                        await handle_depth_message(message)
                    except asyncio.TimeoutError:
                        await websocket.send(json.dumps({"method": "PING"}))
        except Exception as e:
            logger.error(f"Depth WebSocket error: {e}. Reconnecting in 10s...")
            await asyncio.sleep(10)

# =============================================================================
# 4. الوظائف التفاعلية (أوامر البوت)
# =============================================================================
BTN_MOMENTUM = "🚀 كاشف الزخم"
BTN_WHALE_RADAR = "🐋 رادار الحيتان"
BTN_GAINERS = "📈 الأكثر ارتفاعاً"
BTN_LOSERS = "📉 الأكثر انخفاضاً"
BTN_VOLUME = "💰 الأعلى سيولة"
BTN_STATUS = "📊 الحالة"
BTN_PERFORMANCE = "📈 تقرير الأداء"

def build_menu():
    keyboard = [
        [BTN_MOMENTUM, BTN_WHALE_RADAR],
        [BTN_GAINERS, BTN_LOSERS, BTN_VOLUME],
        [BTN_STATUS, BTN_PERFORMANCE]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def start_command(update, context):
    welcome_message = ("✅ **بوت التداول الذكي (v12 - رادار يدوي) جاهز!**\n\n"
                       "**تحسينات رئيسية:**\n"
                       "- **جديد:** زر `🐋 رادار الحيتان` لفحص السوق يدوياً بحثاً عن حوائط الأوامر الضخمة.\n"
                       "- يستمر الرادار التلقائي بمراقبة العملات الرئيسية في الخلفية.\n"
                       "- جميع الأوامر متاحة الآن عبر لوحة الأزرار.")
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def status_command(update, context):
    message = (f"📊 **حالة البوت** 📊\n\n"
               f"**1. الراصد اللحظي (صفقات):**\n   - ✅ متصل، {'يتم تتبع ' + str(len(activity_tracker)) + ' عملة' if activity_tracker else 'السوق هادئ'}.\n"
               f"**2. الرادار التلقائي (أوامر):**\n   - ✅ متصل، يراقب {len(order_books_ws)} عملة ({', '.join(AUTO_WHALE_WATCH_SYMBOLS)}).\n"
               f"\n**3. مساعد إدارة الصفقات:**\n   - ✅ {'نشط، يراقب ' + str(len(active_hunts)) + ' فرصة' if active_hunts else 'ينتظر فرصة جديدة'}.\n"
               f"\n**4. متتبع الأداء:**\n   - ✅ {'نشط، يراقب أداء ' + str(len(performance_tracker)) + ' عملة' if performance_tracker else 'ينتظر تنبيهات'}.")
    update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update, context):
    button_text = update.message.text
    chat_id = update.message.chat_id
    loop = context.bot_data['loop']
    session = context.bot_data['session']
    if button_text == BTN_STATUS:
        status_command(update, context)
        return
    sent_message = context.bot.send_message(chat_id=chat_id, text="🔍 جارِ تنفيذ طلبك...")
    task = None
    if button_text == BTN_GAINERS: task = get_top_10_list(context, chat_id, sent_message.message_id, 'gainers', session)
    elif button_text == BTN_LOSERS: task = get_top_10_list(context, chat_id, sent_message.message_id, 'losers', session)
    elif button_text == BTN_VOLUME: task = get_top_10_list(context, chat_id, sent_message.message_id, 'volume', session)
    elif button_text == BTN_MOMENTUM: task = run_momentum_detector(context, chat_id, sent_message.message_id, session)
    elif button_text == BTN_WHALE_RADAR: task = run_whale_radar_scan(context, chat_id, sent_message.message_id, session)
    elif button_text == BTN_PERFORMANCE: task = get_performance_report(context, chat_id, sent_message.message_id, session)
    if task: asyncio.run_coroutine_threadsafe(task, loop)

async def get_top_10_list(context, chat_id, message_id, list_type, session: aiohttp.ClientSession):
    type_map = {'gainers': {'key': 'priceChangePercent', 'title': '🔥 الأكثر ارتفاعاً', 'reverse': True}, 'losers': {'key': 'priceChangePercent', 'title': '📉 الأكثر انخفاضاً', 'reverse': False}, 'volume': {'key': 'quoteVolume', 'title': '💰 الأعلى سيولة', 'reverse': True}}
    config = type_map[list_type]
    try:
        data = await get_market_data(session)
        if not data: raise ValueError("No market data received.")
        usdt_pairs = [s for s in data if s['symbol'].endswith('USDT')]
        for pair in usdt_pairs: pair['sort_key'] = float(pair.get(config['key'], 0.0))
        sorted_pairs = sorted(usdt_pairs, key=lambda x: x['sort_key'], reverse=config['reverse'])
        message = f"**{config['title']} في آخر 24 ساعة**\n\n"
        for i, pair in enumerate(sorted_pairs[:10]):
            value = pair['sort_key']
            value_str = f"{value * 100:+.2f}%" if list_type != 'volume' else f"${value:,.0f}"
            message += (f"{i+1}. **${pair['symbol'].replace('USDT', '')}**\n   - {'النسبة' if list_type != 'volume' else 'حجم التداول'}: `{value_str}`\n   - السعر الحالي: `${format_price(pair.get('lastPrice', 'N/A'))}`\n\n")
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_top_10_list: {e}")
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="حدث خطأ أثناء جلب البيانات.")

# NEW: Manual Whale Radar Scan Function
async def run_whale_radar_scan(context, chat_id, message_id, session: aiohttp.ClientSession):
    """Performs a manual scan for whale walls on top N volume coins."""
    initial_text = f"🐋 **رادار الحيتان (يدوي)**\n\n🔍 جارِ فحص دفاتر الأوامر لأقوى {MANUAL_WHALE_SCAN_TOP_N} عملة من حيث السيولة..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    market_data = await get_market_data(session)
    if not market_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق."); return

    usdt_pairs = [p for p in market_data if p.get('symbol','').endswith('USDT')]
    for p in usdt_pairs: p['quoteVolume_float'] = float(p.get('quoteVolume', 0))
    top_volume_coins = sorted(usdt_pairs, key=lambda x: x['quoteVolume_float'], reverse=True)[:MANUAL_WHALE_SCAN_TOP_N]

    tasks = [get_order_book(session, p['symbol']) for p in top_volume_coins]
    all_order_books = await asyncio.gather(*tasks)

    found_walls = []
    for i, book in enumerate(all_order_books):
        if not book: continue
        symbol = top_volume_coins[i]['symbol']
        
        # Analyze Bids (Buy Walls)
        for level in book.get('bids', []):
            price, qty = float(level[0]), float(level[1])
            volume = price * qty
            if volume >= WHALE_WALL_THRESHOLD_USDT:
                found_walls.append({'symbol': symbol, 'side': 'Buy', 'price': price, 'volume': volume})
                break # Report only the largest/first wall for this side
        
        # Analyze Asks (Sell Walls)
        for level in book.get('asks', []):
            price, qty = float(level[0]), float(level[1])
            volume = price * qty
            if volume >= WHALE_WALL_THRESHOLD_USDT:
                found_walls.append({'symbol': symbol, 'side': 'Sell', 'price': price, 'volume': volume})
                break # Report only the largest/first wall for this side

    if not found_walls:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **فحص الرادار اكتمل:** لا توجد حوائط أوامر ضخمة حالياً."); return
    
    sorted_walls = sorted(found_walls, key=lambda x: x['volume'], reverse=True)
    message = f"🐋 **تقرير رادار الحيتان - {datetime.now().strftime('%H:%M:%S')}** 🐋\n\n"
    for wall in sorted_walls:
        emoji = "🟢" if wall['side'] == 'Buy' else "🔴"
        side_text = "شراء" if wall['side'] == 'Buy' else "بيع"
        message += (f"{emoji} **${wall['symbol'].replace('USDT', '')}** - حائط {side_text}\n"
                    f"    - **الحجم:** `${wall['volume']:,.0f}` USDT\n"
                    f"    - **عند سعر:** `{format_price(wall['price'])}`\n\n")

    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)


async def run_momentum_detector(context, chat_id, message_id, session: aiohttp.ClientSession):
    initial_text = "🚀 **كاشف الزخم**\n\n🔍 جارِ الفحص المتوازي للسوق..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass
    market_data = await get_market_data(session)
    if not market_data: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق."); return
    potential_coins = [p for p in market_data if p.get('symbol','').endswith('USDT') and float(p.get('lastPrice','1')) <= MOMENTUM_MAX_PRICE and MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= MOMENTUM_MAX_VOLUME_24H]
    if not potential_coins: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="لم يتم العثور على عملات ضمن المعايير الأولية."); return
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
                momentum_coins.append({'symbol': potential_coins[i]['symbol'], 'price_change': price_change, 'current_price': end_p, 'peak_volume': new_v})
        except (ValueError, IndexError): continue
    if not momentum_coins: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **الفحص السريع اكتمل:** لا يوجد زخم حقيقي حالياً."); return
    sorted_coins = sorted(momentum_coins, key=lambda x: x['price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم الفوري - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\n"
    for i, coin in enumerate(sorted_coins[:10]):
        message += (f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n   - السعر: `${format_price(coin['current_price'])}`\n   - **زخم آخر 30 دقيقة: `%{coin['price_change']:+.2f}`**\n\n")
    message += "*(تمت إضافة هذه العملات إلى متتبع الأداء.)*"
    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)
    now = datetime.now(UTC)
    for coin in sorted_coins[:10]: add_to_monitoring(coin['symbol'], float(coin['current_price']), coin.get('peak_volume', 0), now, "الزخم اليدوي")

# =============================================================================
# 5. المهام الآلية الدورية (No changes here)
# =============================================================================
def add_to_monitoring(symbol, alert_price, peak_volume, alert_time, source):
    if symbol not in active_hunts:
        active_hunts[symbol] = {'alert_price': alert_price, 'alert_time': alert_time, 'peak_volume': peak_volume}
        logger.info(f"MONITORING STARTED for ({source}) {symbol}")
    if symbol not in performance_tracker:
        performance_tracker[symbol] = {'alert_price': alert_price, 'alert_time': alert_time, 'source': source, 'current_price': alert_price, 'high_price': alert_price, 'status': 'Tracking'}
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol}")

async def fomo_hunter_loop(session: aiohttp.ClientSession):
    logger.info("Fomo Hunter background task started.")
    while True:
        logger.info("===== Fomo Hunter: Starting Scan =====")
        now = datetime.now(UTC)
        try:
            market_data = await get_market_data(session)
            if not market_data: await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60); continue
            usdt_pairs = [s for s in market_data if s.get('symbol','').endswith('USDT')]
            for pair in usdt_pairs: pair['priceChangePercent_float'] = float(pair.get('priceChangePercent', 0))
            potential_coins = sorted(usdt_pairs, key=lambda x: x['priceChangePercent_float'], reverse=True)[:TOP_GAINERS_CANDIDATE_LIMIT]
            for pair_data in potential_coins:
                symbol = pair_data['symbol']
                if symbol in recently_alerted_fomo and (now - recently_alerted_fomo[symbol]) < timedelta(hours=COOLDOWN_PERIOD_HOURS): continue
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
        except Exception as e: logger.error(f"An unexpected error in fomo_hunter_loop: {e}")
        logger.info("===== Fomo Hunter: Scan Finished =====")
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)

async def analyze_fomo_symbol(session: aiohttp.ClientSession, symbol: str):
    try:
        daily_data = await get_klines(session, symbol, '1d', 2)
        if not daily_data or len(daily_data) < 2: return None
        prev_vol, curr_vol = float(daily_data[0][7]), float(daily_data[1][7])
        if not (curr_vol > MIN_USDT_VOLUME and curr_vol > (prev_vol * VOLUME_SPIKE_MULTIPLIER)): return None
        hourly_data = await get_klines(session, symbol, '1h', 4)
        if not hourly_data or len(hourly_data) < 4: return None
        initial_price = float(hourly_data[0][1])
        if initial_price == 0: return None
        price_increase_percent = ((float(hourly_data[-1][2]) - initial_price) / initial_price) * 100
        if not (price_increase_percent >= PRICE_VELOCITY_THRESHOLD): return None
        current_price = await get_current_price(session, symbol)
        if not current_price: return None
        vol_increase_percent = ((curr_vol - prev_vol) / prev_vol) * 100 if prev_vol > 0 else float('inf')
        last_hour_kline = await get_klines(session, symbol, '1h', 1)
        peak_volume = float(last_hour_kline[0][5]) if last_hour_kline else 0
        return {'symbol': symbol, 'volume_increase': f"+{vol_increase_percent:,.2f}%", 'price_pattern': f"صعود +{price_increase_percent:,.2f}% في 4 ساعات", 'current_price': format_price(current_price), 'peak_volume': peak_volume}
    except (KeyError, IndexError, ValueError): return None

async def new_listings_sniper_loop(session: aiohttp.ClientSession):
    global known_symbols
    logger.info("New Listings Sniper background task started.")
    initial_data = await get_market_data(session)
    if initial_data:
        known_symbols = {s['symbol'] for s in initial_data if s.get('symbol','').endswith('USDT')}
        logger.info(f"Sniper: Initialized with {len(known_symbols)} symbols.")
    else: logger.warning("Sniper: Could not initialize symbols.")
    while True:
        try:
            data = await get_market_data(session)
            if not data: await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS); continue
            current_symbols = {s['symbol'] for s in data if s.get('symbol','').endswith('USDT')}
            if not known_symbols: known_symbols = current_symbols; continue
            newly_listed = current_symbols - known_symbols
            if newly_listed:
                for symbol in newly_listed:
                    logger.info(f"Sniper: NEW LISTING DETECTED: {symbol}")
                    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🎯 **إدراج جديد:** `${symbol}`", parse_mode=ParseMode.MARKDOWN)
                known_symbols.update(newly_listed)
        except Exception as e: logger.error(f"An unexpected error in new_listings_sniper_loop: {e}")
        await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS)

async def monitor_active_hunts_loop(session: aiohttp.ClientSession):
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
                klines = await get_klines(session, symbol, '5m', 3)
                if not klines or len(klines) < 3: continue
                last_candle, prev_candle = klines[-1], klines[-2]
                is_last_red, is_prev_red = float(last_candle[4]) < float(last_candle[1]), float(prev_candle[4]) < float(prev_candle[1])
                weakness_reason = None
                if is_last_red and is_prev_red:
                    weakness_reason = "شمعتان حمراوان متتاليتان"
                    if float(last_candle[5]) < float(prev_candle[5]): weakness_reason += " مع انخفاض في السيولة"
                if weakness_reason:
                    send_weakness_alert(symbol, weakness_reason, float(last_candle[4]))
                    del active_hunts[symbol]
            except Exception as e: logger.error(f"Error monitoring {symbol}: {e}")

def send_weakness_alert(symbol, reason, current_price):
    message = (f"⚠️ **تحذير ضعف الزخم: ${symbol.replace('USDT', '')}** ⚠️\n\n"
               f"- **السبب:** `{reason}`\n"
               f"- **السعر الحالي:** `${format_price(current_price)}`")
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
    logger.info(f"WEAKNESS ALERT sent for {symbol}. Reason: {reason}")

async def performance_tracker_loop(session: aiohttp.ClientSession):
    logger.info("Performance Tracker background task started.")
    while True:
        await asyncio.sleep(RUN_PERFORMANCE_TRACKER_EVERY_MINUTES * 60)
        now = datetime.now(UTC)
        for symbol in list(performance_tracker.keys()):
            if now - performance_tracker[symbol]['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                performance_tracker[symbol]['status'] = 'Archived'
                logger.info(f"PERFORMANCE TRACKING ARCHIVED for {symbol}.")
                continue
            if performance_tracker[symbol]['status'] == 'Archived':
                del performance_tracker[symbol]; continue
            current_price = await get_current_price(session, symbol)
            if current_price:
                performance_tracker[symbol]['current_price'] = current_price
                if current_price > performance_tracker[symbol]['high_price']:
                    performance_tracker[symbol]['high_price'] = current_price

async def get_performance_report(context, chat_id, message_id, session: aiohttp.ClientSession):
    try:
        if not performance_tracker:
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="ℹ️ لا توجد عملات قيد تتبع الأداء حالياً."); return
        symbols_to_update = [symbol for symbol, data in performance_tracker.items() if data.get('status') == 'Tracking']
        price_tasks = [get_current_price(session, symbol) for symbol in symbols_to_update]
        latest_prices = await asyncio.gather(*price_tasks)
        for i, symbol in enumerate(symbols_to_update):
            if latest_prices[i] is not None:
                performance_tracker[symbol]['current_price'] = latest_prices[i]
                if latest_prices[i] > performance_tracker[symbol]['high_price']: performance_tracker[symbol]['high_price'] = latest_prices[i]
        message = "📊 **تقرير أداء العملات المرصودة** 📊\n\n"
        sorted_symbols = sorted(performance_tracker.items(), key=lambda item: item[1]['alert_time'], reverse=True)
        for symbol, data in sorted_symbols:
            if data['status'] == 'Archived': continue
            alert_price, current_price, high_price = data['alert_price'], data['current_price'], data['high_price']
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
# 6. تشغيل البوت
# =============================================================================
def send_startup_message():
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="✅ **بوت التداول الذكي (v12) متصل الآن!**\n\nأرسل /start لعرض القائمة.", parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent successfully.")
    except Exception as e: logger.error(f"Failed to send startup message: {e}")

async def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set.")
        return
    async with aiohttp.ClientSession() as session:
        updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
        dp = updater.dispatcher
        loop = asyncio.get_running_loop()
        dp.bot_data['loop'], dp.bot_data['session'] = loop, session
        dp.add_handler(CommandHandler("start", start_command))
        dp.add_handler(MessageHandler(Filters.text([BTN_MOMENTUM, BTN_WHALE_RADAR, BTN_GAINERS, BTN_LOSERS, BTN_VOLUME, BTN_STATUS, BTN_PERFORMANCE]), handle_button_press))

        tasks = [
            asyncio.create_task(run_trades_websocket_client()),
            asyncio.create_task(run_depth_websocket_client()),
            asyncio.create_task(periodic_activity_checker()),
            asyncio.create_task(fomo_hunter_loop(session)),
            asyncio.create_task(new_listings_sniper_loop(session)),
            asyncio.create_task(monitor_active_hunts_loop(session)),
            asyncio.create_task(performance_tracker_loop(session)),
        ]
        loop.run_in_executor(None, updater.start_polling)
        logger.info("Telegram bot is now polling for commands...")
        send_startup_message()
        await asyncio.gather(*tasks)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")