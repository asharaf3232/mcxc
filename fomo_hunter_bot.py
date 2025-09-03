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

# --- NEW: Gem Hunter Settings ---
# --- جديد: إعدادات صائد الجواهر ---
GEM_MAX_PRICE = 1.0  # تجاهل أي عملة سعرها أعلى من هذا الحد
GEM_MIN_VOLUME_24H = 100000  # يجب أن يكون حجم التداول أعلى من هذا الحد (للتأكد من وجود سيولة)
GEM_MAX_VOLUME_24H = 5000000 # تجاهل العملات التي لديها سيولة ضخمة بالفعل (لأنها انفجرت غالباً)
GEM_HUNTER_CANDIDATE_LIMIT = 20 # فحص أفضل 20 عملة تطابق هذه الشروط

# --- Whale Radar (General Market) Settings ---
WHALE_WALL_THRESHOLD_USDT = 100000
AUTO_WHALE_WATCH_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
MANUAL_WHALE_SCAN_TOP_N = 30

# --- Real-time (WebSocket) Monitoring Criteria ---
INSTANT_TIMEFRAME_SECONDS = 10
INSTANT_VOLUME_THRESHOLD_USDT = 50000
INSTANT_TRADE_COUNT_THRESHOLD = 20

# --- Other Settings ---
MEXC_API_BASE_URL = "https://api.mexc.com"
MEXC_WS_URL = "wss://wbs.mexc.com/ws"
COOLDOWN_PERIOD_HOURS = 2
HTTP_TIMEOUT = 15
STABLECOINS = {'USDCUSDT', 'USDTUSDT', 'FDUSDUSDT', 'DAIUSDT', 'TUSDUSDT', 'USD1USDT'}

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة وتهيئة البوت ---
# =============================================================================
bot = Bot(token=TELEGRAM_BOT_TOKEN)
# ... (باقي المتغيرات العامة كما هي)
recently_alerted_fomo, recently_alerted_instant, recently_alerted_whale = {}, {}, {}
known_symbols, active_hunts, performance_tracker, order_books_ws = set(), {}, {}, {}
activity_lock, order_book_lock = asyncio.Lock(), asyncio.Lock()
activity_tracker = {}
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

async def get_order_book(session: aiohttp.ClientSession, symbol: str, limit: int = 20):
    params = {'symbol': symbol, 'limit': limit}
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/depth", params)

def format_price(price_str):
    try: return f"{float(price_str):.8g}"
    except (ValueError, TypeError): return price_str

# ... (باقي الوظائف المساعدة مثل get_klines, get_current_price تبقى كما هي)
async def get_klines(session: aiohttp.ClientSession, symbol: str, interval: str, limit: int):
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/klines", params=params)

async def get_current_price(session: aiohttp.ClientSession, symbol: str) -> float | None:
    data = await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/price", {'symbol': symbol})
    if data and 'price' in data: return float(data['price'])
    return None
# =============================================================================
# 2. أقسام الرصد التلقائي (WebSocket للصفقات ودفتر الأوامر)
# =============================================================================
# --- هذا القسم يبقى كما هو بدون تغيير ---
# (run_trades_websocket_client, periodic_activity_checker, run_depth_websocket_client, etc.)
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

async def run_depth_websocket_client():
    logger.info("Starting Auto Whale Radar (WebSocket)...")
    params = [f"spot@public.increase.depth.v3.api@{symbol}" for symbol in AUTO_WHALE_WATCH_SYMBOLS]
    subscription_msg = {"method": "SUBSCRIPTION", "params": params}
    while True:
        try:
            async with websockets.connect(MEXC_WS_URL) as websocket:
                await websocket.send(json.dumps(subscription_msg))
                logger.info(f"Auto Whale Radar subscribed to: {', '.join(AUTO_WHALE_WATCH_SYMBOLS)}")
                # ... (rest of the function)
                while True:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=20.0)
                        # await handle_depth_message(message) # Logic for auto-alerts
                    except asyncio.TimeoutError:
                        await websocket.send(json.dumps({"method": "PING"}))
        except Exception as e:
            logger.error(f"Depth WebSocket error: {e}. Reconnecting in 10s...")
            await asyncio.sleep(10)
# =============================================================================
# 3. قسم الوظائف التفاعلية (الأزرار)
# =============================================================================
BTN_GEM_HUNTER = "💎 صائد الجواهر"
BTN_MOMENTUM = "🚀 كاشف الزخم"
BTN_WHALE_RADAR = "🐋 رادار السوق العام"
BTN_GAINERS = "📈 الأكثر ارتفاعاً"
BTN_STATUS = "📊 الحالة"
BTN_PERFORMANCE = "📈 تقرير الأداء"

