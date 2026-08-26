import os, logging, threading, psycopg, requests, hashlib, json
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, request
load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, PreCheckoutQueryHandler, Filters, CallbackContext

TOKEN_NAME = "$MBTC"
TOKEN_FULL_NAME = "MAD BTC - MAKING A DIFFERENCE"
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
NOWPAY_API_KEY = os.getenv("NOWPAY_API_KEY")
NOWPAY_IPN_SECRET = os.getenv("NOWPAY_IPN_SECRET")
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "localhost")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

PRICE_1 = 0.10; PRICE_10 = 0.90; PRICE_50 = 4.00; PRICE_100 = 7.00
STARS_PER_MBTC = 10; FEE = 0.02
CHANNEL_ID = -1002764321871
BOT_USERNAME = "madraka001bot"
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app_flask = Flask(__name__)

def yandex_translate(text, target_lang="en"):
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID: return "Yandex not configured"
    url = "https://translate.api.cloud.yandex.net/translate/v2/translate"
    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
    body = {"targetLanguageCode": target_lang, "texts": [text], "folderId": YANDEX_FOLDER_ID}
    try: return requests.post(url, headers=headers, json=body, timeout=10).json()["translations"][0]["text"]
    except Exception as e: logger.error(f"YANDEX ERROR: {e}"); return "Translation failed"

def get_db(): return psycopg.connect(DATABASE_URL, autocommit=False)
def init_db():
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, mbtc_balance REAL DEFAULT 0, usd_balance REAL DEFAULT 0, referrer_id BIGINT, joined_at TEXT, lang TEXT DEFAULT 'en')")
            c.execute("CREATE TABLE IF NOT EXISTS purchases (id SERIAL PRIMARY KEY, user_id BIGINT, method TEXT, amount REAL, price_usd REAL, status TEXT, time TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS market (id SERIAL PRIMARY KEY, seller_id BIGINT, amount REAL, price_per_mbtc REAL, status TEXT DEFAULT 'OPEN', time TEXT)")
        conn.commit()

def add_user(user_id, username, referrer_id=None):
    with get_db() as conn:
        with conn.cursor() as c: c.execute("INSERT INTO users (user_id, username, referrer_id, joined_at) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING",(user_id, username, referrer_id, datetime.now(timezone.utc).isoformat()))
        conn.commit()

def get_user(user_id):
    with get_db() as conn:
        with conn.cursor() as c: c.execute("SELECT mbtc_balance, usd_balance FROM users WHERE user_id=%s", (user_id,)); res = c.fetchone() or (0,0)
    return res

def add_balance(user_id, mbtc=0, usd=0, method="SYSTEM", context=None):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
            if mbtc!= 0: c.execute("UPDATE users SET mbtc_balance = mbtc_balance + %s WHERE user_id=%s", (mbtc, user_id))
            if usd!= 0: c.execute("UPDATE users SET usd_balance = usd_balance + %s WHERE user_id=%s", (usd, user_id))
            if method!= "SYSTEM": c.execute("INSERT INTO purchases (user_id, method, amount, price_usd, status, time) VALUES (%s,%s,%s,%s,%s,%s)",(user_id, method, mbtc, usd, "SUCCESS", datetime.now(timezone.utc).isoformat()))
        conn.commit()
    if method!= "SYSTEM" and context: threading.Thread(target=announce_purchase, args=(context, user_id, mbtc, method)).start()

def create_sell_order(seller_id, amount, price):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE users SET mbtc_balance = mbtc_balance - %s WHERE user_id=%s AND mbtc_balance >= %s", (amount, seller_id, amount))
            if c.rowcount == 0: return False
            c.execute("INSERT INTO market (seller_id, amount, price_per_mbtc, time) VALUES (%s,%s,%s,%s)", (seller_id, amount, price, datetime.now(timezone.utc).isoformat()))
        conn.commit()
    return True

