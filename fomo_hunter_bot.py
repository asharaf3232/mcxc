# -*- coding: utf-8 -*-
import os
import asyncio
import logging
import aiohttp
import numpy as np
import copy
from datetime import datetime, timedelta, UTC
from telegram import Bot, ParseMode, ReplyKeyboardMarkup, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, ApplicationBuilder

# =============================================================================
# --- الإعدادات الرئيسية ---
# =============================================================================

# --- Telegram Configuration ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# --- إعدادات رادار الحيتان ---
WHALE_GEM_MAX_PRICE = 0.50
WHALE_GEM_MIN_VOLUME_24H = 100000
WHALE_GEM_MAX_VOLUME_24H = 5000000
WHALE_WALL_THRESHOLD_USDT = 25000
WHALE_PRESSURE_RATIO = 3.0
WHALE_SCAN_CANDIDATE_LIMIT = 50

# --- إعدادات كاشف الزخم ---
MOMENTUM_MAX_PRICE = 0.10
MOMENTUM_MIN_VOLUME_24H = 50000
MOMENTUM_MAX_VOLUME_24H = 2000000
MOMENTUM_VOLUME_INCREASE = 1.8
MOMENTUM_PRICE_INCREASE = 4.0
MOMENTUM_KLINE_INTERVAL = '5m'
MOMENTUM_KLINE_LIMIT = 12
MOMENTUM_LOSS_THRESHOLD_PERCENT = -5.0

# --- إعدادات التوصيات الآلية ---
RECOMMENDATION_KLINE_INTERVAL = '5m'
RECOMMENDATION_KLINE_LIMIT = 20
RECOMMENDATION_TAKE_PROFIT_PERCENT = 7.0
RECOMMENDATION_STOP_LOSS_PERCENT = -3.5

# --- إعدادات المهام الدورية ---
RUN_FOMO_SCAN_EVERY_MINUTES = 15
RUN_LISTING_SCAN_EVERY_SECONDS = 60
RUN_PERFORMANCE_TRACKER_EVERY_MINUTES = 5
PERFORMANCE_TRACKING_DURATION_HOURS = 24
MARKET_MOVERS_MIN_VOLUME = 50000

# --- إعدادات عامة ---
HTTP_TIMEOUT = 15
API_CONCURRENCY_LIMIT = 8

# --- إعدادات تسجيل الأخطاء ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =============================================================================
# --- المتغيرات العامة ---
# =============================================================================
api_semaphore = asyncio.Semaphore(API_CONCURRENCY_LIMIT)
PLATFORMS = ["MEXC", "Gate.io", "Binance", "Bybit", "KuCoin", "OKX"]
performance_tracker = {p: {} for p in PLATFORMS}
known_symbols = {p: set() for p in PLATFORMS}
recently_alerted_fomo = {p: {} for p in PLATFORMS}
background_tasks = {}

# =============================================================================
# --- قسم الشبكة والوظائف الأساسية (مشتركة) ---
# =============================================================================
async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict = None, headers: dict = None, retries: int = 3):
    request_headers = {'User-Agent': 'Mozilla/5.0'}
    if headers: request_headers.update(headers)
    for attempt in range(retries):
        try:
            async with session.get(url, params=params, timeout=HTTP_TIMEOUT, headers=request_headers) as response:
                if response.status == 429:
                    logger.warning(f"Rate limit hit (429) for {url}. Retrying after {2 ** attempt}s...")
                    await asyncio.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                return await response.json()
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            if attempt >= retries - 1: return None
            await asyncio.sleep(1)
    return None

def format_price(price_str):
    try: return f"{float(price_str):.8g}"
    except (ValueError, TypeError): return price_str

# =============================================================================
# --- ⚙️ قسم عملاء المنصات (Exchange Clients) ⚙️ ---
# (الكود هنا لم يتغير... هو نفس الكود السابق ويعمل بشكل صحيح)
# =============================================================================
class BaseExchangeClient:
    def __init__(self, session): self.session = session; self.name = "Base"
    async def get_market_data(self): raise NotImplementedError
    async def get_klines(self, symbol, interval, limit): raise NotImplementedError
    async def get_order_book(self, symbol, limit=20): raise NotImplementedError
    async def get_current_price(self, symbol): raise NotImplementedError

class MexcClient(BaseExchangeClient):
    def __init__(self, session, **kwargs): super().__init__(session, **kwargs); self.name = "MEXC"; self.base_api_url = "https://api.mexc.com"
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        return [{'symbol': i['symbol'], 'quoteVolume': i.get('quoteVolume', '0'), 'lastPrice': i.get('lastPrice', '0'), 'priceChangePercent': float(i.get('priceChangePercent', '0')) * 100} for i in data if i.get('symbol','').endswith("USDT")]
    async def get_klines(self, s, i, l):
        p = {'symbol': s, 'interval': i, 'limit': l}; await asyncio.sleep(0.1); d = await fetch_json(self.session, f"{self.base_api_url}/api/v3/klines", p)
        return [[k[0], k[1], k[2], k[3], k[4], k[5]] for k in d] if d else None
    async def get_order_book(self, s, l=20): p = {'symbol': s, 'limit': l}; await asyncio.sleep(0.1); return await fetch_json(self.session, f"{self.base_api_url}/api/v3/depth", p)
    async def get_current_price(self, s): d = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/price", {'symbol': s}); return float(d['price']) if d and 'price' in d else None

