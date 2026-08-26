import os
import logging
import asyncio
import psycopg
import requests
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Thread
from dotenv import load_dotenv
from flask import Flask, request

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# ==================== CONFIG ====================
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

PRICE_1 = 0.10
PRICE_10 = 0.90
PRICE_50 = 4.00
PRICE_100 = 7.00
STARS_PER_MBTC = 10
FEE = 0.02
CHANNEL_ID = -1002764321871
BOT_USERNAME = "madraka001bot"

# ==================== LOGGING ====================
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== FLASK ====================
app_flask = Flask(__name__)

# ==================== DATABASE ====================
@contextmanager
def get_db():
    conn = psycopg.connect(DATABASE_URL, sslmode="require", autocommit=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, mbtc_balance REAL DEFAULT 0, usd_balance REAL DEFAULT 0, referrer_id BIGINT, joined_at TEXT, lang TEXT DEFAULT 'en')")
            c.execute("CREATE TABLE IF NOT EXISTS purchases (id SERIAL PRIMARY KEY, user_id BIGINT, method TEXT, amount REAL, price_usd REAL, status TEXT, time TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS market (id SERIAL PRIMARY KEY, seller_id BIGINT, amount REAL, price_per_mbtc REAL, status TEXT DEFAULT 'OPEN', time TEXT)")

def add_user(user_id, username, referrer_id=None):
    with get_db() as conn:
        with conn.cursor() as c: c.execute("INSERT INTO users (user_id, username, referrer_id, joined_at) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING",(user_id, username, referrer_id, datetime.now(timezone.utc).isoformat()))

def get_user(user_id):
    with get_db() as conn:
        with conn.cursor() as c: c.execute("SELECT mbtc_balance, usd_balance FROM users WHERE user_id=%s", (user_id,)); res = c.fetchone()
    return res if res else (0.0, 0.0)

def add_balance(user_id, mbtc=0, usd=0, method="SYSTEM", context=None):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
            if mbtc!= 0: c.execute("UPDATE users SET mbtc_balance = mbtc_balance + %s WHERE user_id=%s", (mbtc, user_id))
            if usd!= 0: c.execute("UPDATE users SET usd_balance = usd_balance + %s WHERE user_id=%s", (usd, user_id))
            if method!= "SYSTEM": c.execute("INSERT INTO purchases (user_id, method, amount, price_usd, status, time) VALUES (%s,%s,%s,%s,%s,%s)",(user_id, method, mbtc, usd, "SUCCESS", datetime.now(timezone.utc).isoformat()))
    if method!= "SYSTEM" and context:
        try: asyncio.get_running_loop().create_task(announce_purchase(context, user_id, mbtc, method))
        except RuntimeError: logger.warning("No event loop for IPN thread announcement")

def create_sell_order(seller_id, amount, price):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE users SET mbtc_balance = mbtc_balance - %s WHERE user_id=%s AND mbtc_balance >= %s", (amount, seller_id, amount))
            if c.rowcount == 0: return False
            c.execute("INSERT INTO market (seller_id, amount, price_per_mbtc, time) VALUES (%s,%s,%s,%s)", (seller_id, amount, price, datetime.now(timezone.utc).isoformat()))
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
    return "SUCCESS - Tokens Delivered!"

async def announce_purchase(context, user_id, amount, method):
    try: user = await context.bot.get_chat(user_id); name = f"@{user.username}" if user.username else user.first_name; await context.bot.send_message(chat_id=CHANNEL_ID, text=f"🎉 NEW BUYER!\n\n{name} bought {amount:.2f} {TOKEN_NAME}\nPayment: {method.upper()}\n\nJoin: https://t.me/{BOT_USERNAME}")
    except Exception as e: logger.error(f"Announce failed: {e}")

def yandex_translate(text, target_lang="en"):
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID: return "Yandex not configured"
    url = "https://translate.api.cloud.yandex.net/translate/v2/translate"
    headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
    body = {"targetLanguageCode": target_lang, "texts": [text], "folderId": YANDEX_FOLDER_ID}
    try: resp = requests.post(url, headers=headers, json=body, timeout=10); resp.raise_for_status(); return resp.json()["translations"][0]["text"]
    except Exception as e: logger.error(f"YANDEX ERROR: {e}"); return "Translation failed"

