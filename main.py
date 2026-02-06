# Catch Character Bot — final files
## main.py
#!/usr/bin/env python3
# coding: utf-8
"""
Catch Character Bot - single-file final
- Uses a single shared aiosqlite connection
- Owner / sudo / ban / mute support
- Drop system with message counting
- Shop with atomic purchase
- Backup / restore

Configure via .env
"""
# ================= AUTO INSTALL PACKAGES =================
import sys
import subprocess

def auto_install(pkg):
    try:
        __import__(pkg)
    except ImportError:
        print(f"[AUTO] Installing {pkg} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

auto_install("aiosqlite")
auto_install("telegram")
auto_install("dotenv")
# =========================================================

import os
import asyncio
import aiosqlite
import random
import logging
import zipfile
import tempfile
from datetime import datetime, timedelta
from functools import wraps
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedPhoto,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    InlineQueryHandler,
)

# ---------------- config & paths ----------------
load_dotenv()
TOKEN = os.getenv("TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
BACKUP_CHAT = os.getenv("BACKUP_CHAT_ID")  # optional
DROP_NUMBER = int(os.getenv("DROP_NUMBER", "10"))

DB_FILE = "bot.db"
ASSETS_DIR = "assets"
IMAGES_DIR = os.path.join(ASSETS_DIR, "images")
VIDEOS_DIR = os.path.join(ASSETS_DIR, "videos")
BACKUP_DIR = "backups"

# logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("catch_character_bot")

# ---------------- rarities & shop ----------------
RARITY_LEVELS = [
    ("common", "⚪ Common"),
    ("uncommon", "🟢 Uncommon"),
    ("rare", "🔵 Rare"),
    ("epic", "🟣 Epic"),
    ("legendary", "🟠 Legendary"),
    ("mythic", "🔴 Mythic"),
    ("divine", "🟡 Divine"),
    ("celestial", "💎 Celestial"),
    ("supreme", "👑 Supreme"),
    ("animated", "✨ Animated"),
]
RARITY_WEIGHTS = [40, 25, 12, 8, 5, 4, 3, 1, 1, 1]
SHOP = {
    "common": 50,
    "uncommon": 80,
    "rare": 150,
    "epic": 300,
    "legendary": 600,
    "mythic": 800,
    "divine": 1200,
    "celestial": 2000,
    "supreme": 3000,
    "animated": 1000,
}
RARITY_LABEL_MAP = dict(RARITY_LEVELS)
ITEM_LIST = [(k, RARITY_LABEL_MAP.get(k, k.title()), SHOP.get(k, 0)) for k in SHOP.keys()]

# ---------------- global DB connection ----------------
DB: aiosqlite.Connection | None = None
DB_LOCK = asyncio.Lock()  # serialize critical DB transactions when needed

# ---------------- helpers ----------------
def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != OWNER_ID:
            if update.message:
                await update.message.reply_text("🔒 သင်မှာ Owner ခွင့်မရှိပါ။")
            return
        return await func(update, context)
    return wrapper

async def init_db_and_dirs():
    global DB
    os.makedirs(IMAGES_DIR, exist_ok=True)
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    DB = await aiosqlite.connect(DB_FILE)
    # use WAL mode for better concurrency
    await DB.execute("PRAGMA journal_mode=WAL;")
    # create tables
    await DB.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            movie TEXT,
            rarity TEXT,
            rarity_key TEXT,
            file_type TEXT,
            file_id TEXT,
            file_path TEXT,
            owner_id INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    await DB.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            coins INTEGER DEFAULT 0
        )
    """)
    await DB.execute("""
        CREATE TABLE IF NOT EXISTS daily (
            user_id INTEGER PRIMARY KEY,
            last_claim TEXT
        )
    """)
    await DB.execute("CREATE TABLE IF NOT EXISTS banned (id INTEGER PRIMARY KEY)")
    await DB.execute("CREATE TABLE IF NOT EXISTS muted (id INTEGER PRIMARY KEY)")
    await DB.execute("CREATE TABLE IF NOT EXISTS sudo (id INTEGER PRIMARY KEY)")
    await DB.execute("""
        CREATE TABLE IF NOT EXISTS groups_seen (
            chat_id INTEGER PRIMARY KEY,
            messages_count INTEGER DEFAULT 0,
            last_drop_card_id INTEGER DEFAULT 0
        )
    """)
    await DB.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    await DB.commit()
    logger.info("DB initialized: %s", DB_FILE)

async def close_db():
    global DB
    if DB:
        await DB.close()
        DB = None

async def is_banned(user_id: int) -> bool:
    async with DB.execute("SELECT 1 FROM banned WHERE id = ?", (user_id,)) as cur:
        r = await cur.fetchone()
        return bool(r)

async def is_muted(user_id: int) -> bool:
    async with DB.execute("SELECT 1 FROM muted WHERE id = ?", (user_id,)) as cur:
        r = await cur.fetchone()
        return bool(r)

async def is_sudo(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    async with DB.execute("SELECT 1 FROM sudo WHERE id = ?", (user_id,)) as cur:
        r = await cur.fetchone()
        return bool(r)

def pick_rarity():
    keys = [r[0] for r in RARITY_LEVELS]
    key = random.choices(keys, weights=RARITY_WEIGHTS, k=1)[0]
    label = RARITY_LABEL_MAP.get(key, key.title())
    return key, label

async def create_card(name, movie, rarity_key, rarity_label, file_type, file_id, file_path, owner_id=0):
    now = datetime.utcnow().isoformat()
    cur = await DB.execute(
        """
        INSERT INTO cards (name, movie, rarity, rarity_key, file_type, file_id, file_path, owner_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, movie, rarity_label, rarity_key, file_type, file_id, file_path, owner_id, now),
    )
    await DB.commit()
    return cur.lastrowid

