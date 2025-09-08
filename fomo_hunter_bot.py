# -*- coding: utf-8 -*-
# ======================================================================================================================
# == Hybrid Hunter Bot v2.4 | The Professional Version ===============================================================
# ======================================================================================================================
#
# v2.4 "إصلاحات جذرية" Changelog:
# - CRITICAL FIX (Task Locking): تم إصلاح الخلل الذي كان يمنع نظام قفل المهام من العمل. الآن سيتم منع تشغيل الأوامر المتزامنة بشكل صحيح وستظهر رسالة الانتظار.
# - CRITICAL FIX (Pro Scan): تم إصلاح الخطأ الفادح الذي كان يسبب تعطل "الفحص الاحترافي". أصبح الأمر الآن يعمل بثبات.
# - CRITICAL FIX (Trade Tracking): تم إصلاح مشكلة ظهور "N/A" لسعر الصفقات من منصات غير Binance (مثل Mexc). سيتم الآن جلب جميع الأسعار بشكل صحيح.
# - STABILITY: تحسينات عامة في معالجة الأخطاء.
#
# ======================================================================================================================

# --- المكتبات الأساسية ---
import os
import asyncio
import sqlite3
import json
import logging
import time
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional
from functools import wraps

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd
import pandas_ta as ta
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

# =============================================================================
# --- ⚙️ 1. الإعدادات والتهيأة العامة ⚙️ ---
# =============================================================================

# --- إعدادات التليجرام والملفات ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_CHAT_ID')

DATABASE_FILE = "hybrid_hunter.db"
SETTINGS_FILE = "hybrid_settings.json"
LOG_FILE = "hybrid_hunter.log"


# --- إعداد مسجل الأحداث (Logger) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, 'w'), logging.StreamHandler()]
)
logging.getLogger('ccxt').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- إعدادات البوت العامة ---
PLATFORMS = ["Binance", "MEXC", "Gateio", "Bybit", "KuCoin", "OKX"]
SCAN_INTERVAL_MINUTES = 15
TRACK_INTERVAL_MINUTES = 2
UNWANTED_SYMBOL_SUBSTRINGS = ['UP/', 'DOWN/', '3L/', '5L/', '3S/', '5S/', 'BEAR/', 'BULL/', '/USDC', '/FDUSD', '/DAI']
TELEGRAM_MAX_MSG_LENGTH = 4096


# --- الأنماط الجاهزة (Presets) ---
PRESETS = {
    "STRICT": {
        "min_quote_volume_24h_usd": 5000000, "max_spread_percent": 0.2, "min_rvol": 2.0,
        "min_atr_percent": 1.5, "sniper_max_volatility_percent": 12.0
    },
    "BALANCED": {
        "min_quote_volume_24h_usd": 1000000, "max_spread_percent": 0.5, "min_rvol": 1.5,
        "min_atr_percent": 1.0, "sniper_max_volatility_percent": 18.0
    },
    "LAX": {
        "min_quote_volume_24h_usd": 250000, "max_spread_percent": 1.0, "min_rvol": 1.1,
        "min_atr_percent": 0.5, "sniper_max_volatility_percent": 25.0
    }
}

# --- الإعدادات الافتراضية ---
DEFAULT_SETTINGS = {
    "background_tasks_enabled": True,
    "active_manual_exchange": "Binance",
    "active_preset_name": "BALANCED",
    "active_scanners": ["sniper_pro", "momentum_breakout", "whale_radar"],
    "max_concurrent_trades": 75, # Increased limit
    "virtual_trade_size_usdt": 100.0,
    "use_master_trend_filter": True,
    "master_trend_tf": "4h",
    "master_trend_ma": 50,
    "use_trailing_sl": True,
    "trailing_sl_activation_percent": 1.5,
    "trailing_sl_callback_percent": 1.0,
    "risk_reward_ratio": 2.0,
    "atr_sl_multiplier": 2.5,
    "filters": PRESETS["BALANCED"],
    "sniper_compression_hours": 6,
    "whale_wall_threshold_usdt": 30000,
    "gem_min_correction_percent": -70.0,
    "gem_min_24h_volume_usdt": 200000,
    "gem_min_rise_from_atl_percent": 50.0,
    "gem_listing_since_days": 365,
    "pro_scan_min_score": 5,
}

# --- متغيرات الحالة العامة ---
bot_state = {
    "exchanges": {},
    "settings": {},
    "scan_in_progress": False,
    "user_task_in_progress": False, # NEW: for task locking
    "recently_alerted": {},
}
api_semaphore = asyncio.Semaphore(10)