def create_nowpay_invoice(price_amount, price_currency, order_id, pay_currency):
    url = "https://api.nowpayments.io/v1/invoice"
    payload = {"price_amount": price_amount, "price_currency": price_currency, "pay_currency": pay_currency, "order_id": order_id, "ipn_callback_url": f"https://{RAILWAY_PUBLIC_DOMAIN}/ipn"}
    headers = {"x-api-key": NOWPAY_API_KEY}
    try: resp = requests.post(url, json=payload, headers=headers, timeout=15); resp.raise_for_status(); return resp.json()
    except Exception as e: logger.error(f"NOWPAY ERROR: {e}"); return {}

# ==================== WEBSITE ROUTES ====================
@app_flask.route("/")
def home():
    domain = f"https://{RAILWAY_PUBLIC_DOMAIN}"
    return f"""
    <!DOCTYPE html><html lang="en"><head>
    <title>{TOKEN_FULL_NAME} - Community Token on TON</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        :root{{--bg:#0B0F19;--card:#111827;--accent:#F7931A;--text:#E5E7EB;--muted:#9CA3AF}}
        *{{margin:0;padding:0;box-sizing:border-box}}
        body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text)}}
        nav{{display:flex;justify-content:space-between;align-items:center;padding:20px 10%;background:rgba(17,24,39,0.7);backdrop-filter:blur(10px);position:sticky;top:0;z-index:10}}
        nav.logo{{font-weight:800;font-size:1.3em}}
        nav a{{color:var(--text);text-decoration:none;margin:0 15px;font-weight:600}}
       .btn{{background:var(--accent);padding:12px 24px;border-radius:10px;color:#000;text-decoration:none;font-weight:800;transition:0.2s;display:inline-block}}
       .btn:hover{{opacity:0.9;transform:translateY(-2px)}}
       .btn-secondary{{background:#2F81F7;color:#fff}}
       .hero{{text-align:center;padding:120px 10% 80px}}
       .hero h1{{font-size:3.5em;font-weight:800;background:linear-gradient(90deg,#F7931A,#FFD700);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
       .section{{padding:80px 10%}}
       .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px}}
       .card{{background:var(--card);padding:30px;border-radius:16px;border:1px solid #1F2937}}
       .price{{font-size:2.2em;color:var(--accent);font-weight:800;margin:10px 0}}
        footer{{text-align:center;padding:40px 10%;color:var(--muted);border-top:1px solid #1F2937}}
        footer a{{color:var(--muted);margin:0 10px;text-decoration:none}}
       .ipn{{background:#010409;color:#3fb950;padding:12px;border-radius:8px;word-break:break-all;font-size:12px;font-family:monospace}}
    </style>
    </head><body>

    <nav>
        <div class="logo">🚀 {TOKEN_NAME}</div>
        <div>
            <a href="/">Home</a>
            <a href="#about">About</a>
            <a href="#tokenomics">Tokenomics</a>
            <a href="/privacy">Privacy</a>
        </div>
        <a href="https://t.me/{BOT_USERNAME}" class="btn btn-secondary">Launch Bot</a>
    </nav>

    <div class="hero">
        <h1>{TOKEN_FULL_NAME}</h1>
        <p style="font-size:1.2em;color:var(--muted);margin:20px 0;max-width:700px;margin:auto">Making A Difference Through Community, Charity, and Web3 on the TON Blockchain</p>
        <div style="margin-top:30px">
            <a href="#buy" class="btn">Buy Presale Now</a>
            <a href="https://t.me/{BOT_USERNAME}" class="btn btn-secondary">Open Telegram</a>
        </div>
    </div>

    <div id="buy" class="section">
        <h2 style="text-align:center;margin-bottom:40px">Presale Packages</h2>
        <div class="grid">
            <div class="card"><h3>Starter</h3><div class="price">1 {TOKEN_NAME}</div><p>${PRICE_1}</p><a href="{domain}/buy/1" class="btn">Buy Now</a></div>
            <div class="card"><h3>Popular</h3><div class="price">10 {TOKEN_NAME}</div><p>${PRICE_10}</p><a href="{domain}/buy/10" class="btn">Buy Now</a></div>
            <div class="card"><h3>Whale</h3><div class="price">50 {TOKEN_NAME}</div><p>${PRICE_50}</p><a href="{domain}/buy/50" class="btn">Buy Now</a></div>
            <div class="card" style="border:2px solid var(--accent)"><h3>BEST VALUE 🔥</h3><div class="price">100 {TOKEN_NAME}</div><p>${PRICE_100}</p><a href="{domain}/buy/100" class="btn">Buy Now</a></div>
        </div>
    </div>

    <div id="about" class="section" style="background:var(--card)">
        <h2>About {TOKEN_FULL_NAME}</h2>
        <p style="color:var(--muted);margin-top:20px;line-height:1.8;max-width:800px">
        {TOKEN_FULL_NAME} is a community-driven utility token built on TON. Our mission is to leverage blockchain to fund transparency and real-world impact. 
        5% of all transactions go to our charity wallet. We are 100% community owned, no VC, no presale dump. Join us in Making A Difference.
        </p>
    </div>

    <div id="tokenomics" class="section">
        <h2>Tokenomics</h2>
        <div class="grid">
            <div class="card"><h4>Presale Price</h4><p>1 {TOKEN_NAME} = ${PRICE_1}</p></div>
            <div class="card"><h4>Total Supply</h4><p>100,000,000 {TOKEN_NAME}</p></div>
            <div class="card"><h4>Charity Wallet</h4><p>5% of every transaction</p></div>
            <div class="card"><h4>Blockchain</h4><p>TON - Fast & Low Fee</p></div>
        </div>
    </div>

    <div class="section" style="background:var(--card)">
        <h3>NowPayments IPN URL</h3>
        <p class="ipn">{domain}/ipn</p>
    </div>

    <footer>
        <p>© 2026 {TOKEN_FULL_NAME}. All rights reserved.</p>
        <p><a href="/privacy">Privacy Policy</a> | <a href="/terms">Terms</a> | <a href="https://t.me/{BOT_USERNAME}">Telegram</a></p>
    </footer>
    </body></html>
    """

