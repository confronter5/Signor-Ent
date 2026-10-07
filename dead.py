"""
Hidden admin — Store first, deliver later + auto-forward

/startstore  - silent store (videos/files only)
/storeking   - deliver stored items (with ranges)
auto-forward - new posts from source → target (no forward tag)
"""

from __future__ import annotations

import os
import re
import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, Any, List, Set

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError, BadRequest, Forbidden

logger = logging.getLogger(__name__)

def _dead_target() -> int:
    raw = (os.environ.get("DEAD_TARGET_CHANNEL") or "").strip()
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0

def _auto_forward_on() -> bool:
    return (os.environ.get("AUTO_FORWARD") or "false").strip().lower() in ("1", "true", "yes", "on")

def _auto_sources() -> Set[int]:
    raw = (os.environ.get("AUTO_FORWARD_SOURCES") or "").strip()
    out: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out

def _workers() -> int:
    try:
        return max(1, min(4, int(os.environ.get("DEAD_WORKERS", "3"))))
    except Exception:
        return 3

def _store_workers() -> int:
    try:
        return max(1, min(2, int(os.environ.get("DEAD_STORE_WORKERS", "1"))))
    except Exception:
        return 1

def _delay_ms() -> float:
    try:
        return max(0.25, float(os.environ.get("DEAD_DELAY_MS", "80"))) / 1000.0
    except Exception:
        return 0.08

_DEAD_JOBS: Dict[int, Dict[str, Any]] = {}
_DELIVERED: set = set()

def _is_admin(user_id) -> bool:
    try:
        from bot import is_bot_admin
        return is_bot_admin(user_id)
    except Exception:
        try:
            ids = [int(x.strip()) for x in os.environ.get("ADMIN_ID", "").split(",") if x.strip()]
            return int(user_id) in ids
        except Exception:
            return False

async def _safe_reply(update: Update, text: str, **kwargs):
    try:
        from bot import reply_html_safe, apply_premium_emojis, add_footer
        body = apply_premium_emojis(add_footer(text))
        await reply_html_safe(update.message, body, **kwargs)
    except Exception:
        try:
            await update.message.reply_text(text[:4000], parse_mode=ParseMode.HTML, **kwargs)
        except Exception as e:
            logger.warning(f"dead reply fail: {e}")

def _get_store_collection():
    try:
        from bot import db
        if db is not None:
            return db["dead_stored_media"]
    except Exception:
        pass
    try:
        from bot import movies_collection
        if movies_collection is not None:
            return movies_collection.database["dead_stored_media"]
    except Exception:
        pass
    return None

