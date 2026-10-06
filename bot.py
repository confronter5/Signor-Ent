
import os
import logging
import secrets
import requests
import hmac
import hashlib
import io
import csv
import re
import html
import asyncio
import threading
import concurrent.futures
import traceback
import time as time_mod
import base64
import tempfile
import json as json_mod
import shutil
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()


from datetime import datetime, timedelta
from functools import lru_cache, wraps
from flask import Flask, request, jsonify, send_file, send_from_directory
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, InputMediaPhoto, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, ChatMemberHandler, ChatJoinRequestHandler, filters, ApplicationHandlerStop
from telegram.constants import ParseMode, ChatAction
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from bson import ObjectId
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ['BOT_TOKEN']
MONGO_URI = os.environ['MONGO_URI']
ADMIN_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_ID', '').split(',') if x.strip()]

# Premium-emoji donors: Telegram Premium users who ENABLE animated emojis for the bot
# but are NOT admins (no admin commands, treated as normal users if they /start).
PREMIUM_EMOJI_DONOR_IDS = [int(x.strip()) for x in os.environ.get('PREMIUM_EMOJI_DONOR_IDS', os.environ.get('PREMIUM_DONOR_ID', '')).split(',') if x.strip()]
# If any donor ID is set, bot always uses premium emoji mode (unless FORCE_PREMIUM_EMOJIS=false)

ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else 0

def is_bot_admin(user_id) -> bool:
    """True only for ADMIN_ID list — premium donors are never admins."""
    try:
        return int(user_id) in ADMIN_IDS
    except Exception:
        return False

CHANNEL_ID = int(os.environ.get('CHANNEL_ID', '0'))
# Auto-mirror uploaded movies (no "Forwarded from" tag) to this channel/id
# Examples: -1001234567890  or  @mychannel
UPLOAD_TARGET_CHANNEL = (os.environ.get('UPLOAD_TARGET_CHANNEL') or os.environ.get('UPLOAD_CHANNEL') or '').strip()
# true = send poster first (when available) then video; false = videos/files only
UPLOAD_TARGET_POSTERS = (os.environ.get('UPLOAD_TARGET_POSTERS', 'true') or 'true').strip().lower() in ('1', 'true', 'yes', 'on')
# /kingdeliver destination (admin bulk archive copy — no payment, no forward tag)
KING_DELIVER_CHANNEL = (os.environ.get('KING_DELIVER_CHANNEL') or os.environ.get('UPLOAD_TARGET_CHANNEL') or '').strip()
# Auto-mirror on upload: set false to only use /kingdeliver (avoids surprise duplicates)
AUTO_MIRROR_ON_UPLOAD = (os.environ.get('AUTO_MIRROR_ON_UPLOAD', 'true') or 'true').strip().lower() in ('1', 'true', 'yes', 'on')
# Performance: keep pacing tiny (Telegram flood still handled via RetryAfter)
BOT_FAST_DELAY = float(os.environ.get('BOT_FAST_DELAY', '0.03') or 0.03)  # general inter-message gap
BOT_MIRROR_DELAY = float(os.environ.get('BOT_MIRROR_DELAY', '0.05') or 0.05)
BOT_DELIVER_DELAY = float(os.environ.get('BOT_DELIVER_DELAY', '0.05') or 0.05)
BOT_KING_DELAY = float(os.environ.get('BOT_KING_DELAY', '0.06') or 0.06)  # faster kingdeliver pacing

# Fast in-memory cache for settings / counts (commands respond in ms)
_FAST_CACHE = {}
_FAST_CACHE_TS = {}

def _cache_get(key, ttl=20):
    import time as _t
    ts = _FAST_CACHE_TS.get(key)
    if ts is None:
        return None
    if _t.time() - ts > ttl:
        return None
    return _FAST_CACHE.get(key)

def _cache_set(key, value):
    import time as _t
    _FAST_CACHE[key] = value
    _FAST_CACHE_TS[key] = _t.time()
    return value

def track_user_bg(user_id, username=None, first_name=None, last_name=None):
    """Non-blocking user track — never delay command replies."""
    try:
        asyncio.get_event_loop().create_task(
            asyncio.to_thread(track_user, user_id, username, first_name, last_name)
        )
    except Exception:
        try:
            track_user(user_id, username, first_name, last_name)
        except Exception:
            pass

START_IMAGE_URL = os.environ.get('START_IMAGE_URL', '')
ADMIN_LINK = os.environ.get('ADMIN_LINK', '')
PAYSTACK_SECRET_KEY = os.environ['PAYSTACK_SECRET_KEY']
PAYSTACK_PUBLIC_KEY = os.environ.get('PAYSTACK_PUBLIC_KEY', '')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')
PORT = int(os.environ.get('PORT', 10000))
PUBLIC_CHANNEL_USERNAME = os.environ.get('PUBLIC_CHANNEL_USERNAME', '')
VISIT_CHANNEL_LINK = os.environ.get('VISIT_CHANNEL_LINK', '')
BOT_USERNAME = os.environ.get('BOT_USERNAME', '').lstrip('@')
MENU_TITLE = os.environ.get('MENU_TITLE', '')
HELP_TEXT = os.environ.get('HELP_TEXT', '''🎦 {BOT_DISPLAY_NAME} 🍿

✨ 🔥 Available Commands 🎬
🚀 /start — Start the bot / browse movies
🔍 /search — Search movies by title or genre
🎬 /mymovies — Your purchased movies (rewatch free)
📋 /myplanmovies — Rewatch movies from your plan library
📊 /checksubscription — Check subscription status & remaining
💰 /checkpayment — Check pending payment
🛒 /cart — View your shopping cart
🎁 /claimfree — Claim free movie (after 5 purchases)
🎨 /setmenustyle — Change menu display style
🤖 /getbot — Get your own movie bot
🤖 /mybot — Manage your bot
🤖 /dev — Developer info
🏓 /ping — Test if bot is active
❓ /help — Show this help

🎁 Cart Discount (auto):
• Add 5 movies to cart → get 1 free (cheapest)
• Add 10 movies to cart → get 2 free (two cheapest)

🎁 /claimfree — After 5 successful purchases you unlock 1 free movie from your library.

For support contact admin: see ADMIN_USERNAME in .env''')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '')
MY_CHANNEL = os.environ.get('MY_CHANNEL', '')
ADDITIONAL_CHANNELS_RAW = os.environ.get('ADDITIONAL_CHANNELS', '')

PAYHERO_API_USERNAME = os.environ.get('PAYHERO_API_USERNAME', '')
PAYHERO_API_PASSWORD = os.environ.get('PAYHERO_API_PASSWORD', '')
PAYHERO_CHANNEL_ID = int(os.environ.get('PAYHERO_CHANNEL_ID', '6909'))
PAYHERO_BASE_URL = "https://backend.payhero.co.ke/api/v2"
PAYHERO_ENABLED_BY_DEFAULT = os.environ.get('PAYHERO_ENABLED', 'false').lower() == 'true'
PAYMENT_PROMPT_NAME = os.environ.get('PAYMENT_PROMPT_NAME', 'secure payment page')

# ========== MAKAMESCO / NEXUS PAY (3rd gateway) ==========
MAKAMESCO_SECRET_KEY = os.environ.get('MAKAMESCO_SECRET_KEY', '')
MAKAMESCO_PUBLIC_KEY = os.environ.get('MAKAMESCO_PUBLIC_KEY', '')
MAKAMESCO_BASE_URL = os.environ.get('MAKAMESCO_BASE_URL', 'https://makamescopay.com')
DEVELOPER_CONTACT = os.environ.get('DEVELOPER_CONTACT', os.environ.get('ADMIN_USERNAME', '')).lstrip('@')
MAKAMESCO_ENABLED_BY_DEFAULT = os.environ.get('MAKAMESCO_ENABLED', 'false').lower() == 'true'
MAKAMESCO_CALLBACK_URL = os.environ.get('MAKAMESCO_CALLBACK_URL', '')

PAYSTACK_API = "https://api.paystack.co"

DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', 'changeme')
DASHBOARD_SESSION_SECRET = os.environ.get('DASHBOARD_SESSION_SECRET', '') or (DASHBOARD_PASSWORD + '|dash-session')
DASHBOARD_COOKIE_NAME = os.environ.get('DASHBOARD_COOKIE_NAME', 'dash_auth')

FOOTER_TEXT = os.environ.get('FOOTER_TEXT', '')


# ---- Branding from .env (never hardcode usernames in messages) ----
BOT_DISPLAY_NAME = os.environ.get('BOT_DISPLAY_NAME', os.environ.get('MENU_TITLE', 'Movie Bot') or 'Movie Bot')
DASHBOARD_TITLE = os.environ.get('DASHBOARD_TITLE', f'{BOT_DISPLAY_NAME} — Admin')
DASHBOARD_LOGO = os.environ.get('DASHBOARD_LOGO', os.environ.get('START_IMAGE_URL', ''))
YEAR_COPY = os.environ.get('YEAR_COPY', '2026')

def admin_handle() -> str:
    h = (ADMIN_USERNAME or DEVELOPER_CONTACT or '').lstrip('@').strip()
    return h or 'admin'

def admin_mention() -> str:
    return f"@{admin_handle()}"

def powered_footer() -> str:
    if FOOTER_TEXT:
        return FOOTER_TEXT
    return f"│ ✨ ♦ Powered by {admin_mention()} © {YEAR_COPY} ♦"


# ---- Telegram Premium custom emojis (when admin has Premium) ----
# Format env PREMIUM_CUSTOM_EMOJIS as: 🎬:5368324170671202286,🔍:5417915203100614054
# Or JSON object. Fallback unicode always shown for non-premium clients.
_ADMIN_PREMIUM_CACHE = {"checked_at": 0.0, "is_premium": False}
_PREMIUM_EMOJI_MAP = None

def _load_premium_emoji_map() -> dict:
    global _PREMIUM_EMOJI_MAP
    if _PREMIUM_EMOJI_MAP is not None:
        return _PREMIUM_EMOJI_MAP
    raw = (os.environ.get('PREMIUM_CUSTOM_EMOJIS') or '').strip()
    mapping = {}
    # Sensible defaults (public custom emoji ids used widely in bots) — override via env
    defaults = {
        # Working RestrictedEmoji IDs (same set as @texasentsbot — confirmed animated when Premium mode on)
        "👋": "5472055112702629499",
        "👇": "5470177992950946662",
        "👆": "5469718869536940860",
        "👉": "5471978009449731768",
        "👈": "5469735272017043817",
        "👍": "5469770542288478598",
        "👎": "5472309400536358507",
        "👏": "5471921242866981303",
        "🙏": "5472189549473963781",
        "💪": "5471883477219549006",
        "✌️": "5469986291380657759",
        "👌": "5471984997361523302",
        "🤝": "5357080225463149588",
        "🙌": "5469645992531862101",
        "😀": "5372954454653933911",
        "😊": "5370738629486319646",
        "😂": "5370953476635368811",
        "😍": "5372886001465170842",
        "🤩": "5373026167722876724",
        "😎": "5373141891321699086",
        "😢": "5370881342659631698",
        "😭": "5370646412243510708",
        "😱": "5370810157871667232",
        "🤯": "5370745797786738962",
        "🥳": "5370870691140737817",
        "🤔": "5370724846936267183",
        "🥺": "5373230475022179039",
        "😴": "5372923973271034075",
        "👤": "5373012449597335010",
        "👥": "5372926953978341366",
        "🤖": "5372981976804366741",
        "👑": "5467406098367521267",
        "👻": "5371017798065592581",
        "🎃": "5370610867094166617",
        "👨‍💼": "5190498849440931467",
        "🧑‍💻": "5190458330719461749",
        "🔥": "5420315771991497307",
        "✨": "5472164874886846699",
        "⭐": "5435957248314579621",
        "⭐️": "5435957248314579621",
        "🌟": "5458799228719472718",
        "💫": "5469741319330996757",
        "⚡": "5431449001532594346",
        "⚡️": "5431449001532594346",
        "💥": "5469785308386041323",
        "🌍": "5399898266265475100",
        "🌴": "5449372007432985754",
        "🌱": "5449885771420934013",
        "🌼": "5370731117588523522",
        "🌻": "5211226911367257670",
        "🌈": "5427042798878610107",
        "🚀": "5445284980978621387",
        "🎬": "5375464961822695044",
        "🎥": "5375464961822695044",
        "🎦": "5375464961822695044",
        "📺": "5373330964372004748",
        "🍿": "5371081166013078244",
        "🎭": "5359441070201513074",
        "🏆": "5409008750893734809",
        "🎯": "5350460637182993292",
        "🏓": "5269563867305879894",
        "🛒": "5431499171045581032",
        "📦": "5431499171045581032",
        "🎁": "5199749070830197566",
        "🎉": "5436040291507247633",
        "💰": "5375296873982604963",
        "💎": "5471952986970267163",
        "♦": "5471952986970267163",
        "◆": "5471952986970267163",
        "🪙": "5379600444098093058",
        "📱": "5407025283456835913",
        "💻": "5431376038628171216",
        "💡": "5472146462362048818",
        "🔔": "5242628160297641831",
        "📊": "5431577498364158238",
        "📁": "5433653135799228968",
        "📂": "5431721976769027887",
        "📖": "5226512880362332956",
        "📝": "5334882760735598374",
        "🔗": "5375129357373165375",
        "🏠": "5465226866321268133",
        "💬": "5465300082628763143",
        "❤️": "5449505950283078474",
        "👀": "5424885441100782420",
        "✅": "5427009714745517609",
        "❌": "5465665476971471368",
        "❓": "5467666648263564704",
        "⚠️": "5447644880824181073",
        "🆕": "5361979468887893611",
        "🆓": "5364112491381006601",
        "➕": "5226945370684140473",
        "🔄": "5264727218734524899",
        "♻️": "5264727218734524899",
        "ℹ️": "5323442290708985472",
        "📢": "5469903029144657419",
        "📣": "5469903029144657419",
        "💳": "5422683699130933153",
        "💵": "5375296873982604963",
        "📅": "5431897022456145283",
        "📆": "5431897022456145283",
        "⚙️": "5341715473882955310",
        "🔒": "5472308992514464048",
        "🔐": "5472308992514464048",
        "📸": "5206383450977750405",
        "🌐": "5447410659077661506",
        "⬅️": "5877536313623711363",
        "➡️": "5875506366050734240",
        "⬇️": "5386367538735104399",
        "▶️": "5348125953090403204",
        "▶": "5348125953090403204",
        "◀": "5877536313623711363",
        "🔹": "5438496463044752972",
        "🔸": "5438496463044752972",
        "☔": "5416081784641168838",
        "✦": "5435957248314579621",
        "❖": "5471952986970267163",
        "⏱": "5431897022456145283",
        "⏰": "5431897022456145283",
        "⏳": "5431897022456145283",
        "⌛": "5431897022456145283",
        "🕐": "5431897022456145283",
        "🔎": "5264727218734524899",
        "🔍": "5264727218734524899",
        "💸": "5375296873982604963",
        "✔": "5427009714745517609",
        "✓": "5427009714745517609",
        "🏷️": "5431499171045581032",
        "🏷": "5431499171045581032",
        "★": "5435957248314579621",
        "☆": "5435957248314579621",
        "💲": "5375296873982604963",
        "💵": "5375296873982604963",
        "🔵": "5471952986970267163",
        "🟠": "5438496463044752972",
        "🟡": "5438496463044752972",
        "🟢": "5427009714745517609",
        "✏️": "5334882760735598374",
        "✏": "5334882760735598374",
        "🖊️": "5334882760735598374",
        "🖊": "5334882760735598374",
        "📌": "5350460637182993292",
        "📍": "5350460637182993292",
        "🏷️": "5431499171045581032",
        "🔖": "5431499171045581032",
        "💽": "5373330964372004748",
        "💿": "5373330964372004748",
        "📼": "5375464961822695044",
        "🔉": "5469903029144657419",
        "🔊": "5469903029144657419",
        "💲": "5375296873982604963",
        "🤑": "5375296873982604963",
        "🧾": "5431577498364158238",
        "🪪": "5422683699130933153",
        "🏦": "5422683699130933153",
        "🏧": "5422683699130933153",
        # ---- full animated / premium pack from your ID Explorer screenshots ----
        # Crowns / prestige
        "👑": "5222368327116004404",
        "👑‍": "5219827798125846744",
        "💎": "5215191209131123104",
        "⭐": "5438496463044752972",
        "🌟": "5222068860521290556",
        "✨": "5325547803936572038",
        "💫": "5469741319330996757",
        "🔥": "5424972470023104089",
        "⚡": "5364098734600762220",
        "⚡️": "5215444784000277441",
        "💥": "5276032951342088188",
        "💢": "5215444784000277441",
        # Status / UI
        "✅": "5212932275376759608",
        "✔": "5200219696711736568",
        "✓": "5206607081334906820",
        "❌": "5210952531676504517",
        "✖": "5215572533507529497",
        "⚠": "5447644880824181073",
        "⚠️": "5447644880824181073",
        "❗": "5274099962655816924",
        "‼️": "5440660757194744323",
        "❓": "5366257853315167048",
        "❔": "5314504236132747481",
        "🚫": "5240241223632954241",
        "⛔": "5260293700088511294",
        "🆕": "5361979468887893611",
        "🆓": "5406756500108501710",
        "🔝": "5406683434124859552",
        "🔴": "5411225014148014586",
        "🟢": "5416081784641168838",
        "🔵": "5213195496742460729",
        "🟠": "5215200271512117515",
        "🟣": "5220118339778518173",
        "⚫": "5213240288956390744",
        "⚪": "5213287958798410406",
        "🔶": "5213323619911868787",
        "🔷": "5215496461046783960",
        # Arrows / nav
        "➡️": "5215720576735255650",
        "➡": "5215720576735255650",
        "⬅️": "5877536313623711363",
        "⬅": "5877536313623711363",
        "⬆️": "5406745015365943482",
        "⬇": "5406745015365943482",
        "⬇️": "5386367538735104399",
        "▶️": "5348125953090403204",
        "▶": "5348125953090403204",
        "⏸": "5359543311897998264",
        "⏩": "5215209943778466641",
        "⏪": "5215177485389566053",
        "⏭": "5215480011322042129",
        "🔼": "5449683594425410231",
        "🔽": "5447183459602669338",
        # Media / movie
        "🎬": "5375464961822695044",
        "🎥": "5375464961822695044",
        "🎦": "5375464961822695044",
        "📺": "5373330964372004748",
        "🍿": "5371081166013078244",
        "🎭": "5359441070201513074",
        "🎤": "5217716435152805326",
        "🎧": "5217713669193867492",
        "🎵": "5463107823946717464",
        "🎶": "5212941939053175244",
        "📢": "5217943819311389632",
        "📣": "5215659875962462292",
        "🔔": "5213383002129702114",
        "🔔‍": "5458603043203327669",
        # Money / cart
        "💰": "5409048419211682843",
        "💵": "5375296873982604963",
        "💸": "5375296873982604963",
        "💲": "5366352986840772584",
        "💳": "5422683699130933153",
        "🪙": "5379600444098093058",
        "🛒": "5431499171045581032",
        "📦": "5431499171045581032",
        "🎁": "5199749070830197566",
        "🎉": "5461151367559141950",
        "🏆": "5409008750893734809",
        "🎯": "5350460637182993292",
        # Tech / tools
        "🤖": "5372981976804366741",
        "💻": "5431376038628171216",
        "📱": "5407025283456835913",
        "⚙️": "5341715473882955310",
        "🔧": "5215392879320505675",
        "🛠": "5215392879320505675",
        "🔗": "5271604874419647061",
        "📎": "5305265301917549162",
        "📌": "5397782960512444700",
        "📍": "5391032818111363540",
        "🔒": "5472308992514464048",
        "🔓": "5296369303661067030",
        "🔐": "5472308992514464048",
        "🛡": "5215669479509335000",
        "📁": "5433653135799228968",
        "📂": "5431721976769027887",
        "📊": "5220226955206467824",
        "📈": "5244837092042750681",
        "📉": "5246762912428603768",
        "💡": "5422439311196834318",
        "📅": "5413879192267805083",
        "📆": "5431897022456145283",
        "⏰": "5431897022456145283",
        "⏱": "5431897022456145283",
        "⏳": "5431897022456145283",
        "⌛": "5431897022456145283",
        "🕐": "5431897022456145283",
        "🏠": "5416041192905265756",
        "🗑": "5445267414562389170",
        "🗑️": "5445267414562389170",
        "✏️": "5395444784611480792",
        "📝": "5334882760735598374",
        "📖": "5226512880362332956",
        "ℹ️": "5334544901428229844",
        "ℹ": "5334544901428229844",
        "©️": "5323442290708985472",
        "©": "5323442290708985472",
        # People / social
        "👤": "5373012449597335010",
        "👥": "5372926953978341366",
        "💬": "5224327270289644832",
        "💭": "5224617957971206703",
        "👋": "5472055112702629499",
        "👍": "5469770542288478598",
        "👎": "5472309400536358507",
        "👏": "5471921242866981303",
        "🙏": "5472189549473963781",
        "💪": "5471883477219549006",
        "🤝": "5357080225463149588",
        "🙌": "5469645992531862101",
        "👀": "5424885441100782420",
        "❤️": "5337080053119336309",
        "♥️": "5213195758735467306",
        "💙": "5213270053079752225",
        "💚": "5213274008744632782",
        "💜": "5215715272450645010",
        "🖤": "5213148582814687024",
        "💌": "5215324073944423501",
        "💖": "5220092801902976501",
        # Faces (animated)
        "😀": "5372954454653933911",
        "😊": "5370738629486319646",
        "😂": "5370953476635368811",
        "🤣": "5424861062866411089",
        "😍": "5372886001465170842",
        "🤩": "5373026167722876724",
        "😎": "5373141891321699086",
        "😢": "5370881342659631698",
        "😭": "5370646412243510708",
        "😱": "5370810157871667232",
        "🤯": "5370745797786738962",
        "🥳": "5370870691140737817",
        "🤔": "5370724846936267183",
        "🥺": "5373230475022179039",
        "😴": "5372923973271034075",
        "😤": "5363959899782912839",
        "😡": "5364031428168259381",
        "🤬": "5363911285048089157",
        "😇": "5364238162124084793",
        "🥰": "5364233059702937499",
        "😘": "5363821039195264988",
        "😜": "5363862395435358206",
        "🤪": "5452161279454092049",
        "😏": "5363813939614327183",
        "😌": "5435857806936777022",
        "😔": "5363964104555894902",
        "😪": "5375380140513565505",
        "🤤": "5363809112071085623",
        "😮": "5375380140513565505",
        "😯": "5400035967211936555",
        "😲": "5363920802695617184",
        "😳": "5379568936218009290",
        "🥺": "5373230475022179039",
        "😈": "5363911285048089157",
        "👻": "5371017798065592581",
        "🎃": "5370610867094166617",
        # Nature / weather
        "🌍": "5447410659077661506",
        "🌐": "5447410659077661506",
        "🌈": "5409109841538994759",
        "☀️": "5402477260982731644",
        "🌞": "5375338737028841420",
        "🌙": "5449569374065152798",
        "❄️": "5449449325434266744",
        "💧": "5393512611968995988",
        "☁️": "5399913388845322366",
        "🌧": "5399913388845322366",
        # Brand-ish / extra
        "🚀": "5445284980978621387",
        "✈️": "5416117059207572332",
        "🎮": "5213293263083018856",
        "🕹": "5213107179329953547",
        "📡": "5213107020416164074",
        "🔍": "5264727218734524899",
        "🔎": "5264727218734524899",
        "🏓": "5269563867305879894",
        "♦": "5471952986970267163",
        "◆": "5471952986970267163",
        "✦": "5435957248314579621",
        "❖": "5471952986970267163",
        "🔹": "5438496463044752972",
        "🔸": "5438496463044752972",
    }

    mapping.update(defaults)
    if raw:
        try:
            if raw.startswith('{'):
                import json as _json
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    mapping.update({str(k): str(v) for k, v in parsed.items()})
            else:
                for part in raw.split(','):
                    part = part.strip()
                    if not part or ':' not in part:
                        continue
                    # emoji may be multi-codepoint — split from right
                    emoji, eid = part.rsplit(':', 1)
                    mapping[emoji.strip()] = eid.strip()
        except Exception as e:
            logger.warning(f"PREMIUM_CUSTOM_EMOJIS parse error: {e}")
    _PREMIUM_EMOJI_MAP = mapping
    logger.info(f"Premium emoji map loaded: {len(mapping)} official RestrictedEmoji ids")
    return mapping

async def refresh_admin_premium_status(bot=None) -> bool:
    """Enable premium emoji mode from FORCE, donors, or any admin with Premium.
    Donors are checked for Telegram Premium the same way as admins, but NEVER get admin rights.
    """
    import time as _time
    now = _time.time()
    force_raw = (os.environ.get('FORCE_PREMIUM_EMOJIS') or '').strip().lower()
    if force_raw in ('0', 'false', 'no', 'off', 'never'):
        _ADMIN_PREMIUM_CACHE["is_premium"] = False
        _ADMIN_PREMIUM_CACHE["checked_at"] = now
        logger.info("Premium emoji mode: False (FORCE_PREMIUM_EMOJIS off)")
        return False
    if force_raw in ('1', 'true', 'yes', 'on', 'always'):
        _ADMIN_PREMIUM_CACHE["is_premium"] = True
        _ADMIN_PREMIUM_CACHE["checked_at"] = now
        logger.info("Premium emoji mode: True (FORCE_PREMIUM_EMOJIS)")
        return True

    # Short cache so Premium purchased mid-session switches emojis quickly (~30s)
    if now - float(_ADMIN_PREMIUM_CACHE.get("checked_at") or 0) < 30:
        return bool(_ADMIN_PREMIUM_CACHE.get("is_premium"))

    is_prem = False
    source = "none"
    try:
        b = bot or (application.bot if application else None)
        # 1) Non-admin donors first — same Premium check, zero admin access
        check_ids = list(PREMIUM_EMOJI_DONOR_IDS or [])
        for did in check_ids:
            if not b:
                break
            try:
                chat = await b.get_chat(did)
                if bool(getattr(chat, 'is_premium', False)) or bool(getattr(chat, 'premium', False)):
                    is_prem = True
                    source = f"donor:{did}"
                    break
            except Exception as e:
                logger.debug(f"premium check donor {did}: {e}")
                continue
        # 2) Real admins (only for premium detection, rights already via ADMIN_IDS)
        if not is_prem and b and ADMIN_IDS:
            for admin_id in ADMIN_IDS:
                try:
                    chat = await b.get_chat(admin_id)
                    if bool(getattr(chat, 'is_premium', False)) or bool(getattr(chat, 'premium', False)):
                        is_prem = True
                        source = f"admin:{admin_id}"
                        break
                except Exception as e:
                    logger.debug(f"premium check admin {admin_id}: {e}")
                    continue
        # 3) No live Premium found — stay on normal unicode emojis
    except Exception as e:
        logger.warning(f"premium status check failed: {e}")
        return bool(_ADMIN_PREMIUM_CACHE.get("is_premium"))

    _ADMIN_PREMIUM_CACHE["is_premium"] = is_prem
    _ADMIN_PREMIUM_CACHE["checked_at"] = now
    logger.info(f"Premium emoji mode: {is_prem} (source={source})")
    return is_prem

def admin_premium_active() -> bool:
    """Premium/custom animated emojis when bot owner (ADMIN_IDS) or donors have Telegram Premium.
    FORCE_PREMIUM_EMOJIS=true/false is an explicit override only (do not rely on env to force).
    Bot API 9.4+: bots may send custom emoji if the bot owner has Premium.
    """
    force_raw = (os.environ.get('FORCE_PREMIUM_EMOJIS') or '').strip().lower()
    if force_raw in ('0', 'false', 'no', 'off', 'never'):
        return False
    if force_raw in ('1', 'true', 'yes', 'on', 'always'):
        return True
    # Live cache from refresh_admin_premium_status (admins + donors who actually have Premium)
    return bool(_ADMIN_PREMIUM_CACHE.get("is_premium", False))


def strip_premium_emojis(text: str) -> str:
    """Remove ALL <tg-emoji> tags (including nested), keep fallback unicode. Safe for every client."""
    if not text or '<tg-emoji' not in str(text):
        return text
    try:
        # Nested tags (bug from double-apply) — peel until clean
        prev = None
        while prev != text:
            prev = text
            text = re.sub(r'<tg-emoji[^>]*>', '', text)
            text = text.replace('</tg-emoji>', '')
        # leftover attributes / broken tags
        text = re.sub(r'</?tg-emoji[^>]*>', '', text)
    except Exception:
        pass
    return text


def finalize_outgoing_text(text, parse_mode=None):
    """NEVER show raw <tg-emoji> tags to users/channels.
    - Premium OFF → always strip to plain unicode
    - Premium ON + HTML parse mode → keep tags
    - Premium ON + no HTML → strip (tags would show as literal text)
    """
    if not text:
        return text, parse_mode
    # Non-premium must never emit tags
    if not admin_premium_active():
        return strip_premium_emojis(text), parse_mode
    if '<tg-emoji' not in str(text):
        return text, parse_mode
    pm = parse_mode
    pm_ok = False
    try:
        if pm is ParseMode.HTML:
            pm_ok = True
        elif isinstance(pm, str) and pm.upper() == 'HTML':
            pm_ok = True
    except Exception:
        pm_ok = False
    if pm_ok:
        return text, parse_mode
    # No HTML → strip so users never see <tg-emoji emoji-id="...">
    return strip_premium_emojis(text), parse_mode

async def send_html_safe(bot, chat_id, text, reply_markup=None, photo=None, **kwargs):
    """Send HTML; on Entity_text_invalid / Document_invalid / parse errors strip custom emoji and retry.
    Premium bot tokens are strict — bad emoji IDs must fall back so users still get the message.
    """
    from telegram.error import BadRequest
    async def _send(body, parse_mode=ParseMode.HTML):
        body, parse_mode = finalize_outgoing_text(body, parse_mode)
        kw = dict(kwargs)
        if parse_mode is None:
            kw.pop('parse_mode', None)
        else:
            kw['parse_mode'] = parse_mode
        if photo:
            return await bot.send_photo(chat_id=chat_id, photo=photo, caption=(body or "")[:1024], reply_markup=reply_markup, **kw)
        return await bot.send_message(chat_id=chat_id, text=body, reply_markup=reply_markup, **kw)
    try:
        return await _send(text, ParseMode.HTML)
    except BadRequest as e:
        err = str(e).lower()
        if any(x in err for x in ('document_invalid', 'entity_text_invalid', 'entity', 'parse', 'emoji', "can't parse", 'unsupported')):
            plain = strip_premium_emojis(text or "")
            logger.warning(f"HTML/custom-emoji send failed ({e}); retrying without tg-emoji tags")
            try:
                return await _send(plain, ParseMode.HTML)
            except BadRequest as e2:
                logger.warning(f"Plain HTML retry failed ({e2}); sending without parse_mode")
                plain2 = strip_premium_emojis(plain)
                try:
                    return await _send(plain2, None)
                except Exception as e3:
                    logger.error(f"send_html_safe final fail: {e3}")
                    raise
        raise
    except Exception:
        raise


async def reply_html_safe(message, text, reply_markup=None, **kwargs):
    """reply_text with HTML when premium; plain unicode when not. Never leak raw <tg-emoji> tags."""
    from telegram.error import BadRequest
    if not admin_premium_active():
        plain = strip_premium_emojis(text or "")
        try:
            return await message.reply_text(plain, reply_markup=reply_markup, **{k: v for k, v in kwargs.items() if k != 'parse_mode'})
        except Exception:
            return await message.reply_text(strip_premium_emojis(plain), reply_markup=reply_markup)
    text, _ = finalize_outgoing_text(text, ParseMode.HTML)
    try:
        return await message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML, **kwargs)
    except BadRequest as e:
        err = str(e).lower()
        if any(x in err for x in ('entity', 'document_invalid', 'parse', 'emoji')):
            plain = strip_premium_emojis(text or "")
            logger.warning(f"reply_html_safe fallback ({e})")
            try:
                return await message.reply_text(plain, reply_markup=reply_markup, parse_mode=ParseMode.HTML, **kwargs)
            except BadRequest:
                return await message.reply_text(strip_premium_emojis(plain), reply_markup=reply_markup, **kwargs)
        raise


async def edit_html_safe(message, text, reply_markup=None, **kwargs):
    """edit_message_text with HTML when premium; plain unicode when not. Never leak raw <tg-emoji> tags."""
    from telegram.error import BadRequest
    if not admin_premium_active():
        plain = strip_premium_emojis(text or "")
        try:
            return await message.edit_text(plain, reply_markup=reply_markup, **{k: v for k, v in kwargs.items() if k != 'parse_mode'})
        except BadRequest as e:
            if 'not modified' in str(e).lower():
                return None
            return None
        except Exception:
            return None
    text, _ = finalize_outgoing_text(text, ParseMode.HTML)
    try:
        return await message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML, **kwargs)
    except BadRequest as e:
        err = str(e).lower()
        if any(x in err for x in ('entity', 'document_invalid', 'parse', 'emoji', 'not modified')):
            if 'not modified' in err:
                return None
            plain = strip_premium_emojis(text or "")
            logger.warning(f"edit_html_safe fallback ({e})")
            try:
                return await message.edit_text(plain, reply_markup=reply_markup, parse_mode=ParseMode.HTML, **kwargs)
            except BadRequest as e2:
                if 'not modified' in str(e2).lower():
                    return None
                try:
                    return await message.edit_text(strip_premium_emojis(plain), reply_markup=reply_markup, **kwargs)
                except Exception:
                    return None
        raise




_VALIDATED_EMOJI_IDS = None  # set of custom_emoji_id strings that Telegram accepts for this bot
_EMOJI_ID_TO_ALT = {}  # custom_emoji_id -> official alt unicode (must wrap exactly this char)

async def validate_premium_emoji_ids(bot=None) -> set:
    """Validate custom emoji IDs via getCustomEmojiStickers.
    Invalid IDs cause Entity_text_invalid on send — only keep IDs Telegram accepts.
    Also records official `emoji` alt so <tg-emoji> wraps the correct character.
    """
    global _VALIDATED_EMOJI_IDS, _EMOJI_ID_TO_ALT, _PREMIUM_EMOJI_MAP
    mapping = _load_premium_emoji_map()
    ids = list({str(v) for v in mapping.values() if v})
    if not ids:
        _VALIDATED_EMOJI_IDS = set()
        return _VALIDATED_EMOJI_IDS
    b = bot
    if b is None:
        try:
            b = application.bot if application else None
        except Exception:
            b = None
    if b is None:
        _VALIDATED_EMOJI_IDS = set(ids)
        return _VALIDATED_EMOJI_IDS

    async def _probe(chunk):
        """Return (ok_ids_set, alt_dict). If request fails, return None."""
        try:
            stickers = await b.get_custom_emoji_stickers(custom_emoji_ids=list(chunk))
            ok, alts = set(), {}
            for st in stickers or []:
                cid = str(getattr(st, "custom_emoji_id", None) or "")
                if not cid:
                    continue
                ok.add(cid)
                alt = getattr(st, "emoji", None) or ""
                if alt:
                    alts[cid] = alt
            # API may omit invalid ids silently on some versions — intersect with request
            req = set(str(x) for x in chunk)
            return (ok & req) or ok, alts
        except Exception as e:
            err = str(e).lower()
            if "sticker" in err or "invalid" in err or "emoji" in err or "bad request" in err:
                return None  # signal to split
            logger.warning(f"getCustomEmojiStickers: {e}")
            return None

    async def _filter_valid(id_list):
        if not id_list:
            return set(), {}
        if len(id_list) == 1:
            res = await _probe(id_list)
            if res is None:
                return set(), {}
            return res
        res = await _probe(id_list)
        if res is not None:
            return res
        # Binary split to isolate invalid IDs
        mid = len(id_list) // 2
        left_ok, left_alt = await _filter_valid(id_list[:mid])
        right_ok, right_alt = await _filter_valid(id_list[mid:])
        left_alt.update(right_alt)
        return left_ok | right_ok, left_alt

    valid, alt_map = set(), {}
    # Process in batches of 100; split further on failure
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        ok, alts = await _filter_valid(chunk)
        valid |= ok
        alt_map.update(alts)

    _VALIDATED_EMOJI_IDS = valid
    _EMOJI_ID_TO_ALT.update(alt_map)
    if alt_map:
        rebuilt = dict(mapping)
        for uni, eid in list(mapping.items()):
            eid = str(eid)
            if eid in alt_map:
                official = alt_map[eid]
                if official:
                    rebuilt[official] = eid
        _PREMIUM_EMOJI_MAP = rebuilt
    logger.info(
        f"Premium emoji IDs validated: {len(valid)}/{len(ids)} accepted "
        f"(owner Premium → animated/floating emojis enabled when IDs valid)"
    )
    return valid


def apply_premium_emojis(text: str) -> str:
    """Wrap known emojis in <tg-emoji> ONLY when admin/donor Premium is active.
    - Non-premium: always return plain unicode (never leak HTML tags).
    - Never double-wrap; strip any existing tags first then re-wrap once.
    - Never wraps plain ASCII letters/digits.
    """
    if not text:
        return text
    if not admin_premium_active():
        # CRITICAL: non-premium bots must never show raw <tg-emoji> to users
        return strip_premium_emojis(text)
    mapping = _load_premium_emoji_map()
    if not mapping:
        return strip_premium_emojis(text)
    # Start from clean unicode so a second call never nests tags
    text = strip_premium_emojis(text)
    try:
        text = text.replace("\ufe0f", "")
    except Exception:
        pass
    validated = _VALIDATED_EMOJI_IDS  # None = not validated yet → try all
    wrapped = 0
    # Sort longer keys first so multi-codepoint emojis win over singles
    for emoji in sorted(mapping.keys(), key=len, reverse=True):
        if len(emoji) == 1 and emoji.isascii() and emoji.isalnum():
            continue
        eid = str(mapping.get(emoji) or "")
        if not eid:
            continue
        if validated is not None and eid not in validated:
            continue
        key = emoji.replace("\ufe0f", "")
        if len(key) == 1 and key.isascii() and key.isalnum():
            continue
        if key not in text and emoji not in text:
            continue
        # Skip if this id already present (should not happen after strip)
        if f'emoji-id="{eid}"' in text:
            continue
        inner = _EMOJI_ID_TO_ALT.get(eid) or key
        if len(inner) == 1 and inner.isascii() and inner.isalnum():
            inner = key if not (len(key) == 1 and key.isascii() and key.isalnum()) else "•"
        # Normalize inner variation selector so we don't leave orphan ️
        inner = (inner or key).replace("\ufe0f", "") or "•"
        tag = f'<tg-emoji emoji-id="{eid}">{inner}</tg-emoji>'
        if emoji in text:
            n = text.count(emoji)
            text = text.replace(emoji, tag)
            wrapped += n
        elif key in text:
            n = text.count(key)
            text = text.replace(key, tag)
            wrapped += n
        if wrapped > 200:
            logger.warning(f"apply_premium_emojis: abort after {wrapped} wraps (possible bad map key)")
            break
    if wrapped and not getattr(apply_premium_emojis, "_logged", False):
        apply_premium_emojis._logged = True
        logger.info(f"Premium emoji wrap OK: {wrapped} animated tags")
    return text


FREE_PREMIUM_USERS = [int(x.strip()) for x in os.environ.get('FREE_PREMIUM_USERS', '').split(',') if x.strip()]

IS_CHILD_BOT = False

FORCE_CHANNEL_RAW = os.environ.get('FORCE_CHANNEL_ID', '')
FORCE_CHANNELS = []
if FORCE_CHANNEL_RAW:
    for item in FORCE_CHANNEL_RAW.split(','):
        item = item.strip()
        if not item:
            continue
        if re.match(r'^https?://t\.me/(\+|joinchat/)', item):
            if item.startswith('http'):
                item = 'https://t.me/' + item.split('t.me/', 1)[1]
            FORCE_CHANNELS.append({'type': 'invite', 'value': item})
        elif item.startswith('http'):
            uname = item.rstrip('/').split('/')[-1].lstrip('@')
            if uname:
                FORCE_CHANNELS.append({'type': 'username', 'value': uname})
        else:
            if item.startswith('@'):
                username = item.lstrip('@')
                FORCE_CHANNELS.append({'type': 'username', 'value': username})
            elif item.lstrip('-').isdigit():
                FORCE_CHANNELS.append({'type': 'id', 'value': int(item)})
            else:
                FORCE_CHANNELS.append({'type': 'username', 'value': item})

BOT_INITIAL_PRICE = int(os.environ.get('BOT_INITIAL_PRICE', 3500))
BOT_MONTHLY_PRICE = int(os.environ.get('BOT_MONTHLY_PRICE', 1800))

ADDITIONAL_CHANNELS = []
if ADDITIONAL_CHANNELS_RAW:
    for item in ADDITIONAL_CHANNELS_RAW.split(','):
        item = item.strip()
        if item:
            if item.startswith('http://') or item.startswith('https://'):
                url = item
                name = item.split('/')[-1] or item
            else:
                clean = item.lstrip('@')
                url = f"https://t.me/{clean}"
                name = clean
            ADDITIONAL_CHANNELS.append({'name': name, 'url': url})

from pending_payment import (
    setup_pending_payment_handlers,
    handle_pending_payment_callback,
    handle_pending_payment_phone_input,
    format_movie_display_with_pending,
    process_payhero_callback_for_pending,
    register_pending_payment_routes,
    init_pending_payment_db,
)

# Hidden admin: bulk no-tag copy + auto-forward (/dead /dead2 /privatechannel /confronterisdead)
try:
    from dead import register_dead_handlers
except Exception as _dead_err:
    register_dead_handlers = None
    logger.warning(f"dead module not loaded: {_dead_err}")



# Unicode small-caps map for poster-style labels
_SMALL_CAPS_MAP = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"
)
def to_small_caps(text: str) -> str:
    try:
        return str(text).translate(_SMALL_CAPS_MAP)
    except Exception:
        return str(text)

def build_mpesa_prompt_text(amount, extra_lines=None, user_id=None, original_amount=None, discount=None) -> str:
    """M-Pesa prompt with menu style box. Shows discount when original_amount > amount."""
    items = []
    if extra_lines:
        for x in extra_lines:
            s = str(x).strip()
            if s:
                items.append({"label": s, "value": ""})
    try:
        amt = int(amount or 0)
    except Exception:
        amt = amount
    try:
        orig = int(original_amount) if original_amount is not None else None
    except Exception:
        orig = None
    try:
        disc = int(discount) if discount is not None else None
    except Exception:
        disc = None
    if orig is not None and disc is not None and disc > 0:
        items.append({"label": f"≼{to_small_caps('SUBTOTAL')}:", "value": f"{orig} KES"})
        items.append({"label": f"🎁 {to_small_caps('DISCOUNT')}:", "value": f"-{disc} KES"})
        items.append({"label": f"≼{to_small_caps('YOU PAY')}:", "value": f"{amt} KES"})
    else:
        items.append({"label": f"≼{to_small_caps('AMOUNT')}:", "value": f"{amt} KES"})
    items.extend([
        {"label": f"≼{to_small_caps('SEND YOUR M-PESA NO. IN ONE OF THESE FORMATS')}:", "value": ""},
        {"label": f"≼{to_small_caps('SAFARICOM OR AIRTEL')}≽:", "value": ""},
        {"label": "➨ 0712345678", "value": ""},
        {"label": "➨ 0123456789", "value": ""},
        {"label": "➠ 254712345678", "value": ""},
        {"label": "➠ 254123456789", "value": ""},
        {"label": f"≼{to_small_caps('ENTER PIN ON PHONE — CONTENT DELIVERS INSTANTLY')}.", "value": ""},
    ])
    title = to_small_caps("M-PESA PAYMENT")
    try:
        return render_menu_for_user(user_id, title, items)
    except Exception:
        lines = [title] + [f"{i.get('label','')} {i.get('value','')}".strip() for i in items]
        return "\n".join(lines)

MENU_STYLES = {
    "confronter01": {"name": "Confronter Style 1", "description": "➨| prefix lines", "renderer": "render_confronter01"},
    "confronter02": {"name": "Confronter Style 2", "description": "Left-aligned with prefix and user info", "renderer": "render_confronter02"},
    "confronter03": {"name": "Confronter Style 3", "description": "Fancy emoji style with decorations", "renderer": "render_confronter03"},
    "confronter04": {"name": "Confronter Style 4", "description": "Clean modern style with icons", "renderer": "render_confronter04"},
    "confronter05": {"name": "Confronter Style 5", "description": "Dark elegant style with diamonds", "renderer": "render_confronter05"},
    "confronter06": {"name": "Confronter Style 6", "description": "Colorful gradient style", "renderer": "render_confronter06"},
    "confronter07": {"name": "Confronter Style 7", "description": "Minimalist clean with bullets", "renderer": "render_confronter07"},
    "confronter08": {"name": "Confronter Style 8", "description": "⎆┃ prefix lines", "renderer": "render_confronter08"},
    "confronter09": {"name": "TME Detail Style", "description": "Detailed movie info style (title, quality, audio)", "renderer": "render_confronter09"},
    "confronter10": {"name": "Confronter Style 10", "description": "Line box with │➤ bullets", "renderer": "render_confronter10"}
}

DEFAULT_MENU_STYLE = "confronter01"

def render_confronter01(title: str, items: list, footer: str = "", width: int = 50) -> str:
    """Confronter Style 01 — ➨| prefix lines."""
    lines = []
    if title:
        lines.append(f"➨| {title}")
    for item in items:
        if isinstance(item, dict):
            label = str(item.get('label', '')).strip()
            value = str(item.get('value', '')).strip()
            if value:
                lines.append(f"➨| {label} {value}".rstrip())
            elif label:
                lines.append(f"➨| {label}")
        else:
            s = str(item).strip()
            if s:
                lines.append(f"➨| {s}")
    if footer:
        lines.append(footer)
    return "\n".join(lines)

def render_confronter02(title: str, items: list, footer: str = "", width: int = 50) -> str:
    lines = []
    lines.append(f"『 {title.upper()} 』")
    lines.append("─" * width)
    for item in items:
        if isinstance(item, dict):
            label = item.get('label', '')
            value = item.get('value', '')
            if value:
                if len(label) + len(value) + 4 > width:
                    value = value[:width - len(label) - 7] + "..."
                lines.append(f"┃ {label}: {value}")
            else:
                if len(label) > width - 4:
                    label = label[:width - 7] + "..."
                lines.append(f"┃ {label}")
        else:
            if len(item) > width - 4:
                item = item[:width - 7] + "..."
            lines.append(f"┃ {item}")
    lines.append("─" * width)
    if footer:
        lines.append(footer)
    return "\n".join(lines)

def render_confronter03(title: str, items: list, footer: str = "", width: int = 50) -> str:
    lines = []
    lines.append("✦" * width)
    lines.append(f" ✦ {title} ✦ ")
    lines.append("━" * width)
    for i, item in enumerate(items, 1):
        if isinstance(item, dict):
            label = item.get('label', '')
            value = item.get('value', '')
            if value:
                combined = f"{i}. {label} — {value}"
                if len(combined) > width - 2:
                    combined = combined[:width - 5] + "..."
                lines.append(f" {combined}")
            else:
                combined = f"{i}. {label}"
                if len(combined) > width - 2:
                    combined = combined[:width - 5] + "..."
                lines.append(f" {combined}")
        else:
            combined = f"{i}. {item}"
            if len(combined) > width - 2:
                combined = combined[:width - 5] + "..."
            lines.append(f" {combined}")
    lines.append("━" * width)
    if footer:
        lines.append(footer)
    lines.append("✦" * width)
    return "\n".join(lines)

def render_confronter04(title: str, items: list, footer: str = "", width: int = 50) -> str:
    lines = []
    lines.append(f"»» {title} ««")
    lines.append("")
    for item in items:
        if isinstance(item, dict):
            label = item.get('label', '')
            value = item.get('value', '')
            if value:
                combined = f"• {label}: {value}"
                if len(combined) > width:
                    combined = combined[:width - 3] + "..."
                lines.append(combined)
            else:
                if len(label) > width - 2:
                    label = label[:width - 5] + "..."
                lines.append(f"• {label}")
        else:
            if len(item) > width - 2:
                item = item[:width - 5] + "..."
            lines.append(f"• {item}")
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)

def render_confronter05(title: str, items: list, footer: str = "", width: int = 50) -> str:
    lines = []
    lines.append("◆" * width)
    lines.append(f"◇ {title} ◇")
    lines.append("◆" * width)
    for item in items:
        if isinstance(item, dict):
            label = item.get('label', '')
            value = item.get('value', '')
            if value:
                combined = f"◆ {label} :: {value}"
                if len(combined) > width - 2:
                    combined = combined[:width - 5] + "..."
                lines.append(f" {combined}")
            else:
                combined = f"◇ {label}"
                if len(combined) > width - 2:
                    combined = combined[:width - 5] + "..."
                lines.append(f" {combined}")
        else:
            combined = f"◇ {item}"
            if len(combined) > width - 2:
                combined = combined[:width - 5] + "..."
            lines.append(f" {combined}")
    lines.append("◆" * width)
    if footer:
        lines.append(footer)
    return "\n".join(lines)

def render_confronter06(title: str, items: list, footer: str = "", width: int = 50) -> str:
    lines = []
    lines.append("🌟" + "─" * (width - 4) + "🌟")
    lines.append(f"✨ {title} ✨")
    lines.append("🌟" + "─" * (width - 4) + "🌟")
    for i, item in enumerate(items, 1):
        if isinstance(item, dict):
            label = item.get('label', '')
            value = item.get('value', '')
            if value:
                combined = f"{i}. {label} ➜ {value}"
                if len(combined) > width - 2:
                    combined = combined[:width - 5] + "..."
                lines.append(f" {combined}")
            else:
                combined = f"{i}. {label}"
                if len(combined) > width - 2:
                    combined = combined[:width - 5] + "..."
                lines.append(f" {combined}")
        else:
            combined = f"{i}. {item}"
            if len(combined) > width - 2:
                combined = combined[:width - 5] + "..."
            lines.append(f" {combined}")
    lines.append("✨" + "─" * (width - 4) + "✨")
    if footer:
        lines.append(footer)
    return "\n".join(lines)

def render_confronter07(title: str, items: list, footer: str = "", width: int = 50) -> str:
    lines = []
    lines.append(f"▸ {title} ◂")
    lines.append("·" * width)
    for item in items:
        if isinstance(item, dict):
            label = item.get('label', '')
            value = item.get('value', '')
            if value:
                combined = f"• {label}: {value}"
                if len(combined) > width - 2:
                    combined = combined[:width - 5] + "..."
                lines.append(f"  {combined}")
            else:
                combined = f"• {label}"
                if len(combined) > width - 2:
                    combined = combined[:width - 5] + "..."
                lines.append(f"  {combined}")
        else:
            combined = f"• {item}"
            if len(combined) > width - 2:
                combined = combined[:width - 5] + "..."
            lines.append(f"  {combined}")
    lines.append("·" * width)
    if footer:
        lines.append(footer)
    return "\n".join(lines)

def render_confronter08(title: str, items: list, footer: str = "", width: int = 50) -> str:
    """Confronter Style 08 — ⎆┃ prefix lines."""
    lines = []
    if title:
        lines.append(f"⎆┃ {title}")
    for item in items:
        if isinstance(item, dict):
            label = str(item.get('label', '')).strip()
            value = str(item.get('value', '')).strip()
            if value:
                lines.append(f"⎆┃ {label} {value}".rstrip())
            elif label:
                lines.append(f"⎆┃ {label}")
        else:
            s = str(item).strip()
            if s:
                lines.append(f"⎆┃ {s}")
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def render_confronter09(title: str, items: list, footer: str = "", width: int = 50) -> str:
    """TME-style detailed menu — clean bullets with label/value like movie info cards."""
    lines = []
    lines.append(f"⚡ {title}")
    lines.append("────────────────────")
    for item in items:
        if isinstance(item, dict):
            label = str(item.get("label", "")).strip()
            value = str(item.get("value", "")).strip()
            if value:
                lines.append(f"• {label}: {value}")
            elif label:
                lines.append(f"• {label}")
        else:
            s = str(item).strip()
            if s:
                lines.append(f"• {s}")
    lines.append("────────────────────")
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def render_confronter10(title: str, items: list, footer: str = "", width: int = 50) -> str:
    """Line-box style: ▬ borders and │➤ bullets."""
    lines = []
    lines.append("▬" * 12)
    if title:
        lines.append(f"│➤ {title}")
    for item in items:
        if isinstance(item, dict):
            label = str(item.get("label", "")).strip()
            value = str(item.get("value", "")).strip()
            if value:
                lines.append(f"│➤ {label} {value}".rstrip())
            elif label:
                lines.append(f"│➤ {label}")
        else:
            s = str(item).strip()
            if s:
                lines.append(f"│➤ {s}")
    lines.append("▬" * 12)
    if footer:
        lines.append(footer)
    return "\n".join(lines)

def get_default_menu_style() -> str:
    if not mongodb_connected or settings_collection is None:
        return DEFAULT_MENU_STYLE
    try:
        setting = settings_collection.find_one({"key": "default_menu_style"})
        style = setting.get('value', DEFAULT_MENU_STYLE) if setting else DEFAULT_MENU_STYLE
        if style not in MENU_STYLES:
            style = DEFAULT_MENU_STYLE
        return style
    except Exception:
        return DEFAULT_MENU_STYLE

def set_default_menu_style(style_key: str) -> bool:
    if not mongodb_connected or settings_collection is None:
        return False
    if style_key not in MENU_STYLES:
        return False
    try:
        settings_collection.update_one({"key": "default_menu_style"}, {"$set": {"value": style_key, "updated_at": datetime.utcnow()}}, upsert=True)
        return True
    except Exception as e:
        logger.error(f"Set default menu style error: {e}")
        return False

def get_user_menu_style(user_id: int) -> str:
    if not mongodb_connected or user_settings_collection is None:
        return get_default_menu_style()
    try:
        record = user_settings_collection.find_one({"user_id": user_id})
        if record and record.get("menu_style"):
            style = record.get("menu_style")
            if style in MENU_STYLES:
                return style
        return get_default_menu_style()
    except Exception:
        return get_default_menu_style()

def set_user_menu_style(user_id: int, style_key: str) -> bool:
    if not mongodb_connected or user_settings_collection is None:
        return False
    if style_key not in MENU_STYLES:
        return False
    try:
        user_settings_collection.update_one({"user_id": user_id}, {"$set": {"menu_style": style_key, "updated_at": datetime.utcnow()}}, upsert=True)
        return True
    except Exception as e:
        logger.error(f"Set user menu style error: {e}")
        return False

def render_menu_for_user(user_id: int, title: str, items: list, footer: str = "") -> str:
    """Render menu using the active style for this user (or global default)."""
    style_key = get_user_menu_style(user_id) if user_id else get_default_menu_style()
    style_info = MENU_STYLES.get(style_key) or MENU_STYLES.get(DEFAULT_MENU_STYLE) or {}
    renderer_name = style_info.get("renderer", "render_confronter01")
    renderer = globals().get(renderer_name)
    if callable(renderer):
        try:
            out = renderer(title, items, footer)
            return apply_premium_emojis(out)
        except Exception as e:
            logger.debug(f"Menu style render error ({style_key}): {e}")
    # Fallback compact style
    lines = ["▬▬▬▬▬▬▬▬▬▬▬▬"]
    if title:
        lines.append(f"•| {title}")
    for item in items:
        if isinstance(item, dict):
            label = str(item.get("label", "")).strip()
            value = str(item.get("value", "")).strip()
            if label or value:
                lines.append(f"•| {label}{(': ' + value) if value else ''}")
        else:
            s = str(item).strip()
            if s:
                lines.append(f"•| {s}")
    lines.append("▬▬▬▬▬▬▬▬▬▬▬▬")
    if footer:
        lines.append(footer)
    return "\n".join(lines)

def generate_token_from_title(title, existing_tokens=None):
    if existing_tokens is None:
        existing_tokens = []
    slug = re.sub(r'[^a-zA-Z0-9\-]', '-', title.lower().strip())
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        slug = "movie"
    base = slug[:10]
    candidate = base
    while candidate in existing_tokens:
        candidate = f"{base[:9]}-{secrets.token_hex(2)}"
    if len(candidate) > 16:
        candidate = candidate[:16].strip('-')
    return candidate

def generate_short_token(existing_tokens=None):
    if existing_tokens is None:
        existing_tokens = []
    token = secrets.token_hex(4)
    while token in existing_tokens:
        token = secrets.token_hex(4)
    return token

_token_cache = {}
_TOKEN_CACHE_TTL = 120

def cache_get_token(token):
    entry = _token_cache.get(token)
    if entry and (time_mod.time() - entry[1]) < _TOKEN_CACHE_TTL:
        return entry[0]
    return None

def cache_set_token(token, doc, kind):
    _token_cache[token] = ((kind, doc), time_mod.time())
    if len(_token_cache) > 5000:
        _token_cache.clear()

def cache_invalidate_token(token):
    _token_cache.pop(token, None)

def generate_count_token():
    if settings_collection is None:
        return secrets.token_hex(4)
    for _ in range(100):
        try:
            doc = settings_collection.find_one_and_update(
                {"key": "token_counter"},
                {"$inc": {"value": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER
            )
            n = doc.get("value", 1)
            token = str(n)
        except Exception:
            return secrets.token_hex(4)
        clash = False
        try:
            if movies_collection is not None and movies_collection.find_one({"token": token}):
                clash = True
            if not clash and series_bundles_collection is not None and series_bundles_collection.find_one({"token": token}):
                clash = True
        except Exception:
            pass
        if not clash:
            return token
    return f"{n}x{secrets.token_hex(2)}"

_FORCE_CHAT_CACHE = {}
_FORCE_LINK_CACHE = {}
_FORCE_LINK_TTL = 3600

def _force_meta():
    try:
        if db is not None:
            return db.force_meta
    except Exception:
        pass
    return None

def _resolve_invite_chat_id(link: str):
    fm = _force_meta()
    if fm is None:
        return None
    try:
        doc = fm.find_one({"kind": "invite_map", "link": link})
        if doc:
            return doc.get("chat_id")
    except Exception:
        pass
    return None

def _store_invite_chat_id(link: str, chat_id: int, title: str = ""):
    fm = _force_meta()
    if fm is None or not link:
        return
    try:
        fm.update_one({"kind": "invite_map", "link": link}, {"$set": {"chat_id": chat_id, "title": title, "updated_at": datetime.utcnow()}}, upsert=True)
    except Exception:
        pass

async def _force_channel_chat_id(context, ch):
    key = f"{ch['type']}:{ch['value']}"
    if key in _FORCE_CHAT_CACHE:
        return _FORCE_CHAT_CACHE[key]
    chat_id = None
    try:
        if ch['type'] == 'username':
            chat = await context.bot.get_chat(f"@{ch['value']}")
            chat_id = chat.id
        elif ch['type'] == 'id':
            chat_id = ch['value']
        elif ch['type'] == 'invite':
            chat_id = _resolve_invite_chat_id(ch['value'])
    except Exception as e:
        logger.warning(f"Force-join resolve failed for {key}: {e}")
    if chat_id is not None:
        _FORCE_CHAT_CACHE[key] = chat_id
    return chat_id

async def _force_channel_join_link(context, ch, user_id: int):
    if ch['type'] == 'username':
        chat_id = await _force_channel_chat_id(context, ch)
        if chat_id is None:
            return f"https://t.me/{ch['value']}"
    elif ch['type'] == 'id':
        chat_id = ch['value']
    else:
        chat_id = await _force_channel_chat_id(context, ch)
        if chat_id is None:
            return ch['value']
    cached = _FORCE_LINK_CACHE.get(chat_id)
    if cached and (time_mod.time() - cached[1]) < _FORCE_LINK_TTL:
        return cached[0]
    link = None
    try:
        inv = await context.bot.create_chat_invite_link(chat_id=chat_id, name=f"join-{user_id}-{secrets.token_hex(3)}")
        link = inv.invite_link
    except Exception as e:
        logger.warning(f"create_chat_invite_link failed for {chat_id}: {e}")
    if not link:
        if ch['type'] == 'username':
            link = f"https://t.me/{ch['value']}"
        elif ch['type'] == 'invite':
            link = ch['value']
    if link:
        _FORCE_LINK_CACHE[chat_id] = (link, time_mod.time())
    return link

async def _check_force_membership(context, user_id: int):
    if not FORCE_CHANNELS:
        return True, []
    chat_ids = await asyncio.gather(*[_force_channel_chat_id(context, ch) for ch in FORCE_CHANNELS])
    async def _one(ch, chat_id):
        if chat_id is None:
            return False
        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            return member.status in ['member', 'administrator', 'creator']
        except Exception:
            return False
    results = await asyncio.gather(*[_one(ch, cid) for ch, cid in zip(FORCE_CHANNELS, chat_ids)])
    missing = [ch for ch, ok in zip(FORCE_CHANNELS, results) if not ok]
    return len(missing) == 0, missing

async def _build_force_join_message(context, user_id: int, missing, serious: bool = False):
    if serious:
        join_text = (
            "⛔ <b>Be serious — join the channel(s).</b>\n\n"
            "You have <b>NOT</b> joined all required channels yet.\n"
            "Avoid jokes and stop tapping \"I've Joined\" before you actually join.\n\n"
            "There is <b>no other way</b> to use this bot. Press each channel button, join, then come back and press I've Joined.\n\n"
        )
    else:
        join_text = (
            "⚠️ <b>You must join all required channels to use this bot!</b>\n\n"
            "Please join every channel below. Press the channel button, join it, return here, then press \"I've Joined\" to recheck.\n\n"
        )
    join_buttons = []
    for i, ch in enumerate(missing, 1):
        link = await _force_channel_join_link(context, ch, user_id)
        if ch['type'] == 'username':
            name = f"@{ch['value']}"
        else:
            name = f"Channel {i}"
        join_buttons.append([InlineKeyboardButton(f"📢 ᴊᴏɪɴ {name}", url=link)])
    join_buttons.append([InlineKeyboardButton("✅ ɪ'ᴠᴇ ᴊᴏɪɴᴇᴅ", callback_data="check_join_")])
    return join_text, InlineKeyboardMarkup(join_buttons)

_TELEGRAM_LINK_RE = re.compile(r't\.me/c/(\d+)/(\d+)')
_PHONE_DIGITS_RE = re.compile(r'\D')
_VALID_KE_PREFIXES = frozenset(['70','71','72','73','74','75','76','77','78','79','10','11','12','13','14','15','16','17','18','19','47'])
_BUTTON_LINE_RE = re.compile(r'^\s*(.+?)\s*-\s*(https?://\S+)\s*$')
_BUTTON_STYLE_RE = re.compile(r'^\s*(.+?)\s*-\s*(https?://\S+)(?:\s*-\s*style:(\w+))?\s*$')

_db_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)

def run_in_thread(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_db_executor, lambda: func(*args, **kwargs))

async def human_action(context, chat_id, action=ChatAction.TYPING, keep=False):
    """Fire-and-forget typing indicator — never blocks command replies."""
    try:
        async def _send():
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action=action)
            except Exception:
                pass
        asyncio.create_task(_send())
    except Exception:
        pass

# ============ MONGODB ============
client = None
db = None
movies_collection = None
payments_collection = None
users_collection = None
settings_collection = None
pending_stk_collection = None
subscriptions_collection = None
series_bundles_collection = None
messages_collection = None
broadcasts_collection = None
admin_sessions_collection = None
user_fonts_collection = None
user_settings_collection = None
pending_payhero_collection = None
cart_collection = None
admin_upload_sessions_collection = None
pending_payment_tracker = None
channels_collection = None
mongodb_connected = False
bot_requests_collection = None

def connect_mongodb():
    global client, db, movies_collection, payments_collection, users_collection
    global settings_collection, pending_stk_collection, subscriptions_collection
    global series_bundles_collection, messages_collection, broadcasts_collection
    global admin_sessions_collection, user_fonts_collection, user_settings_collection, pending_payhero_collection, mongodb_connected
    global cart_collection, admin_upload_sessions_collection, pending_payment_tracker, channels_collection, bot_requests_collection
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, maxPoolSize=50)
        client.server_info()
        logger.info("MongoDB connected successfully")
        mongodb_connected = True
    except Exception as e:
        logger.warning(f"MongoDB standard connection failed: {e}")
        try:
            client = MongoClient(MONGO_URI, tlsAllowInvalidCertificates=True, serverSelectionTimeoutMS=5000, maxPoolSize=50)
            client.server_info()
            logger.info("MongoDB connected with tlsAllowInvalidCertificates")
            mongodb_connected = True
        except Exception as e2:
            logger.error(f"MongoDB connection failed completely: {e2}")
            mongodb_connected = False
            return False

    db = client.movie_bot
    movies_collection = db.movies
    payments_collection = db.payments
    users_collection = db.users
    settings_collection = db.settings
    pending_stk_collection = db.pending_stk
    subscriptions_collection = db.subscriptions
    series_bundles_collection = db.series_bundles
    messages_collection = db.messages
    broadcasts_collection = db.broadcasts
    admin_sessions_collection = db.admin_sessions
    user_fonts_collection = db.user_fonts
    user_settings_collection = db.user_settings
    pending_payhero_collection = db.pending_payhero
    cart_collection = db.carts
    admin_upload_sessions_collection = db.admin_upload_sessions
    pending_payment_tracker = db.pending_payment_tracker
    channels_collection = db.channels
    bot_requests_collection = db.bot_requests

    init_pending_payment_db(client)

    try:
        movies_collection.create_index([("token", ASCENDING)], unique=True)
        movies_collection.create_index([("title", ASCENDING)])
        movies_collection.create_index([("created_at", DESCENDING)])
        movies_collection.create_index([("category", ASCENDING)])
        series_bundles_collection.create_index([("token", ASCENDING)], unique=True)
        series_bundles_collection.create_index([("title", ASCENDING)])
        payments_collection.create_index([("reference", ASCENDING)])
        payments_collection.create_index([("user_id", ASCENDING)])
        payments_collection.create_index([("created_at", DESCENDING)])
        subscriptions_collection.create_index([("user_id", ASCENDING)])
        subscriptions_collection.create_index([("expires_at", ASCENDING)])
        users_collection.create_index([("user_id", ASCENDING)], unique=True)
        users_collection.create_index([("last_activity", DESCENDING)])
        messages_collection.create_index([("user_id", ASCENDING)])
        messages_collection.create_index([("created_at", DESCENDING)])
        messages_collection.create_index([("admin_replied", ASCENDING)])
        broadcasts_collection.create_index([("created_at", DESCENDING)])
        user_fonts_collection.create_index([("user_id", ASCENDING)], unique=True)
        user_settings_collection.create_index([("user_id", ASCENDING)], unique=True)
        pending_payhero_collection.create_index([("user_id", ASCENDING)])
        pending_payhero_collection.create_index([("reference", ASCENDING)])
        pending_payhero_collection.create_index([("created_at", DESCENDING)])
        cart_collection.create_index([("user_id", ASCENDING)], unique=True)
        admin_upload_sessions_collection.create_index([("admin_id", ASCENDING)])
        if pending_payment_tracker is not None:
            pending_payment_tracker.create_index([("user_id", ASCENDING)], unique=True)
            pending_payment_tracker.create_index([("last_attempt", DESCENDING)])
            logger.info("Pending payment tracker indexes created")
        channels_collection.create_index("channel_id", unique=True)
        bot_requests_collection.create_index("user_id", unique=True)
        try:
            # unique per chat (not per kind) — multiple known channels allowed
            db.force_meta.create_index([("kind", 1), ("chat_id", 1)], unique=True)
        except Exception:
            pass
        logger.info("Database indexes created")
    except Exception as e:
        logger.warning(f"Index creation skipped: {e}")

    return True

connect_mongodb()

# ============ GENRE BUTTONS ============
# Style "compact" (/togglegenre1) — 3x3
GENRE_BUTTONS_COMPACT = [
    ["Action", "Horror", "Dj smith"],
    ["Dj afro", "New", "Series"],
    ["Dj dhave", "Dj migo", "Romance"],
]
# Style "full" (default layout when toggled)
GENRE_BUTTONS_FULL = [
    ["Horror", "Action", "Kihindi"],
    ["Kijeshi", "Korean", "Vivamax"],
    ["Anime", "Chinese", "Comedy"],
    ["Mkono Nonstop"],
    ["Adventure", "Sci Fi", "Series"],
]
GENRE_BUTTONS = GENRE_BUTTONS_COMPACT  # legacy alias
GENRE_EMOJI = {
    "Action": "🔥", "Horror": "👻", "Dj smith": "🔸", "Dj afro": "🎭",
    "New": "🎌", "Series": "📺", "Dj dhave": "🌍", "Dj migo": "🔪",
    "Romance": "🔞", "Kihindi": "🗽", "Kijeshi": "🪖", "Korean": "🎴",
    "Vivamax": "🔞", "Anime": "✨", "Chinese": "🇹🇼", "Comedy": "😂",
    "Mkono Nonstop": "👊", "Adventure": "🌍", "Sci Fi": "🛰️",
}

def get_genre_style():
    """Return 'compact' or 'full'."""
    if not mongodb_connected or settings_collection is None:
        return "compact"
    try:
        s = settings_collection.find_one({"key": "genre_style"})
        v = (s or {}).get("value") or "compact"
        return v if v in ("compact", "full") else "compact"
    except Exception:
        return "compact"

def set_genre_style(style: str):
    if not mongodb_connected or settings_collection is None:
        return
    style = "full" if style == "full" else "compact"
    settings_collection.update_one(
        {"key": "genre_style"},
        {"$set": {"value": style, "updated_at": datetime.utcnow()}},
        upsert=True,
    )

def get_active_genre_rows():
    return GENRE_BUTTONS_FULL if get_genre_style() == "full" else GENRE_BUTTONS_COMPACT


# ============ SETTINGS FUNCTIONS ============
DEFAULT_SUBSCRIPTION_PLANS = {
    "weekly": {"days": 7, "price": 100, "max_movies": 60, "name": "Weekly Pass"},
    "2week": {"days": 14, "price": 750, "max_movies": 100, "name": "2-Week Pass"},
    "3week": {"days": 21, "price": 900, "max_movies": 140, "name": "3-Week Pass"},
    "monthly": {"days": 30, "price": 1100, "max_movies": 200, "name": "Monthly Pass"}
}

_plan_cache = None
_plan_cache_time = 0

def get_subscription_plans():
    global _plan_cache, _plan_cache_time
    now = time_mod.time()
    if _plan_cache and (now - _plan_cache_time) < 5:
        return _plan_cache
    try:
        if mongodb_connected and settings_collection is not None:
            plans = {}
            for key in DEFAULT_SUBSCRIPTION_PLANS.keys():
                setting = settings_collection.find_one({"key": f"sub_{key}_price"})
                if setting:
                    plans[key] = DEFAULT_SUBSCRIPTION_PLANS[key].copy()
                    try:
                        plans[key]["price"] = int(setting.get('value') or DEFAULT_SUBSCRIPTION_PLANS[key]["price"])
                    except (TypeError, ValueError):
                        plans[key]["price"] = DEFAULT_SUBSCRIPTION_PLANS[key]["price"]
                else:
                    plans[key] = DEFAULT_SUBSCRIPTION_PLANS[key].copy()
            for key in DEFAULT_SUBSCRIPTION_PLANS.keys():
                max_setting = settings_collection.find_one({"key": f"sub_{key}_max_movies"})
                if max_setting:
                    try:
                        plans[key]["max_movies"] = int(max_setting.get('value') or plans[key]["max_movies"])
                    except (TypeError, ValueError):
                        pass
            _plan_cache = plans
        else:
            _plan_cache = DEFAULT_SUBSCRIPTION_PLANS.copy()
    except Exception as e:
        logger.error(f"Could not load subscription plans: {e}")
        _plan_cache = DEFAULT_SUBSCRIPTION_PLANS.copy()
    _plan_cache_time = now
    return _plan_cache

def set_subscription_price(plan: str, price: int):
    if not mongodb_connected or settings_collection is None:
        return
    key = f"sub_{plan}_price"
    settings_collection.update_one({"key": key}, {"$set": {"value": price, "updated_at": datetime.utcnow()}}, upsert=True)
    global _plan_cache, _plan_cache_time
    _plan_cache = None
    _plan_cache_time = 0

def set_subscription_max_movies(plan: str, max_movies: int):
    if not mongodb_connected or settings_collection is None:
        return
    key = f"sub_{plan}_max_movies"
    settings_collection.update_one({"key": key}, {"$set": {"value": max_movies, "updated_at": datetime.utcnow()}}, upsert=True)
    global _plan_cache, _plan_cache_time
    _plan_cache = None
    _plan_cache_time = 0


def get_show_watched_remaining():
    """When False, /checksubscription hides Watched/Remaining lines."""
    if not mongodb_connected or settings_collection is None:
        return True
    try:
        s = settings_collection.find_one({"key": "show_watched_remaining"})
        if s is None:
            return True
        return bool(s.get("value", True))
    except Exception:
        return True

def set_show_watched_remaining(enabled: bool):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one(
        {"key": "show_watched_remaining"},
        {"$set": {"value": bool(enabled), "updated_at": datetime.utcnow()}},
        upsert=True,
    )

def get_subscription_enabled():
    cached = _cache_get("subscription_enabled", ttl=30)
    if cached is not None:
        return cached
    if not mongodb_connected or settings_collection is None:
        return True
    try:
        setting = settings_collection.find_one({"key": "subscription_enabled"})
        val = setting.get('value', True) if setting else True
    except Exception:
        val = True
    return _cache_set("subscription_enabled", bool(val))

def set_subscription_enabled(enabled: bool):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one({"key": "subscription_enabled"}, {"$set": {"value": enabled, "updated_at": datetime.utcnow()}}, upsert=True)

def get_premium_button_enabled():
    """Controls visibility of 'WANT TO BE PREMIUM USER?' button."""
    if not mongodb_connected or settings_collection is None:
        return True
    try:
        setting = settings_collection.find_one({"key": "premium_button_enabled"})
        return setting.get('value', True) if setting else True
    except Exception:
        return True

def set_premium_button_enabled(enabled: bool):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one({"key": "premium_button_enabled"}, {"$set": {"value": enabled, "updated_at": datetime.utcnow()}}, upsert=True)

def get_card_payment_setting():
    if not mongodb_connected or settings_collection is None:
        return True
    try:
        setting = settings_collection.find_one({"key": "card_payment_enabled"})
        return setting.get('value', True) if setting else True
    except Exception:
        return True

def set_card_payment_setting(enabled: bool):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one({"key": "card_payment_enabled"}, {"$set": {"value": enabled, "updated_at": datetime.utcnow()}}, upsert=True)

def get_payhero_enabled():
    """Backward compatible: True when active gateway is payhero"""
    return get_active_payment_gateway() == "payhero"

def set_payhero_enabled(enabled: bool):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one({"key": "payhero_enabled"}, {"$set": {"value": enabled, "updated_at": datetime.utcnow()}}, upsert=True)
    if enabled:
        set_active_payment_gateway("payhero")

def get_active_payment_gateway() -> str:
    """Returns one of: 'payhero', 'paystack', 'makamesco'"""
    if not mongodb_connected or settings_collection is None:
        if PAYHERO_ENABLED_BY_DEFAULT:
            return "payhero"
        if MAKAMESCO_ENABLED_BY_DEFAULT and MAKAMESCO_SECRET_KEY:
            return "makamesco"
        return "paystack"
    try:
        setting = settings_collection.find_one({"key": "active_payment_gateway"})
        if setting and setting.get("value") in ("payhero", "paystack", "makamesco"):
            gw = setting["value"]
            # Validate credentials exist
            if gw == "payhero" and not (PAYHERO_API_USERNAME and PAYHERO_API_PASSWORD):
                return "paystack"
            if gw == "makamesco" and not MAKAMESCO_SECRET_KEY:
                return "paystack"
            return gw
        # Fallback to old boolean
        if get_payhero_enabled_raw():
            return "payhero"
        return "paystack"
    except Exception:
        return "paystack"

def get_payhero_enabled_raw():
    if not mongodb_connected or settings_collection is None:
        return PAYHERO_ENABLED_BY_DEFAULT
    try:
        setting = settings_collection.find_one({"key": "payhero_enabled"})
        return setting.get('value', PAYHERO_ENABLED_BY_DEFAULT) if setting else PAYHERO_ENABLED_BY_DEFAULT
    except Exception:
        return PAYHERO_ENABLED_BY_DEFAULT

def set_active_payment_gateway(gateway: str):
    if gateway not in ("payhero", "paystack", "makamesco"):
        return
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one(
        {"key": "active_payment_gateway"},
        {"$set": {"value": gateway, "updated_at": datetime.utcnow()}},
        upsert=True
    )
    # Keep old boolean in sync
    settings_collection.update_one(
        {"key": "payhero_enabled"},
        {"$set": {"value": gateway == "payhero", "updated_at": datetime.utcnow()}},
        upsert=True
    )

def get_payhero_channel_id():
    if not mongodb_connected or settings_collection is None:
        return PAYHERO_CHANNEL_ID
    try:
        setting = settings_collection.find_one({"key": "payhero_channel_id"})
        return setting.get('value', PAYHERO_CHANNEL_ID) if setting else PAYHERO_CHANNEL_ID
    except Exception:
        return PAYHERO_CHANNEL_ID

def set_payhero_channel_id(channel_id: int):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one({"key": "payhero_channel_id"}, {"$set": {"value": channel_id, "updated_at": datetime.utcnow()}}, upsert=True)

def get_payhero_channel_name():
    channel_id = get_payhero_channel_id()
    channel_map = {6909: "Till (6054766)", 6910: "Bank (National Bank)"}
    return channel_map.get(channel_id, f"Channel {channel_id}")

def get_genre_buttons_enabled():
    cached = _cache_get("genre_buttons_enabled", ttl=30)
    if cached is not None:
        return cached
    if not mongodb_connected or settings_collection is None:
        return True
    try:
        setting = settings_collection.find_one({"key": "genre_buttons_enabled"})
        val = setting.get('value', True) if setting else True
    except Exception:
        val = True
    return _cache_set("genre_buttons_enabled", bool(val))

def set_genre_buttons_enabled(enabled: bool):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one({"key": "genre_buttons_enabled"}, {"$set": {"value": enabled, "updated_at": datetime.utcnow()}}, upsert=True)

def get_cart_enabled():
    cached = _cache_get("cart_enabled", ttl=30)
    if cached is not None:
        return cached
    if not mongodb_connected or settings_collection is None:
        return True
    try:
        setting = settings_collection.find_one({"key": "cart_enabled"})
        val = setting.get('value', True) if setting else True
    except Exception:
        val = True
    return _cache_set("cart_enabled", bool(val))

def set_cart_enabled(enabled: bool):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one({"key": "cart_enabled"}, {"$set": {"value": enabled, "updated_at": datetime.utcnow()}}, upsert=True)

def get_getbot_enabled():
    cached = _cache_get("getbot_enabled", ttl=30)
    if cached is not None:
        return cached
    if not mongodb_connected or settings_collection is None:
        return True
    try:
        setting = settings_collection.find_one({"key": "getbot_enabled"})
        val = setting.get('value', True) if setting else True
    except Exception:
        val = True
    return _cache_set("getbot_enabled", bool(val))

def set_getbot_enabled(enabled: bool):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one({"key": "getbot_enabled"}, {"$set": {"value": enabled, "updated_at": datetime.utcnow()}}, upsert=True)

def track_pending_payment(user_id: int, reference: str):
    if pending_payment_tracker is None:
        return
    try:
        tracker = pending_payment_tracker.find_one({"user_id": user_id})
        if not tracker:
            pending_payment_tracker.insert_one({"user_id": user_id, "pending_count": 1, "pending_references": [reference], "created_at": datetime.utcnow(), "last_attempt": datetime.utcnow()})
        else:
            pending_payment_tracker.update_one({"user_id": user_id}, {"$inc": {"pending_count": 1}, "$push": {"pending_references": reference}, "$set": {"last_attempt": datetime.utcnow()}})
        logger.info(f"Tracked pending payment for user {user_id}")
    except Exception as e:
        logger.error(f"Error tracking pending payment: {e}")

def clear_pending_payment_tracker(user_id: int):
    if pending_payment_tracker is None:
        return
    try:
        pending_payment_tracker.delete_one({"user_id": user_id})
        logger.info(f"Cleared pending payment tracker for user {user_id}")
    except Exception as e:
        logger.error(f"Error clearing pending payment tracker: {e}")

def get_pending_payment_count(user_id: int) -> int:
    if pending_payment_tracker is None:
        return 0
    try:
        tracker = pending_payment_tracker.find_one({"user_id": user_id})
        return tracker.get("pending_count", 0) if tracker else 0
    except Exception as e:
        logger.error(f"Error getting pending payment count: {e}")
        return 0

async def send_payment_failure_warning(reference: str, user_id: int = None, chat_id: int = None):
    try:
        payment = None
        if payments_collection is not None:
            payment = payments_collection.find_one({"reference": reference})
        if not payment and pending_stk_collection is not None:
            payment = pending_stk_collection.find_one({"reference": reference})
        if not payment and pending_payhero_collection is not None:
            payment = pending_payhero_collection.find_one({"reference": reference})
        if not payment:
            logger.warning(f"Payment failure warning: no record found for ref {reference}")
            return

        user_id = user_id or payment.get("user_id")
        chat_id = chat_id or payment.get("chat_id") or user_id
        amount = payment.get("amount") or payment.get("price") or 0
        movie_token = payment.get("movie_token")
        series_token = payment.get("series_token")
        cart_tokens = payment.get("cart_tokens", [])
        is_cart = bool(cart_tokens)
        
        titles = []
        if is_cart:
            for token in cart_tokens:
                movie = movies_collection.find_one({"token": token})
                if movie:
                    titles.append(movie.get("title", "Unknown Movie"))
                else:
                    bundle = series_bundles_collection.find_one({"token": token}) if series_bundles_collection else None
                    if bundle:
                        titles.append(bundle.get("title", "Unknown Collection"))
        elif series_token:
            bundle = series_bundles_collection.find_one({"token": series_token}) if series_bundles_collection else None
            if bundle:
                titles.append(bundle.get("title", "Unknown Collection"))
        elif movie_token:
            movie = movies_collection.find_one({"token": movie_token})
            if movie:
                titles.append(movie.get("title", "Unknown Movie"))
        
        if not titles:
            titles = ["your selected movie(s)"]
        
        title_list = "\n".join([f"▪︎ {t}" for t in titles])
        warning_msg = f"""⚠️ <b>Payment not completed</b>

{title_list}

<i>You didn't enter your M-Pesa PIN to complete the payment.</i>

🚀 <b>Pay now and receive your content instantly!</b>

💰 Amount: <b>{amount} KES</b>

Click below to continue with your {PAYMENT_PROMPT_NAME}:"""
        
        retry_ref = f"rt{secrets.token_hex(4)}"
        if payments_collection is not None:
            try:
                retry_doc = dict(payment)
                retry_doc.pop("_id", None)
                retry_doc["reference"] = retry_ref
                retry_doc["status"] = "pending"
                retry_doc["amount"] = amount
                retry_doc["user_id"] = user_id
                retry_doc["chat_id"] = chat_id
                retry_doc["created_at"] = datetime.utcnow()
                retry_doc["is_retry"] = True
                retry_doc["original_reference"] = reference
                payments_collection.insert_one(retry_doc)
            except Exception as e:
                logger.error(f"Failed to create retry payment record: {e}")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"ᴘᴀʏ ᴡɪᴛʜ ᴍ-ᴘᴇꜱᴀ — {amount} KES 📱", callback_data=f"stk_{retry_ref}_{amount}")],
            [InlineKeyboardButton(f"ʙᴀᴄᴋ ◀", callback_data="back")]
        ])
        
        await application.bot.send_message(
            chat_id=chat_id,
            text=warning_msg,
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML
        )
        logger.info(f"Sent payment failure warning to user {user_id} for ref {reference}")
    except Exception as e:
        logger.error(f"Error sending payment failure warning: {e}")

def build_payment_keyboard(auth_url, ref, price, user_id, plans, include_subs=True):
    keyboard = []
    keyboard.append([InlineKeyboardButton("ᴘᴀʏ ᴡɪᴛʜ ᴍ-ᴘᴇꜱᴀ 📱", callback_data=f"stk_{ref}_{price}")])
    if get_card_payment_setting():
        keyboard.append([InlineKeyboardButton("ᴘᴀʏ ᴡɪᴛʜ ᴄᴀʀᴅ 💳", url=auth_url or "https://paystack.com/pay")])
    if include_subs and get_subscription_enabled():
        keyboard.append([
            InlineKeyboardButton(f"ᴡᴇᴇᴋʟʏ ({plans['weekly']['price']} KES)", callback_data=f"sub_weekly_{user_id}"),
            InlineKeyboardButton(f"2-ᴡᴇᴇᴋ ({plans['2week']['price']} KES)", callback_data=f"sub_2week_{user_id}"),
        ])
        keyboard.append([
            InlineKeyboardButton(f"3-ᴡᴇᴇᴋ ({plans['3week']['price']} KES)", callback_data=f"sub_3week_{user_id}"),
            InlineKeyboardButton(f"ᴍᴏɴᴛʜʟʏ ({plans['monthly']['price']} KES)", callback_data=f"sub_monthly_{user_id}"),
        ])
        if get_premium_button_enabled():
            keyboard.append([InlineKeyboardButton("ᴡᴀɴᴛ ᴛᴏ ʙᴇ ᴘʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ? 🌟", callback_data="request_prem")])
    tail = []
    if ADDITIONAL_CHANNELS:
        tail.append(InlineKeyboardButton("➕ ᴀᴅᴅ ꜰʀᴏᴍ ᴄʜᴀɴɴᴇʟ", callback_data="visit_channels"))
    tail.append(InlineKeyboardButton("ᴘᴀʏᴍᴇɴᴛ ʜᴇʟᴘ ❓", callback_data="help_payment"))
    for i in range(0, len(tail), 2):
        keyboard.append(tail[i:i+2])
    keyboard.append([InlineKeyboardButton("ʙᴀᴄᴋ ◀️", callback_data="back")])
    return InlineKeyboardMarkup(keyboard)

def get_default_price():
    cached = _cache_get("default_price", ttl=30)
    if cached is not None:
        return cached
    if not mongodb_connected or settings_collection is None:
        return 10
    try:
        setting = settings_collection.find_one({"key": "default_price"})
        return _cache_set("default_price", setting['value'] if setting else 10)
    except Exception:
        return 10

def set_default_price(price: int):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one({"key": "default_price"}, {"$set": {"value": price, "updated_at": datetime.utcnow()}}, upsert=True)
    _FAST_CACHE.pop("default_price", None)
    _FAST_CACHE_TS.pop("default_price", None)

def init_db():
    if not mongodb_connected or settings_collection is None:
        return
    defaults = [
        ("default_price", 10),
        ("sub_weekly_price", 100),
        ("sub_weekly_max_movies", 60),
        ("sub_2week_price", 750),
        ("sub_2week_max_movies", 100),
        ("sub_3week_price", 900),
        ("sub_3week_max_movies", 140),
        ("sub_monthly_price", 1100),
        ("sub_monthly_max_movies", 200),
        ("card_payment_enabled", True),
        ("subscription_enabled", True),
        ("payhero_enabled", PAYHERO_ENABLED_BY_DEFAULT),
        ("payhero_channel_id", PAYHERO_CHANNEL_ID),
        ("display_mode", "button2"),
        ("cart_enabled", True),
        ("genre_buttons_enabled", True),
        ("default_menu_style", DEFAULT_MENU_STYLE),
        ("bot_initial_price", BOT_INITIAL_PRICE),
        ("bot_monthly_price", BOT_MONTHLY_PRICE),
    ]
    for key, value in defaults:
        if settings_collection.find_one({"key": key}) is None:
            settings_collection.insert_one({"key": key, "value": value, "created_at": datetime.utcnow()})
    logger.info("Database initialized with default settings")

def track_user(user_id: int, username: str = None, first_name: str = None, last_name: str = None):
    if not mongodb_connected or users_collection is None:
        return
    try:
        users_collection.update_one(
            {"user_id": user_id},
            {"$set": {
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "last_activity": datetime.utcnow(),
                "last_interaction": datetime.utcnow(),
                "reengage_sent": False,
                "updated_at": datetime.utcnow()
            }, "$setOnInsert": {
                "user_id": user_id,
                "joined_at": datetime.utcnow(),
                "blocked": False,
                "purchases_count": 0,
                "free_available": False
            }},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Track user error: {e}")


MUTE_DURATIONS = {
    "2m": 2 * 60,
    "5m": 5 * 60,
    "1h": 60 * 60,
    "24h": 24 * 60 * 60,
    "2d": 2 * 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
    "1w": 7 * 24 * 60 * 60,
    "1month": 30 * 24 * 60 * 60,
}
MUTE_LABELS = {
    "2m": "2 minutes", "5m": "5 minutes", "1h": "1 hour",
    "24h": "24 hours", "2d": "2 days", "7d": "1 week",
    "1w": "1 week", "30d": "1 month", "1month": "1 month",
}

def get_ban_fee_settings():
    enabled = True
    amount = 300
    try:
        if settings_collection is not None:
            e = settings_collection.find_one({"key": "ban_fee_enabled"})
            a = settings_collection.find_one({"key": "ban_fee_amount"})
            if e is not None:
                enabled = bool(e.get("value", True))
            if a is not None:
                try:
                    amount = int(a.get("value") or 300)
                except Exception:
                    amount = 300
    except Exception:
        pass
    return enabled, amount

def get_user_access_state(user_id: int) -> dict:
    """Return access state: free | muted | banned | blocked."""
    state = {"status": "free", "until": None, "label": "", "message": ""}
    if users_collection is None:
        return state
    try:
        u = users_collection.find_one({"user_id": int(user_id)}) or {}
    except Exception:
        return state
    now = datetime.utcnow()
    # Hard block = ignore completely
    if u.get("hard_blocked") or (u.get("blocked") and u.get("block_mode") == "hard"):
        state["status"] = "hard_blocked"
        state["message"] = "🚫 Got no access"
        return state
    # Temporary mute
    mute_until = u.get("mute_until")
    if mute_until:
        if isinstance(mute_until, str):
            try:
                mute_until = datetime.fromisoformat(mute_until.replace("Z", ""))
            except Exception:
                mute_until = None
        if mute_until and mute_until > now:
            left = mute_until - now
            secs = int(left.total_seconds())
            if secs >= 86400:
                left_str = f"{secs // 86400}d {(secs % 86400) // 3600}h"
            elif secs >= 3600:
                left_str = f"{secs // 3600}h {(secs % 3600) // 60}m"
            else:
                left_str = f"{max(1, secs // 60)}m"
            state["status"] = "muted"
            state["until"] = mute_until
            state["message"] = f"🔇 You are muted.\nDuration ends in: <b>{left_str}</b>"
            return state
        else:
            # auto unmute
            try:
                users_collection.update_one({"user_id": int(user_id)}, {"$unset": {"mute_until": "", "mute_label": ""}, "$set": {"muted": False}})
            except Exception:
                pass
    # Ban (can unlock with fee)
    if u.get("banned") or (u.get("blocked") and u.get("block_mode") != "hard"):
        state["status"] = "banned"
        enabled, fee = get_ban_fee_settings()
        if enabled and fee > 0:
            state["message"] = (
                f"🚫 You were banned from using this bot.\n\n"
                f"Pay <b>{fee} KES</b> to unlock your access.\n"
                f"After unlock, a repeat offense may result in a permanent block."
            )
            state["fee"] = fee
        else:
            state["message"] = "🚫 You were banned from using this bot. Contact admin."
        return state
    return state

def apply_mute(user_id: int, duration_key: str) -> dict:
    secs = MUTE_DURATIONS.get(duration_key, 3600)
    label = MUTE_LABELS.get(duration_key, duration_key)
    until = datetime.utcnow() + timedelta(seconds=secs)
    users_collection.update_one(
        {"user_id": int(user_id)},
        {"$set": {
            "muted": True, "mute_until": until, "mute_label": label,
            "blocked": False, "banned": False, "hard_blocked": False, "block_mode": None,
            "updated_at": datetime.utcnow(),
        }},
        upsert=True,
    )
    return {"until": until, "label": label}

def apply_ban(user_id: int) -> None:
    users_collection.update_one(
        {"user_id": int(user_id)},
        {"$set": {
            "banned": True, "blocked": True, "block_mode": "ban",
            "banned_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
        }, "$unset": {"mute_until": "", "mute_label": "", "muted": ""}},
        upsert=True,
    )

def apply_hard_block(user_id: int) -> None:
    users_collection.update_one(
        {"user_id": int(user_id)},
        {"$set": {
            "hard_blocked": True, "blocked": True, "block_mode": "hard",
            "banned": True, "blocked_at": datetime.utcnow(), "updated_at": datetime.utcnow(),
        }},
        upsert=True,
    )

def apply_unban(user_id: int) -> None:
    users_collection.update_one(
        {"user_id": int(user_id)},
        {"$set": {
            "banned": False, "blocked": False, "hard_blocked": False, "muted": False,
            "block_mode": None, "updated_at": datetime.utcnow(),
        }, "$unset": {"mute_until": "", "mute_label": "", "banned_at": "", "blocked_at": ""}},
    )

async def notify_user_access_change(user_id: int, kind: str, extra: str = ""):
    """kind: unban | mute | ban"""
    try:
        u = users_collection.find_one({"user_id": int(user_id)}) if users_collection is not None else None
        name = ""
        if u:
            name = u.get("first_name") or u.get("username") or str(user_id)
            if u.get("username"):
                name = f"@{u.get('username')}"
        if kind == "unban":
            text = add_footer(
                f"✅ <b>You were unbanned</b>\n\n"
                f"Welcome back — we missed you {name}!\n"
                f"Press /start to continue."
            )
        elif kind == "mute":
            text = add_footer(f"🔇 You have been muted for <b>{extra}</b>.\nYou cannot use the bot until the mute ends.")
        elif kind == "ban":
            enabled, fee = get_ban_fee_settings()
            fee_line = f"\nPay <b>{fee} KES</b> to unlock." if enabled else "\nContact admin to unlock."
            text = add_footer(f"🚫 You were banned from using this bot.{fee_line}")
        else:
            return
        await application.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.debug(f"notify access change fail: {e}")

async def enforce_user_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if user is blocked from using the bot (caller should return)."""
    user = update.effective_user
    if not user:
        return False
    if user.id in ADMIN_IDS:
        return False
    state = get_user_access_state(user.id)
    status = state["status"]
    if status == "free":
        return False
    query = update.callback_query
    msg = state.get("message") or "🚫 Access denied"
    if status == "hard_blocked":
        # Count attempts; stay silent after a few
        try:
            u = users_collection.find_one({"user_id": user.id}) or {}
            attempts = int(u.get("block_attempts") or 0) + 1
            users_collection.update_one({"user_id": user.id}, {"$set": {"block_attempts": attempts}})
            if attempts <= 2:
                if query:
                    try:
                        await query.answer("🚫 Got no access", show_alert=True)
                    except Exception:
                        pass
                elif update.effective_message:
                    await update.effective_message.reply_text("🚫 Got no access")
            # else: ignore completely
        except Exception:
            pass
        return True
    if status == "muted":
        if query:
            try:
                await query.answer(state["message"].replace("<b>", "").replace("</b>", "")[:180], show_alert=True)
            except Exception:
                pass
            try:
                await query.message.reply_text(add_footer(msg), parse_mode=ParseMode.HTML)
            except Exception:
                pass
        elif update.effective_message:
            await update.effective_message.reply_text(add_footer(msg), parse_mode=ParseMode.HTML)
        return True
    if status == "banned":
        # Allow unlock_ban_* callbacks to pass through so payment can start
        if query and (query.data or "").startswith("unlock_ban_"):
            return False
        enabled, fee = get_ban_fee_settings()
        kb = None
        if enabled and fee > 0:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Unlock ban — {fee} KES", callback_data=f"unlock_ban_{fee}")]])
        if query:
            try:
                await query.answer("You are banned", show_alert=True)
            except Exception:
                pass
            try:
                await query.message.reply_text(add_footer(msg), parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass
        elif update.effective_message:
            await update.effective_message.reply_text(add_footer(msg), parse_mode=ParseMode.HTML, reply_markup=kb)
        return True
    return False

def get_all_users(skip_blocked: bool = True):
    if not mongodb_connected or users_collection is None:
        return []
    query = {"blocked": {"$ne": True}} if skip_blocked else {}
    return list(users_collection.find(query, {"user_id": 1, "username": 1, "first_name": 1}))

def get_user_count():
    cached = _cache_get("user_count", ttl=45)
    if cached is not None:
        return cached
    if not mongodb_connected or users_collection is None:
        return 0
    try:
        n = users_collection.count_documents({"blocked": {"$ne": True}})
    except Exception:
        n = 0
    return _cache_set("user_count", n)


def get_formatted_datetime():
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Africa/Nairobi"))
    return now.strftime("%A, %d %B %Y") + "\n" + now.strftime("%I:%M %p EAT")

def get_datetime_footer():
    # Wrap here so callers that append this AFTER add_footer still get animated 📅
    return apply_premium_emojis(f"\n\n📅 {get_formatted_datetime()}")

def _plain_footer_line(s: str) -> str:
    """Normalize a line for footer comparison (no premium tags, collapsed spaces)."""
    s = strip_premium_emojis(s or "")
    s = re.sub(r"\s+", " ", s).strip()
    # drop variation selectors / zero-width
    s = s.replace("\ufe0f", "").replace("\u200b", "")
    return s


def _line_looks_like_brand_footer(line: str) -> bool:
    """True if this line is our brand footer (FOOTER_TEXT / Powered by … / │ … © YEAR)."""
    plain = _plain_footer_line(line)
    if not plain:
        return False
    low = plain.lower()
    year = str(YEAR_COPY or "")
    raw_foot = _plain_footer_line(powered_footer() or "")
    if raw_foot and (plain == raw_foot or plain.endswith(raw_foot) or raw_foot in plain):
        return True
    if "powered by" in low:
        return True
    # │ … © 2026  /  ★ ⩽ … ⩾ ★  style brand lines
    if year and year in plain:
        if plain.startswith("│") or plain.startswith("|"):
            return True
        if "©" in plain or "©️" in plain or "(c)" in low:
            # brand line with year + copyright, not a normal content line
            if any(x in low for x in ("confronter", "davie", "movie", "powered", "⩽", "⩾")):
                return True
    if re.search(r"[│|]\s*.*⩽.*⩾", plain):
        return True
    return False


def _strip_existing_footers(text: str) -> str:
    """Remove every trailing brand-footer line so we never stack two copies."""
    if not text:
        return text or ""
    lines = (text or "").split("\n")
    # Drop trailing empty + all consecutive brand footer lines from the end
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        if _line_looks_like_brand_footer(last):
            lines.pop()
            continue
        break
    # Also collapse any accidental duplicate footer lines still present near the end
    # (e.g. FOOTER_TEXT in .env itself contained the line twice)
    cleaned = []
    for ln in lines:
        if cleaned and _line_looks_like_brand_footer(ln) and _line_looks_like_brand_footer(cleaned[-1]):
            if _plain_footer_line(ln) == _plain_footer_line(cleaned[-1]):
                continue  # skip exact duplicate footer
        cleaned.append(ln)
    return "\n".join(cleaned).rstrip()


def _dedupe_footer_string(foot: str) -> str:
    """If FOOTER_TEXT env has the same line twice, keep one."""
    if not foot:
        return ""
    lines = [ln for ln in foot.split("\n")]
    out = []
    for ln in lines:
        if out and _plain_footer_line(ln) and _plain_footer_line(ln) == _plain_footer_line(out[-1]):
            continue
        out.append(ln)
    # If two non-empty brand lines are identical after strip
    non_empty = [ln for ln in out if ln.strip()]
    if len(non_empty) >= 2 and all(_line_looks_like_brand_footer(ln) for ln in non_empty):
        if len({_plain_footer_line(ln) for ln in non_empty}) == 1:
            return non_empty[0].strip()
    return "\n".join(out).strip()


def add_footer(text):
    """Append brand footer exactly once. Safe to call multiple times — never doubles.
    Premium ON  → may contain <tg-emoji> (caller MUST send with parse_mode=HTML).
    Premium OFF → plain unicode only (never leaks HTML tags).
    """
    text = _strip_existing_footers(text or "")
    # Footer from env may itself contain leftover tg-emoji — clean first
    foot = _dedupe_footer_string(strip_premium_emojis(powered_footer() or ""))
    if foot:
        text = (text.rstrip() + "\n\n" + foot) if text.strip() else foot
    # Collapse accidental double footers
    text = _strip_existing_footers(text)
    foot = _dedupe_footer_string(strip_premium_emojis(powered_footer() or ""))
    if foot and not _line_looks_like_brand_footer((text or "").strip().split("\n")[-1] if text.strip() else ""):
        text = (text.rstrip() + "\n\n" + foot) if text.strip() else foot
    # Apply premium wrap ONCE at the end (or strip if not premium)
    try:
        text = apply_premium_emojis(text or "")
    except Exception:
        text = strip_premium_emojis(text or "")
    if not admin_premium_active():
        text = strip_premium_emojis(text or "")
    return text

def push_nav_state(context: ContextTypes.DEFAULT_TYPE, state_name: str, text: str, keyboard=None, parse_mode=ParseMode.HTML):
    if "nav_history" not in context.user_data:
        context.user_data["nav_history"] = []
    kb_dict = None
    if keyboard:
        try:
            kb_dict = keyboard.to_dict()
        except Exception:
            pass
    context.user_data["nav_history"].append({
        "state": state_name,
        "text": text,
        "keyboard": kb_dict,
        "parse_mode": parse_mode
    })
    context.user_data["nav_history"] = context.user_data["nav_history"][-10:]

def get_previous_nav_state(context: ContextTypes.DEFAULT_TYPE):
    history = context.user_data.get("nav_history", [])
    if len(history) >= 2:
        history.pop()
        prev = history[-1] if history else None
        context.user_data["nav_history"] = history
        return prev
    return None

def clear_nav_history(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("nav_history", None)

def rebuild_keyboard(kb_dict):
    if not kb_dict or not kb_dict.get('inline_keyboard'):
        return None
    rows = []
    for row in kb_dict['inline_keyboard']:
        buttons = []
        for btn in row:
            if btn.get('url'):
                buttons.append(InlineKeyboardButton(btn['text'], url=btn['url']))
            elif btn.get('callback_data'):
                buttons.append(InlineKeyboardButton(btn['text'], callback_data=btn['callback_data']))
            else:
                buttons.append(InlineKeyboardButton(btn['text'], callback_data='noop'))
        rows.append(buttons)
    return InlineKeyboardMarkup(rows) if rows else None

def _ebtn(text: str, emoji: str = "", callback_data: str = None, url: str = None, max_len: int = 22):
    label = f"{text} {emoji}" if emoji and len(text) <= max_len else text
    if url:
        return InlineKeyboardButton(label, url=url)
    return InlineKeyboardButton(label, callback_data=callback_data)

async def edit_or_send_message(query, text, reply_markup=None, parse_mode=ParseMode.HTML):
    text = add_footer(text)
    message = getattr(query, "message", None)
    if not message:
        return
    async def _try_send(body):
        if getattr(message, "photo", None) or getattr(message, "video", None) or getattr(message, "document", None) or getattr(message, "animation", None):
            if getattr(message, "caption", None) is not None:
                await query.edit_message_caption(caption=body, reply_markup=reply_markup, parse_mode=parse_mode)
            else:
                await message.reply_text(text=body, reply_markup=reply_markup, parse_mode=parse_mode)
        elif getattr(message, "text", None) is not None:
            await query.edit_message_text(text=body, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await message.reply_text(text=body, reply_markup=reply_markup, parse_mode=parse_mode)
    try:
        await _try_send(text)
    except Exception as e:
        err = str(e).lower()
        logger.warning("[v0] Message edit failed: %s", e)
        body = strip_premium_emojis(text) if ('document_invalid' in err or 'entity' in err or 'parse' in err or 'emoji' in err) else text
        try:
            await _try_send(body)
        except Exception:
            try:
                await message.reply_text(text=strip_premium_emojis(text), reply_markup=reply_markup)
            except Exception as e2:
                logger.error("[v0] Fallback message send also failed: %s", e2)

def parse_buttons_from_text(text: str) -> list:
    buttons = []
    if not text or not text.strip():
        return buttons
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        match = _BUTTON_STYLE_RE.match(line)
        if match:
            btn_text = match.group(1).strip()
            url = match.group(2).strip()
            style = match.group(3) if match.group(3) else None
            buttons.append({"text": btn_text, "url": url})
        elif line.startswith('http'):
            buttons.append({"text": line.split('/')[-1] or "Link", "url": line.strip()})
    return buttons

def build_button_rows(buttons: list, per_row: int = 3) -> InlineKeyboardMarkup:
    keyboard_rows = []
    current_row = []
    for btn in buttons:
        if btn.get('url'):
            current_row.append(InlineKeyboardButton(btn['text'], url=btn['url']))
        elif btn.get('callback_data'):
            current_row.append(InlineKeyboardButton(btn['text'], callback_data=btn['callback_data']))
        if len(current_row) == per_row:
            keyboard_rows.append(current_row)
            current_row = []
    if current_row:
        keyboard_rows.append(current_row)
    return InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

def build_back_keyboard(back_data: str = "back", extra_buttons: list = None) -> InlineKeyboardMarkup:
    keyboard = []
    if extra_buttons:
        keyboard.extend(extra_buttons)
    keyboard.append([InlineKeyboardButton(f"⬅️ Back", callback_data=back_data)])
    return InlineKeyboardMarkup(keyboard)

@lru_cache(maxsize=128)
def _search_regex(query: str):
    terms = [t for t in re.split(r"\s+", (query or "").strip()) if t]
    if not terms:
        return re.compile(r"$^")
    if len(terms) == 1:
        return re.compile(re.escape(terms[0]), re.IGNORECASE)
    pattern = "".join(r"(?=.*" + re.escape(t) + r")" for t in terms)
    return re.compile(pattern, re.IGNORECASE)

def search_movies_db(query: str, limit: int = 10, skip: int = 0):
    if not mongodb_connected or movies_collection is None:
        return [], 0
    try:
        regex = _search_regex(query)
        # Match title OR category OR description (genre buttons use genre name)
        movie_q = {"$or": [
            {"title": regex},
            {"category": regex},
            {"description": regex},
            {"button_title": regex},
        ]}
        bundle_q = {"$or": [
            {"title": regex},
            {"category": regex},
            {"description": regex},
        ]}
        movie_total = movies_collection.count_documents(movie_q)
        bundle_total = series_bundles_collection.count_documents(bundle_q) if series_bundles_collection is not None else 0
        total = movie_total + bundle_total
        movies = list(movies_collection.find(movie_q).skip(skip).limit(limit))
        remaining = limit - len(movies)
        if remaining > 0 and series_bundles_collection is not None:
            bundles = list(series_bundles_collection.find(bundle_q).skip(max(0, skip - movie_total)).limit(remaining))
            for b in bundles:
                b['is_bundle'] = True
            movies.extend(bundles)
        return movies, total
    except Exception as e:
        logger.error(f"Search error: {e}")
        return [], 0

# ============ MAKAMESCO / NEXUS PAY API ============
def get_makamesco_headers():
    return {
        "X-API-Key": MAKAMESCO_SECRET_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def get_makamesco_enabled() -> bool:
    if not mongodb_connected or settings_collection is None:
        return MAKAMESCO_ENABLED_BY_DEFAULT
    try:
        setting = settings_collection.find_one({"key": "makamesco_enabled"})
        return setting.get('value', MAKAMESCO_ENABLED_BY_DEFAULT) if setting else MAKAMESCO_ENABLED_BY_DEFAULT
    except Exception:
        return MAKAMESCO_ENABLED_BY_DEFAULT

def set_makamesco_enabled(enabled: bool):
    if not mongodb_connected or settings_collection is None:
        return
    settings_collection.update_one(
        {"key": "makamesco_enabled"},
        {"$set": {"value": enabled, "updated_at": datetime.utcnow()}},
        upsert=True
    )


def _makamesco_safe_remarks(text: str, limit: int = 12) -> str:
    """Makamesco rejects long/special remarks — keep very short alphanumeric."""
    s = re.sub(r'[^A-Za-z0-9]+', '', str(text or 'Payment'))
    s = (s or 'Payment')[:limit]
    return s or 'Payment'

def initiate_makamesco_stk_push(phone: str, amount: int, external_reference: str, callback_url: str = None, customer_name: str = ""):
    """Initiate STK Push via Makamesco / Nexus Pay.
    Tries several common payload shapes so prompt always fires when credentials are valid.
    """
    if not MAKAMESCO_SECRET_KEY:
        return None, "Makamesco API key not configured. Set MAKAMESCO_SECRET_KEY."
    formatted_phone = format_phone_number(phone)
    if not formatted_phone:
        return None, "Invalid phone number. Use: 07XX XXX XXX or 2547XX XXX XXX"
    display_phone = "0" + formatted_phone[3:]
    cb_url = callback_url or MAKAMESCO_CALLBACK_URL or (f"{WEBHOOK_URL.rstrip('/')}/makamesco-callback" if WEBHOOK_URL else "")
    headers_variants = [
        {"X-API-Key": MAKAMESCO_SECRET_KEY, "Content-Type": "application/json", "Accept": "application/json"},
        {"Authorization": f"Bearer {MAKAMESCO_SECRET_KEY}", "Content-Type": "application/json", "Accept": "application/json"},
        {"x-api-key": MAKAMESCO_SECRET_KEY, "Content-Type": "application/json", "Accept": "application/json"},
    ]
    payloads = [
        {
            "phone": formatted_phone,
            "amount": int(amount),
            "reference": external_reference[:50],
            "description": _makamesco_safe_remarks(customer_name or "Payment"),
            "callback_url": cb_url,
        },
        {
            "phoneNumber": formatted_phone,
            "amount": int(amount),
            "accountReference": external_reference[:50],
            "transactionDesc": _makamesco_safe_remarks(customer_name or "Payment"),
            "callbackUrl": cb_url,
        },
        {
            "phone": formatted_phone,
            "amount": str(int(amount)),
            "external_reference": external_reference[:50],
            "account_reference": external_reference[:50],
            "description": _makamesco_safe_remarks(customer_name or "Payment"),
        },
    ]
    endpoints = [
        f"{MAKAMESCO_BASE_URL.rstrip('/')}/api/payments/stkpush",
        f"{MAKAMESCO_BASE_URL.rstrip('/')}/api/v1/payments/stkpush",
        f"{MAKAMESCO_BASE_URL.rstrip('/')}/api/stkpush",
        f"{MAKAMESCO_BASE_URL.rstrip('/')}/api/mpesa/stkpush",
    ]
    last_error = "STK request failed"
    for endpoint in endpoints:
        for headers in headers_variants:
            for payload in payloads:
                try:
                    res = requests.post(endpoint, json=payload, headers=headers, timeout=25)
                    data = {}
                    try:
                        data = res.json() if res.content else {}
                    except Exception:
                        data = {"raw": (res.text or "")[:300]}
                    logger.info(f"Makamesco STK [{res.status_code}] {endpoint}: {data}")
                    ok = res.status_code in (200, 201, 202)
                    body_ok = (
                        data.get("success") is True
                        or str(data.get("status", "")).lower() in ("success", "pending", "queued", "ok", "processing")
                        or data.get("checkout_request_id")
                        or data.get("CheckoutRequestID")
                        or data.get("checkoutRequestId")
                        or data.get("MerchantRequestID")
                        or data.get("id")
                        or (isinstance(data.get("data"), dict) and (
                            data["data"].get("checkout_request_id")
                            or data["data"].get("CheckoutRequestID")
                            or data["data"].get("id")
                        ))
                    )
                    logger.info(f"Makamesco STK [{res.status_code}] {endpoint} body={str(data)[:400]}")
                    if ok and (body_ok or res.status_code in (200, 201)):
                        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
                        checkout_id = (
                            data.get("checkout_request_id") or data.get("CheckoutRequestID")
                            or data.get("checkoutRequestId") or nested.get("checkout_request_id")
                            or nested.get("CheckoutRequestID") or nested.get("id") or data.get("id") or ""
                        )
                        logger.info(f"Makamesco STK SUCCESS checkout={checkout_id} phone={formatted_phone} amount={amount}")
                        return {
                            "reference": external_reference,
                            "checkout_request_id": str(checkout_id),
                            "status": data.get("status", "pending"),
                            "phone": formatted_phone,
                            "display_phone": display_phone,
                            "amount": amount,
                            "provider": "makamesco",
                            "raw": data,
                        }, None
                    last_error = (
                        data.get("message") or data.get("error") or data.get("detail")
                        or (data.get("data") if isinstance(data.get("data"), str) else None)
                        or f"STK failed ({res.status_code})"
                    )
                    logger.warning(f"Makamesco STK attempt failed: {last_error}")
                except requests.exceptions.Timeout:
                    last_error = "Payment request timed out. Please try again."
                    logger.error("Makamesco STK timed out")
                except Exception as e:
                    last_error = str(e)
                    logger.error(f"Makamesco STK error: {e}")
    return None, last_error

# ============ PAYHERO API ============
def get_payhero_auth_headers():
    credentials = f"{PAYHERO_API_USERNAME}:{PAYHERO_API_PASSWORD}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return {"Authorization": f"Basic {encoded}", "Content-Type": "application/json"}

def initiate_payhero_stk_push(phone: str, amount: int, external_reference: str, callback_url: str = None, customer_name: str = "", channel_id: int = None):
    if not PAYHERO_API_USERNAME or not PAYHERO_API_PASSWORD:
        return None, "PayHero API credentials not configured."
    formatted_phone = format_phone_number(phone)
    if not formatted_phone:
        return None, "Invalid phone number. Use: 07XX XXX XXX or 2547XX XXX XXX"
    display_phone = "0" + formatted_phone[3:]
    ch_id = channel_id or get_payhero_channel_id()
    cb_url = callback_url or f"{WEBHOOK_URL}/api/payhero/callback"
    headers = get_payhero_auth_headers()
    payload = {"amount": amount, "phone_number": display_phone, "channel_id": ch_id, "provider": "m-pesa", "external_reference": external_reference[:50], "customer_name": customer_name or "Movie Customer", "callback_url": cb_url}
    try:
        res = requests.post(f"{PAYHERO_BASE_URL}/payments", json=payload, headers=headers, timeout=30)
        data = res.json()
        logger.info(f"PayHero STK response: {data}")
        if data.get("success") or data.get("status") in ["QUEUED", "PENDING", "SUCCESS"]:
            return {"reference": external_reference, "checkout_request_id": data.get("CheckoutRequestID", ""), "status": data.get("status", "QUEUED"), "phone": formatted_phone, "display_phone": display_phone, "channel_id": ch_id, "amount": amount}, None
        else:
            error_msg = data.get("message", "Payment initiation failed")
            return None, error_msg
    except requests.exceptions.Timeout:
        logger.error("PayHero STK request timed out")
        return None, "Payment request timed out. Please try again."
    except requests.exceptions.RequestException as e:
        logger.error(f"PayHero STK network error: {e}")
        return None, f"Network error: {str(e)}"
    except Exception as e:
        logger.error(f"PayHero STK error: {e}")
        return None, str(e)

def check_payhero_transaction_status(checkout_request_id: str):
    if not PAYHERO_API_USERNAME or not PAYHERO_API_PASSWORD:
        return None
    headers = get_payhero_auth_headers()
    try:
        res = requests.get(f"{PAYHERO_BASE_URL}/transactions/{checkout_request_id}", headers=headers, timeout=30)
        if res.status_code == 200:
            return res.json()
        logger.warning(f"PayHero status check failed: {res.status_code} - {res.text}")
        return None
    except Exception as e:
        logger.error(f"PayHero status check error: {e}")
        return None

def get_payhero_transaction_status_api(reference: str):
    return check_payhero_transaction_status(reference)

# ============ SUBSCRIPTION HELPERS ============
def has_active_subscription(user_id: int) -> tuple:
    now = datetime.utcnow()
    if user_id in FREE_PREMIUM_USERS:
        return True, "free_premium", None, 0, 0
    if not mongodb_connected:
        return False, None, None, 0, 0
    if subscriptions_collection is not None:
        sub = subscriptions_collection.find_one({"user_id": user_id, "status": "active", "expires_at": {"$gt": now}}, sort=[("expires_at", DESCENDING)])
        if sub:
            plan_key = sub.get("plan", "monthly")
            plan_info = get_subscription_plans().get(plan_key, DEFAULT_SUBSCRIPTION_PLANS.get(plan_key, {}))
            used = int(sub.get("used_movies", 0))
            max_movies = int(sub.get("max_movies", plan_info.get("max_movies", 0)))
            if max_movies == 0 or used < max_movies:
                return True, plan_key, sub.get("expires_at"), used, max_movies
            subscriptions_collection.update_one({"_id": sub["_id"]}, {"$set": {"status": "expired_limit", "updated_at": now}})
    if users_collection is not None:
        user = users_collection.find_one({"user_id": user_id}) or {}
        prem = user.get("premium", {})
        expiry = prem.get("expiry")
        if prem.get("active") and expiry and now < expiry:
            used = int(prem.get("used_movies", 0))
            max_movies = int(prem.get("max_movies", 0))
            if max_movies == 0 or used < max_movies:
                return True, prem.get("plan", "premium"), expiry, used, max_movies
            users_collection.update_one({"user_id": user_id}, {"$set": {"premium.active": False}})
        elif prem.get("active") and expiry and now >= expiry:
            users_collection.update_one({"user_id": user_id}, {"$set": {"premium.active": False}})
    return False, None, None, 0, 0

def increment_used_movies(user_id: int, count: int = 1):
    sub = subscriptions_collection.find_one({"user_id": user_id, "status": "active", "expires_at": {"$gt": datetime.utcnow()}})
    if sub:
        subscriptions_collection.update_one({"_id": sub["_id"]}, {"$inc": {"used_movies": count}})
        plans = get_subscription_plans()
        plan_key = sub.get("plan")
        max_movies = plans.get(plan_key, {}).get("max_movies", 0)
        if max_movies > 0:
            new_used = sub.get("used_movies", 0) + count
            if new_used >= max_movies:
                subscriptions_collection.update_one({"_id": sub["_id"]}, {"$set": {"status": "expired_limit"}})
                asyncio.create_task(notify_limit_reached(user_id, plan_key))
        return

    user = users_collection.find_one({"user_id": user_id})
    if user and user.get("premium", {}).get("active", False):
        prem = user["premium"]
        max_movies = prem.get("max_movies", 0)
        if max_movies > 0:
            new_used = prem.get("used_movies", 0) + count
            users_collection.update_one({"user_id": user_id}, {"$set": {"premium.used_movies": new_used}})
            if new_used >= max_movies:
                users_collection.update_one({"user_id": user_id}, {"$set": {"premium.active": False}})
                asyncio.create_task(notify_limit_reached(user_id, "premium"))
        else:
            users_collection.update_one({"user_id": user_id}, {"$inc": {"premium.used_movies": count}})

async def notify_limit_reached(user_id: int, plan: str):
    try:
        await application.bot.send_message(
            chat_id=user_id,
            text=add_footer(f"⚠️ <b>Movie Limit Reached</b>\n\nYour <b>{plan.title()}</b> subscription has used all available movies.\n\nRenew now to keep watching!"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👑 Subscribe Now", callback_data="show_plans")],
                [InlineKeyboardButton("🎬 More Movies", callback_data="more_movies")],
                [InlineKeyboardButton("◀️ Back", callback_data="back")]
            ]),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to notify limit reached: {e}")

def create_subscription(user_id: int, plan: str, payment_ref: str):
    if not mongodb_connected or subscriptions_collection is None:
        return False
    plan_info = get_subscription_plans().get(plan)
    if not plan_info:
        return False
    expires_at = datetime.utcnow() + timedelta(days=plan_info["days"])
    max_movies = plan_info.get("max_movies", 0)
    existing = subscriptions_collection.find_one({"user_id": user_id, "status": "active"})
    if existing:
        new_expiry = existing["expires_at"] + timedelta(days=plan_info["days"])
        subscriptions_collection.update_one({"_id": existing["_id"]}, {"$set": {"plan": plan, "expires_at": new_expiry, "last_payment_ref": payment_ref, "updated_at": datetime.utcnow(), "used_movies": existing.get("used_movies", 0), "max_movies": max_movies}})
    else:
        subscriptions_collection.insert_one({"user_id": user_id, "plan": plan, "status": "active", "created_at": datetime.utcnow(), "expires_at": expires_at, "last_payment_ref": payment_ref, "used_movies": 0, "max_movies": max_movies})
    return True

# ============ AUTO-DELIVERY ============
async def auto_deliver_movie(user_id: int, movie_token: str, context: ContextTypes.DEFAULT_TYPE = None):
    clear_pending_payment_tracker(user_id)
    logger.info(f"🚀 AUTO-DELIVER: user={user_id}, token={movie_token}")
    if not mongodb_connected or movies_collection is None:
        try:
            await application.bot.send_message(chat_id=user_id, text=add_footer(f"⚠️ Bot is in maintenance mode. Please try again later."))
        except Exception:
            pass
        return False

    movie = movies_collection.find_one({"token": movie_token})
    if not movie:
        logger.error(f"Movie not found for token: {movie_token}")
        try:
            await application.bot.send_message(chat_id=user_id, text=add_footer(f"❌ Payment confirmed but movie not found. Contact admin for help."))
        except Exception:
            pass
        return False

    has, plan, expiry, used, max_movies = has_active_subscription(user_id)
    if has and plan not in ["free_premium"]:
        increment_used_movies(user_id, 1)

    try:
        file_id = movie.get('file_id')
        file_type = movie.get('file_type', 'video')
        caption = movie['title']
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🎬 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")],
            [InlineKeyboardButton(f"🎦 ʙʀᴏᴡꜱᴇ ᴀʟʟ ᴍᴏᴠɪᴇꜱ", callback_data="watch_all_movies_0")],
            [InlineKeyboardButton(f"🔍 ꜱᴇᴀʀᴄʜ ᴍᴏᴠɪᴇꜱ", callback_data="search_movies")],
            [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]
        ])
        if file_id:
            if file_type == 'video':
                await application.bot.send_video(chat_id=user_id, video=file_id, caption=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                await application.bot.send_document(chat_id=user_id, document=file_id, caption=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        else:
            await application.bot.forward_message(chat_id=user_id, from_chat_id=movie['channel_id'], message_id=movie['message_id'])
            await application.bot.send_message(chat_id=user_id, text=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)

        movies_collection.update_one({"token": movie_token}, {"$inc": {"downloads": 1}})
        # Persist so /mymovies always shows purchased/started movies (no disappearing)
        try:
            record_movie_access(user_id, movie_token, movie.get("title", ""))
        except Exception:
            pass
        logger.info(f"✅ AUTO-DELIVER: SUCCESS user={user_id}, movie={movie['title']}")
        return True
    except Exception as e:
        logger.error(f"❌ AUTO-DELIVER: FAILED - {e}")
        # One fast retry (file_id then forward)
        try:
            await asyncio.sleep(BOT_FAST_DELAY)
            file_id = movie.get('file_id')
            file_type = movie.get('file_type', 'video')
            caption = movie.get('title') or 'Movie'
            if file_id:
                if file_type == 'video':
                    await application.bot.send_video(chat_id=user_id, video=file_id, caption=caption)
                else:
                    await application.bot.send_document(chat_id=user_id, document=file_id, caption=caption)
            elif movie.get('channel_id') and movie.get('message_id'):
                await application.bot.forward_message(chat_id=user_id, from_chat_id=movie['channel_id'], message_id=movie['message_id'])
            record_movie_access(user_id, movie_token, movie.get("title", ""))
            logger.info(f"✅ AUTO-DELIVER RETRY OK user={user_id}")
            return True
        except Exception as e2:
            logger.error(f"❌ AUTO-DELIVER RETRY FAILED - {e2}")
        try:
            await application.bot.send_message(chat_id=user_id, text=add_footer(f"✅ Payment received!\n\n⚠️ Delivery delayed. Tap the movie again or /mymovies — already paid, no extra charge."), reply_markup=build_back_keyboard("back"))
        except Exception:
            pass
        return False

async def auto_deliver_series(user_id: int, series_token: str, context: ContextTypes.DEFAULT_TYPE = None):
    """Deliver full collection after successful payment. Never raises to caller — always notifies user."""
    clear_pending_payment_tracker(user_id)
    logger.info(f"🚀 AUTO-DELIVER BUNDLE: user={user_id}, token={series_token}")
    bot = application.bot if application else None
    if not bot:
        logger.error("auto_deliver_series: no bot instance")
        return False
    if not mongodb_connected or series_bundles_collection is None:
        try:
            await bot.send_message(chat_id=user_id, text=add_footer("⚠️ Bot is in maintenance mode. Please try again later."))
        except Exception:
            pass
        return False

    series = series_bundles_collection.find_one({"token": series_token})
    if not series:
        # Recover: maybe token is a movie that is part of a collection
        try:
            m = movies_collection.find_one({"token": series_token}) if movies_collection is not None else None
            if m and m.get("bundle_parent"):
                series = series_bundles_collection.find_one({"token": m.get("bundle_parent")})
                if series:
                    series_token = series.get("token") or series_token
        except Exception:
            pass
    if not series:
        try:
            await bot.send_message(chat_id=user_id, text=add_footer("❌ Payment confirmed but bundle not found. Contact admin with this token: " + str(series_token)))
        except Exception:
            pass
        return False

    episodes = list(series.get("episodes") or [])
    # Fallback: load parts from movies collection by bundle_parent / bundle_token
    if (not episodes) and movies_collection is not None:
        try:
            parts = list(movies_collection.find({
                "$or": [
                    {"bundle_parent": series_token},
                    {"bundle_token": series_token},
                ]
            }).sort("collection_index", ASCENDING))
            for m in parts:
                episodes.append({
                    "title": m.get("title") or "Part",
                    "file_id": m.get("file_id"),
                    "file_type": m.get("file_type", "video"),
                    "token": m.get("token"),
                    "channel_id": m.get("channel_id") or "",
                    "message_id": m.get("message_id") or 0,
                })
            logger.info(f"Bundle episodes recovered from movies DB: {len(episodes)}")
        except Exception as e:
            logger.error(f"episode recovery: {e}")

    if not episodes:
        try:
            await bot.send_message(chat_id=user_id, text=add_footer("❌ This bundle has no movies stored. Contact admin."))
        except Exception:
            pass
        return False

    total = len(episodes)
    logger.info(f"🚀 AUTO-DELIVER COLLECTION: '{series.get('title', series_token)}' with {total} movies")
    progress_msg = None
    try:
        progress_msg = await bot.send_message(
            chat_id=user_id,
            text=add_footer(f"📦 <b>Delivering {escape_html(str(series.get('title', 'Collection')))}</b>\n\n⏳ Sending {total} movies... (0/{total})\n<i>Please wait...</i>"),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        try:
            progress_msg = await bot.send_message(
                chat_id=user_id,
                text=f"📦 Delivering {series.get('title', 'Collection')}\n⏳ Sending {total} movies...",
            )
        except Exception:
            pass

    sent_count = 0
    failed_episodes = []
    final_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 More Movies", callback_data="more_movies")],
        [InlineKeyboardButton("🎦 Browse All Movies", callback_data="watch_all_movies_0")],
        [InlineKeyboardButton("🔍 Search Movies", callback_data="search_movies")],
        [InlineKeyboardButton("◀ Back", callback_data="back")],
    ])

    for i, ep in enumerate(episodes, 1):
        try:
            try:
                increment_used_movies(user_id, 1)
            except Exception:
                pass

            file_id = ep.get("file_id")
            file_type = (ep.get("file_type") or "video") or "video"
            caption = str(ep.get("title") or f"Part {i}")
            ep_tok = ep.get("token") or ep.get("movie_token")

            # Resolve missing file_id from movies DB
            if (not file_id) and movies_collection is not None:
                mdoc = None
                try:
                    if ep_tok:
                        mdoc = movies_collection.find_one({"token": ep_tok})
                    if not mdoc and caption:
                        mdoc = movies_collection.find_one({
                            "$or": [
                                {"bundle_parent": series_token, "title": caption},
                                {"bundle_token": series_token, "title": caption},
                                {"bundle_parent": series_token, "collection_index": i},
                            ]
                        })
                    if mdoc:
                        file_id = mdoc.get("file_id") or file_id
                        file_type = mdoc.get("file_type") or file_type
                        ep_tok = ep_tok or mdoc.get("token")
                        if not ep.get("channel_id") and mdoc.get("channel_id"):
                            ep = dict(ep)
                            ep["channel_id"] = mdoc.get("channel_id")
                            ep["message_id"] = mdoc.get("message_id")
                except Exception as _re:
                    logger.debug(f"episode resolve: {_re}")

            delivered = False
            if file_id:
                try:
                    if str(file_type).lower() in ("video", "animation", "mpeg4"):
                        await bot.send_video(chat_id=user_id, video=file_id, caption=caption[:1024])
                    else:
                        await bot.send_document(chat_id=user_id, document=file_id, caption=caption[:1024])
                    delivered = True
                except Exception as se:
                    logger.warning(f"send file_id failed ep {i}: {se}")
                    # try document fallback
                    try:
                        await bot.send_document(chat_id=user_id, document=file_id, caption=caption[:1024])
                        delivered = True
                    except Exception as se2:
                        logger.warning(f"send document fallback failed ep {i}: {se2}")

            if (not delivered) and ep.get("channel_id") and ep.get("message_id"):
                try:
                    await bot.forward_message(chat_id=user_id, from_chat_id=ep["channel_id"], message_id=int(ep["message_id"]))
                    delivered = True
                except Exception as fe:
                    logger.warning(f"forward failed ep {i}: {fe}")

            if not delivered:
                logger.error(f"❌ BUNDLE: Movie {i}/{total} no file_id/channel for '{caption}' token={ep_tok}")
                failed_episodes.append(i)
            else:
                sent_count += 1
                try:
                    if ep_tok:
                        record_movie_access(user_id, ep_tok, caption)
                        if payments_collection is not None:
                            payments_collection.update_one(
                                {"user_id": user_id, "movie_token": ep_tok, "status": "success", "from_bundle": True},
                                {"$setOnInsert": {
                                    "user_id": user_id,
                                    "movie_token": ep_tok,
                                    "series_token": series_token,
                                    "status": "success",
                                    "from_bundle": True,
                                    "amount": 0,
                                    "paid_at": datetime.utcnow(),
                                    "created_at": datetime.utcnow(),
                                    "title": caption,
                                }},
                                upsert=True,
                            )
                except Exception as _e:
                    logger.debug(f"bundle episode ownership: {_e}")
                logger.info(f"✅ BUNDLE: Movie {i}/{total} sent")

            if progress_msg and i % 3 == 0:
                try:
                    await progress_msg.edit_text(
                        add_footer(f"📦 <b>Delivering {escape_html(str(series.get('title', 'Collection')))}</b>\n\n⏳ Sending {total} movies... ({i}/{total})\n<i>Please wait...</i>"),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
            if i < total:
                await asyncio.sleep(BOT_FAST_DELAY)
        except Exception as e:
            logger.error(f"❌ BUNDLE: Movie {i}/{total} send failed: {e}")
            failed_episodes.append(i)
            await asyncio.sleep(BOT_FAST_DELAY)
            continue

    try:
        series_bundles_collection.update_one({"token": series_token}, {"$inc": {"downloads": 1}})
    except Exception:
        pass
    try:
        record_movie_access(user_id, series_token, series.get("title", ""))
    except Exception:
        pass
    # Ensure parent payment is success with series_token
    try:
        if payments_collection is not None:
            payments_collection.update_one(
                {"user_id": user_id, "series_token": series_token, "status": {"$ne": "success"}},
                {"$set": {"status": "success", "paid_at": datetime.utcnow(), "updated_at": datetime.utcnow()}},
            )
            payments_collection.update_one(
                {"user_id": user_id, "series_token": series_token},
                {"$setOnInsert": {
                    "user_id": user_id,
                    "series_token": series_token,
                    "status": "success",
                    "amount": series.get("price") or 0,
                    "paid_at": datetime.utcnow(),
                    "created_at": datetime.utcnow(),
                    "title": series.get("title", ""),
                    "from_bundle": True,
                }},
                upsert=True,
            )
    except Exception as e:
        logger.debug(f"bundle payment stamp: {e}")

    if progress_msg:
        try:
            await progress_msg.delete()
        except Exception:
            pass

    title_safe = escape_html(str(series.get("title", "Collection")))
    success_text = add_footer(
        f"🎬 <b>{title_safe}</b>\n\n"
        f"✅ <b>Payment Confirmed!</b>\n"
        f"{sent_count}/{total} movies delivered automatically 🍿"
    )
    if failed_episodes:
        success_text += f"\n\n⚠️ {len(failed_episodes)} part(s) could not be sent. Contact admin if needed."
    try:
        await bot.send_message(chat_id=user_id, text=success_text, reply_markup=final_keyboard, parse_mode=ParseMode.HTML)
        logger.info(f"✅ BUNDLE DELIVERY COMPLETE: {sent_count}/{total} for user={user_id}")
        return sent_count > 0
    except Exception as e:
        logger.error(f"❌ BUNDLE: Final message failed: {e}")
        try:
            await bot.send_message(chat_id=user_id, text=f"✅ Payment confirmed! {sent_count}/{total} movies delivered.")
        except Exception:
            pass
        return sent_count > 0


async def auto_deliver_cart(user_id: int, cart_items: list, context: ContextTypes.DEFAULT_TYPE = None):
    clear_pending_payment_tracker(user_id)
    logger.info(f"🚀 AUTO-DELIVER CART: user={user_id}, items={len(cart_items) if cart_items else 0}")
    if not mongodb_connected or (movies_collection is None and series_bundles_collection is None):
        try:
            await application.bot.send_message(chat_id=user_id, text=add_footer(f"⚠️ Bot is in maintenance mode. Please try again later."))
        except Exception:
            pass
        return False
    if not cart_items:
        cart_doc = cart_collection.find_one({"user_id": user_id}) if cart_collection is not None else None
        if cart_doc:
            cart_items = cart_doc.get("items", [])
        if not cart_items:
            try:
                await application.bot.send_message(chat_id=user_id, text=add_footer(f"❌ Cart is empty. Contact admin for help."))
            except Exception:
                pass
            return False
    total = len(cart_items)
    progress_msg = None
    try:
        progress_msg = await application.bot.send_message(chat_id=user_id, text=add_footer(f"🛒 <b>Delivering Your Movies</b>\n\n⏳ Sending {total} items... (0/{total})\n<i>Please wait...</i>"), parse_mode=ParseMode.HTML)
    except Exception:
        pass
    delivered = 0
    failed_items = []
    resolved_items = []
    for item in cart_items:
        token = item.get("token")
        is_bundle = item.get("is_bundle", False)
        if is_bundle and series_bundles_collection is not None:
            bundle = series_bundles_collection.find_one({"token": token})
            if bundle:
                resolved_items.append({"type": "bundle", "data": bundle})
            else:
                failed_items.append(item.get("title", token))
        elif movies_collection is not None:
            movie = movies_collection.find_one({"token": token})
            if movie:
                resolved_items.append({"type": "movie", "data": movie})
            else:
                failed_items.append(item.get("title", token))
    
    delivery_list = []
    for i, resolved in enumerate(resolved_items, 1):
        if resolved["type"] == "bundle":
            bundle = resolved["data"]
            delivery_list.append(f"{i}. {bundle.get('title', 'Unknown Collection')} (Movie Collection)")
        else:
            movie = resolved["data"]
            delivery_list.append(f"{i}. {movie.get('title', 'Unknown Movie')}")
    
    if delivery_list:
        try:
            await application.bot.send_message(chat_id=user_id, text=add_footer(f"📦 <b>Your Movies:</b>\n\n" + "\n".join(delivery_list)), parse_mode=ParseMode.HTML)
        except Exception:
            pass
    
    final_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎬 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")],
        [InlineKeyboardButton(f"🎦 ʙʀᴏᴡꜱᴇ ᴀʟʟ ᴍᴏᴠɪᴇꜱ", callback_data="watch_all_movies_0")],
        [InlineKeyboardButton(f"🔍 ꜱᴇᴀʀᴄʜ ᴍᴏᴠɪᴇꜱ", callback_data="search_movies")],
        [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]
    ])

    for i, resolved in enumerate(resolved_items, 1):
        if resolved["type"] == "bundle":
            bundle = resolved["data"]
            episodes = bundle.get("episodes", [])
            bundle_delivered = 0
            for ep in episodes:
                try:
                    increment_used_movies(user_id, 1)
                    file_id = ep.get('file_id')
                    caption = ep.get('title', 'Episode')
                    if file_id:
                        if ep.get('file_type') == 'video':
                            await application.bot.send_video(chat_id=user_id, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
                        else:
                            await application.bot.send_document(chat_id=user_id, document=file_id, caption=caption, parse_mode=ParseMode.HTML)
                    else:
                        await application.bot.forward_message(chat_id=user_id, from_chat_id=ep['channel_id'], message_id=ep['message_id'])
                        await application.bot.send_message(chat_id=user_id, text=caption, parse_mode=ParseMode.HTML)
                    bundle_delivered += 1
                    await asyncio.sleep(BOT_FAST_DELAY)
                except Exception as e:
                    logger.error(f"❌ CART: Bundle episode send failed: {e}")
                    continue
            if bundle_delivered > 0:
                delivered += 1
            else:
                failed_items.append(bundle.get("title", "Unknown Collection"))
        else:
            movie = resolved["data"]
            try:
                increment_used_movies(user_id, 1)
                file_id = movie.get('file_id')
                caption = movie['title']
                if file_id:
                    if movie.get('file_type') == 'video':
                        await application.bot.send_video(chat_id=user_id, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
                    else:
                        await application.bot.send_document(chat_id=user_id, document=file_id, caption=caption, parse_mode=ParseMode.HTML)
                else:
                    await application.bot.forward_message(chat_id=user_id, from_chat_id=movie['channel_id'], message_id=movie['message_id'])
                    await application.bot.send_message(chat_id=user_id, text=caption, parse_mode=ParseMode.HTML)
                delivered += 1
                try:
                    record_movie_access(user_id, movie.get("token"), movie.get("title", ""))
                except Exception:
                    pass
                # Persist ownership so /mymovies + rewatch skip payment
                try:
                    if payments_collection is not None and movie.get("token"):
                        payments_collection.update_one(
                            {"user_id": user_id, "movie_token": movie["token"], "status": "success", "from_cart": True},
                            {"$setOnInsert": {
                                "user_id": user_id,
                                "movie_token": movie["token"],
                                "status": "success",
                                "from_cart": True,
                                "amount": 0,
                                "paid_at": datetime.utcnow(),
                                "created_at": datetime.utcnow(),
                                "title": movie.get("title", ""),
                            }},
                            upsert=True,
                        )
                except Exception as e:
                    logger.debug(f"cart ownership persist: {e}")
                if movies_collection is not None:
                    movies_collection.update_one({"token": movie["token"]}, {"$inc": {"downloads": 1}})
            except Exception as e:
                logger.error(f"❌ CART: Movie {i}/{total} send failed: {e}")
                failed_items.append(movie.get("title", "Unknown Movie"))
                await asyncio.sleep(BOT_FAST_DELAY)
                continue
        if progress_msg and i % 2 == 0:
            try:
                await progress_msg.edit_text(add_footer(f"🛒 <b>Delivering Your Movies</b>\n\n⏳ Sending {total} items... ({i}/{total})\n<i>Please wait...</i>"), parse_mode=ParseMode.HTML)
            except Exception:
                pass
        if i < total:
            await asyncio.sleep(BOT_FAST_DELAY)
    clear_user_cart(user_id)
    if progress_msg:
        try:
            await progress_msg.delete()
        except Exception:
            pass
    success_text = add_footer(f"🎬 <b>ᴏʀᴅᴇʀ ᴄᴏᴍᴘʟᴇᴛᴇ!</b>\n\n✅ <b>Payment Confirmed!</b>\n{delivered}/{total} items delivered automatically 🍿")
    if failed_items:
        success_text += f"\n\n⚠️ {len(failed_items)} item(s) failed to deliver. Contact admin if needed."
    try:
        await application.bot.send_message(chat_id=user_id, text=success_text, reply_markup=final_keyboard, parse_mode=ParseMode.HTML)
        logger.info(f"✅ CART DELIVERY COMPLETE: {delivered}/{total} for user={user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ CART: Final message failed: {e}")
        return False

# ============ CART SYSTEM ============
def get_user_cart(user_id: int) -> list:
    if not mongodb_connected or cart_collection is None:
        return []
    try:
        cart = cart_collection.find_one({"user_id": user_id})
        return cart.get("items", []) if cart else []
    except Exception:
        return []

def add_to_cart(user_id: int, movie_token: str) -> bool:
    if not mongodb_connected or cart_collection is None:
        return False
    try:
        item = None
        is_bundle = False
        if movies_collection is not None:
            item = movies_collection.find_one({"token": movie_token})
        if not item and series_bundles_collection is not None:
            item = series_bundles_collection.find_one({"token": movie_token})
            is_bundle = True
        if not item:
            return False
        cart = cart_collection.find_one({"user_id": user_id})
        if not cart:
            cart_collection.insert_one({"user_id": user_id, "items": [{"token": movie_token, "title": item["title"], "price": item.get("price", get_default_price()), "is_bundle": is_bundle, "added_at": datetime.utcnow()}], "created_at": datetime.utcnow()})
        else:
            existing = [i for i in cart.get("items", []) if i["token"] == movie_token]
            if existing:
                return False
            cart_collection.update_one({"user_id": user_id}, {"$push": {"items": {"token": movie_token, "title": item["title"], "price": item.get("price", get_default_price()), "is_bundle": is_bundle, "added_at": datetime.utcnow()}}})
        return True
    except Exception as e:
        logger.error(f"Add to cart error: {e}")
        return False

def remove_from_cart(user_id: int, movie_token: str) -> bool:
    if not mongodb_connected or cart_collection is None:
        return False
    try:
        cart_collection.update_one({"user_id": user_id}, {"$pull": {"items": {"token": movie_token}}})
        return True
    except Exception as e:
        logger.error(f"Remove from cart error: {e}")
        return False

def clear_user_cart(user_id: int):
    if not mongodb_connected or cart_collection is None:
        return
    try:
        cart_collection.delete_one({"user_id": user_id})
    except Exception as e:
        logger.error(f"Clear cart error: {e}")

def get_cart_total(user_id: int) -> int:
    items = get_user_cart(user_id)
    return sum(item.get("price", 0) for item in items)

def apply_cart_discount(user_id: int) -> int:
    """Return total discount amount.
    5+ movies in cart → 1 free (cheapest)
    10+ movies in cart → 2 free (two cheapest)
    """
    items = get_user_cart(user_id)
    if not items:
        return 0
    prices = sorted([int(item.get("price", 0) or 0) for item in items])
    if len(items) >= 10:
        return prices[0] + prices[1]
    if len(items) >= 5:
        return prices[0]
    return 0

def increment_purchase_count(user_id: int):
    if users_collection is not None:
        users_collection.update_one({"user_id": user_id}, {"$inc": {"purchases_count": 1}})
        user = users_collection.find_one({"user_id": user_id})
        if user and user.get("purchases_count", 0) >= 5 and not user.get("free_available", False):
            users_collection.update_one({"user_id": user_id}, {"$set": {"free_available": True}})
            asyncio.create_task(notify_free_available(user_id))

async def notify_free_available(user_id: int):
    try:
        await application.bot.send_message(chat_id=user_id, text=add_footer(f"🎉 <b>Congratulations!</b>\n\nYou have purchased 5 movies!\nYou've earned <b>1 FREE MOVIE</b> as a reward.\n\nUse /claimfree to choose your free movie from your purchased list."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🎁 ᴄʟᴀɪᴍ ꜰʀᴇᴇ ᴍᴏᴠɪᴇ", callback_data="claimfree")], [InlineKeyboardButton(f"🌱 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")]]), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to notify free available: {e}")

def get_user_purchased_tokens(user_id: int):
    """All tokens the user paid for OR was delivered (survives restarts — Mongo only)."""
    tokens = set()
    if mongodb_connected and payments_collection is not None:
        purchased = payments_collection.find({"user_id": user_id, "status": "success", "movie_token": {"$exists": True, "$ne": None}})
        for p in purchased:
            token = p.get("movie_token")
            if token:
                tokens.add(token)
        cart_purchases = payments_collection.find({"user_id": user_id, "status": "success", "cart_tokens": {"$exists": True, "$ne": []}})
        for p in cart_purchases:
            for token in p.get("cart_tokens", []):
                if token:
                    tokens.add(token)
        series_purchases = payments_collection.find({"user_id": user_id, "status": "success", "series_token": {"$exists": True, "$ne": None}})
        for p in series_purchases:
            token = p.get("series_token")
            if token:
                tokens.add(token)
    # Permanent access log (subscription watches + delivered free rewatches)
    if users_collection is not None:
        try:
            u = users_collection.find_one({"user_id": user_id}, {"accessed_movies": 1})
            if u and u.get("accessed_movies"):
                for item in u["accessed_movies"]:
                    if isinstance(item, dict) and item.get("token"):
                        tokens.add(item["token"])
                    elif isinstance(item, str):
                        tokens.add(item)
        except Exception:
            pass
    return list(tokens)



def user_owns_movie(user_id: int, token: str) -> bool:
    """True if user already paid for this token (single, cart, series/bundle, or accessed)."""
    if not token:
        return False
    try:
        owned = set(get_user_purchased_tokens(user_id))
        if token in owned:
            return True
        if mongodb_connected and payments_collection is not None:
            hit = payments_collection.find_one({
                "user_id": user_id,
                "status": "success",
                "$or": [
                    {"movie_token": token},
                    {"series_token": token},
                    {"cart_tokens": token},
                    {"episode_tokens": token},
                ],
            })
            if hit:
                return True
        # Part of a paid bundle? (movie has bundle_parent / bundle_token)
        if movies_collection is not None:
            mdoc = movies_collection.find_one({"token": token}, {"bundle_parent": 1, "bundle_token": 1, "is_bundle_part": 1})
            if mdoc:
                parent = mdoc.get("bundle_parent") or mdoc.get("bundle_token")
                if parent and (parent in owned or (
                    payments_collection is not None and payments_collection.find_one(
                        {"user_id": user_id, "status": "success", "series_token": parent}
                    )
                )):
                    return True
        # Episode listed inside a paid series_bundles doc
        if series_bundles_collection is not None and payments_collection is not None:
            paid_series = payments_collection.find(
                {"user_id": user_id, "status": "success", "series_token": {"$exists": True, "$ne": None}},
                {"series_token": 1},
            )
            for p in paid_series:
                st = p.get("series_token")
                if not st:
                    continue
                if st == token:
                    return True
                bdoc = series_bundles_collection.find_one({"token": st}, {"episodes.token": 1, "episodes.movie_token": 1})
                if not bdoc:
                    continue
                for ep in bdoc.get("episodes") or []:
                    if ep.get("token") == token or ep.get("movie_token") == token:
                        return True
    except Exception as e:
        logger.debug(f"user_owns_movie: {e}")
    return False

def record_movie_access(user_id: int, token: str, title: str = ""):
    """Record that user received/started a movie (for /mymovies including token-link access)."""
    if not mongodb_connected or users_collection is None or not token:
        return
    try:
        users_collection.update_one(
            {"user_id": user_id},
            {
                "$addToSet": {"accessed_movies": {"token": token, "title": title or token, "at": datetime.utcnow()}},
                "$set": {"last_activity": datetime.utcnow()}
            },
            upsert=True
        )
        # Keep list reasonable size
        users_collection.update_one(
            {"user_id": user_id, "accessed_movies.20": {"$exists": True}},
            {"$push": {"accessed_movies": {"$each": [], "$slice": -100}}}
        )
    except Exception as e:
        logger.debug(f"record_movie_access error: {e}")

def get_user_accessed_tokens(user_id: int) -> list:
    """Return tokens user has accessed (purchased + token-link / subscription deliveries)."""
    tokens = set(get_user_purchased_tokens(user_id))
    if users_collection is not None:
        try:
            u = users_collection.find_one({"user_id": user_id}, {"accessed_movies": 1})
            if u and u.get("accessed_movies"):
                for item in u["accessed_movies"]:
                    if isinstance(item, dict) and item.get("token"):
                        tokens.add(item["token"])
                    elif isinstance(item, str):
                        tokens.add(item)
        except Exception:
            pass
    return list(tokens)

def get_all_movies_paginated(page: int = 0, per_page: int = 20):
    if not mongodb_connected or movies_collection is None:
        return [], 0
    try:
        total_movies = movies_collection.count_documents({})
        total_bundles = series_bundles_collection.count_documents({}) if series_bundles_collection is not None else 0
        total = total_movies + total_bundles
        movies = list(movies_collection.find().sort("created_at", DESCENDING).skip(page * per_page).limit(per_page))
        remaining = per_page - len(movies)
        if remaining > 0 and series_bundles_collection is not None:
            bundles = list(series_bundles_collection.find().sort("created_at", DESCENDING).limit(remaining))
            for b in bundles:
                b['is_bundle'] = True
                if 'poster_url' not in b:
                    b['poster_url'] = ''
            movies.extend(bundles)
        for m in movies:
            if 'poster_url' not in m:
                m['poster_url'] = ''
        return movies, total
    except Exception as e:
        logger.error(f"Pagination error: {e}")
        return [], 0

async def post_to_channel_with_buttons(text: str = None, buttons: list = None, image_file_ids: list = None, image_urls: list = None, caption: str = None, movie_title: str = None, price: int = None, bot_link: str = None, category: str = "movie", target_channel: str = None, per_row: int = 3, button_style: str = "full", delay_seconds: float = 0.15):
    channel = target_channel or PUBLIC_CHANNEL_USERNAME or CHANNEL_ID
    if not channel:
        logger.error("No target channel configured")
        return False

    if movie_title and price is not None and bot_link:
        category_emoji = {"movie": "🎬", "series": "📺", "anime": "✨", "documentary": "🌍"}
        cat_emoji = category_emoji.get(category, "🎬")
        price_text = f"{price} KES" if price > 0 else f"FREE 🎁"
        post_text = apply_premium_emojis(f"{cat_emoji} <b>{escape_html(movie_title)}</b>\n\n💰 <b>{price_text}</b> | 👑 Subscribers: <b>FREE</b>\n\n{caption or ''}")
        btn_price_text = f"{price} KES" if price > 0 else f"FREE 🎁"
        if button_style == "movie_only":
            keyboard_buttons = [{"text": f"🚀 ɢᴇᴛ ᴍᴏᴠɪᴇ — {price} KES", "url": bot_link}]
        else:
            keyboard_buttons = [{"text": f"🚀 ɢᴇᴛ ᴍᴏᴠɪᴇ — {price} KES", "url": bot_link}, {"text": f"👑 ꜱᴜʙꜱᴄʀɪʙᴇ ᴡᴇᴇᴋʟʏ", "callback_data": "show_plans"}, {"text": f"🌾 ꜱᴜʙꜱᴄʀɪʙᴇ ᴍᴏɴᴛʜʟʏ", "callback_data": "show_plans"}, {"text": f"📤 ꜱʜᴀʀᴇ", "url": f"https://t.me/share/url?url={bot_link}"}]
        keyboard = build_button_rows(keyboard_buttons, per_row=per_row)
    else:
        post_text = text or ""
        keyboard = build_button_rows(buttons, per_row=per_row) if buttons else None

    try:
        images = []
        raw_images = image_file_ids or image_urls or []
        for img in raw_images:
            if is_base64_data_url(img):
                try:
                    images.append(data_url_to_inputfile(img))
                except Exception as e:
                    logger.error(f"Failed to convert base64 image: {e}")
            else:
                images.append(img)

        if len(images) == 1:
            await application.bot.send_photo(chat_id=channel, photo=images[0], caption=post_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            logger.info(f"Single photo with caption+buttons sent to {channel}")
            return True
        elif len(images) >= 2:
            # Chunked albums — all images sent even when there are more than 10
            await send_album_chunked(application.bot, channel, images, caption=post_text, parse_mode=ParseMode.HTML)
            if keyboard:
                await send_html_safe(application.bot, channel, apply_premium_emojis("🔗 Tap a button below ↓"), reply_markup=keyboard)
            logger.info(f"Album with {len(images)} images sent to {channel}")
            return True
        else:
            await application.bot.send_message(chat_id=channel, text=post_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            return True
    except Exception as e:
        logger.error(f"Channel post error: {e}")
        logger.error(traceback.format_exc())
        return False

def format_payment_guide(price: int, plans: dict = None) -> str:
    if plans is None:
        plans = get_subscription_plans()
    weekly = plans.get('weekly', {})
    monthly = plans.get('monthly', {})
    card_section = ""
    if get_card_payment_setting():
        card_section = f"""1️⃣ <b>Card / Bank</b>
   ➡️ Click "Pay with Card"
   ➡️ Enter card details securely
   ➡️ Movie delivered instantly

"""
    sub_section = ""
    if get_subscription_enabled():
        sub_section = f"""👑 <b>Subscription Plans</b>

☆ <b>Weekly Pass — {weekly.get('price', 50)} KES</b>
✅ Unlimited movies for 7 days
✅ No individual payments
✅ Instant auto-delivery

🔥 <b>Monthly Pass — {monthly.get('price', 150)} KES</b>
✅ Unlimited movies for 30 days
✅ Best value for money
✅ Instant auto-delivery

"""
    guide = f"""💰 <b>Payment Options</b> 💰

🎬 <b>Single Movie Purchase</b>
➡️ Price: <b>{price} KES</b>
➡️ Instant delivery after payment
➡️ One-time access

{sub_section}💳 <b>How to Pay:</b>

{card_section}2️⃣ <b>M-Pesa</b>
   ➡️ Click "Pay with M-Pesa"
   ➡️ Enter your M-Pesa number
   ➡️ Approve M-Pesa prompt on your phone
   ➡️ Movie delivered instantly

🚀 Your movie is delivered <b>automatically</b> after payment confirmation!

❓ Need help? Contact admin"""
    return guide

def format_phone_number(phone: str) -> str:
    if not phone:
        return None
    phone = phone.strip()
    if phone.startswith('+'):
        phone = phone[1:]
    digits = _PHONE_DIGITS_RE.sub('', phone)
    if len(digits) == 12 and digits.startswith('254'):
        prefix = digits[3:5]
        if prefix in _VALID_KE_PREFIXES:
            return digits
        return None
    if len(digits) == 10 and digits.startswith('0'):
        prefix = digits[1:3]
        if prefix in _VALID_KE_PREFIXES:
            return "254" + digits[1:]
        return None
    if len(digits) == 9:
        prefix = digits[0:2]
        if prefix in _VALID_KE_PREFIXES:
            return "254" + digits
        return None
    if len(digits) == 13 and digits.startswith('2540'):
        corrected = "254" + digits[4:]
        prefix = corrected[3:5]
        if prefix in _VALID_KE_PREFIXES:
            return corrected
        return None
    return None

def is_valid_phone(phone: str) -> bool:
    return format_phone_number(phone) is not None

def _looks_like_phone_number(text: str) -> bool:
    digits_only = re.sub(r'\D', '', text)
    if len(digits_only) < 8:
        return False
    if digits_only.startswith('254') and len(digits_only) >= 12:
        return True
    if digits_only.startswith('0') and len(digits_only) >= 10:
        return True
    if digits_only.startswith('7') and len(digits_only) >= 9:
        return True
    return False

def get_carrier_name(phone: str) -> tuple:
    digits = format_phone_number(phone)
    if not digits:
        return "Unknown", None
    prefix2 = digits[3:5]
    prefix3 = digits[3:6]
    if prefix2 == '47':
        return "Faiba", None
    if prefix2 in ('70', '71', '72', '74', '79'):
        return "Safaricom", "mpesa"
    if prefix2 == '11' and prefix3 in ('110', '111', '112', '113', '114', '115'):
        return "Safaricom", "mpesa"
    if prefix2 in ('73', '78'):
        return "Airtel", "atl"
    if prefix2 == '10' and prefix3 in ('100', '101', '102'):
        return "Airtel", "atl"
    if prefix2 == '77':
        return "Telkom", None
    return "Unknown", None

def initialize_paystack_payment(email: str, amount_kes: int, reference: str, metadata: dict = None, plan: str = None):
    amount_kobo = amount_kes * 100
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    payload = {"email": email, "amount": amount_kobo, "reference": reference, "currency": "KES", "callback_url": f"{WEBHOOK_URL}/api/paystack/callback", "metadata": metadata or {}}
    if plan:
        payload["metadata"]["plan"] = plan
        payload["metadata"]["is_subscription"] = True
    try:
        res = requests.post(f"{PAYSTACK_API}/transaction/initialize", json=payload, headers=headers, timeout=30)
        data = res.json()
        if data.get('status'):
            return data['data']['authorization_url'], data['data']['reference']
        return None, None
    except Exception as e:
        logger.error(f"Paystack init error: {e}")
        return None, None

def initiate_mpesa_stk_push(phone: str, amount: int, reference: str, metadata: dict = None):
    formatted_phone = format_phone_number(phone)
    if not formatted_phone:
        return None, "Invalid phone number. Use: 07XX XXX XXX, 01XX XXX XXX, or 2547XX XXX XXX"
    carrier, provider = get_carrier_name(phone)
    if not provider:
        return None, f"❌ {carrier} is not supported for mobile money payments. Please use Safaricom or Airtel."
    phone_with_plus = f"+{formatted_phone}"
    amount_kobo = amount * 100
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    payload = {"email": metadata.get("email", f"user{metadata.get('user_id', '0')}@telegram.bot"), "amount": amount_kobo, "reference": reference, "currency": "KES", "metadata": metadata or {}, "mobile_money": {"phone": phone_with_plus, "provider": provider}}
    try:
        res = requests.post(f"{PAYSTACK_API}/charge", json=payload, headers=headers, timeout=30)
        data = res.json()
        logger.info(f"Paystack charge response: {data}")
        if not data.get('status'):
            return None, data.get('message', 'Payment initialization failed')
        charge_data = data.get('data', {})
        charge_status = charge_data.get('status')
        if charge_status == 'pay_offline':
            return {"reference": reference, "display_text": charge_data.get('display_text', 'Check your phone for M-Pesa prompt'), "status": "pending", "phone": formatted_phone, "carrier": carrier, "provider": provider}, None
        if charge_status == 'success':
            return {"reference": reference, "display_text": "Payment completed instantly", "status": "success", "phone": formatted_phone, "carrier": carrier, "provider": provider}, None
        return None, f"Unexpected charge status: {charge_status}"
    except requests.exceptions.Timeout:
        logger.error("M-Pesa prompt request timed out")
        return None, "Payment request timed out. Please try again."
    except requests.exceptions.RequestException as e:
        logger.error(f"M-Pesa prompt network error: {e}")
        return None, "Network error. Please check your connection and try again."
    except Exception as e:
        logger.error(f"M-Pesa prompt error: {e}")
        return None, str(e)

def verify_paystack_payment(reference: str):
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    try:
        res = requests.get(f"{PAYSTACK_API}/transaction/verify/{reference}", headers=headers, timeout=30)
        return res.json()
    except Exception as e:
        logger.error(f"Paystack verify error: {e}")
        return None

async def show_more_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    
    if not ADDITIONAL_CHANNELS:
        no_channels_text = add_footer(f"📡 No movie channels configured.\n\nContact admin to add channels.")
        no_channels_kb = build_back_keyboard("back")
        if query:
            await edit_or_send_message(query, no_channels_text, reply_markup=no_channels_kb, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(no_channels_text, reply_markup=no_channels_kb, parse_mode=ParseMode.HTML)
        return
    
    user_id = update.effective_user.id
    items = []
    items.append({"label": "🌼 ᴍᴏᴠɪᴇ ᴄʜᴀɴɴᴇʟꜱ", "value": ""})
    items.append({"label": "", "value": ""})
    
    for i, ch in enumerate(ADDITIONAL_CHANNELS, 1):
        items.append({"label": f"{i}. {ch['name']}", "value": ""})
    
    title = "📡 MOVIE CHANNELS"
    menu_text = render_menu_for_user(user_id, title, items)
    menu_text = add_footer(menu_text)
    
    keyboard = []
    for ch in ADDITIONAL_CHANNELS:
        keyboard.append([InlineKeyboardButton(f"📢 {ch['name']}", url=ch['url'])])
    keyboard.append([InlineKeyboardButton(f"🎬 ʙʀᴏᴡꜱᴇ ᴀʟʟ ᴍᴏᴠɪᴇꜱ", callback_data="watch_all_movies_0")])
    keyboard.append([InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")])
    
    more_kb = InlineKeyboardMarkup(keyboard)
    
    if query:
        await edit_or_send_message(query, menu_text, reply_markup=more_kb, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(menu_text, reply_markup=more_kb, parse_mode=ParseMode.HTML)
    push_nav_state(context, "more_movies", menu_text, more_kb)

async def show_cart_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    cart_items = get_user_cart(user_id)
    total = get_cart_total(user_id)
    discount = apply_cart_discount(user_id)
    discounted_total = total - discount if discount > 0 else total
    
    if not cart_items:
        items = []
        items.append({"label": "🛒 YOUR CART", "value": ""})
        items.append({"label": "", "value": ""})
        items.append({"label": "😢 Your cart is empty!", "value": ""})
        items.append({"label": "", "value": ""})
        items.append({"label": "ℹ️ Browse movies and tap", "value": ""})
        items.append({"label": "   'Add to Cart' to add items.", "value": ""})
        
        empty_text = render_menu_for_user(user_id, "🛒 YOUR CART", items)
        empty_text = add_footer(empty_text)
        
        empty_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🎬 ʙʀᴏᴡꜱᴇ ᴀʟʟ ᴍᴏᴠɪᴇꜱ", callback_data="watch_all_movies_0")],
            [InlineKeyboardButton(f"📦 ʙʀᴏᴡꜱᴇ ᴍᴏᴠɪᴇ ᴄᴏʟʟᴇᴄᴛɪᴏɴꜱ", callback_data="browse_bundles_0")],
            [InlineKeyboardButton(f"🍷 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")],
            [InlineKeyboardButton(f"🏠 ʜᴏᴍᴇ", callback_data="go_home")],
            [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]
        ])
        if query:
            await edit_or_send_message(query, empty_text, reply_markup=empty_kb, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(empty_text, reply_markup=empty_kb, parse_mode=ParseMode.HTML)
        push_nav_state(context, "cart_empty", empty_text, empty_kb)
        return

    items = []
    items.append({"label": f"🛒 YOUR CART ({len(cart_items)})", "value": ""})
    items.append({"label": "", "value": ""})
    
    for i, item in enumerate(cart_items, 1):
        price_display = f"{item['price']} KES" if item['price'] > 0 else "FREE"
        # Clean list — no extra emojis per item (user request)
        items.append({"label": f"{i}. {item['title']}", "value": price_display})
    
    items.append({"label": "", "value": ""})
    if discount > 0:
        items.append({"label": f"💰 TOTAL:", "value": f"{total} KES"})
        items.append({"label": f"🎁 Discount (Buy 5 Get 1 Free):", "value": f"-{discount} KES"})
        items.append({"label": f"💳 PAY:", "value": f"{discounted_total} KES"})
    else:
        items.append({"label": f"💰 TOTAL:", "value": f"{total} KES"})
    
    cart_text = render_menu_for_user(user_id, "🛒 YOUR CART", items)
    cart_text = add_footer(cart_text)

    keyboard = []
    for idx, item in enumerate(cart_items):
        tok = item.get("token") or ""
        # Use index-based callback to never exceed 64 bytes
        btn_text = f"➖ Remove: {(item.get('title') or 'Item')[:28]}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"crm_{idx}")])
    keyboard.append([InlineKeyboardButton(f"➖ Clear Cart", callback_data="cart_clear")])
    if discounted_total > 0:
        keyboard.append([InlineKeyboardButton(f"📱 ᴘᴀʏ ᴡɪᴛʜ ᴍ-ᴘᴇꜱᴀ ({discounted_total} KES)", callback_data="cart_checkout")])
    keyboard.append([
        InlineKeyboardButton("View Cart & Pay", callback_data="cart_view"),
        InlineKeyboardButton("Continue Shopping", callback_data="watch_all_movies_0"),
    ])
    keyboard.append([InlineKeyboardButton(f"🎬 ʙʀᴏᴡꜱᴇ ᴍᴏʀᴇ", callback_data="watch_all_movies_0")])
    keyboard.append([InlineKeyboardButton(f"🔊 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")])
    keyboard.append([InlineKeyboardButton(f"🏠 ʜᴏᴍᴇ", callback_data="go_home")])
    keyboard.append([InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")])
    if ADDITIONAL_CHANNELS:
        keyboard.append([InlineKeyboardButton(f"➕ ᴀᴅᴅ ꜰʀᴏᴍ ᴄʜᴀɴɴᴇʟ", callback_data="visit_channels")])
    # Store tokens by index for crm_ callbacks
    try:
        context.user_data["cart_token_list"] = [it.get("token") for it in cart_items]
    except Exception:
        pass
    cart_kb = InlineKeyboardMarkup(keyboard)
    if query:
        await edit_or_send_message(query, cart_text, reply_markup=cart_kb, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(cart_text, reply_markup=cart_kb, parse_mode=ParseMode.HTML)
    push_nav_state(context, "cart", cart_text, cart_kb)

async def show_movies_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    user_id = update.effective_user.id
    mode = get_display_mode()
    per_page = 20 if mode == "button" else 9
    movies, total = await asyncio.to_thread(get_all_movies_paginated, page, per_page)
    if not movies:
        no_text = add_footer(f"😢 No movies available yet. Check back soon!")
        no_kb = build_back_keyboard("back")
        if update.message:
            await update.message.reply_text(no_text, reply_markup=no_kb)
        elif update.callback_query:
            query = update.callback_query
            try:
                if query.message and query.message.photo:
                    await query.edit_message_caption(caption=no_text, reply_markup=no_kb)
                else:
                    await query.edit_message_text(no_text, reply_markup=no_kb)
            except Exception as e:
                logger.warning(f"Movies empty edit failed: {e}")
                await query.message.reply_text(no_text, reply_markup=no_kb)
        push_nav_state(context, "movies_empty", no_text, no_kb)
        return
    await send_poster_grid(update, context, movies, page, total, "", is_search=False)

async def send_poster_grid(update, context, movies, page, total, query_str, is_search=False):
    movies = dedupe_movie_items(list(movies or []))  # never show the same movie twice
    display_mode = get_display_mode()
    # Search always uses album+posters (same as browse album mode) so results show images
    if display_mode == 'button' and not is_search:
        await send_movies_page_button2(update, context, movies, page, total, query_str, is_search)
        return

    user_id = update.effective_user.id
    bot_username = context.bot.username
    per_page = 9
    await human_action(context, user_id, ChatAction.TYPING)

    if is_search:
        header_text = add_footer(f"🔍 <b>Results for \"{escape_html(query_str)}\"</b>\nℹ️ Page {page + 1} | {total} total")
    else:
        total_pages = (total - 1) // per_page + 1 if total > 0 else 1
        header_text = add_footer(f"🎬 <b>All Movies</b>\nℹ️ Page {page + 1} of {total_pages} | {total} total")

    header_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📡 More Movies", callback_data="more_movies")],
        [InlineKeyboardButton(f"🏠 Home", callback_data="go_home")]
    ])

    if update.callback_query:
        query = update.callback_query
        if query.message and query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
        else:
            try:
                await query.edit_message_text(text=add_footer(f"🚀 <b>Loading movies...</b>\n\n⏳ Please wait"), parse_mode=ParseMode.HTML)
            except Exception:
                try:
                    await query.message.delete()
                except Exception:
                    pass
        await context.bot.send_message(chat_id=user_id, text=header_text, reply_markup=header_kb, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(header_text, reply_markup=header_kb, parse_mode=ParseMode.HTML)

    # Send posters as Telegram albums, then compact numbered buttons + cart + GET ALL ONCE
    poster_refs = []
    movie_buttons = []
    cart_enabled = get_cart_enabled()
    tokens_for_all = []
    for i, movie in enumerate(movies):
        price = movie.get('price', get_default_price())
        token = movie.get('token', '')
        title = movie.get('title', 'Unknown')
        poster_url = movie.get('poster_url', '') or ''
        poster_file_id = movie.get('poster_file_id') or movie.get('poster_tg_file_id') or ''
        price_text = "FREE" if price == 0 else f"{price}"
        short_title = clean_movie_title_for_button(title, max_len=14)
        # Collect raw refs; resolved + validated below so a dead URL/file_id
        # can never turn into a blank white album image.
        ref = (poster_file_id, poster_url)
        if ref not in poster_refs:
            poster_refs.append(ref)
        # Short labels + short callback_data (token can exceed Telegram 64-byte limit)
        btn_label = short_btn_label(i + 1, title, max_len=28)
        # gp = get page item by index; resolved via last_page_tokens
        cb = f"gp_{page}_{i}"
        movie_buttons.append(InlineKeyboardButton(btn_label, callback_data=cb))
        tokens_for_all.append(token)

    context.user_data["last_page_tokens"] = list(tokens_for_all)
    context.user_data["last_page_num"] = page
    # Resolve every poster to a ref Telegram can really render — never blank.
    # CONCURRENT: all posters resolve in parallel (was sequential → seconds of lag).
    async def _resolve_one(fid, purl):
        try:
            return await resolve_safe_photo(context.bot, fid, purl, context="poster_grid")
        except Exception:
            return None
    _resolved = await asyncio.gather(*(_resolve_one(fid, purl) for fid, purl in poster_refs))
    media = [p for p in _resolved if p is not None]
    if media:
        # ALL posters shown — albums auto-chunk at Telegram's 10-media limit
        await send_album_chunked(
            context.bot, user_id, media,
            caption=build_album_caption(movies), parse_mode=ParseMode.HTML,
        )

    # Arrange buttons: prefer short "N. Title" 3-per-row; if that fails we fall back below
    keyboard = []
    row = []
    for btn in movie_buttons:
        row.append(btn)
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    # Always also prepare pure-number row as ultimate fallback content
    num_only = []
    nrow = []
    for i, tok in enumerate(tokens_for_all):
        nrow.append(InlineKeyboardButton(str(i + 1), callback_data=f"gp_{page}_{i}"))
        if len(nrow) == 5:
            num_only.append(nrow)
            nrow = []
    if nrow:
        num_only.append(nrow)

    if cart_enabled and movie_buttons:
        # Cart buttons under each group of movies (compact)
        cart_row = []
        for i, movie in enumerate(movies):
            tok = movie.get('token', '')
            cart_row.append(InlineKeyboardButton(f"🛒{i+1}", callback_data=f"cart_add_{tok}"))
            if len(cart_row) == 3:
                keyboard.append(cart_row)
                cart_row = []
        if cart_row:
            keyboard.append(cart_row)
        keyboard.append([
            InlineKeyboardButton("View Cart & Pay", callback_data="cart_view"),
            InlineKeyboardButton("Continue Shopping", callback_data="watch_all_movies_0")
        ])

    if len(tokens_for_all) > 1:
        # Create / reuse a temporary "get all once" for this page
        all_token = f"pageall_{page}_{secrets.token_hex(3)}"
        # We don't store permanently; just link to first or use a special callback
        _n = len(tokens_for_all)
        _unit = int(get_default_price() or 10)
        _disc = calculate_discount(_n, _unit)
        _pay = max(0, _n * _unit - _disc)
        keyboard.append([InlineKeyboardButton(f"Get All {_n} Once — {_pay} KES", callback_data=f"get_all_page_{page}")])

    try:
        await send_html_safe(
            context.bot, user_id,
            apply_premium_emojis("👇 Tap a button below ↓"),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as kb_err:
        logger.error(f"Movie buttons send failed: {kb_err}")
        # Fallback: numbers + cart + get all (always fits Telegram limits)
        try:
            fb_kb = list(num_only) if num_only else []
            if not fb_kb:
                for i, m in enumerate(movies):
                    if m.get("token"):
                        if not fb_kb or len(fb_kb[-1]) >= 5:
                            fb_kb.append([])
                        fb_kb[-1].append(InlineKeyboardButton(str(i + 1), callback_data=f"gp_{page}_{i}"))
            if cart_enabled and tokens_for_all:
                cart_row = []
                for i, tok in enumerate(tokens_for_all):
                    cart_row.append(InlineKeyboardButton(f"🛒{i+1}", callback_data=f"cart_add_{tok}" if len(f"cart_add_{tok}") <= 64 else f"ca_{page}_{i}"))
                    if len(cart_row) == 3:
                        fb_kb.append(cart_row)
                        cart_row = []
                if cart_row:
                    fb_kb.append(cart_row)
                fb_kb.append([
                    InlineKeyboardButton("View Cart & Pay", callback_data="cart_view"),
                    InlineKeyboardButton("Continue Shopping", callback_data="watch_all_movies_0"),
                ])
            if len(tokens_for_all) > 1:
                _n2=len(tokens_for_all); _u2=int(get_default_price() or 10); _d2=calculate_discount(_n2,_u2); _p2=max(0,_n2*_u2-_d2)
                fb_kb.append([InlineKeyboardButton(f"Get All {_n2} Once — {_p2} KES", callback_data=f"get_all_page_{page}")])
            fb_kb.append([InlineKeyboardButton("◀ Back", callback_data="back")])
            await context.bot.send_message(
                chat_id=user_id,
                text="Tap a number (see list above):",
                reply_markup=InlineKeyboardMarkup(fb_kb),
            )
        except Exception as e2:
            logger.error(f"Number fallback failed: {e2}")

    is_bundle_grid = bool(movies and movies[0].get('is_bundle'))
    nav_cb = "browse_bundles_" if is_bundle_grid else "movies_page_"
    nav_keyboard = []
    
    nav_row = []
    if page > 0:
        if is_search:
            nav_row.append(InlineKeyboardButton(f"⬅️ ʙᴀᴄᴋ", callback_data=f"search_page_{page - 1}"))
        else:
            nav_row.append(InlineKeyboardButton(f"⬅️ ʙᴀᴄᴋ", callback_data=f"{nav_cb}{page - 1}"))
    if is_search:
        if total > (page + 1) * per_page:
            nav_row.append(InlineKeyboardButton(f"ɴᴇxᴛ ➡️", callback_data=f"search_page_{page + 1}"))
    else:
        total_pages = (total - 1) // per_page + 1 if total > 0 else 1
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(f"ɴᴇxᴛ ➡️", callback_data=f"{nav_cb}{page + 1}"))
    if nav_row:
        nav_keyboard.append(nav_row)

    action_row = []
    action_row.append(InlineKeyboardButton("🔍 ꜱᴇᴀʀᴄʜ", callback_data="search_prompt"))
    if get_cart_enabled():
        cart_count = len(get_user_cart(user_id))
        action_row.append(InlineKeyboardButton(f"🛒 ᴄᴀʀᴛ ({cart_count})", callback_data="cart_view"))
    action_row.append(InlineKeyboardButton("🌿 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies"))
    nav_keyboard.append(action_row)

    # Skip to page
    nav_keyboard.append([InlineKeyboardButton("🔢 Skip to page", callback_data="skip_to_page")])
    bottom_row = [
        InlineKeyboardButton("🏠 HOME", callback_data="go_home"),
        InlineKeyboardButton("◀️ BACK", callback_data="back")
    ]
    nav_keyboard.append(bottom_row)
    
    if ADDITIONAL_CHANNELS:
        nav_keyboard.append([InlineKeyboardButton("➕ ᴀᴅᴅ ꜰʀᴏᴍ ᴄʜᴀɴɴᴇʟ", callback_data="visit_channels")])

    await context.bot.send_message(chat_id=user_id, text=add_footer("⬇️ Navigation"), reply_markup=InlineKeyboardMarkup(nav_keyboard), parse_mode=ParseMode.HTML)

def init_upload_session(admin_id: int) -> dict:
    session = {"admin_id": admin_id, "posters": [], "videos": [], "merged_movies": [], "status": "awaiting_posters", "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(), "price": None, "category": "movie", "channel_to_post": None}
    return session

def save_upload_session(session: dict):
    if not mongodb_connected or admin_upload_sessions_collection is None:
        return False
    try:
        admin_upload_sessions_collection.update_one({"admin_id": session["admin_id"]}, {"$set": session}, upsert=True)
        return True
    except Exception as e:
        logger.error(f"Save upload session error: {e}")
        return False

def get_upload_session(admin_id: int) -> dict:
    if not mongodb_connected or admin_upload_sessions_collection is None:
        return None
    try:
        session = admin_upload_sessions_collection.find_one({"admin_id": admin_id})
        return session
    except Exception as e:
        logger.error(f"Get upload session error: {e}")
        return None

def delete_upload_session(admin_id: int):
    if not mongodb_connected or admin_upload_sessions_collection is None:
        return
    try:
        admin_upload_sessions_collection.delete_one({"admin_id": admin_id})
    except Exception as e:
        logger.error(f"Delete upload session error: {e}")
        return

def clean_movie_title_for_button(title: str, max_len: int = 28) -> str:
    """Short human title for lists/captions. Always returns something usable."""
    if not title:
        return "Movie"
    prefixes = [
        'dj dhaveman', 'dj dhave', 'dj migo', 'dj kadel', 'dj icef', 'dj lycox',
        'dj smith', 'dj afro', 'series', 'action', 'horror', 'comedy', 'drama',
        'kihindi', 'kijeshi', 'korean', 'vivamax', 'anime', 'chinese', 'movie',
        'full', 'hd', 'bluray', 'afro series hd series', 'afro kijeshi series',
    ]
    cleaned = str(title).lower()
    for p in prefixes:
        if cleaned.startswith(p):
            cleaned = cleaned[len(p):].strip(" :-_|")
    cleaned = re.sub(r'\(20\d{2}\)', '', cleaned)
    cleaned = re.sub(r'20\d{2}', '', cleaned)
    cleaned = re.sub(r'title:\s*', '', cleaned, flags=re.I)
    cleaned = re.sub(r'[._]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(" -_|")
    if not cleaned:
        cleaned = str(title)
    # title-case lightly
    out = cleaned.title() if cleaned.islower() or cleaned == cleaned.lower() else cleaned
    if len(out) > max_len:
        out = out[: max_len - 1].rstrip() + "…"
    return out

def short_btn_label(index: int, title: str, max_len: int = 28) -> str:
    """Number at far left so long titles stay readable. Telegram limit 64 chars."""
    prefix = f"{index}. "
    # Leave room for number prefix
    room = max(8, min(max_len, 60 - len(prefix)))
    t = clean_movie_title_for_button(title, max_len=room)
    label = f"{prefix}{t}"
    return label[:60]



# ---- Rotating arrow / bullet styles for channel posts (dashboard sends) ----
CHANNEL_ARROW_STYLES = [
    "⇒", "➔", "➧", "➨", "➯", "➮", "➭", "➫", "➺", "➻", "➾", "➩", "➤", "➣", "➜", "➙", "➛",
    "☛", "➤", "◆", "◇", "◈", "◉", "●", "◆", "✦", "✧", "✯", "❖",
    "⬥", "⬧", "◊", "☑", "✒", "❥",
]
_arrow_idx = {"n": 0}

def next_channel_arrow() -> str:
    """One arrow style per album/post — advances only when a new album starts."""
    styles = CHANNEL_ARROW_STYLES
    try:
        i = int(_arrow_idx.get("n", 0)) % len(styles)
        _arrow_idx["n"] = i + 1
        return styles[i]
    except Exception:
        return "⇒"

def decorate_title_list(titles: list, arrow: str = None) -> str:
    """Prefix every title in THIS album with the SAME arrow (no mix inside one post)."""
    if not titles:
        return ""
    # One style for the whole album
    a = arrow if arrow is not None else next_channel_arrow()
    lines = []
    for t in titles:
        title = str(t).strip()
        if not title:
            continue
        # strip existing leading arrow-like chars so we don't double
        while title and title[0] in "⇒➔➧➨➯➮➭➫➺➻➾➩➤➣➜➙➛☛◆◇◈◉●✦✧✯❖⬥⬧◊☑✒❥➠":
            title = title[1:].lstrip()
        title = re.sub(r"^\d+[\).\-:]\s*", "", title)
        lines.append(f"{a} {len(lines)+1}. {title}")
    return "\n".join(lines)


def dedupe_movie_items(items: list, title_key_candidates=("button_title", "title")) -> list:
    """Remove duplicate movies from a list of dicts.
    A duplicate = same token, OR same normalized title (case/space/punctuation-insensitive).
    Keeps first occurrence so numbering stays stable: 1. 2. 3. ..."""
    seen_tokens = set()
    seen_titles = set()
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tok = (it.get("token") or "").strip()
        if tok and tok in seen_tokens:
            continue
        norm = ""
        for k in title_key_candidates:
            v = (it.get(k) or "").strip()
            if v:
                norm = v
                break
        norm = re.sub(r"\s+", " ", norm.lower()).strip()
        norm = re.sub(r"[^a-z0-9 ]", "", norm)
        if norm and norm in seen_titles:
            continue
        if tok:
            seen_tokens.add(tok)
        if norm:
            seen_titles.add(norm)
        out.append(it)
    return out


def build_album_caption(movies: list) -> str:
    """Fuller titles — ONE arrow style for the whole album. Keep under 1024."""
    if not movies:
        return "Tap a number below"
    arrow = next_channel_arrow()  # one style for this album only
    lines = []
    for m in movies:
        raw = (m.get("title") or "Movie").strip()
        title = re.sub(r"\s+", " ", raw)
        title = re.sub(r"^\d+[\).\-:]\s*", "", title)
        if len(title) > 48:
            title = title[:47].rstrip() + "…"
        lines.append(f"{arrow} {len(lines)+1}. {title}")
    caption = chr(10).join(lines)
    if len(caption) > 1000:
        while len(caption) > 1000 and len(lines) > 3:
            lines.pop()
            caption = chr(10).join(lines) + chr(10) + f"… +{len(movies)-len(lines)} more"
    try:
        caption = apply_premium_emojis(caption)
    except Exception:
        pass
    return caption[:1020]

def extract_movie_title_for_button(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r'\s+', ' ', text.strip())
    cleaned = re.sub(r'\.[A-Za-z0-9]{2,5}$', '', cleaned).strip()
    return cleaned if cleaned else text

def parse_movie_titles_from_poster(poster_text: str) -> list:
    if not poster_text:
        return []
    cleaned = re.sub(r'^\d+[\.\)]\s*', '', poster_text.strip())
    range_match = re.match(r'(.+?)\s*(\d+)\s*[-–]\s*(\d+)$', cleaned)
    if range_match:
        base = range_match.group(1).strip()
        start = int(range_match.group(2))
        end = int(range_match.group(3))
        return [f"{base} {i}" for i in range(start, end + 1)]
    if ',' in cleaned:
        parts = [p.strip() for p in cleaned.split(',')]
        first_part = parts[0]
        base_match = re.match(r'(.+?)\s*(\d+)$', first_part)
        if base_match:
            base = base_match.group(1).strip()
            numbers = [base_match.group(2)]
            for part in parts[1:]:
                if part.isdigit():
                    numbers.append(part)
                else:
                    numbers.append(part)
            return [f"{base} {num}" for num in numbers]
        else:
            return parts
    if '&' in cleaned:
        parts = cleaned.split('&')
        first_part = parts[0].strip()
        base_match = re.match(r'(.+?)\s*(\d+)$', first_part)
        if base_match:
            base = base_match.group(1).strip()
            numbers = [base_match.group(2)]
            for part in parts[1:]:
                part = part.strip()
                if part.isdigit():
                    numbers.append(part)
                else:
                    numbers.append(part)
            return [f"{base} {num}" for num in numbers]
        else:
            return [p.strip() for p in parts]
    return [cleaned]

async def confronterupload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removed — use dashboard album/upload instead."""
    if update.effective_message:
        await update.effective_message.reply_text("This command was removed. Use the admin dashboard.")
    return
    # legacy disabled below

    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ You are not authorized.")
        return
    
    delete_upload_session(admin.id)
    session = init_upload_session(admin.id)
    save_upload_session(session)
    
    context.user_data["confronter_upload_mode"] = True
    context.user_data["confronter_upload_admin"] = admin.id
    
    channels = []
    if PUBLIC_CHANNEL_USERNAME:
        channels.append({"name": f"📢 {PUBLIC_CHANNEL_USERNAME}", "id": PUBLIC_CHANNEL_USERNAME})
    if MY_CHANNEL:
        channels.append({"name": f"📢 {MY_CHANNEL}", "id": MY_CHANNEL})
    if CHANNEL_ID:
        channels.append({"name": f"📢 Channel ID: {CHANNEL_ID}", "id": str(CHANNEL_ID)})
    if channels_collection is not None:
        for ch in channels_collection.find():
            channels.append({"name": f"📢 {ch.get('title', 'Channel')}", "id": str(ch.get('channel_id'))})
    
    keyboard = []
    for ch in channels[:10]:
        keyboard.append([InlineKeyboardButton(ch["name"], callback_data=f"upload_channel_{ch['id']}")])
    keyboard.append([InlineKeyboardButton("💰 0 KES (FREE)", callback_data="upload_price_0")])
    keyboard.append([InlineKeyboardButton("💰 10 KES", callback_data="upload_price_10")])
    keyboard.append([InlineKeyboardButton("💰 20 KES", callback_data="upload_price_20")])
    keyboard.append([InlineKeyboardButton("💰 Set Price (Custom)", callback_data="upload_price_custom")])
    keyboard.append([InlineKeyboardButton("💰 Default (10 KES)", callback_data="upload_price_default")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="upload_cancel")])
    
    await update.message.reply_text(
        f"📤 <b>Confronter Upload</b>\n\n"
        f"<b>Step 1: Select Target Channel</b>\n"
        f"<b>Step 2: Select Price</b>\n\n"
        f"<b>Step 3: Send Posters as Singles</b>\n"
        f"Send <b>poster images</b> with movie title(s) in caption.\n\n"
        f"<b>Formats:</b>\n"
        f"• Single: <code>Movie Title</code>\n"
        f"• Multiple: <code>Extraction 1&2</code> or <code>Movie 1,2,3</code>\n"
        f"• Range: <code>Spiderman 1-8</code>\n\n"
        f"<b>Step 4: Forward Videos/Files</b>\n"
        f"After each poster, forward the video/file for that poster.\n"
        f"Each video/document grabs its poster.\n\n"
        f"<b>Step 5: Album Posting</b>\n"
        f"After completing, posters will be sent to target channel as an album.\n"
        f"Button text will be shortened with 3 rows.\n\n"
        f"Use <code>/confronteruploaddone</code> when all posters and videos are sent.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

async def handle_upload_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return
    
    data = query.data
    if data == "upload_cancel":
        context.user_data.pop("confronter_upload_mode", None)
        context.user_data.pop("confronter_upload_admin", None)
        delete_upload_session(admin.id)
        await query.edit_message_text("❌ Upload cancelled.")
        return
    
    if data.startswith("upload_channel_"):
        channel = data.replace("upload_channel_", "")
        session = get_upload_session(admin.id)
        if not session:
            await query.edit_message_text("❌ No active session.")
            return
        
        session["channel_to_post"] = channel
        save_upload_session(session)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 0 KES (FREE)", callback_data="upload_price_0")],
            [InlineKeyboardButton("💰 10 KES", callback_data="upload_price_10")],
            [InlineKeyboardButton("💰 20 KES", callback_data="upload_price_20")],
            [InlineKeyboardButton("💰 Set Price (Custom)", callback_data="upload_price_custom")],
            [InlineKeyboardButton("💰 Default (10 KES)", callback_data="upload_price_default")],
            [InlineKeyboardButton("❌ Cancel", callback_data="upload_cancel")]
        ])
        
        await query.edit_message_text(f"✅ Channel selected!\n\n<b>Step 2: Select Price</b>", reply_markup=keyboard, parse_mode=ParseMode.HTML)

async def handle_upload_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return
    
    data = query.data
    session = get_upload_session(admin.id)
    if not session:
        await query.edit_message_text("❌ No active session.")
        return
    
    if data == "upload_price_0":
        session["price"] = 0
        save_upload_session(session)
        await query.edit_message_text(f"✅ Price set to <b>0 KES (FREE)</b>!\n\n<b>Step 3: Send Posters</b>\nSend <b>poster images</b> with movie title(s) in caption.\n\nAfter each poster, forward the video/file.\nUse <code>/confronteruploaddone</code> when complete.", parse_mode=ParseMode.HTML)
        return
    elif data == "upload_price_10":
        session["price"] = 10
        save_upload_session(session)
        await query.edit_message_text(f"✅ Price set to <b>10 KES</b>!\n\n<b>Step 3: Send Posters</b>", parse_mode=ParseMode.HTML)
        return
    elif data == "upload_price_20":
        session["price"] = 20
        save_upload_session(session)
        await query.edit_message_text(f"✅ Price set to <b>20 KES</b>!\n\n<b>Step 3: Send Posters</b>", parse_mode=ParseMode.HTML)
        return
    elif data == "upload_price_custom":
        session["status"] = "awaiting_price"
        save_upload_session(session)
        await query.edit_message_text(f"✍️ <b>Send Your Price</b>\n\nType the price in KES (e.g., <code>15</code>)", parse_mode=ParseMode.HTML)
        return
    elif data == "upload_price_default":
        session["price"] = get_default_price()
        save_upload_session(session)
        await query.edit_message_text(f"✅ Price set to default: <b>{get_default_price()} KES</b>!\n\n<b>Step 3: Send Posters</b>", parse_mode=ParseMode.HTML)
        return
    elif data == "upload_cancel":
        context.user_data.pop("confronter_upload_mode", None)
        context.user_data.pop("confronter_upload_admin", None)
        delete_upload_session(admin.id)
        await query.edit_message_text("❌ Upload cancelled.")
        return

async def handle_confronter_upload_price_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    
    session = get_upload_session(admin.id)
    if not session:
        return
    
    if session.get("status") == "awaiting_price":
        text = update.message.text.strip()
        try:
            price = int(text)
            if price < 0:
                await update.message.reply_text("❌ Price cannot be negative.")
                return
            session["price"] = price
            session["status"] = "awaiting_posters"
            save_upload_session(session)
            await update.message.reply_text(f"✅ Price set to <b>{price} KES</b>!\n\n<b>Step 3: Send Posters</b>", parse_mode=ParseMode.HTML)
        except ValueError:
            await update.message.reply_text("❌ Invalid price. Send a number like <code>15</code>.", parse_mode=ParseMode.HTML)

async def handle_confronter_upload_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    
    if not context.user_data.get("confronter_upload_mode") or context.user_data.get("confronter_upload_admin") != admin.id:
        return
    if _other_session_active(context, "confronter_upload_mode"):
        return

    session = get_upload_session(admin.id)
    if not session:
        context.user_data.pop("confronter_upload_mode", None)
        context.user_data.pop("confronter_upload_admin", None)
        return

    if session.get("status") not in ["awaiting_posters", "awaiting_price"]:
        await update.message.reply_text("⚠️ Poster phase complete. Use /confronteruploaddone")
        return
    
    if session.get("price") is None:
        await update.message.reply_text("❌ Select price first.")
        return
    
    message = update.message
    if not message.photo:
        await update.message.reply_text("❌ Send a <b>photo</b> as the poster.", parse_mode=ParseMode.HTML)
        return
    
    photo = message.photo[-1]
    caption = message.caption or ""
    
    if not caption.strip():
        await update.message.reply_text("❌ Provide caption with movie title(s).\n\n<b>Examples:</b>\n• <code>Extraction 1&2</code>\n• <code>Movie 1,2,3</code>\n• <code>Spiderman 1-8</code>", parse_mode=ParseMode.HTML)
        return
    
    titles = parse_movie_titles_from_poster(caption)
    if not titles:
        await update.message.reply_text("❌ Could not parse titles. Use format:\n<code>Extraction 1&2</code> or <code>Movie 1,2,3</code>", parse_mode=ParseMode.HTML)
        return
    
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        file_bytes = io.BytesIO()
        await tg_file.download_to_memory(file_bytes)
        file_bytes.seek(0)
        poster_url = upload_image_with_fallback(file_bytes.read(), context="confronter_upload")
        if not poster_url:
            poster_url = ""
    except Exception as e:
        logger.error(f"Poster upload error: {e}")
        poster_url = ""
    
    # Always keep Telegram file_id — dashboard proxy can fetch it even if all hosts fail
    poster_data = {"file_id": photo.file_id, "caption": caption, "poster_url": poster_url or "", "titles": titles, "video_assigned": False, "video_file_id": None, "video_file_type": None, "video_caption": None}
    
    session["posters"].append(poster_data)
    session["status"] = "awaiting_posters"
    save_upload_session(session)
    
    total_posters = len(session["posters"])
    total_titles = sum(len(p.get("titles", [])) for p in session["posters"])
    button_title = clean_movie_title_for_button(caption)
    host_note = "✅ hosted" if (poster_url and str(poster_url).startswith("http")) else ("📁 local" if poster_url else "📎 Telegram file_id only (still OK)")
    
    await update.message.reply_text(
        f"✅ Poster {total_posters} saved! ({host_note})\n"
        f"Titles: <b>{', '.join(titles)}</b>\n"
        f"Button: <b>{button_title}</b>\n"
        f"Total: {total_posters} posters | {total_titles} movies\n\n"
        f"Send another poster or forward a video for this poster.\n"
        f"Use <code>/confronteruploaddone</code> when all posters are sent.",
        parse_mode=ParseMode.HTML,
    )

async def handle_confronter_upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    
    if not context.user_data.get("confronter_upload_mode") or context.user_data.get("confronter_upload_admin") != admin.id:
        return
    if _other_session_active(context, "confronter_upload_mode"):
        return

    session = get_upload_session(admin.id)
    if not session:
        context.user_data.pop("confronter_upload_mode", None)
        context.user_data.pop("confronter_upload_admin", None)
        return

    if session.get("status") not in ["awaiting_posters", "awaiting_videos"]:
        await update.message.reply_text("⚠️ Not in video phase. Use /confronteruploaddone first.")
        return
    
    message = update.message
    file_id = None
    file_type = None
    
    if message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
    else:
        await update.message.reply_text("❌ Send a <b>video</b> or <b>document</b>.", parse_mode=ParseMode.HTML)
        return
    
    posters = session.get("posters", [])
    unassigned_index = -1
    for i, poster in enumerate(posters):
        if not poster.get("video_assigned", False):
            unassigned_index = i
            break
    
    if unassigned_index == -1:
        await update.message.reply_text("✅ All posters have videos assigned!\nUse <code>/confronteruploaddone</code> to finalize.", parse_mode=ParseMode.HTML)
        return
    
    posters[unassigned_index]["video_assigned"] = True
    posters[unassigned_index]["video_file_id"] = file_id
    posters[unassigned_index]["video_file_type"] = file_type
    posters[unassigned_index]["video_caption"] = message.caption or ""
    
    session["posters"] = posters
    save_upload_session(session)
    
    poster_titles = ", ".join(posters[unassigned_index].get("titles", []))
    remaining = sum(1 for p in posters if not p.get("video_assigned", False))
    
    await update.message.reply_text(f"✅ Video assigned to poster: <b>{poster_titles}</b>\nRemaining posters without video: <b>{remaining}</b>\n\nSend more videos or use <code>/confronteruploaddone</code>", parse_mode=ParseMode.HTML)

async def confronteruploaddone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    session = get_upload_session(admin.id)
    if not session:
        await update.message.reply_text("❌ No active session.")
        return
    
    if session.get("price") is None:
        await update.message.reply_text("❌ Select price first.")
        return
    
    posters = session.get("posters", [])
    if not posters:
        await update.message.reply_text("❌ No posters uploaded.")
        return
    
    unassigned = [i for i, p in enumerate(posters) if not p.get("video_assigned", False)]
    if unassigned:
        unassigned_titles = []
        for i in unassigned:
            unassigned_titles.extend(posters[i].get("titles", []))
        await update.message.reply_text(f"⚠️ {len(unassigned)} poster(s) have no video:\n<b>{', '.join(unassigned_titles[:5])}</b>\n\nSend videos for these posters first.", parse_mode=ParseMode.HTML)
        return
    
    await reply_html_safe(update.message, add_footer("🚀 Finalizing upload..."))
    
    existing_tokens = [doc['token'] for doc in movies_collection.find({}, {'token': 1})]
    if series_bundles_collection is not None:
        existing_tokens.extend([doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})])
    
    all_titles = []
    for poster in posters:
        all_titles.extend(poster.get("titles", []))
    
    upload_title = posters[0].get("caption", "Movie Collection").split('&')[0].split(',')[0].split('-')[0].strip()
    if not upload_title or len(upload_title) < 2:
        upload_title = "Movie Collection"
    
    total_movies = len(all_titles)
    main_token = generate_count_token()
    existing_tokens.append(main_token)
    
    poster_file_ids = []
    for poster in posters:
        if poster.get("file_id"):
            poster_file_ids.append(poster.get("file_id"))
    
    saved_movies = []
    movie_links = []
    price = session.get("price", get_default_price())
    channel_to_post = session.get("channel_to_post")
    
    for poster in posters:
        titles = poster.get("titles", [])
        poster_url = poster.get("poster_url", "")
        poster_file_id = poster.get("file_id")
        video_file_id = poster.get("video_file_id")
        video_file_type = poster.get("video_file_type", "video")
        video_caption = poster.get("video_caption", "")
        poster_caption = poster.get("caption", "")
        
        for title in titles:
            if video_caption:
                cleaned_vc = extract_movie_title_for_button(video_caption)
                vc_core = re.sub(r'[\s\.\-_]+', '', str(cleaned_vc or '')).lower()
                if cleaned_vc and len(cleaned_vc) > 2 and not vc_core.isdigit() and not re.fullmatch(r'[a-z]', vc_core or '') and not re.fullmatch(r'part\s*\d+[a-z]?', cleaned_vc.lower().strip()):
                    title = cleaned_vc
                
            individual_token = generate_count_token()
            existing_tokens.append(individual_token)
            
            movie_doc = {"token": individual_token, "bundle_token": main_token, "title": title, "button_title": clean_movie_title_for_button(title), "description": f"Part of {upload_title} collection ({total_movies} movies)", "price": price, "poster_url": poster_url or "", "poster_file_id": poster_file_id or "", "file_id": video_file_id, "file_type": video_file_type, "channel_id": None, "message_id": None, "private_link": "", "category": "movie", "downloads": 0, "created_at": datetime.utcnow(), "is_bundle_part": True, "bundle_parent": main_token, "collection_title": upload_title, "collection_total": total_movies, "poster_caption": poster_caption}
            
            try:
                movies_collection.insert_one(movie_doc)
                saved_movies.append(movie_doc)
                movie_links.append(f"https://t.me/{context.bot.username}?start={individual_token}")
            except Exception as e:
                logger.error(f"Failed to save movie {title}: {e}")
    
    bundle_data = None
    if series_bundles_collection is not None and len(saved_movies) > 0:
        episodes = []
        for movie in saved_movies:
            episodes.append({"private_link": f"https://t.me/{context.bot.username}?start={movie.get('token')}", "title": movie.get("title"), "button_title": movie.get("button_title", ""), "channel_id": "", "message_id": 0, "file_id": movie.get("file_id"), "file_type": movie.get("file_type", "video"), "token": movie.get("token")})
        
        bundle_data = {"token": main_token, "title": f"{upload_title} ({total_movies} movies)", "button_title": clean_movie_title_for_button(upload_title), "price": price * total_movies, "category": "series", "description": f"Collection of {total_movies} movies", "poster_url": posters[0].get("poster_url", ""), "episodes": episodes, "episode_count": len(episodes), "created_at": datetime.utcnow(), "downloads": 0, "is_confronter_bundle": True, "is_collection": True}
        try:
            series_bundles_collection.insert_one(bundle_data)
        except Exception as e:
            logger.error(f"Failed to save bundle: {e}")
    
    # Mirror to UPLOAD_TARGET_CHANNEL (no forward tag)
    if saved_movies and get_upload_target_channel():
        try:
            # Group by poster: each poster once then its video(s)
            # Here each poster maps to one video file with multiple title entries already expanded in saved_movies
            shared = poster_file_ids[0] if poster_file_ids and upload_target_include_posters() else None
            await auto_mirror_uploads_to_target(
                context.bot,
                saved_movies,
                shared_poster_file_id=shared,
                shared_poster_caption=upload_title,
                include_posters=upload_target_include_posters(),
            )
        except Exception as _mirror_err:
            logger.error(f"confronteruploaddone mirror: {_mirror_err}")

    if channel_to_post and bundle_data and saved_movies:
        await update.message.reply_text("📤 Posting to channel as album...")
        success = await post_to_channel_as_album_with_buttons(context=context, channel_id=channel_to_post, poster_file_ids=poster_file_ids, saved_movies=saved_movies[:9], main_token=main_token, price=price, total_movies=min(total_movies, 9), upload_title=upload_title)
        if success:
            await update.message.reply_text(f"✅ Posted to channel with {min(len(saved_movies), 9)} movie buttons!")
        else:
            await update.message.reply_text("⚠️ Failed to post to channel.")
    
    delete_upload_session(admin.id)
    context.user_data.pop("confronter_upload_mode", None)
    context.user_data.pop("confronter_upload_admin", None)
    
    summary = f"✅ <b>Upload Complete!</b>\n\nCollection: <b>{upload_title}</b>\nMovies: <b>{len(saved_movies)}</b>\nPrice: <b>{price} KES</b>\nBundle: <b>{price * total_movies} KES</b>\n\n<b>🔗 GET ALL ONCE:</b>\n<code>https://t.me/{context.bot.username}?start={main_token}</code>\n"
    if channel_to_post:
        summary += f"\n📢 Posted to: <b>{channel_to_post}</b>"
    await update.message.reply_text(summary, parse_mode=ParseMode.HTML)

async def post_to_channel_as_album_with_buttons(context, channel_id, poster_file_ids, saved_movies, main_token, price, total_movies, upload_title):
    try:
        try:
            await refresh_admin_premium_status(context.bot)
        except Exception:
            pass
        bot_link = f"https://t.me/{context.bot.username}?start={main_token}"
        price_text = f"{price * total_movies} KES" if price > 0 else "FREE"
        
        movie_buttons = []
        for i, movie in enumerate(saved_movies[:9]):
            title_display = clean_movie_title_for_button(movie.get('title', 'Unknown'))
            part_match = re.search(r'(\d+)$', title_display)
            if part_match:
                title_display = f"PRT {part_match.group(1)}"
            else:
                if len(title_display) > 16:
                    title_display = title_display[:14] + "…"
            movie_buttons.append(InlineKeyboardButton(title_display, callback_data=f"get_movie_{movie.get('token')}"))
        
        keyboard_rows = []
        for i in range(0, min(len(movie_buttons), 9), 3):
            row = movie_buttons[i:i+3]
            if row:
                keyboard_rows.append(row)
        
        keyboard_rows.append([InlineKeyboardButton(f"🎯 GET ALL {total_movies} ONCE — {price_text}", callback_data=f"get_movie_{main_token}")])
        keyboard_rows.append([InlineKeyboardButton("📢 Share Collection", url=f"https://t.me/share/url?url={bot_link}")])
        keyboard = InlineKeyboardMarkup(keyboard_rows)
        
        album_caption = apply_premium_emojis(f"<b>{escape_html(upload_title)}</b>\n\n📦 {total_movies} movies • 💰 {price_text}\n👇 Tap a movie below or GET ALL ONCE")
        
        if poster_file_ids:
            try:
                # Validate EVERY poster first — a stale file_id or dead URL must
                # never reach Telegram as a blank white album image.
                url_by_fid = {}
                for m in (saved_movies or []):
                    if m.get('poster_file_id') and m.get('poster_url'):
                        url_by_fid.setdefault(m['poster_file_id'], m['poster_url'])
                validated_media = []
                for fid in poster_file_ids:
                    try:
                        p = await resolve_safe_photo(context.bot, fid, url_by_fid.get(fid, ''), context="channel_album")
                    except Exception:
                        p = None
                    if p is not None and p not in validated_media:
                        validated_media.append(p)
                if not validated_media:
                    raise RuntimeError("no valid posters — all refs failed validation")
                # All posters, chunked at the 10-media album limit
                await send_album_chunked(
                    context.bot, channel_id, validated_media,
                    caption=album_caption, parse_mode=ParseMode.HTML,
                )
                # Buttons follow-up (media groups cannot carry reply_markup)
                try:
                    await context.bot.send_message(chat_id=channel_id, text=album_caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)
                except Exception as e_btn:
                    err = str(e_btn).lower()
                    if any(x in err for x in ('entity', 'parse', 'emoji')):
                        await context.bot.send_message(chat_id=channel_id, text=strip_premium_emojis(album_caption), reply_markup=keyboard, parse_mode=ParseMode.HTML)
                    else:
                        raise
                logger.info(f"Posted album with {len(validated_media)} validated images and {len(saved_movies[:9])} buttons!")
                return True
            except Exception as e:
                logger.error(f"Album send error: {e}")
                plain = strip_premium_emojis(album_caption)
                sent_one = False
                for fid in poster_file_ids:
                    try:
                        p = await resolve_safe_photo(context.bot, fid, url_by_fid.get(fid, ''), context="channel_album_fb")
                    except Exception:
                        p = None
                    if p is None:
                        continue
                    try:
                        caption = "" if sent_one else plain
                        await context.bot.send_photo(chat_id=channel_id, photo=p, caption=caption, parse_mode=ParseMode.HTML)
                        sent_one = True
                    except Exception as e2:
                        logger.error(f"Fallback photo send error: {e2}")
                try:
                    await context.bot.send_message(chat_id=channel_id, text=plain, reply_markup=keyboard, parse_mode=ParseMode.HTML)
                except Exception:
                    await context.bot.send_message(chat_id=channel_id, text=plain, reply_markup=keyboard)
                return True
        else:
            try:
                await context.bot.send_message(chat_id=channel_id, text=album_caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            except Exception:
                await context.bot.send_message(chat_id=channel_id, text=strip_premium_emojis(album_caption), reply_markup=keyboard, parse_mode=ParseMode.HTML)
            return True
            
    except Exception as e:
        logger.error(f"Channel post error: {e}")
        return False

async def cancelconfronter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    session = get_upload_session(admin.id)
    if session:
        delete_upload_session(admin.id)
        context.user_data.pop("confronter_upload_mode", None)
        context.user_data.pop("confronter_upload_admin", None)
        await update.message.reply_text("❌ Confronter upload cancelled.")
    else:
        await update.message.reply_text("ℹ️ No active session.")

# ============ CONFRONTER KING UPLOAD ============
admin_confronter_king_sessions = {}

def init_confronter_king_session(admin_id: int) -> dict:
    session = {"admin_id": admin_id, "poster_file_id": None, "poster_url": "", "poster_caption": "", "price": None, "prefix": "", "movies": [], "status": "awaiting_poster", "created_at": datetime.utcnow(), "channel_to_post": None, "total_parts": 0, "free_parts": 0}
    return session

async def confronterking_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    admin_confronter_king_sessions[admin.id] = init_confronter_king_session(admin.id)
    
    channels = []
    if PUBLIC_CHANNEL_USERNAME:
        channels.append({"name": f"📢 {PUBLIC_CHANNEL_USERNAME}", "id": PUBLIC_CHANNEL_USERNAME})
    if MY_CHANNEL:
        channels.append({"name": f"📢 {MY_CHANNEL}", "id": MY_CHANNEL})
    if CHANNEL_ID:
        channels.append({"name": f"📢 Channel ID: {CHANNEL_ID}", "id": str(CHANNEL_ID)})
    if channels_collection is not None:
        for ch in channels_collection.find():
            channels.append({"name": f"📢 {ch.get('title', 'Channel')}", "id": str(ch.get('channel_id'))})
    
    keyboard = []
    for ch in channels[:10]:
        keyboard.append([InlineKeyboardButton(ch["name"], callback_data=f"ck_channel_{ch['id']}")])
    keyboard.append([InlineKeyboardButton("💰 0 KES (FREE)", callback_data="ck_price_0")])
    keyboard.append([InlineKeyboardButton("💰 10 KES", callback_data="ck_price_10")])
    keyboard.append([InlineKeyboardButton("💰 20 KES", callback_data="ck_price_20")])
    keyboard.append([InlineKeyboardButton("💰 Send Your Price", callback_data="ck_price_custom")])
    keyboard.append([InlineKeyboardButton("💰 Default Price", callback_data="ck_price_default")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="ck_cancel")])
    
    await update.message.reply_text(
        f"👑 <b>Confronter King Upload</b>\n\n"
        f"<b>For Series with Parts</b>\n"
        f"• Thor 1-3 → Part 1, Part 2, Part 3\n"
        f"• Prison Break 1-42 → Part 1 (FREE), Part 2 (FREE)\n"
        f"• Breaking Bad 12 → Part 12\n\n"
        f"<b>FREE Parts:</b>\n"
        f"• Series 1-5: Part 1 is FREE\n"
        f"• Series 1-10+: Part 1 & 2 are FREE\n\n"
        f"<b>Get All Once Discount:</b>\n"
        f"• 10+ series: -20 KES\n"
        f"• 20+ series: -40 KES\n\n"
        f"<b>Step 1: Select Channel</b>\n"
        f"<b>Step 2: Select Price</b>\n"
        f"<b>Step 3: Choose Prefix</b>\n"
        f"<b>Step 4: Send Poster + Videos</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

async def handle_confronter_king_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return
    
    data = query.data
    if data == "ck_cancel":
        admin_confronter_king_sessions.pop(admin.id, None)
        await query.edit_message_text("❌ Cancelled.")
        return
    
    if data.startswith("ck_channel_"):
        channel = data.replace("ck_channel_", "")
        session = admin_confronter_king_sessions.get(admin.id)
        if not session:
            await query.edit_message_text("❌ No active session.")
            return
        
        session["channel_to_post"] = channel
        admin_confronter_king_sessions[admin.id] = session
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 0 KES (FREE)", callback_data="ck_price_0")],
            [InlineKeyboardButton("💰 10 KES", callback_data="ck_price_10")],
            [InlineKeyboardButton("💰 20 KES", callback_data="ck_price_20")],
            [InlineKeyboardButton("💰 Send Your Price", callback_data="ck_price_custom")],
            [InlineKeyboardButton("💰 Default Price", callback_data="ck_price_default")],
            [InlineKeyboardButton("❌ Cancel", callback_data="ck_cancel")]
        ])
        
        await query.edit_message_text("✅ Channel selected!\n\n<b>Step 2: Select Price</b>", reply_markup=keyboard, parse_mode=ParseMode.HTML)

async def handle_confronter_king_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return
    
    data = query.data
    if data == "ck_cancel":
        admin_confronter_king_sessions.pop(admin.id, None)
        await query.edit_message_text("❌ Cancelled.")
        return
    
    session = admin_confronter_king_sessions.get(admin.id)
    if not session:
        await query.edit_message_text("❌ No active session.")
        return
    
    if data == "ck_price_0":
        session["price"] = 0
        admin_confronter_king_sessions[admin.id] = session
        await query.edit_message_text(f"✅ Price set to <b>0 KES (FREE)</b>!\n\n<b>Step 3: Choose Prefix</b>\nType your prefix now.", parse_mode=ParseMode.HTML)
        session["status"] = "awaiting_prefix"
        return
    elif data == "ck_price_10":
        session["price"] = 10
        admin_confronter_king_sessions[admin.id] = session
        await query.edit_message_text(f"✅ Price set to <b>10 KES</b>!\n\n<b>Step 3: Choose Prefix</b>\nType your prefix now.", parse_mode=ParseMode.HTML)
        session["status"] = "awaiting_prefix"
        return
    elif data == "ck_price_20":
        session["price"] = 20
        admin_confronter_king_sessions[admin.id] = session
        await query.edit_message_text(f"✅ Price set to <b>20 KES</b>!\n\n<b>Step 3: Choose Prefix</b>\nType your prefix now.", parse_mode=ParseMode.HTML)
        session["status"] = "awaiting_prefix"
        return
    elif data == "ck_price_custom":
        session["status"] = "awaiting_price"
        admin_confronter_king_sessions[admin.id] = session
        await query.edit_message_text(f"✍️ <b>Send Your Price</b>\n\nType price in KES (e.g., <code>15</code>)", parse_mode=ParseMode.HTML)
        return
    elif data == "ck_price_default":
        session["price"] = get_default_price()
        admin_confronter_king_sessions[admin.id] = session
        await query.edit_message_text(f"✅ Price set to default: <b>{get_default_price()} KES</b>!\n\n<b>Step 3: Choose Prefix</b>\nType your prefix now.", parse_mode=ParseMode.HTML)
        session["status"] = "awaiting_prefix"
        return

async def handle_confronter_king_price_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    
    session = admin_confronter_king_sessions.get(admin.id)
    if not session:
        return
    
    if session.get("status") == "awaiting_price":
        text = update.message.text.strip()
        try:
            price = int(text)
            if price < 0:
                await update.message.reply_text("❌ Price cannot be negative.")
                return
            session["price"] = price
            admin_confronter_king_sessions[admin.id] = session
            await update.message.reply_text(f"✅ Price set to <b>{price} KES</b>!\n\n<b>Step 3: Choose Prefix</b>\nType your prefix now.", parse_mode=ParseMode.HTML)
            session["status"] = "awaiting_prefix"
        except ValueError:
            await update.message.reply_text("❌ Invalid price. Send a number.", parse_mode=ParseMode.HTML)
        return
    
    if session.get("status") == "awaiting_prefix":
        prefix = update.message.text.strip()
        if not prefix:
            await update.message.reply_text("❌ Send a valid prefix.")
            return
        
        session["prefix"] = prefix
        session["status"] = "awaiting_poster"
        admin_confronter_king_sessions[admin.id] = session
        
        await update.message.reply_text(f"✅ Prefix set to: <b>{prefix}</b>\n\n<b>Step 4: Send Poster</b>\nSend <b>poster photo</b> with caption.\nThen send videos with captions as titles.\n\nType <code>/confronterisundefeated</code> to finalize.", parse_mode=ParseMode.HTML)

async def handle_confronter_king_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    if context.user_data.get("double_confronter_active") or context.user_data.get("confronter_upload_mode") or context.user_data.get("upload_mode") or context.user_data.get("bulk_upload"):
        return
    session = admin_confronter_king_sessions.get(admin.id)
    if not session:
        return
    
    if session.get("status") == "awaiting_poster" and update.message.photo:
        photo = update.message.photo[-1]
        caption = update.message.caption or ""
        
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            file_bytes = io.BytesIO()
            await tg_file.download_to_memory(file_bytes)
            file_bytes.seek(0)
            poster_url = upload_image_with_fallback(file_bytes.read(), context="confronter_king_poster")
            if not poster_url:
                poster_url = ""
        except Exception as e:
            logger.error(f"Poster upload error: {e}")
            poster_url = ""
        
        session["poster_file_id"] = photo.file_id
        session["poster_url"] = poster_url
        session["poster_caption"] = caption
        session["status"] = "awaiting_videos"
        admin_confronter_king_sessions[admin.id] = session
        
        prefix = session.get("prefix", "")
        price_display = f"{session.get('price', get_default_price())} KES" if session.get('price', 0) > 0 else "FREE"
        button_title = clean_movie_title_for_button(caption)
        
        await update.message.reply_text(f"✅ Poster received!\n\nPrefix: <b>{prefix}</b>\nButton: <b>{button_title}</b>\nPrice: <b>{price_display}</b>\n\nSend videos with captions.\nType <code>/confronterisundefeated</code> to finalize.", parse_mode=ParseMode.HTML)
        return
    
    if session.get("status") == "awaiting_videos":
        await handle_confronter_king_video(update, context)

async def handle_confronter_king_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    if context.user_data.get("double_confronter_active") or context.user_data.get("confronter_upload_mode") or context.user_data.get("upload_mode") or context.user_data.get("bulk_upload"):
        return
    session = admin_confronter_king_sessions.get(admin.id)
    if not session:
        return
    
    if session.get("status") != "awaiting_videos":
        await update.message.reply_text("❌ Send poster first or use /confronterisundefeated.")
        return
    
    message = update.message
    file_id = None
    file_type = None
    caption = message.caption or ""
    
    if message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
    else:
        await update.message.reply_text("❌ Send a <b>video</b> or <b>document</b>.", parse_mode=ParseMode.HTML)
        return
    
    if not caption.strip():
        await update.message.reply_text("⚠️ No caption found. Video caption will be used as title.", parse_mode=ParseMode.HTML)
        return
    
    prefix = session.get("prefix", "")
    title = caption.strip()
    if prefix:
        title = f"{prefix} {title}"
    
    button_title = clean_movie_title_for_button(caption)
    
    movie_data = {"title": title, "button_title": button_title, "file_id": file_id, "file_type": file_type, "caption": caption, "prefix": prefix}
    
    session["movies"].append(movie_data)
    admin_confronter_king_sessions[admin.id] = session
    
    total = len(session["movies"])
    price_display = f"{session.get('price', get_default_price())} KES" if session.get('price', 0) > 0 else "FREE"
    
    await update.message.reply_text(f"✅ Movie {total} saved!\nTitle: <b>{title}</b>\nButton: <b>{button_title}</b>\nPrice: <b>{price_display}</b>\n\nSend more or type <code>/confronterisundefeated</code>", parse_mode=ParseMode.HTML)

# ============ HELPER FUNCTIONS FOR CONFRONTER KING ============
def extract_part_number(title: str) -> int:
    match = re.search(r'(\d+)$', title)
    if match:
        return int(match.group(1))
    match = re.search(r'[Pp]art\s*(\d+)', title)
    if match:
        return int(match.group(1))
    match = re.search(r'[Ee]pisode\s*(\d+)', title)
    if match:
        return int(match.group(1))
    match = re.search(r'S(\d+)', title)
    if match:
        return int(match.group(1))
    return 0

def is_part_free(part_number: int, total_parts: int) -> bool:
    if total_parts <= 5:
        return part_number == 1
    elif total_parts <= 10:
        return part_number == 1
    else:
        return part_number <= 2

def calculate_discount(total_parts: int, base_price: int) -> int:
    """Buy 5 get 1 free, buy 10 get 2 free, etc. Discount = free_count * unit price."""
    try:
        n = int(total_parts or 0)
        unit = int(base_price or 0)
    except Exception:
        return 0
    free_count = n // 5  # 5→1, 9→1, 10→2, 14→2, 15→3
    return max(0, free_count * unit)

async def post_series_to_channel_with_parts(context, channel_id, poster_file_id, saved_movies, main_token, get_all_price, total_parts, upload_title, price_per_part=None):
    try:
        bot_link = f"https://t.me/{context.bot.username}?start={main_token}"
        price_text = f"{get_all_price} KES" if get_all_price > 0 else "FREE 🎁"
        
        part_buttons = []
        for i, movie in enumerate(saved_movies, 1):
            # Prefer short PRT / free label so buttons fit well
            part_num = movie.get("part_number") or extract_part_number(movie.get("title", "") or "") or i
            # No gift emoji on buttons — free is already stated in caption
            clean_part = f"PRT {part_num}"
            if len(clean_part) > 16:
                clean_part = clean_part[:14] + "…"
            part_buttons.append(InlineKeyboardButton(clean_part, url=f"https://t.me/{context.bot.username}?start={movie.get('token')}"))
        
        keyboard_rows = []
        for i in range(0, len(part_buttons), 5):
            row = part_buttons[i:i+5]
            if row:
                keyboard_rows.append(row)
        
        discount_text = ""
        try:
            _unit = price_per_part
            if _unit is None:
                _unit = 10
            if len(saved_movies) >= 5:
                discount = calculate_discount(len(saved_movies), int(_unit) if _unit else 10)
                if discount > 0:
                    discount_text = f" (💰 -{discount} KES)"
        except Exception:
            pass
        
        keyboard_rows.append([InlineKeyboardButton(f"🎯 GET ALL {len(saved_movies)} ONCE — {price_text}{discount_text}", url=bot_link)])
        keyboard_rows.append([InlineKeyboardButton("📢 Share Collection", url=f"https://t.me/share/url?url={bot_link}")])
        keyboard = InlineKeyboardMarkup(keyboard_rows)

        # Free parts text (Part 1 free for <=10, Part 1&2 free for >10)
        n_parts = len(saved_movies)
        if n_parts > 10:
            free_txt = "🎁 Part 1 & 2 FREE — Watch free"
        elif n_parts >= 1:
            free_txt = "🎁 Part 1 FREE — Watch free"
        else:
            free_txt = ""
        # Unit price (each paid part)
        unit = price_per_part
        if unit is None:
            try:
                unit = int(round(float(get_all_price) / max(1, n_parts))) if get_all_price else 0
            except Exception:
                unit = 0
            # Prefer stored part price from first paid movie
            for m in saved_movies:
                if not m.get("is_free") and m.get("price"):
                    try:
                        unit = int(m.get("price"))
                        break
                    except Exception:
                        pass
        unit_txt = f"{unit} KES" if unit and unit > 0 else "FREE"
        caption = (
            f"📦 <b>{escape_html(upload_title)}</b> 🚀 Collection ({n_parts} parts)\n"
            f"{n_parts} parts | 💰 {price_text}\n"
            f"💵 Each part <b>{unit_txt}</b>"
        )
        if free_txt:
            caption += f"\n{free_txt}"
        
        # Premium animated emojis in channel post captions
        try:
            caption = apply_premium_emojis(caption)
        except Exception:
            pass
        if poster_file_id:
            try:
                await context.bot.send_photo(chat_id=channel_id, photo=poster_file_id, caption=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)
                logger.info(f"Posted series with {len(saved_movies)} part buttons!")
                return True
            except Exception as e:
                logger.error(f"Photo send error: {e}")
                plain = strip_premium_emojis(caption)
                try:
                    await context.bot.send_photo(chat_id=channel_id, photo=poster_file_id, caption=plain, reply_markup=keyboard, parse_mode=ParseMode.HTML)
                except Exception:
                    await context.bot.send_message(chat_id=channel_id, text=plain, reply_markup=keyboard, parse_mode=ParseMode.HTML)
                return True
        else:
            try:
                await context.bot.send_message(chat_id=channel_id, text=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            except Exception:
                await context.bot.send_message(chat_id=channel_id, text=strip_premium_emojis(caption), reply_markup=keyboard, parse_mode=ParseMode.HTML)
            return True
            
    except Exception as e:
        logger.error(f"Channel post error: {e}")
        return False

async def confronterisundefeated_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    session = admin_confronter_king_sessions.get(admin.id)
    if not session:
        await update.message.reply_text("❌ No active session.")
        return
    
    movies = session.get("movies", [])
    if not movies:
        await update.message.reply_text("❌ No movies uploaded.")
        return
    
    poster_file_id = session.get("poster_file_id")
    poster_url = session.get("poster_url", "")
    poster_caption = session.get("poster_caption", "")
    base_price = session.get("price", get_default_price())
    channel_to_post = session.get("channel_to_post")
    prefix = session.get("prefix", "")
    
    await reply_html_safe(update.message, add_footer(f"🚀 Finalizing {len(movies)} movies with part detection..."))
    
    for movie_data in movies:
        title = movie_data.get("title", "")
        part_number = extract_part_number(title)
        movie_data["part_number"] = part_number
    
    movies.sort(key=lambda x: x.get("part_number", 0))
    
    total_parts = len(movies)
    
    price_per_movie = base_price
    get_all_price = price_per_movie * total_parts
    
    discount = calculate_discount(total_parts, price_per_movie)
    get_all_price = max(0, get_all_price - discount)
    
    existing_tokens = [doc['token'] for doc in movies_collection.find({}, {'token': 1})]
    if series_bundles_collection is not None:
        existing_tokens.extend([doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})])
    
    saved_movies = []
    movie_links = []
    
    upload_title = f"{prefix} Collection" if prefix else "Movie Collection"
    main_token = generate_count_token()
    existing_tokens.append(main_token)
    
    for i, movie_data in enumerate(movies, 1):
        title = movie_data.get("title", "Unknown Movie")
        part_number = movie_data.get("part_number", i)
        button_title = clean_movie_title_for_button(title)
        file_id = movie_data.get("file_id")
        file_type = movie_data.get("file_type", "video")
        
        is_free = is_part_free(part_number, total_parts)
        part_price = 0 if is_free else price_per_movie
        
        part_display = f"Part {part_number}" if part_number > 0 else title
        
        individual_token = generate_count_token()
        existing_tokens.append(individual_token)
        
        movie_doc = {"token": individual_token, "bundle_token": main_token, "title": title, "button_title": button_title, "part_number": part_number, "part_display": part_display, "is_free": is_free, "description": f"Part {part_number} of {total_parts}", "price": part_price, "poster_url": poster_url or "", "poster_file_id": (session.get("poster_file_id") or ""), "poster_caption": poster_caption, "file_id": file_id, "file_type": file_type, "channel_id": None, "message_id": None, "private_link": "", "category": "movie", "downloads": 0, "created_at": datetime.utcnow(), "is_bundle_part": True, "bundle_parent": main_token, "collection_title": upload_title, "collection_total": total_parts, "collection_index": i}
        
        try:
            movies_collection.insert_one(movie_doc)
            saved_movies.append(movie_doc)
            movie_links.append(f"https://t.me/{context.bot.username}?start={individual_token}")
        except Exception as e:
            logger.error(f"Failed to save movie {title}: {e}")
        
        await asyncio.sleep(BOT_FAST_DELAY)
    
    bundle_data = None
    if series_bundles_collection is not None and len(saved_movies) > 0:
        episodes = []
        for movie in saved_movies:
            episodes.append({"private_link": f"https://t.me/{context.bot.username}?start={movie.get('token')}", "title": movie.get("title"), "button_title": movie.get("button_title", ""), "channel_id": "", "message_id": 0, "file_id": movie.get("file_id"), "file_type": movie.get("file_type", "video"), "token": movie.get("token")})
        
        bundle_data = {"token": main_token, "title": f"{upload_title} ({total_parts} parts)", "button_title": clean_movie_title_for_button(prefix or "Collection"), "price": get_all_price, "original_price": price_per_movie * total_parts, "discount": discount, "total_parts": total_parts, "free_parts": sum(1 for m in saved_movies if m.get("is_free", False)), "category": "series", "description": f"Collection of {total_parts} parts. {discount} KES discount applied!", "poster_url": poster_url, "episodes": episodes, "episode_count": len(episodes), "created_at": datetime.utcnow(), "downloads": 0, "is_confronter_bundle": True, "is_collection": True}
        try:
            series_bundles_collection.insert_one(bundle_data)
        except Exception as e:
            logger.error(f"Failed to save bundle: {e}")
    
    # Always mirror raw files to UPLOAD_TARGET_CHANNEL (no forward tag): 1 poster + all parts
    if saved_movies and get_upload_target_channel():
        try:
            await auto_mirror_uploads_to_target(
                context.bot,
                saved_movies,
                shared_poster_file_id=poster_file_id if upload_target_include_posters() else None,
                shared_poster_caption=upload_title,
                include_posters=upload_target_include_posters(),
            )
        except Exception as _mirror_err:
            logger.error(f"confronterking mirror: {_mirror_err}")

    if channel_to_post and bundle_data and saved_movies:
        await update.message.reply_text("📤 Posting to channel with part buttons...")
        success = await post_series_to_channel_with_parts(context=context, channel_id=channel_to_post, poster_file_id=poster_file_id, saved_movies=saved_movies, main_token=main_token, get_all_price=get_all_price, total_parts=total_parts, upload_title=upload_title, price_per_part=price_per_movie)
        if success:
            await update.message.reply_text(f"✅ Posted to channel with {len(saved_movies)} part buttons!")
        else:
            await update.message.reply_text("⚠️ Failed to post to channel.")
    
    admin_confronter_king_sessions.pop(admin.id, None)
    
    free_parts = sum(1 for m in saved_movies if m.get("is_free", False))
    summary = f"✅ <b>Upload Complete!</b>\n\nCollection: <b>{upload_title}</b>\nTotal Parts: <b>{len(saved_movies)}</b>\nFREE Parts: <b>{free_parts}</b> (Part 1{' & 2' if total_parts > 10 else ''})\nPrice per part: <b>{price_per_movie} KES</b>\nOriginal Total: <b>{price_per_movie * total_parts} KES</b>\n"
    if discount > 0:
        summary += f"💰 Discount: <b>-{discount} KES</b>\n"
    summary += f"💰 Get All Once: <b>{get_all_price} KES</b>\n\n<b>🎬 Parts:</b>\n"
    for i, movie in enumerate(saved_movies[:10], 1):
        price_display = "FREE 🎁" if movie.get("is_free", False) else f"{movie.get('price', price_per_movie)} KES"
        part_display = movie.get("part_display", f"Part {i}")
        summary += f"{i}. {part_display} — {price_display}\n"
    if len(saved_movies) > 10:
        summary += f"... and {len(saved_movies) - 10} more\n"
    summary += f"\n<b>🔗 GET ALL ONCE:</b>\n<code>https://t.me/{context.bot.username}?start={main_token}</code>\n"
    if channel_to_post:
        summary += f"\n📢 Posted to: <b>{channel_to_post}</b>"
    await update.message.reply_text(summary, parse_mode=ParseMode.HTML)

async def confronterkingcancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    session = admin_confronter_king_sessions.get(admin.id)
    if session:
        admin_confronter_king_sessions.pop(admin.id, None)
        await update.message.reply_text("❌ Confronter King upload cancelled.")
    else:
        await update.message.reply_text("ℹ️ No active session.")

# ============ SELECT BUNDLE WITH SEARCH ============
async def selectbundle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removed — use dashboard album/upload instead."""
    if update.effective_message:
        await update.effective_message.reply_text("This command was removed. Use the admin dashboard.")
    return
    # legacy disabled below

    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Show All Movies", callback_data="sel_show_all")],
        [InlineKeyboardButton("❌ Cancel", callback_data="sel_cancel")]
    ])
    
    await update.message.reply_text(
        f"🔍 <b>Select Movies for Bundle</b>\n\n"
        f"Type a <b>search query</b> to find movies (e.g., 'action', 'horror', 'movie title').\n"
        f"Or tap 'Show All Movies' to browse everything.\n\n"
        f"<b>Recent movies appear first.</b>",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )
    
    context.user_data["select_bundle_mode"] = "awaiting_search"
    context.user_data["select_bundle_session"] = {"step": "selecting", "selected_tokens": [], "selected_titles": [], "page": 0, "total": 0, "search_query": ""}

# ===== SELECT COLLECTION (alias for selectbundle with price buttons) =====
async def selectcollection_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text("This command was removed. Use the admin dashboard.")
    return

    """Alias for /selectbundle — same functionality with price selection."""
    await selectbundle_cmd(update, context)

async def handle_selectbundle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    
    if context.user_data.get("select_bundle_mode") != "awaiting_search":
        return
    
    text = update.message.text.strip()
    if not text or text.startswith('/'):
        return
    
    lines = text.split('\n')
    tokens = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('http'):
            match = re.search(r'[?&]start=([^&\s]+)', line)
            if match:
                tokens.append(match.group(1))
        else:
            tokens.append(line)
    
    if tokens:
        valid_tokens = []
        for token in tokens:
            movie = movies_collection.find_one({"token": token})
            if movie:
                valid_tokens.append(token)
        
        if valid_tokens:
            session = context.user_data.get("select_bundle_session", {})
            session["selected_tokens"] = valid_tokens
            session["selected_titles"] = [movies_collection.find_one({"t": t}).get("title", "Unknown") for t in valid_tokens]
            session["step"] = "details"
            context.user_data["select_bundle_session"] = session
            context.user_data["select_bundle_mode"] = None
            
            await show_select_bundle_details(update, context)
            return
    
    try:
        regex = _search_regex(text)
        movies = list(movies_collection.find({"title": regex}).sort("created_at", DESCENDING).limit(50))
    except Exception as e:
        logger.error(f"Search error: {e}")
        await update.message.reply_text(f"❌ Search failed. Please try again with a different query.", reply_markup=build_back_keyboard("back"))
        return
    
    if not movies:
        await update.message.reply_text(f"😢 No movies found for <b>\"{escape_html(text)}\"</b>\n\nTry a different search term or use <code>/selectbundle</code> again.", parse_mode=ParseMode.HTML, reply_markup=build_back_keyboard("back"))
        return
    
    session = context.user_data.get("select_bundle_session", {})
    session["search_results"] = movies
    session["search_query"] = text
    session["page"] = 0
    session["total"] = len(movies)
    context.user_data["select_bundle_session"] = session
    context.user_data["select_bundle_mode"] = "selecting"
    
    await show_select_bundle_page(update, context, 0)

async def show_select_bundle_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    per_page = 20
    session = context.user_data.get("select_bundle_session", {})
    search_results = session.get("search_results", [])
    total = len(search_results)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    
    if page >= total_pages:
        page = total_pages - 1
    if page < 0:
        page = 0
    
    start = page * per_page
    end = min(start + per_page, total)
    page_movies = search_results[start:end]
    
    selected = session.get("selected_tokens", [])
    search_query = session.get("search_query", "")
    
    keyboard = []
    
    if search_query:
        keyboard.append([InlineKeyboardButton(f"🔍 Results for: {search_query} ({total} total)", callback_data="sel_none")])
    
    for movie in page_movies:
        token = movie.get("token", "")
        title = movie.get("title", "Unknown")
        button_title = clean_movie_title_for_button(title)
        is_selected = token in selected
        status = "✅" if is_selected else "⬜"
        price = movie.get("price", 0)
        price_str = f"{price} KES" if price > 0 else "FREE"
        display_title = button_title[:30] + '...' if len(button_title) > 30 else button_title
        keyboard.append([InlineKeyboardButton(f"{status} {display_title} ({price_str})", callback_data=f"sel_toggle_{token}")])
    
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sel_page_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"sel_page_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)
    
    keyboard.append([InlineKeyboardButton(f"📊 Selected: {len(selected)} movies", callback_data="sel_none")])
    keyboard.append([InlineKeyboardButton("🔍 New Search", callback_data="sel_new_search")])
    keyboard.append([InlineKeyboardButton("✅ Done — Set Price & Poster", callback_data="sel_done")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="sel_cancel")])
    
    admin_id = update.effective_user.id
    items = []
    items.append({"label": f"Page {page + 1} of {total_pages}", "value": f"{total} movies"})
    items.append({"label": "", "value": ""})
    items.append({"label": "📋 Selected:", "value": f"{len(selected)}"})
    
    if selected:
        for token in selected[:5]:
            movie = movies_collection.find_one({"token": token})
            if movie:
                button_title = clean_movie_title_for_button(movie.get('title', 'Unknown'))[:30]
                items.append({"label": "▪︎", "value": button_title})
        if len(selected) > 5:
            items.append({"label": "...", "value": f"and {len(selected) - 5} more"})
    else:
        items.append({"label": "None selected yet.", "value": ""})
    
    text = render_menu_for_user(admin_id, "📦 SELECT MOVIES", items)
    text = add_footer(text)
    text += f"\n\n<i>Tap a movie to select/deselect. Select at least 2 movies to create a bundle.</i>"
    
    if update.callback_query:
        query = update.callback_query
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def show_select_bundle_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = context.user_data.get("select_bundle_session", {})
    selected = session.get("selected_tokens", [])
    selected_titles = session.get("selected_titles", [])
    
    if len(selected) < 2:
        await update.message.reply_text("❌ Please select at least 2 movies.", reply_markup=build_back_keyboard("back"))
        return
    
    title_list = "\n".join([f"▪︎ {t}" for t in selected_titles])
    
    total_price = 0
    for token in selected:
        movie = movies_collection.find_one({"token": token})
        if movie:
            total_price += movie.get("price", 0)
    
    # Show price selection buttons
    keyboard = [
        [InlineKeyboardButton("💰 10 KES", callback_data="sel_price_10"),
         InlineKeyboardButton("💰 20 KES", callback_data="sel_price_20")],
        [InlineKeyboardButton("💰 30 KES", callback_data="sel_price_30"),
         InlineKeyboardButton("💰 50 KES", callback_data="sel_price_50")],
        [InlineKeyboardButton("✍️ Send Your Price", callback_data="sel_price_custom")],
        [InlineKeyboardButton("❌ Cancel", callback_data="sel_cancel")]
    ]
    
    text = f"""📦 <b>Bundle Details</b>

<b>Selected Movies ({len(selected)}):</b>
{title_list}

Total individual price: <b>{total_price} KES</b>

Now select a price for the bundle:
• Tap a price button below
• Or send your own price via "Send Your Price"

After selecting a price, you'll be asked to send a poster."""
    
    text = add_footer(text)
    
    if update.callback_query:
        query = update.callback_query
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    
    session["step"] = "price_select"
    context.user_data["select_bundle_session"] = session
    context.user_data["select_bundle_mode"] = "awaiting_price"

async def handle_select_bundle_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return
    
    data = query.data
    session = context.user_data.get("select_bundle_session")
    if not session:
        await query.edit_message_text("❌ No active session.")
        return
    
    if data == "sel_price_10":
        session["price"] = 10
    elif data == "sel_price_20":
        session["price"] = 20
    elif data == "sel_price_30":
        session["price"] = 30
    elif data == "sel_price_50":
        session["price"] = 50
    elif data == "sel_price_custom":
        session["step"] = "awaiting_custom_price"
        context.user_data["select_bundle_session"] = session
        await query.edit_message_text("✍️ Send your custom price in KES (e.g., <code>25</code>)", parse_mode=ParseMode.HTML)
        return
    else:
        await query.answer("Invalid option.")
        return
    
    session["step"] = "awaiting_poster"
    context.user_data["select_bundle_session"] = session
    await query.edit_message_text(f"✅ Price set to <b>{session['price']} KES</b>!\n\nNow send a <b>poster photo</b> for the bundle (optional).\nType <code>/skipposter</code> to skip.\nAfter that, type <code>/donebyconfronter</code> to finalize.", parse_mode=ParseMode.HTML)

async def handle_select_bundle_price_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    
    session = context.user_data.get("select_bundle_session")
    if not session or session.get("step") != "awaiting_custom_price":
        return
    
    text = update.message.text.strip()
    try:
        price = int(text)
        if price < 0:
            await update.message.reply_text("❌ Price cannot be negative.")
            return
        session["price"] = price
        session["step"] = "awaiting_poster"
        context.user_data["select_bundle_session"] = session
        await update.message.reply_text(f"✅ Price set to <b>{price} KES</b>!\n\nNow send a <b>poster photo</b> for the bundle (optional).\nType <code>/skipposter</code> to skip.\nAfter that, type <code>/donebyconfronter</code> to finalize.", parse_mode=ParseMode.HTML)
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Send a number like <code>25</code>.", parse_mode=ParseMode.HTML)

async def handle_select_bundle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    
    session = context.user_data.get("select_bundle_session")
    if not session or session.get("step") != "awaiting_poster":
        return
    
    if update.message.photo:
        photo = update.message.photo[-1]
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            file_bytes = io.BytesIO()
            await tg_file.download_to_memory(file_bytes)
            file_bytes.seek(0)
            url = upload_image_with_fallback(file_bytes.read(), context="select_bundle_poster")
            if url:
                session["poster_url"] = url
                await reply_html_safe(update.message, add_footer("✅ Poster saved."))
            else:
                session["poster_url"] = None
                await reply_html_safe(update.message, add_footer("⚠️ Failed to upload poster. Skipping."))
        except Exception as e:
            logger.error(f"Select bundle poster upload error: {e}")
            session["poster_url"] = None
            await reply_html_safe(update.message, add_footer("⚠️ Poster upload failed. Skipping."))
        
        session["step"] = "done"
        context.user_data["select_bundle_session"] = session
        await update.message.reply_text(add_footer("�� Bundle ready. Type <code>/donebyconfronter</code> to create the bundle."), parse_mode=ParseMode.HTML)

async def skip_select_bundle_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    
    session = context.user_data.get("select_bundle_session")
    if not session or session.get("step") != "awaiting_poster":
        await reply_html_safe(update.message, add_footer("ℹ️ No active bundle session."))
        return
    
    session["poster_url"] = None
    session["step"] = "done"
    context.user_data["select_bundle_session"] = session
    await update.message.reply_text(add_footer("✅ Poster skipped. Type <code>/donebyconfronter</code> to create the bundle."), parse_mode=ParseMode.HTML)

async def donebyconfronter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    session = context.user_data.get("select_bundle_session")
    if not session or session.get("step") != "done":
        await reply_html_safe(update.message, add_footer("❌ No active bundle session or not ready. Use /selectbundle or /selectcollection first."), reply_markup=build_back_keyboard("back"))
        return

    tokens = session.get("selected_tokens", [])
    price = session.get("price", 0)
    poster_url = session.get("poster_url", "")
    title = session.get("title") or f"Bundle of {len(tokens)} movies"

    movies = []
    for token in tokens:
        movie = movies_collection.find_one({"token": token})
        if movie:
            movies.append(movie)

    if not movies:
        await reply_html_safe(update.message, add_footer("❌ No valid movies found."), reply_markup=build_back_keyboard("back"))
        context.user_data.pop("select_bundle_session")
        return

    episodes = []
    for movie in movies:
        episodes.append({"private_link": f"https://t.me/c/{abs(movie.get('channel_id', 0))}/{movie.get('message_id', '')}" if movie.get('channel_id') and movie.get('message_id') else "", "title": movie.get("title", "Unknown"), "button_title": clean_movie_title_for_button(movie.get("title", "Unknown")), "channel_id": str(movie.get("channel_id", "")), "message_id": int(movie.get("message_id", 0) or 0), "file_id": movie.get("file_id"), "file_type": movie.get("file_type")})

    existing_tokens = [doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})] if series_bundles_collection is not None else []
    bundle_token = generate_count_token()

    bundle_data = {"token": bundle_token, "title": title, "button_title": clean_movie_title_for_button(title), "price": price, "category": "series", "description": f"Bundle of {len(movies)} movies", "poster_url": poster_url or (movies[0].get('poster_url', '') if movies else ''), "episodes": episodes, "episode_count": len(episodes), "created_at": datetime.utcnow(), "downloads": 0}

    series_bundles_collection.insert_one(bundle_data)

    await update.message.reply_text(
        add_footer(f"""✅ Bundle created!

Title: {title}
Token: `{bundle_token}`
Movies: {len(movies)}
Total Price: {price} KES
Deep link: https://t.me/{context.bot.username}?start={bundle_token}"""),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 View Bundle", url=f"https://t.me/{context.bot.username}?start={bundle_token}")],
            [InlineKeyboardButton("📡 More Movies", callback_data="more_movies")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")]
        ])
    )
    context.user_data.pop("select_bundle_session")
    context.user_data.pop("select_bundle_mode", None)

async def handle_select_bundle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return
    
    session = context.user_data.get("select_bundle_session", {})
    
    if data == "sel_new_search":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="sel_cancel")]])
        await query.edit_message_text(f"🔍 <b>Search for Movies</b>\n\nType a movie name, genre, or keyword.\nExample: <code>Action</code> or <code>Furious</code>", reply_markup=keyboard, parse_mode=ParseMode.HTML)
        context.user_data["select_bundle_mode"] = "awaiting_search"
        return
    
    if data == "sel_show_all":
        all_movies = list(movies_collection.find().sort("created_at", DESCENDING).limit(50))
        session["search_results"] = all_movies
        session["search_query"] = "All Movies"
        session["page"] = 0
        session["total"] = len(all_movies)
        context.user_data["select_bundle_session"] = session
        context.user_data["select_bundle_mode"] = "selecting"
        await show_select_bundle_page(update, context, 0)
        return
    
    if data.startswith("sel_toggle_"):
        token = data.replace("sel_toggle_", "")
        selected = session.get("selected_tokens", [])
        titles = session.get("selected_titles", [])
        
        movie = movies_collection.find_one({"token": token})
        if not movie:
            await query.answer("Movie not found!", show_alert=True)
            return
        
        if token in selected:
            selected.remove(token)
            button_title = clean_movie_title_for_button(movie.get('title', 'Unknown'))
            if button_title in titles:
                titles.remove(button_title)
            await query.answer(f"Removed: {button_title}")
        else:
            selected.append(token)
            button_title = clean_movie_title_for_button(movie.get('title', 'Unknown'))
            titles.append(button_title)
            await query.answer(f"Added: {button_title}")
        
        session["selected_tokens"] = selected
        session["selected_titles"] = titles
        context.user_data["select_bundle_session"] = session
        
        page = session.get("page", 0)
        await show_select_bundle_page(update, context, page)
    
    elif data.startswith("sel_page_"):
        page = int(data.replace("sel_page_", ""))
        await show_select_bundle_page(update, context, page)
    
    elif data == "sel_done":
        selected = session.get("selected_tokens", [])
        if len(selected) < 2:
            await query.answer("Please select at least 2 movies!", show_alert=True)
            return
        
        await show_select_bundle_details(update, context)
    
    elif data == "sel_cancel":
        context.user_data.pop("select_bundle_session", None)
        context.user_data.pop("select_bundle_mode", None)
        await query.edit_message_text(add_footer("❌ Bundle creation cancelled."), reply_markup=build_back_keyboard("back"))
    
    elif data == "sel_none":
        await query.answer()
    
    # Price selection callbacks
    elif data.startswith("sel_price_"):
        await handle_select_bundle_price_callback(update, context)

async def handle_select_bundle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return

    if context.user_data.get("select_bundle_mode") == "awaiting_search":
        await handle_selectbundle_search(update, context)
        return
    
    # Handle custom price input
    if context.user_data.get("select_bundle_mode") == "awaiting_price":
        await handle_select_bundle_price_text(update, context)
        return

    session = context.user_data.get("select_bundle_session")
    if not session or session.get("step") != "details":
        return

    text = update.message.text.strip()

    if text.startswith("/"):
        return

    if "title" not in session:
        session["title"] = text
        context.user_data["select_bundle_session"] = session

        selected_tokens = session.get("selected_tokens", [])
        total_price = 0
        for token in selected_tokens:
            movie = movies_collection.find_one({"token": token})
            if movie:
                total_price += movie.get("price", 0)

        await update.message.reply_text(add_footer(f"""✅ Title set: <b>{text}</b>

💰 Now send the <b>total price</b> for this bundle (in KES).
Example: 150
Or type <code>/skip</code> to use total of individual prices: {total_price} KES."""), parse_mode=ParseMode.HTML)
        return

    if "price" not in session:
        if text.lower() == "/skip":
            selected_tokens = session.get("selected_tokens", [])
            total_price = 0
            for token in selected_tokens:
                movie = movies_collection.find_one({"token": token})
                if movie:
                    total_price += movie.get("price", 0)
            session["price"] = total_price
            context.user_data["select_bundle_session"] = session
            await update.message.reply_text(add_footer(f"✅ Using total of individual prices: {total_price} KES\n\nNow send a <b>poster photo</b> (optional) or /skipposter."), parse_mode=ParseMode.HTML)
            return

        try:
            price = int(text)
            if price < 0:
                await reply_html_safe(update.message, add_footer("❌ Price must be 0 or more."))
                return
            session["price"] = price
            context.user_data["select_bundle_session"] = session
            await update.message.reply_text(add_footer(f"✅ Price set to {price} KES\n\nNow send a <b>poster photo</b> (optional) or /skipposter."), parse_mode=ParseMode.HTML)
        except ValueError:
            await reply_html_safe(update.message, add_footer("❌ Invalid price. Please send a number."))

# ============ BUILD MOVIE BOX FOR USER ============
def build_movie_box_for_user(user_id: int, movie, idx, is_bundle, price_text, cat, width=50):
    items = []
    title_display = clean_movie_title_for_button(movie.get('title', 'Unknown'))
    items.append({"label": f"{idx}. {title_display}", "value": ""})
    if is_bundle:
        ep_count = movie.get('episode_count', len(movie.get('episodes', [])))
        items.append({"label": "📦 Movies:", "value": f"{ep_count} movies"})
    items.append({"label": "💰 Price:", "value": price_text})
    items.append({"label": "🏷️ Category:", "value": cat})
    
    title_text = "🎥 𝐌𝐨𝐯𝐢𝐞 𝐈𝐧𝐟𝐨𝐫𝐦𝐚𝐭𝐢𝐨𝐧" if not is_bundle else "📦 𝐁𝐮𝐧𝐝𝐥𝐞 𝐈𝐧𝐟𝐨𝐫𝐦𝐚𝐭𝐢𝐨𝐧"
    return render_menu_for_user(user_id, title_text, items)

def build_movie_title_only(user_id: int, movie, idx, is_bundle, width=40):
    title_text = f"{idx}. {clean_movie_title_for_button(movie.get('title', 'Unknown'))}"
    style = get_user_menu_style(user_id)
    return render_simple_title(title_text, style, width)

def render_simple_title(title: str, style: str = None, width: int = 40) -> str:
    if style is None:
        style = DEFAULT_MENU_STYLE
    
    if style == "confronter01":
        return "═" * width + "\n" + f"   {title}   \n" + "═" * width
    elif style == "confronter02":
        return f"『 {title.upper()} 』\n" + "─" * width
    elif style == "confronter03":
        return f"✦ {title} ✦\n" + "━" * width
    elif style == "confronter04":
        return f"»» {title} ««"
    elif style == "confronter05":
        return f"◇ {title} ◇\n" + "◆" * width
    elif style == "confronter06":
        return f"✨ {title} ✨\n" + "🌟" + "─" * (width - 4) + "🌟"
    elif style == "confronter07":
        return f"▸ {title} ◂\n" + "·" * width
    elif style == "confronter08":
        return f"█ {title} █\n" + "█" + "═" * (width - 2) + "█"
    else:
        return f"===== {title} ====="

# ============ DISPLAY MODE TOGGLES ============
def get_display_mode() -> str:
    """Return 'button' or 'poster'. Legacy button1/button2 map to button."""
    cached = _cache_get("display_mode", ttl=30)
    if cached is not None:
        return cached
    if not mongodb_connected or settings_collection is None:
        return 'button'
    try:
        setting = settings_collection.find_one({"key": "display_mode"})
        mode = setting.get('value', 'button') if setting else 'button'
        if mode in ('button1', 'button2'):
            mode = 'button'
        if mode not in ('button', 'poster'):
            mode = 'button'
    except Exception:
        mode = 'button'
    return _cache_set("display_mode", mode)

def set_display_mode(mode: str):
    if not mongodb_connected or settings_collection is None:
        return
    # Only two styles: "poster" and "button"
    if mode in ('button1', 'button2'):
        mode = 'button'
    if mode not in ('button', 'poster'):
        return
    settings_collection.update_one({"key": "display_mode"}, {"$set": {"value": mode, "updated_at": datetime.utcnow()}}, upsert=True)

def render_movie_list_button2(user_id: int, movies: list, page: int, total: int, title: str = "🎬 MOVIES") -> str:
    per = 20
    total_pages = ((total - 1) // per + 1) if total > 0 else 1
    items = []
    items.append({"label": f"{title}", "value": ""})
    items.append({"label": f"📊 Page {page + 1} of {total_pages}", "value": f"{total} movies"})
    items.append({"label": "👇 Tap a movie below", "value": ""})
    return render_menu_for_user(user_id, "🚀 MOVIE LIST", items)

async def send_movies_page_button2(update, context, movies, page, total, query_str="", is_search=False):
    user_id = update.effective_user.id
    bot_username = context.bot.username
    await human_action(context, user_id, ChatAction.TYPING)

    if is_search:
        title = f"🔍 Results for \"{query_str}\""
    else:
        title = "🎦 ᴀʟʟ ᴍᴏᴠɪᴇꜱ"
    
    is_bundle_grid = bool(movies and movies[0].get('is_bundle'))
    nav_cb = "browse_bundles_" if is_bundle_grid else "movies_page_"
    if is_bundle_grid and not is_search:
        title = "📦 ᴍᴏᴠɪᴇ ᴄᴏʟʟᴇᴄᴛɪᴏɴꜱ"
    movies_text = render_movie_list_button2(user_id, movies, page, total, title)
    movies_text = add_footer(movies_text)
    movies_text += get_datetime_footer()
    
    keyboard = []
    
    context.user_data["last_page_tokens"] = [m.get("token") for m in movies if m.get("token")]
    context.user_data["last_page_num"] = page
    for i, movie in enumerate(movies):
        token = movie.get('token', '')
        if not token:
            continue
        btn_text = short_btn_label(i + 1, movie.get('title', 'Movie'), max_len=32)
        # Short callback — long tokens break Telegram keyboards
        cb = f"gp_{page}_{i}" if len(f"get_movie_{token}") > 60 else f"get_movie_{token}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=cb)])
    
    nav_row = []
    per_page = 20
    total_pages = (total - 1) // per_page + 1 if total > 0 else 1
    
    if page > 0:
        if is_search:
            nav_row.append(InlineKeyboardButton("◀ ʙᴀᴄᴋ", callback_data=f"search_page_{page - 1}"))
        else:
            nav_row.append(InlineKeyboardButton("◀ ʙᴀᴄᴋ", callback_data=f"{nav_cb}{page - 1}"))
    
    if page < total_pages - 1:
        if is_search:
            nav_row.append(InlineKeyboardButton("ɴᴇxᴛ ▶", callback_data=f"search_page_{page + 1}"))
        else:
            nav_row.append(InlineKeyboardButton("ɴᴇxᴛ ▶", callback_data=f"{nav_cb}{page + 1}"))
    
    if nav_row:
        keyboard.append(nav_row)
    
    # View cart + Get All on this page
    cart_actions = []
    if get_cart_enabled():
        cart_count = len(get_user_cart(user_id))
        cart_actions.append(InlineKeyboardButton(f"🛒 View Cart & Pay ({cart_count})", callback_data="cart_view"))
    context.user_data["last_page_tokens"] = [m.get("token") for m in movies if m.get("token")]
    context.user_data["last_page_num"] = page
    if movies:
        _ga_n = len(movies)
        _ga_unit = int(movies[0].get("price") or get_default_price() or 10) if movies else 10
        _ga_sub = sum(int(m.get("price") or _ga_unit or 0) for m in movies)
        _ga_disc = calculate_discount(_ga_n, _ga_unit if all(int(m.get("price") or 0)==_ga_unit for m in movies) else (int(_ga_sub/_ga_n) if _ga_n else _ga_unit))
        # If mixed prices, free_count * cheapest-like unit via average is ok; prefer apply formula free_count * unit of first
        _ga_disc = calculate_discount(_ga_n, _ga_unit)
        _ga_pay = max(0, _ga_sub - _ga_disc)
        _ga_label = f"Get All {_ga_n} Once — {_ga_pay} KES"
        cart_actions.append(InlineKeyboardButton(_ga_label, callback_data=f"get_all_page_{page}"))
    if cart_actions:
        keyboard.append(cart_actions)

    # Skip to page
    total_pages_btn = (total - 1) // 20 + 1 if total > 0 else 1
    keyboard.append([InlineKeyboardButton(f"🔢 Skip to page (1-{total_pages_btn})", callback_data="skip_to_page")])

    action_row = []
    action_row.append(InlineKeyboardButton("🔍 Search", callback_data="search_prompt"))
    action_row.append(InlineKeyboardButton("More Movies", callback_data="more_movies"))
    action_row.append(InlineKeyboardButton("🏠 Home", callback_data="go_home"))
    keyboard.append(action_row)
    
    keyboard.append([InlineKeyboardButton("◀ Back", callback_data="back")])
    
    if ADDITIONAL_CHANNELS:
        keyboard.append([InlineKeyboardButton("➕ ᴀᴅᴅ ꜰʀᴏᴍ ᴄʜᴀɴɴᴇʟ", callback_data="visit_channels")])
    
    kb = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        query = update.callback_query
        try:
            await edit_html_safe(query.message, movies_text, reply_markup=kb)
        except Exception:
            try:
                await reply_html_safe(query.message, movies_text, reply_markup=kb)
            except Exception as e:
                logger.error(f"send_movies_page_button2: {e}")
    else:
        await reply_html_safe(update.message, movies_text, reply_markup=kb)

async def _send_poster_grid_impl(update, context, movies, page, total, query_str, is_search=False):
    # Always album style: 9 posters as album → numbered buttons + cart → next
    user_id = update.effective_user.id
    bot_username = context.bot.username
    per_page = 9
    await human_action(context, user_id, ChatAction.TYPING)

    if is_search:
        header_text = f"Results for \"{query_str}\"\nPage {page + 1} | {total} total"
    else:
        total_pages = (total - 1) // per_page + 1 if total > 0 else 1
        header_text = f"All Movies\nPage {page + 1} of {total_pages} | {total} total"

    header_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("More Movies", callback_data="more_movies")],
        [InlineKeyboardButton("Home", callback_data="go_home")]
    ])

    if update.callback_query:
        query = update.callback_query
        try:
            if query.message:
                await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=user_id, text=header_text, reply_markup=header_kb, parse_mode=None)
    else:
        await update.message.reply_text(header_text, reply_markup=header_kb, parse_mode=None)

    # ===== ALBUMS OF ALL VALID POSTERS (chunked at Telegram's 10-media limit) =====
    poster_refs2 = []
    movie_buttons = []
    cart_enabled = get_cart_enabled()
    for i, movie in enumerate(movies):
        price = movie.get('price', get_default_price())
        token = movie.get('token', '')
        title = movie.get('title', 'Unknown')
        poster_url = movie.get('poster_url', '') or ''
        poster_file_id = movie.get('poster_file_id') or movie.get('poster_tg_file_id') or ''
        short_title = clean_movie_title_for_button(title, max_len=14)
        ref = (poster_file_id, poster_url)
        if ref not in poster_refs2:
            poster_refs2.append(ref)
        movie_buttons.append(InlineKeyboardButton(f"{i+1}. {short_title}", callback_data=f"get_movie_{token}"))

    # Validate every poster so users never see blank white images
    media = []
    for fid, purl in poster_refs2:
        try:
            p = await resolve_safe_photo(context.bot, fid, purl, context="poster_grid2")
        except Exception:
            p = None
        if p is not None:
            media.append(p)

    if media:
        await send_album_chunked(
            context.bot, user_id, media,
            caption=build_album_caption(movies), parse_mode=ParseMode.HTML,
        )

    # Numbered buttons (3 per row) + cart row
    keyboard = []
    row = []
    for btn in movie_buttons:
        row.append(btn)
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    if cart_enabled and movies:
        cart_row = []
        for i, movie in enumerate(movies):
            tok = movie.get('token', '')
            cart_row.append(InlineKeyboardButton(f"Cart {i+1}", callback_data=f"cart_add_{tok}"))
            if len(cart_row) == 3:
                keyboard.append(cart_row)
                cart_row = []
        if cart_row:
            keyboard.append(cart_row)
        keyboard.append([
            InlineKeyboardButton("View Cart & Pay", callback_data="cart_view"),
            InlineKeyboardButton(f"Get All {len(movies)} Once — {max(0, len(movies)*int(get_default_price() or 10)-calculate_discount(len(movies), int(get_default_price() or 10)))} KES", callback_data=f"get_all_page_{page}")
        ])

    # Next / Prev after the album + buttons
    nav_row = []
    if page > 0:
        if is_search:
            nav_row.append(InlineKeyboardButton("Back", callback_data=f"search_page_{page - 1}"))
        else:
            nav_row.append(InlineKeyboardButton("Back", callback_data=f"movies_page_{page - 1}"))
    total_pages = (total - 1) // per_page + 1 if total > 0 else 1
    if is_search:
        if total > (page + 1) * per_page:
            nav_row.append(InlineKeyboardButton("Next", callback_data=f"search_page_{page + 1}"))
    else:
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next", callback_data=f"movies_page_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    # Skip to page (user request from screenshots)
    keyboard.append([InlineKeyboardButton("🔢 Skip to page", callback_data="skip_to_page")])

    keyboard.append([
        InlineKeyboardButton("🔍 Search", callback_data="search_prompt"),
        InlineKeyboardButton("📦 More Movies", callback_data="more_movies"),
        InlineKeyboardButton("🏠 Home", callback_data="go_home")
    ])

    await send_html_safe(
        context.bot, user_id,
        apply_premium_emojis("👇 Tap a button below"),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return  # done — skip old individual poster loop
    
    if ADDITIONAL_CHANNELS:
        nav_keyboard.append([InlineKeyboardButton(f"➕ ᴀᴅᴅ ꜰʀᴏᴍ ᴄʜᴀɴɴᴇʟ", callback_data="visit_channels")])

    await context.bot.send_message(chat_id=user_id, text=add_footer(f"ℹ️ <b>Navigation</b>"), reply_markup=InlineKeyboardMarkup(nav_keyboard), parse_mode=ParseMode.HTML)

# ============ BASE64 IMAGE HELPER ============
_base64_counter = 0

def is_base64_data_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith('data:')

def data_url_to_inputfile(data_url: str) -> InputFile:
    global _base64_counter
    header, _, encoded = data_url.partition(',')
    if not encoded:
        raise ValueError("Invalid data URL format")
    image_bytes = base64.b64decode(encoded)
    _base64_counter += 1
    return InputFile(io.BytesIO(image_bytes), filename=f"image_{_base64_counter}.jpg")


# ============ SAFE POSTER RESOLUTION (never send blank white images) ============
def _check_image_url(url, timeout=3):
    """Return True only if the URL really serves an image right now.
    Expired tmpfiles/litterbox/0x0 links and dead hosts return False."""
    try:
        resp = requests.get(url, headers={'User-Agent': _UA}, timeout=timeout, stream=True, allow_redirects=True)
        try:
            if resp.status_code != 200:
                return False
            ctype = (resp.headers.get('Content-Type') or '').lower()
            chunk = next(resp.iter_content(1024), b'')
            if len(chunk) <= 100:
                return False  # empty/error body
            # Sniff real image magic bytes — an HTML error page or text body can
            # never masquerade as a poster (this is what caused blank white images).
            head = chunk[:16]
            is_jpeg = head[:3] == b'\xff\xd8\xff'
            is_png = head[:8] == b'\x89PNG\r\n\x1a\n'
            is_gif = head[:6] in (b'GIF87a', b'GIF89a')
            is_webp = head[:4] == b'RIFF' and head[8:12] == b'WEBP'
            if 'image' in ctype:
                return True
            if 'octet-stream' in ctype or 'binary' in ctype:
                return bool(is_jpeg or is_png or is_gif or is_webp)
            # No/unknown content-type — trust only real image bytes
            return bool(is_jpeg or is_png or is_gif or is_webp)
        finally:
            resp.close()
    except Exception:
        return False


_VALIDATED_FILE_IDS = set()       # file_ids proven good (stable — cache forever)
_VALIDATED_URLS = {}              # url -> timestamp (URLs can expire — short TTL)
_VALIDATED_URL_TTL = 600          # 10 min


async def resolve_safe_photo(bot, file_id, url, context="poster"):
    """Return a photo ref Telegram can actually render — never a blank image.
    Order: validated file_id → live http URL → downloaded bytes → base64 → None (skip).
    None means 'do not include in album' — a missing slot beats a white nothing image."""
    import time as _t
    # 1) Telegram file_id — INSTANT path, zero API calls.
    # file_ids are permanent on Telegram's side; the old code did a getFile
    # round-trip per poster (9 posters = 9 sequential API calls = seconds of lag).
    # If a rare stale file_id fails, send_album_chunked retries per-item and
    # skips it — a missing slot always beats a slow menu.
    if file_id:
        _VALIDATED_FILE_IDS.add(file_id)
        return file_id
    # 2) http(s) poster URL — only if it serves a real image right now
    if url and str(url).startswith(('http://', 'https://')):
        u = str(url)
        ts = _VALIDATED_URLS.get(u)
        if ts is not None and (_t.time() - ts) < _VALIDATED_URL_TTL:
            return u
        try:
            ok = await asyncio.to_thread(_check_image_url, u)
        except Exception:
            ok = False
        if ok:
            _VALIDATED_URLS[u] = _t.time()
            return u
        logger.warning(f"{context}: dead poster URL skipped: {u[:80]}")
    # 3) base64 data-url last resort
    if url and is_base64_data_url(url):
        try:
            return data_url_to_inputfile(url)
        except Exception:
            return None
    return None


async def send_album_chunked(bot, chat_id, media, caption=None, parse_mode=None, chunk=10):
    """Send ALL posters as albums (Telegram caps albums at 10 media).
    Caption rides on the last item of the first album. If one album fails,
    posters retry one-by-one and only the broken item is skipped — the rest
    always reach the user, no matter how many images there are."""
    if not media:
        return False
    sent_any = False
    for start in range(0, len(media), chunk):
        part = media[start:start + chunk]
        group = []
        for i, ph in enumerate(part):
            if not sent_any and i == len(part) - 1 and caption:
                group.append(InputMediaPhoto(media=ph, caption=(caption or "")[:1024], parse_mode=parse_mode))
            else:
                group.append(InputMediaPhoto(media=ph))
        try:
            await bot.send_media_group(chat_id=chat_id, media=group)
            sent_any = True
        except Exception as exc:
            logger.warning(f"Album chunk failed ({exc}); retrying posters individually")
            for i, ph in enumerate(part):
                try:
                    if not sent_any and i == len(part) - 1 and caption:
                        await bot.send_photo(chat_id=chat_id, photo=ph, caption=(caption or "")[:1024], parse_mode=parse_mode)
                    else:
                        await bot.send_photo(chat_id=chat_id, photo=ph)
                    sent_any = True
                except Exception as e2:
                    logger.warning(f"Broken poster skipped (no blank sent): {e2}")
        try:
            await asyncio.sleep(BOT_FAST_DELAY)
        except Exception:
            pass
    return sent_any

# ============ ROBUST IMAGE UPLOAD HELPERS ============
# Goal: poster upload must almost NEVER fail for bot + dashboard.
# Strategy (priority):
#   1) Local disk served by Flask /uploads/  (best for dashboard — never expires)
#   2) Imgur (official API + uploadimgur-style anonymous)
#   3) Catbox / Telegraph / 0x0 / tmpfiles / litterbox / freeimage / imgbb / filegarden
#   4) data-URL last resort (still works for Telegram via InputFile)
CATBOX_API = "https://catbox.moe/user/api.php"
LITTERBOX_API = "https://litterbox.catbox.moe/resources/internals/api.php"
FILEGARDEN_API = "https://api.filegarden.com/upload"
ZEROX0_API = "https://0x0.st"
TMPFILES_API = "https://tmpfiles.org/api/v1/upload"
FREEIMAGE_API = "https://freeimage.host/api/1/upload"
IMGUR_API = "https://api.imgur.com/3/image"
SERVER_UPLOAD_DIR = os.environ.get('SERVER_UPLOAD_DIR', '/tmp/bot_uploads')
SERVER_UPLOAD_URL_BASE = os.environ.get('SERVER_UPLOAD_URL_BASE', '')
os.makedirs(SERVER_UPLOAD_DIR, exist_ok=True)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _normalize_image_bytes(image_data: bytes, max_side: int = 1280, max_bytes: int = 4_500_000) -> bytes:
    """Ensure JPEG, reasonable size. Falls back to original on any error."""
    if not image_data:
        return image_data
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_data))
        if img.mode in ("RGBA", "P", "LA"):
            bg = Image.new("RGB", img.size, (0, 0, 0))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        quality = 88
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()
        while len(data) > max_bytes and quality > 40:
            quality -= 12
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            data = out.getvalue()
        return data
    except Exception as e:
        logger.debug(f"normalize_image_bytes skipped: {e}")
        return image_data


def _try_telegraph_upload(image_data: bytes) -> str:
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            files = {'file': ('poster.jpg', io.BytesIO(image_data), 'image/jpeg')}
            resp = requests.post('https://telegra.ph/upload', files=files, headers={'User-Agent': _UA}, timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0 and 'src' in data[0]:
                    return f"https://telegra.ph{data[0]['src']}"
            logger.warning(f"Telegraph upload attempt {attempt+1} failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Telegraph upload error (attempt {attempt+1}): {e}")
        if attempt < max_attempts - 1:
            time_mod.sleep(0.2)
    return None


def _try_catbox_upload(image_data: bytes) -> str:
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            files = {'fileToUpload': ('poster.jpg', io.BytesIO(image_data), 'image/jpeg')}
            data = {'reqtype': 'fileupload'}
            resp = requests.post(CATBOX_API, data=data, files=files, headers={'User-Agent': _UA}, timeout=12)
            if resp.status_code == 200:
                text = resp.text.strip()
                if text.startswith('http'):
                    return text
                logger.warning(f"Catbox unexpected (attempt {attempt+1}): {text[:200]}")
            else:
                logger.warning(f"Catbox HTTP {resp.status_code} (attempt {attempt+1})")
        except Exception as e:
            logger.error(f"Catbox exception (attempt {attempt+1}): {e}")
        time_mod.sleep(0.2)
    return None


def _try_litterbox_upload(image_data: bytes) -> str:
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            files = {'fileToUpload': ('poster.jpg', io.BytesIO(image_data), 'image/jpeg')}
            data = {'reqtype': 'fileupload', 'time': '72h'}
            resp = requests.post(LITTERBOX_API, data=data, files=files, headers={'User-Agent': _UA}, timeout=12)
            if resp.status_code == 200 and resp.text.strip().startswith('http'):
                return resp.text.strip()
            logger.warning(f"Litterbox failed (attempt {attempt+1}): {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Litterbox error (attempt {attempt+1}): {e}")
        time_mod.sleep(0.2)
    return None


def _try_filegarden_upload(image_data: bytes) -> str:
    try:
        files = {'file': ('poster.jpg', io.BytesIO(image_data), 'image/jpeg')}
        resp = requests.post(FILEGARDEN_API, files=files, headers={'User-Agent': _UA}, timeout=12)
        if resp.status_code == 200 and resp.text.strip().startswith('http'):
            return resp.text.strip()
        logger.warning(f"FileGarden upload failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"FileGarden upload error: {e}")
    return None


def _try_0x0_upload(image_data: bytes) -> str:
    try:
        files = {'file': ('poster.jpg', io.BytesIO(image_data), 'image/jpeg')}
        resp = requests.post(ZEROX0_API, files=files, headers={'User-Agent': _UA}, timeout=12)
        if resp.status_code in (200, 201) and resp.text.strip().startswith('http'):
            return resp.text.strip().split()[0]
        logger.warning(f"0x0.st failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"0x0.st error: {e}")
    return None


def _try_tmpfiles_upload(image_data: bytes) -> str:
    try:
        files = {'file': ('poster.jpg', io.BytesIO(image_data), 'image/jpeg')}
        resp = requests.post(TMPFILES_API, files=files, headers={'User-Agent': _UA}, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            url = (data.get('data') or {}).get('url') or ''
            if url.startswith('http'):
                return url.replace('tmpfiles.org/', 'tmpfiles.org/dl/')
        logger.warning(f"tmpfiles failed: {getattr(resp, 'status_code', '?')} {getattr(resp, 'text', '')[:200]}")
    except Exception as e:
        logger.error(f"tmpfiles error: {e}")
    return None


def _try_freeimage_upload(image_data: bytes) -> str:
    """Anonymous freeimage.host upload (no key required for basic)."""
    try:
        files = {'source': ('poster.jpg', io.BytesIO(image_data), 'image/jpeg')}
        data = {'type': 'file', 'action': 'upload', 'timestamp': str(int(time_mod.time()))}
        resp = requests.post(FREEIMAGE_API, data=data, files=files, headers={'User-Agent': _UA}, timeout=12)
        if resp.status_code == 200:
            j = resp.json()
            url = (
                (j.get('image') or {}).get('url')
                or (j.get('image') or {}).get('display_url')
                or j.get('url')
                or ''
            )
            if isinstance(url, str) and url.startswith('http'):
                return url
        logger.warning(f"freeimage.host failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"freeimage.host error: {e}")
    return None


def _try_imgbb_upload(image_data: bytes) -> str:
    key = (os.environ.get('IMGBB_API_KEY') or '').strip()
    if not key:
        return None
    try:
        b64 = base64.b64encode(image_data).decode('ascii')
        resp = requests.post(
            'https://api.imgbb.com/1/upload',
            data={'key': key, 'image': b64},
            headers={'User-Agent': _UA},
            timeout=45,
        )
        if resp.status_code == 200:
            j = resp.json()
            url = ((j.get('data') or {}).get('url') or '')
            if url.startswith('http'):
                return url
        logger.warning(f"imgbb failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"imgbb error: {e}")
    return None


def _try_imgur_upload(image_data: bytes) -> str:
    """Upload to Imgur (same backend uploadimgur.com uses). Needs Client-ID.
    Set IMGUR_CLIENT_ID in .env (free at https://api.imgur.com/oauth2/addclient).
    Falls back to a few known public client IDs used by anonymous tools.
    """
    client_ids = []
    env_id = (os.environ.get('IMGUR_CLIENT_ID') or '').strip()
    if env_id:
        client_ids.append(env_id)
    # Public/anonymous client IDs commonly used by open-source tools (may rotate)
    client_ids.extend([
        '546c25a59c58ad7',  # widely used anonymous client
        '1bfd5819cdc1d952',
        '6c02bd90aef1243',
    ])
    seen = set()
    for cid in client_ids:
        if not cid or cid in seen:
            continue
        seen.add(cid)
        try:
            b64 = base64.b64encode(image_data).decode('ascii')
            resp = requests.post(
                IMGUR_API,
                data={'image': b64, 'type': 'base64'},
                headers={
                    'Authorization': f'Client-ID {cid}',
                    'User-Agent': _UA,
                },
                timeout=45,
            )
            if resp.status_code == 200:
                j = resp.json()
                link = ((j.get('data') or {}).get('link') or '')
                if isinstance(link, str) and link.startswith('http'):
                    return link
            logger.warning(f"Imgur client {cid[:8]}… HTTP {resp.status_code}: {resp.text[:160]}")
        except Exception as e:
            logger.error(f"Imgur upload error ({cid[:8]}…): {e}")
    return None


def _try_pixeldrain_upload(image_data: bytes) -> str:
    """pixeldrain.com anonymous file upload — returns direct download link."""
    try:
        files = {'file': ('poster.jpg', io.BytesIO(image_data), 'image/jpeg')}
        resp = requests.post(
            'https://pixeldrain.com/api/file',
            files=files,
            headers={'User-Agent': _UA},
            timeout=60,
        )
        if resp.status_code in (200, 201):
            j = resp.json() if resp.headers.get('content-type', '').startswith('application/json') else {}
            fid = j.get('id') or j.get('file_id') or ''
            if fid:
                return f"https://pixeldrain.com/api/file/{fid}"
            # sometimes returns plain id
            text = resp.text.strip()
            if text and len(text) < 40 and ' ' not in text and not text.startswith('{'):
                return f"https://pixeldrain.com/api/file/{text}"
        logger.warning(f"pixeldrain failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"pixeldrain error: {e}")
    return None


def _save_to_server(image_data: bytes, filename: str = None) -> str:
    try:
        if not filename:
            filename = f"poster_{secrets.token_hex(8)}.jpg"
        filename = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
        if not filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
            filename += '.jpg'
        filepath = os.path.join(SERVER_UPLOAD_DIR, filename)
        with open(filepath, 'wb') as f:
            f.write(image_data)
        logger.info(f"Server fallback: saved image to {filepath}")
        base = (SERVER_UPLOAD_URL_BASE or WEBHOOK_URL or '').strip()
        if base:
            return f"{base.rstrip('/')}/uploads/{filename}"
        return f"/uploads/{filename}"
    except Exception as e:
        logger.error(f"Server fallback save error: {e}")
        return None


def _bytes_to_data_url(image_data: bytes) -> str:
    """Last-resort permanent (DB-stored) representation. Telegram accepts data URLs via InputFile conversion."""
    try:
        if len(image_data) > 2_500_000:
            image_data = _normalize_image_bytes(image_data, max_side=900, max_bytes=1_800_000)
        b64 = base64.b64encode(image_data).decode('ascii')
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        logger.error(f"data URL build failed: {e}")
        return None


def _absolute_upload_url(url: str) -> str:
    """Turn /uploads/x.jpg into full https URL when WEBHOOK_URL is known."""
    if not url:
        return url
    if str(url).startswith('http'):
        return url
    if str(url).startswith('/'):
        base = (SERVER_UPLOAD_URL_BASE or WEBHOOK_URL or '').strip()
        if base:
            return f"{base.rstrip('/')}{url}"
    return url


def upload_image_with_fallback(image_data: bytes or str, context: str = "") -> str:
    """Upload image bytes (or data-URL / existing http URL). Never raises.
    Prefer permanent local /uploads URL when possible so dashboard posters never break.
    """
    if isinstance(image_data, str) and image_data.startswith('http'):
        return image_data

    raw_bytes = image_data
    if isinstance(image_data, str) and image_data.startswith('data:'):
        try:
            _, _, encoded = image_data.partition(',')
            if encoded:
                raw_bytes = base64.b64decode(encoded)
        except Exception as e:
            logger.error(f"Base64 decode error: {e}")
            return None

    if not isinstance(raw_bytes, bytes) or not raw_bytes:
        return None

    try:
        raw_bytes = _normalize_image_bytes(raw_bytes)
    except Exception:
        pass

    # 1) LOCAL FIRST — return immediately (fast uploads). External hosts only if local fails.
    try:
        local_url = _save_to_server(raw_bytes)
        abs_local = _absolute_upload_url(local_url) if local_url else None
        if abs_local and str(abs_local).startswith('http'):
            logger.info(f"Poster saved locally ({context}): {abs_local}")
            return abs_local
        if local_url:
            # Relative /uploads path — good for same-origin dashboard
            logger.info(f"Poster saved locally relative ({context}): {local_url}")
            # One quick public host only (short timeout) so Telegram can fetch if needed
            for uploader in (_try_telegraph_upload, _try_imgur_upload):
                try:
                    url = uploader(raw_bytes)
                    if url and str(url).startswith('http'):
                        logger.info(f"Poster hosted via {uploader.__name__} ({context})")
                        return url
                except Exception:
                    continue
            return local_url
    except Exception as e:
        logger.error(f"local save path failed: {e}")

    # 2) External hosts — only when local save failed (max a few, short timeouts inside helpers)
    for uploader in (
        _try_telegraph_upload,
        _try_imgur_upload,
        _try_catbox_upload,
        _try_pixeldrain_upload,
        _try_0x0_upload,
    ):
        try:
            url = uploader(raw_bytes)
            if url and str(url).startswith('http'):
                logger.info(f"Poster hosted via {uploader.__name__} ({context})")
                return url
        except Exception as e:
            logger.warning(f"{uploader.__name__} crashed: {e}")

    # 3) data-URL last resort (still works via file_id for bot sends)
    data_url = _bytes_to_data_url(raw_bytes)
    if data_url:
        logger.warning(f"Using data-URL fallback ({context}), size={len(raw_bytes)}")
        return data_url

    logger.error(f"All upload attempts failed for image in context: {context}")
    return None


def upload_poster_with_fallback(poster_url: str, context: str = "") -> str:
    if not poster_url:
        return None
    if isinstance(poster_url, str) and poster_url.startswith('http'):
        return poster_url
    if is_base64_data_url(poster_url):
        try:
            header, _, encoded = poster_url.partition(',')
            if encoded:
                image_bytes = base64.b64decode(encoded)
                hosted_url = upload_image_with_fallback(image_bytes, context=context or "poster_conversion")
                if hosted_url:
                    return hosted_url
        except Exception as e:
            logger.error(f"Poster conversion error: {e}")
        return None
    return None


def upload_image_to_catbox(image_data: bytes or str) -> str:
    return upload_image_with_fallback(image_data, context="catbox_compat")


def upload_poster_to_catbox(poster_url: str) -> str:
    return upload_poster_with_fallback(poster_url, context="catbox_compat")


# ============ NOTIFY SUBSCRIPTION ============
async def notify_subscription_activated(user_id: int, plan: str, context: ContextTypes.DEFAULT_TYPE = None):
    plans = get_subscription_plans()
    plan_info = plans.get(plan, {})
    plan_name = plan_info.get("name", plan.title() + " Pass")
    days = plan_info.get("days", 7)
    max_movies = plan_info.get("max_movies", 0)
    expires = datetime.utcnow() + timedelta(days=days)
    try:
        items = [
            {"label": "✅ Payment", "value": "Confirmed"},
            {"label": "👑 Plan", "value": plan_name},
            {"label": "📅 Expires", "value": expires.strftime('%d %b %Y %H:%M')},
            {"label": "🎬 Movie limit", "value": str(max_movies) if max_movies else "Unlimited"},
            {"label": "", "value": ""},
            {"label": "🎉 Access is active until expiry", "value": ""},
            {"label": "Use /start to browse movies", "value": ""},
        ]
        text = add_footer(render_menu_for_user(user_id, f"🎉 {plan_name.upper()} ACTIVATED", items))
        await application.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Browse Movies", callback_data="watch_all_movies_0")],
                [InlineKeyboardButton("◀ Back", callback_data="back")]
            ]),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Subscription notify error: {e}")

# ============ CHECK SUBSCRIPTION COMMAND ============
async def checksubscription_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check if user has an active subscription — uses menu styles + emojis."""
    user = update.effective_user
    user_id = user.id
    has_sub, sub_type, expiry, used, max_movies = has_active_subscription(user_id)
    
    if has_sub:
        expiry_str = expiry.strftime('%d %b %Y %H:%M') if expiry else 'Never'
        remaining = max(0, (max_movies or 0) - (used or 0)) if max_movies else "∞"
        items = [
            {"label": "✅ Status", "value": "ACTIVE"},
            {"label": "👑 Plan", "value": f"{(sub_type or 'Pass').title()} Pass"},
            {"label": "📅 Expires", "value": expiry_str},
        ]
        if get_show_watched_remaining():
            items.append({"label": "Watched", "value": str(used or 0)})
            items.append({"label": "Remaining", "value": str(remaining)})
        text = render_menu_for_user(user_id, "SUBSCRIPTION FOR MOVIES", items)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Browse Movies", callback_data="watch_all_movies_0")],
            [InlineKeyboardButton("📦 More Movies", callback_data="more_movies")],
            [InlineKeyboardButton("◀ Back", callback_data="back")]
        ])
    else:
        plans = get_subscription_plans()
        items = [
            {"label": "❌ Status", "value": "No active plan"},
            {"label": "", "value": ""},
            {"label": "📋 Available Plans", "value": ""},
            {"label": "🗓 Weekly", "value": f"{plans.get('weekly', {}).get('price', 100)} KES"},
            {"label": "📅 2-Week", "value": f"{plans.get('2week', {}).get('price', 750)} KES"},
            {"label": "📆 3-Week", "value": f"{plans.get('3week', {}).get('price', 900)} KES"},
            {"label": "🗓 Monthly", "value": f"{plans.get('monthly', {}).get('price', 1100)} KES"},
        ]
        text = render_menu_for_user(user_id, "📊 SUBSCRIPTION", items)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👑 Subscribe Now", callback_data="show_plans")],
            [InlineKeyboardButton("🎬 Browse Movies", callback_data="watch_all_movies_0")],
            [InlineKeyboardButton("◀ Back", callback_data="back")]
        ])
    
    text = add_footer(text)
    if update.message:
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    elif update.callback_query:
        await edit_or_send_message(update.callback_query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

# ============ WEBHOOK HANDLER ============
async def _handle_webhook_payment_success(reference: str, user_id: int, chat_id: int,
                                          movie_token: str, is_subscription: bool,
                                          plan: str, amount_kobo: int, channel: str,
                                          series_token: str = None, cart_tokens: list = None):
    clear_pending_payment_tracker(user_id)
    payment_method = 'mpesa' if 'mobile_money' in str(channel).lower() else 'card'
    amount_kes = amount_kobo / 100
    logger.info(f"💰 WEBHOOK: ref={reference}, user={user_id}, method={payment_method}, sub={is_subscription}, movie={movie_token}, series={series_token}")
    if payments_collection is not None:
        payments_collection.update_one({"reference": reference}, {"$set": {"status": "success", "paid_at": datetime.utcnow(), "payment_method": payment_method}}, upsert=True)
    # Ban unlock payment
    if plan == "ban_unlock" or (reference or "").startswith("banunlock_"):
        try:
            apply_unban(user_id)
            try:
                await notify_user_access_change(user_id, "unban")
            except Exception:
                pass
            logger.info(f"Ban unlocked via payment for user {user_id}")
        except Exception as e:
            logger.error(f"ban unlock after pay: {e}")
        return
    if is_subscription and plan:
        create_subscription(user_id, plan, reference)
        try:
            await notify_subscription_activated(user_id, plan)
        except Exception as e:
            logger.error(f"❌ WEBHOOK: Sub notify error: {e}")
    elif series_token:
        logger.info(f"🚀 WEBHOOK: Triggering bundle delivery for series_token={series_token}")
        asyncio.create_task(auto_deliver_series(user_id, series_token))
    elif movie_token:
        logger.info(f"🚀 WEBHOOK: Triggering auto-delivery for movie_token={movie_token}")
        asyncio.create_task(auto_deliver_movie(user_id, movie_token))
    elif cart_tokens:
        logger.info(f"🚀 WEBHOOK: Triggering cart delivery for {len(cart_tokens)} movies")
        cart_items_for_delivery = [{"token": t} for t in cart_tokens]
        asyncio.create_task(auto_deliver_cart(user_id, cart_items_for_delivery))
    else:
        payment_record = payments_collection.find_one({"reference": reference}) if payments_collection is not None else None
        cart_tokens_from_db = payment_record.get("cart_tokens", []) if payment_record else []
        if cart_tokens_from_db:
            logger.info(f"🚀 WEBHOOK: Triggering cart delivery for {len(cart_tokens_from_db)} movies")
            cart_items_for_delivery = [{"token": t} for t in cart_tokens_from_db]
            asyncio.create_task(auto_deliver_cart(user_id, cart_items_for_delivery))
        else:
            logger.warning(f"⚠️ WEBHOOK: No content token found for ref={reference}")

# ============ PAYHERO CALLBACK PROCESSOR ============
async def process_payhero_callback(callback_data: dict):
    await process_payhero_callback_for_pending(callback_data)
    response = callback_data.get("response", {})
    if not response and callback_data:
        response = callback_data
    status = str(response.get("Status", "")).lower()
    external_ref = response.get("ExternalReference", "")
    amount = response.get("Amount", 0)
    mpesa_receipt = response.get("MpesaReceiptNumber", "")
    phone = response.get("Phone", "")
    result_code = response.get("ResultCode", -1)
    logger.info(f"💳 PayHero callback: ref={external_ref}, status={status}, amount={amount}, receipt={mpesa_receipt}")
    if status != "success" and result_code != 0:
        logger.warning(f"PayHero payment failed: ref={external_ref}, result_code={result_code}")
        if payments_collection is not None:
            payments_collection.update_one({"reference": external_ref}, {"$set": {"status": "failed", "payhero_result_code": result_code, "payhero_result_desc": response.get("ResultDesc", ""), "updated_at": datetime.utcnow()}}, upsert=True)
        await send_payment_failure_warning(external_ref, user_id=None, chat_id=None)
        return
    if pending_payhero_collection is None:
        logger.error("PayHero: pending_payhero_collection not available")
        return
    pending = pending_payhero_collection.find_one({"payhero_ref": external_ref})
    if not pending:
        pending = pending_payhero_collection.find_one({"reference": external_ref})
    if not pending:
        logger.warning(f"PayHero: No pending payment found for ref={external_ref}")
        return
    user_id = pending.get("user_id")
    chat_id = pending.get("chat_id", user_id)
    movie_token = pending.get("movie_token")
    series_token = pending.get("series_token")
    is_subscription = pending.get("is_subscription", False)
    plan = pending.get("plan")
    cart_tokens = pending.get("cart_tokens", [])
    is_cart = pending.get("is_cart", False)
    logger.info(f"🚀 PayHero: Processing delivery for user={user_id}, movie={movie_token}, series={series_token}, cart={is_cart}")
    if payments_collection is not None:
        payments_collection.update_one({"reference": pending.get('reference', external_ref)}, {"$set": {"status": "success", "paid_at": datetime.utcnow(), "payment_method": "payhero", "mpesa_receipt": mpesa_receipt, "phone": phone, "amount": amount}}, upsert=True)
    clear_pending_payment_tracker(user_id)
    pending_payhero_collection.update_one({"_id": pending["_id"]}, {"$set": {"status": "completed", "completed_at": datetime.utcnow(), "mpesa_receipt": mpesa_receipt}})
    if is_subscription and plan:
        create_subscription(user_id, plan, external_ref)
        plan_info = get_subscription_plans().get(plan, {})
        plan_name = plan_info.get("name", plan.title() + " Pass")
        days = plan_info.get("days", 7)
        expires = datetime.utcnow() + timedelta(days=days)
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"""🎉 {plan_name} Activated!

✅ Payment of {amount:.0f} KES confirmed via M-Pesa
📱 Receipt: {mpesa_receipt}
📅 Expires: {expires.strftime('%d %b %Y %H:%M')}

You now have access until the expiry date.
Use /start to browse movies.""",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Browse Movies", callback_data="watch_all_movies_0")],
                    [InlineKeyboardButton("Back", callback_data="back")]
                ]),
                parse_mode=None
            )
        except Exception as e:
            logger.error(f"PayHero subscription notify error: {e}")
    elif is_cart or cart_tokens:
        if not cart_tokens and pending.get("reference"):
            payment_record = payments_collection.find_one({"reference": pending.get("reference")}) if payments_collection is not None else None
            if payment_record:
                cart_tokens = payment_record.get("cart_tokens", [])
        if cart_tokens:
            cart_items_for_delivery = [{"token": t} for t in cart_tokens]
            asyncio.create_task(auto_deliver_cart(user_id, cart_items_for_delivery))
        else:
            cart_doc = cart_collection.find_one({"user_id": user_id}) if cart_collection is not None else None
            if cart_doc:
                cart_items = cart_doc.get("items", [])
                asyncio.create_task(auto_deliver_cart(user_id, cart_items))
            else:
                await application.bot.send_message(chat_id=chat_id, text=add_footer(f"✅ Payment of {amount:.0f} KES confirmed! But cart is empty. Contact admin."), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🌼 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")], [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]]))
    elif series_token:
        asyncio.create_task(auto_deliver_series(user_id, series_token))
    elif movie_token:
        asyncio.create_task(auto_deliver_movie(user_id, movie_token))
    else:
        # Last chance: check payment record for subscription or content
        recovered = False
        try:
            payment_record = payments_collection.find_one({"reference": pending.get("reference") or external_ref}) if payments_collection is not None else None
            if payment_record:
                if payment_record.get("is_subscription") or payment_record.get("type") == "subscription":
                    plan = payment_record.get("plan") or plan
                    if plan:
                        create_subscription(user_id, plan, external_ref)
                        plan_info = get_subscription_plans().get(plan, {})
                        plan_name = plan_info.get("name", plan.title() + " Pass")
                        days = plan_info.get("days", 7)
                        expires = datetime.utcnow() + timedelta(days=days)
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=f"""🎉 {plan_name} Activated!

✅ Payment of {amount:.0f} KES confirmed
📅 Expires: {expires.strftime('%d %b %Y %H:%M')}

Use /start to browse movies.""",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("Browse Movies", callback_data="watch_all_movies_0")],
                                [InlineKeyboardButton("Back", callback_data="back")]
                            ]),
                            parse_mode=None
                        )
                        recovered = True
                elif payment_record.get("movie_token"):
                    asyncio.create_task(auto_deliver_movie(user_id, payment_record["movie_token"]))
                    recovered = True
                elif payment_record.get("series_token"):
                    asyncio.create_task(auto_deliver_series(user_id, payment_record["series_token"]))
                    recovered = True
                elif payment_record.get("cart_tokens"):
                    asyncio.create_task(auto_deliver_cart(user_id, [{"token": t} for t in payment_record["cart_tokens"]]))
                    recovered = True
        except Exception as e:
            logger.error(f"Recovery after missing content: {e}")
        if not recovered:
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"""✅ Payment of {amount:.0f} KES confirmed!
📱 Receipt: {mpesa_receipt}

Your payment was received. If content did not arrive, contact admin.""",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("More Movies", callback_data="more_movies")],
                        [InlineKeyboardButton("Back", callback_data="back")]
                    ]),
                    parse_mode=None
                )
            except Exception as e:
                logger.error(f"PayHero generic notify error: {e}")

# ============ MESSAGE FORWARDING TO ADMIN ============
async def forward_user_message_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.effective_user
        message = update.effective_message
        if not message or not user:
            return
        if message.text and message.text.startswith('/'):
            return
        if user.id in ADMIN_IDS:
            return
        if context.user_data.get("phone_input_handled"):
            return
        if context.user_data.get("awaiting_search"):
            return
        if pending_stk_collection is not None:
            pending = pending_stk_collection.find_one({"user_id": user.id, "status": "awaiting_phone"})
            if pending:
                return
        if pending_payhero_collection is not None:
            pending_ph = pending_payhero_collection.find_one({"user_id": user.id, "status": "awaiting_phone"})
            if pending_ph:
                return
        asyncio.create_task(asyncio.to_thread(track_user, user.id, user.username, user.first_name, user.last_name))
        await human_action(context, update.effective_chat.id, ChatAction.TYPING)
        message_doc = {"user_id": user.id, "username": user.username, "first_name": user.first_name, "text": message.text or (message.caption or ""), "message_type": message.content_type if hasattr(message, 'content_type') else 'text', "admin_replied": False, "created_at": datetime.utcnow()}
        msg_db_id = None
        if messages_collection is not None:
            try:
                result = messages_collection.insert_one(message_doc)
                msg_db_id = str(result.inserted_id)
            except Exception as e:
                logger.error(f"Message store error: {e}")
        user_info = f"👤 <b>{escape_html(user.first_name or 'Unknown')}</b>"
        if user.username:
            user_info += f" (@{escape_html(user.username)})"
        user_info += f"\n🏷️ <code>{user.id}</code>"
        forward_text = add_footer(f"📨 <b>New Message from User</b>\n\n{user_info}\n\n")
        if message.text:
            forward_text += f"💬 {escape_html(message.text)}"
        elif message.caption:
            forward_text += f"💬 {escape_html(message.caption)}"
        else:
            content_type = message.content_type if hasattr(message, 'content_type') else 'media'
            forward_text += f"💬 <i>({content_type} message)</i>"
        forward_text += f"\n\n<i>Reply to this message to respond directly to the user. Your reply will be forwarded instantly.</i>"
        try:
            sent_msg = await application.bot.send_message(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0, text=forward_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✏️ Reply", callback_data=f"reply_{user.id}_{msg_db_id or 'none'}")], [InlineKeyboardButton(f"🔕 Block User", callback_data=f"block_{user.id}")]]))
            if admin_sessions_collection is not None and msg_db_id:
                admin_sessions_collection.insert_one({"admin_message_id": sent_msg.message_id, "user_id": user.id, "user_chat_id": message.chat.id, "original_message_id": message.message_id, "message_db_id": msg_db_id, "created_at": datetime.utcnow()})
            logger.info(f"Forwarded message from user {user.id} to admin")
            if context.user_data.get("chat_with_admin"):
                try:
                    await asyncio.sleep(BOT_FAST_DELAY)
                    await message.reply_text("📩 Message delivered to admin — you'll get the reply here.")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to send admin notification: {e}")
        media_tasks = []
        try:
            if message.photo:
                photo = message.photo[-1]
                media_tasks.append(application.bot.send_photo(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0, photo=photo.file_id, caption=add_footer(f"📷 Photo from {escape_html(user.first_name)} (ID: <code>{user.id}</code>)"), parse_mode=ParseMode.HTML))
            elif message.video:
                media_tasks.append(application.bot.send_video(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0, video=message.video.file_id, caption=add_footer(f"🎥 Video from {escape_html(user.first_name)} (ID: <code>{user.id}</code>)"), parse_mode=ParseMode.HTML))
            elif message.document:
                media_tasks.append(application.bot.send_document(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0, document=message.document.file_id, caption=add_footer(f"📁 Document from {escape_html(user.first_name)} (ID: <code>{user.id}</code>)"), parse_mode=ParseMode.HTML))
            elif message.voice:
                media_tasks.append(application.bot.send_voice(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0, voice=message.voice.file_id, caption=add_footer(f"🔊 Voice from {escape_html(user.first_name)} (ID: <code>{user.id}</code>)"), parse_mode=ParseMode.HTML))
            elif message.audio:
                media_tasks.append(application.bot.send_audio(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0, audio=message.audio.file_id, caption=add_footer(f"🎧 Audio from {escape_html(user.first_name)} (ID: <code>{user.id}</code>)"), parse_mode=ParseMode.HTML))
            if media_tasks:
                asyncio.create_task(_gather_with_ignore(*media_tasks))
        except Exception as e:
            logger.error(f"Failed to forward media to admin: {e}")
    except Exception as e:
        logger.error(f"forward_user_message_to_admin error: {e}")
        logger.error(traceback.format_exc())

async def _gather_with_ignore(*tasks):
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if not isinstance(r, Exception)]

def escape_html(text: str) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ============ ADMIN REPLY (simple, no prefix) ============
async def handle_admin_reply_simple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    message = update.effective_message
    reply_to_message = message.reply_to_message
    if not reply_to_message:
        return
    
    session = None
    if admin_sessions_collection is not None:
        session = admin_sessions_collection.find_one({"admin_message_id": reply_to_message.message_id})
    
    if not session:
        text = reply_to_message.text or ""
        id_match = re.search(r'ID:\s*<code>(\d+)</code>', text)
        if id_match:
            user_id = int(id_match.group(1))
            user_chat_id = user_id
        else:
            return
    else:
        user_id = session["user_id"]
        user_chat_id = session["user_chat_id"]
    
    try:
        text = message.text or message.caption or ""
        if text:
            await application.bot.send_message(chat_id=user_chat_id, text=text, parse_mode=ParseMode.HTML)
        if message.photo:
            photo = message.photo[-1]
            await application.bot.send_photo(chat_id=user_chat_id, photo=photo.file_id, caption=message.caption)
        elif message.video:
            await application.bot.send_video(chat_id=user_chat_id, video=message.video.file_id, caption=message.caption)
        elif message.document:
            await application.bot.send_document(chat_id=user_chat_id, document=message.document.file_id, caption=message.caption)
        elif message.audio:
            await application.bot.send_audio(chat_id=user_chat_id, audio=message.audio.file_id, caption=message.caption)
        elif message.voice:
            await application.bot.send_voice(chat_id=user_chat_id, voice=message.voice.file_id, caption=message.caption)
        elif message.sticker:
            await application.bot.send_sticker(chat_id=user_chat_id, sticker=message.sticker.file_id)
        
        await message.reply_text(f"✅ Message sent to user")
    except Exception as e:
        logger.error(f"Admin reply delivery error: {e}")
        await message.reply_text(f"❌ Failed to send: {e}")

async def handle_pending_reply_simple(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    pending_user_id = context.user_data.get("pending_reply_to")
    if not pending_user_id:
        return
    
    text = update.message.text if update.message.text else ""
    try:
        await application.bot.send_message(chat_id=pending_user_id, text=text, parse_mode=ParseMode.HTML)
        await update.message.reply_text(f"✅ Reply sent to user {pending_user_id}")
        context.user_data.pop("pending_reply_to", None)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")

# ============ ADMIN REPLY HANDLERS ============
async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_admin_reply_simple(update, context)

async def handle_admin_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("reply_"):
        parts = data.split("_")
        if len(parts) >= 3:
            user_id = int(parts[1])
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✍️ Type your reply and send it here", callback_data="noop")]]))
            context.user_data["pending_reply_to"] = user_id
            await application.bot.send_message(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0, text=add_footer(f"✍️ <b>Reply Mode</b>\n\nType your message and I'll send it to user <code>{user_id}</code>.\nSend /cancel to exit reply mode."), parse_mode=ParseMode.HTML)
    elif data.startswith("block_"):
        user_id = int(data.split("_")[1])
        if users_collection is not None:
            users_collection.update_one({"user_id": user_id}, {"$set": {"blocked": True, "blocked_at": datetime.utcnow()}})
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🔕 User Blocked", callback_data="noop")]]))
        await application.bot.send_message(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0, text=add_footer(f"🔕 User {user_id} blocked."))

# ============ FIXED KEYBOARD HELPER ============
def add_cart_button_to_keyboard(keyboard: InlineKeyboardMarkup, token: str) -> InlineKeyboardMarkup:
    if not get_cart_enabled():
        return keyboard
    
    if isinstance(keyboard.inline_keyboard, tuple):
        keyboard_rows = list(keyboard.inline_keyboard)
    else:
        keyboard_rows = keyboard.inline_keyboard.copy() if keyboard.inline_keyboard else []
    
    cb = f"cart_add_{token}"
    if len(cb.encode("utf-8")) > 64:
        import hashlib
        h = hashlib.md5(str(token).encode("utf-8")).hexdigest()[:12]
        cb = f"ca_h_{h}"
    keyboard_rows.insert(0, [InlineKeyboardButton(f"Add to Cart ➕", callback_data=cb)])
    
    return InlineKeyboardMarkup(keyboard_rows)


def _cart_cb(action: str, token: str) -> str:
    """Build cart callback_data that never exceeds Telegram 64-byte limit.
    action: 'add' | 'rm'
    """
    prefix = "ca_" if action == "add" else "cr_"
    # Prefer short token; if still long, use hash
    raw = f"{prefix}{token}"
    if len(raw.encode("utf-8")) <= 64:
        return raw
    import hashlib
    h = hashlib.md5(str(token).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}h_{h}"

def _resolve_cart_token_from_cb(data: str, context=None) -> str:
    """Resolve token from cart callback (supports ca_/cr_ and hashed form)."""
    token = None
    if data.startswith("cart_add_"):
        token = data[9:]
    elif data.startswith("cart_remove_"):
        token = data[12:]
    elif data.startswith("ca_h_"):
        h = data[5:]
        # Look up from user cart or movies by hash
        token = None
        if context is not None:
            mapping = context.user_data.get("_cart_hash_map") or {}
            token = mapping.get(h)
        if not token and movies_collection is not None:
            # scan recent - expensive fallback avoided; store map on add
            pass
        return token
    elif data.startswith("cr_h_"):
        h = data[5:]
        if context is not None:
            mapping = context.user_data.get("_cart_hash_map") or {}
            token = mapping.get(h)
        return token
    elif data.startswith("ca_"):
        token = data[3:]
    elif data.startswith("cr_"):
        token = data[3:]
    return token

def _remember_cart_hash(context, token: str) -> None:
    if context is None:
        return
    import hashlib
    h = hashlib.md5(str(token).encode("utf-8")).hexdigest()[:12]
    m = context.user_data.setdefault("_cart_hash_map", {})
    m[h] = token


# ============ HANDLE CALLBACK ============
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    if await enforce_user_access(update, context):
        return

    _POPUP_TEXTS = {
        "watch_all_movies_0": "🎬 Loading movies…",
        "browse_bundles_0": "📦 Loading bundles…",
        "search_movies": "🔍 Type the movie name to search",
        "cart_view": "🛒 Opening your cart…",
        "show_plans": "💳 Loading subscription plans…",
        "more_movies": "🍃 Loading more movies…",
        "how_bot_works": "📖 How this bot works",
        "getbot_cmd": "🤖 Get a bot like this",
        "go_home": "🏠 Home",
        "back": "◀ Going back…",
        "visit_channels": "➕ ᴀᴅᴅ ꜰʀᴏᴍ ᴄʜᴀɴɴᴇʟ",
    }
    # Cart toasts answered IMMEDIATELY here (before any DB) so Telegram always shows top toast
    _cart_answered = False
    if data.startswith("cart_add_") or data.startswith("ca_") or data.startswith("ca_h_"):
        try:
            await query.answer("Added to cart", show_alert=False)
            _cart_answered = True
        except Exception:
            pass
    elif data.startswith("cart_remove_") or data.startswith("cr_") or data.startswith("crm_"):
        try:
            await query.answer("Removed from cart", show_alert=False)
            _cart_answered = True
        except Exception:
            pass
    _skip_early_answer = (
        _cart_answered or data.startswith("unlock_ban_")
    )
    if data in _POPUP_TEXTS:
        try:
            await query.answer(_POPUP_TEXTS[data])
        except Exception:
            pass
    elif data.startswith("movie_detail_"):
        try:
            await query.answer("✅ Movie selected")
        except Exception:
            pass
    elif data.startswith("stk_"):
        try:
            await query.answer("💳 Pay to receive your content")
        except Exception:
            pass
    elif not _skip_early_answer:
        try:
            await query.answer()
        except Exception:
            pass

    if not await check_force_join(update, context):
        return

    if data == "noop":
        return
    if await handle_pending_payment_callback(update, context):
        return

    if data == "getbot_cmd":
        await show_getbot_screen(update, context)
        return
    if data.startswith("check_join_"):
        if FORCE_CHANNELS:
            all_joined, missing = await _check_force_membership(context, user_id)
            if all_joined:
                await edit_or_send_message(query, "✅ You have joined all required channels! You can now use the bot.", parse_mode=ParseMode.HTML)
                context.user_data.pop("force_join_pending", None)
                await show_home(update, context, edit=True)
            else:
                try:
                    await query.answer(
                        "You have NOT joined yet. Be serious — join every channel first, then press I've Joined.",
                        show_alert=True,
                    )
                except Exception:
                    pass
                try:
                    join_text, join_kb = await _build_force_join_message(context, user_id, missing, serious=True)
                    await edit_or_send_message(query, join_text, reply_markup=join_kb, parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.warning(f"check_join serious refresh failed: {e}")
        return

    if data.startswith("bot_"):
        await handle_bot_callbacks(update, context)
        return

    if data.startswith("ck_price_"):
        await handle_confronter_king_price_callback(update, context)
        return
    
    if data.startswith("upload_price_"):
        await handle_upload_price_callback(update, context)
        return
    
    if data == "visit_channels":
        if not ADDITIONAL_CHANNELS:
            await query.answer("No channels configured.", show_alert=True)
            return
        kb = []
        for ch in ADDITIONAL_CHANNELS:
            kb.append([InlineKeyboardButton(f"➕ {ch['name']}", url=ch['url'])])
        kb.append([InlineKeyboardButton(f"☔ ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")])
        kb.append([InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")])
        await edit_or_send_message(query, add_footer("📌 <b>ᴀᴅᴅ ꜰʀᴏᴍ ᴄʜᴀɴɴᴇʟꜱ</b>\n\nTap a channel to browse more movies:"), reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        push_nav_state(context, "visit_channels", "🌱 ᴀᴅᴅ ꜰʀᴏᴍ ᴄʜᴀɴɴᴇʟꜱ", InlineKeyboardMarkup(kb))
        return
    
    if data == "more_movies":
        await show_more_movies(update, context)
        return
    
    if data == "toggle_display_mode":
        current = get_display_mode()
        new_mode = 'poster' if current == 'button' else 'button'
        set_display_mode(new_mode)
        status = "Button Style (titles only, 20/page)" if new_mode == 'button' else "Poster Style (album + buttons)"
        
        text = add_footer(f"⚙️ <b>Display Mode Changed!</b>\n\nDisplay mode: <b>{status}</b>\n\nMode is now active for all users.")
        
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="back")]])
        push_nav_state(context, "toggle_display", text, back_kb)
        
        await query.edit_message_text(text=text, reply_markup=back_kb, parse_mode=ParseMode.HTML)
        return
    
    if data == "cart_view":
        await show_cart_page(update, context)
        return
    
    if data == "cart_checkout":
        cart_items = get_user_cart(user_id)
        total = get_cart_total(user_id)
        discount = apply_cart_discount(user_id)
        discounted_total = total - discount if discount > 0 else total
        if not cart_items:
            await query.answer("Your cart is empty!", show_alert=True)
            return
        
        cart_ref = f"cart_{secrets.token_hex(4)}"
        track_pending_payment(user_id, cart_ref)
        if payments_collection is not None:
            cart_tokens = [item['token'] for item in cart_items]
            payments_collection.insert_one({"reference": cart_ref, "user_id": user_id, "cart_tokens": cart_tokens, "chat_id": query.message.chat.id, "amount": discounted_total, "status": "pending", "created_at": datetime.utcnow(), "payment_method": None, "auto_deliver": True, "is_subscription": False})
        
        items = []
        items.append({"label": "🛒 CHECKOUT", "value": ""})
        items.append({"label": "", "value": ""})
        for i, item in enumerate(cart_items, 1):
            price_display = f"{item['price']} KES" if item['price'] > 0 else "FREE"
            item_icon = f"📦" if item.get('is_bundle') else f"🎬"
            items.append({"label": f"{i}. {item_icon} {clean_movie_title_for_button(item['title'])}", "value": price_display})
        items.append({"label": "", "value": ""})
        if discount > 0:
            items.append({"label": f"💰 TOTAL:", "value": f"{total} KES"})
            items.append({"label": f"🎁 Discount:", "value": f"-{discount} KES"})
            items.append({"label": f"💳 PAY:", "value": f"{discounted_total} KES"})
        else:
            items.append({"label": f"💰 TOTAL:", "value": f"{total} KES"})
        
        checkout_text = render_menu_for_user(user_id, "🛒 CHECKOUT", items)
        checkout_text = add_footer(checkout_text)
        checkout_text += apply_premium_emojis(f"\n\n💳 Choose payment method:") + get_datetime_footer()
        
        auth_url = None
        if get_card_payment_setting():
            try:
                auth_url, _ = initialize_paystack_payment(email=f"user{user_id}@telegram.bot", amount_kes=discounted_total, reference=cart_ref, metadata={"user_id": user_id, "cart_tokens": [item['token'] for item in cart_items], "chat_id": query.message.chat.id, "auto_deliver": True})
            except Exception as e:
                logger.error(f"Cart checkout Paystack init failed: {e}")
        plans = get_subscription_plans()
        keyboard = build_payment_keyboard(auth_url, cart_ref, discounted_total, user_id, plans)
        await edit_or_send_message(query, checkout_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        push_nav_state(context, "cart_checkout", checkout_text, keyboard)
        return
    
    if data == "cart_clear":
        clear_user_cart(user_id)
        await query.answer("Cart cleared!")
        empty_text = add_footer(f"""🛒 <b>Your Shopping Cart</b>

😢 Your cart is empty!

ℹ️ Browse movies and tap "Add to Cart" to add items.""")
        empty_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🎬 ʙʀᴏᴡꜱᴇ ᴀʟʟ ᴍᴏᴠɪᴇꜱ", callback_data="watch_all_movies_0")],
            [InlineKeyboardButton(f"📦 ʙʀᴏᴡꜱᴇ ᴍᴏᴠɪᴇ ᴄᴏʟʟᴇᴄᴛɪᴏɴꜱ", callback_data="browse_bundles_0")],
            [InlineKeyboardButton(f"🍃 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")],
            [InlineKeyboardButton(f"🏠 ʜᴏᴍᴇ", callback_data="go_home")],
            [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]
        ])
        await edit_or_send_message(query, empty_text, reply_markup=empty_kb, parse_mode=ParseMode.HTML)
        push_nav_state(context, "cart_empty", empty_text, empty_kb)
        return
    # Index-based remove from cart page (crm_0, crm_1, ...)
    if data.startswith("crm_"):
        try:
            idx = int(data.split("_", 1)[1])
            tokens = context.user_data.get("cart_token_list") or []
            if not tokens:
                # fallback: live cart order
                tokens = [it.get("token") for it in (get_user_cart(user_id) or [])]
            if 0 <= idx < len(tokens):
                token = tokens[idx]
                remove_from_cart(user_id, token)
                try:
                    await query.answer("Removed from cart", show_alert=False)
                except Exception:
                    pass
                await show_cart_page(update, context)
            else:
                try:
                    await query.answer("Item gone", show_alert=False)
                except Exception:
                    pass
                await show_cart_page(update, context)
        except Exception as e:
            logger.error(f"crm_ remove error: {e}")
            try:
                await query.answer("Error", show_alert=False)
            except Exception:
                pass
        return

    if data.startswith("cart_remove_") or data.startswith("cr_"):
        if data.startswith("cart_remove_"):
            token = data.replace("cart_remove_", "")
        elif data.startswith("cr_h_"):
            import hashlib
            h = data[5:]
            token = (context.user_data.get("_cart_hash_map") or {}).get(h)
            if not token:
                # try match from cart
                for it in (get_user_cart(user_id) or []):
                    th = hashlib.md5(str(it.get("token","")).encode()).hexdigest()[:12]
                    if th == h:
                        token = it.get("token")
                        break
        else:
            token = data[3:]  # cr_
        if not token:
            try:
                await query.answer("Item not found", show_alert=False)
            except Exception:
                pass
            return
        remove_from_cart(user_id, token)
        try:
            await query.answer("Removed from cart", show_alert=False)
        except Exception as e:
            logger.debug(f"cart_remove answer failed: {e}")
        try:
            msg_text = (query.message.text or query.message.caption or "") if query.message else ""
            if "Your Cart" in msg_text or "CART" in msg_text.upper():
                await show_cart_page(update, context)
            else:
                current_kb = query.message.reply_markup if query.message else None
                if current_kb and current_kb.inline_keyboard:
                    # short safe add callback
                    add_cb = f"cart_add_{token}"
                    if len(add_cb.encode("utf-8")) > 64:
                        import hashlib
                        hh = hashlib.md5(str(token).encode()).hexdigest()[:12]
                        add_cb = f"ca_h_{hh}"
                        context.user_data.setdefault("_cart_hash_map", {})[hh] = token
                    new_rows = []
                    for row in current_kb.inline_keyboard:
                        new_row = []
                        for btn in row:
                            bcd = btn.callback_data or ""
                            if bcd in (f"cart_remove_{token}", f"cr_{token}") or bcd.startswith("cr_h_") or bcd.startswith("crm_"):
                                # only flip the matching remove button
                                if bcd == f"cart_remove_{token}" or bcd == f"cr_{token}" or (bcd.startswith("cr_h_") and True):
                                    new_row.append(InlineKeyboardButton("Add to Cart ➕", callback_data=add_cb))
                                else:
                                    new_row.append(btn)
                            else:
                                new_row.append(btn)
                        new_rows.append(new_row)
                    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows))
        except Exception:
            pass
        return

    if data.startswith("ca_h_"):
        import hashlib
        h = data[5:]
        token = (context.user_data.get("_cart_hash_map") or {}).get(h)
        if not token and movies_collection is not None:
            # cannot reverse MD5; fail soft
            try:
                await query.answer("List expired — open movie again", show_alert=True)
            except Exception:
                pass
            return
        data = f"cart_add_{token}"
    elif data.startswith("ca_") and not data.startswith("ca_h_"):
        # ca_{page}_{index} short cart-add when token too long
        try:
            parts = data.split("_")
            idx_m = int(parts[-1])
            tokens = context.user_data.get("last_page_tokens") or []
            if 0 <= idx_m < len(tokens):
                data = f"cart_add_{tokens[idx_m]}"
            else:
                await query.answer("List expired", show_alert=True)
                return
        except Exception:
            await query.answer("Error", show_alert=True)
            return
    if data.startswith("cart_add_"):
        if not get_cart_enabled():
            try:
                await query.answer("Cart disabled", show_alert=False)
            except Exception:
                pass
            return
        token = data.replace("cart_add_", "")
        # remember hash for future short cbs
        try:
            import hashlib
            hh = hashlib.md5(str(token).encode()).hexdigest()[:12]
            context.user_data.setdefault("_cart_hash_map", {})[hh] = token
        except Exception:
            pass
        item = None
        if movies_collection is not None:
            item = movies_collection.find_one({"token": token})
        if not item and series_bundles_collection is not None:
            item = series_bundles_collection.find_one({"token": token})
        if not item:
            try:
                await query.answer("Movie not found", show_alert=False)
            except Exception:
                pass
            return
        success = add_to_cart(user_id, token)
        # Top toast (same style as "Going back…") — must answer quickly
        try:
            if success:
                await query.answer("✅ Added to cart", show_alert=False)  # may already be answered early
            else:
                await query.answer("Already in cart ✅", show_alert=False)
        except Exception as e:
            logger.debug(f"cart_add answer failed: {e}")
        if success:
            try:
                current_kb = query.message.reply_markup if query.message else None
                if current_kb and current_kb.inline_keyboard:
                    import hashlib
                    rm_cb = f"cart_remove_{token}"
                    if len(rm_cb.encode("utf-8")) > 64:
                        hh = hashlib.md5(str(token).encode()).hexdigest()[:12]
                        rm_cb = f"cr_h_{hh}"
                        context.user_data.setdefault("_cart_hash_map", {})[hh] = token
                    new_rows = []
                    pay_row_idx = None
                    for ri, row in enumerate(current_kb.inline_keyboard):
                        new_row = []
                        for btn in row:
                            bcd = btn.callback_data or ""
                            # flip any add-to-cart variant for this token
                            if bcd == f"cart_add_{token}" or bcd.startswith("ca_h_") or (bcd.startswith("ca_") and token in bcd):
                                new_row.append(InlineKeyboardButton("Remove from Cart ✅", callback_data=rm_cb))
                            else:
                                new_row.append(btn)
                            # detect pay / stk row
                            if bcd.startswith("stk_") or "ᴍ-ᴘᴇꜱᴀ" in (btn.text or "") or "M-PESA" in (btn.text or "").upper() or "M-Pesa" in (btn.text or ""):
                                pay_row_idx = ri
                        new_rows.append(new_row)
                    has_view = any((b.callback_data or "") == "cart_view" for r in new_rows for b in r)
                    if not has_view:
                        view_row = [
                            InlineKeyboardButton("View Cart & Pay", callback_data="cart_view"),
                            InlineKeyboardButton("Continue Shopping", callback_data="watch_all_movies_0")
                        ]
                        # insert RIGHT BELOW pay with mpesa (or after first row if not found)
                        insert_at = (pay_row_idx + 1) if pay_row_idx is not None else 1
                        if insert_at > len(new_rows):
                            insert_at = len(new_rows)
                        new_rows.insert(insert_at, view_row)
                    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows))
            except Exception as e:
                logger.debug(f"cart_add keyboard flip skipped: {e}")
        return

    if data.startswith("genre_"):
        genre = data.replace("genre_", "")
        try:
            context.user_data["awaiting_search"] = False
            await perform_search(update, context, genre, page=0)
        except Exception as e:
            # Results may already have been sent — log only, never spam error after success
            logger.error(f"Genre search error for {genre}: {e}")
            logger.error(traceback.format_exc())
        return
    if data.startswith("unlock_ban_"):
        try:
            fee = int(data.replace("unlock_ban_", "") or 300)
        except Exception:
            fee = 300
        # Start M-Pesa flow for ban unlock (must pass enforce_user_access)
        context.user_data["awaiting_phone"] = True
        context.user_data["pending_ban_unlock"] = True
        context.user_data["ban_unlock_fee"] = fee
        ref = f"banunlock_{user_id}_{secrets.token_hex(3)}"
        try:
            if payments_collection is not None:
                payments_collection.update_one(
                    {"reference": ref},
                    {"$set": {
                        "user_id": user_id, "amount": fee, "status": "pending",
                        "reference": ref, "plan": "ban_unlock", "movie_title": "Ban unlock",
                        "is_ban_unlock": True, "created_at": datetime.utcnow(),
                    }},
                    upsert=True,
                )
            if pending_payhero_collection is not None:
                pending_payhero_collection.update_one(
                    {"user_id": user_id},
                    {"$set": {
                        "user_id": user_id, "reference": ref, "payhero_ref": ref,
                        "price": fee, "amount": fee, "status": "awaiting_phone",
                        "is_ban_unlock": True, "plan": "ban_unlock",
                        "provider": get_active_payment_gateway(),
                        "created_at": datetime.utcnow(),
                    }},
                    upsert=True,
                )
        except Exception as e:
            logger.error(f"ban unlock pending store: {e}")
        try:
            await query.answer("Enter M-Pesa number…")
        except Exception:
            pass
        text = add_footer(build_mpesa_prompt_text(fee, extra_lines=["🔓 Unlock your ban"], user_id=user_id))
        await edit_or_send_message(query, text, reply_markup=build_back_keyboard("back"), parse_mode=ParseMode.HTML)
        return

    if data == "go_home":
        try:
            await query.answer("Going home...")
        except Exception:
            pass
        context.user_data.pop("chat_with_admin", None)
        clear_nav_history(context)
        await show_home(update, context, edit=True)
        return
    if data == "how_bot_works":
        how_text = add_footer(f"""📖 <b>How This Bot Works</b>

1️⃣ <b>Single Movie</b>
   Browse or search for a movie.
   Tap "Get Movie" – you'll see payment options.
   Pay once and receive your movie instantly.

2️⃣ <b>Multiple Movies (Cart)</b>
   While browsing, tap "Add to Cart" for any movie.
   You can add as many as you want.
   When ready, type /cart or tap "My Cart".
   Review your items and tap "Pay with M-Pesa".
   Complete the payment once and receive all movies instantly.

💡 <b>Pro Tip:</b>
   • Subscribe for unlimited movies (weekly/monthly)
   • All payments are via M-Pesa (prompt on your phone)
   • No delays – instant delivery after payment

❓ Need help? Contact admin""")
        how_kb = build_back_keyboard("back")
        await edit_or_send_message(query, how_text, reply_markup=how_kb, parse_mode=ParseMode.HTML)
        push_nav_state(context, "how_bot_works", how_text, how_kb)
        return

    if data == "back":
        prev_state = get_previous_nav_state(context)
        if prev_state:
            keyboard = rebuild_keyboard(prev_state.get("keyboard"))
            try:
                if query.message and query.message.photo:
                    await query.edit_message_caption(caption=prev_state["text"], reply_markup=keyboard, parse_mode=prev_state.get("parse_mode", ParseMode.HTML))
                else:
                    await query.edit_message_text(text=prev_state["text"], reply_markup=keyboard, parse_mode=prev_state.get("parse_mode", ParseMode.HTML))
            except Exception as e:
                logger.error(f"Back navigation edit failed: {e}")
                await show_home(update, context, edit=True)
        else:
            await show_home(update, context, edit=True)
        return
    if data.startswith("search_page_"):
        try:
            page = int(data.replace("search_page_", "").strip() or "0")
            query_str = context.user_data.get("last_search_query") or ""
            if not query_str:
                await query.answer("Search expired — use /search again", show_alert=True)
                return
            await perform_search(update, context, query_str, page=page)
        except Exception as e:
            logger.error(f"Search pagination error: {e}")
            logger.error(traceback.format_exc())
            err_text = add_footer(f"❌ Error navigating search results.")
            err_kb = build_back_keyboard("back")
            await edit_or_send_message(query, err_text, reply_markup=err_kb)
        return
    if data == "search_movies" or data == "search_prompt":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"❌ Cancel", callback_data="cancel_search")]])
        search_text = add_footer(f"""🔍 <b>Search Movies</b>

Type the movie name or Genre you want to search for.
Example: <code>Action</code> or <code>Horror</code> or <code>Furious</code>

ℹ️ I'll search all movies in our library and show you results with instant download links!""")
        await edit_or_send_message(query, search_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        push_nav_state(context, "search_prompt", search_text, keyboard)
        context.user_data["awaiting_search"] = True
        return
    if data == "cancel_search":
        context.user_data["awaiting_search"] = False
        cancel_text = add_footer(f"❌ Search cancelled. Use /search anytime to find movies!")
        cancel_kb = build_back_keyboard("back")
        await edit_or_send_message(query, cancel_text, reply_markup=cancel_kb)
        push_nav_state(context, "search_cancelled", cancel_text, cancel_kb)
        return

    if data.startswith("stk_"):
        parts = data.split("_")
        if len(parts) < 3:
            await query.answer("Invalid payment request.")
            return
        price = parts[-1]
        ref = "_".join(parts[1:-1])
        track_pending_payment(user_id, ref)
        payment_record = payments_collection.find_one({"reference": ref}) if payments_collection is not None else None
        if not payment_record:
            await query.answer("Payment record not found.", show_alert=True)
            return
        movie_token = payment_record.get("movie_token")
        series_token = payment_record.get("series_token")
        cart_tokens = payment_record.get("cart_tokens", [])
        is_cart = bool(cart_tokens)
        # CRITICAL: carry subscription plan so activation works after payment
        is_subscription = bool(payment_record.get("is_subscription") or payment_record.get("type") == "subscription")
        plan = payment_record.get("plan")
        active_gw = get_active_payment_gateway()
        is_payhero = active_gw == "payhero"
        is_makamesco = active_gw == "makamesco"
        if is_payhero or is_makamesco:
            if pending_payhero_collection is not None:
                payhero_ref = f"{'mk' if is_makamesco else 'ph'}_{user_id}_{secrets.token_hex(4)}"
                pending_payhero_collection.update_one(
                    {"user_id": user_id},
                    {"$set": {
                        "reference": ref,
                        "payhero_ref": payhero_ref,
                        "price": int(price),
                        "status": "awaiting_phone",
                        "movie_token": movie_token,
                        "series_token": series_token,
                        "cart_tokens": cart_tokens,
                        "is_cart": is_cart,
                        "is_subscription": is_subscription,
                        "plan": plan,
                        "provider": active_gw,
                        "created_at": datetime.utcnow()
                    }},
                    upsert=True
                )
            logger.info(f"{active_gw} mode: Stored pending payment user={user_id} sub={is_subscription} plan={plan}")
        else:
            if pending_stk_collection is not None:
                stk_ref = f"stk_{ref}_{secrets.token_hex(4)}"
                pending_stk_collection.update_one(
                    {"user_id": user_id},
                    {"$set": {
                        "reference": ref,
                        "stk_reference": stk_ref,
                        "price": int(price),
                        "status": "awaiting_phone",
                        "movie_token": movie_token,
                        "series_token": series_token,
                        "cart_tokens": cart_tokens,
                        "is_cart": is_cart,
                        "is_subscription": is_subscription,
                        "plan": plan,
                        "created_at": datetime.utcnow()
                    }},
                    upsert=True
                )
        # Clean prompt using PAYMENT_PROMPT_NAME from .env (no Paystack/PayHero branding)
        prompt_name = PAYMENT_PROMPT_NAME or "payment"
        stk_text = add_footer(build_mpesa_prompt_text(price))
        stk_kb = build_back_keyboard("back")
        await edit_or_send_message(query, stk_text, reply_markup=stk_kb, parse_mode=ParseMode.HTML)
        push_nav_state(context, "stk_prompt", stk_text, stk_kb)
        return
    
    if data.startswith("sub_weekly_") or data.startswith("sub_monthly_") or data.startswith("sub_2week_") or data.startswith("sub_3week_"):
        if not get_subscription_enabled():
            disabled_text = add_footer(f"❌ Subscriptions are currently disabled.")
            disabled_kb = build_back_keyboard("back")
            await edit_or_send_message(query, disabled_text, reply_markup=disabled_kb, parse_mode=ParseMode.HTML)
            push_nav_state(context, "sub_disabled", disabled_text, disabled_kb)
            return
        if data.startswith("sub_weekly_"):
            plan = "weekly"
        elif data.startswith("sub_monthly_"):
            plan = "monthly"
        elif data.startswith("sub_2week_"):
            plan = "2week"
        elif data.startswith("sub_3week_"):
            plan = "3week"
        else:
            await query.answer("Invalid plan.")
            return
        plans = get_subscription_plans()
        plan_info = plans[plan]
        sub_ref = f"sub_{secrets.token_hex(4)}"
        track_pending_payment(user_id, sub_ref)
        if payments_collection is not None:
            payments_collection.insert_one({"reference": sub_ref, "user_id": user_id, "plan": plan, "chat_id": query.message.chat.id, "amount": plan_info["price"], "status": "pending", "created_at": datetime.utcnow(), "type": "subscription", "is_subscription": True})
        payment_text = add_footer(f"""🌴 <b>{plan_info['name']}</b>

📅 Duration: {plan_info['days']} days
💰 Price: <b>{plan_info['price']} KES</b>

🔥 Get unlimited access to ALL movies!
""" + apply_premium_emojis("💳 Choose payment method:") + get_datetime_footer())
        auth_url, _ = initialize_paystack_payment(email=f"user{user_id}@telegram.bot", amount_kes=plan_info["price"], reference=sub_ref, metadata={"user_id": user_id, "plan": plan, "chat_id": update.effective_chat.id, "is_subscription": True}, plan=plan)
        keyboard = build_payment_keyboard(auth_url, sub_ref, plan_info["price"], user_id, plans)
        await edit_or_send_message(query, payment_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        push_nav_state(context, f"sub_payment_{plan}", payment_text, keyboard)
        return
    if data.startswith("substk_"):
        parts = data.split("_")
        price = parts[-1]
        ref = "_".join(parts[1:-1])
        payment_record = payments_collection.find_one({"reference": ref})
        if not payment_record:
            await query.answer("Payment record not found.", show_alert=True)
            return
        plan = payment_record.get("plan", "weekly")
        substk_text = add_footer(build_mpesa_prompt_text(price))
        substk_kb = build_back_keyboard("back")
        await edit_or_send_message(query, substk_text, reply_markup=substk_kb, parse_mode=ParseMode.HTML)
        push_nav_state(context, "substk_prompt", substk_text, substk_kb)
        is_payhero = get_payhero_enabled() and PAYHERO_API_USERNAME and PAYHERO_API_PASSWORD
        if is_payhero:
            if pending_payhero_collection is not None:
                payhero_ref = f"phsub_{user_id}_{secrets.token_hex(4)}"
                pending_payhero_collection.update_one({"user_id": user_id}, {"$set": {"reference": ref, "payhero_ref": payhero_ref, "price": int(price), "status": "awaiting_phone", "is_subscription": True, "plan": plan, "created_at": datetime.utcnow()}}, upsert=True)
        else:
            if pending_stk_collection is not None:
                stk_ref = f"stk_{ref}_{secrets.token_hex(4)}"
                pending_stk_collection.update_one({"user_id": user_id}, {"$set": {"reference": ref, "stk_reference": stk_ref, "price": int(price), "status": "awaiting_phone", "is_subscription": True, "plan": plan, "created_at": datetime.utcnow()}}, upsert=True)
        return
    if data == "show_plans":
        if not get_subscription_enabled():
            disabled_text = add_footer(f"❌ Subscriptions are currently disabled.")
            disabled_kb = build_back_keyboard("back")
            await edit_or_send_message(query, disabled_text, reply_markup=disabled_kb, parse_mode=ParseMode.HTML)
            push_nav_state(context, "plans_disabled", disabled_text, disabled_kb)
            return
        has_sub, sub_type, expiry, used, max_movies = has_active_subscription(user_id)
        if has_sub:
            remaining = (max_movies - used) if max_movies > 0 else "∞"
            exp_str = expiry.strftime('%d %b %Y %H:%M') if expiry else 'Never'
            items = [
                {"label": "✅ Status", "value": "ACTIVE"},
                {"label": "👑 Plan", "value": f"{(sub_type or 'Premium').replace('2week','2-Week').replace('3week','3-Week').title()} Pass"},
                {"label": "📅 Expires", "value": exp_str},
                {"label": "🎬 Movies left", "value": str(remaining)},
            ]
            text = add_footer(render_menu_for_user(user_id, "📊 SUBSCRIPTION FOR MOVIES", items))
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Browse Movies", callback_data="watch_all_movies_0")],
                [InlineKeyboardButton("📦 More Movies", callback_data="more_movies")],
                [InlineKeyboardButton("🛒 My Cart", callback_data="cart_view")],
                [InlineKeyboardButton("◀️ Back", callback_data="back")]
            ])
        else:
            plans = get_subscription_plans()
            items = [
                {"label": "🔥 Get access to ALL movies", "value": ""},
                {"label": "", "value": ""},
                {"label": "📅 WEEKLY PASS", "value": f"{plans['weekly']['price']} KES (7d)"},
                {"label": "📅 2-WEEK PASS", "value": f"{plans['2week']['price']} KES (14d)"},
                {"label": "📅 3-WEEK PASS", "value": f"{plans['3week']['price']} KES (21d)"},
                {"label": "📅 MONTHLY PASS", "value": f"{plans['monthly']['price']} KES (30d)"},
            ]
            text = add_footer(render_menu_for_user(user_id, "👑 SUBSCRIPTION FOR MOVIES", items))
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"📅 WEEKLY — {plans['weekly']['price']} KES", callback_data=f"sub_weekly_{user_id}")],
                [InlineKeyboardButton(f"📅 2-WEEK — {plans['2week']['price']} KES", callback_data=f"sub_2week_{user_id}")],
                [InlineKeyboardButton(f"📅 3-WEEK — {plans['3week']['price']} KES", callback_data=f"sub_3week_{user_id}")],
                [InlineKeyboardButton(f"📅 MONTHLY — {plans['monthly']['price']} KES", callback_data=f"sub_monthly_{user_id}")],
                [InlineKeyboardButton("ℹ️ How subscription works", callback_data="sub_how_it_works")],
                [InlineKeyboardButton("⭐ WANT PREMIUM?", callback_data="request_prem")],
                [InlineKeyboardButton("More Movies", callback_data="more_movies")],
                [InlineKeyboardButton("◀️ BACK", callback_data="back")]
            ])
        await edit_or_send_message(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        push_nav_state(context, "show_plans", text, keyboard)
        return
    if data == "sub_how_it_works":
        plans = get_subscription_plans()
        show_lim = get_show_watched_remaining()
        lines = [
            "ℹ️ <b>How Subscription Works</b>",
            "",
            "When you buy a plan you get:",
            "",
            "• Access to <b>ALL movies</b> in the library for the plan duration",
        ]
        # Duration lines
        w = plans.get("weekly") or {}
        t2 = plans.get("2week") or {}
        t3 = plans.get("3week") or {}
        m = plans.get("monthly") or {}
        lines.append(
            f"• Weekly = {w.get('days',7)} days · 2-Week = {t2.get('days',14)} days · "
            f"3-Week = {t3.get('days',21)} days · Monthly = {m.get('days',30)} days"
        )
        if show_lim:
            lines.append(
                f"• Movie limits: Weekly <b>{w.get('max_movies',60)}</b> · "
                f"2-Week <b>{t2.get('max_movies',100)}</b> · "
                f"3-Week <b>{t3.get('max_movies',140)}</b> · "
                f"Monthly <b>{m.get('max_movies',200)}</b>"
            )
        lines.extend([
            "• Movies you already started stay in /mymovies — you can <b>rewatch them free</b> even after the plan ends",
            "• New movies after expiry need a new plan or single purchase",
            "",
            "Tap a plan below to subscribe.",
        ])
        how = add_footer("\n".join(lines))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👑 View Plans", callback_data="show_plans")],
            [InlineKeyboardButton("◀️ Back", callback_data="back")]
        ])
        await edit_or_send_message(query, how, reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    if data == "help_payment":
        guide = format_payment_guide(get_default_price())
        guide = add_footer(guide)
        guide_kb = build_back_keyboard("back")
        await edit_or_send_message(query, guide, reply_markup=guide_kb, parse_mode=ParseMode.HTML)
        push_nav_state(context, "help_payment", guide, guide_kb)
        return
    if data.startswith("check_"):
        reference = data.replace("check_", "")
        await check_payment_status(update, context, reference)
        return
    if data.startswith("reply_"):
        parts = data.split("_")
        if len(parts) >= 3:
            target_user_id = int(parts[1])
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✍️ Type your reply and send it here", callback_data="noop")]]))
            context.user_data["pending_reply_to"] = target_user_id
            await application.bot.send_message(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0, text=add_footer(f"✍️ <b>Reply Mode</b>\n\nType your message and I'll send it to user <code>{target_user_id}</code>.\nSend /cancel to exit reply mode."), parse_mode=ParseMode.HTML)
        return
    if data.startswith("block_"):
        block_user_id = int(data.split("_")[1])
        if users_collection is not None:
            users_collection.update_one({"user_id": block_user_id}, {"$set": {"blocked": True, "blocked_at": datetime.utcnow()}})
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🔕 User Blocked", callback_data="noop")]]))
        return
    if data in ("upload_single", "upload_series", "cancel_upload", "complete_upload") or data.startswith("upload_cat_"):
        await handle_upload_callback(update, context)
        return
    if data.startswith("free_"):
        token = data[5:]
        movie = movies_collection.find_one({"token": token}) if mongodb_connected else None
        if movie:
            await auto_deliver_movie(user_id, token, context)
        return
    if data.startswith("browse_bundles_"):
        page = int(data.split("_")[-1]) if data.split("_")[-1].isdigit() else 0
        await show_bundles_page(update, context, page)
        return
    if data.startswith("watch_all_movies_"):
        page = int(data.split("_")[-1]) if data.split("_")[-1].isdigit() else 0
        await show_movies_page(update, context, page)
        return
    if data.startswith("movie_detail_"):
        movie_token = data[13:]
        await show_movie_detail(update, context, movie_token)
        return
    if data.startswith("movies_page_"):
        page = int(data.split("_")[-1])
        await show_movies_page(update, context, page)
        return
    if data.startswith("mymovie_watch_"):
        movie_token = data[14:]
        movie = movies_collection.find_one({"token": movie_token}) if mongodb_connected else None
        if movie:
            try:
                file_id = movie.get('file_id')
                caption = movie['title']
                if file_id:
                    if movie.get('file_type') == 'video':
                        await context.bot.send_video(chat_id=user_id, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
                    else:
                        await context.bot.send_document(chat_id=user_id, document=file_id, caption=caption, parse_mode=ParseMode.HTML)
                else:
                    await context.bot.forward_message(chat_id=user_id, from_chat_id=movie['channel_id'], message_id=movie['message_id'])
                    await context.bot.send_message(chat_id=user_id, text=caption, parse_mode=ParseMode.HTML)
                movies_collection.update_one({"token": movie_token}, {"$inc": {"downloads": 1}})
                await context.bot.send_message(chat_id=user_id, text=add_footer(f"🎬 <b>{movie['title']}</b>\n\n✅ Enjoy your movie 🍿"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🎬 ʙʀᴏᴡꜱᴇ ᴍᴏᴠɪᴇꜱ", callback_data="watch_all_movies_0")], [InlineKeyboardButton(f"🌴 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")], [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ ᴛᴏ ᴍʏ ᴍᴏᴠɪᴇꜱ", callback_data="back")]]), parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"My Movies re-watch error: {e}")
                await query.edit_message_text(add_footer(f"❌ Could not deliver movie. Please try again."), reply_markup=build_back_keyboard("back"))
        else:
            await query.edit_message_text(add_footer(f"❌ Movie not found. It may have been removed."), reply_markup=build_back_keyboard("back"))
        return
    if data == "myplanmovies":
        await myplanmovies_cmd(update, context)
        return
    if data.startswith("rewatch_"):
        await rewatch_movie_callback(update, context, data.replace("rewatch_", "", 1))
        return
    if data == "mymovies_list":
        await show_my_movies_page(update, context, 0)
        return
    if data == "mymovies_videos":
        await deliver_all_my_movies(update, context)
        return
    if data.startswith("mymovies_page_"):
        page = int(data.split("_")[-1])
        await show_my_movies_page(update, context, page)
        return
    if data.startswith("mymovie_bundle_"):
        bundle_token = data[15:]
        bundle = series_bundles_collection.find_one({"token": bundle_token}) if mongodb_connected else None
        if bundle:
            episodes = bundle.get('episodes', [])
            total = len(episodes)
            if total == 0:
                await query.edit_message_text(add_footer(f"❌ This bundle has no movies."), reply_markup=build_back_keyboard("back"))
                return
            try:
                progress_msg = await context.bot.send_message(chat_id=user_id, text=add_footer(f"📦 <b>Re-watching {bundle['title']}</b>\n\n⏳ Sending {total} movies... (0/{total})\n<i>Please wait...</i>"), parse_mode=ParseMode.HTML)
                sent_count = 0
                for i, ep in enumerate(episodes, 1):
                    try:
                        file_id = ep.get('file_id')
                        caption = ep.get('title', 'Episode')
                        if file_id:
                            if ep.get('file_type') == 'video':
                                await context.bot.send_video(chat_id=user_id, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
                            else:
                                await context.bot.send_document(chat_id=user_id, document=file_id, caption=caption, parse_mode=ParseMode.HTML)
                        else:
                            await context.bot.forward_message(chat_id=user_id, from_chat_id=ep['channel_id'], message_id=ep['message_id'])
                            await context.bot.send_message(chat_id=user_id, text=caption, parse_mode=ParseMode.HTML)
                        sent_count += 1
                        if progress_msg and i % 3 == 0:
                            try:
                                await progress_msg.edit_text(add_footer(f"📦 <b>Re-watching {bundle['title']}</b>\n\n⏳ Sending {total} movies... ({i}/{total})\n<i>Please wait...</i>"), parse_mode=ParseMode.HTML)
                            except Exception:
                                pass
                    except Exception as e:
                        logger.error(f"My Movies bundle delivery error ep {i}: {e}")
                        await asyncio.sleep(BOT_FAST_DELAY)
                        continue
                    if i < total:
                        await asyncio.sleep(BOT_FAST_DELAY)
                if progress_msg:
                    try:
                        await progress_msg.delete()
                    except Exception:
                        pass
                series_bundles_collection.update_one({"token": bundle_token}, {"$inc": {"downloads": 1}})
                await context.bot.send_message(chat_id=user_id, text=add_footer(f"""🎬 <b>{bundle['title']}</b>

✅ {sent_count}/{total} movies delivered!
🔥 Enjoy watching 🍿"""), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🎬 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="watch_all_movies_0")], [InlineKeyboardButton(f"🌼 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")], [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ ᴛᴏ ᴍʏ ᴍᴏᴠɪᴇꜱ", callback_data="back")]]), parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"My Movies bundle delivery error: {e}")
                await context.bot.send_message(chat_id=user_id, text=add_footer(f"❌ Could not deliver bundle. Please try again."), reply_markup=build_back_keyboard("back"))
        else:
            await query.edit_message_text(add_footer(f"❌ Bundle not found. It may have been removed."), reply_markup=build_back_keyboard("back"))
        return
    
    if data.startswith("sel_"):
        await handle_select_bundle_callback(update, context)
        return

    if data == "claimfree":
        await show_claim_free(update, context)
        return
    if data.startswith("claimfree_select_"):
        token = data.replace("claimfree_select_", "")
        await claim_free_movie(update, context, token)
        return



    if data.startswith("setstyle_"):
        if user_id not in ADMIN_IDS:
            await query.answer("Admin only", show_alert=True)
            return
        style_key = data.replace("setstyle_", "")
        if style_key not in MENU_STYLES:
            await query.answer("Unknown style", show_alert=True)
            return
        set_default_menu_style(style_key)
        name = MENU_STYLES[style_key].get("name", style_key)
        await query.answer(f"Style set: {name}", show_alert=True)
        try:
            await query.edit_message_text(
                add_footer(f"✅ Default menu style set to:\n🎨 <b>{name}</b> (<code>{style_key}</code>)"),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="back")]])
            )
        except Exception:
            pass
        return

    if data.startswith("pending_pay_"):
        try:
            await query.answer("Type your M-Pesa number to pay")
            amount = 0
            parts = data.split("_")
            if len(parts) >= 4:
                try:
                    amount = int(parts[3])
                except Exception:
                    pass
            provider = get_active_payment_gateway()
            pend = None
            if payments_collection is not None:
                try:
                    pend = payments_collection.find_one({"user_id": user_id, "status": "pending"}, sort=[("created_at", DESCENDING)])
                except Exception:
                    pass
            if pend and amount <= 0:
                amount = int(pend.get("amount") or 0)
            ref = (pend or {}).get("reference") or f"PEND{user_id}{int(datetime.utcnow().timestamp())}"
            movie_token = (pend or {}).get("movie_token")
            series_token = (pend or {}).get("series_token")
            cart_tokens = (pend or {}).get("cart_tokens") or []
            is_cart = bool(cart_tokens) or bool((pend or {}).get("is_cart"))
            is_subscription = bool((pend or {}).get("is_subscription") or (pend or {}).get("type") == "subscription")
            plan = (pend or {}).get("plan")
            if pending_payhero_collection is not None:
                pending_payhero_collection.update_one(
                    {"user_id": user_id},
                    {"$set": {
                        "user_id": user_id, "reference": ref, "payhero_ref": ref,
                        "price": amount, "amount": amount, "status": "awaiting_phone",
                        "provider": provider, "movie_token": movie_token, "series_token": series_token,
                        "cart_tokens": cart_tokens, "is_cart": is_cart, "is_subscription": is_subscription,
                        "plan": plan, "chat_id": user_id, "created_at": datetime.utcnow(),
                    }},
                    upsert=True,
                )
            if provider == "paystack" and pending_stk_collection is not None:
                pending_stk_collection.update_one(
                    {"user_id": user_id},
                    {"$set": {
                        "user_id": user_id, "reference": ref, "stk_reference": f"stk_{ref}",
                        "price": amount, "status": "awaiting_phone",
                        "movie_token": movie_token, "series_token": series_token,
                        "cart_tokens": cart_tokens, "is_cart": is_cart, "is_subscription": is_subscription,
                        "plan": plan, "created_at": datetime.utcnow(),
                    }},
                    upsert=True,
                )
            context.user_data["awaiting_phone"] = True
            await context.bot.send_message(
                chat_id=user_id,
                text=add_footer(
                    f"📱 <b>Pay Now — {amount} KES</b>\n\n"
                    f"Type your M-Pesa number (e.g. <code>07XX XXX XXX</code>) and send it."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Cancel", callback_data="pending_cancel_" + str(user_id))]]),
            )
        except Exception as e:
            logger.error(f"pending_pay: {e}")
            logger.error(traceback.format_exc())
        return
    if data.startswith("pending_cancel_"):
        try:
            await query.answer("Cancelled")
            if pending_payhero_collection is not None:
                pending_payhero_collection.update_many(
                    {"user_id": user_id, "status": {"$in": ["pending", "awaiting_phone", "stk_sent"]}},
                    {"$set": {"status": "cancelled", "updated_at": datetime.utcnow()}},
                )
            if payments_collection is not None:
                payments_collection.update_many(
                    {"user_id": user_id, "status": "pending"},
                    {"$set": {"status": "cancelled", "updated_at": datetime.utcnow()}},
                )
            await query.edit_message_text(add_footer("✅ Pending payment cancelled. Browse movies anytime."), parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"pending_cancel: {e}")
        return
    if data == "skip_to_page":
        context.user_data["awaiting_skip_page"] = True
        await query.answer()
        await context.bot.send_message(
            chat_id=user_id,
            text=add_footer("🔢 <b>Skip to page</b>\n\nSend the page number you want (e.g. <code>5</code>)."),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Cancel", callback_data="back")]])
        )
        return
    if data.startswith("gp_"):
        # gp_{page}_{index} → resolve token from last_page_tokens
        try:
            parts = data.split("_")
            # gp, page, index
            idx_m = int(parts[-1])
            tokens = context.user_data.get("last_page_tokens") or []
            if 0 <= idx_m < len(tokens):
                token = tokens[idx_m]
                # reuse get_movie flow
                data = f"get_movie_{token}"
            else:
                await query.answer("List expired — open movies again", show_alert=True)
                return
        except Exception as e:
            logger.error(f"gp_ callback error: {e}")
            await query.answer("Error opening movie", show_alert=True)
            return
        # fall through by re-assigning — call get_movie path below
    if data.startswith("get_movie_"):
        token = data.replace("get_movie_", "")
        await handle_get_movie_callback(update, context, token)
        return

    if data.startswith("get_all_page_"):
        try:
            page = int(data.replace("get_all_page_", "").strip() or "0")
        except Exception:
            page = 0
        # ONLY the movies currently shown on this page (not whole library / old cart)
        page_tokens = context.user_data.get("last_page_tokens") or []
        movies = []
        if page_tokens:
            for tok in page_tokens:
                m = None
                if movies_collection is not None:
                    m = movies_collection.find_one({"token": tok})
                if not m and series_bundles_collection is not None:
                    m = series_bundles_collection.find_one({"token": tok})
                    if m:
                        m = dict(m)
                        m["is_bundle"] = True
                if m:
                    movies.append(m)
        if not movies:
            # fallback: paginated browse list for this page only
            mode = get_display_mode()
            per = 20 if mode == "button" else 9
            movies, _ = get_all_movies_paginated(page, per)
        movies = dedupe_movie_items(movies)  # no duplicate entries in GET ALL ONCE
        page_tokens = []
        for m in movies:
            t = m.get("token")
            if t and t not in page_tokens:
                page_tokens.append(t)
        has_sub, sub_type, expiry, used, max_movies = has_active_subscription(user_id)
        if has_sub:
            delivered = 0
            for m in movies:
                if max_movies > 0 and (used + delivered) >= max_movies:
                    await query.answer("Subscription limit reached!", show_alert=True)
                    await notify_limit_reached(user_id, sub_type or "subscription")
                    break
                try:
                    await forward_movie(update, context, m, is_subscriber=True)
                    delivered += 1
                except Exception as e:
                    logger.error(f"get_all_page deliver error: {e}")
            try:
                await query.answer(f"✅ Delivered {delivered} movies with your subscription!", show_alert=True)
            except Exception:
                pass
            return
        # Non-subscribers: pay for page items with buy-5-get-1 / buy-10-get-2 discount
        total = 0
        priced_items = []
        for m in movies:
            tok = m.get("token")
            if not tok:
                continue
            # For collections, use stored price (may already include part discount)
            price = int(m.get("price") or get_default_price() or 0)
            total += price
            priced_items.append({"token": tok, "title": m.get("title", "Movie"), "price": price})
        if total <= 0 or not priced_items:
            await query.answer("Nothing to pay on this page", show_alert=True)
            return
        prices_sorted = sorted(int(i["price"] or 0) for i in priced_items)
        n = len(priced_items)
        discount = 0
        if n >= 10:
            discount = prices_sorted[0] + prices_sorted[1]
        elif n >= 5:
            discount = prices_sorted[0]
        pay_total = max(0, total - discount)
        try:
            if discount > 0:
                await query.answer(f"✅ {n} items · {total}→{pay_total} KES (−{discount})", show_alert=False)
            else:
                await query.answer(f"Pay {pay_total} KES for {n} items…", show_alert=False)
        except Exception:
            pass
        context.user_data["awaiting_phone"] = True
        ref = f"PAGE{user_id}{int(datetime.utcnow().timestamp())}"
        track_pending_payment(user_id, ref)
        if payments_collection is not None:
            payments_collection.update_one(
                {"reference": ref},
                {"$set": {
                    "reference": ref, "user_id": user_id, "cart_tokens": page_tokens,
                    "is_cart": True, "amount": pay_total, "original_amount": total,
                    "discount": discount, "status": "pending",
                    "chat_id": query.message.chat.id if query.message else user_id,
                    "created_at": datetime.utcnow(), "auto_deliver": True,
                    "titles": [i["title"] for i in priced_items],
                }},
                upsert=True,
            )
        if pending_payhero_collection is not None:
            pending_payhero_collection.update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id, "reference": ref, "payhero_ref": ref,
                    "price": pay_total, "amount": pay_total, "is_cart": True,
                    "cart_tokens": page_tokens, "status": "awaiting_phone",
                    "provider": get_active_payment_gateway(),
                    "created_at": datetime.utcnow(),
                }},
                upsert=True,
            )
        titles_preview = ", ".join((i["title"][:18] for i in priced_items[:5]))
        if len(priced_items) > 5:
            titles_preview += f" +{len(priced_items)-5} more"
        extra = []
        if titles_preview:
            extra.append(titles_preview)
        if discount > 0:
            free_n = n // 5
            extra.append(f"🎁 Buy {n} — {free_n} free (−{discount} KES)")
        prompt = build_mpesa_prompt_text(
            pay_total,
            extra_lines=extra or None,
            user_id=user_id,
            original_amount=total if discount > 0 else None,
            discount=discount if discount > 0 else None,
        )
        await context.bot.send_message(
            chat_id=user_id,
            text=add_footer(prompt),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="back")]]),
        )
        return


    if data.startswith("mst_"):
        await master_pick_callback(update, context)
        return
    if data.startswith("master_select_"):
        await master_select_callback(update, context)
        return
    if data == "master_delete":
        await master_delete_callback(update, context)
        return
    if data == "master_edit":
        await master_edit_callback(update, context)
        return
    if data == "master_clear":
        await master_clear_callback(update, context)
        return
    if data.startswith("master_page_"):
        await master_page_callback(update, context)
        return
    if data.startswith("dcprice_"):
        await double_price_callback(update, context)
        return

    if data == "mybot_refresh":
        await handle_mybot_refresh(update, context)
        return

# ============ GET MOVIE CALLBACK HANDLER ============
async def handle_get_movie_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    user = update.effective_user
    user_id = user.id
    query = update.callback_query
    await query.answer()

    movie = None
    is_bundle = False
    cached = cache_get_token(token)
    if cached and cached[0] == 'movie':
        movie = cached[1]
    elif cached and cached[0] == 'series':
        movie = cached[1]
        is_bundle = True
    if movie is None and movies_collection is not None:
        movie = await run_in_thread(movies_collection.find_one, {"token": token})
        if movie:
            cache_set_token(token, movie, 'movie')
    if not movie and series_bundles_collection is not None:
        movie = await run_in_thread(series_bundles_collection.find_one, {"token": token})
        if movie:
            is_bundle = True
            cache_set_token(token, movie, 'series')

    if not movie:
        await query.edit_message_text("❌ Movie not found.")
        return

    if is_bundle:
        has_sub, sub_type, expiry, used, max_movies = await run_in_thread(has_active_subscription, user_id)
        if has_sub:
            if max_movies > 0 and used >= max_movies:
                await query.answer("Limit reached!", show_alert=True)
                await notify_limit_reached(user_id, sub_type or "subscription")
                return
            await forward_series(update, context, movie, is_subscriber=True)
            return
        # Paid bundle OR any ownership record → rewatch free
        if await run_in_thread(user_owns_movie, user_id, token):
            await forward_series(update, context, movie)
            return
        await show_movie_detail(update, context, token)
        return

    price = movie.get('price', get_default_price())
    has_sub, sub_type, expiry, used, max_movies = await run_in_thread(has_active_subscription, user_id)
    if has_sub:
        if max_movies > 0 and used >= max_movies:
            await query.answer("Limit reached!", show_alert=True)
            await notify_limit_reached(user_id, sub_type or "subscription")
            return
        await forward_movie(update, context, movie, is_subscriber=True)
        return
    # Single movie: paid alone, via cart, or as part of a paid bundle
    if price == 0 or await run_in_thread(user_owns_movie, user_id, token):
        await forward_movie(update, context, movie)
        return
    await show_movie_detail(update, context, token)

# ============ CLAIM FREE MOVIE ============
async def show_claim_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    user_doc = users_collection.find_one({"user_id": user_id})
    if not user_doc or not user_doc.get("free_available", False):
        text = add_footer("❌ You don't have a free movie available. Purchase 5 movies to earn one.")
        if query:
            await edit_html_safe(query.message, text, reply_markup=build_back_keyboard("back"))
        else:
            await reply_html_safe(update.message, text, reply_markup=build_back_keyboard("back"))
        return

    purchased_tokens = get_user_purchased_tokens(user_id)
    if not purchased_tokens:
        text = add_footer("❌ You haven't purchased any movies yet.")
        if query:
            await edit_html_safe(query.message, text, reply_markup=build_back_keyboard("back"))
        else:
            await reply_html_safe(update.message, text, reply_markup=build_back_keyboard("back"))
        return

    items = []
    items.append({"label": "🎁 CLAIM YOUR FREE MOVIE", "value": ""})
    items.append({"label": "Select one from your purchased movies:", "value": ""})
    items.append({"label": "", "value": ""})

    keyboard = []
    for token in purchased_tokens:
        movie = movies_collection.find_one({"token": token})
        if movie:
            title = clean_movie_title_for_button(movie.get('title', 'Unknown'))
            items.append({"label": f"▪︎ {title}", "value": ""})
            keyboard.append([InlineKeyboardButton(title, callback_data=f"claimfree_select_{token}")])

    if not keyboard:
        text = add_footer("❌ No purchased movies found.")
        if query:
            await edit_html_safe(query.message, text, reply_markup=build_back_keyboard("back"))
        else:
            await reply_html_safe(update.message, text, reply_markup=build_back_keyboard("back"))
        return

    text = render_menu_for_user(user_id, "🎁 CLAIM FREE MOVIE", items)
    text = add_footer(text)
    text += "\n\nTap a movie to claim it for free. You can only claim one."

    kb = InlineKeyboardMarkup(keyboard)
    if query:
        await edit_html_safe(query.message, text, reply_markup=kb)
    else:
        await reply_html_safe(update.message, text, reply_markup=kb)


async def claim_free_movie(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    user = update.effective_user
    user_id = user.id
    query = update.callback_query
    await query.answer()

    user_doc = users_collection.find_one({"user_id": user_id})
    if not user_doc or not user_doc.get("free_available", False):
        await edit_html_safe(query.message, add_footer("❌ You already claimed your free movie or it's not available."))
        return

    purchased_tokens = get_user_purchased_tokens(user_id)
    if token not in purchased_tokens:
        await edit_html_safe(query.message, add_footer("❌ You haven't purchased this movie."))
        return

    await edit_html_safe(query.message, add_footer("🎁 Delivering your free movie..."))
    await auto_deliver_movie(user_id, token, context)

    users_collection.update_one({"user_id": user_id}, {"$set": {"free_available": False}})

    await send_html_safe(context.bot, user_id, add_footer("🎉 Congratulations! You've used your free movie reward.\nPurchase 5 more movies to earn another!"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📡 More Movies", callback_data="more_movies")]]))

# ============ PHONE INPUT HANDLER ============
async def handle_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Channel posts / non-private updates have no update.message — ignore
    if not update.message or not update.effective_user:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        return
    user = update.effective_user
    phone = (update.message.text or "").strip()
    context.user_data.pop("phone_input_handled", None)
    await human_action(context, update.effective_chat.id, ChatAction.TYPING)
    try:
        if await handle_pending_payment_phone_input(update, context):
            return
    except Exception as e:
        logger.error(f"pending_payment phone handler error (continuing): {e}")
    if context.user_data.get("awaiting_search"):
        return
    # pending_payhero_collection is used for PayHero + Makamesco + dashboard pending
    # Do NOT gate on get_payhero_enabled() — Makamesco/pending would never fire
    if pending_payhero_collection is not None:
        pending_ph = pending_payhero_collection.find_one({"user_id": user.id, "status": "awaiting_phone"})
        # If no armed row but user just got a dashboard pending reminder, auto-arm from payments
        if pending_ph is None and _looks_like_phone_number(phone) and payments_collection is not None:
            try:
                pend = payments_collection.find_one(
                    {"user_id": user.id, "status": "pending"},
                    sort=[("created_at", DESCENDING)],
                )
                if pend:
                    provider = pend.get("provider") or get_active_payment_gateway()
                    amount = int(pend.get("amount") or pend.get("price") or 0)
                    ref = pend.get("reference") or f"PEND{user.id}{int(datetime.utcnow().timestamp())}"
                    pending_payhero_collection.update_one(
                        {"user_id": user.id},
                        {"$set": {
                            "user_id": user.id,
                            "reference": ref,
                            "payhero_ref": ref,
                            "price": amount,
                            "amount": amount,
                            "status": "awaiting_phone",
                            "provider": provider,
                            "movie_token": pend.get("movie_token"),
                            "series_token": pend.get("series_token"),
                            "cart_tokens": pend.get("cart_tokens") or [],
                            "is_cart": bool(pend.get("is_cart") or pend.get("cart_tokens")),
                            "is_subscription": bool(pend.get("is_subscription") or pend.get("type") == "subscription"),
                            "plan": pend.get("plan"),
                            "chat_id": update.effective_chat.id,
                            "created_at": datetime.utcnow(),
                            "from_phone_auto_arm": True,
                        }},
                        upsert=True,
                    )
                    pending_ph = pending_payhero_collection.find_one({"user_id": user.id, "status": "awaiting_phone"})
                    logger.info(f"Auto-armed pending from payments for user={user.id} provider={provider} amount={amount}")
            except Exception as e:
                logger.error(f"auto-arm pending from payments: {e}")
        if pending_ph is not None:
            if not _looks_like_phone_number(phone):
                return
            logger.info(f"Phone → gateway path provider={pending_ph.get('provider')} user={user.id}")
            await handle_payhero_phone_input(update, context, user, phone, pending_ph)
            return
    if pending_stk_collection is None:
        return
    pending = pending_stk_collection.find_one({"user_id": user.id, "status": "awaiting_phone"})
    if pending is None:
        return
    if not _looks_like_phone_number(phone):
        return
    if not is_valid_phone(phone):
        err_text = add_footer(f"""❌ <b>Invalid phone number format</b>

ℹ️ Please use a valid Kenyan number:
📱 <code>0712 345 678</code> ✅ Safaricom
📱 <code>0733 123 456</code> ✅ Airtel
📱 <code>0112 345 678</code> ✅ Airtel

🔄 Try again:""")
        await update.message.reply_text(err_text, reply_markup=build_back_keyboard("back"), parse_mode=ParseMode.HTML)
        return
    formatted_phone = format_phone_number(phone)
    carrier, provider = get_carrier_name(phone)
    stk_ref = pending.get('stk_reference') or pending['reference']
    movie_token = pending.get('movie_token')
    series_token = pending.get('series_token')
    is_subscription = pending.get('is_subscription', False)
    plan = pending.get('plan')
    is_cart = pending.get('is_cart', False)
    cart_tokens = pending.get('cart_tokens', [])
    logger.info(f"Phone input: user={user.id}, movie={movie_token}, series={series_token}, is_cart={is_cart}, plan={plan}")
    result, error = initiate_mpesa_stk_push(phone=formatted_phone, amount=pending['price'], reference=stk_ref, metadata={"user_id": user.id, "movie_token": movie_token, "series_token": series_token, "chat_id": update.effective_chat.id, "auto_deliver": True, "is_subscription": is_subscription, "plan": plan, "original_reference": pending['reference'], "is_cart": is_cart, "cart_tokens": cart_tokens})
    if error:
        await reply_html_safe(update.message, add_footer(f"❌ {error}"), reply_markup=build_back_keyboard("back"))
        pending_stk_collection.delete_one({"user_id": user.id})
        return
    pending_stk_collection.update_one({"user_id": user.id}, {"$set": {"status": "stk_sent", "phone": formatted_phone, "paystack_ref": stk_ref, "carrier": result.get('carrier'), "provider": result.get('provider'), "sent_at": datetime.utcnow()}})
    context.user_data["phone_input_handled"] = True
    carrier = result.get('carrier', 'Unknown')
    provider_display = "M-Pesa" if result.get('provider') == 'mpesa' else "Airtel Money"
    prompt_name = PAYMENT_PROMPT_NAME or "payment"
    mini = add_footer(render_menu_for_user(user.id, "📲 M-Pesa Prompt Sent", [
        {"label": "Phone", "value": formatted_phone},
        {"label": "Amount", "value": f"{pending['price']} KES"},
        {"label": "", "value": ""},
        {"label": "Enter your M-Pesa PIN on the phone prompt to complete.", "value": ""},
        {"label": "Content delivers instantly after confirmation.", "value": ""},
    ]))
    await update.message.reply_text(
        mini,
        reply_markup=build_back_keyboard("back"),
        parse_mode=ParseMode.HTML
    )
    context.job_queue.run_repeating(check_stk_status_job, interval=8, first=8, data={"user_id": user.id, "reference": stk_ref, "chat_id": update.effective_chat.id, "movie_token": movie_token, "series_token": series_token, "is_subscription": is_subscription, "plan": plan, "is_cart": is_cart, "cart_tokens": cart_tokens}, name=f"stk_{user.id}")

async def handle_payhero_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE, user, phone, pending):
    if not is_valid_phone(phone):
        err_text = add_footer(f"""❌ <b>Invalid phone number format</b>

ℹ️ Please use a valid Kenyan number:
📱 <code>0712 345 678</code> ✅ Safaricom
📱 <code>0733 123 456</code> ✅ Airtel
📱 <code>0112 345 678</code> ✅ Airtel

🔄 Try again:""")
        await update.message.reply_text(err_text, reply_markup=build_back_keyboard("back"), parse_mode=ParseMode.HTML)
        return
    formatted_phone = format_phone_number(phone)
    payhero_ref = pending.get('payhero_ref') or pending['reference']
    movie_token = pending.get('movie_token')
    series_token = pending.get('series_token')
    is_subscription = pending.get('is_subscription', False)
    plan = pending.get('plan')
    price = pending.get('price', 0)
    is_cart = pending.get('is_cart', False)
    cart_tokens = pending.get('cart_tokens', [])
    provider = pending.get('provider') or get_active_payment_gateway()
    logger.info(f"{provider} phone input: user={user.id}, movie={movie_token}, series={series_token}, is_cart={is_cart}, plan={plan}")
    customer_name = user.first_name or "Customer"
    if provider == "makamesco":
        callback_url = MAKAMESCO_CALLBACK_URL or f"{WEBHOOK_URL}/makamesco-callback"
        result, error = initiate_makamesco_stk_push(
            phone=formatted_phone,
            amount=price,
            external_reference=payhero_ref,
            callback_url=callback_url,
            customer_name=customer_name
        )
        payment_method = "makamesco"
    elif provider == "paystack":
        stk_ref = pending.get("stk_reference") or f"stk_{payhero_ref}"
        result, error = initiate_mpesa_stk_push(
            phone=formatted_phone,
            amount=price,
            reference=stk_ref,
            metadata={
                "user_id": user.id,
                "movie_token": movie_token,
                "series_token": series_token,
                "chat_id": update.effective_chat.id,
                "auto_deliver": True,
                "is_subscription": is_subscription,
                "plan": plan,
                "original_reference": pending.get("reference"),
                "is_cart": is_cart,
                "cart_tokens": cart_tokens,
            },
        )
        payment_method = "paystack"
        if not error and result:
            if pending_stk_collection is not None:
                pending_stk_collection.update_one(
                    {"user_id": user.id},
                    {"$set": {
                        "user_id": user.id, "reference": pending.get("reference"),
                        "stk_reference": stk_ref, "price": price, "status": "stk_sent",
                        "phone": formatted_phone, "movie_token": movie_token,
                        "series_token": series_token, "cart_tokens": cart_tokens,
                        "is_cart": is_cart, "is_subscription": is_subscription, "plan": plan,
                        "sent_at": datetime.utcnow(),
                    }},
                    upsert=True,
                )
            try:
                context.job_queue.run_repeating(
                    check_stk_status_job, interval=8, first=8,
                    data={
                        "user_id": user.id, "reference": stk_ref,
                        "chat_id": update.effective_chat.id,
                        "movie_token": movie_token, "series_token": series_token,
                        "is_subscription": is_subscription, "plan": plan,
                        "is_cart": is_cart, "cart_tokens": cart_tokens,
                    },
                    name=f"stk_{user.id}",
                )
            except Exception as je:
                logger.warning(f"paystack job schedule: {je}")
    else:
        callback_url = f"{WEBHOOK_URL}/api/payhero/callback"
        result, error = initiate_payhero_stk_push(
            phone=formatted_phone,
            amount=price,
            external_reference=payhero_ref,
            callback_url=callback_url,
            customer_name=customer_name,
            channel_id=get_payhero_channel_id()
        )
        payment_method = "payhero"
    if error:
        await update.message.reply_text(add_footer(f"❌ <b>Payment Error:</b> {error}"), reply_markup=build_back_keyboard("back"), parse_mode=ParseMode.HTML)
        return
    checkout_id = (result or {}).get('checkout_request_id', '') or (result or {}).get('reference', '')
    pending_payhero_collection.update_one({"user_id": user.id}, {"$set": {"status": "stk_sent", "phone": formatted_phone, "checkout_request_id": checkout_id, "channel_id": (result or {}).get('channel_id'), "provider": provider, "sent_at": datetime.utcnow()}})
    context.user_data["phone_input_handled"] = True
    prompt_name = PAYMENT_PROMPT_NAME or "payment"
    # Minimal clean prompt as requested
    stk_items = [
        {"label": "Phone", "value": formatted_phone},
        {"label": "Amount", "value": f"{price} KES"},
        {"label": "", "value": ""},
        {"label": "Enter your M-Pesa PIN on the prompt to complete payment.", "value": ""},
        {"label": "Content delivers instantly after confirmation.", "value": ""},
    ]
    stk_text = render_menu_for_user(user.id, "📲 M-Pesa Prompt Sent", stk_items)
    await update.message.reply_text(
        add_footer(stk_text),
        reply_markup=build_back_keyboard("back"),
        parse_mode=ParseMode.HTML
    )
    if payments_collection is not None:
        payments_collection.update_one({"reference": pending.get('reference')}, {"$set": {"user_id": user.id, "movie_token": movie_token, "series_token": series_token, "chat_id": update.effective_chat.id, "amount": price, "status": "pending", "payment_method": payment_method, "is_subscription": is_subscription, "plan": plan, "is_cart": is_cart, "cart_tokens": cart_tokens, "updated_at": datetime.utcnow()}}, upsert=True)
    if provider == "payhero":
        context.job_queue.run_repeating(check_payhero_status_job, interval=10, first=10, data={"user_id": user.id, "reference": payhero_ref, "chat_id": update.effective_chat.id, "movie_token": movie_token, "series_token": series_token, "is_subscription": is_subscription, "plan": plan, "is_cart": is_cart, "cart_tokens": cart_tokens}, name=f"payhero_{user.id}")
    elif provider == "makamesco":
        # Backup: if webhook never arrives, poll pending and deliver after delay when status completed externally
        # Also: soft reminder — re-check pending after 45s and log
        try:
            context.job_queue.run_once(
                check_makamesco_pending_job,
                when=45,
                data={
                    "user_id": user.id,
                    "reference": payhero_ref,
                    "chat_id": update.effective_chat.id,
                    "movie_token": movie_token,
                    "series_token": series_token,
                    "is_subscription": is_subscription,
                    "plan": plan,
                    "is_cart": is_cart,
                    "cart_tokens": cart_tokens,
                    "checkout_id": checkout_id,
                },
                name=f"makamesco_chk_{user.id}",
            )
        except Exception as e:
            logger.warning(f"Could not schedule makamesco check job: {e}")

# ============ STK STATUS CHECK JOBS ============
async def check_makamesco_pending_job(context: ContextTypes.DEFAULT_TYPE):
    """Backup delivery if Makamesco webhook was missed. Does not charge again — only delivers if still pending."""
    try:
        job_data = context.job.data or {}
        user_id = job_data.get("user_id")
        if not user_id or pending_payhero_collection is None:
            return
        pending = pending_payhero_collection.find_one({"user_id": user_id})
        if not pending:
            return
        if pending.get("status") in ("completed", "failed", "cancelled"):
            return
        # Still open after 45s — remind user; webhook may still arrive
        chat_id = job_data.get("chat_id") or user_id
        logger.info(f"Makamesco pending still open after 45s user={user_id} status={pending.get('status')}")
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=add_footer(
                    "⏳ Still waiting for M-Pesa confirmation.\n"
                    "If you already entered your PIN, content will arrive shortly.\n"
                    "If not, complete the prompt on your phone."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=build_back_keyboard("back"),
            )
        except Exception:
            pass
    except Exception as e:
        logger.error(f"check_makamesco_pending_job: {e}")


async def check_payhero_status_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id = job_data['user_id']
    chat_id = job_data['chat_id']
    logger.info(f"PayHero status check: user={user_id}")
    if pending_payhero_collection is None:
        context.job.schedule_removal()
        return
    pending = pending_payhero_collection.find_one({"user_id": user_id})
    if not pending or pending.get('status') in ('completed', 'failed'):
        context.job.schedule_removal()
        return
    checkout_id = pending.get('checkout_request_id')
    if not checkout_id:
        checkout_id = pending.get('payhero_ref')
    if not checkout_id:
        context.job.schedule_removal()
        return
    status_data = check_payhero_transaction_status(checkout_id)
    if not status_data:
        return
    response = status_data.get("response", {})
    if not response and status_data:
        response = status_data
    status = str(response.get("Status", "")).lower()
    result_code = response.get("ResultCode", -1)
    mpesa_receipt = response.get("MpesaReceiptNumber", "")
    amount = response.get("Amount", 0)
    if status == "success" or result_code == 0:
        logger.info(f"PayHero payment SUCCESS: ref={pending.get('payhero_ref')}, receipt={mpesa_receipt}")
        if payments_collection is not None:
            payments_collection.update_one({"reference": pending.get('reference')}, {"$set": {"status": "success", "paid_at": datetime.utcnow(), "payment_method": "payhero", "mpesa_receipt": mpesa_receipt}})
        clear_pending_payment_tracker(user_id)
        pending_payhero_collection.update_one({"_id": pending["_id"]}, {"$set": {"status": "completed", "completed_at": datetime.utcnow(), "mpesa_receipt": mpesa_receipt}})
        movie_token = pending.get('movie_token')
        series_token = pending.get('series_token')
        is_subscription = pending.get('is_subscription', False)
        plan = pending.get('plan')
        is_cart = pending.get('is_cart', False)
        cart_tokens = pending.get('cart_tokens', [])
        if is_subscription and plan:
            create_subscription(user_id, plan, pending.get('payhero_ref'))
            await notify_subscription_activated(user_id, plan, context)
        elif is_cart or cart_tokens:
            if cart_tokens:
                cart_items_for_delivery = [{"token": t} for t in cart_tokens]
                asyncio.create_task(auto_deliver_cart(user_id, cart_items_for_delivery))
            else:
                cart_doc = cart_collection.find_one({"user_id": user_id}) if cart_collection is not None else None
                if cart_doc:
                    cart_items = cart_doc.get("items", [])
                    asyncio.create_task(auto_deliver_cart(user_id, cart_items))
        elif series_token:
            asyncio.create_task(auto_deliver_series(user_id, series_token, context))
        elif movie_token:
            asyncio.create_task(auto_deliver_movie(user_id, movie_token, context))
        else:
            try:
                await context.bot.send_message(chat_id=chat_id, text=add_footer(f"✅ Payment of {amount:.0f} KES confirmed!\n📱 Receipt: <code>{mpesa_receipt}</code>"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🔈 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")], [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]]))
            except Exception as e:
                logger.error(f"PayHero success notify error: {e}")
        context.job.schedule_removal()
    elif status in ('failed', 'cancelled') or result_code not in (0, -1):
        logger.warning(f"PayHero payment FAILED: ref={pending.get('payhero_ref')}, result_code={result_code}")
        if payments_collection is not None:
            payments_collection.update_one({"reference": pending.get('reference')}, {"$set": {"status": "failed", "updated_at": datetime.utcnow()}})
        pending_payhero_collection.update_one({"_id": pending["_id"]}, {"$set": {"status": "failed", "failed_at": datetime.utcnow()}})
        await send_payment_failure_warning(pending.get('reference'), user_id, chat_id)
        context.job.schedule_removal()

async def check_stk_status_job(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    user_id = job_data['user_id']
    stk_ref = job_data['reference']
    chat_id = job_data['chat_id']
    movie_token = job_data.get('movie_token')
    series_token = job_data.get('series_token')
    is_subscription = job_data.get('is_subscription', False)
    plan = job_data.get('plan')
    is_cart = job_data.get('is_cart', False)
    cart_tokens = job_data.get('cart_tokens', [])
    logger.info(f"STK check: user={user_id}, ref={stk_ref}, movie={movie_token}, series={series_token}, is_cart={is_cart}")
    if payments_collection is None or pending_stk_collection is None:
        context.job.schedule_removal()
        return
    pending = pending_stk_collection.find_one({"user_id": user_id})
    original_ref = pending.get('reference') if pending else None
    verify = verify_paystack_payment(stk_ref)
    if verify and verify.get('status') and verify['data']['status'] == 'success':
        logger.info(f"STK payment SUCCESS: ref={stk_ref}")
        # Stop any mistaken PayHero poller for this user
        try:
            for j in list(context.job_queue.jobs()):
                if j.name == f"payhero_{user_id}":
                    j.schedule_removal()
        except Exception:
            pass
        clear_pending_payment_tracker(user_id)
        if original_ref and payments_collection is not None:
            payments_collection.update_one({"reference": original_ref}, {"$set": {"status": "success", "paid_at": datetime.utcnow(), "payment_method": "mpesa", "stk_reference": stk_ref}})
        # Also try to recover plan/is_subscription from DB if job_data missed them
        if not is_subscription or not plan:
            try:
                prec = payments_collection.find_one({"reference": original_ref}) if original_ref else None
                if prec:
                    if prec.get("is_subscription") or prec.get("type") == "subscription":
                        is_subscription = True
                        plan = prec.get("plan") or plan
                    if not movie_token:
                        movie_token = prec.get("movie_token")
                    if not series_token:
                        series_token = prec.get("series_token")
                    if not cart_tokens:
                        cart_tokens = prec.get("cart_tokens", [])
                        is_cart = bool(cart_tokens)
            except Exception as e:
                logger.error(f"STK recovery lookup error: {e}")

        if is_subscription and plan:
            create_subscription(user_id, plan, original_ref or stk_ref)
            await notify_subscription_activated(user_id, plan, context)
        elif is_cart or cart_tokens:
            if cart_tokens:
                cart_items_for_delivery = [{"token": t} for t in cart_tokens]
                asyncio.create_task(auto_deliver_cart(user_id, cart_items_for_delivery, context))
            else:
                cart_doc = cart_collection.find_one({"user_id": user_id}) if cart_collection is not None else None
                if cart_doc:
                    cart_items = cart_doc.get("items", [])
                    asyncio.create_task(auto_deliver_cart(user_id, cart_items, context))
        elif series_token:
            asyncio.create_task(auto_deliver_series(user_id, series_token, context))
        elif movie_token:
            asyncio.create_task(auto_deliver_movie(user_id, movie_token, context))
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Payment confirmed. If your content did not arrive, contact admin.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("More Movies", callback_data="more_movies")],
                    [InlineKeyboardButton("Back", callback_data="back")]
                ]),
                parse_mode=None
            )
        pending_stk_collection.delete_one({"user_id": user_id})
        context.job.schedule_removal()
    elif verify and verify.get('data', {}).get('status') in ['failed', 'abandoned', 'cancelled']:
        await send_payment_failure_warning(original_ref or stk_ref, user_id, chat_id)
        pending_stk_collection.delete_one({"user_id": user_id})
        context.job.schedule_removal()

# ============ OTHER HANDLERS ============
ITEMS_PER_PAGE_MY_MOVIES = 10

async def show_my_movies_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    query = update.callback_query
    if query:
        await query.answer()
    user_id = update.effective_user.id
    bot_username = context.bot.username
    
    if not mongodb_connected or payments_collection is None or movies_collection is None:
        text = add_footer(f"⚠️ Bot is in maintenance mode.")
        kb = build_back_keyboard("back")
        if query:
            await edit_or_send_message(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    purchased_movies = list(payments_collection.find({"user_id": user_id, "status": "success", "movie_token": {"$exists": True, "$ne": None}}).sort("paid_at", DESCENDING))
    purchased_bundles = list(payments_collection.find({"user_id": user_id, "status": "success", "series_token": {"$exists": True, "$ne": None}}).sort("paid_at", DESCENDING))
    purchased_carts = list(payments_collection.find({"user_id": user_id, "status": "success", "cart_tokens": {"$exists": True, "$ne": []}}).sort("paid_at", DESCENDING))
    
    seen_movie_tokens = set()
    unique_movies = []
    for p in purchased_movies:
        token = p.get("movie_token")
        if token and token not in seen_movie_tokens:
            seen_movie_tokens.add(token)
            unique_movies.append(p)
    seen_bundle_tokens = set()
    unique_bundles = []
    for p in purchased_bundles:
        token = p.get("series_token")
        if token and token not in seen_bundle_tokens:
            seen_bundle_tokens.add(token)
            unique_bundles.append(p)
    seen_cart_tokens = set()
    unique_cart_items = []
    for p in purchased_carts:
        cart_tokens = p.get('cart_tokens') or []
        for token in cart_tokens:
            if token not in seen_cart_tokens:
                seen_cart_tokens.add(token)
                movie = movies_collection.find_one({"token": token})
                if movie:
                    unique_cart_items.append({"type": "movie", "data": movie, "token": token})
                else:
                    bundle = series_bundles_collection.find_one({"token": token}) if series_bundles_collection is not None else None
                    if bundle:
                        unique_cart_items.append({"type": "bundle", "data": bundle, "token": token})
    
    total_purchases = len(unique_movies) + len(unique_bundles) + len(unique_cart_items)
    if total_purchases == 0:
        text = add_footer(f"😢 You haven't purchased any movies yet.")
        kb = build_back_keyboard("back")
        if query:
            await edit_or_send_message(query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    start = page * ITEMS_PER_PAGE_MY_MOVIES
    end = start + ITEMS_PER_PAGE_MY_MOVIES
    total_pages = (total_purchases + ITEMS_PER_PAGE_MY_MOVIES - 1) // ITEMS_PER_PAGE_MY_MOVIES
    if page >= total_pages:
        page = total_pages - 1
        start = page * ITEMS_PER_PAGE_MY_MOVIES
        end = start + ITEMS_PER_PAGE_MY_MOVIES

    all_items = []
    for purchase in unique_movies:
        movie = movies_collection.find_one({"token": purchase["movie_token"]})
        if movie:
            all_items.append({"type": "movie", "data": movie, "token": purchase["movie_token"]})
    for purchase in unique_bundles:
        bundle = series_bundles_collection.find_one({"token": purchase["series_token"]}) if series_bundles_collection is not None else None
        if bundle:
            all_items.append({"type": "bundle", "data": bundle, "token": purchase["series_token"]})
    for item in unique_cart_items:
        all_items.append(item)
    page_items = all_items[start:end]

    items = []
    items.append({"label": f"MY MOVIES ({total_purchases})", "value": ""})
    items.append({"label": "", "value": ""})
    
    keyboard = []
    idx = start + 1
    for item in page_items:
        if item["type"] == "movie":
            movie = item["data"]
            title = clean_movie_title_for_button(movie.get("title", "Unknown Movie"))
            token = item["token"]
            get_callback = f"get_movie_{token}"
            items.append({"label": f"{idx}. {title}", "value": ""})
            keyboard.append([InlineKeyboardButton(f"🚀 Watch: {title[:30]}{'...' if len(title)>30 else ''}", callback_data=get_callback)])
        elif item["type"] == "bundle":
            bundle = item["data"]
            title = clean_movie_title_for_button(bundle.get("title", "Unknown Collection"))
            token = item["token"]
            get_callback = f"get_movie_{token}"
            ep_count = bundle.get("episode_count", len(bundle.get("episodes", [])))
            items.append({"label": f"{idx}. 📦 {title}", "value": f"({ep_count})"})
            keyboard.append([InlineKeyboardButton(f"📦 Watch Bundle: {title[:26]}{'...' if len(title)>26 else ''}", callback_data=get_callback)])
        idx += 1

    items.append({"label": "", "value": ""})
    items.append({"label": f"Page {page+1} of {total_pages}", "value": ""})
    
    movies_text = render_menu_for_user(user_id, "MY MOVIES", items)
    movies_text = add_footer(movies_text)
    movies_text += get_datetime_footer()
    
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀ ᴘʀᴇᴠ", callback_data=f"mymovies_page_{page-1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶ ɴᴇxᴛ", callback_data=f"mymovies_page_{page+1}"))
    if nav_row:
        keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton(f"🎬 Browse More Movies", callback_data="watch_all_movies_0")])
    keyboard.append([InlineKeyboardButton(f"🌼 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")])
    keyboard.append([InlineKeyboardButton(f"🏠 ʜᴏᴍᴇ", callback_data="go_home")])
    keyboard.append([InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")])

    movies_kb = InlineKeyboardMarkup(keyboard)
    if query:
        await edit_or_send_message(query, movies_text, reply_markup=movies_kb, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(movies_text, reply_markup=movies_kb, parse_mode=ParseMode.HTML)
    push_nav_state(context, "my_movies", movies_text, movies_kb)

async def deliver_all_my_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer(f"🚀 Delivering all your movies...", show_alert=True)
    except Exception:
        pass
    user_id = update.effective_user.id
    if not mongodb_connected or payments_collection is None or movies_collection is None:
        await query.edit_message_text(add_footer(f"⚠️ Bot is in maintenance mode."), reply_markup=build_back_keyboard("back"))
        return
    purchased_movies = list(payments_collection.find({"user_id": user_id, "status": "success", "movie_token": {"$exists": True, "$ne": None}}).sort("paid_at", DESCENDING))
    seen_movie_tokens = set()
    unique_movies = []
    for p in purchased_movies:
        token = p.get("movie_token")
        if token and token not in seen_movie_tokens:
            seen_movie_tokens.add(token)
            unique_movies.append(p)
    total = len(unique_movies)
    if total == 0:
        await query.edit_message_text(add_footer(f"😢 No purchased movies found."), reply_markup=build_back_keyboard("back"))
        return
    delivered = 0
    failed = 0
    try:
        progress_msg = await context.bot.send_message(chat_id=user_id, text=add_footer(f"📦 <b>Delivering Your Movies</b>\n\n⏳ Sending {total} movies... (0/{total})\n<i>Please wait...</i>"), parse_mode=ParseMode.HTML)
    except Exception:
        progress_msg = None
    for i, purchase in enumerate(unique_movies, 1):
        movie = movies_collection.find_one({"token": purchase.get("movie_token", "")})
        if not movie:
            failed += 1
            continue
        try:
            file_id = movie.get('file_id')
            caption = movie['title']
            if file_id:
                if movie.get('file_type') == 'video':
                    await context.bot.send_video(chat_id=user_id, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
                else:
                    await context.bot.send_document(chat_id=user_id, document=file_id, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await context.bot.forward_message(chat_id=user_id, from_chat_id=movie["channel_id"], message_id=movie["message_id"])
                await context.bot.send_message(chat_id=user_id, text=caption, parse_mode=ParseMode.HTML)
            movies_collection.update_one({"token": purchase["movie_token"]}, {"$inc": {"downloads": 1}})
            delivered += 1
            if progress_msg and i % 3 == 0:
                try:
                    await progress_msg.edit_text(add_footer(f"📦 <b>Delivering Your Movies</b>\n\n⏳ Sending {total} movies... ({i}/{total})\n<i>Please wait...</i>"), parse_mode=ParseMode.HTML)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"MyMovies bulk delivery error {i}: {e}")
            failed += 1
            await asyncio.sleep(BOT_FAST_DELAY)
            continue
        if i < total:
            await asyncio.sleep(BOT_FAST_DELAY)
    if progress_msg:
        try:
            await progress_msg.delete()
        except Exception:
            pass
    completion_text = add_footer(f"🎬 <b>All Movies Delivered!</b> 🍿\n\n✅ <b>{delivered}/{total}</b> movies delivered successfully!")
    if failed > 0:
        completion_text += f"\n⚠️ {failed} movie(s) failed to deliver."
    completion_text += f"\n\n🔥 Enjoy watching!"
    await context.bot.send_message(chat_id=user_id, text=completion_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🎬 ʙʀᴏᴡꜱᴇ ᴀʟʟ ᴍᴏᴠɪᴇꜱ", callback_data="watch_all_movies_0")], [InlineKeyboardButton(f"🌼 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")], [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]]), parse_mode=ParseMode.HTML)

async def cart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_bg(user.id, user.username, user.first_name, user.last_name)
    await show_cart_page(update, context)

async def movies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_bg(user.id, user.username, user.first_name, user.last_name)
    await show_movies_page(update, context, 0)

async def browse_bundles_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_bg(user.id, user.username, user.first_name, user.last_name)
    await show_bundles_page(update, context, 0)

async def show_bundles_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    user_id = update.effective_user.id
    mode = get_display_mode()
    per_page = 20 if mode == "button" else 9
    if not mongodb_connected or series_bundles_collection is None:
        no_text = add_footer(f"⚠️ Bot is in maintenance mode.")
        no_kb = build_back_keyboard("back")
        if update.message:
            await update.message.reply_text(no_text, reply_markup=no_kb)
        elif update.callback_query:
            await edit_or_send_message(update.callback_query, no_text, reply_markup=no_kb)
        return
    def _fetch_bundles():
        t = series_bundles_collection.count_documents({})
        b = list(series_bundles_collection.find().sort("created_at", DESCENDING).skip(page * per_page).limit(per_page))
        return t, b
    total, bundles = await asyncio.to_thread(_fetch_bundles)
    if not bundles:
        no_text = add_footer(f"😢 No bundles available yet. Check back soon!")
        no_kb = build_back_keyboard("back")
        if update.message:
            await update.message.reply_text(no_text, reply_markup=no_kb)
        elif update.callback_query:
            query = update.callback_query
            try:
                await query.edit_message_text(no_text, reply_markup=no_kb)
            except Exception:
                await context.bot.send_message(chat_id=user_id, text=no_text, reply_markup=no_kb)
        push_nav_state(context, "bundles_empty", no_text, no_kb)
        return
    for b in bundles:
        b['is_bundle'] = True
        if 'poster_url' not in b:
            b['poster_url'] = ''
    await send_poster_grid(update, context, bundles, page, total, "", is_search=False)

async def show_movie_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, movie_token: str):
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    if not mongodb_connected or movies_collection is None:
        err_text = add_footer(f"⚠️ Bot is in maintenance mode.")
        err_kb = build_back_keyboard("back")
        if query:
            await query.edit_message_text(err_text, reply_markup=err_kb)
        return
    movie = None
    cached = cache_get_token(movie_token)
    if cached:
        movie = cached[1]
    if movie is None:
        movie = await run_in_thread(movies_collection.find_one, {"token": movie_token})
        if movie:
            cache_set_token(movie_token, movie, 'movie')
    if not movie and series_bundles_collection is not None:
        series = await run_in_thread(series_bundles_collection.find_one, {"token": movie_token})
        if series:
            cache_set_token(movie_token, series, 'series')
            if query:
                await query.answer()
            has_sub, sub_type, expiry, used, max_movies = await run_in_thread(has_active_subscription, user_id)
            if has_sub:
                await forward_series(update, context, series, is_subscriber=True)
                return
            existing_payment = None
            if payments_collection is not None:
                existing_payment = await run_in_thread(payments_collection.find_one, {"user_id": user_id, "series_token": movie_token, "status": "success"})
            if existing_payment:
                await forward_series(update, context, series)
                return
            price = series.get('price', get_default_price())
            ep_count = series.get('episode_count', len(series.get('episodes', [])))
            
            items = []
            items.append({"label": f"📦 {series['title']}", "value": f"({ep_count} movies)"})
            items.append({"label": "💰 Bundle Price:", "value": f"{price} KES"})
            _eps_inside = series.get('episodes', []) or []
            if _eps_inside:
                items.append({"label": "🎬 Movies inside:", "value": ""})
                for _i, _ep in enumerate(_eps_inside[:25], 1):
                    _et = (_ep.get('button_title') or _ep.get('title') or "").strip()
                    # If title is empty or just a number, try movie lookup / Part N
                    if (not _et) or re.fullmatch(r"\d+", _et):
                        _tok = _ep.get("token")
                        if _tok and movies_collection is not None:
                            try:
                                _m = movies_collection.find_one({"token": _tok}, {"title": 1, "button_title": 1})
                                if _m:
                                    _et = (_m.get("button_title") or _m.get("title") or "").strip()
                            except Exception:
                                pass
                    if (not _et) or re.fullmatch(r"\d+", _et):
                        _et = f"Part {_i}"
                    _et = clean_movie_title_for_button(_et, max_len=40)
                    items.append({"label": f"   {_i}. {_et}", "value": ""})
            
            bundle_text = render_menu_for_user(user_id, "📦 BUNDLE DETAILS", items)
            bundle_text = add_footer(bundle_text)
            bundle_text += apply_premium_emojis(f"\n\n💳 Choose payment method:") + get_datetime_footer()
            
            ref = f"sr_{secrets.token_hex(4)}"
            if payments_collection is not None:
                _pay_doc = {"reference": ref, "user_id": user_id, "series_token": movie_token, "chat_id": query.message.chat.id if query else update.effective_chat.id, "amount": price, "status": "pending", "created_at": datetime.utcnow(), "payment_method": None, "auto_deliver": True, "is_subscription": False}
                asyncio.create_task(asyncio.to_thread(payments_collection.insert_one, _pay_doc))
            auth_url = None
            plans = get_subscription_plans()
            keyboard = build_payment_keyboard(auth_url, ref, price, user_id, plans)
            if get_cart_enabled():
                keyboard = add_cart_button_to_keyboard(keyboard, movie_token)
            _eps = (series.get('episodes') or [])[:21]
            _part_rows = []
            _row = []
            for _i, _ep in enumerate(_eps, 1):
                _pt = clean_movie_title_for_button(_ep.get('button_title') or _ep.get('title') or f"Part {_i}")
                if len(_pt) > 18:
                    _pt = _pt[:15] + "..."
                _etok = _ep.get('token')
                if _etok:
                    _row.append(InlineKeyboardButton(_pt, callback_data=f"get_movie_{_etok}"))
                if len(_row) == 3:
                    _part_rows.append(_row)
                    _row = []
            if _row:
                _part_rows.append(_row)
            if _part_rows:
                _base = list(keyboard.inline_keyboard) if keyboard else []
                keyboard = InlineKeyboardMarkup(_part_rows + _base)
            if query:
                _sent = await query.edit_message_text(bundle_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                _sent = await update.message.reply_text(bundle_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

            async def _attach_smd_bundle_card(_message, _ref=ref, _price=price, _tk=movie_token, _uid=user_id, _plans=plans, _chat=query.message.chat.id if query else update.effective_chat.id):
                try:
                    url, _ = await run_in_thread(initialize_paystack_payment, email=f"user{_uid}@telegram.bot", amount_kes=_price, reference=_ref, metadata={"user_id": _uid, "series_token": _tk, "chat_id": _chat, "auto_deliver": True})
                except Exception:
                    url = None
                if not url:
                    return
                try:
                    new_kb = build_payment_keyboard(url, _ref, _price, _uid, _plans)
                    if get_cart_enabled():
                        new_kb = add_cart_button_to_keyboard(new_kb, _tk)
                    await context.bot.edit_message_reply_markup(chat_id=_chat, message_id=_message.message_id, reply_markup=new_kb)
                except Exception as e:
                    logger.debug(f"smd bundle card attach skipped: {e}")

            if get_card_payment_setting() and _sent:
                asyncio.create_task(_attach_smd_bundle_card(_sent))
            return
    if not movie:
        err_text = add_footer(f"❌ Movie not found.")
        err_kb = build_back_keyboard("back")
        if query:
            await query.edit_message_text(err_text, reply_markup=err_kb)
        return
    has_sub, sub_type, expiry, used, max_movies = await run_in_thread(has_active_subscription, user.id)
    price = movie.get('price', get_default_price())
    existing_payment = None
    if price > 0:
        if await run_in_thread(user_owns_movie, user_id, movie_token):
            existing_payment = True
        elif payments_collection is not None:
            existing_payment = await run_in_thread(user_owns_movie, user_id, movie_token) or await run_in_thread(payments_collection.find_one, {"user_id": user_id, "movie_token": movie_token, "status": "success"})
    if has_sub or existing_payment or price == 0:
        if query:
            await query.answer()
        await forward_movie(update, context, movie, is_subscriber=bool(has_sub))
        return
    plans = get_subscription_plans()
    ref = f"mv_{secrets.token_hex(4)}"
    asyncio.create_task(asyncio.to_thread(track_pending_payment, user_id, ref))
    if payments_collection is not None:
        _pay_doc = {"reference": ref, "user_id": user_id, "movie_token": movie_token, "chat_id": query.message.chat.id if query else update.effective_chat.id, "amount": price, "status": "pending", "created_at": datetime.utcnow(), "payment_method": None, "auto_deliver": True, "is_subscription": False}
        asyncio.create_task(asyncio.to_thread(payments_collection.insert_one, _pay_doc))
    
    items = []
    items.append({"label": "🎬 Title:", "value": clean_movie_title_for_button(movie['title'])})
    desc = movie.get('description', '')
    if desc:
        items.append({"label": "📝 Desc:", "value": desc[:40]})
    items.append({"label": "💳 Price:", "value": f"{price} KES" if price > 0 else "FREE 🎁"})
    items.append({"label": "🏷️ Category:", "value": movie.get('category', 'movie').title()})
    _mv_eps = movie.get('episodes', []) or []
    if _mv_eps:
        items.append({"label": "🎬 Movies inside:", "value": ""})
        for _i, _ep in enumerate(_mv_eps[:25], 1):
            _et = (_ep.get('button_title') or _ep.get('title') or "").strip()
            if (not _et) or re.fullmatch(r"\d+", str(_et)):
                _tok = _ep.get("token")
                if _tok and movies_collection is not None:
                    try:
                        _m = movies_collection.find_one({"token": _tok}, {"title": 1, "button_title": 1})
                        if _m:
                            _et = (_m.get("button_title") or _m.get("title") or "").strip()
                    except Exception:
                        pass
            if (not _et) or re.fullmatch(r"\d+", str(_et)):
                _et = f"Part {_i}"
            _et = clean_movie_title_for_button(_et, max_len=40)
            items.append({"label": f"   {_i}. {_et}", "value": ""})
    
    caption = render_menu_for_user(user_id, "🎦 𝐌𝐨𝐯𝐢𝐞 𝐈𝐧𝐟𝐨𝐫𝐦𝐚𝐭𝐢𝐨𝐧", items)
    caption = add_footer(caption)
    caption += apply_premium_emojis(f"\n\n💳 ᴄʜᴏᴏꜱᴇ ᴘᴀʏᴍᴇɴᴛ ᴍᴇᴛʜᴏᴅ:") + get_datetime_footer()
    
    auth_url = None
    keyboard = build_payment_keyboard(auth_url, ref, price, user_id, plans)
    if get_cart_enabled():
        keyboard = add_cart_button_to_keyboard(keyboard, movie_token)
    reply_markup = keyboard
    poster_url = movie.get('poster_url', '') or ''
    poster_file_id = (movie.get('poster_file_id') or movie.get('poster_tg_file_id') or '').strip()
    sent_message = None
    if query:
        await query.answer()
        # Resolve a poster Telegram can REALLY render — validated file_id first,
        # then live-checked URL, then base64. Never send a dead URL (blank white photo).
        photo_ref = None
        try:
            photo_ref = await resolve_safe_photo(context.bot, poster_file_id, poster_url, context="movie_detail")
        except Exception:
            photo_ref = None
        if photo_ref is not None:
            try:
                await query.message.delete()
                sent_message = await send_html_safe(context.bot, user_id, caption, reply_markup=reply_markup, photo=photo_ref)
            except Exception as e:
                logger.error(f"Failed to send poster in show_movie_detail: {e}")
                sent_message = None
        if sent_message is None and photo_ref is not None:
            # Resolved ref failed at send time (race / revoked) — text card, NO blank image
            try:
                await query.message.delete()
            except Exception:
                pass
            sent_message = await context.bot.send_message(chat_id=user_id, text=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        elif sent_message is None:
            try:
                sent_message = await query.edit_message_text(caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            except Exception:
                sent_message = await query.message.reply_text(caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def _attach_smd_card(_ref=ref, _price=price, _tk=movie_token, _uid=user_id, _plans=plans, _chat=query.message.chat.id if query else update.effective_chat.id, _msg=sent_message):
        try:
            url, _ = await run_in_thread(initialize_paystack_payment, email=f"user{_uid}@telegram.bot", amount_kes=_price, reference=_ref, metadata={"user_id": _uid, "movie_token": _tk, "chat_id": _chat, "auto_deliver": True})
        except Exception:
            url = None
        if not url or _msg is None:
            return
        try:
            new_kb = build_payment_keyboard(url, _ref, _price, _uid, _plans)
            if get_cart_enabled():
                new_kb = add_cart_button_to_keyboard(new_kb, _tk)
            await context.bot.edit_message_reply_markup(chat_id=_chat, message_id=_msg.message_id, reply_markup=new_kb)
        except Exception as e:
            logger.debug(f"smd card attach skipped: {e}")

    if get_card_payment_setting() and sent_message is not None:
        asyncio.create_task(_attach_smd_card())
    push_nav_state(context, f"movie_detail_{movie_token}", caption, reply_markup)

async def forward_movie(update: Update, context, movie, is_subscriber=False):
    user_id = update.effective_user.id
    try:
        # Enforce subscription movie limit before delivering
        if is_subscriber:
            has, plan, expiry, used, max_movies = has_active_subscription(user_id)
            if not has:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=add_footer("⚠️ Your subscription limit has been reached or expired.\n\nPlease renew to continue watching."),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🎈 Subscribe Now", callback_data="show_plans")],
                        [InlineKeyboardButton("◀ Back", callback_data="back")]
                    ]),
                    parse_mode=ParseMode.HTML
                )
                return
            if max_movies > 0 and used >= max_movies:
                await notify_limit_reached(user_id, plan or "subscription")
                return

        file_id = movie.get('file_id')
        await human_action(context, user_id, ChatAction.UPLOAD_VIDEO if movie.get('file_type') == 'video' else ChatAction.UPLOAD_DOCUMENT)
        caption = movie['title']
        if file_id:
            if movie.get('file_type') == 'video':
                await context.bot.send_video(chat_id=user_id, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_document(chat_id=user_id, document=file_id, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await context.bot.forward_message(chat_id=user_id, from_chat_id=movie['channel_id'], message_id=movie['message_id'])
            await context.bot.send_message(chat_id=user_id, text=caption, parse_mode=ParseMode.HTML)
        if movies_collection is not None:
            movies_collection.update_one({"token": movie['token']}, {"$inc": {"downloads": 1}})
        # Count against subscription limit
        if is_subscriber:
            increment_used_movies(user_id, 1)
        record_movie_access(user_id, movie.get("token", ""), movie.get("title", ""))
        msg_text = add_footer(f"🎬 <b>{movie['title']}</b>\n\n✅ Enjoy your movie 🚀")
        keyboard = [
            [InlineKeyboardButton(f"♦️ ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")],
            [InlineKeyboardButton(f"🎦 ʙʀᴏᴡꜱᴇ ᴀʟʟ ᴍᴏᴠɪᴇꜱ", callback_data="watch_all_movies_0")],
            [InlineKeyboardButton(f"🌿 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")],
            [InlineKeyboardButton(f"🔍 ꜱᴇᴀʀᴄʜ ᴍᴏᴠɪᴇꜱ", callback_data="search_movies")],
            [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]
        ]
        await context.bot.send_message(chat_id=user_id, text=msg_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Forward error: {e}")
        await reply_html_safe(update.message, add_footer(f"❌ Could not deliver movie. Contact admin."), reply_markup=build_back_keyboard("back"))

async def forward_series(update: Update, context, series, is_subscriber=False):
    user_id = update.effective_user.id
    await human_action(context, user_id, ChatAction.UPLOAD_VIDEO)
    episodes = series.get('episodes', [])
    if not episodes:
        await reply_html_safe(update.message, add_footer(f"❌ This bundle has no movies yet. Contact admin."), reply_markup=build_back_keyboard("back"))
        return
    total = len(episodes)
    progress_msg = None
    try:
        progress_msg = await context.bot.send_message(chat_id=user_id, text=add_footer(f"📦 <b>Delivering {series['title']}</b>\n\n⏳ Sending {total} movies... (0/{total})\n<i>Please wait...</i>"), parse_mode=ParseMode.HTML)
    except Exception:
        pass
    sent_count = 0
    for i, ep in enumerate(episodes, 1):
        try:
            file_id = ep.get('file_id')
            caption = (ep.get('title') or '').strip()
            if not caption or re.fullmatch(r'[a-zA-Z0-9]{1,3}', caption):
                caption = f"{series.get('title', 'Movie')} — Part {i}"
            if file_id:
                if ep.get('file_type') == 'video':
                    await context.bot.send_video(chat_id=user_id, video=file_id, caption=caption, parse_mode=ParseMode.HTML)
                else:
                    await context.bot.send_document(chat_id=user_id, document=file_id, caption=caption, parse_mode=ParseMode.HTML)
            else:
                await context.bot.forward_message(chat_id=user_id, from_chat_id=ep['channel_id'], message_id=ep['message_id'])
                await context.bot.send_message(chat_id=user_id, text=caption, parse_mode=ParseMode.HTML)
            sent_count += 1
            if progress_msg and i % 3 == 0:
                try:
                    await progress_msg.edit_text(add_footer(f"📦 <b>Delivering {series['title']}</b>\n\n⏳ Sending {total} movies... ({i}/{total})\n<i>Please wait...</i>"), parse_mode=ParseMode.HTML)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Forward bundle movie {i} error: {e}")
            await asyncio.sleep(BOT_FAST_DELAY)
            continue
        if i < total:
            await asyncio.sleep(BOT_FAST_DELAY)
    if series_bundles_collection is not None:
        series_bundles_collection.update_one({"token": series['token']}, {"$inc": {"downloads": 1}})
    # Count each delivered episode against subscription limit
    if is_subscriber and sent_count > 0:
        increment_used_movies(user_id, sent_count)
    if progress_msg:
        try:
            await progress_msg.delete()
        except Exception:
            pass
    if is_subscriber:
        sent_text = add_footer(f"""🎬 <b>{series['title']}</b>

🎉 Delivered with your subscription!
✅ {sent_count}/{total} movies sent.
🔥 Enjoy your movies 🍿""")
    else:
        sent_text = add_footer(f"""🎬 <b>{series['title']}</b>

✅ {sent_count}/{total} movies delivered successfully!
🔥 Enjoy your bundle 🍿""")
    keyboard = [
        [InlineKeyboardButton(f"🔷️ ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")],
        [InlineKeyboardButton(f"🎦 ʙʀᴏᴡꜱᴇ ᴀʟʟ ᴍᴏᴠɪᴇꜱ", callback_data="watch_all_movies_0")],
        [InlineKeyboardButton(f"🌴 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")],
        [InlineKeyboardButton(f"🔍 ꜱᴇᴀʀᴄʜ ᴍᴏᴠɪᴇꜱ", callback_data="search_movies")],
        [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]
    ]
    await update.message.reply_text(sent_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)


# ============ AUTO MIRROR UPLOADS TO TARGET CHANNEL ============
def get_upload_target_channel():
    """Return chat id (int) or @username for UPLOAD_TARGET_CHANNEL, or None if unset."""
    raw = (UPLOAD_TARGET_CHANNEL or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        return raw
    # numeric / -100… channel id
    try:
        return int(raw)
    except Exception:
        # bare username without @
        if raw.replace("_", "").isalnum():
            return f"@{raw}" if not raw.startswith("@") else raw
    return None


def upload_target_include_posters() -> bool:
    return bool(UPLOAD_TARGET_POSTERS)


async def _send_media_no_forward(bot, chat_id, file_id, file_type: str = "video", caption: str = None):
    """Send photo/video/document by file_id — NOT forward — so no 'Forwarded from' tag."""
    caption = (caption or "")[:1024] or None
    ft = (file_type or "video").lower()
    try:
        if ft in ("photo", "image", "poster"):
            return await bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption, parse_mode=ParseMode.HTML if caption else None)
        if ft in ("document", "doc", "file"):
            return await bot.send_document(chat_id=chat_id, document=file_id, caption=caption, parse_mode=ParseMode.HTML if caption else None)
        # default video
        return await bot.send_video(chat_id=chat_id, video=file_id, caption=caption, parse_mode=ParseMode.HTML if caption else None, supports_streaming=True)
    except Exception as e1:
        err = str(e1).lower()
        # Retry without HTML if entity/parse issues
        if caption and any(x in err for x in ("entity", "parse", "emoji", "can't parse")):
            try:
                plain = strip_premium_emojis(caption)
                if ft in ("photo", "image", "poster"):
                    return await bot.send_photo(chat_id=chat_id, photo=file_id, caption=plain)
                if ft in ("document", "doc", "file"):
                    return await bot.send_document(chat_id=chat_id, document=file_id, caption=plain)
                return await bot.send_video(chat_id=chat_id, video=file_id, caption=plain, supports_streaming=True)
            except Exception as e2:
                logger.warning(f"_send_media_no_forward plain retry: {e2}")
        # Last resort: try as document
        try:
            return await bot.send_document(chat_id=chat_id, document=file_id, caption=strip_premium_emojis(caption) if caption else None)
        except Exception as e3:
            logger.error(f"_send_media_no_forward failed file_id={file_id}: {e3}")
            return None


async def auto_mirror_uploads_to_target(
    bot,
    movies: list,
    *,
    shared_poster_file_id: str = None,
    shared_poster_caption: str = None,
    include_posters: bool = None,
    notify_chat_id=None,
    force: bool = False,
) -> dict:
    """Copy uploaded movies into UPLOAD_TARGET_CHANNEL without 'Forwarded from' tags.

    Layout:
      - shared_poster_file_id set (e.g. /confronterking): ONE poster, then each video with its title caption
      - per-movie posters: for each movie → poster (if any) then video/file with title caption
      - include_posters=False: videos/files only

    movies: list of dicts with keys:
      title, file_id, file_type, poster_file_id (optional)
    Set AUTO_MIRROR_ON_UPLOAD=false to disable silent auto-mirror (use /kingdeliver instead).
    """
    if not force and not AUTO_MIRROR_ON_UPLOAD:
        return {"ok": False, "skipped": True, "reason": "AUTO_MIRROR_ON_UPLOAD disabled — use /kingdeliver"}
    target = get_upload_target_channel()
    if not target:
        return {"ok": False, "skipped": True, "reason": "UPLOAD_TARGET_CHANNEL not set"}
    if not movies:
        return {"ok": False, "skipped": True, "reason": "no movies"}

    if include_posters is None:
        include_posters = upload_target_include_posters()

    sent = 0
    errors = 0
    try:
        # Shared poster once (series / confronter king style)
        if include_posters and shared_poster_file_id:
            cap = shared_poster_caption or None
            r = await _send_media_no_forward(bot, target, shared_poster_file_id, "photo", cap)
            if r:
                sent += 1
            else:
                errors += 1
            await asyncio.sleep(BOT_MIRROR_DELAY)

        last_poster_sent = None
        for m in movies:
            title = (m.get("title") or m.get("button_title") or "Movie").strip()
            # Prefer plain title as caption (user-set title)
            caption = escape_html(title) if title else None
            poster_fid = m.get("poster_file_id") or m.get("poster_tg_file_id") or ""
            file_id = m.get("file_id") or ""
            file_type = m.get("file_type") or "video"

            if not file_id:
                errors += 1
                continue

            # Per-movie poster when not using a shared one
            if include_posters and not shared_poster_file_id and poster_fid:
                # Avoid re-sending identical poster back-to-back
                if poster_fid != last_poster_sent:
                    pr = await _send_media_no_forward(bot, target, poster_fid, "photo", caption)
                    if pr:
                        sent += 1
                        last_poster_sent = poster_fid
                    await asyncio.sleep(BOT_FAST_DELAY)

            vr = await _send_media_no_forward(bot, target, file_id, file_type, caption)
            if vr:
                sent += 1
            else:
                errors += 1
            await asyncio.sleep(BOT_MIRROR_DELAY)

        result = {"ok": errors == 0, "sent": sent, "errors": errors, "target": str(target)}
        # Silent on purpose — admin keeps normal upload UX; only logs
        logger.info(f"auto_mirror_uploads_to_target: {result}")
        return result
    except Exception as e:
        logger.error(f"auto_mirror_uploads_to_target: {e}")
        logger.error(traceback.format_exc())
        return {"ok": False, "sent": sent, "errors": errors + 1, "error": str(e)}



# ============ KING DELIVER (hidden admin archive tools) ============
# /confronterisdead — menu of hidden commands
# /kingmovies — total movies in DB (same pool as dashboard)
# /kingdeliver 1-10 | 4-90 | all | pause | resume | stop
# Copies to KING_DELIVER_CHANNEL without "Forwarded from" tag; flood-safe.

_king_deliver_jobs = {}  # admin_id -> job dict


def get_king_deliver_channel():
    raw = (KING_DELIVER_CHANNEL or UPLOAD_TARGET_CHANNEL or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        return raw
    try:
        return int(raw)
    except Exception:
        if raw.replace("_", "").isalnum():
            return f"@{raw}"
    return None


def _king_movies_cursor(skip: int = 0, limit: int = None):
    """Stable ordering for index numbers: oldest first (created_at ASC), then _id."""
    if not mongodb_connected or movies_collection is None:
        return []
    cur = movies_collection.find().sort([("created_at", ASCENDING), ("_id", ASCENDING)])
    if skip:
        cur = cur.skip(int(skip))
    if limit is not None:
        cur = cur.limit(int(limit))
    return list(cur)


def _king_movies_count() -> int:
    if not mongodb_connected or movies_collection is None:
        return 0
    try:
        return int(movies_collection.count_documents({}))
    except Exception:
        return 0


async def _king_sleep_flood(seconds: float, bot=None, admin_id=None, note: str = ""):
    """Sleep through Telegram flood / rate limits; optional admin ping."""
    seconds = max(1.0, float(seconds or 1))
    logger.warning(f"kingdeliver flood wait {seconds:.1f}s {note}")
    if bot and admin_id:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=f"⏳ Flood wait {int(seconds)}s — auto-continuing… {note}",
            )
        except Exception:
            pass
    await asyncio.sleep(seconds + 0.5)


def _king_parse_private_link(link: str):
    """Extract (channel_id, message_id) from any t.me / telegram.me private or public link."""
    if not link:
        return None, None
    try:
        s = str(link).strip()
        # https://t.me/c/1234567890/100  or telegram.me/c/...
        m = re.search(r"(?:t\.me|telegram\.me)/c/(\d+)/(\d+)", s, re.I)
        if m:
            return int(f"-100{m.group(1)}"), int(m.group(2))
        # https://t.me/username/123
        m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z][\w\d_]{3,})/(\d+)", s, re.I)
        if m and m.group(1).lower() not in ("c", "joinchat", "addstickers", "share"):
            return f"@{m.group(1).lstrip('@')}", int(m.group(2))
        # bare path /c/123/456
        m = re.search(r"/c/(\d+)/(\d+)", s)
        if m:
            return int(f"-100{m.group(1)}"), int(m.group(2))
    except Exception:
        pass
    return None, None


def _king_normalize_channel_id(ch):
    """Normalize stored channel_id to a form Telegram accepts for copy/forward.
    Handles: -100…, positive internal id, string numbers, @username.
    Returns list of candidates to try (most likely first).
    """
    if ch is None or ch == "" or str(ch).strip() in ("0", "None", "none"):
        return []
    candidates = []
    s = str(ch).strip()
    # @username
    if s.startswith("@") or (s and not s.lstrip("-").isdigit()):
        uname = s if s.startswith("@") else f"@{s}"
        candidates.append(uname)
        return candidates
    try:
        n = int(s)
    except Exception:
        return []
    # Already full -100… form
    if n < 0:
        candidates.append(n)
        # also try without -100 if someone stored wrong
        abs_s = str(abs(n))
        if abs_s.startswith("100") and len(abs_s) > 3:
            candidates.append(int(abs_s[3:]))  # rare legacy
        return candidates
    # Positive: try as-is AND as -100 prefix (private supergroup/channel)
    candidates.append(int(f"-100{n}"))
    candidates.append(n)
    return candidates


def _king_extract_source(movie: dict):
    """Pull every possible deliverable reference from a movie/web-upload doc."""
    file_id = (movie.get("file_id") or movie.get("video_file_id") or "").strip()
    file_type = (movie.get("file_type") or movie.get("video_file_type") or "video").lower()
    mid = movie.get("message_id") or movie.get("msg_id") or movie.get("messageId")
    try:
        mid = int(mid) if mid not in (None, "", 0, "0") else None
    except Exception:
        mid = None
    ch_raw = movie.get("channel_id") or movie.get("from_chat_id") or movie.get("chat_id")
    links = []
    for key in ("private_link", "telegram_link", "source_link", "link", "url"):
        v = movie.get(key)
        if v and str(v).strip():
            links.append(str(v).strip())
    # Parse links into ch/mid
    for link in links:
        pl_ch, pl_mid = _king_parse_private_link(link)
        if pl_ch is not None and ch_raw in (None, "", 0, "0"):
            ch_raw = pl_ch
        if pl_mid is not None and mid is None:
            mid = pl_mid
    ch_candidates = _king_normalize_channel_id(ch_raw)
    # If link gave @username or -100 id directly
    if not ch_candidates and ch_raw is not None:
        ch_candidates = _king_normalize_channel_id(ch_raw)
    return file_id, file_type, ch_candidates, mid, links


async def _king_send_one_movie(bot, target, movie: dict, index: int, total: int) -> str:
    """Deliver one movie. NEVER skips if any file_id / private link / channel+msg exists.
    Works for: Telegram uploads, old text tokens, dashboard t.me/c/ pastes.
    """
    title = (movie.get("title") or movie.get("button_title") or f"Movie {index}").strip()
    caption = escape_html(title)[:1024]
    file_id, file_type, ch_candidates, mid, links = _king_extract_source(movie)

    def _is_flood(err) -> bool:
        e = str(err).lower()
        return any(x in e for x in ("retry", "flood", "too many", "retryafter"))

    # 1) file_id — instant, no forward tag
    if file_id:
        for ft in (file_type, "video", "document", "photo"):
            try:
                await _send_media_no_forward(bot, target, file_id, ft, caption)
                return "ok"
            except Exception as e:
                if _is_flood(e):
                    raise
                continue
        try:
            await _send_media_no_forward(bot, target, file_id, "document", None)
            return "ok"
        except Exception as e:
            if _is_flood(e):
                raise

    # 2) copy_message from every channel id candidate (web paste + old uploads)
    if mid and ch_candidates:
        for from_chat in ch_candidates:
            for with_cap in (True, False):
                try:
                    kw = dict(chat_id=target, from_chat_id=from_chat, message_id=int(mid))
                    if with_cap and caption:
                        kw["caption"] = caption
                        kw["parse_mode"] = ParseMode.HTML
                    await bot.copy_message(**kw)
                    return "ok"
                except Exception as e:
                    if _is_flood(e):
                        raise
                    continue
            # forward fallback for this channel id
            try:
                await bot.forward_message(chat_id=target, from_chat_id=from_chat, message_id=int(mid))
                return "ok"
            except Exception as e:
                if _is_flood(e):
                    raise
                continue

    # 3) Re-parse links only (in case ch/mid were wrong in DB fields)
    for link in links:
        pl_ch, pl_mid = _king_parse_private_link(link)
        if not pl_ch or not pl_mid:
            continue
        for from_chat in _king_normalize_channel_id(pl_ch):
            try:
                await bot.copy_message(chat_id=target, from_chat_id=from_chat, message_id=int(pl_mid))
                return "ok"
            except Exception as e:
                if _is_flood(e):
                    raise
            try:
                await bot.forward_message(chat_id=target, from_chat_id=from_chat, message_id=int(pl_mid))
                return "ok"
            except Exception as e:
                if _is_flood(e):
                    raise

    # 4) Fresh DB read (token may be text or numeric)
    try:
        tok = movie.get("token")
        if tok is not None and movies_collection is not None:
            fresh = movies_collection.find_one({"token": tok})
            if not fresh and str(tok).isdigit():
                fresh = movies_collection.find_one({"token": int(tok)})
            if fresh:
                fid2, ft2, chs2, mid2, links2 = _king_extract_source(fresh)
                if fid2 and fid2 != file_id:
                    try:
                        await _send_media_no_forward(bot, target, fid2, ft2 or "video", caption)
                        return "ok"
                    except Exception as e:
                        if _is_flood(e):
                            raise
                if mid2 and chs2:
                    for from_chat in chs2:
                        try:
                            await bot.copy_message(chat_id=target, from_chat_id=from_chat, message_id=int(mid2))
                            return "ok"
                        except Exception as e:
                            if _is_flood(e):
                                raise
                        try:
                            await bot.forward_message(chat_id=target, from_chat_id=from_chat, message_id=int(mid2))
                            return "ok"
                        except Exception as e:
                            if _is_flood(e):
                                raise
    except Exception as e:
        if _is_flood(e):
            raise
        logger.debug(f"kingdeliver refresh #{index}: {e}")

    logger.warning(
        f"kingdeliver FAIL #{index} '{title}' token={movie.get('token')} "
        f"file_id={bool(file_id)} ch={ch_candidates} mid={mid} links={links[:1]}"
    )
    return "fail"


async def _king_deliver_worker(bot, admin_id: int):
    """Background loop: deliver range with pause/resume/stop + flood waits."""
    from telegram.error import RetryAfter, TimedOut, NetworkError

    job = _king_deliver_jobs.get(admin_id)
    if not job:
        return
    target = job["target"]
    start_i = int(job["start"])  # 1-based inclusive
    end_i = int(job["end"])      # 1-based inclusive
    job["status"] = "running"
    job["paused"] = False

    try:
        await bot.send_message(
            chat_id=admin_id,
            text=(
                f"🚀 <b>King Deliver started</b>\n"
                f"📦 Range: <b>{start_i}–{end_i}</b> (of {job.get('total', '?')})\n"
                f"🎯 Channel: <code>{escape_html(str(target))}</code>\n"
                f"▶️ /kingdeliver pause · resume · stop"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    idx = int(job.get("current") or start_i)  # 1-based
    ok = skip = fail = 0

    while idx <= end_i:
        job = _king_deliver_jobs.get(admin_id) or job
        if job.get("stop"):
            job["status"] = "stopped"
            break
        while job.get("paused") and not job.get("stop"):
            job["status"] = "paused"
            await asyncio.sleep(1.0)
            job = _king_deliver_jobs.get(admin_id) or job
        if job.get("stop"):
            job["status"] = "stopped"
            break
        job["status"] = "running"
        job["current"] = idx

        # Fetch single movie by 0-based offset
        batch = _king_movies_cursor(skip=idx - 1, limit=1)
        if not batch:
            fail += 1
            idx += 1
            continue
        movie = batch[0]
        title = movie.get("title") or f"#{idx}"

        result = None
        attempts = 0
        while attempts < 3:
            attempts += 1
            try:
                result = await _king_send_one_movie(bot, target, movie, idx, end_i)
                if result == "ok":
                    break
                # fail → brief pause then retry same movie
                await asyncio.sleep(0.4 * attempts)
            except RetryAfter as ra:
                wait = float(getattr(ra, "retry_after", 5) or 5)
                await _king_sleep_flood(wait, bot, admin_id, f"(at #{idx})")
                continue  # same attempt slot, same idx
            except TimedOut:
                await _king_sleep_flood(3, bot, admin_id, f"timeout #{idx}")
                continue
            except NetworkError:
                await _king_sleep_flood(5, bot, admin_id, f"network #{idx}")
                continue
            except Exception as e:
                err = str(e).lower()
                if "retry after" in err or "flood" in err or "too many requests" in err:
                    import re as _re
                    m = _re.search(r"(\d+)", str(e))
                    wait = float(m.group(1)) if m else 8.0
                    await _king_sleep_flood(wait, bot, admin_id, f"(at #{idx})")
                    continue
                logger.error(f"kingdeliver #{idx} {title}: {e}")
                result = "fail"
                break

        if result == "ok":
            ok += 1
        else:
            fail += 1  # no more "skip" — either sent or failed after retries

        job["ok"] = ok
        job["skip"] = 0  # legacy field kept at 0 — we no longer skip
        job["fail"] = fail

        # Silent progress: only every 25 items (like store mirror — less spam)
        if idx % 25 == 0 or idx == end_i:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"📊 King progress: <b>{idx}/{end_i}</b> · ✅{ok} ❌{fail}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

        idx += 1
        await asyncio.sleep(BOT_KING_DELAY)

    job["current"] = min(idx, end_i)
    if job.get("status") != "stopped":
        job["status"] = "done"
    try:
        if job.get("status") == "stopped":
            msg = (
                f"⏹ <b>King Deliver stopped</b>\n"
                f"Stopped at #{job['current']} (range {start_i}–{end_i})\n"
                f"✅ Sent: <b>{ok}</b> · ❌ Fail: <b>{fail}</b>\n"
                f"Continue: <code>/kingdeliver {job['current']}-{end_i}</code>"
            )
        else:
            msg = (
                f"✅ <b>King Deliver finished</b>\n"
                f"Range {start_i}–{end_i}\n"
                f"✅ Sent: <b>{ok}</b> · ❌ Fail: <b>{fail}</b>\n"
                f"<i>No movies skipped — every item was attempted.</i>"
            )
        await bot.send_message(chat_id=admin_id, text=msg, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    logger.info(f"kingdeliver done admin={admin_id} ok={ok} fail={fail} status={job.get('status')}")


async def confronterisdead_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hidden admin menu for archive / king tools."""
    user = update.effective_user
    if not is_bot_admin(user.id):
        return  # silent for non-admins
    total = _king_movies_count()
    target = get_king_deliver_channel()
    job = _king_deliver_jobs.get(user.id) or {}
    job_line = ""
    if job:
        job_line = (
            f"\n\n📡 <b>Active job:</b> {job.get('status','?')} "
            f"#{job.get('current','?')}/{job.get('end','?')} "
            f"(✅{job.get('ok',0)} ❌{job.get('fail',0)})"
        )
    text = (
        f"🕶 <b>Confronter is Dead — Hidden Admin</b>\n\n"
        f"🗄 Movies in DB: <b>{total}</b>\n"
        f"🎯 Deliver channel: <code>{escape_html(str(target) if target else 'NOT SET')}</code>\n"
        f"   <i>Set KING_DELIVER_CHANNEL in .env</i>\n"
        f"{job_line}\n\n"
        f"<b>Commands</b>\n"
        f"• <code>/kingmovies</code> — count + sample titles\n"
        f"• <code>/kingdeliver 1-10</code> — deliver #1…#10\n"
        f"• <code>/kingdeliver 4-90</code> — deliver #4…#90\n"
        f"• <code>/kingdeliver all</code> — deliver entire library\n"
        f"• <code>/kingdeliver pause</code> — pause current run\n"
        f"• <code>/kingdeliver resume</code> — continue after pause\n"
        f"• <code>/kingdeliver stop</code> — abort (resume with new range)\n\n"
        f"<b>Notes</b>\n"
        f"• No payment · admin only · silent (like store mirror)\n"
        f"• <b>No skips</b> — every movie is attempted (file_id → copy → forward)\n"
        f"• Flood waits auto-retry · 3 attempts per movie\n"
        f"• Index order: oldest → newest (stable #)\n"
        f"• Auto-upload mirror: <code>AUTO_MIRROR_ON_UPLOAD</code>="
        f"{'on' if AUTO_MIRROR_ON_UPLOAD else 'off'}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def kingmovies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_bot_admin(user.id):
        return
    total = _king_movies_count()
    sample = _king_movies_cursor(skip=0, limit=5)
    lines = [f"🎬 <b>King Movies</b>\n\n🗄 Total in database: <b>{total}</b>\n"]
    if sample:
        lines.append("<b>First 5 (#1 = oldest):</b>")
        for i, m in enumerate(sample, 1):
            t = escape_html((m.get("title") or "?")[:60])
            lines.append(f"{i}. {t}")
    if total > 5:
        last = _king_movies_cursor(skip=max(0, total - 3), limit=3)
        lines.append(f"\n<b>Last 3 (#{total - len(last) + 1}…#{total}):</b>")
        base = total - len(last) + 1
        for i, m in enumerate(last):
            t = escape_html((m.get("title") or "?")[:60])
            lines.append(f"{base + i}. {t}")
    target = get_king_deliver_channel()
    lines.append(f"\n🎯 <code>{escape_html(str(target) if target else 'KING_DELIVER_CHANNEL not set')}</code>")
    lines.append("Deliver: <code>/kingdeliver 1-10</code> · <code>/kingdeliver all</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def kingdeliver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_bot_admin(user.id):
        return
    args = (context.args or [])
    arg = " ".join(args).strip().lower() if args else ""

    # pause / resume / stop
    if arg in ("pause", "p"):
        job = _king_deliver_jobs.get(user.id)
        if not job or job.get("status") not in ("running", "paused"):
            await update.message.reply_text("ℹ️ No active kingdeliver job.")
            return
        job["paused"] = True
        job["status"] = "paused"
        await update.message.reply_text(f"⏸ Paused at #{job.get('current')}. Resume: /kingdeliver resume")
        return
    if arg in ("resume", "continue", "r"):
        job = _king_deliver_jobs.get(user.id)
        if not job:
            await update.message.reply_text("ℹ️ Nothing to resume. Start with /kingdeliver 1-10")
            return
        if job.get("status") == "done":
            await update.message.reply_text("ℹ️ Last job already finished. Start a new range.")
            return
        if job.get("status") == "running" and not job.get("paused"):
            await update.message.reply_text("ℹ️ Already running.")
            return
        job["stop"] = False
        job["paused"] = False
        # If worker exited (stopped), start a new worker from current index
        if job.get("status") == "stopped":
            job["start"] = int(job.get("current") or job.get("start") or 1)
            job["status"] = "running"
            asyncio.create_task(_king_deliver_worker(context.bot, user.id))
        else:
            job["status"] = "running"
        await update.message.reply_text(f"▶️ Resuming from #{job.get('current')}…")
        return
    if arg in ("stop", "cancel", "x"):
        job = _king_deliver_jobs.get(user.id)
        if not job:
            await update.message.reply_text("ℹ️ No active job.")
            return
        job["stop"] = True
        job["paused"] = False
        await update.message.reply_text(f"⏹ Stopping after current item (at #{job.get('current')})…")
        return

    target = get_king_deliver_channel()
    if not target:
        await update.message.reply_text(
            "❌ Set <code>KING_DELIVER_CHANNEL</code> in .env (channel id or @username).\n"
            "Bot must be admin there.",
            parse_mode=ParseMode.HTML,
        )
        return

    total = _king_movies_count()
    if total <= 0:
        await update.message.reply_text("🗄 Database has 0 movies.")
        return

    # parse range
    start_i, end_i = 1, total
    if not arg or arg in ("all", "*"):
        start_i, end_i = 1, total
    else:
        import re as _re
        m = _re.match(r"^(\d+)\s*[-:]\s*(\d+)$", arg)
        if m:
            start_i, end_i = int(m.group(1)), int(m.group(2))
        elif arg.isdigit():
            start_i = end_i = int(arg)
        else:
            await update.message.reply_text(
                "Usage:\n"
                "<code>/kingdeliver 1-10</code>\n"
                "<code>/kingdeliver 4-90</code>\n"
                "<code>/kingdeliver all</code>\n"
                "<code>/kingdeliver pause|resume|stop</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    if start_i < 1:
        start_i = 1
    if end_i > total:
        end_i = total
    if start_i > end_i:
        start_i, end_i = end_i, start_i

    existing = _king_deliver_jobs.get(user.id)
    if existing and existing.get("status") == "running" and not existing.get("paused"):
        await update.message.reply_text(
            f"⚠️ Job already running at #{existing.get('current')}.\n"
            f"Use /kingdeliver pause or stop first."
        )
        return

    _king_deliver_jobs[user.id] = {
        "target": target,
        "start": start_i,
        "end": end_i,
        "current": start_i,
        "total": total,
        "status": "starting",
        "paused": False,
        "stop": False,
        "ok": 0,
        "skip": 0,
        "fail": 0,
    }
    asyncio.create_task(_king_deliver_worker(context.bot, user.id))


# ============ UPLOAD FLOW ============
async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text(f"❌ You are not authorized.")
        return
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 0 KES (FREE)", callback_data="upload_price_0")],
        [InlineKeyboardButton("💰 10 KES", callback_data="upload_price_10")],
        [InlineKeyboardButton("💰 20 KES", callback_data="upload_price_20")],
        [InlineKeyboardButton("💰 Send Your Price", callback_data="upload_price_custom")],
        [InlineKeyboardButton("💰 Default Price", callback_data="upload_price_default")]
    ])
    
    await update.message.reply_text(f"📷 <b>Movie Upload</b>\n\n<b>Select the price for all movies:</b>\n• 0 KES (FREE)\n• 10 KES\n• 20 KES\n• Send Your Price — Enter any amount\n• Default Price — Use bot default\n\nAfter selecting, send posters and movies using the normal upload flow.", reply_markup=keyboard, parse_mode=ParseMode.HTML)
    
    context.user_data["upload_price_selected"] = False

async def handle_upload_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return
    
    data = query.data
    
    if data == "upload_price_0":
        context.user_data["upload_price"] = 0
        context.user_data["upload_price_selected"] = True
        await query.edit_message_text(f"✅ Price set to <b>0 KES (FREE)</b>!\n\nNow send a <b>poster photo</b> with the movie title as the caption.\nThen send the <b>movie file</b> (video or document).\nRepeat for as many movies as you want.\n\nWhen done, type <code>/complete</code> to finalize.", parse_mode=ParseMode.HTML)
        _clear_upload_state(admin.id, context)
        context.user_data["upload_mode"] = True
        context.user_data["upload_step"] = "poster"
        context.user_data["upload_data"] = {"movies": [], "price": 0, "category": "movie"}
        return
    
    elif data == "upload_price_10":
        context.user_data["upload_price"] = 10
        context.user_data["upload_price_selected"] = True
        await query.edit_message_text(f"✅ Price set to <b>10 KES</b>!\n\nNow send a <b>poster photo</b> with the movie title as the caption.\nThen send the <b>movie file</b> (video or document).\nRepeat for as many movies as you want.\n\nWhen done, type <code>/complete</code> to finalize.", parse_mode=ParseMode.HTML)
        _clear_upload_state(admin.id, context)
        context.user_data["upload_mode"] = True
        context.user_data["upload_step"] = "poster"
        context.user_data["upload_data"] = {"movies": [], "price": 10, "category": "movie"}
        return
    
    elif data == "upload_price_20":
        context.user_data["upload_price"] = 20
        context.user_data["upload_price_selected"] = True
        await query.edit_message_text(f"✅ Price set to <b>20 KES</b>!\n\nNow send a <b>poster photo</b> with the movie title as the caption.\nThen send the <b>movie file</b> (video or document).\nRepeat for as many movies as you want.\n\nWhen done, type <code>/complete</code> to finalize.", parse_mode=ParseMode.HTML)
        _clear_upload_state(admin.id, context)
        context.user_data["upload_mode"] = True
        context.user_data["upload_step"] = "poster"
        context.user_data["upload_data"] = {"movies": [], "price": 20, "category": "movie"}
        return
    
    elif data == "upload_price_custom":
        context.user_data["awaiting_upload_price"] = True
        await query.edit_message_text(f"✍️ <b>Send Your Price</b>\n\nType the price in KES (e.g., <code>15</code> or <code>25</code>).\n\nSend a number only.", parse_mode=ParseMode.HTML)
        return
    
    elif data == "upload_price_default":
        price = get_default_price()
        context.user_data["upload_price"] = price
        context.user_data["upload_price_selected"] = True
        await query.edit_message_text(f"✅ Price set to default: <b>{price} KES</b>!\n\nNow send a <b>poster photo</b> with the movie title as the caption.\nThen send the <b>movie file</b> (video or document).\nRepeat for as many movies as you want.\n\nWhen done, type <code>/complete</code> to finalize.", parse_mode=ParseMode.HTML)
        _clear_upload_state(admin.id, context)
        context.user_data["upload_mode"] = True
        context.user_data["upload_step"] = "poster"
        context.user_data["upload_data"] = {"movies": [], "price": price, "category": "movie"}
        return

async def handle_upload_price_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    
    if not context.user_data.get("awaiting_upload_price"):
        return
    
    text = update.message.text.strip()
    try:
        price = int(text)
        if price < 0:
            await update.message.reply_text("❌ Price cannot be negative. Please enter 0 or a positive number.")
            return
        
        context.user_data["upload_price"] = price
        context.user_data["upload_price_selected"] = True
        context.user_data.pop("awaiting_upload_price", None)
        
        await update.message.reply_text(f"✅ Price set to <b>{price} KES</b>!\n\nNow send a <b>poster photo</b> with the movie title as the caption.\nThen send the <b>movie file</b> (video or document).\nRepeat for as many movies as you want.\n\nWhen done, type <code>/complete</code> to finalize.", parse_mode=ParseMode.HTML)
        _clear_upload_state(admin.id, context)
        context.user_data["upload_mode"] = True
        context.user_data["upload_step"] = "poster"
        context.user_data["upload_data"] = {"movies": [], "price": price, "category": "movie"}
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Please send a number like <code>15</code> or <code>25</code>.", parse_mode=ParseMode.HTML)

async def cancel_upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    had_upload = context.user_data.get("upload_mode")
    _clear_upload_state(update.effective_user.id, context)
    if had_upload:
        await reply_html_safe(update.message, add_footer(f"❌ Upload cancelled."))
    else:
        await reply_html_safe(update.message, add_footer(f"ℹ️ No active upload to cancel."))

def _clear_upload_state(admin_id, context):
    keys_to_remove = ["upload_mode", "upload_step", "upload_data", "upload_type", "upload_movies", "upload_poster", "upload_title", "upload_description", "upload_price", "upload_category", "upload_telegram_link", "upload_series_title", "upload_episode_count", "upload_episode_label", "admin_upload_state", "upload_price_selected", "awaiting_upload_price"]
    for key in keys_to_remove:
        context.user_data.pop(key, None)

def _other_session_active(context, *except_flags):
    flags = ("double_confronter_active", "confronter_upload_mode", "upload_mode", "bulk_upload", "bundle_session")
    for f in flags:
        if f in except_flags:
            continue
        if context.user_data.get(f):
            return True
    try:
        if admin_confronter_king_sessions:
            return True
    except Exception:
        pass
    return False

async def handle_upload_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS or not context.user_data.get("upload_mode"):
        return
    if _other_session_active(context, "upload_mode"):
        return
    step = context.user_data.get("upload_step")
    upload_data = context.user_data["upload_data"]
    if step == "poster" and update.message.photo:
        photo = update.message.photo[-1]
        caption = update.message.caption or "Untitled"
        upload_data["temp_poster"] = {"file_id": photo.file_id, "caption": caption}
        context.user_data["upload_step"] = "movie"
        await update.message.reply_text(add_footer(f"""✅ Poster received! Title: <b>{escape_html(caption)}</b>

Now send the <b>movie file</b> (video or document) for this movie.
You can also forward the movie from a channel (the bot will detect it).
Send /skip to skip this movie? (not recommended)"""), parse_mode=ParseMode.HTML)
        return
    elif step == "movie":
        message = update.message
        file_id = None
        file_type = None
        if message.video:
            file_id = message.video.file_id
            file_type = "video"
        elif message.document:
            file_id = message.document.file_id
            file_type = "document"
        elif message.forward_from_chat:
            upload_data["temp_movie"] = {"channel_id": message.forward_from_chat.id, "message_id": message.forward_from_message_id, "file_id": None, "file_type": None}
        else:
            await update.message.reply_text(add_footer(f"""⚠️ Please send a <b>video</b> or <b>document</b> file, or forward a message from a channel.
Send /skip to skip this movie."""), parse_mode=ParseMode.HTML)
            return

        if file_id:
            upload_data["temp_movie"] = {"file_id": file_id, "file_type": file_type, "channel_id": None, "message_id": None}

        poster = upload_data.pop("temp_poster", None)
        movie = upload_data.pop("temp_movie", None)
        if poster and movie:
            upload_data["movies"].append({"poster_file_id": poster["file_id"], "title": poster["caption"], "file_id": movie.get("file_id"), "file_type": movie.get("file_type"), "channel_id": movie.get("channel_id"), "message_id": movie.get("message_id")})
            await update.message.reply_text(add_footer(f"""✅ Movie <b>{escape_html(poster['caption'])}</b> saved! ({len(upload_data['movies'])} total)

Send another poster + movie, or type <code>/complete</code> to finish."""), parse_mode=ParseMode.HTML)
            context.user_data["upload_step"] = "poster"
        else:
            await reply_html_safe(update.message, add_footer(f"❌ Missing poster or movie. Please start with a poster first.\nSend /cancelupload to cancel."))
        return

async def handle_upload_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    
    if context.user_data.get("awaiting_upload_price"):
        await handle_upload_price_text(update, context)
        return
    
    if not context.user_data.get("upload_mode"):
        return
    if context.user_data.get("bulk_upload"):
        return
    text = update.message.text.strip()
    if text == "/complete":
        await complete_upload(update, context)
        return
    if text == "/skip":
        await reply_html_safe(update.message, add_footer(f"ℹ️ Skipped. Please follow the prompts."))
        return
    if text == "/cancelupload":
        await cancel_upload_cmd(update, context)
        return

async def complete_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    upload_data = context.user_data.get("upload_data", {})
    movies = upload_data.get("movies", [])
    if not movies:
        await reply_html_safe(update.message, add_footer(f"❌ No movies to save. Upload cancelled."))
        _clear_upload_state(admin.id, context)
        return

    await reply_html_safe(update.message, add_footer(f"🚀 Finalizing {len(movies)} movies..."))

    # Fast finalize: keep poster_file_id; only host URL if missing (skip slow download waterfall)
    poster_urls = {}
    for m in movies:
        file_id = m.get("poster_file_id") or ""
        if not file_id:
            poster_urls[file_id] = ""
            continue
        # Prefer already-known URL on the movie entry
        existing = (m.get("poster_url") or "").strip()
        if existing.startswith("http") or existing.startswith("/uploads/") or existing.startswith("data:"):
            poster_urls[file_id] = existing
            continue
        try:
            tg_file = await context.bot.get_file(file_id)
            file_bytes = io.BytesIO()
            await tg_file.download_to_memory(file_bytes)
            file_bytes.seek(0)
            url = upload_image_with_fallback(file_bytes.read(), context="complete_upload")
            poster_urls[file_id] = url
        except Exception as e:
            logger.error(f"Poster upload error for {m.get('title')}: {e}")
            poster_urls[file_id] = None

    existing_tokens = [doc['token'] for doc in movies_collection.find({}, {'token': 1})]
    if series_bundles_collection is not None:
        existing_tokens.extend([doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})])

    saved = []
    for idx, m in enumerate(movies, 1):
        title = m["title"].strip() or f"Movie {idx}"
        token = generate_count_token()
        existing_tokens.append(token)
        poster_url = poster_urls.get(m["poster_file_id"], "")
        if poster_url is None:
            poster_url = ""
        file_id = m.get("file_id")
        file_type = m.get("file_type")
        channel_id = m.get("channel_id")
        message_id = m.get("message_id")
        movie_doc = {"token": token, "title": title, "description": "", "price": upload_data.get("price", get_default_price()), "poster_url": poster_url or "", "poster_file_id": m.get("poster_file_id") or "", "file_id": file_id, "file_type": file_type, "channel_id": channel_id, "message_id": message_id, "private_link": f"https://t.me/c/{abs(channel_id) if channel_id else ''}/{message_id if message_id else ''}" if channel_id and message_id else "", "category": upload_data.get("category", "movie"), "downloads": 0, "created_at": datetime.utcnow()}
        try:
            movies_collection.insert_one(movie_doc)
            saved.append(movie_doc)
        except Exception as e:
            logger.error(f"Failed to save movie {title}: {e}")

    _clear_upload_state(admin.id, context)

    # Auto-mirror videos (+ optional posters) to UPLOAD_TARGET_CHANNEL — no forward tag
    if saved and get_upload_target_channel():
        try:
            await auto_mirror_uploads_to_target(
                context.bot,
                saved,
                include_posters=upload_target_include_posters(),
            )
        except Exception as _mirror_err:
            logger.error(f"complete_upload mirror: {_mirror_err}")

    if saved:
        items = []
        items.append({"label": "✅ UPLOAD COMPLETE!", "value": ""})
        items.append({"label": "🎬 Movies saved:", "value": str(len(saved))})
        items.append({"label": "💰 Price:", "value": f"{upload_data.get('price', 10)} KES"})
        
        summary = render_menu_for_user(admin.id, "✅ UPLOAD COMPLETE!", items)
        summary = add_footer(summary)

        keyboard = []
        token_list = []
        for i, m in enumerate(saved, 1):
            link = f"https://t.me/{context.bot.username}?start={m['token']}"
            token_list.append(f"{i}. {m['title']}\n   {link}\n   Token: {m['token']}")
            keyboard.append([InlineKeyboardButton(f"{i}. {m['title'][:20]}", url=link)])
        keyboard.append([InlineKeyboardButton(f"📡 More Movies", callback_data="more_movies")])
        keyboard.append([InlineKeyboardButton(f"🏠 Home", callback_data="go_home")])

        await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        await update.message.reply_text("\n".join(token_list))
    else:
        await reply_html_safe(update.message, add_footer(f"❌ No movies were saved. Please try again."))

# ============ BULK UPLOAD ============
async def bulk_upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text(f"❌ You are not authorized.")
        return

    _clear_bulk_upload_state(admin.id, context)
    context.user_data["bulk_upload"] = True
    context.user_data["bulk_step"] = "titles"
    context.user_data["bulk_data"] = {"tokens": [], "video_index": 0, "poster_url": "", "price": get_default_price(), "category": "movie"}

    await update.message.reply_text(add_footer(f"""📦 <b>Bulk Upload</b>

Send me a list of titles, one per line.
Example:
1. Warrior
2. Temple
3. Sabirrr
Or for series with range:
Flash 1-20
Breaking Bad 1-3

After that, send an optional <b>poster photo</b> (or /skipposter) and then forward/send the videos in the same order.
Send /cancelbulk to cancel."""), parse_mode=ParseMode.HTML)

async def cancel_bulk_upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    had_bulk = context.user_data.get("bulk_upload")
    _clear_bulk_upload_state(update.effective_user.id, context)
    if had_bulk:
        await reply_html_safe(update.message, add_footer(f"❌ Bulk upload cancelled."))
    else:
        await reply_html_safe(update.message, add_footer(f"ℹ️ No active bulk upload."))

def _clear_bulk_upload_state(admin_id, context):
    for key in ["bulk_upload", "bulk_step", "bulk_data"]:
        context.user_data.pop(key, None)

async def handle_bulk_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS or not context.user_data.get("bulk_upload"):
        return

    step = context.user_data.get("bulk_step")
    if step == "titles":
        text = update.message.text.strip()
        if not text:
            return
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        tokens = []
        for line in lines:
            title = re.sub(r'^\d+[\.\)]\s*', '', line)
            match = re.match(r'(.+?)\s*(\d+)\s*[-–]\s*(\d+)$', title)
            if match:
                base, start, end = match.group(1).strip(), int(match.group(2)), int(match.group(3))
                for i in range(start, end + 1):
                    tokens.append(f"{base} {i}")
            else:
                tokens.append(title)
        if not tokens:
            await reply_html_safe(update.message, add_footer(f"No valid titles found. Try again or /cancelbulk."))
            return
        context.user_data["bulk_data"]["tokens"] = tokens
        context.user_data["bulk_step"] = "poster"
        await update.message.reply_text(add_footer(f"""✅ Parsed {len(tokens)} titles.

Now send an optional <b>poster photo</b> that will be used for all these movies.
If you don't want a poster, type <code>/skipposter</code>.
After that, forward the videos in the same order as the titles."""), parse_mode=ParseMode.HTML)
    elif step == "poster":
        pass

async def skip_poster_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS or not context.user_data.get("bulk_upload"):
        return
    if context.user_data.get("bulk_step") == "poster":
        context.user_data["bulk_data"]["poster_url"] = ""
        context.user_data["bulk_step"] = "videos"
        await reply_html_safe(update.message, add_footer(f"✅ Poster skipped.\n\nNow forward the videos in the same order as the titles.\nI will save them automatically.\nType /cancelbulk to cancel."))
    else:
        await reply_html_safe(update.message, add_footer(f"ℹ️ You are not in the poster step."))

async def handle_bulk_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS or not context.user_data.get("bulk_upload"):
        return
    if _other_session_active(context, "bulk_upload"):
        return

    step = context.user_data.get("bulk_step")
    bulk_data = context.user_data["bulk_data"]

    if step == "poster" and update.message.photo:
        photo = update.message.photo[-1]
        file_id = photo.file_id
        try:
            tg_file = await context.bot.get_file(file_id)
            file_bytes = io.BytesIO()
            await tg_file.download_to_memory(file_bytes)
            file_bytes.seek(0)
            url = upload_image_with_fallback(file_bytes.read(), context="bulk_upload_poster")
            bulk_data["poster_url"] = url or ""
            bulk_data["poster_file_id"] = file_id
        except Exception as e:
            logger.error(f"Poster upload error: {e}")
            bulk_data["poster_url"] = ""
            bulk_data["poster_file_id"] = file_id
        context.user_data["bulk_step"] = "videos"
        await reply_html_safe(update.message, add_footer(f"✅ Poster saved.\n\nNow forward the videos in the same order as the titles.\nI will save them automatically.\nType /cancelbulk to cancel."))
        return

    if step == "videos":
        tokens = bulk_data.get("tokens", [])
        index = bulk_data.get("video_index", 0)
        if index >= len(tokens):
            await reply_html_safe(update.message, add_footer(f"⚠️ You have sent more videos than titles. Extra ignored."))
            return

        title = tokens[index]
        file_id = None
        file_type = None
        channel_id = None
        message_id = None
        if update.message.video:
            file_id = update.message.video.file_id
            file_type = "video"
        elif update.message.document:
            file_id = update.message.document.file_id
            file_type = "document"
        elif update.message.forward_from_chat:
            channel_id = update.message.forward_from_chat.id
            message_id = update.message.forward_from_message_id
        else:
            await reply_html_safe(update.message, add_footer(f"❌ Unsupported media type. Please send a video, document, or forward from a channel."))
            return

        existing_tokens = [doc['token'] for doc in movies_collection.find({}, {'token': 1})]
        if series_bundles_collection is not None:
            existing_tokens.extend([doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})])
        token = generate_count_token()

        movie_doc = {"token": token, "title": title, "price": bulk_data.get("price", get_default_price()), "poster_url": bulk_data.get("poster_url", "") or "", "poster_file_id": bulk_data.get("poster_file_id", "") or "", "file_id": file_id, "file_type": file_type, "channel_id": channel_id, "message_id": message_id, "category": bulk_data.get("category", "movie"), "description": "", "downloads": 0, "created_at": datetime.utcnow()}
        try:
            movies_collection.insert_one(movie_doc)
            bulk_data.setdefault("saved_movies", []).append(movie_doc)
        except Exception as e:
            logger.error(f"Error saving movie {title}: {e}")
            await reply_html_safe(update.message, add_footer(f"❌ Failed to save {title}. Skipping."))
            bulk_data["video_index"] = index + 1
            return
        
        bulk_data["video_index"] = index + 1
        await update.message.reply_text(add_footer(f"✅ Saved ({index+1}/{len(tokens)}): <b>{title}</b> (token: <code>{token}</code>)"), parse_mode=ParseMode.HTML)

        if bulk_data["video_index"] >= len(tokens):
            # Mirror all bulk movies to UPLOAD_TARGET_CHANNEL
            if get_upload_target_channel() and bulk_data.get("saved_movies"):
                try:
                    await auto_mirror_uploads_to_target(
                        context.bot,
                        bulk_data["saved_movies"],
                        shared_poster_file_id=(bulk_data.get("poster_file_id") if upload_target_include_posters() else None),
                        shared_poster_caption=None,
                        include_posters=upload_target_include_posters(),
                    )
                except Exception as _mirror_err:
                    logger.error(f"bulk mirror: {_mirror_err}")
            await update.message.reply_text(add_footer(f"""🎉 <b>Bulk upload complete!</b>
Saved {len(tokens)} movies.
Poster: {'Yes' if bulk_data.get('poster_url') else 'No'}
Price: {bulk_data.get('price')} KES each."""), parse_mode=ParseMode.HTML)
            _clear_bulk_upload_state(admin.id, context)

# ============ BUNDLE CREATION ============
async def create_bundle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return

    context.user_data["bundle_session"] = {"step": "tokens", "tokens": [], "price": None, "poster_url": None, "title": None}

    await update.message.reply_text(add_footer(f"""📦 <b>Create Bundle</b>

Send me the list of movie tokens or links, one per line.
Example:
smith-prison-break-41
https://t.me/YOUR_BOT?start=example-token
smith-prison-break-36

After sending the list, I'll ask for title, price, and poster.
Type /cancelbundle to cancel.
<i>Minimum 2 tokens required.</i>"""), parse_mode=ParseMode.HTML)

async def cancel_bundle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if context.user_data.get("bundle_session"):
        context.user_data.pop("bundle_session")
        await reply_html_safe(update.message, add_footer(f"❌ Bundle creation cancelled."))
    else:
        await reply_html_safe(update.message, add_footer(f"ℹ️ No active bundle creation."))

async def handle_bundle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    session = context.user_data.get("bundle_session")
    if not session:
        return
    step = session.get("step")
    text = update.message.text.strip()

    if step == "tokens":
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        tokens = []
        for line in lines:
            if line.startswith("http"):
                match = re.search(r'[?&]start=([^&\s]+)', line)
                if match:
                    tokens.append(match.group(1))
                else:
                    await reply_html_safe(update.message, add_footer(f"❌ Could not extract token from: {line}"))
                    return
            else:
                tokens.append(line)
        if not tokens:
            await reply_html_safe(update.message, add_footer(f"❌ No tokens found."))
            return
        if len(tokens) < 2:
            await reply_html_safe(update.message, add_footer(f"❌ Minimum 2 tokens required."))
            return
        valid_tokens = []
        for token in tokens:
            movie = movies_collection.find_one({"token": token})
            if not movie:
                await reply_html_safe(update.message, add_footer(f"❌ Movie with token `{token}` not found."))
                return
            valid_tokens.append(token)
        session["tokens"] = valid_tokens
        session["step"] = "title"
        await update.message.reply_text(add_footer(f"""✅ {len(valid_tokens)} movies added.

Now send a <b>title</b> for this bundle (e.g., "Action Pack Collection").
Or type /skip to auto-generate a title."""), parse_mode=ParseMode.HTML)
    elif step == "title":
        if text.lower() == "/skip":
            title = f"Bundle of {len(session['tokens'])} movies"
            session["title"] = title
            await reply_html_safe(update.message, add_footer(f"✅ Using auto-title: {title}"))
        else:
            session["title"] = text
            await reply_html_safe(update.message, add_footer(f"✅ Title set: {text}"))
        session["step"] = "price"
        total = sum(movies_collection.find_one({"token": t}).get("price", 0) for t in session["tokens"])
        await update.message.reply_text(add_footer(f"""💰 Now send the <b>total price</b> for this bundle (in KES).
Example: 150
Or type /skip to use the sum of individual prices: {total} KES."""), parse_mode=ParseMode.HTML)
    elif step == "price":
        if text.lower() == "/skip":
            total = sum(movies_collection.find_one({"token": t}).get("price", get_default_price()) for t in session["tokens"])
            session["price"] = total
            await reply_html_safe(update.message, add_footer(f"✅ Using total of individual prices: {total} KES"))
            session["step"] = "poster"
            await update.message.reply_text(add_footer(f"""📷 Now send an optional <b>poster photo</b> for the bundle.
Or type /skipposter to skip.
After that, type /completebundle to finalize."""), parse_mode=ParseMode.HTML)
            return
        try:
            price = int(text)
            if price < 0:
                await reply_html_safe(update.message, add_footer(f"❌ Price must be 0 or more."))
                return
            session["price"] = price
            session["step"] = "poster"
            await update.message.reply_text(add_footer(f"""✅ Price set to {price} KES.

Now send an optional <b>poster photo</b> for the bundle.
Or type /skipposter to skip.
After that, type /completebundle to finalize."""), parse_mode=ParseMode.HTML)
        except ValueError:
            await reply_html_safe(update.message, add_footer(f"❌ Invalid price. Please send a number or /skip."))
    elif step == "poster":
        pass

async def handle_bundle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    if _other_session_active(context, "bundle_session"):
        return
    session = context.user_data.get("bundle_session")
    if not session or session.get("step") != "poster":
        return
    if update.message.photo:
        photo = update.message.photo[-1]
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            file_bytes = io.BytesIO()
            await tg_file.download_to_memory(file_bytes)
            file_bytes.seek(0)
            url = upload_image_with_fallback(file_bytes.read(), context="bundle_poster")
            if url:
                session["poster_url"] = url
                await reply_html_safe(update.message, add_footer(f"✅ Poster saved."))
            else:
                session["poster_url"] = None
                await reply_html_safe(update.message, add_footer(f"⚠️ Failed to upload poster. Skipping."))
        except Exception as e:
            logger.error(f"Bundle poster upload error: {e}")
            session["poster_url"] = None
            await reply_html_safe(update.message, add_footer(f"⚠️ Poster upload failed. Skipping."))
        session["step"] = "done"
        await update.message.reply_text(add_footer(f"✅ Bundle ready. Type /completebundle to create the bundle."), parse_mode=ParseMode.HTML)
    else:
        await reply_html_safe(update.message, add_footer(f"📷 Please send a photo or type /skipposter."))

async def skip_bundle_poster_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return
    session = context.user_data.get("bundle_session")
    if not session or session.get("step") != "poster":
        await reply_html_safe(update.message, add_footer(f"ℹ️ You are not in the poster step."))
        return
    session["poster_url"] = None
    session["step"] = "done"
    await reply_html_safe(update.message, add_footer(f"✅ Poster skipped. Type /completebundle to create the bundle."))

async def complete_bundle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    session = context.user_data.get("bundle_session")
    if not session or session.get("step") != "done":
        await reply_html_safe(update.message, add_footer(f"❌ No active bundle session or not ready. Use /createbundle first."))
        return

    tokens = session.get("tokens", [])
    price = session.get("price", 0)
    poster_url = session.get("poster_url", "")
    title = session.get("title") or f"Bundle of {len(tokens)} movies"

    movies = []
    for token in tokens:
        movie = movies_collection.find_one({"token": token})
        if movie:
            movies.append(movie)

    if not movies:
        await reply_html_safe(update.message, add_footer(f"❌ No valid movies found."))
        context.user_data.pop("bundle_session")
        return

    episodes = []
    for movie in movies:
        episodes.append({"private_link": f"https://t.me/c/{abs(movie.get('channel_id', 0))}/{movie.get('message_id', '')}" if movie.get('channel_id') and movie.get('message_id') else "", "title": movie.get("title", "Unknown"), "button_title": clean_movie_title_for_button(movie.get("title", "Unknown")), "channel_id": str(movie.get("channel_id", "")), "message_id": int(movie.get("message_id", 0) or 0), "file_id": movie.get("file_id"), "file_type": movie.get("file_type")})

    existing_tokens = [doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})] if series_bundles_collection is not None else []
    bundle_token = generate_count_token()

    bundle_data = {"token": bundle_token, "title": title, "button_title": clean_movie_title_for_button(title), "price": price, "category": "series", "description": f"Bundle of {len(movies)} movies", "poster_url": poster_url or (movies[0].get('poster_url', '') if movies else ''), "episodes": episodes, "episode_count": len(episodes), "created_at": datetime.utcnow(), "downloads": 0}

    series_bundles_collection.insert_one(bundle_data)

    # Mirror bundle episodes to UPLOAD_TARGET_CHANNEL (videos with titles; shared poster if any)
    if get_upload_target_channel() and movies:
        try:
            poster_fid = None
            if upload_target_include_posters():
                poster_fid = session.get("poster_file_id") or (movies[0].get("poster_file_id") if movies else None)
            await auto_mirror_uploads_to_target(
                context.bot,
                movies,
                shared_poster_file_id=poster_fid,
                shared_poster_caption=title,
                include_posters=upload_target_include_posters(),
            )
        except Exception as _mirror_err:
            logger.error(f"complete_bundle mirror: {_mirror_err}")

    await update.message.reply_text(add_footer(f"""✅ Bundle created!

Title: {title}
Token: `{bundle_token}`
Movies: {len(movies)}
Total Price: {price} KES
Deep link: https://t.me/{context.bot.username}?start={bundle_token}"""), parse_mode=ParseMode.HTML)
    context.user_data.pop("bundle_session")

# ============ SELECT BUNDLE COMMAND ============
async def select_bundle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return

    context.user_data.pop("select_bundle_session", None)
    context.user_data.pop("select_bundle_selected", None)

    all_movies = list(movies_collection.find().sort("title", ASCENDING))
    if not all_movies:
        await update.message.reply_text("❌ No movies found in database.")
        return

    context.user_data["select_bundle_session"] = {"step": "selecting", "selected_tokens": [], "selected_titles": [], "page": 0, "total": len(all_movies)}

    await show_select_bundle_page(update, context, 0)

async def show_select_bundle_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    per_page = 20
    all_movies = list(movies_collection.find().sort("title", ASCENDING))
    total = len(all_movies)
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    if page >= total_pages:
        page = total_pages - 1

    start = page * per_page
    end = min(start + per_page, total)
    page_movies = all_movies[start:end]

    session = context.user_data.get("select_bundle_session", {})
    selected = session.get("selected_tokens", [])

    keyboard = []
    for movie in page_movies:
        token = movie.get("token", "")
        title = clean_movie_title_for_button(movie.get("title", "Unknown"))
        is_selected = token in selected
        status = "✅" if is_selected else "⬜"
        display_title = title[:30] + '...' if len(title) > 30 else title
        keyboard.append([InlineKeyboardButton(f"{status} {display_title}", callback_data=f"sel_toggle_{token}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sel_page_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"sel_page_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton(f"📊 Selected: {len(selected)} movies", callback_data="sel_none")])
    keyboard.append([InlineKeyboardButton("✅ Done Selecting", callback_data="sel_done")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="sel_cancel")])

    admin_id = update.effective_user.id
    items = []
    items.append({"label": f"Page {page + 1} of {total_pages}", "value": f"{total} movies total"})
    items.append({"label": "", "value": ""})
    items.append({"label": "📋 Selected:", "value": f"{len(selected)}"})
    if selected:
        for t in selected[:5]:
            movie = movies_collection.find_one({"token": t})
            if movie:
                title_display = clean_movie_title_for_button(movie.get('title', 'Unknown'), max_len=22)
                items.append({"label": "▪︎", "value": title_display})
        if len(selected) > 5:
            items.append({"label": "...", "value": f"and {len(selected) - 5} more"})
    else:
        items.append({"label": "None selected yet.", "value": ""})

    text = render_menu_for_user(admin_id, "📦 SELECT MOVIES", items)
    text = add_footer(text)

    if update.callback_query:
        query = update.callback_query
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    if session:
        session["page"] = page
        context.user_data["select_bundle_session"] = session

async def handle_select_bundle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return
    
    session = context.user_data.get("select_bundle_session", {})
    
    if data == "sel_new_search":
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="sel_cancel")]])
        await query.edit_message_text(f"🔍 <b>Search for Movies</b>\n\nType a movie name, genre, or keyword.\nExample: <code>Action</code> or <code>Furious</code>", reply_markup=keyboard, parse_mode=ParseMode.HTML)
        context.user_data["select_bundle_mode"] = "awaiting_search"
        return

    if data.startswith("sel_toggle_"):
        token = data.replace("sel_toggle_", "")
        selected = session.get("selected_tokens", [])
        titles = session.get("selected_titles", [])
        
        movie = movies_collection.find_one({"token": token})
        if not movie:
            await query.answer("Movie not found!", show_alert=True)
            return
        
        if token in selected:
            selected.remove(token)
            if movie.get("title") in titles:
                titles.remove(movie.get("title"))
            await query.answer(f"Removed: {movie.get('title')}")
        else:
            selected.append(token)
            titles.append(movie.get("title"))
            await query.answer(f"Added: {movie.get('title')}")
        
        session["selected_tokens"] = selected
        session["selected_titles"] = titles
        context.user_data["select_bundle_session"] = session
        
        page = session.get("page", 0)
        await show_select_bundle_page(update, context, page)
    
    elif data.startswith("sel_page_"):
        page = int(data.replace("sel_page_", ""))
        await show_select_bundle_page(update, context, page)
    
    elif data == "sel_done":
        selected = session.get("selected_tokens", [])
        if len(selected) < 2:
            await query.answer("Please select at least 2 movies!", show_alert=True)
            return
        
        await show_select_bundle_details(update, context)
    
    elif data == "sel_cancel":
        context.user_data.pop("select_bundle_session", None)
        context.user_data.pop("select_bundle_mode", None)
        await query.edit_message_text(add_footer("❌ Bundle creation cancelled."), reply_markup=build_back_keyboard("back"))
    
    elif data == "sel_none":
        await query.answer()

async def show_select_bundle_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    session = context.user_data.get("select_bundle_session", {})
    selected = session.get("selected_tokens", [])
    selected_titles = session.get("selected_titles", [])
    
    if len(selected) < 2:
        await query.answer("Please select at least 2 movies!", show_alert=True)
        return
    
    title_list = "\n".join([f"▪︎ {t}" for t in selected_titles])
    
    total_price = 0
    for token in selected:
        movie = movies_collection.find_one({"token": token})
        if movie:
            total_price += movie.get("price", 0)
    
    text = f"""📦 <b>Bundle Details</b>

<b>Selected Movies ({len(selected)}):</b>
{title_list}

Total individual price: <b>{total_price} KES</b>

Now send:
1️⃣ <b>Title</b> for the bundle (or /skip to auto-generate)
2️⃣ <b>Price</b> (or /skip to use total: {total_price} KES)
3️⃣ <b>Poster</b> (optional, send a photo or /skipposter)
4️⃣ Type <b>/donebyconfronter</b> to finalize

<i>Example: "Action Pack Collection"</i>"""
    
    text = add_footer(text)
    
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="sel_cancel")]])
    
    if query:
        try:
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    
    session["step"] = "details"
    context.user_data["select_bundle_session"] = session
    context.user_data["select_bundle_mode"] = "awaiting_details"

async def handle_select_bundle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return

    if context.user_data.get("select_bundle_mode") == "awaiting_search":
        await handle_selectbundle_search(update, context)
        return

    session = context.user_data.get("select_bundle_session")
    if not session or session.get("step") != "details":
        return

    text = update.message.text.strip()

    if text.startswith("/"):
        return

    if "title" not in session:
        session["title"] = text
        context.user_data["select_bundle_session"] = session

        selected_tokens = session.get("selected_tokens", [])
        total_price = 0
        for token in selected_tokens:
            movie = movies_collection.find_one({"token": token})
            if movie:
                total_price += movie.get("price", 0)

        await update.message.reply_text(add_footer(f"""✅ Title set: <b>{text}</b>

💰 Now send the <b>total price</b> for this bundle (in KES).
Example: 150
Or type <code>/skip</code> to use total of individual prices: {total_price} KES."""), parse_mode=ParseMode.HTML)
        return

    if "price" not in session:
        if text.lower() == "/skip":
            selected_tokens = session.get("selected_tokens", [])
            total_price = 0
            for token in selected_tokens:
                movie = movies_collection.find_one({"token": token})
                if movie:
                    total_price += movie.get("price", 0)
            session["price"] = total_price
            context.user_data["select_bundle_session"] = session
            await update.message.reply_text(add_footer(f"✅ Using total of individual prices: {total_price} KES\n\nNow send a <b>poster photo</b> (optional) or /skipposter."), parse_mode=ParseMode.HTML)
            return

        try:
            price = int(text)
            if price < 0:
                await reply_html_safe(update.message, add_footer("❌ Price must be 0 or more."))
                return
            session["price"] = price
            context.user_data["select_bundle_session"] = session
            await update.message.reply_text(add_footer(f"✅ Price set to {price} KES\n\nNow send a <b>poster photo</b> (optional) or /skipposter."), parse_mode=ParseMode.HTML)
        except ValueError:
            await reply_html_safe(update.message, add_footer("❌ Invalid price. Please send a number."))

async def handle_select_bundle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return

    session = context.user_data.get("select_bundle_session")
    if not session or session.get("step") != "details":
        return

    if update.message.photo:
        photo = update.message.photo[-1]
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            file_bytes = io.BytesIO()
            await tg_file.download_to_memory(file_bytes)
            file_bytes.seek(0)
            url = upload_image_with_fallback(file_bytes.read(), context="select_bundle_poster")
            if url:
                session["poster_url"] = url
                await reply_html_safe(update.message, add_footer("✅ Poster saved."))
            else:
                session["poster_url"] = None
                await reply_html_safe(update.message, add_footer("⚠️ Failed to upload poster. Skipping."))
        except Exception as e:
            logger.error(f"Select bundle poster upload error: {e}")
            session["poster_url"] = None
            await reply_html_safe(update.message, add_footer("⚠️ Poster upload failed. Skipping."))

        context.user_data["select_bundle_session"] = session
        await update.message.reply_text(add_footer("✅ Bundle ready. Type <code>/donebyconfronter</code> to create the bundle."), parse_mode=ParseMode.HTML)

async def skip_select_bundle_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        return

    session = context.user_data.get("select_bundle_session")
    if not session or session.get("step") != "details":
        await reply_html_safe(update.message, add_footer("ℹ️ No active bundle session."))
        return

    session["poster_url"] = None
    context.user_data["select_bundle_session"] = session
    await update.message.reply_text(add_footer("✅ Poster skipped. Type <code>/donebyconfronter</code> to create the bundle."), parse_mode=ParseMode.HTML)

async def donebyconfronter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    
    session = context.user_data.get("select_bundle_session")
    if not session or session.get("step") != "details":
        await reply_html_safe(update.message, add_footer("❌ No active bundle session or not ready. Use /selectbundle or /selectcollection first."), reply_markup=build_back_keyboard("back"))
        return

    tokens = session.get("selected_tokens", [])
    price = session.get("price", 0)
    poster_url = session.get("poster_url", "")
    title = session.get("title") or f"Bundle of {len(tokens)} movies"

    movies = []
    for token in tokens:
        movie = movies_collection.find_one({"token": token})
        if movie:
            movies.append(movie)

    if not movies:
        await reply_html_safe(update.message, add_footer("❌ No valid movies found."), reply_markup=build_back_keyboard("back"))
        context.user_data.pop("select_bundle_session")
        return

    episodes = []
    for movie in movies:
        episodes.append({"private_link": f"https://t.me/c/{abs(movie.get('channel_id', 0))}/{movie.get('message_id', '')}" if movie.get('channel_id') and movie.get('message_id') else "", "title": movie.get("title", "Unknown"), "button_title": clean_movie_title_for_button(movie.get("title", "Unknown")), "channel_id": str(movie.get("channel_id", "")), "message_id": int(movie.get("message_id", 0) or 0), "file_id": movie.get("file_id"), "file_type": movie.get("file_type")})

    existing_tokens = [doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})] if series_bundles_collection is not None else []
    bundle_token = generate_count_token()

    bundle_data = {"token": bundle_token, "title": title, "button_title": clean_movie_title_for_button(title), "price": price, "category": "series", "description": f"Bundle of {len(movies)} movies", "poster_url": poster_url or (movies[0].get('poster_url', '') if movies else ''), "episodes": episodes, "episode_count": len(episodes), "created_at": datetime.utcnow(), "downloads": 0}

    series_bundles_collection.insert_one(bundle_data)

    await update.message.reply_text(add_footer(f"""✅ Bundle created!

Title: {title}
Token: `{bundle_token}`
Movies: {len(movies)}
Total Price: {price} KES
Deep link: https://t.me/{context.bot.username}?start={bundle_token}"""), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 View Bundle", url=f"https://t.me/{context.bot.username}?start={bundle_token}")], [InlineKeyboardButton("📡 More Movies", callback_data="more_movies")], [InlineKeyboardButton("⬅️ Back", callback_data="back")]]))
    context.user_data.pop("select_bundle_session")
    context.user_data.pop("select_bundle_mode", None)

# ============ CHANNEL POST CREATION ============
async def add_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    context.user_data["add_channel_mode"] = True
    await update.message.reply_text("📢 **Add a Channel**\n\nPlease **forward any message** from the channel you want to add to the bot.\nThe bot must be an admin in that channel to post messages.\n\nSend /cancel to abort.")

async def handle_add_channel_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.user_data.get("add_channel_mode"):
        return
    msg = update.message
    origin = msg.forward_origin
    if not origin:
        await msg.reply_text("❌ Please forward a message from a channel.")
        return
    if hasattr(origin, 'type') and origin.type == 'channel' and hasattr(origin, 'chat'):
        chat = origin.chat
        channel_id = chat.id
        channel_title = chat.title or "Unknown Channel"
        username = chat.username or ""
    else:
        await msg.reply_text("❌ Please forward a message from a channel, not a user or group.")
        return
    
    if channels_collection.find_one({"channel_id": channel_id}):
        await msg.reply_text(f"❌ Channel **{channel_title}** is already added.")
        context.user_data.pop("add_channel_mode", None)
        return
    
    channels_collection.insert_one({"channel_id": channel_id, "title": channel_title, "username": username, "added_by": ADMIN_ID, "added_at": datetime.utcnow()})
    await msg.reply_text(f"✅ Channel **{channel_title}** added successfully!\nUse /createpost to post to it.")
    context.user_data.pop("add_channel_mode", None)

async def list_channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    channels = list(channels_collection.find().sort("title", ASCENDING))
    if not channels:
        await update.message.reply_text("📢 No channels added yet.\nUse /addchannel to add one.")
        return
    
    text = "📢 <b>Your Channels</b>\n\n"
    for c in channels:
        channel_id_escaped = escape_html(str(c['channel_id']))
        text += f"• {escape_html(c['title'])} (ID: <code>{channel_id_escaped}</code>)\n"
    text += "\nUse /removechannel &lt;channel_id&gt; to remove."
    
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def remove_channel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /removechannel <channel_id>")
        return
    try:
        channel_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID.")
        return
    result = channels_collection.delete_one({"channel_id": channel_id})
    if result.deleted_count:
        await update.message.reply_text(f"✅ Channel removed.")
    else:
        await update.message.reply_text("❌ Channel not found.")

async def create_post_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return
    if context.user_data.get("post_draft"):
        await update.message.reply_text("⚠️ You have an ongoing post. Use /sendpost to publish, /preview to preview, or /cancelpost to cancel.")
        return

    if channels_collection is None:
        await update.message.reply_text("❌ Database unavailable. Please try again later.")
        return

    channels = list(channels_collection.find().sort("title", ASCENDING))
    if not channels:
        await update.message.reply_text("📢 No channels added yet.\nUse /addchannel to add one.")
        return

    keyboard = []
    for c in channels:
        keyboard.append([InlineKeyboardButton(c['title'], callback_data=f"post_channel_{c['channel_id']}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="post_cancel")])

    await update.message.reply_text("📢 **Create a New Post**\n\nSelect a channel to post to:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_post_channel_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "post_cancel":
        await query.edit_message_text("❌ Post creation cancelled.")
        context.user_data.pop("post_draft", None)
        return

    if data.startswith("post_channel_"):
        if channels_collection is None:
            await query.edit_message_text("❌ Database error. Please contact admin.")
            return

        try:
            channel_id = int(data.split("_")[2])
        except (IndexError, ValueError):
            await query.edit_message_text("❌ Invalid channel ID.")
            return

        channel = channels_collection.find_one({"channel_id": channel_id})
        if not channel:
            await query.edit_message_text("❌ Channel not found.")
            return

        context.user_data["post_draft"] = {"channel_id": channel_id, "channel_title": channel["title"], "messages": [], "buttons": [], "per_row": 3, "button_style": "default"}

        text = f"📝 **Composing post for {channel['title']}**\n\nSend me the content for the post.\nYou can send:\n- Text messages\n- Photos (with optional caption)\n- Videos\n- Documents\n- Stickers\n\n**Important:** When you add buttons, the button text will be displayed - NOT the caption.\nThe caption you add to the post will be used as the message text.\n\nUse **/buttons** to add URL buttons.\nUse **/preview** to see a preview.\nUse **/sendpost** to publish.\nUse **/cancelpost** to cancel."

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Buttons", callback_data="post_add_buttons")], [InlineKeyboardButton("👁️ Preview", callback_data="post_preview")], [InlineKeyboardButton("📤 Send Now", callback_data="post_send")], [InlineKeyboardButton("❌ Cancel", callback_data="post_cancel")]])

        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def buttons_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return

    draft = context.user_data.get("post_draft")
    if not draft:
        await update.message.reply_text("❌ No active post. Use /createpost first.")
        return

    context.user_data["awaiting_buttons"] = True
    await update.message.reply_text("🔗 **Add URL Buttons**\n\nSend me a list of buttons using this format:\n`Button text 1 - http://example.com`\n`Button text 2 - http://example2.com`\n\nYou can put up to 3 buttons per row using `|`:\n`Button 1 - http://a.com | Button 2 - http://b.com`\n\nSend /cancel to abort.\n\n⚠️ **Important:** The button text is what will be displayed, not the URL.", parse_mode=ParseMode.HTML)

async def preview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return

    draft = context.user_data.get("post_draft")
    if not draft:
        await update.message.reply_text("❌ No active post.")
        return

    if not draft["messages"]:
        await update.message.reply_text("❌ No messages in the post. Send some content first.")
        return

    await update.message.reply_text("👁️ **Post Preview** (will be sent to channel):")
    for i, msg in enumerate(draft["messages"]):
        try:
            if msg["type"] == "text":
                await update.message.reply_text(msg["data"])
            elif msg["type"] == "photo":
                await update.message.reply_photo(photo=msg["data"], caption=msg["caption"])
            elif msg["type"] == "video":
                await update.message.reply_video(video=msg["data"], caption=msg["caption"])
            elif msg["type"] == "document":
                await update.message.reply_document(document=msg["data"], caption=msg["caption"])
            elif msg["type"] == "sticker":
                await update.message.reply_sticker(sticker=msg["data"])
        except Exception as e:
            await update.message.reply_text(f"❌ Error previewing message {i+1}: {e}")

    if draft["buttons"]:
        kb = build_button_rows(draft["buttons"], per_row=3)
        await update.message.reply_text("🔘 Buttons:", reply_markup=kb)

async def sendpost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return

    draft = context.user_data.get("post_draft")
    if not draft:
        await update.message.reply_text("❌ No active post.")
        return

    if not draft["messages"]:
        await update.message.reply_text("❌ No messages to send.")
        return

    channel_id = draft["channel_id"]
    buttons = draft.get("buttons", [])
    per_row = draft.get("per_row", 3)

    await update.message.reply_text("📤 Sending post to channel...")
    success_count = 0

    photo_messages = []
    non_photo_messages = []
    for msg in draft["messages"]:
        if msg["type"] == "photo":
            photo_messages.append(msg)
        else:
            non_photo_messages.append(msg)

    keyboard = None
    if buttons:
        keyboard = build_button_rows(buttons, per_row=per_row)

    if len(photo_messages) >= 2:
        media_group = []
        for i, msg in enumerate(photo_messages):
            if i == len(photo_messages) - 1 and len(non_photo_messages) == 0 and msg.get("caption"):
                media_group.append(InputMediaPhoto(media=msg["data"], caption=msg.get("caption", ""), parse_mode=ParseMode.HTML))
            else:
                media_group.append(InputMediaPhoto(media=msg["data"]))
        try:
            await application.bot.send_media_group(chat_id=channel_id, media=media_group)
            success_count += len(photo_messages)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to send album: {e}")
    elif len(photo_messages) == 1:
        msg = photo_messages[0]
        try:
            await application.bot.send_photo(chat_id=channel_id, photo=msg["data"], caption=msg.get("caption", ""), reply_markup=keyboard, parse_mode=ParseMode.HTML)
            success_count += 1
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to send photo: {e}")
    else:
        for msg in non_photo_messages:
            try:
                if msg["type"] == "text":
                    await application.bot.send_message(chat_id=channel_id, text=msg["data"], reply_markup=keyboard, parse_mode=ParseMode.HTML)
                elif msg["type"] == "video":
                    await application.bot.send_video(chat_id=channel_id, video=msg["data"], caption=msg.get("caption", ""), reply_markup=keyboard, parse_mode=ParseMode.HTML)
                elif msg["type"] == "document":
                    await application.bot.send_document(chat_id=channel_id, document=msg["data"], caption=msg.get("caption", ""), reply_markup=keyboard, parse_mode=ParseMode.HTML)
                elif msg["type"] == "sticker":
                    await application.bot.send_sticker(chat_id=channel_id, sticker=msg["data"])
                success_count += 1
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to send message: {e}")

    if len(photo_messages) >= 2 and buttons and keyboard:
        has_caption = any(hasattr(media, 'caption') and media.caption for media in media_group)
        if not has_caption and non_photo_messages:
            try:
                await send_html_safe(application.bot, channel_id, apply_premium_emojis("🔗 Tap a button below ↓"), reply_markup=keyboard)
            except Exception as e:
                await update.message.reply_text(f"❌ Failed to send buttons: {e}")

    await update.message.reply_text(f"✅ Post sent! {success_count}/{len(draft['messages'])} messages delivered.\n🔘 Buttons: {'✅' if buttons else '❌'}", parse_mode=ParseMode.HTML)
    context.user_data.pop("post_draft", None)

async def cancelpost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized.")
        return

    draft = context.user_data.pop("post_draft", None)
    if draft:
        await update.message.reply_text("❌ Post creation cancelled.")
    else:
        await update.message.reply_text("ℹ️ No active post to cancel.")

async def handle_post_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return

    draft = context.user_data.get("post_draft")
    if not draft:
        await query.edit_message_text("❌ No active post. Use /createpost first.")
        return

    context.user_data["awaiting_buttons"] = True
    await query.edit_message_text("🔗 **Add URL Buttons**\n\nSend me a list of buttons using this format:\n`Button text 1 - http://example.com`\n`Button text 2 - http://example2.com`\n\nYou can put up to 3 buttons per row using `|`:\n`Button 1 - http://a.com | Button 2 - http://b.com`\n\nSend /cancel to abort.\n\n⚠️ **Important:** The button text is what will be displayed, not the URL.", parse_mode=ParseMode.HTML)

async def handle_buttons_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.user_data.get("awaiting_buttons"):
        return
    text = update.message.text.strip()
    if text == "/cancel":
        context.user_data.pop("awaiting_buttons", None)
        await update.message.reply_text("❌ Button input cancelled.")
        return
    
    buttons = parse_buttons_from_text(text)
    if not buttons:
        await update.message.reply_text("❌ No valid buttons found. Please use the format: `Text - URL`")
        return
    
    draft = context.user_data.get("post_draft")
    if draft:
        draft["buttons"] = buttons
        await update.message.reply_text(f"✅ {len(buttons)} button(s) added to the post.\nUse /preview to see them.")
    else:
        await update.message.reply_text("❌ No active post.")
    context.user_data.pop("awaiting_buttons", None)

async def handle_post_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return

    draft = context.user_data.get("post_draft")
    if not draft:
        await query.edit_message_text("❌ No active post.")
        return

    if not draft["messages"]:
        await query.edit_message_text("❌ No messages in the post. Send some content first.")
        return

    await query.message.reply_text("👁️ **Post Preview** (will be sent to channel):")
    for i, msg in enumerate(draft["messages"]):
        try:
            if msg["type"] == "text":
                await query.message.reply_text(msg["data"])
            elif msg["type"] == "photo":
                await query.message.reply_photo(photo=msg["data"], caption=msg["caption"])
            elif msg["type"] == "video":
                await query.message.reply_video(video=msg["data"], caption=msg["caption"])
            elif msg["type"] == "document":
                await query.message.reply_document(document=msg["data"], caption=msg["caption"])
            elif msg["type"] == "sticker":
                await query.message.reply_sticker(sticker=msg["data"])
        except Exception as e:
            await query.message.reply_text(f"❌ Error previewing message {i+1}: {e}")

    if draft["buttons"]:
        kb = build_button_rows(draft["buttons"], per_row=3)
        await query.message.reply_text("🔘 Buttons:", reply_markup=kb)

async def handle_post_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return

    draft = context.user_data.get("post_draft")
    if not draft:
        await query.edit_message_text("❌ No active post.")
        return

    if not draft["messages"]:
        await query.edit_message_text("❌ No messages to send.")
        return

    channel_id = draft["channel_id"]
    buttons = draft.get("buttons", [])
    per_row = draft.get("per_row", 3)

    await query.edit_message_text("📤 Sending post to channel...")
    success_count = 0

    photo_messages = []
    non_photo_messages = []
    for msg in draft["messages"]:
        if msg["type"] == "photo":
            photo_messages.append(msg)
        else:
            non_photo_messages.append(msg)

    keyboard = None
    if buttons:
        keyboard = build_button_rows(buttons, per_row=per_row)

    if len(photo_messages) >= 2:
        media_group = []
        for i, msg in enumerate(photo_messages):
            if i == len(photo_messages) - 1 and len(non_photo_messages) == 0 and msg.get("caption"):
                media_group.append(InputMediaPhoto(media=msg["data"], caption=msg.get("caption", ""), parse_mode=ParseMode.HTML))
            else:
                media_group.append(InputMediaPhoto(media=msg["data"]))
        try:
            await application.bot.send_media_group(chat_id=channel_id, media=media_group)
            success_count += len(photo_messages)
        except Exception as e:
            await query.message.reply_text(f"❌ Failed to send album: {e}")
    elif len(photo_messages) == 1:
        msg = photo_messages[0]
        try:
            await application.bot.send_photo(chat_id=channel_id, photo=msg["data"], caption=msg.get("caption", ""), reply_markup=keyboard, parse_mode=ParseMode.HTML)
            success_count += 1
        except Exception as e:
            await query.message.reply_text(f"❌ Failed to send photo: {e}")
    else:
        for msg in non_photo_messages:
            try:
                if msg["type"] == "text":
                    await application.bot.send_message(chat_id=channel_id, text=msg["data"], reply_markup=keyboard, parse_mode=ParseMode.HTML)
                elif msg["type"] == "video":
                    await application.bot.send_video(chat_id=channel_id, video=msg["data"], caption=msg.get("caption", ""), reply_markup=keyboard, parse_mode=ParseMode.HTML)
                elif msg["type"] == "document":
                    await application.bot.send_document(chat_id=channel_id, document=msg["data"], caption=msg.get("caption", ""), reply_markup=keyboard, parse_mode=ParseMode.HTML)
                elif msg["type"] == "sticker":
                    await application.bot.send_sticker(chat_id=channel_id, sticker=msg["data"])
                success_count += 1
            except Exception as e:
                await query.message.reply_text(f"❌ Failed to send message: {e}")

    if len(photo_messages) >= 2 and buttons and keyboard:
        has_caption = any(hasattr(media, 'caption') and media.caption for media in media_group)
        if not has_caption and non_photo_messages:
            try:
                await send_html_safe(application.bot, channel_id, apply_premium_emojis("🔗 Tap a button below ↓"), reply_markup=keyboard)
            except Exception as e:
                await query.message.reply_text(f"❌ Failed to send buttons: {e}")

    await query.edit_message_text(f"✅ Post sent! {success_count}/{len(draft['messages'])} messages delivered.\n🔘 Buttons: {'✅' if buttons else '❌'}", parse_mode=ParseMode.HTML)
    context.user_data.pop("post_draft", None)

async def handle_post_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("❌ Unauthorized.")
        return

    draft = context.user_data.pop("post_draft", None)
    if draft:
        await query.edit_message_text("❌ Post creation cancelled.")
    else:
        await query.edit_message_text("ℹ️ No active post to cancel.")

async def handle_post_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    draft = context.user_data.get("post_draft")
    if not draft:
        return

    msg = update.message
    msg_obj = None
    if msg.text:
        msg_obj = {"type": "text", "data": msg.text, "caption": None}
    elif msg.photo:
        photo = msg.photo[-1]
        msg_obj = {"type": "photo", "data": photo.file_id, "caption": msg.caption or ""}
    elif msg.video:
        msg_obj = {"type": "video", "data": msg.video.file_id, "caption": msg.caption or ""}
    elif msg.document:
        msg_obj = {"type": "document", "data": msg.document.file_id, "caption": msg.caption or ""}
    elif msg.sticker:
        msg_obj = {"type": "sticker", "data": msg.sticker.file_id, "caption": msg.caption or ""}
    else:
        await update.message.reply_text("❌ Unsupported media type.")
        return

    if msg_obj:
        draft["messages"].append(msg_obj)
        count = len(draft["messages"])
        await update.message.reply_text(f"✅ Message {count} added to the post.\nSend more or use /preview /sendpost.")

async def handle_upload_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Upload feature is under development. Use /upload command.")

# ============ FORCE CHANNEL CHECK FUNCTION ============
async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not FORCE_CHANNELS:
        return True
    user = update.effective_user
    if user is None:
        return True
    all_joined, missing = await _check_force_membership(context, user.id)
    if not all_joined:
        join_text, join_kb = await _build_force_join_message(context, user.id, missing)
        try:
            if update.message:
                await update.message.reply_text(add_footer(join_text), reply_markup=join_kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            elif update.callback_query:
                await edit_or_send_message(update.callback_query, add_footer(join_text), reply_markup=join_kb, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=add_footer(join_text), reply_markup=join_kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Failed to send force join message: {e}")
        return False
    return True

async def force_join_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None:
        return
    if update.effective_user.id in ADMIN_IDS:
        return
    ok = await check_force_join(update, context)
    if not ok:
        raise ApplicationHandlerStop()

async def force_join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if req is None:
        return
    try:
        link = req.invite_link.invite_link if req.invite_link else None
        if link:
            for ch in FORCE_CHANNELS:
                if ch['type'] == 'invite' and ch['value'] == link:
                    _store_invite_chat_id(link, req.chat.id, req.chat.title or "")
                    _FORCE_CHAT_CACHE.pop(f"invite:{link}", None)
                    break
        await req.approve()
    except Exception as e:
        logger.warning(f"Join-request handling failed: {e}")

async def force_my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.my_chat_member
    if m is None:
        return
    try:
        if m.chat.type in ('channel', 'supergroup') and m.new_chat_member.status in ('administrator', 'member'):
            fm = _force_meta()
            if fm is not None:
                fm.update_one({"kind": "known_chat", "chat_id": m.chat.id}, {"$set": {"title": m.chat.title or "", "username": m.chat.username or "", "updated_at": datetime.utcnow()}}, upsert=True)
    except Exception as e:
        logger.warning(f"my_chat_member handling failed: {e}")

async def resolvechannels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    unresolved = []
    for ch in FORCE_CHANNELS:
        if ch['type'] == 'invite' and _resolve_invite_chat_id(ch['value']) is None:
            unresolved.append(ch['value'])
    if not unresolved:
        await update.message.reply_text("✅ All private channel links are already resolved.")
        return
    context.user_data["awaiting_resolve_forward"] = True
    txt = "🔧 <b>Resolve Private Channels</b>\n\nThese invite links are not resolved yet:\n"
    for i, link in enumerate(unresolved, 1):
        txt += f"{i}. <code>{link}</code>\n"
    txt += ("\n<b>Steps:</b>\n1️⃣ Make sure the bot is ADMIN in the private channel.\n2️⃣ Forward ANY message from that channel to me.\n3️⃣ Then send the invite link (from the list) it belongs to.\n\nSend /cancel to abort.")
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)

async def handle_resolve_channel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.user_data.get("awaiting_resolve_forward"):
        return
    msg = update.message
    origin = getattr(msg, 'forward_origin', None)
    if origin is not None and getattr(origin, 'chat', None) is not None:
        chat = origin.chat
        context.user_data["resolve_pending_chat"] = {"chat_id": chat.id, "title": chat.title or ""}
        await msg.reply_text(f"📌 Got channel: <b>{chat.title or chat.id}</b> (<code>{chat.id}</code>)\n\nNow send the invite link from the list that belongs to this channel.", parse_mode=ParseMode.HTML)
        raise ApplicationHandlerStop()
    text = (msg.text or "").strip()
    pending = context.user_data.get("resolve_pending_chat")
    if pending and re.match(r'^https?://t\.me/(\+|joinchat/)', text):
        _store_invite_chat_id(text, pending["chat_id"], pending.get("title", ""))
        _FORCE_CHAT_CACHE.pop(f"invite:{text}", None)
        context.user_data.pop("resolve_pending_chat", None)
        context.user_data["awaiting_resolve_forward"] = False
        await msg.reply_text(f"✅ Resolved! <code>{text}</code> → <code>{pending['chat_id']}</code>\nForce-join for this private channel now works.", parse_mode=ParseMode.HTML)
        raise ApplicationHandlerStop()

async def toggle_getbot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    current = get_getbot_enabled()
    set_getbot_enabled(not current)
    await update.message.reply_text(f"✅ 'Get Bot Like This' is now <b>{'ENABLED' if not current else 'DISABLED'}</b>.", parse_mode=ParseMode.HTML)

async def toggleprem_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: toggle visibility of WANT TO BE PREMIUM USER button."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    current = get_premium_button_enabled()
    set_premium_button_enabled(not current)
    await update.message.reply_text(
        f"✅ Premium button is now <b>{'ENABLED' if not current else 'DISABLED'}</b>.\n"
        f"Users {'will' if not current else 'will not'} see “WANT TO BE PREMIUM USER?”.",
        parse_mode=ParseMode.HTML
    )

# ============ SHOW HOME ============
async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_id = user.id

    track_user_bg(user.id, user.username, user.first_name, user.last_name)
    
    total_users = get_user_count()
    username_display = f"@{user.username}" if user.username else "No username"
    has_sub, sub_type, expiry, used, max_movies = has_active_subscription(user.id)
    
    items = []
    items.append({"label": f"👋 ʜɪ ᴡᴇʟᴄᴏᴍᴇ, {user.first_name}!", "value": ""})
    items.append({"label": f"👤 {username_display}", "value": ""})
    items.append({"label": f"👥 User: {total_users}", "value": ""})
    items.append({"label": "", "value": ""})
    
    if has_sub:
        items.append({"label": f"🎉 Active: {sub_type.title()} Pass", "value": ""})
        if expiry:
            items.append({"label": f"📅 Expires: {expiry.strftime('%Y-%m-%d')}", "value": ""})
        # Show used movies but NOT the limit
        if used > 0:
            items.append({"label": f"🎬 Movies used: {used}", "value": ""})
        items.append({"label": "", "value": ""})
        items.append({"label": "✅️ No limit for getting movies!", "value": ""})
    else:
        plans = get_subscription_plans()
        items.append({"label": "🚀 Get movies without delay cheap!", "value": ""})
        items.append({"label": "", "value": ""})
        items.append({"label": f"💳 Single: {get_default_price()} KES", "value": ""})
        if get_subscription_enabled():
            items.append({"label": f"🌼 Weekly: {plans['weekly']['price']} KES", "value": ""})
            items.append({"label": f"🌴 2-Week: {plans['2week']['price']} KES", "value": ""})
            items.append({"label": f"☔ 3-Week: {plans['3week']['price']} KES", "value": ""})
            items.append({"label": f"🔥 Monthly: {plans['monthly']['price']} KES", "value": ""})
    
    welcome_text = render_menu_for_user(user_id, MENU_TITLE, items)
    welcome_text = add_footer(welcome_text)
    welcome_text += get_datetime_footer()
    # Premium wrap already applied in add_footer/render — do NOT await network refresh here (keeps /start fast)
    welcome_text = apply_premium_emojis(welcome_text)

    genre_rows = []
    if get_genre_buttons_enabled():
        for row in get_active_genre_rows():
            btn_row = []
            for genre in row:
                emoji = GENRE_EMOJI.get(genre, "🎬")
                # compact: name then emoji; full: emoji then name (matches screenshots)
                if get_genre_style() == "full":
                    label = f"{emoji} {genre}"
                else:
                    label = f"{genre} {emoji}"
                btn_row.append(InlineKeyboardButton(label, callback_data=f"genre_{genre}"))
            genre_rows.append(btn_row)

    keyboard = [
        [InlineKeyboardButton("ꜱᴇᴀʀᴄʜ ᴀʟʟ ᴍᴏᴠɪᴇꜱ 🔍", callback_data="search_movies"),
         InlineKeyboardButton("ʙʀᴏᴡꜱᴇ ᴀʟʟ ᴍᴏᴠɪᴇꜱ 🎬", callback_data="watch_all_movies_0")],
        [InlineKeyboardButton("ʙʀᴏᴡꜱᴇ ᴍᴏᴠɪᴇ ᴄᴏʟʟᴇᴄᴛɪᴏɴꜱ 📦", callback_data="browse_bundles_0"),
         InlineKeyboardButton("ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ ♻️", callback_data="more_movies")],
    ]
    if get_cart_enabled():
        keyboard.append([InlineKeyboardButton("ᴍʏ ᴄᴀʀᴛ 🛒", callback_data="cart_view")])
    keyboard.extend(genre_rows)

    second_block = []
    if get_subscription_enabled():
        second_block.append(InlineKeyboardButton("ɢᴇᴛ ꜱᴜʙꜱᴄʀɪᴘᴛɪᴏɴ 🔹", callback_data="show_plans"))
    if get_getbot_enabled():
        second_block.append(InlineKeyboardButton("ɢᴇᴛ ʙᴏᴛ ʟɪᴋᴇ ᴛʜɪꜱ 🤖", callback_data="getbot_cmd"))
    second_block.append(InlineKeyboardButton("ʜᴏᴡ ᴛʜɪꜱ ʙᴏᴛ ᴡᴏʀᴋꜱ 📖", callback_data="how_bot_works"))
    if MY_CHANNEL:
        channel_url = f"https://t.me/{MY_CHANNEL.lstrip('@')}" if not MY_CHANNEL.startswith('http') else MY_CHANNEL
        second_block.append(InlineKeyboardButton("ᴠɪꜱɪᴛ ᴏᴜʀ ᴄʜᴀɴɴᴇʟ 🌍", url=channel_url))
    if ADMIN_USERNAME:
        admin_url = f"https://t.me/{ADMIN_USERNAME.lstrip('@')}" if not ADMIN_USERNAME.startswith('http') else ADMIN_USERNAME
        second_block.append(InlineKeyboardButton("ᴀᴅᴍɪɴ 👨‍💼", url=admin_url))
    if ADDITIONAL_CHANNELS:
        second_block.append(InlineKeyboardButton("ᴀᴅᴅ ꜰʀᴏᴍ ᴄʜᴀɴɴᴇʟ ➕", callback_data="visit_channels"))
    for i in range(0, len(second_block), 2):
        keyboard.append(second_block[i:i+2])
    
    welcome_kb = InlineKeyboardMarkup(keyboard)

    if edit:
        query = update.callback_query
        if query:
            try:
                if query.message and query.message.photo:
                    await query.edit_message_caption(caption=welcome_text, reply_markup=welcome_kb, parse_mode=ParseMode.HTML)
                else:
                    await query.edit_message_text(text=welcome_text, reply_markup=welcome_kb, parse_mode=ParseMode.HTML)
                push_nav_state(context, "start", welcome_text, welcome_kb)
                return
            except Exception as e:
                logger.warning(f"Failed to edit home message: {e}")
        try:
            await send_html_safe(context.bot, chat_id, welcome_text, reply_markup=welcome_kb)
            push_nav_state(context, "start", welcome_text, welcome_kb)
            return
        except Exception as e:
            logger.error(f"Failed to send home fallback: {e}")
            return

    try:
        if START_IMAGE_URL:
            try:
                await send_html_safe(context.bot, chat_id, welcome_text, reply_markup=welcome_kb, photo=START_IMAGE_URL)
            except Exception as e:
                logger.warning(f"Failed to send start image: {e}")
                await send_html_safe(context.bot, chat_id, welcome_text, reply_markup=welcome_kb)
        else:
            await send_html_safe(context.bot, chat_id, welcome_text, reply_markup=welcome_kb)
    except Exception as e:
        logger.error(f"show_home send failed: {e}")
        try:
            await context.bot.send_message(chat_id=chat_id, text=strip_premium_emojis(welcome_text)[:4000], reply_markup=welcome_kb)
        except Exception as e2:
            logger.error(f"show_home ultimate fallback failed: {e2}")
    push_nav_state(context, "start", welcome_text, welcome_kb)

# ============ START COMMAND ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Premium / emoji validation in background — never block the first reply (ms response)
    async def _bg_premium():
        try:
            await refresh_admin_premium_status(context.bot)
            if _VALIDATED_EMOJI_IDS is None:
                try:
                    await validate_premium_emoji_ids(context.bot)
                except Exception:
                    pass
        except Exception:
            pass
    asyncio.create_task(_bg_premium())
    user = update.effective_user
    args = context.args
    asyncio.create_task(asyncio.to_thread(track_user, user.id, user.username, user.first_name, user.last_name))
    logger.info(f"/start from user {user.id}, args: {args}")

    is_token_start = bool(args) and not args[0].startswith(('pay_', 'sub_')) and args[0] != 'search'

    if FORCE_CHANNELS and not is_token_start:
        all_joined, missing = await _check_force_membership(context, user.id)
        if not all_joined:
            join_text, join_kb = await _build_force_join_message(context, user.id, missing)
            await context.bot.send_message(chat_id=update.effective_chat.id, text=add_footer(join_text), reply_markup=join_kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            context.user_data["force_join_pending"] = True
            return

    if user.id in ADMIN_IDS and context.user_data.get("pending_reply_to"):
        context.user_data.pop("pending_reply_to", None)

    if args:
        token = args[0]
        logger.info(f"Processing token: {token} for user {user.id}")

        if token.startswith('pay_'):
            reference = token.replace('pay_', '')
            await check_payment_status(update, context, reference)
            return
        if token.startswith('sub_'):
            plan = token.replace('sub_', '')
            await handle_subscription_payment(update, context, plan)
            return
        if token == 'search':
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(f"❌ Cancel", callback_data="cancel_search")]])
            search_text = add_footer(f"""🔍 <b>Search Movies</b>

Type the movie name or Genre you want to search for.
Example: <code>Horror</code> or <code>Furious</code>

ℹ️ I'll search all movies in our library and show you results with instant download links!""")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=search_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            push_nav_state(context, "search_prompt", search_text, keyboard)
            context.user_data["awaiting_search"] = True
            return

        if not mongodb_connected or movies_collection is None:
            maint_text = add_footer(f"⚠️ Bot is in maintenance mode. Please try again later.")
            maint_kb = build_back_keyboard("back")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=maint_text, reply_markup=maint_kb, parse_mode=ParseMode.HTML)
            return

        series = None
        cached = cache_get_token(token)
        if cached and cached[0] == 'series':
            series = cached[1]
        elif series_bundles_collection is not None:
            try:
                series = series_bundles_collection.find_one({"token": token})
                if series:
                    cache_set_token(token, series, 'series')
            except Exception as e: logger.error(f"Series lookup error: {e}")
        if series:
            logger.info(f"Token matched bundle: {series['title']}")
            has_sub, sub_type, expiry, used, max_movies = has_active_subscription(user.id)
            if has_sub:
                await forward_series(update, context, series, is_subscriber=True)
                return
            existing_payment = None
            try:
                existing_payment = payments_collection.find_one({"user_id": user.id, "series_token": token, "status": "success"}) if payments_collection is not None else None
            except Exception as e: logger.error(f"Payment lookup error: {e}")
            if existing_payment:
                await forward_series(update, context, series)
                return

            price = series.get('price', get_default_price())
            ep_count = series.get('episode_count', len(series.get('episodes', [])))
            plans = get_subscription_plans()
            ref = f"sr_{secrets.token_hex(4)}"
            if payments_collection is not None:
                _pay_doc = {"reference": ref, "user_id": user.id, "series_token": token, "chat_id": update.effective_chat.id, "amount": price, "status": "pending", "created_at": datetime.utcnow(), "payment_method": None, "auto_deliver": True, "is_subscription": False}
                asyncio.create_task(asyncio.to_thread(payments_collection.insert_one, _pay_doc))

            # Build short movie title list for album caption — deduped, numbered 1. 2. 3.
            _eps_inside = dedupe_movie_items(series.get('episodes', []) or [])
            title_bits = []
            for _i, _ep in enumerate(_eps_inside[:12], 1):
                _et = (_ep.get('button_title') or _ep.get('title') or "").strip()
                if (not _et) or re.fullmatch(r"\d+", str(_et)):
                    _tok = _ep.get("token")
                    if _tok and movies_collection is not None:
                        try:
                            _m = movies_collection.find_one({"token": _tok}, {"title": 1, "button_title": 1, "poster_url": 1})
                            if _m:
                                _et = (_m.get("button_title") or _m.get("title") or "").strip()
                        except Exception:
                            pass
                if (not _et) or re.fullmatch(r"\d+", str(_et)):
                    _et = f"Part {_i}"
                title_bits.append(f"{len(title_bits)+1}. {clean_movie_title_for_button(_et, max_len=36)}")
            titles_line = ", ".join(title_bits)
            if len(_eps_inside) > 12:
                titles_line += f" +{len(_eps_inside)-12} more"

            # --- Album of posters first (GET ALL from channel) ---
            # Collect BOTH file_id and URL per episode; resolve_safe_photo validates each
            # so a dead URL or stale file_id can never become a blank white album image.
            poster_pairs = []  # (file_id, url)
            for _ep in _eps_inside:
                pfid = (_ep.get('poster_file_id') or _ep.get('poster_tg_file_id') or '').strip()
                pu = (_ep.get('poster_url') or '').strip()
                _tok = _ep.get('token')
                if (not pfid or not pu) and _tok and movies_collection is not None:
                    try:
                        _m = movies_collection.find_one({"token": _tok}, {"poster_url": 1, "poster_file_id": 1, "poster_tg_file_id": 1})
                        if _m:
                            pfid = pfid or (_m.get('poster_file_id') or _m.get('poster_tg_file_id') or '').strip()
                            pu = pu or (_m.get('poster_url') or '').strip()
                    except Exception:
                        pass
                if isinstance(pu, str) and pu.startswith('/uploads/'):
                    _base = (SERVER_UPLOAD_URL_BASE or WEBHOOK_URL or '').strip().rstrip('/')
                    if _base:
                        pu = f"{_base}{pu}"
                pair = (pfid, pu)
                if (pfid or pu) and pair not in poster_pairs:
                    poster_pairs.append(pair)
            if not poster_pairs:
                sfid = (series.get('poster_file_id') or series.get('poster_tg_file_id') or '').strip()
                spu = (series.get('poster_url') or '').strip()
                if sfid or spu:
                    poster_pairs = [(sfid, spu)]

            chat_id = update.effective_chat.id
            album_caption = apply_premium_emojis(
                f"<b>{escape_html(str(series.get('title') or 'Collection'))}</b>\n"
                f"{escape_html(titles_line)}\n\n"
                f"📦 {len(_eps_inside)} movies · <b>{price} KES</b>"
            )
            resolved_photos = []
            for _fid, _url in poster_pairs:
                try:
                    _p = await resolve_safe_photo(context.bot, _fid, _url, context="bundle_album")
                except Exception:
                    _p = None
                if _p is not None:
                    resolved_photos.append(_p)
            if resolved_photos:
                # ALL posters — album chunks at 10; failures fall back per-photo, never blank
                await send_album_chunked(
                    context.bot, chat_id, resolved_photos,
                    caption=album_caption[:1020], parse_mode=ParseMode.HTML,
                )
                logger.info(f"Bundle album sent: {len(resolved_photos)} posters for token {token}")
            else:
                # No posters — still show a short caption message
                try:
                    await context.bot.send_message(chat_id=chat_id, text=album_caption, parse_mode=ParseMode.HTML)
                except Exception:
                    pass

            # Minimal payment message — NO long BUNDLE DETAILS block
            payment_text = apply_premium_emojis(f"💳 <b>Pay {price} KES</b> to receive all {ep_count} movies\n\nChoose payment method:") + get_datetime_footer()
            auth_url = None
            keyboard = build_payment_keyboard(auth_url, ref, price, user.id, plans, include_subs=True)
            if get_cart_enabled():
                keyboard = add_cart_button_to_keyboard(keyboard, token)
            sent_msg = await context.bot.send_message(chat_id=chat_id, text=payment_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

            async def _attach_bundle_card_url(message, _ref=ref, _price=price, _token=token, _chat_id=update.effective_chat.id, _plans=plans):
                try:
                    url, _ = await run_in_thread(initialize_paystack_payment, email=f"user{user.id}@telegram.bot", amount_kes=_price, reference=_ref, metadata={"user_id": user.id, "series_token": _token, "chat_id": _chat_id, "auto_deliver": True})
                except Exception:
                    url = None
                if not url:
                    return
                try:
                    new_kb = build_payment_keyboard(url, _ref, _price, user.id, _plans)
                    if get_cart_enabled():
                        new_kb = add_cart_button_to_keyboard(new_kb, _token)
                    await message.edit_reply_markup(reply_markup=new_kb)
                except Exception as e:
                    logger.debug(f"Bundle card URL attach skipped: {e}")

            if get_card_payment_setting():
                asyncio.create_task(_attach_bundle_card_url(sent_msg))
            push_nav_state(context, "bundle_payment", payment_text, keyboard)
            return

        movie = None
        cached = cache_get_token(token)
        if cached and cached[0] == 'movie':
            movie = cached[1]
        else:
            try:
                movie = movies_collection.find_one({"token": token})
                if not movie and token.isdigit():
                    try:
                        movie = movies_collection.find_one({"token": int(token)})
                    except Exception:
                        movie = None
                if not movie:
                    cleaned = token.strip()
                    if cleaned != token:
                        movie = movies_collection.find_one({"token": cleaned})
                if movie:
                    cache_set_token(token, movie, 'movie')
            except Exception as e: logger.error(f"Movie lookup error: {e}")
        if not movie:
            invalid_text = add_footer(f"❌ Invalid or expired movie link.")
            invalid_kb = build_back_keyboard("back")
            await context.bot.send_message(chat_id=update.effective_chat.id, text=invalid_text, reply_markup=invalid_kb, parse_mode=ParseMode.HTML)
            return

        has_sub, sub_type, expiry, used, max_movies = has_active_subscription(user.id)
        if has_sub:
            await forward_movie(update, context, movie, is_subscriber=True)
            return
        existing_payment = None
        try: existing_payment = user_owns_movie(user.id, token) or (payments_collection.find_one({"user_id": user.id, "movie_token": token, "status": "success"}) if payments_collection is not None else None)
        except Exception as e: logger.error(f"Payment lookup error: {e}")
        if existing_payment or movie.get('price', 0) == 0:
            await forward_movie(update, context, movie)
            return

        price = movie.get('price', get_default_price())
        plans = get_subscription_plans()
        poster_url = movie.get("poster_url", "")

        items = []
        items.append({"label": "🎬 Title:", "value": clean_movie_title_for_button(movie['title'])})
        desc = movie.get('description', '')
        if desc: items.append({"label": "📝 Desc:", "value": desc[:40]})
        items.append({"label": "💳 Price:", "value": f"{price} KES"})
        items.append({"label": "🏷️ Category:", "value": movie.get('category', 'movie').title()})

        caption = render_menu_for_user(user.id, "🎦 𝐌𝐨𝐯𝐢𝐞 𝐈𝐧𝐟𝐨𝐫𝐦𝐚𝐭𝐢𝐨𝐧", items)
        caption = add_footer(caption)
        caption += apply_premium_emojis(f"\n\n💳 Choose payment method:") + get_datetime_footer()

        ref = f"mv_{secrets.token_hex(4)}"
        asyncio.create_task(asyncio.to_thread(track_pending_payment, user.id, ref))
        if payments_collection is not None:
            _pay_doc = {"reference": ref, "user_id": user.id, "movie_token": token, "chat_id": update.effective_chat.id, "amount": price, "status": "pending", "created_at": datetime.utcnow(), "payment_method": None, "auto_deliver": True, "is_subscription": False}
            asyncio.create_task(asyncio.to_thread(payments_collection.insert_one, _pay_doc))

        async def _attach_card_url(message, _ref=ref, _price=price, _token=token, _chat_id=update.effective_chat.id, _plans=plans):
            try:
                url, _ = await run_in_thread(initialize_paystack_payment, email=f"user{user.id}@telegram.bot", amount_kes=_price, reference=_ref, metadata={"user_id": user.id, "movie_token": _token, "chat_id": _chat_id, "auto_deliver": True})
            except Exception:
                url = None
            if not url:
                return
            try:
                new_kb = build_payment_keyboard(url, _ref, _price, user.id, _plans, include_subs=False)
                if get_cart_enabled():
                    new_kb = add_cart_button_to_keyboard(new_kb, _token)
                if message.photo:
                    await message.edit_reply_markup(reply_markup=new_kb)
                else:
                    await message.edit_reply_markup(reply_markup=new_kb)
            except Exception as e:
                logger.debug(f"Card URL attach skipped: {e}")

        auth_url = None
        keyboard = build_payment_keyboard(auth_url, ref, price, user.id, plans, include_subs=False)
        if get_cart_enabled(): keyboard = add_cart_button_to_keyboard(keyboard, token)
        reply_markup = keyboard
        chat_id = update.effective_chat.id

        sent_msg = None
        if poster_url:
            try:
                if is_base64_data_url(poster_url): photo_input = data_url_to_inputfile(poster_url)
                else: photo_input = poster_url
                sent_msg = await send_html_safe(context.bot, chat_id, caption, reply_markup=reply_markup, photo=photo_input)
            except Exception as e:
                logger.error(f"Failed to send poster photo: {e}")
        if sent_msg is None:
            try:
                sent_msg = await send_html_safe(context.bot, chat_id, caption, reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Failed to send movie caption: {e}")
                sent_msg = await context.bot.send_message(chat_id=chat_id, text=strip_premium_emojis(caption or ""), reply_markup=reply_markup)
        if get_card_payment_setting():
            asyncio.create_task(_attach_card_url(sent_msg))
        push_nav_state(context, "movie_payment", caption, reply_markup)
        return

    await show_home(update, context)

# ============ OTHER COMMAND HANDLERS ============
async def handle_subscription_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, plan: str):
    user = update.effective_user
    plans = get_subscription_plans()
    plan_info = plans.get(plan)
    if not plan_info:
        invalid_text = add_footer(f"❌ Invalid subscription plan.")
        invalid_kb = build_back_keyboard("back")
        await context.bot.send_message(chat_id=update.effective_chat.id, text=invalid_text, reply_markup=invalid_kb, parse_mode=ParseMode.HTML)
        push_nav_state(context, "invalid_plan", invalid_text, invalid_kb)
        return
    ref = f"sub_{secrets.token_hex(4)}"
    track_pending_payment(user.id, ref)
    if payments_collection is not None:
        payments_collection.insert_one({"reference": ref, "user_id": user.id, "plan": plan, "chat_id": update.effective_chat.id, "amount": plan_info["price"], "status": "pending", "created_at": datetime.utcnow(), "type": "subscription", "is_subscription": True})
    
    items = []
    items.append({"label": f"🎟️ {plan_info['name']}", "value": ""})
    items.append({"label": f"📅 Duration:", "value": f"{plan_info['days']} days"})
    items.append({"label": f"💰 Price:", "value": f"{plan_info['price']} KES"})
    
    payment_text = render_menu_for_user(user.id, "🎟️ SUBSCRIPTION", items)
    payment_text = add_footer(payment_text)
    payment_text += apply_premium_emojis(f"\n\n🔥 Get unlimited access to ALL movies!\n💳 Choose payment method:") + get_datetime_footer()
    
    auth_url, _ = initialize_paystack_payment(email=f"user{user.id}@telegram.bot", amount_kes=plan_info["price"], reference=ref, metadata={"user_id": user.id, "plan": plan, "chat_id": update.effective_chat.id, "is_subscription": True}, plan=plan)
    keyboard = build_payment_keyboard(auth_url, ref, plan_info["price"], user.id, plans)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=payment_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    push_nav_state(context, "sub_payment", payment_text, keyboard)

async def check_payment_status(update: Update, context: ContextTypes.DEFAULT_TYPE, reference: str = None):
    user = update.effective_user
    if not reference:
        if payments_collection is None:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=add_footer(f"⚠️ Bot is in maintenance mode."), reply_markup=build_back_keyboard("back"))
            return
        payment = payments_collection.find_one({"user_id": user.id, "status": "pending"}, sort=[("created_at", DESCENDING)])
        if not payment:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=add_footer(f"No pending payments found."), reply_markup=build_back_keyboard("back"))
            return
        reference = payment['reference']
    verify = verify_paystack_payment(reference)
    # Safely access verify data to avoid KeyError
    if verify and verify.get('status') and verify.get('data', {}).get('status') == 'success':
        clear_pending_payment_tracker(user.id)
        if payments_collection is not None:
            payments_collection.update_one({"reference": reference}, {"$set": {"status": "success", "paid_at": datetime.utcnow(), "payment_method": "card"}})
        payment = payments_collection.find_one({"reference": reference}) if payments_collection is not None else None
        if payment is not None and (payment.get('type') == 'subscription' or payment.get('is_subscription')):
            plan = payment.get('plan', 'weekly')
            create_subscription(user.id, plan, reference)
            await notify_subscription_activated(user.id, plan, context)
        else:
            movie_token = payment.get('movie_token') if payment is not None else None
            series_token = payment.get('series_token') if payment is not None else None
            cart_tokens = payment.get('cart_tokens', []) if payment is not None else []
            if cart_tokens:
                cart_items_for_delivery = [{"token": t} for t in cart_tokens]
                asyncio.create_task(auto_deliver_cart(user.id, cart_items_for_delivery))
            elif series_token:
                asyncio.create_task(auto_deliver_series(user.id, series_token, context))
            elif movie_token:
                asyncio.create_task(auto_deliver_movie(user.id, movie_token, context))
            else:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=add_footer(f"Payment confirmed! Content will be delivered."), reply_markup=build_back_keyboard("back"))
    else:
        status = verify.get('data', {}).get('status') if verify else 'unknown'
        await context.bot.send_message(chat_id=update.effective_chat.id, text=add_footer(f"""⏳ Payment status: <b>{status}</b>

ℹ️ If you completed payment, please wait a moment.
Your movie will be delivered automatically.

👨‍💼 Need help? Contact admin"""), reply_markup=build_back_keyboard("back"), parse_mode=ParseMode.HTML)

# ============ SEARCH FUNCTIONS ============
async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_bg(user.id, user.username, user.first_name, user.last_name)
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ CANCEL", callback_data="cancel_search")]])
        items = [
            {"label": "Type movie name or genre", "value": ""},
            {"label": "Example", "value": "Horror / Furious"},
            {"label": "", "value": ""},
            {"label": "🔎 I'll search the full library", "value": ""},
        ]
        search_text = add_footer(render_menu_for_user(user.id, "🔍 SEARCH MOVIES", items))
        await context.bot.send_message(chat_id=update.effective_chat.id, text=search_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        push_nav_state(context, "search_prompt", search_text, keyboard)
        context.user_data["awaiting_search"] = True
        return
    context.user_data["awaiting_search"] = False
    await perform_search(update, context, query, page=0)

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, page: int = 0):
    user_id = update.effective_user.id
    context.user_data["last_search_query"] = query
    mode = get_display_mode()
    ITEMS_PER_PAGE = 20 if mode == "button" else 9
    search_msg_text = add_footer(f"🔍 Searching for <b>\"{escape_html(query)}\"</b>...")
    if update.message:
        search_msg = await update.message.reply_text(search_msg_text, parse_mode=ParseMode.HTML)
    elif update.callback_query:
        query_obj = update.callback_query
        if query_obj.message and query_obj.message.photo:
            try:
                await query_obj.message.delete()
            except Exception:
                pass
            search_msg = await context.bot.send_message(chat_id=user_id, text=search_msg_text, parse_mode=ParseMode.HTML)
        else:
            try:
                search_msg = query_obj.message
                await search_msg.edit_text(search_msg_text, parse_mode=ParseMode.HTML)
            except Exception:
                search_msg = await context.bot.send_message(chat_id=user_id, text=search_msg_text, parse_mode=ParseMode.HTML)
    if query.lower() == "bundlemovies":
        await show_bundles_page(update, context, page)
        return
    # Run blocking PyMongo in a thread so one search never freezes the whole bot
    movies, total = await asyncio.to_thread(search_movies_db, query, ITEMS_PER_PAGE, page * ITEMS_PER_PAGE)
    if not movies:
        if page > 0:
            await perform_search(update, context, query, page=0)
            return
        ch_hint = ""
        if ADDITIONAL_CHANNELS:
            for ch in ADDITIONAL_CHANNELS[:3]:
                url = ch.get("url") if isinstance(ch, dict) else str(ch)
                name = ch.get("name", "Channel") if isinstance(ch, dict) else "Channel"
                if url:
                    ch_hint += f"\n🎬 <a href=\"{url}\">{escape_html(str(name))}</a>"
        if not ch_hint and MY_CHANNEL:
            ch_hint = f"\n🎬 https://t.me/{str(MY_CHANNEL).lstrip('@')}"
        no_results_text = add_footer(f"""😢 <b>No results found for \"{escape_html(query)}\"</b>

ℹ️ Try a different search term or browse our channels:{ch_hint or ''}

❓ Contact admin for requests""")
        nr_rows = []
        if ADDITIONAL_CHANNELS:
            nr_rows.append([InlineKeyboardButton("➕ Add from channels", callback_data="visit_channels")])
        if MY_CHANNEL:
            cu = f"https://t.me/{str(MY_CHANNEL).lstrip('@')}" if not str(MY_CHANNEL).startswith("http") else str(MY_CHANNEL)
            nr_rows.append([InlineKeyboardButton("🌍 Visit our channel", url=cu)])
        nr_rows.append([InlineKeyboardButton("🔍 Search again", callback_data="search_prompt")])
        nr_rows.append([InlineKeyboardButton("◀ Back", callback_data="back")])
        no_results_kb = InlineKeyboardMarkup(nr_rows)
        if update.message:
            await search_msg.edit_text(no_results_text, reply_markup=no_results_kb, parse_mode=ParseMode.HTML)
        else:
            try:
                await search_msg.edit_text(no_results_text, reply_markup=no_results_kb, parse_mode=ParseMode.HTML)
            except Exception:
                await context.bot.send_message(chat_id=user_id, text=no_results_text, reply_markup=no_results_kb, parse_mode=ParseMode.HTML)
        push_nav_state(context, "search_results_empty", no_results_text, no_results_kb)
        return
    try:
        await search_msg.delete()
    except Exception:
        pass
    try:
        await send_poster_grid(update, context, movies, page, total, query, is_search=True)
    except Exception as e:
        logger.error(f"send_poster_grid after search failed: {e}")
        logger.error(traceback.format_exc())
        # Fallback: send simple text list so search never looks silent/broken
        lines = [f"{i+1}. {escape_html(m.get('title','Movie'))}" for i, m in enumerate(movies[:20])]
        fb = add_footer(f"🔍 <b>Results for \"{escape_html(query)}\"</b> ({total})\n\n" + "\n".join(lines))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(clean_movie_title_for_button(m.get('title','Movie'))[:28], callback_data=f"get_movie_{m.get('token','')}")]
            for m in movies[:10] if m.get('token')
        ] + [[InlineKeyboardButton("◀ Back", callback_data="back")]])
        try:
            await context.bot.send_message(chat_id=user_id, text=fb, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e2:
            logger.error(f"search fallback also failed: {e2}")

# ============ KEYWORD AUTO-REPLIES (chat-admin quick help) ============
_KEYWORD_REPLIES = [
    (("i need help", "help me", "need help"), "🆘 <b>Need help?</b>\n\nNo worries — I'm here for you! Tap the button below to chat directly with the admin, or type /help to see what I can do."),
    (("admin", "confronter"), "👨‍💼 <b>Looking for the admin?</b>\n\nTap the button below to chat with admin directly — you'll get help fast!"),
    # {BOT_NAME} replaced at send time from BOT_DISPLAY_NAME / MENU_TITLE (.env) — never hardcode brand
    (("hello", "hi", "hey", "habari", "mambo"), "👋 <b>Hello there!</b>\n\nWelcome to <b>{BOT_NAME}</b> 🍿\nSearch any movie with /search or browse from the menu. Need a human? Chat the admin below!"),
    (("bot",), "🤖 <b>Yes, I'm a bot!</b>\n\nI deliver movies instantly after payment. Type /start to open the menu, or chat the admin below if you need anything."),
]

async def handle_keyword_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user is None or update.effective_user.id in ADMIN_IDS:
        return
    if context.user_data.get("awaiting_search"):
        return
    text = (update.message.text or "").strip().lower()
    if not text or len(text) > 60:
        return
    matched = None
    for terms, reply in _KEYWORD_REPLIES:
        for term in terms:
            if text == term or text.startswith(term + " ") or text.endswith(" " + term) or f" {term} " in f" {text} ":
                matched = reply
                break
        if matched:
            break
    if not matched:
        return
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass
    await asyncio.sleep(BOT_MIRROR_DELAY)
    admin_url = f"https://t.me/{ADMIN_USERNAME.lstrip('@')}" if ADMIN_USERNAME else "https://t.me/confronter"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📩 ᴘʀᴇꜱꜱ ʜᴇʀᴇ — ᴄʜᴀᴛ ᴀᴅᴍɪɴ", callback_data="chat_admin")], [InlineKeyboardButton("🏠 ʜᴏᴍᴇ", callback_data="go_home")]])
    display = (BOT_DISPLAY_NAME or MENU_TITLE or os.environ.get("BOT_USERNAME", "") or "Movie Bot").strip() or "Movie Bot"
    matched = (matched or "").replace("{BOT_NAME}", display).replace("Confronter Movies", display)
    await update.message.reply_text(add_footer(matched), reply_markup=kb, parse_mode=ParseMode.HTML)

async def chat_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["chat_with_admin"] = True
    context.user_data["awaiting_search"] = False
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass
    await asyncio.sleep(BOT_MIRROR_DELAY)
    text = add_footer("💬 <b>Chat with Admin</b>\n\nType your message below and send it — the admin will receive it instantly and reply right here.\n\nSend /cancel or tap 🏠 Home to leave the chat.")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 ʜᴏᴍᴇ", callback_data="go_home")]])
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def handle_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Channel posts / non-private updates have no update.message — ignore
    if not update.message or not update.effective_user:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith('/'):
        return

    # Skip to page number
    if context.user_data.get("awaiting_skip_page"):
        context.user_data["awaiting_skip_page"] = False
        try:
            page_num = int(text)
            if page_num < 1:
                page_num = 1
            await show_movies_page(update, context, page_num - 1)
        except ValueError:
            await reply_html_safe(update.message, add_footer("❌ Please send a valid page number (e.g. 5)."),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="back")]]))
        return

    if not context.user_data.get("awaiting_search"):
        return
    # Clear flag FIRST so other handlers don't race
    context.user_data["awaiting_search"] = False
    context.user_data.pop("phone_input_handled", None)
    try:
        await perform_search(update, context, text, page=0)
    except Exception as e:
        logger.error(f"Search failed for '{text}': {e}")
        logger.error(traceback.format_exc())
        try:
            await reply_html_safe(update.message, add_footer(f"❌ Search error. Please try again with /search"),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Search", callback_data="search_prompt")], [InlineKeyboardButton("🏠 Home", callback_data="go_home")]]))
        except Exception:
            pass

async def cancel_search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["awaiting_search"] = False
    cancel_text = add_footer(f"❌ Search cancelled. Use /search anytime to find movies!")
    cancel_kb = build_back_keyboard("back")
    await query.edit_message_text(cancel_text, reply_markup=cancel_kb)
    push_nav_state(context, "search_cancelled", cancel_text, cancel_kb)

# ============ ERROR HANDLER ============
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error: {context.error}")
    logger.error(traceback.format_exc())

# ============ ADMIN COMMANDS ============

async def setdefaultstyle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: set the global default menu style. Usage: /setdefaultstyle confronter09"""
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args or []
    if not args:
        # List styles
        lines = ["🎨 <b>Available Menu Styles</b>\n"]
        current = get_default_menu_style()
        for key, info in MENU_STYLES.items():
            mark = " ✅" if key == current else ""
            lines.append(f"• <code>{key}</code> — {info.get('name', key)}{mark}")
        lines.append("\nUsage: <code>/setdefaultstyle confronter09</code>")
        lines.append("Or pick a button below:")
        kb_rows = []
        row = []
        for key in MENU_STYLES:
            row.append(InlineKeyboardButton(key.replace("confronter", "C"), callback_data=f"setstyle_{key}"))
            if len(row) == 3:
                kb_rows.append(row)
                row = []
        if row:
            kb_rows.append(row)
        kb_rows.append([InlineKeyboardButton("◀️ Back", callback_data="back")])
        await update.message.reply_text(
            add_footer("\n".join(lines)),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode=ParseMode.HTML
        )
        return
    style_key = args[0].strip().lower()
    if style_key not in MENU_STYLES:
        await update.message.reply_text(add_footer(f"❌ Unknown style <code>{style_key}</code>\nUse /setdefaultstyle to list styles."), parse_mode=ParseMode.HTML)
        return
    ok = set_default_menu_style(style_key)
    name = MENU_STYLES[style_key].get("name", style_key)
    if ok:
        await update.message.reply_text(add_footer(f"✅ Default menu style set to:\n🎨 <b>{name}</b> (<code>{style_key}</code>)"), parse_mode=ParseMode.HTML)
    else:
        await reply_html_safe(update.message, add_footer("❌ Failed to save style (DB offline?)"))

async def setmenustyle_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User: set personal menu style. Usage: /setmenustyle confronter05"""
    user_id = update.effective_user.id
    args = context.args or []
    if not args:
        lines = ["🎨 <b>Your Menu Styles</b>\n"]
        current = get_user_menu_style(user_id)
        for key, info in MENU_STYLES.items():
            mark = " ✅" if key == current else ""
            lines.append(f"• <code>{key}</code> — {info.get('name', key)}{mark}")
        lines.append("\nUsage: <code>/setmenustyle confronter09</code>")
        await update.message.reply_text(add_footer("\n".join(lines)), parse_mode=ParseMode.HTML)
        return
    style_key = args[0].strip().lower()
    if style_key not in MENU_STYLES:
        await update.message.reply_text(add_footer(f"❌ Unknown style. Use /setmenustyle to list."), parse_mode=ParseMode.HTML)
        return
    ok = set_user_menu_style(user_id, style_key)
    name = MENU_STYLES[style_key].get("name", style_key)
    if ok:
        await update.message.reply_text(add_footer(f"✅ Your menu style is now:\n🎨 <b>{name}</b>"), parse_mode=ParseMode.HTML)
    else:
        await reply_html_safe(update.message, add_footer("❌ Could not save your style preference."))

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not mongodb_connected:
        await reply_html_safe(update.message, add_footer(f"⚠️ Stats unavailable — database offline"))
        return

    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    next_month = month_start.replace(day=28) + timedelta(days=4)
    month_end = next_month - timedelta(days=next_month.day)

    total_movies = movies_collection.count_documents({})
    total_bundles = series_bundles_collection.count_documents({}) if series_bundles_collection is not None else 0
    total_sales = payments_collection.count_documents({"status": "success"})
    revenue = 0
    for p in payments_collection.find({"status": "success"}):
        try:
            revenue += float(p.get("amount") or 0)
        except (TypeError, ValueError):
            pass
    revenue = int(revenue)
    total_users = get_user_count()

    new_users_this_month = users_collection.count_documents({"joined_at": {"$gte": month_start, "$lt": month_end}})
    active_this_month = users_collection.count_documents({"last_activity": {"$gte": month_start, "$lt": month_end}})

    mpesa_sales = payments_collection.count_documents({"status": "success", "payment_method": "mpesa"})
    card_sales = payments_collection.count_documents({"status": "success", "payment_method": "card"})
    payhero_sales = payments_collection.count_documents({"status": "success", "payment_method": "payhero"})
    bundle_sales = payments_collection.count_documents({"status": "success", "series_token": {"$exists": True}})
    active_subs = subscriptions_collection.count_documents({"status": "active", "expires_at": {"$gt": datetime.utcnow()}})
    pending_msgs = messages_collection.count_documents({"admin_replied": False}) if messages_collection is not None else 0

    card_status = f"✅ ON" if get_card_payment_setting() else f"❌ OFF"
    sub_status = f"✅ ON" if get_subscription_enabled() else f"❌ OFF"
    payhero_status = f"✅ ON ({get_payhero_channel_name()})" if get_payhero_enabled() else f"❌ OFF"
    cart_status = f"✅ ON" if get_cart_enabled() else f"❌ OFF"
    display_mode = get_display_mode()
    display_name = "Button Style 2 (Detailed)" if display_mode == 'button2' else "Button Style 1 (Compact)" if display_mode == 'button1' else "Poster Style"
    genre_status = f"✅ ON" if get_genre_buttons_enabled() else f"❌ OFF"
    default_style = get_default_menu_style()
    default_style_name = MENU_STYLES.get(default_style, {}).get("name", "Confronter 1")

    pending_trackers = pending_payment_tracker.count_documents({}) if pending_payment_tracker is not None else 0
    pending_users = pending_payment_tracker.distinct("user_id") if pending_payment_tracker is not None else []

    text = add_footer(f"""📊 <b>Admin Dashboard</b> 🚀

👥 <b>Users</b>
   • Total Users: {total_users}
   • New this month: {new_users_this_month}
   • Active this month: {active_this_month}

🎬 <b>Content</b>
   • Movies: {total_movies}
   • Bundles: {total_bundles}

💰 <b>Sales</b>
   • Total sales: {total_sales}
   • Revenue: {revenue} KES
   • Bundle sales: {bundle_sales}
   • M-Pesa (Paystack): {mpesa_sales}
   • M-Pesa (PayHero): {payhero_sales}
   • Card/Bank: {card_sales}

👑 <b>Subscriptions</b>
   • Active subscriptions: {active_subs}

📨 <b>Messages</b>
   • Pending replies: {pending_msgs}

⚙️ <b>Settings</b>
   • Default price: {get_default_price()} KES
   • Card payments: {card_status}
   • Subscriptions: {sub_status}
   • PayHero: {payhero_status}
   • Shopping cart: {cart_status}
   • Display mode: {display_name}
   • Genre buttons: {genre_status}
   • Default menu style: {default_style_name} ({default_style})

🚨 <b>Pending payments</b>
   • Trackers: {pending_trackers}
   • Users with pending: {len(pending_users)}

{get_datetime_footer()}""")

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def monthlyusers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not mongodb_connected:
        await update.message.reply_text("⚠️ Database offline.")
        return
    
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    next_month = month_start.replace(day=28) + timedelta(days=4)
    month_end = next_month - timedelta(days=next_month.day)
    
    total_users = get_user_count()
    active_this_month = users_collection.count_documents({"last_activity": {"$gte": month_start, "$lt": month_end}})
    new_this_month = users_collection.count_documents({"joined_at": {"$gte": month_start, "$lt": month_end}})
    
    text = f"📊 <b>Monthly Statistics</b>\n━━━━━━━━━━━━━━━━━━━━━━\n👥 <b>Total users:</b> {total_users:,}\n📈 <b>Monthly active:</b> {active_this_month:,}\n🆕 <b>New this month:</b> {new_this_month:,}\n━━━━━━━━━━━━━━━━━━━━━━\n📅 {now.strftime('%B %Y')}"
    
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def updatedescription_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return
    await update.message.reply_text("🔄 Updating bot description...")
    await update_bot_description()
    await update.message.reply_text("✅ Bot description updated with latest monthly stats!")

async def update_bot_description(context: ContextTypes.DEFAULT_TYPE = None):
    if not mongodb_connected:
        return
    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)
    next_month = month_start.replace(day=28) + timedelta(days=4)
    month_end = next_month - timedelta(days=next_month.day)
    
    total_users = get_user_count()
    active_this_month = users_collection.count_documents({"last_activity": {"$gte": month_start, "$lt": month_end}})
    new_this_month = users_collection.count_documents({"joined_at": {"$gte": month_start, "$lt": month_end}})
    
    description = f"🎬 ᴄᴏɴꜰʀᴏɴᴛᴇʀ ᴍᴏᴠɪᴇ ʙᴏᴛ\n━━━━━━━━━━━━━━━━━━━━━━\n👥 {total_users:,} total users\n📈 {active_this_month:,} monthly active\n🆕 {new_this_month:,} new this month\n━━━━━━━━━━━━━━━━━━━━━━\n🍿 Search & watch movies instantly.\n👑 Subscribe for unlimited access.\n🛒 Buy 5, get 1 free!"
    try:
        await application.bot.set_my_description(description)
        short_desc = f"{active_this_month:,} monthly users 🎬🍿"
        await application.bot.set_my_short_description(short_desc)
        logger.info("Bot description updated with monthly stats")
    except Exception as e:
        logger.error(f"Failed to update bot description: {e}")

# ============ ADMIN PREMIUM COMMANDS ============
async def addpremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /addpremium <user_id> [duration_days] [max_movies]")
        return
    target_id = int(args[0])
    duration_days = int(args[1]) if len(args) > 1 else 30
    max_movies = int(args[2]) if len(args) > 2 else 0
    expiry = datetime.utcnow() + timedelta(days=duration_days)
    update_data = {"premium": {"expiry": expiry, "max_movies": max_movies, "used_movies": 0, "active": True}}
    users_collection.update_one({"user_id": target_id}, {"$set": update_data}, upsert=True)
    await update.message.reply_text(f"✅ Premium added for user {target_id} for {duration_days} days, max movies {max_movies}.")

async def setduration_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /setduration <user_id> <duration_days>")
        return
    target_id = int(args[0])
    duration_days = int(args[1])
    user = users_collection.find_one({"user_id": target_id})
    if not user or not user.get("premium", {}).get("active"):
        await update.message.reply_text("User does not have an active premium.")
        return
    new_expiry = datetime.utcnow() + timedelta(days=duration_days)
    users_collection.update_one({"user_id": target_id, "premium.active": True}, {"$set": {"premium.expiry": new_expiry}})
    await update.message.reply_text(f"✅ Premium duration updated to {duration_days} days for user {target_id}.")

async def setlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /setlimit <user_id> <max_movies>")
        return
    target_id = int(args[0])
    max_movies = int(args[1])
    user = users_collection.find_one({"user_id": target_id})
    if not user or not user.get("premium", {}).get("active"):
        await update.message.reply_text("User does not have an active premium.")
        return
    users_collection.update_one({"user_id": target_id, "premium.active": True}, {"$set": {"premium.max_movies": max_movies}})
    await update.message.reply_text(f"✅ Premium movie limit set to {max_movies} for user {target_id}.")

async def removepremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /removepremium <user_id>")
        return
    target_id = int(args[0])
    users_collection.update_one({"user_id": target_id}, {"$set": {"premium.active": False}})
    await update.message.reply_text(f"✅ Premium removed (deactivated) for user {target_id}.")

async def revoke_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await removepremium_cmd(update, context)

async def endsubscription_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /endsubscription <user_id>")
        return
    target_id = int(args[0])
    subscriptions_collection.update_one({"user_id": target_id, "status": "active"}, {"$set": {"status": "ended_by_admin"}})
    await update.message.reply_text(f"✅ Subscription for user {target_id} has been ended (expired).")

async def request_prem_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    await query.answer("✅ Request sent to admin")
    text = f"🌟 <b>User requests permanent premium:</b>\nID: <code>{user.id}</code>\nUsername: @{user.username or 'N/A'}\nName: {user.first_name or ''}\nMessage: I want to be permanent premium user."
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Failed to send to admin {admin_id}: {e}")
    await edit_or_send_message(query, add_footer("✅ <b>Your request has been sent to the admin.</b>\n\nYou will receive a response shortly.\n\n⚠️ <i>If you are not serious, you may get banned. Be careful.</i>"), reply_markup=build_back_keyboard("back"), parse_mode=ParseMode.HTML)

# ============ GET BOT (simplified) ============
async def getbot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if IS_CHILD_BOT:
        await update.message.reply_text("This command is not available on your bots.")
        return
    if not get_getbot_enabled():
        dev_url = "https://t.me/confronter"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 ᴄʜᴀᴛ ᴅᴇᴠᴇʟᴏᴘᴇʀ", url=dev_url)], [InlineKeyboardButton("🚀 ʀᴇᴀᴄʜ ᴅᴇᴠ ꜰᴏʀ ꜱᴇᴛᴜᴘ", url=dev_url)], [InlineKeyboardButton("🏠 ʜᴏᴍᴇ", callback_data="go_home")]])
        text = add_footer("🤖 <b>Get a bot like this</b>\n\nReach the developer to set up your own movie bot:")
        if update.message:
            await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        elif update.callback_query:
            await edit_or_send_message(update.callback_query, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    initial = BOT_INITIAL_PRICE
    if settings_collection is not None:
        setting = settings_collection.find_one({"key": "bot_initial_price"})
        if setting:
            initial = setting['value']

    text = add_footer(f"""🤖 <b>Get Your Own Movie Bot!</b>

🚀 Start selling movies with your own independent Telegram bot!

<b>How it works:</b>
1️⃣ Pay the one-time setup fee of <b>{initial} KES</b>.
2️⃣ Create a bot with @BotFather and get the token.
3️⃣ Send the token to admin — we connect everything.
4️⃣ Your users buy movies from YOUR bot.
5️⃣ You receive the money from your customers.

<b>What you get:</b>
• Fully independent bot
• Your own /start, /search, /cart, subscriptions
• Full movie library access
• Set your own prices
• All earnings go directly to you

<i>After payment, send your bot token to admin to go live.</i>""")

    dev = DEVELOPER_CONTACT if 'DEVELOPER_CONTACT' in globals() else 'confronter'
    keyboard = [
        [InlineKeyboardButton("SUBSCRIBE NOW 💳", callback_data="bot_subscribe")],
        [InlineKeyboardButton("MORE INFO ❓", callback_data="bot_info")],
        [InlineKeyboardButton("💬 Reach Developer", url=f"https://t.me/{dev}")],
        [InlineKeyboardButton("BACK ◀", callback_data="back")]
    ]
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    elif update.callback_query:
        await edit_or_send_message(update.callback_query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def show_getbot_screen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await getbot_cmd(update, context)

async def handle_bot_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "bot_subscribe":
        initial = BOT_INITIAL_PRICE
        if settings_collection is not None:
            setting = settings_collection.find_one({"key": "bot_initial_price"})
            if setting:
                initial = setting['value']
        ref = f"bot_init_{secrets.token_hex(4)}"
        track_pending_payment(user_id, ref)
        if payments_collection is not None:
            payments_collection.insert_one({"reference": ref, "user_id": user_id, "type": "bot_initial", "amount": initial, "status": "pending", "created_at": datetime.utcnow(), "chat_id": query.message.chat.id})
        plans = get_subscription_plans()
        auth_url = None
        try:
            auth_url, _ = initialize_paystack_payment(email=f"user{user_id}@telegram.bot", amount_kes=initial, reference=ref, metadata={"user_id": user_id, "type": "bot_initial"})
        except Exception:
            pass
        keyboard = build_payment_keyboard(auth_url, ref, initial, user_id, plans)
        text = add_footer(f"💳 <b>Bot Subscription</b>\n\n💰 Amount: <b>{initial} KES</b>\n\nChoose payment method:")
        await edit_or_send_message(query, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    elif data == "bot_info":
        text = add_footer(f"""🤖 <b>More About Your Own Bot</b>

After subscription, you will provide your bot token.
Your bot will have all the features you need.

<b>What you get:</b>
• Your own bot with /start, /search, /cart, etc.
• Full movie database access
• Set your own pricing for movies and subscriptions
• All payments go to you directly
• Independent bot — not connected to this main bot

<b>What you need:</b>
• A Telegram bot token (get from @BotFather)

<b>Monthly fee:</b> {BOT_MONTHLY_PRICE} KES
<b>Initial setup:</b> {BOT_INITIAL_PRICE} KES

Contact @{DEVELOPER_CONTACT} for assistance.""")
        keyboard = [
            [InlineKeyboardButton("💳 ꜱᴜʙꜱᴄʀɪʙᴇ", callback_data="bot_subscribe")],
            [InlineKeyboardButton("💬 Reach Developer", url=f"https://t.me/{DEVELOPER_CONTACT}")],
            [InlineKeyboardButton("◀ ʙᴀᴄᴋ", callback_data="back")],
        ]
        await edit_or_send_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

    elif data == "bot_retry":
        if bot_requests_collection is not None:
            existing = bot_requests_collection.find_one({"user_id": user_id})
            if existing and existing.get("status") == "pending_token":
                await query.edit_message_text(add_footer(f"✍️ Please send your bot token via /add_bot_token <token>"))
            else:
                await query.edit_message_text("❌ No pending bot registration.")
        else:
            await query.edit_message_text("❌ Database error.")

async def add_bot_token_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /add_bot_token <your_bot_token>")
        return
    token = args[0]
    if bot_requests_collection is None:
        await update.message.reply_text("❌ Database not available.")
        return
    payment = payments_collection.find_one({"user_id": user_id, "type": "bot_initial", "status": "success"})
    if not payment:
        await update.message.reply_text("❌ You need to subscribe first. Use /getbot and pay the initial fee.")
        return
    bot_requests_collection.update_one({"user_id": user_id}, {"$set": {"bot_token": token, "status": "active", "activated_at": datetime.utcnow(), "initial_payment_ref": payment.get("reference")}}, upsert=True)
    await application.bot.send_message(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0, text=add_footer(f"🤖 New bot token received from user {user_id}\nToken: <code>{token}</code>"), parse_mode=ParseMode.HTML)
    await update.message.reply_text(f"✅ Bot token saved! Your your bot is now active.\nUse /mybot to manage it.")

async def mybot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if bot_requests_collection is None:
        await update.message.reply_text("❌ Database not available.")
        return
    bot_doc = bot_requests_collection.find_one({"user_id": user_id})
    if not bot_doc:
        await update.message.reply_text("❌ You don't have a your bot. Use /getbot to subscribe.")
        return
    status = bot_doc.get("status", "inactive")
    token = bot_doc.get("bot_token", "Not set")
    activated = bot_doc.get("activated_at")
    if activated:
        activated = activated.strftime("%Y-%m-%d %H:%M")
    else:
        activated = "N/A"

    text = add_footer(f"""🤖 <b>ʏᴏᴜʀ ᴏᴡɴ ʙᴏᴛ</b>

Status: <b>{status}</b>
Token: <code>{token}</code>
Activated: {activated}

Your bot will be set up manually by admin. Please contact admin for any changes.""")
    keyboard = [[InlineKeyboardButton("♻ ʀᴇꜰʀᴇꜱʜ", callback_data="mybot_refresh")], [InlineKeyboardButton("◀ ʙᴀᴄᴋ", callback_data="back")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def handle_mybot_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if bot_requests_collection is None:
        await query.edit_message_text("❌ Database error.")
        return
    bot_doc = bot_requests_collection.find_one({"user_id": user_id})
    if not bot_doc:
        await query.edit_message_text("❌ Bot not found.")
        return
    status = bot_doc.get("status", "inactive")
    token = bot_doc.get("bot_token", "Not set")
    activated = bot_doc.get("activated_at")
    if activated:
        activated = activated.strftime("%Y-%m-%d %H:%M")
    else:
        activated = "N/A"
    text = add_footer(f"""🤖 <b>ʏᴏᴜʀ ᴏᴡɴ ʙᴏᴛ</b>

Status: <b>{status}</b>
Token: <code>{token}</code>
Activated: {activated}""")
    await query.edit_message_text(text, parse_mode=ParseMode.HTML)

# Admin your bot commands (simple manual)
async def list_bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if bot_requests_collection is None:
        await update.message.reply_text("❌ Database not available.")
        return
    bots = list(bot_requests_collection.find().sort("activated_at", DESCENDING).limit(50))
    if not bots:
        await update.message.reply_text("🤖 No bot requests yet.")
        return
    text = "🤖 <b>Bot Requests</b>\n\n"
    for b in bots:
        status_emoji = {"active": "✅", "inactive": "❌", "pending_token": "⏳"}.get(b.get("status", "inactive"), "❓")
        text += f"{status_emoji} User: <code>{b['user_id']}</code>\n   Token: <code>{b.get('bot_token', 'N/A')}</code>\n   Activated: {b.get('activated_at', '').strftime('%Y-%m-%d') if b.get('activated_at') else 'N/A'}\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def delete_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /delete_bot <user_id>")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if bot_requests_collection is None:
        await update.message.reply_text("❌ Database not available.")
        return
    result = bot_requests_collection.delete_one({"user_id": target_id})
    if result.deleted_count:
        await update.message.reply_text(f"✅ Bot request for user {target_id} deleted.")
    else:
        await update.message.reply_text("❌ Bot not found.")

# ============ MASTER MOVIES (admin list and delete) ============
async def mastermovies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg is None:
        return
    if update.effective_user.id not in ADMIN_IDS:
        await msg.reply_text("❌ Admin only.")
        return
    page = 0
    if context.args and context.args[0].isdigit():
        page = int(context.args[0]) - 1
    if page < 0:
        page = 0
    context.user_data["master_current_page"] = page
    per_page = 20
    total = movies_collection.count_documents({})
    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if page >= total_pages:
        page = total_pages - 1
    movies = list(movies_collection.find().sort("created_at", DESCENDING).skip(page * per_page).limit(per_page))
    if not movies:
        await msg.reply_text("❌ No movies in database.")
        return

    if "mastermovies_selected" not in context.user_data:
        context.user_data["mastermovies_selected"] = set()

    selected = context.user_data["mastermovies_selected"]
    text = f"📦 <b>Master Movie List</b> (Page {page+1}/{total_pages} — {total} total)\n\nTap a movie to select it (then Delete / Edit), or tap 🔗 to copy its token link.\n\n"
    for i, m in enumerate(movies, 1):
        token = m['token']
        title = clean_movie_title_for_button(m.get('title', 'Unknown'))
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        status = "✅" if token in selected else "⬜"
        text += f"{status} {i+page*per_page}. <b>{title}</b>\n   <code>{link}</code>\n"

    keyboard = []
    context.user_data["master_page_tokens"] = [m['token'] for m in movies]
    row = []
    for i, m in enumerate(movies):
        token = m['token']
        mark = "✅" if token in selected else f"{i+1+page*per_page}"
        row.append(InlineKeyboardButton(mark, callback_data=f"mst_{i}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ ᴘʀᴇᴠ", callback_data=f"master_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ ɴᴇxᴛ", callback_data=f"master_page_{page+1}"))
    if nav:
        keyboard.append(nav)

    if selected:
        keyboard.append([InlineKeyboardButton(f"🗑️ Delete Selected ({len(selected)})", callback_data="master_delete"), InlineKeyboardButton("✏️ Edit Selected", callback_data="master_edit")])
    keyboard.append([InlineKeyboardButton("❌ Clear Selection", callback_data="master_clear"), InlineKeyboardButton("🏠 Home", callback_data="go_home")])

    await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)

async def master_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tokens = context.user_data.get("master_page_tokens", [])
    try:
        idx = int(query.data.replace("mst_", ""))
        token = tokens[idx]
    except (ValueError, IndexError):
        await query.answer("Expired list — run /mastermovies again.", show_alert=True)
        return
    selected = context.user_data.get("mastermovies_selected", set())
    if token in selected:
        selected.remove(token)
    else:
        selected.add(token)
    context.user_data["mastermovies_selected"] = selected
    context.args = [str(context.user_data.get("master_current_page", 0) + 1)]
    await mastermovies_cmd(update, context)

async def master_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Selection cleared")
    context.user_data["mastermovies_selected"] = set()
    context.args = [str(context.user_data.get("master_current_page", 0) + 1)]
    await mastermovies_cmd(update, context)

async def master_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    selected = context.user_data.get("mastermovies_selected", set())
    if not selected:
        await query.answer("No movies selected.", show_alert=True)
        return
    context.user_data["master_editing"] = True
    lines = []
    for t in list(selected)[:20]:
        lines.append(f"<code>{t} : title = New Title Here</code>")
        lines.append(f"<code>{t} : price = 20</code>")
    await query.message.reply_text(add_footer("✏️ <b>Edit movies</b>\n\nSend one line per change:\n<code>TOKEN : title = New Title</code>\n<code>TOKEN : price = 25</code>\n\nExamples for your selection:\n" + "\n".join(lines[:6]) + "\n\nAfter saving you'll get the fresh working start link.\nUse <code>/cancel</code> to abort."), parse_mode=ParseMode.HTML)

async def handle_master_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.user_data.get("master_editing"):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith('/'):
        return
    updated, failed = 0, 0
    result_lines = []
    for line in text.split('\n'):
        line = line.strip()
        if not line or ':' not in line:
            continue
        tok, spec = [p.strip() for p in line.split(':', 1)]
        if not tok or not spec:
            continue
        movie = movies_collection.find_one({"token": tok})
        if not movie and tok.isdigit():
            try:
                movie = movies_collection.find_one({"token": int(tok)})
            except Exception:
                movie = None
        if not movie:
            failed += 1
            result_lines.append(f"❌ <code>{escape_html(tok)}</code> — not found")
            continue
        real_tok = movie['token']
        m = re.match(r'(?i)\s*(title|price)\s*=\s*(.+)$', spec)
        update_doc = None
        if m:
            field, value = m.group(1).lower(), m.group(2).strip()
            if field == 'price':
                digits = re.sub(r'[^\d]', '', value)
                if not digits:
                    failed += 1
                    result_lines.append(f"❌ <code>{escape_html(str(real_tok))}</code> — invalid price")
                    continue
                update_doc = {"price": int(digits)}
            else:
                if not value:
                    failed += 1
                    continue
                update_doc = {"title": value}
        else:
            new_tok = spec
            if movies_collection.find_one({"token": new_tok}):
                failed += 1
                result_lines.append(f"❌ <code>{escape_html(new_tok)}</code> — token already exists")
                continue
            update_doc = {"token": new_tok}
        res = movies_collection.update_one({"_id": movie["_id"]}, {"$set": update_doc})
        if res.modified_count or res.matched_count:
            cache_invalidate_token(real_tok)
            final_tok = update_doc.get("token", real_tok)
            if "token" in update_doc:
                cache_invalidate_token(final_tok)
            fresh = movies_collection.find_one({"token": final_tok})
            link = f"https://t.me/{BOT_USERNAME}?start={final_tok}"
            title_now = (fresh or movie).get('title', '')
            price_now = (fresh or movie).get('price', get_default_price())
            result_lines.append(f"✅ <b>{escape_html(clean_movie_title_for_button(title_now))}</b> — {price_now} KES\n<code>{link}</code>")
            updated += 1
        else:
            failed += 1
            result_lines.append(f"❌ <code>{escape_html(str(real_tok))}</code> — save failed")
    context.user_data["master_editing"] = False
    context.user_data["mastermovies_selected"] = set()
    summary = f"✅ Updated {updated} movie(s). ❌ Failed {failed}."
    if result_lines:
        summary += "\n\n" + "\n".join(result_lines[:20])
    await update.message.reply_text(add_footer(summary), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    raise ApplicationHandlerStop()

async def master_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    token = query.data.replace("master_select_", "")
    selected = context.user_data.get("mastermovies_selected", set())
    if token in selected:
        selected.remove(token)
    else:
        selected.add(token)
    context.user_data["mastermovies_selected"] = selected
    await mastermovies_cmd(update, context)

async def master_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    selected = context.user_data.get("mastermovies_selected", set())
    if not selected:
        await query.edit_message_text("No movies selected.")
        return
    deleted = 0
    for token in selected:
        result = movies_collection.delete_one({"token": token})
        if result.deleted_count:
            cache_invalidate_token(token)
            deleted += 1
    context.user_data["mastermovies_selected"] = set()
    await query.edit_message_text(f"✅ Deleted {deleted} movies.")
    await mastermovies_cmd(update, context)

async def master_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    context.user_data["master_current_page"] = page
    context.args = [str(page+1)]
    await mastermovies_cmd(update, context)

# ============ COMMAND: /claimfree ============
async def claimfree_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    track_user_bg(user.id, user.username, user.first_name, user.last_name)
    
    user_doc = users_collection.find_one({"user_id": user_id})
    if not user_doc or not user_doc.get("free_available", False):
        await reply_html_safe(
            update.message,
            add_footer("❌ You don't have a free movie available. Purchase 5 movies to earn one."),
            reply_markup=build_back_keyboard("back"),
        )
        return

    purchased_tokens = get_user_purchased_tokens(user_id)
    if not purchased_tokens:
        await reply_html_safe(
            update.message,
            add_footer("❌ You haven't purchased any movies yet."),
            reply_markup=build_back_keyboard("back"),
        )
        return

    items = []
    items.append({"label": "🎁 CLAIM YOUR FREE MOVIE", "value": ""})
    items.append({"label": "Select one from your purchased movies:", "value": ""})
    items.append({"label": "", "value": ""})

    keyboard = []
    for token in purchased_tokens:
        movie = movies_collection.find_one({"token": token})
        if movie:
            title = clean_movie_title_for_button(movie.get('title', 'Unknown'))
            items.append({"label": f"▪︎ {title}", "value": ""})
            keyboard.append([InlineKeyboardButton(title, callback_data=f"claimfree_select_{token}")])

    if not keyboard:
        await reply_html_safe(
            update.message,
            add_footer("❌ No purchased movies found."),
            reply_markup=build_back_keyboard("back"),
        )
        return

    text = render_menu_for_user(user_id, "🎁 CLAIM FREE MOVIE", items)
    text = add_footer(text)
    text += "\n\nTap a movie to claim it for free. You can only claim one."

    kb = InlineKeyboardMarkup(keyboard)
    await reply_html_safe(update.message, text, reply_markup=kb)

# ============ COMMAND: /dev ============
async def togglewatched_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin only: toggle whether users see Watched/Remaining on /checksubscription."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    current = get_show_watched_remaining()
    set_show_watched_remaining(not current)
    state = "ON (users see watched/remaining)" if not current else "OFF (hidden from users)"
    await update.message.reply_text(f"✅ show_watched_remaining is now <b>{state}</b>", parse_mode=ParseMode.HTML)

async def dev_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dev = (DEVELOPER_CONTACT or "confronter").lstrip("@")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Reach Developer", url=f"https://t.me/{dev}")],
        [InlineKeyboardButton("🏠 Home", callback_data="go_home")],
    ])
    await update.message.reply_text(
        add_footer(f"""🤖 <b>Developer Info</b> 👑

Want to know the Real king 🤴 Behind this

I am @{dev}
Bring business dude! 🔥

🚀 <b>Confronter Tech Wizard</b>
🌍 Building the future, Don't be weak dude reach me."""),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )

# ============ COMMAND: /help ============
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = update.effective_user.id in ADMIN_IDS
    # Use bot display name from .env (BOT_DISPLAY_NAME / MENU_TITLE) — never hardcode "Movie Bot"
    display = (BOT_DISPLAY_NAME or MENU_TITLE or os.environ.get('BOT_USERNAME', '') or 'Bot').strip() or 'Bot'
    help_text = HELP_TEXT or ''
    help_text = help_text.replace('{BOT_DISPLAY_NAME}', display)
    help_text = help_text.replace('Movie Bot', display)
    # inject live admin handle from .env (never hardcode)
    help_text = help_text.replace('@confronter', admin_mention())
    help_text = help_text.replace('see ADMIN_USERNAME in .env', admin_mention())
    if admin_mention() not in help_text and 'contact admin' in help_text.lower():
        help_text += f"\n\nFor support contact admin: {admin_mention()}"
    help_text = add_footer(help_text)
    if "/ping" not in help_text:
        help_text += "\n🏓 /ping — Test if bot is active"
    if is_admin:
        help_text += add_footer(f"""

🔒 <b>Admin Commands:</b>
📷 /upload — Upload movies (poster + movie)
✅ /complete — Finalize upload batch
📦 /bulkupload — Bulk upload (titles + videos)
👑 /confronterking — Single poster + multiple videos
❌ /confronterkingcancel — Cancel Confronter King upload
✅ /confronterisundefeated — Finalize Confronter King upload
📬 /inbox — User messages
📊 /stats — Dashboard stats
👥 /users — List users
📢 /broadcast — Send broadcast
✅ /confirmbroadcast — Confirm broadcast
❌ /cancelbroadcast — Cancel broadcast
📅 /broadcaststats — Broadcast history
✏️ /reply &lt;user_id&gt; &lt;msg&gt; — Reply to user
❌ /cancel — Cancel reply mode
💰 /setprice &lt;plan&gt; &lt;amount&gt; — Set plan price
📊 /setmaxmovies &lt;plan&gt; &lt;max&gt; — Set plan movie limit
💳 /togglecard — Toggle card/bank payments
👑 /togglesubscription — Toggle subscriptions
📱 /togglepayhero — Toggle M-Pesa gateway
⚙️ /togglegenre — Toggle genre buttons on/off
⚙️ /togglegenre1 — Toggle genre button style
👁️ /togglelimit — Show/hide movie limit remaining to users
🧹 /clear_pending &lt;user_id&gt; — Clear pending tracker
📢 /addchannel — Add a channel (forward a message)
📋 /listchannels — List added channels
❌ /removechannel &lt;id&gt; — Remove a channel
📝 /createpost — Create a channel post
🔘 /buttons — Add URL buttons to current post
👁️ /preview — Preview current post
📤 /sendpost — Publish current post
❌ /cancelpost — Cancel current post
📦 /createbundle — Create a bundle from tokens
✅ /donebyconfronter — Complete bundle selection
❌ /cancelbundle — Cancel bundle creation
👑 /addpremium &lt;user_id&gt; [days] [max_movies] — Grant premium
📅 /setduration &lt;user_id&gt; &lt;days&gt; — Extend premium
📊 /setlimit &lt;user_id&gt; &lt;max_movies&gt; — Set premium limit
❌ /removepremium &lt;user_id&gt; — Remove premium
❌ /endsubscription &lt;user_id&gt; — End user's subscription
🤖 /getbot — Get your own movie bot
🤖 /add_bot_token &lt;token&gt; — Add your bot token
🤖 /mybot — Manage your bot
💰 /set_bot_price &lt;initial&gt; &lt;monthly&gt; — Set bot prices (admin)
📋 /list_bots — List all your bots (admin)
🔄 /restart_bot &lt;user_id&gt; — Restart a your bot (admin)
🗑️ /delete_bot &lt;user_id&gt; — Delete a your bot (admin)
🗂️ /mastermovies — Master list of all movies
🔧 /resolvechannels — Resolve private force-join invite links
🏓 /ping — Test if bot is active""")
    try:
        await refresh_admin_premium_status(context.bot)
    except Exception:
        pass
    help_text = apply_premium_emojis(help_text)
    await reply_html_safe(update.message, help_text)

# ============ MY MOVIES COMMAND ============
async def my_movies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_bg(user.id, user.username, user.first_name, user.last_name)
    await show_my_movies_page(update, context, 0)

async def myplanmovies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all movies the user can rewatch forever (purchased + plan watches). Show plan buttons."""
    user = update.effective_user
    user_id = user.id
    track_user_bg(user.id, user.username, user.first_name, user.last_name)
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass

    has_sub, plan, expiry, used, max_movies = has_active_subscription(user_id)
    tokens = get_user_purchased_tokens(user_id)

    items = []
    items.append({"label": "MY PLAN MOVIES", "value": ""})
    if has_sub:
        days_left = max(0, (expiry - datetime.utcnow()).days) if expiry else 0
        items.append({"label": "Plan", "value": f"{(plan or 'active').title()}"})
        items.append({"label": "Days left", "value": str(days_left)})
        items.append({"label": "Watched", "value": f"{used}/{max_movies if max_movies else '∞'}"})
        items.append({"label": "", "value": ""})
        items.append({"label": "Rewatch any movie below anytime — free while plan is active.", "value": ""})
    else:
        items.append({"label": "No active plan — purchased movies still rewatch free.", "value": ""})
    items.append({"label": "", "value": ""})

    keyboard = []
    shown = 0
    for token in tokens[:40]:
        movie = movies_collection.find_one({"token": token}) if movies_collection is not None else None
        title = None
        if movie:
            title = clean_movie_title_for_button(movie.get("title", token))
        else:
            # try accessed_movies title
            title = token[:28]
            if users_collection is not None:
                u = users_collection.find_one({"user_id": user_id}, {"accessed_movies": 1})
                if u:
                    for a in u.get("accessed_movies") or []:
                        if isinstance(a, dict) and a.get("token") == token:
                            title = clean_movie_title_for_button(a.get("title") or token)
                            break
        if not title:
            continue
        items.append({"label": f"• {title}", "value": ""})
        keyboard.append([InlineKeyboardButton(f"▶ {title[:30]}", callback_data=f"rewatch_{token}")])
        shown += 1

    if shown == 0:
        items.append({"label": "No movies yet. Buy or watch with a plan first.", "value": ""})

    # Plan buttons always shown
    plans = get_subscription_plans()
    plan_row = []
    if get_subscription_enabled():
        plan_row = [
            InlineKeyboardButton(f"Weekly {plans['weekly']['price']}KES", callback_data="buy_plan_weekly"),
            InlineKeyboardButton(f"Monthly {plans['monthly']['price']}KES", callback_data="buy_plan_monthly"),
        ]
    kb_rows = keyboard[:15]  # telegram limit practical
    if plan_row:
        kb_rows.append(plan_row)
    kb_rows.append([InlineKeyboardButton("🎬 Browse Movies", callback_data="watch_all_movies_0")])
    kb_rows.append([InlineKeyboardButton("◀ Back", callback_data="back")])

    text = render_menu_for_user(user_id, "🎬 MY PLAN MOVIES", items)
    text = add_footer(text)
    kb = InlineKeyboardMarkup(kb_rows)
    if query:
        try:
            await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def rewatch_movie_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str):
    """Deliver a previously accessed/purchased movie again (no charge)."""
    query = update.callback_query
    user_id = update.effective_user.id
    await query.answer("Loading…")
    tokens = set(get_user_purchased_tokens(user_id))
    has_sub, _, _, _, _ = has_active_subscription(user_id)
    if token not in tokens and not has_sub:
        await query.answer("Not in your library. Buy or use a plan.", show_alert=True)
        return
    # Subscribers can rewatch anything; library users rewatch owned
    movie = movies_collection.find_one({"token": token}) if movies_collection is not None else None
    if not movie:
        await query.answer("Movie not found", show_alert=True)
        return
    try:
        await auto_deliver_movie(user_id, token, context)
    except Exception as e:
        logger.error(f"rewatch failed: {e}")
        await context.bot.send_message(chat_id=user_id, text=add_footer("❌ Could not deliver. Try /start or contact admin."))


async def mystatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias → same styled output as /checksubscription"""
    await checksubscription_cmd(update, context)
    return
    # legacy path kept below (unreachable) for reference
    user = update.effective_user
    has_sub, sub_type, expiry, used, max_movies = has_active_subscription(user.id)
    if has_sub:
        text = f"""🎉 <b>Your Subscription</b> 🎟️

👑 Plan: <b>{sub_type.title()} Pass</b>
📅 Expires: {expiry.strftime('%Y-%m-%d %H:%M') if expiry else 'Never'}
{'🎬 Movies used: ' + str(used) if used > 0 else ''}

✅ You have unlimited access to all movies! 🍿"""
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🌴 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")],
            [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]
        ])
    else:
        plans = get_subscription_plans()
        text = f"""📊 <b>Your Status</b> 📊

❌ ɴᴏ ᴀᴄᴛɪᴠᴇ ꜱᴜʙꜱᴄʀɪᴘᴛɪᴏɴ

🔥 ɢᴇᴛ ᴜɴʟɪᴍɪᴛᴇᴅ ᴀᴄᴄᴇꜱꜱ:
🌱 ᴡᴇᴇᴋʟʏ ᴘᴀꜱꜱ: {plans['weekly']['price']} KES
🌼 2-ᴡᴇᴇᴋ ᴘᴀꜱꜱ: {plans['2week']['price']} KES
☔ 3-ᴡᴇᴇᴋ ᴘᴀꜱꜱ: {plans['3week']['price']} KES
⚡ ᴍᴏɴᴛʜʟʏ ᴘᴀꜱꜱ: {plans['monthly']['price']} KES

🚀 Use /start to browse movies! 🎬"""
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔥 ꜱᴜʙꜱᴄʀɪʙᴇ ɴᴏᴡ", callback_data="show_plans")],
            [InlineKeyboardButton(f"🌼 ᴍᴏʀᴇ ᴍᴏᴠɪᴇꜱ", callback_data="more_movies")],
            [InlineKeyboardButton(f"◀ ʙᴀᴄᴋ", callback_data="back")]
        ])
    text = add_footer(text)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

async def cancel_reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if context.user_data.get("pending_reply_to"):
        context.user_data.pop("pending_reply_to", None)
        await update.message.reply_text("✅ Reply mode cancelled.")
        return
    
    if context.user_data.get("upload_mode"):
        _clear_upload_state(user.id, context)
        await update.message.reply_text("❌ Upload cancelled.")
        return
    
    if context.user_data.get("bulk_upload"):
        _clear_bulk_upload_state(user.id, context)
        await update.message.reply_text("❌ Bulk upload cancelled.")
        return
    
    if context.user_data.get("bundle_session"):
        context.user_data.pop("bundle_session", None)
        await update.message.reply_text("❌ Bundle creation cancelled.")
        return
    
    if context.user_data.get("select_bundle_session"):
        context.user_data.pop("select_bundle_session", None)
        context.user_data.pop("select_bundle_mode", None)
        await update.message.reply_text("❌ Bundle selection cancelled.")
        return
    
    if context.user_data.get("confronter_upload_mode"):
        session = get_upload_session(user.id)
        if session:
            delete_upload_session(user.id)
        context.user_data.pop("confronter_upload_mode", None)
        context.user_data.pop("confronter_upload_admin", None)
        await update.message.reply_text("❌ Confronter upload cancelled.")
        return
    
    if context.user_data.get("add_channel_mode"):
        context.user_data.pop("add_channel_mode", None)
        await update.message.reply_text("❌ Channel addition cancelled.")
        return
    
    if context.user_data.get("awaiting_buttons"):
        context.user_data.pop("awaiting_buttons", None)
        await update.message.reply_text("❌ Button input cancelled.")
        return
    
    if context.user_data.get("post_draft"):
        context.user_data.pop("post_draft", None)
        await update.message.reply_text("❌ Post creation cancelled.")
        return
    
    await update.message.reply_text("ℹ️ No active operation to cancel.\n\nUse /help to see available commands.")

# ============ TOGGLE COMMANDS (admin) ============
async def toggle_display_mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    current = get_display_mode()
    new_mode = 'poster' if current == 'button' else 'button'
    set_display_mode(new_mode)
    if new_mode == 'button':
        display_name = "Button Style"
        desc = "Shows movie titles as buttons (20 per page). No posters."
    else:
        display_name = "Poster Style"
        desc = "Shows movie posters as album + action buttons."
    text = add_footer(f"""⚙️ <b>Display Mode Changed!</b>

Display mode: <b>{display_name}</b>

{desc}

Mode is now active for all users.""")
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data="back")]])
    push_nav_state(context, "toggle_display_admin", text, back_kb)
    await update.message.reply_text(text=text, reply_markup=back_kb, parse_mode=ParseMode.HTML)

async def toggle_genre_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: show/hide genre buttons on home."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    current = get_genre_buttons_enabled()
    new_val = not current
    set_genre_buttons_enabled(new_val)
    status = f"✅ ENABLED" if new_val else f"❌ DISABLED"
    await update.message.reply_text(add_footer(f"""⚙️ <b>Genre Buttons Toggle</b>

Status: {status}

Users will {'see' if new_val else 'not see'} the genre buttons on the home screen.
Style is: <b>{get_genre_style()}</b> (use /togglegenre1 to switch style)."""), parse_mode=ParseMode.HTML)

async def toggle_genre1_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: switch genre layout compact <-> full."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    cur = get_genre_style()
    nxt = "full" if cur == "compact" else "compact"
    set_genre_style(nxt)
    set_genre_buttons_enabled(True)
    await update.message.reply_text(add_footer(
        f"🎭 <b>Genre style</b> set to <b>{nxt}</b>.\n"
        f"compact = Dj afro / Dj smith grid\n"
        f"full = Horror / Action / Kihindi grid\n\n"
        f"Genre buttons are ON. Use /togglegenre to hide them."
    ), parse_mode=ParseMode.HTML)

async def togglecard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    current = get_card_payment_setting()
    new_val = not current
    set_card_payment_setting(new_val)
    status = f"✅ ENABLED" if new_val else f"❌ DISABLED"
    await reply_html_safe(update.message, add_footer(f"""💳 <b>Card/Bank Payment Toggle</b>

Status: {status}

Users will {'see' if new_val else 'not see'} the Card/Bank payment option.

You can also toggle this from the admin dashboard."""))

async def togglesubscription_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    current = get_subscription_enabled()
    new_val = not current
    set_subscription_enabled(new_val)
    status = f"✅ ENABLED" if new_val else f"❌ DISABLED"
    await reply_html_safe(update.message, add_footer(f"""👑 <b>Subscription Toggle</b>

Status: {status}

Users will {'see' if new_val else 'not see'} subscription plan options.

You can also toggle this from the admin dashboard."""))

async def togglepayhero_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cycle active M-Pesa gateway: PayHero → Paystack → Makamesco → PayHero ..."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    current = get_active_payment_gateway()
    order = ["payhero", "paystack", "makamesco"]
    try:
        idx = order.index(current)
        next_gw = order[(idx + 1) % len(order)]
    except ValueError:
        next_gw = "paystack"
    # Skip gateways that have no credentials
    for _ in range(3):
        if next_gw == "payhero" and not (PAYHERO_API_USERNAME and PAYHERO_API_PASSWORD):
            next_gw = "paystack"
            continue
        if next_gw == "makamesco" and not MAKAMESCO_SECRET_KEY:
            next_gw = "payhero" if (PAYHERO_API_USERNAME and PAYHERO_API_PASSWORD) else "paystack"
            continue
        break
    set_active_payment_gateway(next_gw)
    names = {
        "payhero": "PayHero",
        "paystack": "Paystack",
        "makamesco": "Makamesco (Nexus Pay)"
    }
    channel_info = ""
    if next_gw == "payhero":
        channel_info = f"\nChannel: <b>{get_payhero_channel_name()}</b>"
    elif next_gw == "makamesco":
        channel_info = ""  # never expose gateway URL to chat
    await update.message.reply_text(
        add_footer(f"""💳 <b>Payment Gateway Toggled</b>

Active M-Pesa gateway: <b>{names.get(next_gw, next_gw)}</b>{channel_info}

Cycle order: PayHero → Paystack → Makamesco → ...

Use /togglepayhero again to switch to the next one.
Card payments still go through Paystack."""),
        parse_mode=ParseMode.HTML,
    )

async def setpayherochannel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if not args:
        current_channel = get_payhero_channel_name()
        await reply_html_safe(update.message, add_footer(f"""📡 <b>Set PayHero Channel</b>

Current: <b>{current_channel}</b>

Usage:
<code>/setpayherochannel till</code> — Use Till (ID: 6909)
<code>/setpayherochannel bank</code> — Use Bank (ID: 6910)
<code>/setpayherochannel &lt;channel_id&gt;</code> — Use custom channel ID"""))
        return
    channel_arg = args[0].lower().strip()
    if channel_arg in ("till", "6909"):
        set_payhero_channel_id(6909)
        await reply_html_safe(update.message, add_footer(f"✅ PayHero channel set to <b>Till (6054766)</b>\n\nAll M-Pesa payments via PayHero will go to your Till number."))
    elif channel_arg in ("bank", "6910"):
        set_payhero_channel_id(6910)
        await reply_html_safe(update.message, add_footer(f"✅ PayHero channel set to <b>Bank (National Bank — 01521247318000)</b>\n\nAll M-Pesa payments via PayHero will go to your bank account."))
    elif channel_arg.isdigit():
        set_payhero_channel_id(int(channel_arg))
        await reply_html_safe(update.message, add_footer(f"✅ PayHero channel set to ID: <b>{channel_arg}</b>"))
    else:
        await reply_html_safe(update.message, add_footer(f"❌ Invalid channel. Use:\n<code>/setpayherochannel till</code> or <code>/setpayherochannel bank</code>"))

async def togglecart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    current = get_cart_enabled()
    new_val = not current
    set_cart_enabled(new_val)
    status = f"✅ ENABLED" if new_val else f"❌ DISABLED"
    await reply_html_safe(update.message, add_footer(f"""🛒 <b>Shopping Cart Toggle</b>

Status: {status}

Users will {'see' if new_val else 'not see'} the shopping cart feature.

When enabled, users can add multiple movies to cart and pay once."""))

# ============ RE-ENGAGEMENT (24H AUTO MESSAGE) ============
DEFAULT_REENGAGE_TEXT = (
    "Hi {name}! Am online and active — press /start to continue. "
    "I have new amazing films waiting for you 🎬"
)

def get_reengage_settings():
    """Return (enabled: bool, text: str, hours: float)."""
    enabled = False  # off by default until admin turns on
    msg = DEFAULT_REENGAGE_TEXT
    hours = 48.0  # 2 days default
    try:
        if mongodb_connected and settings_collection is not None:
            s = settings_collection.find_one({"key": "reengage_enabled"})
            if s is not None:
                enabled = bool(s.get("value", False))
            t = settings_collection.find_one({"key": "reengage_text"})
            if t and t.get("value"):
                msg = str(t["value"])
            h = settings_collection.find_one({"key": "reengage_hours"})
            if h is not None:
                try:
                    hours = float(h.get("value") or 48)
                except (TypeError, ValueError):
                    hours = 48.0
            if hours <= 0:
                hours = 48.0
    except Exception:
        pass
    return enabled, msg, hours

def set_reengage_enabled(enabled: bool):
    if mongodb_connected and settings_collection is not None:
        settings_collection.update_one(
            {"key": "reengage_enabled"},
            {"$set": {"value": bool(enabled), "updated_at": datetime.utcnow()}},
            upsert=True,
        )

def set_reengage_text(msg: str):
    if mongodb_connected and settings_collection is not None:
        settings_collection.update_one(
            {"key": "reengage_text"},
            {"$set": {"value": (msg or "").strip() or DEFAULT_REENGAGE_TEXT, "updated_at": datetime.utcnow()}},
            upsert=True,
        )

def set_reengage_hours(hours):
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        hours = 48.0
    if hours <= 0:
        hours = 48.0
    if mongodb_connected and settings_collection is not None:
        settings_collection.update_one(
            {"key": "reengage_hours"},
            {"$set": {"value": hours, "updated_at": datetime.utcnow()}},
            upsert=True,
        )

def format_reengage_message(template: str, user_doc: dict) -> str:
    """Replace {name} / {username} placeholders."""
    uname = (user_doc or {}).get("username") or ""
    first = (user_doc or {}).get("first_name") or ""
    last = (user_doc or {}).get("last_name") or ""
    display = first or (f"@{uname}" if uname else "friend")
    if first and last:
        display = f"{first} {last}".strip()
    elif uname and not first:
        display = f"@{uname}"
    text = template or DEFAULT_REENGAGE_TEXT
    text = text.replace("{name}", display)
    text = text.replace("{username}", f"@{uname}" if uname else display)
    text = text.replace("{first_name}", first or display)
    return text

async def togglereengage_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    enabled, _, hours = get_reengage_settings()
    set_reengage_enabled(not enabled)
    state = "ON ✅" if (not enabled) else "OFF ❌"
    await reply_html_safe(update.message, add_footer(f"🔁 Auto re-engage message is now {state}"))

async def setreengagetext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    txt = " ".join(context.args).strip() if context.args else ""
    if not txt:
        _, cur, hours = get_reengage_settings()
        await update.message.reply_text(f"Usage: /setreengagetext <message>\nCurrent: {cur}")
        return
    set_reengage_text(txt)
    await reply_html_safe(update.message, add_footer("✅ Re-engage auto-text updated."))

async def setreengagehours_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: set inactivity hours before auto message (e.g. 1, 2, 24, 48)."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        _, _, hours = get_reengage_settings()
        await update.message.reply_text(f"Usage: /setreengagehours <hours>\nCurrent: {hours}h")
        return
    try:
        h = float(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid hours. Example: /setreengagehours 48")
        return
    set_reengage_hours(h)
    await update.message.reply_text(
        add_footer(f"✅ Re-engage timer set to <b>{h}</b> hours."),
        parse_mode=ParseMode.HTML,
    )

# ============ BROADCAST SYSTEM ============
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text(add_footer(f"""📢 <b>Broadcast Command</b>

Usage:
<code>/broadcast Your message here</code>

Or reply to any message with:
<code>/broadcast</code>

Supports text, photos, videos, documents."""), parse_mode=ParseMode.HTML)
        return
    if update.message.reply_to_message:
        target_message = update.message.reply_to_message
        broadcast_text = target_message.text or target_message.caption or ""
    else:
        broadcast_text = " ".join(context.args)
        target_message = None
    if not broadcast_text and not target_message:
        await reply_html_safe(update.message, add_footer(f"❌ Please provide a message or reply to one."))
        return
    broadcast_doc = {"text": broadcast_text, "admin_id": ADMIN_ID, "status": "pending", "total_users": 0, "sent_count": 0, "failed_count": 0, "created_at": datetime.utcnow(), "started_at": None, "completed_at": None}
    if broadcasts_collection is not None:
        result = broadcasts_collection.insert_one(broadcast_doc)
        broadcast_id = str(result.inserted_id)
    else:
        broadcast_id = None
    preview_text = add_footer(f"""📢 <b>Broadcast Preview</b>

{broadcast_text}

<i>Send /confirmbroadcast to send to all users.</i>
<i>Send /cancelbroadcast to cancel.</i>""")
    await update.message.reply_text(preview_text, parse_mode=ParseMode.HTML)
    context.user_data["pending_broadcast"] = {"text": broadcast_text, "broadcast_id": broadcast_id, "has_media": target_message is not None and (target_message.photo or target_message.video or target_message.document), "media_message": target_message}

async def confirm_broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    pending = context.user_data.get("pending_broadcast")
    if not pending:
        await reply_html_safe(update.message, add_footer(f"❌ No pending broadcast. Use /broadcast first."))
        return
    users = get_all_users(skip_blocked=True)
    total = len(users)
    if total == 0:
        await reply_html_safe(update.message, add_footer(f"❌ No users to broadcast to."))
        return
    if pending.get("broadcast_id") and broadcasts_collection is not None:
        broadcasts_collection.update_one({"_id": ObjectId(pending["broadcast_id"])}, {"$set": {"status": "sending", "total_users": total, "started_at": datetime.utcnow()}})
    await reply_html_safe(update.message, add_footer(f"🚀 Starting broadcast to {total} users..."))
    sent = 0
    failed = 0
    batch_size = 25
    for i, user_doc in enumerate(users):
        user_id = user_doc["user_id"]
        try:
            if pending.get("has_media") and pending.get("media_message"):
                msg = pending["media_message"]
                if msg.photo:
                    photo = msg.photo[-1]
                    await application.bot.send_photo(chat_id=user_id, photo=photo.file_id, caption=pending["text"] or msg.caption, parse_mode=ParseMode.HTML)
                elif msg.video:
                    await application.bot.send_video(chat_id=user_id, video=msg.video.file_id, caption=pending["text"] or msg.caption, parse_mode=ParseMode.HTML)
                elif msg.document:
                    await application.bot.send_document(chat_id=user_id, document=msg.document.file_id, caption=pending["text"] or msg.caption, parse_mode=ParseMode.HTML)
            else:
                await application.bot.send_message(chat_id=user_id, text=pending['text'], parse_mode=ParseMode.HTML)
            sent += 1
            if (i + 1) % batch_size == 0:
                await asyncio.sleep(BOT_FAST_DELAY)
        except Exception as e:
            failed += 1
            logger.error(f"Broadcast to {user_id} failed: {e}")
            if "blocked" in str(e).lower() or "initiate" in str(e).lower():
                if users_collection is not None:
                    users_collection.update_one({"user_id": user_id}, {"$set": {"blocked": True, "blocked_at": datetime.utcnow()}})
            continue
    if pending.get("broadcast_id") and broadcasts_collection is not None:
        broadcasts_collection.update_one({"_id": ObjectId(pending["broadcast_id"])}, {"$set": {"status": "completed", "sent_count": sent, "failed_count": failed, "completed_at": datetime.utcnow()}})
    context.user_data.pop("pending_broadcast", None)
    await update.message.reply_text(add_footer(f"""✅ <b>Broadcast Complete!</b>

📤 Sent: {sent}
❌ Failed: {failed}
👥 Total: {total}"""), parse_mode=ParseMode.HTML)

async def cancel_broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data.pop("pending_broadcast", None)
    await reply_html_safe(update.message, add_footer(f"❌ Broadcast cancelled."))

async def broadcast_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if broadcasts_collection is None:
        await reply_html_safe(update.message, add_footer(f"⚠️ Database offline"))
        return
    broadcasts = list(broadcasts_collection.find().sort("created_at", DESCENDING).limit(10))
    if not broadcasts:
        await reply_html_safe(update.message, add_footer(f"📨 No broadcasts yet."))
        return
    text = add_footer(f"📢 <b>Broadcast History</b>\n\n")
    for b in broadcasts:
        status_emoji = {"completed": "✅", "sending": "⏳", "pending": "✏️", "failed": "❌"}.get(b.get("status", ""), "❓")
        text += f"{status_emoji} {b['text'][:50]}{'...' if len(b['text']) > 50 else ''}\n   📤 {b.get('sent_count', 0)}/{b.get('total_users', 0)} sent | 📅 {b['created_at'].strftime('%Y-%m-%d %H:%M')}\n\n"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ============ OTHER ADMIN COMMANDS ============
async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if users_collection is None:
        await reply_html_safe(update.message, add_footer(f"⚠️ Database offline"))
        return
    users = list(users_collection.find().sort("last_activity", DESCENDING).limit(20))
    if not users:
        await reply_html_safe(update.message, add_footer(f"👥 No users yet."))
        return
    text = add_footer(f"👥 <b>Recent Users</b>\n\n")
    for u in users:
        name = u.get('first_name', 'Unknown')
        username = f"@{u['username']}" if u.get('username') else "no username"
        blocked = f" 🔕" if u.get('blocked') else ""
        text += f"👤 <code>{u['user_id']}</code> — {escape_html(name)} ({username}){blocked}\n"
    text += f"\n<i>Total: {get_user_count()} users</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def usermsg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if messages_collection is None:
        await reply_html_safe(update.message, add_footer(f"⚠️ Database offline"))
        return
    pending_count = messages_collection.count_documents({"admin_replied": False})
    msgs = list(messages_collection.find().sort("created_at", DESCENDING).limit(20))
    if not msgs:
        await reply_html_safe(update.message, add_footer(f"📨 No messages yet."))
        return
    text = add_footer(f"📨 <b>User Messages</b>\n")
    if pending_count > 0:
        text += f"🆕 <b>{pending_count} pending reply</b>\n"
    text += "\n"
    for m in msgs:
        status = f"✅" if m.get('admin_replied') else f"🆕"
        name = escape_html(m.get('first_name', 'Unknown'))
        msg_text = escape_html(m.get('text', ''))[:100]
        date = m['created_at'].strftime('%m/%d %H:%M')
        text += f"{status} <code>{m['user_id']}</code> — {name}\n💬 {msg_text}{'...' if len(m.get('text', '')) > 100 else ''}\n📅 {date}\n\n"
    text += f"ℹ️ Reply to any forwarded message to respond, or use /reply &lt;user_id&gt; &lt;msg&gt;"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /reply &lt;user_id&gt; &lt;message&gt;", parse_mode=ParseMode.HTML)
        return
    try:
        target_user_id = int(context.args[0])
        reply_text = " ".join(context.args[1:])
        await application.bot.send_message(chat_id=target_user_id, text=add_footer(f"📨 <b>Reply from Admin:</b>\n\n{reply_text}"), parse_mode=ParseMode.HTML)
        await reply_html_safe(update.message, add_footer(f"✅ Message sent to user {target_user_id}"))
    except ValueError:
        await update.message.reply_text(f"❌ Invalid user ID")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")

async def setprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /setprice <plan> <price>\nPlans: weekly, 2week, 3week, monthly")
        return
    plan = args[0].lower()
    try:
        price = int(args[1])
    except ValueError:
        await update.message.reply_text("Invalid price.")
        return
    if plan not in DEFAULT_SUBSCRIPTION_PLANS:
        await update.message.reply_text(f"Invalid plan. Choose from: {', '.join(DEFAULT_SUBSCRIPTION_PLANS.keys())}")
        return
    set_subscription_price(plan, price)
    await update.message.reply_text(f"✅ Price for {plan} set to {price} KES.")

async def setmaxmovies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /setmaxmovies <plan> <max_movies>\nPlans: weekly, 2week, 3week, monthly")
        return
    plan = args[0].lower()
    try:
        max_movies = int(args[1])
    except ValueError:
        await update.message.reply_text("Invalid max movies.")
        return
    if plan not in DEFAULT_SUBSCRIPTION_PLANS:
        await update.message.reply_text(f"Invalid plan. Choose from: {', '.join(DEFAULT_SUBSCRIPTION_PLANS.keys())}")
        return
    set_subscription_max_movies(plan, max_movies)
    await update.message.reply_text(f"✅ Max movies for {plan} set to {max_movies}.")

async def clear_pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /clear_pending <user_id>\nClears pending payment tracker for the specified user.", parse_mode=ParseMode.HTML)
        return
    try:
        user_id = int(args[0])
        clear_pending_payment_tracker(user_id)
        await update.message.reply_text(f"✅ Cleared pending payment tracker for user {user_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Please provide a valid numeric user ID.")

# ============ DOUBLE CONFRONTER COMMANDS ============
async def doubleconfronter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removed — use dashboard album/upload instead."""
    if update.effective_message:
        await update.effective_message.reply_text("This command was removed. Use the admin dashboard.")
    return
    # legacy disabled below

    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return
    context.user_data["double_confronter_active"] = True
    context.user_data["double_confronter_movies"] = []
    context.user_data["double_confronter_poster"] = None
    context.user_data["double_confronter_prefix"] = None
    context.user_data.pop("double_confronter_price", None)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("10 KES", callback_data="dcprice_10"), InlineKeyboardButton("20 KES", callback_data="dcprice_20")],
        [InlineKeyboardButton(f"Default ({get_default_price()} KES)", callback_data="dcprice_default"), InlineKeyboardButton("✍️ Send your price", callback_data="dcprice_custom")],
        [InlineKeyboardButton("❌ Cancel", callback_data="dcprice_cancel")],
    ])
    await update.message.reply_text("📸 <b>Double Confronter Mode</b>\n\n1️⃣ (Optional) Send a <b>poster photo</b> — caption can be a prefix/title.\n2️⃣ Send the <b>videos</b> one by one, each with a caption like <code>wrong turn afro 3a</code>, <code>wrong turn afro 3b</code>, <code>wrong turn afro 4a</code>… the title is extracted from each video caption.\n3️⃣ Pick a price below, or send your own.\n4️⃣ Use <code>/completedouble</code> to finish — you'll get <b>one short token link</b>. A paying user receives the whole collection.\n\nUse <code>/canceldouble</code> to abort.", reply_markup=kb, parse_mode=ParseMode.HTML)

async def double_price_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id not in ADMIN_IDS:
        await query.answer("Admin only.", show_alert=True)
        return
    data = query.data
    if data == "dcprice_cancel":
        context.user_data.pop("double_confronter_active", None)
        context.user_data.pop("double_confronter_movies", None)
        context.user_data.pop("double_confronter_poster", None)
        context.user_data.pop("double_confronter_prefix", None)
        context.user_data.pop("double_confronter_price", None)
        await query.answer("Cancelled")
        try:
            await query.edit_message_text("❌ Double Confronter mode cancelled.")
        except Exception:
            pass
        return
    if data == "dcprice_custom":
        context.user_data["double_awaiting_price"] = True
        await query.answer()
        try:
            await query.edit_message_text("✍️ Send the price for this collection (numbers only, e.g. <code>50</code>).", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return
    if data == "dcprice_default":
        price = get_default_price()
    else:
        try:
            price = int(data.replace("dcprice_", ""))
        except ValueError:
            await query.answer("Invalid price", show_alert=True)
            return
    context.user_data["double_confronter_price"] = price
    await query.answer(f"✅ Price set: {price} KES")
    try:
        await query.edit_message_text(f"✅ Collection price set to <b>{price} KES</b>.\n\nNow send the videos (poster first if you have one).", parse_mode=ParseMode.HTML)
    except Exception:
        pass

async def handle_double_confronter_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.user_data.get("double_confronter_active"):
        return
    msg = update.message
    if msg.photo:
        context.user_data["double_confronter_poster"] = msg.photo[-1].file_id
        if msg.caption and msg.caption.strip():
            context.user_data["double_confronter_prefix"] = msg.caption.strip()
        await msg.reply_text("🖼 Poster saved. Now send the videos.")
        raise ApplicationHandlerStop()
    video = msg.video or msg.document
    if not video:
        return
    file_id = video.file_id
    file_type = "video" if msg.video else "document"
    raw_title = (msg.caption or "").strip()
    if not raw_title:
        raw_title = getattr(video, "file_name", "") or f"Part {len(context.user_data['double_confronter_movies']) + 1}"
        raw_title = re.sub(r'\.[A-Za-z0-9]{2,5}$', '', raw_title).replace('_', ' ').strip()
    title = extract_movie_title_for_button(raw_title) if raw_title else ""
    if not title or re.fullmatch(r'[a-zA-Z0-9]{1,3}', title):
        prefix = context.user_data.get("double_confronter_prefix")
        if prefix and title:
            title = f"{prefix} {title}"
        elif prefix:
            title = f"{prefix} Part {len(context.user_data['double_confronter_movies']) + 1}"
        else:
            title = f"Part {len(context.user_data['double_confronter_movies']) + 1}"
    context.user_data["double_confronter_movies"].append({"file_id": file_id, "file_type": file_type, "title": title, "caption": raw_title})
    count = len(context.user_data["double_confronter_movies"])
    await msg.reply_text(f"✅ Added <b>{title}</b> ({count} video{'s' if count != 1 else ''} so far). Send more or use /completedouble.", parse_mode=ParseMode.HTML)
    raise ApplicationHandlerStop()

async def handle_double_confronter_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if context.user_data.get("double_awaiting_price"):
        text = (update.message.text or "").strip()
        try:
            price = int(re.sub(r'[^\d]', '', text))
            if price <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Invalid price. Send a number like <code>50</code>.", parse_mode=ParseMode.HTML)
            raise ApplicationHandlerStop()
        context.user_data["double_confronter_price"] = price
        context.user_data["double_awaiting_price"] = False
        await update.message.reply_text(f"✅ Price set to <b>{price} KES</b>. Now send the videos.", parse_mode=ParseMode.HTML)
        raise ApplicationHandlerStop()
    if context.user_data.get("double_confronter_active") and not context.user_data.get("double_confronter_prefix"):
        text = (update.message.text or "").strip()
        if text and not text.startswith('/'):
            context.user_data["double_confronter_prefix"] = text
            await update.message.reply_text(f"🏷 Title prefix set: <b>{text}</b>. Now send the videos.", parse_mode=ParseMode.HTML)
            raise ApplicationHandlerStop()

async def setdoubleprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /setdoubleprice <price>")
        return
    try:
        price = int(args[0])
        context.user_data["double_confronter_price"] = price
        await update.message.reply_text(f"✅ Price set to {price} KES for the double bundle.")
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Send a number.")

async def canceldouble_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data.pop("double_confronter_active", None)
    context.user_data.pop("double_confronter_movies", None)
    context.user_data.pop("double_confronter_poster", None)
    context.user_data.pop("double_confronter_prefix", None)
    context.user_data.pop("double_confronter_price", None)
    context.user_data.pop("double_awaiting_price", None)
    await update.message.reply_text("❌ Double Confronter mode cancelled.")

async def completedouble_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.user_data.get("double_confronter_active"):
        await update.message.reply_text("❌ No active session. Start with /doubleconfronter.")
        return
    movies = context.user_data.get("double_confronter_movies", [])
    if len(movies) < 2:
        await update.message.reply_text("❌ At least 2 movies required.")
        return
    poster = context.user_data.get("double_confronter_poster")
    prefix = context.user_data.get("double_confronter_prefix")
    price = context.user_data.get("double_confronter_price", get_default_price())
    existing_tokens = [doc['token'] for doc in movies_collection.find({}, {'token': 1})]
    if series_bundles_collection is not None:
        existing_tokens.extend([doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})])
    if prefix:
        title = prefix.strip()
    else:
        first_title = movies[0].get("title", "")
        common = re.sub(r'\s*(\d+[a-z]?|part\s*\d+|ep\s*\d+)\s*$', '', first_title, flags=re.IGNORECASE).strip()
        title = common or "Movie Collection"
    main_token = generate_count_token()
    existing_tokens.append(main_token)
    saved_movies = []
    for idx, m in enumerate(movies, 1):
        token = generate_count_token()
        existing_tokens.append(token)
        movie_doc = {"token": token, "bundle_token": main_token, "title": m.get("title", f"Part {idx}"), "button_title": clean_movie_title_for_button(m.get("title", f"Part {idx}")), "description": f"Part {idx} of {len(movies)}", "price": price, "poster_url": poster, "file_id": m.get("file_id"), "file_type": m.get("file_type", "video"), "channel_id": None, "message_id": None, "private_link": "", "category": "movie", "downloads": 0, "created_at": datetime.utcnow(), "is_bundle_part": True, "bundle_parent": main_token, "collection_title": title, "collection_total": len(movies), "collection_index": idx}
        movies_collection.insert_one(movie_doc)
        saved_movies.append(movie_doc)

    # Generate individual tokens for each movie
    individual_tokens = []
    for movie in saved_movies:
        individual_tokens.append(movie['token'])

    # Generate combined token
    episodes = []
    for movie in saved_movies:
        episodes.append({"private_link": f"https://t.me/{context.bot.username}?start={movie['token']}", "title": movie["title"], "button_title": movie.get("button_title", ""), "channel_id": "", "message_id": 0, "file_id": movie.get("file_id"), "file_type": movie.get("file_type", "video"), "token": movie["token"]})
    bundle_data = {"token": main_token, "title": title, "button_title": clean_movie_title_for_button(title), "price": price * len(movies), "category": "series", "description": f"Collection of {len(movies)} movies", "poster_url": poster, "episodes": episodes, "episode_count": len(episodes), "created_at": datetime.utcnow(), "downloads": 0, "is_collection": True}
    series_bundles_collection.insert_one(bundle_data)
    cache_set_token(main_token, bundle_data, 'series')

    # Build output message with three options
    individual_links = "\n".join([f"• https://t.me/{context.bot.username}?start={t}" for t in individual_tokens])
    combined_link = f"https://t.me/{context.bot.username}?start={main_token}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Get each movie token", callback_data=f"get_each_{main_token}")],
        [InlineKeyboardButton("🔗 Get one token", callback_data=f"get_one_{main_token}")],
        [InlineKeyboardButton("📦 Get each and all", callback_data=f"get_each_all_{main_token}")]
    ])

    await update.message.reply_text(
        add_footer(f"""✅ Double Confronter bundle created!

Title: {title}
Movies: {len(saved_movies)}
Price per movie: {price} KES
Total: {price * len(movies)} KES

Individual tokens:
{individual_links}

Combined token:
{combined_link}

Choose how to deliver:"""),
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )
    context.user_data.pop("double_confronter_active", None)
    context.user_data.pop("double_confronter_movies", None)
    context.user_data.pop("double_confronter_poster", None)
    context.user_data.pop("double_confronter_prefix", None)
    context.user_data.pop("double_confronter_price", None)
    context.user_data.pop("double_awaiting_price", None)

async def handle_double_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    if data.startswith("get_each_"):
        token = data.replace("get_each_", "")
        bundle = series_bundles_collection.find_one({"token": token})
        if not bundle:
            await query.edit_message_text("❌ Bundle not found.")
            return
        episodes = bundle.get("episodes", [])
        for ep in episodes:
            await context.bot.send_message(chat_id=user_id, text=f"🎬 {ep.get('title')}\nhttps://t.me/{context.bot.username}?start={ep.get('token')}")
        await query.edit_message_text("✅ All individual tokens sent above.")
    elif data.startswith("get_one_"):
        token = data.replace("get_one_", "")
        await query.edit_message_text(f"🔗 One token link:\nhttps://t.me/{context.bot.username}?start={token}")
    elif data.startswith("get_each_all_"):
        token = data.replace("get_each_all_", "")
        bundle = series_bundles_collection.find_one({"token": token})
        if not bundle:
            await query.edit_message_text("❌ Bundle not found.")
            return
        episodes = bundle.get("episodes", [])
        for ep in episodes:
            await context.bot.send_message(chat_id=user_id, text=f"🎬 {ep.get('title')}\nhttps://t.me/{context.bot.username}?start={ep.get('token')}")
        await query.edit_message_text(f"✅ All individual tokens sent above.\n\nCombined token:\nhttps://t.me/{context.bot.username}?start={token}")

# ============ ADMIN CHILD BOT EXTRA COMMANDS (stubs) ============
async def set_bot_price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /set_bot_price <initial> <monthly>")
        return
    try:
        initial = int(args[0])
        monthly = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ Prices must be numbers.")
        return
    if settings_collection is not None:
        settings_collection.update_one({"key": "bot_initial_price"}, {"$set": {"value": initial, "updated_at": datetime.utcnow()}}, upsert=True)
        settings_collection.update_one({"key": "bot_monthly_price"}, {"$set": {"value": monthly, "updated_at": datetime.utcnow()}}, upsert=True)
    global BOT_INITIAL_PRICE, BOT_MONTHLY_PRICE
    BOT_INITIAL_PRICE = initial
    BOT_MONTHLY_PRICE = monthly
    await update.message.reply_text(f"✅ Bot prices updated: Initial = {initial} KES, Monthly = {monthly} KES")

async def stop_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /stop_bot <user_id>")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if bot_requests_collection is not None:
        result = bot_requests_collection.update_one({"user_id": target_id}, {"$set": {"status": "inactive"}})
        if result.matched_count:
            await update.message.reply_text(f"✅ Bot for user {target_id} stopped.")
        else:
            await update.message.reply_text("❌ Bot not found.")

async def start_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /start_bot <user_id>")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if bot_requests_collection is not None:
        result = bot_requests_collection.update_one({"user_id": target_id}, {"$set": {"status": "active"}})
        if result.matched_count:
            await update.message.reply_text(f"✅ Bot for user {target_id} started.")
        else:
            await update.message.reply_text("❌ Bot not found.")


async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Anyone: test if bot is active."""
    user = update.effective_user
    if not user:
        return
    t0 = datetime.utcnow()
    msg = await update.message.reply_text("🏓 Pong…")
    t1 = datetime.utcnow()
    ms = int((t1 - t0).total_seconds() * 1000)
    try:
        await msg.edit_text(f"🏓 <b>Pong</b>\nLatency: <b>{ms} ms</b>\nBot is active ✅ ⚡", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text(f"🏓 Pong — {ms} ms")

async def restart_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /restart_bot <user_id>")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return
    if bot_requests_collection is not None:
        result = bot_requests_collection.update_one({"user_id": target_id}, {"$set": {"status": "active", "restarted_at": datetime.utcnow()}})
        if result.matched_count:
            await update.message.reply_text(f"✅ Bot for user {target_id} restarted.")
        else:
            await update.message.reply_text("❌ Bot not found.")

async def broadcast_bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    msg = " ".join(context.args)
    if not msg:
        await update.message.reply_text("Usage: /broadcast_bots <message>")
        return
    if bot_requests_collection is not None:
        bots = list(bot_requests_collection.find({"status": "active"}))
        sent = 0
        for b in bots:
            logger.info(f"Broadcast to bot {b['user_id']}: {msg}")
            sent += 1
        await update.message.reply_text(f"✅ Broadcast sent to {sent} active bots.")
    else:
        await update.message.reply_text("❌ Database not available.")

# ============ COMMAND: /moremovies ============
async def more_movies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    track_user_bg(user.id, user.username, user.first_name, user.last_name)
    await show_more_movies(update, context)

# ============ FLASK APP ============
app = Flask(__name__, static_folder='static', static_url_path='')

# --- Fast Telegram HTTP layer -------------------------------------------------
# The default HTTPXRequest uses connection_pool_size=1: EVERY Bot API call
# (webhook replies, dashboard album sends, poster uploads) waits in a single
# queue. That is the "long loading / delay" on album posts. Pool + timeouts fix it.
try:
    from telegram.request import HTTPXRequest
    _tg_request = HTTPXRequest(
        connection_pool_size=16,   # parallel API calls (albums, buttons, replies)
        connect_timeout=10.0,
        read_timeout=30.0,
        write_timeout=60.0,        # room for poster uploads
        pool_timeout=10.0,
    )
    _tg_updates_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=10.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=5.0,
    )
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(_tg_request)
        .get_updates_request(_tg_updates_request)
        .concurrent_updates(True)  # webhook updates processed in parallel
        .build()
    )
    logger.info("Telegram HTTP client: pooled connections (16) + concurrent updates ON")
except Exception as _req_err:
    logger.warning(f"Pooled HTTP client setup failed, using defaults: {_req_err}")
    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

# ---- Global safety: never leak raw <tg-emoji> tags to users ----
# If outgoing text contains premium emoji tags and parse_mode is not HTML,
# force HTML mode. On entity errors, strip tags and retry (handled in send_html_safe paths too).
def _ensure_no_raw_tg_emoji(fn):
    import functools
    from telegram.constants import ParseMode as _PM
    @functools.wraps(fn)
    async def _wrapped(*args, **kwargs):
        text = kwargs.get("text")
        caption = kwargs.get("caption")
        body = text if text is not None else caption
        pm = kwargs.get("parse_mode")
        if body and "<tg-emoji" in str(body):
            pm_ok = False
            try:
                if pm is _PM.HTML:
                    pm_ok = True
                elif isinstance(pm, str) and str(pm).upper() == "HTML":
                    pm_ok = True
            except Exception:
                pm_ok = False
            if not pm_ok:
                kwargs["parse_mode"] = _PM.HTML
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                err = str(e).lower()
                if any(x in err for x in ("entity", "document_invalid", "parse", "emoji", "can't parse")):
                    plain = strip_premium_emojis(str(body))
                    if text is not None:
                        kwargs["text"] = plain
                    if caption is not None:
                        kwargs["caption"] = plain
                    try:
                        return await fn(*args, **kwargs)
                    except Exception:
                        kwargs.pop("parse_mode", None)
                        if text is not None:
                            kwargs["text"] = strip_premium_emojis(plain)
                        if caption is not None:
                            kwargs["caption"] = strip_premium_emojis(plain)
                        return await fn(*args, **kwargs)
                raise
        return await fn(*args, **kwargs)
    return _wrapped

try:
    application.bot.send_message = _ensure_no_raw_tg_emoji(application.bot.send_message)
    application.bot.edit_message_text = _ensure_no_raw_tg_emoji(application.bot.edit_message_text)
    application.bot.edit_message_caption = _ensure_no_raw_tg_emoji(application.bot.edit_message_caption)
    application.bot.send_photo = _ensure_no_raw_tg_emoji(application.bot.send_photo)
    logger.info("Global tg-emoji leak protection enabled on bot send/edit methods")
except Exception as _wrap_err:
    logger.warning(f"Could not wrap bot send methods for emoji safety: {_wrap_err}")


try:
    _pending_payments_coll = db.pending_payments
except NameError:
    _pending_payments_coll = None

collections_dict = {
    'movies': movies_collection,
    'payments': payments_collection,
    'users': users_collection,
    'settings': settings_collection,
    'subscriptions': subscriptions_collection,
    'series_bundles': series_bundles_collection,
    'pending_payments': _pending_payments_coll,
    'mongodb_connected': mongodb_connected,
}
setup_pending_payment_handlers(application, collections_dict)

_bot_loop = None
_loop_thread = None

def start_bot_loop():
    global _bot_loop
    _bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_bot_loop)
    _bot_loop.run_forever()

def run_async(coro, timeout=60):
    if _bot_loop is None or not _bot_loop.is_running():
        logger.error("Bot event loop is not running!")
        return None
    future = asyncio.run_coroutine_threadsafe(coro, _bot_loop)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        logger.error(f"Async operation timed out after {timeout}s — the post may still land late; retry from dashboard")
        future.cancel()
        return None
    except Exception as e:
        logger.error(f"Async operation failed: {e}")
        logger.error(traceback.format_exc())
        return None

def run_async_background(coro):
    if _bot_loop is None or not _bot_loop.is_running():
        logger.error("Bot event loop is not running!")
        return None
    try:
        return asyncio.run_coroutine_threadsafe(coro, _bot_loop)
    except Exception as e:
        logger.error(f"Background async operation failed: {e}")
        return None

async def cancelking_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel every active admin workflow without deleting stored movies."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    admin_confronter_king_sessions.pop(user.id, None)
    if 'admin_upload_sessions_collection' in globals() and admin_upload_sessions_collection is not None:
        admin_upload_sessions_collection.delete_one({'admin_id': user.id})
    for key in list(context.user_data):
        if any(part in key.lower() for part in ('session', 'upload', 'bundle', 'post', 'bulk', 'confronter', 'pending')):
            context.user_data.pop(key, None)
    await update.message.reply_text("All active operations were cancelled. Stored movies and payments were kept.")


async def togglelimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: show/hide remaining movie limit to users (alias of /togglewatched)."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    current = get_show_watched_remaining()
    set_show_watched_remaining(not current)
    state = "ON (users see remaining limit)" if not current else "OFF (limit hidden)"
    await update.message.reply_text(f"👁️ Show movie limit: <b>{state}</b>", parse_mode=ParseMode.HTML)

def get_show_movie_limit() -> bool:
    if not mongodb_connected or settings_collection is None:
        return True
    try:
        cur = settings_collection.find_one({"key": "show_movie_limit"})
        if not cur:
            return True
        return bool(cur.get("value", True))
    except Exception:
        return True

# ============ REGISTER HANDLERS ============
def setup_handlers():
    application.add_error_handler(error_handler)
    # Hidden admin features (not in /help or BotCommand menu)
    if register_dead_handlers:
        register_dead_handlers(application)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("search", search_cmd))
    application.add_handler(CommandHandler("mystatus", mystatus_cmd))
    application.add_handler(CommandHandler("checksubscription", checksubscription_cmd))
    application.add_handler(CommandHandler("mymovies", my_movies_cmd))
    application.add_handler(CommandHandler("myplanmovies", myplanmovies_cmd))
    application.add_handler(CommandHandler("togglewatched", togglewatched_cmd))
    application.add_handler(CommandHandler("checkpayment", check_payment_status))

    application.add_handler(CommandHandler("dev", dev_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("cart", cart_cmd))
    application.add_handler(CommandHandler("movies", movies_cmd))
    application.add_handler(CommandHandler("browsebundles", browse_bundles_cmd))
    application.add_handler(CommandHandler("moremovies", more_movies_cmd))
    application.add_handler(CommandHandler("claimfree", claimfree_cmd))
    application.add_handler(CommandHandler("getbot", getbot_cmd))
    application.add_handler(CommandHandler("add_bot_token", add_bot_token_cmd))
    application.add_handler(CommandHandler("mybot", mybot_cmd))
    application.add_handler(CommandHandler("mastermovies", mastermovies_cmd))
    application.add_handler(CommandHandler("resolvechannels", resolvechannels_cmd))
    application.add_handler(CommandHandler("list_bots", list_bots_cmd))
    application.add_handler(CommandHandler("delete_bot", delete_bot_cmd))
    application.add_handler(CommandHandler("createbundle", create_bundle_cmd))
    application.add_handler(CommandHandler("cancelbundle", cancel_bundle_cmd))
    application.add_handler(CommandHandler("skipposter", skip_bundle_poster_cmd))
    application.add_handler(CommandHandler("completebundle", complete_bundle_cmd))
    application.add_handler(CommandHandler("donebyconfronter", donebyconfronter_cmd))
    application.add_handler(CommandHandler("togglereengage", togglereengage_cmd))
    application.add_handler(CommandHandler("setreengagetext", setreengagetext_cmd))
    application.add_handler(CommandHandler("setreengagehours", setreengagehours_cmd))

    async def _reengage_job():
        try:
            enabled, rtext, hours = get_reengage_settings()
            if not enabled or users_collection is None or application is None:
                return
            cutoff = datetime.utcnow() - timedelta(hours=float(hours or 48))
            # Prefer last_activity, fall back to last_interaction
            query = {
                "blocked": {"$ne": True},
                "reengage_sent": {"$ne": True},
                "$or": [
                    {"last_activity": {"$exists": True, "$lt": cutoff}},
                    {"last_interaction": {"$exists": True, "$lt": cutoff}},
                    # users with no activity fields but old join time
                    {"last_activity": {"$exists": False}, "last_interaction": {"$exists": False},
                     "joined_at": {"$exists": True, "$lt": cutoff}},
                ],
            }
            stale = list(users_collection.find(
                query,
                {"user_id": 1, "username": 1, "first_name": 1, "last_name": 1,
                 "last_activity": 1, "last_interaction": 1},
            ).limit(80))
            sent = 0
            for u in stale:
                uid = u.get("user_id")
                if not uid:
                    continue
                # Double-check inactivity against preferred field
                last = u.get("last_activity") or u.get("last_interaction")
                if last and last > cutoff:
                    continue
                try:
                    msg = format_reengage_message(rtext, u)
                    # Plain friendly note — strip any accidental premium tags
                    msg = strip_premium_emojis(msg or "")
                    try:
                        await application.bot.send_message(chat_id=uid, text=msg, parse_mode=None)
                    except Exception:
                        await application.bot.send_message(chat_id=uid, text=strip_premium_emojis(msg))
                    users_collection.update_one(
                        {"user_id": uid},
                        {"$set": {"reengage_sent": True, "reengage_sent_at": datetime.utcnow()}},
                    )
                    sent += 1
                    await asyncio.sleep(BOT_FAST_DELAY)
                except Exception as e:
                    err_txt = str(e).lower()
                    # Dead users (blocked the bot / deactivated) clog the 80-user
                    # batch every cycle — mark them so real idle users get pinged.
                    if any(x in err_txt for x in ('forbidden', 'blocked', 'deactivated', 'chat not found', 'user not found')):
                        try:
                            users_collection.update_one(
                                {"user_id": uid},
                                {"$set": {"blocked": True, "reengage_sent": True}},
                            )
                        except Exception:
                            pass
                    logger.debug(f"reengage send fail {uid}: {e}")
            if sent:
                logger.info(f"Re-engage: sent {sent} messages (idle > {hours}h)")
        except Exception as e:
            logger.error(f"Re-engage job error: {e}")
    # IMPORTANT: this is a sync thread — AsyncIOScheduler.start() here binds to a
    # dead event loop, which is why auto re-engage never fired. The job is only
    # QUEUED here; scheduler.start() happens inside init_bot() on the bot loop.
    try:
        scheduler.add_job(_reengage_job, "interval", minutes=2, id="reengage_job", replace_existing=True)
        logger.info("Re-engage job queued — starts on the bot loop (runs every 2 min)")
    except Exception as e:
        logger.error(f"Re-engage scheduler error: {e}")
    application.add_handler(CommandHandler("confronterking", confronterking_cmd))
    application.add_handler(CommandHandler("confronterkingcancel", confronterkingcancel_cmd))
    application.add_handler(CommandHandler("confronterupload", confronterupload_cmd))
    application.add_handler(CommandHandler("confronteruploaddone", confronteruploaddone_cmd))
    # King deliver (menu text also listed in dead.py /confronterisdead)
    application.add_handler(CommandHandler("kingmovies", kingmovies_cmd))
    application.add_handler(CommandHandler("kingdeliver", kingdeliver_cmd))
    application.add_handler(CommandHandler("cancelking", cancelking_cmd))
    application.add_handler(CommandHandler("confronterisundefeated", confronterisundefeated_cmd))

    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("monthlyusers", monthlyusers_cmd))
    application.add_handler(CommandHandler("updatedescription", updatedescription_cmd))
    application.add_handler(CommandHandler("users", users_cmd))
    application.add_handler(CommandHandler("usermsg", usermsg_cmd))
    application.add_handler(CommandHandler("inbox", usermsg_cmd))
    application.add_handler(CommandHandler("reply", reply_cmd))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))
    application.add_handler(CommandHandler("confirmbroadcast", confirm_broadcast_cmd))
    application.add_handler(CommandHandler("cancelbroadcast", cancel_broadcast_cmd))
    application.add_handler(CommandHandler("broadcaststats", broadcast_stats_cmd))
    application.add_handler(CommandHandler("setprice", setprice_cmd))
    application.add_handler(CommandHandler("setmaxmovies", setmaxmovies_cmd))
    application.add_handler(CommandHandler("togglecard", togglecard_cmd))
    application.add_handler(CommandHandler("togglesubscription", togglesubscription_cmd))
    application.add_handler(CommandHandler("togglepayhero", togglepayhero_cmd))
    application.add_handler(CommandHandler("setpayherochannel", setpayherochannel_cmd))
    application.add_handler(CommandHandler("togglecart", togglecart_cmd))
    application.add_handler(CommandHandler("toggledisplay", toggle_display_mode_cmd))
    application.add_handler(CommandHandler("setdefaultstyle", setdefaultstyle_cmd))
    application.add_handler(CommandHandler("setmenustyle", setmenustyle_cmd))
    application.add_handler(CommandHandler("togglegenre", toggle_genre_cmd))
    application.add_handler(CommandHandler("togglegenre1", toggle_genre1_cmd))
    application.add_handler(CommandHandler("togglelimit", togglelimit_cmd))
    application.add_handler(CommandHandler("clear_pending", clear_pending_cmd))
    application.add_handler(CommandHandler("cancel", cancel_reply_cmd))
    application.add_handler(CommandHandler("upload", upload_cmd))
    application.add_handler(CommandHandler("cancelupload", cancel_upload_cmd))
    application.add_handler(CommandHandler("complete", complete_upload))
    application.add_handler(CommandHandler("bulkupload", bulk_upload_cmd))
    application.add_handler(CommandHandler("cancelbulk", cancel_bulk_upload_cmd))
    application.add_handler(CommandHandler("skipposter", skip_poster_cmd))
    application.add_handler(CommandHandler("addchannel", add_channel_cmd))
    application.add_handler(CommandHandler("listchannels", list_channels_cmd))
    application.add_handler(CommandHandler("removechannel", remove_channel_cmd))
    application.add_handler(CommandHandler("createpost", create_post_cmd))
    application.add_handler(CommandHandler("buttons", buttons_cmd))
    application.add_handler(CommandHandler("preview", preview_cmd))
    application.add_handler(CommandHandler("sendpost", sendpost_cmd))
    application.add_handler(CommandHandler("cancelpost", cancelpost_cmd))
    application.add_handler(CommandHandler("addpremium", addpremium_cmd))
    application.add_handler(CommandHandler("setduration", setduration_cmd))
    application.add_handler(CommandHandler("setlimit", setlimit_cmd))
    application.add_handler(CommandHandler("removepremium", removepremium_cmd))
    application.add_handler(CommandHandler("revoke", revoke_cmd))
    application.add_handler(CommandHandler("endsubscription", endsubscription_cmd))
    application.add_handler(CommandHandler("set_bot_price", set_bot_price_cmd))
    application.add_handler(CommandHandler("stop_bot", stop_bot_cmd))
    application.add_handler(CommandHandler("start_bot", start_bot_cmd))
    application.add_handler(CommandHandler("ping", ping_cmd))
    application.add_handler(CommandHandler("restart_bot", restart_bot_cmd))
    application.add_handler(CommandHandler("broadcast_bots", broadcast_bots_cmd))
    application.add_handler(CommandHandler("togglegetbot", toggle_getbot_cmd))
    application.add_handler(CommandHandler("toggleprem", toggleprem_cmd))

    application.add_handler(MessageHandler(filters.ALL & filters.ChatType.PRIVATE & ~filters.Chat(chat_id=[a for a in ADMIN_IDS]), force_join_gate), group=-2)
    application.add_handler(ChatJoinRequestHandler(force_join_request_handler), group=-2)
    application.add_handler(ChatMemberHandler(force_my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER), group=-2)
    application.add_handler(MessageHandler(filters.Chat(chat_id=[a for a in ADMIN_IDS]) & (filters.TEXT | filters.FORWARDED) & ~filters.COMMAND, handle_resolve_channel_flow), group=-1)
    application.add_handler(MessageHandler(filters.Chat(chat_id=[a for a in ADMIN_IDS]) & (filters.PHOTO | filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND, handle_double_confronter_media), group=-1)
    application.add_handler(MessageHandler(filters.Chat(chat_id=[a for a in ADMIN_IDS]) & filters.TEXT & ~filters.COMMAND, handle_double_confronter_text), group=-1)
    application.add_handler(MessageHandler(filters.Chat(chat_id=[a for a in ADMIN_IDS]) & filters.TEXT & ~filters.COMMAND, handle_master_edit_text), group=-1)

    application.add_handler(CallbackQueryHandler(request_prem_callback, pattern="^request_prem$"))
    application.add_handler(CallbackQueryHandler(chat_admin_callback, pattern="^chat_admin$"))
    application.add_handler(CallbackQueryHandler(handle_post_channel_selection, pattern=r"^post_channel_-?\d+$"))
    application.add_handler(CallbackQueryHandler(handle_post_cancel, pattern=r"^post_cancel$"))
    application.add_handler(CallbackQueryHandler(handle_post_preview, pattern=r"^post_preview$"))
    application.add_handler(CallbackQueryHandler(handle_post_send, pattern=r"^post_send$"))
    application.add_handler(CallbackQueryHandler(handle_post_buttons, pattern=r"^post_add_buttons$"))
    application.add_handler(CallbackQueryHandler(handle_upload_price_callback, pattern=r"^upload_price_"))
    application.add_handler(CallbackQueryHandler(handle_upload_channel_callback, pattern=r"^(upload_channel_|upload_cancel$)"))
    application.add_handler(CallbackQueryHandler(handle_confronter_king_channel_callback, pattern=r"^ck_channel_"))
    application.add_handler(CallbackQueryHandler(handle_confronter_king_channel_callback, pattern=r"^ck_cancel$"))
    application.add_handler(CallbackQueryHandler(handle_confronter_king_price_callback, pattern=r"^ck_price_"))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(CallbackQueryHandler(handle_double_choice_callback, pattern=r"^get_(each|one|each_all)_"))
    application.add_handler(MessageHandler(filters.REPLY & filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & ~filters.COMMAND, handle_admin_reply_simple))
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & filters.TEXT & ~filters.COMMAND, handle_bundle_text), group=0)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & filters.TEXT & ~filters.COMMAND, handle_select_bundle_text), group=1)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & filters.TEXT & ~filters.COMMAND, handle_bulk_text), group=2)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & filters.TEXT & ~filters.COMMAND, handle_upload_text), group=3)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & (filters.PHOTO) & ~filters.COMMAND, handle_bundle_media), group=4)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & (filters.PHOTO) & ~filters.COMMAND, handle_select_bundle_media), group=5)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & (filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.FORWARDED) & ~filters.COMMAND, handle_bulk_media), group=6)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & (filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.FORWARDED) & ~filters.COMMAND, handle_upload_media), group=7)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & filters.TEXT & ~filters.COMMAND, handle_pending_reply_simple), group=8)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & filters.FORWARDED & ~filters.COMMAND, handle_add_channel_forward), group=9)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & filters.TEXT & ~filters.COMMAND, handle_buttons_input), group=10)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.Sticker.ALL) & ~filters.COMMAND, handle_post_message), group=11)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_phone_input), group=12)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Chat(chat_id=[a for a in ADMIN_IDS]), handle_keyword_reply), group=13)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Chat(chat_id=[a for a in ADMIN_IDS]), handle_search_text), group=5)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND & ~filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0), handle_search_text), group=13)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.ALL & ~filters.COMMAND & ~filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0), forward_user_message_to_admin), group=14)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & filters.PHOTO & ~filters.COMMAND, handle_confronter_upload_poster), group=15)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & (filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND, handle_confronter_upload_video), group=16)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & filters.TEXT & ~filters.COMMAND, handle_confronter_king_price_text), group=17)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & filters.PHOTO & ~filters.COMMAND, handle_confronter_king_poster), group=18)
    application.add_handler(MessageHandler(filters.Chat(chat_id=ADMIN_IDS[0] if ADMIN_IDS else 0) & (filters.VIDEO | filters.Document.ALL) & ~filters.COMMAND, handle_confronter_king_video), group=19)

# ============ FLASK ROUTES ============

def _dash_cookie_value() -> str:
    """HMAC token proving dashboard password was entered."""
    import hashlib, hmac
    return hmac.new(
        DASHBOARD_SESSION_SECRET.encode('utf-8'),
        b'dashboard-ok',
        hashlib.sha256
    ).hexdigest()

def _dashboard_authed() -> bool:
    try:
        tok = request.cookies.get(DASHBOARD_COOKIE_NAME, '')
        if not tok:
            return False
        import hmac as _hmac
        return _hmac.compare_digest(tok, _dash_cookie_value())
    except Exception:
        return False

def _login_shell_html() -> str:
    """Minimal gate only — no admin JS, no API map. Real UI only after cookie auth."""
    name = (BOT_DISPLAY_NAME or 'Admin').replace('<', '')
    logo = (DASHBOARD_LOGO or START_IMAGE_URL or '').replace('"', '').replace("'", '')
    logo_tag = ('<img src="' + logo + '" alt="">') if logo else ''
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>" + name + "</title><style>"
        "body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
        "background:#000;color:#f4f4f8;font-family:system-ui,sans-serif}"
        ".card{background:#101013;border:1px solid #232329;border-radius:16px;padding:1.6rem;"
        "width:min(400px,92vw);text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.8)}"
        ".card:before{content:'';display:block;height:3px;background:#ff3b30;margin:-1.6rem -1.6rem 1rem;border-radius:16px 16px 0 0}"
        "img{width:56px;height:56px;border-radius:14px;object-fit:cover;margin-bottom:.6rem}"
        "h1{font-size:1.15rem;margin:0 0 .2rem} h1 span{color:#ff3b30}"
        "p{color:#6a6a8a;font-size:12px;margin:0 0 1rem}"
        "input{width:100%;padding:12px;border-radius:10px;border:1px solid #232329;background:#0c0c0e;"
        "color:#fff;font-size:15px;letter-spacing:2px;text-align:center;box-sizing:border-box;margin-bottom:.6rem}"
        "button{width:100%;padding:12px;border:none;border-radius:10px;background:#ff3b30;color:#fff;"
        "font-weight:700;cursor:pointer;font-size:14px}"
        ".err{color:#ff7675;font-size:12px;min-height:18px;margin-top:.5rem}"
        "</style></head><body><div class=\"card\">"
        + logo_tag +
        "<h1>" + name + " <span>Admin</span></h1>"
        "<p>Enter password to continue</p>"
        "<input type=\"password\" id=\"pw\" placeholder=\"Password\" autocomplete=\"current-password\" "
        "onkeydown=\"if(event.key===\'Enter\')go()\">"
        "<button type=\"button\" onclick=\"go()\">Unlock</button>"
        "<div class=\"err\" id=\"err\"></div></div>"
        "<script>async function go(){var pw=document.getElementById('pw').value;var err=document.getElementById('err');"
        "err.textContent='';try{var r=await fetch('/api/check-password',{method:'POST',"
        "headers:{'Content-Type':'application/json'},credentials:'same-origin',"
        "body:JSON.stringify({password:pw})});var d=await r.json();"
        "if(d.success||d.ok){location.reload();return;}err.textContent='Wrong password';}"
        "catch(e){err.textContent='Network error';}}</script></body></html>"
    )


def _load_dashboard_html() -> str:
    """Load real admin UI (never from public index.html decoy). Inject .env branding."""
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, 'dashboard_ui.html'),
        os.path.join(base, 'static', 'dashboard_ui.html'),
        os.path.join(base, 'artifacts', 'dashboard_ui.html'),
    ]
    raw = None
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw = f.read()
                break
            except Exception as e:
                logger.error(f"dashboard_ui read error: {e}")
    if not raw:
        return "<!DOCTYPE html><html><body style='background:#000;color:#fff;font-family:sans-serif;padding:2rem'><h2>Dashboard UI missing</h2><p>Place <code>dashboard_ui.html</code> next to bot.py</p></body></html>"
    logo = DASHBOARD_LOGO or START_IMAGE_URL or ''
    mapping = {
        '{{DASHBOARD_TITLE}}': DASHBOARD_TITLE or f'{BOT_DISPLAY_NAME} — Admin',
        '{{BOT_DISPLAY_NAME}}': BOT_DISPLAY_NAME or 'Movie Bot',
        '{{BOT_USERNAME}}': (BOT_USERNAME or '').lstrip('@'),
        '{{DASHBOARD_LOGO}}': logo,
        '{{ADMIN_USERNAME}}': admin_handle(),
        '{{YEAR_COPY}}': str(YEAR_COPY),
    }
    for k, v in mapping.items():
        raw = raw.replace(k, str(v or ''))
    return raw


@app.before_request
def _protect_dashboard_apis():
    """Block admin API without cookie. Payment webhooks stay public."""
    path = (request.path or '')
    if not path.startswith('/api/'):
        return None
    public_exact = {
        '/api/check-password',
        '/api/public-config',
        '/api/logout-dashboard',
    }
    if path in public_exact:
        return None
    public_prefixes = (
        '/api/paystack/',
        '/api/payhero/',
        '/api/makamesco',
        '/api/poster/',
    )
    if any(path.startswith(pref) for pref in public_prefixes):
        return None
    if 'callback' in path or 'webhook' in path:
        return None
    if _dashboard_authed():
        return None
    return jsonify({"error": "Unauthorized", "login": True}), 401


@app.route('/', methods=['GET', 'POST', 'HEAD'])
def dashboard():
    # Makamesco / some gateways POST webhooks to the root URL — accept them here
    if request.method == 'POST':
        try:
            data = request.get_json(silent=True) or {}
            blob = json_mod.dumps(data).lower() if data else ""
            if data and any(k in blob for k in (
                "checkoutrequestid", "checkout_request_id", "mpesareceipt",
                "payment.completed", "payment.failed", "merchantrequestid",
                "stk", "makamesco", "resultcode", "externalreference",
                "charge.success", "charge.failed", "paystack",
            )):
                # Paystack payloads often hit root — route correctly
                is_paystack = (
                    "paystack" in blob
                    or data.get("event") in ("charge.success", "charge.failed", "charge.abandoned")
                    or (isinstance(data.get("data"), dict) and data["data"].get("domain") in ("live", "test"))
                    or (isinstance(data.get("data"), dict) and str((data.get("data") or {}).get("reference", "")).startswith("stk_"))
                )
                if is_paystack:
                    logger.info("Root POST looks like Paystack webhook → routing")
                    try:
                        return paystack_webhook()
                    except Exception as pe:
                        logger.error(f"paystack root route: {pe}")
                        # fall through to makamesco recovery
                logger.info(f"Root POST looks like Makamesco/payment webhook → routing")
                return makamesco_callback()
        except Exception as e:
            logger.error(f"Root POST webhook route error: {e}")
        return jsonify({"status": "ok"}), 200
    # Without signed cookie → login shell only (no admin UI in page source)
    if not _dashboard_authed():
        return _login_shell_html(), 200, {
            'Content-Type': 'text/html; charset=utf-8',
            'Cache-Control': 'no-store, no-cache, must-revalidate',
            'X-Robots-Tag': 'noindex, nofollow',
        }
    html = _load_dashboard_html()
    return html, 200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Cache-Control': 'no-store, no-cache, must-revalidate',
        'X-Robots-Tag': 'noindex, nofollow',
    }


@app.route('/manifest.json')
def serve_manifest():
    root = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root, 'manifest.json')
    if os.path.exists(path):
        return send_from_directory(root, 'manifest.json', mimetype='application/manifest+json')
    # Fallback minimal manifest
    return jsonify({
        "name": (BOT_DISPLAY_NAME or "Admin") + " Dashboard",
        "short_name": BOT_DISPLAY_NAME[:12] or "Admin",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#ff3b30",
        "icons": [{"src": "https://files.catbox.moe/1nhlk2.jpg", "sizes": "192x192", "type": "image/jpeg"}]
    })

@app.route('/sw.js')
def serve_sw():
    root = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(root, 'sw.js')
    if os.path.exists(path):
        return send_from_directory(root, 'sw.js', mimetype='application/javascript')
    return "self.addEventListener('install', e => self.skipWaiting());", 200, {'Content-Type': 'application/javascript'}

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """Serve local poster cache. Missing files return tiny placeholder (no 404 spam — disk is ephemeral on Render)."""
    from flask import Response
    try:
        os.makedirs(SERVER_UPLOAD_DIR, exist_ok=True)
        safe = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
        fpath = os.path.join(SERVER_UPLOAD_DIR, safe)
        if os.path.isfile(fpath):
            return send_from_directory(SERVER_UPLOAD_DIR, safe, max_age=86400 * 30)
        # Ephemeral disk lost the file — silent placeholder
        return Response(
            base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="),
            mimetype='image/png',
            headers={'Cache-Control': 'public, max-age=60'},
        )
    except Exception as e:
        logger.debug(f"serve upload {filename}: {e}")
        return Response(
            base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="),
            mimetype='image/png',
            headers={'Cache-Control': 'no-cache'},
        )


# Tiny 1x1 transparent PNG — returned instead of 404 so UI/logs stay clean
_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)
_POSTER_FAIL_CACHE = {}  # token -> timestamp of last hard-fail (skip external retries briefly)

# In-memory poster bytes cache — dashboard hits /api/poster/<token> once per image
# on every render; without this each tap/page-load pays a Mongo query + possibly a
# Telegram getFile round-trip. TTL 10 min, bounded size.
_POSTER_MEM_CACHE = {}   # token -> (expires_at, content_bytes, mimetype)
_POSTER_MEM_TTL = 600
_POSTER_MEM_MAX = 400

def _poster_mem_get(token):
    ent = _POSTER_MEM_CACHE.get(token)
    if ent and ent[0] > time_mod.time():
        return ent[1], ent[2]
    if ent:
        _POSTER_MEM_CACHE.pop(token, None)
    return None

def _poster_mem_set(token, content, mimetype):
    try:
        if not content or len(content) > 8 * 1024 * 1024:
            return
        if len(_POSTER_MEM_CACHE) >= _POSTER_MEM_MAX:
            # drop oldest ~10%
            for k in sorted(_POSTER_MEM_CACHE, key=lambda k: _POSTER_MEM_CACHE[k][0])[:_POSTER_MEM_MAX // 10]:
                _POSTER_MEM_CACHE.pop(k, None)
        _POSTER_MEM_CACHE[token] = (time_mod.time() + _POSTER_MEM_TTL, content, mimetype)
    except Exception:
        pass

@app.route('/api/poster/<token>', methods=['GET'])
def api_poster_proxy(token):
    """Fast poster for dashboard. Order: memory cache → local → Telegram file_id → data-URL → external (short timeout).
    Always prefers file_id. Never hangs on dead catbox. Returns placeholder (200) instead of 404 spam.
    """
    from flask import Response
    try:
        # 0) In-memory cache — repeat dashboard loads are instant (no Mongo, no network)
        cached_hit = _poster_mem_get(token)
        if cached_hit:
            return Response(cached_hit[0], mimetype=cached_hit[1], headers={
                'Cache-Control': 'public, max-age=604800',
                'X-Poster': 'mem-cache',
            })

        movie = None
        if mongodb_connected and movies_collection is not None:
            movie = movies_collection.find_one({"token": token})
        if not movie and mongodb_connected and series_bundles_collection is not None:
            movie = series_bundles_collection.find_one({"token": token})
        if not movie:
            # No such token — silent placeholder (stops 404 flood for series-N etc.)
            return Response(_PLACEHOLDER_PNG, mimetype='image/png', headers={
                'Cache-Control': 'public, max-age=60',
                'X-Poster': 'missing',
            })

        poster_url = (movie.get('poster_url') or '').strip()
        file_id = (movie.get('poster_file_id') or movie.get('poster_tg_file_id') or '').strip()

        # 1) Local /uploads (instant)
        local_name = None
        if poster_url.startswith('/uploads/'):
            local_name = poster_url.split('/uploads/', 1)[-1]
        elif '/uploads/' in poster_url:
            local_name = poster_url.rsplit('/uploads/', 1)[-1].split('?')[0]
        if local_name:
            local_name = re.sub(r'[^a-zA-Z0-9._-]', '_', local_name)
            fpath = os.path.join(SERVER_UPLOAD_DIR, local_name)
            if os.path.isfile(fpath):
                return send_from_directory(SERVER_UPLOAD_DIR, local_name, max_age=86400 * 30)

        # 2) Telegram file_id FIRST (reliable, never depends on catbox)
        if file_id and BOT_TOKEN:
            try:
                info = requests.get(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                    params={'file_id': file_id},
                    timeout=8,
                ).json()
                fpath_rel = ((info.get('result') or {}).get('file_path') or '')
                if fpath_rel:
                    dl = requests.get(
                        f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fpath_rel}",
                        timeout=15,
                    )
                    if dl.status_code == 200 and dl.content:
                        try:
                            os.makedirs(SERVER_UPLOAD_DIR, exist_ok=True)
                            cache_name = f"tg_{re.sub(r'[^a-zA-Z0-9_-]', '_', token)[:40]}.jpg"
                            cache_path = os.path.join(SERVER_UPLOAD_DIR, cache_name)
                            if not os.path.isfile(cache_path):
                                with open(cache_path, 'wb') as cf:
                                    cf.write(dl.content)
                            if mongodb_connected:
                                base = (SERVER_UPLOAD_URL_BASE or WEBHOOK_URL or '').strip()
                                new_url = f"{base.rstrip('/')}/uploads/{cache_name}" if base else f"/uploads/{cache_name}"
                                coll = movies_collection if movie.get('token') and movies_collection is not None else series_bundles_collection
                                if coll is not None:
                                    coll.update_one(
                                        {"token": token},
                                        {"$set": {"poster_url": new_url}},
                                    )
                        except Exception as ce:
                            logger.debug(f"poster cache write: {ce}")
                        _poster_mem_set(token, dl.content, 'image/jpeg')
                        return Response(dl.content, mimetype='image/jpeg', headers={
                            'Cache-Control': 'public, max-age=604800',
                            'X-Poster': 'file_id',
                        })
            except Exception as e:
                logger.debug(f"poster telegram download {token}: {e}")

        # 3) data-URL
        if poster_url.startswith('data:'):
            try:
                header, _, encoded = poster_url.partition(',')
                raw = base64.b64decode(encoded)
                ctype = 'image/jpeg'
                if 'png' in header:
                    ctype = 'image/png'
                elif 'webp' in header:
                    ctype = 'image/webp'
                return Response(raw, mimetype=ctype, headers={'Cache-Control': 'public, max-age=604800'})
            except Exception as e:
                logger.debug(f"poster data-url decode: {e}")

        # 4) External http(s) — short timeout only; skip known-dead hosts if we recently failed
        now = time_mod.time()
        last_fail = _POSTER_FAIL_CACHE.get(token, 0)
        skip_external = (now - last_fail) < 300  # 5 min cooldown after hard fail
        if not skip_external and (poster_url.startswith('http://') or poster_url.startswith('https://')):
            # Skip catbox/litterbox entirely when file_id exists (already tried above) — they are unreliable
            is_catbox = 'catbox.moe' in poster_url or 'litterbox' in poster_url
            if is_catbox and file_id:
                pass  # already preferred file_id; don't waste time on dead host
            else:
                try:
                    r = requests.get(
                        poster_url,
                        headers={'User-Agent': _UA},
                        timeout=4,  # hard 4s — never block dashboard
                        stream=True,
                    )
                    if r.status_code == 200 and r.content and len(r.content) > 100:
                        # Sniff magic bytes — never serve an HTML error page as an "image"
                        _h = r.content[:16]
                        _is_img = (_h[:3] == b'\xff\xd8\xff' or _h[:8] == b'\x89PNG\r\n\x1a\n'
                                   or _h[:6] in (b'GIF87a', b'GIF89a') or (_h[:4] == b'RIFF' and _h[8:12] == b'WEBP'))
                        if not _is_img:
                            _POSTER_FAIL_CACHE[token] = now
                            raise ValueError("not real image bytes")
                        ctype = r.headers.get('Content-Type') or 'image/jpeg'
                        if 'image' not in ctype and 'octet' not in ctype:
                            ctype = 'image/jpeg'
                        _poster_mem_set(token, r.content, ctype)
                        return Response(r.content, mimetype=ctype, headers={
                            'Cache-Control': 'public, max-age=604800',
                            'X-Poster': 'external',
                        })
                except Exception as e:
                    _POSTER_FAIL_CACHE[token] = now
                    # Log once at debug — not WARNING spam
                    logger.debug(f"poster proxy skip {poster_url[:60]}: {e}")

        # Nothing worked — return tiny placeholder (200) so UI never shows broken image / 404 flood
        return Response(_PLACEHOLDER_PNG, mimetype='image/png', headers={
            'Cache-Control': 'public, max-age=120',
            'X-Poster': 'placeholder',
        })
    except Exception as e:
        logger.error(f"api_poster_proxy: {e}")
        return Response(_PLACEHOLDER_PNG, mimetype='image/png', headers={'Cache-Control': 'no-cache'}), 200


@app.route('/api/public-config', methods=['GET'])
def public_config():
    """Non-secret branding for the dashboard UI."""
    return jsonify({
        "bot_username": (BOT_USERNAME or "").lstrip("@"),
        "bot_display_name": BOT_DISPLAY_NAME,
        "admin_username": admin_handle(),
        "dashboard_title": DASHBOARD_TITLE,
        "logo": DASHBOARD_LOGO or START_IMAGE_URL or "",
    })

@app.route('/api/movies', methods=['GET'])
def get_movies():
    if not mongodb_connected or movies_collection is None:
        return jsonify({"error": "Database offline"}), 503
    movies = list(movies_collection.find().sort("created_at", DESCENDING))
    for m in movies:
        m['_id'] = str(m['_id'])
        if 'poster_url' not in m:
            m['poster_url'] = ''
        # Prefer live proxy so dashboard never shows broken external hosts (catbox/litterbox dead links)
        token = m.get('token') or ''
        poster = (m.get('poster_url') or '').strip()
        has_file_id = bool(m.get('poster_file_id') or m.get('poster_tg_file_id'))
        if token and (poster or has_file_id):
            # Same-origin proxy always works in the admin UI
            m['poster_url'] = f"/api/poster/{token}"
            m['poster_display_url'] = m['poster_url']
        elif poster.startswith('/uploads/'):
            m['poster_url'] = poster  # relative — fine for same host
        elif poster.startswith('data:'):
            # Keep data URL; browsers can render it
            pass
    return jsonify(movies)

@app.route('/api/movies', methods=['POST'])
def create_movie():
    if not mongodb_connected or movies_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    title = data.get('title', 'Unknown Movie')
    existing_tokens = [doc['token'] for doc in movies_collection.find({}, {'token': 1})]
    if series_bundles_collection is not None:
        existing_tokens.extend([doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})])
    token = generate_count_token()
    price = data.get('price', get_default_price())
    poster_url = data.get('poster_url', '')
    if poster_url and poster_url.startswith("data:image"):
        try:
            hosted = upload_poster_with_fallback(poster_url, context="api_create")
            if hosted:
                poster_url = hosted
            else:
                poster_url = ""
        except Exception as e:
            logger.error(f"Poster conversion failed: {e}")
            poster_url = ""
    movie_data = {"token": token, "title": title, "channel_id": int(data['channel_id']), "message_id": int(data['message_id']), "private_link": data.get('private_link', ''), "poster_url": poster_url, "price": int(price), "category": data.get('category', 'movie'), "description": data.get('description', ''), "created_at": datetime.utcnow(), "downloads": 0}
    movies_collection.insert_one(movie_data)
    if poster_url:
        try:
            # Flask thread has NO running event loop — asyncio.create_task here
            # silently did nothing. Submit to the bot loop instead.
            run_async_background(post_to_channel_with_buttons(movie_title=movie_data['title'], price=movie_data['price'], image_urls=[poster_url], bot_link=f"https://t.me/{BOT_USERNAME}?start={token}", category=movie_data.get('category', 'movie'), caption=movie_data.get('description', '')))
        except Exception as e:
            logger.warning(f"Auto-post to channel failed: {e}")
    return jsonify({"success": True, "token": token, "price": price, "deep_link": f"https://t.me/{BOT_USERNAME}?start={token}", "movie": {k: v for k, v in movie_data.items() if k != '_id'}})

@app.route('/api/movies/<token>/send-to-channel', methods=['POST'])
def send_movie_to_channel(token):
    if not mongodb_connected or movies_collection is None:
        return jsonify({"error": "Database offline"}), 503
    movie = movies_collection.find_one({"token": token})
    if not movie:
        return jsonify({"error": "Movie not found"}), 404
    async def post_to_channel():
        try:
            await refresh_admin_premium_status(application.bot)
        except Exception:
            pass
        channel = PUBLIC_CHANNEL_USERNAME or CHANNEL_ID
        if not channel:
            return False
        poster_url = movie.get('poster_url', '')
        if poster_url and is_base64_data_url(poster_url):
            try:
                hosted_url = upload_poster_with_fallback(poster_url)
                if hosted_url:
                    poster_url = hosted_url
                else:
                    poster_url = ''
            except Exception as e:
                logger.error(f"Poster conversion error: {e}")
                poster_url = ''
        price = movie.get('price', get_default_price())
        category = movie.get('category', 'movie')
        bot_link = f"https://t.me/{BOT_USERNAME}?start={token}"
        title = movie.get('title', 'Untitled')
        description = movie.get('description', '')
        category_emoji = {"movie": "🎬", "series": "📺", "anime": "✨", "documentary": "🌍"}
        cat_emoji = category_emoji.get(category, "🎬")
        price_text = f"{price} KES" if price > 0 else f"FREE 🎁"
        post_text = apply_premium_emojis(f"{cat_emoji} <b>{escape_html(title)}</b>\n\n💰 <b>{price_text}</b> | 👑 Subscribers: <b>FREE</b>")
        if description:
            post_text += f"\n\n{escape_html(description)}"
        keyboard_buttons = [{"text": f"🚀 Get Movie — {price} KES", "url": bot_link}]
        keyboard = build_button_rows(keyboard_buttons, per_row=1)
        try:
            if poster_url and not is_base64_data_url(poster_url):
                try:
                    await application.bot.send_photo(chat_id=channel, photo=poster_url, caption=post_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
                except Exception as e1:
                    err = str(e1).lower()
                    if any(x in err for x in ('entity', 'parse', 'emoji')):
                        await application.bot.send_photo(chat_id=channel, photo=poster_url, caption=strip_premium_emojis(post_text), reply_markup=keyboard, parse_mode=ParseMode.HTML)
                    else:
                        await application.bot.send_photo(chat_id=channel, photo=poster_url)
                        await application.bot.send_message(chat_id=channel, text=strip_premium_emojis(post_text), reply_markup=keyboard, parse_mode=ParseMode.HTML)
            else:
                try:
                    await application.bot.send_message(chat_id=channel, text=post_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
                except Exception as e2:
                    err = str(e2).lower()
                    if any(x in err for x in ('entity', 'parse', 'emoji')):
                        await application.bot.send_message(chat_id=channel, text=strip_premium_emojis(post_text), reply_markup=keyboard, parse_mode=ParseMode.HTML)
                    else:
                        raise
            return True
        except Exception as e:
            logger.error(f"Channel post error: {e}")
            return False
    try:
        result = run_async(post_to_channel(), timeout=60)
        return jsonify({"success": bool(result)})
    except Exception as e:
        logger.error(f"Send to channel error: {e}")
        return jsonify({"error": str(e)}), 500

# ============ FAST ALBUM HELPERS (dashboard posts) ============
def _resolve_album_poster_sources(movies):
    """Return [(movie_doc, kind, value)] — kind 'ref' = file_id or http URL, 'bytes' = raw image.
    Old code only used file_id/base64, so movies with an http(s) or local /uploads/ poster
    were silently DROPPED from the album (posters not visible). Now every poster is used."""
    base = (SERVER_UPLOAD_URL_BASE or WEBHOOK_URL or '').strip().rstrip('/')
    resolved = []
    for m in movies:
        fid = (m.get('poster_file_id') or m.get('poster_tg_file_id') or '').strip()
        if fid:
            resolved.append((m, 'ref', fid))  # instant — Telegram already has the image
            continue
        p = (m.get('poster_url') or '').strip()
        if not p:
            continue
        if is_base64_data_url(p):
            try:
                _header, _, encoded = p.partition(',')
                resolved.append((m, 'bytes', base64.b64decode(encoded)))
            except Exception:
                pass
            continue
        if p.startswith('/uploads/') and base:
            p = f"{base}{p}"  # Telegram fetches it directly from this server
        if p.startswith('http://') or p.startswith('https://'):
            resolved.append((m, 'ref', p))
    return resolved

def _build_album_media_group(resolved, caption=None):
    """Build fresh InputMediaPhoto list (safe to call again for a retry — new streams each time)."""
    media_group = []
    for i, (_m, kind, value) in enumerate(resolved[:10]):  # Telegram album max = 10
        media = value if kind == 'ref' else InputFile(io.BytesIO(value), filename=f"poster_{i}.jpg")
        if i == 0 and caption:
            media_group.append(InputMediaPhoto(media=media, caption=caption[:1020], parse_mode=ParseMode.HTML))
        else:
            media_group.append(InputMediaPhoto(media=media))
    return media_group

def _save_poster_file_ids(pairs):
    """Persist file_ids captured from Telegram responses → future album sends are instant
    and posters always show (no dependency on external hosts)."""
    if not mongodb_connected or movies_collection is None:
        return
    for m, fid in pairs:
        try:
            tok = m.get('token')
            if tok and fid and not (m.get('poster_file_id') or m.get('poster_tg_file_id')):
                movies_collection.update_one({"token": tok}, {"$set": {"poster_file_id": fid}})
        except Exception:
            pass

async def _send_album_media(channel, resolved, caption=None):
    """Send poster album fast. On media-group failure: retry with plain caption,
    then fall back to individual photos — posters are NEVER silently dropped."""
    if not resolved:
        return []
    sent_messages = None
    try:
        sent_messages = await application.bot.send_media_group(
            chat_id=channel, media=_build_album_media_group(resolved, caption))
    except Exception as e:
        logger.error(f"Album media group failed: {e}")
        err = str(e).lower()
        if caption and any(x in err for x in ('entity', 'parse', 'emoji')):
            try:
                sent_messages = await application.bot.send_media_group(
                    chat_id=channel,
                    media=_build_album_media_group(resolved, strip_premium_emojis(caption)))
            except Exception as e2:
                logger.error(f"Album media group plain-caption retry failed: {e2}")
        else:
            # Bad/stale file_id or unreachable URL → convert http URLs to downloaded
            # bytes (Telegram never fetches → no blank cells), keep file_ids as-is.
            rebuilt = []
            for (m, k, v) in resolved:
                if k == 'ref' and str(v).startswith(('http://', 'https://')):
                    try:
                        dl = await asyncio.to_thread(requests.get, str(v), headers={'User-Agent': _UA}, timeout=12)
                        if dl.status_code == 200 and len(dl.content) > 100:
                            rebuilt.append((m, 'bytes', dl.content))
                            continue
                    except Exception:
                        pass
                    logger.warning(f"Poster URL dropped from album (undownloadable): {str(v)[:80]}")
                    continue
                rebuilt.append((m, k, v))
            if rebuilt and len(rebuilt) != len(resolved):
                try:
                    sent_messages = await application.bot.send_media_group(
                        chat_id=channel, media=_build_album_media_group(rebuilt, caption))
                    resolved = rebuilt
                except Exception as e2:
                    logger.error(f"Album rebuilt retry failed: {e2}")
    if sent_messages is None:
        # Last resort: individual photos so EVERY poster is still visible.
        # http URLs that failed inside the album are DOWNLOADED and sent as bytes —
        # Telegram never has to fetch them, so no blank white images, no drops.
        sent_messages = []
        for _m, kind, value in resolved[:10]:
            try:
                media = None
                if kind == 'bytes':
                    media = InputFile(io.BytesIO(value), filename="poster.jpg")
                elif kind == 'ref' and str(value).startswith(('http://', 'https://')):
                    try:
                        dl = await asyncio.to_thread(requests.get, str(value), headers={'User-Agent': _UA}, timeout=12)
                        if dl.status_code == 200 and len(dl.content) > 100:
                            media = InputFile(io.BytesIO(dl.content), filename="poster.jpg")
                    except Exception:
                        media = None
                    if media is None:
                        logger.warning(f"Poster URL undownloadable, skipped (no blank sent): {str(value)[:80]}")
                        continue
                else:
                    media = value  # Telegram file_id
                msg = await application.bot.send_photo(chat_id=channel, photo=media)
                sent_messages.append(msg)
                await asyncio.sleep(BOT_FAST_DELAY)
            except Exception as ex:
                logger.warning(f"Album single-photo fallback failed: {ex}")
    # Harvest file_ids from what Telegram returned → next album send is instant
    try:
        pairs = []
        for (m, _kind, _value), msg in zip(resolved, sent_messages or []):
            photos = getattr(msg, 'photo', None) or []
            if photos:
                pairs.append((m, photos[-1].file_id))
        if pairs:
            _save_poster_file_ids(pairs)
    except Exception as e:
        logger.debug(f"poster file_id harvest: {e}")
    return sent_messages or []

@app.route('/api/movies/send-album', methods=['POST'])
def send_movies_album_to_channel():
    if not mongodb_connected or movies_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json or {}
    tokens = data.get('tokens') or []
    channel = data.get('channel_id') or PUBLIC_CHANNEL_USERNAME or CHANNEL_ID
    if not tokens:
        return jsonify({"error": "No movies selected"}), 400
    if not channel:
        return jsonify({"error": "No target channel (provide channel_id)"}), 400
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        channel = str(channel).strip()
        if channel and not channel.startswith('@'):
            channel = f"@{channel}"
    # NO hardcoded limit — every selected movie is posted. One batched Mongo query
    # (instead of N find_one calls) keeps the dashboard "Send" fast for 20+ posters.
    wanted = [str(t) for t in tokens if t]
    movies = []
    if wanted:
        found = {str(d.get('token')): d for d in movies_collection.find({"token": {"$in": wanted}})}
        movies = [found[t] for t in wanted if t in found]  # keep the admin's selection order
        movies = dedupe_movie_items(movies)  # no repeated titles in the channel post
    if not movies:
        return jsonify({"error": "None of the selected movies exist"}), 404

    album_title = (data.get('title') or '').strip()
    if not album_title:
        album_title = clean_movie_title_for_button(movies[0].get('title', 'Movie Collection'))
        if len(movies) > 1:
            album_title = f"{album_title} Collection"
    # custom button texts from dashboard (token -> button label). Movie titles stay unchanged.
    custom_titles = data.get('custom_titles') or {}
    per_row = int(data.get('per_row') or 3)
    if per_row < 2:
        per_row = 2
    if per_row > 5:
        per_row = 5

    async def post_album():
        # Skip live premium refresh here — keeps dashboard "Send Selection" fast
        main_token = generate_count_token()
        n = len(movies)
        unit = int(movies[0].get('price', get_default_price()) or 10)
        try:
            prices = [int(m.get('price', unit) or unit) for m in movies]
            subtotal = sum(prices)
            unit = prices[0] if prices else unit
        except Exception:
            subtotal = n * unit
        discount = calculate_discount(n, unit)
        pay_total = max(0, subtotal - discount)
        if series_bundles_collection is not None and n > 1:
            episodes = []
            for m in movies:
                btn = custom_titles.get(m['token']) or m.get('button_title') or clean_movie_title_for_button(m.get('title', 'Movie'))
                episodes.append({"private_link": f"https://t.me/{BOT_USERNAME}?start={m['token']}", "title": m.get('title'), "button_title": btn, "channel_id": m.get('channel_id', ""), "message_id": m.get('message_id', 0), "file_id": m.get('file_id'), "file_type": m.get('file_type', 'video'), "token": m['token']})
            bundle_doc = {
                "token": main_token,
                "title": f"{album_title} ({n} movies)",
                "button_title": clean_movie_title_for_button(album_title),
                "price": pay_total,
                "original_price": subtotal,
                "discount": discount,
                "category": "series",
                "description": f"Collection of {n} movies",
                "poster_url": movies[0].get('poster_url', ''),
                "episodes": episodes,
                "episode_count": n,
                "created_at": datetime.utcnow(),
                "downloads": 0,
                "is_collection": True,
                "from_dashboard_album": True,
            }
            series_bundles_collection.insert_one(bundle_doc)
            try:
                cache_set_token(main_token, bundle_doc, 'series')
            except Exception:
                pass
        else:
            main_token = movies[0]['token']

        bot_un = BOT_USERNAME
        price_text = f"{pay_total} KES" if pay_total > 0 else "FREE"
        kb_rows = []
        btn_row = []
        # Telegram caps a keyboard at 100 buttons — leave room for GET ALL + never crash
        max_movie_buttons = 98 if n > 1 else 100
        for m in movies[:max_movie_buttons]:
            label = custom_titles.get(m['token']) or m.get('button_title') or clean_movie_title_for_button(m.get('title', 'Movie'))
            if len(label) > 28:
                label = label[:25] + "..."
            btn_row.append(InlineKeyboardButton(label, url=f"https://t.me/{bot_un}?start={m['token']}"))
            if len(btn_row) >= per_row:
                kb_rows.append(btn_row)
                btn_row = []
        if btn_row:
            kb_rows.append(btn_row)
        if n > 1:
            # Pay price only on button — no (−) suffix
            kb_rows.append([InlineKeyboardButton(f"GET ALL {n} ONCE — {price_text}", url=f"https://t.me/{bot_un}?start={main_token}")])
        kb = InlineKeyboardMarkup(kb_rows)

        # Caption: ONE arrow style for this whole album (next album gets next style)
        # Numbered 1. 2. 3. … and already deduped above — no repeated titles.
        album_arrow = next_channel_arrow()
        title_bits = [f"{album_arrow} {_bi}. {escape_html(m.get('title') or 'Movie')}" for _bi, m in enumerate(movies, 1)]
        titles_csv = " , ".join(title_bits)
        btn_caption = (
            f"{titles_csv}\n\n"
            f"● One movie {unit}KES  ● {n} movie(s) · {price_text}\n"
            f"👇 Tap a movie to get it, or GET ALL ONCE"
        )
        try:
            btn_caption = apply_premium_emojis(btn_caption)
        except Exception:
            pass

        # Channel album: file_id / http URL / base64 — EVERY movie poster is included & visible.
        # Telegram fetches URLs itself; file_ids are instant.
        # Telegram allows max 10 photos per media group, so >10 posters go out as
        # consecutive albums of up to 10 (NO selection cap — 20 posters = 2 albums),
        # then ONE buttons message with a button per movie + GET ALL ONCE.
        resolved = _resolve_album_poster_sources(movies)
        if resolved:
            chunks = [resolved[i:i + 10] for i in range(0, len(resolved), 10)]
            for chunk in chunks:
                await _send_album_media(channel, chunk)
                if len(chunks) > 1:
                    await asyncio.sleep(BOT_FAST_DELAY)  # keep album order, avoid flood limits
        # Titles + price + pointing-down live WITH the buttons message (premium emojis when owner has Premium)
        try:
            await application.bot.send_message(
                chat_id=channel,
                text=btn_caption[:4000],
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e_cap:
            err = str(e_cap).lower()
            if any(x in err for x in ('entity', 'parse', 'emoji')):
                await application.bot.send_message(
                    chat_id=channel,
                    text=strip_premium_emojis(btn_caption)[:4000],
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
            else:
                raise
        return main_token

    try:
        # Timeout scales with selection size: ~20s per 10-poster album + 60s base
        album_count = max(1, -(-len(movies) // 10))
        send_timeout = 60 + 20 * album_count
        main_token = run_async(post_album(), timeout=send_timeout)
        if not main_token:
            # Old bug: run_async returns None on timeout/failure but the dashboard
            # was told "success" with a null GET-ALL link.
            return jsonify({"error": "Album posting failed or timed out — check bot logs, then retry"}), 500
        return jsonify({"success": True, "get_all_token": main_token, "get_all_link": f"https://t.me/{BOT_USERNAME}?start={main_token}", "count": len(movies)})
    except Exception as e:
        logger.error(f"Send album error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/movies/bulk', methods=['POST'])
def bulk_upload_movies():
    if not mongodb_connected or movies_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    if not data or 'movies' not in data:
        return jsonify({"error": "No movies provided"}), 400
    movies_list = data['movies']
    default_price = data.get('default_price', get_default_price())
    results = []
    existing_tokens = [doc['token'] for doc in movies_collection.find({}, {'token': 1})]
    if series_bundles_collection is not None:
        existing_tokens.extend([doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})])
    for movie_data in movies_list:
        try:
            title = movie_data.get('title', 'Unknown Movie')
            token = generate_count_token()
            existing_tokens.append(token)
            private_link = movie_data.get('private_link', '')
            channel_id = movie_data.get('channel_id')
            message_id = movie_data.get('message_id')
            if not channel_id or not message_id:
                match = _TELEGRAM_LINK_RE.match(private_link)
                if match:
                    channel_id = f"-100{match.group(1)}"
                    message_id = int(match.group(2))
            if not channel_id or not message_id:
                results.append({"title": title, "success": False, "error": "Missing channel_id or message_id"})
                continue
            movie_doc = {"token": token, "title": title, "channel_id": int(channel_id), "message_id": int(message_id), "private_link": private_link, "price": int(movie_data.get('price', default_price)), "category": movie_data.get('category', 'movie'), "created_at": datetime.utcnow(), "downloads": 0}
            movies_collection.insert_one(movie_doc)
            results.append({"title": movie_doc['title'], "success": True, "token": token, "price": movie_doc['price']})
        except Exception as e:
            results.append({"title": movie_data.get('title', 'Unknown'), "success": False, "error": str(e)})
    successful = sum(1 for r in results if r['success'])
    return jsonify({"success": True, "total": len(movies_list), "successful": successful, "failed": len(movies_list) - successful, "results": results})

@app.route('/api/movies/bulk/template', methods=['GET'])
def download_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['title', 'private_link', 'channel_id', 'message_id', 'price', 'category'])
    writer.writerow(['Inception (2010)', 'https://t.me/c/1234567890/100', '', '', '10', 'movie'])
    writer.writerow(['Breaking Bad S01E01', 'https://t.me/c/1234567890/101', '', '', '15', 'series'])
    writer.writerow(['Naruto Movie 1', '', '-1001234567890', '102', '10', 'anime'])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode()), mimetype='text/csv', as_attachment=True, download_name='movie_template.csv')

@app.route('/api/movies/bulk/csv', methods=['POST'])
def bulk_upload_csv():
    if not mongodb_connected or movies_collection is None:
        return jsonify({"error": "Database offline"}), 503
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Empty filename"}), 400
    if not file.filename.endswith('.csv'):
        return jsonify({"error": "Only CSV files allowed"}), 400
    try:
        stream = io.StringIO(file.stream.read().decode('utf-8'))
        reader = csv.DictReader(stream)
        movies_list = []
        for row in reader:
            movies_list.append({"title": row.get('title', ''), "private_link": row.get('private_link', ''), "channel_id": row.get('channel_id', ''), "message_id": row.get('message_id', ''), "price": row.get('price', get_default_price()), "category": row.get('category', 'movie')})
        with app.test_client() as client:
            resp = client.post('/api/movies/bulk', json={'movies': movies_list, 'default_price': get_default_price()})
            return resp.get_json(), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/movies/<token>', methods=['GET'])
def get_movie(token):
    if not mongodb_connected or movies_collection is None:
        return jsonify({"error": "Database offline"}), 503
    movie = movies_collection.find_one({"token": token})
    if not movie:
        return jsonify({"error": "Movie not found"}), 404
    movie['_id'] = str(movie['_id'])
    if 'poster_url' not in movie:
        movie['poster_url'] = ''
    return jsonify(movie)

@app.route('/api/movies/<token>', methods=['PUT'])
def update_movie(token):
    if not mongodb_connected or movies_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
    update_fields = {}
    allowed_fields = ['title', 'description', 'price', 'poster_url', 'category']
    for field in allowed_fields:
        if field in data:
            if field == 'price':
                update_fields[field] = int(data[field])
            else:
                update_fields[field] = data[field]
    if not update_fields:
        return jsonify({"error": "No valid fields to update"}), 400
    update_fields['updated_at'] = datetime.utcnow()
    result = movies_collection.update_one({"token": token}, {"$set": update_fields})
    if result.matched_count == 0:
        return jsonify({"error": "Movie not found"}), 404
    return jsonify({"success": True, "updated": list(update_fields.keys())})

@app.route('/api/movies/<token>', methods=['DELETE'])
def delete_movie(token):
    if not mongodb_connected or movies_collection is None:
        return jsonify({"error": "Database offline"}), 503
    result = movies_collection.delete_one({"token": token})
    return jsonify({"success": result.deleted_count > 0})

@app.route('/api/series', methods=['GET'])
def get_series():
    if not mongodb_connected or series_bundles_collection is None:
        return jsonify([])
    bundles = list(series_bundles_collection.find().sort("created_at", DESCENDING))
    for b in bundles:
        b['_id'] = str(b['_id'])
        if 'poster_url' not in b:
            b['poster_url'] = ''
        b['is_bundle'] = True
        token = b.get('token') or ''
        poster = (b.get('poster_url') or '').strip()
        has_file_id = bool(b.get('poster_file_id') or b.get('poster_tg_file_id'))
        if token and (poster or has_file_id):
            b['poster_url'] = f"/api/poster/{token}"
    return jsonify(bundles)

@app.route('/api/series', methods=['POST'])
def create_series():
    if not mongodb_connected or series_bundles_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    logger.info(f"Received series creation data: {data}")
    title = data.get('title', 'Unknown Collection')
    existing_tokens = [doc['token'] for doc in movies_collection.find({}, {'token': 1})]
    if series_bundles_collection is not None:
        existing_tokens.extend([doc['token'] for doc in series_bundles_collection.find({}, {'token': 1})])
    token = generate_count_token()
    episodes = data.get('episodes', [])
    if not episodes or len(episodes) < 2:
        return jsonify({"error": "Bundle must have at least 2 movies"}), 400
    normalized_episodes = []
    for ep in episodes:
        title = ep.get('title') or ep.get('name') or 'Movie'
        token = ep.get('token') or ''
        file_id = ep.get('file_id') or ''
        channel_id = str(ep.get('channel_id') or '')
        message_id = int(ep.get('message_id') or 0)
        private_link = ep.get('private_link') or ''
        # Accept any episode that has a deliverable reference
        if channel_id and message_id:
            normalized_episodes.append({
                "private_link": private_link,
                "title": title,
                "channel_id": channel_id,
                "message_id": message_id,
                "token": token,
                "file_id": file_id,
                "file_type": ep.get('file_type', 'video'),
            })
        elif token or file_id or private_link:
            normalized_episodes.append({
                "private_link": private_link or (f"https://t.me/c/0/0" if not private_link else private_link),
                "title": title,
                "channel_id": channel_id or "0",
                "message_id": message_id or 0,
                "token": token,
                "file_id": file_id,
                "file_type": ep.get('file_type', 'video'),
            })
    if not normalized_episodes:
        return jsonify({"error": "No valid episodes provided. Select movies that exist in your library."}), 400
    poster_url = data.get('poster_url', '')
    if poster_url and poster_url.startswith("data:image"):
        try:
            hosted = upload_poster_with_fallback(poster_url, context="api_create_bundle")
            if hosted:
                poster_url = hosted
            else:
                poster_url = ""
        except Exception as e:
            logger.error(f"Bundle poster upload error: {e}")
            poster_url = ""
    series_data = {"token": token, "title": title, "price": int(data.get('price', 50)), "category": data.get('category', 'series'), "description": data.get('description', ''), "poster_url": poster_url, "episodes": normalized_episodes, "episode_count": len(normalized_episodes), "created_at": datetime.utcnow(), "downloads": 0}
    series_bundles_collection.insert_one(series_data)
    return jsonify({"success": True, "token": token, "price": series_data['price'], "episode_count": series_data['episode_count'], "poster_url": poster_url, "deep_link": f"https://t.me/{BOT_USERNAME}?start={token}"})

@app.route('/api/series/<token>', methods=['GET'])
def get_bundle(token):
    if not mongodb_connected or series_bundles_collection is None:
        return jsonify({"error": "Database offline"}), 503
    bundle = series_bundles_collection.find_one({"token": token})
    if not bundle:
        return jsonify({"error": "Bundle not found"}), 404
    bundle['_id'] = str(bundle['_id'])
    if 'poster_url' not in bundle:
        bundle['poster_url'] = ''
    return jsonify(bundle)

@app.route('/api/series/<token>', methods=['PUT'])
def update_bundle(token):
    if not mongodb_connected or series_bundles_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400
    update_fields = {}
    allowed_fields = ['title', 'description', 'price', 'poster_url', 'category']
    for field in allowed_fields:
        if field in data:
            if field == 'price':
                update_fields[field] = int(data[field])
            else:
                update_fields[field] = data[field]
    if not update_fields:
        return jsonify({"error": "No valid fields to update"}), 400
    update_fields['updated_at'] = datetime.utcnow()
    result = series_bundles_collection.update_one({"token": token}, {"$set": update_fields})
    if result.matched_count == 0:
        return jsonify({"error": "Bundle not found"}), 404
    return jsonify({"success": True, "updated": list(update_fields.keys())})

@app.route('/api/series/<token>', methods=['DELETE'])
def delete_series(token):
    if not mongodb_connected or series_bundles_collection is None:
        return jsonify({"error": "Database offline"}), 503
    result = series_bundles_collection.delete_one({"token": token})
    return jsonify({"success": result.deleted_count > 0})

@app.route('/api/series/<token>/send-to-channel', methods=['POST'])
def send_bundle_to_channel(token):
    if not mongodb_connected or series_bundles_collection is None:
        return jsonify({"error": "Database offline"}), 503
    series = series_bundles_collection.find_one({"token": token})
    if not series:
        return jsonify({"error": "Bundle not found"}), 404
    body = request.json or {}
    collection_name = (body.get('collection_name') or '').strip() or series.get('title', 'Collection')
    # Optionally persist new name
    if body.get('collection_name') and body.get('collection_name').strip():
        try:
            series_bundles_collection.update_one({"token": token}, {"$set": {"title": collection_name, "updated_at": datetime.utcnow()}})
        except Exception:
            pass
    async def post_bundle_to_channel():
        bot_link = f"https://t.me/{BOT_USERNAME}?start={token}"
        return await post_to_channel_with_buttons(
            movie_title=collection_name,
            price=series.get('price', 50),
            bot_link=bot_link,
            category=series.get('category', 'series'),
            caption=f"📦 {collection_name} — {series.get('episode_count', 0)} movies",
            button_style="movie_only"
        )
    try:
        result = run_async(post_bundle_to_channel(), timeout=60)
        return jsonify({"success": bool(result) if not isinstance(result, dict) else result.get('ok', False)})
    except Exception as e:
        logger.error(f"Send bundle to channel error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/series/<token>/send-as-buttons', methods=['POST'])
def send_series_as_buttons(token):
    if not mongodb_connected or series_bundles_collection is None:
        return jsonify({"error": "Database offline"}), 503
    series = series_bundles_collection.find_one({"token": token})
    if not series:
        return jsonify({"error": "Collection not found"}), 404
    data = request.get_json(silent=True) or {}
    target = data.get('channel') or data.get('target_channel') or PUBLIC_CHANNEL_USERNAME or CHANNEL_ID
    if not target:
        return jsonify({"success": False, "error": "No target channel. Pass channel (id or @username) in body."}), 400
    # Normalize: allow numeric string channel ids
    try:
        if isinstance(target, str) and target.lstrip('-').isdigit():
            target = int(target)
    except Exception:
        pass
    bot_user = (BOT_USERNAME or os.environ.get('BOT_USERNAME', '') or '').lstrip('@')
    episodes = series.get('episodes') or []
    per_row = int(data.get('per_row', 3) or 3)
    async def _post_parts():
        try:
            rows = []
            row = []
            for i, ep in enumerate(episodes[:50], 1):
                raw = ep.get('button_title') or ep.get('title') or f"PRT {i}"
                # Prefer short PRT label
                pm = re.search(r'(\d+)$', str(raw))
                if pm:
                    pt = f"PRT {pm.group(1)}"
                else:
                    pt = clean_movie_title_for_button(raw)
                    if len(pt) > 16:
                        pt = pt[:14] + "…"
                etok = ep.get('token')
                if not etok:
                    continue
                row.append(InlineKeyboardButton(pt, url=f"https://t.me/{bot_user}?start={etok}"))
                if len(row) == per_row:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            price = series.get('price', 0)
            price_txt = f"{price} KES" if price > 0 else "FREE 🎁"
            rows.append([InlineKeyboardButton(f"🎯 GET ALL {len(episodes)} ONCE — {price_txt}", url=f"https://t.me/{bot_user}?start={token}")])
            rows.append([InlineKeyboardButton("📢 Share Collection", url=f"https://t.me/share/url?url=https://t.me/{bot_user}?start={token}")])
            kb = InlineKeyboardMarkup(rows)
            caption = f"📦 <b>{escape_html(series.get('title', 'Collection'))}</b>\n\n{len(episodes)} parts | 💰 {price_txt}"
            poster = series.get('poster_url', '')
            if poster and str(poster).startswith('http'):
                try:
                    await application.bot.send_photo(chat_id=target, photo=poster, caption=caption, reply_markup=kb, parse_mode=ParseMode.HTML)
                    return {"ok": True}
                except Exception as e:
                    logger.error(f"send-as-buttons photo error: {e}")
            await application.bot.send_message(chat_id=target, text=caption, reply_markup=kb, parse_mode=ParseMode.HTML)
            return {"ok": True}
        except Exception as e:
            logger.error(f"send-as-buttons error: {e}")
            return {"ok": False, "error": str(e)}
    try:
        result = run_async(_post_parts(), timeout=60)
        if isinstance(result, dict) and result.get("ok"):
            return jsonify({"success": True})
        err = result.get("error") if isinstance(result, dict) else "Failed (check channel id / bot is admin)"
        return jsonify({"success": False, "error": err or "Chat not found – ensure bot is admin in the channel and channel id/username is correct"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/settings/price', methods=['GET'])
def get_price():
    return jsonify({"default_price": get_default_price()})

@app.route('/api/settings/price', methods=['POST'])
def update_price():
    if not mongodb_connected or settings_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    price = data.get('price', 10)
    set_default_price(int(price))
    return jsonify({"success": True, "default_price": price})

@app.route('/api/settings/subscription-prices', methods=['GET'])
def get_subscription_prices():
    plans = get_subscription_plans()
    return jsonify({"weekly": plans["weekly"]["price"], "2week": plans["2week"]["price"], "3week": plans["3week"]["price"], "monthly": plans["monthly"]["price"]})

@app.route('/api/settings/subscription-prices', methods=['POST'])
def update_subscription_prices():
    if not mongodb_connected or settings_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    weekly = data.get('weekly')
    _2week = data.get('2week')
    _3week = data.get('3week')
    monthly = data.get('monthly')
    if weekly is not None and int(weekly) >= 1:
        set_subscription_price("weekly", int(weekly))
    if _2week is not None and int(_2week) >= 1:
        set_subscription_price("2week", int(_2week))
    if _3week is not None and int(_3week) >= 1:
        set_subscription_price("3week", int(_3week))
    if monthly is not None and int(monthly) >= 1:
        set_subscription_price("monthly", int(monthly))
    plans = get_subscription_plans()
    return jsonify({"success": True, "weekly": plans["weekly"]["price"], "2week": plans["2week"]["price"], "3week": plans["3week"]["price"], "monthly": plans["monthly"]["price"]})

@app.route('/api/settings/subscription-limits', methods=['GET'])
def get_subscription_limits():
    plans = get_subscription_plans()
    return jsonify({"weekly": plans["weekly"]["max_movies"], "2week": plans["2week"]["max_movies"], "3week": plans["3week"]["max_movies"], "monthly": plans["monthly"]["max_movies"]})

@app.route('/api/settings/subscription-limits', methods=['POST'])
def update_subscription_limits():
    if not mongodb_connected or settings_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    weekly = data.get('weekly')
    _2week = data.get('2week')
    _3week = data.get('3week')
    monthly = data.get('monthly')
    if weekly is not None and int(weekly) >= 0:
        set_subscription_max_movies("weekly", int(weekly))
    if _2week is not None and int(_2week) >= 0:
        set_subscription_max_movies("2week", int(_2week))
    if _3week is not None and int(_3week) >= 0:
        set_subscription_max_movies("3week", int(_3week))
    if monthly is not None and int(monthly) >= 0:
        set_subscription_max_movies("monthly", int(monthly))
    plans = get_subscription_plans()
    return jsonify({"success": True, "weekly": plans["weekly"]["max_movies"], "2week": plans["2week"]["max_movies"], "3week": plans["3week"]["max_movies"], "monthly": plans["monthly"]["max_movies"]})

@app.route('/api/settings/card-payment', methods=['GET'])
def get_card_payment():
    return jsonify({"enabled": get_card_payment_setting()})

@app.route('/api/settings/card-payment', methods=['POST'])
def update_card_payment():
    if not mongodb_connected or settings_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    enabled = data.get('enabled', True)
    set_card_payment_setting(bool(enabled))
    return jsonify({"success": True, "enabled": bool(enabled)})

@app.route('/api/settings/subscription-enabled', methods=['GET'])
def get_subscription_enabled_api():
    return jsonify({"enabled": get_subscription_enabled()})

@app.route('/api/settings/subscription-enabled', methods=['POST'])
def update_subscription_enabled_api():
    if not mongodb_connected or settings_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    enabled = data.get('enabled', True)
    set_subscription_enabled(bool(enabled))
    return jsonify({"success": True, "enabled": bool(enabled)})

@app.route('/api/settings/payhero', methods=['GET'])
def get_payhero_settings_api():
    return jsonify({"enabled": get_payhero_enabled(), "channel_id": get_payhero_channel_id(), "channel_name": get_payhero_channel_name(), "configured": bool(PAYHERO_API_USERNAME and PAYHERO_API_PASSWORD)})

@app.route('/api/settings/payhero', methods=['POST'])
def update_payhero_settings_api():
    if not mongodb_connected or settings_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    if 'enabled' in data:
        set_payhero_enabled(bool(data['enabled']))
    if 'channel_id' in data:
        set_payhero_channel_id(int(data['channel_id']))
    return jsonify({"success": True, "enabled": get_payhero_enabled(), "channel_id": get_payhero_channel_id(), "channel_name": get_payhero_channel_name()})

@app.route('/api/settings/cart-enabled', methods=['GET'])
def get_cart_enabled_api():
    return jsonify({"enabled": get_cart_enabled()})

@app.route('/api/settings/cart-enabled', methods=['POST'])
def update_cart_enabled_api():
    if not mongodb_connected or settings_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    enabled = data.get('enabled', True)
    set_cart_enabled(bool(enabled))
    return jsonify({"success": True, "enabled": bool(enabled)})

@app.route('/api/settings/display-mode', methods=['GET'])
def get_display_mode_api():
    mode = get_display_mode()
    mode_names = {'button1': 'Button Style 1 (Compact)', 'button2': 'Button Style 2 (Detailed)', 'poster': 'Poster Style'}
    return jsonify({"mode": mode, "name": mode_names.get(mode, 'Button Style 2 (Detailed)')})

@app.route('/api/settings/display-mode', methods=['POST'])
def update_display_mode_api():
    if not mongodb_connected or settings_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    mode = data.get('mode', 'button2')
    if mode not in ['button1', 'button2', 'poster']:
        return jsonify({"error": "Invalid mode. Use 'button1', 'button2', or 'poster'"}), 400
    set_display_mode(mode)
    mode_names = {'button1': 'Button Style 1 (Compact)', 'button2': 'Button Style 2 (Detailed)', 'poster': 'Poster Style'}
    return jsonify({"success": True, "mode": mode, "name": mode_names.get(mode, 'Button Style 2 (Detailed)')})

@app.route('/api/settings/genre-buttons', methods=['GET'])
def get_genre_buttons_api():
    return jsonify({"enabled": get_genre_buttons_enabled()})

@app.route('/api/settings/genre-buttons', methods=['POST'])
def update_genre_buttons_api():
    if not mongodb_connected or settings_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    enabled = data.get('enabled', True)
    set_genre_buttons_enabled(bool(enabled))
    return jsonify({"success": True, "enabled": bool(enabled)})

@app.route('/api/settings/default-menu-style', methods=['GET'])
def get_default_menu_style_api():
    style = get_default_menu_style()
    return jsonify({"style": style, "name": MENU_STYLES.get(style, {}).get("name", "Confronter 1"), "styles": MENU_STYLES})

@app.route('/api/settings/default-menu-style', methods=['POST'])
def update_default_menu_style_api():
    if not mongodb_connected or settings_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    style = data.get('style', DEFAULT_MENU_STYLE)
    if style not in MENU_STYLES:
        return jsonify({"error": "Invalid style"}), 400
    set_default_menu_style(style)
    return jsonify({"success": True, "style": style, "name": MENU_STYLES.get(style, {}).get("name", "Confronter 1")})


@app.route('/api/check-password', methods=['POST'])
def check_password_api():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}
    password = str(data.get('password', ''))
    if password == DASHBOARD_PASSWORD:
        resp = jsonify({"success": True, "ok": True})
        resp.set_cookie(
            DASHBOARD_COOKIE_NAME,
            _dash_cookie_value(),
            max_age=60 * 60 * 24 * 7,
            httponly=True,
            samesite='Lax',
            secure=bool(request.is_secure),
            path='/',
        )
        return resp
    return jsonify({"success": False, "ok": False, "error": "Wrong password"}), 200

@app.route('/api/logout-dashboard', methods=['POST'])
def logout_dashboard_api():
    resp = jsonify({"success": True})
    resp.set_cookie(DASHBOARD_COOKIE_NAME, '', max_age=0, httponly=True, samesite='Lax', path='/')
    return resp

@app.route('/api/settings/getbot-enabled', methods=['GET'])
def get_getbot_enabled_api():
    return jsonify({"enabled": get_getbot_enabled()})

@app.route('/api/settings/getbot-enabled', methods=['POST'])
def update_getbot_enabled_api():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}
    enabled = bool(data.get('enabled', True))
    set_getbot_enabled(enabled)
    return jsonify({"ok": True, "enabled": enabled})

@app.route('/api/settings/premium-button-enabled', methods=['GET'])
def get_premium_button_enabled_api():
    return jsonify({"enabled": get_premium_button_enabled()})

@app.route('/api/settings/premium-button-enabled', methods=['POST'])
def update_premium_button_enabled_api():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        data = {}
    enabled = bool(data.get('enabled', True))
    set_premium_button_enabled(enabled)
    return jsonify({"ok": True, "enabled": enabled})

@app.route('/api/stats', methods=['GET'])
def get_stats():
    empty = {
        "total_movies": 0, "total_bundles": 0, "total_sales": 0, "revenue": 0,
        "total_users": 0, "mpesa_sales": 0, "card_sales": 0, "payhero_sales": 0,
        "bundle_sales": 0, "active_subscriptions": 0, "pending_messages": 0,
        "card_payment_enabled": True, "subscription_enabled": True,
        "payhero_enabled": False, "cart_enabled": True, "display_mode": "button2",
        "genre_buttons_enabled": True,
    }
    if not mongodb_connected:
        return jsonify(empty)
    try:
        total_movies = movies_collection.count_documents({}) if movies_collection is not None else 0
        total_bundles = series_bundles_collection.count_documents({}) if series_bundles_collection is not None else 0
        total_sales = payments_collection.count_documents({"status": "success"}) if payments_collection is not None else 0
        revenue = 0
        mpesa_count = 0
        card_count = 0
        payhero_count = 0
        bundle_sales = 0
        if payments_collection is not None:
            for p in payments_collection.find({"status": "success"}):
                try:
                    revenue += float(p.get("amount") or p.get("price") or 0)
                except (TypeError, ValueError):
                    pass
                method = str(p.get("payment_method") or p.get("provider") or p.get("gateway") or "").lower()
                # All phone STK (makamesco / payhero / mpesa) count as M-Pesa
                if method in ("card", "paystack", "stripe"):
                    card_count += 1
                else:
                    mpesa_count += 1
                    if "payhero" in method:
                        payhero_count += 1
                if p.get("series_token") or p.get("is_bundle") or p.get("is_cart"):
                    bundle_sales += 1
        revenue = int(revenue)
        total_users = get_user_count()
        active_subs = 0
        if subscriptions_collection is not None:
            active_subs = subscriptions_collection.count_documents({
                "status": "active", "expires_at": {"$gt": datetime.utcnow()}
            })
        pending_msgs = messages_collection.count_documents({"admin_replied": False}) if messages_collection is not None else 0
        default_style = get_default_menu_style()
        mode_names = {
            "button": "Button Style", "button1": "Button Style 1",
            "button2": "Button Style 2", "poster": "Poster Style",
        }
        return jsonify({
            "total_movies": total_movies, "total_bundles": total_bundles,
            "total_sales": total_sales, "revenue": revenue, "total_users": total_users,
            "mpesa_sales": mpesa_count, "card_sales": card_count,
            "payhero_sales": payhero_count, "bundle_sales": bundle_sales,
            "active_subscriptions": active_subs, "pending_messages": pending_msgs,
            "card_payment_enabled": get_card_payment_setting(),
            "subscription_enabled": get_subscription_enabled(),
            "payhero_enabled": get_payhero_enabled(),
            "cart_enabled": get_cart_enabled(),
            "display_mode": get_display_mode(),
            "display_mode_name": mode_names.get(get_display_mode(), "Button"),
            "genre_buttons_enabled": get_genre_buttons_enabled(),
            "default_menu_style": default_style,
            "default_menu_style_name": MENU_STYLES.get(default_style, {}).get("name", "Confronter 1"),
        })
    except Exception as e:
        logger.error(f"get_stats error: {e}")
        return jsonify(empty)

@app.route('/api/subscriptions', methods=['GET'])
def get_subscriptions():
    """Return ALL subscriptions (active + inactive/expired). Never auto-delete expired."""
    if not mongodb_connected or subscriptions_collection is None:
        return jsonify([])
    now = datetime.utcnow()
    # Mark expired actives as inactive without deleting
    try:
        subscriptions_collection.update_many(
            {"status": "active", "expires_at": {"$lte": now}},
            {"$set": {"status": "inactive", "updated_at": now}}
        )
    except Exception:
        pass
    # Return both active and inactive (exclude only explicitly deleted)
    subs = list(subscriptions_collection.find(
        {"status": {"$nin": ["deleted"]}}
    ).sort("expires_at", DESCENDING).limit(300))
    for s in subs:
        s['_id'] = str(s['_id'])
        exp = s.get('expires_at')
        s['expires_at'] = exp.isoformat() if isinstance(exp, datetime) else exp
        s['created_at'] = s['created_at'].isoformat() if isinstance(s.get('created_at'), datetime) else s.get('created_at')
        s['movies_watched'] = s.get('used_movies', 0)
        s['limit'] = s.get('max_movies', 0)
        # Derive display status
        status = s.get('status', 'active')
        if status == 'active' and isinstance(exp, datetime) and exp <= now:
            status = 'inactive'
        s['status'] = status
        try:
            if users_collection is not None:
                u = users_collection.find_one({"user_id": s.get("user_id")})
                if u:
                    s['username'] = u.get('username') or ''
                    s['name'] = u.get('first_name') or u.get('name') or ''
        except Exception:
            pass
    return jsonify(subs)

@app.route('/api/subscriptions/<int:user_id>/prolong', methods=['POST'])
def prolong_subscription(user_id):
    if not mongodb_connected or subscriptions_collection is None:
        return jsonify({"success": False, "error": "DB offline"}), 503
    data = request.json or {}
    days = int(data.get('days', 7) or 7)
    sub = subscriptions_collection.find_one(
        {"user_id": user_id},
        sort=[("expires_at", DESCENDING)],
    )
    if not sub:
        return jsonify({"success": False, "error": "No subscription found"}), 404
    new_expiry = sub.get("expires_at") or datetime.utcnow()
    if isinstance(new_expiry, str):
        try:
            new_expiry = datetime.fromisoformat(new_expiry.replace('Z', '+00:00'))
        except Exception:
            new_expiry = datetime.utcnow()
    # If already expired, start from now
    if new_expiry < datetime.utcnow():
        new_expiry = datetime.utcnow()
    new_expiry = new_expiry + timedelta(days=days)
    extra_limit = int(data.get("extra_movies") or data.get("extra_limit") or 0)
    update_fields = {
        "expires_at": new_expiry,
        "status": "active",
        "updated_at": datetime.utcnow(),
    }
    if extra_limit:
        # bump max_movies / limit so user can keep watching
        cur_max = int(sub.get("max_movies") or sub.get("limit") or 0)
        update_fields["max_movies"] = cur_max + extra_limit
        update_fields["limit"] = cur_max + extra_limit
    subscriptions_collection.update_one(
        {"_id": sub["_id"]},
        {"$set": update_fields},
    )
    # Notify user
    try:
        days_left = max(0, (new_expiry - datetime.utcnow()).days)
        async def _notify():
            await application.bot.send_message(
                chat_id=user_id,
                text=add_footer(
                    f"✅ <b>Subscription renewed</b>\n\n"
                    f"You have an active subscription.\n"
                    f"📅 Days remaining: <b>{days_left}</b>\n"
                    f"Expires: {new_expiry.strftime('%d %b %Y %H:%M')}\n\n"
                    f"Tap /start to continue watching movies with your plan."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Browse Movies", callback_data="watch_all_movies_0")]]),
            )
        run_async(_notify())
    except Exception as e:
        logger.error(f"prolong notify: {e}")
    return jsonify({"success": True, "expires_at": new_expiry.isoformat()})

@app.route('/api/subscriptions/<int:user_id>/end', methods=['POST'])
def end_subscription(user_id):
    if not mongodb_connected or subscriptions_collection is None:
        return jsonify({"success": False, "error": "DB offline"}), 503
    result = subscriptions_collection.update_many(
        {"user_id": user_id, "status": "active"},
        {"$set": {"status": "ended", "ended_at": datetime.utcnow(), "updated_at": datetime.utcnow()}}
    )
    return jsonify({"success": True, "ended": result.modified_count})


@app.route('/api/subscriptions/<int:user_id>/delete', methods=['POST', 'DELETE'])
def delete_subscription(user_id):
    """Admin permanently marks subscription as deleted (or removes it)."""
    if not mongodb_connected or subscriptions_collection is None:
        return jsonify({"success": False, "error": "DB offline"}), 503
    data = request.json or {}
    hard = bool(data.get('hard'))
    if hard:
        result = subscriptions_collection.delete_many({"user_id": user_id})
        return jsonify({"success": True, "deleted": result.deleted_count})
    result = subscriptions_collection.update_many(
        {"user_id": user_id, "status": {"$ne": "deleted"}},
        {"$set": {"status": "deleted", "deleted_at": datetime.utcnow(), "updated_at": datetime.utcnow()}}
    )
    return jsonify({"success": True, "deleted": result.modified_count})

@app.route('/api/payments', methods=['GET'])
def get_payments():
    """Fast payments list — uses stored titles only (no N+1 movie lookups)."""
    if not mongodb_connected or payments_collection is None:
        return jsonify([])
    try:
        payments = list(payments_collection.find().sort("created_at", DESCENDING).limit(120))
    except Exception as e:
        logger.error(f"get_payments query: {e}")
        return jsonify([])
    # Batch user lookup
    uids = list({int(p["user_id"]) for p in payments if p.get("user_id") is not None})
    users_map = {}
    if uids and users_collection is not None:
        try:
            for u in users_collection.find({"user_id": {"$in": uids}}, {"user_id": 1, "username": 1, "first_name": 1, "last_name": 1, "name": 1}):
                users_map[int(u["user_id"])] = u
        except Exception:
            pass
    # One-shot title map for cart tokens (fast $in, not N+1)
    title_map = {}
    try:
        all_toks = []
        for p in payments:
            toks = list(p.get("cart_tokens") or [])
            if p.get("movie_token"):
                toks.append(p.get("movie_token"))
            if p.get("series_token"):
                toks.append(p.get("series_token"))
            all_toks.extend(toks)
        uniq = list({t for t in all_toks if t})[:400]
        if uniq and movies_collection is not None:
            for md in movies_collection.find({"token": {"$in": uniq}}, {"token": 1, "title": 1}):
                title_map[md.get("token")] = md.get("title") or ""
        if uniq and series_bundles_collection is not None:
            for md in series_bundles_collection.find({"token": {"$in": uniq}}, {"token": 1, "title": 1}):
                title_map.setdefault(md.get("token"), md.get("title") or "")
    except Exception as e:
        logger.debug(f"title_map: {e}")
    out = []
    for p in payments:
        try:
            titles = p.get("titles") if isinstance(p.get("titles"), list) else []
            title = p.get("movie_title") or p.get("title") or ""
            cart_toks = p.get("cart_tokens") or []
            # Resolve titles from tokens when not stored (cart / single movie)
            # Fast path: do NOT do N+1 movie lookups here (that froze the Payments page).
            # Titles come from stored p.titles / movie_title. Expand can show cart_tokens count.
            if not titles:
                toks = list(cart_toks)
                if p.get("movie_token"):
                    toks.append(p.get("movie_token"))
                if p.get("series_token"):
                    toks.append(p.get("series_token"))
                titles = [title_map[t] for t in toks if t in title_map and title_map[t]]
            if not title and titles:
                title = ", ".join(str(x) for x in titles[:3] if x)
                if len(titles) > 3:
                    title += "…"
            if not title and p.get("plan"):
                title = f"Plan: {p.get('plan')}"
            if not title and cart_toks:
                title = f"Cart ×{len(cart_toks)}"
            if not title:
                title = "Item"
            doc = {
                "_id": str(p.get("_id", "")),
                "user_id": p.get("user_id"),
                "amount": p.get("amount") or p.get("price") or 0,
                "status": p.get("status") or "pending",
                "reference": p.get("reference"),
                "is_cart": bool(p.get("is_cart") or p.get("cart_tokens")),
                "plan": p.get("plan"),
                "movie_token": p.get("movie_token"),
                "series_token": p.get("series_token"),
                "cart_tokens": p.get("cart_tokens") or [],
                "titles": titles,
                "movie_title": title,
                "title": title,
                "payment_method": p.get("payment_method"),
                "created_at": p["created_at"].isoformat() if isinstance(p.get("created_at"), datetime) else p.get("created_at"),
            }
            uid = doc.get("user_id")
            if uid is not None and int(uid) in users_map:
                u = users_map[int(uid)]
                doc["username"] = u.get("username") or ""
                doc["first_name"] = u.get("first_name") or ""
                doc["last_name"] = u.get("last_name") or ""
                doc["name"] = u.get("name") or doc["first_name"]
            out.append(doc)
        except Exception as e:
            logger.debug(f"payment row skip: {e}")
            continue
    return jsonify(out)

@app.route('/api/users', methods=['GET'])
def get_users():
    if not mongodb_connected or users_collection is None:
        return jsonify([])
    users = list(users_collection.find().sort("last_activity", DESCENDING).limit(100))
    for u in users:
        u['_id'] = str(u['_id'])
        for field in ['last_activity', 'joined_at']:
            if isinstance(u.get(field), datetime):
                u[field] = u[field].isoformat()
    return jsonify(users)


@app.route('/api/users/<int:user_id>/mute', methods=['POST'])
def api_mute_user(user_id):
    if not mongodb_connected or users_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json or {}
    duration = str(data.get("duration") or "1h")
    if duration not in MUTE_DURATIONS:
        return jsonify({"success": False, "error": "invalid duration"}), 400
    info = apply_mute(user_id, duration)
    try:
        run_async(notify_user_access_change(user_id, "mute", info["label"]))
    except Exception:
        pass
    return jsonify({"success": True, "until": info["until"].isoformat(), "label": info["label"]})

@app.route('/api/users/<int:user_id>/ban', methods=['POST'])
def api_ban_user(user_id):
    if not mongodb_connected or users_collection is None:
        return jsonify({"error": "Database offline"}), 503
    apply_ban(user_id)
    try:
        run_async(notify_user_access_change(user_id, "ban"))
    except Exception:
        pass
    return jsonify({"success": True})

@app.route('/api/users/<int:user_id>/unban', methods=['POST'])
def api_unban_user(user_id):
    if not mongodb_connected or users_collection is None:
        return jsonify({"error": "Database offline"}), 503
    apply_unban(user_id)
    try:
        run_async(notify_user_access_change(user_id, "unban"))
    except Exception:
        pass
    return jsonify({"success": True})

@app.route('/api/users/<int:user_id>/hardblock', methods=['POST'])
def api_hardblock_user(user_id):
    if not mongodb_connected or users_collection is None:
        return jsonify({"error": "Database offline"}), 503
    apply_hard_block(user_id)
    return jsonify({"success": True})

@app.route('/api/users/<int:user_id>/block', methods=['POST'])
def block_user(user_id):
    if not mongodb_connected or users_collection is None:
        return jsonify({"error": "Database offline"}), 503
    apply_hard_block(user_id)
    return jsonify({"success": True})

@app.route('/api/users/<int:user_id>/unblock', methods=['POST'])
def unblock_user(user_id):
    if not mongodb_connected or users_collection is None:
        return jsonify({"error": "Database offline"}), 503
    apply_unban(user_id)
    try:
        run_async(notify_user_access_change(user_id, "unban"))
    except Exception:
        pass
    return jsonify({"success": True})

@app.route('/api/messages', methods=['GET'])
def get_messages():
    if not mongodb_connected or messages_collection is None:
        return jsonify([])
    msgs = list(messages_collection.find().sort("created_at", DESCENDING).limit(100))
    for m in msgs:
        m['_id'] = str(m['_id'])
        if isinstance(m.get('created_at'), datetime):
            m['created_at'] = m['created_at'].isoformat()
        # Enrich with user profile if available
        uid = m.get('user_id')
        if uid and users_collection is not None:
            try:
                u = users_collection.find_one({"user_id": int(uid)}, {"username": 1, "first_name": 1, "last_name": 1, "last_activity": 1, "last_interaction": 1})
                if u:
                    m['username'] = u.get('username') or m.get('username')
                    m['first_name'] = u.get('first_name') or m.get('first_name')
                    m['last_name'] = u.get('last_name') or m.get('last_name')
                    la = u.get('last_activity') or u.get('last_interaction')
                    if isinstance(la, datetime):
                        m['last_activity'] = la.isoformat()
                    elif la:
                        m['last_activity'] = str(la)
            except Exception:
                pass
    return jsonify(msgs)

@app.route('/api/messages/<msg_id>/reply', methods=['POST'])
def reply_to_message_api(msg_id):
    if not mongodb_connected or messages_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json or {}
    reply_text = (data.get('text') or data.get('message') or '').strip()
    # Accept user_id from body OR from the URL path (when path is the telegram user id)
    user_id = data.get('user_id') or data.get('chat_id')
    if not user_id:
        try:
            user_id = int(msg_id)
        except Exception:
            user_id = None
    plain = data.get('plain', True)  # default plain for dashboard replies
    if not reply_text:
        return jsonify({"error": "Missing text"}), 400
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    try:
        user_id = int(user_id)
    except Exception:
        return jsonify({"error": "Invalid user_id"}), 400
    async def send_reply():
        try:
            if plain:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=reply_text,
                    parse_mode=None,
                    disable_web_page_preview=True
                )
            else:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=add_footer(f"📨 <b>Reply from Admin:</b>\n\n{reply_text}"),
                    parse_mode=ParseMode.HTML
                )
            return True
        except Exception as e:
            logger.error(f"API reply error: {e}")
            return False
    success = run_async(send_reply())
    if success:
        try:
            # msg_id may be a real message _id or just the user_id (from payments page)
            try:
                oid = ObjectId(msg_id)
                messages_collection.update_one(
                    {"_id": oid},
                    {"$set": {"admin_replied": True, "replied_at": datetime.utcnow(), "reply_text": reply_text}}
                )
            except Exception:
                # fallback: mark latest message from this user
                messages_collection.update_one(
                    {"user_id": int(user_id)},
                    {"$set": {"admin_replied": True, "replied_at": datetime.utcnow(), "reply_text": reply_text}},
                    sort=[("created_at", -1)]
                )
        except Exception:
            pass
        return jsonify({"success": True})
    return jsonify({"error": "Failed to send reply"}), 500

@app.route('/api/broadcasts', methods=['GET'])
def get_broadcasts():
    if not mongodb_connected or broadcasts_collection is None:
        return jsonify([])
    broadcasts = list(broadcasts_collection.find().sort("created_at", DESCENDING).limit(20))
    for b in broadcasts:
        b['_id'] = str(b['_id'])
        for field in ['created_at', 'started_at', 'completed_at']:
            if isinstance(b.get(field), datetime):
                b[field] = b[field].isoformat()
    return jsonify(broadcasts)

@app.route('/api/broadcast', methods=['POST'])
def send_broadcast_api():
    if not mongodb_connected:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    text = data.get('text', '')
    if not text:
        return jsonify({"error": "No message text"}), 400
    users = get_all_users(skip_blocked=True)
    total = len(users)
    broadcast_doc = {"text": text, "admin_id": ADMIN_ID, "status": "sending", "total_users": total, "sent_count": 0, "failed_count": 0, "created_at": datetime.utcnow(), "started_at": datetime.utcnow()}
    broadcast_id = None
    if broadcasts_collection is not None:
        result = broadcasts_collection.insert_one(broadcast_doc)
        broadcast_id = str(result.inserted_id)
    sent = 0
    failed = 0
    async def do_broadcast():
        nonlocal sent, failed
        for i, user_doc in enumerate(users):
            uid = user_doc["user_id"]
            try:
                await application.bot.send_message(chat_id=uid, text=text, parse_mode=ParseMode.HTML)
                sent += 1
                if (i + 1) % 50 == 0:
                    await asyncio.sleep(BOT_FAST_DELAY)
            except Exception as e:
                nonlocal failed
                failed += 1
                if "blocked" in str(e).lower() and users_collection is not None:
                    users_collection.update_one({"user_id": uid}, {"$set": {"blocked": True}})
                continue
    run_async(do_broadcast())
    if broadcast_id and broadcasts_collection is not None:
        broadcasts_collection.update_one({"_id": ObjectId(broadcast_id)}, {"$set": {"status": "completed", "sent_count": sent, "failed_count": failed, "completed_at": datetime.utcnow()}})
    return jsonify({"success": True, "sent": sent, "failed": failed, "total": total})

@app.route('/api/cart/<int:user_id>', methods=['GET'])
def get_cart_api(user_id):
    if not mongodb_connected or cart_collection is None:
        return jsonify({"error": "Database offline"}), 503
    cart = cart_collection.find_one({"user_id": user_id})
    if not cart:
        return jsonify({"items": [], "total": 0})
    cart['_id'] = str(cart['_id'])
    items = cart.get('items', [])
    total = sum(item.get('price', 0) for item in items)
    return jsonify({"items": items, "total": total})

@app.route('/api/cart/<int:user_id>/add', methods=['POST'])
def add_to_cart_api(user_id):
    if not mongodb_connected or cart_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    token = data.get('token')
    if not token:
        return jsonify({"error": "Missing token"}), 400
    success = add_to_cart(user_id, token)
    if success:
        return jsonify({"success": True, "message": "Added to cart"})
    return jsonify({"success": False, "message": "Item already in cart or not found"})

@app.route('/api/cart/<int:user_id>/remove', methods=['POST'])
def remove_from_cart_api(user_id):
    if not mongodb_connected or cart_collection is None:
        return jsonify({"error": "Database offline"}), 503
    data = request.json
    token = data.get('token')
    if not token:
        return jsonify({"error": "Missing token"}), 400
    success = remove_from_cart(user_id, token)
    if success:
        return jsonify({"success": True, "message": "Removed from cart"})
    return jsonify({"success": False, "message": "Item not in cart"})

@app.route('/api/cart/<int:user_id>/clear', methods=['POST'])
def clear_cart_api(user_id):
    if not mongodb_connected or cart_collection is None:
        return jsonify({"error": "Database offline"}), 503
    clear_user_cart(user_id)
    return jsonify({"success": True, "message": "Cart cleared"})

@app.route('/api/payhero/callback', methods=['POST'])
def payhero_callback():
    try:
        data = request.get_json()
        if not data:
            logger.warning("PayHero callback: Empty payload")
            return jsonify({"status": "ok"}), 200
        logger.info(f"💳 PAYHERO CALLBACK received: {json_mod.dumps(data, indent=2)}")
        response_data = data.get("response", {})
        if not response_data and data:
            response_data = data
        status = str(response_data.get("Status", "")).lower()
        external_ref = response_data.get("ExternalReference", "")
        result_code = response_data.get("ResultCode", -1)
        mpesa_receipt = response_data.get("MpesaReceiptNumber", "")
        logger.info(f"PayHero callback: ref={external_ref}, status={status}, result_code={result_code}, receipt={mpesa_receipt}")
        if _bot_loop and _bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(process_payhero_callback(data), _bot_loop)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"PayHero callback error: {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "ok"}), 200

@app.route('/makamesco-callback', methods=['POST'])
@app.route('/api/makamesco/callback', methods=['POST'])
@app.route('/api/payments/callback', methods=['POST'])
@app.route('/callback', methods=['POST'])
def makamesco_callback():
    """Handle Makamesco / Nexus Pay payment callbacks. Always 200 so gateway does not retry forever."""
    try:
        data = request.get_json(silent=True) or {}
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        event = str(data.get("event") or payload.get("event") or "").lower()
        logger.info(f"💳 MAKAMESCO CALLBACK received event={event}: {json_mod.dumps(data)[:900]}")

        status = str(
            payload.get("status") or payload.get("Status") or data.get("status")
            or data.get("result") or payload.get("ResultDesc") or event or ""
        ).lower()
        external_ref = str(
            payload.get("reference") or payload.get("external_reference") or payload.get("ExternalReference")
            or payload.get("merchantReference") or payload.get("merchant_reference")
            or payload.get("accountReference") or payload.get("account_reference")
            or data.get("reference") or payload.get("BillRefNumber") or ""
        )
        checkout_id = str(
            payload.get("checkoutRequestId") or payload.get("CheckoutRequestID")
            or payload.get("checkout_request_id") or payload.get("checkoutRequestID")
            or data.get("checkoutRequestId") or data.get("CheckoutRequestID") or ""
        )
        merchant_req = str(
            payload.get("merchantRequestId") or payload.get("MerchantRequestID")
            or data.get("merchantRequestId") or ""
        )
        result_code = payload.get("result_code") or payload.get("ResultCode") or payload.get("code") or payload.get("responseCode")
        mpesa_receipt = str(
            payload.get("mpesaReceiptNumber") or payload.get("mpesa_receipt") or payload.get("MpesaReceiptNumber")
            or payload.get("receipt") or payload.get("transaction_id") or payload.get("MpesaReceiptNumber")
            or payload.get("TransID") or ""
        )
        amount = payload.get("amount") or data.get("amount") or payload.get("Amount") or 0
        phone = str(
            payload.get("phone") or payload.get("phoneNumber") or payload.get("msisdn")
            or payload.get("Phone") or data.get("phone") or ""
        )

        success = (
            "completed" in status or "success" in status or "paid" in status or status == "ok"
            or event in ("payment.completed", "subscription.activated", "stk.success")
            or str(result_code) in ("0", "00")
            or (mpesa_receipt and str(result_code) not in ("1", "1032", "1037"))
        )
        failed = (
            "failed" in status or "cancelled" in status or "declined" in status
            or event in ("payment.failed", "payment.cancelled")
        )
        logger.info(
            f"Makamesco callback: ref={external_ref}, checkout={checkout_id}, status={status}, "
            f"event={event}, success={success}, receipt={mpesa_receipt}, phone={phone}"
        )

        # Find pending by reference OR checkout_request_id OR phone (recent stk_sent)
        pending = None
        if pending_payhero_collection is not None:
            if external_ref:
                pending = pending_payhero_collection.find_one({"$or": [
                    {"payhero_ref": external_ref},
                    {"reference": external_ref},
                ]})
            if not pending and checkout_id:
                pending = pending_payhero_collection.find_one({"checkout_request_id": checkout_id})
            if not pending and phone:
                # normalize phone
                ph = phone.replace("+", "").replace(" ", "")
                if ph.startswith("0"):
                    ph = "254" + ph[1:]
                pending = pending_payhero_collection.find_one(
                    {"phone": {"$regex": ph[-9:]}, "status": {"$in": ["stk_sent", "awaiting_phone", "pending"]}},
                    sort=[("sent_at", -1), ("created_at", -1)],
                )
            if not pending:
                # last open makamesco pending
                pending = pending_payhero_collection.find_one(
                    {"provider": "makamesco", "status": {"$in": ["stk_sent", "awaiting_phone"]}},
                    sort=[("sent_at", -1), ("created_at", -1)],
                )

        if success and pending and _bot_loop and _bot_loop.is_running():
            ref = pending.get("reference") or external_ref or checkout_id or pending.get("payhero_ref")
            user_id = pending.get("user_id")
            chat_id = pending.get("chat_id") or user_id
            movie_token = pending.get("movie_token")
            series_token = pending.get("series_token")
            is_subscription = pending.get("is_subscription", False)
            plan = pending.get("plan")
            cart_tokens = pending.get("cart_tokens") or []
            is_cart = pending.get("is_cart") or bool(cart_tokens)
            logger.info(
                f"🚀 MAKAMESCO DELIVER: user={user_id} movie={movie_token} series={series_token} "
                f"sub={is_subscription} cart={is_cart} ref={ref}"
            )
            # Mark payment success permanently
            if payments_collection is not None and ref:
                payments_collection.update_one(
                    {"reference": ref},
                    {"$set": {
                        "status": "success",
                        "mpesa_receipt": mpesa_receipt,
                        "provider": "makamesco",
                        "checkout_request_id": checkout_id,
                        "phone": phone,
                        "amount": amount,
                        "user_id": user_id,
                        "movie_token": movie_token,
                        "series_token": series_token,
                        "cart_tokens": cart_tokens,
                        "is_subscription": is_subscription,
                        "plan": plan,
                        "paid_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                        "raw_callback": data,
                    }},
                    upsert=True,
                )
            pending_payhero_collection.update_one(
                {"_id": pending["_id"]},
                {"$set": {
                    "status": "completed",
                    "completed_at": datetime.utcnow(),
                    "mpesa_receipt": mpesa_receipt,
                    "checkout_request_id": checkout_id or pending.get("checkout_request_id"),
                }},
            )
            if user_id:
                clear_pending_payment_tracker(user_id)

            async def _deliver():
                try:
                    if is_subscription and plan:
                        create_subscription(user_id, plan, ref or checkout_id)
                        plan_info = get_subscription_plans().get(plan, {})
                        plan_name = plan_info.get("name", (plan or "Plan").title() + " Pass")
                        days = plan_info.get("days", 7)
                        expires = datetime.utcnow() + timedelta(days=days)
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=add_footer(
                                f"🎉 <b>{plan_name} Activated!</b>\n\n"
                                f"✅ Payment confirmed.\n"
                                f"📅 Expires: {expires.strftime('%d %b %Y %H:%M')}\n\n"
                                f"You now have access until the expiry date.\n"
                                f"Use /start or /myplanmovies to browse & rewatch."
                            ),
                            parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("Browse Movies", callback_data="watch_all_movies_0")],
                                [InlineKeyboardButton("My Plan Movies", callback_data="myplanmovies")],
                            ]),
                        )
                    elif is_cart and cart_tokens:
                        await auto_deliver_cart(user_id, [{"token": t} for t in cart_tokens])
                    elif series_token:
                        await auto_deliver_series(user_id, series_token)
                    elif movie_token:
                        await auto_deliver_movie(user_id, movie_token)
                    else:
                        await application.bot.send_message(
                            chat_id=chat_id,
                            text=add_footer("✅ Payment received! Use /start to get your content."),
                            parse_mode=ParseMode.HTML,
                        )
                except Exception as de:
                    logger.error(f"Makamesco deliver error: {de}")
                    logger.error(traceback.format_exc())
                    # Retry series/movie once more after short pause
                    try:
                        await asyncio.sleep(1.5)
                        if series_token:
                            ok = await auto_deliver_series(user_id, series_token)
                            if ok:
                                return
                        elif movie_token:
                            ok = await auto_deliver_movie(user_id, movie_token)
                            if ok:
                                return
                        elif is_cart and cart_tokens:
                            await auto_deliver_cart(user_id, [{"token": t} for t in cart_tokens])
                            return
                    except Exception as de2:
                        logger.error(f"Makamesco deliver retry failed: {de2}")
                    try:
                        await application.bot.send_message(
                            chat_id=chat_id or user_id,
                            text=add_footer(
                                "✅ <b>Payment confirmed!</b>\n\n"
                                "⚠️ Auto-delivery hit a temporary error.\n"
                                "Tap the same collection link again or use /mymovies — "
                                "<b>you will not be charged again</b>."
                            ),
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass

            asyncio.run_coroutine_threadsafe(_deliver(), _bot_loop)
            logger.info(f"✅ Makamesco delivery scheduled for user={user_id}")
        elif success and not pending:
            # Recovery: Paystack STK refs sometimes arrive on Makamesco route
            recovered = False
            try:
                if pending_stk_collection is not None and external_ref:
                    pstk = pending_stk_collection.find_one({"$or": [
                        {"stk_reference": external_ref},
                        {"reference": external_ref},
                        {"reference": external_ref.replace("stk_", "", 1) if external_ref.startswith("stk_") else external_ref},
                    ]})
                    if pstk and pstk.get("status") not in ("completed", "success"):
                        user_id = pstk.get("user_id")
                        plan = pstk.get("plan")
                        is_subscription = pstk.get("is_subscription")
                        movie_token = pstk.get("movie_token")
                        series_token = pstk.get("series_token")
                        cart_tokens = pstk.get("cart_tokens") or []
                        is_cart = pstk.get("is_cart") or bool(cart_tokens)
                        chat_id = pstk.get("chat_id") or user_id
                        pending_stk_collection.update_one({"_id": pstk["_id"]}, {"$set": {"status": "completed"}})
                        if payments_collection is not None:
                            payments_collection.update_one(
                                {"$or": [{"reference": external_ref}, {"reference": pstk.get("reference")}]},
                                {"$set": {"status": "success", "completed_at": datetime.utcnow()}},
                            )
                        clear_pending_payment_tracker(user_id)
                        # Flask handler is sync — schedule async delivery on bot loop
                        async def _recover_deliver():
                            try:
                                if is_subscription and plan:
                                    create_subscription(user_id, plan, external_ref)
                                    try:
                                        await notify_subscription_activated(user_id, plan, application)
                                    except Exception:
                                        pass
                                elif is_cart and cart_tokens:
                                    await auto_deliver_cart(user_id, [{"token": t} for t in cart_tokens])
                                elif series_token:
                                    await auto_deliver_series(user_id, series_token)
                                elif movie_token:
                                    await auto_deliver_movie(user_id, movie_token)
                            except Exception as de:
                                logger.error(f"recover deliver error: {de}")
                        try:
                            if _bot_loop is not None:
                                asyncio.run_coroutine_threadsafe(_recover_deliver(), _bot_loop)
                            else:
                                run_async(_recover_deliver())
                        except Exception as se:
                            logger.error(f"schedule recover deliver: {se}")
                        recovered = True
                        logger.info(f"Recovered Paystack-via-root delivery user={user_id} ref={external_ref}")
            except Exception as re_err:
                logger.error(f"pending recovery: {re_err}")
            if not recovered:
                logger.warning(f"Makamesco success but NO pending found ref={external_ref} checkout={checkout_id} phone={phone}")
            # Still mark payments if we have a ref
            if payments_collection is not None and external_ref:
                payments_collection.update_one(
                    {"reference": external_ref},
                    {"$set": {"status": "success", "mpesa_receipt": mpesa_receipt, "provider": "makamesco",
                              "updated_at": datetime.utcnow(), "raw_callback": data}},
                    upsert=True,
                )
        elif failed:
            if pending:
                pending_payhero_collection.update_one(
                    {"_id": pending["_id"]},
                    {"$set": {"status": "failed", "updated_at": datetime.utcnow()}},
                )
            if payments_collection is not None and (external_ref or (pending and pending.get("reference"))):
                payments_collection.update_one(
                    {"reference": external_ref or pending.get("reference")},
                    {"$set": {"status": "failed", "provider": "makamesco", "updated_at": datetime.utcnow(), "raw_callback": data}},
                    upsert=True,
                )
        return jsonify({"status": "ok", "received": True}), 200
    except Exception as e:
        logger.error(f"Makamesco callback error: {e}")
        logger.error(traceback.format_exc())
        return jsonify({"status": "ok"}), 200

@app.route('/api/paystack/webhook', methods=['POST'])
def paystack_webhook():
    payload = request.get_data()
    signature = request.headers.get('x-paystack-signature')
    expected = hmac.new(PAYSTACK_SECRET_KEY.encode(), payload, hashlib.sha512).hexdigest()
    if signature != expected:
        logger.warning("Paystack webhook: Invalid signature")
        return jsonify({"error": "Invalid signature"}), 400
    data = request.json
    event = data.get('event')
    logger.info(f"🔔 PAYSTACK WEBHOOK: event={event}")
    if event == 'charge.success':
        charge_data = data['data']
        reference = charge_data['reference']
        amount_kobo = charge_data.get('amount', 0)
        channel = charge_data.get('channel', 'card')
        webhook_metadata = charge_data.get('metadata', {}) or {}
        if not webhook_metadata and payments_collection is not None:
            payment_record = payments_collection.find_one({"reference": reference})
            if payment_record:
                webhook_metadata = {"user_id": payment_record.get("user_id"), "movie_token": payment_record.get("movie_token"), "series_token": payment_record.get("series_token"), "cart_tokens": payment_record.get("cart_tokens", []), "chat_id": payment_record.get("chat_id"), "is_subscription": payment_record.get("is_subscription", False), "plan": payment_record.get("plan")}
        user_id = webhook_metadata.get('user_id')
        movie_token = webhook_metadata.get('movie_token')
        series_token = webhook_metadata.get('series_token')
        cart_tokens = webhook_metadata.get('cart_tokens', [])
        is_subscription = webhook_metadata.get('is_subscription', False)
        plan = webhook_metadata.get('plan')
        chat_id = webhook_metadata.get('chat_id', user_id)
        logger.info(f"🚀 WEBHOOK: ref={reference}, user={user_id}, movie={movie_token}, series={series_token}, cart={len(cart_tokens)} items")
        if user_id:
            coro = _handle_webhook_payment_success(reference=reference, user_id=user_id, chat_id=chat_id, movie_token=movie_token, is_subscription=is_subscription, plan=plan, amount_kobo=amount_kobo, channel=channel, series_token=series_token, cart_tokens=cart_tokens)
            result = run_async(coro)
            logger.info(f"✅ WEBHOOK result={result}")
    return jsonify({"status": "ok"})

@app.route('/webhook', methods=['GET', 'POST', 'HEAD'])
def telegram_webhook():
    """Telegram update endpoint. GET = health check; POST = process update."""
    if request.method in ('GET', 'HEAD'):
        loop_ok = bool(_bot_loop and _bot_loop.is_running())
        return jsonify({
            "ok": True,
            "service": "telegram-webhook",
            "bot_loop_running": loop_ok,
            "app_started": bool(getattr(application, "_initialized", True)),
        }), 200
    try:
        if _bot_loop is None or not _bot_loop.is_running():
            logger.error("Webhook: bot event loop NOT running — update dropped")
            return 'OK', 200
        json_data = request.get_json(silent=True)
        if not json_data:
            logger.warning("Webhook: empty/non-JSON body")
            return 'OK', 200
        update = Update.de_json(json_data, application.bot)
        if update is None:
            logger.warning("Webhook: Update.de_json returned None")
            return 'OK', 200
        # Fast diagnostic log so we can see updates arriving
        uid = None
        kind = "unknown"
        try:
            if update.message:
                kind = "message"
                uid = update.message.from_user.id if update.message.from_user else None
                txt = (update.message.text or "")[:40]
                logger.info(f"Webhook RX message uid={uid} text={txt!r}")
            elif update.callback_query:
                kind = "callback"
                uid = update.callback_query.from_user.id if update.callback_query.from_user else None
                logger.info(f"Webhook RX callback uid={uid} data={update.callback_query.data!r}")
            elif update.channel_post:
                kind = "channel_post"
                logger.info(f"Webhook RX channel_post chat={getattr(update.channel_post.chat, 'id', None)}")
            else:
                logger.info(f"Webhook RX update_id={update.update_id} kind=other")
        except Exception:
            logger.info(f"Webhook RX update_id={getattr(update, 'update_id', None)}")

        async def process_update():
            try:
                await application.process_update(update)
            except Exception as pe:
                logger.error(f"process_update failed: {pe}")
                logger.error(traceback.format_exc())

        asyncio.run_coroutine_threadsafe(process_update(), _bot_loop)
        return 'OK', 200
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        logger.error(traceback.format_exc())
        return 'OK', 200



@app.route('/api/settings/ban-fee', methods=['GET', 'POST'])
def api_ban_fee():
    if request.method == 'GET':
        enabled, amount = get_ban_fee_settings()
        return jsonify({"enabled": enabled, "amount": amount})
    data = request.json or {}
    if settings_collection is not None:
        if "enabled" in data:
            settings_collection.update_one(
                {"key": "ban_fee_enabled"},
                {"$set": {"value": bool(data.get("enabled")), "updated_at": datetime.utcnow()}},
                upsert=True,
            )
        if "amount" in data:
            try:
                amt = int(data.get("amount") or 300)
            except Exception:
                amt = 300
            settings_collection.update_one(
                {"key": "ban_fee_amount"},
                {"$set": {"value": amt, "updated_at": datetime.utcnow()}},
                upsert=True,
            )
    enabled, amount = get_ban_fee_settings()
    return jsonify({"success": True, "enabled": enabled, "amount": amount})

@app.route('/api/settings/show-watched-remaining', methods=['GET', 'POST'])
def api_show_watched_remaining():
    if request.method == 'GET':
        return jsonify({"enabled": get_show_watched_remaining()})
    data = request.json or {}
    enabled = data.get("enabled", True)
    set_show_watched_remaining(bool(enabled))
    return jsonify({"success": True, "enabled": get_show_watched_remaining()})


@app.route('/api/settings/genre-style', methods=['GET', 'POST'])
def api_genre_style():
    if request.method == 'GET':
        return jsonify({"style": get_genre_style(), "enabled": get_genre_buttons_enabled()})
    data = request.json or {}
    if "style" in data:
        set_genre_style(data.get("style") or "compact")
    if "enabled" in data:
        set_genre_buttons_enabled(bool(data.get("enabled")))
    return jsonify({"success": True, "style": get_genre_style(), "enabled": get_genre_buttons_enabled()})


@app.route('/api/settings/reengage', methods=['GET', 'POST'])
def api_reengage_settings():
    """Dashboard: on/off + hours + message text for inactive-user auto message."""
    if request.method == 'GET':
        enabled, msg, hours = get_reengage_settings()
        return jsonify({
            "enabled": enabled,
            "text": msg,
            "hours": hours,
            "presets": [
                {"label": "5 minutes (test)", "hours": 0.083},
                {"label": "1 hour", "hours": 1},
                {"label": "2 hours", "hours": 2},
                {"label": "6 hours", "hours": 6},
                {"label": "12 hours", "hours": 12},
                {"label": "24 hours", "hours": 24},
                {"label": "2 days", "hours": 48},
                {"label": "3 days", "hours": 72},
                {"label": "7 days", "hours": 168},
            ],
        })
    data = request.json or {}
    if "enabled" in data:
        set_reengage_enabled(bool(data.get("enabled")))
    if "text" in data and data.get("text") is not None:
        set_reengage_text(str(data.get("text")))
    if "hours" in data:
        set_reengage_hours(data.get("hours"))
    enabled, msg, hours = get_reengage_settings()
    return jsonify({"success": True, "enabled": enabled, "text": msg, "hours": hours})

@app.route('/health')
def health():
    return jsonify({"status": "ok", "mongodb": mongodb_connected, "payhero_configured": bool(PAYHERO_API_USERNAME and PAYHERO_API_PASSWORD), "payhero_enabled": get_payhero_enabled(), "timestamp": datetime.utcnow().isoformat()})

@app.route('/api/admin/db-status', methods=['GET'])
def db_status():
    if not mongodb_connected:
        return jsonify({"connected": False, "error": "MongoDB not connected"}), 503
    try:
        status = {"connected": True, "collections": {}}
        for coll_name, coll in [("movies", movies_collection), ("payments", payments_collection), ("users", users_collection), ("settings", settings_collection), ("subscriptions", subscriptions_collection), ("series_bundles", series_bundles_collection), ("messages", messages_collection), ("broadcasts", broadcasts_collection), ("user_fonts", user_fonts_collection), ("user_settings", user_settings_collection), ("pending_payhero", pending_payhero_collection), ("carts", cart_collection), ("pending_payment_tracker", pending_payment_tracker), ("channels", channels_collection), ("bot_requests", bot_requests_collection)]:
            if coll is not None:
                status["collections"][coll_name] = coll.count_documents({})
        return jsonify(status)
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 500

@app.route('/api/admin/db-clear', methods=['POST'])
def db_clear():
    if not mongodb_connected:
        return jsonify({"error": "Database offline"}), 503
    data = request.json or {}
    confirm = data.get('confirm', False)
    if not confirm:
        return jsonify({"error": "Set confirm=true to clear all data"}), 400
    try:
        cleared = {}
        for coll_name, coll in [("movies", movies_collection), ("payments", payments_collection), ("users", users_collection), ("pending_stk", pending_stk_collection), ("pending_payhero", pending_payhero_collection), ("subscriptions", subscriptions_collection), ("series_bundles", series_bundles_collection), ("messages", messages_collection), ("broadcasts", broadcasts_collection), ("admin_sessions", admin_sessions_collection), ("user_fonts", user_fonts_collection), ("user_settings", user_settings_collection), ("carts", cart_collection), ("pending_payment_tracker", pending_payment_tracker), ("channels", channels_collection), ("bot_requests", bot_requests_collection)]:
            if coll is not None:
                result = coll.delete_many({})
                cleared[coll_name] = result.deleted_count
        init_db()
        return jsonify({"success": True, "cleared": cleared})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/db-drop-collection', methods=['POST'])
def db_drop_collection():
    if not mongodb_connected:
        return jsonify({"error": "Database offline"}), 503
    data = request.json or {}
    collection_name = data.get('collection', '')
    confirm = data.get('confirm', False)
    if not collection_name or not confirm:
        return jsonify({"error": "Provide collection name and confirm=true"}), 400
    try:
        if collection_name in db.list_collection_names():
            db.drop_collection(collection_name)
            return jsonify({"success": True, "message": f"Collection {collection_name} dropped"})
        return jsonify({"error": f"Collection {collection_name} not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/channel-post/album', methods=['POST'])
def channel_post_album():
    channel = request.form.get('channel')
    caption = request.form.get('text') or request.form.get('caption', '')
    buttons_text = request.form.get('buttons', '')
    per_row = int(request.form.get('per_row', 3))
    delay_seconds = float(request.form.get('delay', 0.15))
    if not channel:
        return jsonify({"success": False, "error": "No target channel specified"}), 400
    image_files = []
    image_count = int(request.form.get('image_count', 0))
    for i in range(image_count):
        img_key = f'image{i}'
        if img_key in request.files:
            image_files.append(request.files[img_key])
    if not image_files:
        return jsonify({"success": False, "error": "No images provided"}), 400
    buttons = parse_buttons_from_text(buttons_text)
    keyboard = build_button_rows(buttons, per_row=per_row) if buttons else None
    if len(image_files) == 1:
        async def _do_single():
            return await send_single_photo_to_channel(image_files[0], channel, caption, keyboard)
        result = run_async(_do_single(), timeout=60)
    else:
        async def _do_multiple():
            return await send_multiple_photos_to_channel(image_files, channel, caption, keyboard, delay_seconds)
        result = run_async(_do_multiple(), timeout=180)
    if result is None:
        return jsonify({"success": False, "error": "Async operation timed out"}), 500
    if isinstance(result, dict):
        if result.get('ok'):
            return jsonify({"success": True, **result})
        else:
            return jsonify({"success": False, **result}), 500
    return jsonify({"success": bool(result)})

@app.route('/api/channel-post/caption', methods=['POST'])
def channel_post_caption():
    channel = request.form.get('channel')
    text = request.form.get('text') or request.form.get('caption', '')
    if not channel:
        return jsonify({"success": False, "error": "No target channel specified"}), 400
    if not text:
        return jsonify({"success": False, "error": "No caption text provided"}), 400
    async def _do_post():
        await application.bot.send_message(chat_id=channel, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return {"ok": True}
    result = run_async(_do_post(), timeout=60)
    if result is None:
        return jsonify({"success": False, "error": "Async operation timed out"}), 500
    return jsonify(result)

@app.route('/api/channel-post/buttons', methods=['POST'])
def channel_post_buttons():
    channel = request.form.get('channel')
    buttons_text = request.form.get('buttons', '')
    per_row = int(request.form.get('per_row', 3))
    if not channel:
        return jsonify({"success": False, "error": "No target channel specified"}), 400
    if not buttons_text:
        return jsonify({"success": False, "error": "No buttons data provided"}), 400
    buttons = parse_buttons_from_text(buttons_text)
    keyboard = build_button_rows(buttons, per_row=per_row) if buttons else None
    async def _do_post():
        await application.bot.send_message(chat_id=channel, text=f"🔗 Tap a button below ↓", reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return {"ok": True}
    result = run_async(_do_post(), timeout=60)
    if result is None:
        return jsonify({"success": False, "error": "Async operation timed out"}), 500
    return jsonify(result)

@app.route('/api/channel-post', methods=['POST'])
def channel_post():
    channel = request.form.get('channel')
    caption = request.form.get('text') or request.form.get('caption', '')
    buttons_text = request.form.get('buttons', '')
    per_row = int(request.form.get('per_row', 3))
    delay_seconds = float(request.form.get('delay', 0.15))
    buttons = parse_buttons_from_text(buttons_text)
    keyboard = build_button_rows(buttons, per_row=per_row) if buttons else None
    image_files = []
    image_count = int(request.form.get('image_count', 0))
    for i in range(image_count):
        img_key = f'image{i}'
        if img_key in request.files:
            image_files.append(request.files[img_key])
    image_urls = []
    if not image_files:
        try:
            json_data = request.get_json(silent=True) or {}
            image_urls = json_data.get('image_urls', [])
            caption = caption or json_data.get('text') or json_data.get('caption', '')
            buttons_text = buttons_text or json_data.get('buttons', '')
            per_row = int(json_data.get('per_row', 3)) if not per_row else per_row
            delay_seconds = float(json_data.get('delay', 0.15))
            if buttons_text and not buttons:
                buttons = parse_buttons_from_text(buttons_text)
                keyboard = build_button_rows(buttons, per_row=per_row) if buttons else None
        except Exception:
            pass
    else:
        image_urls = []
    if not channel:
        return jsonify({"success": False, "error": "No target channel specified"}), 400
    if not caption and not image_files and not image_urls:
        return jsonify({"success": False, "error": "No caption or images provided"}), 400
    if len(image_files) == 1:
        result = run_async(send_single_photo_to_channel(image_files[0], channel, caption, keyboard), timeout=60)
        if result is None:
            return jsonify({"success": False, "error": "Async operation timed out"}), 500
        if isinstance(result, dict):
            if result.get('ok'):
                return jsonify({"success": True, **result, "single_photo": True, "caption_sent": True, "buttons_sent": bool(keyboard)})
            return jsonify({"success": False, **result}), 500
        return jsonify({"success": bool(result)})
    elif len(image_files) >= 2:
        result = run_async(send_multiple_photos_to_channel(image_files, channel, caption, keyboard, delay_seconds), timeout=180)
        if result is None:
            return jsonify({"success": False, "error": "Async operation timed out"}), 500
        if isinstance(result, dict):
            if result.get('ok'):
                return jsonify({"success": True, **result})
            return jsonify({"success": False, **result}), 500
        return jsonify({"success": bool(result)})
    elif image_urls:
        if len(image_urls) == 1:
            try:
                run_async(application.bot.send_photo(chat_id=channel, photo=image_urls[0], caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard), timeout=60)
                return jsonify({"success": True, "single_photo": True, "caption_sent": True, "buttons_sent": bool(keyboard)})
            except Exception as e:
                return jsonify({"success": False, "error": f"Photo URL failed: {e}"}), 500
        else:
            results = []
            total = len(image_urls)
            for i, url in enumerate(image_urls):
                is_last = (i == total - 1)
                try:
                    photo_arg = url
                    if is_base64_data_url(url):
                        photo_arg = data_url_to_inputfile(url)
                    if is_last:
                        run_async(application.bot.send_photo(chat_id=channel, photo=photo_arg, caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard), timeout=60)
                    else:
                        run_async(application.bot.send_photo(chat_id=channel, photo=photo_arg), timeout=60)
                    results.append({"index": i, "ok": True})
                    if not is_last:
                        time_mod.sleep(delay_seconds)
                except Exception as e:
                    results.append({"index": i, "ok": False, "error": str(e)})
            success_count = sum(1 for r in results if r.get("ok"))
            return jsonify({"success": success_count > 0, "single_posts": True, "image_count": total, "sent_count": success_count, "caption_sent": True, "buttons_sent": bool(keyboard), "results": results})
    else:
        try:
            run_async(application.bot.send_message(chat_id=channel, text=caption or "\u200b", reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True), timeout=30)
            return jsonify({"success": True, "text_only": True})
        except Exception as e:
            return jsonify({"success": False, "error": f"Text send failed: {e}"}), 500

register_pending_payment_routes(app)

@app.route('/api/send-payment-pending', methods=['POST'])
def api_send_payment_pending():
    """Remind user of pending payment and arm phone→STK for active gateway (Makamesco/PayHero/Paystack)."""
    data = request.json or {}
    user_id = data.get('user_id')
    amount = data.get('amount', 0)
    if not user_id:
        return jsonify({"success": False, "error": "user_id required"}), 400
    try:
        user_id = int(user_id)
    except Exception:
        return jsonify({"success": False, "error": "invalid user_id"}), 400
    try:
        amount = int(amount or 0)
    except Exception:
        amount = 0

    pend = None
    if payments_collection is not None:
        try:
            q = {"user_id": user_id, "status": "pending"}
            if data.get("reference"):
                q = {"reference": data.get("reference")}
            pend = payments_collection.find_one(q, sort=[("created_at", DESCENDING)])
        except Exception:
            pend = None
    if pend and amount <= 0:
        try:
            amount = int(pend.get("amount") or 0)
        except Exception:
            pass
    if amount <= 0:
        try:
            cart_items = get_user_cart(user_id)
            if cart_items:
                amount = sum(int(i.get("price", 0) or 0) for i in cart_items)
        except Exception:
            pass

    movie_token = (pend or {}).get("movie_token") or data.get("movie_token")
    series_token = (pend or {}).get("series_token") or data.get("series_token")
    cart_tokens = (pend or {}).get("cart_tokens") or data.get("cart_tokens") or []
    is_cart = bool(cart_tokens) or bool((pend or {}).get("is_cart"))
    is_subscription = bool((pend or {}).get("is_subscription") or (pend or {}).get("type") == "subscription")
    plan = (pend or {}).get("plan")
    ref = (pend or {}).get("reference") or data.get("reference") or f"PEND{user_id}{int(datetime.utcnow().timestamp())}"
    provider = get_active_payment_gateway()

    try:
        if pending_payhero_collection is not None:
            pending_payhero_collection.update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id,
                    "reference": ref,
                    "payhero_ref": ref,
                    "price": amount,
                    "amount": amount,
                    "status": "awaiting_phone",
                    "provider": provider,
                    "movie_token": movie_token,
                    "series_token": series_token,
                    "cart_tokens": cart_tokens,
                    "is_cart": is_cart,
                    "is_subscription": is_subscription,
                    "plan": plan,
                    "chat_id": user_id,
                    "created_at": datetime.utcnow(),
                    "from_dashboard_pending": True,
                }},
                upsert=True,
            )
        if provider == "paystack" and pending_stk_collection is not None:
            pending_stk_collection.update_one(
                {"user_id": user_id},
                {"$set": {
                    "user_id": user_id,
                    "reference": ref,
                    "stk_reference": f"stk_{ref}",
                    "price": amount,
                    "status": "awaiting_phone",
                    "movie_token": movie_token,
                    "series_token": series_token,
                    "cart_tokens": cart_tokens,
                    "is_cart": is_cart,
                    "is_subscription": is_subscription,
                    "plan": plan,
                    "created_at": datetime.utcnow(),
                }},
                upsert=True,
            )
        if payments_collection is not None:
            payments_collection.update_one(
                {"reference": ref},
                {"$set": {
                    "user_id": user_id,
                    "reference": ref,
                    "amount": amount,
                    "status": "pending",
                    "movie_token": movie_token,
                    "series_token": series_token,
                    "cart_tokens": cart_tokens,
                    "is_cart": is_cart,
                    "is_subscription": is_subscription,
                    "plan": plan,
                    "provider": provider,
                    "updated_at": datetime.utcnow(),
                }},
                upsert=True,
            )
        track_pending_payment(user_id, ref)
    except Exception as e:
        logger.error(f"arm pending payment: {e}")

    extra_titles = data.get("titles") or (pend or {}).get("titles") or []
    extra_movie = data.get("movie_title") or (pend or {}).get("movie_title") or (pend or {}).get("title") or ""

    async def _send():
        try:
            details_lines = []
            if extra_titles:
                details_lines.append("🎬 <b>Items:</b>")
                for tt in extra_titles[:20]:
                    details_lines.append(f"➠ {tt}")
            elif extra_movie:
                details_lines.append(f"🎬 <b>{escape_html(str(extra_movie))}</b>")
            elif series_token:
                details_lines.append(f"📦 Collection token: <code>{series_token}</code>")
            elif movie_token:
                details_lines.append(f"🎬 Movie token: <code>{movie_token}</code>")
            if is_subscription and plan:
                details_lines.append(f"👑 Subscription: <b>{plan}</b>")
            details_block = "\n".join(details_lines) if details_lines else "You still have an unpaid item."

            text = add_footer(
                f"⏳ <b>Payment Pending</b>\n\n"
                f"{details_block}\n\n"
                f"💰 <b>Total: {amount} KES</b>\n\n"
                f"📲 <b>Send your M-Pesa number now</b> (e.g. <code>07XX XXX XXX</code>)\n"
                f"or tap <b>Pay Now</b>."
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Pay Now 📱", callback_data=f"pending_pay_{user_id}_{amount}"),
                    InlineKeyboardButton("Cancel", callback_data=f"pending_cancel_{user_id}"),
                ]
            ])
            await application.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return True
        except Exception as e:
            logger.error(f"send-payment-pending error: {e}")
            logger.error(traceback.format_exc())
            return False

    ok = run_async(_send())
    if ok:
        return jsonify({"success": True, "reference": ref, "provider": provider, "amount": amount})
    return jsonify({"success": False, "error": "Failed to send"}), 500



async def send_single_photo_to_channel(image_file, channel, caption, keyboard):
    img_bytes = image_file.read()
    # Detect extension
    name = getattr(image_file, "filename", "") or "poster.jpg"
    suffix = ".jpg"
    lower = name.lower()
    for ext in (".png", ".jpeg", ".jpg", ".webp", ".gif"):
        if lower.endswith(ext):
            suffix = ext
            break
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(img_bytes)
    tmp.close()
    try:
        try:
            await application.bot.send_photo(
                chat_id=channel,
                photo=InputFile(tmp.name),
                caption=(caption or None),
                parse_mode=ParseMode.HTML if caption else None,
                reply_markup=keyboard,
            )
            return {"ok": True}
        except Exception as e1:
            logger.warning(f"send_photo HTML failed ({e1}), retry plain")
            try:
                await application.bot.send_photo(
                    chat_id=channel,
                    photo=InputFile(tmp.name),
                    caption=(caption or None),
                    reply_markup=keyboard,
                )
                return {"ok": True}
            except Exception as e2:
                logger.error(f"Single photo send failed: {e2}")
                # Last resort: send photo then buttons as separate message
                try:
                    await application.bot.send_photo(chat_id=channel, photo=InputFile(tmp.name))
                    if caption or keyboard:
                        await application.bot.send_message(
                            chat_id=channel,
                            text=caption or "\u200b",
                            reply_markup=keyboard,
                            parse_mode=ParseMode.HTML if caption else None,
                        )
                    return {"ok": True}
                except Exception as e3:
                    logger.error(f"Photo fallback failed: {e3}")
                    return {"ok": False, "error": str(e3)}
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

async def send_multiple_photos_to_channel(image_files, channel, caption, keyboard, delay_seconds=2.0):
    """Send 2–10 images as a Telegram album (media group), caption on first, buttons after."""
    total = len(image_files)
    temp_paths = []
    try:
        # Prepare files
        for img_file in image_files[:10]:  # Telegram album max 10
            img_file.seek(0)
            img_bytes = img_file.read()
            tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
            tmp.write(img_bytes)
            tmp.close()
            temp_paths.append(tmp.name)

        media_group = []
        for i, path in enumerate(temp_paths):
            if i == 0 and caption:
                media_group.append(InputMediaPhoto(media=InputFile(path), caption=caption, parse_mode=ParseMode.HTML))
            else:
                media_group.append(InputMediaPhoto(media=InputFile(path)))

        await application.bot.send_media_group(chat_id=channel, media=media_group)
        logger.info(f"Album sent: {len(media_group)} photos to {channel}")

        # Buttons cannot attach to media groups — send as follow-up message
        if keyboard:
            await asyncio.sleep(BOT_FAST_DELAY)
            btn_text = caption[:100] if caption else "⬇️"
            try:
                await application.bot.send_message(
                    chat_id=channel,
                    text=btn_text if caption else "Tap a button:",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML if caption else None,
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.warning(f"Album buttons follow-up failed: {e}")

        return {
            "ok": True,
            "album": True,
            "image_count": len(media_group),
            "sent_count": len(media_group),
            "caption_sent": bool(caption),
            "buttons_sent": bool(keyboard)
        }
    except Exception as e:
        logger.error(f"Album send error: {e} — falling back to single photos")
        # Fallback: individual photos
        results = []
        for i, path in enumerate(temp_paths):
            is_last = (i == len(temp_paths) - 1)
            try:
                if is_last:
                    await application.bot.send_photo(
                        chat_id=channel, photo=InputFile(path),
                        caption=caption, parse_mode=ParseMode.HTML,
                        reply_markup=keyboard, disable_web_page_preview=True
                    )
                else:
                    await application.bot.send_photo(chat_id=channel, photo=InputFile(path))
                results.append({"index": i, "ok": True})
                if not is_last:
                    await asyncio.sleep(BOT_MIRROR_DELAY)
            except Exception as ex:
                results.append({"index": i, "ok": False, "error": str(ex)})
        success_count = sum(1 for r in results if r.get("ok"))
        return {"ok": success_count > 0, "single_posts": True, "image_count": total, "sent_count": success_count, "caption_sent": True, "buttons_sent": bool(keyboard), "results": results}
    finally:
        for tp in temp_paths:
            try:
                os.unlink(tp)
            except Exception:
                pass

@app.route('/api/album-post', methods=['POST'])
def album_post():
    """Send selected movies as album + buttons. Applies buy-5-get-1 discount on GET ALL price.
    Caption lists movie titles (editable via album_title from dashboard).
    """
    data = request.json or {}
    channel = data.get('channel')
    movie_tokens = data.get('tokens', [])
    album_title = (data.get('album_title') or data.get('title') or '').strip()
    button_texts = data.get('button_texts') or []  # optional override labels
    caption_override = (data.get('caption') or '').strip()
    if not channel:
        return jsonify({"error": "No target channel"}), 400
    if not movie_tokens:
        return jsonify({"error": "Select at least one movie"}), 400
    movies = []
    for t in movie_tokens:
        mdoc = movies_collection.find_one({"token": t}) if movies_collection is not None else None
        if mdoc:
            movies.append(mdoc)
    movies = dedupe_movie_items(movies)  # never post the same movie twice
    if not movies:
        return jsonify({"error": "No valid movies found"}), 400
    n = len(movies)
    unit = int(movies[0].get('price') or get_default_price() or 10)
    # prefer consistent unit from default if mixed
    try:
        units = [int(m.get('price') or unit or 0) for m in movies]
        unit = units[0] if units else unit
        subtotal = sum(units)
    except Exception:
        subtotal = n * unit
    discount = calculate_discount(n, unit)
    pay_total = max(0, subtotal - discount)
    # Title list for caption
    title_lines = []
    for mdoc in movies:
        title_lines.append(mdoc.get('title') or 'Movie')
    titles_block = decorate_title_list(title_lines)
    if not album_title:
        album_title = " · ".join([(m.get('title') or '')[:25] for m in movies[:4]])
        if n > 4:
            album_title += f" +{n-4} more"
    # Caption: album title + numbered titles + unit/total price line
    if caption_override:
        caption = apply_premium_emojis(caption_override)
    else:
        price_line = f"● One movie {unit}KES  ● {n} movie(s) · {pay_total} KES"
        if discount > 0:
            price_line += f"  (🎁 {n//5} free)"
        # titles_block uses ONE arrow for whole album; keep 👇 tip line
        caption = f"<b>{escape_html(album_title)}</b>\n\n{titles_block}\n\n{price_line}\n👇 Tap a movie to get it, or GET ALL ONCE"
        try:
            caption = apply_premium_emojis(caption)
        except Exception:
            pass
    # Store bundle for GET ALL deep link
    main_token = generate_count_token() if 'generate_count_token' in dir() else secrets.token_hex(4)
    try:
        main_token = generate_count_token()
    except Exception:
        main_token = secrets.token_hex(4)
    episodes = []
    for mdoc in movies:
        episodes.append({
            "private_link": f"https://t.me/{BOT_USERNAME}?start={mdoc['token']}",
            "title": mdoc.get('title'),
            "button_title": clean_movie_title_for_button(mdoc.get('title') or 'Movie'),
            "file_id": mdoc.get('file_id'),
            "file_type": mdoc.get('file_type', 'video'),
            "token": mdoc.get('token'),
            "channel_id": str(mdoc.get('channel_id', '')),
            "message_id": int(mdoc.get('message_id', 0) or 0),
            "poster_url": mdoc.get('poster_url'),
        })
    if series_bundles_collection is not None:
        series_bundles_collection.insert_one({
            "token": main_token,
            "title": album_title,
            "button_title": clean_movie_title_for_button(album_title),
            "price": pay_total,
            "original_price": subtotal,
            "discount": discount,
            "category": "series",
            "description": f"Album of {n} movies",
            "poster_url": next((m.get('poster_url') for m in movies if m.get('poster_url')), None),
            "episodes": episodes,
            "episode_count": n,
            "created_at": datetime.utcnow(),
            "downloads": 0,
            "is_collection": True,
        })
    async def post_album():
        bot_link = f"https://t.me/{BOT_USERNAME}?start={main_token}"
        # Buttons: custom texts if provided
        buttons = []
        for i, mdoc in enumerate(movies):
            if i < len(button_texts) and str(button_texts[i]).strip():
                label = str(button_texts[i]).strip()[:30]
            else:
                label = clean_movie_title_for_button(mdoc.get('title') or 'Movie')[:28]
            buttons.append({"text": label, "url": f"https://t.me/{BOT_USERNAME}?start={mdoc['token']}"})
        # GET ALL — pay price only (no (−) on button)
        buttons.append({"text": f"GET ALL {n} ONCE — {pay_total} KES", "url": bot_link})
        # Media group with caption on first image (Telegram limit ~1024)
        # Channel album: file_id / http URL / base64 — every poster visible
        resolved = _resolve_album_poster_sources(movies)
        cap = caption[:1020]
        if resolved:
            sent = await _send_album_media(channel, resolved, caption=cap)
            if not sent:
                try:
                    await application.bot.send_message(chat_id=channel, text=cap, parse_mode=ParseMode.HTML)
                except Exception:
                    await application.bot.send_message(chat_id=channel, text=strip_premium_emojis(cap))
        else:
            try:
                await application.bot.send_message(chat_id=channel, text=cap, parse_mode=ParseMode.HTML)
            except Exception:
                await application.bot.send_message(chat_id=channel, text=strip_premium_emojis(cap))
        per_row = 4 if n >= 10 else 3
        button_rows = build_button_rows(buttons, per_row=per_row)
        await send_html_safe(
            application.bot, channel,
            apply_premium_emojis("👆 Tap a movie to get it, or GET ALL ONCE"),
            reply_markup=button_rows,
        )
        return True
    try:
        result = run_async(post_album(), timeout=90)
        if result:
            return jsonify({
                "success": True,
                "bundle_token": main_token,
                "movie_count": n,
                "subtotal": subtotal,
                "discount": discount,
                "pay_total": pay_total,
            })
        return jsonify({"success": False, "error": "Posting failed"}), 500
    except Exception as e:
        logger.error(f"album_post: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("Starting bot initialization...")
    try:
        init_db()
        _loop_thread = threading.Thread(target=start_bot_loop, daemon=True)
        _loop_thread.start()
        import time
        time.sleep(0.5)
        async def init_bot():
            await application.initialize()
            logger.info("Telegram Application initialized")
            await application.start()
            logger.info("Telegram Application started")
            # Start APScheduler ON the bot's event loop — required for the
            # auto re-engage job (queued in setup_handlers) to actually fire.
            try:
                if not scheduler.running:
                    scheduler.start()
                logger.info("AsyncIOScheduler started on bot loop — auto re-engage ACTIVE")
            except Exception as _sched_err:
                logger.error(f"Scheduler start failed (auto re-engage OFF): {_sched_err}")
            await application.bot.set_my_commands([
                BotCommand("start", "🚀 Start the bot / browse movies"),
                BotCommand("search", "🔍 Search movies by title or genre"),
                BotCommand("mymovies", "🎬 Your purchased movies (rewatch free)"),
                BotCommand("myplanmovies", "📋 Rewatch movies from your plan library"),
                BotCommand("checksubscription", "📊 Check subscription status & remaining"),
                BotCommand("checkpayment", "💰 Check pending payment"),
                BotCommand("cart", "🛒 View your shopping cart"),
                BotCommand("moremovies", "📺 Browse movie channels"),
                BotCommand("claimfree", "🎁 Claim free movie after 5 purchases"),
                BotCommand("getbot", "🤖 Get your own movie bot"),
                BotCommand("mybot", "⚙️ Manage your bot"),
                BotCommand("dev", "🛠 Developer info"),
                BotCommand("help", "✨ Show help message"),
                BotCommand("ping", "🏓 Test if bot is active"),
                BotCommand("setmenustyle", "🎨 Set your personal menu style"),
            ])
            logger.info("Bot commands set")
            # Detect if bot owner (ADMIN_IDS) / donors have Telegram Premium → enable animated emojis
            try:
                await refresh_admin_premium_status(application.bot)
            except Exception as e:
                logger.warning(f"premium status at init: {e}")
            try:
                await validate_premium_emoji_ids(application.bot)
            except Exception as e:
                logger.warning(f"emoji id validation at init: {e}")
            if WEBHOOK_URL:
                webhook_path = f"{WEBHOOK_URL.rstrip('/')}/webhook"
                await application.bot.delete_webhook(drop_pending_updates=True)
                logger.info("Old webhook deleted")
                await application.bot.set_webhook(
                    url=webhook_path,
                    allowed_updates=[
                        "message", "edited_message", "callback_query",
                        "channel_post", "edited_channel_post",
                        "my_chat_member", "chat_member", "chat_join_request",
                    ],
                    drop_pending_updates=True,
                )
                logger.info(f"Webhook set: {webhook_path}")
                try:
                    info = await application.bot.get_webhook_info()
                    logger.info(
                        f"WebhookInfo url={info.url} pending={info.pending_update_count} "
                        f"last_error={getattr(info, 'last_error_message', None)}"
                    )
                except Exception as wi:
                    logger.warning(f"getWebhookInfo failed: {wi}")

        setup_handlers()
        logger.info("Handlers setup complete")
        future = asyncio.run_coroutine_threadsafe(init_bot(), _bot_loop)
        future.result(timeout=30)
        logger.info("Bot initialization complete!")
        logger.info("=" * 50)
        app.run(host='0.0.0.0', port=PORT, threaded=True)
    except Exception as e:
        logger.error(f"Bot initialization failed: {e}")
        logger.error(traceback.format_exc())
        logger.error("=" * 50)
    finally:
        try:
            if _bot_loop and _bot_loop.is_running():
                future = asyncio.run_coroutine_threadsafe(application.stop(), _bot_loop)
                future.result(timeout=10)
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
