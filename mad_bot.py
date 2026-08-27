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
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler,
    PreCheckoutQueryHandler, ContextTypes, filters,
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

BASE_PRICE = 0.10 # $0.10 per 1 MBTC
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
    try: yield conn; conn.commit()
    except Exception: conn.rollback(); raise
    finally: conn.close()

def init_db():
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, mbtc_balance REAL DEFAULT 0, usd_spent REAL DEFAULT 0, referrer_id BIGINT, joined_at TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS purchases (id SERIAL PRIMARY KEY, user_id BIGINT, method TEXT, amount REAL, price_usd REAL, status TEXT, time TEXT, order_id TEXT)")

def get_discount_price(amount):
    if amount >= 100: return amount * 0.08 # 20% off
    elif amount >= 50: return amount * 0.09 # 10% off
    elif amount >= 10: return amount * 0.095 # 5% off
    else: return amount * BASE_PRICE

def add_user(user_id, username, referrer_id=None):
    with get_db() as conn:
        with conn.cursor() as c: c.execute("INSERT INTO users (user_id, username, referrer_id, joined_at) VALUES (%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING",(user_id, username, referrer_id, datetime.now(timezone.utc).isoformat()))

def add_balance(user_id, mbtc=0, usd=0, method="SYSTEM", order_id=""):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
            if mbtc!= 0: c.execute("UPDATE users SET mbtc_balance = mbtc_balance + %s WHERE user_id=%s", (mbtc, user_id))
            if usd!= 0: c.execute("UPDATE users SET usd_spent = usd_spent + %s WHERE user_id=%s", (usd, user_id))
            c.execute("INSERT INTO purchases (user_id, method, amount, price_usd, status, time, order_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",(user_id, method, mbtc, usd, "SUCCESS", datetime.now(timezone.utc).isoformat(), order_id))

def create_nowpay_invoice(price_amount, order_id):
    url = "https://api.nowpayments.io/v1/invoice"
    # THIS LINE MAKES ALL COINS APPEAR
    payload = {
        "price_amount": round(price_amount, 2), 
        "price_currency": "USD", 
        "pay_currency": "btc,eth,usdt_trc20,usdt_bep20,sol,ton,trx", 
        "order_id": order_id, 
        "ipn_callback_url": f"https://{RAILWAY_PUBLIC_DOMAIN}/ipn"
    }
    headers = {"x-api-key": NOWPAY_API_KEY}
    try: resp = requests.post(url, json=payload, headers=headers, timeout=15); resp.raise_for_status(); return resp.json()
    except Exception as e: logger.error(f"NOWPAY ERROR: {e} | Resp: {resp.text if 'resp' in locals() else 'No Resp'}"); return {}

async def announce_purchase(context, user_id, amount, method):
    try: user = await context.bot.get_chat(user_id); name = f"@{user.username}" if user.username else user.first_name; await context.bot.send_message(chat_id=CHANNEL_ID, text=f"🎉 NEW BUYER!\n\n{name} bought {amount:.2f} {TOKEN_NAME}\nPayment: {method.upper()}\n\nJoin: https://t.me/{BOT_USERNAME}")
    except Exception as e: logger.error(f"Announce failed: {e}")

# ==================== WEBSITE ROUTES ====================
@app_flask.route("/")
def home():
    domain = f"https://{RAILWAY_PUBLIC_DOMAIN}"
    return f"""
    <!DOCTYPE html><html lang="en"><head>
    <title>{TOKEN_FULL_NAME} - Digital Bucks</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        :root{{--bg:#0B0F19;--card:#111827;--accent:#F7931A;--text:#E5E7EB;--muted:#9CA3AF}}
        *{{margin:0;padding:0;box-sizing:border-box}}
        body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text)}}
        nav{{display:flex;justify-content:space-between;align-items:center;padding:20px 10%;background:rgba(17,24,39,0.7);backdrop-filter:blur(10px);position:sticky;top:0}}
       .btn{{background:var(--accent);padding:12px 24px;border-radius:10px;color:#000;text-decoration:none;font-weight:800;transition:0.2s;display:inline-block;border:none;cursor:pointer}}
       .hero{{text-align:center;padding:120px 10% 80px}}
       .hero h1{{font-size:3.5em;font-weight:800;background:linear-gradient(90deg,#F7931A,#FFD700);-webkit-background-clip:text;-webkit-text-fill-color:transparent}}
       .section{{padding:80px 10%}}
       .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px}}
       .card{{background:var(--card);padding:30px;border-radius:16px;border:1px solid #1F2937;text-align:center}}
       .price{{font-size:2em;color:var(--accent);font-weight:800}}
       .custom-box{{background:var(--card);padding:40px;border-radius:16px;max-width:500px;margin:40px auto;text-align:center}}
        input{{padding:12px;border-radius:8px;border:1px solid #374151;background:#1F2937;color:#fff;width:200px;margin-right:10px}}
    </style>
    </head><body>
    <nav><div style="font-weight:800;font-size:1.3em">💰 {TOKEN_NAME} Bucks</div><a href="https://t.me/{BOT_USERNAME}" class="btn">Launch Bot</a></nav>
    <div class="hero">
        <h1>{TOKEN_FULL_NAME}</h1>
        <p style="font-size:1.2em;color:var(--muted);margin:20px 0">Digital Money for the Internet. Use Anywhere.</p>
    </div>
    <div class="section">
        <h2 style="text-align:center;margin-bottom:40px">Discounted Bulk Bucks</h2>
        <div class="grid">
            <div class="card"><h3>1 {TOKEN_NAME}</h3><div class="price">$0.10</div><p>$0.10 each</p><a href="{domain}/buy/1/0" class="btn">Buy</a></div>
            <div class="card"><h3>10 {TOKEN_NAME}</h3><div class="price">$0.95</div><p>$0.095 each - 5% OFF</p><a href="{domain}/buy/10/0" class="btn">Buy</a></div>
            <div class="card"><h3>50 {TOKEN_NAME}</h3><div class="price">$4.50</div><p>$0.09 each - 10% OFF</p><a href="{domain}/buy/50/0" class="btn">Buy</a></div>
            <div class="card" style="border:2px solid var(--accent)"><h3>100 {TOKEN_NAME}</h3><div class="price">$8.00</div><p>$0.08 each - 20% OFF</p><a href="{domain}/buy/100/0" class="btn">Buy</a></div>
        </div>
    </div>
    <div class="custom-box">
        <h2>Or Buy Any Amount</h2>
        <p style="color:var(--muted);margin:10px 0">From 1 {TOKEN_NAME} upward. Base price: $0.10</p>
        <input type="number" id="amount" placeholder="Enter amount" min="1">
        <button class="btn" onclick="buyCustom()">Buy Now</button>
    </div>
    <script>
    function buyCustom(){{
        const amt = document.getElementById('amount').value;
        if(amt < 1){{alert('Min 1 $MBTC')}} else {{window.location = '{domain}/buy/' + amt + '/0'}}
    }}
    </script>
    </body></html>
    """

