import os, logging, asyncio, psycopg2, requests, hashlib, json
from datetime import datetime, timezone
from contextlib import closing
from threading import Thread
from dotenv import load_dotenv
from flask import Flask, request
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, PreCheckoutQueryHandler, ContextTypes, filters

# ========= CONFIG =========
TOKEN_NAME = "$MBTC"
TOKEN_FULL_NAME = "MAD BTC"
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
NOWPAY_API_KEY = os.getenv("NOWPAY_API_KEY")
NOWPAY_IPN_SECRET = os.getenv("NOWPAY_IPN_SECRET")

# PRICING - FOREIGN PRICING ONLY
STARS_PER_MBTC = 10 # 10 Stars = 1 $MBTC = $0.10
TON_PER_MBTC = 0.0001 # 0.0001 TON = 1 $MBTC = $0.10
BTC_USD = 0.10 # 1 $MBTC = $0.10
ETH_PER_MBTC = 0.00006 # ~$0.10
BNB_PER_MBTC = 0.0004 # ~$0.10
SOL_PER_MBTC = 0.0008 # ~$0.10
TRX_PER_MBTC = 0.001 # ~$0.10
REF_BONUS = 0.05 # 5%

WALLETS = {"TON": "UQB4iAkAt7F8nsajNOulyWZjNVewIUgoVTbxjIDkC1-G_GoO", "BTC": "bc1qvqa2zn7fdajmcvvj0q0khvm3ye7fndvjhuhhrp", "ETH": "0x10fc9e08494f983B86260579024d77E918A528b2", "BNB": "0x10fc9e08494f983B86260579024d77E918A528b2", "ARB": "0x10fc9e08494f983B86260579024d77E918A528b2", "SOL": "4wpHps8ZsfksZHsXUMDLyHKJ2wuq2oLM23TRc7c1SDEF", "TRX": "TLhtTks9ihyqiokftm3H6E74BoXAkWSxVU"}
CHANNEL_ID = -1002764321871
BOT_USERNAME = "madraka001bot"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app_flask = Flask(__name__)

# ========= DATABASE =========
def get_db(): return psycopg2.connect(DATABASE_URL, sslmode='require')
def init_db():
    with closing(get_db().cursor()) as c:
        c.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, mbtc_balance REAL DEFAULT 0, referrer_id BIGINT, joined_at TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS purchases (id SERIAL PRIMARY KEY, user_id BIGINT, method TEXT, amount REAL, status TEXT, time TEXT)")
    get_db().commit()

def add_user(user_id, username, referrer_id=None):
    with closing(get_db().cursor()) as c:
        c.execute("INSERT INTO users (user_id, username, referrer_id, joined_at) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING",(user_id, username, referrer_id, datetime.now(timezone.utc).isoformat()))
    get_db().commit()

def get_balance(user_id):
    with closing(get_db().cursor()) as c: c.execute("SELECT mbtc_balance FROM users WHERE user_id=%s", (user_id,)); res = c.fetchone(); return res[0] if res else 0

def add_balance(user_id, amount, method="SYSTEM", context=None):
    conn = get_db()
    with closing(conn.cursor()) as c:
        c.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        c.execute("UPDATE users SET mbtc_balance = mbtc_balance + %s WHERE user_id=%s", (amount, user_id))
        c.execute("INSERT INTO purchases (user_id, method, amount, status, time) VALUES (%s,%s,%s,%s,%s)",(user_id, method, amount, "SUCCESS", datetime.now(timezone.utc).isoformat()))
        c.execute("SELECT referrer_id FROM users WHERE user_id=%s", (user_id,)); ref = c.fetchone()
        if ref and ref[0] and method!= "SYSTEM": c.execute("UPDATE users SET mbtc_balance = mbtc_balance + %s WHERE user_id=%s", (amount * REF_BONUS, ref[0]))
    conn.commit()
    if method!= "SYSTEM" and context: asyncio.run(announce_purchase(context, user_id, amount, method))

async def announce_purchase(context, user_id, amount, method):
    try:
        user = await context.bot.get_chat(user_id); name = f"@{user.username}" if user.username else user.first_name
        await context.bot.send_message(chat_id=CHANNEL_ID, text=f"🎉 NEW BUYER!\n\n{name} bought {amount:.4f} {TOKEN_NAME}\nPayment: {method.upper()}\n\nJoin: https://t.me/{BOT_USERNAME}")
    except Exception as e: logger.error(f"Announce failed: {e}")

# ========= NOWPAYMENTS =========
def create_nowpay_invoice(price_amount, price_currency, order_id, pay_currency):
    url = "https://api.nowpayments.io/v1/invoice"
    domain = os.getenv('RAILWAY_PUBLIC_DOMAIN', 'localhost')
    payload = {"price_amount": price_amount, "price_currency": price_currency, "pay_currency": pay_currency, "order_id": order_id, "ipn_callback_url": f"https://{domain}/ipn"}
    headers = {"x-api-key": NOWPAY_API_KEY}
    try: return requests.post(url, json=payload, headers=headers, timeout=10).json()
    except Exception as e: logger.error(f"NOWPAY ERROR: {e}"); return {}