# =============================================================================
# ---  Decorator for Task Locking ---
# =============================================================================
def user_task_lock(func):
    """Decorator to prevent concurrent execution of user-initiated long tasks."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if bot_state.get('user_task_in_progress', False):
            await update.message.reply_text("⏳ يوجد أمر آخر قيد التنفيذ حالياً. يرجى الانتظار حتى يكتمل.")
            return
        try:
            bot_state['user_task_in_progress'] = True
            return await func(update, context, *args, **kwargs)
        finally:
            bot_state['user_task_in_progress'] = False
    return wrapper

# =============================================================================
# --- 🗄️ 2. إدارة الإعدادات وقاعدة البيانات 🗄️ ---
# =============================================================================

def load_settings():
    """تحميل الإعدادات من ملف JSON ومطابقتها مع الإعدادات الافتراضية."""
    global DEFAULT_SETTINGS
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                saved_settings = json.load(f)
            
            bot_state["settings"] = DEFAULT_SETTINGS.copy()
            bot_state["settings"].update(saved_settings)

            preset = bot_state["settings"]["active_preset_name"]
            if preset in PRESETS:
                 bot_state["settings"]["filters"] = PRESETS[preset]
            logger.info("Settings loaded and synchronized successfully.")
        else:
            bot_state["settings"] = DEFAULT_SETTINGS.copy()
            save_settings()
            logger.info("No settings file found, using default settings.")
    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        bot_state["settings"] = DEFAULT_SETTINGS.copy()

def save_settings():
    """حفظ الإعدادات الحالية في ملف JSON."""
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(bot_state["settings"], f, indent=4)
        logger.info("Settings saved successfully.")
    except Exception as e:
        logger.error(f"Failed to save settings: {e}")

def setup_database():
    """إنشاء قاعدة البيانات والجداول إذا لم تكن موجودة."""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                exchange TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                entry_price REAL NOT NULL,
                initial_stop_loss REAL NOT NULL,
                current_stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                trade_value_usdt REAL NOT NULL,
                status TEXT NOT NULL,
                exit_price REAL,
                closed_at TEXT,
                pnl_usdt REAL,
                pnl_percent REAL,
                highest_price REAL,
                trailing_sl_active BOOLEAN DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()
        logger.info("Database is set up and ready.")
    except Exception as e:
        logger.error(f"Database setup failed: {e}")

# =============================================================================
# --- 🌐 3. محرك الاتصال بالمنصات (CCXT) 🌐 ---
# =============================================================================

async def initialize_exchanges():
    """تهيئة الاتصال بجميع المنصات باستخدام CCXT."""
    for ex_id in PLATFORMS:
        try:
            exchange_class = getattr(ccxt, ex_id.lower())
            exchange = exchange_class({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
            await exchange.load_markets()
            bot_state["exchanges"][ex_id] = exchange
            logger.info(f"Successfully connected to {ex_id}.")
        except Exception as e:
            logger.error(f"Failed to connect to {ex_id}: {e}")

async def safe_fetch_ohlcv(exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int) -> Optional[List]:
    """جلب بيانات الشموع مع معالجة الأخطاء."""
    async with api_semaphore:
        try:
            return await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        except ccxt.BadSymbol:
            logger.warning(f"Symbol {symbol} not found on {exchange.id}.")
            return None
        except Exception as e:
            logger.warning(f"Could not fetch OHLCV for {symbol} on {exchange.id}: {e}")
            return None

async def get_current_price(exchange: ccxt.Exchange, symbol: str) -> Optional[float]:
    """جلب السعر الحالي للعملة."""
    try:
        ticker = await exchange.fetch_ticker(symbol)
        return ticker['last'] if ticker and 'last' in ticker else None
    except Exception as e:
        logger.warning(f"Could not fetch current price for {symbol} on {exchange.id}: {e}")
        return None

# =============================================================================
# --- 🔬 4. وحدات التحليل الفني والكمي 🔬 ---
# =============================================================================

def find_support_resistance(high_prices, low_prices, window=10):
    supports, resistances = [], []
    for i in range(window, len(high_prices) - window):
        if high_prices[i] == max(high_prices[i-window:i+window+1]): resistances.append(high_prices[i])
        if low_prices[i] == min(low_prices[i-window:i+window+1]): supports.append(low_prices[i])
    return sorted(list(set(supports)), reverse=True), sorted(list(set(resistances)), reverse=True)

def analyze_trend(current_price, ema21, ema50, sma100):
    if ema21 and ema50 and sma100:
        if current_price > ema21 > ema50 > sma100: return "🟢 اتجاه صاعد قوي.", 2
        if current_price > ema50 and current_price > ema21: return "🟢 اتجاه صاعد.", 1
        if current_price < ema21 < ema50 < sma100: return "🔴 اتجاه هابط قوي.", -2
        if current_price < ema50 and current_price < ema21: return "🔴 اتجاه هابط.", -1
    return "🟡 جانبي / غير واضح.", 0

def format_price(price):
    try: 
        price_float = float(price)
        if price_float < 1e-4: return f"{price_float:.10f}".rstrip('0')
        return f"{price_float:.8g}"
    except (ValueError, TypeError): return price

# =============================================================================
# --- 🛡️ 5. نظام الفلترة المتقدم 🛡️ ---
# =============================================================================

def is_symbol_unwanted(symbol: str) -> bool:
    """Universal filter for leveraged tokens and other unwanted pairs."""
    return any(sub in symbol.upper() for sub in UNWANTED_SYMBOL_SUBSTRINGS)

async def pre_scan_filter(exchange: ccxt.Exchange) -> List[Dict]:
    """
    يقوم بالفلترة الأولية للسوق بناءً على السيولة والتقلب والاتجاه العام.
    يعيد قائمة بالعملات المرشحة لمزيد من التحليل.
    """
    logger.info(f"[{exchange.id}] Starting pre-scan filter...")
    filters_cfg = bot_state["settings"]["filters"]
    try:
        tickers = await exchange.fetch_tickers()
        if not tickers: return []
    except Exception as e:
        logger.error(f"[{exchange.id}] Could not fetch tickers for filtering: {e}")
        return []

    candidates = []
    for symbol, ticker in tickers.items():
        if (symbol.endswith('/USDT') and 
            not is_symbol_unwanted(symbol) and
            ticker.get('quoteVolume') and ticker['quoteVolume'] > filters_cfg['min_quote_volume_24h_usd'] and
            ticker.get('bid') and ticker.get('ask') and ticker.get('ask') > 0):
            
            spread = (ticker['ask'] - ticker['bid']) / ticker['ask'] * 100
            if spread < filters_cfg['max_spread_percent']:
                candidates.append({'symbol': symbol, 'ticker': ticker})

    logger.info(f"[{exchange.id}] Found {len(candidates)} candidates after initial volume and spread filter.")
    
    final_candidates = []
    binance_exchange = bot_state["exchanges"].get("Binance")
    if not binance_exchange:
        logger.error("Binance exchange not found for master trend, proceeding without it.")
        btc_trend_is_bullish = True
    else:
        btc_trend_is_bullish = await get_master_trend(binance_exchange)
    
    if bot_state["settings"]["use_master_trend_filter"] and not btc_trend_is_bullish:
        logger.warning(f"[{exchange.id}] Master Trend Filter is ACTIVE and market is BEARISH. Skipping deep scan.")
        return []

    for candidate in candidates:
        symbol = candidate['symbol']
        await asyncio.sleep(exchange.rateLimit / 1000) 
        ohlcv = await safe_fetch_ohlcv(exchange, symbol, '15m', 50)
        if not ohlcv or len(ohlcv) < 30: continue
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        avg_volume = df['volume'].rolling(window=20).mean().iloc[-2]
        rvol = df['volume'].iloc[-2] / avg_volume if avg_volume > 0 else 0
        if rvol < filters_cfg['min_rvol']: continue
        
        atr = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
        atr_percent = (atr / df['close'].iloc[-1]) * 100 if df['close'].iloc[-1] > 0 else 0
        if atr_percent < filters_cfg['min_atr_percent']: continue
        
        final_candidates.append({'symbol': symbol, 'df_15m': df})

    logger.info(f"[{exchange.id}] Pre-scan complete. Found {len(final_candidates)} high-quality candidates.")
    return final_candidates

async def get_master_trend(exchange: ccxt.Exchange) -> bool:
    """يحدد الاتجاه العام للسوق من خلال تحليل BTC/USDT."""
    settings = bot_state["settings"]
    try:
        ohlcv = await safe_fetch_ohlcv(exchange, 'BTC/USDT', settings["master_trend_tf"], settings["master_trend_ma"] + 5)
        if not ohlcv: return True 
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        sma = ta.sma(df['close'], length=settings["master_trend_ma"]).iloc[-1]
        current_price = df['close'].iloc[-1]
        
        is_bullish = current_price > sma
        logger.info(f"Master Trend (BTC/{settings['master_trend_tf']}): {'BULLISH' if is_bullish else 'BEARISH'} (Price: {current_price:.0f}, SMA{settings['master_trend_ma']}: {sma:.0f})")
        return is_bullish
    except Exception as e:
        logger.error(f"Failed to get master trend: {e}")
        return True

# =============================================================================
# --- 🎯 6. محركات الاستراتيجيات الهجينة 🎯 ---
# =============================================================================

async def run_sniper_pro_scan(exchange: ccxt.Exchange, candidate: Dict) -> Optional[Dict]:
    """استراتيجية القناص المدمجة: تبحث عن انضغاط سعري ثم اختراق مؤكد."""
    symbol = candidate['symbol']
    filters_cfg = bot_state["settings"]["filters"]
    compression_candles = int(bot_state["settings"]["sniper_compression_hours"] * 4)

    ohlcv = await safe_fetch_ohlcv(exchange, symbol, '15m', compression_candles + 20)
    if not ohlcv or len(ohlcv) < compression_candles: return None

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    compression_df = df.iloc[-compression_candles-1:-1]
    highest_high = compression_df['high'].max()
    lowest_low = compression_df['low'].min()
    
    volatility = (highest_high - lowest_low) / lowest_low * 100 if lowest_low > 0 else float('inf')
    
    if volatility < filters_cfg["sniper_max_volatility_percent"]:
        last_candle = df.iloc[-2]
        if last_candle['close'] > highest_high:
            avg_volume = compression_df['volume'].mean()
            if last_candle['volume'] > avg_volume * 2:
                return {"strategy": "Sniper Pro", "entry_price": last_candle['close']}
    return None

async def run_momentum_breakout_scan(exchange: ccxt.Exchange, candidate: Dict) -> Optional[Dict]:
    """[FIXED] استراتيجية اختراق الزخم."""
    df = candidate['df_15m'].copy()
    
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.bbands(length=20, std=2, append=True)
    
    if not all(col in df.columns for col in ['MACD_12_26_9', 'MACDs_12_26_9', 'BBU_20_2.0', 'RSI_14']):
        logger.warning(f"Could not calculate all required indicators for {candidate['symbol']}.")
        return None

    last = df.iloc[-2]
    prev = df.iloc[-3]
    
    macd_crossed_up = prev['MACD_12_26_9'] <= prev['MACDs_12_26_9'] and last['MACD_12_26_9'] > last['MACDs_12_26_9']
    price_broke_bb = last['close'] > last['BBU_20_2.0']
    rsi_not_overbought = last['RSI_14'] < 70
    
    if macd_crossed_up and price_broke_bb and rsi_not_overbought:
        return {"strategy": "Momentum Breakout", "entry_price": last['close']}
    return None

async def run_whale_radar_scan(exchange: ccxt.Exchange, candidate: Dict) -> Optional[Dict]:
    """رادار الحيتان: يبحث عن جدران الشراء الكبيرة."""
    symbol = candidate['symbol']
    threshold = bot_state["settings"]["whale_wall_threshold_usdt"]
    try:
        ob = await exchange.fetch_order_book(symbol, limit=20)
        if not ob: return None
        
        bids = ob.get('bids', [])
        total_bid_value = sum(float(price) * float(qty) for price, qty in bids[:10])

        if total_bid_value > threshold:
            return {"strategy": f"Whale Radar ({total_bid_value/1000:.0f}K)", "entry_price": candidate['df_15m']['close'].iloc[-1]}
    except Exception as e:
        logger.warning(f"Whale radar failed for {symbol} on {exchange.id}: {e}")
    return None

ACTIVE_SCANNERS = {
    "sniper_pro": run_sniper_pro_scan,
    "momentum_breakout": run_momentum_breakout_scan,
    "whale_radar": run_whale_radar_scan,
}

# =============================================================================
# --- 📈 7. محرك المسح، إدارة الصفقات، والتتبع 📈 ---
# =============================================================================

async def perform_scan_and_trade(context: ContextTypes.DEFAULT_TYPE):
    """الوظيفة الرئيسية التي تقوم بالمسح المتكامل وإدارة الصفقات."""
    if not bot_state["settings"].get("background_tasks_enabled", True):
        logger.info("Background tasks are disabled. Skipping scan.")
        return
        
    if bot_state["scan_in_progress"]:
        logger.warning("Scan is already in progress. Skipping.")
        return
    bot_state["scan_in_progress"] = True
    logger.info("================== 🚀 Starting New Hybrid Scan Cycle 🚀 ==================")
    
    settings = bot_state["settings"]
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        active_trades_count = cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'Active'").fetchone()[0]
        conn.close()
        
        if active_trades_count >= settings["max_concurrent_trades"]:
            logger.info(f"Max concurrent trades ({settings['max_concurrent_trades']}) reached. Skipping new signals scan.")
        else:
            for ex_id, exchange in bot_state["exchanges"].items():
                if not exchange.markets: continue
                
                high_quality_candidates = await pre_scan_filter(exchange)
                
                for candidate in high_quality_candidates:
                    for scanner_name in settings["active_scanners"]:
                        scanner_func = ACTIVE_SCANNERS.get(scanner_name)
                        if not scanner_func: continue
                        
                        signal = await scanner_func(exchange, candidate)
                        
                        if signal:
                            now = time.time()
                            if (now - bot_state["recently_alerted"].get(candidate['symbol'], 0)) < SCAN_INTERVAL_MINUTES * 60 * 2:
                                logger.info(f"Skipping signal for {candidate['symbol']} due to cooldown.")
                                continue

                            await process_new_signal(context.bot, exchange, candidate['symbol'], signal)
                            bot_state["recently_alerted"][candidate['symbol']] = now
                            break
    except Exception as e:
        logger.error(f"Critical error during scan cycle: {e}", exc_info=True)
    finally:
        bot_state["scan_in_progress"] = False
        logger.info("================== ✅ Hybrid Scan Cycle Finished ✅ ==================")

async def process_new_signal(bot, exchange: ccxt.Exchange, symbol: str, signal: Dict):
    """معالجة إشارة جديدة: حساب المخاطر، تسجيلها، وإرسال تنبيه."""
    settings = bot_state["settings"]
    entry_price = signal['entry_price']
    
    # FIX: Find the correctly capitalized exchange name to store in DB
    exchange_name_to_store = next((p for p in PLATFORMS if p.lower() == exchange.id.lower()), exchange.id.capitalize())

    try:
        ohlcv = await safe_fetch_ohlcv(exchange, symbol, '15m', 20)
        if not ohlcv: return
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        atr = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
        
        stop_loss = entry_price - (atr * settings["atr_sl_multiplier"])
        take_profit = entry_price + ((entry_price - stop_loss) * settings["risk_reward_ratio"])
    except Exception:
        sl_percent = 2.0
        stop_loss = entry_price * (1 - sl_percent / 100)
        take_profit = entry_price * (1 + (sl_percent * settings["risk_reward_ratio"]) / 100)

    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trades (timestamp, exchange, symbol, strategy, entry_price, initial_stop_loss, current_stop_loss, take_profit, trade_value_usdt, status, highest_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            exchange_name_to_store, symbol, signal['strategy'], entry_price, stop_loss, stop_loss, take_profit,
            settings['virtual_trade_size_usdt'], 'Active', entry_price
        ))
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        message = (
            f"🎯 **توصية صفقة جديدة** 🎯\n\n"
            f"▫️ **العملة:** `{symbol}`\n"
            f"▫️ **المنصة:** *{exchange_name_to_store}*\n"
            f"▫️ **الاستراتيجية:** `{signal['strategy']}`\n"
            f"━━━━━━━━━━━━━━\n"
            f"📈 **سعر الدخول:** `{format_price(entry_price)}`\n"
            f"🎯 **الهدف:** `{format_price(take_profit)}`\n"
            f"🛑 **الوقف:** `{format_price(stop_loss)}`\n\n"
            f"*ID: {trade_id}*"
        )
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Successfully processed and sent signal for {symbol} (ID: {trade_id}) on {exchange_name_to_store}")

    except Exception as e:
        logger.error(f"Failed to process signal for {symbol}: {e}")

async def track_active_trades(context: ContextTypes.DEFAULT_TYPE):
    """متابعة الصفقات النشطة لتحديث وقف الخسارة المتحرك وتفعيل الأهداف."""
    if not bot_state["settings"].get("background_tasks_enabled", True):
        return

    settings = bot_state["settings"]
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        active_trades = cursor.execute("SELECT * FROM trades WHERE status = 'Active'").fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"DB error in track_active_trades: {e}")
        return

    for trade_row in active_trades:
        trade = dict(trade_row)
        exchange = bot_state["exchanges"].get(trade['exchange'])
        if not exchange:
            logger.warning(f"Could not find active exchange '{trade['exchange']}' for trade #{trade['id']}. Skipping.")
            continue

        current_price = await get_current_price(exchange, trade['symbol'])
        if not current_price:
            logger.warning(f"Could not fetch price for {trade['symbol']} on {trade['exchange']} for tracking.")
            continue
            
        logger.info(f"Tracking #{trade['id']} ({trade['symbol']}): Price={current_price:.4f}, TP={trade['take_profit']:.4f}, SL={trade['current_stop_loss']:.4f}")

        # Check for TP/SL triggers
        if current_price >= trade['take_profit']:
            await close_trade(context.bot, trade, current_price, 'Take Profit Hit')
            continue
        if current_price <= trade['current_stop_loss']:
            await close_trade(context.bot, trade, current_price, 'Stop Loss Hit')
            continue

        # Trailing Stop Loss Logic
        if settings["use_trailing_sl"]:
            highest_price = max(trade.get('highest_price', current_price), current_price)
            if not trade['trailing_sl_active']:
                # Activate TSL
                activation_price = trade['entry_price'] * (1 + settings['trailing_sl_activation_percent'] / 100)
                if current_price >= activation_price:
                    new_stop_loss = trade['entry_price'] # Move SL to entry
                    if new_stop_loss > trade['current_stop_loss']:
                         await update_trade_sl(context.bot, trade['id'], new_stop_loss, highest_price, is_activation=True)
            else:
                # Update active TSL
                new_stop_loss = highest_price * (1 - settings['trailing_sl_callback_percent'] / 100)
                if new_stop_loss > trade['current_stop_loss']:
                    await update_trade_sl(context.bot, trade['id'], new_stop_loss, highest_price)
            
            # Update peak price if it has increased
            if highest_price > trade.get('highest_price', 0):
                 await update_trade_peak_price(trade['id'], highest_price)

async def close_trade(bot, trade: Dict, exit_price: float, reason: str):
    """إغلاق الصفقة وتحديث قاعدة البيانات وإرسال إشعار."""
    pnl_usdt = (exit_price - trade['entry_price']) / trade['entry_price'] * trade['trade_value_usdt']
    pnl_percent = pnl_usdt / trade['trade_value_usdt'] * 100
    
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE trades SET status = ?, exit_price = ?, closed_at = ?, pnl_usdt = ?, pnl_percent = ? WHERE id = ?
        """, ('Closed', exit_price, datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), pnl_usdt, pnl_percent, trade['id']))
        conn.commit()
        conn.close()

        icon = "✅" if pnl_usdt >= 0 else "❌"
        message = (
            f"{icon} **تم إغلاق الصفقة #{trade['id']}** {icon}\n\n"
            f"▫️ **العملة:** `{trade['symbol']}`\n"
            f"▫️ **السبب:** *{reason}*\n"
            f"▫️ **الربح/الخسارة:** `${pnl_usdt:+.2f}` (`{pnl_percent:+.2f}%`)"
        )
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
        logger.info(f"Trade {trade['id']} ({trade['symbol']}) closed. Reason: {reason}. PnL: ${pnl_usdt:.2f}")

    except Exception as e:
        logger.error(f"Failed to close trade {trade['id']}: {e}")

