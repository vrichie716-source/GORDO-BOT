"""
Telegram Invite Bot — 𝔾𝕠𝕣𝕕𝕠's Bot
- Shows BTC price from Coinbase
- Language selection (EN / ES)
- Math captcha with multiple choice
- Generates expiring invite links (60s) with countdown timer + visual bar
- Invite links require admin approval (creates_join_request)
- /staff — Show the group staff/team list
- /chatid — Show the current chat's ID and type (admin only)
- Service message cleanup — auto-deletes join/leave/pin/title-change messages
- /privacy_post — Post 𝔾𝕠𝕣𝕕𝕠 Adblocking / Privacy interactive menu
"""

import asyncio
import io
import json
import logging
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone, timedelta

import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember,
    ChatPermissions,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest, TelegramError

from broadcaster import Broadcaster

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these values
# ──────────────────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError(
        "BOT_TOKEN environment variable is not set. "
        "Set it in Railway → Variables before deploying."
    )

# Bot start time — used by /status for uptime reporting
_BOT_START_TIME: float = time.time()

# List your group/channel chat IDs (negative numbers for groups/channels)
# The bot must be an admin with "Invite Users via Link" permission in each one.
GROUP_IDS = [
    -1003786381449,
    -1003884987682,
]

# Optional: human-friendly names shown on the buttons (per language)
GROUP_NAMES = {
    "en": {
        -1003786381449: "VIP Group",
        -1003884987682: "Channel",
    },
    "es": {
        -1003786381449: "Grupo VIP",
        -1003884987682: "Canal",
    },
}

MAIN_GROUP_ID = -1003786381449   # join requests held for manual approval

LINK_EXPIRE_SECONDS = 60        # invite link lifetime
COUNTDOWN_SECONDS   = 60        # visual countdown duration

# Usernames with full admin privileges everywhere (lowercase, no @)
SUPERADMIN_USERNAMES = {"gordo"}
# Superadmin user IDs (always recognised, even before first message)
_superadmin_ids: set[int] = {7032935515}

# Usernames authorised to use /bot broadcaster (lowercase, no @)
BROADCASTER_USERNAMES = {"gordo", "gorda", "lavado"}
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Admin cache ──────────────────────────────────────────────────────────────
# Maps chat_id -> {user_id: ChatMember, ...}
_admin_cache: dict[int, dict[int, ChatMember]] = {}


async def refresh_admin_cache(bot, chat_id: int):
    """Fetch and cache the admin list for a chat."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
        _admin_cache[chat_id] = {a.user.id: a for a in admins}
    except TelegramError as e:
        logger.warning(f"Could not fetch admins for {chat_id}: {e}")


async def is_admin(bot, chat_id: int, user_id: int, username: str | None = None) -> bool:
    """Check if a user is an admin in the given chat (uses cache).
    Also returns True for superadmin usernames/IDs."""
    if is_superadmin(user_id, username):
        return True
    if chat_id not in _admin_cache:
        await refresh_admin_cache(bot, chat_id)
    return user_id in _admin_cache.get(chat_id, {})


def is_superadmin(user_id: int, username: str | None = None) -> bool:
    """Check if a user is a superadmin."""
    if username and username.lower() in SUPERADMIN_USERNAMES:
        _superadmin_ids.add(user_id)
        return True
    return user_id in _superadmin_ids


async def _track_superadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs on every update (group -1) to learn superadmin user IDs."""
    user = update.effective_user
    if user and user.username and user.username.lower() in SUPERADMIN_USERNAMES:
        _superadmin_ids.add(user.id)


# ── Anonymous admin mode (chat_ids where it's on) ────────────────────────────
_anon_admin_chats: set[int] = set()



# ── Federation persistence ───────────────────────────────────────────────────
_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
FED_FILE = os.path.join(_DATA_DIR, "federations.json")
BLOCKLIST_FILE = os.path.join(_DATA_DIR, "blocklist.json")
APPROVED_FILE = os.path.join(_DATA_DIR, "approved.json")
ANTIRAID_FILE = os.path.join(_DATA_DIR, "antiraid.json")
FLOOD_FILE = os.path.join(_DATA_DIR, "flood.json")
MODLOG_FILE = os.path.join(_DATA_DIR, "modlog.json")
SCORES_FILE = os.path.join(_DATA_DIR, "scores.json")
RULES_FILE = os.path.join(_DATA_DIR, "rules.json")
WARN_FILE  = os.path.join(_DATA_DIR, "warnings.json")   # legacy — migrated into scores.json

# Offender scoring escalation thresholds
_SCORE_WARN_THRESHOLD = 3
_SCORE_MUTE_THRESHOLD = 6
_SCORE_BAN_THRESHOLD = 10


def _load_feds() -> dict:
    if os.path.exists(FED_FILE):
        with open(FED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"federations": {}, "chat_to_fed": {}}


def _save_feds(data: dict):
    with open(FED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _get_fed_for_chat(chat_id: int) -> tuple:
    """Return (fed_id, fed_data) for a chat, or (None, None)."""
    data = _load_feds()
    fed_id = data.get("chat_to_fed", {}).get(str(chat_id))
    if fed_id:
        return fed_id, data["federations"].get(fed_id)
    return None, None


# ── Blocklist persistence ────────────────────────────────────────────────────
# Structure: { "chat_id": { "words": [...], "mode": "delete", "delete_msg": true,
#              "reason": "Blocked content" } }

def _load_blocklist() -> dict:
    if os.path.exists(BLOCKLIST_FILE):
        with open(BLOCKLIST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_blocklist(data: dict):
    with open(BLOCKLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _get_chat_blocklist(chat_id: int) -> dict:
    data = _load_blocklist()
    return data.get(str(chat_id), {
        "words": [], "mode": "delete", "delete_msg": True,
        "reason": "⚠️ That content is not allowed here.",
    })


# ── Approved users persistence ───────────────────────────────────────────────
# Structure: { "chat_id": [user_id, ...] }

def _load_approved() -> dict:
    if os.path.exists(APPROVED_FILE):
        with open(APPROVED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_approved(data: dict):
    with open(APPROVED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _is_approved(chat_id: int, user_id: int) -> bool:
    data = _load_approved()
    return user_id in data.get(str(chat_id), [])


# ── Anti-raid persistence ────────────────────────────────────────────────────
# Structure: { "chat_id": { "enabled": false, "raid_time": 300,
#              "action_time": 10, "auto": false } }

def _load_antiraid() -> dict:
    if os.path.exists(ANTIRAID_FILE):
        with open(ANTIRAID_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_antiraid(data: dict):
    with open(ANTIRAID_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _get_antiraid(chat_id: int) -> dict:
    data = _load_antiraid()
    return data.get(str(chat_id), {
        "enabled": False, "raid_time": 300, "action_time": 10, "auto": False,
    })

# Runtime: track recent joins for auto-antiraid detection
_recent_joins: dict[int, list[float]] = {}   # chat_id -> [timestamps]
_raid_active_until: dict[int, float] = {}    # chat_id -> epoch when raid mode expires


# ── Flood control persistence ────────────────────────────────────────────────
# Structure: { "chat_id": { "limit": 0, "timer": 5, "mode": "mute" } }

def _load_flood() -> dict:
    if os.path.exists(FLOOD_FILE):
        with open(FLOOD_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_flood(data: dict):
    with open(FLOOD_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _get_flood(chat_id: int) -> dict:
    data = _load_flood()
    return data.get(str(chat_id), {"limit": 0, "timer": 5, "mode": "mute"})

# Runtime flood counters: { (chat_id, user_id): [timestamps] }
_flood_counter: dict[tuple[int, int], list[float]] = {}


# ── Mod Logs persistence ───────────────────────────────────────────────────────
# Structure: { "chat_id": "log_channel_id" }
def _load_modlog() -> dict:
    if os.path.exists(MODLOG_FILE):
        with open(MODLOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_modlog(data: dict):
    with open(MODLOG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── Offender Scoring persistence ─────────────────────────────────────────────
# Structure: { "chat_id": { "user_id": { "score": N, "history": [{action, reason, time}] } } }
def _load_scores() -> dict:
    if os.path.exists(SCORES_FILE):
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    # Migrate old warnings file if it exists, but then we'll save to scores.json
    if os.path.exists(WARN_FILE):
        try:
            with open(WARN_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
                new_data = {}
                for cid, users in old.items():
                    new_data[cid] = {}
                    for uid, count in users.items():
                        new_data[cid][uid] = {"score": count, "history": []}
                return new_data
        except Exception:
            pass
    return {}

def _save_scores(data: dict):
    with open(SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── Onboarding Rules persistence ─────────────────────────────────────────────
# Structure: { "chat_id": "Rules HTML text" }
def _load_rules() -> dict:
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_rules(data: dict):
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── Core Mod Logging & Scoring Helpers ────────────────────────────────────────

async def _log_mod_action(
    bot, chat_id: int, actor, action: str, target, reason: str, duration: str = ""
):
    channel_id = _load_modlog().get(str(chat_id))
    if not channel_id:
        return
        
    target_name = getattr(target, "full_name", str(target))
    target_id_str = getattr(target, "id", str(target))
    actor_name = getattr(actor, "full_name", str(actor))
    actor_id_str = getattr(actor, "id", str(actor))
    
    text = (
        f"🚨 <b>{action}</b>\n"
        f"<b>Chat:</b> <code>{chat_id}</code>\n"
        f"<b>Actor:</b> {actor_name} [<code>{actor_id_str}</code>]\n"
        f"<b>Target:</b> {target_name} [<code>{target_id_str}</code>]\n"
        f"<b>Reason:</b> {reason or 'No reason provided'}"
    )
    if duration:
        text += f"\n<b>Duration:</b> {duration}"
    try:
        await bot.send_message(channel_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Failed to post modlog: {e}")

async def _add_infraction(
    chat_id: int, target_id: int, action: str, reason: str, actor_id: int, points: int = 1
) -> tuple[int, list[dict]]:
    """Records an infraction and returns (new_score, history)."""
    data = _load_scores()
    chat_data = data.setdefault(str(chat_id), {})
    user_data = chat_data.setdefault(str(target_id), {"score": 0, "history": []})
    
    user_data["score"] += points
    user_data["history"].append({
        "time": time.time(),
        "action": action,
        "reason": reason,
        "actor": actor_id
    })
    _save_scores(data)
    return user_data["score"], user_data["history"]

def _clear_infractions(chat_id: int, target_id: int):
    data = _load_scores()
    chat_data = data.get(str(chat_id), {})
    if str(target_id) in chat_data:
        del chat_data[str(target_id)]
        _save_scores(data)

def _get_infractions(chat_id: int, target_id: int) -> tuple[int, list[dict]]:
    data = _load_scores()
    user_data = data.get(str(chat_id), {}).get(str(target_id), {"score": 0, "history": []})
    return user_data["score"], user_data["history"]

async def send_rules(bot, chat_id: int, user_id: int):
    rules = _load_rules().get(str(chat_id))
    if not rules:
        return
    try:
        await bot.send_message(user_id, rules, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        pass  # Silent skip if DM fails


# ── helpers ──────────────────────────────────────────────────────────────────

def parse_duration(text: str) -> int | None:
    """Parse duration strings like '30m', '2h', '1d' into seconds.
    Returns None if parsing fails."""
    m = re.match(r'^(\d+)([mhd])$', text.lower().strip())
    if not m:
        return None
    val, unit = int(m.group(1)), m.group(2)
    multiplier = {'m': 60, 'h': 3600, 'd': 86400}
    return val * multiplier[unit]


def get_btc_price() -> str:
    try:
        r = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5
        )
        data = r.json()
        price = float(data["data"]["amount"])
        return f"${price:,.2f}"
    except Exception:
        return "N/A"


def build_progress_bar(seconds_left: int, total: int = COUNTDOWN_SECONDS):
    """Return (bar string, percentage int)."""
    pct = seconds_left / total
    filled = round(pct * 12)
    bar = "█" * filled + "░" * (12 - filled)
    return bar, round(pct * 100)


TEXTS = {
    "en": {
        "lang_prompt": (
            "⚡ *WELCOME* ⚡\n\n"
            "₿ BTC/USD · *{btc}*\n\n"
            "Select your language / Selecciona tu idioma:"
        ),
        "math_prompt": (
            "🔐 *VERIFICATION*\n\n"
            "Solve the equation:\n\n"
            "✏️  *{q} \\= ?*\n\n"
            "👇 Tap the correct answer"
        ),
        "wrong": (
            "⛔ *Wrong answer\\!*\n\n"
            "Restarting in 5 seconds\\."
        ),
        "links_intro": (
            "✅ *ACCESS GRANTED*\n\n"
            "🎉 Your invite links are ready\\!\n"
            "Tap a button to send a join request\\.\n"
            "Admins must approve it\\.\n\n"
            "⏱ *{secs}s* · {bar} · *{pct}%*"
        ),
        "expired": (
            "🔒 *LINKS EXPIRED*\n\n"
            "Type /start to get new ones\\."
        ),
        "welcome_btn": "Join",
    },
    "es": {
        "lang_prompt": (
            "⚡ *BIENVENIDO* ⚡\n\n"
            "₿ BTC/USD · *{btc}*\n\n"
            "Select your language / Selecciona tu idioma:"
        ),
        "math_prompt": (
            "🔐 *VERIFICACIÓN*\n\n"
            "Resuelve la ecuación:\n\n"
            "✏️  *{q} \\= ?*\n\n"
            "👇 Toca la respuesta correcta"
        ),
        "wrong": (
            "⛔ *Respuesta incorrecta\\!*\n\n"
            "Reiniciando en 5 segundos\\."
        ),
        "links_intro": (
            "✅ *ACCESO CONCEDIDO*\n\n"
            "🎉 ¡Tus enlaces están listos\\!\n"
            "Toca un botón para solicitar unirte\\.\n"
            "Los admins deben aprobar la solicitud\\.\n\n"
            "⏱ *{secs}s* · {bar} · *{pct}%*"
        ),
        "expired": (
            "🔒 *ENLACES EXPIRADOS*\n\n"
            "Escribe /start para obtener nuevos\\."
        ),
        "welcome_btn": "Unirse",
    },
}

# ── state helpers ─────────────────────────────────────────────────────────────

def user_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    if "state" not in context.user_data:
        context.user_data["state"] = {}
    return context.user_data["state"]


async def delete_tracked(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Delete all messages we sent to this user."""
    msgs: list = context.user_data.pop("sent_msgs", [])
    for mid in msgs:
        try:
            await context.bot.delete_message(chat_id, mid)
        except BadRequest:
            pass


def track(context: ContextTypes.DEFAULT_TYPE, message_id: int):
    context.user_data.setdefault("sent_msgs", []).append(message_id)


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve target user_id from a reply or command arguments.
    Returns (user_id, extra_text) or (None, None)."""
    msg = update.message
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target_id = msg.reply_to_message.from_user.id
        reason = " ".join(context.args) if context.args else None
        return target_id, reason
    if context.args:
        try:
            target_id = int(context.args[0])
            reason = " ".join(context.args[1:]) if len(context.args) > 1 else None
            return target_id, reason
        except ValueError:
            pass
    return None, None

# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await delete_tracked(context, chat_id)
    context.user_data.clear()

    # Cancel any running countdown
    job = context.user_data.pop("countdown_job", None)
    if job:
        job.schedule_removal()

    btc = get_btc_price()
    context.user_data["btc"] = btc

    # Escape BTC price for MarkdownV2
    btc_escaped = btc.replace("$", "\\$").replace(",", "\\,").replace(".", "\\.")
    text = TEXTS["en"]["lang_prompt"].format(btc=btc_escaped)

    kb = [
        [
            InlineKeyboardButton("🇺🇸  English", callback_data="lang_en", style="primary"),
            InlineKeyboardButton("🇲🇽  Español", callback_data="lang_es", style="primary"),
        ]
    ]
    msg = await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="MarkdownV2"
    )
    track(context, msg.message_id)

# ── language selection ────────────────────────────────────────────────────────

async def handle_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    lang = "en" if query.data == "lang_en" else "es"
    context.user_data["lang"] = lang

    # Generate a math question
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    op = random.choice(["+", "-"])
    answer = a + b if op == "+" else a - b
    question = f"{a} {op} {b}"

    # Build 4 choices (correct + 3 wrong)
    wrong_pool = list(range(answer - 5, answer + 6))
    wrong_pool = [x for x in wrong_pool if x != answer]
    choices = random.sample(wrong_pool, 3) + [answer]
    random.shuffle(choices)

    context.user_data["math_answer"] = answer

    t = TEXTS[lang]
    # Escape special chars in question for MarkdownV2
    q_escaped = question.replace("+", "\\+").replace("-", "\\-")
    kb = [[InlineKeyboardButton(str(c), callback_data=f"math_{c}", style="primary") for c in choices]]

    chat_id = query.message.chat_id
    await delete_tracked(context, chat_id)

    msg = await context.bot.send_message(
        chat_id,
        t["math_prompt"].format(q=q_escaped),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="MarkdownV2",
    )
    track(context, msg.message_id)

# ── math answer ───────────────────────────────────────────────────────────────

async def handle_math(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    lang    = context.user_data.get("lang", "en")
    correct = context.user_data.get("math_answer")
    chosen  = int(query.data.split("_", 1)[1])
    chat_id = query.message.chat_id

    if chosen != correct:
        # Show error, clear after 5 s, restart
        await delete_tracked(context, chat_id)
        err_msg = await context.bot.send_message(
            chat_id, TEXTS[lang]["wrong"], parse_mode="MarkdownV2"
        )
        await asyncio.sleep(5)
        await context.bot.delete_message(chat_id, err_msg.message_id)
        # Re-run /start flow
        await context.bot.send_message(chat_id, "/start")
        return

    # ── Correct! Generate invite links ──
    await delete_tracked(context, chat_id)

    expire_epoch = int(time.time()) + LINK_EXPIRE_SECONDS

    # Build invite-link buttons — creates_join_request requires admin approval
    link_buttons = []
    invite_links = []   # store (gid, invite_link_str) for later revocation
    for gid in GROUP_IDS:
        try:
            link_obj = await context.bot.create_chat_invite_link(
                chat_id=gid,
                expire_date=expire_epoch,
                creates_join_request=True,
            )
            invite_links.append((gid, link_obj.invite_link))
            name = GROUP_NAMES.get(lang, {}).get(gid) or GROUP_NAMES["en"].get(gid, str(gid))
            btn_label = f"{TEXTS[lang]['welcome_btn']} → {name}"
            link_buttons.append([InlineKeyboardButton(btn_label, url=link_obj.invite_link, style="success")])
        except Exception as e:
            logger.warning(f"Could not create invite for {gid}: {e}")

    if not link_buttons:
        await context.bot.send_message(
            chat_id,
            "⚠️ Could not generate invite links. Make sure I am an admin in your groups.",
        )
        return

    t = TEXTS[lang]
    secs = COUNTDOWN_SECONDS
    bar, pct = build_progress_bar(secs)

    msg = await context.bot.send_message(
        chat_id,
        t["links_intro"].format(bar=bar, secs=secs, pct=pct),
        reply_markup=InlineKeyboardMarkup(link_buttons),
        parse_mode="MarkdownV2",
    )
    track(context, msg.message_id)

    # Store for countdown job
    context.user_data["countdown_msg_id"] = msg.message_id
    context.user_data["countdown_chat_id"] = chat_id
    context.user_data["countdown_lang"]    = lang
    context.user_data["countdown_buttons"] = link_buttons
    context.user_data["countdown_secs"]    = secs
    context.user_data["invite_links"]      = invite_links

    # Schedule tick every second
    job = context.job_queue.run_repeating(
        countdown_tick,
        interval=1,
        first=1,
        data={"user_data": context.user_data},
        chat_id=chat_id,
        user_id=query.from_user.id,
    )
    context.user_data["countdown_job"] = job

# ── countdown tick ────────────────────────────────────────────────────────────

async def countdown_tick(context: ContextTypes.DEFAULT_TYPE):
    ud: dict = context.job.data["user_data"]

    secs: int = ud.get("countdown_secs", 0) - 1
    ud["countdown_secs"] = secs

    chat_id = ud["countdown_chat_id"]
    msg_id  = ud["countdown_msg_id"]
    lang    = ud["countdown_lang"]
    buttons = ud["countdown_buttons"]
    t       = TEXTS[lang]

    if secs <= 0:
        # Expired — revoke invite links
        context.job.schedule_removal()
        for gid, link_str in ud.get("invite_links", []):
            try:
                await context.bot.revoke_chat_invite_link(gid, link_str)
                logger.info(f"Revoked invite link for {gid}")
            except TelegramError:
                pass
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=t["expired"],
                parse_mode="MarkdownV2",
            )
        except BadRequest:
            pass
        return

    bar, pct = build_progress_bar(secs)
    text = t["links_intro"].format(bar=bar, secs=secs, pct=pct)

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="MarkdownV2",
        )
    except BadRequest:
        pass  # message not modified or already deleted


# ══════════════════════════════════════════════════════════════════════════════
# NEW FEATURES
# ══════════════════════════════════════════════════════════════════════════════

# ── /chatid — Show current chat's ID and type (admin only) ───────────────────

async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    # In groups, restrict to admins only
    if chat.type in ("group", "supergroup", "channel"):
        if not await is_admin(context.bot, chat.id, user.id):
            await update.message.reply_text("⛔ This command is for admins only.")
            return

    type_labels = {
        "private": "🔒 Private chat",
        "group": "👥 Group",
        "supergroup": "👥 Supergroup",
        "channel": "📢 Channel",
    }
    label = type_labels.get(chat.type, chat.type)
    title = f"\n📝 Title: {chat.title}" if chat.title else ""

    await update.message.reply_text(
        f"ℹ️ <b>Chat Info</b>\n\n"
        f"🆔 ID: <code>{chat.id}</code>\n"
        f"📂 Type: {label}{title}",
        parse_mode="HTML",
    )


# ── /staff — Show the group staff/team list ──────────────────────────────────

async def staff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ This command only works in groups.")
        return

    # Always fetch fresh for /staff
    await refresh_admin_cache(context.bot, chat.id)
    admins = _admin_cache.get(chat.id, {})

    if not admins:
        await update.message.reply_text("⚠️ Could not retrieve the admin list.")
        return

    creator = []
    admin_list = []

    for uid, member in admins.items():
        user = member.user
        if user.is_bot:
            continue
        name = user.full_name
        username = f" (@{user.username})" if user.username else ""
        title = f" — {member.custom_title}" if member.custom_title else ""

        if member.status == "creator":
            creator.append(f"👑 {name}{username}{title}")
        else:
            admin_list.append(f"⭐ {name}{username}{title}")

    lines = ["<b>👮 Group Staff</b>\n"]
    if creator:
        lines.append("<b>Owner:</b>")
        lines.extend(creator)
        lines.append("")
    if admin_list:
        lines.append(f"<b>Admins ({len(admin_list)}):</b>")
        lines.extend(admin_list)

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── Service message cleanup ──────────────────────────────────────────────────
# Auto-deletes join/leave/pin/title-change system messages in groups.

async def cleanup_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete system/service messages to keep the chat clean."""
    if update.message:
        try:
            await update.message.delete()
        except (BadRequest, TelegramError):
            pass  # Bot may lack delete permission


# ── Join request handler ─────────────────────────────────────────────────────
# Main group: hold for manual admin approval.  Other groups: auto-approve.

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    chat_id = request.chat.id
    user = request.from_user

    if chat_id == MAIN_GROUP_ID:
        logger.info(
            f"Join request from {user.full_name} ({user.id}) for main group "
            f"— held for manual approval"
        )
        return  # admins approve manually

    # Other groups: auto-approve
    try:
        await request.approve()
        logger.info(f"Auto-approved {user.full_name} ({user.id}) for {chat_id}")
    except TelegramError as e:
        logger.warning(f"Could not approve {user.id} for {chat_id}: {e}")


# ── Welcome on member join ───────────────────────────────────────────────────

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """When a user joins, show a welcome message with their info."""
    result = update.chat_member
    new = result.new_chat_member
    old = result.old_chat_member

    # Only care about users transitioning TO member/restricted
    if new.status not in ("member", "restricted"):
        return
    if old.status in ("member", "administrator", "creator", "restricted"):
        return

    chat_id = result.chat.id
    user = new.user
    if user.is_bot:
        return

    # Check if user is fedbanned → auto-ban
    fed_id, fed = _get_fed_for_chat(chat_id)
    if fed and str(user.id) in fed.get("bans", {}):
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            logger.info(f"Auto-banned fedbanned user {user.id} in {chat_id}")
        except TelegramError:
            pass
        return

    # Anti-raid: ban joiners while raid mode is active
    if chat_id in _raid_active_until:
        if time.time() < _raid_active_until[chat_id]:
            try:
                await context.bot.ban_chat_member(chat_id, user.id)
                logger.info(f"Anti-raid: banned {user.id} in {chat_id}")
            except TelegramError:
                pass
            return
        else:
            _raid_active_until.pop(chat_id, None)

    # Auto anti-raid detection: track join timestamps
    ar = _get_antiraid(chat_id)
    if ar.get("auto"):
        now = time.time()
        joins = _recent_joins.setdefault(chat_id, [])
        joins.append(now)
        _recent_joins[chat_id] = [t for t in joins if t > now - 60]
        if len(_recent_joins[chat_id]) >= ar.get("action_time", 10):
            _raid_active_until[chat_id] = now + ar.get("raid_time", 300)
            _recent_joins[chat_id] = []
            try:
                await context.bot.send_message(
                    chat_id,
                    "🛡 <b>ANTI-RAID ACTIVATED</b> — Join spike detected!\n"
                    f"New joiners will be auto-banned for {ar.get('raid_time', 300)}s.",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass
            # Ban this user too
            try:
                await context.bot.ban_chat_member(chat_id, user.id)
            except TelegramError:
                pass
            return

    # Send onboarding rules if configured
    await send_rules(context.bot, chat_id, user.id)

    # Welcome message — only in the main group
    if chat_id != MAIN_GROUP_ID:
        return

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    uname = f"@{user.username}" if user.username else "N/A"
    await context.bot.send_message(
        chat_id,
        f"👋 Welcome <b>{user.full_name}</b>!\n\n"
        f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
        f"👤 <b>Name:</b> {user.full_name}\n"
        f"🔗 <b>Username:</b> {uname}\n"
        f"📅 <b>Joined:</b> {now_str}",
        parse_mode="HTML",
    )


# ── /admincache — Refresh the cached admin list ──────────────────────────────

async def admincache_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this command in a group.")
        return
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    await refresh_admin_cache(context.bot, chat.id)
    count = len(_admin_cache.get(chat.id, {}))
    await update.message.reply_text(f"✅ Admin cache refreshed. {count} admins cached.")


# ── /anonadmin — Toggle anonymous admin mode ─────────────────────────────────

async def anonadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this command in a group.")
        return
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return

    if chat.id in _anon_admin_chats:
        _anon_admin_chats.discard(chat.id)
        await update.message.reply_text(
            "👤 Anonymous admin mode: <b>OFF</b>", parse_mode="HTML",
        )
    else:
        _anon_admin_chats.add(chat.id)
        await update.message.reply_text(
            "🕶 Anonymous admin mode: <b>ON</b>\n"
            "Admin actions will not show who performed them.",
            parse_mode="HTML",
        )


# ── /adminerror — Debug admin permission errors ──────────────────────────────

async def adminerror_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    bot_user = await context.bot.get_me()

    lines = ["<b>🔧 Admin Permission Debug</b>\n"]

    # Bot's permissions
    try:
        bm = await context.bot.get_chat_member(chat.id, bot_user.id)
        lines.append(f"🤖 Bot status: <b>{bm.status}</b>")
        if hasattr(bm, "can_restrict_members"):
            lines.append(f"  can_restrict_members: {bm.can_restrict_members}")
            lines.append(f"  can_delete_messages: {bm.can_delete_messages}")
            lines.append(f"  can_invite_users: {bm.can_invite_users}")
            lines.append(f"  can_promote_members: {bm.can_promote_members}")
            lines.append(f"  can_manage_chat: {bm.can_manage_chat}")
    except TelegramError as e:
        lines.append(f"⚠️ Could not check bot permissions: {e}")

    # User's permissions
    try:
        um = await context.bot.get_chat_member(chat.id, user.id)
        lines.append(f"\n👤 Your status: <b>{um.status}</b>")
    except TelegramError as e:
        lines.append(f"\n⚠️ Could not check your permissions: {e}")

    # Cache status
    cached = chat.id in _admin_cache
    lines.append(f"\n📦 Admin cache: {'loaded' if cached else 'not loaded'}")
    if cached:
        lines.append(f"  Cached admins: {len(_admin_cache[chat.id])}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── /promote / /demote ───────────────────────────────────────────────────────

async def promote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this command in a group.")
        return
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return

    target_id, title = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /promote &lt;reply|user_id&gt; [title]", parse_mode="HTML")
        return

    try:
        await context.bot.promote_chat_member(
            chat.id, target_id,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_manage_chat=True,
            can_manage_video_chats=True,
        )
        if title:
            try:
                await context.bot.set_chat_administrator_custom_title(
                    chat.id, target_id, title[:16],
                )
            except TelegramError:
                pass
        await refresh_admin_cache(context.bot, chat.id)
        await update.message.reply_text(
            f"✅ User <code>{target_id}</code> has been promoted.", parse_mode="HTML",
        )
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ Could not promote: {e}")


async def demote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this command in a group.")
        return
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return

    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /demote &lt;reply|user_id&gt;", parse_mode="HTML")
        return

    try:
        await context.bot.promote_chat_member(
            chat.id, target_id,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_manage_chat=False,
            can_manage_video_chats=False,
            can_promote_members=False,
        )
        await refresh_admin_cache(context.bot, chat.id)
        await update.message.reply_text(
            f"✅ User <code>{target_id}</code> has been demoted.", parse_mode="HTML",
        )
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ Could not demote: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# FEDERATION COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def newfed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /newfed &lt;federation name&gt;", parse_mode="HTML")
        return
    name = " ".join(context.args)
    user_id = update.effective_user.id
    fed_id = str(uuid.uuid4())[:8]

    data = _load_feds()
    data["federations"][fed_id] = {
        "name": name,
        "owner_id": user_id,
        "admins": [user_id],
        "chats": [],
        "bans": {},
    }
    _save_feds(data)

    await update.message.reply_text(
        f"✅ Federation <b>{name}</b> created!\n"
        f"🆔 ID: <code>{fed_id}</code>\n\n"
        f"Use /joinfed {fed_id} in a group to add it.",
        parse_mode="HTML",
    )


async def joinfed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use this command in a group.")
        return
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /joinfed &lt;federation_id&gt;", parse_mode="HTML")
        return

    fed_id = context.args[0]
    data = _load_feds()

    if fed_id not in data["federations"]:
        await update.message.reply_text("⚠️ Federation not found.")
        return
    existing = data.get("chat_to_fed", {}).get(str(chat.id))
    if existing:
        await update.message.reply_text(
            f"This group is already in federation <code>{existing}</code>.",
            parse_mode="HTML",
        )
        return

    data["federations"][fed_id]["chats"].append(chat.id)
    data.setdefault("chat_to_fed", {})[str(chat.id)] = fed_id
    _save_feds(data)

    fed_name = data["federations"][fed_id]["name"]
    await update.message.reply_text(
        f"✅ Group joined federation <b>{fed_name}</b>.", parse_mode="HTML",
    )


async def leavefed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Use this command in a group.")
        return
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return

    data = _load_feds()
    fed_id = data.get("chat_to_fed", {}).get(str(chat.id))
    if not fed_id:
        await update.message.reply_text("This group isn't in any federation.")
        return

    fed = data["federations"].get(fed_id)
    if fed and chat.id in fed["chats"]:
        fed["chats"].remove(chat.id)
    data["chat_to_fed"].pop(str(chat.id), None)
    _save_feds(data)

    await update.message.reply_text("✅ Group left the federation.")


async def fedban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    fed_id, fed = _get_fed_for_chat(chat.id)
    if not fed:
        await update.message.reply_text("This group isn't in any federation.")
        return
    if user.id not in fed["admins"]:
        await update.message.reply_text("⛔ Federation admins only.")
        return

    target_id, reason = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /fedban &lt;user_id|reply&gt; [reason]", parse_mode="HTML")
        return

    data = _load_feds()
    data["federations"][fed_id]["bans"][str(target_id)] = {
        "reason": reason or "No reason",
        "banned_by": user.id,
    }
    _save_feds(data)

    # Ban in all federated chats
    banned_in = 0
    for gid in fed["chats"]:
        try:
            await context.bot.ban_chat_member(gid, target_id)
            banned_in += 1
        except TelegramError:
            pass

    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> fedbanned in <b>{fed['name']}</b>.\n"
        f"Banned in {banned_in}/{len(fed['chats'])} chats.\n"
        f"Reason: {reason or 'No reason'}",
        parse_mode="HTML",
    )


async def unfedban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    fed_id, fed = _get_fed_for_chat(chat.id)
    if not fed:
        await update.message.reply_text("This group isn't in any federation.")
        return
    if user.id not in fed["admins"]:
        await update.message.reply_text("⛔ Federation admins only.")
        return

    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /unfedban &lt;user_id|reply&gt;", parse_mode="HTML")
        return

    data = _load_feds()
    data["federations"][fed_id]["bans"].pop(str(target_id), None)
    _save_feds(data)

    for gid in fed["chats"]:
        try:
            await context.bot.unban_chat_member(gid, target_id, only_if_banned=True)
        except TelegramError:
            pass

    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> un-fedbanned from <b>{fed['name']}</b>.",
        parse_mode="HTML",
    )


async def fedpromote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    fed_id, fed = _get_fed_for_chat(chat.id)
    if not fed:
        await update.message.reply_text("This group isn't in any federation.")
        return
    if user.id != fed["owner_id"]:
        await update.message.reply_text("⛔ Only the federation owner can do this.")
        return

    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /fedpromote &lt;user_id|reply&gt;", parse_mode="HTML")
        return

    data = _load_feds()
    if target_id not in data["federations"][fed_id]["admins"]:
        data["federations"][fed_id]["admins"].append(target_id)
        _save_feds(data)

    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> is now a federation admin.",
        parse_mode="HTML",
    )


async def feddemote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    fed_id, fed = _get_fed_for_chat(chat.id)
    if not fed:
        await update.message.reply_text("This group isn't in any federation.")
        return
    if user.id != fed["owner_id"]:
        await update.message.reply_text("⛔ Only the federation owner can do this.")
        return

    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /feddemote &lt;user_id|reply&gt;", parse_mode="HTML")
        return
    if target_id == fed["owner_id"]:
        await update.message.reply_text("Can't demote the owner.")
        return

    data = _load_feds()
    admins = data["federations"][fed_id]["admins"]
    if target_id in admins:
        admins.remove(target_id)
        _save_feds(data)

    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> removed from federation admins.",
        parse_mode="HTML",
    )


async def fedadmins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    fed_id, fed = _get_fed_for_chat(chat.id)
    if not fed:
        await update.message.reply_text("This group isn't in any federation.")
        return

    lines = [f"<b>👮 Federation Admins — {fed['name']}</b>\n"]
    for uid in fed["admins"]:
        tag = "👑 Owner" if uid == fed["owner_id"] else "⭐ Admin"
        try:
            member = await context.bot.get_chat(uid)
            name = member.full_name or str(uid)
        except TelegramError:
            name = str(uid)
        lines.append(f"{tag}: {name} (<code>{uid}</code>)")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def fedinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if context.args:
        fed_id = context.args[0]
        data = _load_feds()
        fed = data["federations"].get(fed_id)
    else:
        fed_id, fed = _get_fed_for_chat(chat.id)

    if not fed:
        await update.message.reply_text("Federation not found. Usage: /fedinfo [fed_id]")
        return

    try:
        owner = await context.bot.get_chat(fed["owner_id"])
        owner_name = owner.full_name
    except TelegramError:
        owner_name = str(fed["owner_id"])

    await update.message.reply_text(
        f"<b>📋 Federation Info</b>\n\n"
        f"🏷 Name: <b>{fed['name']}</b>\n"
        f"🆔 ID: <code>{fed_id}</code>\n"
        f"👑 Owner: {owner_name}\n"
        f"👮 Admins: {len(fed['admins'])}\n"
        f"💬 Chats: {len(fed['chats'])}\n"
        f"🚫 Bans: {len(fed.get('bans', {}))}",
        parse_mode="HTML",
    )


async def fedchats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    fed_id, fed = _get_fed_for_chat(chat.id)
    if not fed:
        await update.message.reply_text("This group isn't in any federation.")
        return
    if user.id not in fed["admins"]:
        await update.message.reply_text("⛔ Federation admins only.")
        return

    lines = [f"<b>💬 Federated Chats — {fed['name']}</b>\n"]
    for gid in fed["chats"]:
        try:
            c = await context.bot.get_chat(gid)
            lines.append(f"• {c.title or gid} (<code>{gid}</code>)")
        except TelegramError:
            lines.append(f"• <code>{gid}</code> (inaccessible)")

    if not fed["chats"]:
        lines.append("No chats in this federation yet.")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════════════════
# BLOCKLIST COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def addblocklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this in a group.")
        return
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addblocklist &lt;word or phrase&gt;", parse_mode="HTML")
        return

    word = " ".join(context.args).lower()
    data = _load_blocklist()
    entry = data.setdefault(str(chat.id), {
        "words": [], "mode": "delete", "delete_msg": True,
        "reason": "⚠️ That content is not allowed here.",
    })
    if word in entry["words"]:
        await update.message.reply_text(f"<code>{word}</code> is already blocklisted.", parse_mode="HTML")
        return
    entry["words"].append(word)
    _save_blocklist(data)
    await update.message.reply_text(f"✅ Added <code>{word}</code> to the blocklist.", parse_mode="HTML")


async def rmblocklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this in a group.")
        return
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /rmblocklist &lt;word or phrase&gt;", parse_mode="HTML")
        return

    word = " ".join(context.args).lower()
    data = _load_blocklist()
    entry = data.get(str(chat.id))
    if not entry or word not in entry.get("words", []):
        await update.message.reply_text(f"<code>{word}</code> is not in the blocklist.", parse_mode="HTML")
        return
    entry["words"].remove(word)
    _save_blocklist(data)
    await update.message.reply_text(f"✅ Removed <code>{word}</code> from the blocklist.", parse_mode="HTML")


async def blocklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    bl = _get_chat_blocklist(chat.id)
    words = bl.get("words", [])
    if not words:
        await update.message.reply_text("📋 The blocklist is empty.")
        return
    listed = "\n".join(f"• <code>{w}</code>" for w in words)
    await update.message.reply_text(
        f"<b>🚫 Blocklist ({len(words)} words)</b>\n\n{listed}\n\n"
        f"Mode: <b>{bl.get('mode', 'delete')}</b> | Delete msg: <b>{bl.get('delete_msg', True)}</b>",
        parse_mode="HTML",
    )


async def blocklistmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args or context.args[0].lower() not in ("delete", "mute", "ban"):
        await update.message.reply_text("Usage: /blocklistmode &lt;delete|mute|ban&gt;", parse_mode="HTML")
        return
    mode = context.args[0].lower()
    data = _load_blocklist()
    entry = data.setdefault(str(chat.id), {
        "words": [], "mode": "delete", "delete_msg": True,
        "reason": "⚠️ That content is not allowed here.",
    })
    entry["mode"] = mode
    _save_blocklist(data)
    await update.message.reply_text(f"✅ Blocklist mode set to <b>{mode}</b>.", parse_mode="HTML")


async def blocklistdelete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    data = _load_blocklist()
    entry = data.setdefault(str(chat.id), {
        "words": [], "mode": "delete", "delete_msg": True,
        "reason": "⚠️ That content is not allowed here.",
    })
    entry["delete_msg"] = not entry.get("delete_msg", True)
    _save_blocklist(data)
    state = "ON" if entry["delete_msg"] else "OFF"
    await update.message.reply_text(f"✅ Blocklist message deletion: <b>{state}</b>", parse_mode="HTML")


async def setblocklistreason_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setblocklistreason &lt;reason text&gt;", parse_mode="HTML")
        return
    reason = " ".join(context.args)
    data = _load_blocklist()
    entry = data.setdefault(str(chat.id), {
        "words": [], "mode": "delete", "delete_msg": True,
        "reason": "⚠️ That content is not allowed here.",
    })
    entry["reason"] = reason
    _save_blocklist(data)
    await update.message.reply_text("✅ Blocklist reason updated.")


async def resetblocklistreason_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    data = _load_blocklist()
    entry = data.get(str(chat.id))
    if entry:
        entry["reason"] = "⚠️ That content is not allowed here."
        _save_blocklist(data)
    await update.message.reply_text("✅ Blocklist reason reset to default.")


async def unblocklistall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    data = _load_blocklist()
    data.pop(str(chat.id), None)
    _save_blocklist(data)
    await update.message.reply_text("✅ Blocklist cleared.")


# ══════════════════════════════════════════════════════════════════════════════
# APPROVAL COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /approve &lt;reply|user_id&gt;", parse_mode="HTML")
        return
    data = _load_approved()
    lst = data.setdefault(str(chat.id), [])
    if target_id not in lst:
        lst.append(target_id)
        _save_approved(data)
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> approved — bypasses captcha & flood.",
        parse_mode="HTML",
    )


async def unapprove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /unapprove &lt;reply|user_id&gt;", parse_mode="HTML")
        return
    data = _load_approved()
    lst = data.get(str(chat.id), [])
    if target_id in lst:
        lst.remove(target_id)
        _save_approved(data)
    await update.message.reply_text(
        f"✅ User <code>{target_id}</code> approval removed.", parse_mode="HTML",
    )


async def approved_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    data = _load_approved()
    lst = data.get(str(chat.id), [])
    if not lst:
        await update.message.reply_text("📋 No approved users in this chat.")
        return
    lines = [f"<b>✅ Approved Users ({len(lst)})</b>\n"]
    for uid in lst:
        try:
            u = await context.bot.get_chat(uid)
            lines.append(f"• {u.full_name} (<code>{uid}</code>)")
        except TelegramError:
            lines.append(f"• <code>{uid}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def unapproveall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    data = _load_approved()
    data.pop(str(chat.id), None)
    _save_approved(data)
    await update.message.reply_text("✅ All approvals cleared.")


async def approval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    data = _load_approved()
    lst = data.get(str(chat.id), [])
    bl = _get_chat_blocklist(chat.id)
    fl = _get_flood(chat.id)
    ar = _get_antiraid(chat.id)
    await update.message.reply_text(
        f"<b>⚙️ Approval & Protection Settings</b>\n\n"
        f"✅ Approved users: <b>{len(lst)}</b>\n"
        f"🚫 Blocklist words: <b>{len(bl.get('words', []))}</b>\n"
        f"🌊 Flood limit: <b>{fl['limit'] or 'OFF'}</b>\n"
        f"🛡 Anti-raid: <b>{'ON' if ar['enabled'] else 'OFF'}</b>",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
# ANTI-RAID COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def antiraid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    data = _load_antiraid()
    entry = data.setdefault(str(chat.id), {
        "enabled": False, "raid_time": 300, "action_time": 10, "auto": False,
    })
    entry["enabled"] = not entry["enabled"]
    _save_antiraid(data)

    if entry["enabled"]:
        _raid_active_until[chat.id] = time.time() + entry["raid_time"]
        await update.message.reply_text(
            f"🛡 Anti-raid: <b>ON</b> for {entry['raid_time']}s\n"
            f"New joiners will be auto-banned.", parse_mode="HTML",
        )
    else:
        _raid_active_until.pop(chat.id, None)
        await update.message.reply_text("🛡 Anti-raid: <b>OFF</b>", parse_mode="HTML")


async def raidtime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /raidtime &lt;seconds&gt;", parse_mode="HTML")
        return
    try:
        secs = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please provide a number of seconds.")
        return
    if secs < 30 or secs > 86400:
        await update.message.reply_text("Value must be between 30 and 86400 seconds.")
        return
    data = _load_antiraid()
    entry = data.setdefault(str(chat.id), {
        "enabled": False, "raid_time": 300, "action_time": 10, "auto": False,
    })
    entry["raid_time"] = secs
    _save_antiraid(data)
    await update.message.reply_text(f"✅ Raid duration set to <b>{secs}s</b>.", parse_mode="HTML")


async def raidactiontime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /raidactiontime &lt;joins_per_minute&gt;\n"
            "Number of joins within 60 s that triggers raid mode.",
            parse_mode="HTML",
        )
        return
    try:
        threshold = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please provide a number.")
        return
    if threshold < 2 or threshold > 100:
        await update.message.reply_text("Threshold must be between 2 and 100.")
        return
    data = _load_antiraid()
    entry = data.setdefault(str(chat.id), {
        "enabled": False, "raid_time": 300, "action_time": 10, "auto": False,
    })
    entry["action_time"] = threshold
    _save_antiraid(data)
    await update.message.reply_text(
        f"✅ Raid triggers at <b>{threshold}</b> joins per minute.", parse_mode="HTML",
    )


async def autoantiraid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    data = _load_antiraid()
    entry = data.setdefault(str(chat.id), {
        "enabled": False, "raid_time": 300, "action_time": 10, "auto": False,
    })
    entry["auto"] = not entry["auto"]
    _save_antiraid(data)
    state = "ON" if entry["auto"] else "OFF"
    await update.message.reply_text(
        f"✅ Auto anti-raid: <b>{state}</b>\n"
        f"{'Will auto-activate when a join spike is detected.' if entry['auto'] else 'Disabled.'}",
        parse_mode="HTML",
    )


# ══════════════════════════════════════════════════════════════════════════════
# FLOOD CONTROL COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def flood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    fl = _get_flood(chat.id)
    limit_str = str(fl["limit"]) if fl["limit"] else "OFF"
    await update.message.reply_text(
        f"<b>🌊 Flood Settings</b>\n\n"
        f"Message limit: <b>{limit_str}</b>\n"
        f"Time window: <b>{fl['timer']}s</b>\n"
        f"Action: <b>{fl['mode']}</b>",
        parse_mode="HTML",
    )


async def setflood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /setflood &lt;number|off&gt;\nMessages allowed in the time window.",
            parse_mode="HTML",
        )
        return
    val = context.args[0].lower()
    if val == "off":
        limit = 0
    else:
        try:
            limit = int(val)
        except ValueError:
            await update.message.reply_text("Provide a number or 'off'.")
            return
        if limit < 2:
            await update.message.reply_text("Minimum is 2.")
            return
    data = _load_flood()
    entry = data.setdefault(str(chat.id), {"limit": 0, "timer": 5, "mode": "mute"})
    entry["limit"] = limit
    _save_flood(data)
    if limit:
        await update.message.reply_text(f"✅ Flood limit set to <b>{limit}</b> messages.", parse_mode="HTML")
    else:
        await update.message.reply_text("✅ Flood control <b>disabled</b>.", parse_mode="HTML")


async def setfloodtimer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setfloodtimer &lt;seconds&gt;", parse_mode="HTML")
        return
    try:
        secs = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Provide a number of seconds.")
        return
    if secs < 1 or secs > 600:
        await update.message.reply_text("Value must be between 1 and 600.")
        return
    data = _load_flood()
    entry = data.setdefault(str(chat.id), {"limit": 0, "timer": 5, "mode": "mute"})
    entry["timer"] = secs
    _save_flood(data)
    await update.message.reply_text(f"✅ Flood time window set to <b>{secs}s</b>.", parse_mode="HTML")


async def floodmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args or context.args[0].lower() not in ("mute", "ban", "kick"):
        await update.message.reply_text("Usage: /floodmode &lt;mute|ban|kick&gt;", parse_mode="HTML")
        return
    mode = context.args[0].lower()
    data = _load_flood()
    entry = data.setdefault(str(chat.id), {"limit": 0, "timer": 5, "mode": "mute"})
    entry["mode"] = mode
    _save_flood(data)
    await update.message.reply_text(f"✅ Flood action set to <b>{mode}</b>.", parse_mode="HTML")


async def clearflood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return
    # Clear runtime counters for this chat
    keys_to_remove = [k for k in _flood_counter if k[0] == chat.id]
    for k in keys_to_remove:
        del _flood_counter[k]
    await update.message.reply_text("✅ Flood counters cleared.")


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGE HANDLER — blocklist check + flood check
# ══════════════════════════════════════════════════════════════════════════════

async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs on every text message in groups: checks blocklist and flood."""
    msg = update.message
    if not msg or not msg.text:
        return
    chat = msg.chat
    user = msg.from_user
    if not user or chat.type not in ("group", "supergroup"):
        return

    # Skip admins and approved users
    if await is_admin(context.bot, chat.id, user.id):
        return
    if _is_approved(chat.id, user.id):
        return

    # ── Blocklist check ──
    bl = _get_chat_blocklist(chat.id)
    words = bl.get("words", [])
    if words:
        text_lower = msg.text.lower()
        for w in words:
            if w in text_lower:
                # Delete the message
                if bl.get("delete_msg", True):
                    try:
                        await msg.delete()
                    except TelegramError:
                        pass

                mode = bl.get("mode", "delete")
                reason_bl = bl.get("reason", f"Used blocked word: {w}")

                if mode == "mute":
                    try:
                        await context.bot.restrict_chat_member(
                            chat.id, user.id,
                            permissions=ChatPermissions(can_send_messages=False),
                        )
                        await _log_mod_action(context.bot, chat.id, context.bot, "Auto-Mute (Blocklist)", user.id, reason_bl)
                        await _add_infraction(chat.id, user.id, "Auto-Mute (Blocklist)", reason_bl, context.bot.id)
                    except TelegramError:
                        pass
                elif mode == "ban":
                    try:
                        await context.bot.ban_chat_member(chat.id, user.id)
                        await _log_mod_action(context.bot, chat.id, context.bot, "Auto-Ban (Blocklist)", user.id, reason_bl)
                        await _add_infraction(chat.id, user.id, "Auto-Ban (Blocklist)", reason_bl, context.bot.id)
                    except TelegramError:
                        pass
                else:
                    # Just delete mode
                    await _log_mod_action(context.bot, chat.id, context.bot, "Auto-Delete (Blocklist)", user.id, reason_bl)
                    await _add_infraction(chat.id, user.id, "Auto-Delete (Blocklist)", reason_bl, context.bot.id)

                return  # stop checking other words once matched

    # ── Flood check ──
    fl = _get_flood(chat.id)
    limit = fl.get("limit", 0)
    if limit:
        now = time.time()
        timer = fl.get("timer", 5)
        key = (chat.id, user.id)
        stamps = _flood_counter.setdefault(key, [])
        stamps.append(now)
        # Prune old timestamps
        cutoff = now - timer
        _flood_counter[key] = [t for t in stamps if t > cutoff]

        if len(_flood_counter[key]) > limit:
            _flood_counter[key] = []  # reset
            mode = fl.get("mode", "mute")
            try:
                if mode == "mute":
                    await context.bot.restrict_chat_member(
                        chat.id, user.id,
                        permissions=ChatPermissions(can_send_messages=False),
                    )
                    await context.bot.send_message(
                        chat.id, f"🌊 {user.full_name} has been muted (flood)."
                    )
                elif mode == "ban":
                    await context.bot.ban_chat_member(chat.id, user.id)
                    await context.bot.send_message(
                        chat.id, f"🌊 {user.full_name} has been banned (flood)."
                    )
                elif mode == "kick":
                    await context.bot.ban_chat_member(chat.id, user.id)
                    await context.bot.unban_chat_member(chat.id, user.id)
                    await context.bot.send_message(
                        chat.id, f"🌊 {user.full_name} has been kicked (flood)."
                    )
            except TelegramError:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# BAN / MUTE / KICK / WARN COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

# ── helper: admin check boilerplate ──────────────────────────────────────────

async def _admin_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if caller is admin in current group, else reply and False."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this in a group.")
        return False
    if not await is_admin(context.bot, chat.id, update.effective_user.id):
        await update.message.reply_text("⛔ Admins only.")
        return False
    return True


# ── /ban ─────────────────────────────────────────────────────────────────────

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, reason = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /ban &lt;reply|user_id&gt; [reason]", parse_mode="HTML")
        return
    chat = update.effective_chat
    try:
        await context.bot.ban_chat_member(chat.id, target_id)
        text = f"🔨 User <code>{target_id}</code> banned."
        if reason:
            text += f"\nReason: {reason}"
        kb = [[InlineKeyboardButton("🔓 Unban", callback_data=f"unban_{target_id}", style="success")]]
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        
        await _log_mod_action(context.bot, chat.id, update.effective_user, "Ban", target_id, reason)
        await _add_infraction(chat.id, target_id, "Ban", reason or "No reason", update.effective_user.id)
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def dban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, reason = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /dban &lt;reply|user_id&gt; [reason]", parse_mode="HTML")
        return
    chat = update.effective_chat
    try:
        await context.bot.ban_chat_member(chat.id, target_id)
        # Delete the replied-to message
        if update.message.reply_to_message:
            try:
                await update.message.reply_to_message.delete()
            except TelegramError:
                pass
        text = f"🔨 User <code>{target_id}</code> banned &amp; messages deleted."
        if reason:
            text += f"\nReason: {reason}"
        await update.message.reply_text(text, parse_mode="HTML")
        
        await _log_mod_action(context.bot, chat.id, update.effective_user, "DBan (Ban+Delete)", target_id, reason)
        await _add_infraction(chat.id, target_id, "DBan", reason or "No reason", update.effective_user.id)
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def sban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        return
    chat = update.effective_chat
    try:
        await context.bot.ban_chat_member(chat.id, target_id)
        # Silent — delete the command message
        try:
            await update.message.delete()
        except TelegramError:
            pass
            
        await _log_mod_action(context.bot, chat.id, update.effective_user, "SBan (Silent Ban)", target_id, "Silent action")
        await _add_infraction(chat.id, target_id, "SBan", "Silent action", update.effective_user.id)
    except TelegramError:
        pass


async def tban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, extra = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text(
            "Usage: /tban &lt;reply|user_id&gt; &lt;duration&gt;\nE.g. /tban 30m, /tban 2h, /tban 1d",
            parse_mode="HTML",
        )
        return
    duration = parse_duration(extra) if extra else None
    if not duration:
        await update.message.reply_text("Provide a duration: e.g. 30m, 2h, 1d")
        return
    chat = update.effective_chat
    until = int(time.time()) + duration
    try:
        await context.bot.ban_chat_member(chat.id, target_id, until_date=until)
        await update.message.reply_text(
            f"🔨 User <code>{target_id}</code> banned for <b>{extra}</b>.",
            parse_mode="HTML",
        )
        
        await _log_mod_action(context.bot, chat.id, update.effective_user, "TBan (Temp Ban)", target_id, "Temp action", duration=extra)
        await _add_infraction(chat.id, target_id, "TBan", f"Temp action for {extra}", update.effective_user.id)
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /unban &lt;reply|user_id&gt;", parse_mode="HTML")
        return
    chat = update.effective_chat
    try:
        await context.bot.unban_chat_member(chat.id, target_id, only_if_banned=True)
        await update.message.reply_text(
            f"✅ User <code>{target_id}</code> unbanned.", parse_mode="HTML",
        )
        await _log_mod_action(context.bot, chat.id, update.effective_user, "Unban", target_id, "Manual unban")
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def unban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = query.message.chat
    if not await is_admin(context.bot, chat.id, query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    target_id = int(query.data.split("_")[1])
    try:
        await context.bot.unban_chat_member(chat.id, target_id, only_if_banned=True)
        await query.answer("✅ Unbanned!")
        await query.message.edit_text(
            query.message.text + f"\n\n✅ Unbanned by {query.from_user.full_name}",
            parse_mode="HTML",
        )
    except TelegramError as e:
        await query.answer(f"Error: {e}", show_alert=True)


# ── /mute ────────────────────────────────────────────────────────────────────

async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, reason = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /mute &lt;reply|user_id&gt; [reason]", parse_mode="HTML")
        return
    chat = update.effective_chat
    try:
        await context.bot.restrict_chat_member(
            chat.id, target_id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        text = f"🔇 User <code>{target_id}</code> muted."
        if reason:
            text += f"\nReason: {reason}"
        kb = [[InlineKeyboardButton("🔊 Unmute", callback_data=f"unmute_{target_id}", style="success")]]
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        
        await _log_mod_action(context.bot, chat.id, update.effective_user, "Mute", target_id, reason)
        await _add_infraction(chat.id, target_id, "Mute", reason or "No reason", update.effective_user.id)
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def dmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, reason = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /dmute &lt;reply|user_id&gt;", parse_mode="HTML")
        return
    chat = update.effective_chat
    try:
        await context.bot.restrict_chat_member(
            chat.id, target_id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        if update.message.reply_to_message:
            try:
                await update.message.reply_to_message.delete()
            except TelegramError:
                pass
        text = f"🔇 User <code>{target_id}</code> muted &amp; messages deleted."
        if reason:
            text += f"\nReason: {reason}"
        await update.message.reply_text(text, parse_mode="HTML")
        
        await _log_mod_action(context.bot, chat.id, update.effective_user, "DMute (Mute+Delete)", target_id, reason)
        await _add_infraction(chat.id, target_id, "DMute", reason or "No reason", update.effective_user.id)
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def smute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        return
    chat = update.effective_chat
    try:
        await context.bot.restrict_chat_member(
            chat.id, target_id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        try:
            await update.message.delete()
        except TelegramError:
            pass
    except TelegramError:
        pass


async def tmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, extra = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text(
            "Usage: /tmute &lt;reply|user_id&gt; &lt;duration&gt;\nE.g. /tmute 30m, /tmute 2h",
            parse_mode="HTML",
        )
        return
    duration = parse_duration(extra) if extra else None
    if not duration:
        await update.message.reply_text("Provide a duration: e.g. 30m, 2h, 1d")
        return
    chat = update.effective_chat
    until = int(time.time()) + duration
    try:
        await context.bot.restrict_chat_member(
            chat.id, target_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await update.message.reply_text(
            f"🔇 User <code>{target_id}</code> muted for <b>{extra}</b>.",
            parse_mode="HTML",
        )
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /unmute &lt;reply|user_id&gt;", parse_mode="HTML")
        return
    chat = update.effective_chat
    try:
        await context.bot.restrict_chat_member(
            chat.id, target_id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True,
                can_send_documents=True, can_send_photos=True,
                can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
                can_invite_users=True,
            ),
        )
        await update.message.reply_text(
            f"🔊 User <code>{target_id}</code> unmuted.", parse_mode="HTML",
        )
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def unmute_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat = query.message.chat
    if not await is_admin(context.bot, chat.id, query.from_user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    target_id = int(query.data.split("_")[1])
    try:
        await context.bot.restrict_chat_member(
            chat.id, target_id,
            permissions=ChatPermissions(
                can_send_messages=True, can_send_audios=True,
                can_send_documents=True, can_send_photos=True,
                can_send_videos=True, can_send_video_notes=True,
                can_send_voice_notes=True, can_send_polls=True,
                can_send_other_messages=True, can_add_web_page_previews=True,
                can_invite_users=True,
            ),
        )
        await query.answer("✅ Unmuted!")
        await query.message.edit_text(
            query.message.text + f"\n\n🔊 Unmuted by {query.from_user.full_name}",
            parse_mode="HTML",
        )
    except TelegramError as e:
        await query.answer(f"Error: {e}", show_alert=True)


# ── /kick ────────────────────────────────────────────────────────────────────

async def kick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, reason = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /kick &lt;reply|user_id&gt; [reason]", parse_mode="HTML")
        return
    chat = update.effective_chat
    try:
        await context.bot.ban_chat_member(chat.id, target_id)
        await context.bot.unban_chat_member(chat.id, target_id)
        text = f"👢 User <code>{target_id}</code> kicked."
        if reason:
            text += f"\nReason: {reason}"
        await update.message.reply_text(text, parse_mode="HTML")
        
        await _log_mod_action(context.bot, chat.id, update.effective_user, "Kick", target_id, reason)
        await _add_infraction(chat.id, target_id, "Kick", reason or "No reason", update.effective_user.id)
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def dkick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /dkick &lt;reply|user_id&gt;", parse_mode="HTML")
        return
    chat = update.effective_chat
    try:
        await context.bot.ban_chat_member(chat.id, target_id)
        await context.bot.unban_chat_member(chat.id, target_id)
        if update.message.reply_to_message:
            try:
                await update.message.reply_to_message.delete()
            except TelegramError:
                pass
        await update.message.reply_text(
            f"👢 User <code>{target_id}</code> kicked &amp; messages deleted.", parse_mode="HTML",
        )
        
        await _log_mod_action(context.bot, chat.id, update.effective_user, "DKick (Kick+Delete)", target_id, "Deleted msg trigger")
        await _add_infraction(chat.id, target_id, "DKick", "Deleted msg trigger", update.effective_user.id)
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


async def skick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        return
    chat = update.effective_chat
    try:
        await context.bot.ban_chat_member(chat.id, target_id)
        await context.bot.unban_chat_member(chat.id, target_id)
        try:
            await update.message.delete()
        except TelegramError:
            pass
            
        await _log_mod_action(context.bot, chat.id, update.effective_user, "SKick (Silent Kick)", target_id, "Silent action")
        await _add_infraction(chat.id, target_id, "SKick", "Silent action", update.effective_user.id)
    except TelegramError:
        pass


async def kickme_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this in a group.")
        return
    try:
        await context.bot.ban_chat_member(chat.id, user.id)
        await context.bot.unban_chat_member(chat.id, user.id)
        await update.message.reply_text(f"👋 {user.full_name} has left the group.")
    except TelegramError as e:
        await update.message.reply_text(f"⚠️ {e}")


# ── /warn, /infractions, /clearinfractions (Offender Scoring) ────────────────

async def warn_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adds an infraction and checks for auto-escalation."""
    if not await _admin_guard(update, context):
        return
    msg = update.message
    chat = update.effective_chat
    actor = update.effective_user
    
    target_id, reason = await _resolve_target(update, context)
    if not target_id:
        await msg.reply_text("Usage: /warn &lt;reply|user_id&gt; [reason]", parse_mode="HTML")
        return
        
    try:
        target_member = await chat.get_member(target_id)
        target_user = target_member.user
    except TelegramError:
        await msg.reply_text("Could not find that user in this chat.")
        return

    score, _ = await _add_infraction(
        chat.id, target_id, "warn", reason or "No reason", actor.id, points=1
    )
    
    # Check escalation thresholds
    if score >= _SCORE_BAN_THRESHOLD:
        try:
            await context.bot.ban_chat_member(chat.id, target_id)
            await msg.reply_text(f"🚨 User {target_user.mention_html()} reached <b>{score} infractions</b> and was <b>BANNED</b>.", parse_mode="HTML")
            await _log_mod_action(context.bot, chat.id, actor, "Auto-Ban (Threshold)", target_user, reason)
        except TelegramError as e:
            await msg.reply_text(f"⚠️ Reached ban threshold but failed to ban: {e}")
    elif score >= _SCORE_MUTE_THRESHOLD:
        try:
            until = datetime.now(timezone.utc) + timedelta(hours=1)
            await context.bot.restrict_chat_member(
                chat.id, target_id, permissions=ChatPermissions(can_send_messages=False), until_date=until
            )
            await msg.reply_text(f"🔇 User {target_user.mention_html()} reached <b>{score} infractions</b> and was <b>MUTED</b> for 1 hour.", parse_mode="HTML")
            await _log_mod_action(context.bot, chat.id, actor, "Auto-Mute 1h (Threshold)", target_user, reason, duration="1h")
        except TelegramError as e:
            await msg.reply_text(f"⚠️ Reached mute threshold but failed to mute: {e}")
    else:
        # Just a warning
        text = f"⚠️ User {target_user.mention_html()} has been warned (Score: <b>{score}</b>)."
        if reason:
            text += f"\nReason: {reason}"
        await msg.reply_text(text, parse_mode="HTML")
        await _log_mod_action(context.bot, chat.id, actor, "Warn", target_user, reason)


async def infractions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View a user's infraction history."""
    if not await _admin_guard(update, context):
        return
    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /infractions &lt;reply|user_id&gt;", parse_mode="HTML")
        return
    
    score, history = _get_infractions(update.effective_chat.id, target_id)
    if not history:
        await update.message.reply_text(f"User <code>{target_id}</code> has a clean record (Score: 0).", parse_mode="HTML")
        return
        
    lines = [f"📊 <b>Infractions for <code>{target_id}</code></b> (Score: <b>{score}</b>)\n"]
    for i, inf in enumerate(history[-10:], 1):  # show last 10
        dt_str = datetime.fromtimestamp(inf['time'], timezone.utc).strftime('%Y-%m-%d')
        lines.append(f"{i}. <b>{inf['action']}</b> ({dt_str}) by <code>{inf['actor']}</code>\n   Reason: {inf['reason']}")
        
    if len(history) > 10:
        lines.append(f"\n<i>...and {len(history)-10} older infractions.</i>")
        
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def clearinfractions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear a user's infractions."""
    if not await _admin_guard(update, context):
        return
    target_id, _ = await _resolve_target(update, context)
    if not target_id:
        await update.message.reply_text("Usage: /clearinfractions &lt;reply|user_id&gt;", parse_mode="HTML")
        return
    
    _clear_infractions(update.effective_chat.id, target_id)
    await update.message.reply_text(f"✅ Cleared all infractions for <code>{target_id}</code>.", parse_mode="HTML")

# ── /modlog and /setmodlog (Mod Logs) ────────────────────────────────────────

async def setmodlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the mod log channel for the current group."""
    if not await _admin_guard(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setmodlog &lt;channel_id|off&gt;", parse_mode="HTML")
        return
    
    val = context.args[0]
    chat_id = update.effective_chat.id
    data = _load_modlog()
    
    if val.lower() == "off":
        if str(chat_id) in data:
            del data[str(chat_id)]
            _save_modlog(data)
        await update.message.reply_text("✅ Mod logging disabled for this group.")
        return
        
    data[str(chat_id)] = val
    _save_modlog(data)
    await update.message.reply_text(f"✅ Mod log channel set to <code>{val}</code>.", parse_mode="HTML")

async def modlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    chat_id = update.effective_chat.id
    val = _load_modlog().get(str(chat_id))
    if val:
        await update.message.reply_text(f"📝 Mod log channel is currently: <code>{val}</code>", parse_mode="HTML")
    else:
        await update.message.reply_text("Mod logging is currently OFF. Use /setmodlog &lt;channel_id&gt;.", parse_mode="HTML")


# ── /setrules and /rules (Onboarding Rules) ──────────────────────────────────

async def setrules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _admin_guard(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setrules &lt;your rules text here (HTML supported)&gt;\nUse /setrules off to disable.", parse_mode="HTML")
        return
        
    text = " ".join(context.args)
    chat_id = update.effective_chat.id
    data = _load_rules()
    
    if text.lower() == "off":
        if str(chat_id) in data:
            del data[str(chat_id)]
            _save_rules(data)
        await update.message.reply_text("✅ Onboarding rules disabled.")
        return
        
    data[str(chat_id)] = text
    _save_rules(data)
    await update.message.reply_text("✅ Onboarding rules updated. New users will receive them via DM after captcha.")

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this in a group.")
        return
    rules = _load_rules().get(str(chat.id))
    if not rules:
        await update.message.reply_text("📋 This group hasn't set any onboarding rules yet.")
        return
    try:
        await context.bot.send_message(update.effective_user.id, f"📜 <b>Rules for {chat.title}</b>\n\n{rules}", parse_mode="HTML", disable_web_page_preview=True)
        await update.message.reply_text("📬 I've sent you the rules in a DM!")
    except TelegramError:
        await update.message.reply_text("⚠️ Could not send you a DM. Please message me first and try again.")


# ── /backup and /restore (Backup & Restore) ──────────────────────────────────

async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exports blocklist, fed bans, and approved list for the current group."""
    chat = update.effective_chat
    user = update.effective_user
    if not is_superadmin(user.id, user.username):
        await update.message.reply_text("⛔ Superadmins only.")
        return
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this in a group.")
        return
        
    # Gather data
    chat_id = str(chat.id)
    bl = _load_blocklist().get(chat_id, {})
    antraid = _load_antiraid().get(chat_id, {})
    flood = _load_flood().get(chat_id, {})
    approved = _load_approved().get(chat_id, [])
    
    fed_id, fed = _get_fed_for_chat(chat.id)
    fed_bans = fed.get("bans", {}) if fed else {}
    
    export_data = {
        "chat_id": chat.id,
        "chat_title": chat.title,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "blocklist": bl,
        "antiraid": antraid,
        "flood": flood,
        "approved": approved,
        "fed_bans": fed_bans
    }
    
    json_str = json.dumps(export_data, indent=2)
    with io.BytesIO(json_str.encode("utf-8")) as f:
        f.name = f"backup_{chat.id}.json"
        await update.message.reply_document(f, caption=f"💾 Backup for <b>{chat.title}</b>", parse_mode="HTML")

async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Imports backup JSON data."""
    chat = update.effective_chat
    user = update.effective_user
    if not is_superadmin(user.id, user.username):
        await update.message.reply_text("⛔ Superadmins only.")
        return
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("ℹ️ Use this in a group.")
        return
    
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.document:
        await msg.reply_text("⚠️ Please reply to a valid JSON backup document with /restore.")
        return
        
    doc = msg.reply_to_message.document
    if not doc.file_name.endswith(".json"):
        await msg.reply_text("⚠️ That doesn't look like a JSON file.")
        return
        
    try:
        file = await context.bot.get_file(doc.file_id)
        byte_arr = await file.download_as_bytearray()
        data = json.loads(byte_arr.decode("utf-8"))
    except Exception as e:
        await msg.reply_text(f"❌ Failed to parse backup file: {e}")
        return
        
    chat_id = str(chat.id)
    updates = []
    
    # Restore blocklist
    if "blocklist" in data:
        bl_data = _load_blocklist()
        bl_data[chat_id] = data["blocklist"]
        _save_blocklist(bl_data)
        updates.append("Blocklist")
        
    # Restore antiraid
    if "antiraid" in data:
        ar_data = _load_antiraid()
        ar_data[chat_id] = data["antiraid"]
        _save_antiraid(ar_data)
        updates.append("Anti-Raid")
        
    # Restore flood
    if "flood" in data:
        fl_data = _load_flood()
        fl_data[chat_id] = data["flood"]
        _save_flood(fl_data)
        updates.append("Flood")
        
    # Restore approved
    if "approved" in data:
        ap_data = _load_approved()
        ap_data[chat_id] = data["approved"]
        _save_approved(ap_data)
        updates.append("Approved List")
        
    # Restore Fed Bans if applicable
    if "fed_bans" in data and data["fed_bans"]:
        fed_id, fed = _get_fed_for_chat(chat.id)
        if fed:
            f_data = _load_feds()
            f_data["federations"][fed_id]["bans"].update(data["fed_bans"])
            _save_feds(f_data)
            updates.append("Fed Bans")
            
    await msg.reply_text(f"✅ <b>Restore complete.</b>\nRestored: {', '.join(updates)}", parse_mode="HTML")



# ── /admin-settings <chat_id> ─────────────────────────────────────────────────


def _build_settings_text_and_kb(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build the settings panel text and inline keyboard for a chat."""
    ar = _get_antiraid(chat_id)
    fl = _get_flood(chat_id)
    bl = _get_chat_blocklist(chat_id)

    antiraid_on = ar.get("enabled", False)
    auto_ar_on = ar.get("auto", False)
    flood_on = fl.get("limit", 0) > 0
    blocklist_on = len(bl.get("words", [])) > 0

    def icon(on: bool) -> str:
        return "✅" if on else "❌"

    text = (
        f"⚙️ <b>Settings for</b> <code>{chat_id}</code>\n"
        f"{'━' * 30}\n\n"
        f"{icon(antiraid_on)}  <b>Anti-Raid</b>\n"
        f"    Raid duration: <code>{ar.get('raid_time', 300)}s</code>\n\n"
        f"{icon(auto_ar_on)}  <b>Auto Anti-Raid</b>\n"
        f"    Trigger: <code>{ar.get('action_time', 10)} joins/min</code>\n\n"
        f"{icon(flood_on)}  <b>Flood Control</b>\n"
        f"    Limit: <code>{fl.get('limit', 0) or 'OFF'}</code> msgs / <code>{fl.get('timer', 5)}s</code>\n"
        f"    Action: <code>{fl.get('mode', 'mute')}</code>\n\n"
        f"{icon(blocklist_on)}  <b>Blocklist</b>\n"
        f"    Words: <code>{len(bl.get('words', []))}</code>\n"
        f"{'━' * 30}\n"
        f"<i>Tap a button to toggle ON/OFF</i>"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{icon(antiraid_on)} Anti-Raid",
                callback_data=f"aset_{chat_id}_antiraid",

                style="primary"
            ),
            InlineKeyboardButton(
                f"{icon(auto_ar_on)} Auto Anti-Raid",
                callback_data=f"aset_{chat_id}_autoar",

                style="primary"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{icon(flood_on)} Flood Control",
                callback_data=f"aset_{chat_id}_flood",

                style="primary"
            ),
            InlineKeyboardButton(
                f"{icon(blocklist_on)} Blocklist",
                callback_data=f"aset_{chat_id}_blocklist",

                style="primary"
            ),
        ],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"aset_{chat_id}_refresh", style="primary")],
    ])
    return text, kb


async def admin_settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings panel for a chat. Usage: /admin-settings <chat_id>"""
    user = update.effective_user

    # Must be admin in at least one configured group
    is_any_admin = False
    for gid in GROUP_IDS:
        if await is_admin(context.bot, gid, user.id):
            is_any_admin = True
            break
    if not is_any_admin:
        await update.message.reply_text("⛔ Admins only.")
        return

    raw_text = update.message.text or ""
    parts = raw_text.split()
    if len(parts) < 2:
        await update.message.reply_text(
            "<b>Usage:</b> /admin-settings &lt;chat_id&gt;",
            parse_mode="HTML",
        )
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("Chat ID must be a number.")
        return

    text, kb = _build_settings_text_and_kb(chat_id)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def admin_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings toggle buttons."""
    query = update.callback_query
    user = query.from_user

    # Must be admin
    is_any_admin = False
    for gid in GROUP_IDS:
        if await is_admin(context.bot, gid, user.id):
            is_any_admin = True
            break
    if not is_any_admin:
        await query.answer("⛔ Admins only.", show_alert=True)
        return

    # Parse: aset_<chat_id>_<action>
    data = query.data  # e.g. "aset_-100123_antiraid"
    parts = data.split("_", 2)
    if len(parts) < 3:
        await query.answer("Invalid.")
        return
    try:
        chat_id = int(parts[1])
    except ValueError:
        await query.answer("Invalid.")
        return
    action = parts[2]

    if action == "antiraid":
        ar_data = _load_antiraid()
        entry = ar_data.setdefault(str(chat_id), {
            "enabled": False, "raid_time": 300, "action_time": 10, "auto": False,
        })
        entry["enabled"] = not entry["enabled"]
        _save_antiraid(ar_data)
        if entry["enabled"]:
            _raid_active_until[chat_id] = time.time() + entry["raid_time"]
        else:
            _raid_active_until.pop(chat_id, None)
        await query.answer(f"Anti-Raid {'ON' if entry['enabled'] else 'OFF'}")

    elif action == "autoar":
        ar_data = _load_antiraid()
        entry = ar_data.setdefault(str(chat_id), {
            "enabled": False, "raid_time": 300, "action_time": 10, "auto": False,
        })
        entry["auto"] = not entry["auto"]
        _save_antiraid(ar_data)
        await query.answer(f"Auto Anti-Raid {'ON' if entry['auto'] else 'OFF'}")

    elif action == "flood":
        fl_data = _load_flood()
        entry = fl_data.setdefault(str(chat_id), {"limit": 0, "timer": 5, "mode": "mute"})
        if entry["limit"] > 0:
            entry["limit"] = 0  # turn off
        else:
            entry["limit"] = 5  # turn on with default limit of 5
        _save_flood(fl_data)
        await query.answer(f"Flood Control {'ON (limit 5)' if entry['limit'] else 'OFF'}")

    elif action == "blocklist":
        await query.answer(
            "Blocklist is ON when words are added.\n"
            "Use /addblocklist or /unblocklistall to manage.",
            show_alert=True,
        )
        return  # no toggle, just info

    elif action == "refresh":
        await query.answer("Refreshed!")

    else:
        await query.answer("Unknown action.")
        return

    # Update the settings panel
    text, kb = _build_settings_text_and_kb(chat_id)
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramError:
        pass


# ── /admin-custommessage conversation wizard ───────────────────────────────────

_CUSTOMMSG_TEXT   = 300
_CUSTOMMSG_TARGET = 301

# ── /admin-edit conversation states ──────────────────────────────────────────
_ADMINEDIT_URL     = 310   # waiting for the message URL
_ADMINEDIT_CONFIRM = 311   # waiting for Yes / No button press
_ADMINEDIT_NEWTEXT = 312   # waiting for new message body

_CUSTOMMSG_FORMAT_HELP = (
    "👋 Sure! What message would you like me to send?\n\n"
    "<b>Formatting options:</b>\n"
    "  <code>&lt;bold&gt;text&lt;bold&gt;</code> → <b>bold</b>\n"
    "  <code>&lt;italic&gt;text&lt;italic&gt;</code> → <i>italic</i>\n"
    "  <code>&lt;underlined&gt;text&lt;underlined&gt;</code> → <u>underlined</u>\n"
    "  <code>&lt;strike&gt;text&lt;strike&gt;</code> → strikethrough\n"
    "  <code>&lt;spoiler&gt;text&lt;spoiler&gt;</code> → spoiler\n"
    "  <code>&lt;monospace&gt;text&lt;monospace&gt;</code> → <code>monospace</code>\n"
    "  <code>&lt;url&gt;Label(https://...)&lt;url&gt;</code> → hyperlink\n"
    "  <code>&lt;button&gt;&lt;url&gt;Label(https://)&lt;url&gt;&lt;button&gt;</code> → inline button\n"
    "  <code>{countdown:60}</code> → live countdown timer\n"
    "  <code>{progressbar:60}</code> → visual progress bar\n\n"
    "Send /cancel to abort."
)


def _apply_custommsg_formatting(text: str, plain_text: str = "") -> tuple[str, list]:
    """Apply custom formatting tags and extract inline buttons.
    NOTE: `text` is the HTML version (from message.text_html) — existing Telegram
    formatting is already encoded as HTML tags and plain `<`/`>` are HTML-escaped.
    `plain_text` is the raw message.text and is used to reliably extract
    <button><url>…</button> tags that the admin typed as literal text, since PTB
    HTML-escapes those angle brackets before we ever see them in `text`.
    """
    def replace_tag(t, tag, open_html, close_html):
        return re.sub(
            rf'<{tag}>(.*?)</?{tag}>',
            lambda m: f'{open_html}{m.group(1)}{close_html}',
            t, flags=re.DOTALL | re.IGNORECASE,
        )

    # ── Extract inline buttons ──────────────────────────────────────────────
    # Pattern that matches both <button><url>Label(URL)</url></button> forms:
    #   - closing tags optional, slash optional (users often omit it)
    _BTN_PLAIN = re.compile(
        r'<button>\s*<url>([^(<\n]+?)\(([^)\n]+)\)\s*(?:</?url>)?\s*(?:</?button>)?',
        re.IGNORECASE,
    )
    _BTN_ESCAPED = re.compile(
        r'&lt;button&gt;\s*&lt;url&gt;([^(<\n]+?)\(([^)\n]+)\)\s*(?:&lt;/?url&gt;)?\s*(?:&lt;/?button&gt;)?',
        re.IGNORECASE,
    )

    inline_buttons = []

    # Step 1: extract from plain text first (most reliable — no HTML-escaping)
    if plain_text:
        for m in _BTN_PLAIN.finditer(plain_text):
            label, url = m.group(1).strip(), m.group(2).strip()
            if url:  # only add if we got a real URL
                inline_buttons.append(InlineKeyboardButton(label, url=url, style="success"))
        # Strip button syntax from the HTML body.
        # Use a permissive match (.*?) so that HTML tags such as <a href="...">
        # embedded inside the button block by PTB's text_html don't break the match.
        text = re.sub(
            r'(?:&lt;button&gt;|<button>).*?(?:&lt;/?button&gt;|</?button>)',
            '', text, flags=re.IGNORECASE | re.DOTALL,
        )
    else:
        # Fallback: parse directly from the HTML body
        def _extract_button(m):
            inline_buttons.append(InlineKeyboardButton(m.group(1).strip(), url=m.group(2).strip(), style="success"))
            return ''
        text = _BTN_ESCAPED.sub(_extract_button, text)
        text = _BTN_PLAIN.sub(_extract_button, text)

    # <url>Label(URL)</url> or <url>Label(URL)<url>  (HTML-escaped and plain)
    text = re.sub(
        r'&lt;url&gt;([^(<\n]+?)\(([^)\n]+)\)\s*(?:&lt;/?url&gt;)?',
        lambda m: f'<a href="{m.group(2).strip()}">{m.group(1).strip()}</a>',
        text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r'<url>([^(<\n]+?)\(([^)\n]+)\)\s*(?:</?url>)?',
        lambda m: f'<a href="{m.group(2).strip()}">{m.group(1).strip()}</a>',
        text, flags=re.IGNORECASE,
    )

    # Custom formatting tags (both plain and HTML-escaped variants handled)
    text = replace_tag(text, 'bold',       '<b>',          '</b>')
    text = replace_tag(text, 'italic',     '<i>',          '</i>')
    text = replace_tag(text, 'underlined', '<u>',          '</u>')
    text = replace_tag(text, 'strike',     '<s>',          '</s>')
    text = replace_tag(text, 'spoiler',    '<tg-spoiler>', '</tg-spoiler>')
    text = replace_tag(text, 'monospace',  '<code>',       '</code>')
    text = replace_tag(text, 'quote',      '<blockquote>', '</blockquote>')

    return text.strip(), inline_buttons


def _parse_custommsg_target(url: str) -> tuple:
    """Parse a Telegram link into (chat_id, topic_id).

    https://t.me/username           → ("@username", None)
    https://t.me/c/1234567890/5     → (-1001234567890, 5)
    """
    url = url.strip()
    m = re.match(r'https?://t\.me/c/(\d+)(?:/(\d+))?', url)
    if m:
        chat_id = int(f"-100{m.group(1)}")
        topic_id = int(m.group(2)) if m.group(2) and m.group(2) != '1' else None
        return chat_id, topic_id
    m = re.match(r'https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,})', url)
    if m:
        return f"@{m.group(1)}", None
    return None, None


async def custommessage_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: /admin-custommessage"""
    user = update.effective_user
    if user is None:
        return ConversationHandler.END
    uname = (user.username or "").lower()
    # Fast-path: superadmin username check (avoids API calls)
    if uname in SUPERADMIN_USERNAMES or user.id in _superadmin_ids:
        _superadmin_ids.add(user.id)
    else:
        is_any_admin = False
        for gid in GROUP_IDS:
            if await is_admin(context.bot, gid, user.id, uname or None):
                is_any_admin = True
                break
        if not is_any_admin:
            await update.message.reply_text("⛔ Admins only.")
            return ConversationHandler.END

    await update.message.reply_text(_CUSTOMMSG_FORMAT_HELP, parse_mode="HTML")
    return _CUSTOMMSG_TEXT


async def custommessage_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Got message body — ask for destination.
    We use text_html so all pre-existing Telegram formatting (bold, italic,
    hyperlinks, spoilers, etc.) is preserved exactly as typed.
    We also save the plain text so that <button><url>…</button> tags typed
    literally by the admin are extracted before PTB's HTML-escaping obscures them.
    """
    # text_html preserves Telegram entities as HTML; text is the raw plain version
    body_html = update.message.text_html
    body_plain = update.message.text or ""
    context.user_data["custommsg_body"] = body_html
    context.user_data["custommsg_body_plain"] = body_plain
    # Preview of formatted message so admin can confirm it looks right
    await update.message.reply_text(
        "✅ Got it! Where would you like me to post this message?\n\n"
        "<b>Send a link like:</b>\n"
        "  • <code>https://t.me/gordochannel</code>\n"
        "  • <code>https://t.me/c/3786381449/1</code>\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return _CUSTOMMSG_TARGET


async def custommessage_got_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Got destination URL — format and send the message."""
    url = update.message.text.strip()
    chat_id, topic_id = _parse_custommsg_target(url)
    if chat_id is None:
        await update.message.reply_text(
            "⚠️ Couldn't parse that link. Send a valid Telegram URL, or /cancel."
        )
        return _CUSTOMMSG_TARGET

    raw_body = context.user_data.pop("custommsg_body", "")
    raw_plain = context.user_data.pop("custommsg_body_plain", "")
    body, inline_buttons = _apply_custommsg_formatting(raw_body, raw_plain)
    kb = InlineKeyboardMarkup([inline_buttons]) if inline_buttons else None

    cd_match = re.search(r'\{countdown:(\d+)\}', body)
    pb_match = re.search(r'\{progressbar:(\d+)\}', body)
    total_secs = int(cd_match.group(1)) if cd_match else (int(pb_match.group(1)) if pb_match else None)

    if total_secs:
        secs = total_secs
        display = body
        if cd_match:
            display = display.replace(cd_match.group(0), f"⏱ {secs}s")
        if pb_match:
            bar, pct = build_progress_bar(secs, total_secs)
            display = display.replace(pb_match.group(0), f"{bar} {pct}%")
        kwargs = {
            "chat_id": chat_id,
            "text": display,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        if kb:
            kwargs["reply_markup"] = kb
        try:
            sent = await context.bot.send_message(**kwargs)
        except TelegramError as e:
            await update.message.reply_text(f"⚠️ {e}")
            return ConversationHandler.END
        context.job_queue.run_repeating(
            _custom_countdown_tick, interval=1, first=1,
            data={
                "chat_id": chat_id, "msg_id": sent.message_id,
                "body": body,
                "cd_pattern": cd_match.group(0) if cd_match else None,
                "pb_pattern": pb_match.group(0) if pb_match else None,
                "total": total_secs, "secs": total_secs, "kb": kb,
            },
        )
        await update.message.reply_text("✅ Message sent with live countdown!")
    else:
        kwargs = {
            "chat_id": chat_id,
            "text": body,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        if kb:
            kwargs["reply_markup"] = kb
        try:
            await context.bot.send_message(**kwargs)
            await update.message.reply_text("✅ Message sent!")
        except TelegramError as e:
            await update.message.reply_text(f"⚠️ Telegram error: {e}\n\nDouble-check the formatting — raw HTML in your message may have conflicting tags.")
    return ConversationHandler.END


async def custommessage_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("custommsg_body", None)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# /admin-edit — Edit an existing bot message in-place
# ══════════════════════════════════════════════════════════════════════════════

def _parse_edit_target(url: str) -> tuple:
    """Parse a Telegram message URL into (chat_id, message_id).

    Supported formats:
      https://t.me/c/3786381449/1/413   → (-1003786381449, 413)
      https://t.me/c/3786381449/413     → (-1003786381449, 413)
    Returns (None, None) on failure.
    """
    url = url.strip()
    # Format with 3 numeric segments: /c/<chat>/<topic>/<msg_id>
    m = re.match(r'https?://t\.me/c/(\d+)/(\d+)/(\d+)', url)
    if m:
        chat_id = int(f"-100{m.group(1)}")
        msg_id = int(m.group(3))
        return chat_id, msg_id
    # Format with 2 numeric segments: /c/<chat>/<msg_id>
    m = re.match(r'https?://t\.me/c/(\d+)/(\d+)', url)
    if m:
        chat_id = int(f"-100{m.group(1)}")
        msg_id = int(m.group(2))
        return chat_id, msg_id
    return None, None


async def adminedit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry: /admin-edit [optional URL]"""
    user = update.effective_user
    if user is None:
        return ConversationHandler.END
    uname = (user.username or "").lower()
    # Superadmin-only guard
    if uname in SUPERADMIN_USERNAMES or user.id in _superadmin_ids:
        _superadmin_ids.add(user.id)
    else:
        await update.message.reply_text("⛔ This command is for superadmins only.")
        return ConversationHandler.END

    # If URL was provided inline (e.g. "/admin-edit https://t.me/c/…"), handle immediately
    raw = update.message.text or ""
    parts = raw.split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("http"):
        return await _adminedit_process_url(update, context, parts[1])

    await update.message.reply_text(
        "✏️ <b>Edit a bot message</b>\n\n"
        "Send me the link to the message you want to edit, e.g.:\n"
        "  • <code>https://t.me/c/3786381449/1/413</code>\n"
        "  • <code>https://t.me/c/3786381449/413</code>\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return _ADMINEDIT_URL


async def adminedit_got_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received the message URL — validate and ask for confirmation."""
    url = (update.message.text or "").strip()
    return await _adminedit_process_url(update, context, url)


async def _adminedit_process_url(update, context, url: str):
    """Shared logic after we have a URL string."""
    chat_id, msg_id = _parse_edit_target(url)
    if chat_id is None:
        await update.message.reply_text(
            "⚠️ Couldn't parse that link.\n"
            "Expected format: <code>https://t.me/c/3786381449/1/413</code>\n"
            "Try again or /cancel.",
            parse_mode="HTML",
        )
        return _ADMINEDIT_URL

    # Store target for later steps
    context.user_data["adminedit_chat_id"] = chat_id
    context.user_data["adminedit_msg_id"] = msg_id

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, edit this message", callback_data="aedit_yes", style="success"),
        InlineKeyboardButton("❌ No, cancel",            callback_data="aedit_no", style="danger"),
    ]])
    await update.message.reply_text(
        "🔎 <b>Is this the message you want me to edit?</b>\n\n"
        f"📢 <b>Chat ID:</b> <code>{chat_id}</code>\n"
        f"📨 <b>Message ID:</b> <code>{msg_id}</code>\n\n"
        "<i>Tap Yes to proceed, or No to cancel.</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return _ADMINEDIT_CONFIRM


async def adminedit_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Yes / No confirmation buttons."""
    query = update.callback_query
    await query.answer()

    if query.data == "aedit_no":
        context.user_data.pop("adminedit_chat_id", None)
        context.user_data.pop("adminedit_msg_id", None)
        await query.edit_message_text("❌ Edit cancelled.")
        return ConversationHandler.END

    # aedit_yes
    await query.edit_message_text(
        "✅ Got it! Now send me the <b>new text</b> for that message.\n\n"
        "<b>Formatting options:</b>\n"
        "  <code>&lt;bold&gt;text&lt;bold&gt;</code> → <b>bold</b>\n"
        "  <code>&lt;italic&gt;text&lt;italic&gt;</code> → <i>italic</i>\n"
        "  <code>&lt;underlined&gt;text&lt;underlined&gt;</code> → <u>underlined</u>\n"
        "  <code>&lt;strike&gt;text&lt;strike&gt;</code> → strikethrough\n"
        "  <code>&lt;spoiler&gt;text&lt;spoiler&gt;</code> → spoiler\n"
        "  <code>&lt;monospace&gt;text&lt;monospace&gt;</code> → <code>monospace</code>\n"
        "  <code>&lt;url&gt;Label(https://...)&lt;url&gt;</code> → hyperlink\n"
        "  <code>&lt;button&gt;&lt;url&gt;Label(https://)&lt;url&gt;&lt;button&gt;</code> → inline button\n"
        "  <code>{countdown:60}</code> → live countdown timer\n"
        "  <code>{progressbar:60}</code> → visual progress bar\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return _ADMINEDIT_NEWTEXT


async def adminedit_got_newtext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received new message body — format and edit the original message."""
    chat_id = context.user_data.pop("adminedit_chat_id", None)
    msg_id  = context.user_data.pop("adminedit_msg_id", None)

    if not chat_id or not msg_id:
        await update.message.reply_text("⚠️ Session expired. Please start over with /admin_edit.")
        return ConversationHandler.END

    body_html  = update.message.text_html
    body_plain = update.message.text or ""
    body, inline_buttons = _apply_custommsg_formatting(body_html, body_plain)
    kb = InlineKeyboardMarkup([inline_buttons]) if inline_buttons else None

    # Handle countdown / progressbar in edited message
    cd_match = re.search(r'\{countdown:(\d+)\}', body)
    pb_match = re.search(r'\{progressbar:(\d+)\}', body)
    total_secs = int(cd_match.group(1)) if cd_match else (int(pb_match.group(1)) if pb_match else None)

    if total_secs:
        secs = total_secs
        display = body
        if cd_match:
            display = display.replace(cd_match.group(0), f"⏱ {secs}s")
        if pb_match:
            bar, pct = build_progress_bar(secs, total_secs)
            display = display.replace(pb_match.group(0), f"{bar} {pct}%")
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=display,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb,
            )
        except TelegramError as e:
            await update.message.reply_text(f"⚠️ Telegram error: {e}")
            return ConversationHandler.END
        context.job_queue.run_repeating(
            _custom_countdown_tick, interval=1, first=1,
            data={
                "chat_id": chat_id, "msg_id": msg_id,
                "body": body,
                "cd_pattern": cd_match.group(0) if cd_match else None,
                "pb_pattern": pb_match.group(0) if pb_match else None,
                "total": total_secs, "secs": total_secs, "kb": kb,
            },
        )
        await update.message.reply_text("✅ Message edited with live countdown!")
    else:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=body,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb,
            )
            await update.message.reply_text("✅ Message edited successfully!")
        except TelegramError as e:
            await update.message.reply_text(
                f"⚠️ Telegram error: {e}\n\n"
                "Make sure the bot is the author of that message, and that the "
                "formatting is valid."
            )
    return ConversationHandler.END


async def adminedit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("adminedit_chat_id", None)
    context.user_data.pop("adminedit_msg_id", None)
    await update.message.reply_text("❌ Edit cancelled.")
    return ConversationHandler.END



async def _custom_countdown_tick(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    d["secs"] -= 1
    secs = d["secs"]

    if secs <= 0:
        context.job.schedule_removal()
        display = d["body"]
        if d["cd_pattern"]:
            display = display.replace(d["cd_pattern"], "⏱ Expired")
        if d["pb_pattern"]:
            display = display.replace(d["pb_pattern"], "░" * 20 + " 0%")
        try:
            await context.bot.edit_message_text(
                chat_id=d["chat_id"], message_id=d["msg_id"],
                text=display, parse_mode="HTML", reply_markup=d.get("kb"),
            )
        except TelegramError:
            pass
        return

    display = d["body"]
    if d["cd_pattern"]:
        display = display.replace(d["cd_pattern"], f"⏱ {secs}s")
    if d["pb_pattern"]:
        bar, pct = build_progress_bar(secs, d["total"])
        display = display.replace(d["pb_pattern"], f"{bar} {pct}%")

    try:
        await context.bot.edit_message_text(
            chat_id=d["chat_id"], message_id=d["msg_id"],
            text=display, parse_mode="HTML", reply_markup=d.get("kb"),
        )
    except TelegramError:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# BROADCASTER — /bot command, auth flow, full control panel
# ══════════════════════════════════════════════════════════════════════════════

# ── Conversation states ──────────────────────────────────────────────────────
_BCAST_SETUP_API_ID   = 900   # setup wizard: collecting API ID
_BCAST_SETUP_API_HASH = 901   # setup wizard: collecting API HASH
_BCAST_SETUP_PHONE    = 902   # setup wizard: collecting phone number
_BCAST_AUTH_CODE      = 903   # auth: waiting for Telegram login code
_BCAST_AUTH_2FA       = 904   # auth: waiting for 2FA password
_BCAST_CFG_VALUE      = 905   # config edit: waiting for new value
_BCAST_ADD_MSG        = 906   # add template: waiting for message text

# Module-level broadcaster instance (shared across all handlers)
_broadcaster: Broadcaster = Broadcaster()


def _is_broadcaster_admin(user) -> bool:
    """Only @gordo, @gorda, and @lavado can use /bot."""
    if user is None:
        return False
    if (user.username or "").lower() in BROADCASTER_USERNAMES:
        return True
    if user.id in _superadmin_ids:
        return True
    return False


# ── Keyboard builders ────────────────────────────────────────────────────────

def _bcast_main_kb() -> InlineKeyboardMarkup:
    """Main control panel keyboard."""
    running = _broadcaster.is_running
    paused  = _broadcaster.is_paused
    rows = []

    if running and not paused:
        rows.append([InlineKeyboardButton("⏸ Pause", callback_data="bcast_pause"),
                     InlineKeyboardButton("⏹ Stop", callback_data="bcast_stop")])
    elif running and paused:
        rows.append([InlineKeyboardButton("▶️ Resume", callback_data="bcast_resume"),
                     InlineKeyboardButton("⏹ Stop", callback_data="bcast_stop")])
    else:
        rows.append([InlineKeyboardButton("🚀 Start Broadcast", callback_data="bcast_start")])

    rows.append([InlineKeyboardButton("📊 Status", callback_data="bcast_status"),
                 InlineKeyboardButton("📝 Messages", callback_data="bcast_msgs")])
    rows.append([InlineKeyboardButton("⚙️ Settings", callback_data="bcast_settings"),
                 InlineKeyboardButton("🔄 Reload", callback_data="bcast_reload")])
    return InlineKeyboardMarkup(rows)


def _bcast_settings_kb() -> InlineKeyboardMarkup:
    """Settings panel keyboard — shows current values on buttons."""
    cfg = _broadcaster.config
    arch_icon = "✅" if cfg.include_archived else "❌"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏱ Delay: {cfg.delay_minutes}min", callback_data="bcast_cfg_delay"),
         InlineKeyboardButton(f"📊 Cap: {cfg.max_per_hour}/hr", callback_data="bcast_cfg_cap")],
        [InlineKeyboardButton(f"🎲 Variance: ±{int(cfg.delay_variance * 100)}%", callback_data="bcast_cfg_variance"),
         InlineKeyboardButton(f"🛡 Warmup: {cfg.warmup_count}", callback_data="bcast_cfg_warmup")],
        [InlineKeyboardButton(f"{arch_icon} Archived groups", callback_data="bcast_cfg_toggle_archived")],
        [InlineKeyboardButton("◀️ Main Panel", callback_data="bcast_main")],
    ])


def _bcast_messages_kb() -> InlineKeyboardMarkup:
    """Messages management keyboard — one row per template."""
    rows = []
    for i, msg in enumerate(_broadcaster.config.messages):
        enabled = _broadcaster.is_template_enabled(i)
        state_icon = "✅" if enabled else "⏸"
        toggle_action = f"bcast_msg_pause_{i}" if enabled else f"bcast_msg_resume_{i}"
        toggle_label  = "⏸ Pause" if enabled else "▶️ Resume"
        # Preview: first 20 chars of the message text
        preview = (msg.get("text") or "")[:20].replace("\n", " ")
        rows.append([InlineKeyboardButton(f"{state_icon} #{i+1}: {preview}…",
                                          callback_data=f"bcast_msg_view_{i}")])
        rows.append([InlineKeyboardButton(toggle_label, callback_data=toggle_action),
                     InlineKeyboardButton("🗑 Remove", callback_data=f"bcast_msg_del_{i}")])
    rows.append([InlineKeyboardButton("➕ Add Message", callback_data="bcast_add_msg")])
    rows.append([InlineKeyboardButton("◀️ Main Panel", callback_data="bcast_main")])
    return InlineKeyboardMarkup(rows)


def _bcast_cancel_kb() -> InlineKeyboardMarkup:
    """Single-button cancel keyboard for mid-conversation prompts."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data="bcast_cfg_cancel")
    ]])


# ── Auth flow ────────────────────────────────────────────────────────────────

async def bot_broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bot — broadcaster control panel (DM only, authorised users only).

    Flow:
      1. Check user is @gordo / @gorda / @lavado
      2. Must be in a private DM
      3. If no credentials stored → start interactive setup wizard
         (asks for API ID → API HASH → Phone → login code → 2FA if needed)
      4. If credentials exist but not authenticated → send code, ask for it
      5. If already authenticated → show control panel
    """
    user = update.effective_user
    chat = update.effective_chat

    if not _is_broadcaster_admin(user):
        await update.message.reply_text("⛔ This command is restricted.")
        return ConversationHandler.END

    if chat.type != "private":
        await update.message.reply_text("🔒 Use this command in a private DM with me.")
        return ConversationHandler.END

    # ── Step 1: Check if credentials are stored ──
    # If credentials file doesn't exist, start the interactive setup wizard.
    # The wizard collects API ID, API HASH, and phone number, then
    # triggers authentication automatically.
    from broadcaster import SessionManager as SM
    if not SM.has_credentials():
        await update.message.reply_text(
            "🚀 <b>Broadcaster Setup Wizard</b>\n\n"
            "I need a few details to connect your Telegram account.\n"
            "This is a one-time setup — credentials are saved locally.\n\n"
            "<b>Step 1/3 — API ID</b>\n"
            "Go to <a href='https://my.telegram.org'>my.telegram.org</a>, "
            "log in, then copy your <b>App api_id</b>.\n\n"
            "Send it here (numbers only, e.g. <code>39767938</code>)\n\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return _BCAST_SETUP_API_ID

    # ── Step 2: Credentials exist — try to connect ──
    try:
        if not _broadcaster.client:
            _broadcaster._create_client()
        if not _broadcaster.client.is_connected():
            await _broadcaster.connect()

        if await _broadcaster.is_authenticated():
            me = await _broadcaster.client.get_me()
            await update.message.reply_text(
                f"📡 <b>𝔾𝕠𝕣𝕕𝕠's Broadcaster</b>\n\n"
                f"✅ Logged in as <b>{me.first_name}</b>"
                f"{' @' + me.username if me.username else ''}\n"
                f"Use the panel below to control broadcasts.",
                parse_mode="HTML",
                reply_markup=_bcast_main_kb(),
            )
            return ConversationHandler.END

        # Session expired or never authenticated — send code
        creds = SM.load()
        phone = creds.get("phone", "")
        phone_code_hash = await _broadcaster.send_code(phone)
        context.user_data["bcast_phone_code_hash"] = phone_code_hash
        await update.message.reply_text(
            "📲 <b>Authentication Required</b>\n\n"
            f"A login code was sent to <code>{phone}</code>.\n\n"
            "📩 <b>Type the code here</b> (e.g. <code>12345</code>)\n\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
        )
        return _BCAST_AUTH_CODE

    except Exception as e:
        logger.error("Broadcaster auth error: %s", e)
        await update.message.reply_text(
            f"❌ Connection error: <code>{e}</code>", parse_mode="HTML"
        )
        return ConversationHandler.END


# ── Setup wizard handlers ─────────────────────────────────────────────────────

async def bcast_got_api_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Setup step 1 — received API ID."""
    raw = (update.message.text or "").strip()
    # Try to delete the message (API ID is sensitive)
    try:
        await update.message.delete()
    except (BadRequest, TelegramError):
        pass

    try:
        api_id = int(raw)
        if api_id <= 0:
            raise ValueError
    except ValueError:
        await context.bot.send_message(
            update.effective_chat.id,
            "⚠️ API ID must be a positive number (e.g. <code>39767938</code>).\n"
            "Try again, or /cancel.",
            parse_mode="HTML",
        )
        return _BCAST_SETUP_API_ID

    context.user_data["bcast_setup_api_id"] = api_id
    await context.bot.send_message(
        update.effective_chat.id,
        "✅ API ID saved.\n\n"
        "<b>Step 2/3 — API Hash</b>\n"
        "Back on <a href='https://my.telegram.org'>my.telegram.org</a>, "
        "copy your <b>App api_hash</b>.\n\n"
        "Send it here (e.g. <code>21ae63ca03f1cab8c8c70c716b258737</code>)\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return _BCAST_SETUP_API_HASH


async def bcast_got_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Setup step 2 — received API Hash."""
    api_hash = (update.message.text or "").strip()
    # Delete the message — API hash is sensitive
    try:
        await update.message.delete()
    except (BadRequest, TelegramError):
        pass

    if len(api_hash) < 10:
        await context.bot.send_message(
            update.effective_chat.id,
            "⚠️ That doesn't look like a valid API Hash. It should be a long hex string.\n"
            "Try again, or /cancel.",
            parse_mode="HTML",
        )
        return _BCAST_SETUP_API_HASH

    context.user_data["bcast_setup_api_hash"] = api_hash
    await context.bot.send_message(
        update.effective_chat.id,
        "✅ API Hash saved.\n\n"
        "<b>Step 3/3 — Phone Number</b>\n"
        "Enter the phone number of the Telegram account\n"
        "that will send the broadcasts.\n\n"
        "Include the country code (e.g. <code>+529811815398</code>)\n\n"
        "Send /cancel to abort.",
        parse_mode="HTML",
    )
    return _BCAST_SETUP_PHONE


async def bcast_got_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Setup step 3 — received phone number. Creates client and sends login code."""
    phone = (update.message.text or "").strip()
    # Try to delete
    try:
        await update.message.delete()
    except (BadRequest, TelegramError):
        pass

    if not phone.startswith("+") or len(phone) < 8:
        await context.bot.send_message(
            update.effective_chat.id,
            "⚠️ Include the country code with <code>+</code> "
            "(e.g. <code>+529811815398</code>).\n"
            "Try again, or /cancel.",
            parse_mode="HTML",
        )
        return _BCAST_SETUP_PHONE

    api_id   = context.user_data.get("bcast_setup_api_id")
    api_hash = context.user_data.get("bcast_setup_api_hash")

    # Save credentials now (before auth, so they're available if bot restarts)
    from broadcaster import SessionManager as SM
    SM.save(api_id, api_hash, phone, session="")

    status_msg = await context.bot.send_message(
        update.effective_chat.id,
        f"✅ Phone saved. Sending login code to <code>{phone}</code>...",
        parse_mode="HTML",
    )

    try:
        # Create and connect the Telethon client with the new credentials
        _broadcaster._create_client(api_id=api_id, api_hash=api_hash)
        await _broadcaster.connect()
        phone_code_hash = await _broadcaster.send_code(phone)
        context.user_data["bcast_phone_code_hash"] = phone_code_hash
        # Clean up setup data
        context.user_data.pop("bcast_setup_api_id", None)
        context.user_data.pop("bcast_setup_api_hash", None)

        try:
            await status_msg.delete()
        except (BadRequest, TelegramError):
            pass

        await context.bot.send_message(
            update.effective_chat.id,
            "📲 <b>Login code sent!</b>\n\n"
            f"A code was sent to <code>{phone}</code> via Telegram.\n\n"
            "📩 <b>Type the code here</b> (e.g. <code>12345</code>)\n\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
        )
        return _BCAST_AUTH_CODE

    except Exception as e:
        logger.error("Broadcaster setup error: %s", e)
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Failed to connect: <code>{e}</code>\n\n"
            "Check your API ID and HASH, then try /bot again.",
            parse_mode="HTML",
        )
        return ConversationHandler.END



async def bcast_got_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received login code — attempt sign-in."""
    code = (update.message.text or "").strip()
    phone_code_hash = context.user_data.get("bcast_phone_code_hash", "")
    # Delete the code message immediately for security
    try:
        await update.message.delete()
    except (BadRequest, TelegramError):
        pass
    if not code:
        await context.bot.send_message(
            update.effective_chat.id, "Please enter the code, or /cancel."
        )
        return _BCAST_AUTH_CODE
    try:
        result = await _broadcaster.sign_in_code(code, phone_code_hash)
        if result == "2fa":
            # Try auto-auth with stored 2FA password first
            from broadcaster import SessionManager as SM
            stored_pw = SM.load_2fa_password()
            if stored_pw:
                try:
                    await _broadcaster.sign_in_2fa(stored_pw)
                    await context.bot.send_message(
                        update.effective_chat.id,
                        "✅ <b>Authenticated!</b> (2FA auto-completed)\n\n"
                        "🎛 <b>Broadcaster Control Panel</b>",
                        parse_mode="HTML",
                        reply_markup=_bcast_main_kb(),
                    )
                    context.user_data.pop("bcast_phone_code_hash", None)
                    return ConversationHandler.END
                except Exception as e2fa:
                    logger.warning("Stored 2FA password failed: %s", e2fa)
                    # Fall through to manual prompt

            await context.bot.send_message(
                update.effective_chat.id,
                "🔐 <b>Two-Factor Authentication</b>\n\n"
                "This account has 2FA enabled.\n"
                "<b>Type your 2FA password:</b>\n\n"
                "<i>Your message will be deleted immediately for security.</i>\n\n"
                "Send /cancel to abort.",
                parse_mode="HTML",
            )
            return _BCAST_AUTH_2FA

        # Authenticated — session auto-saved to credentials file
        await context.bot.send_message(
            update.effective_chat.id,
            "✅ <b>Authenticated!</b> All credentials saved locally.\n\n"
            "🎛 <b>Broadcaster Control Panel</b>",
            parse_mode="HTML",
            reply_markup=_bcast_main_kb(),
        )
        context.user_data.pop("bcast_phone_code_hash", None)
        return ConversationHandler.END

    except Exception as e:
        logger.error("Broadcaster sign-in error: %s", e)
        await context.bot.send_message(
            update.effective_chat.id,
            f"❌ Sign-in failed: <code>{e}</code>\n\nCheck the code and try again, or /cancel.",
            parse_mode="HTML",
        )
        return _BCAST_AUTH_CODE


async def bcast_got_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received 2FA password — complete sign-in."""
    password = (update.message.text or "").strip()
    if not password:
        await update.message.reply_text("Please enter your 2FA password, or /cancel.")
        return _BCAST_AUTH_2FA
    try:
        await _broadcaster.sign_in_2fa(password)
        # Delete password message immediately for security
        try:
            await update.message.delete()
        except (BadRequest, TelegramError):
            pass
        await context.bot.send_message(
            update.effective_chat.id,
            "✅ <b>2FA Authentication Successful!</b>\n"
            "<i>(Your password message was deleted for security.)</i>\n\n"
            "All credentials are saved locally. You won't need to\n"
            "do this again unless the bot is redeployed.\n\n"
            "🎛 <b>Broadcaster Control Panel</b>",
            parse_mode="HTML",
            reply_markup=_bcast_main_kb(),
        )
        context.user_data.pop("bcast_phone_code_hash", None)
        return ConversationHandler.END
    except Exception as e:
        logger.error("Broadcaster 2FA error: %s", e)
        await update.message.reply_text(
            f"❌ 2FA failed: <code>{e}</code>\n\nCheck the password and try again, or /cancel.",
            parse_mode="HTML",
        )
        return _BCAST_AUTH_2FA


async def bcast_auth_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the broadcaster auth conversation."""
    context.user_data.pop("bcast_phone_code_hash", None)
    await update.message.reply_text("❌ Authentication cancelled.")
    return ConversationHandler.END


# ── Config editing conversation ───────────────────────────────────────────────

async def bcast_cfg_got_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received a new config value from the user."""
    field = context.user_data.get("bcast_cfg_field", "")
    raw = (update.message.text or "").strip()
    cfg = _broadcaster.config

    try:
        if field == "delay":
            val = float(raw)
            if val < 0.1:
                raise ValueError("Minimum delay is 0.1 minutes.")
            cfg.delay_minutes = round(val, 2)
        elif field == "cap":
            val = int(raw)
            if val < 1:
                raise ValueError("Minimum cap is 1.")
            cfg.max_per_hour = val
        elif field == "variance":
            val = float(raw.strip("%")) / 100
            if not (0 <= val <= 1):
                raise ValueError("Variance must be between 0% and 100%.")
            cfg.delay_variance = round(val, 2)
        elif field == "warmup":
            val = int(raw)
            if val < 0:
                raise ValueError("Warmup must be 0 or more.")
            cfg.warmup_count = val
        else:
            await update.message.reply_text("⚠️ Unknown field. Try /cancel.")
            return _BCAST_CFG_VALUE

        cfg.save()
        await context.bot.send_message(
            update.effective_chat.id,
            f"✅ <b>{field.capitalize()} updated!</b>\n\n"
            + "⚙️ <b>Settings</b>\n\n" + _broadcaster.get_config_text(),
            parse_mode="HTML",
            reply_markup=_bcast_settings_kb(),
        )
        context.user_data.pop("bcast_cfg_field", None)
        return ConversationHandler.END

    except ValueError as e:
        await update.message.reply_text(
            f"⚠️ Invalid value: {e}\n\nTry again, or /cancel.",
            parse_mode="HTML",
        )
        return _BCAST_CFG_VALUE


async def bcast_cfg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel config editing conversation."""
    context.user_data.pop("bcast_cfg_field", None)
    await update.message.reply_text("❌ Cancelled.")
    await context.bot.send_message(
        update.effective_chat.id,
        "⚙️ <b>Settings</b>\n\n" + _broadcaster.get_config_text(),
        parse_mode="HTML",
        reply_markup=_bcast_settings_kb(),
    )
    return ConversationHandler.END


# ── Add message conversation ──────────────────────────────────────────────────

async def bcast_add_msg_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Received new message text — add it as a template."""
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Please send the message text, or /cancel.")
        return _BCAST_ADD_MSG
    _broadcaster.add_template(text)
    idx = len(_broadcaster.config.messages) - 1
    await update.message.reply_text(
        f"✅ <b>Template #{idx + 1} added!</b>\n\n"
        f"<i>{text[:200]}</i>",
        parse_mode="HTML",
    )
    await context.bot.send_message(
        update.effective_chat.id,
        _broadcaster.get_templates_text(),
        parse_mode="HTML",
        reply_markup=_bcast_messages_kb(),
    )
    return ConversationHandler.END


async def bcast_add_msg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel add-message conversation."""
    await update.message.reply_text("❌ Cancelled.")
    await context.bot.send_message(
        update.effective_chat.id,
        _broadcaster.get_templates_text(),
        parse_mode="HTML",
        reply_markup=_bcast_messages_kb(),
    )
    return ConversationHandler.END


# ── Main callback dispatcher ──────────────────────────────────────────────────

async def bcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatch all bcast_* callback queries to the appropriate panel."""
    query = update.callback_query
    user  = query.from_user

    if not _is_broadcaster_admin(user):
        await query.answer("⛔ Not authorised.", show_alert=True)
        return

    await query.answer()
    action = query.data
    cfg = _broadcaster.config

    # ── Navigate to main panel ──
    if action in ("bcast_main", "bcast_status"):
        await query.edit_message_text(
            "🎛 <b>Broadcaster Control Panel</b>\n\n" + _broadcaster.get_status_text(),
            parse_mode="HTML",
            reply_markup=_bcast_main_kb(),
        )

    # ── Start ──
    elif action == "bcast_start":
        if _broadcaster.is_running:
            await query.edit_message_text(
                "⚠️ Already running.\n\n" + _broadcaster.get_status_text(),
                parse_mode="HTML", reply_markup=_bcast_main_kb(),
            )
            return
        if not await _broadcaster.is_authenticated():
            await query.edit_message_text(
                "⚠️ Not authenticated. Use /bot to log in first.",
                parse_mode="HTML",
            )
            return
        chat_id = query.message.chat_id
        async def _progress(msg: str):
            try:
                await context.bot.send_message(chat_id, msg, parse_mode="HTML")
            except TelegramError:
                pass
        result = await _broadcaster.start_broadcast(progress_callback=_progress)
        await query.edit_message_text(
            result + "\n\nProgress updates will appear below.",
            parse_mode="HTML",
            reply_markup=_bcast_main_kb(),
        )

    # ── Stop ──
    elif action == "bcast_stop":
        result = await _broadcaster.stop_broadcast()
        await query.edit_message_text(
            result + "\n\n" + _broadcaster.get_status_text(),
            parse_mode="HTML", reply_markup=_bcast_main_kb(),
        )

    # ── Pause ──
    elif action == "bcast_pause":
        result = _broadcaster.pause_broadcast()
        await query.edit_message_text(
            result + "\n\n" + _broadcaster.get_status_text(),
            parse_mode="HTML", reply_markup=_bcast_main_kb(),
        )

    # ── Resume ──
    elif action == "bcast_resume":
        result = _broadcaster.resume_broadcast()
        await query.edit_message_text(
            result + "\n\n" + _broadcaster.get_status_text(),
            parse_mode="HTML", reply_markup=_bcast_main_kb(),
        )

    # ── Reload config ──
    elif action == "bcast_reload":
        cfg.reload()
        await query.edit_message_text(
            "🔄 <b>Config reloaded from file!</b>\n\n" + _broadcaster.get_config_text(),
            parse_mode="HTML", reply_markup=_bcast_main_kb(),
        )

    # ────────────────────────────────────────────────
    # SETTINGS PANEL
    # ────────────────────────────────────────────────

    elif action == "bcast_settings":
        await query.edit_message_text(
            "⚙️ <b>Broadcaster Settings</b>\n\n"
            + _broadcaster.get_config_text()
            + "\n\n<i>Tap a button to edit that setting.</i>",
            parse_mode="HTML",
            reply_markup=_bcast_settings_kb(),
        )

    elif action == "bcast_cfg_toggle_archived":
        cfg.include_archived = not cfg.include_archived
        cfg.save()
        icon = "✅" if cfg.include_archived else "❌"
        await query.edit_message_text(
            f"{icon} <b>Archived groups {'enabled' if cfg.include_archived else 'disabled'}!</b>\n\n"
            + _broadcaster.get_config_text(),
            parse_mode="HTML", reply_markup=_bcast_settings_kb(),
        )

    elif action in ("bcast_cfg_delay", "bcast_cfg_cap", "bcast_cfg_variance", "bcast_cfg_warmup"):
        field = action.replace("bcast_cfg_", "")
        prompts = {
            "delay":    (f"⏱ Current delay: <b>{cfg.delay_minutes} minutes</b>\n\n"
                         "Send the new delay in minutes (e.g. <code>2.5</code>):"),
            "cap":      (f"📊 Current cap: <b>{cfg.max_per_hour}/hour</b>\n\n"
                         "Send the new max messages per hour (e.g. <code>20</code>):"),
            "variance": (f"🎲 Current variance: <b>±{int(cfg.delay_variance * 100)}%</b>\n\n"
                         "Send the new variance as a % (e.g. <code>30</code> for ±30%):"),
            "warmup":   (f"🛡 Current warmup: <b>{cfg.warmup_count} messages</b>\n\n"
                         "Send the new warmup count (e.g. <code>5</code>):"),
        }
        context.user_data["bcast_cfg_field"] = field
        await query.edit_message_text(
            prompts[field] + "\n\nSend /cancel to abort.",
            parse_mode="HTML",
            reply_markup=_bcast_cancel_kb(),
        )
        # Signal to the config ConversationHandler to listen next
        context.user_data["bcast_awaiting_cfg"] = True

    # ────────────────────────────────────────────────
    # MESSAGES PANEL
    # ────────────────────────────────────────────────

    elif action == "bcast_msgs":
        await query.edit_message_text(
            _broadcaster.get_templates_text(),
            parse_mode="HTML",
            reply_markup=_bcast_messages_kb(),
        )

    elif action == "bcast_add_msg":
        await query.edit_message_text(
            "➕ <b>Add Message Template</b>\n\n"
            "Send the message text you want to broadcast.\n"
            "Supports Telegram formatting (bold, italic, links, etc.)\n\n"
            "Send /cancel to abort.",
            parse_mode="HTML",
            reply_markup=_bcast_cancel_kb(),
        )
        context.user_data["bcast_awaiting_add_msg"] = True

    elif action.startswith("bcast_msg_view_"):
        idx = int(action.split("_")[-1])
        if 0 <= idx < len(cfg.messages):
            msg = cfg.messages[idx]
            state = "✅ Active" if _broadcaster.is_template_enabled(idx) else "⏸ Paused"
            text = (msg.get("text") or "").replace("<", "&lt;").replace(">", "&gt;")
            await query.edit_message_text(
                f"📝 <b>Template #{idx + 1}</b> — {state}\n\n"
                f"{text}",
                parse_mode="HTML",
                reply_markup=_bcast_messages_kb(),
            )

    elif action.startswith("bcast_msg_pause_"):
        idx = int(action.split("_")[-1])
        _broadcaster.disable_template(idx)
        await query.edit_message_text(
            f"⏸ <b>Template #{idx + 1} paused.</b>\n\n"
            + _broadcaster.get_templates_text(),
            parse_mode="HTML",
            reply_markup=_bcast_messages_kb(),
        )

    elif action.startswith("bcast_msg_resume_"):
        idx = int(action.split("_")[-1])
        _broadcaster.enable_template(idx)
        await query.edit_message_text(
            f"▶️ <b>Template #{idx + 1} resumed.</b>\n\n"
            + _broadcaster.get_templates_text(),
            parse_mode="HTML",
            reply_markup=_bcast_messages_kb(),
        )

    elif action.startswith("bcast_msg_del_"):
        idx = int(action.split("_")[-1])
        if 0 <= idx < len(cfg.messages):
            preview = (cfg.messages[idx].get("text") or "")[:60]
            _broadcaster.remove_template(idx)
            await query.edit_message_text(
                f"🗑 <b>Template #{idx + 1} removed.</b>\n"
                f"<i>{preview}</i>\n\n"
                + _broadcaster.get_templates_text(),
                parse_mode="HTML",
                reply_markup=_bcast_messages_kb(),
            )

    elif action == "bcast_cfg_cancel":
        context.user_data.pop("bcast_cfg_field", None)
        context.user_data.pop("bcast_awaiting_cfg", None)
        context.user_data.pop("bcast_awaiting_add_msg", None)
        await query.edit_message_text(
            "❌ Cancelled.\n\n🎛 <b>Broadcaster Control Panel</b>\n\n"
            + _broadcaster.get_status_text(),
            parse_mode="HTML",
            reply_markup=_bcast_main_kb(),
        )


async def bcast_inline_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch-all text handler for in-panel config editing and add-message flow.

    When user taps an edit button (delay/cap/variance/warmup or add message),
    the callback sets a flag in user_data. The next private text message
    arrives here (if the user isn't in a ConversationHandler state).
    """
    user = update.effective_user
    if not _is_broadcaster_admin(user):
        return
    if update.effective_chat.type != "private":
        return

    # ── Config value edit ──
    if context.user_data.get("bcast_awaiting_cfg"):
        context.user_data.pop("bcast_awaiting_cfg", None)
        await bcast_cfg_got_value(update, context)
        return

    # ── Add message text ──
    if context.user_data.get("bcast_awaiting_add_msg"):
        context.user_data.pop("bcast_awaiting_add_msg", None)
        await bcast_add_msg_got_text(update, context)
        return







# ── post_init: verify bot membership in configured groups ────────────────────

async def post_init(application: Application):
    """Run once after bot starts — verify access to all configured groups
    and initialise the Telethon broadcaster client if credentials exist."""
    bot = application.bot
    for gid in GROUP_IDS:
        try:
            chat = await bot.get_chat(gid)
            logger.info(f"✅ Access OK: {chat.title or gid} ({gid})")
        except TelegramError as e:
            logger.warning(
                f"⚠️  Cannot access chat {gid}: {e}. "
                f"Make sure the bot is added as a member/admin."
            )

    # ── Initialise Telethon broadcaster client ──
    # If credentials file exists (saved by the /bot setup wizard),
    # pre-connect the Telethon client so /bot is instant next time.
    from broadcaster import SessionManager as SM
    if SM.has_credentials():
        try:
            _broadcaster._create_client()
            await _broadcaster.connect()
            if await _broadcaster.is_authenticated():
                me = await _broadcaster.client.get_me()
                logger.info(
                    "✅ Broadcaster authenticated as: %s (ID: %s)",
                    me.first_name, me.id,
                )
            else:
                logger.info(
                    "ℹ️  Broadcaster credentials found but not authenticated. "
                    "Use /bot in a DM to log in."
                )
        except Exception as e:
            logger.warning("⚠️  Broadcaster init failed: %s", e)



# ── 𝔾𝕠𝕣𝕕𝕠 Adblocking / Privacy interactive menu ───────────────────────────────

# Flat category list: (key, emoji, label)
_GORDO_CATEGORIES = [
    ("adblock",       "🛡️", "Adblocking"),
    ("adfilters",     "📋", "Adblock Filters"),
    ("dnsblock",      "🌐", "DNS Adblocking"),
    ("dnsfilters",    "📝", "DNS Filters"),
    ("antivirus",     "🦠", "Antivirus | Anti-Malware"),
    ("filescan",      "🔍", "File Scanners"),
    ("sitelegit",     "✅", "Site Legitimacy Check"),
    ("privacy",       "🔒", "Privacy"),
    ("privindex",     "📚", "Privacy Indexes"),
    ("netsec",        "🔐", "Network Security"),
    ("webpriv",       "🌍", "Web Privacy"),
    ("browser",       "🧭", "Browser Privacy"),
    ("pass2fa",       "🔑", "Password Privacy | 2FA"),
    ("encmsg",        "💬", "Encrypted Messengers"),
    ("emailpriv",     "📧", "Email Privacy"),
    ("breach",        "🚨", "Data Breach Monitoring"),
    ("tracking",      "👁️", "Fingerprinting | Tracking"),
    ("search",        "🔎", "Search Engines"),
    ("vpn",           "🛜", "VPN"),
    ("vpnsrv",        "🖥️", "VPN Server"),
    ("vpntools",      "🔧", "VPN Tools"),
    ("proxy",         "🔄", "Proxy"),
    ("proxysrv",      "📡", "Proxy Servers"),
    ("proxycli",      "📱", "Proxy Clients"),
    ("anticensor",    "🚫", "Anti Censorship"),
    ("proxysites",    "🌐", "Proxy Sites"),
]

# Tools per category — placeholder empty lists; will be filled later
GORDO_TOOLS: dict[str, list[tuple[str, str]]] = {key: [] for key, _, _ in _GORDO_CATEGORIES}

# ── Adblocking tools ──
GORDO_TOOLS["adblock"] = [
    ("uBlock Origin — Adblocker", "https://github.com/gorhill/uBlock"),
    ("uBO Lite (MV3) — Adblocker", "https://github.com/uBlockOrigin/uBOL-home"),
    ("AdGuard — Adblocker", "https://github.com/AdguardTeam/AdguardBrowserExtension"),
    ("Redundant Extensions", "https://github.com/arkenfox/user.js/wiki/4.1-Extensions/#-dont-bother"),
    ("Report: uAssets", "https://github.com/uBlockOrigin/uAssets/issues"),
    ("Report: Hosts", "https://github.com/uBlockOrigin/uAssets/discussions/27472"),
    ("Report: AdGuard", "https://reports.adguard.com/new_issue.html"),
    ("Report: EasyList", "https://github.com/easylist/easylist/issues"),
    ("SponsorBlock", "https://sponsor.ajay.app/"),
    ("SponsorBlock Bookmarklet", "https://github.com/mchangrh/sb.js"),
    ("SponsorBlock Script", "https://greasyfork.org/en/scripts/453320"),
    ("SponsorBlock Ports", "https://github.com/ajayyy/SponsorBlock/wiki/3rd-Party-Ports"),
    ("SponsorBlock Database", "https://sb.ltn.fi/"),
    ("SponsorBlock Chromecast", "https://github.com/gabe565/CastSponsorSkip"),
    ("Disblock Origin — Hide Discord Ads", "https://codeberg.org/AllPurposeMat/Disblock-Origin"),
    ("Discord Adblock — Hide Nitro & Boost Ads", "https://codeberg.org/ridge/Discord-AdBlock"),
    ("Popup Blocker (strict)", "https://github.com/schomery/popup-blocker"),
    ("Popupblocker All", "https://addons.mozilla.org/en-US/firefox/addon/popupblockerall/"),
    ("PopUpOFF", "https://popupoff.org/"),
    ("Popup Blocker Userscript", "https://github.com/AdguardTeam/PopupBlocker"),
    ("BehindTheOverlay — Hide Overlays", "https://github.com/NicolaeNMV/BehindTheOverlay"),
    ("Spot SponsorBlock — Skip Spotify Podcast Ads", "https://spotsponsorblock.org/"),
    ("BilibiliSponsorBlock — Bilibili Ads", "https://github.com/hanydd/BilibiliSponsorBlock"),
]

# ── Adblock Filters ──
GORDO_TOOLS["adfilters"] = [
    ("LegitimateURLShortener — Query Param Cleaning", "https://raw.githubusercontent.com/DandelionSprout/adfilt/refs/heads/master/LegitimateURLShortener.txt"),
    ("Hagezi Blocklists — Blocklist Collection", "https://github.com/hagezi/dns-blocklists"),
    ("FilterLists — Filter | Host List Directory", "https://filterlists.com/"),    ("Filterlist — Unsafe Sites Filter", "https://github.com/fmhy/FMHYFilterlist"),
    ("AI uBlock Blacklist — Blocks AI Sites", "https://github.com/alvi-se/ai-ublock-blacklist"),
    ("Huge AI Blocklist — Remove AI from Search", "https://github.com/laylavish/uBlockOrigin-HUGE-AI-Blocklist"),
]

# ── DNS Adblocking ──
GORDO_TOOLS["dnsblock"] = [
    ("DNS Providers — Provider Index", "https://adguard-dns.io/kb/general/dns-providers/"),
    ("Pi-Hole — Self-Hosted DNS Adblocking", "https://pi-hole.net/"),
    ("Pi-Hole Filters", "https://firebog.net/"),
    ("Pi-Hole Tray App", "https://github.com/PinchToDebug/Pihole-Tray/"),
    ("Pi-Hole Android (root)", "https://github.com/DesktopECHO/Pi-hole-for-Android"),
    ("AdGuard Home — Self-Hosted DNS Adblocking", "https://adguard.com/en/adguard-home/overview.html"),
    ("Balena-AdGuard", "https://github.com/klutchell/balena-adguard"),
    ("Mullvad DNS — Adblocking | Filtering", "https://mullvad.net/en/help/dns-over-https-and-dns-over-tls/"),    ("Mullvad Extension", "https://mullvad.net/en/download/browser/extension"),
    ("DNS Speed Test", "https://dnsspeedtest.online/"),
    ("DNS Perf — Speed Benchmark", "https://dnsperf.com/dns-speed-benchmark"),
    ("NameBench — Speed Benchmark", "https://code.google.com/archive/p/namebench/"),
    ("YogaDNS — Custom DNS Client (Windows)", "https://yogadns.com/"),
    ("NextDNS — Customizable DNS Adblocking", "https://nextdns.io/"),
    ("LibreDNS — DNS Adblocking", "https://libredns.gr/"),
    ("Tiarap — DNS Adblocking", "https://doh.tiar.app/"),
    ("Rethink DNS — DNS Adblocking", "https://rethinkdns.com/configure"),
    ("DNSWarden — DNS Adblocking", "https://dnswarden.com/"),
    ("Blocky — DNS Adblocking", "https://0xerr0r.github.io/blocky/latest/"),
    ("AdGuard DNS — Customizable DNS Adblocking", "https://adguard-dns.io/"),
    ("Control D — Customizable DNS Adblocking", "https://controld.com/free-dns"),
    ("NxFilter — Self-Hosted DNS Adblocking", "https://nxfilter.org/"),
    ("TBlock — DNS Adblocking Client", "https://tblock.me/"),
    ("Diversion — Asuswrt-Merlin Router Adblock", "https://diversion.ch/"),
    ("Phishing Army — DNS Phishing Blocklist", "https://phishing.army/"),
    ("Technitium — Self-Hosted DNS Server", "https://technitium.com/dns"),
]

# ── DNS Filters ──
GORDO_TOOLS["dnsfilters"] = [
    ("OISD", "https://oisd.nl/"),
    ("hBlock", "https://github.com/hectorm/hblock"),
    ("Hosts File Aggregator", "https://github.com/StevenBlack/hosts"),
    ("Spamhaus", "https://www.spamhaus.org/blocklists/"),
    ("black-mirror", "https://github.com/T145/black-mirror"),
    ("Scam Blocklist", "https://github.com/durablenapkin/scamblocklist"),
    ("neodevhost", "https://github.com/neodevpro/neodevhost"),
    ("1Hosts", "https://o0.pages.dev/"),
]

# ── Antivirus | Malware ──
GORDO_TOOLS["antivirus"] = [
    ("Malwarebytes — Antivirus", "https://www.malwarebytes.com/"),
    ("ESET — Antivirus", "https://rentry.co/FMHYB64#eset"),
    ("AdwCleaner — Anti-Adware", "https://www.malwarebytes.com/adwcleaner/"),
    ("Triage — Online Sandbox", "https://tria.ge/"),
    ("Cuckoo — Online Sandbox", "https://cuckoo.cert.ee/"),
    ("Cuckoo 2 — Online Sandbox", "https://sandbox.pikker.ee/"),
    ("Security Multireddit", "https://www.reddit.com/user/goretsky/m/security/"),
    ("SafeGuard — Trusted/Untrusted Sites", "https://fmhy.github.io/FMHY-SafeGuard/"),
    ("BleepingComputer — Malware Removal Forum", "https://www.bleepingcomputer.com/forums/f/22/virus-trojan-spyware-and-malware-removal-help/"),
    ("Malwarebytes Forums — Malware Removal", "https://forums.malwarebytes.com/forum/7-windows-malware-removal-help-support/"),
    ("Sysnative — Malware Removal Forum", "https://www.sysnative.com/forums/forums/security-arena.66/"),
    ("Sandboxie Plus — Sandbox Environment", "https://sandboxie-plus.com/"),
    ("Windows Sandbox — VM Sandbox", "https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-overview"),
    ("Dangerzone — Convert Malicious PDFs", "https://dangerzone.rocks/"),
    ("No More Ransom — Ransomware Decryption", "https://www.nomoreransom.org/en/decryption-tools.html"),
    ("ID Ransomware — Ransomware Identifier", "https://id-ransomware.malwarehunterteam.com/"),
    ("ConfigureDefender — Windows Defender Settings", "https://github.com/AndyFul/ConfigureDefender"),
]

# ── File Scanners ──
GORDO_TOOLS["filescan"] = [
    ("The Second Opinion — Portable Malware Scanner", "https://jijirae.github.io/thesecondopinion/index.html"),
    ("The Second Opinion (mirror)", "https://rentry.co/thesecondopinion"),
    ("VirusTotal — Online File Scanner", "https://www.virustotal.com/"),
    ("VirusTotal Scan Results Guide", "https://claraiscute.neocities.org/Guides/vtguide/"),
    ("Hybrid Analysis — Online File Scanner", "https://hybrid-analysis.com/"),
    ("VirusTotal CLI", "https://github.com/VirusTotal/vt-cli"),
    ("VirusTotal Uploader", "https://github.com/SamuelTulach/VirusTotalUploader"),
    ("VirusTotal Lite", "https://www.virustotal.com/old-browsers/"),
    ("Microsoft Safety Scanner — On-Demand AV", "https://learn.microsoft.com/en-us/defender-endpoint/safety-scanner-download"),
    ("Manalyzer — PE File Scanner", "https://manalyzer.org/"),
    ("YARA — Malware Identification Tool", "https://virustotal.github.io/yara/"),
    ("Winitor — EXE Malware Assessment", "https://www.winitor.com/"),
    ("pyWhat — Identify Anything", "https://github.com/bee-san/pyWhat"),
    ("Grype — Container Vulnerability Scanner", "https://github.com/anchore/grype"),
    ("Jotti — Online File Scanner", "https://virusscan.jotti.org/en"),
    ("Threat Insights Portal — Online File Scanner", "https://www.threat.rip/"),
    ("Filescan.io — Online File Scanner", "https://www.filescan.io/"),
    ("MetaDefender Cloud — Online File Scanner", "https://metadefender.com/"),
    ("Farbar — Local File Scanner", "https://www.bleepingcomputer.com/download/farbar-recovery-scan-tool/"),
]

# ── Site Legitimacy Check ──
GORDO_TOOLS["sitelegit"] = [
    ("URL Void", "https://www.urlvoid.com/"),
    ("URLScan", "https://urlscan.io/"),
    ("Trend Micro — Site Safety", "https://global.sitesafety.trendmicro.com/"),
    ("ScamAdviser", "https://www.scamadviser.com/"),
    ("IsLegitSite", "https://www.islegitsite.com/"),
    ("ZScaler — Zulu", "https://zulu.zscaler.com/"),
    ("Talos Intelligence", "https://talosintelligence.com/"),
]

# ── Privacy ──
GORDO_TOOLS["privacy"] = [
    ("Whonix — Privacy-Focused OS", "https://www.whonix.org/"),
    ("Qubes — Privacy-Focused OS", "https://www.qubes-os.org/"),
    ("Tails — Privacy-Focused OS", "https://tails.net/"),
    ("W10Privacy — Data Protection Tools", "https://www.w10privacy.de/english-home/"),
    ("Telemetry.md — Disable Win 10/11 Telemetry", "https://gist.github.com/ave9858/a2153957afb053f7d0e7ffdd6c3dcb89"),
    ("Agent DVR — Security Camera System", "https://www.ispyconnect.com/"),
    ("Frigate — Security Camera System", "https://frigate.video/"),
    ("ZoneMinder — Security Camera System", "https://zoneminder.com/"),
    ("go2rtc — Security Camera Bridge", "https://github.com/AlexxIT/go2rtc"),
    ("Team Elite — Security Software", "https://www.te-home.net/"),
    ("YourDigitalRights — Request Data Deletion", "https://yourdigitalrights.org/"),
    ("Big Ass Data Broker Opt-Out List", "https://github.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List"),
    ("DataRequests — GDPR Request Generator", "https://www.datarequests.org/"),
    ("Surfer Protocol — User Data Exporter", "https://github.com/Surfer-Org/Protocol"),
    ("GnuPG — Data | Comms Encryption", "https://gnupg.org/"),    ("PrivNote — Self-Destructing Messages", "https://privnote.com/"),
    ("Hemmelig — Self-Destructing Messages", "https://hemmelig.app/"),
    ("OneTimeSecret — Self-Destructing Messages", "https://onetimesecret.com/"),
    ("Forensic Focus — Digital Forensics Forums", "https://www.forensicfocus.com/forums/"),
    ("SurveillanceWatch — Surveillance Connections", "https://www.surveillancewatch.io/"),
    ("ALPR Watch — License Plate Reader Map", "https://alprwatch.org/"),
    ("DeFlock — License Plate Reader Map", "https://deflock.me/"),
    ("ALPR Watch — Gov Surveillance Meetings", "https://alpr.watch/"),
    ("People Over Papers — ICE Activity Map", "https://iceout.org/en/"),
    ("ICE Map", "https://www.icemap.dev/"),
    ("If An Agent Knocks — Know Your Rights", "https://docs.google.com/document/d/176Yds1p63Q3iaKilw0luChMzlJhODdiPvF2I4g9eIXo/"),
]

# ── Privacy Indexes ──
GORDO_TOOLS["privindex"] = [
    ("Privacy Guides — Educational Guide", "https://www.privacyguides.org/"),
    ("Surveillance Self-Defense — Educational Guide", "https://ssd.eff.org/"),
    ("The New Oil — Educational Guide", "https://thenewoil.org/"),
    ("No Trace — Educational Guide", "https://www.notrace.how/"),
    ("The Hitchhiker's Guide — Anonymity Guide", "https://anonymousplanet.org/"),
    ("The OPSEC Bible — Anonymity Guide", "https://opsec.hackliberty.org/"),
    ("Consumer Rights Wiki", "https://consumerrights.wiki/"),
    ("Lissy93's Awesome Privacy", "https://awesome-privacy.xyz/"),
    ("Awesome Security Hardening", "https://github.com/decalage2/awesome-security-hardening"),
    ("pluja's Awesome Privacy", "https://pluja.github.io/awesome-privacy/"),
    ("Defensive Computing Checklist", "https://defensivecomputingchecklist.com/"),
    ("Whonix Wiki — Educational Guide", "https://www.whonix.org/wiki"),
    ("Kicksecure Wiki — Educational Guide", "https://www.kicksecure.com/wiki"),
    ("OPSEC Guide", "https://whos-zycher.github.io/opsec-guide/"),
    ("PrivSec — Educational Guide", "https://privsec.dev/"),
    ("Digital Defense — Privacy Checklist", "https://digital-defense.io/"),
    ("AvoidTheHack — Educational Blog", "https://avoidthehack.com/"),
    ("Hostux — Privacy Tools", "https://hostux.network/"),
    ("Privacy Settings — Setting Guides", "https://github.com/StellarSand/privacy-settings"),
    ("Privacy Not Included — Product Ratings", "https://www.mozillafoundation.org/en/privacynotincluded/"),
    ("EncryptedList — Encrypted Services", "https://encryptedlist.xyz/"),
    ("Awesome Vehicle Security", "https://github.com/jaredthecoder/awesome-vehicle-security"),
]

# ── Network Security ──
GORDO_TOOLS["netsec"] = [
    ("Safing Portmaster — Network Monitor | Firewall", "https://safing.io/"),    ("I2P — Encrypted Private Network Layer", "https://geti2p.net/en/"),
    ("Simplewall — Firewall", "https://github.com/henrypp/simplewall"),
    ("Fort — Firewall", "https://github.com/tnodir/fort"),
    ("WFC — Firewall", "https://www.binisoft.org/wfc.php"),
]

# ── Web Privacy ──
GORDO_TOOLS["webpriv"] = [
    ("PrivacySpy — Privacy Policy Ratings", "https://privacyspy.org/"),
    ("ToS;DR — Terms of Service Ratings", "https://tosdr.org/"),
    ("JustDeleteMe — Find | Terminate Old Accounts", "https://justdeleteme.xyz/"),    ("No More Google — Google App Alternatives", "https://nomoregoogle.com/"),
    ("Phish Report — Report Phishing Sites", "https://phish.report/"),
    ("OpenPhish — Phishing Intelligence", "https://openphish.com/"),
    ("PhishTank — Report Phishing Sites", "https://phishtank.org/"),
    ("PhishStats — Phishing Database", "https://phishstats.info/"),
    ("DNS Jumper — DNS Switcher", "https://www.sordum.org/7952/dns-jumper-v2-3/"),
    ("OnionHop — Tor Network Client", "https://www.onionhop.de/"),
    ("PeerTube — Decentralized Video Hosting", "https://joinpeertube.org/"),
    ("tweetXer — Delete X.com Posts", "https://github.com/lucahammer/tweetXer"),
    ("Power Delete Suite — Reddit Auto Post Delete", "https://github.com/j0be/PowerDeleteSuite"),
    ("Hyphanet — Browse | Publish Freenet Sites", "https://www.hyphanet.org/"),]

# ── Browser Privacy ──
GORDO_TOOLS["browser"] = [
    ("Browser Privacy Guides — Setup Guides", "https://www.privacyguides.org/en/desktop-browsers"),
    ("Tor Browser — Onion-Routed Browser", "https://www.torproject.org/"),
    ("Mullvad Browser — Tor Browser Fork", "https://mullvad.net/en/browser"),
    ("arkenfox — Firefox Privacy Tweak", "https://github.com/arkenfox/user.js"),
    ("LibreWolf — Privacy-Focused Firefox Fork", "https://librewolf.net/"),
    ("Phoenix — Firefox Privacy Tweak", "https://codeberg.org/celenity/Phoenix"),
    ("Brave Browser — Privacy Chromium Browser", "https://brave.com/"),
    ("Encrypted SNI — Cloudflare Browser Check", "https://www.cloudflare.com/ssl/encrypted-sni/"),
]

# ── Password Privacy / 2FA ──
GORDO_TOOLS["pass2fa"] = [
    ("2FA Directory — Sites with 2FA Support", "https://2fa.directory/"),
    ("Ente Auth — 2FA | All Platforms", "https://ente.io/auth/"),    ("Aegis — 2FA | Android", "https://getaegis.app/"),    ("Stratum — 2FA | Android", "https://stratumauth.com/"),    ("2FAS — 2FA | Android + iOS", "https://2fas.com/"),    ("Proton Authenticator — 2FA | All Platforms", "https://proton.me/authenticator"),    ("Mauth — 2FA | Android", "https://github.com/X1nto/Mauth"),    ("FreeOTPPlus — 2FA | Android", "https://github.com/helloworld1/FreeOTPPlus"),    ("KeePassXC — 2FA | Desktop", "https://keepassxc.org/"),    ("AuthMe — 2FA | Desktop", "https://authme.levminer.com/"),    ("Yubioath — 2FA | YubiKey Support", "https://developers.yubico.com/yubioath-flutter/"),    ("OTPClient — 2FA | Linux", "https://github.com/paolostivanin/OTPClient"),    ("Sentinel — 2FA | macOS + iOS", "https://getsentinel.io/"),    ("OTP Auth — 2FA | iOS", "https://apps.apple.com/app/otp-auth/id659877384"),    ("Tofu — 2FA | iOS", "https://www.tofuauth.com/"),    ("Authenticator — 2FA Browser Extension", "https://authenticator.cc/"),
    ("2FAuth — Self-Hosted 2FA", "https://docs.2fauth.app/"),
    ("VaultWarden — Unofficial Bitwarden Self-Hosted", "https://github.com/dani-garcia/vaultwarden"),
    ("OTP Helper — Extract OTP Tokens", "https://github.com/jd1378/otphelper"),
    ("steamguard-cli — Generate Steam 2FA Codes", "https://github.com/dyc3/steamguard-cli"),
]

# ── Encrypted Messengers ──
GORDO_TOOLS["encmsg"] = [
    ("Eylenburg Comparisons — Chat App Index", "https://eylenburg.github.io/im_comparison.htm"),
    ("SecuChart — Messenger Comparisons", "https://bkil.gitlab.io/secuchart/"),
    ("Messenger-Matrix — Messenger Comparisons", "https://www.messenger-matrix.de/messenger-matrix-en.html"),
    ("Secure Messaging Apps — App Comparisons", "https://www.securemessagingapps.com/"),
    ("Matrix — Decentralized E2EE Chat | Clients", "https://matrix.org/ecosystem/clients/"),    ("SimpleX — All Platforms | No User IDs", "https://simplex.chat/"),    ("Signal — All Platforms | Requires Phone", "https://signal.org/"),    ("Molly — Signal Fork | Android", "https://github.com/mollyim/mollyim-android"),    ("Briar — Encrypted P2P Messenger", "https://briarproject.org/"),
    ("Wire — All Platforms | Requires Phone", "https://wire.com/en/download/"),    ("Session — All Platforms | No Phone Needed", "https://getsession.org/"),    ("Keybase — All Platforms", "https://keybase.io/"),
    ("Jami — All Platforms | P2P", "https://jami.net/"),    ("Tox — All Platforms | P2P", "https://tox.chat/"),    ("Cabal — P2P | Serverless | All Platforms", "https://cabal.chat/"),    ("Linphone — All Platforms | SIP", "https://www.linphone.org/"),    ("Berty — Mobile | P2P", "https://berty.tech/"),    ("Ricochet Refresh — Desktop | Anonymous", "https://www.ricochetrefresh.net/"),    ("Cwtch — All Platforms | Onion-Routed", "https://docs.cwtch.im/"),    ("Delta Chat — Email-Based Messenger", "https://delta.chat/"),
    ("Status — Mobile | Web3", "https://status.app/"),    ("Damus — iOS | Nostr Protocol", "https://damus.io/"),    ("MySudo — iOS | Compartmentalized IDs", "https://anonyome.com/individuals/mysudo/"),    ("Databag — Self-Hosted | All Platforms", "https://github.com/balzack/databag"),    ("ssh-chat — SSH Chat", "https://github.com/shazow/ssh-chat"),
    ("Devzat — SSH Chat", "https://github.com/quackduck/devzat"),
]

# ── Email Privacy ──
GORDO_TOOLS["emailpriv"] = [
    ("Proton Mail — 1GB Free | Encrypted Email", "https://proton.me/mail"),    ("Disroot — 1GB Free | Encrypted Email", "https://disroot.org/en/services/email"),    ("Tuta — Encrypted Email", "https://tuta.com/"),
    ("DNMX — Onion-Based Email", "https://dnmx.cc/"),
    ("Mailvelope — PGP Encryption for Emails", "https://mailvelope.com/"),
    ("Email Privacy Tester — Email Privacy Test", "https://www.emailprivacytester.com/"),
    ("SecLists — Security Mailing List Archive", "https://seclists.org/"),
]

# ── Data Breach Monitoring ──
GORDO_TOOLS["breach"] = [
    ("Have I Been Pwned? — Monitor Email Breaches", "https://haveibeenpwned.com/"),
    ("F-Secure — Identity Theft Checker", "https://www.f-secure.com/en/identity-theft-checker"),
    ("Have I Been Pwned Passwords — Password Breach Check", "https://haveibeenpwned.com/Passwords"),
    ("Mozilla Monitor — Data Breach Check", "https://monitor.mozilla.org/"),
    ("BreachDirectory — Data Breach Search Engine", "https://breachdirectory.org/"),
    ("Snusbase — Data Breach Search Engine", "https://snusbase.com/"),
    ("Leak Lookup — Data Breach Search Engine", "https://leak-lookup.com/"),
    ("Trufflehog — Secrets | Credential Scanner", "https://trufflesecurity.com/"),    ("LeakPeek — Data Breach Search Engine", "https://leakpeek.com/"),
    ("Intelligence X — Password Breach Check", "https://intelx.io/"),
    ("ScatteredSecrets — Password Breach Check", "https://scatteredsecrets.com/"),
    ("BreachDetective — Password Breach Check", "https://breachdetective.com/"),
]

# ── Fingerprinting / Tracking ──
GORDO_TOOLS["tracking"] = [
    ("CreepJS — Fingerprinting Test", "https://abrahamjuliot.github.io/creepjs"),
    ("webkay — Tracking Test", "https://webkay.robinlinus.com/"),
    ("browserrecon — Browser Fingerprint Scan", "https://www.computec.ch/projekte/browserrecon/?s=scan"),
    ("TZP — Fingerprinting Test", "https://arkenfox.github.io/TZP/tzp.html"),
    ("Cover Your Tracks — Tracking Test", "https://coveryourtracks.eff.org/"),
    ("PersonalData — Fingerprinting Test", "https://personaldata.info/"),
    ("ClearURLs — Remove Tracking from URLs", "https://docs.clearurls.xyz/"),
    ("URLCleaner — Remove Tracking from URLs", "https://urlcleaner.net/"),
    ("Webbkoll — Site Tracking Info", "https://webbkoll.5july.net/"),
    ("Blacklight — Site Tracking Info", "https://themarkup.org/blacklight"),
    ("Data Removal Guide — Remove Online Data", "https://inteltechniques.com/workbook.html"),
    ("GameIndustry — Block Trackers in Games", "https://gameindustry.eu/en/"),
    ("BrowserLeaks — IP Leak Test", "https://browserleaks.com/"),
    ("Do I leak? — IP Leak Test", "https://www.top10vpn.com/tools/do-i-leak/"),
    ("IPLeak.net — IP Leak Test", "https://ipleak.net/"),
    ("JShelter — Prevent Fingerprinting", "https://jshelter.org/"),
    ("Locale Switcher — Change Language Identifier", "https://chromewebstore.google.com/detail/locale-switcher/kngfjpghaokedippaapkfihdlmmlafcc"),
    ("AnonymousRedirect — Anonymize Links", "https://adguardteam.github.io/AnonymousRedirect/"),
    ("X.com Direct — Remove t.co Tracking", "https://greasyfork.org/en/scripts/404632"),
]

# ── Search Engines ──
GORDO_TOOLS["search"] = [
    ("Search Engine Party — Privacy SE Comparisons", "https://searchengine.party/"),
    ("searx.space — SearXNG Instances | Metasearch", "https://searx.space/"),    ("Searx — SearXNG Instance", "https://searx.fmhy.net/"),
    ("Brave Search — Independent Search Engine", "https://search.brave.com/"),
    ("DuckDuckGo — Metasearch | Bing Based", "https://start.duckduckgo.com/"),    ("Fuck Off Google — Searx Instance", "https://search.fuckoffgoogle.net/"),
    ("nixnet — Searx Instance", "https://searx.nixnet.services/"),
    ("monocles — Searx Instance", "https://monocles.de/"),
    ("LibreY — Metasearch", "https://ly.owo.si/"),
    ("Nilch — AI Free Metasearch", "https://nilch.org/"),
    ("4get — Metasearch", "https://4get.ca/"),
    ("Mojeek — Independent Search Engine", "https://www.mojeek.com/"),
    ("YaCy — Decentralized | P2P Search Engine", "https://yacy.net/"),    ("Startpage — Google Based | Private", "https://www.startpage.com/"),    ("SearXNG — Self-Hosted Metasearch", "https://docs.searxng.org/"),
]

# ── VPN ──
GORDO_TOOLS["vpn"] = [
    ("Techlore Chart — VPN Comparison Charts", "https://techlore.tech/vpn"),
    ("VPN Relationships — VPN Relationship Map", "https://kumu.io/Windscribe/vpn-relationships"),
    ("Cloudflare One — Free | Unlimited VPN", "https://one.one.one.one/"),    ("Proton VPN — Free | Unlimited VPN", "https://protonvpn.com/"),    ("Windscribe — Free | 10GB Monthly VPN", "https://windscribe.com/"),    ("RiseupVPN — Free | Unlimited VPN", "https://riseup.net/en/vpn"),    ("AirVPN — Paid VPN", "https://airvpn.org/"),
    ("Mullvad VPN — Paid | No-Logging VPN", "https://mullvad.net/"),    ("IVPN — Paid | No-Logging VPN", "https://www.ivpn.net/"),    ("Nym — Paid | 5-Hop Mixnet VPN", "https://nym.com/"),    ("PrivadoVPN — Free | 10GB Monthly VPN", "https://privadovpn.com/freevpn"),    ("Calyx VPN — Free | Unlimited VPN", "https://calyxos.org/docs/guide/apps/calyx-vpn/"),]

# ── VPN Server ──
GORDO_TOOLS["vpnsrv"] = [
    ("WireGuard — VPN Tunnel", "https://www.wireguard.com/"),
    ("Tailscale — WireGuard Mesh VPN", "https://tailscale.com/"),
    ("NetBird — WireGuard Mesh VPN", "https://netbird.io/"),
    ("Amnezia — VPN Server", "https://amnezia.org/"),
    ("OpenVPN — VPN Server", "https://openvpn.net/"),
    ("WGDashboard — WireGuard Panel", "https://wgdashboard.dev/"),
    ("Twingate — Zero Trust Access Tunnel", "https://www.twingate.com/"),
    ("Headscale — Self-Hosted Tailscale", "https://github.com/juanfont/headscale"),
    ("Nebula — Mesh VPN Server", "https://github.com/slackhq/nebula"),
    ("ZeroTier — Mesh VPN Server", "https://www.zerotier.com/"),
    ("IPsec VPN — VPN Server", "https://github.com/hwdsl2/setup-ipsec-vpn"),
    ("Cloudflare Tunnels — Application Tunnel | VPN Alt", "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/"),    ("tinc VPN — VPN Tunnel", "https://www.tinc-vpn.org/"),
    ("WireHole — WireGuard + Pi-hole VPN", "https://github.com/IAmStoxe/wirehole"),
    ("OpenConnect — SSL VPN", "https://gitlab.com/openconnect/openconnect"),
    ("Pritunl — VPN Server", "https://pritunl.com/"),
    ("Algo VPN — Cloud VPN", "https://blog.trailofbits.com/2016/12/12/meet-algo-the-vpn-that-works/"),
    ("SShuttle — SSH VPN Server", "https://sshuttle.readthedocs.io/en"),
    ("DSVPN — Simple VPN Server", "https://github.com/jedisct1/dsvpn"),
    ("Openconnect — SSL VPN Server", "https://ocserv.gitlab.io/www/index.html"),
]

# ── VPN Tools ──
GORDO_TOOLS["vpntools"] = [
    ("VPN Binding Guide — Bind VPN to Torrent Client", "https://wispydocs.pages.dev/torrenting/"),
    ("WireSock — WireGuard Split Tunneling Client", "https://wiresock.net/"),
    ("Tunnl — WireGuard Split Tunneling Client", "https://tunnl.to/"),
    ("WG Tunnel — WireGuard Client | AmneziaWG", "https://wgtunnel.com/"),    ("VPN Hotspot — Share VPN over Hotspot (Android)", "https://github.com/Mygod/VPNHotspot"),
    ("Gluetun — VPN using Docker", "https://github.com/qdm12/gluetun"),
]

# ── Proxy ──
GORDO_TOOLS["proxy"] = [
    ("Psiphon — Hybrid Proxy VPN App", "https://psiphon.ca/"),
    ("Lantern — Proxy App", "https://lantern.io/"),
    ("FreeSocks — Shadowsocks App", "https://freesocks.org/"),
    ("Snowflake — Tor Proxy Browser Extension", "https://snowflake.torproject.org/"),
    ("Censor Tracker — Proxy Extension", "https://censortracker.org/"),
    ("SmartProxy — Proxy Extension", "https://github.com/salarcode/SmartProxy"),
    ("FoxyProxy — Proxy Extension", "https://getfoxyproxy.org/"),
    ("ZeroOmega — Proxy Extension", "https://github.com/zero-peak/ZeroOmega"),
    ("Acrylic — Local DNS Proxy", "https://mayakron.altervista.org/"),
    ("SimpleDnsCrypt — Local DNS Encryption Proxy", "https://github.com/instantsc/SimpleDnsCrypt"),
    ("DNSCrypt — Local DNS Encryption Proxy", "https://dnscrypt.info/"),
]

# ── Proxy Servers ──
GORDO_TOOLS["proxysrv"] = [
    ("Censordex — Proxy Server Setup", "https://censordex.fr.to/"),
    ("3X-UI — Proxy Panel", "https://github.com/MHSanaei/3x-ui"),
    ("Project X — Xray Proxy Core", "https://github.com/XTLS/Xray-core"),
    ("NaïveProxy — Chromium-Based Proxy", "https://github.com/klzgrad/naiveproxy"),
    ("Hysteria — Speed Focused Proxy Protocol", "https://v2.hysteria.network/"),
    ("Shadowsocks — Simple Proxy Protocol", "https://shadowsocks.org/"),
    ("sing-box — Proxy Core", "https://sing-box.sagernet.org/"),
    ("Amnezia — Multi Protocol Server", "https://amnezia.org/self-hosted"),
    ("Hiddify Manager — Proxy Panel", "https://hiddify.com/"),
    ("Outline — Shadowsocks Server", "https://getoutline.org/"),
    ("VpnHood — Proxy Server", "https://github.com/vpnhood/VpnHood"),
    ("Scramjet — Web Proxy Server", "https://docs.titaniumnetwork.org/proxies/scramjet/"),
    ("Nebula — Web Proxy Server", "https://github.com/NebulaServices/Nebula"),
    ("Nginx Proxy Manager — Reverse Proxy UI", "https://nginxproxymanager.com/"),
]

# ── Proxy Clients ──
GORDO_TOOLS["proxycli"] = [
    ("v2rayN — Proxy Client | Windows", "https://github.com/2dust/v2rayN"),    ("NekoBox — Proxy Client | Android", "https://matsuridayo.github.io/"),    ("v2rayNG — Proxy Client | Android", "https://github.com/2dust/v2rayNG"),    ("MahsaNG — Proxy Client | Android", "https://github.com/GFW-knocker/MahsaNG"),    ("Hiddify — Proxy Client | All Platforms", "https://hiddify.com/"),    ("Amnezia — Proxy Client | All Platforms", "https://amnezia.org/"),    ("Shadowsocks — Shadowsocks Client", "https://shadowsocks.org/doc/getting-started.html#gui-clients"),
    ("sing-box — Proxy Client", "https://sing-box.sagernet.org/clients/"),
    ("Throne — Sing-Box GUI Client", "https://throneproj.github.io/"),
    ("V2Box — Proxy Client | Android + iOS", "https://play.google.com/store/apps/details?id=dev.hexasoftware.v2box"),    ("ClashVerge — Proxy Client | Desktop + Mobile", "https://www.clashverge.dev/"),    ("Streisand — Proxy Client | iOS", "https://streisand.pages.dev/"),    ("FlClash — Proxy Client | All Platforms", "https://github.com/chen08209/FlClash/blob/main/README.md"),    ("husi — Proxy Client | Android", "https://github.com/xchacha20-poly1305/husi"),    ("Proxifier — Add Proxy Support to Any App", "https://www.proxifier.com/"),
    ("wireproxy — WireGuard as HTTP Proxy", "https://github.com/whyvl/wireproxy"),
]

# ── Anti Censorship ──
GORDO_TOOLS["anticensor"] = [
    ("Censorship Bypass Guide — Full Guide", "https://cbg.fmhy.bid/"),
    ("Net4people — Censorship Circumvention Discussion", "https://github.com/net4people/bbs/issues"),
    ("ByeDPIAndroid — Network Packet Alter | Android", "https://github.com/dovecoteescapee/ByeDPIAndroid"),    ("zapret — Network Packet Alter Tool", "https://github.com/bol-van/zapret"),
    ("SpoofDPI — Network Packet Alter Tool", "https://github.com/xvzc/SpoofDPI"),
    ("GoodbyeDPI — Network Packet Alter Tool", "https://github.com/ValdikSS/GoodbyeDPI/"),
    ("DNSveil — DNS Client", "https://msasanmh.github.io/DNSveil/"),
    ("DNSTT.XYZ — DNS Tunneling | Censorship Bypass", "https://dnstt.xyz/"),    ("HTTP Injector — Mobile DNS Tunnel", "https://play.google.com/store/apps/details?id=com.evozi.injector"),
    ("HTTP Custom — Mobile DNS Tunnel", "https://play.google.com/store/apps/details?id=xyz.easypro.httpcustom"),
    ("NetMod VPN — Mobile DNS Tunnel", "https://play.google.com/store/apps/details?id=com.netmod.syna"),
    ("DarkTunnel — Mobile DNS Tunnel", "https://play.google.com/store/apps/details?id=net.darktunnel.app"),
    ("FilterWatch — Censorship News | Articles", "https://filter.watch/english/"),    ("ByeByeDPI — Packet Level Proxy", "https://github.com/romanvht/ByeByeDPI/blob/master/README-en.md"),
    ("PowerTunnel — Network Packet Alter", "https://github.com/krlvm/PowerTunnel"),
    ("Green Tunnel — Network Packet Alter", "https://github.com/SadeghHayeri/GreenTunnel"),
    ("YouTubeUnblock — Unblock YouTube via SNI Spoof", "https://github.com/Waujito/youtubeUnblock"),
    ("Scamalytics — Check IP Blacklists", "https://scamalytics.com/"),
]

# ── Proxy Sites ──
GORDO_TOOLS["proxysites"] = [
    ("Holy Unblocker — Web Proxy", "https://holyunblocker.org/"),
    ("Titanium Network — Multi Proxy", "https://titaniumnetwork.org/services/"),
    ("US5 — Multi-Site + App Proxy | Adblocker", "https://us5.thetravelingtourguide.com/"),    ("SSLSecureProxy — Web Proxy", "https://www.sslsecureproxy.com/"),
    ("ProxyOf2 — Web Proxy", "https://proxyof2.com/"),
    ("Phantom — Web Proxy", "https://phantom.lol/"),
    ("Reflect4 — Web Proxy", "https://reflect4.me/"),
    ("CroxyProxy — Web Proxy", "https://www.croxyproxy.com/"),
    ("Blockaway — Web Proxy", "https://www.blockaway.net/"),
    ("Delusionz — Web Proxy", "https://delusionz.xyz/"),
    ("ProxyPal — Web Proxy", "https://proxypal.net/"),
    ("Proxyium — Web Proxy", "https://proxyium.com/"),
    ("Google Translate — Use as Web Proxy", "https://translate.google.com/"),
    ("Proxy Checker — Proxy Scraper | Checker", "https://proxy-checker.net/"),    ("proxy-scraper-checker — Proxy Scraper", "https://github.com/monosans/proxy-scraper-checker"),
    ("CheckSocks5 — SOCKS5 Proxy Checker", "https://checksocks5.com/"),
    ("Knaben.info — Torrent Site Proxies", "https://knaben.info/"),
]

_GORDO_PAGE_SIZE = 8   # categories per page (1 column)

_GORDO_MAIN_TEXT = (
    "🛡️ <b><a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a> — Adblocking | Privacy</b>\n\n"
    "Your comprehensive guide to online privacy, security, and adblocking tools "
    "curated by <a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a>.\n\n"
    "📌 <i>Select a category below to explore:</i>"
)

def _gordo_main_kb(page: int = 0):
    """Build the main 𝔾𝕠𝕣𝕕𝕠 menu keyboard (1 column, paginated)."""
    start = page * _GORDO_PAGE_SIZE
    end = start + _GORDO_PAGE_SIZE
    cats = _GORDO_CATEGORIES[start:end]
    total_pages = (len(_GORDO_CATEGORIES) + _GORDO_PAGE_SIZE - 1) // _GORDO_PAGE_SIZE
    rows = []
    for key, emoji, label in cats:
        rows.append([InlineKeyboardButton(f"{emoji}  {label}", callback_data=f"gordo_cat_{key}", style="primary")])
    # Pagination row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"◀️  Page {page}", callback_data=f"gordo_page_{page - 1}", style="primary"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(f"Page {page + 2}  ▶️", callback_data=f"gordo_page_{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

def _GORDO_TOOLS_kb(cat_key: str):
    """Build keyboard for a category showing tools as URL buttons (1 per row)."""
    tools = GORDO_TOOLS.get(cat_key, [])
    rows = []
    for name, url in tools:
        rows.append([InlineKeyboardButton(f"{name} 🔗", url=url, style="success")])
    # Determine which page this category is on for the back button
    idx = next((i for i, (k, _, _) in enumerate(_GORDO_CATEGORIES) if k == cat_key), 0)
    page = idx // _GORDO_PAGE_SIZE
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"gordo_page_{page}", style="primary")])
    return InlineKeyboardMarkup(rows)

async def gordo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all gordo_ callback queries for interactive navigation."""
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "gordo_main" or data == "gordo_page_0":
        await q.edit_message_text(_GORDO_MAIN_TEXT, reply_markup=_gordo_main_kb(0),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("gordo_page_"):
        try:
            page = int(data[len("gordo_page_"):])
        except ValueError:
            return
        await q.edit_message_text(_GORDO_MAIN_TEXT, reply_markup=_gordo_main_kb(page),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("gordo_cat_"):
        cat_key = data[len("gordo_cat_"):]
        cat = next(((k, e, l) for k, e, l in _GORDO_CATEGORIES if k == cat_key), None)
        if not cat:
            return
        _, emoji, label = cat
        tools = GORDO_TOOLS.get(cat_key, [])
        if tools:
            text = f"{emoji} <b>{label}</b>\n\n🔽 <i>Tap a tool to open it:</i>"
        else:
            text = f"{emoji} <b>{label}</b>\n\n⏳ <i>Tools coming soon…</i>"
        await q.edit_message_text(text, reply_markup=_GORDO_TOOLS_kb(cat_key),
                                  parse_mode="HTML", disable_web_page_preview=True)

_GORDO_POST_TARGET = 400

async def gordo_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: /privacy_post — ask superadmin where to post the menu."""
    user = update.effective_user
    if not user:
        return ConversationHandler.END
    is_sa = (user.id in _superadmin_ids
             or (user.username or "").lower() in SUPERADMIN_USERNAMES)
    if not is_sa:
        await update.effective_message.reply_text(
            f"❌ Not authorised.\nYour ID: {user.id}\nUsername: @{user.username or '(none)'}"
        )
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "📌 Where should I post the Privacy menu?\n\n"
        "Send a Telegram link, e.g.:\n"
        "<code>https://t.me/c/3786381449/344</code>\n\n"
        "Or /cancel to abort.",
        parse_mode="HTML",
    )
    return _GORDO_POST_TARGET

async def gordo_post_got_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive the link, parse it and post the menu there."""
    url = (update.message.text or "").strip()
    chat_id, topic_id = _parse_custommsg_target(url)
    if chat_id is None:
        await update.message.reply_text(
            "⚠️ Couldn't parse that link. Try again or /cancel."
        )
        return _GORDO_POST_TARGET
    try:
        kwargs = {
            "chat_id": chat_id,
            "text": _GORDO_MAIN_TEXT,
            "reply_markup": _gordo_main_kb(0),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)
        dest = f"{chat_id}" + (f" / topic {topic_id}" if topic_id else "")
        await update.message.reply_text(f"✅ Privacy menu posted to {dest}.")
    except Exception as e:
        logger.error("gordo_post_got_target error: %s", e)
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def gordo_post_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════════
# ██  AI — Artificial Intelligence menu
# ════════════════════════════════════════════════════════════════════════════════

_AI_CATEGORIES = [
    ("aiofficial",  "🤖", "Official Model Sites"),
    ("aimulti",     "🔀", "Multiple Model Sites"),
    ("aispecial",   "🔬", "Specialized Chatbots"),
    ("ailocal",     "💻", "Local AI Frontends"),
    ("aiself",      "🖥️", "Self-Hosting Tools"),
    ("airp",        "🎭", "Roleplaying Chatbots"),
    ("aitools",     "🔧", "AI Tools"),
    ("aiprompts",   "💬", "AI Prompts"),
    ("aiindex",     "📂", "AI Indexes"),
    ("aibench",     "📊", "AI Benchmarks"),
    ("aispecbench", "🔬", "Specialized Benchmarks"),
    ("aicodebench",    "💻",  "Coding Benchmarks"),
    ("aiwriting",     "✍️",  "AI Writing Tools"),
    ("aivideo",       "🎬",  "Video Generation"),
    ("aiimage",       "🖼️",  "Image Generation"),
    ("aiimglocal",    "🖥️",  "Image Local Frontends"),
    ("aiimgguides",   "📖",  "Image Guides | Tools"),    ("aiaudio",       "🎵",  "Audio Generation"),
    ("aitts",         "🔊",  "Text to Speech"),
    ("aivoiceclone",  "🎤",  "Voice Change | Clone"),    ("aivoiceremove", "✂️",  "Voice Removal | Separation"),    ("aiml",          "🧠",  "Machine Learning"),
]

AI_TOOLS: dict[str, list[tuple[str, str]]] = {k: [] for k, _, _ in _AI_CATEGORIES}

# ── Official Model Sites ──
AI_TOOLS["aiofficial"] = [
    ("AI Studio", "https://aistudio.google.com/app/prompts/new_chat"),
    ("Grok", "https://grok.com/"),
    ("Z.ai", "https://chat.z.ai/"),
    ("Gemini", "https://gemini.google.com/"),
    ("Kimi", "https://www.kimi.com/"),
    ("ChatGPT", "https://chatgpt.com/"),
    ("Claude", "https://claude.ai/"),
    ("Ernie", "https://ernie.baidu.com/"),
    ("MiniMax AI", "https://agent.minimax.io/"),
    ("Mistral", "https://chat.mistral.ai/"),
    ("Ai2 Playground", "https://playground.allenai.org/"),
    ("LongCat", "https://longcat.chat/"),
    ("Cohere", "https://dashboard.cohere.com/playground/chat"),
    ("Solar", "https://console.upstage.ai/playground/chat"),
    ("StepFun", "https://stepfun.ai/"),
    ("Apertus", "https://publicai.co/chat"),
    ("Reka", "https://www.reka.ai/"),
    ("K2Think", "https://www.k2think.ai/"),
    ("MiMo Studio", "https://aistudio.xiaomimimo.com/"),
    ("Inception Chat", "https://chat.inceptionlabs.ai/"),
    ("Dolphin Chat", "https://chat.dphn.ai/"),
]

# ── Multiple Model Sites ──
AI_TOOLS["aimulti"] = [
    ("Arena", "https://arena.ai/text/direct"),
    ("Yupp.ai", "https://yupp.ai/"),
    ("ISH", "https://ish.chat/"),
    ("Pollinations Chat", "https://pollinations-chat.vercel.app/"),
    ("Woozlit", "https://woozlit.com/"),
    ("Khoj", "https://app.khoj.dev/"),
    ("Together.ai", "https://chat.together.ai/"),
    ("NVIDIA NIM", "https://build.nvidia.com/"),
    ("Indic LLM Arena", "https://arena.ai4bharat.org/#/chat"),
    ("AI Assistant", "https://aiassistantbot.pages.dev/"),
    ("Cerebras Chat", "https://chat.cerebras.ai/"),
    ("Groq", "https://console.groq.com/playground"),
    ("Duck AI", "https://duck.ai/chat?q=DuckDuckGo+AI+Chat&duckai=1"),
]

# ── Specialized Chatbots ──
AI_TOOLS["aispecial"] = [
    ("Awesome AI Web Search", "https://github.com/felladrin/awesome-ai-web-search"),
    ("Arena Search", "https://arena.ai/?mode=direct&chat-modality=search"),
    ("NotebookLM", "https://notebooklm.google/"),
    ("Google AI Mode", "https://google.com/aimode"),
    ("Ask Brave", "https://search.brave.com/ask"),
    ("Perplexity", "https://www.perplexity.ai/"),
    ("Exa", "https://exa.ai/search"),
    ("Perplexica", "https://github.com/ItzCrazyKns/Perplexica"),
    ("Learn About", "https://learning.google.com/experiments/learn-about"),
    ("MiroThinker", "https://dr.miromind.ai/"),
    ("SciSpace", "https://scispace.com/"),
    ("iAsk AI", "https://iask.ai/"),
    ("Scinito", "https://ekb.scinito.ai/ai/chat"),
    ("Elicit", "https://elicit.com/"),
    ("Alphaxiv", "https://www.alphaxiv.org/"),
    ("PrivateGPT", "https://privategpt.dev/"),
    ("Onyx", "https://www.onyx.app/"),
]

# ── Local AI Frontends ──
AI_TOOLS["ailocal"] = [
    ("Awesome Local LLM", "https://github.com/rafska/awesome-local-llm/"),
    ("SillyTavern", "https://docs.sillytavern.app/"),
    ("Open WebUI", "https://openwebui.com/"),
    ("Msty", "https://msty.app/"),
    ("Cherry Studio", "https://www.cherry-ai.com/"),
    ("GPT4Free", "https://github.com/xtekky/gpt4free"),
    ("LobeChat", "https://lobechat.com/chat"),
    ("Noi", "https://noib.app/"),
    ("Chatbot UI", "https://chatbotui.com/"),
    ("LocalAI", "https://localai.io/"),
    ("tgpt", "https://github.com/aandrew-me/tgpt"),
    ("ch.at", "https://github.com/Deep-ai-inc/ch.at"),
]

# ── Self-Hosting Tools ──
AI_TOOLS["aiself"] = [
    ("Jan", "https://jan.ai/"),
    ("LM Studio", "https://lmstudio.ai/"),
    ("llama.cpp", "https://github.com/ggerganov/llama.cpp"),
    ("KoboldCpp", "https://github.com/LostRuins/koboldcpp"),
    ("oobabooga", "https://github.com/oobabooga/text-generation-webui"),
    ("Aphrodite Engine", "https://aphrodite.pygmalion.chat/"),
    ("Petals", "https://petals.dev/"),
    ("Leon", "https://getleon.ai/"),
    ("Ollama", "https://ollama.com/"),
    ("LoLLMs Web UI", "https://github.com/ParisNeo/lollms-webui"),
    ("AnythingLLM", "https://anythingllm.com/"),
    ("LibreChat", "https://librechat.ai/"),
    ("GPT4All", "https://www.nomic.ai/gpt4all"),
    ("llamafile", "https://github.com/Mozilla-Ocho/llamafile"),
]

# ── Roleplaying Chatbots ──
AI_TOOLS["airp"] = [
    ("Sukino-Findings", "https://rentry.org/Sukino-Findings"),
    ("Perchance", "https://perchance.org/ai-character-chat"),
    ("PygmalionAI", "https://pygmalion.chat/"),
    ("FlowGPT", "https://flowgpt.com/"),
    ("Chub", "https://chub.ai/"),
    ("KoboldAI", "https://koboldai.com/"),
    ("Miku", "https://docs.miku.gg/"),
    ("HammerAI", "https://www.hammerai.com/desktop"),
    ("Agnai", "https://agnai.chat/"),
    ("4thWall AI", "https://beta.4wall.ai/"),
    ("WyvernChat", "https://app.wyvern.chat/"),
    ("FictionLab", "https://fictionlab.ai/"),
    ("AI Dungeon", "https://aidungeon.com/"),
    ("Spellbound", "https://www.tryspellbound.com/"),
    ("TavernAI", "https://tavernai.net/"),
    ("KoboldAI Lite", "https://lite.koboldai.net/"),
]

# ── AI Tools ──
AI_TOOLS["aitools"] = [
    ("AI Price Compare", "https://countless.dev/"),
    ("LLM Pricing", "https://www.llm-prices.com/"),
    ("PricePerToken", "https://pricepertoken.com/"),
    ("Awesome ChatGPT", "https://github.com/sindresorhus/awesome-chatgpt"),
    ("Every ChatGPT GUI", "https://github.com/billmei/every-chatgpt-gui"),
    ("LLM Timeline", "https://llm-timeline.com/"),
    ("tldraw computer", "https://computer.tldraw.com/"),
    ("Page Assist", "https://github.com/n4ze3m/page-assist"),
    ("ChatGPT Box", "https://github.com/josStorer/chatGPTBox"),
    ("KeepChatGPT", "https://github.com/xcanwin/KeepChatGPT/blob/main/docs/README_EN.md"),
    ("LLM CLI", "https://llm.datasette.io/"),
    ("LightSession", "https://github.com/11me/light-session"),
    ("Privatiser", "https://privatiser.net/"),
    ("ChatGPT DeMod", "https://github.com/4as/ChatGPT-DeMod"),
    ("MassiveMark", "https://www.bibcit.com/en/massivemark"),
    ("ChatGPT Widescreen", "https://chatgptevo.com/widescreen/"),
    ("screenpipe", "https://screenpi.pe/"),
    ("ChatGPT Exporter", "https://greasyfork.org/en/scripts/456055"),
    ("GPThemes", "https://github.com/itsmartashub/GPThemes"),
    ("VRAM Calculator", "https://huggingface.co/spaces/NyxKrage/LLM-Model-VRAM-Calculator"),
    ("AI Piracy Resources", "https://rentry.org/aipiracyresources"),
]

# ── AI Prompts ──
AI_TOOLS["aiprompts"] = [
    ("L1B3RT4S", "https://github.com/elder-plinius/L1B3RT4S"),
    ("BlackFriday GPTs Prompts", "https://github.com/friuns2/BlackFriday-GPTs-Prompts"),
    ("Leaked Prompts", "https://github.com/linexjlin/GPTs"),
    ("Prompt Engineering Guide", "https://www.promptingguide.ai/"),
    ("ChatGPT System Prompt", "https://github.com/LouisShark/chatgpt_system_prompt"),
    ("Big Prompt Library", "https://github.com/0xeb/TheBigPromptLibrary"),
    ("Jailbreak Listings", "https://rentry.org/jb-listing"),
    ("Heretic", "https://github.com/p-e-w/heretic"),
    ("promptfoo", "https://www.promptfoo.dev/"),
    ("Tensor Trust", "https://tensortrust.ai/"),
    ("Gandalf", "https://gandalf.lakera.ai/"),
    ("Gobble Bot", "https://gobble.bot/"),
]

# ── AI Indexes ──
AI_TOOLS["aiindex"] = [
    ("LLM Explorer", "https://llm-explorer.com/"),
    ("LifeArchitect", "https://lifearchitect.ai/models-table/"),
    ("FutureTools", "https://www.futuretools.io/?pricing-model=free"),
    ("Google Labs", "https://labs.google/"),
    ("Google Labs FX", "https://labs.google/fx"),
    ("Models.dev", "https://models.dev/"),
    ("YP for AI", "https://www.ypforai.com/"),
    ("LLM Resources Hub", "https://llmresourceshub.vercel.app/"),
    ("Awesome AI Tools", "https://github.com/mahseema/awesome-ai-tools"),
    ("It's Better With AI", "https://itsbetterwithai.com/"),
    ("GPT Demo", "https://www.gptdemo.net/gpt/search?lg=en&cate=&keywords=&tags=free,&sort=popular"),
    ("ArtificialStudio", "https://app.artificialstudio.ai/tools"),
]

# ── AI Benchmarks ──
AI_TOOLS["aibench"] = [
    ("LM Council", "https://lmcouncil.ai/benchmarks"),
    ("Artificial Analysis", "https://artificialanalysis.ai/"),
    ("Kaggle Benchmarks", "https://www.kaggle.com/benchmarks"),
    ("Arena Leaderboard", "https://arena.ai/leaderboard"),
    ("OpenRouter Rankings", "https://openrouter.ai/rankings"),
    ("SEAL LLM Leaderboards", "https://scale.com/leaderboard"),
    ("Yupp Leaderboard", "https://yupp.ai/leaderboard"),
    ("Context Arena", "https://contextarena.ai/"),
    ("RankedAGI", "https://rankedagi.com/"),
    ("LLM Stats", "https://llm-stats.com/"),
    ("OpenLM Arena", "https://openlm.ai/chatbot-arena/"),
    ("Wolfram LLM Benchmarking", "https://www.wolfram.com/llm-benchmarking-project/"),
    ("Epoch AI", "https://epoch.ai/benchmarks/eci"),
]

# ── Specialized Benchmarks ──
AI_TOOLS["aispecbench"] = [
    ("Simple Bench", "https://simple-bench.com/"),
    ("VPCT", "https://cbrower.dev/vpct"),
    ("SpeechMap AI", "https://speechmap.ai/"),
    ("LLMs Bullshit Benchmark", "https://petergpt.github.io/bullshit-benchmark/viewer/index.html"),
    ("AI Elo", "https://aielo.co/"),
    ("ChessArena", "https://www.chessarena.ai/"),
    ("VoxelBench", "https://voxelbench.ai/"),
]

# ── Coding Benchmarks ──
AI_TOOLS["aicodebench"] = [
    ("SWEBench", "https://www.swebench.com/"),
    ("AIBenchmarks", "https://aibenchmarks.net/"),
    ("WebDev Arena", "https://arena.ai/leaderboard/webdev"),
    ("Aider Leaderboards", "https://aider.chat/docs/leaderboards/"),
    ("Vals AI", "https://www.vals.ai/"),
]

# ── AI Writing Tools ──
AI_TOOLS["aiwriting"] = [
    ("Toolbaz", "https://toolbaz.com/"),
    ("TextFX", "https://textfx.withgoogle.com/"),
    ("Rytr", "https://rytr.me/"),
    ("Dreamily", "https://dreamily.ai/"),
    ("Quarkle", "https://quarkle.ai/"),
]

# ── Video Generation ──
AI_TOOLS["aivideo"] = [
    ("VBench", "https://huggingface.co/spaces/Vchitect/VBench_Leaderboard"),
    ("GeminiGenAI", "https://geminigen.ai/app/video-gen"),
    ("Grok Imagine", "https://grok.com/imagine"),
    ("Klipy", "https://klipy.com/create/gif-maker/"),
    ("Arena", "https://arena.ai/?chat-modality=video"),
    ("Design Arena", "https://www.designarena.ai/"),
    ("Bing Create", "https://www.bing.com/images/create"),
    ("Wan AI", "https://wan.video/"),
    ("HunyuanVideo", "https://hunyuan.tencent.com/modelSquare/home/play?modelId=303&from=/visual"),
    ("Pollinations Chat", "https://pollinations-chat.vercel.app/"),
    ("Sora", "https://openai.com/index/sora/"),
    ("Meta AI", "https://www.meta.ai/"),
    ("Vheer", "https://vheer.com/"),
    ("Qwen", "https://chat.qwen.ai/"),
    ("AIFreeVideo", "https://aifreevideo.com/"),
    ("ModelScope Video", "https://modelscope.ai/civision/videoGeneration"),
    ("Dreamina", "https://dreamina.capcut.com/ai-tool/home"),
    ("Google Flow", "https://labs.google/fx/tools/flow"),
    ("PixVerse", "https://pixverse.ai/"),
    ("Genmo", "https://www.genmo.ai/"),
    ("FramePack", "https://github.com/colinurbs/FramePack-Studio"),
    ("Eggnog", "https://www.eggnog.ai/"),
    ("Pinokio", "https://pinokio.co/"),
]

# ── Image Generation ──
AI_TOOLS["aiimage"] = [
    ("Arena", "https://arena.ai/?mode=direct&chat-modality=image"),
    ("Google Flow", "https://labs.google/fx/tools/flow"),
    ("Google AI Mode", "https://google.com/aimode"),
    ("GeminiGenAI", "https://geminigen.ai/app/imagen"),
    ("Gemini", "https://gemini.google.com/"),
    ("Bing Create", "https://www.bing.com/images/create"),
    ("Dreamina", "https://dreamina.capcut.com/ai-tool/home"),
    ("Hunyuan Image", "https://hunyuan.tencent.com/visual"),
    ("Perchance", "https://perchance.org/ai-photo-generator"),
    ("Pollinations Chat", "https://pollinations-chat.vercel.app/"),
    ("Recraft", "https://www.recraft.ai/"),
    ("Reve Image", "https://app.reve.com/"),
    ("Vheer", "https://vheer.com/"),
    ("Meta AI", "https://www.meta.ai/"),
    ("Design Arena", "https://www.designarena.ai/"),
    ("OpenSourceGen", "https://opensourcegen.com/"),
    ("imgsys", "https://imgsys.org/"),
    ("Illusion Diffusion", "https://huggingface.co/spaces/AP123/IllusionDiffusion"),
    ("Mage", "https://www.mage.space/"),
    ("Grok", "https://grok.com/"),
]

# ── Image Local Frontends ──
AI_TOOLS["aiimglocal"] = [
    ("Stability Matrix", "https://lykos.ai/"),
    ("Invoke", "https://invoke-ai.github.io/InvokeAI/"),
    ("ComfyUI", "https://www.comfy.org/"),
    ("Fooocus", "https://github.com/lllyasviel/Fooocus"),
    ("Automatic1111", "https://github.com/AUTOMATIC1111/stable-diffusion-webui"),
    ("Easy Diffusion", "https://easydiffusion.github.io/"),
    ("Makeayo", "https://makeayo.com/"),
    ("biniou", "https://github.com/Woolverine94/biniou"),
    ("Dione", "https://getdione.app/"),
    ("SD WebUI Forge", "https://github.com/lllyasviel/stable-diffusion-webui-forge"),
    ("Mochi Diffusion", "https://github.com/MochiDiffusion/MochiDiffusion"),
    ("DiffusionBee", "https://diffusionbee.com/"),
]

# ── Image Guides / Tools ──
AI_TOOLS["aiimgguides"] = [
    ("Civitai", "https://civitai.com/"),
    ("A Traveler's Guide", "https://sweet-hall-e72.notion.site/A-Traveler-s-Guide-to-the-Latent-Space-85efba7e5e6a40e5bd3cae980f30235f"),
    ("ImagePromptGuru", "https://imagepromptguru.net/"),
    ("CLIP Interrogator", "https://huggingface.co/spaces/fffiloni/CLIP-Interrogator-2"),
    ("Generative AI for Beginners", "https://microsoft.github.io/generative-ai-for-beginners/"),
    ("AI Horde", "https://stablehorde.net/"),
    ("IOPaint", "https://www.iopaint.com/"),
]

# ── Audio Generation ──
AI_TOOLS["aiaudio"] = [
    ("Suno", "https://suno.com/"),
    ("Sonauto", "https://sonauto.ai/"),
    ("Pollinations Chat", "https://pollinations-chat.vercel.app/"),
    ("MusicFX", "https://labs.google/fx/tools/music-fx"),
    ("WolframTones", "https://tones.wolfram.com/"),
    ("Stable Audio", "https://www.stableaudio.com/"),
    ("MusicGen", "https://github.com/facebookresearch/audiocraft/blob/main/docs/MUSICGEN.md"),
    ("Waveformer", "https://waveformer.replicate.dev/"),
    ("SOUNDRAW", "https://soundraw.io/"),
    ("Mubert", "https://mubert.com/"),
    ("ACE-Step 1.5", "https://huggingface.co/spaces/ACE-Step/Ace-Step-v1.5"),
    ("AIVA", "https://aiva.ai/"),
    ("Boomy", "https://boomy.com/"),
    ("MusicGPT", "https://musicgpt.com/"),
    ("AI Jukebox", "https://huggingface.co/spaces/enzostvs/ai-jukebox"),
    ("Eapy", "https://home.eapy.io/"),
    ("Pack Generator", "https://output.com/products/pack-generator"),
    ("MMAudio", "https://hkchengrex.com/MMAudio/"),
]

# ── Text to Speech ──
AI_TOOLS["aitts"] = [
    ("Arena TTS", "https://arena.ai4bharat.org/#/tts"),
    ("TTS Online", "https://www.text-to-speech.online/"),
    ("Audiblez", "https://github.com/santinic/audiblez"),
    ("Ebook2Audiobook", "https://github.com/DrewThomasson/ebook2audiobook"),
    ("ElevenReader", "https://elevenreader.io/"),
    ("Google Illuminate", "https://illuminate.google.com/"),
    ("ElevenLabs", "https://elevenlabs.io/"),
    ("Pollinations Chat", "https://pollinations-chat.vercel.app/"),
    ("Google Speech Gen", "https://aistudio.google.com/generate-speech"),
    ("TTS-WebUI", "https://ttswebui.com/"),
    ("FakeYou", "https://fakeyou.com/"),
    ("Tortoise TTS", "https://github.com/neonbjb/tortoise-tts"),
    ("Bark", "https://github.com/suno-ai/bark"),
    ("TTSOpenAI", "https://ttsopenai.com/"),
    ("OpenAI.fm", "https://www.openai.fm/"),
    ("GPT-SoVITS", "https://github.com/RVC-Boss/GPT-SoVITS"),
    ("Kyutai TTS", "https://kyutai.org/next/tts"),
    ("AudioArena", "https://audioarena.ai/"),
    ("LazyPy", "https://lazypy.ro/tts/"),
    ("Kokoro TTS", "https://huggingface.co/spaces/hexgrad/Kokoro-TTS"),
    ("Ondoku", "https://ondoku3.com/en/"),
    ("Speechma", "https://speechma.com/"),
    ("Kokoro-82M", "https://huggingface.co/hexgrad/Kokoro-82M"),
    ("Chatterbox", "https://huggingface.co/spaces/ResembleAI/Chatterbox"),
    ("Audio.Z.AI", "https://audio.z.ai/"),
    ("AnyVoiceLab", "https://anyvoicelab.com/long-form-text-to-speech-converter/"),
    ("VoiceCraft", "https://github.com/jasonppy/VoiceCraft"),
    ("EmotiVoice", "https://github.com/netease-youdao/EmotiVoice"),
    ("Cartesia", "https://play.cartesia.ai/"),
    ("Fish Audio", "https://fish.audio/"),
    ("Audio-WebUI", "https://github.com/gitmylo/audio-webui"),
    ("VanillaVoice", "https://www.vanillavoice.com/"),
    ("LOVO", "https://lovo.ai/"),
    ("SoundofText", "https://soundoftext.com/"),
    ("FreeTTS", "https://freetts.com/"),
    ("Hume", "https://www.hume.ai/"),
    ("NaturalReaders", "https://www.naturalreaders.com/online/"),
    ("AIVocal", "https://aivocal.io/"),
    ("Moe TTS", "https://huggingface.co/spaces/skytnt/moe-tts"),
]

# ── Voice Change / Clone ──
AI_TOOLS["aivoiceclone"] = [
    ("Applio", "https://applio.org/"),
    ("RVC V2", "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/docs/en/README.en.md"),
    ("Voice Changer", "https://github.com/w-okada/voice-changer/blob/master/docs_i18n/README_en.md"),
    ("Voice Models", "https://voice-models.com/"),
    ("AnyVoiceLab", "https://anyvoicelab.com/voice-cloning/"),
    ("AllVoiceLab", "https://www.allvoicelab.com/"),
    ("Zyphra", "https://playground.zyphra.com/audio"),
]

# ── Voice Removal / Separation ──
AI_TOOLS["aivoiceremove"] = [
    ("MultiSong Leaderboard", "https://mvsep.com/quality_checker/multisong_leaderboard"),
    ("MVSEP", "https://mvsep.com/"),
    ("Splitter", "https://www.bandlab.com/splitter"),
    ("MDX23", "https://github.com/jarredou/MVSEP-MDX23-Colab_v2"),
    ("Music-Source-Separation", "https://github.com/jarredou/Music-Source-Separation-Training-Colab-Inference"),
    ("VocalRemover", "https://vocalremover.org/"),
    ("Audacity Effects", "https://www.audacityteam.org/download/openvino/"),
    ("Ultimate Vocal Remover", "https://colab.research.google.com/github/NaJeongMo/Colaboratory-Notebook-for-Ultimate-Vocal-Remover/blob/main/Vocal%20Remover%205_arch.ipynb"),
    ("Remove Vocals", "https://www.remove-vocals.com/"),
    ("Vocali.se", "https://vocali.se/en"),
    ("Mazmazika", "https://www.mazmazika.com/"),
    ("Ezstems", "https://ezstems.com/"),
]

# ── Machine Learning ──
AI_TOOLS["aiml"] = [
    ("Awesome Machine Learning", "https://github.com/josephmisiti/awesome-machine-learning"),
    ("Awesome ML", "https://github.com/underlines/awesome-ml"),
    ("Hugging Face", "https://huggingface.co/"),
    ("ModelScope", "https://www.modelscope.ai/"),
    ("OpenML", "https://www.openml.org/"),
    ("TensorFlow Playground", "https://playground.tensorflow.org/"),
    ("LLM Visualization", "https://bbycroft.net/llm"),
    ("LLM Course", "https://github.com/mlabonne/llm-course"),
    ("Transformer Explainer", "https://poloclub.github.io/transformer-explainer/"),
    ("Deep ML", "https://www.deep-ml.com/"),
    ("AI-For-Beginners", "https://github.com/microsoft/AI-For-Beginners"),
    ("DeepLearning.ai", "https://www.deeplearning.ai/"),
    ("Practical Deep Learning", "https://course.fast.ai/"),
    ("Unsloth", "https://github.com/unslothai/unsloth"),
    ("DeepSpeed", "https://www.deepspeed.ai/"),
    ("Netron", "https://github.com/lutzroeder/netron"),
]

_AI_PAGE_SIZE = 8   # categories per page (1 column)

_AI_MAIN_TEXT = (
    "🤖 <b><a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a> — Artificial Intelligence | Chatbots | Tools</b>\n\n"
    "Your comprehensive guide to AI chatbots, image generators, and tools "
    "curated by <a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a>.\n\n"
    "📌 <i>Select a category below to explore:</i>"
)

def _ai_main_kb(page: int = 0):
    """Build the main AI menu keyboard (1 column, paginated)."""
    start = page * _AI_PAGE_SIZE
    end = start + _AI_PAGE_SIZE
    cats = _AI_CATEGORIES[start:end]
    total_pages = (len(_AI_CATEGORIES) + _AI_PAGE_SIZE - 1) // _AI_PAGE_SIZE
    rows = []
    for key, emoji, label in cats:
        rows.append([InlineKeyboardButton(f"{emoji}  {label}", callback_data=f"ai_cat_{key}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"◀️  Page {page}", callback_data=f"ai_page_{page - 1}", style="primary"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(f"Page {page + 2}  ▶️", callback_data=f"ai_page_{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

def _AI_TOOLS_kb(cat_key: str):
    """Build keyboard for an AI category showing tools as URL buttons (1 per row)."""
    tools = AI_TOOLS.get(cat_key, [])
    rows = []
    for name, url in tools:
        rows.append([InlineKeyboardButton(f"{name} 🔗", url=url, style="success")])
    idx = next((i for i, (k, _, _) in enumerate(_AI_CATEGORIES) if k == cat_key), 0)
    page = idx // _AI_PAGE_SIZE
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"ai_page_{page}", style="primary")])
    return InlineKeyboardMarkup(rows)

async def ai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all ai_ callback queries for interactive navigation."""
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "ai_main" or data == "ai_page_0":
        await q.edit_message_text(_AI_MAIN_TEXT, reply_markup=_ai_main_kb(0),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("ai_page_"):
        try:
            page = int(data[len("ai_page_"):])
        except ValueError:
            return
        await q.edit_message_text(_AI_MAIN_TEXT, reply_markup=_ai_main_kb(page),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("ai_cat_"):
        cat_key = data[len("ai_cat_"):]
        cat = next(((k, e, l) for k, e, l in _AI_CATEGORIES if k == cat_key), None)
        if not cat:
            return
        _, emoji, label = cat
        tools = AI_TOOLS.get(cat_key, [])
        if tools:
            text = f"{emoji} <b>{label}</b>\n\n🔽 <i>Tap a tool to open it:</i>"
        else:
            text = f"{emoji} <b>{label}</b>\n\n⏳ <i>Tools coming soon…</i>"
        await q.edit_message_text(text, reply_markup=_AI_TOOLS_kb(cat_key),
                                  parse_mode="HTML", disable_web_page_preview=True)

_AI_POST_TARGET = 401

async def ai_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uname = (user.username or "").lower()
    uid = user.id
    if uname not in {"gordo"} and uid not in {7032935515}:
        await update.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "📌 Where should I post the AI menu?\n"
        "Send a Telegram link, e.g.:\n"
        "<code>https://t.me/c/3786381449/344</code>",
        parse_mode="HTML",
    )
    return _AI_POST_TARGET

async def ai_post_got_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id, topic_id = _parse_custommsg_target(url)
    if chat_id is None:
        await update.message.reply_text("❌ Invalid link. Try again or /cancel.")
        return _AI_POST_TARGET
    try:
        kwargs = dict(
            chat_id=chat_id,
            text=_AI_MAIN_TEXT,
            reply_markup=_ai_main_kb(0),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)
        await update.message.reply_text("✅ AI menu posted!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def ai_post_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ══ Downloading menu ══════════════════════════════════════════════════════════

_DL_CATEGORIES = [
    ("dldir",         "📂", "Download Directories"),
    ("dlsites",       "🌐", "Download Sites"),
    ("dlmgr",         "⬇️", "Download Managers"),
    ("dlsearch",      "🔍", "Search Sites"),
    ("dlsoftware",    "💾", "Software Sites"),
    ("dlfoss",        "🟢", "FOSS Sites"),
    ("dlfreeware",    "🆓", "Freeware Sites"),
    ("dlirc",         "💬", "IRC Tools"),
    ("dlusenet",      "📡", "Usenet"),
    ("dlindexers",    "📋", "Indexers"),
    ("dlproviders",   "🏢", "Providers"),
    ("dldownloaders", "📥", "Downloaders"),
    ("dldebrid",      "🔓", "Debrid | Leeches"),]

DL_TOOLS: dict[str, list[tuple[str, str]]] = {k: [] for k, _, _ in _DL_CATEGORIES}

DL_TOOLS["dldir"] = [
    ("r/opendirectories",          "https://www.reddit.com/r/opendirectories/"),
    ("EyeDex",                     "https://www.eyedex.org/"),
    ("ODCrawler",                  "https://odcrawler.xyz/"),
    ("mmnt",                       "https://www.mmnt.net/"),
    ("Nicotine+",                  "https://nicotine-plus.org/"),
    ("Soulseek",                   "https://slsknet.org/"),
    ("eMule Plus",                 "https://www.emule-project.com/"),
    ("Napalm FTP",                 "https://www.searchftps.net/"),
    ("Open Directory Downloader",  "https://github.com/KoalaBear84/OpenDirectoryDownloader"),
    ("DemoZoo",                    "https://demozoo.org/"),
    ("Defacto2",                   "https://defacto2.net/"),
]

DL_TOOLS["dlsites"] = [
    ("Internet Archive",           "https://archive.org/"),
    ("r/DataHoarder",              "https://reddit.com/r/DataHoarder"),
    ("Archive Team",               "https://wiki.archiveteam.org/"),
    ("MaxRelease",                 "https://max-rls.com/"),
    ("SCNLOG",                     "https://scnlog.me/"),
    ("SceneSource",                "https://www.scnsrc.me/"),
    ("WorldSRC",                   "https://www.worldsrc.net/"),
    ("DirtyWarez",                 "https://forum.dirtywarez.com/"),
    ("WarezForums",                "https://warezforums.com/"),
    ("AditHD",                     "https://www.adit-hd.com/"),
    ("Novanon",                    "https://novanon.net/"),
    ("ReleaseBB",                  "https://rlsbb.ru/"),
    ("SoftArchive",                "https://sanet.st/"),
    ("TehParadox",                 "https://www.tehparadox.net/"),
    ("downTURK",                   "https://www.downturk.net/"),
]

DL_TOOLS["dlmgr"] = [
    ("JDownloader",                "https://jdownloader.org/jdownloader2"),
    ("AB Download Manager",        "https://abdownloadmanager.com/"),
    ("Gopeed",                     "https://gopeed.com/"),
    ("Free Download Manager",      "https://www.freedownloadmanager.org/"),
    ("aria2",                      "https://aria2.github.io/"),
    ("Persepolis",                 "https://persepolisdm.github.io/"),
    ("Brisk",                      "https://github.com/BrisklyDev/brisk"),
    ("pyLoad",                     "https://pyload.net/"),
    ("Hitomi Downloader",          "https://github.com/KurtBestor/Hitomi-Downloader"),
    ("ArrowDL",                    "https://github.com/setvisible/ArrowDL/"),
    ("DownThemAll",                "https://www.downthemall.org/"),
    ("File Centipede",             "https://filecxx.com/"),
    ("HTTP Downloader",            "https://erickutcher.github.io/"),
]

DL_TOOLS["dlsearch"] = [
    ("Download CSE",               "https://cse.google.com/cse?cx=006516753008110874046:1ugcdt3vo7z"),
    ("4Shared",                    "https://www.4shared.com/"),
    ("File Host Search",           "https://cse.google.com/cse?cx=90a35b59cee2a42e1"),
    ("Linktury",                   "https://www.ddlspot.com/"),
    ("MediafireTrend",             "https://mediafiretrend.com/"),
    ("WarezOmen",                  "https://warezomen.com/"),
    ("SkullXDCC",                  "https://skullxdcc.com/"),
    ("XDCC.EU",                    "https://www.xdcc.eu/"),
]

DL_TOOLS["dlsoftware"] = [
    ("CracksURL",                  "https://cracksurl.com/"),
    ("LRepacks",                   "https://lrepacks.net/"),
    ("Mobilism",                   "https://forum.mobilism.org/"),
    ("AlternativeTo",              "https://alternativeto.net/"),
    ("European Alternatives",      "https://european-alternatives.eu/"),
    ("AIOWares",                   "https://www.aiowares.com/"),
    ("DownloadHa",                 "https://www.downloadha.com/"),
    ("Nsane Forums",               "https://www.nsaneforums.com/"),
    ("soft98",                     "https://soft98.ir/"),
    ("Virgil Software Search",     "https://virgil.samidy.com/Software/"),
    ("Software Heritage",          "https://www.softwareheritage.org/"),
    ("Rarewares",                  "https://www.rarewares.org/"),
    ("Adobe Alternatives",         "https://github.com/KenneyNL/Adobe-Alternatives"),
    ("Moum",                       "https://moum.top/en/"),
    ("Libreware",                  "https://t.me/Libreware"),
]

DL_TOOLS["dlfoss"] = [
    ("Awesome Open Source",        "https://awesomeopensource.com/"),
    ("definitive-opensource",      "https://github.com/mustbeperfect/definitive-opensource"),
    ("new(releases)",              "https://newreleases.io/"),
    ("Is It Really FOSS",          "https://isitreallyfoss.xn--com-xw0a/"),
    ("SourceForge",                "https://sourceforge.net/"),
    ("OSSSoftware",                "https://osssoftware.org/"),
    ("Fossies",                    "https://fossies.org/"),
    ("FossHub",                    "https://www.fosshub.com/"),
    ("OSS Gallery",                "https://oss.gallery/"),
    ("Opensource Builders",        "https://opensource.builders/"),
    ("OpenAlternative",            "https://openalternative.co/"),
    ("opensourcealternative.to",   "https://www.opensourcealternative.to/"),
    ("Awesome CLI Apps",           "https://github.com/toolleeo/awesome-cli-apps-in-a-csv"),
    ("Free Software Directory",    "https://directory.fsf.org/wiki/Main_Page"),
]

DL_TOOLS["dlfreeware"] = [
    ("Awesome Free Software",      "https://github.com/johnjago/awesome-free-software"),
    ("Awesome Free Apps",          "https://github.com/Axorax/awesome-free-apps"),
    ("FluentStore",                "https://github.com/yoshiask/FluentStore"),
    ("DanStore",                   "https://danstore-ms.vercel.app/"),
    ("store.rg",                   "https://store.rg-adguard.net/"),
    ("MajorGeeks",                 "https://www.majorgeeks.com/"),
    ("Softpedia",                  "https://www.softpedia.com/"),
    ("OlderGeeks",                 "https://oldergeeks.com/"),
    ("FilePuma",                   "https://www.filepuma.com/"),
    ("FileEagle",                  "https://www.fileeagle.com/"),
    ("LO4D",                       "https://www.lo4d.com/"),
    ("SoftwareOK",                 "https://www.softwareok.com/"),
    ("PortableApps.com",           "https://portableapps.com/"),
    ("Nirsoft",                    "https://www.nirsoft.net/"),
    ("VETUSWARE",                  "https://vetusware.com/"),
    ("OldVersion",                 "http://www.oldversion.com/"),
    ("SCiZE's Classic Warez",      "https://scenelist.org/"),
]

DL_TOOLS["dlirc"] = [
    ("Awesome IRC",                "https://github.com/davisonio/awesome-irc"),
    ("IRC Client Comparisons",     "https://wikipedia.org/wiki/Comparison_of_IRC_clients"),
    ("IRC Guide",                  "https://rentry.org/ircfmhyguide"),
    ("Libera Guides",              "https://libera.chat/guides/"),
    ("AdiIRC",                     "https://adiirc.com/"),
    ("KVIrc",                      "https://github.com/kvirc/KVIrc"),
    ("Halloy",                     "https://github.com/squidowl/halloy"),
    ("TheLounge",                  "https://thelounge.chat/"),
    ("Libera",                     "https://libera.chat/"),
    ("Rizon",                      "https://rizon.net/"),
]

DL_TOOLS["dlusenet"] = [
    ("Usenet Guide",               "https://docs.google.com/document/d/1TwUrRj982WlWUhrxvMadq6gdH0mPW0CGtHsTOFWprCo/mobilebasic"),
    ("Usenet-Uploaders",           "https://github.com/animetosho/Nyuu/wiki/Usenet-Uploaders"),
    ("r/usenet",                   "https://reddit.com/r/usenet"),
]

DL_TOOLS["dlindexers"] = [
    ("NZBHydra2",                  "https://github.com/theotherp/nzbhydra2"),
    ("SceneNZBs",                  "https://scenenzbs.com/"),
    ("NzbPlanet",                  "https://nzbplanet.net/"),
    ("Orion",                      "https://orionoid.com/"),
    ("binsearch",                  "https://binsearch.info/"),
    ("NZB King",                   "https://nzbking.com/"),
    ("NZB Index",                  "https://www.nzbindex.com/"),
    ("Newznab",                    "https://www.newznab.com/"),
    ("NZBStars",                   "https://nzbstars.com/"),
    ("usenet-crawler",             "https://www.usenet-crawler.com/"),
    ("GingaDaddy",                 "https://www.gingadaddy.com/"),
    ("NZBFinder",                  "https://nzbfinder.ws/"),
    ("g4u",                        "https://g4u.to/"),
    ("altHUB",                     "https://althub.co.za/"),
    ("Spotweb",                    "https://github.com/spotweb/spotweb"),
    ("Indexer List",               "https://www.reddit.com/r/usenet/wiki/indexers/"),
    ("r/UsenetInvites",            "https://reddit.com/r/UsenetInvites"),
]

DL_TOOLS["dlproviders"] = [
    ("r/usenet Providers",         "https://www.reddit.com/r/usenet/wiki/providers"),
    ("r/usenet Deals",             "https://www.reddit.com/r/usenet/wiki/providerdeals"),
    ("Usenet Provider Deals",      "https://usenet.rexum.space/deals"),
    ("Usenet Providers Map",       "https://usenet.rexum.space/tree"),
    ("Free Trials",                "https://www.ngprovider.com/free-usenet-trials.php"),
]

DL_TOOLS["dldownloaders"] = [
    ("sabnzbd",                    "https://sabnzbd.org/"),
    ("NZBUnity",                   "https://github.com/tumblfeed/nzbunity"),
    ("NZBGet",                     "https://nzbget.com/"),
    ("Usenet File Hashes",         "https://gist.github.com/4chenz/de3a3490aff19fd72e4fdd9b7dafc8f4"),
]

DL_TOOLS["dldebrid"] = [
    ("Debrid Services Comparison", "https://debridcompare.pages.dev/"),
    ("TorBox",                     "https://torbox.app/"),
    ("Real-Debrid",                "https://real-debrid.com/"),
    ("Cocoleech",                  "https://cocoleech.com/premium-link-generator"),
    ("MixDebrid",                  "https://mixdebrid.com/"),
    ("Debrid Media Manager",       "https://debridmediamanager.com/"),
    ("Multi-OCH Helper",           "https://greasyfork.org/en/scripts/13884-multi-och-helper"),
]

_DL_PAGE_SIZE = 8   # categories per page (1 column)

_DL_MAIN_TEXT = (
    "⬇️ <b><a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a> — Downloading | Software | Open Directories</b>\n\n"
    "Your comprehensive guide to download sites, software repositories, and tools "
    "curated by <a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a>.\n\n"
    "📌 <i>Select a category below to explore:</i>"
)

def _dl_main_kb(page: int = 0):
    """Build the main Downloading menu keyboard (1 column, paginated)."""
    start = page * _DL_PAGE_SIZE
    end = start + _DL_PAGE_SIZE
    cats = _DL_CATEGORIES[start:end]
    total_pages = (len(_DL_CATEGORIES) + _DL_PAGE_SIZE - 1) // _DL_PAGE_SIZE
    rows = []
    for key, emoji, label in cats:
        rows.append([InlineKeyboardButton(f"{emoji}  {label}", callback_data=f"dl_cat_{key}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"◀️  Page {page}", callback_data=f"dl_page_{page - 1}", style="primary"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(f"Page {page + 2}  ▶️", callback_data=f"dl_page_{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

def _DL_TOOLS_kb(cat_key: str):
    """Build keyboard for a DL category showing tools as URL buttons (1 per row)."""
    tools = DL_TOOLS.get(cat_key, [])
    rows = []
    for name, url in tools:
        rows.append([InlineKeyboardButton(f"{name} 🔗", url=url, style="success")])
    idx = next((i for i, (k, _, _) in enumerate(_DL_CATEGORIES) if k == cat_key), 0)
    page = idx // _DL_PAGE_SIZE
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"dl_page_{page}", style="primary")])
    return InlineKeyboardMarkup(rows)

async def dl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all dl_ callback queries for interactive navigation."""
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "dl_main" or data == "dl_page_0":
        await q.edit_message_text(_DL_MAIN_TEXT, reply_markup=_dl_main_kb(0),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("dl_page_"):
        try:
            page = int(data[len("dl_page_"):])
        except ValueError:
            return
        await q.edit_message_text(_DL_MAIN_TEXT, reply_markup=_dl_main_kb(page),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("dl_cat_"):
        cat_key = data[len("dl_cat_"):]
        cat = next(((k, e, l) for k, e, l in _DL_CATEGORIES if k == cat_key), None)
        if not cat:
            return
        _, emoji, label = cat
        tools = DL_TOOLS.get(cat_key, [])
        if tools:
            text = f"{emoji} <b>{label}</b>\n\n🔽 <i>Tap a tool to open it:</i>"
        else:
            text = f"{emoji} <b>{label}</b>\n\n⏳ <i>Tools coming soon…</i>"
        await q.edit_message_text(text, reply_markup=_DL_TOOLS_kb(cat_key),
                                  parse_mode="HTML", disable_web_page_preview=True)

_DL_POST_TARGET = 402

async def dl_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uname = (user.username or "").lower()
    uid = user.id
    if uname not in {"gordo"} and uid not in {7032935515}:
        await update.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "📌 Where should I post the Downloading menu?\n"
        "Send a Telegram link, e.g.:\n"
        "<code>https://t.me/c/3786381449/344</code>",
        parse_mode="HTML",
    )
    return _DL_POST_TARGET

async def dl_post_got_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id, topic_id = _parse_custommsg_target(url)
    if chat_id is None:
        await update.message.reply_text("❌ Invalid link. Try again or /cancel.")
        return _DL_POST_TARGET
    try:
        kwargs = dict(
            chat_id=chat_id,
            text=_DL_MAIN_TEXT,
            reply_markup=_dl_main_kb(0),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)
        await update.message.reply_text("✅ Downloading menu posted!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def dl_post_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ══ Torrenting menu ═══════════════════════════════════════════════════════════

_TR_CATEGORIES = [
    ("trsites",       "🧲", "Torrent Sites"),
    ("tragg",         "🔀", "Aggregators"),
    ("trclients",     "💻", "Torrent Clients"),
    ("trqbit",        "⚙️", "qBittorrent Tools"),
    ("trremote",      "☁️", "Remote Torrenting"),
    ("trdebrid",      "🔓", "Debrid | Leeches"),
    ("trtorrentapps", "📱", "Torrent Apps"),
    ("trprivate",     "🔒", "Private Trackers"),
    ("trhelp",        "🛠️", "Helpful Sites | Apps"),
]

TR_TOOLS: dict[str, list[tuple[str, str]]] = {k: [] for k, _, _ in _TR_CATEGORIES}

# ── Torrent Sites ──
TR_TOOLS["trsites"] = [
    ("RuTracker",        "https://rutracker.org/"),
    ("1337x",            "https://1337x.to/home/"),
    ("RARBG Dump",       "https://rarbgdump.com/"),
    ("LimeTorrents",     "https://www.limetorrents.lol/"),
    ("TorrentDownloads", "https://www.torrentdownloads.pro/"),
    ("ExtraTorrent",     "https://extratorrent.st/"),
    ("NNM-Club",         "https://nnmclub.to/"),
    ("rutor.info",       "https://rutor.is/"),
]

# ── Aggregators ──
TR_TOOLS["tragg"] = [
    ("ExT",             "https://ext.to/"),
    ("BTDigg",          "https://btdig.com/"),
    ("Knaben",          "https://knaben.org/"),
    ("TorrentProject",  "https://torrentproject.cc/"),
    ("DaMagNet",        "https://damag.net/"),
    ("TorrentDownload", "https://www.torrentdownload.info/"),
    ("snowfl",          "https://snowfl.com/"),
    ("BT4G",            "https://bt4gprx.com/"),
]

# ── Torrent Clients ──
TR_TOOLS["trclients"] = [
    ("qBittorrent",          "https://www.qbittorrent.org/"),
    ("qBittorrent Enhanced", "https://github.com/c0re100/qBittorrent-Enhanced-Edition"),
    ("Deluge",               "https://www.deluge-torrent.org/"),
    ("Transmission",         "https://transmissionbt.com/"),
    ("rTorrent",             "https://rakshasa.github.io/rtorrent/"),
    ("Tixati",               "https://tixati.com/"),
    ("BiglyBT",              "https://www.biglybt.com/"),
    ("PikaTorrent",          "https://www.pikatorrent.com/"),
]

# ── qBittorrent Tools ──
TR_TOOLS["trqbit"] = [
    ("qBit Plugins",   "https://github.com/qbittorrent/search-plugins"),
    ("qBit Themes",    "https://github.com/qbittorrent/qBittorrent/wiki/List-of-known-qBittorrent-themes"),
    ("qBit WebUIs",    "https://github.com/qbittorrent/qBittorrent/wiki/List-of-known-alternate-WebUIs"),
    ("VueTorrent",     "https://github.com/VueTorrent/VueTorrent"),
    ("qBit Manage",    "https://github.com/StuffAnThings/qbit_manage"),
    ("qBitController", "https://github.com/Bartuzen/qBitController"),
    ("Docker qBit",    "https://github.com/linuxserver/docker-qbittorrent"),
    ("Quantum",        "https://github.com/UHAXM1/Quantum"),
]

# ── Remote Torrenting (excludes Debrid | Leeches subsection) ──
TR_TOOLS["trremote"] = [
    ("TorBox",      "https://torbox.app/"),
    ("Seedr",       "https://www.seedr.cc/"),
    ("BitTorrented","https://bittorrented.com/"),
    ("webtor",      "https://webtor.io/"),
    ("Multi-Up",    "https://multiup.io/en/upload/from-torrent"),
]

# ── Debrid | Leeches (excludes Debrid Compatible Apps) ──
TR_TOOLS["trdebrid"] = [
    ("Debrid Services Comparison", "https://debridcompare.pages.dev/"),
    ("TorBox",                     "https://torbox.app/"),
    ("Real-Debrid",                "https://real-debrid.com/"),
    ("Cocoleech",                  "https://cocoleech.com/premium-link-generator"),
    ("MixDebrid",                  "https://mixdebrid.com/"),
]

# ── Torrent Apps ──
TR_TOOLS["trtorrentapps"] = [
    ("Stremio",      "https://www.stremio.com/"),
    ("PlayTorrio",   "https://playtorrio.xyz/"),
    ("Awesome *Arr", "https://ravencentric.cc/awesome-arr/"),
    ("Radarr",       "https://radarr.video/"),
    ("Sonarr",       "https://sonarr.tv/"),
    ("Prowlarr",     "https://github.com/Prowlarr/Prowlarr"),
    ("WebTorrent",   "https://webtorrent.io/"),
    ("Autobrr",      "https://github.com/autobrr/autobrr"),
]

# ── Private Trackers ──
TR_TOOLS["trprivate"] = [
    ("Private Trackers General",   "https://claraiscute.neocities.org/Guides/private-trackers"),
    ("Scene Related",              "https://opentrackers.org/links/warez-scene/#scenerelated"),
    ("TrackerStatus",              "https://trackerstatus.info/"),
    ("Tracker Pathways",           "https://trackerpathways.org/"),
    ("Private Tracker Spreadsheet","https://hdvinnie.github.io/Private-Trackers-Spreadsheet/"),
    ("r/trackers",                 "https://reddit.com/r/trackers"),
    ("OpenSignups",                "https://t.me/trackersignup"),
    ("cross-seed",                 "https://www.cross-seed.org/"),
]

# ── Helpful Sites | Apps ──
TR_TOOLS["trhelp"] = [
    ("Trackerslist",          "https://ngosang.github.io/trackerslist/"),
    ("TrackersList.com",      "https://trackerslist.com/"),
    ("newTrackon",            "https://newtrackon.com/list"),
    ("Milkie",                "https://milkie.cc/"),
    ("Scnlog",                "https://scnlog.me/"),
    ("T2M",                   "https://nutbread.github.io/t2m/"),
    ("Torrent Kitty",         "https://www.torrentkitty.tv/"),
    ("Magnet2Torrent",        "https://magnet2torrent.com/"),
    ("TorrentTags",           "https://torrenttags.com/"),
    ("Magnet Link Generator", "https://magnetlinkgenerator.com/"),
    ("PrivTracker",           "https://privtracker.com/"),
    ("PeerBanHelper",         "https://github.com/PBH-BTN/PeerBanHelper/blob/master/README.EN.md"),
]

_TR_PAGE_SIZE = 8

_TR_MAIN_TEXT = (
    "🧲 <b><a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a> — Torrenting | Clients | Sites | Trackers</b>\n\n"
    "Your comprehensive guide to torrent clients, sites, and tools "
    "curated by <a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a>.\n\n"
    "📌 <i>Select a category below to explore:</i>"
)

def _tr_main_kb(page: int = 0):
    """Build the main Torrenting menu keyboard (1 column, paginated)."""
    start = page * _TR_PAGE_SIZE
    end = start + _TR_PAGE_SIZE
    cats = _TR_CATEGORIES[start:end]
    total_pages = (len(_TR_CATEGORIES) + _TR_PAGE_SIZE - 1) // _TR_PAGE_SIZE
    rows = []
    for key, emoji, label in cats:
        rows.append([InlineKeyboardButton(f"{emoji}  {label}", callback_data=f"tr_cat_{key}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"◀️  Page {page}", callback_data=f"tr_page_{page - 1}", style="primary"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(f"Page {page + 2}  ▶️", callback_data=f"tr_page_{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

def _TR_TOOLS_kb(cat_key: str):
    """Build keyboard for a Torrenting category showing tools as URL buttons (1 per row)."""
    tools = TR_TOOLS.get(cat_key, [])
    rows = []
    for name, url in tools:
        rows.append([InlineKeyboardButton(f"{name} 🔗", url=url, style="success")])
    idx = next((i for i, (k, _, _) in enumerate(_TR_CATEGORIES) if k == cat_key), 0)
    page = idx // _TR_PAGE_SIZE
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"tr_page_{page}", style="primary")])
    return InlineKeyboardMarkup(rows)

async def tr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all tr_ callback queries for interactive navigation."""
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "tr_main" or data == "tr_page_0":
        await q.edit_message_text(_TR_MAIN_TEXT, reply_markup=_tr_main_kb(0),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("tr_page_"):
        try:
            page = int(data[len("tr_page_"):])
        except ValueError:
            return
        await q.edit_message_text(_TR_MAIN_TEXT, reply_markup=_tr_main_kb(page),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("tr_cat_"):
        cat_key = data[len("tr_cat_"):]
        cat = next(((k, e, l) for k, e, l in _TR_CATEGORIES if k == cat_key), None)
        if not cat:
            return
        _, emoji, label = cat
        tools = TR_TOOLS.get(cat_key, [])
        if tools:
            text = f"{emoji} <b>{label}</b>\n\n🔽 <i>Tap a tool to open it:</i>"
        else:
            text = f"{emoji} <b>{label}</b>\n\n⏳ <i>Tools coming soon…</i>"
        await q.edit_message_text(text, reply_markup=_TR_TOOLS_kb(cat_key),
                                  parse_mode="HTML", disable_web_page_preview=True)

_TR_POST_TARGET = 403

async def tr_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uname = (user.username or "").lower()
    uid = user.id
    if uname not in {"gordo"} and uid not in {7032935515}:
        await update.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "📌 Where should I post the Torrenting menu?\n"
        "Send a Telegram link, e.g.:\n"
        "<code>https://t.me/c/3786381449/344</code>",
        parse_mode="HTML",
    )
    return _TR_POST_TARGET

async def tr_post_got_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id, topic_id = _parse_custommsg_target(url)
    if chat_id is None:
        await update.message.reply_text("❌ Invalid link. Try again or /cancel.")
        return _TR_POST_TARGET
    try:
        kwargs = dict(
            chat_id=chat_id,
            text=_TR_MAIN_TEXT,
            reply_markup=_tr_main_kb(0),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)
        await update.message.reply_text("✅ Torrenting menu posted!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def tr_post_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ══ File Tools menu ═══════════════════════════════════════════════════════════

_FT_CATEGORIES = [
    ("ftscan",      "🛡️", "File Scanners"),
    ("fttools",     "🧰", "File Tools"),
    ("ftdl",        "⬇️", "Download Managers"),
    ("ftarch",      "🗜️", "Archiving | Compression"),
    ("ftconv",      "🔄", "File Converters"),
    ("ftmgr",       "🗂️", "File Managers"),
    ("ftsearch",    "🔎", "File Searching"),
    ("ftencrypt",   "🔐", "File Encryption"),
    ("ftsync",      "🔁", "File Sync"),
    ("ftbackup",    "💾", "File Backup"),
    ("ftrecover",   "🩹", "File Recovery"),
    ("ftmeta",      "🏷️", "File Info | Metadata"),
    ("ftformat",    "🧹", "Formatting | Deletion"),
    ("ftauto",      "🤖", "Data Automation"),
    ("ftpdf",       "📄", "PDF Tools"),
    ("ftpdfonline", "🌐", "Online PDF Toolkits"),
    ("ftpdfoff",    "💻", "Offline PDF Toolkits"),
    ("ftpdfconv",   "📤", "PDF Conversion Tools"),
    ("fttransfer",  "📨", "File Transfer"),
    ("ftp2p",       "🧲", "P2P Transfer"),
    ("fthosts",     "🗄️", "File Hosts"),
    ("ftcloud",     "☁️", "Cloud Storage"),
    ("ftcloudmgr",  "🧭", "Cloud Managers"),
    ("ftgdrive",    "🟢", "Google Drive Tools"),
    ("ftmega",      "🔴", "MEGA Tools"),
]

FT_TOOLS: dict[str, list[tuple[str, str]]] = {k: [] for k, _, _ in _FT_CATEGORIES}

FT_TOOLS["ftscan"] = [
    ("The Second Opinion", "https://jijirae.github.io/thesecondopinion/index.html"),
    ("VirusTotal", "https://www.virustotal.com/"),
    ("Hybrid Analysis", "https://hybrid-analysis.com/"),
    ("Microsoft Safety Scanner", "https://learn.microsoft.com/en-us/defender-endpoint/safety-scanner-download"),
    ("Jotti", "https://virusscan.jotti.org/en"),
    ("MetaDefender Cloud", "https://metadefender.com/"),
    ("Filescan.io", "https://www.filescan.io/"),
    ("Farbar", "https://www.bleepingcomputer.com/download/farbar-recovery-scan-tool/"),
]

FT_TOOLS["fttools"] = [
    ("czkawka", "https://github.com/qarmin/czkawka"),
    ("dupeGuru", "https://dupeguru.voltaicideas.net/"),
    ("UnLock IT", "https://emcosoftware.com/unlock-it/download"),
    ("Lock Hunter", "https://lockhunter.com/"),
    ("Icaros", "https://github.com/Xanashi/Icaros"),
    ("copyparty", "https://github.com/9001/copyparty/"),
    ("hfs", "https://rejetto.com/hfs/"),
    ("File-Examples", "https://file-examples.com/"),
]

FT_TOOLS["ftdl"] = [
    ("JDownloader", "https://jdownloader.org/jdownloader2"),
    ("AB Download Manager", "https://abdownloadmanager.com/"),
    ("Go Speed", "https://gopeed.com/"),
    ("Brisk", "https://github.com/BrisklyDev/brisk"),
    ("Free Download Manager", "https://www.freedownloadmanager.org/"),
    ("aria2", "https://aria2.github.io/"),
    ("pyLoad", "https://pyload.net/"),
    ("HTTP Downloader", "https://erickutcher.github.io/"),
]

FT_TOOLS["ftarch"] = [
    ("7-Zip", "https://www.7-zip.org/"),
    ("NanaZip", "https://github.com/M2Team/NanaZip"),
    ("PeaZip", "https://peazip.github.io/"),
    ("WinRAR", "https://www.win-rar.com/"),
    ("CompactGUI", "https://github.com/IridiumIO/CompactGUI"),
    ("Efficient Compression Tool", "https://github.com/fhanau/Efficient-Compression-Tool"),
    ("UPX", "https://upx.github.io/"),
    ("TurboBench", "https://github.com/powturbo/TurboBench"),
]

FT_TOOLS["ftconv"] = [
    ("File Converter", "https://file-converter.io/"),
    ("VERT", "https://vert.sh/"),
    ("Convert to it!", "https://p2r3.github.io/convert/"),
    ("Aconvert", "https://www.aconvert.com/"),
    ("Pandoc", "https://pandoc.org/"),
    ("Shutter Encoder", "https://www.shutterencoder.com/"),
    ("FreeConvert", "https://www.freeconvert.com/"),
    ("ConvertX", "https://github.com/C4illin/ConvertX"),
]

FT_TOOLS["ftmgr"] = [
    ("DoubleCMD", "https://doublecmd.sourceforge.io/"),
    ("Sigma", "https://sigma-file-manager.vercel.app/"),
    ("Yazi", "https://yazi-rs.github.io/"),
    ("Files", "https://files.community/"),
    ("Explorer++", "https://explorerplusplus.com/"),
    ("Total Commander", "https://www.ghisler.com/"),
    ("Vifm", "https://vifm.info/"),
    ("FileBrowser Quantum", "https://filebrowserquantum.com/"),
]

FT_TOOLS["ftsearch"] = [
    ("Everything", "https://voidtools.com/"),
    ("Recoll", "https://www.recoll.org/"),
    ("DocFetcher", "https://docfetcher.sourceforge.io/"),
    ("AnyTXT", "https://anytxt.net/"),
    ("WizFile", "https://antibody-software.com/wizfile/"),
    ("dnGrep", "https://dngrep.github.io/"),
    ("fd", "https://github.com/sharkdp/fd"),
    ("sist2", "https://github.com/simon987/sist2"),
]

FT_TOOLS["ftencrypt"] = [
    ("Cryptomator", "https://cryptomator.org/"),
    ("VeraCrypt", "https://www.veracrypt.fr/en/Home.html"),
    ("age", "https://github.com/FiloSottile/age"),
    ("REM", "https://github.com/liriliri/rem"),
    ("Picocrypt-NG", "https://github.com/Picocrypt-NG/Picocrypt-NG"),
    ("gocryptfs", "https://github.com/bailey27/cppcryptfs"),
    ("Kryptor", "https://www.kryptor.co.uk/"),
    ("Tahoe-LAFS", "https://tahoe-lafs.org/trac/tahoe-lafs"),
]

FT_TOOLS["ftsync"] = [
    ("SyncThing", "https://syncthing.net/"),
    ("FreeFileSync", "https://freefilesync.org/"),
    ("Resilio", "https://www.resilio.com/individuals/"),
    ("TangoShare", "https://tangoshare.com/"),
    ("rsync", "https://rsync.samba.org/"),
    ("Unison", "https://github.com/bcpierce00/unison"),
    ("allwaysync", "https://allwaysync.com/"),
    ("SmartFTP", "https://www.smartftp.com/"),
]

FT_TOOLS["ftbackup"] = [
    ("restic", "https://restic.net/"),
    ("Kopia", "https://kopia.io/"),
    ("Rescuezilla", "https://rescuezilla.com/"),
    ("CloneZilla", "https://clonezilla.org/"),
    ("UrBackup", "https://www.urbackup.org/"),
    ("Duplicati", "https://www.duplicati.com/"),
    ("Borg", "https://www.borgbackup.org/"),
    ("USBImager", "https://bztsrc.gitlab.io/usbimager/"),
]

FT_TOOLS["ftrecover"] = [
    ("Data Recovery Wiki", "https://igwiki.lyci.de/wiki/Data_recovery"),
    ("TestDisk", "https://www.cgsecurity.org/wiki/TestDisk"),
    ("PhotoRec", "https://www.cgsecurity.org/wiki/PhotoRec"),
    ("DMDE", "https://dmde.com/download.html"),
    ("Windows File Recovery", "https://apps.microsoft.com/detail/9n26s50ln705"),
    ("MultiPar", "https://github.com/Yutaka-Sawada/MultiPar"),
    ("ShadowExplorer", "https://www.shadowexplorer.com/"),
    ("ShadowCopyView", "https://www.nirsoft.net/utils/shadow_copy_view.html/"),
]

FT_TOOLS["ftmeta"] = [
    ("Fileinfo", "https://fileinfo.com/"),
    ("Filext", "https://filext.com/"),
    ("MediaInfo", "https://mediaarea.net/en/MediaInfo"),
    ("Metadata2Go", "https://www.metadata2go.com/"),
    ("PrivMeta", "https://www.privmeta.com/"),
    ("mat2", "https://github.com/jvoisin/mat2"),
    ("OpenHashTab", "https://github.com/namazso/OpenHashTab"),
    ("TagSpaces", "https://www.tagspaces.org/"),
]

FT_TOOLS["ftformat"] = [
    ("SDelete", "https://learn.microsoft.com/en-us/sysinternals/downloads/sdelete"),
    ("Eraser", "https://eraser.heidi.ie/"),
    ("File Shredder", "https://fileshredder.org/"),
    ("Permadelete", "https://developerstree.github.io/permadelete/"),
    ("SSuite File Shredder", "https://www.ssuiteoffice.com/software/ssuitefileshredder.htm"),
    ("Low Level Format", "https://www.lowlevelformat.info/"),
    ("ShredOS", "https://github.com/PartialVolume/shredos.x86_64"),
    ("RED", "https://www.jonasjohn.de/red.htm"),
]

FT_TOOLS["ftauto"] = [
    ("Advanced Renamer", "https://www.advancedrenamer.com/"),
    ("PowerRename", "https://learn.microsoft.com/en-us/windows/powertoys/powerrename"),
    ("Bulk Rename Utility", "https://www.bulkrenameutility.co.uk/"),
    ("FoliCon", "https://dineshsolanki.github.io/FoliCon/"),
    ("FileBot", "https://www.filebot.net/"),
    ("tinyMediaManager", "https://www.tinymediamanager.org/"),
    ("TVRename", "https://www.tvrename.com/"),
    ("Shoko", "https://github.com/shokoanime"),
]

FT_TOOLS["ftpdf"] = [
    ("PDFGrep", "https://pdfgrep.org/"),
    ("OCRmyPDF", "https://github.com/ocrmypdf/OCRmyPDF"),
    ("PDFEncrypt", "https://pdfencrypt.net/"),
    ("PDF Fixer", "https://pdffixer.com/"),
    ("OpenSign", "https://github.com/OpenSignLabs/OpenSign"),
    ("Adobe Sign", "https://www.adobe.com/acrobat/online/sign-pdf.html"),
    ("Google Drive PDF Downloader", "https://github.com/zeltox/Google-Drive-PDF-Downloader"),
    ("PrintFriendly", "https://www.printfriendly.com/"),
]

FT_TOOLS["ftpdfonline"] = [
    ("BentoPDF", "https://bentopdf.com/"),
    ("PDFCraft", "https://pdfcraft.devtoolcafe.com/"),
    ("BreezePDF", "https://breezepdf.com/"),
    ("Sejda", "https://www.sejda.com/"),
    ("ILovePDF", "https://www.ilovepdf.com/"),
    ("PDF2Go", "https://www.pdf2go.com/"),
    ("DPDF", "https://dpdf.com/"),
    ("Digiparser", "https://www.digiparser.com/free-tools/pdf"),
]

FT_TOOLS["ftpdfoff"] = [
    ("Stirling-PDF", "https://www.stirlingpdf.com/"),
    ("PDF24", "https://www.pdf24.org/"),
    ("PDF4QT", "https://jakubmelka.github.io/"),
    ("Foxit", "https://www.foxit.com/pdf-reader/"),
    ("xPDFReader", "https://www.xpdfreader.com/"),
    ("PDF Arranger", "https://github.com/pdfarranger/pdfarranger"),
]

FT_TOOLS["ftpdfconv"] = [
    ("online2pdf", "https://online2pdf.com/"),
    ("Rare2PDF", "https://rare2pdf.com/"),
    ("2PDFConverter", "https://www.2pdfconverter.com/"),
    ("MD2PDF", "https://md2pdf.netlify.app/"),
    ("Marker", "https://github.com/VikParuchuri/marker"),
    ("wkhtmltopdf", "https://wkhtmltopdf.org/"),
    ("WebToPDF", "https://webtopdf.com/"),
    ("Dangerzone", "https://dangerzone.rocks/"),
]

FT_TOOLS["fttransfer"] = [
    ("LocalSend", "https://localsend.org/"),
    ("Blip", "https://blip.net/"),
    ("KDE Connect", "https://kdeconnect.kde.org/"),
    ("Wormhole", "https://wormhole.app/"),
    ("Warpinator", "https://github.com/linuxmint/warpinator"),
    ("Magic Wormhole", "https://github.com/magic-wormhole/magic-wormhole"),
    ("croc", "https://github.com/schollz/croc"),
    ("OnionShare", "https://onionshare.org/"),
]

FT_TOOLS["ftp2p"] = [
    ("PairDrop", "https://pairdrop.net/"),
    ("JustBeamIt", "https://justbeamit.com/"),
    ("Surge", "https://getsurge.io/"),
    ("ToffeeShare", "https://toffeeshare.com/"),
    ("Station307", "https://www.station307.com/"),
    ("new.space", "https://new.space/"),
    ("WebWormhole", "https://webwormhole.io/"),
]

FT_TOOLS["fthosts"] = [
    ("Gofile", "https://gofile.io/"),
    ("Pixeldrain", "https://pixeldrain.com/"),
    ("VikingFile", "https://vikingfile.com/"),
    ("Rootz", "https://rootz.so/"),
    ("Buzzheavier", "https://buzzheavier.com/"),
    ("Catbox", "https://catbox.moe/"),
    ("Send.now", "https://send.now/"),
    ("SwissTransfer", "https://www.swisstransfer.com/"),
]

FT_TOOLS["ftcloud"] = [
    ("Google Drive", "https://drive.google.com/"),
    ("MEGA", "https://mega.io/"),
    ("Filen", "https://filen.io/"),
    ("Dropbox", "https://www.dropbox.com/"),
    ("mediafire", "https://www.mediafire.com/"),
    ("pCloud", "https://www.pcloud.com/"),
    ("Proton Drive", "https://proton.me/drive"),
    ("Blomp", "https://www.blomp.com/"),
]

FT_TOOLS["ftcloudmgr"] = [
    ("Rclone", "https://rclone.org/"),
    ("gclone", "https://github.com/dogbutcat/gclone"),
    ("Air Explorer", "https://airexplorer.net/en/"),
    ("RaiDrive", "https://www.raidrive.com/"),
    ("Cyberduck", "https://cyberduck.io/"),
    ("SpaceDrive", "https://www.spacedrive.com/"),
    ("OpenList", "https://github.com/OpenListTeam/OpenList"),
    ("MultCloud", "https://www.multcloud.com/"),
]

FT_TOOLS["ftgdrive"] = [
    ("OneClickRun", "https://colab.research.google.com/github/biplobsd/OneClickRun/blob/master/OneClickRun.ipynb"),
    ("DriveUploader", "https://driveuploader.com/"),
    ("ZIP Extractor", "https://zipextractor.app/"),
    ("Google Drive CLI", "https://github.com/glotlabs/gdrive"),
    ("goodls", "https://github.com/tanaikech/goodls"),
    ("gdrivedl", "https://github.com/matthuisman/gdrivedl"),
    ("goindex-extended", "https://github.com/menukaonline/goindex-extended"),
    ("Google Drive Clone Bot", "https://jsmsj.github.io/GdriveCloneBot/"),
]

FT_TOOLS["ftmega"] = [
    ("Megabasterd", "https://github.com/tonikelope/megabasterd"),
    ("MEGA Desktop", "https://mega.io/desktop"),
    ("MEGA CMD", "https://mega.io/cmd"),
]

_FT_PAGE_SIZE = 8

_FT_MAIN_TEXT = (
    "📁 <b><a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a> — File Tools | PDF | Transfer | Cloud</b>\n\n"
    "Your comprehensive guide to file utilities, managers, converters, PDF tools, "
    "transfer tools, and cloud resources curated by <a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a>.\n\n"
    "📌 <i>Select a category below to explore:</i>"
)

def _ft_main_kb(page: int = 0):
    """Build the main File Tools menu keyboard (1 column, paginated)."""
    start = page * _FT_PAGE_SIZE
    end = start + _FT_PAGE_SIZE
    cats = _FT_CATEGORIES[start:end]
    total_pages = (len(_FT_CATEGORIES) + _FT_PAGE_SIZE - 1) // _FT_PAGE_SIZE
    rows = []
    for key, emoji, label in cats:
        rows.append([InlineKeyboardButton(f"{emoji}  {label}", callback_data=f"ft_cat_{key}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"◀️  Page {page}", callback_data=f"ft_page_{page - 1}", style="primary"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(f"Page {page + 2}  ▶️", callback_data=f"ft_page_{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

def _FT_TOOLS_kb(cat_key: str):
    """Build keyboard for a File Tools category showing tools as URL buttons (1 per row)."""
    tools = FT_TOOLS.get(cat_key, [])
    rows = []
    for name, url in tools:
        rows.append([InlineKeyboardButton(f"{name} 🔗", url=url, style="success")])
    idx = next((i for i, (k, _, _) in enumerate(_FT_CATEGORIES) if k == cat_key), 0)
    page = idx // _FT_PAGE_SIZE
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"ft_page_{page}", style="primary")])
    return InlineKeyboardMarkup(rows)

async def ft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all ft_ callback queries for interactive navigation."""
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "ft_main" or data == "ft_page_0":
        await q.edit_message_text(_FT_MAIN_TEXT, reply_markup=_ft_main_kb(0),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("ft_page_"):
        try:
            page = int(data[len("ft_page_"):])
        except ValueError:
            return
        await q.edit_message_text(_FT_MAIN_TEXT, reply_markup=_ft_main_kb(page),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("ft_cat_"):
        cat_key = data[len("ft_cat_"):]
        cat = next(((k, e, l) for k, e, l in _FT_CATEGORIES if k == cat_key), None)
        if not cat:
            return
        _, emoji, label = cat
        tools = FT_TOOLS.get(cat_key, [])
        if tools:
            text = f"{emoji} <b>{label}</b>\n\n🔽 <i>Tap a tool to open it:</i>"
        else:
            text = f"{emoji} <b>{label}</b>\n\n⏳ <i>Tools coming soon…</i>"
        await q.edit_message_text(text, reply_markup=_FT_TOOLS_kb(cat_key),
                                  parse_mode="HTML", disable_web_page_preview=True)

_FT_POST_TARGET = 404

async def ft_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uname = (user.username or "").lower()
    uid = user.id
    if uname not in {"gordo"} and uid not in {7032935515}:
        await update.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "📌 Where should I post the File Tools menu?\n"
        "Send a Telegram link, e.g.:\n"
        "<code>https://t.me/c/3786381449/344</code>",
        parse_mode="HTML",
    )
    return _FT_POST_TARGET

async def ft_post_got_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id, topic_id = _parse_custommsg_target(url)
    if chat_id is None:
        await update.message.reply_text("❌ Invalid link. Try again or /cancel.")
        return _FT_POST_TARGET
    try:
        kwargs = dict(
            chat_id=chat_id,
            text=_FT_MAIN_TEXT,
            reply_markup=_ft_main_kb(0),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)
        await update.message.reply_text("✅ File Tools menu posted!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def ft_post_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ══ Internet Tools menu ══════════════════════════════════════════════════════

_IT_CATEGORIES = [
    ("itnet",       "🌐", "Network Tools"),
    ("itpass",      "🔑", "Password Managers"),
    ("itpay",       "🧱", "Paywall Bypass"),
    ("itlinkbio",   "🔗", "Link in Bio"),
    ("itcaptcha",   "🧩", "Captcha Tools"),
    ("itchat",      "💬", "Chat Tools"),
    ("itqr",        "📷", "QR Code Tools"),
    ("itrss",       "📰", "RSS Tools"),
    ("itrssread",   "📖", "RSS Readers"),
    ("itrssgen",    "⚙️", "RSS Feed Generators"),
    ("itsearch",    "🔎", "Search Tools"),
    ("itengines",   "🧭", "Search Engines"),
    ("itcse",       "🧠", "Custom Search Engines"),
    ("itgoogle",    "🟦", "Google Search Tools"),
    ("iturl",       "🧷", "URL Tools"),
    ("itredir",     "↪️", "Redirect Bypass"),
    ("itshort",     "🪢", "Short Link Tools"),
    ("itshorteners", "✂️", "URL Shorteners"),
    ("itdown",      "📉", "Down Site Checkers"),
    ("itemail",     "📧", "Email Tools"),
    ("ittemp",      "🕓", "Temp Mail"),
    ("italias",     "🎭", "Email Aliasing"),
    ("itwebpriv",   "🛡️", "Web Privacy"),
    ("itpasspriv",  "🔐", "Password Privacy | 2FA"),
    ("itencryptmsg", "🔒", "Encrypted Messengers"),
    ("itemailpriv", "📮", "Email Privacy"),
    ("itbreach",    "🚨", "Data Breach Monitoring"),
    ("itfinger",    "🫥", "Fingerprinting | Tracking"),
    ("itvpn",       "🛰️", "VPN"),
    ("itproxy",     "🧱", "Proxy"),
    ("itanticensor", "🚧", "Anti Censorship"),
    ("itgfw",       "🇨🇳", "Great Firewall Bypass"),
    ("itvpncfg",    "🧾", "Free VPN Configs"),
    ("itproxylist", "📃", "Proxy Lists"),
    ("itimgsearch", "🖼️", "Image Search Engines"),
    ("itreddit",    "👽", "Reddit Search Tools"),
    ("itdiscord",   "🎮", "Discord Tools"),
    ("ittelegram",  "✈️", "Telegram Tools"),
]

IT_TOOLS: dict[str, list[tuple[str, str]]] = {k: [] for k, _, _ in _IT_CATEGORIES}

IT_TOOLS["itnet"] = [
    ("fast", "https://fast.com/"),
    ("Cloudflare Speed Test", "https://speed.cloudflare.com/"),
    ("LibreSpeed", "https://librespeed.org/"),
    ("Pinging", "https://www.pinging.net/"),
    ("GlobalPing", "https://globalping.io/"),
    ("MxToolbox", "https://mxtoolbox.com/NetworkTools.aspx"),
    ("WireShark", "https://www.wireshark.org/"),
    ("PortChecker", "https://portchecker.co/"),
]

IT_TOOLS["itpass"] = [
    ("Bitwarden", "https://bitwarden.com/"),
    ("KeePass", "https://keepass.info/"),
    ("KeePassXC", "https://keepassxc.org/"),
    ("Proton Pass", "https://proton.me/pass"),
    ("Pashword", "https://pashword.app/"),
    ("LessPass", "https://lesspass.com/"),
    ("Buttercup", "https://buttercup.pw/"),
    ("VaultWarden", "https://github.com/dani-garcia/vaultwarden"),
]

IT_TOOLS["itpay"] = [
    ("Bypass Paywalls Clean", "https://gitflic.ru/project/magnolia1234/bpc_uploads"),
    ("Freedium", "https://freedium-mirror.cfd/"),
    ("ReadMedium", "https://readmedium.com/"),
    ("PaywallBuster", "https://paywallbuster.com/"),
    ("ByeByePaywall", "https://byebyepaywall.com/en/"),
    ("RemovePaywalls", "https://removepaywalls.com/"),
    ("smry.ai", "https://smry.ai/"),
    ("Unpaywall", "https://unpaywall.org/"),
]

IT_TOOLS["itlinkbio"] = [
    ("Linktree", "https://linktr.ee/"),
    ("Beacons", "https://beacons.ai/"),
    ("Carrd", "https://carrd.co/"),
    ("Koji", "https://withkoji.com/"),
    ("about.me", "https://about.me/"),
]

IT_TOOLS["itcaptcha"] = [
    ("Buster", "https://github.com/dessant/buster"),
    ("NopeCHA", "https://nopecha.com/"),
    ("Privacy Pass", "https://github.com/cloudflare/pp-browser-extension"),
    ("Democaptcha", "https://democaptcha.com/demo-form-eng/hcaptcha.html"),
    ("reCAPTCHA Demo", "https://www.google.com/recaptcha/api2/demo"),
]

IT_TOOLS["itchat"] = [
    ("Discord Tools", "https://fmhy.net/social-media-tools/#discord-tools"),
    ("Telegram Tools", "https://fmhy.net/social-media-tools/#telegram-tools"),
    ("Privacy-Focused Messengers", "https://fmhy.net/privacy/#encrypted-messengers"),
    ("Stoat", "https://stoat.chat/"),
    ("Mumble", "https://www.mumble.info/"),
    ("Hack.chat", "https://hack.chat/"),
    ("Ferdium", "https://ferdium.org/"),
    ("MatterBridge", "https://github.com/42wim/matterbridge"),
]

IT_TOOLS["itqr"] = [
    ("Mini QR", "https://mini-qr-code-generator.vercel.app/"),
    ("QArt Coder", "https://research.swtch.com/qr/draw/"),
    ("QRcodly", "https://www.qrcodly.de/"),
    ("QRCode Monkey", "https://www.qrcode-monkey.com/"),
    ("FreeQRApp", "https://freeqrapp.com/"),
    ("2QR", "https://2qr.info/"),
    ("barcodrod.io", "https://barcodrod.io/"),
]

IT_TOOLS["itrss"] = [
    ("All about RSS", "https://github.com/AboutRSS/ALL-about-RSS"),
    ("RSSTango", "https://rentry.org/rrstango"),
    ("FeedButler", "https://feedbutler.app/en"),
    ("siftrss", "https://siftrss.com/"),
    ("RSS.app", "https://rss.app/"),
    ("Kill the Newsletter", "https://kill-the-newsletter.com/"),
]

IT_TOOLS["itrssread"] = [
    ("Feedbro", "https://nodetics.com/feedbro/"),
    ("Brief", "https://github.com/brief-rss/brief"),
    ("Fluent Reader", "https://hyliu.me/fluent-reader/"),
    ("Feed Flow", "https://www.feedflow.dev/"),
    ("yarr", "https://github.com/nkanaev/yarr"),
    ("NewsBlur", "https://www.newsblur.com/"),
    ("WebFeed", "https://taoshu.in/webfeed/turn-browser-into-feed-reader.html"),
]

IT_TOOLS["itrssgen"] = [
    ("RSS Bridge", "https://rss-bridge.org/bridge01/"),
    ("MoRSS", "https://morss.it/"),
    ("RSSHub", "https://github.com/DIYgod/RSSHub"),
    ("Open RSS", "https://openrss.org/"),
    ("RSS Finder", "https://rss-finder.rook1e.com/"),
    ("FetchRSS", "https://fetchrss.com/"),
    ("PolitePol", "https://politepaul.com/en//"),
    ("FiveFilters", "https://createfeed.fivefilters.org/"),
]

IT_TOOLS["itsearch"] = [
    ("SimilarSiteSearch", "https://www.similarsitesearch.com/"),
    ("AIO Search", "https://www.aiosearch.com/"),
    ("UserSearch", "https://usersearch.com/"),
    ("Sherlock", "https://github.com/sherlock-project/sherlock"),
    ("Maigret", "https://github.com/soxoj/maigret"),
    ("Intelligence X", "https://intelx.io/tools"),
    ("Shodan", "https://www.shodan.io/"),
    ("FOFA", "https://fofa.info/"),
]

IT_TOOLS["itengines"] = [
    ("Google", "https://google.com/"),
    ("Lycos", "https://www.lycos.com/"),
    ("WebCrawler", "https://www.webcrawler.com/"),
    ("Andi", "https://andisearch.com/"),
    ("Yandex", "https://yandex.com/"),
    ("Yahoo", "https://www.yahoo.com/"),
    ("AOL", "https://search.aol.com/"),
    ("All the Internet", "https://www.alltheinternet.com/"),
]

IT_TOOLS["itcse"] = [
    ("CSE Utopia", "https://start.me/p/EL84Km/cse-utopia"),
    ("Awesome CSEs", "https://github.com/davzoku/awesome-custom-search-engines"),
    ("Virgil Game Search", "https://virgil.samidy.com/Games/"),
    ("TV Streaming CSE", "https://cse.google.com/cse?cx=006516753008110874046:hrhinud6efg"),
    ("Torrent CSE", "https://cse.google.com/cse?cx=006516753008110874046:0led5tukccj"),
    ("Reading CSE", "https://cse.google.com/cse?cx=006516753008110874046:s9ddesylrm8"),
    ("Extensions CSE", "https://cse.google.com/cse?cx=86d64a73544824102"),
    ("Telegago", "https://cse.google.com/cse?&cx=006368593537057042503:efxu7xprihg#gsc.tab=0"),
]

IT_TOOLS["itgoogle"] = [
    ("Google Images Tools Enhanced", "https://greasyfork.org/en/scripts/537524"),
    ("View Image", "https://github.com/bijij/ViewImage"),
    ("Show Image Dimensions", "https://greasyfork.org/scripts/401432"),
    ("Google DWIMages", "https://greasyfork.org/en/scripts/29420"),
    ("Endless Google", "https://openuserjs.org/scripts/tumpio/Endless_Google"),
    ("Google Bangs", "https://greasyfork.org/en/scripts/424160"),
    ("DisableAMP", "https://github.com/AdguardTeam/DisableAMP"),
]

IT_TOOLS["iturl"] = [
    ("HTTPStatus", "https://httpstatus.io/"),
    ("lychee", "https://lychee.cli.rs/"),
    ("ChangeDetection.io", "https://github.com/dgtlmoon/changedetection.io"),
    ("Linkify Plus Plus", "https://greasyfork.org/scripts/4255"),
    ("Open Bulk URL", "https://openbulkurl.com/"),
    ("Link Lock", "https://rekulous.github.io/link-lock/"),
    ("XML-Sitemaps", "https://www.xml-sitemaps.com/"),
]

IT_TOOLS["itredir"] = [
    ("Bypass All Shortlinks Debloated", "https://codeberg.org/gongchandang49/bypass-all-shortlinks-debloated"),
    ("Evade", "https://skipped.lol/evade/evade.user.js"),
    ("Bypass.vip", "https://bypass.vip/"),
    ("Zen Bypass", "https://izen.lol/"),
    ("RIP Linkvertise", "https://rip.linkvertise.lol/"),
    ("bypass.tools", "https://bypass.tools/"),
    ("bypass.link", "https://bypass.link/"),
    ("Adsbypasser", "https://adsbypasser.github.io/"),
]

IT_TOOLS["itshort"] = [
    ("WhereGoes", "https://wheregoes.com/"),
    ("Redirect Detective", "https://redirectdetective.com/"),
    ("URL Expander", "https://t.ly/tools/link-expander/"),
    ("CheckShortURL", "https://checkshorturl.com/"),
    ("ExpandURL", "https://www.expandurl.net/"),
    ("TrueURL", "https://trueurl.com/"),
    ("Unshorten.me", "https://unshorten.me/"),
]

IT_TOOLS["itshorteners"] = [
    ("AI6", "https://ai6.net/"),
    ("Kutt", "https://kutt.it/"),
    ("Anon.to", "https://anon.to/"),
    ("Thinfi", "https://thinfi.com/"),
    ("Wikimedia Shortener", "https://meta.wikimedia.org/wiki/Special:UrlShortener"),
]

IT_TOOLS["itdown"] = [
    ("Down For Everyone Or Just Me", "https://downforeveryoneorjustme.com/"),
    ("Is It Down Right Now", "https://www.isitdownrightnow.com/"),
    ("Down.com", "https://down.com/"),
    ("StatusGator", "https://statusgator.com/"),
    ("IsItWP", "https://www.isitwp.com/"),
]

IT_TOOLS["itemail"] = [
    ("Email Providers", "https://wikipedia.org/wiki/Comparison_of_webmail_providers"),
    ("Email Privacy Services | Tools", "https://fmhy.net/privacy/#email-privacy"),
    ("InboxReads", "https://inboxreads.co/"),
    ("Readsom", "https://readsom.com/"),
    ("Delta Chat", "https://delta.chat/"),
    ("Useplaintext", "https://useplaintext.email/"),
    ("Got Your Back", "https://github.com/GAM-team/got-your-back"),
]

IT_TOOLS["ittemp"] = [
    ("SmailPro", "https://smailpro.com/temporary-email"),
    ("Zemail", "https://zemail.me/"),
    ("Gmailnator", "https://emailnator.com/"),
    ("Tempr.email", "https://tempr.email/en/"),
    ("Mail.tm", "https://mail.tm/"),
    ("temp-mail.org", "https://temp-mail.org/"),
    ("YOPmail", "https://yopmail.com/email-generator"),
    ("10minemail.com", "https://10minemail.com/"),
]

IT_TOOLS["italias"] = [
    ("SimpleLogin", "https://simplelogin.io/"),
    ("Mailgw", "https://mailgw.com/"),
    ("erine.email", "https://erine.email/"),
    ("33mail", "https://33mail.com/"),
    ("TrashMail", "https://trashmail.com/"),
    ("AdGuard Mail", "https://adguard-mail.com/"),
]

IT_TOOLS["itwebpriv"] = [
    ("Web Privacy", "https://fmhy.net/privacy/#web-privacy"),
    ("Browser Privacy", "https://fmhy.net/privacy/#browser-privacy"),
    ("Search Engines (Privacy)", "https://fmhy.net/privacy/#search-engines"),
    ("Privacy Guides", "https://www.privacyguides.org/"),
    ("JustDeleteMe", "https://justdeleteme.xyz/"),
]

IT_TOOLS["itpasspriv"] = [
    ("Password Privacy | 2FA", "https://fmhy.net/privacy/#password-privacy-2fa"),
    ("2FA Directory", "https://2fa.directory/"),
    ("Ente Auth", "https://ente.io/auth/"),
    ("Aegis", "https://getaegis.app/"),
    ("2FAS", "https://2fas.com/"),
    ("KeePassXC", "https://keepassxc.org/"),
]

IT_TOOLS["itencryptmsg"] = [
    ("Encrypted Messengers", "https://fmhy.net/privacy/#encrypted-messengers"),
    ("SimpleX", "https://simplex.chat/"),
    ("Signal", "https://signal.org/"),
    ("Session", "https://getsession.org/"),
    ("Briar", "https://briarproject.org/"),
    ("Matrix Clients", "https://matrix.org/ecosystem/clients/"),
]

IT_TOOLS["itemailpriv"] = [
    ("Email Privacy", "https://fmhy.net/privacy/#email-privacy"),
    ("Proton Mail", "https://proton.me/mail"),
    ("Disroot", "https://disroot.org/en/services/email"),
    ("Tuta", "https://tuta.com/"),
    ("Mailvelope", "https://mailvelope.com/"),
    ("Email Privacy Tester", "https://www.emailprivacytester.com/"),
]

IT_TOOLS["itbreach"] = [
    ("Have I Been Pwned?", "https://haveibeenpwned.com/"),
    ("Mozilla Monitor", "https://monitor.mozilla.org/"),
    ("BreachDirectory", "https://breachdirectory.org/"),
    ("Leak Lookup", "https://leak-lookup.com/"),
    ("LeakPeek", "https://leakpeek.com/"),
    ("Intelligence X", "https://intelx.io/"),
]

IT_TOOLS["itfinger"] = [
    ("CreepJS", "https://abrahamjuliot.github.io/creepjs"),
    ("webkay", "https://webkay.robinlinus.com/"),
    ("Cover Your Tracks", "https://coveryourtracks.eff.org/"),
    ("BrowserLeaks", "https://browserleaks.com/"),
    ("IPLeak.net", "https://ipleak.net/"),
    ("Blacklight", "https://themarkup.org/blacklight"),
    ("ClearURLs", "https://docs.clearurls.xyz/"),
]

IT_TOOLS["itvpn"] = [
    ("VPN", "https://fmhy.net/privacy/#vpn"),
    ("WireGuard", "https://www.wireguard.com/"),
    ("Proton VPN", "https://protonvpn.com/"),
    ("Mullvad VPN", "https://mullvad.net/"),
    ("IVPN", "https://www.ivpn.net/"),
    ("Cloudflare One", "https://one.one.one.one/"),
    ("RiseupVPN", "https://riseup.net/en/vpn"),
]

IT_TOOLS["itproxy"] = [
    ("Proxy", "https://fmhy.net/privacy/#proxy"),
    ("Project X", "https://github.com/XTLS/Xray-core"),
    ("NaïveProxy", "https://github.com/klzgrad/naiveproxy"),
    ("Shadowsocks", "https://shadowsocks.org/"),
    ("sing-box", "https://sing-box.sagernet.org/"),
    ("Hiddify Manager", "https://hiddify.com/"),
    ("Outline", "https://getoutline.org/"),
]

IT_TOOLS["itanticensor"] = [
    ("Anti Censorship", "https://fmhy.net/privacy/#anti-censorship"),
    ("Censorship Bypass Guide", "https://cbg.fmhy.bid/"),
    ("Net4people", "https://github.com/net4people/bbs/issues"),
    ("GoodbyeDPI", "https://github.com/ValdikSS/GoodbyeDPI/"),
    ("SpoofDPI", "https://github.com/xvzc/SpoofDPI"),
    ("ByeByeDPI", "https://github.com/romanvht/ByeByeDPI/blob/master/README-en.md"),
]

IT_TOOLS["itgfw"] = [
    ("Great Firewall Bypass", "https://fmhy.net/non-english/#great-firewall"),
    ("gfwlist", "https://github.com/gfwlist/gfwlist"),
    ("gfw.report", "https://gfw.report/"),
    ("GHProxy", "https://ghproxy.link/"),
    ("GFWMass", "https://github.com/eli32-vlc/gfwmass"),
    ("Accesser", "https://github.com/URenko/Accesser/"),
]

IT_TOOLS["itvpncfg"] = [
    ("Free VPN Configs", "https://fmhy.net/storage/#free-vpn-configs"),
    ("F0rc3Run", "https://f0rc3run.github.io/F0rc3Run-panel/"),
    ("V2Nodes", "https://v2nodes.com/"),
    ("v2ray servers", "https://github.com/ebrasha/free-v2ray-public-list"),
    ("RaceVPN", "https://www.racevpn.com/"),
    ("VPN Jantit", "https://www.vpnjantit.com/"),
]

IT_TOOLS["itproxylist"] = [
    ("Proxy Lists", "https://fmhy.net/storage/#proxy-lists"),
    ("PROXY List", "https://github.com/TheSpeedX/PROXY-List"),
    ("Free-Proxy-List", "https://free-proxy-list.net/"),
    ("OpenProxyList", "https://openproxylist.com/"),
    ("ProxyScrape", "https://www.proxyscrape.com/free-proxy-list"),
    ("proxy-list", "https://github.com/mmpx12/proxy-list"),
]

IT_TOOLS["itimgsearch"] = [
    ("Image Search Engines", "https://fmhy.net/image-tools/#image-search-engines"),
    ("Yandex Images", "https://yandex.com/images/"),
    ("Google Lens", "https://www.google.com/?olud"),
    ("TinEye", "https://tineye.com/"),
    ("Bing Visual Search", "https://bing.com/camera"),
    ("SauceNao", "https://saucenao.com/"),
]

IT_TOOLS["itreddit"] = [
    ("Reddit Search", "https://fmhy.net/social-media-tools/#reddit-search"),
    ("PullPush Search", "https://search.pullpush.io/"),
    ("Better Reddit Search", "https://betterredditsearch.web.app/"),
    ("Redditle", "https://redditle.com/"),
    ("Arctic Shift", "https://arctic-shift.photon-reddit.com/"),
    ("redarcs", "https://the-eye.eu/redarcs/"),
]

IT_TOOLS["itdiscord"] = [
    ("Discord Tools", "https://fmhy.net/social-media-tools/#discord-tools"),
    ("AnswersOverflow", "https://www.answeroverflow.com/"),
    ("Discord Chat Exporter", "https://github.com/Tyrrrz/DiscordChatExporter"),
    ("Nelly", "https://nelly.tools/"),
    ("dsc.gg", "https://dsc.gg/"),
    ("Discord Timestamp Generator", "https://dank.tools/discord-timestamp"),
]

IT_TOOLS["ittelegram"] = [
    ("Telegram Tools", "https://fmhy.net/social-media-tools/#telegram-tools"),
    ("TDirectory", "https://tdirectory.me/"),
    ("TGStat", "https://tgstat.com/"),
    ("Searchee Bot", "https://t.me/SearcheeBot"),
    ("TheFeedReaderBot", "https://thefeedreaderbot.com/"),
    ("SaveRestrictedContentBot", "https://github.com/vasusen-code/SaveRestrictedContentBot"),
]

_IT_PAGE_SIZE = 8

_IT_MAIN_TEXT = (
    "🌍 <b><a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a> — Internet Tools | Search | URL | Email</b>\n\n"
    "A massive index of internet utilities: network tests, search engines, URL tools, "
    "email helpers, privacy tools, VPN/proxy resources, and social search resources curated by "
    "<a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a>.\n\n"
    "📌 <i>Select a category below to explore:</i>"
)

def _it_main_kb(page: int = 0):
    """Build the main Internet Tools menu keyboard (1 column, paginated)."""
    start = page * _IT_PAGE_SIZE
    end = start + _IT_PAGE_SIZE
    cats = _IT_CATEGORIES[start:end]
    total_pages = (len(_IT_CATEGORIES) + _IT_PAGE_SIZE - 1) // _IT_PAGE_SIZE
    rows = []
    for key, emoji, label in cats:
        rows.append([InlineKeyboardButton(f"{emoji}  {label}", callback_data=f"it_cat_{key}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"◀️  Page {page}", callback_data=f"it_page_{page - 1}", style="primary"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(f"Page {page + 2}  ▶️", callback_data=f"it_page_{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

def _IT_TOOLS_kb(cat_key: str):
    """Build keyboard for an Internet Tools category showing tools as URL buttons (1 per row)."""
    tools = IT_TOOLS.get(cat_key, [])
    rows = []
    for name, url in tools:
        rows.append([InlineKeyboardButton(f"{name} 🔗", url=url, style="success")])
    idx = next((i for i, (k, _, _) in enumerate(_IT_CATEGORIES) if k == cat_key), 0)
    page = idx // _IT_PAGE_SIZE
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"it_page_{page}", style="primary")])
    return InlineKeyboardMarkup(rows)

async def it_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all it_ callback queries for interactive navigation."""
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "it_main" or data == "it_page_0":
        await q.edit_message_text(_IT_MAIN_TEXT, reply_markup=_it_main_kb(0),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("it_page_"):
        try:
            page = int(data[len("it_page_"):])
        except ValueError:
            return
        await q.edit_message_text(_IT_MAIN_TEXT, reply_markup=_it_main_kb(page),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("it_cat_"):
        cat_key = data[len("it_cat_"):]
        cat = next(((k, e, l) for k, e, l in _IT_CATEGORIES if k == cat_key), None)
        if not cat:
            return
        _, emoji, label = cat
        tools = IT_TOOLS.get(cat_key, [])
        if tools:
            text = f"{emoji} <b>{label}</b>\n\n🔽 <i>Tap a tool to open it:</i>"
        else:
            text = f"{emoji} <b>{label}</b>\n\n⏳ <i>Tools coming soon…</i>"
        await q.edit_message_text(text, reply_markup=_IT_TOOLS_kb(cat_key),
                                  parse_mode="HTML", disable_web_page_preview=True)

_IT_POST_TARGET = 405

async def it_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uname = (user.username or "").lower()
    uid = user.id
    if uname not in {"gordo"} and uid not in {7032935515}:
        await update.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "📌 Where should I post the Internet Tools menu?\n"
        "Send a Telegram link, e.g.:\n"
        "<code>https://t.me/c/3786381449/344</code>",
        parse_mode="HTML",
    )
    return _IT_POST_TARGET

async def it_post_got_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id, topic_id = _parse_custommsg_target(url)
    if chat_id is None:
        await update.message.reply_text("❌ Invalid link. Try again or /cancel.")
        return _IT_POST_TARGET
    try:
        kwargs = dict(
            chat_id=chat_id,
            text=_IT_MAIN_TEXT,
            reply_markup=_it_main_kb(0),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)
        await update.message.reply_text("✅ Internet Tools menu posted!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def it_post_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ══ Text Tools menu ══════════════════════════════════════════════════════════

_TT_CATEGORIES = [
    ("ttmain",       "📝", "Text Tools"),
    ("ttocr",        "🖼️", "Image to Text | OCR"),
    ("tttts",        "🗣️", "Text to Speech"),
    ("ttdataviz",    "📊", "Data Visualization Tools"),
    ("ttpaste",      "📋", "Pastebins"),
    ("tttrans",      "🌍", "Translators"),
    ("ttaudio",      "🎙️", "Audio Transcription"),
    ("ttencode",     "🔐", "Encode | Decode"),
    ("ttgrammar",    "✅", "Grammar Check"),
    ("ttemoji",      "😀", "Emoji Indexes"),
    ("ttunicode",    "🔣", "Unicode Characters"),
    ("tteditors",    "✏️", "Text Editors"),
    ("ttnotes",      "📒", "Note-Taking"),
    ("ttoffice",     "📄", "Office Suites"),
    ("ttwinact",     "🪟", "Windows Activation"),
    ("ttonline",     "🌐", "Online Editors"),
    ("ttmind",       "🧠", "Mind Mapping"),
    ("ttcollab",     "🤝", "Text | Code Collaboration"),
    ("ttspread",     "📈", "Spreadsheet Editors"),
    ("ttwriting",    "🖋️", "Writing Tools"),
    ("tttodo",       "☑️", "To Do Lists"),
    ("ttascii",      "⌨️", "ASCII Art"),
    ("ttmarkup",     "🏷️", "Markup Tools"),
    ("tthtml",       "🌐", "HTML Tools"),
    ("ttmarkdown",   "🧾", "Markdown Editors"),
    ("ttlatex",      "∑", "LaTeX Tools"),
    ("ttfonts",      "🔤", "Fonts"),
    ("ttfonttools",  "🛠️", "Font Tools"),
    ("ttfontgen",    "🎨", "Font | Text Generators"),
    ("ttossfonts",   "🆓", "Open Source | Freeware"),
    ("ttunicodegen", "✨", "Unicode Text Generators"),
]

TT_TOOLS: dict[str, list[tuple[str, str]]] = {k: [] for k, _, _ in _TT_CATEGORIES}

TT_TOOLS["ttmain"] = [
    ("Text Tools", "https://fmhy.net/text-tools#text-tools"),
    ("SortMyList", "https://sortmylist.com/"),
    ("TextCleanr", "https://www.textcleanr.com/"),
    ("Text Mechanic", "https://textmechanic.com/"),
    ("OnlineTextTools", "https://onlinetexttools.com/"),
    ("Convert Case", "https://convertcase.net/"),
    ("Diffr", "https://loilo.github.io/diffr/"),
    ("DocuSeal", "https://www.docuseal.com/"),
]

TT_TOOLS["ttocr"] = [
    ("Image to Text | OCR", "https://fmhy.net/image-tools#image-to-text-ocr"),
    ("ImageToText", "https://www.imagetotext.info/"),
    ("Capture2Text", "https://capture2text.sourceforge.net/"),
    ("NormCap", "https://dynobo.github.io/normcap/"),
    ("tesseract", "https://github.com/tesseract-ocr/tesseract"),
    ("Text Grab", "https://github.com/TheJoeFin/Text-Grab"),
    ("OCR.SPACE", "https://ocr.space/"),
]

TT_TOOLS["tttts"] = [
    ("Text to Speech", "https://fmhy.net/ai#text-to-speech"),
    ("TTSMaker", "https://ttsmaker.com/"),
    ("ElevenLabs", "https://elevenlabs.io/"),
    ("ttsMP3", "https://ttsmp3.com/"),
    ("NaturalReaders", "https://www.naturalreaders.com/online/"),
    ("Balabolka", "http://www.cross-plus-a.com/balabolka.htm"),
]

TT_TOOLS["ttdataviz"] = [
    ("Data Visualization Tools", "https://fmhy.net/storage#data-visualization-tools"),
    ("RAWGraphs", "https://app.rawgraphs.io/"),
    ("draw.io", "https://www.drawio.com/"),
    ("Kroki", "https://kroki.io/#try"),
    ("flowchart fun", "https://flowchart.fun/"),
    ("Mermaid", "https://mermaid.js.org/"),
]

TT_TOOLS["ttpaste"] = [
    ("GitHub Gists", "https://gist.github.com/"),
    ("GitLab Snippets", "https://docs.gitlab.com/user/snippets/"),
    ("pastes.dev", "https://pastes.dev/"),
    ("PrivateBin", "https://privatebin.net/"),
    ("Rentry", "https://rentry.co/"),
    ("Katbin", "https://katb.in/"),
    ("paste.myst.rs", "https://paste.myst.rs/"),
    ("Opengist", "https://opengist.io/"),
]

TT_TOOLS["tttrans"] = [
    ("DeepL", "https://www.deepl.com/translator"),
    ("Google Translate", "https://translate.google.com/"),
    ("Kagi Translate", "https://translate.kagi.com/"),
    ("LibreTranslate", "https://libretranslate.com/"),
    ("Translate Shell", "https://www.soimort.org/translate-shell/"),
    ("Reverso", "https://context.reverso.net/translation/"),
    ("Yandex Translator", "https://translate.yandex.com/"),
    ("Sign Translate", "https://sign.mt/"),
]

TT_TOOLS["ttaudio"] = [
    ("ASR Leaderboard", "https://huggingface.co/spaces/hf-audio/open_asr_leaderboard"),
    ("Whisper", "https://github.com/openai/whisper"),
    ("SpeechTexter", "https://www.speechtexter.com/"),
    ("oTranscribe", "https://otranscribe.com/"),
    ("Revoldiv", "https://revoldiv.com/"),
    ("WhisperX", "https://github.com/m-bain/whisperX"),
    ("Buzz", "https://github.com/chidiwilliams/buzz"),
]

TT_TOOLS["ttencode"] = [
    ("CyberChef", "https://gchq.github.io/CyberChef/"),
    ("DecodeUnicode", "https://decodeunicode.org/"),
    ("Base64 Decode", "https://www.base64decode.org/"),
    ("Ciphey", "https://github.com/Ciphey/Ciphey"),
    ("cryptii", "https://cryptii.com/"),
    ("DenCode", "https://dencode.com/"),
    ("URL Decode", "https://url-decode.com/"),
    ("StegCloak", "https://stegcloak.surge.sh/"),
]

TT_TOOLS["ttgrammar"] = [
    ("LanguageTool", "https://languagetool.org/"),
    ("QuillBot", "https://quillbot.com/grammar-check"),
    ("Grammarly", "https://www.grammarly.com/grammar-check"),
    ("Harper", "https://writewithharper.com/"),
    ("DeepL Write", "https://www.deepl.com/write"),
    ("Kagi Proofread", "https://translate.kagi.com/proofread"),
    ("Scribens", "https://www.scribens.com/"),
]

TT_TOOLS["ttemoji"] = [
    ("Emojipedia", "https://emojipedia.org/"),
    ("EmojiDB", "https://emojidb.org/"),
    ("Slackmojis", "https://slackmojis.com/"),
    ("Emoji Picker", "https://github-emoji-picker.vercel.app/"),
    ("EmojiBatch", "https://www.emojibatch.com/"),
    ("Emoji Engine", "https://www.emojiengine.com/"),
    ("Emojify", "https://madelinemiller.dev/apps/emojify/"),
]

TT_TOOLS["ttunicode"] = [
    ("Amp What", "https://www.amp-what.com/"),
    ("CopyChar", "https://copychar.cc/"),
    ("Unicode Table", "https://symbl.cc/"),
    ("Unicode Explorer", "https://unicode-explorer.com/"),
    ("Symbol.so", "https://symbol.so/"),
    ("Graphemica", "https://graphemica.com/"),
    ("Character Map", "https://github.com/character-map-uwp/Character-Map-UWP"),
]

TT_TOOLS["tteditors"] = [
    ("Text Editors", "https://fmhy.net/text-tools#text-editors"),
    ("Notepad++", "https://notepad-plus-plus.org/"),
    ("NotepadNext", "https://github.com/dail8859/NotepadNext"),
    ("EncryptPad", "https://evpo.net/encryptpad/"),
    ("Notepads", "https://www.notepadsapp.com/"),
    ("Sublime Text", "https://www.sublimetext.com/"),
    ("SciTE", "https://www.scintilla.org/SciTE.html"),
]

TT_TOOLS["ttnotes"] = [
    ("Obsidian", "https://obsidian.md/"),
    ("AnyType", "https://anytype.io/"),
    ("Memos", "https://usememos.com/"),
    ("Joplin", "https://joplinapp.org/"),
    ("Standard Notes", "https://standardnotes.com/"),
    ("Crypt.ee", "https://crypt.ee/"),
    ("Google Keep", "https://keep.google.com/"),
]

TT_TOOLS["ttoffice"] = [
    ("LibreOffice", "https://www.libreoffice.org/"),
    ("OnlyOffice", "https://www.onlyoffice.com/"),
    ("Microsoft Office", "https://massgrave.dev/office_c2r_links"),
    ("Calligra", "https://calligra.org/"),
    ("Ziziyi Office", "https://office.ziziyi.com/"),
]

TT_TOOLS["ttwinact"] = [
    ("Windows Activation", "https://fmhy.net/system-tools#windows-activation"),
    ("Microsoft Activation Scripts", "https://massgrave.dev/"),
]

TT_TOOLS["ttonline"] = [
    ("Proton Docs", "https://proton.me/drive/docs"),
    ("takenote", "https://takenote.dev/"),
    ("Zen", "https://zen.unit.ms/"),
    ("Leaflet", "https://leaflet.pub/"),
    ("Browserpad", "https://browserpad.org/"),
    ("Shrib", "https://shrib.com/"),
    ("dDocs", "https://docs.fileverse.io/"),
    ("AnyTextEditor", "https://anytexteditor.com/"),
]

TT_TOOLS["ttmind"] = [
    ("Obsidian Canvas", "https://obsidian.md/canvas"),
    ("FreeMind", "https://freemind.sourceforge.net/"),
    ("Kinopio", "https://kinopio.club/"),
    ("Freeplane", "https://github.com/freeplane/freeplane"),
    ("MindMeister", "https://www.mindmeister.com/"),
    ("markmap", "https://markmap.js.org/"),
    ("Coggle", "https://coggle.it/"),
]

TT_TOOLS["ttcollab"] = [
    ("Google Docs", "https://www.google.com/docs/about/"),
    ("CryptPad", "https://cryptpad.fr/"),
    ("Mattermost", "https://mattermost.com/"),
    ("HackMD", "https://hackmd.io/"),
    ("Etherpad", "https://etherpad.org/"),
    ("Overleaf", "https://www.overleaf.com/"),
    ("Rustpad", "https://rustpad.io/"),
]

TT_TOOLS["ttspread"] = [
    ("Proton Sheets", "https://proton.me/drive/sheets"),
    ("EditCSVOnline", "https://www.editcsvonline.com/"),
    ("qsv", "https://github.com/dathere/qsv"),
    ("Xan", "https://github.com/medialab/xan"),
    ("VisiData", "https://www.visidata.org/"),
    ("Framacalc", "https://framacalc.org/"),
    ("EtherCalc", "https://ethercalc.net/"),
    ("Plain Text Table", "https://plaintexttools.github.io/plain-text-table/"),
]

TT_TOOLS["ttwriting"] = [
    ("Writer", "https://www.gibney.org/writer"),
    ("FocusWriter", "https://gottcode.org/focuswriter/"),
    ("ZenPen", "https://zenpen.io/"),
    ("Write.as", "https://write.as/"),
    ("Manuskript", "https://www.theologeek.ch/manuskript/"),
    ("NovelWriter", "https://novelwriter.io/"),
    ("Twinery", "https://twinery.org/"),
    ("STARC", "https://starc.app/"),
]

TT_TOOLS["tttodo"] = [
    ("Goblin.tools", "https://goblin.tools/"),
    ("TickTick", "https://www.ticktick.com/"),
    ("Super Productivity", "https://super-productivity.com/"),
    ("SuperList", "https://www.superlist.com/"),
    ("Microsoft To Do", "https://to-do.office.com/"),
    ("Taskwarrior", "https://taskwarrior.org/"),
    ("Vikunja", "https://vikunja.io/"),
]

TT_TOOLS["ttascii"] = [
    ("TAAG", "https://patorjk.com/software/taag/"),
    ("ASCII Art Studio", "https://www.majorgeeks.com/files/details/ascii_art_studio.html"),
    ("REXPaint", "https://www.gridsagegames.com/rexpaint/"),
    ("PabloDraw", "https://picoe.ca/products/pablodraw/"),
    ("ASCII Blaster", "https://asdf.us/asciiblaster/"),
    ("ascii-image-converter", "https://github.com/TheZoraiz/ascii-image-converter"),
    ("AnsiLove", "https://www.ansilove.org/downloads.html"),
]

TT_TOOLS["ttmarkup"] = [
    ("Markup Tools", "https://fmhy.net/text-tools#markup-tools"),
    ("Markdown Guide", "https://www.markdownguide.org/"),
    ("markup.rocks", "https://markup.rocks/"),
    ("Markup Validation Service", "https://validator.w3.org/"),
    ("YAMLine", "https://yamline.com/"),
    ("yq", "https://mikefarah.gitbook.io/yq/"),
    ("Tableconvert", "https://tableconvert.com/"),
]

TT_TOOLS["tthtml"] = [
    ("HTML Tools", "https://fmhy.net/developer-tools#html"),
    ("HTML Reference", "https://developer.mozilla.org/en-US/docs/Web/HTML"),
]

TT_TOOLS["ttmarkdown"] = [
    ("MarkD", "https://markd.it/"),
    ("HedgeDoc", "https://hedgedoc.org/"),
    ("Markdown Monster", "https://markdownmonster.west-wind.com/"),
    ("Zettlr", "https://www.zettlr.com/"),
    ("Dillinger", "https://dillinger.io/"),
    ("Glow", "https://github.com/charmbracelet/glow"),
    ("Vrite", "https://editor.vrite.io/"),
]

TT_TOOLS["ttlatex"] = [
    ("Typst", "https://typst.app/home"),
    ("Overleaf", "https://www.overleaf.com/"),
    ("LyX", "https://www.lyx.org/"),
    ("TeXStudio", "https://texstudio.org/"),
    ("SimpleTex", "https://simpletex.cn/"),
    ("Learn LaTeX", "https://www.learnlatex.org/"),
    ("Detexify", "https://detexify.kirelabs.org/classify.html"),
]

TT_TOOLS["ttfonts"] = [
    ("Fonts", "https://fmhy.net/text-tools#fonts"),
    ("Nerd Fonts", "https://www.nerdfonts.com/"),
    ("OpenDyslexic", "https://opendyslexic.org/"),
    ("Typewolf", "https://www.typewolf.com/"),
    ("FiraCode", "https://github.com/tonsky/FiraCode"),
    ("Cascadia Code", "https://github.com/microsoft/cascadia-code"),
]

TT_TOOLS["ttfonttools"] = [
    ("Font Tools", "https://fmhy.net/text-tools#font-tools"),
    ("FontDrop", "https://fontdrop.info/"),
    ("WhatTheFont", "https://www.myfonts.com/pages/whatthefont"),
    ("Identifont", "http://www.identifont.com/"),
    ("Transfonter", "https://transfonter.org/"),
    ("FontBase", "https://fontba.se/"),
    ("Fonts Ninja", "https://fonts.ninja/tools"),
]

TT_TOOLS["ttfontgen"] = [
    ("Make WordArt", "https://www.makewordart.com/"),
    ("FlameText", "https://www.flamingtext.com/"),
    ("MakeText", "https://maketext.io/"),
    ("TextStudio", "https://www.textstudio.com/"),
    ("Textanim", "https://textanim.com/"),
    ("Glitch", "https://glitchtextgenerator.com/"),
    ("The Ransomizer", "https://www.ransomizer.com/"),
]

TT_TOOLS["ttossfonts"] = [
    ("Open Source | Freeware", "https://fmhy.net/text-tools#open-source-freeware"),
    ("FontSource", "https://fontsource.org/"),
    ("Font Squirrel", "https://www.fontsquirrel.com/"),
    ("DaFont", "https://www.dafont.com/"),
    ("Google Fonts", "https://fonts.google.com/"),
    ("FontShare", "https://fontshare.com/"),
    ("Bunny Fonts", "https://fonts.bunny.net/"),
]

TT_TOOLS["ttunicodegen"] = [
    ("Unicode Text Generators", "https://fmhy.net/text-tools#unicode-text-generators"),
    ("YayText", "https://yaytext.com/"),
    ("Messletters", "https://www.messletters.com/"),
    ("FSymbols", "https://fsymbols.com/generators/"),
    ("Fancy Text", "https://fancy-text.net/"),
    ("Aesthetic Font Generator", "https://www.tesms.net/"),
    ("Fancy Text Decorator", "https://fancytextdecorator.com/"),
]

_TT_PAGE_SIZE = 8

_TT_MAIN_TEXT = (
    "🧾 <b><a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a> — Text Tools | Editors | Markup | Fonts</b>\n\n"
    "Your complete text toolkit: pastebins, translators, OCR/TTS links, editors, collaboration, "
    "markdown/latex, and font resources curated by <a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a>.\n\n"
    "📌 <i>Select a category below to explore:</i>"
)

def _tt_main_kb(page: int = 0):
    """Build the main Text Tools menu keyboard (1 column, paginated)."""
    start = page * _TT_PAGE_SIZE
    end = start + _TT_PAGE_SIZE
    cats = _TT_CATEGORIES[start:end]
    total_pages = (len(_TT_CATEGORIES) + _TT_PAGE_SIZE - 1) // _TT_PAGE_SIZE
    rows = []
    for key, emoji, label in cats:
        rows.append([InlineKeyboardButton(f"{emoji}  {label}", callback_data=f"tt_cat_{key}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"◀️  Page {page}", callback_data=f"tt_page_{page - 1}", style="primary"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(f"Page {page + 2}  ▶️", callback_data=f"tt_page_{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

def _TT_TOOLS_kb(cat_key: str):
    """Build keyboard for a Text Tools category showing tools as URL buttons (1 per row)."""
    tools = TT_TOOLS.get(cat_key, [])
    rows = []
    for name, url in tools:
        rows.append([InlineKeyboardButton(f"{name} 🔗", url=url, style="success")])
    idx = next((i for i, (k, _, _) in enumerate(_TT_CATEGORIES) if k == cat_key), 0)
    page = idx // _TT_PAGE_SIZE
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"tt_page_{page}", style="primary")])
    return InlineKeyboardMarkup(rows)

async def tt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all tt_ callback queries for interactive navigation."""
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "tt_main" or data == "tt_page_0":
        await q.edit_message_text(_TT_MAIN_TEXT, reply_markup=_tt_main_kb(0),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("tt_page_"):
        try:
            page = int(data[len("tt_page_"):])
        except ValueError:
            return
        await q.edit_message_text(_TT_MAIN_TEXT, reply_markup=_tt_main_kb(page),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("tt_cat_"):
        cat_key = data[len("tt_cat_"):]
        cat = next(((k, e, l) for k, e, l in _TT_CATEGORIES if k == cat_key), None)
        if not cat:
            return
        _, emoji, label = cat
        tools = TT_TOOLS.get(cat_key, [])
        if tools:
            text = f"{emoji} <b>{label}</b>\n\n🔽 <i>Tap a tool to open it:</i>"
        else:
            text = f"{emoji} <b>{label}</b>\n\n⏳ <i>Tools coming soon…</i>"
        await q.edit_message_text(text, reply_markup=_TT_TOOLS_kb(cat_key),
                                  parse_mode="HTML", disable_web_page_preview=True)

_TT_POST_TARGET = 406

async def tt_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uname = (user.username or "").lower()
    uid = user.id
    if uname not in {"gordo"} and uid not in {7032935515}:
        await update.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "📌 Where should I post the Text Tools menu?\n"
        "Send a Telegram link, e.g.:\n"
        "<code>https://t.me/c/3786381449/344</code>",
        parse_mode="HTML",
    )
    return _TT_POST_TARGET

async def tt_post_got_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id, topic_id = _parse_custommsg_target(url)
    if chat_id is None:
        await update.message.reply_text("❌ Invalid link. Try again or /cancel.")
        return _TT_POST_TARGET
    try:
        kwargs = dict(
            chat_id=chat_id,
            text=_TT_MAIN_TEXT,
            reply_markup=_tt_main_kb(0),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)
        await update.message.reply_text("✅ Text Tools menu posted!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def tt_post_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ════════════════════════════════════════════════════════════════════════════════
# ██  Developer Tools menu
# ════════════════════════════════════════════════════════════════════════════════

_DT_CATEGORIES = [
    ("dtcommunities",  "💬", "Dev Communities"),
    ("dtnews",         "📰", "Dev News"),
    ("dtdevtools",     "🛠️", "Developer Tools"),
    ("dtonline",       "🌐", "Online Toolkits"),
    ("dtsoftware",     "💾", "Software Dev Tools"),
    ("dtmobile",       "📱", "Mobile Dev Tools"),
    ("dtdatabase",     "🗄️", "Database Tools"),
    ("dtgit",          "🔀", "Git Tools"),
    ("dtgithub",       "🐙", "GitHub Tools"),
    ("dtdocker",       "🐳", "Docker Tools"),
    ("dtcli",          "⌨️", "CLI Tools"),
    ("dtapi",          "🔌", "API Tools"),
    ("dtgamedev",      "🎮", "Game Dev Tools"),
    ("dtgameassets",   "🎨", "Game Assets"),
    ("dtide",          "📝", "IDEs / Code Editors"),
    ("dtcloudide",     "☁️", "Cloud IDEs / Collab"),
    ("dtandroidcode",  "🤖", "Android Code Editors"),
    ("dtcodingtools",  "🔧", "Coding Tools"),
    ("dtvim",          "🟢", "Vim / Neovim Tools"),
    ("dtvscode",       "🔷", "VSCode Tools"),
    ("dtcodingagents", "🤖", "Coding Agents / Extensions"),
    ("dtwebapp",       "🏗️", "Web / App Builders"),
    ("dtdevutils",     "⚙️", "Developer Utilities"),
    ("dtprolangs",     "📖", "Programming Languages"),
    ("dtpython",       "🐍", "Python"),
    ("dtclang",        "©️", "C Languages"),
    ("dtjava",         "☕", "Java / Kotlin"),
    ("dthtml",         "🔶", "HTML"),
    ("dtcss",          "🎨", "CSS"),
    ("dtjavascript",   "🟨", "JavaScript"),
    ("dtreact",        "⚛️", "React"),
    ("dtphp",          "🐘", "PHP"),
    ("dtwebdev",       "🌍", "Web Dev Tools"),
    ("dtwebbuilder",   "🏠", "Website Builders"),
    ("dtcolors",       "🎨", "Color Schemes"),
    ("dtfrontend",     "🖥️", "Frontend Tools"),
    ("dtwordpress",    "📰", "WordPress Tools"),
    ("dtregex",        "🔣", "Regex Tools"),
    ("dtbenchmark",    "📊", "Benchmark Tools"),
    ("dtsvg",          "✏️", "SVG Tools"),
    ("dthosting",      "🖧", "Hosting Tools"),
    ("dtdynamic",      "⚡", "Dynamic Page Hosting"),
    ("dtstatic",       "📄", "Static Page Hosting"),
    ("dtcyber",        "🛡️", "Cybersecurity Tools"),
    ("dtcyberindex",   "📚", "Cybersecurity Indexes"),
    ("dtpentest",      "🔓", "Pen Testing"),
    ("dtdns",          "🌐", "DNS Tools"),
    ("dtwebsec",       "🔒", "Web Security"),
    ("dtencrypt",      "🔐", "Encryption / Certificates"),
    ("dtreverse",      "🔍", "Reverse Engineering"),
]

DT_TOOLS: dict[str, list[tuple[str, str]]] = {k: [] for k, _, _ in _DT_CATEGORIES}

# ── Dev Communities ──
DT_TOOLS["dtcommunities"] = [
    ("StackOverflow — Developer Forum", "https://stackoverflow.com/"),
    ("XDA — App Development Forum", "https://xdaforums.com/"),
    ("Spiceworks Community — Developer Forum", "https://community.spiceworks.com/"),
    ("DEV Community — Developer Forum", "https://dev.to/"),
    ("Blind — Developer Forum", "https://www.teamblind.com/"),
    ("IndieHackers — Developer Forum", "https://www.indiehackers.com/"),
    ("CyberArsenal — Cybersecurity Forums", "https://cyberarsenal.org/"),
    ("Tech-Blogs — Blogs for Developers", "https://tech-blogs.dev/"),
    ("The Devs Network — Developer Chat", "https://thedevs.network/"),
]

# ── Dev News ──
DT_TOOLS["dtnews"] = [
    ("KrebsOnSecurity — Cybersecurity News", "https://krebsonsecurity.com/"),
    ("Lobsters — Dev News", "https://lobste.rs/"),
    ("DevURLs — Dev News", "https://devurls.com/"),
    ("daily.dev — Dev News", "https://app.daily.dev/posts"),
    ("This Week in Rust — Rust News", "https://this-week-in-rust.org/"),
    ("hackertab.dev — Dev Browser Startpage", "https://hackertab.dev/"),
]

# ── Developer Tools ──
DT_TOOLS["dtdevtools"] = [
    ("DevToys — Dev Multi-Tool App", "https://devtoys.app/"),
    ("DevDocs — Dev Documentation", "https://devdocs.io/"),
    ("Free for Developers — Tool Index", "https://free-for.dev/"),
    ("Tiny Helpers — Tool Index", "https://tiny-helpers.dev/"),
    ("ImHex — Hex Editor", "https://imhex.werwolv.net/"),
    ("StackShare — Tech Stack Collaboration", "https://stackshare.io/"),
    ("Devhints — Developer Cheat Sheets", "https://devhints.io/"),
    ("Libraries.io — Package / Framework Search", "https://libraries.io/"),
    ("N8N — Workflow Automation", "https://n8n.io/"),
    ("Sentry — Error Tracking Platform", "https://sentry.io/"),
    ("Webhook.site — Webhook Tools", "https://webhook.site/"),
    ("Wakatime — Programmer Stat Tracking", "https://wakatime.com/"),
]

# ── Online Toolkits ──
DT_TOOLS["dtonline"] = [
    ("AppDevTools — Online Dev Toolkit", "https://appdevtools.com/"),
    ("IT Tools — Online Dev Toolkit", "https://it-tools.tech/"),
    ("Web Toolbox — Online Dev Toolkit", "https://web-toolbox.dev/en"),
    ("devina — Online Dev Toolkit", "https://devina.io/"),
    ("Coders Tool — Online Dev Toolkit", "https://www.coderstool.com/"),
]

# ── Software Dev Tools ──
DT_TOOLS["dtsoftware"] = [
    ("Budibase — Internal Tool Builder", "https://budibase.com/"),
    ("Appsmith — Internal Tool Builder", "https://www.appsmith.com/"),
    ("Dokploy — App Deployment", "https://github.com/dokploy/dokploy"),
    ("PublicWWW — Source Code Search", "https://publicwww.com/"),
    ("grep.app — Source Code Search", "https://grep.app/"),
    ("PM2 — Process Manager", "https://pm2.keymetrics.io/"),
    ("Crontab Guru — Crontab Editor", "https://crontab.guru/"),
    ("dnSpyEx — .NET Debugger", "https://github.com/dnSpyEx/dnSpy"),
    ("Slint — GUI Development Tools", "https://slint.dev"),
    ("Inno Setup — Create Installation Programs", "https://jrsoftware.org/isinfo.php"),
]

# ── Mobile Dev Tools ──
DT_TOOLS["dtmobile"] = [
    ("AndroidRepo — Android Dev Resources", "https://androidrepo.com/"),
    ("Awesome iOS — iOS Dev Resources", "https://github.com/vsouza/awesome-ios"),
    ("Mobbin — Mobile UI Resources", "https://mobbin.com/"),
    ("Android Developer Roadmap", "https://github.com/skydoves/android-developer-roadmap"),
    ("App ideas — Collection of App Ideas", "https://github.com/florinpop17/app-ideas"),
    ("IconKitchen — App Icon Generator", "https://icon.kitchen/"),
    ("CS Android — Android Code Search", "https://cs.android.com/"),
    ("Official Android Courses", "https://developer.android.com/courses"),
    ("Android Libhunt — Android Packages", "https://android.libhunt.com/"),
    ("React Native Apps — Examples", "https://github.com/ReactNativeNews/React-Native-Apps/"),
]

# ── Database Tools ──
DT_TOOLS["dtdatabase"] = [
    ("DB Engines — Database Rankings", "https://db-engines.com/en/ranking"),
    ("DB Browser — SQLite Browser", "https://sqlitebrowser.org/"),
    ("DuckDB — Database Manager", "https://duckdb.org/"),
    ("DBeaver — Universal Database Tool", "https://dbeaver.io/"),
    ("Grafana — Dev Data Dashboard", "https://grafana.com/"),
    ("NocoDB — Database Manager", "https://github.com/nocodb/nocodb"),
    ("Baserow — Database Manager", "https://baserow.io/"),
    ("ChartDB — Database Visualization", "https://chartdb.io/"),
    ("Ingestr — Transfer Data Between DBs", "https://bruin-data.github.io/ingestr/"),
    ("Sqlable — SQL Tools", "https://sqlable.com/"),
]

# ── Git Tools ──
DT_TOOLS["dtgit"] = [
    ("Git — Version Control System", "https://git-scm.com/"),
    ("GitButler — Git Desktop Client", "https://github.com/gitbutlerapp/gitbutler"),
    ("Codeberg — Git Hosting", "https://codeberg.org/"),
    ("GitLab — Git Hosting", "https://about.gitlab.com/"),
    ("Gitea — Self-Hosted Git", "https://about.gitea.com/"),
    ("Forgejo — Self-Hosted Git", "https://forgejo.org/"),
    ("GitKraken — Git GUI", "https://www.gitkraken.com/"),
    ("lazygit — Git TUI", "https://github.com/jesseduffield/lazygit"),
    ("Difftastic — Syntax-Aware Diff", "https://difftastic.wilfred.me.uk/"),
    ("Delta — Syntax Highlighting Diff", "https://github.com/dandavison/delta"),
    ("pre-commit — Manage Pre-Commit Hooks", "https://pre-commit.com/"),
    ("GIT Quick Stats — View Git Statistics", "https://git-quick-stats.sh/"),
]

# ── GitHub Tools ──
DT_TOOLS["dtgithub"] = [
    ("refined-github — Improved GitHub UI", "https://github.com/refined-github/refined-github"),
    ("GitHub Desktop — Desktop Client", "https://github.com/apps/desktop"),
    ("OSS Insight — GitHub Project Index", "https://ossinsight.io/"),
    ("GitHub Cheat Sheet", "https://github.com/tiimgreen/github-cheat-sheet"),
    ("Download Directory — Download Repo Sub-Folders", "https://download-directory.github.io/"),
    ("act — Run GitHub Actions Locally", "https://nektosact.com/"),
    ("Star History — Repo Star History Graph", "https://star-history.com/"),
    ("Octotree — Repo File Tree View", "https://www.octotree.io/"),
    ("GitHub Readme Stats — Dynamic Stats", "https://github.com/anuraghazra/github-readme-stats"),
    ("SkillIcons — Skill Badges for Readme", "https://skillicons.dev/"),
]

# ── Docker Tools ──
DT_TOOLS["dtdocker"] = [
    ("Docker — Build & Run Containers", "https://www.docker.com/"),
    ("Podman — Rootless Docker Alternative", "https://podman.io/"),
    ("Portainer — Container Manager", "https://portainer.io/"),
    ("DockGE — Container Manager", "https://dockge.kuma.pet/"),
    ("LazyDocker — Docker TUI", "https://github.com/jesseduffield/lazydocker"),
    ("Composerize — Compose Docker Files", "https://www.composerize.com/"),
    ("Hub Docker — Docker Images", "https://hub.docker.com/"),
    ("Dive — Analyze Docker Images", "https://github.com/wagoodman/dive"),
    ("WatchTower — Container Automation", "http://watchtower.nickfedor.com/"),
    ("Dozzle — Log Viewer", "https://dozzle.dev/"),
    ("Dockle — Image Linter", "https://github.com/goodwithtech/dockle"),
]

# ── CLI Tools ──
DT_TOOLS["dtcli"] = [
    ("Charm — Terminal-Based App Backend", "https://charm.sh/"),
    ("OhMyPosh — Terminal Theme Engine", "https://ohmyposh.dev/"),
    ("ripgrep — grep Alternative", "https://github.com/BurntSushi/ripgrep"),
    ("Atuin — Shell History Sync & Search", "https://atuin.sh/"),
    ("Zoxide — Improved CD Command", "https://github.com/ajeetdsouza/zoxide"),
    ("sshx — Share Terminal Screen", "https://sshx.io/"),
    ("pueue — Shell Command Manager", "https://github.com/Nukesor/pueue"),
    ("Command Not Found — Install Missing Commands", "https://command-not-found.com/"),
    ("VisiData — Spreadsheet CLI Editor", "https://www.visidata.org/"),
    ("Lip Gloss — Terminal Layout Styles", "https://github.com/charmbracelet/lipgloss"),
]

# ── API Tools ──
DT_TOOLS["dtapi"] = [
    ("Public APIs — API Index", "https://publicapis.dev/"),
    ("API List — API Index", "https://apilist.fun/"),
    ("hoppscotch — API Builder", "https://hoppscotch.io/"),
    ("HTTPie — Test REST / GraphQL APIs", "https://httpie.io/"),
    ("Bruno — API Testing Client", "https://www.usebruno.com/"),
    ("Posting — API Client TUI", "https://posting.sh/"),
    ("Insomnia — API Client", "https://insomnia.rest/"),
    ("FastAPI — API Framework", "https://fastapi.tiangolo.com/"),
    ("Pipedream — Connect APIs", "https://pipedream.com/"),
    ("ReDoc — Generate API Documentation", "https://redocly.github.io/redoc/"),
    ("Telegram Bot API", "https://core.telegram.org/bots"),
]

# ── Game Dev Tools ──
DT_TOOLS["dtgamedev"] = [
    ("Awesome Game Engine — Engine Resources", "https://github.com/stevinz/awesome-game-engine-dev"),
    ("EnginesDatabase — Game Engines Database", "https://enginesdatabase.com/"),
    ("Awesome Game Dev — Resources", "https://github.com/Calinou/awesome-gamedev"),
    ("GameDev Torch — Multi-Site Search", "https://gamedevtorch.com/"),
    ("Tracy Profiler — Frame Profiler", "https://github.com/wolfpld/tracy"),
    ("Decompedia — Game Decomp Resources", "https://decomp.wiki/"),
    ("Fantasy Consoles / Computers", "https://github.com/paladin-t/fantasy"),
    ("Xelu's Controller Prompts", "https://thoseawesomeguys.com/prompts/"),
]

# ── Game Assets ──
DT_TOOLS["dtgameassets"] = [
    ("Itch.io Assets — Free Game Assets", "https://itch.io/game-assets/free"),
    ("Kenney — Free Game Assets", "https://www.kenney.nl/"),
    ("OpenGameArt.org — Game Art Community", "https://opengameart.org/"),
    ("Game UI Database", "https://www.gameuidatabase.com/"),
    ("Game-icons — Game Icons", "https://game-icons.net/"),
    ("CraftPix — 2D Game Assets", "https://craftpix.net/freebies/"),
    ("GameDev Market — Indie Assets", "https://www.gamedevmarket.net/"),
    ("SteamGridDB — Custom Game Assets", "https://www.steamgriddb.com/"),
    ("Game Sounds — Royalty Free", "https://gamesounds.xyz/"),
    ("jfxr — Sound Effects Creator", "https://jfxr.frozenfractal.com/"),
]

# ── IDEs / Code Editors ──
DT_TOOLS["dtide"] = [
    ("VSCodium — FOSS VS Code", "https://vscodium.com/"),
    ("Visual Studio Code", "https://code.visualstudio.com/"),
    ("JetBrains — IDE Suite", "https://jetbrains.com/"),
    ("Neovim — Code Editor", "https://neovim.io/"),
    ("Zed — Code Editor", "https://zed.dev/"),
    ("Helix — Code Editor", "https://helix-editor.com/"),
    ("Lite XL — Lightweight Editor", "https://lite-xl.com/"),
    ("Emacs — Code Editor", "https://www.gnu.org/software/emacs/"),
    ("Lapce — Code Editor", "https://lap.dev/lapce/"),
    ("Geany — Lightweight Editor", "https://www.geany.org/"),
    ("CudaText — Code Editor", "https://cudatext.github.io/"),
    ("JSON Hero — JSON Viewer / Editor", "https://jsonhero.io/"),
]

# ── Cloud IDEs / Collab ──
DT_TOOLS["dtcloudide"] = [
    ("Google Colaboratory — Cloud IDE", "https://colab.research.google.com/"),
    ("CodeSandbox — VSCode Cloud IDE", "https://codesandbox.io/"),
    ("StackBlitz — VSCode Cloud IDE", "https://stackblitz.com/"),
    ("CodePen — Code Sandbox", "https://codepen.io/"),
    ("JSFiddle — Cloud IDE", "https://jsfiddle.net/"),
    ("PlayCode — Cloud IDE", "https://playcode.io/"),
    ("Ideone — Cloud IDE", "https://www.ideone.com/"),
    ("glot.io — Pastebin w/ Runnable Snippets", "https://glot.io/"),
    ("CoCalc — Virtual Online Workspace", "https://cocalc.com/"),
    ("DevPod — Dev Environments", "https://devpod.sh"),
]

# ── Android Code Editors ──
DT_TOOLS["dtandroidcode"] = [
    ("ChromeXt — Mobile Dev Tools", "https://github.com/JingMatrix/ChromeXt"),
    ("APKEditor — APK Editing / Merging", "https://github.com/REAndroid/APKEditor"),
    ("Apktool M — APK Editor", "https://maximoff.su/apktool/?lang=en"),
]

# ── Coding Tools ──
DT_TOOLS["dtcodingtools"] = [
    ("Prettier — Code Formatter", "https://prettier.io/"),
    ("codebeautify — Code Formatting", "https://codebeautify.org/"),
    ("Compiler Explorer — Online Compilers", "https://compiler-explorer.com/"),
    ("Carbon — Code Screenshots", "https://carbon.now.sh/"),
    ("Ray — Code Screenshots", "https://www.ray.so/"),
    ("massCode — Code Snippet Manager", "https://masscode.io/"),
    ("Code2Flow — Code to Flowchart", "https://app.code2flow.com/"),
    ("Sourcegraph — Code Searching", "https://sourcegraph.com/search"),
    ("WinMerge — File Comparison", "https://winmerge.org/"),
    ("Dracula — Code Editor Theme", "https://draculatheme.com/"),
    ("Freeze — Generate Code Images", "https://github.com/charmbracelet/freeze"),
]

# ── Vim / Neovim Tools ──
DT_TOOLS["dtvim"] = [
    ("Neovim — Code Editor", "https://neovim.io/"),
    ("Helix — Neovim-Based Editor", "https://helix-editor.com/"),
    ("Fresh — TUI Code Editor", "https://getfresh.dev/"),
]

# ── VSCode Tools ──
DT_TOOLS["dtvscode"] = [
    ("Awesome VSC Extensions", "https://hl2guide.github.io/Awesome-Visual-Studio-Code-Extensions/"),
    ("VS Studio Marketplace", "https://marketplace.visualstudio.com/"),
    ("Open VSX — VSCode Extension Registry", "https://open-vsx.org/"),
    ("code-server — VSCode Web Server", "https://coder.com/"),
    ("VSCodeThemes — Theme Browser", "https://vscodethemes.com/"),
    ("snippet-generator — Snippet Generator", "https://snippet-generator.app/"),
    ("chatgpt-vscode — ChatGPT Extension", "https://github.com/mpociot/chatgpt-vscode"),
    ("oslo — Theme Generator", "https://oslo-vsc.netlify.app/"),
]

# ── Coding Agents / Extensions ──
DT_TOOLS["dtcodingagents"] = [
    ("Aider — Terminal Coding AI", "https://aider.chat/"),
    ("Gemini CLI — Coding AI", "https://geminicli.com/"),
    ("Google Antigravity — Coding AI", "https://antigravity.google/"),
    ("Windsurf — Agentic IDE", "https://www.windsurf.com/"),
    ("OpenCode — Coding AI", "https://opencode.ai/"),
    ("Cline — VS Code Agent", "https://cline.bot/"),
    ("Roo Code — VS Code Agent", "https://roocode.com/"),
    ("OpenHands — Coding AI", "https://www.all-hands.dev/"),
    ("Continue — Coding AI", "https://continue.dev/"),
    ("Supermaven — Tab Completion AI", "https://supermaven.com/"),
    ("Qodo — Coding AI", "https://www.qodo.ai/"),
]

# ── Web / App Builders ──
DT_TOOLS["dtwebapp"] = [
    ("Arena — AI Website Builder", "https://arena.ai/code"),
    ("Z.ai — AI Website Builder", "https://chat.z.ai/"),
    ("v0 — Text to Site Code", "https://v0.app/"),
    ("Bolt.new — AI Web App Builder", "https://bolt.new/"),
    ("Websim — App Builder", "https://websim.com/"),
    ("AnyCoder — App Builder", "https://huggingface.co/spaces/akhaliq/anycoder"),
    ("Llama Coder — App Builder", "https://llamacoder.together.ai/"),
    ("Devv — Coding Search Engine", "https://devv.ai/"),
]

# ── Developer Utilities ──
DT_TOOLS["dtdevutils"] = [
    ("CodeRabbit — PR Reviews / Feedback", "https://www.coderabbit.ai/"),
    ("Code2prompt — Codebase To LLM Prompt", "https://github.com/mufeedvh/code2prompt"),
    ("Gitingest — GitHub Repo To Prompt", "https://gitingest.com/"),
    ("Repomix — GitHub Repo To Prompt", "https://repomix.com/"),
    ("Pieces — Multi-LLM Coding Search", "https://pieces.app/"),
    ("Skills — Add Capabilities to AI Agents", "https://skills.sh/"),
    ("PR-Agent — Pull Request Reviews", "https://github.com/qodo-ai/pr-agent"),
]

# ── Programming Languages ──
DT_TOOLS["dtprolangs"] = [
    ("Awesome Cheatsheets — Programming", "https://lecoupa.github.io/awesome-cheatsheets/"),
    ("QuickRef.me — Cheat Sheets", "https://quickref.me/"),
    ("TheAlgorithms — Coding Algorithms", "https://the-algorithms.com/"),
    ("30 Seconds of Code — Code Snippets", "https://www.30secondsofcode.org/"),
    ("Try It Online — Language Interpreters", "https://tio.run/"),
    ("Learn X in Y minutes — Language Rundowns", "https://learnxinyminutes.com/"),
    ("Codigo — Programming Language Repo", "https://codigolangs.com/"),
    ("Awesome Go — Go Resources", "https://awesome-go.com/"),
]

# ── Python ──
DT_TOOLS["dtpython"] = [
    ("Python.org — Official Site", "https://www.python.org/"),
    ("Awesome Python — Resources", "https://awesome-python.com/"),
    ("PyPI — Python Package Index", "https://pypi.org/"),
    ("Real Python — Tutorials", "https://realpython.com/"),
    ("FastAPI — API Framework", "https://fastapi.tiangolo.com/"),
]

# ── C Languages ──
DT_TOOLS["dtclang"] = [
    ("cppreference — C/C++ Reference", "https://en.cppreference.com/"),
    ("Compiler Explorer — C/C++ Compiler", "https://godbolt.org/"),
]

# ── Java / Kotlin ──
DT_TOOLS["dtjava"] = [
    ("Kotlin — Official Site", "https://kotlinlang.org/"),
    ("Spring — Java Framework", "https://spring.io/"),
    ("Awesome Java — Resources", "https://github.com/akullpp/awesome-java"),
]

# ── HTML ──
DT_TOOLS["dthtml"] = [
    ("Awesome HTML5 — Resources", "https://diegocard.com/awesome-html5"),
    ("HTML Reference — Guide", "https://htmlreference.io/"),
    ("HTML Cheat Sheet", "https://htmlcheatsheet.com/"),
    ("HTMLRev — Free HTML Templates", "https://htmlrev.com/"),
    ("HTML-Minifier — HTML Minifier", "https://github.com/j9t/html-minifier-next"),
    ("Markdown to HTML — Converter", "https://markdowntohtml.com/"),
]

# ── CSS ──
DT_TOOLS["dtcss"] = [
    ("Awesome CSS — Resources", "https://github.com/awesome-css-group/awesome-css"),
    ("CSS Tricks — Snippets", "https://css-tricks.com/snippets/"),
    ("Easings — Animation Cheat Sheet", "https://easings.net/"),
    ("Glass UI — Glassmorphism Generator", "https://ui.glass/generator/"),
    ("CSS Doodle — Pattern Generator", "https://css-doodle.com/"),
    ("Animista — CSS Animations", "https://animista.net/"),
    ("CSS Reference — Guide", "https://cssreference.io/"),
    ("Buttons.cool — Copy CSS Buttons", "https://www.buttons.cool/"),
    ("Hover.CSS — CSS Hover Effects", "https://ianlunn.github.io/Hover/"),
    ("Modern CSS — Guide", "https://moderncss.dev/"),
]

# ── JavaScript ──
DT_TOOLS["dtjavascript"] = [
    ("MDN Web Docs — JS Reference", "https://developer.mozilla.org/en-US/docs/Web/JavaScript"),
    ("JavaScript.info — Modern JS Tutorial", "https://javascript.info/"),
    ("npm — Package Manager", "https://www.npmjs.com/"),
]

# ── React ──
DT_TOOLS["dtreact"] = [
    ("React — Official Site", "https://react.dev/"),
    ("Next.js — React Framework", "https://nextjs.org/"),
    ("React Native — Mobile Framework", "https://reactnative.dev/"),
]

# ── PHP ──
DT_TOOLS["dtphp"] = [
    ("PHP.net — Official Site", "https://www.php.net/"),
    ("Laravel — PHP Framework", "https://laravel.com/"),
    ("Composer — PHP Package Manager", "https://getcomposer.org/"),
]

# ── Web Dev Tools ──
DT_TOOLS["dtwebdev"] = [
    ("Wappalyzer — Identify Technologies", "https://www.wappalyzer.com/"),
    ("shadcn-ui — Web Component Library", "https://ui.shadcn.com/"),
    ("GoAccess — Web Log Analyzer", "https://goaccess.io/"),
    ("Selenium — Browser Automation", "https://www.selenium.dev/"),
    ("PlayWright — Browser Automation", "https://playwright.dev/"),
    ("Can I Use? — Browser Support Tables", "https://caniuse.com/"),
    ("Umami — Site Analytics", "https://umami.is/"),
    ("cURL — HTTP Client / Transfer Data", "https://curl.se/"),
    ("PocketBase — Open-Source Backend", "https://pocketbase.io/"),
    ("Caddy — Web Server", "https://caddyserver.com/"),
    ("Motion — Animation Library", "https://motion.dev/"),
]

# ── Website Builders ──
DT_TOOLS["dtwebbuilder"] = [
    ("Framer — Website Builder", "https://www.framer.com/"),
    ("Hugo — Static Site Generator", "https://gohugo.io/"),
    ("Eleventy — Static Site Generator", "https://11ty.dev/"),
    ("Astro — Static Site Generator", "https://astro.build/"),
    ("VitePress — Static Site Generator", "https://vitepress.dev/"),
    ("Docusaurus — Static Markdown Site", "https://docusaurus.io/"),
    ("Jekyll — Static Markdown Site", "https://jekyllrb.com/"),
    ("Webstudio — Website Builder", "https://webstudio.is/"),
    ("Carrd — Simple Website Builder", "https://carrd.co/"),
    ("Publii — No Coding Static Site", "https://getpublii.com/"),
]

# ── Color Schemes ──
DT_TOOLS["dtcolors"] = [
    ("Palette Generators — FMHY", "https://fmhy.net/image-tools#palette-generators"),
    ("Color Pickers — FMHY", "https://fmhy.net/image-tools#color-pickers"),
    ("Coolors — Color Palette Generator", "https://coolors.co/"),
    ("CSS Gradient — Gradient Generator", "https://cssgradient.io/"),
]

# ── Frontend Tools ──
DT_TOOLS["dtfrontend"] = [
    ("shadcn-ui — Component Library", "https://ui.shadcn.com/"),
    ("FreeFrontend — Code Snippets", "https://freefrontend.com/"),
    ("Tailwind CSS — Utility Framework", "https://tailwindcss.com/"),
    ("Bootstrap — CSS Framework", "https://getbootstrap.com/"),
]

# ── WordPress Tools ──
DT_TOOLS["dtwordpress"] = [
    ("WordPress.org — Official Site", "https://wordpress.org/"),
    ("Developer Resources — WordPress.org", "https://developer.wordpress.org/"),
]

# ── Regex Tools ──
DT_TOOLS["dtregex"] = [
    ("regex101 — Regex Tester", "https://regex101.com/"),
    ("Regexr — Regex Tester / Visualizer", "https://regexr.com/"),
    ("iHateRegex — Regex Cheat Sheet", "https://ihateregex.io/"),
]

# ── Benchmark Tools ──
DT_TOOLS["dtbenchmark"] = [
    ("Benchmarks Game — Measure PL Speeds", "https://benchmarksgame-team.pages.debian.net/benchmarksgame/"),
    ("Language Benchmarks — PL Comparisons", "https://programming-language-benchmarks.vercel.app/"),
]

# ── SVG Tools ──
DT_TOOLS["dtsvg"] = [
    ("SVG Repo — Free SVG Icons", "https://www.svgrepo.com/"),
    ("SVGOMG — SVG Optimizer", "https://jakearchibald.github.io/svgomg/"),
    ("SVG Path Editor", "https://yqnn.github.io/svg-path-editor/"),
]

# ── Hosting Tools ──
DT_TOOLS["dthosting"] = [
    ("Awesome Web Hosting — Provider Index", "https://nuhmanpk.github.io/Awesome-Web-Hosting/"),
    ("Oracle Cloud — Free VPS", "https://www.oracle.com/cloud/free/"),
    ("Uptime Kuma — Uptime Monitor", "https://github.com/louislam/uptime-kuma"),
    ("Server Hunter — Search / Compare Servers", "https://www.serverhunter.com/"),
    ("GetDeploying — Compare Cloud Providers", "https://getdeploying.com/"),
    ("Kener — Self-Hosted Status Page", "https://kener.ing/"),
    ("Cloudron — Web App Host", "https://www.cloudron.io/"),
    ("VPS Price Tracker — Compare VPS", "https://vpspricetracker.com/"),
    ("TLD-List — Domain Price Comparisons", "https://tld-list.com/"),
    ("OpenPanel — Web Hosting Panel", "https://openpanel.com/"),
]

# ── Dynamic Page Hosting ──
DT_TOOLS["dtdynamic"] = [
    ("Railway — App Hosting", "https://railway.app/"),
    ("Vercel — App Hosting", "https://vercel.com/"),
    ("Fly.io — App Hosting", "https://fly.io/"),
    ("Render — App Hosting", "https://render.com/"),
    ("Heroku Alternatives — FMHY", "https://rentry.co/Heroku-Alt"),
]

# ── Static Page Hosting ──
DT_TOOLS["dtstatic"] = [
    ("GitHub Pages — Free Static Hosting", "https://pages.github.com/"),
    ("Netlify — Static Hosting", "https://www.netlify.com/"),
    ("Cloudflare Pages — Static Hosting", "https://pages.cloudflare.com/"),
    ("Surge — Static Web Publishing", "https://surge.sh/"),
]

# ── Cybersecurity Tools ──
DT_TOOLS["dtcyber"] = [
    ("Nmap — Network Security Scanner", "https://nmap.org/"),
    ("osquery — Security Monitor", "https://osquery.io"),
    ("Nuclei — Vulnerability Scanner", "https://docs.projectdiscovery.io/tools/nuclei"),
    ("Sniffnet — Network Monitor", "https://www.sniffnet.net/"),
    ("Crowdsec — Intrusion Detection", "https://crowdsec.net/"),
    ("Wazuh — Site Security Monitor", "https://wazuh.com/"),
    ("CVE Details — CVE Search", "https://www.cvedetails.com/"),
    ("Canarytokens — Network Breach Check", "https://canarytokens.org/generate"),
    ("Observatory — HTTP Header Security Test", "https://developer.mozilla.org/en-US/observatory"),
    ("Open Source Security Software", "https://open-source-security-software.net/"),
]

# ── Cybersecurity Indexes ──
DT_TOOLS["dtcyberindex"] = [
    ("Awesome Hacking — Resources", "https://github.com/Hack-with-Github/Awesome-Hacking"),
    ("NVD — National Vulnerability Database", "https://nvd.nist.gov/"),
    ("X-Force Exchange — Threat Intelligence", "https://exchange.xforce.ibmcloud.com/"),
    ("BBRadar — Bug Bounty Tracker", "https://bbradar.io/"),
]

# ── Pen Testing ──
DT_TOOLS["dtpentest"] = [
    ("Kali Linux — Pen Testing OS", "https://www.kali.org/"),
    ("Parrot OS — Security OS", "https://parrotsec.org/"),
    ("Metasploit — Pen Testing Framework", "https://www.metasploit.com/"),
    ("Burp Suite — Web Security Testing", "https://portswigger.net/burp"),
    ("AllSafe — Intentionally Vulnerable App", "https://github.com/t0thkr1s/allsafe"),
]

# ── DNS Tools ──
DT_TOOLS["dtdns"] = [
    ("Free DNS Resolvers — FMHY", "https://fmhy.net/storage#free-dns-resolvers"),
    ("DNSTwist — Typosquatting Checker", "https://dnstwist.it/"),
    ("DNS Perf — Speed Benchmark", "https://dnsperf.com/dns-speed-benchmark"),
    ("WhoisRequest — Whois Search", "https://whoisrequest.com/"),
    ("censys — Domain Info Tool", "https://search.censys.io/"),
]

# ── Web Security ──
DT_TOOLS["dtwebsec"] = [
    ("Snyk — Vulnerability Tracking", "https://security.snyk.io/"),
    ("Greenbone — Vulnerability Management", "https://github.com/greenbone"),
    ("Evervault — Security Infrastructure", "https://evervault.com/"),
    ("DarkVisitors — Data Scraper List", "https://darkvisitors.com/"),
    ("Anubis — Anti-Web Crawler", "https://anubis.techaro.lol/"),
]

# ── Encryption / Certificates ──
DT_TOOLS["dtencrypt"] = [
    ("Let's Encrypt — Free SSL Certificates", "https://letsencrypt.org/"),
    ("SSL Labs — SSL/TLS Testing", "https://www.ssllabs.com/ssltest/"),
    ("Keybase — Cryptographic Identity", "https://keybase.io/"),
    ("age — File Encryption", "https://age-encryption.org/"),
]

# ── Reverse Engineering ──
DT_TOOLS["dtreverse"] = [
    ("Ghidra — NSA Reverse Engineering Tool", "https://ghidra-sre.org/"),
    ("x64dbg — Open-Source Debugger", "https://x64dbg.com/"),
    ("Cutter — RE Platform", "https://cutter.re/"),
    ("DogBolt — Decompiler Explorer", "https://dogbolt.org/"),
    ("Decompiler — Online Decompiler", "https://www.decompiler.com/"),
]

_DT_PAGE_SIZE = 8

_DT_MAIN_TEXT = (
    "🛠️ <b><a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a> — Developer Tools</b>\n\n"
    "Your comprehensive guide to developer tools, IDEs, hosting, security, "
    "and programming resources curated by <a href='https://t.me/gordo'>𝔾𝕠𝕣𝕕𝕠</a>.\n\n"
    "📌 <i>Select a category below to explore:</i>"
)

def _dt_main_kb(page: int = 0):
    """Build the main Dev Tools menu keyboard (1 column, paginated)."""
    start = page * _DT_PAGE_SIZE
    end = start + _DT_PAGE_SIZE
    cats = _DT_CATEGORIES[start:end]
    total_pages = (len(_DT_CATEGORIES) + _DT_PAGE_SIZE - 1) // _DT_PAGE_SIZE
    rows = []
    for key, emoji, label in cats:
        rows.append([InlineKeyboardButton(f"{emoji}  {label}", callback_data=f"dt_cat_{key}", style="primary")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(f"◀️  Page {page}", callback_data=f"dt_page_{page - 1}", style="primary"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(f"Page {page + 2}  ▶️", callback_data=f"dt_page_{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)

def _DT_TOOLS_kb(cat_key: str):
    """Build keyboard for a Dev Tools category showing tools as URL buttons (1 per row)."""
    tools = DT_TOOLS.get(cat_key, [])
    rows = []
    for name, url in tools:
        rows.append([InlineKeyboardButton(f"{name} 🔗", url=url, style="success")])
    idx = next((i for i, (k, _, _) in enumerate(_DT_CATEGORIES) if k == cat_key), 0)
    page = idx // _DT_PAGE_SIZE
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"dt_page_{page}", style="primary")])
    return InlineKeyboardMarkup(rows)

async def dt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all dt_ callback queries for interactive navigation."""
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "dt_main" or data == "dt_page_0":
        await q.edit_message_text(_DT_MAIN_TEXT, reply_markup=_dt_main_kb(0),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("dt_page_"):
        try:
            page = int(data[len("dt_page_"):])
        except ValueError:
            return
        await q.edit_message_text(_DT_MAIN_TEXT, reply_markup=_dt_main_kb(page),
                                  parse_mode="HTML", disable_web_page_preview=True)
        return

    if data.startswith("dt_cat_"):
        cat_key = data[len("dt_cat_"):]
        cat = next(((k, e, l) for k, e, l in _DT_CATEGORIES if k == cat_key), None)
        if not cat:
            return
        _, emoji, label = cat
        tools = DT_TOOLS.get(cat_key, [])
        if tools:
            text = f"{emoji} <b>{label}</b>\n\n🔽 <i>Tap a tool to open it:</i>"
        else:
            text = f"{emoji} <b>{label}</b>\n\n⏳ <i>Tools coming soon…</i>"
        await q.edit_message_text(text, reply_markup=_DT_TOOLS_kb(cat_key),
                                  parse_mode="HTML", disable_web_page_preview=True)

_DT_POST_TARGET = 407

async def dt_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uname = (user.username or "").lower()
    uid = user.id
    if uname not in {"gordo"} and uid not in {7032935515}:
        await update.message.reply_text("⛔ Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "📌 Where should I post the Developer Tools menu?\n"
        "Send a Telegram link, e.g.:\n"
        "<code>https://t.me/c/3786381449/344</code>",
        parse_mode="HTML",
    )
    return _DT_POST_TARGET

async def dt_post_got_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    chat_id, topic_id = _parse_custommsg_target(url)
    if chat_id is None:
        await update.message.reply_text("❌ Invalid link. Try again or /cancel.")
        return _DT_POST_TARGET
    try:
        kwargs = dict(
            chat_id=chat_id,
            text=_DT_MAIN_TEXT,
            reply_markup=_dt_main_kb(0),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        await context.bot.send_message(**kwargs)
        await update.message.reply_text("✅ Developer Tools menu posted!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def dt_post_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# ── /help, /status, /features ─────────────────────────────────────────────────

_HELP_PUBLIC = (
    "🤖 <b>𝔾𝕠𝕣𝕕𝕠's Bot — Command Guide</b>\n\n"
    "<b>📋 General</b>\n"
    "  /start — Start the bot / request an invite\n"
    "  /help — Show this help message\n"
    "  /features — View planned upcoming features\n\n"
    "<b>🛡️ Moderation (group admins only)</b>\n"
    "  /ban /dban /sban /tban /unban\n"
    "  /mute /dmute /smute /tmute /unmute\n"
    "  /kick /dkick /skick /kickme\n"
    "  /promote /demote\n\n"
    "<b>✅ Approvals</b>\n"
    "  /approve /unapprove /approved /unapproveall /approval\n\n"
    "<b>🔒 Blocklist</b>\n"
    "  /addblocklist /rmblocklist /blocklist\n"
    "  /blocklistmode /blocklistdelete\n"
    "  /setblocklistreason /resetblocklistreason /unblocklistall\n\n"
    "<b>🌐 Federation</b>\n"
    "  /newfed /joinfed /leavefed\n"
    "  /fedban /unfedban /fedpromote /feddemote\n"
    "  /fedadmins /fedinfo /fedchats\n\n"
    "<b>🌊 Anti-Raid & Flood</b>\n"
    "  /antiraid /raidtime /raidactiontime /autoantiraid\n"
    "  /flood /setflood /setfloodtimer /floodmode /clearflood\n\n"
    "<b>ℹ️ Info</b>\n"
    "  /chatid /staff /status\n"
)

_HELP_ADMIN_EXTRA = (
    "\n<b>🔑 Superadmin / Bot Admin</b>\n"
    "  /admin_custommessage — Post a custom formatted message\n"
    "  /admin_edit — Edit a previously sent bot message\n"
    "  /privacy_post — Post Privacy &amp; Adblocking menu\n"
    "  /ai_post — Post AI Tools menu\n"
    "  /dl_post — Post Downloading Tools menu\n"
    "  /tr_post — Post Torrenting Tools menu\n"
    "  /ft_post — Post File Tools menu\n"
    "  /it_post — Post Internet Tools menu\n"
    "  /tt_post — Post Text Tools menu\n"
    "  /dt_post — Post Developer Tools menu\n"
    "  /bot — 📡 Broadcaster control panel (DM only)\n"
    "  /admincache /anonadmin /adminerror\n"
)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show context-aware command guide."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    is_group = chat.type in ("group", "supergroup")
    uname = (user.username or "").lower()
    is_superadmin = uname in SUPERADMIN_USERNAMES or user.id in _superadmin_ids
    # Check if user is admin in any configured group
    is_any_admin = is_superadmin
    if not is_any_admin:
        for gid in GROUP_IDS:
            if await is_admin(context.bot, gid, user.id, uname or None):
                is_any_admin = True
                break

    text = _HELP_PUBLIC
    if is_any_admin:
        text += _HELP_ADMIN_EXTRA

    if is_group:
        # In groups: send as reply, keep it brief with DM suggestion
        await update.message.reply_text(
            text + "\n💬 <i>For the full guide, DM me directly.</i>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(text, parse_mode="HTML")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show live runtime status — superadmins and group admins only."""
    user = update.effective_user
    if user is None:
        return
    uname = (user.username or "").lower()
    is_superadmin = uname in SUPERADMIN_USERNAMES or user.id in _superadmin_ids
    is_any_admin = is_superadmin
    if not is_any_admin:
        for gid in GROUP_IDS:
            if await is_admin(context.bot, gid, user.id, uname or None):
                is_any_admin = True
                break
    if not is_any_admin:
        await update.message.reply_text("⛔ Admins only.")
        return

    # Uptime
    elapsed = time.time() - _BOT_START_TIME
    hours, rem = divmod(int(elapsed), 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}h {mins}m {secs}s"

    # Cache info
    cached_groups = len(_admin_cache)
    total_cached_admins = sum(len(v) for v in _admin_cache.values())
    known_superadmins = len(_superadmin_ids)

    mode = "🪝 Webhook" if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "🔄 Polling"

    text = (
        "📊 <b>Bot Status</b>\n\n"
        f"⏱ <b>Uptime:</b> <code>{uptime_str}</code>\n"
        f"🌐 <b>Mode:</b> {mode}\n\n"
        f"👥 <b>Configured groups:</b> {len(GROUP_IDS)}\n"
        f"💾 <b>Admin-cached groups:</b> {cached_groups} "
        f"({total_cached_admins} admins cached)\n"
        f"🔑 <b>Known superadmins:</b> {known_superadmins}\n\n"
        f"📋 <b>Groups monitored:</b>\n"
        + "\n".join(f"  • <code>{gid}</code>" for gid in GROUP_IDS)
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def features_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the bot roadmap / planned features."""
    text = (
        "🗺️ <b>𝔾𝕠𝕣𝕕𝕠's Bot — Feature Roadmap</b>\n\n"
        "<b>✅ Implemented</b>\n"
        "  📝 <b>Mod Logs</b> — Forward mod actions to a private log channel with full context (/setmodlog).\n\n"
        "  📊 <b>Offender Scoring</b> — Track per-user infractions and auto-escalate (warn → mute → ban). (/warn, /infractions)\n\n"
        "  📜 <b>Onboarding Rules</b> — DMs custom rules to new members automatically. (/setrules)\n\n"
        "  💾 <b>Backup &amp; Restore</b> — Export/import group data to JSON. (/backup, /restore)\n\n"
        "<b>🔜 Coming Soon</b>\n"
        "  🤖 <b>Spam Scoring</b> — Heuristic scoring for repeated messages, "
        "link-dropping, and new-account behaviour with configurable thresholds\n\n"
        "  📅 <b>Daily Digest</b> — Scheduled daily summary posted to a log "
        "channel: new members, bans, mutes, join requests, BTC price\n\n"
        "💡 <i>Have an idea? Let a superadmin know!</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ── Global error handler ─────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log all unhandled exceptions so the bot never silently dies."""
    import traceback
    logger.error(
        "Unhandled exception while processing update %s",
        update,
        exc_info=context.error,
    )
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    logger.error("Traceback:\n%s", tb)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── Register global error handler ──
    app.add_error_handler(error_handler)

    # ── Superadmin ID tracking (runs before all other handlers) ──
    app.add_handler(TypeHandler(Update, _track_superadmin), group=-1)

    # ── DM flow ──
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_lang, pattern=r"^lang_"))
    app.add_handler(CallbackQueryHandler(handle_math, pattern=r"^math_"))

    # ── Join request handling ──
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # ── New member welcome + captcha ──
    app.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))

    # ── DM / general commands ──
    app.add_handler(CommandHandler("help",     help_cmd))
    app.add_handler(CommandHandler("features", features_cmd))
    app.add_handler(CommandHandler("status",   status_cmd))

    # ── Group commands ──
    app.add_handler(CommandHandler("chatid", chatid_cmd))
    app.add_handler(CommandHandler("staff", staff_cmd))
    app.add_handler(CommandHandler("admincache", admincache_cmd))

    app.add_handler(CommandHandler("anonadmin", anonadmin_cmd))
    app.add_handler(CommandHandler("adminerror", adminerror_cmd))
    app.add_handler(CommandHandler("promote", promote_cmd))
    app.add_handler(CommandHandler("demote", demote_cmd))

    # ── Ban / Mute / Kick commands ──
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("dban", dban_cmd))
    app.add_handler(CommandHandler("sban", sban_cmd))
    app.add_handler(CommandHandler("tban", tban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CallbackQueryHandler(unban_callback, pattern=r"^unban_"))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("dmute", dmute_cmd))
    app.add_handler(CommandHandler("smute", smute_cmd))
    app.add_handler(CommandHandler("tmute", tmute_cmd))
    app.add_handler(CommandHandler("unmute", unmute_cmd))
    app.add_handler(CallbackQueryHandler(unmute_callback, pattern=r"^unmute_"))
    app.add_handler(CommandHandler("kick", kick_cmd))
    app.add_handler(CommandHandler("dkick", dkick_cmd))
    app.add_handler(CommandHandler("skick", skick_cmd))
    app.add_handler(CommandHandler("kickme", kickme_cmd))

    # ── Admin settings ── (hyphen in name, so we use a regex handler)
    app.add_handler(MessageHandler(
        filters.Regex(r"^/admin-settings\b"),
        admin_settings_cmd,
    ))
    app.add_handler(CallbackQueryHandler(admin_settings_callback, pattern=r"^aset_"))

    # ── Custom message wizard (2-step conversation) ──
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(r"^/admin_custommessage\b") & filters.ChatType.PRIVATE,
            custommessage_start,
        )],
        states={
            _CUSTOMMSG_TEXT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                custommessage_got_text,
            )],
            _CUSTOMMSG_TARGET: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                custommessage_got_target,
            )],
        },
        fallbacks=[CommandHandler("cancel", custommessage_cancel)],
        per_user=True, per_chat=True,
    ))

    # ── Admin-edit wizard ──
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(r"^/admin_edit\b") & filters.ChatType.PRIVATE,
            adminedit_start,
        )],
        states={
            _ADMINEDIT_URL: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                adminedit_got_url,
            )],
            _ADMINEDIT_CONFIRM: [CallbackQueryHandler(
                adminedit_confirm_callback, pattern=r"^aedit_(yes|no)$"
            )],
            _ADMINEDIT_NEWTEXT: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                adminedit_got_newtext,
            )],
        },
        fallbacks=[CommandHandler("cancel", adminedit_cancel)],
        per_user=True, per_chat=True,
    ))

    # ── New Feature Commands ──
    app.add_handler(CommandHandler("warn", warn_cmd))
    app.add_handler(CommandHandler("infractions", infractions_cmd))
    app.add_handler(CommandHandler("clearinfractions", clearinfractions_cmd))
    
    app.add_handler(CommandHandler("modlog", modlog_cmd))
    app.add_handler(CommandHandler("setmodlog", setmodlog_cmd))
    
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("setrules", setrules_cmd))
    
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("restore", restore_cmd))

    # ── Federation commands ──
    app.add_handler(CommandHandler("newfed", newfed_cmd))
    app.add_handler(CommandHandler("joinfed", joinfed_cmd))
    app.add_handler(CommandHandler("leavefed", leavefed_cmd))
    app.add_handler(CommandHandler("fedban", fedban_cmd))
    app.add_handler(CommandHandler("unfedban", unfedban_cmd))
    app.add_handler(CommandHandler("fedpromote", fedpromote_cmd))
    app.add_handler(CommandHandler("feddemote", feddemote_cmd))
    app.add_handler(CommandHandler("fedadmins", fedadmins_cmd))
    app.add_handler(CommandHandler("fedinfo", fedinfo_cmd))
    app.add_handler(CommandHandler("fedchats", fedchats_cmd))

    # ── Blocklist commands ──
    app.add_handler(CommandHandler("addblocklist", addblocklist_cmd))
    app.add_handler(CommandHandler("rmblocklist", rmblocklist_cmd))
    app.add_handler(CommandHandler("blocklist", blocklist_cmd))
    app.add_handler(CommandHandler("blocklistmode", blocklistmode_cmd))
    app.add_handler(CommandHandler("blocklistdelete", blocklistdelete_cmd))
    app.add_handler(CommandHandler("setblocklistreason", setblocklistreason_cmd))
    app.add_handler(CommandHandler("resetblocklistreason", resetblocklistreason_cmd))
    app.add_handler(CommandHandler("unblocklistall", unblocklistall_cmd))

    # ── Approval commands ──
    app.add_handler(CommandHandler("approve", approve_cmd))
    app.add_handler(CommandHandler("unapprove", unapprove_cmd))
    app.add_handler(CommandHandler("approved", approved_cmd))
    app.add_handler(CommandHandler("unapproveall", unapproveall_cmd))
    app.add_handler(CommandHandler("approval", approval_cmd))

    # ── Anti-raid commands ──
    app.add_handler(CommandHandler("antiraid", antiraid_cmd))
    app.add_handler(CommandHandler("raidtime", raidtime_cmd))
    app.add_handler(CommandHandler("raidactiontime", raidactiontime_cmd))
    app.add_handler(CommandHandler("autoantiraid", autoantiraid_cmd))

    # ── Flood control commands ──
    app.add_handler(CommandHandler("flood", flood_cmd))
    app.add_handler(CommandHandler("setflood", setflood_cmd))
    app.add_handler(CommandHandler("setfloodtimer", setfloodtimer_cmd))
    app.add_handler(CommandHandler("floodmode", floodmode_cmd))
    app.add_handler(CommandHandler("clearflood", clearflood_cmd))

    # ── Broadcaster /bot command ──
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("bot", bot_broadcast_cmd)],
        states={
            # Setup wizard: collect credentials interactively
            _BCAST_SETUP_API_ID: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                bcast_got_api_id,
            )],
            _BCAST_SETUP_API_HASH: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                bcast_got_api_hash,
            )],
            _BCAST_SETUP_PHONE: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                bcast_got_phone,
            )],
            # Auth: login code + optional 2FA
            _BCAST_AUTH_CODE: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                bcast_got_code,
            )],
            _BCAST_AUTH_2FA: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                bcast_got_2fa,
            )],
            # Config editing: new value for delay/cap/variance/warmup
            _BCAST_CFG_VALUE: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                bcast_cfg_got_value,
            )],
            # Add message template
            _BCAST_ADD_MSG: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                bcast_add_msg_got_text,
            )],
        },
        fallbacks=[CommandHandler("cancel", bcast_auth_cancel)],
        per_user=True, per_chat=True,
    ))
    app.add_handler(CallbackQueryHandler(bcast_callback, pattern=r"^bcast_"))
    # Fallback handler for config edits triggered via inline button (user_data flag)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        bcast_inline_text_handler,
    ), group=1)

    # ── Message handler for blocklist + flood (all text messages in groups) ──
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        check_message,
    ))

    # ── Service message cleanup (groups only) ──
    # Catches: new members joined, member left, pinned message,
    # new chat title, new chat photo, delete chat photo,
    # group created, supergroup created, channel created,
    # migrate to/from chat id, video chat started/ended/participants invited.
    service_filter = (
        filters.StatusUpdate.NEW_CHAT_MEMBERS
        | filters.StatusUpdate.LEFT_CHAT_MEMBER
        | filters.StatusUpdate.PINNED_MESSAGE
        | filters.StatusUpdate.NEW_CHAT_TITLE
        | filters.StatusUpdate.NEW_CHAT_PHOTO
        | filters.StatusUpdate.DELETE_CHAT_PHOTO
    )
    app.add_handler(MessageHandler(service_filter, cleanup_service_message))

    # ── 𝔾𝕠𝕣𝕕𝕠 Privacy interactive menu ──
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(r"^/privacy_post\b") & filters.ChatType.PRIVATE,
            gordo_post_cmd,
        )],
        states={
            _GORDO_POST_TARGET: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                gordo_post_got_target,
            )],
        },
        fallbacks=[CommandHandler("cancel", gordo_post_cancel)],
        per_user=True, per_chat=True,
    ))
    app.add_handler(CallbackQueryHandler(gordo_callback, pattern=r"^gordo_"))

    # ── AI — Artificial Intelligence interactive menu ──
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(r"^/ai_post\b") & filters.ChatType.PRIVATE,
            ai_post_cmd,
        )],
        states={
            _AI_POST_TARGET: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                ai_post_got_target,
            )],
        },
        fallbacks=[CommandHandler("cancel", ai_post_cancel)],
        per_user=True, per_chat=True,
    ))
    app.add_handler(CallbackQueryHandler(ai_callback, pattern=r"^ai_"))

    # ── Downloading interactive menu ──
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(r"^/dl_post\b") & filters.ChatType.PRIVATE,
            dl_post_cmd,
        )],
        states={
            _DL_POST_TARGET: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                dl_post_got_target,
            )],
        },
        fallbacks=[CommandHandler("cancel", dl_post_cancel)],
        per_user=True, per_chat=True,
    ))
    app.add_handler(CallbackQueryHandler(dl_callback, pattern=r"^dl_"))

    # ── Torrenting interactive menu ──
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(r"^/tr_post\b") & filters.ChatType.PRIVATE,
            tr_post_cmd,
        )],
        states={
            _TR_POST_TARGET: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                tr_post_got_target,
            )],
        },
        fallbacks=[CommandHandler("cancel", tr_post_cancel)],
        per_user=True, per_chat=True,
    ))
    app.add_handler(CallbackQueryHandler(tr_callback, pattern=r"^tr_"))

    # ── File Tools interactive menu ──
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(r"^/ft_post\b") & filters.ChatType.PRIVATE,
            ft_post_cmd,
        )],
        states={
            _FT_POST_TARGET: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                ft_post_got_target,
            )],
        },
        fallbacks=[CommandHandler("cancel", ft_post_cancel)],
        per_user=True, per_chat=True,
    ))
    app.add_handler(CallbackQueryHandler(ft_callback, pattern=r"^ft_"))

    # ── Internet Tools interactive menu ──
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(r"^/it_post\b") & filters.ChatType.PRIVATE,
            it_post_cmd,
        )],
        states={
            _IT_POST_TARGET: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                it_post_got_target,
            )],
        },
        fallbacks=[CommandHandler("cancel", it_post_cancel)],
        per_user=True, per_chat=True,
    ))
    app.add_handler(CallbackQueryHandler(it_callback, pattern=r"^it_"))

    # ── Text Tools interactive menu ──
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(r"^/tt_post\b") & filters.ChatType.PRIVATE,
            tt_post_cmd,
        )],
        states={
            _TT_POST_TARGET: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                tt_post_got_target,
            )],
        },
        fallbacks=[CommandHandler("cancel", tt_post_cancel)],
        per_user=True, per_chat=True,
    ))
    app.add_handler(CallbackQueryHandler(tt_callback, pattern=r"^tt_"))

    # ── Developer Tools interactive menu ──
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(
            filters.Regex(r"^/dt_post\b") & filters.ChatType.PRIVATE,
            dt_post_cmd,
        )],
        states={
            _DT_POST_TARGET: [MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
                dt_post_got_target,
            )],
        },
        fallbacks=[CommandHandler("cancel", dt_post_cancel)],
        per_user=True, per_chat=True,
    ))
    app.add_handler(CallbackQueryHandler(dt_callback, pattern=r"^dt_"))

    # ── Railway.com webhook / local polling ────────────────────────────────
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    port = int(os.environ.get("PORT", 8080))

    if railway_domain:
        # Webhook mode — PTB owns the port, no extra server needed
        base_url = f"https://{railway_domain}"
        webhook_path = f"webhook/{BOT_TOKEN}"
        webhook_url = f"{base_url}/{webhook_path}"
        logger.info(
            "Startup mode=WEBHOOK | base_url=%s | webhook_url=%s | listen=0.0.0.0:%s",
            base_url,
            webhook_url,
            port,
        )
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        # Polling mode — start a tiny health-check server so Railway doesn't
        # kill the container for not listening on PORT.
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            def log_message(self, *args):
                pass

        threading.Thread(
            target=lambda: HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever(),
            daemon=True,
            name="health-check",
        ).start()
        logger.info("Polling mode — health-check server on port %s", port)

        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )


if __name__ == "__main__":
    main()
