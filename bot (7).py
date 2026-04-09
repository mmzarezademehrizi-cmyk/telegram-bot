import os
import sys
import json
import time
import random
import threading
import urllib.parse
import logging
from collections import deque

import telebot
from telebot import types
from flask import Flask

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = 7655519400
DEFAULT_VIPS = [549558305, 7349434960]
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")
AGENT_TASKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_tasks.txt")
TEXTS_PER_WINDOW = 8
DEFAULT_LIMIT_HOURS = 2
LOW_STOCK_THRESHOLD = 10
RATE_LIMIT_CLICKS = 3
RATE_LIMIT_WINDOW = 10
RATE_LIMIT_BLOCK = 15 * 60
BACKUP_INTERVAL = 24 * 3600
FLASK_PORT = 5000
MAX_ERROR_LOGS = 50

DEFAULT_OP_START_MSG = (
    "!کمربندا رو سفت ببندید\n"
    "شروع {hashtag} این مرحله تازه اول راهه؛ عملیات شد.\n"
    "بریم که داشته باشیم یه پیروزی مشتی."
)
DEFAULT_OP_CANCEL_MSG = (
    "!نقطه، سر خط\n"
    "تمام شد {hashtag} این مرحله رو رد کردیم و عملیات تمام شد.\n"
    "منتظر اتفاقای بعدی باشید."
)

DEFAULT_WELCOME = (
    "سلام! 👋 به ربات محتوا خوش آمدید.\n\n"
    "🔢 دکمه‌های ۱ تا ۸: دریافت متن\n"
    "🎲 Surprise Me: تصادفی\n"
    "👤 پروفایل من: اطلاعات حساب و تاریخچه\n"
    "🏷 هشتگ چیست؟: هشتگ فعلی\n"
    "☎️ ارتباط با مدیریت: ارسال تیکت\n"
    "💡 پیشنهاد متن: ارسال متن پیشنهادی\n\n"
    "یکی از گزینه‌های زیر را انتخاب کنید:"
)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
app = Flask(__name__)

conv_state = {}
rate_tracker = {}
error_logs = deque(maxlen=MAX_ERROR_LOGS)