@app_flask.route("/buy/<amount>")
def buy_page(amount):
    packs = {"1":PRICE_1,"10":PRICE_10,"50":PRICE_50,"100":PRICE_100}
    if amount not in packs: return "Invalid pack", 400
    price = packs[amount]
    invoice = create_nowpay_invoice(price, "USD", f"web_{amount}_0", "ton")
    url = invoice.get("invoice_url")
    if url: return f'<script>window.location = "{url}"</script>'
    else: return "Error creating invoice. Try again.", 500

@app_flask.route("/privacy")
def privacy():
    return f"""
    <html><head><title>Privacy Policy - {TOKEN_FULL_NAME}</title><style>body{{font-family:Inter;background:#0B0F19;color:#E5E7EB;padding:40px 10%;max-width:900px;margin:auto;line-height:1.8}} h1,h3{{color:#F7931A}} a{{color:#F7931A}}</style></head><body>
    <h1>Privacy Policy</h1>
    <p>Last Updated: August 26, 2026</p>
    <h3>1. Information We Collect</h3>
    <p>We collect your Telegram User ID and Username to manage your {TOKEN_NAME} balance. We do not collect emails or KYC data during presale.</p>
    <h3>2. Payments</h3>
    <p>Payments are processed by NowPayments.io. We do not store your crypto wallet address or card details. Refer to NowPayments Privacy Policy.</p>
    <h3>3. Data Usage</h3>
    <p>Your data is used only to provide the bot service, process transactions, and send you updates about {TOKEN_NAME}.</p>
    <h3>4. Contact</h3>
    <p>For privacy questions contact us via Telegram: @{BOT_USERNAME}</p>
    <br><a href="/">← Back to Home</a>
    </body></html>
    """

@app_flask.route("/terms")
def terms():
    return f"""
    <html><head><title>Terms - {TOKEN_FULL_NAME}</title><style>body{{font-family:Inter;background:#0B0F19;color:#E5E7EB;padding:40px 10%;max-width:900px;margin:auto;line-height:1.8}} h1{{color:#F7931A}} a{{color:#F7931A}}</style></head><body>
    <h1>Terms & Conditions</h1>
    <p>{TOKEN_NAME} is a utility token. Buying {TOKEN_NAME} does not give you equity or ownership in {TOKEN_FULL_NAME}.</p>
    <p>Cryptocurrency is volatile and high risk. Only purchase what you can afford to lose. All sales are final. No refunds.</p>
    <p>By using our Telegram bot and website you agree to these terms.</p>
    <br><a href="/">← Back to Home</a>
    </body></html>
    """

# ==================== FLASK ROUTES ====================
@app_flask.route("/ipn", methods=["POST"])
def ipn():
    try:
        raw_body = request.get_data(as_text=True)
        data = request.get_json(force=True, silent=True) or {}
        received_hmac = request.headers.get("x-nowpayments-sig", "")
        generated_hmac = hashlib.sha512((raw_body + NOWPAY_IPN_SECRET).encode()).hexdigest()
        if not received_hmac or received_hmac!= generated_hmac: return "Invalid signature", 400
        if data.get("payment_status") == "finished":
            parts = data.get("order_id", "").split("_")
            if len(parts) >= 3: add_balance(int(parts[-1]), mbtc=float(parts[-2]), usd=float(parts[-2]) * PRICE_1, method=data.get("pay_currency", "NOWPAY"))
        return "ok", 200
    except Exception as e: logger.error(f"IPN error: {e}"); return "error", 500