@app_flask.route("/buy/<amount>/<user_id>")
def buy_page(amount, user_id):
    try: amount = float(amount); user_id = int(user_id)
    except: return "Invalid amount", 400
    if amount < 1: return "Min 1 $MBTC", 400
    
    price = get_discount_price(amount)
    order_id = f"web_{amount}_{user_id}_{int(datetime.now().timestamp())}"
    invoice = create_nowpay_invoice(price, order_id)
    url = invoice.get("invoice_url")
    if url: return f'<script>window.location = "{url}"</script>'
    else: return f"Error creating invoice. Resp: {invoice}", 500

@app_flask.route("/ipn", methods=["POST"])
def ipn():
    try:
        raw_body = request.get_data(as_text=True)
        data = request.get_json(force=True, silent=True) or {}
        received_hmac = request.headers.get("x-nowpayments-sig", "")
        generated_hmac = hashlib.sha512((raw_body + NOWPAY_IPN_SECRET).encode()).hexdigest()
        if not received_hmac or received_hmac!= generated_hmac: return "Invalid signature", 400
        
        if data.get("payment_status") == "finished":
            order_id = data.get("order_id", "")
            parts = order_id.split("_")
            if len(parts) >= 3: 
                amount = float(parts[1])
                user_id = int(parts[2])
                price_paid = float(data.get("price_amount", 0)) # Real USD they paid
                add_balance(user_id, mbtc=amount, usd=price_paid, method=data.get("pay_currency", "NOWPAY"), order_id=order_id)
                logger.info(f"CREDITED {amount} MBTC to {user_id} for ${price_paid}")
        return "ok", 200
    except Exception as e: logger.error(f"IPN error: {e}"); return "error", 500

# ==================== TELEGRAM HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username)
    await update.message.reply_text(f"Welcome to {TOKEN_FULL_NAME}!\n\nUse /buy 10 to buy {TOKEN_NAME}\nUse /balance to check")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        with conn.cursor() as c: c.execute("SELECT mbtc_balance FROM users WHERE user_id=%s", (update.effective_user.id,)); res = c.fetchone()
    bal = res[0] if res else 0.0
    await update.message.reply_text(f"Your Balance: {bal:.2f} {TOKEN_NAME}")

async def buy_cmd_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Usage: /buy 25"); return
    try: amount = float(context.args[0])
    except: await update.message.reply_text("Invalid amount"); return
    if amount < 1: await update.message.reply_text("Min 1 $MBTC"); return
    
    price = get_discount_price(amount)
    order_id = f"mbtc_{amount}_{update.effective_user.id}_{int(datetime.now().timestamp())}"
    invoice = await asyncio.to_thread(create_nowpay_invoice, price, order_id)
    url = invoice.get('invoice_url')
    if url:
        kb = [[InlineKeyboardButton("💳 Pay Now", url=url)]]
        await update.message.reply_text(f"Pay ${price:.2f} for {amount} {TOKEN_NAME}:\nYou can pay with BTC, ETH, USDT, SOL, TRX, TON", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(f"Error creating invoice. Contact admin.")

def main():
    init_db(); Thread(target=run_flask, daemon=True).start(); logger.info("Flask Website + IPN server started")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("buy", buy_cmd_amount))
    app.run_polling(drop_pending_updates=True, stop_signals=None)

def run_flask(): app_flask.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), threaded=True)
if __name__ == "__main__": main()