def build_menu():
    keyboard = [
        [BTN_GEM_HUNTER, BTN_MOMENTUM],
        [BTN_WHALE_RADAR, BTN_GAINERS],
        [BTN_STATUS, BTN_PERFORMANCE]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def start_command(update, context):
    welcome_message = ("✅ **بوت التداول الذكي (v13 - صائد الجواهر) جاهز!**\n\n"
                       "**تحسينات رئيسية:**\n"
                       "- **جديد:** زر `💎 صائد الجواهر` للبحث عن حيتان في العملات الصغيرة الواعدة فقط.\n"
                       "- `🐋 رادار السوق العام` أصبح مخصصاً للعملات الكبرى والسيولة الضخمة.\n"
                       "- استخدم الأداة المناسبة لهدفك!")
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def status_command(update, context):
    message = (f"📊 **حالة البوت** 📊\n\n"
               f"**1. الراصد اللحظي (صفقات):**\n   - ✅ متصل، {'يتم تتبع ' + str(len(activity_tracker)) + ' عملة' if activity_tracker else 'السوق هادئ'}.\n"
               f"**2. الرادار التلقائي (أوامر):**\n   - ✅ متصل، يراقب {len(order_books_ws)} عملة ({', '.join(AUTO_WHALE_WATCH_SYMBOLS)}).\n"
               f"\n**3. أدوات التحليل اليدوي:**\n"
               f"   - 💎 `صائد الجواهر` (للعملات الصغيرة)\n"
               f"   - 🐋 `رادار السوق العام` (للعملات الكبيرة)\n"
               f"   - 🚀 `كاشف الزخم` (للعملات فائقة السرعة)")
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
    if button_text == BTN_GEM_HUNTER: task = run_gem_hunter_scan(context, chat_id, sent_message.message_id, session)
    elif button_text == BTN_WHALE_RADAR: task = run_general_market_scan(context, chat_id, sent_message.message_id, session)
    elif button_text == BTN_MOMENTUM: task = run_momentum_detector(context, chat_id, sent_message.message_id, session)
    elif button_text == BTN_GAINERS: task = get_top_10_list(context, chat_id, sent_message.message_id, 'gainers', session)
    # ... (add other buttons if any)
    elif button_text == BTN_PERFORMANCE: task = get_performance_report(context, chat_id, sent_message.message_id, session)

    if task: asyncio.run_coroutine_threadsafe(task, loop)

async def get_top_10_list(context, chat_id, message_id, list_type, session: aiohttp.ClientSession):
    # This function remains the same
    # ...
    pass
# =============================================================================
# 4. منطق الأدوات اليدوية (Gem Hunter, Whale Radar, Momentum)
# =============================================================================

async def run_gem_hunter_scan(context, chat_id, message_id, session: aiohttp.ClientSession):
    """
    Finds whale walls specifically in low-cap, high-potential coins (gems).
    """
    initial_text = f"💎 **صائد الجواهر**\n\n🔍 جارِ فلترة السوق والبحث عن جواهر محتملة..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    market_data = await get_market_data(session)
    if not market_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق."); return

    potential_gems = [
        p for p in market_data
        if p.get('symbol','').endswith('USDT')
        and p.get('symbol') not in STABLECOINS
        and float(p.get('lastPrice','999')) <= GEM_MAX_PRICE
        and GEM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= GEM_MAX_VOLUME_24H
    ]

    if not potential_gems:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **فحص الجواهر اكتمل:** لم يتم العثور على عملات تطابق معايير البحث الدقيقة حالياً."); return

    # Sort by 24h gainers to find the hottest ones
    for p in potential_gems: p['change_float'] = float(p.get('priceChangePercent', 0))
    top_gems = sorted(potential_gems, key=lambda x: x['change_float'], reverse=True)[:GEM_HUNTER_CANDIDATE_LIMIT]

    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"💎 **صائد الجواهر**\n\n✅ تم العثور على {len(top_gems)} جوهرة محتملة. جارِ فحص دفاتر الأوامر الخاصة بهم...")

    tasks = [get_order_book(session, p['symbol']) for p in top_gems]
    all_order_books = await asyncio.gather(*tasks)

    found_walls = await find_walls_in_books(all_order_books, top_gems)

    if not found_walls:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **فحص الجواهر اكتمل:** لا توجد حوائط حيتان واضحة في العملات الواعدة حالياً."); return

    sorted_walls = sorted(found_walls, key=lambda x: x['volume'], reverse=True)
    message = f"💎 **تقرير صائد الجواهر - {datetime.now().strftime('%H:%M:%S')}** 💎\n\n"
    message += "تم رصد حوائط الحيتان هذه في **العملات الصغيرة الواعدة فقط**:\n\n"
    for wall in sorted_walls:
        emoji = "🟢" if wall['side'] == 'Buy' else "🔴"
        side_text = "شراء" if wall['side'] == 'Buy' else "بيع"
        message += (f"{emoji} **${wall['symbol'].replace('USDT', '')}** - حائط {side_text}\n"
                    f"    - **الحجم:** `${wall['volume']:,.0f}` USDT\n"
                    f"    - **عند سعر:** `{format_price(wall['price'])}`\n\n")

    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)