def run_flask():
    app_flask.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), threaded=True)

# ==================== TELEGRAM HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; referrer = int(context.args[0].replace("ref", "")) if context.args and context.args[0].startswith("ref") else None; add_user(user.id, user.username, referrer)
    await update.message.reply_text(f"🚀 Welcome to {TOKEN_FULL_NAME}\n\nPre-Sale: 1 {TOKEN_NAME} = $0.10\nBulk: 100 for $7\n\n/buy /market /sell /balance /ref /translate")

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"1 {TOKEN_NAME} - $0.10", callback_data="buy_1")],[InlineKeyboardButton(f"10 {TOKEN_NAME} - $0.90", callback_data="buy_10")],[InlineKeyboardButton(f"50 {TOKEN_NAME} - $4.00", callback_data="buy_50")],[InlineKeyboardButton(f"100 {TOKEN_NAME} - $7.00 🔥", callback_data="buy_100")]]
    await update.message.reply_text(f"Buy {TOKEN_NAME} from Bot:", reply_markup=InlineKeyboardMarkup(keyboard))

async def market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        with conn.cursor() as c: c.execute("SELECT id, amount, price_per_mbtc FROM market WHERE status='OPEN' LIMIT 10"); orders = c.fetchall()
    if not orders: await update.message.reply_text("Market is empty. Use /sell to list yours")
    else: await update.message.reply_text("📊 P2P MARKET\n" + "\n".join([f"ID:{o[0]} | {o[1]} {TOKEN_NAME} @ ${o[2]:.3f} each" for o in orders]) + "\n\nTo buy: /buyorder ID")

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: amount = float(context.args[0]); price = float(context.args[1]); msg = "✅ Listed!" if create_sell_order(update.effective_user.id, amount, price) else "❌ Insufficient $MBTC"; await update.message.reply_text(msg)
    except: await update.message.reply_text("Usage: /sell 100 0.12")

async def buyorder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: result = buy_from_market(update.effective_user.id, int(context.args[0])); await update.message.reply_text(result)
    except: await update.message.reply_text("Usage: /buyorder 5")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE): bal = get_user(update.effective_user.id); await update.message.reply_text(f"💰 Balance:\n{bal[0]:.4f} {TOKEN_NAME}\n${bal[1]:.2f} USD")
async def ref(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text(f"🔗 Your Link:\nhttps://t.me/{BOT_USERNAME}?start=ref{update.effective_user.id}")

async def translate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2: await update.message.reply_text("Usage: /translate ru Hello"); return
    translated = await asyncio.to_thread(yandex_translate, " ".join(context.args[1:]), context.args[0])
    await update.message.reply_text(f"🇺🇸 -> {context.args[0].upper()}\n{translated}")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    packs = {"buy_1":(1,PRICE_1),"buy_10":(10,PRICE_10),"buy_50":(50,PRICE_50),"buy_100":(100,PRICE_100)}
    if query.data in packs: amount, price = packs[query.data]; invoice = await asyncio.to_thread(create_nowpay_invoice, price, "USD", f"mbtc_{amount}_{query.from_user.id}", "ton"); await query.message.reply_text(f"Pay here:\n{invoice.get('invoice_url', 'Error')}")

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mbtc = (update.message.successful_payment.total_amount / 100) / STARS_PER_MBTC; add_balance(update.message.from_user.id, mbtc=mbtc, usd=mbtc*PRICE_1, method="STARS", context=context); await update.message.reply_text(f"✅ Payment Received!\nYou got: {mbtc:.2f} {TOKEN_NAME}")

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.pre_checkout_query.answer(ok=True)

# ==================== MAIN ====================
def main():
    init_db(); Thread(target=run_flask, daemon=True).start(); logger.info("Flask Website + IPN server started")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("buy", buy)); app.add_handler(CommandHandler("market", market)); app.add_handler(CommandHandler("sell", sell)); app.add_handler(CommandHandler("buyorder", buyorder)); app.add_handler(CommandHandler("balance", balance)); app.add_handler(CommandHandler("ref", ref)); app.add_handler(CommandHandler("translate", translate_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler)); app.add_handler(PreCheckoutQueryHandler(precheckout_handler)); app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    logger.info("Starting bot polling..."); app.run_polling(drop_pending_updates=True, stop_signals=None)

if __name__ == "__main__": main()