async def get_card(card_id):
    async with DB.execute("SELECT id,name,movie,rarity,rarity_key,file_type,file_id,file_path,owner_id FROM cards WHERE id = ?", (card_id,)) as cur:
        return await cur.fetchone()

async def update_card(card_id, name=None, movie=None):
    if name and movie:
        await DB.execute("UPDATE cards SET name=?, movie=? WHERE id=?", (name, movie, card_id))
    elif name:
        await DB.execute("UPDATE cards SET name=? WHERE id=?", (name, card_id))
    elif movie:
        await DB.execute("UPDATE cards SET movie=? WHERE id=?", (movie, card_id))
    await DB.commit()

async def delete_card_db(card_id):
    await DB.execute("DELETE FROM cards WHERE id=?", (card_id,))
    await DB.commit()

async def add_coins(user_id: int, amount: int):
    async with DB.execute("SELECT coins FROM users WHERE id = ?", (user_id,)) as cur:
        r = await cur.fetchone()
    if r:
        new = r[0] + amount
        await DB.execute("UPDATE users SET coins = ? WHERE id = ?", (new, user_id))
    else:
        await DB.execute("INSERT INTO users (id, coins) VALUES (?, ?)", (user_id, amount))
    await DB.commit()

async def get_coins(user_id: int) -> int:
    async with DB.execute("SELECT coins FROM users WHERE id = ?", (user_id,)) as cur:
        r = await cur.fetchone()
        return r[0] if r else 0

async def count_available_cards(rarity_key: str) -> int:
    async with DB.execute("SELECT COUNT(*) FROM cards WHERE rarity_key=? AND owner_id=0", (rarity_key,)) as cur:
        r = await cur.fetchone()
        return r[0] if r else 0

async def pick_random_unowned_card(rarity_key: str):
    async with DB.execute("SELECT id FROM cards WHERE rarity_key=? AND owner_id=0 ORDER BY RANDOM() LIMIT 1", (rarity_key,)) as cur:
        r = await cur.fetchone()
        return r[0] if r else None

async def extract_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.reply_to_message:
        return update.message.reply_to_message.from_user
    if context.args:
        raw = context.args[0]
        if raw.startswith("@"):
            raw = raw[1:]
        try:
            if raw.isdigit():
                return await context.bot.get_chat(int(raw))
            else:
                return await context.bot.get_chat(raw)
        except Exception:
            return None
    return None