async def run_general_market_scan(context, chat_id, message_id, session: aiohttp.ClientSession):
    """
    Performs the original whale scan on top N volume coins (general market view).
    """
    initial_text = f"🐋 **رادار السوق العام**\n\n🔍 جارِ فحص دفاتر الأوامر لأقوى {MANUAL_WHALE_SCAN_TOP_N} عملة من حيث السيولة..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    market_data = await get_market_data(session)
    if not market_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق."); return

    usdt_pairs = [p for p in market_data if p.get('symbol','').endswith('USDT') and p.get('symbol') not in STABLECOINS]
    for p in usdt_pairs: p['quoteVolume_float'] = float(p.get('quoteVolume', 0))
    top_volume_coins = sorted(usdt_pairs, key=lambda x: x['quoteVolume_float'], reverse=True)[:MANUAL_WHALE_SCAN_TOP_N]

    tasks = [get_order_book(session, p['symbol']) for p in top_volume_coins]
    all_order_books = await asyncio.gather(*tasks)

    found_walls = await find_walls_in_books(all_order_books, top_volume_coins)

    if not found_walls:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **فحص الرادار العام اكتمل:** لا توجد حوائط أوامر ضخمة حالياً في العملات الكبرى."); return

    sorted_walls = sorted(found_walls, key=lambda x: x['volume'], reverse=True)
    message = f"🐋 **تقرير رادار السوق العام - {datetime.now().strftime('%H:%M:%S')}** 🐋\n\n"
    message += "تم رصد الحوائط الضخمة في **العملات ذات السيولة الأعلى**:\n\n"
    for wall in sorted_walls:
        emoji = "🟢" if wall['side'] == 'Buy' else "🔴"
        side_text = "شراء" if wall['side'] == 'Buy' else "بيع"
        message += (f"{emoji} **${wall['symbol'].replace('USDT', '')}** - حائط {side_text}\n"
                    f"    - **الحجم:** `${wall['volume']:,.0f}` USDT\n"
                    f"    - **عند سعر:** `{format_price(wall['price'])}`\n\n")

    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)


async def find_walls_in_books(all_order_books, coins_data):
    """Helper function to process order books and find walls."""
    found_walls = []
    for i, book in enumerate(all_order_books):
        if not book: continue
        symbol = coins_data[i]['symbol']
        # Analyze Bids
        for level in book.get('bids', []):
            price, qty = float(level[0]), float(level[1])
            if (price * qty) >= WHALE_WALL_THRESHOLD_USDT:
                found_walls.append({'symbol': symbol, 'side': 'Buy', 'price': price, 'volume': price * qty})
                break
        # Analyze Asks
        for level in book.get('asks', []):
            price, qty = float(level[0]), float(level[1])
            if (price * qty) >= WHALE_WALL_THRESHOLD_USDT:
                found_walls.append({'symbol': symbol, 'side': 'Sell', 'price': price, 'volume': price * qty})
                break
    return found_walls

# Momentum detector and other functions remain the same
async def run_momentum_detector(context, chat_id, message_id, session: aiohttp.ClientSession):
    # ... This function remains as it was
    pass

# =============================================================================
# 5. المهام الآلية الدورية (No changes here)
# =============================================================================
# ... (fomo_hunter_loop, new_listings_sniper_loop, etc. all remain the same)
async def performance_tracker_loop(session: aiohttp.ClientSession):
    # This function remains as it was
    pass
async def get_performance_report(context, chat_id, message_id, session: aiohttp.ClientSession):
    # This function remains as it was
    pass
# =============================================================================
# 6. تشغيل البوت
# =============================================================================
def send_startup_message():
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="✅ **بوت التداول الذكي (v13) متصل الآن!**\n\nأرسل /start لعرض القائمة.", parse_mode=ParseMode.MARKDOWN)
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
        dp.add_handler(MessageHandler(Filters.text([BTN_GEM_HUNTER, BTN_MOMENTUM, BTN_WHALE_RADAR, BTN_GAINERS, BTN_STATUS, BTN_PERFORMANCE]), handle_button_press))

        tasks = [ # Starting all background tasks
            asyncio.create_task(run_trades_websocket_client()),
            asyncio.create_task(run_depth_websocket_client()),
            # ... and all other background tasks from previous versions
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

