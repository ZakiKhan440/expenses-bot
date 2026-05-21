import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os, re, json, datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

DATA_FILE = "data.json"

# ── Billing cycle helper ──────────────────────────────────────────
def get_cycle_label():
    """Returns e.g. '12 Mar – 12 Apr 2026' based on today's date."""
    today = datetime.date.today()
    if today.day >= 12:
        start = today.replace(day=12)
        # next month
        if today.month == 12:
            end = today.replace(year=today.year+1, month=1, day=12)
        else:
            end = today.replace(month=today.month+1, day=12)
    else:
        # previous month
        if today.month == 1:
            start = today.replace(year=today.year-1, month=12, day=12)
        else:
            start = today.replace(month=today.month-1, day=12)
        end = today.replace(day=12)
    return f"{start.strftime('%d %b')} – {end.strftime('%d %b %Y')}"

def get_cycle_key():
    """Returns a key like '2026-03' representing the current billing cycle start."""
    today = datetime.date.today()
    if today.day >= 12:
        return today.strftime('%Y-%m')
    else:
        if today.month == 1:
            return f"{today.year-1}-12"
        return today.replace(month=today.month-1).strftime('%Y-%m')

# ── Data storage ──────────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_cycle(data):
    key = get_cycle_key()
    if key not in data:
        data[key] = {"transactions": [], "total": 0}
    return data[key], key

# ── BOP SMS parser ────────────────────────────────────────────────
def extract_bop_amount(text):
    """
    Handles BOP SMS format:
    'your BOP Credit Card ending 4016 has been charged for PKR 4250.00 on ...'
    Also handles plain numbers like: 1500 or 1,500.50
    """
    # BOP SMS pattern — looks for "charged for PKR XXXX"
    bop_match = re.search(r'charged\s+for\s+PKR\s+([\d,]+(?:\.\d{1,2})?)', text, re.IGNORECASE)
    if bop_match:
        return float(bop_match.group(1).replace(',', ''))

    # Fallback: plain number or generic PKR/Rs amount
    text_clean = text.replace(',', '')
    generic = re.search(r'(?:PKR|Rs\.?)\s*([\d]+(?:\.\d{1,2})?)', text_clean, re.IGNORECASE)
    if generic:
        return float(generic.group(1))

    # Last resort: just a number on its own
    plain = re.fullmatch(r'\s*([\d]+(?:\.\d{1,2})?)\s*', text_clean)
    if plain:
        return float(plain.group(1))

    return None

def parse_message(text):
    """
    Parse the user's message into (amount, category).
    Supports:
      - "4250"                  → amount=4250, category=None
      - "4250 groceries"        → amount=4250, category="groceries"
      - "groceries 4250"        → amount=4250, category="groceries"
      - Forwarded BOP SMS       → amount extracted, category=None
    """
    # Try BOP SMS first
    amount = extract_bop_amount(text)
    if amount:
        return amount, None

    # Try "number + optional word" or "word + number"
    text_clean = text.strip().replace(',', '')
    match = re.match(r'^([\d]+(?:\.\d{1,2})?)\s*([a-zA-Z].*)?$', text_clean)
    if match:
        return float(match.group(1)), (match.group(2).strip() if match.group(2) else None)

    match2 = re.match(r'^([a-zA-Z][a-zA-Z\s]*?)\s+([\d]+(?:\.\d{1,2})?)$', text_clean)
    if match2:
        return float(match2.group(2)), match2.group(1).strip()

    return None, None

# ── Commands ──────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *BOP Expense Tracker*\n\n"
        "Send me:\n"
        "• `4250` — log PKR 4,250\n"
        "• `4250 groceries` — log with a category\n"
        "• Forward your BOP SMS — I'll extract the amount\n\n"
        "*Commands:*\n"
        "/total — current cycle summary\n"
        "/history — all transactions this cycle\n"
        "/categories — spending by category\n"
        "/undo — remove last transaction\n"
        "/alltime — see all past cycles",
        parse_mode="Markdown"
    )