# decorator to allow owner or sudo
def admin_or_owner(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        if user.id == OWNER_ID:
            return await func(update, context)
        async with DB.execute("SELECT 1 FROM sudo WHERE id = ?", (user.id,)) as cur:
            r = await cur.fetchone()
            if r:
                return await func(update, context)
        if update.message:
            await update.message.reply_text("🔒 သင့်မှာ permission မရှိပါ။")
        return
    return wrapper

# decorator to block banned or muted users for user-facing commands
def user_allowed(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        if await is_banned(user.id):
            if update.message:
                await update.message.reply_text("🔒 သင့်ကို global ban ထားပါသည်။")
            return
        if await is_muted(user.id):
            # silently ignore or send a brief notice
            if update.message:
                await update.message.reply_text("🔇 သင့်ကို global mute ထားသည်။")
            return
        return await func(update, context)
    return wrapper

# ---------------- command handlers ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎮 Catch Character Bot မှကြိုဆိုပါသည်။\n/harem => ကိုယ့်ကဒ်များ ကြည့်ရန်။")

# ===== Admin/Owner commands =====
@admin_or_owner
async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("📷 ဓာတ်ပုံတစ်ပုံကို /upload နဲ့ အတူပေးပို့ပါ (caption: name|movie optional).")
        return
    photo = update.message.photo[-1]
    f = await photo.get_file()
    local_path = os.path.join(IMAGES_DIR, f"{photo.file_id}.jpg")
    await f.download_to_drive(local_path)
    caption = update.message.caption or ""
    parts = [p.strip() for p in caption.split("|")]
    name = parts[0] if parts[0] else f"Card-{photo.file_unique_id[:6]}"
    movie = parts[1] if len(parts) > 1 else "Unknown"
    rarity_key, rarity_label = pick_rarity()
    cid = await create_card(name, movie, rarity_key, rarity_label, "photo", photo.file_id, local_path, owner_id=0)
    await update.message.reply_text(f"✅ Image uploaded as card #{cid} — {rarity_label}")

@admin_or_owner
async def cmd_uploadvd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.video:
        await update.message.reply_text("🎬 ဗီဒီယိုကို /uploadvd နဲ့ အတူပေးပို့ပါ (caption: name|movie optional).")
        return
    video = update.message.video
    f = await video.get_file()
    local_path = os.path.join(VIDEOS_DIR, f"{video.file_id}.mp4")
    await f.download_to_drive(local_path)
    caption = update.message.caption or ""
    parts = [p.strip() for p in caption.split("|")]
    name = parts[0] if parts[0] else f"VideoCard-{video.file_unique_id[:6]}"
    movie = parts[1] if len(parts) > 1 else "Unknown"
    rarity_key = "animated"
    rarity_label = RARITY_LABEL_MAP[rarity_key]
    cid = await create_card(name, movie, rarity_key, rarity_label, "video", video.file_id, local_path, owner_id=0)
    await update.message.reply_text(f"✅ Video uploaded as card #{cid} — {rarity_label}")

@admin_or_owner
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("အသုံး: /edit <id> <name> <movie>")
        return
    try:
        cid = int(context.args[0])
    except:
        await update.message.reply_text("❌ id မမှန်ပါ")
        return
    name = context.args[1]
    movie = " ".join(context.args[2:])
    await update_card(cid, name=name, movie=movie)
    await update.message.reply_text(f"✏️ Card #{cid} ကို ပြင်ပြီးပါပြီ။")

@admin_or_owner
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("အသုံး: /delete <id>")
        return
    try:
        cid = int(context.args[0])
    except:
        await update.message.reply_text("❌ id မမှန်ပါ")
        return
    card = await get_card(cid)
    if not card:
        await update.message.reply_text("❌ Card မတွေ့ပါ")
        return
    if card[7] and os.path.exists(card[7]):
        try:
            os.remove(card[7])
        except Exception:
            pass
    await delete_card_db(cid)
    await update.message.reply_text(f"🗑️ Card #{cid} ဖျက်ပြီးပါပြီ။")

@admin_or_owner
async def cmd_setdrop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("အသုံး: /setdrop <number>")
        return
    try:
        n = int(context.args[0])
    except:
        await update.message.reply_text("❌ နံပါတ် မမှန်ပါ")
        return
    await DB.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("drop_number", str(n)))
    await DB.commit()
    await update.message.reply_text(f"✅ Drop number ကို {n} အဖြစ် သတ်မှတ်လိုက်သည်။")

@admin_or_owner
async def cmd_gban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await extract_target_user(update, context)
    if not target:
        await update.message.reply_text("❌ အဓိက user တွေ မရွေးနိုင်ပါ")
        return
    await DB.execute("INSERT OR IGNORE INTO banned (id) VALUES (?)", (target.id,))
    await DB.commit()
    await update.message.reply_text(f"🚫 {target.full_name} ({target.id}) ကို global ban လုပ်ပြီးပါပြီ။")

@admin_or_owner
async def cmd_ungban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await extract_target_user(update, context)
    if not target:
        await update.message.reply_text("❌ အဓိက user တွေ မရွေးနိုင်ပါ")
        return
    await DB.execute("DELETE FROM banned WHERE id = ?", (target.id,))
    await DB.commit()
    await update.message.reply_text(f"✅ {target.full_name} ကို unban လုပ်ပြီးပါပြီ။")

@admin_or_owner
async def cmd_gmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await extract_target_user(update, context)
    if not target:
        await update.message.reply_text("❌ အဓိက user တွေ မရွေးနိုင်ပါ")
        return
    await DB.execute("INSERT OR IGNORE INTO muted (id) VALUES (?)", (target.id,))
    await DB.commit()
    await update.message.reply_text(f"🔇 {target.full_name} ကို global mute လုပ်ပြီးပါပြီ။")

@admin_or_owner
async def cmd_ungmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await extract_target_user(update, context)
    if not target:
        await update.message.reply_text("❌ အဓိက user တွေ မရွေးနိုင်ပါ")
        return
    await DB.execute("DELETE FROM muted WHERE id = ?", (target.id,))
    await DB.commit()
    await update.message.reply_text(f"✅ {target.full_name} ကို unmute လုပ်ပြီးပါပြီ။")

@admin_or_owner
async def cmd_importcards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply လုပ်ထားသော message တစ်ခုနဲ့ /importcards သုံးပါ (caption: name|movie optional).")
        return
    msg = update.message.reply_to_message
    caption = msg.caption or ""
    parts = [p.strip() for p in caption.split("|")]
    name = parts[0] if parts[0] else f"Imported-{int(datetime.utcnow().timestamp())}"
    movie = parts[1] if len(parts) > 1 else "Unknown"
    if msg.photo:
        p = msg.photo[-1]
        f = await p.get_file()
        local_path = os.path.join(IMAGES_DIR, f"{p.file_id}.jpg")
        await f.download_to_drive(local_path)
        rar_key, rar_label = pick_rarity()
        cid = await create_card(name, movie, rar_key, rar_label, "photo", p.file_id, local_path, owner_id=0)
        await update.message.reply_text(f"✅ Imported photo as card #{cid} — {rar_label}")
    elif msg.video:
        v = msg.video
        f = await v.get_file()
        local_path = os.path.join(VIDEOS_DIR, f"{v.file_id}.mp4")
        await f.download_to_drive(local_path)
        rar_key = "animated"
        rar_label = RARITY_LABEL_MAP[rar_key]
        cid = await create_card(name, movie, rar_key, rar_label, "video", v.file_id, local_path, owner_id=0)
        await update.message.reply_text(f"✅ Imported video as card #{cid} — {rar_label}")
    else:
        await update.message.reply_text("❌ Reply message တွင် photo သို့ video မပါပါ။")

@admin_or_owner
async def cmd_addsudo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = await extract_target_user(update, context)
    if not target:
        await update.message.reply_text("❌ အဓိက user မရွေးနိုင်ပါ")
        return
    await DB.execute("INSERT OR IGNORE INTO sudo (id) VALUES (?)", (target.id,))
    await DB.commit()
    await update.message.reply_text(f"✅ Sudo user ထည့်ပြီး: {target.full_name} ({target.id})")

@admin_or_owner
async def cmd_sudolist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await DB.execute_fetchall("SELECT id FROM sudo")
    if not rows:
        await update.message.reply_text("Sudo user မရှိသေးပါ။")
        return
    text = "Sudo users:\n" + "\n".join([str(r[0]) for r in rows])
    await update.message.reply_text(text)

@admin_or_owner
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with DB.execute("SELECT value FROM settings WHERE key = ?", ("drop_number",)) as cur:
        r = await cur.fetchone()
    drop = int(r[0]) if r else DROP_NUMBER
    await update.message.reply_text(f"Settings:\nDROP_NUMBER = {drop}\nBACKUP_CHAT = {BACKUP_CHAT or 'not set'}")

@admin_or_owner
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔁 Backup လုပ်နေပါတယ်...")
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = os.path.join(tmpdir, f"catch_backup_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.zip")
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(DB_FILE):
                zf.write(DB_FILE, arcname=os.path.basename(DB_FILE))
            for root, _, files in os.walk(ASSETS_DIR):
                for f in files:
                    full = os.path.join(root, f)
                    arc = os.path.relpath(full, start=ASSETS_DIR)
                    zf.write(full, arcname=os.path.join("assets", arc))
        target = int(BACKUP_CHAT) if BACKUP_CHAT and BACKUP_CHAT.isdigit() else update.effective_user.id
        try:
            await context.bot.send_document(chat_id=target, document=InputFile(zip_path))
            await update.message.reply_text("✅ Backup ပေးပို့်ပြီးပါပြီ။")
        except Exception as e:
            logger.exception("backup failed: %s", e)
            await update.message.reply_text(f"❌ Backup ပို့မရပါ: {e}")

@admin_or_owner
async def cmd_restore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text("Restore လုပ်ချင်ရင် zip file ကို reply လုပ်ပြီး /restore သုံးပါ။")
        return
    doc = update.message.reply_to_message.document
    tmpfile = os.path.join(tempfile.gettempdir(), f"restore_{doc.file_unique_id}.zip")
    await doc.get_file().download_to_drive(tmpfile)
    try:
        with zipfile.ZipFile(tmpfile, 'r') as zf:
            zf.extractall(path=".")
        await update.message.reply_text("✅ Restore ပြီးပါပြီ။ (လိုအပ်လျှင် bot restart လုပ်ပါ)")
    except Exception as e:
        logger.exception("restore failed: %s", e)
        await update.message.reply_text(f"❌ Restore မအောင်မြင်ပါ: {e}")

@admin_or_owner
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text("နမူနာ: /broadcast Hello OR reply to message and use /broadcast")
        return
    rows = await DB.execute_fetchall("SELECT chat_id FROM groups_seen")
    if not rows:
        await update.message.reply_text("No known groups to broadcast.")
        return
    if update.message.reply_to_message:
        for (cid,) in rows:
            try:
                await context.bot.forward_message(chat_id=cid, from_chat_id=update.message.reply_to_message.chat_id,
                                                  message_id=update.message.reply_to_message.message_id)
            except Exception:
                pass
        await update.message.reply_text("✅ Broadcast forwarded.")
    else:
        text = " ".join(context.args)
        for (cid,) in rows:
            try:
                await context.bot.send_message(chat_id=cid, text=text)
            except Exception:
                pass
        await update.message.reply_text("✅ Broadcast sent.")

# ===== User commands =====
@user_allowed
async def cmd_harem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = await DB.execute_fetchall("SELECT id, name, rarity FROM cards WHERE owner_id = ?", (user.id,))
    if not rows:
        await update.message.reply_text("🗃️ ကိုယ့်မှာ card မရှိသေးပါ။")
        return
    text = "🌟 ကိုယ့်ကဒ်များ:\n" + "\n".join([f"#{r[0]} — {r[2]}: {r[1]}" for r in rows])
    await update.message.reply_text(text)

@user_allowed
async def cmd_see(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("အသုံး: /see <card_id>")
        return
    try:
        cid = int(context.args[0])
    except:
        await update.message.reply_text("❌ id မမှန်ပါ")
        return
    card = await get_card(cid)
    if not card:
        await update.message.reply_text("❌ Card မတွေ့ပါ")
        return
    text = f"ID: {card[0]}\nName: {card[1]}\nMovie: {card[2]}\nRarity: {card[3]}\nOwner: {card[8]}"
    ftype = card[5]
    file_id = card[6]
    file_path = card[7]
    try:
        if ftype == "photo":
            if file_id:
                await update.message.reply_photo(photo=file_id, caption=text)
            else:
                await update.message.reply_photo(photo=open(file_path, "rb"), caption=text)
        elif ftype == "video":
            if file_id:
                await update.message.reply_video(video=file_id, caption=text)
            else:
                await update.message.reply_video(video=open(file_path, "rb"), caption=text)
        else:
            await update.message.reply_text(text)
    except Exception:
        await update.message.reply_text(text)

@user_allowed
async def cmd_gift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.reply_to_message:
        await update.message.reply_text("ထိုက်သူကို reply လုပ်ပြီး /gift <card_id> သုံးပါ။")
        return
    if not context.args:
        await update.message.reply_text("Usage: /gift <card_id>")
        return
    try:
        cid = int(context.args[0])
    except:
        await update.message.reply_text("❌ id မမှန်ပါ")
        return
    card = await get_card(cid)
    if not card:
        await update.message.reply_text("❌ Card မတွေ့ပါ")
        return
    if card[8] != update.effective_user.id:
        await update.message.reply_text("❌ သင်ကဒီ card ရဲ့ပိုင်ရှင်မဟုတ်ပါ။")
        return
    target = update.message.reply_to_message.from_user
    await DB.execute("UPDATE cards SET owner_id = ? WHERE id = ?", (target.id, cid))
    await DB.commit()
    await add_coins(update.effective_user.id, 5)
    await update.message.reply_text(f"🎁 Card #{cid} ကို {target.full_name} ထံ လက်ဆောင်ပေးပြီးပါပြီ။ (+5 coins)")

@user_allowed
async def cmd_ziceko(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /ziceko <card_name> (ဒါမမှန်ရင် Drop message ရဲ့ Claim ကိုနှိပ်ပါ)")
        return
    wanted = " ".join(context.args).lower()
    rows = await DB.execute_fetchall("SELECT id,name,rarity,owner_id FROM cards WHERE LOWER(name)=?", (wanted,))
    if not rows:
        await update.message.reply_text("ဒီနာမည်နဲ့ card မတွေ့ပါ။")
        return
    for r in rows:
        if r[3] == 0:
            cid = r[0]
            await DB.execute("UPDATE cards SET owner_id = ? WHERE id = ?", (update.effective_user.id, cid))
            await DB.commit()
            await add_coins(update.effective_user.id, 20)
            await update.message.reply_text(f"🎉 သင်က {r[1]} (#{cid}) ကို claim လိုက်ပြီးပါပြီ (+20 coins)!")
            return
    await update.message.reply_text("အဲ့ဒီနာမည်ရဲ့ unowned card မရှိပါ။")

@user_allowed
async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await DB.execute_fetchall("""
        SELECT owner_id, COUNT(*) as cnt FROM cards WHERE owner_id != 0
        GROUP BY owner_id ORDER BY cnt DESC LIMIT 10
    """)
    if not rows:
        await update.message.reply_text("Top list မရှိသေးပါ။")
        return
    lines = []
    for i, r in enumerate(rows, start=1):
        uid = r[0]
        try:
            user = await context.bot.get_chat(uid)
            name = user.full_name
        except Exception:
            name = str(uid)
        lines.append(f"{i}. {name} — {r[1]} cards")
    await update.message.reply_text("🏆 Top collectors:\n" + "\n".join(lines))

# ===== Coins & Shop commands =====
@user_allowed
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    coins = await get_coins(uid)
    await update.message.reply_text(f"💰 သင့် Coin: {coins}")

@user_allowed
async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    now = datetime.utcnow()
    async with DB.execute("SELECT last_claim FROM daily WHERE user_id = ?", (uid,)) as cur:
        r = await cur.fetchone()
    if r:
        last = datetime.fromisoformat(r[0])
        if now - last < timedelta(hours=24):
            remain = timedelta(hours=24) - (now - last)
            hours = remain.seconds // 3600
            minutes = (remain.seconds % 3600) // 60
            await update.message.reply_text(f"⏳ နောက် {hours} နာရီ {minutes} မိနစ်ကြာမှ ပြန်ယူနိုင်ပါမယ်")
            return
    await DB.execute("INSERT OR REPLACE INTO daily (user_id, last_claim) VALUES (?, ?)", (uid, now.isoformat()))
    await DB.commit()
    await add_coins(uid, 50)
    await update.message.reply_text("🎁 Daily +50 coins ရယူပြီးပါပြီ!")

# Shop UI
def shop_keyboard_for(page_index: int):
    total = len(ITEM_LIST)
    page_index = page_index % total
    key, label, price = ITEM_LIST[page_index]
    buy_cb = f"shopbuy:{key}:{page_index}"
    next_cb = f"shop:page:{(page_index + 1) % total}"
    prev_cb = f"shop:page:{(page_index - 1) % total}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🛒 ဝယ်မယ် ({price} coins)", callback_data=buy_cb)],
        [InlineKeyboardButton("⬅️ နောက်ကောင် Prev", callback_data=prev_cb), InlineKeyboardButton("Next ➡️", callback_data=next_cb)],
        [InlineKeyboardButton("ပိတ်မည် Close", callback_data="shop:close")]
    ])
    return kb