def buy_from_market(buyer_id, order_id):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT seller_id, amount, price_per_mbtc FROM market WHERE id=%s AND status='OPEN'", (order_id,)); order = c.fetchone()
            if not order: return "Order not found"
            seller_id, amount, price = order; total_cost = amount * price; fee = total_cost * FEE; seller_gets = total_cost - fee
            c.execute("SELECT usd_balance FROM users WHERE user_id=%s", (buyer_id,)); bal = c.fetchone()
            if not bal or bal[0] < total_cost: return "Insufficient USD balance"
            c.execute("UPDATE users SET usd_balance = usd_balance - %s WHERE user_id=%s", (total_cost, buyer_id))
            c.execute("UPDATE users SET mbtc_balance = mbtc_balance + %s WHERE user_id=%s", (amount, buyer_id))
            c.execute("UPDATE users SET usd_balance = usd_balance + %s WHERE user_id=%s", (seller_gets, seller_id))
            c.execute("UPDATE users SET usd_balance = usd_balance + %s WHERE user_id=%s", (fee, ADMIN_ID))
            c.execute("UPDATE market SET status='SOLD' WHERE id=%s", (order_id,))
        conn.commit()
    return "SUCCESS - Tokens Delivered!"

def announce_purchase(context, user_id, amount, method):
    try: user = context.bot.get_chat(user_id); name = f"@{user.username}" if user.username else user.first_name; context.bot.send_message(chat_id=CHANNEL_ID, text=f"🎉 NEW BUYER!\n\n{name} bought {amount:.2f} {TOKEN_NAME}\nPayment: {method.upper()}\n\nJoin: https://t.me/{BOT_USERNAME}")
    except Exception as e: logger.error(f"Announce failed: {e}")

def create_nowpay_invoice(price_amount, price_currency, order_id, pay_currency):
    url = "https://api.nowpayments.io/v1/invoice"
    payload = {"price_amount": price_amount, "price_currency": price_currency, "pay_currency": pay_currency, "order_id": order_id, "ipn_callback_url": f"https://{RAILWAY_PUBLIC_DOMAIN}/ipn"}
    headers = {"x-api-key": NOWPAY_API_KEY}
    try: return requests.post(url, json=payload, headers=headers, timeout=10).json()
    except Exception as e: logger.error(f"NOWPAY ERROR: {e}"); return {}

@app_flask.route('/ipn', methods=['POST'])
def ipn():
    data = request.get_json(); received_hmac = request.headers.get('x-nowpayments-sig'); sorted_data = json.dumps(data, separators=(',', ':'), sort_keys=True); generated_hmac = hashlib.sha512((sorted_data + NOWPAY_IPN_SECRET).encode()).hexdigest()
    if received_hmac!= generated_hmac: return 'Invalid signature', 400
    if data.get('payment_status') == 'finished': parts = data.get('order_id').split('_'); user_id = int(parts[-1]); amount = float(parts[-2]); add_balance(user_id, mbtc=amount, usd=amount*PRICE_1, method=data.get('pay_currency'))
    return 'ok', 200

def start(update: Update, context: CallbackContext):
    user = update.effective_user; referrer = int(context.args[0].replace("ref", "")) if context.args and context.args[0].startswith("ref") else None; add_user(user.id, user.username, referrer)
    update.message.reply_text(f"🚀 Welcome to {TOKEN_FULL_NAME}\n\nPre-Sale: 1 {TOKEN_NAME} = $0.10\nBulk: 100 for $7\n/buy /market /sell /balance /ref /translate")

def buy(update: Update, context: CallbackContext):
    keyboard = [[InlineKeyboardButton(f"1 {TOKEN_NAME} - $0.10", callback_data="buy_1")],[InlineKeyboardButton(f"10 {TOKEN_NAME} - $0.90", callback_data="buy_10")],[InlineKeyboardButton(f"50 {TOKEN_NAME} - $4.00", callback_data="buy_50")],[InlineKeyboardButton(f"100 {TOKEN_NAME} - $7.00 🔥", callback_data="buy_100")]]
    update.message.reply_text(f"Buy {TOKEN_NAME} from Bot:", reply_markup=InlineKeyboardMarkup(keyboard))