def _strip_tme_links(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'https?://(?:t\.me|telegram\.me|telegram\.dog)/\S+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'(?:t\.me|telegram\.me)/\S+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

HIDDEN_HELP = """🔐 <b>Hidden admin commands</b>

<b>Store → Deliver</b>
<code>/startstore -100SOURCE 14209</code>
<code>/startstore -100SOURCE 374 9000</code>

<code>/storedtotal</code>
<code>/storeking</code> · <code>/storeking 60</code> · <code>/storeking 21-40</code>
<code>/stopstore</code> · <code>/cancelstore</code>

<b>King movies (DB library)</b>
<code>/kingmovies</code> — total movies in database
<code>/kingdeliver 1-10</code> · <code>/kingdeliver 4-90</code>
<code>/kingdeliver all</code>
<code>/kingdeliver pause</code> · <code>resume</code> · <code>stop</code>
.env: <code>KING_DELIVER_CHANNEL=-100TARGET</code>

<b>Auto-forward</b> (new posts only)
Set in .env:
<code>AUTO_FORWARD=true</code>
<code>AUTO_FORWARD_SOURCES=-100SOURCE</code>
<code>DEAD_TARGET_CHANNEL=-100TARGET</code>

<code>/confronterisdead</code> · <code>/deadtest</code>
"""

async def confronterisdead_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    await _safe_reply(update, HIDDEN_HELP)

async def deadtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    coll = _get_store_collection()
    total = 0
    if coll is not None:
        try:
            total = coll.count_documents({"admin_id": update.effective_user.id})
        except Exception:
            pass
    af = "ON" if _auto_forward_on() else "OFF"
    await _safe_reply(
        update,
        f"✅ dead.py OK\n"
        f"Stored: <b>{total}</b>\n"
        f"Auto-forward: <b>{af}</b>\n"
        f"Target: <code>{_dead_target()}</code>\n"
        f"Sources: <code>{_auto_sources() or 'none'}</code>"
    )

async def storedtotal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    coll = _get_store_collection()
    if coll is None:
        await _safe_reply(update, "❌ Store collection not available")
        return
    try:
        total = coll.count_documents({"admin_id": update.effective_user.id})
        await _safe_reply(update, f"📦 <b>Stored:</b> <code>{total}</code>")
    except Exception as e:
        await _safe_reply(update, f"❌ {e}")

# ---------------------------------------------------------------------------
# SILENT STORE
# ---------------------------------------------------------------------------
async def _run_store_job(bot, admin_chat_id, source, start_id, end_id, job_key):
    if end_id < start_id:
        start_id, end_id = end_id, start_id
    if end_id - start_id > 250000:
        end_id = start_id + 250000

    total = end_id - start_id + 1
    workers = _store_workers()
    coll = _get_store_collection()
    delay = _delay_ms()

    if coll is None:
        await bot.send_message(admin_chat_id, "❌ Store collection not available (Mongo problem)")
        return

    # count before
    try:
        before = coll.count_documents({"admin_id": job_key})
    except Exception:
        before = 0

    state = _DEAD_JOBS.setdefault(job_key, {})
    state.update({
        "stop": False, "ok": 0, "skip": 0, "fail": 0, "floods": 0, "new": 0,
        "total": total, "started": time.time(), "last_ok": start_id - 1,
    })

    status_msg = await bot.send_message(
        admin_chat_id,
        f"📥 <b>STORE running (silent)</b>\n"
        f"<code>{source}</code>  {start_id}→{end_id}\n"
        f"Already stored: {before}\n"
        f"✅ 0 new  ⏭ 0\n/stopstore",
        parse_mode=ParseMode.HTML,
    )
    status_id = status_msg.message_id

    queue: asyncio.Queue = asyncio.Queue()
    for mid in range(start_id, end_id + 1):
        await queue.put(mid)

    sem = asyncio.Semaphore(workers)

    async def _try_store(mid: int):
        if state.get("stop"):
            return
        for attempt in range(5):
            if state.get("stop"):
                return
            try:
                msg = await bot.copy_message(
                    chat_id=admin_chat_id,
                    from_chat_id=source,
                    message_id=mid,
                )
                raw_caption = getattr(msg, "caption", None) or ""
                clean_caption = _strip_tme_links(raw_caption)

                # delete temp copy immediately
                try:
                    await bot.delete_message(chat_id=admin_chat_id, message_id=msg.message_id)
                except Exception:
                    try:
                        await asyncio.sleep(0.25)
                        await bot.delete_message(chat_id=admin_chat_id, message_id=msg.message_id)
                    except Exception:
                        pass

                # insert / update
                try:
                    result = coll.update_one(
                        {"channel_id": source, "message_id": mid, "admin_id": job_key},
                        {"$set": {
                            "channel_id": source,
                            "message_id": mid,
                            "admin_id": job_key,
                            "caption": clean_caption,
                            "stored_at": datetime.utcnow(),
                        }},
                        upsert=True,
                    )
                    if result.upserted_id is not None:
                        state["new"] = state.get("new", 0) + 1
                except Exception as e:
                    logger.warning(f"store db write fail: {e}")

                state["ok"] = state.get("ok", 0) + 1
                state["last_ok"] = mid
                if delay > 0:
                    await asyncio.sleep(delay)
                return

            except RetryAfter as e:
                wait = float(getattr(e, "retry_after", 5) or 5) + 1.0
                state["floods"] = state.get("floods", 0) + 1
                await asyncio.sleep(wait)
            except (TimedOut, NetworkError):
                await asyncio.sleep(0.8 + attempt * 0.5)
            except (BadRequest, Forbidden):
                state["skip"] = state.get("skip", 0) + 1
                return
            except Exception as e:
                logger.debug(f"store {source}/{mid}: {e}")
                state["skip"] = state.get("skip", 0) + 1
                return
        state["fail"] = state.get("fail", 0) + 1

    async def worker():
        while not state.get("stop"):
            try:
                mid = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            async with sem:
                await _try_store(mid)
            queue.task_done()

    async def reporter():
        while not state.get("stop"):
            await asyncio.sleep(12)
            done = state.get("ok", 0) + state.get("skip", 0) + state.get("fail", 0)
            if done >= total:
                return
            try:
                await bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=status_id,
                    text=(
                        f"📥 <b>STORE running (silent)</b>\n"
                        f"<code>{source}</code>  {start_id}→{end_id}\n"
                        f"✅ <b>{state.get('ok',0)}</b> (new {state.get('new',0)})  "
                        f"⏭ {state.get('skip',0)}  ({done}/{total})\n"
                        f"last: <code>{state.get('last_ok')}</code>\n"
                        f"/stopstore"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    rt = asyncio.create_task(reporter())
    await asyncio.gather(*[asyncio.create_task(worker()) for _ in range(workers)])
    state["stop"] = True
    rt.cancel()
    try:
        await rt
    except Exception:
        pass

    try:
        after = coll.count_documents({"admin_id": job_key})
    except Exception:
        after = before

    elapsed = max(0.1, time.time() - state["started"])
    _DEAD_JOBS.pop(job_key, None)

    final = (
        f"✅ <b>STORE finished</b>\n"
        f"✅ processed: <b>{state.get('ok',0)}</b>\n"
        f"🆕 newly added: <b>{state.get('new',0)}</b>\n"
        f"⏭ skipped: {state.get('skip',0)}\n"
        f"📦 total now: <b>{after}</b> (was {before})\n"
        f"⏱ {elapsed:.0f}s\n\n"
        f"<code>/storedtotal</code> → <code>/storeking</code>"
    )
    try:
        await bot.edit_message_text(chat_id=admin_chat_id, message_id=status_id, text=final, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(admin_chat_id, final, parse_mode=ParseMode.HTML)

async def startstore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    uid = update.effective_user.id
    if uid in _DEAD_JOBS and not _DEAD_JOBS[uid].get("stop"):
        await _safe_reply(update, "⚠️ Job running. /stopstore first.")
        return

    args = list(context.args or [])
    if not args:
        await _safe_reply(update, "❌ <code>/startstore -100CHANNEL LAST_ID</code>")
        return

    clean = [a for a in args if a.lower() != "all"]
    source = start_id = end_id = None

    if len(clean) >= 2 and "-" in clean[-1] and not clean[-1].startswith("-100"):
        try:
            source = int(clean[0])
            a, b = clean[-1].split("-", 1)
            start_id, end_id = int(a), int(b)
        except ValueError:
            await _safe_reply(update, "❌ Invalid range")
            return
    else:
        nums = []
        for a in clean:
            try:
                nums.append(int(a))
            except ValueError:
                await _safe_reply(update, f"❌ {a}")
                return
        if len(nums) < 2:
            await _safe_reply(update, "❌ Need channel + last ID")
            return
        source = nums[0]
        if len(nums) == 2:
            start_id, end_id = 1, nums[1]
        else:
            start_id, end_id = nums[1], nums[2]

    if source is None or end_id is None or source > 0 or end_id < 1:
        await _safe_reply(update, "❌ Example: <code>/startstore -1002664182156 14209</code>")
        return

    _DEAD_JOBS[uid] = {"stop": False}
    asyncio.create_task(_run_store_job(context.bot, update.effective_chat.id, source, start_id, end_id, uid))
    await _safe_reply(update, f"🚀 STORE (silent) <code>{source}</code> {start_id}→{end_id}")

async def stopstore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    uid = update.effective_user.id
    if uid in _DEAD_JOBS:
        _DEAD_JOBS[uid]["stop"] = True
        await _safe_reply(update, "⏹ Stopped.")
    else:
        await _safe_reply(update, "ℹ️ No job.")

async def cancelstore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    uid = update.effective_user.id
    if uid in _DEAD_JOBS:
        _DEAD_JOBS[uid]["stop"] = True
    coll = _get_store_collection()
    deleted = 0
    if coll is not None:
        try:
            res = coll.delete_many({"admin_id": uid})
            deleted = res.deleted_count
        except Exception as e:
            await _safe_reply(update, f"❌ {e}")
            return
    await _safe_reply(update, f"🗑 Cleared <b>{deleted}</b> + stopped.")

# ---------------------------------------------------------------------------
# DELIVER
# ---------------------------------------------------------------------------
async def _deliver_items(bot, admin_id: int, target: int, items: List[dict], label: str):
    if not items:
        await bot.send_message(admin_id, "ℹ️ No items to deliver.")
        return

    _DEAD_JOBS[admin_id] = {
        "stop": False, "ok": 0, "fail": 0, "skip": 0, "floods": 0,
        "total": len(items), "started": time.time(),
    }
    state = _DEAD_JOBS[admin_id]
    workers = _workers()
    delay = _delay_ms()
    sem = asyncio.Semaphore(workers)
    queue: asyncio.Queue = asyncio.Queue()

    for it in items:
        key = (int(it["channel_id"]), int(it["message_id"]))
        if key in _DELIVERED:
            continue
        await queue.put(it)

    actual_total = queue.qsize()
    if actual_total == 0:
        await bot.send_message(admin_id, "ℹ️ All selected items already delivered.")
        _DEAD_JOBS.pop(admin_id, None)
        return

    status = await bot.send_message(
        admin_id,
        f"🚀 Delivering <b>{actual_total}</b> → {label}\n/stopstore to cancel",
        parse_mode=ParseMode.HTML,
    )
    status_id = status.message_id

    async def _copy_one(item: dict):
        src = int(item["channel_id"])
        mid = int(item["message_id"])
        key = (src, mid)
        clean_caption = item.get("caption") or ""

        for attempt in range(6):
            if state.get("stop"):
                return
            try:
                kwargs = {"chat_id": target, "from_chat_id": src, "message_id": mid}
                if clean_caption:
                    kwargs["caption"] = clean_caption
                await bot.copy_message(**kwargs)
                _DELIVERED.add(key)
                state["ok"] = state.get("ok", 0) + 1
                if delay > 0:
                    await asyncio.sleep(delay)
                return
            except RetryAfter as e:
                wait = float(getattr(e, "retry_after", 5) or 5) + 1.0
                state["floods"] = state.get("floods", 0) + 1
                await asyncio.sleep(wait)
            except (TimedOut, NetworkError):
                await asyncio.sleep(0.9 + attempt * 0.5)
            except (BadRequest, Forbidden):
                state["skip"] = state.get("skip", 0) + 1
                return
            except Exception:
                state["fail"] = state.get("fail", 0) + 1
                return
        state["fail"] = state.get("fail", 0) + 1

    async def worker():
        while not state.get("stop"):
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            async with sem:
                await _copy_one(item)
            queue.task_done()

    async def reporter():
        while not state.get("stop"):
            await asyncio.sleep(10)
            done = state.get("ok", 0) + state.get("skip", 0) + state.get("fail", 0)
            if done >= actual_total:
                return
            try:
                await bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=status_id,
                    text=(
                        f"🚀 Delivering…\n"
                        f"✅ <b>{state.get('ok',0)}</b>  ⏭ {state.get('skip',0)}  ({done}/{actual_total})\n"
                        f"/stopstore"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

    rt = asyncio.create_task(reporter())
    await asyncio.gather(*[asyncio.create_task(worker()) for _ in range(workers)])
    state["stop"] = True
    rt.cancel()
    try:
        await rt
    except Exception:
        pass

    elapsed = max(0.1, time.time() - state["started"])
    _DEAD_JOBS.pop(admin_id, None)

    final = (
        f"✅ <b>Delivery finished</b>\n"
        f"✅ {state.get('ok',0)}  ⏭ {state.get('skip',0)}  ❌ {state.get('fail',0)}\n"
        f"⏱ {elapsed:.0f}s\n→ {label}"
    )
    try:
        await bot.edit_message_text(chat_id=admin_id, message_id=status_id, text=final, parse_mode=ParseMode.HTML)
    except Exception:
        await bot.send_message(admin_id, final, parse_mode=ParseMode.HTML)

async def storeking_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    uid = update.effective_user.id
    coll = _get_store_collection()
    if coll is None:
        await _safe_reply(update, "❌ Store collection not available")
        return

    try:
        total = coll.count_documents({"admin_id": uid})
    except Exception as e:
        await _safe_reply(update, f"❌ {e}")
        return

    if total == 0:
        await _safe_reply(update, "ℹ️ Nothing stored. Use /startstore first.")
        return

    start_idx = 0
    end_idx = total
    args = list(context.args or [])
    if args:
        raw = args[0]
        try:
            if "-" in raw:
                a, b = raw.split("-", 1)
                start_idx = max(0, int(a) - 1)
                end_idx = int(b)
            else:
                end_idx = int(raw)
        except ValueError:
            await _safe_reply(update, "❌ Use <code>/storeking 60</code> or <code>/storeking 21-40</code>")
            return

    try:
        cursor = coll.find(
            {"admin_id": uid},
            {"channel_id": 1, "message_id": 1, "caption": 1, "_id": 0}
        ).sort([("stored_at", 1), ("message_id", 1)]).skip(start_idx).limit(max(0, end_idx - start_idx))
        items = list(cursor)
    except Exception as e:
        await _safe_reply(update, f"❌ DB error: {e}")
        return

    if not items:
        await _safe_reply(update, "ℹ️ No items in that range.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Send to my PM", callback_data=f"sk_pm_{uid}_{start_idx}_{end_idx}")],
        [InlineKeyboardButton("📢 Send to Target Channel", callback_data=f"sk_ch_{uid}_{start_idx}_{end_idx}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="sk_cancel")],
    ])
    await update.message.reply_text(
        f"📦 Selected <b>{len(items)}</b> items "
        f"(#{start_idx+1} → #{start_idx+len(items)}) / {total}\n"
        f"Where to deliver?",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )

async def storeking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    data = query.data
    uid = query.from_user.id if query.from_user else 0
    if not _is_admin(uid):
        return

    if data == "sk_cancel":
        await query.edit_message_text("Cancelled.")
        return

    parts = data.split("_")
    if len(parts) < 4:
        await query.edit_message_text("Invalid data.")
        return

    mode = parts[1]
    start_idx = int(parts[3])
    end_idx = int(parts[4]) if len(parts) > 4 else 999999

    if mode == "pm":
        target = uid
        label = "your PM"
    else:
        target = _dead_target()
        if not target:
            await query.edit_message_text("❌ DEAD_TARGET_CHANNEL not set")
            return
        label = f"channel <code>{target}</code>"

    coll = _get_store_collection()
    if coll is None:
        await query.edit_message_text("❌ Store collection missing")
        return

    try:
        cursor = coll.find(
            {"admin_id": uid},
            {"channel_id": 1, "message_id": 1, "caption": 1, "_id": 0}
        ).sort([("stored_at", 1), ("message_id", 1)]).skip(start_idx).limit(max(0, end_idx - start_idx))
        items = list(cursor)
    except Exception as e:
        await query.edit_message_text(f"❌ {e}")
        return

    await query.edit_message_text(f"🚀 Delivering <b>{len(items)}</b> items…", parse_mode=ParseMode.HTML)
    await _deliver_items(context.bot, uid, target, items, label)

# ---------------------------------------------------------------------------
# AUTO-FORWARD (new posts only)
# ---------------------------------------------------------------------------
async def auto_forward_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _auto_forward_on():
        return
    sources = _auto_sources()
    if not sources:
        return
    msg = update.channel_post or update.edited_channel_post
    if not msg:
        return
    if not (msg.video or msg.document or msg.animation or msg.photo):
        return
    chat = msg.chat
    if not chat:
        return
    src_id = int(chat.id)
    if src_id not in sources:
        return
    target = _dead_target()
    if not target or target == src_id:
        return
    try:
        # strip t.me from caption if present
        caption = _strip_tme_links(msg.caption or "")
        kwargs = {
            "chat_id": target,
            "from_chat_id": src_id,
            "message_id": msg.message_id,
        }
        if caption:
            kwargs["caption"] = caption
        await context.bot.copy_message(**kwargs)
        logger.info(f"AUTO_FORWARD {src_id}/{msg.message_id} → {target}")
    except RetryAfter as e:
        await asyncio.sleep(float(getattr(e, "retry_after", 3) or 3) + 1)
        try:
            await context.bot.copy_message(chat_id=target, from_chat_id=src_id, message_id=msg.message_id)
        except Exception as e2:
            logger.warning(f"AUTO_FORWARD retry fail: {e2}")
    except Exception as e:
        logger.warning(f"AUTO_FORWARD fail: {e}")

async def deadscan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    await _safe_reply(update, "Use <code>/startstore</code> + <code>/storeking</code> instead.")

async def deadstop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not _is_admin(update.effective_user.id):
        return
    uid = update.effective_user.id
    if uid in _DEAD_JOBS:
        _DEAD_JOBS[uid]["stop"] = True
        await _safe_reply(update, "⏹ Stopped.")
    else:
        await _safe_reply(update, "ℹ️ No job.")

def register_dead_handlers(application):
    application.add_handler(CommandHandler("confronterisdead", confronterisdead_cmd))
    application.add_handler(CommandHandler("deadtest", deadtest_cmd))
    application.add_handler(CommandHandler("startstore", startstore_cmd))
    application.add_handler(CommandHandler("stopstore", stopstore_cmd))
    application.add_handler(CommandHandler("cancelstore", cancelstore_cmd))
    application.add_handler(CommandHandler("storedtotal", storedtotal_cmd))
    application.add_handler(CommandHandler("storeking", storeking_cmd))
    application.add_handler(CallbackQueryHandler(storeking_callback, pattern=r"^sk_"))
    application.add_handler(CommandHandler("deadscan", deadscan_cmd))
    application.add_handler(CommandHandler("deadstop", deadstop_cmd))
    application.add_handler(
        MessageHandler(filters.UpdateType.CHANNEL_POST, auto_forward_channel_post),
        group=20,
    )
    logger.info("✅ dead_features (silent store + auto-forward restored)")