@user_allowed
async def cmd_shop_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    page = 0
    key, label, price = ITEM_LIST[page]
    avail = await count_available_cards(key)
    text = f"🛒 Shop\n\n{label}\nဈေးနှုန်း: {price} coins\nAvailable: {avail} ကဒ်\n\nBuy ကိုနှိပ်ပါ။"
    await update.message.reply_text(text, reply_markup=shop_keyboard_for(page))

async def cb_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "shop:close":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if data.startswith("shop:page:"):
        try:
            page = int(data.split(":")[2])
        except:
            page = 0
        page = page % len(ITEM_LIST)
        key, label, price = ITEM_LIST[page]
        avail = await count_available_cards(key)
        text = f"🛒 Shop\n\n{label}\nဈေးနှုန်း: {price} coins\nAvailable: {avail} ကဒ်\n\nBuy ကိုနှိပ်ပါ။"
        try:
            await query.edit_message_text(text, reply_markup=shop_keyboard_for(page))
        except Exception:
            await query.message.reply_text(text, reply_markup=shop_keyboard_for(page))
        return
    if data.startswith("shopbuy:"):
        parts = data.split(":")
        if len(parts) < 3:
            await query.answer("Invalid request", show_alert=True)
            return
        rarity_key = parts[1]
        try:
            page = int(parts[2])
        except:
            page = 0
        uid = query.from_user.id
        price = SHOP.get(rarity_key, None)
        if price is None:
            await query.answer("Invalid item", show_alert=True)
            return
        # perform atomic purchase under lock
        async with DB_LOCK:
            # pick an unowned card
            async with DB.execute("SELECT id, owner_id FROM cards WHERE rarity_key=? AND owner_id=0 ORDER BY RANDOM() LIMIT 1", (rarity_key,)) as cur:
                row = await cur.fetchone()
            if not row:
                await query.answer("❌ ဒီ rarity က အသင့်ရရှိနိုင်တဲ့ ကဒ် မရှိပါ", show_alert=True)
                return
            cid = row[0]
            # check user coins
            async with DB.execute("SELECT coins FROM users WHERE id = ?", (uid,)) as cur:
                r = await cur.fetchone()
            curcoins = r[0] if r else 0
            if curcoins < price:
                await query.answer("❌ Coins မလုံလောက်ပါ", show_alert=True)
                return
            # assign and deduct
            newcoins = curcoins - price
            if r:
                await DB.execute("UPDATE users SET coins = ? WHERE id = ?", (newcoins, uid))
            else:
                await DB.execute("INSERT INTO users (id, coins) VALUES (?, ?)", (uid, newcoins))
            await DB.execute("UPDATE cards SET owner_id = ? WHERE id = ?", (uid, cid))
            await DB.commit()
        new_coins = await get_coins(uid)
        text = f"✅ သင်ဝယ်ပြီးဖြစ်သည် — Card #{cid} ({RARITY_LABEL_MAP.get(rarity_key, rarity_key)})\nကျန်ရှိ Coins: {new_coins}\n\n/see {cid} ဖြင့် ကြည့်ပါ"
        try:
            await query.edit_message_text(text)
        except Exception:
            await query.message.reply_text(text)
        try:
            await context.bot.send_message(chat_id=uid, text=f"🎉 ဝယ်ယူပြီး — Card #{cid} ({RARITY_LABEL_MAP.get(rarity_key, rarity_key)})")
        except Exception:
            pass
        return