# ─────────────────────────────────────────────
# STATE I/O
# ─────────────────────────────────────────────
def default_state():
    return {
        "available_texts": [],
        "used_texts": {},
        "user_data": {},
        "vip_users": list(DEFAULT_VIPS),
        "operation_active": True,
        "current_hashtag": "",
        "user_limits": {},
        "welcome_message": DEFAULT_WELCOME,
        "suggested_texts": [],
        "hashtag_list": [],
        "op_start_msg": DEFAULT_OP_START_MSG,
        "op_cancel_msg": DEFAULT_OP_CANCEL_MSG,
    }


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default_state().items():
            if k not in data:
                data[k] = v
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return default_state()


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def append_agent_task(text):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(AGENT_TASKS_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n[{timestamp}] ADMIN TASK:\n{text}\n{'─'*50}\n")


def log_error(msg):
    ts = time.strftime("%H:%M:%S")
    error_logs.append(f"[{ts}] {msg}")


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def escape_md2(text):
    special = r'\_*[]()~`>#+-=|{}.!'
    out = []
    for ch in str(text):
        if ch in special:
            out.append('\\')
        out.append(ch)
    return ''.join(out)


def is_vip(uid):
    state = load_state()
    return int(uid) in state.get("vip_users", [])


def is_admin(uid):
    return int(uid) == ADMIN_ID


def user_name(user):
    name = (user.first_name or "").strip()
    if user.last_name:
        name = (name + " " + user.last_name).strip()
    return name or str(user.id)


def get_limit_secs(uid):
    state = load_state()
    hours = state.get("user_limits", {}).get(str(uid), DEFAULT_LIMIT_HOURS)
    return int(hours) * 3600


def register_user(user):
    state = load_state()
    uid = str(user.id)
    is_new = uid not in state.get("user_data", {})
    if is_new:
        state["user_data"][uid] = {
            "name": user_name(user),
            "first_seen": time.time(),
            "received_count": 0,
            "last_window_start": 0,
            "used_in_window": 0,
        }
    else:
        state["user_data"][uid]["name"] = user_name(user)
    save_state(state)
    if is_new and not is_admin(user.id):
        try:
            bot.send_message(ADMIN_ID, f"🆕 کاربر جدید!\nنام: {user_name(user)}\nآیدی: {user.id}")
        except Exception:
            pass


def check_low_stock():
    state = load_state()
    n = len(state.get("available_texts", []))
    if 0 < n < LOW_STOCK_THRESHOLD:
        try:
            bot.send_message(ADMIN_ID, f"⚠️ هشدار موجودی: {n} متن باقی‌مانده!")
        except Exception:
            pass


def check_rate(uid):
    now = time.time()
    key = str(uid)
    if key not in rate_tracker:
        rate_tracker[key] = {"clicks": [], "blocked_until": 0}
    info = rate_tracker[key]
    if info["blocked_until"] > now:
        left = int(info["blocked_until"] - now)
        return False, f"⛔ {left // 60} دقیقه و {left % 60} ثانیه مسدود هستید."
    info["clicks"] = [t for t in info["clicks"] if now - t < RATE_LIMIT_WINDOW]
    info["clicks"].append(now)
    if len(info["clicks"]) > RATE_LIMIT_CLICKS:
        info["blocked_until"] = now + RATE_LIMIT_BLOCK
        return False, "⛔ کلیک بیش از حد! ۱۵ دقیقه مسدود شدید."
    return True, ""


def count_active_users():
    state = load_state()
    cutoff = time.time() - 86400
    count = 0
    for info in state.get("user_data", {}).values():
        if info.get("last_window_start", 0) > cutoff or info.get("received_count", 0) > 0:
            count += 1
    return count


# ─────────────────────────────────────────────
# TEXT DELIVERY
# ─────────────────────────────────────────────
def fetch_texts(uid, requested):
    state = load_state()
    pool = state.get("available_texts", [])
    if not pool:
        return [], "❌ مخزن خالی است. لطفاً بعداً تلاش کنید."

    uid_int = int(uid)
    uid = str(uid)
    limit_reset_notify = False

    if not is_vip(uid) and not is_admin(uid_int):
        info = state["user_data"].get(uid, {})
        now = time.time()
        lim = get_limit_secs(uid)
        ws = info.get("last_window_start", 0)
        used = info.get("used_in_window", 0)
        if now - ws > lim:
            if used > 0:
                limit_reset_notify = True
            used = 0
            ws = now
            info["last_window_start"] = ws
            info["used_in_window"] = 0
        remaining = TEXTS_PER_WINDOW - used
        if remaining <= 0:
            wait = int(lim - (now - ws))
            h, m, s = wait // 3600, (wait % 3600) // 60, wait % 60
            return [], f"⏳ سقف شما تمام شده.\n{h} ساعت {m} دقیقه {s} ثانیه تا بازنشانی."
        requested = min(requested, remaining)

    requested = min(requested, len(pool))
    selected = random.sample(pool, requested)
    for t in selected:
        pool.remove(t)
    state["available_texts"] = pool

    if uid not in state.get("used_texts", {}):
        state["used_texts"][uid] = []
    state["used_texts"][uid].extend(selected)

    if uid in state["user_data"]:
        info = state["user_data"][uid]
        now = time.time()
        lim = get_limit_secs(uid)
        if now - info.get("last_window_start", 0) > lim:
            info["last_window_start"] = now
            info["used_in_window"] = 0
        info["used_in_window"] = info.get("used_in_window", 0) + requested
        info["received_count"] = info.get("received_count", 0) + requested

    save_state(state)
    check_low_stock()

    if limit_reset_notify:
        try:
            bot.send_message(uid_int, "محدودیت رفع شد! از الان می‌تونی تا سقف ۸ تا متن بگیری.")
        except Exception:
            pass

    return selected, None


def deliver(chat_id, texts):
    for text in texts:
        esc = escape_md2(text)
        enc = urllib.parse.quote(text, safe='')
        url = f"https://twitter.com/intent/tweet?text={enc}"
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🐦 پست در توییتر", url=url))
        try:
            bot.send_message(chat_id, f"```\n{esc}\n```", parse_mode="MarkdownV2", reply_markup=markup)
        except Exception as e:
            logger.warning(f"MarkdownV2 failed: {e}")
            try:
                bot.send_message(chat_id, f"`{esc}`", parse_mode="MarkdownV2", reply_markup=markup)
            except Exception:
                bot.send_message(chat_id, text, reply_markup=markup)


# ─────────────────────────────────────────────
# BROADCAST HELPERS
# ─────────────────────────────────────────────
def _broadcast_all(text):
    state = load_state()
    for u in state.get("user_data", {}).keys():
        try:
            bot.send_message(int(u), text)
        except Exception:
            pass


def _broadcast_start_op(hashtag=""):
    state = load_state()
    tag = hashtag or state.get("current_hashtag", "")
    hashtag_part = f"#{tag}" if tag else "#"
    template = state.get("op_start_msg", DEFAULT_OP_START_MSG)
    msg = template.replace("{hashtag}", hashtag_part)
    _broadcast_all(msg)


def _broadcast_cancel_op():
    state = load_state()
    tag = state.get("current_hashtag", "")
    hashtag_part = f"#{tag}" if tag else "#"
    template = state.get("op_cancel_msg", DEFAULT_OP_CANCEL_MSG)
    msg = template.replace("{hashtag}", hashtag_part)
    _broadcast_all(msg)


# ─────────────────────────────────────────────
# MENUS
# ─────────────────────────────────────────────
def start_markup():
    m = types.InlineKeyboardMarkup(row_width=4)
    m.row(*[types.InlineKeyboardButton(str(i), callback_data=f"get_{i}") for i in range(1, 5)])
    m.row(*[types.InlineKeyboardButton(str(i), callback_data=f"get_{i}") for i in range(5, 9)])
    m.row(types.InlineKeyboardButton("🎲 Surprise Me!", callback_data="surprise"))
    m.row(types.InlineKeyboardButton("👤 پروفایل من", callback_data="u_profile"))
    m.row(types.InlineKeyboardButton("هشتگ چیست؟ 🏷", callback_data="u_hashtag"))
    m.row(types.InlineKeyboardButton("☎️ ارتباط با مدیریت", callback_data="u_support"))
    m.row(types.InlineKeyboardButton("💡 پیشنهاد متن", callback_data="u_suggest"))
    return m


def sistem_markup():
    state = load_state()
    op = "✅ فعال" if state.get("operation_active", True) else "🛑 غیرفعال"
    n_texts = len(state.get("available_texts", []))
    n_users = len(state.get("user_data", {}))
    n_active = count_active_users()
    pending_suggests = len([s for s in state.get("suggested_texts", []) if not s.get("reviewed")])

    m = types.InlineKeyboardMarkup(row_width=2)
    m.row(
        types.InlineKeyboardButton("اضافه کردن متن ➕", callback_data="s_addtext"),
        types.InlineKeyboardButton("تخلیه مخزن 🗑", callback_data="s_wipe"),
    )
    m.row(
        types.InlineKeyboardButton("مدیریت VIP 👑", callback_data="s_vip"),
        types.InlineKeyboardButton("حذف کاربر ❌", callback_data="s_deluser"),
    )
    m.row(
        types.InlineKeyboardButton("جستجوی کاربران 🔍", callback_data="s_search_users"),
        types.InlineKeyboardButton("آمار 📊", callback_data="s_stats"),
    )
    m.row(types.InlineKeyboardButton("محدودیت کاربر ⏳", callback_data="s_limits"))
    m.row(
        types.InlineKeyboardButton("شروع عملیات ✅", callback_data="s_op_on"),
        types.InlineKeyboardButton("لغو عملیات 🏁", callback_data="s_cancel_op"),
    )
    m.row(types.InlineKeyboardButton("توقف موقت عملیات ⏸", callback_data="s_op_off"))
    m.row(
        types.InlineKeyboardButton("پشتیبان‌گیری 💾", callback_data="s_backup"),
        types.InlineKeyboardButton("لاگ خطاها 🪲", callback_data="s_errlogs"),
    )
    m.row(
        types.InlineKeyboardButton("ویرایش پیام خوش‌آمد ✏️", callback_data="s_edit_welcome"),
        types.InlineKeyboardButton("ویرایش پیام‌های عملیات 📝", callback_data="s_edit_op_msgs"),
    )
    m.row(types.InlineKeyboardButton("صحبت با Replit Agent 🤖", callback_data="s_agent"))
    if pending_suggests:
        m.row(types.InlineKeyboardButton(
            f"متون پیشنهادی 💡 ({pending_suggests})", callback_data="s_suggestions"
        ))

    text = (
        f"🔧 پنل مدیریت\n\n"
        f"عملیات: {op}\n"
        f"متون: {n_texts}\n"
        f"کاربران: {n_users}\n"
        f"فعال (۲۴ساعت): {n_active}"
    )
    return m, text


def befrest_markup():
    m = types.InlineKeyboardMarkup()
    m.row(types.InlineKeyboardButton("ارسال پیام به افراد 📩", callback_data="b_msg"))
    m.row(types.InlineKeyboardButton("ارسال متن به افراد 📥", callback_data="b_texts"))
    m.row(types.InlineKeyboardButton("همه کاربران 👥", callback_data="b_listall"))
    return m


# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    register_user(msg.from_user)
    state = load_state()
    welcome = state.get("welcome_message", DEFAULT_WELCOME)
    bot.send_message(msg.chat.id, welcome, reply_markup=start_markup())


@bot.message_handler(commands=["sistem"])
def cmd_sistem(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "⛔ دسترسی غیرمجاز.")
        return
    markup, text = sistem_markup()
    bot.send_message(msg.chat.id, text, reply_markup=markup)


@bot.message_handler(commands=["befrest"])
def cmd_befrest(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "⛔ دسترسی غیرمجاز.")
        return
    bot.send_message(msg.chat.id, "📢 پنل ارسال:", reply_markup=befrest_markup())


@bot.message_handler(commands=["admin"])
def cmd_admin(msg):
    if not is_admin(msg.from_user.id):
        bot.reply_to(msg, "⛔ دسترسی غیرمجاز.")
        return
    send_stats(msg.chat.id)


# ─────────────────────────────────────────────
# CALLBACKS
# ─────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: True)
def on_callback(call):
    uid = str(call.from_user.id)
    data = call.data
    chat = call.message.chat.id

    try:
        # ── USER BUTTONS ──
        if data.startswith("get_"):
            _cb_get(call)

        elif data == "surprise":
            _cb_surprise(call)

        elif data == "u_profile":
            _cb_user_profile(call)

        elif data == "u_hashtag":
            state = load_state()
            tag = state.get("current_hashtag", "")
            bot.send_message(chat, f"🏷 هشتگ فعلی: {tag}" if tag else "🏷 هنوز هشتگی ثبت نشده.")

        elif data == "u_support":
            conv_state[uid] = {"step": "support_msg"}
            bot.send_message(chat, "📩 پیام خود را بنویسید:")

        elif data == "u_suggest":
            conv_state[uid] = {"step": "suggest_text"}
            bot.send_message(chat, "💡 متن پیشنهادی خود را بنویسید:\n(بعد از تأیید مدیریت در مخزن اضافه می‌شود)")

        # ── /sistem ──
        elif data == "s_addtext":
            if not is_admin(call.from_user.id): return
            _send_hashtag_picker(chat, prefix="addtag_pick_", new_btn="addtag_new",
                                 title="🏷 برای کدام هشتگ متن اضافه کنید؟")

        elif data == "addtag_new":
            if not is_admin(call.from_user.id): return
            conv_state[uid] = {"step": "add_hashtag"}
            bot.send_message(chat, "🏷 هشتگ جدید را بنویسید:")

        elif data.startswith("addtag_pick_"):
            if not is_admin(call.from_user.id): return
            idx = int(data[12:])
            state = load_state()
            hashtags = state.get("hashtag_list", [])
            if idx >= len(hashtags):
                bot.send_message(chat, "❌ هشتگ یافت نشد.")
            else:
                chosen = hashtags[idx]
                conv_state[uid] = {"step": "add_content", "data": {"hashtag": chosen}}
                bot.send_message(
                    chat,
                    f"🏷 هشتگ: {chosen}\n\n"
                    f"لطفاً متن‌ها را وارد کنید (جداکننده: ﷼)\n\n"
                    f"مثال:\nمتن اول\n﷼\nمتن دوم"
                )

        elif data == "s_wipe":
            if not is_admin(call.from_user.id): return
            _send_hashtag_picker(chat, prefix="wipe_tag_", new_btn=None,
                                 title="🗑 متون کدام هشتگ حذف شود؟")

        elif data.startswith("wipe_tag_"):
            if not is_admin(call.from_user.id): return
            idx = int(data[9:])
            state = load_state()
            hashtags = state.get("hashtag_list", [])
            if idx >= len(hashtags):
                bot.send_message(chat, "❌ هشتگ یافت نشد.")
            else:
                chosen = hashtags[idx]
                m = types.InlineKeyboardMarkup()
                m.row(
                    types.InlineKeyboardButton("بله ✅", callback_data=f"wipe_tyes_{idx}"),
                    types.InlineKeyboardButton("خیر ❌", callback_data="wipe_tno"),
                )
                bot.send_message(
                    chat,
                    f"⚠️ آیا همه متون هشتگ ({chosen}) حذف شوند؟",
                    reply_markup=m
                )

        elif data.startswith("wipe_tyes_"):
            if not is_admin(call.from_user.id): return
            idx = int(data[10:])
            state = load_state()
            hashtags = state.get("hashtag_list", [])
            if idx >= len(hashtags):
                bot.send_message(chat, "❌ هشتگ یافت نشد.")
            else:
                chosen = hashtags[idx]
                before = len(state.get("available_texts", []))
                state["available_texts"] = [
                    t for t in state.get("available_texts", [])
                    if not t.endswith(f" {chosen}")
                ]
                removed = before - len(state["available_texts"])
                save_state(state)
                bot.send_message(chat, f"✅ {removed} متن با هشتگ ({chosen}) حذف شد.")

        elif data == "wipe_tno":
            bot.send_message(chat, "❌ لغو شد.")

        elif data == "s_vip":
            if not is_admin(call.from_user.id): return
            _send_vip_panel(chat)

        elif data == "vip_add":
            if not is_admin(call.from_user.id): return
            conv_state[uid] = {"step": "vip_add_id"}
            bot.send_message(chat, "🆔 آیدی کاربر VIP جدید را ارسال کنید:")

        elif data.startswith("vip_rm_"):
            if not is_admin(call.from_user.id): return
            target = int(data[7:])
            state = load_state()
            if target in state["vip_users"]:
                state["vip_users"].remove(target)
                save_state(state)
                bot.send_message(chat, f"✅ کاربر {target} از VIP حذف شد.")
                try: bot.send_message(target, "❌ شما دیگر عضو مشترکین VIP نیستید")
                except Exception: pass
            else:
                bot.send_message(chat, "❌ در لیست VIP نیست.")

        elif data == "s_deluser":
            if not is_admin(call.from_user.id): return
            _send_del_user_list(chat)

        elif data.startswith("delu_pick_"):
            if not is_admin(call.from_user.id): return
            target = data[10:]
            m = types.InlineKeyboardMarkup()
            m.row(
                types.InlineKeyboardButton("بله ✅", callback_data=f"delu_yes_{target}"),
                types.InlineKeyboardButton("خیر ❌", callback_data="delu_no"),
            )
            bot.send_message(chat, f"کاربر {target} حذف شود؟", reply_markup=m)

        elif data.startswith("delu_yes_"):
            if not is_admin(call.from_user.id): return
            target = data[9:]
            state = load_state()
            removed = target in state.get("user_data", {})
            state.get("user_data", {}).pop(target, None)
            state.get("used_texts", {}).pop(target, None)
            save_state(state)
            bot.send_message(chat, f"✅ کاربر {target} حذف شد." if removed else "❌ یافت نشد.")

        elif data == "delu_no":
            bot.send_message(chat, "❌ لغو شد.")

        elif data == "s_search_users":
            if not is_admin(call.from_user.id): return
            _send_user_search_list(chat)

        elif data.startswith("search_u_"):
            if not is_admin(call.from_user.id): return
            target = data[9:]
            _send_user_detail(chat, target)

        elif data == "s_stats":
            if not is_admin(call.from_user.id): return
            send_stats(chat)

        elif data == "s_limits":
            if not is_admin(call.from_user.id): return
            _send_limits_list(chat)

        elif data.startswith("lim_pick_"):
            if not is_admin(call.from_user.id): return
            target = data[9:]
            _send_hour_picker(chat, target)

        elif data.startswith("lim_set_"):
            if not is_admin(call.from_user.id): return
            parts = data[8:].rsplit("_", 1)
            target, hours = parts[0], int(parts[1])
            state = load_state()
            state.setdefault("user_limits", {})[target] = hours
            save_state(state)
            bot.send_message(chat, f"✅ محدودیت کاربر {target} = {hours} ساعت")

        elif data == "s_op_on":
            if not is_admin(call.from_user.id): return
            _send_hashtag_picker(chat, prefix="op_start_tag_", new_btn=None,
                                 title="✅ هشتگ این عملیات را انتخاب کنید:")

        elif data.startswith("op_start_tag_"):
            if not is_admin(call.from_user.id): return
            idx = int(data[13:])
            state = load_state()
            hashtags = state.get("hashtag_list", [])
            if idx >= len(hashtags):
                bot.send_message(chat, "❌ هشتگ یافت نشد.")
            else:
                chosen = hashtags[idx]
                state["operation_active"] = True
                state["current_hashtag"] = chosen
                save_state(state)
                bot.send_message(chat, f"✅ عملیات با هشتگ ({chosen}) شروع شد. در حال ارسال پیام...")
                threading.Thread(target=_broadcast_start_op, args=(chosen,), daemon=True).start()

        elif data == "s_cancel_op":
            if not is_admin(call.from_user.id): return
            state = load_state()
            state["operation_active"] = False
            save_state(state)
            bot.send_message(chat, "🏁 عملیات لغو شد. در حال ارسال پیام به همه کاربران...")
            threading.Thread(target=_broadcast_cancel_op, daemon=True).start()

        elif data == "s_op_off":
            if not is_admin(call.from_user.id): return
            state = load_state()
            state["operation_active"] = False
            save_state(state)
            bot.send_message(chat, "⏸ عملیات متوقف شد. در حال اطلاع‌رسانی...")
            threading.Thread(target=lambda: _broadcast_all(".عملیات موقتاً متوقف شد"), daemon=True).start()

        elif data == "s_backup":
            if not is_admin(call.from_user.id): return
            _send_backup(chat)

        elif data == "s_errlogs":
            if not is_admin(call.from_user.id): return
            _send_error_logs(chat)

        elif data == "s_edit_welcome":
            if not is_admin(call.from_user.id): return
            conv_state[uid] = {"step": "edit_welcome"}
            state = load_state()
            bot.send_message(chat, "✏️ پیام خوش‌آمد جدید را بنویسید:\n(متن فعلی برای کپی:")
            bot.send_message(chat, state.get("welcome_message", DEFAULT_WELCOME))

        elif data == "s_edit_op_msgs":
            if not is_admin(call.from_user.id): return
            state = load_state()
            m = types.InlineKeyboardMarkup()
            m.row(types.InlineKeyboardButton("✏️ پیام شروع عملیات", callback_data="s_edit_op_start"))
            m.row(types.InlineKeyboardButton("✏️ پیام لغو عملیات", callback_data="s_edit_op_cancel"))
            cur_start = state.get("op_start_msg", DEFAULT_OP_START_MSG)
            cur_cancel = state.get("op_cancel_msg", DEFAULT_OP_CANCEL_MSG)
            bot.send_message(
                chat,
                f"📝 پیام‌های عملیات\n\n"
                f"🔹 پیام شروع فعلی:\n{cur_start}\n\n"
                f"🔸 پیام لغو فعلی:\n{cur_cancel}\n\n"
                f"(از {{hashtag}} برای جای‌گذاری هشتگ استفاده کنید)",
                reply_markup=m
            )

        elif data == "s_edit_op_start":
            if not is_admin(call.from_user.id): return
            state = load_state()
            conv_state[uid] = {"step": "edit_op_start"}
            bot.send_message(chat, "✏️ پیام جدید شروع عملیات:\n(متن فعلی:")
            bot.send_message(chat, state.get("op_start_msg", DEFAULT_OP_START_MSG))

        elif data == "s_edit_op_cancel":
            if not is_admin(call.from_user.id): return
            state = load_state()
            conv_state[uid] = {"step": "edit_op_cancel"}
            bot.send_message(chat, "✏️ پیام جدید لغو عملیات:\n(متن فعلی:")
            bot.send_message(chat, state.get("op_cancel_msg", DEFAULT_OP_CANCEL_MSG))

        elif data == "s_agent":
            if not is_admin(call.from_user.id): return
            conv_state[uid] = {"step": "agent_task"}
            bot.send_message(chat, "🤖 پیام خود را برای Replit Agent بنویسید.\nاین پیام در agent_tasks.txt ذخیره خواهد شد:")

        elif data == "s_suggestions":
            if not is_admin(call.from_user.id): return
            _send_suggestions_panel(chat)

        elif data.startswith("suggest_approve_"):
            if not is_admin(call.from_user.id): return
            idx = int(data[16:])
            _approve_suggestion(chat, idx)

        elif data.startswith("suggest_reject_"):
            if not is_admin(call.from_user.id): return
            idx = int(data[15:])
            _reject_suggestion(chat, idx)

        # ── /befrest ──
        elif data == "b_msg":
            if not is_admin(call.from_user.id): return
            m = types.InlineKeyboardMarkup()
            m.row(types.InlineKeyboardButton("به یک نفر 👤", callback_data="bm_one"))
            m.row(types.InlineKeyboardButton("به همه 📢", callback_data="bm_all"))
            bot.send_message(chat, "ارسال به:", reply_markup=m)

        elif data == "bm_one":
            if not is_admin(call.from_user.id): return
            _send_user_pick_list(chat, prefix="bm_pick_")

        elif data.startswith("bm_pick_"):
            if not is_admin(call.from_user.id): return
            target = data[8:]
            conv_state[uid] = {"step": "bm_one_msg", "data": {"target": target}}
            bot.send_message(chat, f"✉️ پیام به کاربر {target}:")

        elif data == "bm_all":
            if not is_admin(call.from_user.id): return
            conv_state[uid] = {"step": "bm_all_msg"}
            bot.send_message(chat, "📢 پیام همگانی:")

        elif data == "b_texts":
            if not is_admin(call.from_user.id): return
            _send_user_pick_list(chat, prefix="bt_pick_")

        elif data.startswith("bt_pick_"):
            if not is_admin(call.from_user.id): return
            target = data[8:]
            m = types.InlineKeyboardMarkup(row_width=5)
            m.add(*[types.InlineKeyboardButton(str(i), callback_data=f"bt_send_{target}_{i}") for i in range(1, 11)])
            bot.send_message(chat, f"چند متن برای {target} ارسال شود؟", reply_markup=m)

        elif data.startswith("bt_send_"):
            if not is_admin(call.from_user.id): return
            parts = data[8:].rsplit("_", 1)
            target, n = parts[0], int(parts[1])
            texts, err = fetch_texts(ADMIN_ID, n)
            if err:
                bot.send_message(chat, err)
            else:
                try:
                    deliver(int(target), texts)
                    bot.send_message(chat, f"✅ {len(texts)} متن به {target} ارسال شد.")
                except Exception as e:
                    bot.send_message(chat, f"❌ خطا: {e}")

        elif data == "b_listall":
            if not is_admin(call.from_user.id): return
            state = load_state()
            users = state.get("user_data", {})
            if not users:
                bot.send_message(chat, "هیچ کاربری ثبت نشده.")
            else:
                lines = ["👥 همه کاربران:\n"]
                for u, info in users.items():
                    tag = " 👑" if int(u) in state.get("vip_users", []) else ""
                    lines.append(f"• {info.get('name', u)}{tag} | {u}")
                bot.send_message(chat, "\n".join(lines))

        # ── ADMIN REPLY TO USER ──
        elif data.startswith("areply_"):
            if not is_admin(call.from_user.id): return
            target = data[7:]
            conv_state[uid] = {"step": "admin_reply", "data": {"target": target}}
            bot.send_message(chat, f"💬 پاسخ به {target}:")

    except Exception as e:
        err_msg = f"Callback [{data}] error: {e}"
        logger.error(err_msg, exc_info=True)
        log_error(err_msg)

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


# ─────────────────────────────────────────────
# CALLBACK HELPERS
# ─────────────────────────────────────────────
def _cb_get(call):
    uid = call.from_user.id
    state = load_state()
    if not state.get("operation_active", True) and not is_admin(uid):
        bot.send_message(call.message.chat.id, "عملیاتی نیست")
        return
    if not is_vip(uid) and not is_admin(uid):
        ok, msg = check_rate(uid)
        if not ok:
            bot.send_message(call.message.chat.id, msg)
            return
    n = int(call.data.split("_")[1])
    texts, err = fetch_texts(uid, n)
    if err:
        bot.send_message(call.message.chat.id, err)
    else:
        deliver(call.message.chat.id, texts)


def _cb_surprise(call):
    uid = call.from_user.id
    state = load_state()
    if not state.get("operation_active", True) and not is_admin(uid):
        bot.send_message(call.message.chat.id, "عملیاتی نیست")
        return
    if not is_vip(uid) and not is_admin(uid):
        ok, msg = check_rate(uid)
        if not ok:
            bot.send_message(call.message.chat.id, msg)
            return
    n = random.randint(1, 8)
    texts, err = fetch_texts(uid, n)
    if err:
        bot.send_message(call.message.chat.id, err)
    else:
        bot.send_message(call.message.chat.id, f"🎲 {n} متن تصادفی:")
        deliver(call.message.chat.id, texts)


def _cb_user_profile(call):
    uid = str(call.from_user.id)
    state = load_state()
    info = state.get("user_data", {}).get(uid, {})
    vip_tag = "✅ VIP" if is_vip(uid) else "عادی"
    first_seen = info.get("first_seen", 0)
    join_date = time.strftime("%Y-%m-%d", time.localtime(first_seen)) if first_seen else "نامشخص"
    received = info.get("received_count", 0)
    lim = state.get("user_limits", {}).get(uid, DEFAULT_LIMIT_HOURS)

    now = time.time()
    lim_secs = get_limit_secs(uid)
    ws = info.get("last_window_start", 0)
    used = info.get("used_in_window", 0)
    if now - ws > lim_secs:
        remaining = TEXTS_PER_WINDOW
    else:
        remaining = max(0, TEXTS_PER_WINDOW - used)

    bot.send_message(
        call.message.chat.id,
        f"👤 پروفایل شما\n\n"
        f"🆔 آیدی: {uid}\n"
        f"📅 تاریخ عضویت: {join_date}\n"
        f"📝 مجموع متون دریافتی: {received}\n"
        f"👑 وضعیت: {vip_tag}\n"
        f"⏳ محدودیت: {lim} ساعت\n"
        f"📊 باقی‌مانده این دوره: {remaining} از {TEXTS_PER_WINDOW}"
    )


def _send_hashtag_picker(chat, prefix, new_btn, title):
    state = load_state()
    hashtags = state.get("hashtag_list", [])
    m = types.InlineKeyboardMarkup()
    for i, tag in enumerate(hashtags):
        m.row(types.InlineKeyboardButton(f"🏷 {tag}", callback_data=f"{prefix}{i}"))
    if new_btn:
        m.row(types.InlineKeyboardButton("🆕 هشتگ جدید", callback_data=new_btn))
    if not hashtags and not new_btn:
        bot.send_message(chat, "❌ هیچ هشتگی ثبت نشده. ابتدا از «اضافه کردن متن» یک هشتگ بسازید.")
        return
    bot.send_message(chat, title, reply_markup=m)


def _send_vip_panel(chat):
    state = load_state()
    vips = state.get("vip_users", [])
    m = types.InlineKeyboardMarkup()
    m.row(types.InlineKeyboardButton("➕ افزودن VIP", callback_data="vip_add"))
    for v in vips:
        name = state.get("user_data", {}).get(str(v), {}).get("name", str(v))
        m.row(types.InlineKeyboardButton(f"❌ حذف {name} ({v})", callback_data=f"vip_rm_{v}"))
    vip_lines = "\n".join(
        f"• {state.get('user_data',{}).get(str(v),{}).get('name',str(v))} ({v})" for v in vips
    )
    bot.send_message(chat, f"👑 مدیریت VIP\n\n{vip_lines or 'خالی'}", reply_markup=m)


def _send_del_user_list(chat):
    state = load_state()
    users = state.get("user_data", {})
    if not users:
        bot.send_message(chat, "هیچ کاربری وجود ندارد.")
        return
    m = types.InlineKeyboardMarkup()
    for u, info in users.items():
        m.row(types.InlineKeyboardButton(f"{info.get('name', u)} ({u})", callback_data=f"delu_pick_{u}"))
    bot.send_message(chat, "🗑 کدام کاربر حذف شود؟", reply_markup=m)


def _send_user_search_list(chat):
    state = load_state()
    users = state.get("user_data", {})
    if not users:
        bot.send_message(chat, "هیچ کاربری ثبت نشده.")
        return
    m = types.InlineKeyboardMarkup()
    for u, info in users.items():
        vip_tag = " 👑" if int(u) in state.get("vip_users", []) else ""
        label = f"{info.get('name', u)}{vip_tag} | {u}"
        m.row(types.InlineKeyboardButton(label, callback_data=f"search_u_{u}"))
    bot.send_message(chat, "🔍 انتخاب کاربر برای مشاهده اطلاعات:", reply_markup=m)


def _send_user_detail(chat, target_uid):
    state = load_state()
    info = state.get("user_data", {}).get(target_uid)
    if not info:
        bot.send_message(chat, "❌ کاربر یافت نشد.")
        return
    vip_tag = "✅ VIP" if int(target_uid) in state.get("vip_users", []) else "❌ عادی"
    first_seen = info.get("first_seen", 0)
    join_date = time.strftime("%Y-%m-%d %H:%M", time.localtime(first_seen)) if first_seen else "نامشخص"
    received = info.get("received_count", 0)
    lim = state.get("user_limits", {}).get(target_uid, DEFAULT_LIMIT_HOURS)
    received_texts = state.get("used_texts", {}).get(target_uid, [])

    lines = [
        f"🔍 اطلاعات کاربر\n",
        f"👤 نام: {info.get('name', target_uid)}",
        f"🆔 آیدی: {target_uid}",
        f"📅 عضویت: {join_date}",
        f"👑 وضعیت: {vip_tag}",
        f"⏳ محدودیت: {lim} ساعت",
        f"📝 مجموع متون دریافتی: {received}",
        f"\n📋 تاریخچه متون ({len(received_texts)} عدد):",
    ]
    if received_texts:
        for i, t in enumerate(received_texts[-20:], 1):
            lines.append(f"{i}. {t[:80]}{'...' if len(t) > 80 else ''}")
        if len(received_texts) > 20:
            lines.append(f"... و {len(received_texts) - 20} متن دیگر")
    else:
        lines.append("(هنوز متنی دریافت نشده)")

    full_text = "\n".join(lines)
    for chunk in [full_text[i:i+4000] for i in range(0, max(len(full_text), 1), 4000)]:
        bot.send_message(chat, chunk)


def _send_limits_list(chat):
    state = load_state()
    users = state.get("user_data", {})
    vips = state.get("vip_users", [])
    normal = {u: i for u, i in users.items() if int(u) not in vips}
    if not normal:
        bot.send_message(chat, "کاربر عادی وجود ندارد.")
        return
    m = types.InlineKeyboardMarkup()
    for u, info in normal.items():
        cur = state.get("user_limits", {}).get(u, DEFAULT_LIMIT_HOURS)
        m.row(types.InlineKeyboardButton(f"{info.get('name', u)} — {cur}h", callback_data=f"lim_pick_{u}"))
    bot.send_message(chat, "⏳ کدام کاربر؟", reply_markup=m)


def _send_hour_picker(chat, target):
    state = load_state()
    cur = state.get("user_limits", {}).get(target, DEFAULT_LIMIT_HOURS)
    m = types.InlineKeyboardMarkup(row_width=6)
    m.add(*[types.InlineKeyboardButton(str(h), callback_data=f"lim_set_{target}_{h}") for h in range(1, 25)])
    bot.send_message(chat, f"⏳ محدودیت کاربر {target}\nفعلی: {cur} ساعت\n\nساعت جدید:", reply_markup=m)


def _send_user_pick_list(chat, prefix):
    state = load_state()
    users = state.get("user_data", {})
    if not users:
        bot.send_message(chat, "هیچ کاربری ثبت نشده.")
        return
    m = types.InlineKeyboardMarkup()
    for u, info in users.items():
        m.row(types.InlineKeyboardButton(f"{info.get('name', u)} ({u})", callback_data=f"{prefix}{u}"))
    bot.send_message(chat, "👤 کاربر مقصد:", reply_markup=m)


def _send_backup(chat):
    bot_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
    sent = False
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "rb") as f:
                bot.send_document(chat, f, caption="💾 bot_state.json")
            sent = True
        else:
            bot.send_message(chat, "⚠️ bot_state.json یافت نشد.")
    except Exception as e:
        bot.send_message(chat, f"❌ خطا در ارسال state: {e}")
    try:
        if os.path.exists(bot_file):
            with open(bot_file, "rb") as f:
                bot.send_document(chat, f, caption="🐍 bot.py")
            sent = True
        else:
            bot.send_message(chat, "⚠️ bot.py یافت نشد.")
    except Exception as e:
        bot.send_message(chat, f"❌ خطا در ارسال bot.py: {e}")
    if not sent:
        bot.send_message(chat, "❌ هیچ فایلی ارسال نشد.")


