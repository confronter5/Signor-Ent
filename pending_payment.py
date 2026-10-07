"""
Pending payment module for Confronter Movies bot.
Provides: STK helpers, pending handlers, PayHero callback bridge, display helper.
Supports Makamesco, PayHero, and Paystack (phone → STK is owned by bot.py).
"""
from __future__ import annotations

import base64
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("pending_payment")

_db = None
_pending_col = None
_collections: Dict[str, Any] = {}


def init_pending_payment_db(client):
    """Wire Mongo client and create indexes."""
    global _db, _pending_col
    try:
        if client is None:
            return
        db_name = (
            os.environ.get("MONGO_DB_NAME")
            or os.environ.get("DB_NAME")
            or os.environ.get("MONGODB_DB")
            or "confronter"
        )
        try:
            _db = client.get_default_database()
        except Exception:
            try:
                _db = client[db_name]
            except Exception:
                _db = None
                for n in client.list_database_names():
                    if n not in ("admin", "local", "config"):
                        _db = client[n]
                        break
        if _db is None:
            return
        _pending_col = _db["pending_payments"]
        try:
            _pending_col.create_index([("user_id", 1)])
            _pending_col.create_index([("reference", 1)])
            _pending_col.create_index([("status", 1)])
            logger.info("Pending payment indexes created")
        except Exception as e:
            logger.warning("Pending index create: %s", e)
        logger.info("pending_payment DB ready")
    except Exception as e:
        logger.error("init_pending_payment_db failed: %s", e)


def get_payhero_auth_headers() -> Dict[str, str]:
    username = os.environ.get("PAYHERO_API_USERNAME", "") or os.environ.get("PAYHERO_USERNAME", "")
    password = os.environ.get("PAYHERO_API_PASSWORD", "") or os.environ.get("PAYHERO_PASSWORD", "")
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def initiate_payhero_stk_push(
    phone: str,
    amount: int,
    external_reference: str,
    callback_url: str = None,
    customer_name: str = "",
    channel_id: int = None,
) -> Tuple[Optional[dict], Optional[str]]:
    """PayHero STK fallback. Prefer bot.py initiate when available."""
    try:
        import requests

        username = os.environ.get("PAYHERO_API_USERNAME", "") or os.environ.get("PAYHERO_USERNAME", "")
        password = os.environ.get("PAYHERO_API_PASSWORD", "") or os.environ.get("PAYHERO_PASSWORD", "")
        if not username or not password:
            return None, "PayHero credentials not set"
        headers = get_payhero_auth_headers()
        ch = channel_id or int(os.environ.get("PAYHERO_CHANNEL_ID", "6909") or "6909")
        payload = {
            "amount": int(amount),
            "phone_number": str(phone),
            "channel_id": ch,
            "provider": "m-pesa",
            "external_reference": external_reference,
            "customer_name": customer_name or "Customer",
            "callback_url": callback_url or os.environ.get("PAYHERO_CALLBACK_URL", ""),
        }
        res = requests.post(
            "https://backend.payhero.co.ke/api/v2/payments",
            json=payload,
            headers=headers,
            timeout=30,
        )
        try:
            data = res.json()
        except Exception:
            data = {"raw": res.text}
        if res.status_code in (200, 201) and (
            data.get("success")
            or data.get("status") in ("success", "pending", "queued", "QUEUED", "PENDING")
            or data.get("checkout_request_id")
            or data.get("CheckoutRequestID")
        ):
            return data, None
        return None, f"PayHero STK failed: {res.status_code} {data}"
    except Exception as e:
        logger.error("initiate_payhero_stk_push: %s", e)
        return None, str(e)