async def update_trade_sl(bot, trade_id: int, new_sl: float, highest_price: float, is_activation: bool = False):
    """تحديث وقف الخسارة في قاعدة البيانات وإرسال إشعار."""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE trades SET current_stop_loss = ?, highest_price = ?, trailing_sl_active = 1 WHERE id = ?", (new_sl, highest_price, trade_id))
        conn.commit()
        conn.close()
        
        if is_activation:
            message = f"🔒 **تأمين صفقة (ID: {trade_id})** 🔒\nتم نقل وقف الخسارة إلى نقطة الدخول: `{format_price(new_sl)}`"
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode=ParseMode.MARKDOWN)
            logger.info(f"Trailing SL activated for trade {trade_id}. New SL: {new_sl}")
        else:
             logger.info(f"Trailing SL updated for trade {trade_id}. New SL: {new_sl}")
    except Exception as e:
         logger.error(f"Failed to update SL for trade {trade_id}: {e}")

async def update_trade_peak_price(trade_id: int, highest_price: float):
    """تحديث أعلى سعر وصلت إليه الصفقة فقط."""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE trades SET highest_price = ? WHERE id = ?", (highest_price, trade_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to update peak price for trade {trade_id}: {e}")

# =============================================================================
# --- 🔍 8. وظائف التحليل عند الطلب 🔍 ---
# =============================================================================
@user_task_lock
async def run_full_technical_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    try:
        symbol = context.args[0].upper()
        if not symbol.endswith('/USDT'):
            symbol += '/USDT'
    except IndexError:
        await update.message.reply_text("يرجى إرسال رمز العملة.")
        return

    ex_id = "Binance"
    exchange = bot_state["exchanges"].get(ex_id)
    if not exchange:
        await update.message.reply_text(f"لم يتم العثور على منصة {ex_id}.")
        return

    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"🔬 جارِ إجراء تحليل فني شامل لـ ${symbol} على {exchange.id}...")

    try:
        timeframes = {'يومي': '1d', '4 ساعات': '4h', 'ساعة': '1h'}
        report_parts = [f"📊 **التحليل الفني المفصل لـ ${symbol}** ({exchange.id})\n\n"]
        
        for tf_name, tf_interval in timeframes.items():
            ohlcv = await safe_fetch_ohlcv(exchange, symbol, tf_interval, 200)
            tf_report = f"--- **إطار {tf_name}** ---\n"
            if not ohlcv or len(ohlcv) < 100:
                tf_report += "لا توجد بيانات كافية للتحليل.\n\n"
                report_parts.append(tf_report)
                continue
            
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            current_price = df['close'].iloc[-1]
            report_lines = []
            
            ema21 = ta.ema(df['close'], length=21).iloc[-1]
            ema50 = ta.ema(df['close'], length=50).iloc[-1]
            sma100 = ta.sma(df['close'], length=100).iloc[-1]
            trend_text, _ = analyze_trend(current_price, ema21, ema50, sma100)
            report_lines.append(f"**الاتجاه:** {trend_text}")
            
            macd = df.ta.macd(fast=12, slow=26, signal=9)
            if macd is not None and not macd.empty:
                 if macd.iloc[-1]['MACD_12_26_9'] > macd.iloc[-1]['MACDs_12_26_9']: report_lines.append("🟢 **MACD:** إيجابي.")
                 else: report_lines.append("🔴 **MACD:** سلبي.")

            rsi = ta.rsi(df['close'], length=14).iloc[-1]
            if rsi:
                if rsi > 70: report_lines.append(f"🔴 **RSI ({rsi:.1f}):** تشبع شرائي.")
                elif rsi < 30: report_lines.append(f"🟢 **RSI ({rsi:.1f}):** تشبع بيعي.")
                else: report_lines.append(f"🟡 **RSI ({rsi:.1f}):** محايد.")

            supports, resistances = find_support_resistance(df['high'].to_numpy(), df['low'].to_numpy())
            if r := min([r for r in resistances if r > current_price], default=None): report_lines.append(f"🛡️ **أقرب مقاومة:** {format_price(r)}")
            if s := max([s for s in supports if s < current_price], default=None): report_lines.append(f"💰 **أقرب دعم:** {format_price(s)}")

            tf_report += "\n".join(report_lines) + f"\n*السعر الحالي: {format_price(current_price)}*\n\n"
            report_parts.append(tf_report)
        
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text="".join(report_parts), parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in full TA for {symbol}: {e}", exc_info=True)
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=f"حدث خطأ أثناء تحليل {symbol}. تأكد من صحة الرمز.")

