from dotenv import load_dotenv
load_dotenv()

import os
import sqlite3
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from contextlib import closing
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, ContextTypes, filters
)

TOKEN_NAME = "$MBTC"
TOKEN_FULL_NAME = "MAD BTC"
BOT_NAME = "MAD BOT"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "")
DB_PATH = "madbot.db"

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
BOT_USERNAME = "madraka001bot"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

DB = sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with closing(DB.cursor()) as c:
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, mbtc_balance REAL DEFAULT 0, referrer_id INTEGER, joined_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, method TEXT, amount REAL, status TEXT, time TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS tasks (user_id INTEGER PRIMARY KEY, daily_last_claim TEXT, joined_channel INTEGER DEFAULT 0)")
    DB.commit()

def get_user(user_id):
    with closing(DB.cursor()) as c:
        c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return c.fetchone()

def add_user(user_id, username, referrer_id=None):
    with closing(DB.cursor()) as c:
        c.execute("INSERT OR IGNORE INTO users (user_id, username, referrer_id, joined_at) VALUES (?,?,?,?)",
                  (user_id, username, referrer_id, datetime.now(timezone.utc).isoformat()))
        c.execute("INSERT OR IGNORE INTO tasks (user_id) VALUES (?)", (user_id,))
    DB.commit()

def get_balance(user_id):
    with closing(DB.cursor()) as c:
        c.execute("SELECT mbtc_balance FROM users WHERE user_id=?", (user_id,))
        res = c.fetchone()
        return res[0] if res else 0

def add_balance(user_id, amount):
    with closing(DB.cursor()) as c:
        c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        c.execute("UPDATE users SET mbtc_balance = mbtc_balance +? WHERE user_id=?", (amount, user_id))
    DB.commit()

def get_all_users():
    with closing(DB.cursor()) as c:
        c.execute("SELECT user_id FROM users")
        return [row[0] for row in c.fetchall()]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referrer = None
    if context.args and context.args[0].startswith("ref"):
        try: referrer = int(context.args[0].replace("ref", ""))
        except: pass
    add_user(user.id, user.username, referrer)
    text = f"🚀 Welcome to {TOKEN_FULL_NAME}\n\nBuy {TOKEN_NAME} with 7 methods:\n⭐ Stars | 💎 TON | ₿ BTC | 🔷 ETH | 🟡 BNB | 🔵 ARB | 🟣 SOL | 🔴 TRX\nUse /buy to start\nUse /ref to earn 5% from referrals"
    await update.message.reply_text(text)

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⭐ Stars - 1000", callback_data="buy_stars")],
        [InlineKeyboardButton("💎 TON - 0.01", callback_data="buy_ton")],
        [InlineKeyboardButton("₿ Bitcoin - ~$0.05", callback_data="buy_btc")],
        [InlineKeyboardButton("🔷 Ethereum - 0.000002", callback_data="buy_eth")],
        [InlineKeyboardButton("🟡 BNB - 0.0001", callback_data="buy_bnb")],
        [InlineKeyboardButton("🔵 Arbitrum - 0.000002", callback_data="buy_arb")],
        [InlineKeyboardButton("🟣 Solana - 0.0003", callback_data="buy_sol")],
        [InlineKeyboardButton("🔴 Tron - 0.5", callback_data="buy_trx")],
    ]
    await update.message.reply_text(f"Choose payment for 1 {TOKEN_NAME}:", reply_markup=InlineKeyboardMarkup(keyboard))

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = get_balance(update.effective_user.id)
    await update.message.reply_text(f"💰 Your {TOKEN_NAME} Balance: {bal:.4f}")

async def ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    link = f"https://t.me/{BOT_USERNAME}?start=ref{user_id}"
    with closing(DB.cursor()) as c:
        c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (user_id,))
        count = c.fetchone()[0]
    await update.message.reply_text(f"👥 Your Referral Link:\n`{link}`\n\nReferred: {count} users\nEarn 5% bonus when they buy with Stars", parse_mode=ParseMode.MARKDOWN)

async def daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with closing(DB.cursor()) as c:
        c.execute("SELECT daily_last_claim FROM tasks WHERE user_id=?", (user_id,))
        last = c.fetchone()[0]
    if last:
        last_time = datetime.fromisoformat(last)
        if datetime.now(timezone.utc) - last_time < timedelta(hours=24):
            await update.message.reply_text("⏰ You already claimed today. Come back in 24h")
            return
    add_balance(user_id, DAILY_REWARD)
    with closing(DB.cursor()) as c:
        c.execute("UPDATE tasks SET daily_last_claim=? WHERE user_id=?", (datetime.now(timezone.utc).isoformat(), user_id))
    DB.commit()
    await update.message.reply_text(f"✅ Claimed {DAILY_REWARD} {TOKEN_NAME}! Use /daily again in 24h")