async def total(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    cycle, _ = get_cycle(data)
    label = get_cycle_label()
    count = len(cycle["transactions"])
    await update.message.reply_text(
        f"📊 *Billing Cycle: {label}*\n\n"
        f"Total Spent: *PKR {cycle['total']:,.2f}*\n"
        f"Transactions: {count}",
        parse_mode="Markdown"
    )

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    cycle, _ = get_cycle(data)
    txns = cycle["transactions"]
    if not txns:
        await update.message.reply_text("No transactions this cycle yet!")
        return
    lines = [f"📋 *This Cycle ({get_cycle_label()}):*\n"]
    for i, t in enumerate(txns[-20:], 1):
        cat = f" [{t['category']}]" if t.get('category') else ""
        lines.append(f"{i}. PKR {t['amount']:,.2f}{cat} — {t['date']}")
    lines.append(f"\n*Total: PKR {cycle['total']:,.2f}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    cycle, _ = get_cycle(data)
    cats = {}
    uncategorized = 0
    for t in cycle["transactions"]:
        cat = t.get("category") or "Uncategorized"
        cats[cat] = cats.get(cat, 0) + t["amount"]
    if not cats:
        await update.message.reply_text("No transactions this cycle yet!")
        return
    lines = [f"🗂 *Spending by Category ({get_cycle_label()}):*\n"]
    for cat, total_amt in sorted(cats.items(), key=lambda x: -x[1]):
        pct = (total_amt / cycle["total"]) * 100 if cycle["total"] > 0 else 0
        lines.append(f"• {cat}: PKR {total_amt:,.2f} ({pct:.0f}%)")
    lines.append(f"\n*Total: PKR {cycle['total']:,.2f}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    cycle, key = get_cycle(data)
    if not cycle["transactions"]:
        await update.message.reply_text("Nothing to undo!")
        return
    last = cycle["transactions"].pop()
    cycle["total"] = round(cycle["total"] - last["amount"], 2)
    save_data(data)
    cat = f" [{last['category']}]" if last.get('category') else ""
    await update.message.reply_text(
        f"↩️ Removed PKR {last['amount']:,.2f}{cat}\n"
        f"New total: *PKR {cycle['total']:,.2f}*",
        parse_mode="Markdown"
    )

async def alltime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    if not data:
        await update.message.reply_text("No data yet!")
        return
    lines = ["📅 *All Billing Cycles:*\n"]
    grand_total = 0
    for key in sorted(data.keys(), reverse=True):
        cycle = data[key]
        lines.append(f"• {key}: PKR {cycle['total']:,.2f} ({len(cycle['transactions'])} txns)")
        grand_total += cycle["total"]
    lines.append(f"\n*Grand Total: PKR {grand_total:,.2f}*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── Message handler ───────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or update.message.caption or "").strip()
    amount, category = parse_message(text)

    if not amount or amount <= 0:
        await update.message.reply_text(
            "❓ Couldn't find an amount.\n"
            "Try: `4250` or `4250 groceries`\n"
            "Or forward your BOP SMS directly.",
            parse_mode="Markdown"
        )
        return

    data = load_data()
    cycle, key = get_cycle(data)
    now = datetime.datetime.now().strftime("%d %b %Y, %I:%M %p")
    cycle["transactions"].append({
        "amount": amount,
        "category": category,
        "date": now
    })
    cycle["total"] = round(cycle["total"] + amount, 2)
    save_data(data)

    cat_line = f"\nCategory: _{category}_" if category else ""
    await update.message.reply_text(
        f"✅ *PKR {amount:,.2f} added*{cat_line}\n"
        f"Cycle total: *PKR {cycle['total']:,.2f}*\n"
        f"({len(cycle['transactions'])} transactions this cycle)",
        parse_mode="Markdown"
    )

# ── Run ───────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def run_web():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), Handler).serve_forever()

if __name__ == "__main__":
    TOKEN = os.environ.get("BOT_TOKEN")
    threading.Thread(target=run_web, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("total", total))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("undo", undo))
    app.add_handler(CommandHandler("alltime", alltime))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot running...")
    app.run_polling()