def format_movie_display_with_pending(
    title: str = "",
    price: Any = None,
    pending: bool = False,
    extra: str = "",
    **kwargs,
) -> str:
    """Format a movie line; safe with any call signature from bot.py."""
    try:
        # Support bot.py calling with (user_id, title, price, extra=...)
        if isinstance(title, int) and price is not None and not isinstance(price, bool):
            # (user_id, title, price) style
            user_id = title
            real_title = str(price)
            real_price = pending if not isinstance(pending, bool) else kwargs.get("price")
            pending_flag = False
            try:
                if _pending_col is not None and user_id:
                    doc = _pending_col.find_one({"user_id": user_id, "status": {"$in": ["pending", "awaiting_phone", "stk_sent"]}})
                    pending_flag = bool(doc)
            except Exception:
                pass
            parts = [real_title]
            if real_price is not None and not isinstance(real_price, bool):
                parts.append(f"{real_price} KES")
            if pending_flag:
                parts.append("Pending")
            if extra:
                parts.append(str(extra))
            return " · ".join(parts)

        parts = []
        t = title or kwargs.get("name") or kwargs.get("movie_title") or "Movie"
        parts.append(str(t))
        if price is not None and not isinstance(price, bool):
            parts.append(f"{price} KES")
        if pending:
            parts.append("Pending")
        if extra:
            parts.append(str(extra))
        return " · ".join(parts)
    except Exception:
        return str(title or "Movie")


async def process_payhero_callback_for_pending(callback_data: dict):
    """
    Pre-hook for PayHero / Makamesco / generic STK callbacks.
    Marks pending rows; actual delivery is done in bot.py.
    """
    try:
        if not callback_data:
            return
        response = callback_data.get("response") or callback_data.get("data") or callback_data
        if not isinstance(response, dict):
            response = callback_data
        external_ref = (
            response.get("ExternalReference")
            or response.get("external_reference")
            or response.get("reference")
            or response.get("CheckoutRequestID")
            or response.get("checkout_request_id")
            or ""
        )
        status = str(
            response.get("Status")
            or response.get("status")
            or response.get("ResultCode")
            or response.get("result_code")
            or ""
        ).lower()
        if not external_ref or _pending_col is None:
            return
        if status in ("success", "completed", "paid", "0", "00") or response.get("ResultCode") in (0, "0", "00"):
            _pending_col.update_many(
                {"reference": external_ref},
                {"$set": {"status": "completed", "completed_at": datetime.utcnow(), "provider_status": status}},
            )
            # also try payhero_ref field
            _pending_col.update_many(
                {"payhero_ref": external_ref},
                {"$set": {"status": "completed", "completed_at": datetime.utcnow()}},
            )
        elif status in ("failed", "cancelled", "abandoned", "declined"):
            _pending_col.update_many(
                {"$or": [{"reference": external_ref}, {"payhero_ref": external_ref}]},
                {"$set": {"status": status, "updated_at": datetime.utcnow()}},
            )
    except Exception as e:
        logger.error("process_payhero_callback_for_pending: %s", e)


async def handle_pending_payment_phone_input(update, context) -> bool:
    """
    Return False so bot.py owns phone → STK for Makamesco / PayHero / Paystack.
    Set awaiting_phone_for_pending only when pending_pay_* was tapped.
    """
    try:
        if not context or not getattr(context, "user_data", None):
            return False
        if not context.user_data.get("awaiting_phone_for_pending"):
            return False
        # Do not consume — let bot.py handle_phone_input route to active gateway
        return False
    except Exception as e:
        logger.error("handle_pending_payment_phone_input: %s", e)
        return False


async def handle_pending_payment_callback(update, context) -> bool:
    """
    Intentionally does NOT handle pending_pay_* / pending_cancel_*.
    Those are fully handled in bot.py (arms Makamesco/PayHero/Paystack STK).
    Return False always so bot.py continues.
    """
    return False


def setup_pending_payment_handlers(application, collections_dict=None):
    """
    Called from bot.py as:
        setup_pending_payment_handlers(application, collections_dict)
    Must accept 1 or 2 args.
    """
    global _collections
    try:
        if collections_dict and isinstance(collections_dict, dict):
            _collections = collections_dict
        logger.info("Pending payment handlers registered (Makamesco + PayHero + Paystack)")
    except Exception as e:
        logger.warning("setup_pending_payment_handlers: %s", e)


def register_pending_payment_routes(app):
    """Optional Flask routes for pending payment status."""
    try:
        @app.route("/api/pending-payment/status", methods=["GET"])
        def pending_status():
            return {"ok": True, "module": "pending_payment", "gateways": ["makamesco", "payhero", "paystack"]}

        logger.info("Pending payment routes registered")
    except Exception as e:
        logger.warning("register_pending_payment_routes: %s", e)