@user_task_lock
async def run_scalp_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[IMPROVED] Provides a more detailed scalp analysis."""
    chat_id = update.message.chat_id
    try:
        symbol = context.args[0].upper()
        if not symbol.endswith('/USDT'):
            symbol += '/USDT'
    except IndexError:
        await update.message.reply_text("يرجى إرسال رمز العملة.")
        return

    ex_id = "Binance" 
    exchange = bot_state["exchanges"].get(ex_id)
    if not exchange:
        await update.message.reply_text(f"لم يتم العثور على منصة {ex_id}.")
        return

    sent_message = await context.bot.send_message(chat_id=chat_id, text=f"⚡️ جارِ إجراء تحليل سريع ومطور لـ ${symbol} على {exchange.id}...")

    try:
        timeframes = {'15 دقيقة': '15m', '5 دقائق': '5m'}
        report_parts = [f"⚡️ **التحليل السريع المطور لـ ${symbol}** ({exchange.id})\n"]
        final_summary_points = []

        for tf_name, tf_interval in timeframes.items():
            ohlcv = await safe_fetch_ohlcv(exchange, symbol, tf_interval, 50)
            tf_report = f"\n--- **إطار {tf_name}** ---\n"
            if not ohlcv or len(ohlcv) < 20:
                tf_report += "لا توجد بيانات كافية.\n\n"
                report_parts.append(tf_report)
                continue

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            current_price = df['close'].iloc[-1]
            
            # Volume analysis
            avg_volume = df['volume'][-20:-1].mean()
            last_volume = df['volume'].iloc[-1]
            vol_ratio = last_volume / avg_volume if avg_volume > 0 else 0
            if vol_ratio > 2.0:
                tf_report += f"🟢 **الفوليوم:** عالٍ ({vol_ratio:.1f}x).\n"
                if tf_interval == '5m': final_summary_points.append("فوليوم عالٍ على 5 دقائق")
            else: 
                tf_report += f"🟡 **الفوليوم:** عادي.\n"

            # Short-term trend analysis
            ema9 = ta.ema(df['close'], length=9).iloc[-1]
            if current_price > ema9:
                tf_report += f"🟢 **الاتجاه القصير:** إيجابي (فوق EMA9).\n"
                if tf_interval == '5m': final_summary_points.append("اتجاه إيجابي قصير")
            else:
                tf_report += f"🔴 **الاتجاه القصير:** سلبي (تحت EMA9).\n"
            
            # Volatility analysis
            atr = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
            atr_percent = (atr / current_price) * 100
            if atr_percent > 0.5:
                 tf_report += f"🟢 **التقلب (ATR):** جيد للمضاربة ({atr_percent:.2f}%).\n"
                 if tf_interval == '5m': final_summary_points.append("تقلب جيد")
            else:
                 tf_report += f"🟡 **التقلب (ATR):** ضعيف ({atr_percent:.2f}%).\n"

            report_parts.append(tf_report)
        
        # Final Summary
        summary = "\n--- **الخلاصة** ---\n"
        if "فوليوم عالٍ على 5 دقائق" in final_summary_points and "اتجاه إيجابي قصير" in final_summary_points:
            summary += "🟢 **زخم صاعد قوي،** قد تكون مناسبة للمراقبة والدخول السريع."
        elif "اتجاه إيجابي قصير" in final_summary_points:
            summary += "🟡 **إيجابية نسبية،** لكن تحتاج تأكيد من الفوليوم."
        else:
            summary += "🔴 **لا يوجد زخم واضح حالياً،** يفضل الانتظار."
        report_parts.append(summary)

        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text="".join(report_parts), parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error in scalp analysis for {symbol}: {e}")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=sent_message.message_id, text=f"حدث خطأ أثناء تحليل {symbol}.")

@user_task_lock
async def run_gem_hunter_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[IMPROVEMENT] Scans only the active manual exchange for potential gems."""
    ex_id = context.user_data.get('active_manual_exchange', 'Binance')
    exchange = bot_state["exchanges"].get(ex_id)
    if not exchange:
        await update.message.reply_text(f"المنصة المحددة '{ex_id}' غير متصلة.")
        return

    await update.message.reply_text(f"💎 **صائد الجواهر**\n\n🔍 جارِ تنفيذ مسح عميق على منصة **{ex_id}**...")
    
    settings = bot_state["settings"]
    listing_since = datetime.now(timezone.utc) - timedelta(days=settings["gem_listing_since_days"])
    
    async def scan_exchange_for_gems(ex_id, exchange):
        try:
            tickers = await exchange.fetch_tickers()
            if not tickers: return []
            platform_gems = []
            symbols_to_check = [s for s, t in tickers.items() if s.endswith('/USDT') and not is_symbol_unwanted(s) and t.get('quoteVolume') and t['quoteVolume'] > settings["gem_min_24h_volume_usdt"]]

            for symbol in symbols_to_check:
                await asyncio.sleep(exchange.rateLimit / 1000)
                ohlcv = await safe_fetch_ohlcv(exchange, symbol, '1d', 1000)
                if not ohlcv or len(ohlcv) < 30: continue
                
                first_candle_time = datetime.fromtimestamp(ohlcv[0][0] / 1000, timezone.utc)
                if first_candle_time < listing_since: continue

                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                ath, atl, current = df['high'].max(), df['low'].min(), df['close'].iloc[-1]
                
                if not all([ath, atl, current]) or ath <= 0 or atl <= 0 or current <= 0: continue

                correction = ((current - ath) / ath) * 100
                rise_from_atl = ((current - atl) / atl) * 100
                
                if correction <= settings["gem_min_correction_percent"] and rise_from_atl >= settings["gem_min_rise_from_atl_percent"]:
                    potential_x = ath / current
                    if potential_x < 1_000_000:
                        platform_gems.append({'symbol': symbol, 'potential_x': potential_x, 'correction_percent': correction})
            
            return sorted(platform_gems, key=lambda x: x['potential_x'], reverse=True)[:10]
        except Exception as e:
            logger.error(f"Error scanning gems on {ex_id}: {e}")
            return []

    gems = await scan_exchange_for_gems(ex_id, exchange)

    if not gems:
        await update.message.reply_text(f"✅ **بحث الجواهر اكتمل:** لم يتم العثور على عملات تتوافق مع الشروط على منصة {ex_id}.")
        return

    message = f"💎 **تقرير الجواهر المخفية ({ex_id})** 💎\n\n"
    for gem in gems:
        message += (f"**${gem['symbol'].replace('/USDT', '')}**\n"
                    f"  - 🚀 **العودة للقمة:** {gem['potential_x']:.1f}X\n"
                    f"  - 🩸 مصححة بنسبة: {gem['correction_percent']:.1f}%\n")
    await update.message.reply_text(message, parse_mode="Markdown")