def _send_error_logs(chat):
    if not error_logs:
        bot.send_message(chat, "✅ هیچ خطایی ثبت نشده.")
        return
    logs_text = "\n".join(list(error_logs)[-30:])
    for chunk in [logs_text[i:i+4000] for i in range(0, max(len(logs_text), 1), 4000)]:
        bot.send_message(chat, f"🪲 لاگ خطاها:\n\n{chunk}")


def _send_suggestions_panel(chat):
    state = load_state()
    pending = [s for s in state.get("suggested_texts", []) if not s.get("reviewed")]
    if not pending:
        bot.send_message(chat, "✅ هیچ متن پیشنهادی در انتظار بررسی نیست.")
        return
    for i, s in enumerate(pending):
        m = types.InlineKeyboardMarkup()
        m.row(
            types.InlineKeyboardButton("✅ تأیید", callback_data=f"suggest_approve_{i}"),
            types.InlineKeyboardButton("❌ رد", callback_data=f"suggest_reject_{i}"),
        )
        bot.send_message(
            chat,
            f"💡 پیشنهاد از {s.get('name', s.get('uid', '?'))} ({s.get('uid', '?')}):\n\n{s.get('text', '')}",
            reply_markup=m
        )


def _approve_suggestion(chat, idx):
    state = load_state()
    pending = [s for s in state.get("suggested_texts", []) if not s.get("reviewed")]
    if idx >= len(pending):
        bot.send_message(chat, "❌ پیشنهاد یافت نشد.")
        return
    suggestion = pending[idx]
    for s in state["suggested_texts"]:
        if (s.get("uid") == suggestion.get("uid") and
                s.get("text") == suggestion.get("text") and
                not s.get("reviewed")):
            s["reviewed"] = True
            s["approved"] = True
            break
    state["available_texts"].append(suggestion["text"])
    save_state(state)
    bot.send_message(chat, "✅ متن تأیید شد و به مخزن اضافه گردید.")
    try:
        bot.send_message(int(suggestion["uid"]), "✅ متن پیشنهادی شما توسط مدیریت تأیید و به مخزن اضافه شد!")
    except Exception:
        pass


