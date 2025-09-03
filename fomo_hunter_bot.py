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
# هام: استبدل القيم التالية بالقيم الحقيقية الخاصة بك أو قم بتعيينها كمتغيرات بيئة
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# --- إعدادات صائد الجواهر (Gem Hunter) ---
GEM_MAX_PRICE = 1.0  # تجاهل أي عملة سعرها أعلى من هذا الحد
GEM_MIN_VOLUME_24H = 100000  # يجب أن يكون حجم التداول أعلى من هذا الحد (للتأكد من وجود سيولة)
GEM_MAX_VOLUME_24H = 5000000 # تجاهل العملات التي لديها سيولة ضخمة بالفعل (لأنها انفجرت غالباً)
GEM_HUNTER_CANDIDATE_LIMIT = 20 # فحص أفضل 20 عملة تطابق هذه الشروط

# --- إعدادات رادار السوق العام (Whale Radar) ---
WHALE_WALL_THRESHOLD_USDT = 100000 # الحد الأدنى لحجم حائط الأوامر بالدولار
AUTO_WHALE_WATCH_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"] # عملات للمراقبة التلقائية
MANUAL_WHALE_SCAN_TOP_N = 30 # فحص أفضل 30 عملة من حيث السيولة عند الضغط على الزر

# --- معايير الرصد اللحظي (WebSocket) ---
INSTANT_TIMEFRAME_SECONDS = 10 # الإطار الزمني للزخم (بالثواني)
INSTANT_VOLUME_THRESHOLD_USDT = 50000 # عتبة حجم التداول للزخم
INSTANT_TRADE_COUNT_THRESHOLD = 20 # عتبة عدد الصفقات للزخم

# --- إعدادات أخرى ---
MEXC_API_BASE_URL = "https://api.mexc.com"
MEXC_WS_URL = "wss://wbs.mexc.com/ws"
COOLDOWN_PERIOD_HOURS = 2
HTTP_TIMEOUT = 15
STABLECOINS = {'USDCUSDT', 'USDTUSDT', 'FDUSDUSDT', 'DAIUSDT', 'TUSDUSDT', 'USD1USDT'}

# --- إعدادات تسجيل الأخطاء ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة وتهيئة البوت ---
# =============================================================================
bot = Bot(token=TELEGRAM_BOT_TOKEN)
performance_tracker = {} # لتتبع أداء التنبيهات
activity_tracker = {} # لتتبع الصفقات اللحظية
activity_lock = asyncio.Lock()

# =============================================================================
# 1. قسم الشبكة والوظائف الأساسية (Async)
# =============================================================================
async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None):
    """وظيفة مساعدة لجلب البيانات من API بصيغة JSON"""
    try:
        async with session.get(url, params=params, timeout=HTTP_TIMEOUT, headers={'User-Agent': 'Mozilla/5.0'}) as response:
            response.raise_for_status()
            return await response.json()
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None

async def get_market_data(session: aiohttp.ClientSession):
    """جلب بيانات السوق الكاملة (24hr ticker)"""
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/24hr")

async def get_order_book(session: aiohttp.ClientSession, symbol: str, limit: int = 20):
    """جلب دفتر الأوامر لعملة محددة"""
    params = {'symbol': symbol, 'limit': limit}
    return await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/depth", params)

async def get_current_price(session: aiohttp.ClientSession, symbol: str) -> float | None:
    """جلب السعر الحالي لعملة محددة"""
    data = await fetch_json(session, f"{MEXC_API_BASE_URL}/api/v3/ticker/price", {'symbol': symbol})
    if data and 'price' in data: return float(data['price'])
    return None

def format_price(price_str):
    """تنسيق السعر لإزالة الأصفار غير الضرورية"""
    try: return f"{float(price_str):.8g}"
    except (ValueError, TypeError): return price_str