class GateioClient(BaseExchangeClient):
    def __init__(self, session, **kwargs): super().__init__(session, **kwargs); self.name = "Gate.io"; self.base_api_url = "https://api.gateio.ws/api/v4"
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/spot/tickers")
        if not data: return []
        return [{'symbol': i['currency_pair'].replace('_', ''), 'quoteVolume': i.get('quote_volume', '0'), 'lastPrice': i.get('last', '0'), 'priceChangePercent': float(i.get('change_percentage', '0'))} for i in data if i.get('currency_pair', '').endswith("_USDT")]
    async def get_klines(self, s, i, l):
        gs = f"{s[:-4]}_{s[-4:]}"; p = {'currency_pair': gs, 'interval': i, 'limit': l}; await asyncio.sleep(0.1); d = await fetch_json(self.session, f"{self.base_api_url}/spot/candlesticks", p)
        if not d: return None
        return [[int(k[0])*1000, k[5], k[3], k[4], k[2], k[1]] for k in d]
    async def get_order_book(self, s, l=20): gs = f"{s[:-4]}_{s[-4:]}"; p = {'currency_pair': gs, 'limit': l}; await asyncio.sleep(0.1); return await fetch_json(self.session, f"{self.base_api_url}/spot/order_book", p)
    async def get_current_price(self, s): gs = f"{s[:-4]}_{s[-4:]}"; d = await fetch_json(self.session, f"{self.base_api_url}/spot/tickers", {'currency_pair': gs}); return float(d[0]['last']) if d and len(d) > 0 else None

class BinanceClient(BaseExchangeClient):
    def __init__(self, session, **kwargs): super().__init__(session, **kwargs); self.name = "Binance"; self.base_api_url = "https://api.binance.com"
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/24hr")
        if not data: return []
        return [{'symbol': i['symbol'], 'quoteVolume': i.get('quoteVolume', '0'), 'lastPrice': i.get('lastPrice', '0'), 'priceChangePercent': float(i.get('priceChangePercent', '0'))} for i in data if i.get('symbol','').endswith("USDT")]
    async def get_klines(self, s, i, l): p = {'symbol': s, 'interval': i, 'limit': l}; await asyncio.sleep(0.1); d = await fetch_json(self.session, f"{self.base_api_url}/api/v3/klines", p); return [[k[0], k[1], k[2], k[3], k[4], k[5]] for k in d] if d else None
    async def get_order_book(self, s, l=20): p = {'symbol': s, 'limit': l}; await asyncio.sleep(0.1); return await fetch_json(self.session, f"{self.base_api_url}/api/v3/depth", p)
    async def get_current_price(self, s): d = await fetch_json(self.session, f"{self.base_api_url}/api/v3/ticker/price", {'symbol': s}); return float(d['price']) if d and 'price' in d else None

class BybitClient(BaseExchangeClient):
    def __init__(self, session, **kwargs): super().__init__(session, **kwargs); self.name = "Bybit"; self.base_api_url = "https://api.bybit.com"
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/v5/market/tickers", {'category': 'spot'})
        if not data or not data.get('result') or not data['result'].get('list'): return []
        return [{'symbol': i['symbol'], 'quoteVolume': i.get('turnover24h', '0'), 'lastPrice': i.get('lastPrice', '0'), 'priceChangePercent': float(i.get('price24hPcnt', '0')) * 100} for i in data['result']['list'] if i['symbol'].endswith("USDT")]
    async def get_klines(self, s, i, l):
        bim = {'5m': '5', '15m': '15'}; bi = bim.get(i, '5'); p = {'category': 'spot', 'symbol': s, 'interval': bi, 'limit': l}; await asyncio.sleep(0.1); d = await fetch_json(self.session, f"{self.base_api_url}/v5/market/kline", p)
        if not d or not d.get('result') or not d['result'].get('list'): return None
        kl = d['result']['list']; kl.reverse(); return [[int(k[0]), k[1], k[2], k[3], k[4], k[5]] for k in kl]
    async def get_order_book(self, s, l=50): p = {'category': 'spot', 'symbol': s, 'limit': l}; await asyncio.sleep(0.1); d = await fetch_json(self.session, f"{self.base_api_url}/v5/market/orderbook", p); return {'bids': d['result'].get('b', []), 'asks': d['result'].get('a', [])} if d and d.get('result') else None
    async def get_current_price(self, s): d = await fetch_json(self.session, f"{self.base_api_url}/v5/market/tickers", {'category': 'spot', 'symbol': s}); return float(d['result']['list'][0]['lastPrice']) if d and d.get('result') and d['result'].get('list') else None