# Inline search
async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query.query.strip().lower()
    results = []
    if not q:
        rows = await DB.execute_fetchall("SELECT id, name, file_id, file_type FROM cards ORDER BY id DESC LIMIT 10")
    else:
        rows = await DB.execute_fetchall("SELECT id, name, file_id, file_type FROM cards WHERE LOWER(name) LIKE ? LIMIT 50", (f"%{q}%",))
    for r in rows[:50]:
        cid, name, file_id, ftype = r
        if ftype == "photo" and file_id:
            results.append(InlineQueryResultCachedPhoto(
                id=str(cid),
                photo_file_id=file_id,
                title=f"{name} (#{cid})",
                description=f"See with /see {cid}"
            ))
        else:
            results.append(InlineQueryResultArticle(
                id=f"art{cid}",
                title=f"{name} (#{cid})",
                input_message_content=InputTextMessageContent(f"{name} — use /see {cid} to view")
            ))
    try:
        await update.inline_query.answer(results[:50], cache_time=15)
    except Exception:
        pass

# ---------------- Drop system ----------------
DROP_LOCKS = {}  # simple in-memory per-chat lock timestamps (prevents duplicate concurrent drops)

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    if update.effective_user and update.effective_user.is_bot:
        return
    user = update.effective_user
    if await is_banned(user.id):
        try:
            await update.message.reply_text("🔒 သင့်ကို global ban ထားပါသည်။")
        except Exception:
            pass
        return
    chat_id = update.effective_chat.id
    # ensure group record
    await DB.execute("INSERT OR IGNORE INTO groups_seen (chat_id, messages_count, last_drop_card_id) VALUES (?, 0, 0)", (chat_id,))
    await DB.execute("UPDATE groups_seen SET messages_count = messages_count + 1 WHERE chat_id = ?", (chat_id,))
    await DB.commit()
    async with DB.execute("SELECT messages_count FROM groups_seen WHERE chat_id = ?", (chat_id,)) as cur:
        row = await cur.fetchone()
        count = row[0] if row else 0
    async with DB.execute("SELECT value FROM settings WHERE key = ?", ("drop_number",)) as cur:
        r = await cur.fetchone()
    drop_n = int(r[0]) if r else DROP_NUMBER
    if count >= drop_n:
        # reset counter
        await DB.execute("UPDATE groups_seen SET messages_count = 0 WHERE chat_id = ?", (chat_id,))
        await DB.commit()
        # prevent duplicate drops concurrently
        now_ts = datetime.utcnow().timestamp()
        last_ts = DROP_LOCKS.get(chat_id, 0)
        if now_ts - last_ts < 2:  # brief debounce
            return
        DROP_LOCKS[chat_id] = now_ts
        # pick random unowned card
        async with DB.execute("SELECT id,name,rarity,file_type,file_id,file_path FROM cards WHERE owner_id=0 ORDER BY RANDOM() LIMIT 1") as cur:
            card = await cur.fetchone()
        if not card:
            try:
                await context.bot.send_message(chat_id=chat_id, text="🎲 Drop ဖြစ်ရန် ကြိုးစားခဲ့သော်လည်း unowned card မရှိသေးပါ။ Admin ပေးပါ။")
            except Exception:
                pass
            return
        card_id, name, rarity, ftype, file_id, file_path = card
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Claim (ziceko)", callback_data=f"claim:{chat_id}:{card_id}")]])
        caption = f"🎁 Card dropped!\n{name}\nRarity: {rarity}\nPress Claim to grab it!"
        try:
            if ftype == "photo":
                if file_id:
                    await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption, reply_markup=kb)
                else:
                    await context.bot.send_photo(chat_id=chat_id, photo=open(file_path, "rb"), caption=caption, reply_markup=kb)
            else:
                if file_id:
                    await context.bot.send_video(chat_id=chat_id, video=file_id, caption=caption, reply_markup=kb)
                else:
                    await context.bot.send_video(chat_id=chat_id, video=open(file_path, "rb"), caption=caption, reply_markup=kb)
            await DB.execute("UPDATE groups_seen SET last_drop_card_id = ? WHERE chat_id = ?", (card_id, chat_id))
            await DB.commit()
        except Exception as e:
            logger.exception("drop send failed: %s", e)