# =============================================================================
# 2. أقسام الرصد التلقائي (WebSocket للصفقات)
# =============================================================================
async def handle_trades_message(message):
    """معالجة رسائل الصفقات الواردة من WebSocket"""
    try:
        data = json.loads(message)
        if "method" in data and data["method"] == "PONG": return
        if 'd' in data and 'e' in data['d'] and data['d']['e'] == 'spot@public.deals.v3.api':
            symbol = data['s']
            for deal in data['d']['D']:
                if deal['S'] == 1: # 1 for buy trades
                    volume_usdt = float(deal['p']) * float(deal['q'])
                    timestamp = float(deal['t']) / 1000.0
                    async with activity_lock:
                        if symbol not in activity_tracker: activity_tracker[symbol] = deque(maxlen=200)
                        activity_tracker[symbol].append({'v': volume_usdt, 't': timestamp})
    except (json.JSONDecodeError, KeyError, ValueError): pass

async def run_trades_websocket_client():
    """تشغيل WebSocket للاستماع إلى جميع الصفقات في السوق"""
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
    """تشغيل WebSocket لمراقبة حوائط الأوامر للعملات الكبرى تلقائياً"""
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
                        # يمكنك إضافة منطق هنا لإرسال تنبيهات تلقائية عند رصد حائط كبير
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
    """بناء قائمة الأزرار الرئيسية"""
    keyboard = [
        [BTN_GEM_HUNTER, BTN_MOMENTUM],
        [BTN_WHALE_RADAR, BTN_GAINERS],
        [BTN_STATUS, BTN_PERFORMANCE]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def start_command(update, context):
    """دالة الأمر /start"""
    welcome_message = ("✅ **بوت التداول الذكي (v13 - صائد الجواهر) جاهز!**\n\n"
                       "**تحسينات رئيسية:**\n"
                       "- **جديد:** زر `💎 صائد الجواهر` للبحث عن حيتان في العملات الصغيرة الواعدة فقط.\n"
                       "- `🐋 رادار السوق العام` أصبح مخصصاً للعملات الكبرى والسيولة الضخمة.\n"
                       "- استخدم الأداة المناسبة لهدفك!")
    update.message.reply_text(welcome_message, reply_markup=build_menu(), parse_mode=ParseMode.MARKDOWN)

def status_command(update, context):
    """دالة عرض حالة البوت"""
    message = (f"📊 **حالة البوت** 📊\n\n"
               f"**1. الراصد اللحظي (صفقات):**\n   - ✅ متصل، {'يتم تتبع ' + str(len(activity_tracker)) + ' عملة' if activity_tracker else 'السوق هادئ'}.\n"
               f"**2. الرادار التلقائي (أوامر):**\n   - ✅ متصل، يراقب {len(AUTO_WHALE_WATCH_SYMBOLS)} عملة ({', '.join(AUTO_WHALE_WATCH_SYMBOLS)}).\n"
               f"\n**3. أدوات التحليل اليدوي:**\n"
               f"   - 💎 `صائد الجواهر` (للعملات الصغيرة)\n"
               f"   - 🐋 `رادار السوق العام` (للعملات الكبيرة)\n"
               f"   - 🚀 `كاشف الزخم` (للعملات فائقة السرعة)")
    update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def handle_button_press(update, context):
    """معالجة جميع ضغطات الأزرار"""
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
    elif button_text == BTN_PERFORMANCE: task = get_performance_report(context, chat_id, sent_message.message_id, session)

    if task: asyncio.run_coroutine_threadsafe(task, loop)

# =============================================================================
# 4. منطق الأدوات اليدوية (Gem Hunter, Whale Radar, Momentum)
# =============================================================================

async def run_gem_hunter_scan(context, chat_id, message_id, session: aiohttp.ClientSession):
    """
    البحث عن حوائط الحيتان في العملات الصغيرة الواعدة (الجواهر).
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

    for p in potential_gems: p['change_float'] = float(p.get('priceChangePercent', 0))
    top_gems = sorted(potential_gems, key=lambda x: x['change_float'], reverse=True)[:GEM_HUNTER_CANDIDATE_LIMIT]

    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"💎 **صائد الجواهر**\n\n✅ تم العثور على {len(top_gems)} جوهرة محتملة. جارِ فحص دفاتر الأوامر الخاصة بهم...")

    tasks = [get_order_book(session, p['symbol']) for p in top_gems]
    all_order_books = await asyncio.gather(*tasks)
    found_walls = await find_walls_in_books(all_order_books, top_gems, WHALE_WALL_THRESHOLD_USDT)

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
    إجراء فحص الحيتان الأصلي على أفضل N عملة من حيث الحجم (نظرة عامة على السوق).
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
    found_walls = await find_walls_in_books(all_order_books, top_volume_coins, WHALE_WALL_THRESHOLD_USDT)

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


async def find_walls_in_books(all_order_books, coins_data, threshold):
    """وظيفة مساعدة لمعالجة دفاتر الأوامر والعثور على الحوائط."""
    found_walls = []
    for i, book in enumerate(all_order_books):
        if not book: continue
        symbol = coins_data[i]['symbol']
        # Analyze Bids (Buy Walls)
        for level in book.get('bids', []):
            price, qty = float(level[0]), float(level[1])
            if (price * qty) >= threshold:
                found_walls.append({'symbol': symbol, 'side': 'Buy', 'price': price, 'volume': price * qty})
                break
        # Analyze Asks (Sell Walls)
        for level in book.get('asks', []):
            price, qty = float(level[0]), float(level[1])
            if (price * qty) >= threshold:
                found_walls.append({'symbol': symbol, 'side': 'Sell', 'price': price, 'volume': price * qty})
                break
    return found_walls

async def run_momentum_detector(context, chat_id, message_id, session: aiohttp.ClientSession):
    """
    يكشف عن الزخم العالي بناءً على حجم وعدد الصفقات في فترة زمنية قصيرة.
    """
    initial_text = f"🚀 **كاشف الزخم**\n\n🔍 جارِ تحليل بيانات الصفقات اللحظية..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    found_momentum = []
    current_time = datetime.now(UTC).timestamp()
    
    async with activity_lock:
        if not activity_tracker:
            context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⏳ لا توجد بيانات صفقات كافية بعد. يرجى الانتظار قليلاً.")
            return

        for symbol, trades in activity_tracker.items():
            recent_trades = [t for t in trades if current_time - t['t'] <= INSTANT_TIMEFRAME_SECONDS]
            if not recent_trades: continue

            total_volume = sum(t['v'] for t in recent_trades)
            trade_count = len(recent_trades)

            if total_volume >= INSTANT_VOLUME_THRESHOLD_USDT and trade_count >= INSTANT_TRADE_COUNT_THRESHOLD:
                found_momentum.append({'symbol': symbol, 'volume': total_volume, 'trades': trade_count})

    if not found_momentum:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="✅ **فحص الزخم اكتمل:** السوق هادئ حالياً. لا يوجد نشاط تداول استثنائي.")
        return

    sorted_momentum = sorted(found_momentum, key=lambda x: x['volume'], reverse=True)
    message = f"🚀 **تقرير الزخم اللحظي - {datetime.now().strftime('%H:%M:%S')}** 🚀\n\n"
    message += f"تم رصد نشاط تداول عالٍ في آخر **{INSTANT_TIMEFRAME_SECONDS} ثانية**:\n\n"
    for coin in sorted_momentum:
        message += (f"🔥 **${coin['symbol'].replace('USDT', '')}**\n"
                    f"    - **حجم التداول:** `${coin['volume']:,.0f}`\n"
                    f"    - **عدد الصفقات:** `{coin['trades']}`\n\n")

    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

async def get_top_10_list(context, chat_id, message_id, list_type, session: aiohttp.ClientSession):
    """
    جلب وعرض قائمة أفضل 10 عملات من حيث الارتفاع.
    """
    initial_text = f"📈 **الأكثر ارتفاعاً**\n\n🔍 جارِ جلب بيانات السوق وترتيبها..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    market_data = await get_market_data(session)
    if not market_data:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⚠️ تعذر جلب بيانات السوق."); return

    usdt_pairs = [p for p in market_data if p.get('symbol','').endswith('USDT') and p.get('symbol') not in STABLECOINS]
    for p in usdt_pairs: p['change_float'] = float(p.get('priceChangePercent', '-999')) * 100

    sorted_coins = sorted(usdt_pairs, key=lambda x: x['change_float'], reverse=True)
    top_10 = sorted_coins[:10]

    message = f"📈 **أعلى 10 عملات ارتفاعاً (24 ساعة) - {datetime.now().strftime('%H:%M:%S')}**\n\n"
    for coin in top_10:
        symbol = coin['symbol'].replace('USDT', '')
        change = coin['change_float']
        price = format_price(coin['lastPrice'])
        message += f"🟢 **${symbol}** | **{change:+.2f}%**\n    - السعر الحالي: `{price}`\n\n"

    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)


async def get_performance_report(context, chat_id, message_id, session: aiohttp.ClientSession):
    """
    عرض تقرير أداء التنبيهات التي تم إرسالها سابقاً.
    """
    initial_text = "📈 **تقرير الأداء**\n\n🔍 جارِ تجميع نتائج التنبيهات السابقة..."
    try: context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=initial_text)
    except Exception: pass

    if not performance_tracker:
        context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="📊 **تقرير الأداء:**\n\nلا توجد تنبيهات نشطة يتم تتبعها حالياً.")
        return

    message = f"📊 **تقرير أداء التنبيهات - {datetime.now().strftime('%H:%M:%S')}**\n\n"
    message += "هذا التقرير يوضح أداء العملات منذ لحظة إرسال التنبيه الخاص بها:\n\n"

    for symbol, data in performance_tracker.items():
        initial_price = data.get('initial_price', 0)
        highest_price = data.get('highest_price', initial_price)
        if initial_price == 0: continue
        
        peak_gain = ((highest_price - initial_price) / initial_price) * 100
        current_price = await get_current_price(session, symbol) or initial_price
        current_gain = ((current_price - initial_price) / initial_price) * 100
        
        message += (f"📈 **${symbol.replace('USDT','')}**\n"
                    f"    - **أقصى ارتفاع:** `{peak_gain:+.2f}%` (عند سعر `{format_price(highest_price)}`)\n"
                    f"    - **التغير الحالي:** `{current_gain:+.2f}%` (السعر الحالي `{format_price(current_price)}`)\n"
                    f"    - (سعر التنبيه: `{format_price(initial_price)}`)\n\n")

    context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN)

# =============================================================================
# 5. المهام الآلية الدورية
# =============================================================================
async def performance_tracker_loop(session: aiohttp.ClientSession):
    """
    حلقة دورية لتحديث أسعار العملات التي يتم تتبع أدائها.
    """
    global performance_tracker
    while True:
        await asyncio.sleep(30) # Check every 30 seconds
        try:
            symbols_to_update = list(performance_tracker.keys())
            if not symbols_to_update: continue

            for symbol in symbols_to_update:
                current_price = await get_current_price(session, symbol)
                if current_price and current_price > performance_tracker[symbol].get('highest_price', 0):
                    performance_tracker[symbol]['highest_price'] = current_price
                
                if datetime.now(UTC) - performance_tracker[symbol]['alert_time'] > timedelta(hours=24):
                    del performance_tracker[symbol]
        except Exception as e:
            logger.error(f"Error in performance tracker loop: {e}")

# =============================================================================
# 6. تشغيل البوت
# =============================================================================
def send_startup_message():
    """إرسال رسالة عند بدء تشغيل البوت"""
    try:
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="✅ **بوت التداول الذكي (v13) متصل الآن!**\n\nأرسل /start لعرض القائمة.", parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent successfully.")
    except Exception as e: logger.error(f"Failed to send startup message: {e}")

async def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("خطأ فادح: لم يتم تعيين توكن البوت أو معرف الدردشة. يرجى تعديل الملف.")
        return
        
    async with aiohttp.ClientSession() as session:
        updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
        dp = updater.dispatcher
        loop = asyncio.get_running_loop()
        dp.bot_data['loop'], dp.bot_data['session'] = loop, session
        
        # إضافة المعالجات (Handlers)
        dp.add_handler(CommandHandler("start", start_command))
        dp.add_handler(MessageHandler(Filters.text([
            BTN_GEM_HUNTER, BTN_MOMENTUM, BTN_WHALE_RADAR, 
            BTN_GAINERS, BTN_STATUS, BTN_PERFORMANCE
        ]), handle_button_press))

        # بدء المهام الخلفية
        tasks = [
            asyncio.create_task(run_trades_websocket_client()),
            asyncio.create_task(run_depth_websocket_client()),
            asyncio.create_task(performance_tracker_loop(session)),
        ]
        
        # بدء البوت
        loop.run_in_executor(None, updater.start_polling)
        logger.info("Telegram bot is now polling for commands...")
        send_startup_message()
        await asyncio.gather(*tasks)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually.")