def market(update: Update, context: CallbackContext):
    with get_db() as conn:
        with conn.cursor() as c: c.execute("SELECT id, amount, price_per_mbtc FROM market WHERE status='OPEN' LIMIT 10"); orders = c.fetchall()
    if not orders: update.message.reply_text("Market is empty. Use /sell to list yours")
    else: update.message.reply_text("📊 P2P MARKET\n" + "\n".join([f"ID:{o[0]} | {o[1]} {TOKEN_NAME} @ ${o[2]:.3f} each" for o in orders]) + "\n\nTo buy: /buyorder ID")

def sell(update: Update, context: CallbackContext):
    try: amount = float(context.args[0]); price = float(context.args[1]); msg = "✅ Listed!" if create_sell_order(update.effective_user.id, amount, price) else "❌ Insufficient $MBTC"; update.message.reply_text(msg)
    except: update.message.reply_text("Usage: /sell 100 0.12")

def buyorder(update: Update, context: CallbackContext):
    try: result = buy_from_market(update.effective_user.id, int(context.args[0])); update.message.reply_text(result)
    except: update.message.reply_text("Usage: /buyorder 5")

def balance(update: Update, context: CallbackContext): bal = get_user(update.effective_user.id); update.message.reply_text(f"💰 Balance:\n{bal[0]:.4f} {TOKEN_NAME}\n${bal[1]:.2f} USD")
def ref(update: Update, context: CallbackContext): update.message.reply_text(f"🔗 Your Link:\nhttps://t.me/{BOT_USERNAME}?start=ref{update.effective_user.id}")
def translate_cmd(update: Update, context: CallbackContext):
    if len(context.args) < 2: update.message.reply_text("Usage: /translate ru Hello"); return
    update.message.reply_text(f"🇺🇸 -> {context.args[0].upper()}\n{yandex_translate(' '.join(context.args[1:]), context.args[0])}")

def callback_handler(update: Update, context: CallbackContext):
    query = update.callback_query; query.answer()
    packs = {"buy_1":(1,PRICE_1),"buy_10":(10,PRICE_10),"buy_50":(50,PRICE_50),"buy_100":(100,PRICE_100)}
    if query.data in packs: amount, price = packs[query.data]; invoice = create_nowpay_invoice(price, "USD", f"mbtc_{amount}_{query.from_user.id}", "ton"); query.message.reply_text(f"Pay here:\n{invoice.get('invoice_url', 'Error')}")

def successful_payment(update: Update, context: CallbackContext):
    mbtc = (update.message.successful_payment.total_amount / 100) / STARS_PER_MBTC; add_balance(update.message.from_user.id, mbtc=mbtc, usd=mbtc*PRICE_1, method="STARS", context=context); update.message.reply_text(f"✅ Payment Received!\nYou got: {mbtc:.2f} {TOKEN_NAME}")

def run_flask(): app_flask.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

def main():
    init_db(); threading.Thread(target=run_flask, daemon=True).start()
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start)); dp.add_handler(CommandHandler("buy", buy)); dp.add_handler(CommandHandler("market", market)); dp.add_handler(CommandHandler("sell", sell)); dp.add_handler(CommandHandler("buyorder", buyorder)); dp.add_handler(CommandHandler("balance", balance)); dp.add_handler(CommandHandler("ref", ref)); dp.add_handler(CommandHandler("translate", translate_cmd))
    dp.add_handler(CallbackQueryHandler(callback_handler)); dp.add_handler(PreCheckoutQueryHandler(lambda u,c: u.pre_checkout_query.answer(ok=True))); dp.add_handler(MessageHandler(Filters.successful_payment, successful_payment))
    print("Bot is running"); updater.start_polling(drop_pending_updates=True); updater.idle()

if __name__ == "__main__": main()