async def get_top_movers_data(exchange: ccxt.Exchange, mode: str) -> List[str]:
    """Helper function to fetch and format top movers data."""
    modes = {
        "gainers": {"key": "change", "reverse": True},
        "losers": {"key": "change", "reverse": False},
        "volume": {"key": "quoteVolume", "reverse": True}
    }
    config = modes[mode]
    
    try:
        tickers = await exchange.fetch_tickers()
        valid_tickers = []
        for symbol, t in tickers.items():
            if t['symbol'].endswith('/USDT') and not is_symbol_unwanted(t['symbol']) and t.get('quoteVolume', 0) > 100000:
                if mode != 'volume':
                    open_price, last_price = t.get('open'), t.get('last')
                    if open_price and last_price and open_price > 0:
                        t['change'] = ((last_price - open_price) / open_price) * 100
                    else:
                        t['change'] = 0
                valid_tickers.append(t)
        
        sorted_tickers = sorted(valid_tickers, key=lambda x: x.get(config['key'], 0) or 0, reverse=config['reverse'])[:5] # Top 5 for summary
        
        report_lines = []
        for i, coin in enumerate(sorted_tickers):
            symbol_name = coin['symbol'].replace('/USDT', '')
            if mode != 'volume':
                stat = f"`%{coin.get('change', 0):+.2f}`"
            else:
                volume = coin.get('quoteVolume', 0)
                stat = f"`${volume/1_000_000:.2f}M`"
            report_lines.append(f"**{i+1}. ${symbol_name}:** {stat}")
        return report_lines
    except Exception as e:
        logger.error(f"Error in get_top_movers_data ({mode}): {e}")
        return ["حدث خطأ أثناء جلب البيانات."]

