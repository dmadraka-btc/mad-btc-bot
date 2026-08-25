import os
import logging
import asyncio
import psycopg2
from datetime import datetime, timezone, timedelta
from contextlib import closing
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, ContextTypes, filters
)

# ========= CONFIG =========
TOKEN_NAME = "$MBTC"
TOKEN_FULL_NAME = "MAD BTC"
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")
DATABASE_URL = os.getenv("DATABASE_URL") # FROM RAILWAY POSTGRES

STARS_PER_MBTC = 100000
TON_PER_MBTC = 0.01
ETH_PER_MBTC = 0.000002
BNB_PER_MBTC = 0.0001
SOL_PER_MBTC = 0.0003
TRX_PER_MBTC = 0.5
BTC_USD = 0.05
DAILY_REWARD = 0.001
TASK_REWARD = 0.01
REF_BONUS = 0.05

WALLETS = {
    "TON": "UQB4iAkAt7F8nsajNOulyWZjNVewIUgoVTbxjIDkC1-G_GoO",
    "BTC": "bc1qvqa2zn7fdajmcvvj0q0khvm3ye7fndvjhuhhrp",
    "ETH": "0x10fc9e08494f983B86260579024d77E918A528b2",
    "BNB": "0x10fc9e08494f983B86260579024d77E918A528b2",
    "ARB": "0x10fc9e08494f983B86260579024d77E918A528b2",
    "SOL": "4wpHps8ZsfksZHsXUMDLyHKJ2wuq2oLM23TRc7c1SDEF",
    "TRX": "TLhtTks9ihyqiokftm3H6E74BoXAkWSxVU"
}

CHANNEL = "@MADBTC"
CHANNEL_ID = -1001234567890 # <-- REPLACE WITH YOUR CHANNEL ID. Get it from @userinfobot
BOT_USERNAME = "madraka001bot"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ========= DATABASE =========
def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    with closing(get_db().cursor()) as c:
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, mbtc_balance REAL DEFAULT 0, referrer_id BIGINT, joined_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS purchases (id SERIAL PRIMARY KEY, user_id BIGINT, method TEXT, amount REAL, status TEXT, time TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS tasks (user_id BIGINT PRIMARY KEY, daily_last_claim TEXT, joined_channel INTEGER DEFAULT 0)")
    get_db().commit()

def add_user(user_id, username, referrer_id=None):
    with closing(get_db().cursor()) as c:
        c.execute("INSERT INTO users (user_id, username, referrer_id, joined_at) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING",
                  (user_id, username, referrer_id, datetime.now(timezone.utc).isoformat()))
        c.execute("INSERT INTO tasks (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
    get_db().commit()

def get_balance(user_id):
    with closing(get_db().cursor()) as c:
        c.execute("SELECT mbtc_balance FROM users WHERE user_id=%s", (user_id,))
        res = c.fetchone()
        return res[0] if res else 0

def add_balance(user_id, amount, method="SYSTEM", context=None):
    conn = get_db()
    with closing(conn.cursor()) as c:
        c.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        c.execute("UPDATE users SET mbtc_balance = mbtc_balance + %s WHERE user_id=%s", (amount, user_id))
        c.execute("INSERT INTO purchases (user_id, method, amount, status, time) VALUES (%s,%s,%s,%s,%s)",
                  (user_id, method, amount, "SUCCESS", datetime.now(timezone.utc).isoformat()))
    conn.commit()
    # AUTO ANNOUNCE TO CHANNEL
    if method != "SYSTEM" and context:
        asyncio.create_task(announce_purchase(context, user_id, amount, method))

def get_all_users():
    with closing(get_db().cursor()) as c:
        c.execute("SELECT user_id FROM users")
        return [row[0] for row in c.fetchall()]

async def announce_purchase(context, user_id, amount, method):
    try:
        user = await context.bot.get_chat(user_id)
        name = f"@{user.username}" if user.username else user.first_name
        text = f"🎉 NEW BUYER!\n\n{name} just bought {amount:.4f} {TOKEN_NAME}\nPayment: {method}\n\nJoin: https://t.me/{BOT_USERNAME}"
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
    except Exception as e:
        logger.error(f"Failed to announce: {e}")

# ========= HANDLERS - SAME AS BEFORE =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referrer = None
    if context.args and context.args[0].startswith("ref"):
        try: referrer = int(context.args[0].replace("ref", ""))
        except: pass
    add_user(user.id, user.username, referrer)
    text = f"🚀 Welcome to {TOKEN_FULL_NAME}\n\nBuy {TOKEN_NAME} with 8 methods\n/buy /balance /ref /daily /tasks"
    await update.message.reply_text(text)

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("⭐ Stars - 1000", callback_data="buy_stars")],[InlineKeyboardButton("💎 TON - 0.01", callback_data="buy_ton")]]
    await update.message.reply_text(f"Choose payment for 1 {TOKEN_NAME}:", reply_markup=InlineKeyboardMarkup(keyboard))

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = get_balance(update.effective_user.id)
    await update.message.reply_text(f"💰 Your {TOKEN_NAME} Balance: {bal:.4f}")

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    amount = update.message.successful_payment.total_amount / 100
    mbtc = amount / STARS_PER_MBTC
    add_balance(user_id, mbtc, method="STARS", context=context) # <-- ANNOUNCE HAPPENS HERE
    await update.message.reply_text(f"✅ Paid with Stars!\nYou received: {mbtc:.4f} {TOKEN_NAME}")

# ADD ALL OTHER HANDLERS FROM PREVIOUS CODE HERE: ref, daily, tasks, verify, etc.

# ========= MAIN =========
def main():
    if not BOT_TOKEN or not DATABASE_URL:
        logger.error("BOT_TOKEN or DATABASE_URL not set!")
        return
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(PreCheckoutQueryHandler(lambda u,c: u.pre_checkout_query.answer(ok=True)))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    # ADD ALL OTHER HANDLERS HERE
    
    logger.info("MAD BOT RUNNING WITH POSTGRES...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