def _reject_suggestion(chat, idx):
    state = load_state()
    pending = [s for s in state.get("suggested_texts", []) if not s.get("reviewed")]
    if idx >= len(pending):
        bot.send_message(chat, "❌ پیشنهاد یافت نشد.")
        return
    suggestion = pending[idx]
    for s in state["suggested_texts"]:
        if (s.get("uid") == suggestion.get("uid") and
                s.get("text") == suggestion.get("text") and
                not s.get("reviewed")):
            s["reviewed"] = True
            s["approved"] = False
            break
    save_state(state)
    bot.send_message(chat, "❌ متن رد شد.")
    try:
        bot.send_message(int(suggestion["uid"]), "❌ متأسفانه متن پیشنهادی شما توسط مدیریت رد شد.")
    except Exception:
        pass


def send_stats(chat):
    state = load_state()
    total = len(state.get("user_data", {}))
    left = len(state.get("available_texts", []))
    active = count_active_users()
    lines = [
        f"📊 آمار ربات\n\n"
        f"👥 کاربران کل: {total}\n"
        f"🟢 فعال (۲۴ساعت): {active}\n"
        f"📝 متون موجود: {left}\n\n"
        f"📋 لیست کاربران:"
    ]
    for u, info in state.get("user_data", {}).items():
        tag = " 👑" if int(u) in state.get("vip_users", []) else ""
        lines.append(f"• {info.get('name', u)}{tag} | {u} | {info.get('received_count', 0)}")
    text = "\n".join(lines)
    for chunk in [text[i:i+4000] for i in range(0, max(len(text), 1), 4000)]:
        bot.send_message(chat, chunk)