@user_task_lock
async def run_market_summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[NEW] Fetches gainers, losers, and volume in one consolidated report."""
    ex_id = context.user_data.get('active_manual_exchange', 'Binance')
    exchange = bot_state["exchanges"].get(ex_id)
    if not exchange:
        await update.message.reply_text(f"المنصة المحددة '{ex_id}' غير متصلة.")
        return
        
    await update.message.reply_text(f"📊 جارِ إعداد ملخص السوق لمنصة {ex_id}...")
    
    gainers_task = get_top_movers_data(exchange, 'gainers')
    losers_task = get_top_movers_data(exchange, 'losers')
    volume_task = get_top_movers_data(exchange, 'volume')

    gainers, losers, volume = await asyncio.gather(gainers_task, losers_task, volume_task)
    
    message = f"📊 **ملخص السوق لمنصة {ex_id}** 📊\n\n"
    message += "--- **📈 الأعلى ربحاً** ---\n" + "\n".join(gainers) + "\n\n"
    message += "--- **📉 الأعلى خسارة** ---\n" + "\n".join(losers) + "\n\n"
    message += "--- **💰 الأعلى تداولاً** ---\n" + "\n".join(volume)

    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

def calculate_pro_score(df: pd.DataFrame) -> Optional[Dict]:
    """Calculates a technical score from a given dataframe."""
    try:
        score = 0
        analysis = {}
        current_price = df['close'].iloc[-1]

        ema20 = ta.ema(df['close'], length=20).iloc[-1]
        ema50 = ta.ema(df['close'], length=50).iloc[-1]
        if current_price > ema20 > ema50: score += 2; analysis['Trend'] = "Strong Up"
        elif current_price > ema20: score += 1; analysis['Trend'] = "Up"
        else: analysis['Trend'] = "Sideways/Down"
        
        rsi = ta.rsi(df['close'], length=14).iloc[-1]
        analysis['RSI'] = f"{rsi:.1f}"
        if 30 < rsi < 65: score += 1
        if rsi >= 65: score += 2
        
        avg_vol = df['volume'][-20:-1].mean()
        last_vol = df['volume'].iloc[-1]
        if avg_vol > 0:
            vol_ratio = last_vol / avg_vol
            if vol_ratio > 1.5: score += 1
            if vol_ratio > 2.5: score += 1
            analysis['Vol Ratio'] = f"{vol_ratio:.1f}x"
        
        analysis['Score'] = score
        analysis['Price'] = format_price(current_price)
        return analysis
    except Exception:
        return None

@user_task_lock
async def run_pro_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Performs a professional scan with a scoring system."""
    ex_id = context.user_data.get('active_manual_exchange', 'Binance')
    exchange = bot_state["exchanges"].get(ex_id)
    if not exchange:
        await update.message.reply_text(f"المنصة المحددة '{ex_id}' غير متصلة.")
        return

    await update.message.reply_text(f"🎯 **الفحص الاحترافي**\n\n🔍 جارِ تحليل وتصنيف العملات على {ex_id}...")

    try:
        tickers = await exchange.fetch_tickers()
        candidates = [s for s, t in tickers.items() if s.endswith('/USDT') and not is_symbol_unwanted(s) and t.get('quoteVolume', 0) > 500000]

        strong_opportunities = []
        for symbol in candidates[:150]: # Limit for performance
             await asyncio.sleep(exchange.rateLimit / 2000)
             ohlcv = await safe_fetch_ohlcv(exchange, symbol, '15m', 100)
             if not ohlcv or len(ohlcv) < 50: continue
             
             df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
             result = calculate_pro_score(df)
             if result and result.get('Score', 0) >= bot_state["settings"]["pro_scan_min_score"]:
                 result['symbol'] = symbol
                 strong_opportunities.append(result)

        if not strong_opportunities:
            await update.message.reply_text(f"✅ **الفحص الاحترافي اكتمل:** لم يتم العثور على فرص قوية حالياً على {ex_id}.")
            return

        sorted_ops = sorted(strong_opportunities, key=lambda x: x['Score'], reverse=True)
        message = f"🎯 **أفضل الفرص حسب النقاط ({ex_id})** 🎯\n\n"
        for coin in sorted_ops[:10]:
            message += (f"**${coin['symbol'].replace('/USDT', '')}**\n"
                        f"    - **النقاط:** `{coin['Score']}` ⭐\n"
                        f"    - **السعر:** `${coin.get('Price', 'N/A')}`\n"
                        f"    - **الاتجاه:** `{coin.get('Trend', 'N/A')}` | **RSI:** `{coin.get('RSI', 'N/A')}`\n\n")
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in Pro Scan: {e}", exc_info=True)
        await update.message.reply_text("حدث خطأ فادح أثناء الفحص الاحترافي.")

# =============================================================================
# --- 🤖 10. أوامر التليجرام وواجهة المستخدم 🤖 ---
# =============================================================================