# ========= IPN - AUTO CREDIT =========
@app_flask.route('/ipn', methods=['POST'])
def ipn():
    data = request.get_json()
    received_hmac = request.headers.get('x-nowpayments-sig')
    sorted_data = json.dumps(data, separators=(',', ':'), sort_keys=True)
    generated_hmac = hashlib.sha512((sorted_data + NOWPAY_IPN_SECRET).encode()).hexdigest()
    if received_hmac!= generated_hmac: return 'Invalid signature', 400
    if data.get('payment_status') == 'finished':
        user_id = int(data.get('order_id').split('_')[-1])
        add_balance(user_id, 1, method=data.get('pay_currency'))
    return 'ok', 200

# ========= TELEGRAM HANDLERS =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; referrer = int(context.args[0].replace("ref", "")) if context.args and context.args[0].startswith("ref") else None
    add_user(user.id, user.username, referrer)
    await update.message.reply_text(f"🚀 Welcome to {TOKEN_FULL_NAME}\n\nPre-Sale: 1 {TOKEN_NAME} = $0.10\nWhen listed: 1 {TOKEN_NAME} = $1.00\n\n/buy - Buy {TOKEN_NAME}\n/balance - Check Balance\n/ref - Your Referral Link")

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"⭐ Stars - {STARS_PER_MBTC}", callback_data="buy_stars")],
        [InlineKeyboardButton(f"💎 TON - {TON_PER_MBTC}", callback_data="buy_ton")],
        [InlineKeyboardButton(f"₿ BTC - ${BTC_USD}", callback_data="buy_btc")],
        [InlineKeyboardButton(f"💵 USDT - ${BTC_USD}", callback_data="buy_usdt")],
        [InlineKeyboardButton(f"🔷 ETH - {ETH_PER_MBTC}", callback_data="buy_eth")],
        [InlineKeyboardButton(f"🟡 BNB - {BNB_PER_MBTC}", callback_data="buy_bnb")],
        [InlineKeyboardButton(f"◎ SOL - {SOL_PER_MBTC}", callback_data="buy_sol")],
        [InlineKeyboardButton(f"🔺 TRX - {TRX_PER_MBTC}", callback_data="buy_trx")]
    ]
    await update.message.reply_text(f"Choose payment for 1 {TOKEN_NAME}:", reply_markup=InlineKeyboardMarkup(keyboard))

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text(f"💰 Your {TOKEN_NAME} Balance: {get_balance(update.effective_user.id):.4f}")

async def ref(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text(f"🔗 Your Referral Link:\nhttps://t.me/{BOT_USERNAME}?start=ref{update.effective_user.id}\n\nEarn 5% of every referral's purchase!")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; user_id = query.from_user.id; await query.answer()
    prices = {"buy_ton": ("ton", TON_PER_MBTC), "buy_btc": ("btc", BTC_USD), "buy_usdt": ("usdt", BTC_USD), "buy_eth": ("eth", ETH_PER_MBTC), "buy_bnb": ("bnb", BNB_PER_MBTC), "buy_sol": ("sol", SOL_PER_MBTC), "buy_trx": ("trx", TRX_PER_MBTC)}
    if query.data == "buy_stars": await context.bot.send_invoice(user_id, f"Buy 1 {TOKEN_NAME}", "1 $MBTC", "buy_mbtc_stars", "", "XTR", [LabeledPrice(TOKEN_NAME, STARS_PER_MBTC * 100)])
    elif query.data in prices:
        coin, amount = prices[query.data]
        invoice = create_nowpay_invoice(BTC_USD, "USD", f"mbtc_{coin}_{user_id}", coin)
        await query.message.reply_text(f"Pay here:\n{invoice.get('invoice_url')}")

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mbtc = (update.message.successful_payment.total_amount / 100) / STARS_PER_MBTC
    add_balance(update.message.from_user.id, mbtc, method="STARS", context=context)
    await update.message.reply_text(f"✅ Payment Received!\nYou got: {mbtc:.4f} {TOKEN_NAME}")

def run_flask(): app_flask.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
def main():
    init_db(); Thread(target=run_flask).start()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("buy", buy)); app.add_handler(CommandHandler("balance", balance)); app.add_handler(CommandHandler("ref", ref))
    app.add_handler(CallbackQueryHandler(callback_handler)); app.add_handler(PreCheckoutQueryHandler(lambda u,c: u.pre_checkout_query.answer(ok=True))); app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.run_polling(drop_pending_updates=True)
if __name__ == "__main__": main()