class KucoinClient(BaseExchangeClient):
    def __init__(self, session, **kwargs): super().__init__(session, **kwargs); self.name = "KuCoin"; self.base_api_url = "https://api.kucoin.com"
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/allTickers")
        if not data or not data.get('data') or not data['data'].get('ticker'): return []
        return [{'symbol': i['symbol'].replace('-', ''), 'quoteVolume': i.get('volValue', '0'), 'lastPrice': i.get('last', '0'), 'priceChangePercent': float(i.get('changeRate', '0')) * 100} for i in data['data']['ticker'] if i.get('symbol','').endswith("-USDT")]
    async def get_klines(self, s, i, l):
        ks = f"{s[:-4]}-{s[-4:]}"; ki = {'5m': '5min', '15m': '15min'}.get(i, '5min'); p = {'symbol': ks, 'type': ki}; await asyncio.sleep(0.1); d = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/candles", p)
        if not d or not d.get('data'): return None
        return [[int(k[0])*1000, k[1], k[3], k[4], k[2], k[5]] for k in d['data'][-l:]]
    async def get_order_book(self, s, l=20): ks = f"{s[:-4]}-{s[-4:]}"; p = {'symbol': ks}; await asyncio.sleep(0.1); d = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/orderbook/level2_20", p); return d.get('data') if d else None
    async def get_current_price(self, s): ks = f"{s[:-4]}-{s[-4:]}"; d = await fetch_json(self.session, f"{self.base_api_url}/api/v1/market/orderbook/level1", {'symbol': ks}); return float(d['data']['price']) if d and d.get('data') else None

class OkxClient(BaseExchangeClient):
    def __init__(self, session, **kwargs): super().__init__(session, **kwargs); self.name = "OKX"; self.base_api_url = "https://www.okx.com"
    async def get_market_data(self):
        data = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/tickers", {'instType': 'SPOT'})
        if not data or not data.get('data'): return []
        r = [];
        for i in data['data']:
            if not i.get('instId','').endswith("-USDT"): continue
            try: o=float(i.get('open24h', '0')); l=float(i.get('last', '0')); c= ((l-o)/o)*100 if o > 0 else 0; r.append({'symbol': i['instId'].replace('-', ''),'quoteVolume': i.get('volCcy24h', '0'),'lastPrice': l, 'priceChangePercent': c})
            except (ValueError, TypeError): continue
        return r
    async def get_klines(self, s, i, l):
        os = f"{s[:-4]}-{s[-4:]}"; oi = {'5m': '5m', '15m': '15m'}.get(i, '5m'); p = {'instId': os, 'bar': oi, 'limit': l}; await asyncio.sleep(0.2); d = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/candles", p)
        if not d or not d.get('data'): return None
        kl = d['data']; kl.reverse(); return [[int(k[0]), k[1], k[2], k[3], k[4], k[5]] for k in kl]
    async def get_order_book(self, s, l=20): os = f"{s[:-4]}-{s[-4:]}"; p = {'instId': os, 'sz': l}; await asyncio.sleep(0.2); d = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/books", p); return d['data'][0] if d and d.get('data') else None
    async def get_current_price(self, s): os = f"{s[:-4]}-{s[-4:]}"; d = await fetch_json(self.session, f"{self.base_api_url}/api/v5/market/ticker", {'instId': os}); return float(d['data'][0]['last']) if d and d.get('data') else None

def get_exchange_client(exchange_name, session):
    clients = {'mexc': MexcClient, 'gate.io': GateioClient, 'binance': BinanceClient, 'bybit': BybitClient, 'kucoin': KucoinClient, 'okx': OkxClient}
    return clients.get(exchange_name.lower())(session)