def build_main_menu() -> ReplyKeyboardMarkup:
    """Builds the main menu with a dynamic tasks toggle button."""
    tasks_enabled = bot_state["settings"].get("background_tasks_enabled", True)
    toggle_button_text = "إيقاف المهام 🔴" if tasks_enabled else "تفعيل المهام 🟢"
    
    keyboard = [
        ["🔬 تحليل فني", "⚡️ تحليل سريع"],
        ["🎯 فحص احترافي", "💎 صائد الجواهر"],
        ["📊 ملخص السوق"],
        ["📊 تقرير الأداء", "📈 الصفقات النشطة"],
        [toggle_button_text],
        ["⚙️ الإعدادات", "🔬 فحوصات يدوية"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

settings_menu_keyboard = [
    ["🎭 الماسحات الآلية", "🏁 أنماط جاهزة"],
    ["📊 منصة التقارير", "🩺 تقرير التشخيص"],
    ["🔙 القائمة الرئيسية"]
]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر البدء."""
    welcome_message = (
        "أهلاً بك في **بوت الصياد الهجين v2.4 (إصلاحات جذرية)**!\n\n"
        "تم تطبيق تطويرات جديدة بناءً على طلباتك."
    )
    context.user_data.setdefault('active_manual_exchange', 'Binance')
    context.user_data.setdefault('next_step', None)

    await update.message.reply_text(welcome_message, reply_markup=build_main_menu(), parse_mode=ParseMode.MARKDOWN)

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[FIXED] معالجة الرسائل النصية من القائمة الرئيسية."""
    text = update.message.text
    user_data = context.user_data
    next_step = user_data.get('next_step')

    # Handle stateful inputs first
    if next_step == 'get_ta_symbol':
        user_data['next_step'] = None
        context.args = [text.strip()]
        await run_full_technical_analysis(update, context)
        return
    if next_step == 'get_scalp_symbol':
        user_data['next_step'] = None
        context.args = [text.strip()]
        await run_scalp_analysis(update, context)
        return

    # Regular button handlers
    if text in ["إيقاف المهام 🔴", "تفعيل المهام 🟢"]: await toggle_tasks_command(update, context)
    elif text == "📊 تقرير الأداء": await performance_report_command(update, context)
    elif text == "📈 الصفقات النشطة": await active_trades_command(update, context)
    elif text == "⚙️ الإعدادات": await update.message.reply_text("اختر من قائمة الإعدادات:", reply_markup=ReplyKeyboardMarkup(settings_menu_keyboard, resize_keyboard=True))
    elif text == "🔬 فحوصات يدوية": await manual_scans_menu(update, context)
    elif text == "🎭 الماسحات الآلية": await scanners_menu_command(update, context)
    elif text == "🏁 أنماط جاهزة": await presets_menu_command(update, context)
    elif text == "📊 منصة التقارير": await select_manual_exchange_menu(update, context)
    elif text == "🩺 تقرير التشخيص": await run_diagnostic_report(update, context)
    elif text == "🔙 القائمة الرئيسية": await start_command(update, context)
    elif text == "🔬 تحليل فني":
        user_data['next_step'] = 'get_ta_symbol'
        await update.message.reply_text("🔬 يرجى إرسال رمز العملة للتحليل المعمق (مثال: `BTC`)")
    elif text == "⚡️ تحليل سريع":
        user_data['next_step'] = 'get_scalp_symbol'
        await update.message.reply_text("⚡️ يرجى إرسال رمز العملة للتحليل السريع (مثال: `PEPE`)")
    elif text == "💎 صائد الجواهر": await run_gem_hunter_scan(update, context)
    elif text == "🎯 فحص احترافي": await run_pro_scan(update, context)
    elif text == "📊 ملخص السوق": await run_market_summary_command(update, context)
    # Manual Scan Handlers
    elif text == "فحص القناص الآن": await run_manual_scanner(update, context, "sniper_pro")
    elif text == "فحص الزخم الآن": await run_manual_scanner(update, context, "momentum_breakout")
    elif text == "فحص الحيتان الآن": await run_manual_scanner(update, context, "whale_radar")


async def toggle_tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggles the background tasks on or off."""
    current_status = bot_state["settings"].get("background_tasks_enabled", True)
    new_status = not current_status
    bot_state["settings"]["background_tasks_enabled"] = new_status
    save_settings()
    
    status_text = "تفعيل" if new_status else "إيقاف"
    await update.message.reply_text(
        f"✅ تم **{status_text}** المهام الخلفية بنجاح.",
        reply_markup=build_main_menu()
    )
    logger.info(f"Background tasks toggled {'ON' if new_status else 'OFF'} by user.")

async def performance_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إنشاء وإرسال تقرير أداء شامل."""
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        total_trades = cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'Closed'").fetchone()[0]
        successful_trades = cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'Closed' and pnl_usdt > 0").fetchone()[0]
        total_pnl = cursor.execute("SELECT SUM(pnl_usdt) FROM trades WHERE status = 'Closed'").fetchone()[0] or 0
        win_rate = (successful_trades / total_trades * 100) if total_trades > 0 else 0
        
        report = [
            f"📊 **تقرير الأداء الشامل** 📊\n",
            f"▪️ إجمالي الصفقات المغلقة: *{total_trades}*",
            f"▪️ نسبة النجاح: *{win_rate:.2f}%*",
            f"▪️ صافي الربح/الخسارة: *${total_pnl:,.2f}*\n",
            "--- **أداء الاستراتيجيات** ---"
        ]

        strategy_stats = cursor.execute("SELECT strategy, COUNT(*), AVG(pnl_percent), SUM(pnl_usdt) FROM trades WHERE status = 'Closed' GROUP BY strategy").fetchall()
        conn.close()

        for stat in strategy_stats:
            report.append(f"\n- `{stat['strategy']}`:")
            report.append(f"  - عدد الصفقات: {stat['COUNT(*)']}")
            report.append(f"  - متوسط الربح/الخسارة: {stat['AVG(pnl_percent)']:.2f}%")
            report.append(f"  - صافي الربح/الخسارة: ${stat['SUM(pnl_usdt)']:.2f}")

        await update.message.reply_text("\n".join(report), parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        await update.message.reply_text(f"حدث خطأ أثناء إنشاء التقرير: {e}")
        logger.error(f"Error in performance_report_command: {e}")

@user_task_lock
async def active_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[PAGINATION FIX] Fetches and displays all active trades, paginated."""
    await update.message.reply_text("📈 **الصفقات النشطة**\n\n🔍 جارِ جلب الصفقات الحالية...")
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        active_trades = cursor.execute("SELECT * FROM trades WHERE status = 'Active' ORDER BY id DESC").fetchall()
        conn.close()

        if not active_trades:
            await update.message.reply_text("لا توجد صفقات نشطة حالياً.")
            return

        all_trade_reports = []
        for trade_row in active_trades:
            trade = dict(trade_row)
            exchange = bot_state["exchanges"].get(trade['exchange'])
            current_price_str = "N/A"
            pnl_str = ""

            if exchange:
                current_price = await get_current_price(exchange, trade['symbol'])
                if current_price:
                    current_price_str = f"{format_price(current_price)}"
                    pnl_percent = ((current_price - trade['entry_price']) / trade['entry_price']) * 100
                    pnl_icon = "🟢" if pnl_percent >= 0 else "🔴"
                    pnl_str = f"\n    {pnl_icon} **الربح/الخسارة:** `{pnl_percent:+.2f}%`"
            
            trade_info = (
                f"**#{trade['id']} | ${trade['symbol'].replace('/USDT', '')}** ({trade['exchange']})\n"
                f"    - **الاستراتيجية:** `{trade['strategy']}`\n"
                f"    - **الدخول:** `{format_price(trade['entry_price'])}`\n"
                f"    - **الحالي:** `{current_price_str}`"
                f"{pnl_str}\n"
            )
            all_trade_reports.append(trade_info)
        
        # Pagination Logic
        page_size = 8  # Trades per message
        total_pages = math.ceil(len(all_trade_reports) / page_size)
        
        for page_num in range(total_pages):
            start_index = page_num * page_size
            end_index = start_index + page_size
            page_trades = all_trade_reports[start_index:end_index]
            
            header = f"📈 **تقرير الصفقات النشطة (صفحة {page_num + 1}/{total_pages})** 📈\n\n"
            message = header + "\n".join(page_trades)
            
            await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(0.5) # Avoid hitting rate limits

    except Exception as e:
        logger.error(f"Error in active_trades_command: {e}", exc_info=True)
        await update.message.reply_text("حدث خطأ أثناء جلب الصفقات النشطة.")


async def scanners_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_scanners = bot_state["settings"].get("active_scanners", [])
    keyboard = [
        [InlineKeyboardButton(f"{'✅' if name in active_scanners else '❌'} {name}", callback_data=f"toggle_scanner_{name}")]
        for name in ACTIVE_SCANNERS.keys()
    ]
    await update.message.reply_text("اختر الماسحات الآلية لتفعيلها أو تعطيلها:", reply_markup=InlineKeyboardMarkup(keyboard))

async def presets_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_preset = bot_state["settings"].get("active_preset_name", "N/A")
    keyboard = [
        [InlineKeyboardButton(f"{'▶️' if name == active_preset else ''} {name}", callback_data=f"set_preset_{name}")]
        for name in PRESETS.keys()
    ]
    await update.message.reply_text("اختر نمط الإعدادات الجاهز:", reply_markup=InlineKeyboardMarkup(keyboard))

async def select_manual_exchange_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows a menu to select the active exchange for manual reports."""
    active_exchange = context.user_data.get('active_manual_exchange', 'Binance')
    keyboard = [
        [InlineKeyboardButton(f"{'▶️' if name == active_exchange else ''} {name}", callback_data=f"set_manual_exchange_{name}")]
        for name in PLATFORMS
    ]
    await update.message.reply_text("اختر المنصة للتقارير والفحوصات اليدوية:", reply_markup=InlineKeyboardMarkup(keyboard))

async def manual_scans_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows a menu for on-demand manual scans."""
    keyboard = [
        ["فحص القناص الآن"],
        ["فحص الزخم الآن"],
        ["فحص الحيتان الآن"],
        ["🔙 القائمة الرئيسية"],
    ]
    await update.message.reply_text(
        "اختر فحصًا يدويًا لتشغيله على المنصة المحددة:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

@user_task_lock
async def run_manual_scanner(update: Update, context: ContextTypes.DEFAULT_TYPE, scanner_name: str):
    """Wrapper to run a specific scanner on demand."""
    ex_id = context.user_data.get('active_manual_exchange', 'Binance')
    exchange = bot_state["exchanges"].get(ex_id)
    if not exchange:
        await update.message.reply_text(f"المنصة المحددة '{ex_id}' غير متصلة.")
        return

    await update.message.reply_text(f"🔬 جارِ بدء **{scanner_name}** على منصة **{ex_id}**...")
    
    try:
        candidates = await pre_scan_filter(exchange)
        if not candidates:
            await update.message.reply_text("لم يتم العثور على عملات مرشحة بعد الفلترة الأولية.")
            return

        scanner_func = ACTIVE_SCANNERS.get(scanner_name)
        if not scanner_func:
            await update.message.reply_text("خطأ: الماسح المحدد غير موجود.")
            return
            
        found_signals = []
        for candidate in candidates:
            signal = await scanner_func(exchange, candidate)
            if signal:
                found_signals.append(f"`{candidate['symbol']}`")
        
        if not found_signals:
            await update.message.reply_text(f"✅ **الفحص اليدوي ({scanner_name}) اكتمل:**\n\nلم يتم العثور على إشارات جديدة على {ex_id}.")
        else:
            message = f"🎯 **نتائج الفحص اليدوي ({scanner_name}) على {ex_id}:**\n\n" + "\n".join(found_signals)
            await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

    except Exception as e:
        logger.error(f"Error during manual scan ({scanner_name}) on {ex_id}: {e}", exc_info=True)
        await update.message.reply_text("حدث خطأ فادح أثناء الفحص اليدوي.")

async def run_diagnostic_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """[NEW] Provides a comprehensive diagnostic report of the bot's state."""
    report = ["🩺 **تقرير التشخيص والحالة** 🩺\n"]
    
    # General Settings
    settings = bot_state["settings"]
    report.append("--- **الإعدادات العامة** ---")
    report.append(f"- حالة المهام الخلفية: `{'🟢 تعمل' if settings.get('background_tasks_enabled') else '🔴 متوقفة'}`")
    report.append(f"- النمط النشط: `{settings.get('active_preset_name', 'N/A')}`")
    report.append(f"- الماسحات النشطة: `{', '.join(settings.get('active_scanners', []))}`")
    report.append(f"- منصة التقارير اليدوية: `{context.user_data.get('active_manual_exchange', 'N/A')}`\n")
    
    # Job Queue Status
    report.append("--- **حالة المهام المجدولة** ---")
    if context.job_queue:
        for job in context.job_queue.jobs():
            next_run = job.next_t.astimezone(timezone.utc).strftime('%H:%M:%S (UTC)') if job.next_t else 'N/A'
            report.append(f"- المهمة: `{job.name}` | التشغيل التالي: `{next_run}`")
    else:
        report.append("- `JobQueue` غير مفعل.")
    report.append("")

    # Connection Status
    report.append("--- **حالة الاتصال بالمنصات** ---")
    for ex_id in PLATFORMS:
        status = '✅ متصل' if ex_id in bot_state["exchanges"] and bot_state["exchanges"][ex_id].markets else '❌ فشل الاتصال'
        report.append(f"- {ex_id}: `{status}`")
    
    await update.message.reply_text("\n".join(report), parse_mode=ParseMode.MARKDOWN)


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة ضغطات الأزرار (Inline Keyboard)."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("toggle_scanner_"):
        scanner_name = data.replace("toggle_scanner_", "")
        active = bot_state["settings"]["active_scanners"]
        if scanner_name in active: active.remove(scanner_name)
        else: active.append(scanner_name)
        save_settings()
        
        active_scanners = bot_state["settings"].get("active_scanners", [])
        keyboard = [
            [InlineKeyboardButton(f"{'✅' if name in active_scanners else '❌'} {name}", callback_data=f"toggle_scanner_{name}")]
            for name in ACTIVE_SCANNERS.keys()
        ]
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("set_preset_"):
        preset_name = data.replace("set_preset_", "")
        if preset_name in PRESETS:
            bot_state["settings"]["active_preset_name"] = preset_name
            bot_state["settings"]["filters"] = PRESETS[preset_name]
            save_settings()
            load_settings()
            await query.edit_message_text(f"✅ تم تفعيل النمط: **{preset_name}**", parse_mode=ParseMode.MARKDOWN)
        else:
             await query.edit_message_text("❌ نمط غير معروف.")

    elif data.startswith("set_manual_exchange_"):
        exchange_name = data.replace("set_manual_exchange_", "")
        context.user_data['active_manual_exchange'] = exchange_name
        await query.edit_message_text(f"✅ تم تحديد منصة التقارير إلى: **{exchange_name}**", parse_mode=ParseMode.MARKDOWN)

# =============================================================================
# --- 🚀 11. نقطة انطلاق البوت 🚀 ---
# =============================================================================

async def post_init(application: Application):
    """يتم تنفيذها بعد تهيئة البوت وقبل بدء التشغيل."""
    logger.info("Bot post-init sequence started...")
    load_settings()
    setup_database()
    await initialize_exchanges()
    
    if not bot_state["exchanges"]:
        logger.critical("CRITICAL: No exchanges could be initialized. Bot cannot run.")
        return

    job_queue = application.job_queue
    if not job_queue:
        logger.warning("JobQueue is not available. Automated scans will not run.")
        return
        
    job_queue.run_repeating(perform_scan_and_trade, interval=timedelta(minutes=SCAN_INTERVAL_MINUTES), first=10, name='main_scan')
    job_queue.run_repeating(track_active_trades, interval=timedelta(minutes=TRACK_INTERVAL_MINUTES), first=20, name='trade_tracker')
    
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="✅ **بوت الصياد الهجين v2.4 متصل وجاهز للعمل!**", parse_mode=ParseMode.MARKDOWN)
    logger.info("Bot is fully initialized and background jobs are scheduled.")

async def post_shutdown(application: Application):
    """يتم تنفيذها عند إيقاف البوت لإغلاق الاتصالات."""
    logger.info("Closing all exchange connections...")
    for ex in bot_state["exchanges"].values():
        await ex.close()
    logger.info("Shutdown complete.")

def main() -> None:
    """الوظيفة الرئيسية لتشغيل البوت."""
    if 'YOUR_TELEGRAM' in TELEGRAM_BOT_TOKEN or 'YOUR_CHAT' in TELEGRAM_CHAT_ID:
        logger.critical("FATAL ERROR: Bot token or Admin Chat ID is not set. Please edit the script.")
        return
        
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("ta", run_full_technical_analysis))
    application.add_handler(CommandHandler("scalp", run_scalp_analysis))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Exception while handling an update:", exc_info=context.error)

    application.add_error_handler(error_handler)

    logger.info("Starting Hybrid Hunter Bot v2.4...")
    application.run_polling()

if __name__ == '__main__':
    main()