# ─────────────────────────────────────────────
# TEXT MESSAGE HANDLER
# ─────────────────────────────────────────────
@bot.message_handler(content_types=["text"])
def on_text(msg):
    uid = str(msg.from_user.id)
    chat = msg.chat.id
    text = msg.text or ""
    register_user(msg.from_user)

    if text.startswith("/"):
        return

    cs = conv_state.get(uid)
    if cs:
        step = cs.get("step", "")

        if step == "support_msg":
            del conv_state[uid]
            m = types.InlineKeyboardMarkup()
            m.add(types.InlineKeyboardButton("پاسخ 💬", callback_data=f"areply_{msg.from_user.id}"))
            try:
                bot.send_message(
                    ADMIN_ID,
                    f"📩 تیکت از {user_name(msg.from_user)} (ID: {msg.from_user.id}):\n\n{text}",
                    reply_markup=m
                )
                bot.reply_to(msg, "✅ پیام به مدیریت ارسال شد.")
            except Exception:
                bot.reply_to(msg, "❌ خطا در ارسال.")
            return

        if step == "admin_reply" and is_admin(msg.from_user.id):
            target = cs.get("data", {}).get("target")
            del conv_state[uid]
            try:
                bot.send_message(int(target), f"💬 پاسخ مدیریت:\n\n{text}")
                bot.reply_to(msg, "✅ ارسال شد.")
            except Exception as e:
                bot.reply_to(msg, f"❌ خطا: {e}")
            return

        if step == "add_hashtag" and is_admin(msg.from_user.id):
            tag = text.strip()
            conv_state[uid] = {"step": "add_content", "data": {"hashtag": tag}}
            bot.reply_to(
                msg,
                f"🏷 هشتگ: {tag}\n\n"
                f"لطفاً متن‌ها را وارد کنید (جداکننده: ﷼)\n\n"
                f"مثال:\nمتن اول\n﷼\nمتن دوم"
            )
            return

        if step == "add_content" and is_admin(msg.from_user.id):
            hashtag = cs.get("data", {}).get("hashtag", "")
            del conv_state[uid]
            parts = [p.strip() for p in text.split("﷼")]
            state = load_state()
            existing = set(state.get("available_texts", []))
            added = 0
            for p in parts:
                if len(p) < 2:
                    continue
                full = f"{p} {hashtag}" if hashtag else p
                if full not in existing:
                    state["available_texts"].append(full)
                    existing.add(full)
                    added += 1
            state["current_hashtag"] = hashtag
            if hashtag and hashtag not in state.get("hashtag_list", []):
                state.setdefault("hashtag_list", []).append(hashtag)
            save_state(state)
            bot.reply_to(msg, f"✅ {added} متن اضافه شد. کل: {len(state['available_texts'])}")
            check_low_stock()
            return

        if step == "vip_add_id" and is_admin(msg.from_user.id):
            del conv_state[uid]
            try:
                new_vip = int(text.strip())
                state = load_state()
                if new_vip not in state.get("vip_users", []):
                    state["vip_users"].append(new_vip)
                    save_state(state)
                    bot.reply_to(msg, f"✅ {new_vip} به VIP اضافه شد.")
                    try:
                        bot.send_message(new_vip, "✨ شما از الان جزو مشترکین VIP هستید")
                    except Exception:
                        pass
                else:
                    bot.reply_to(msg, "این کاربر قبلاً VIP است.")
            except ValueError:
                bot.reply_to(msg, "❌ آیدی نامعتبر.")
            return

        if step == "edit_welcome" and is_admin(msg.from_user.id):
            del conv_state[uid]
            state = load_state()
            state["welcome_message"] = text
            save_state(state)
            bot.reply_to(msg, "✅ پیام خوش‌آمد به‌روزرسانی شد.")
            return

        if step == "edit_op_start" and is_admin(msg.from_user.id):
            del conv_state[uid]
            state = load_state()
            state["op_start_msg"] = text
            save_state(state)
            bot.reply_to(msg, "✅ پیام شروع عملیات به‌روز شد.")
            return

        if step == "edit_op_cancel" and is_admin(msg.from_user.id):
            del conv_state[uid]
            state = load_state()
            state["op_cancel_msg"] = text
            save_state(state)
            bot.reply_to(msg, "✅ پیام لغو عملیات به‌روز شد.")
            return

        if step == "suggest_text":
            del conv_state[uid]
            state = load_state()
            suggestion = {
                "uid": uid,
                "name": user_name(msg.from_user),
                "text": text,
                "timestamp": time.time(),
                "reviewed": False,
            }
            state.setdefault("suggested_texts", []).append(suggestion)
            save_state(state)
            bot.reply_to(msg, "✅ متن پیشنهادی شما ارسال شد.\nبعد از بررسی مدیریت به مخزن اضافه می‌شود.")
            try:
                m = types.InlineKeyboardMarkup()
                m.row(types.InlineKeyboardButton("بررسی پیشنهادات 💡", callback_data="s_suggestions"))
                bot.send_message(
                    ADMIN_ID,
                    f"💡 متن پیشنهادی جدید از {user_name(msg.from_user)} ({uid}):\n\n{text}",
                    reply_markup=m
                )
            except Exception:
                pass
            return

        if step == "agent_task" and is_admin(msg.from_user.id):
            del conv_state[uid]
            try:
                append_agent_task(text)
                bot.reply_to(msg, "✅ دستور در agent_tasks.txt ذخیره شد.\nReplit Agent در اولین فرصت آن را اجرا می‌کند.")
            except Exception as e:
                bot.reply_to(msg, f"❌ خطا در ذخیره: {e}")
            return

        if step == "bm_one_msg" and is_admin(msg.from_user.id):
            target = cs.get("data", {}).get("target")
            del conv_state[uid]
            try:
                bot.send_message(int(target), f"📢 پیام از مدیریت:\n\n{text}")
                bot.reply_to(msg, "✅ ارسال شد.")
            except Exception as e:
                bot.reply_to(msg, f"❌ خطا: {e}")
            return

        if step == "bm_all_msg" and is_admin(msg.from_user.id):
            del conv_state[uid]
            state = load_state()
            sent = 0
            for t in state.get("user_data", {}).keys():
                try:
                    bot.send_message(int(t), f"📢 پیام از مدیریت:\n\n{text}")
                    sent += 1
                except Exception:
                    pass
            bot.reply_to(msg, f"✅ به {sent} کاربر ارسال شد.")
            return

    # ── SPY MODE (non-admin) ──
    if not is_admin(msg.from_user.id):
        try:
            m = types.InlineKeyboardMarkup()
            m.add(types.InlineKeyboardButton("پاسخ 💬", callback_data=f"areply_{msg.from_user.id}"))
            bot.send_message(
                ADMIN_ID,
                f"📩 پیام از {user_name(msg.from_user)} (ID: {msg.from_user.id}):\n\n{text}",
                reply_markup=m
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
# AGENT TASK WATCHER
# ─────────────────────────────────────────────
TASK_HEADER = "# Agent Tasks File - Admin instructions from Telegram will appear here"
TASK_CHECK_INTERVAL = 10


def _clear_task_file():
    with open(AGENT_TASKS_FILE, "w", encoding="utf-8") as f:
        f.write(TASK_HEADER + "\n")


def _extract_task_body(raw: str) -> str:
    lines = []
    capture = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and "ADMIN TASK:" in stripped:
            capture = True
            continue
        if set(stripped) <= {"─", "-", ""}:
            continue
        if stripped == TASK_HEADER.strip() or stripped.startswith("#"):
            continue
        if capture and stripped:
            lines.append(stripped)
    return "\n".join(lines).strip()


def agent_task_watcher():
    last_mtime = 0.0
    while True:
        time.sleep(TASK_CHECK_INTERVAL)
        try:
            if not os.path.exists(AGENT_TASKS_FILE):
                continue
            mtime = os.path.getmtime(AGENT_TASKS_FILE)
            if mtime <= last_mtime:
                continue
            with open(AGENT_TASKS_FILE, "r", encoding="utf-8") as f:
                raw = f.read()
            task = _extract_task_body(raw)
            if not task:
                last_mtime = mtime
                continue
            last_mtime = time.time()
            _clear_task_file()
            logger.info(f"[AgentWatcher] New task received: {task[:120]}")
            try:
                bot.send_message(ADMIN_ID,
                    f"🤖 دستور دریافت شد:\n\n{task[:800]}\n\n⏳ در حال پردازش...")
            except Exception:
                pass
            low = task.lower()
            if low.startswith("exec:"):
                code = task[5:].strip()
                try:
                    exec(code, globals())  # noqa: S102
                    bot.send_message(ADMIN_ID, "✅ کد با موفقیت اجرا شد.")
                except Exception as exc:
                    bot.send_message(ADMIN_ID, f"❌ خطا در اجرا:\n{exc}")
            elif low.startswith("restart:"):
                new_code = task[8:].strip()
                if not new_code:
                    bot.send_message(ADMIN_ID, "❌ کد جدیدی برای نوشتن ارسال نشد.")
                    continue
                bot_file = os.path.abspath(__file__)
                with open(bot_file, "w", encoding="utf-8") as f:
                    f.write(new_code)
                logger.info("[AgentWatcher] bot.py updated — restarting process.")
                try:
                    bot.send_message(ADMIN_ID,
                        "✅ کد جدید نوشته شد.\n🔄 ربات در حال راه‌اندازی مجدد است...")
                except Exception:
                    pass
                time.sleep(1)
                os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                bot.send_message(ADMIN_ID,
                    "⚠️ فرمت دستور شناسایی نشد.\n\n"
                    "فرمت‌های پشتیبانی‌شده:\n"
                    "• exec: <کد پایتون>\n"
                    "• restart: <کامل‌ترین کد جدید bot.py>")
        except Exception as e:
            err = f"[AgentWatcher] Error: {e}"
            logger.error(err, exc_info=True)
            log_error(err)


# ─────────────────────────────────────────────
# AUTO BACKUP
# ─────────────────────────────────────────────
def auto_backup():
    while True:
        time.sleep(BACKUP_INTERVAL)
        try:
            _send_backup(ADMIN_ID)
        except Exception as e:
            logger.error(f"Backup error: {e}")


# ─────────────────────────────────────────────
# FLASK HEALTH
# ─────────────────────────────────────────────
@app.route("/")
def root():
    return "Bot is running!", 200


@app.route("/health")
def health():
    state = load_state()
    return {
        "status": "ok",
        "operation_active": state.get("operation_active", True),
        "texts": len(state.get("available_texts", [])),
        "users": len(state.get("user_data", {})),
        "active_users": count_active_users(),
    }, 200


def run_flask():
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False, use_reloader=False)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("Starting bot...")

    for attempt in range(5):
        try:
            bot.remove_webhook()
            logger.info("Webhook cleared.")
            break
        except Exception as e:
            logger.warning(f"Webhook removal attempt {attempt+1} failed: {e}")
            time.sleep(2)

    time.sleep(3)

    state = load_state()
    for v in DEFAULT_VIPS:
        if v not in state["vip_users"]:
            state["vip_users"].append(v)
    if ADMIN_ID not in state["vip_users"]:
        state["vip_users"].append(ADMIN_ID)
    save_state(state)

    threading.Thread(target=run_flask, daemon=True).start()
    logger.info(f"Flask health on :{FLASK_PORT}")

    threading.Thread(target=auto_backup, daemon=True).start()
    logger.info("Auto-backup thread started.")

    _clear_task_file()
    threading.Thread(target=agent_task_watcher, daemon=True).start()
    logger.info("Agent task watcher started (checking every 10s).")

    logger.info("Polling...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60, restart_on_change=False)