# =============================================================================
# --- الوظائف المساعدة للتحليل ---
# =============================================================================
async def helper_get_momentum_symbols(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {}
    potential_coins = [p for p in market_data if float(p.get('lastPrice','1')) <= MOMENTUM_MAX_PRICE and MOMENTUM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= MOMENTUM_MAX_VOLUME_24H]
    if not potential_coins: return {}
    tasks = [client.get_klines(p['symbol'], MOMENTUM_KLINE_INTERVAL, MOMENTUM_KLINE_LIMIT) for p in potential_coins]
    all_klines_data = await asyncio.gather(*tasks)
    momentum_coins_data = {}
    for i, klines in enumerate(all_klines_data):
        if not klines or len(klines) < MOMENTUM_KLINE_LIMIT: continue
        try:
            sp = MOMENTUM_KLINE_LIMIT // 2
            old_v = sum(float(k[5]) for k in klines[:sp]); new_v = sum(float(k[5]) for k in klines[sp:])
            start_p = float(klines[sp][1])
            if old_v > 0 and start_p > 0:
                end_p = float(klines[-1][4])
                price_change = ((end_p - start_p) / start_p) * 100
                if new_v > old_v * MOMENTUM_VOLUME_INCREASE and price_change > MOMENTUM_PRICE_INCREASE:
                    coin_symbol = potential_coins[i]['symbol']
                    momentum_coins_data[coin_symbol] = {'symbol': coin_symbol, 'price_change': price_change, 'current_price': end_p}
        except (ValueError, IndexError, TypeError): continue
    return momentum_coins_data

async def helper_get_whale_activity(client: BaseExchangeClient):
    market_data = await client.get_market_data()
    if not market_data: return {}
    potential_gems = [p for p in market_data if float(p.get('lastPrice','999')) <= WHALE_GEM_MAX_PRICE and WHALE_GEM_MIN_VOLUME_24H <= float(p.get('quoteVolume','0')) <= WHALE_GEM_MAX_VOLUME_24H]
    if not potential_gems: return {}
    for p in potential_gems: p['change_float'] = p.get('priceChangePercent', 0)
    top_gems = sorted(potential_gems, key=lambda x: x['change_float'], reverse=True)[:WHALE_SCAN_CANDIDATE_LIMIT]
    tasks = [client.get_order_book(p['symbol']) for p in top_gems]
    all_order_books = await asyncio.gather(*tasks)
    whale_signals = {}
    for i, book in enumerate(all_order_books):
        symbol = top_gems[i]['symbol']
        signals = await analyze_order_book_for_whales(book)
        if signals: whale_signals[symbol] = signals
    return whale_signals

async def analyze_order_book_for_whales(book):
    signals = []
    if not book or not book.get('bids') or not book.get('asks'): return signals
    try:
        bids = sorted([(float(p), float(q)) for p, q, *_ in book['bids']], key=lambda x: x[0], reverse=True)
        asks = sorted([(float(p), float(q)) for p, q, *_ in book['asks']], key=lambda x: x[0])
        for p, q in bids[:5]:
            if p * q >= WHALE_WALL_THRESHOLD_USDT: signals.append({'type': 'Buy Wall', 'value': p * q, 'price': p}); break
        for p, q in asks[:5]:
            if p * q >= WHALE_WALL_THRESHOLD_USDT: signals.append({'type': 'Sell Wall', 'value': p * q, 'price': p}); break
        bids_val = sum(p * q for p, q in bids[:10]); asks_val = sum(p * q for p, q in asks[:10])
        if asks_val > 0 and (bids_val / asks_val) >= WHALE_PRESSURE_RATIO: signals.append({'type': 'Buy Pressure', 'value': bids_val / asks_val})
        elif bids_val > 0 and (asks_val / bids_val) >= WHALE_PRESSURE_RATIO: signals.append({'type': 'Sell Pressure', 'value': asks_val / bids_val})
    except (ValueError, TypeError): pass
    return signals

# =============================================================================
# --- 4. الوظائف التفاعلية (أوامر البوت) ---
# =============================================================================
BTN_WHALE_RADAR = "🐋 رادار الحيتان"; BTN_MOMENTUM = "🚀 كاشف الزخم"; BTN_RECOMMENDATIONS = "💡 توصيات آلية"
BTN_STATUS = "📊 الحالة"; BTN_PERFORMANCE = "📈 تقرير الأداء"; BTN_CROSS_ANALYSIS = "💪 تحليل متقاطع"
BTN_TOP_GAINERS = "📈 الأعلى ربحاً"; BTN_TOP_LOSERS = "📉 الأعلى خسارة"; BTN_TOP_VOLUME = "💰 الأعلى تداولاً"
BTN_SELECT_MEXC = "MEXC"; BTN_SELECT_GATEIO = "Gate.io"; BTN_SELECT_BINANCE = "Binance"
BTN_SELECT_BYBIT = "Bybit"; BTN_SELECT_KUCOIN = "KuCoin"; BTN_SELECT_OKX = "OKX"
BTN_TASKS_ON = "🔴 إيقاف المهام"; BTN_TASKS_OFF = "🟢 تفعيل المهام"

def build_menu(context: CallbackContext):
    ud = context.user_data; bd = context.bot_data
    se = ud.get('exchange', 'mexc'); te = bd.get('background_tasks_enabled', True)
    mxc = f"✅ {BTN_SELECT_MEXC}" if se=='mexc' else BTN_SELECT_MEXC; gat = f"✅ {BTN_SELECT_GATEIO}" if se=='gate.io' else BTN_SELECT_GATEIO
    bnn = f"✅ {BTN_SELECT_BINANCE}" if se=='binance' else BTN_SELECT_BINANCE; byb = f"✅ {BTN_SELECT_BYBIT}" if se=='bybit' else BTN_SELECT_BYBIT
    kcn = f"✅ {BTN_SELECT_KUCOIN}" if se=='kucoin' else BTN_SELECT_KUCOIN; okx = f"✅ {BTN_SELECT_OKX}" if se=='okx' else BTN_SELECT_OKX
    ttb = BTN_TASKS_ON if te else BTN_TASKS_OFF
    keyboard = [[BTN_MOMENTUM, BTN_WHALE_RADAR, BTN_RECOMMENDATIONS], [BTN_TOP_GAINERS, BTN_TOP_VOLUME, BTN_TOP_LOSERS], [BTN_CROSS_ANALYSIS, BTN_PERFORMANCE, BTN_STATUS], [mxc, gat, bnn], [byb, kcn, okx, ttb]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start_command(update: Update, context: CallbackContext):
    context.user_data['exchange'] = 'mexc'
    context.bot_data.setdefault('background_tasks_enabled', True)
    msg = ("✅ **بوت التداول الذكي (v16 - Stable) جاهز!**\n\n"
           "**ما الجديد؟**\n"
           "- **إصلاح جذري لمشكلة عدم الاستقرار** والتعارض التي كانت تسبب أخطاء عشوائية.\n"
           "- يجب أن يعمل البوت الآن بشكل موثوق ومستمر.\n\n"
           "المنصة الحالية: **MEXC**")
    if update.message: await update.message.reply_text(msg, reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

async def set_exchange(update: Update, context: CallbackContext, exchange_name: str):
    context.user_data['exchange'] = exchange_name.lower()
    await update.message.reply_text(f"✅ تم تحويل المنصة إلى: **{exchange_name}**", reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

async def toggle_background_tasks(update: Update, context: CallbackContext):
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    context.bot_data['background_tasks_enabled'] = not tasks_enabled
    status = "تفعيل" if not tasks_enabled else "إيقاف"
    await update.message.reply_text(f"✅ تم **{status}** المهام الخلفية.", reply_markup=build_menu(context), parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: CallbackContext):
    tasks_enabled = context.bot_data.get('background_tasks_enabled', True)
    message = f"📊 **حالة البوت** 📊\n\n**1. المهام الخلفية:** {'🟢 نشطة' if tasks_enabled else '🔴 متوقفة'}\n\n"
    for platform in PLATFORMS: message += f"**منصة {platform}:**\n   - 📈 الأداء المتتبع: {len(performance_tracker.get(platform, {}))}\n\n"
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

async def handle_button_press(update: Update, context: CallbackContext):
    if not update.message or not update.message.text: return
    button_text = update.message.text.strip().replace("✅ ", "")
    if button_text in [BTN_SELECT_MEXC, BTN_SELECT_GATEIO, BTN_SELECT_BINANCE, BTN_SELECT_BYBIT, BTN_SELECT_KUCOIN, BTN_SELECT_OKX]:
        await set_exchange(update, context, button_text); return
    if button_text in [BTN_TASKS_ON, BTN_TASKS_OFF]: await toggle_background_tasks(update, context); return
    if button_text == BTN_STATUS: await status_command(update, context); return
        
    current_exchange = context.user_data.get('exchange', 'mexc')
    session = context.application.bot_data['session']
    client = get_exchange_client(current_exchange, session)
    sent_message = await update.message.reply_text(f"🔍 جارِ تنفيذ طلبك على منصة {client.name}...")
    
    chat_id = update.message.chat_id
    message_id = sent_message.message_id
    
    if button_text == BTN_MOMENTUM: await run_momentum_detector(context, chat_id, message_id, client)
    elif button_text == BTN_WHALE_RADAR: await run_whale_radar_scan(context, chat_id, message_id, client)
    elif button_text == BTN_CROSS_ANALYSIS: await run_cross_analysis(context, chat_id, message_id, client)
    elif button_text == BTN_PERFORMANCE: await get_performance_report(context, chat_id, message_id)
    elif button_text == BTN_RECOMMENDATIONS: await run_automated_recommendations(context, chat_id, message_id, client)
    elif button_text == BTN_TOP_GAINERS: await run_top_gainers(context, chat_id, message_id, client)
    elif button_text == BTN_TOP_LOSERS: await run_top_losers(context, chat_id, message_id, client)
    elif button_text == BTN_TOP_VOLUME: await run_top_volume(context, chat_id, message_id, client)

async def run_momentum_detector(context, chat_id, message_id, client: BaseExchangeClient):
    try: await context.bot.edit_message_text("🚀 **كاشف الزخم...**", chat_id=chat_id, message_id=message_id)
    except: pass
    momentum_coins = await helper_get_momentum_symbols(client)
    if not momentum_coins:
        await context.bot.edit_message_text(f"✅ **الفحص على {client.name} اكتمل:** لا يوجد زخم حالياً.", chat_id=chat_id, message_id=message_id); return
    sorted_coins = sorted(momentum_coins.values(), key=lambda x: x['price_change'], reverse=True)
    message = f"🚀 **تقرير الزخم ({client.name})** 🚀\n\n"
    for i, coin in enumerate(sorted_coins[:10]): message += f"**{i+1}. ${coin['symbol'].replace('USDT', '')}**\n   - السعر: `${format_price(coin['current_price'])}`\n   - **زخم: `%{coin['price_change']:+.2f}`**\n\n"
    message += "*(تمت الإضافة لمتتبع الأداء.)*"
    await context.bot.edit_message_text(message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN)
    now = datetime.now(UTC)
    for coin in sorted_coins[:10]: add_to_monitoring(coin['symbol'], float(coin['current_price']), now, f"الزخم ({client.name})", client.name)

async def run_whale_radar_scan(context, chat_id, message_id, client: BaseExchangeClient):
    try: await context.bot.edit_message_text("🐋 **رادار الحيتان...**", chat_id=chat_id, message_id=message_id)
    except: pass
    whale_signals = await helper_get_whale_activity(client)
    if not whale_signals:
        await context.bot.edit_message_text(f"✅ **فحص الرادار على {client.name} اكتمل:** لا يوجد نشاط حيتان واضح.", chat_id=chat_id, message_id=message_id); return
    message = f"🐋 **تقرير رادار الحيتان ({client.name})** 🐋\n\n"
    for symbol, signals in whale_signals.items():
        for signal in signals:
            sn = symbol.replace('USDT', '')
            if signal['type'] == 'Buy Wall': message += f"🟢 **حائط شراء على ${sn}**\n   - الحجم: `${signal['value']:,.0f}`\n   - السعر: `{format_price(signal['price'])}`\n\n"
            elif signal['type'] == 'Sell Wall': message += f"🔴 **حائط بيع على ${sn}**\n   - الحجم: `${signal['value']:,.0f}`\n   - السعر: `{format_price(signal['price'])}`\n\n"
            elif signal['type'] == 'Buy Pressure': message += f"📈 **ضغط شراء على ${sn}**\n   - النسبة: `{signal['value']:.1f}x`\n\n"
            elif signal['type'] == 'Sell Pressure': message += f"📉 **ضغط بيع على ${sn}**\n   - النسبة: `{signal['value']:.1f}x`\n\n"
    await context.bot.edit_message_text(message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN)

async def run_cross_analysis(context, chat_id, message_id, client: BaseExchangeClient):
    try: await context.bot.edit_message_text("💪 **تحليل متقاطع...**", chat_id=chat_id, message_id=message_id)
    except: pass
    try:
        mc, ws = await asyncio.gather(helper_get_momentum_symbols(client), helper_get_whale_activity(client))
        strong_symbols = set(mc.keys()).intersection(set(ws.keys()))
        if not strong_symbols:
            await context.bot.edit_message_text(f"✅ **التحليل المتقاطع على {client.name} اكتمل:** لا توجد إشارات قوية.", chat_id=chat_id, message_id=message_id); return
        message = f"💪 **تقرير الإشارات القوية ({client.name})** 💪\n\n"
        for s in strong_symbols:
            md = mc[s]; wsig = ws[s]
            message += f"💎 **${s.replace('USDT', '')}** 💎\n   - الزخم: `%{md['price_change']:+.2f}`\n"
            wip = [f"حائط شراء ({sig['value']:,.0f})" for sig in wsig if sig['type'] == 'Buy Wall'] + \
                  [f"ضغط شراء ({sig['value']:.1f}x)" for sig in wsig if sig['type'] == 'Buy Pressure']
            if wip: message += f"   - الحيتان: " + ", ".join(wip) + ".\n\n"
        await context.bot.edit_message_text(message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in cross_analysis: {e}", exc_info=True)
        await context.bot.edit_message_text("حدث خطأ أثناء التحليل المتقاطع.", chat_id=chat_id, message_id=message_id)

async def get_performance_report(context, chat_id, message_id):
    try:
        tracker_snapshot = copy.deepcopy(performance_tracker)
        if not any(tracker_snapshot.values()):
            await context.bot.edit_message_text("ℹ️ لا توجد عملات قيد تتبع الأداء حالياً.", chat_id=chat_id, message_id=message_id)
            return
        
        all_items = []
        for platform, symbols in tracker_snapshot.items():
            for symbol, data in symbols.items():
                if data.get('status') != 'Archived':
                    data['exchange'] = platform; all_items.append(data)
        
        if not all_items:
            await context.bot.edit_message_text("ℹ️ لا توجد عملات قيد تتبع الأداء حالياً.", chat_id=chat_id, message_id=message_id); return
             
        message = "📊 **تقرير أداء العملات المرصودة** 📊\n\n"
        sorted_items = sorted(all_items, key=lambda item: item['alert_time'], reverse=True)
        
        for data in sorted_items:
            ap = data.get('alert_price', 0); cp = data.get('current_price', ap); hp = data.get('high_price', ap)
            cc = ((cp - ap) / ap) * 100 if ap > 0 else 0
            pc = ((hp - ap) / ap) * 100 if ap > 0 else 0
            emoji = "🟢" if cc >= 0 else "🔴"
            tsa = datetime.now(UTC) - data['alert_time']; h, rem = divmod(tsa.total_seconds(), 3600); m, _ = divmod(rem, 60)
            time_str = f"{int(h)}س و {int(m)}د"
            
            message += (f"{emoji} **${data['symbol'].replace('USDT','')}** ({data.get('exchange', 'N/A')}) ({time_str})\n"
                        f"   - سعر التنبيه: `${format_price(ap)}`\n"
                        f"   - السعر الحالي: `${format_price(cp)}` (**{cc:+.2f}%**)\n"
                        f"   - أعلى سعر: `${format_price(hp)}` (**{pc:+.2f}%**)\n\n")
        await context.bot.edit_message_text(message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in get_performance_report: {e}", exc_info=True)
        await context.bot.edit_message_text("حدث خطأ أثناء جلب تقرير الأداء.", chat_id=chat_id, message_id=message_id)

async def run_automated_recommendations(context, chat_id, message_id, client: BaseExchangeClient):
    try: await context.bot.edit_message_text("💡 **توصيات آلية...**", chat_id=chat_id, message_id=message_id)
    except: pass
    try:
        mc, ws = await asyncio.gather(helper_get_momentum_symbols(client), helper_get_whale_activity(client))
        strong_symbols = set(mc.keys()).intersection(set(ws.keys()))
        if not strong_symbols:
            await context.bot.edit_message_text(f"✅ **تحليل التوصيات على {client.name} اكتمل:** لا توجد فرص قوية.", chat_id=chat_id, message_id=message_id); return
        message = f"💡 **أفضل التوصيات الآلية ({client.name})** 💡\n\n"
        for s in strong_symbols:
            klines = await client.get_klines(s, RECOMMENDATION_KLINE_INTERVAL, RECOMMENDATION_KLINE_LIMIT)
            if not klines or len(klines) < 10: continue
            cp = np.array([float(k[4]) for k in klines]); curr_p = cp[-1]
            entry_avg = np.mean(cp[-5:]); el = entry_avg * 0.995; eh = entry_avg * 1.005
            tp = entry_avg * (1 + RECOMMENDATION_TAKE_PROFIT_PERCENT/100); sl = entry_avg * (1 + RECOMMENDATION_STOP_LOSS_PERCENT/100)
            message += (f"💎 **${s.replace('USDT','')}**\n"
                        f"   - **الدخول:** بين `{format_price(el)}` - `{format_price(eh)}`\n"
                        f"   - **الهدف 🎯:** `{format_price(tp)}` (+{RECOMMENDATION_TAKE_PROFIT_PERCENT}%)\n"
                        f"   - **الوقف 🛡️:** `{format_price(sl)}` ({RECOMMENDATION_STOP_LOSS_PERCENT}%)\n\n")
        message += "**إخلاء مسؤولية:** هذه ليست نصيحة مالية."
        await context.bot.edit_message_text(message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in recommendations: {e}", exc_info=True)
        await context.bot.edit_message_text("حدث خطأ أثناء توليد التوصيات.", chat_id=chat_id, message_id=message_id)

async def run_top_movers(context, chat_id, message_id, client: BaseExchangeClient, mode: str):
    title_map = {'gainers': "الأعلى ربحاً", 'losers': "الأعلى خسارة", 'volume': "الأعلى تداولاً"}
    emoji_map = {'gainers': "📈", 'losers': "📉", 'volume': "💰"}
    try: await context.bot.edit_message_text(f"{emoji_map[mode]} **{title_map[mode]}...**", chat_id=chat_id, message_id=message_id)
    except: pass
    market_data = await client.get_market_data()
    if not market_data: await context.bot.edit_message_text("خطأ في جلب بيانات السوق.", chat_id=chat_id, message_id=message_id); return
    
    if mode in ['gainers', 'losers']:
        valid_data = [i for i in market_data if float(i.get('quoteVolume', '0')) > MARKET_MOVERS_MIN_VOLUME]
        sorted_data = sorted(valid_data, key=lambda x: x.get('priceChangePercent', 0), reverse=(mode == 'gainers'))[:10]
        message = f"{emoji_map[mode]} **{title_map[mode]} على {client.name}** {emoji_map[mode]}\n\n"
        for i, c in enumerate(sorted_data): message += f"**{i+1}. ${c['symbol'].replace('USDT', '')}:** `%{c.get('priceChangePercent', 0):+.2f}` (السعر: ${format_price(c['lastPrice'])})\n"
    else: # volume
        for item in market_data: item['qv_f'] = float(item.get('quoteVolume', '0'))
        sorted_data = sorted(market_data, key=lambda x: x['qv_f'], reverse=True)[:10]
        message = f"{emoji_map[mode]} **{title_map[mode]} على {client.name}** {emoji_map[mode]}\n\n"
        for i, c in enumerate(sorted_data):
            v = c['qv_f']; vs = f"{v/1_000_000:.2f}M" if v > 1_000_000 else f"{v/1_000:.1f}K"
            message += f"**{i+1}. ${c['symbol'].replace('USDT', '')}:** (الحجم: `${vs}`)\n"
            
    await context.bot.edit_message_text(message, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.MARKDOWN)

async def run_top_gainers(c, cid, mid, cl): await run_top_movers(c, cid, mid, cl, 'gainers')
async def run_top_losers(c, cid, mid, cl): await run_top_movers(c, cid, mid, cl, 'losers')
async def run_top_volume(c, cid, mid, cl): await run_top_movers(c, cid, mid, cl, 'volume')

# =============================================================================
# --- 5. المهام الآلية الدورية ---
# =============================================================================
def add_to_monitoring(symbol, alert_price, alert_time, source, exchange_name):
    if exchange_name not in PLATFORMS: return
    if symbol not in performance_tracker.get(exchange_name, {}):
        performance_tracker[exchange_name][symbol] = {'symbol': symbol, 'alert_price': alert_price, 'alert_time': alert_time, 'source': source, 'current_price': alert_price, 'high_price': alert_price, 'status': 'Tracking', 'momentum_lost_alerted': False}
        logger.info(f"PERFORMANCE TRACKING STARTED for {symbol} on {exchange_name}")

async def fomo_hunter_task(client: BaseExchangeClient, bot: Bot):
    logger.info(f"Fomo Hunter task started for {client.name}.")
    while True:
        if not bot.application.bot_data.get('background_tasks_enabled', True): await asyncio.sleep(60); continue
        logger.info(f"===== Fomo Hunter ({client.name}): Starting Scan =====")
        try:
            momentum_coins = await helper_get_momentum_symbols(client)
            if not momentum_coins: logger.info(f"Fomo Hunter ({client.name}): No momentum."); await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60); continue
            now = datetime.now(UTC); new_alerts = []
            for s, d in momentum_coins.items():
                if not recently_alerted_fomo.get(client.name, {}).get(s) or (now - recently_alerted_fomo[client.name][s]) > timedelta(minutes=RUN_FOMO_SCAN_EVERY_MINUTES * 4):
                     new_alerts.append(d); recently_alerted_fomo[client.name][s] = now
            if new_alerts:
                sorted_coins = sorted(new_alerts, key=lambda x: x['price_change'], reverse=True)
                message = f"🚨 **تنبيه تلقائي من صياد الفومو ({client.name})** 🚨\n\n"
                for i, c in enumerate(sorted_coins[:5]): message += f"**{i+1}. ${c['symbol'].replace('USDT', '')}**\n   - زخم: `%{c['price_change']:+.2f}`\n\n"
                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
                for c in sorted_coins[:5]: add_to_monitoring(c['symbol'], float(c['current_price']), now, f"صياد الفومو ({client.name})", client.name)
        except Exception as e: logger.error(f"Error in fomo_hunter_task for {client.name}: {e}", exc_info=True)
        await asyncio.sleep(RUN_FOMO_SCAN_EVERY_MINUTES * 60)

async def new_listings_sniper_task(client: BaseExchangeClient, bot: Bot):
    logger.info(f"Listings Sniper task started for {client.name}.")
    try:
        initial_data = await client.get_market_data()
        if initial_data: known_symbols[client.name] = {s['symbol'] for s in initial_data}; logger.info(f"Sniper for {client.name}: Initialized with {len(known_symbols[client.name])} symbols.")
    except Exception as e: logger.error(f"Failed to initialize sniper for {client.name}: {e}")
    while True:
        if not bot.application.bot_data.get('background_tasks_enabled', True): await asyncio.sleep(60); continue
        try:
            data = await client.get_market_data()
            if data:
                current_symbols = {s['symbol'] for s in data}
                if known_symbols[client.name]:
                    newly_listed = current_symbols - known_symbols[client.name]
                    if newly_listed:
                        for s in newly_listed:
                            logger.info(f"Sniper ({client.name}): NEW LISTING: {s}")
                            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"🎯 **إدراج جديد على {client.name}:** `${s}`", parse_mode=ParseMode.MARKDOWN)
                        known_symbols[client.name].update(newly_listed)
                else: known_symbols[client.name] = current_symbols
        except Exception as e: logger.error(f"Error in new_listings_sniper_task for {client.name}: {e}")
        await asyncio.sleep(RUN_LISTING_SCAN_EVERY_SECONDS)
            
async def performance_tracker_task(session: aiohttp.ClientSession, bot: Bot):
    logger.info("Performance Tracker task started.")
    while True:
        now = datetime.now(UTC)
        tracker_snapshot = copy.deepcopy(performance_tracker)
        for platform, symbols in tracker_snapshot.items():
            for symbol, data in symbols.items():
                if now - data['alert_time'] > timedelta(hours=PERFORMANCE_TRACKING_DURATION_HOURS):
                    if performance_tracker.get(platform, {}).get(symbol): performance_tracker[platform][symbol]['status'] = 'Archived'
                    continue
                if data.get('status') == 'Archived':
                    if performance_tracker.get(platform, {}).get(symbol): del performance_tracker[platform][symbol]
                    continue
                try:
                    client = get_exchange_client(platform, session)
                    current_price = await client.get_current_price(symbol)
                    if current_price and performance_tracker.get(platform, {}).get(symbol):
                        entry = performance_tracker[platform][symbol]
                        entry['current_price'] = current_price
                        if current_price > entry.get('high_price', 0): entry['high_price'] = current_price
                        hp = entry['high_price']
                        if "الزخم" in data.get('source', '') or "الفومو" in data.get('source', '') and not data.get('momentum_lost_alerted', False) and hp > 0:
                            drop = ((current_price - hp) / hp) * 100
                            if drop <= MOMENTUM_LOSS_THRESHOLD_PERCENT:
                                msg = (f"⚠️ **تنبيه: فقدان الزخم لـ ${symbol.replace('USDT','')}** ({platform})\n"
                                       f"   - أعلى سعر: `${format_price(hp)}`\n"
                                       f"   - هبوط: `{drop:.2f}%`")
                                await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)
                                entry['momentum_lost_alerted'] = True
                except Exception as e: logger.error(f"Error updating price for {symbol} on {platform}: {e}")
        await asyncio.sleep(RUN_PERFORMANCE_TRACKER_EVERY_MINUTES * 60)

# =============================================================================
# --- 6. تشغيل البوت ---
# =============================================================================
async def post_init(application: Updater.application):
    """
    وظيفة تعمل بعد بدء تشغيل البوت لإطلاق المهام الخلفية.
    """
    bot = application.bot
    session = application.bot_data['session']
    
    # إرسال رسالة بدء التشغيل
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="✅ **بوت التداول الذكي (v16 - Stable) متصل الآن!**\n\nأرسل /start لعرض القائمة.", parse_mode=ParseMode.MARKDOWN)
        logger.info("Startup message sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

    # إطلاق المهام الخلفية
    background_tasks['performance'] = asyncio.create_task(performance_tracker_task(session, bot))
    for platform_name in PLATFORMS:
        client = get_exchange_client(platform_name, session)
        background_tasks[f'fomo_{platform_name}'] = asyncio.create_task(fomo_hunter_task(client, bot))
        background_tasks[f'listings_{platform_name}'] = asyncio.create_task(new_listings_sniper_task(client, bot))
    logger.info("All background tasks have been scheduled.")


def main():
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_TELEGRAM' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or chat ID are not set."); return

    # استخدام aiohttp.ClientSession لجميع طلبات الشبكة
    session = aiohttp.ClientSession()

    # بناء التطبيق بطريقة نظيفة
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # إضافة البيانات المشتركة (مثل جلسة الشبكة) إلى التطبيق
    application.bot_data['session'] = session
    application.bot_data['background_tasks_enabled'] = True
    
    # إضافة معالجات الأوامر
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(Filters.text & (~Filters.command), handle_button_press))
    
    # جدولة المهام لتعمل بعد بدء التشغيل الكامل
    application.post_init = post_init
    
    # تشغيل البوت
    logger.info("Starting bot polling...")
    application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logger.critical(f"Bot failed to run: {e}", exc_info=True)