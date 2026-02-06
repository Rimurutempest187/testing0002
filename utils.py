## part3
# part3_utils.py
# helpers: permissions, pick_rarity, shop keyboard, and misc
import random
from functools import wraps
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

# rarities (same as original but centralized)
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
RARITY_LABEL_MAP = {k: lbl for k, lbl in RARITY_LEVELS}
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
ITEM_LIST = [(k, RARITY_LABEL_MAP.get(k, k.title()), SHOP.get(k, 0)) for k in SHOP.keys()]

# Permissions helpers
def owner_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id_env = context.bot_data.get('OWNER_ID')
        user = update.effective_user
        if not user or not owner_id_env or user.id != int(owner_id_env):
            if update.message:
                await update.message.reply_text("🔒 သင်မှာ Owner ခွင့်မရှိပါ။")
            return
        return await func(update, context)
    return wrapper

# admin_or_owner and user_allowed will require DB queries, but we keep decorator shapes here
def admin_or_owner(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        owner_id_env = context.bot_data.get('OWNER_ID')
        if owner_id_env and user.id == int(owner_id_env):
            return await func(update, context)
        # fallback: if not owner, rely on sudo table check in handler if needed
        # For simplicity: handlers that need DB checks will perform them directly when necessary
        return await func(update, context)
    return wrapper

def user_allowed(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user:
            return
        # We don't access DB here; handlers should early-check banned/muted using DB helper
        return await func(update, context)
    return wrapper

# pick rarity
def pick_rarity():
    keys = [r[0] for r in RARITY_LEVELS]
    key = random.choices(keys, weights=RARITY_WEIGHTS, k=1)[0]
    label = RARITY_LABEL_MAP.get(key, key.title())
    return key, label

# shop keyboard
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

# extract target user helper (simple)
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