# Callback claim handler
async def cb_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("claim:"):
        return
    parts = data.split(":")
    if len(parts) != 3:
        await query.edit_message_text("Invalid claim data.")
        return
    try:
        chat_id = int(parts[1])
        card_id = int(parts[2])
    except Exception:
        await query.edit_message_text("Invalid claim identifiers.")
        return
    user = query.from_user
    # atomic assign under lock
    async with DB_LOCK:
        async with DB.execute("SELECT owner_id, name, rarity FROM cards WHERE id = ?", (card_id,)) as cur:
            r = await cur.fetchone()
        if not r:
            await query.edit_message_text("This card no longer exists.")
            return
        owner_id = r[0]
        if owner_id != 0:
            await query.edit_message_text("Sorry — someone already claimed it.")
            return
        await DB.execute("UPDATE cards SET owner_id = ? WHERE id = ?", (user.id, card_id))
        await DB.commit()
    await add_coins(user.id, 20)
    await query.edit_message_text(f"🎉 {user.full_name} claimed card #{card_id} — {r[1]} ({r[2]})\n(+20 coins)")
    try:
        await context.bot.send_message(chat_id=user.id, text=f"✅ သင် {r[1]} (#{card_id}) ကို claim လုပ်ပြီး (+20 coins)!")
    except Exception:
        pass