async def tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"📋 TASKS:\n1. Join {CHANNEL} - Reward: {TASK_REWARD} {TOKEN_NAME}\n\nAfter joining use /verify"
    await update.message.reply_text(text)

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with closing(DB.cursor()) as c:
        c.execute("SELECT joined_channel FROM tasks WHERE user_id=?", (user_id,))
        if c.fetchone()[0] == 1:
            await update.message.reply_text("You already claimed this task")
            return
        c.execute("UPDATE tasks SET joined_channel=1 WHERE user_id=?", (user_id,))
    add_balance(user_id, TASK_REWARD)
    DB.commit()
    await update.message.reply_text(f"✅ Verified! You got {TASK_REWARD} {TOKEN_NAME}")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with closing(DB.cursor()) as c:
        c.execute("SELECT username, mbtc_balance FROM users ORDER BY mbtc_balance DESC LIMIT 10")
        rows = c.fetchall()
    text = f"🏆 TOP 10 {TOKEN_NAME} HOLDERS:\n\n"
    for i, (user, bal) in enumerate(rows, 1):
        text += f"{i}. @{user or 'user'} - {bal:.4f} {TOKEN_NAME}\n"
    await update.message.reply_text(text)

async def addbalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id!= ADMIN_ID: return
    try:
        target = int(context.args[0])
        amount = float(context.args[1])
        add_balance(target, amount)
        await update.message.reply_text(f"✅ Added {amount} {TOKEN_NAME} to {target}")
    except: await update.message.reply_text("Usage: /addbalance user_id amount")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id!= ADMIN_ID: return
    with closing(DB.cursor()) as c:
        c.execute("SELECT COUNT(*), SUM(mbtc_balance) FROM users")
        count, total = c.fetchone()
    await update.message.reply_text(f"👥 Total Users: {count}\n💰 Total {TOKEN_NAME}: {total:.4f}")

async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id!= ADMIN_ID: return
    msg = " ".join(context.args)
    users = get_all_users()
    for uid in users:
        try: await context.bot.send_message(uid, f"📢 ANNOUNCEMENT:\n\n{msg}")
        except: pass
    await update.message.reply_text(f"Sent to {len(users)} users")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = {
        "buy_stars": (f"Pay {STARS_PER_MBTC/100} Stars in app", "XTR"),
        "buy_ton": (f"Send {TON_PER_MBTC} TON to\n`{WALLETS['TON']}`", None),
        "buy_btc": (f"Send ~${BTC_USD} BTC to\n`{WALLETS['BTC']}`", None),
        "buy_eth": (f"Send {ETH_PER_MBTC} ETH [ERC20] to\n`{WALLETS['ETH']}`", None),
        "buy_bnb": (f"Send {BNB_PER_MBTC} BNB [BSC] to\n`{WALLETS['BNB']}`", None),
        "buy_arb": (f"Send {ETH_PER_MBTC} ETH [Arbitrum] to\n`{WALLETS['ARB']}`", None),
        "buy_sol": (f"Send {SOL_PER_MBTC} SOL to\n`{WALLETS['SOL']}`", None),
        "buy_trx": (f"Send {TRX_PER_MBTC} TRX to\n`{WALLETS['TRX']}`", None),
    }
    msg, curr = data[query.data]
    if curr == "XTR":
        await query.message.reply_invoice(
            title=TOKEN_NAME, description=f"Buy 1 {TOKEN_NAME}", payload="mbtc_1",
            provider_token="", currency="XTR", prices=[LabeledPrice(label=f"1 {TOKEN_NAME}", amount=STARS_PER_MBTC)]
        )
    else:
        await query.message.reply_text(f"{msg}\n\nSend TX hash here after payment.", parse_mode=ParseMode.MARKDOWN)

async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    amount = update.message.successful_payment.total_amount / 100
    mbtc = amount / STARS_PER_MBTC
    add_balance(user_id, mbtc)
    user = get_user(user_id)
    if user and user[3]:
        ref_bonus = mbtc * REF_BONUS
        add_balance(user[3], ref_bonus)
        try: await context.bot.send_message(user[3], f"🎉 Referral bonus! +{ref_bonus:.4f} {TOKEN_NAME}")
        except: pass
    await update.message.reply_text(f"✅ Paid with Stars!\nYou received: {mbtc:.4f} {TOKEN_NAME}\nNew Balance: {get_balance(user_id):.4f}")

def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("ref", ref))
    app.add_handler(CommandHandler("daily", daily))
    app.add_handler(CommandHandler("tasks", tasks))
    app.add_handler(CommandHandler("verify", verify))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("addbalance", addbalance))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("announce", announce))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    logger.info("MAD BOT RUNNING...")
    app.run_polling(allowed_updates=Update.ALL_TYPES) # FIXED LINE

if __name__ == "__main__":
    main()