# track when bot added to group
async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = update.chat_member.chat
        if chat and chat.type in ("group", "supergroup"):
            await DB.execute("INSERT OR IGNORE INTO groups_seen (chat_id, messages_count, last_drop_card_id) VALUES (?,0,0)", (chat.id,))
            await DB.commit()
    except Exception:
        pass

# ----------------- Startup & main -----------------
async def main():
    if not TOKEN:
        raise RuntimeError("TOKEN missing in .env")
    await init_db_and_dirs()
    application = ApplicationBuilder().token(TOKEN).build()

    # basic
    application.add_handler(CommandHandler("start", cmd_start))
    # owner/admin
    application.add_handler(CommandHandler("upload", cmd_upload))
    application.add_handler(CommandHandler("uploadvd", cmd_uploadvd))
    application.add_handler(CommandHandler("edit", cmd_edit))
    application.add_handler(CommandHandler("delete", cmd_delete))
    application.add_handler(CommandHandler("setdrop", cmd_setdrop))
    application.add_handler(CommandHandler("gban", cmd_gban))
    application.add_handler(CommandHandler("ungban", cmd_ungban))
    application.add_handler(CommandHandler("gmute", cmd_gmute))
    application.add_handler(CommandHandler("ungmute", cmd_ungmute))
    application.add_handler(CommandHandler("importcards", cmd_importcards))
    application.add_handler(CommandHandler("addsudo", cmd_addsudo))
    application.add_handler(CommandHandler("sudolist", cmd_sudolist))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("backup", cmd_backup))
    application.add_handler(CommandHandler("restore", cmd_restore))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # user
    application.add_handler(CommandHandler("harem", cmd_harem))
    application.add_handler(CommandHandler("see", cmd_see))
    application.add_handler(CommandHandler("gift", cmd_gift))
    application.add_handler(CommandHandler("ziceko", cmd_ziceko))
    application.add_handler(CommandHandler("top", cmd_top))

    # coins & shop
    application.add_handler(CommandHandler("balance", cmd_balance))
    application.add_handler(CommandHandler("daily", cmd_daily))
    application.add_handler(CommandHandler("shop", cmd_shop_buttons))
    application.add_handler(CallbackQueryHandler(cb_shop, pattern=r'^(shop:|shopbuy:)'))

    # inline and claim callback
    application.add_handler(InlineQueryHandler(inline_search))
    application.add_handler(CallbackQueryHandler(cb_claim, pattern=r'^claim:'))

    # group messages & chat member updates
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.UpdateType.EDITED, on_group_message))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_chat_member))
    application.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_chat_member))

    logger.info("Starting Catch Character Bot")
    try:
        await application.run_polling()
    finally:
        await close_db()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
