VERSION = "1.17.2"
import requests, os, secrets, logging, threading, time, hmac, hashlib
from functools import wraps
from dotenv import load_dotenv
load_dotenv(override=True)
from flask import Flask, request, render_template, redirect
from flask_cors import CORS
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pymongo.errors import DuplicateKeyError

from functions import (
    store_embeddings,
    send_similar_reel,
    get_access_token,
    send_error_message,
    send_reaction,
    exchange_for_long_lived_token,
    get_access_token,
    run_gemini,
    refresh_long_lived_token,
    resolve_permalink,
    describe_media_file,
    one_sentence_summary,
    classify_note,
    ensure_account,
    record_message,
    check_user_limit,
    credit_balance,
    debit_credit,
    grant_credits,
    out_of_credits_message,
    start_typing,
    mark_reel_seen,
    reel_seen_recently,
    daily_summary,
    stats,
    accounts,
    FREE_REELS_LINKED,
    is_admin,
    welcome,
    onboarding_reply,
    ONBOARD_TEXT,
    set_ice_breakers,
    MAX_MESSAGE_CHARS,
    _is_permalink,
    _auth_params,
    GRAPH,
    users,
    processed,
    unprocessed_mids,
    creds,
)
from agent import handle_text_with_gemini_agent, answer_question

# Configure logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("pymongo").setLevel(logging.WARNING)
logging.getLogger("genai").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("google.auth").setLevel(logging.WARNING)
logging.getLogger("google.api_core").setLevel(logging.WARNING)

logger.info(f"Launching version {VERSION}")

# Fask config
app = Flask(__name__)
CORS(app=app)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(16))  # Use env variable if available
app.config["DEBUG"] = os.environ.get("FLASK_DEBUG", "False").lower() == "true"

executor = ThreadPoolExecutor(max_workers=10)
logger.info(f"Finished Executor")


# --------------------------------------------------------------------------
# Idempotency: each webhook mid is claimed atomically before work starts, so
# Instagram's retries cannot process the same message twice. The claim is
# released on failure so a retry can still succeed.
# --------------------------------------------------------------------------

def _ensure_indexes():
    """Create the indexes the hot paths depend on. Safe to run on every boot."""
    try:
        # Drop duplicate idempotency markers so the unique index can be built.
        for dupe in processed.aggregate([
            {"$group": {"_id": "$mid", "ids": {"$push": "$_id"}, "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}},
        ]):
            for extra_id in dupe["ids"][1:]:
                processed.delete_one({"_id": extra_id})

        processed.create_index("mid", unique=True, name="mid_unique")
        users.create_index("sender_id", unique=True, name="sender_unique")
        unprocessed_mids.create_index("sender_id", name="sender_idx")
        logger.info("MongoDB indexes ready")
    except Exception:
        logger.exception("Failed to create MongoDB indexes")


def _claim(mid):
    """Atomically claim a mid. False means another delivery already has it."""
    try:
        processed.insert_one({
            "mid": mid,
            "status": "processing",
            "timestamp": int(datetime.now().timestamp() * 1000),
        })
        return True
    except DuplicateKeyError:
        return False


def _finish(mid, ok):
    if not mid:
        return
    if ok:
        processed.update_one(
            {"mid": mid},
            {"$set": {"status": "done",
                      "finished_at": int(datetime.now().timestamp() * 1000)}},
        )
    else:
        processed.delete_one({"mid": mid, "status": "processing"})


def _release(mid):
    processed.delete_one({"mid": mid, "status": "processing"})


def _run_tracked(fn, *args, mid=None, **kwargs):
    """Run a background handler and settle the claim from its return value."""
    ok = False
    try:
        ok = fn(*args, **kwargs) is not False
    except Exception:
        logger.exception(f"Unhandled error in {fn.__name__} for mid {mid}")
    finally:
        _finish(mid, ok)


# --------------------------------------------------------------------------
# Security: verify Meta's webhook signature, protect operational routes,
# and alert on token trouble.
# --------------------------------------------------------------------------

_sig_warning_shown = False


def _verify_signature(raw_body, header):
    """Validate X-Hub-Signature-256 against INSTA_APP_SECRET.

    Returns True/False when a secret is configured, or None when it is not and
    verification is therefore impossible.
    """
    secret = os.environ.get("INSTA_APP_SECRET")
    if not secret:
        return None
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def require_admin(view):
    """Guard operational routes with ADMIN_TOKEN (Bearer or X-Admin-Token)."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        expected = os.environ.get("ADMIN_TOKEN")
        if not expected:
            return {"error": "ADMIN_TOKEN is not configured on the server"}, 503
        supplied = request.headers.get("X-Admin-Token")
        auth = request.headers.get("Authorization", "")
        if not supplied and auth.startswith("Bearer "):
            supplied = auth[7:]
        if not supplied or not hmac.compare_digest(supplied, expected):
            return {"error": "unauthorized"}, 401
        return view(*args, **kwargs)
    return wrapper


def _notify(text):
    """Best-effort alert POST to ALERT_WEBHOOK_URL (ntfy-style plain body)."""
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return
    try:
        requests.post(url, data=text.encode("utf-8"), timeout=10)
    except Exception:
        logger.warning("Alert webhook failed", exc_info=True)



def _token_health_loop(interval_seconds=6 * 3600):
    """Refresh the token from day 30 of its 60-day life, and alert on trouble.

    Refreshing at 30 days leaves ~30 daily checks before expiry, so a short
    outage or a failed refresh cannot silently kill the bot.
    """
    while True:
        try:
            token_doc = creds.find_one()
            if not token_doc:
                logger.critical("No Instagram token stored; the bot cannot send DMs")
                _notify("reel-finder: no Instagram token stored, DMs will fail")
            else:
                expires_at = token_doc.get("expires_at")
                if token_doc.get("token_type") == "system_user":
                    logger.info("Instagram system-user token never expires; nothing to refresh")
                elif not isinstance(expires_at, datetime):
                    logger.warning("Stored token has no expires_at; cannot schedule refresh")
                else:
                    days_left = (expires_at - datetime.now()).total_seconds() / 86400
                    if days_left <= 0:
                        logger.critical(
                            f"Instagram token EXPIRED {abs(days_left):.1f} days ago. "
                            "Re-authenticate at / to restore the bot."
                        )
                        _notify(
                            f"reel-finder: Instagram token expired {abs(days_left):.1f} days "
                            "ago. Re-authenticate at /."
                        )
                    elif days_left <= 30:
                        logger.info(f"Token has {days_left:.1f} days left; refreshing")
                        result = refresh_long_lived_token()
                        if result.get("error"):
                            logger.error(f"Automatic token refresh failed: {result['error']}")
                            _notify(
                                f"reel-finder: token refresh failed ({result['error']}). "
                                f"{days_left:.1f} days left."
                            )
                        else:
                            logger.info(
                                f"Token refreshed; valid {result['days_remaining']} more days"
                            )
                    else:
                        logger.info(f"Instagram token valid for {days_left:.1f} more days")
        except Exception:
            logger.exception("Token health check failed")
        time.sleep(interval_seconds)


def _seed_token_from_env():
    """Persist a dashboard-generated token on boot, and recover from expiry.

    The DB is the source of truth so the refresh loop can take over. If the
    stored token has already expired, a fresh INSTA_ACCESS_TOKEN replaces it —
    otherwise the app would keep using a dead token forever.
    """
    token = os.environ.get("INSTA_ACCESS_TOKEN")
    existing = creds.find_one()

    if existing:
        expires_at = existing.get("expires_at")
        expired = isinstance(expires_at, datetime) and expires_at <= datetime.now()
        if not expired:
            return
        if not token:
            logger.critical("Stored Instagram token expired and no INSTA_ACCESS_TOKEN to recover with")
            return
        logger.info("Stored Instagram token has expired; replacing it from INSTA_ACCESS_TOKEN")
    elif not token:
        logger.warning("No stored token and no INSTA_ACCESS_TOKEN: DMs will fail")
        return

    try:
        target = os.environ.get("IG_ID") or "me"
        # Use the env token explicitly: the stored one may be the expired token
        # we are trying to replace.
        params = {"access_token": token, "fields": "id,username"}
        secret = os.environ.get("INSTA_APP_SECRET")
        if secret:
            params["appsecret_proof"] = hmac.new(
                secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
            ).hexdigest()
        r = requests.get(f"{GRAPH}/{target}", params=params, timeout=15)
        data = r.json()
        if r.status_code != 200 or data.get("error"):
            logger.error(f"INSTA_ACCESS_TOKEN is not usable: {data}")
            return
        permanent = token.startswith("EAA")
        creds.delete_many({})
        creds.insert_one({
            "access_token": token,
            "user_id": data.get("id") or os.environ.get("IG_ID"),
            "username": data.get("username"),
            "expires_in": None if permanent else 60 * 24 * 3600,
            "created_at": datetime.now(),
            "expires_at": None if permanent else datetime.now() + timedelta(days=60),
            "token_type": "system_user" if permanent else "long_lived",
            "source": "env_bootstrap",
        })
        logger.info(
            f"Seeded creds from INSTA_ACCESS_TOKEN for @{data.get('username')} "
            f"({'non-expiring' if permanent else '60 days'})"
        )
    except Exception:
        logger.exception("Could not validate INSTA_ACCESS_TOKEN at startup")


_ensure_indexes()
_seed_token_from_env()
threading.Thread(target=_token_health_loop, name="token-health", daemon=True).start()


@app.route("/internal/send", methods=["POST"])
def internal_send():
    """Deliver a DM on behalf of the bridge or an admin. Service token only."""
    expected = os.environ.get("SERVICE_TOKEN")
    provided = request.headers.get("X-Service-Token") or ""
    if not expected or not hmac.compare_digest(provided, expected):
        return {"error": "unauthorized"}, 401

    data = request.get_json(silent=True) or {}
    igsid = str(data.get("igsid") or "").strip()
    text = (data.get("text") or "").strip()
    if not igsid or not text:
        return {"error": "igsid and text are required"}, 400

    ok = send_error_message(igsid, text[:3500])
    return {"ok": bool(ok)}, 200


@app.route("/internal/account/<igsid>", methods=["GET"])
def internal_account(igsid):
    """Balance for the site's account page. Service token only."""
    expected = os.environ.get("SERVICE_TOKEN")
    provided = request.headers.get("X-Service-Token") or ""
    if not expected or not hmac.compare_digest(provided, expected):
        return {"error": "unauthorized"}, 401
    doc = accounts.find_one({"sender_id": igsid}) or {}
    return {"igsid": igsid, "credits": int(doc.get("credits", 0)),
            "plan": doc.get("plan") or "free"}, 200


@app.route("/internal/credit", methods=["POST"])
def internal_credit():
    """Grant credits from the site after a Stripe payment. Service token only."""
    expected = os.environ.get("SERVICE_TOKEN")
    provided = request.headers.get("X-Service-Token") or ""
    if not expected or not hmac.compare_digest(provided, expected):
        return {"error": "unauthorized"}, 401

    data = request.get_json(silent=True) or {}
    igsid = str(data.get("igsid") or "").strip()
    ref = str(data.get("ref") or "").strip()
    reason = str(data.get("reason") or "purchase")
    try:
        units = int(data.get("units") or 0)
    except (TypeError, ValueError):
        return {"error": "units must be an integer"}, 400

    if not igsid or not ref or units == 0:
        return {"error": "igsid, ref and units are required"}, 400
    if units > 500:
        return {"error": "refusing an implausible grant"}, 400

    balance = grant_credits(igsid, units, reason, ref=ref)
    logger.info(f"Granted {units} credits to {igsid} via {reason} ({ref})")
    return {"ok": True, "igsid": igsid, "balance": balance}, 200


@app.route("/health")
def health():
    """Unauthenticated liveness probe: no secrets, no user data."""
    return {"ok": True, "version": VERSION}, 200


@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    """Handle Instagram webhook verification and message processing."""
    global _sig_warning_shown
    try:
        if request.method == "GET":
            verify_token = str(request.args.get("hub.verify_token"))
            challenge = request.args.get("hub.challenge")
            logger.info(
                f"GET request received with verify_token: {verify_token} and challenge: {challenge}"
            )
            if verify_token == str(os.getenv("WEBHOOK_VERIFY_TOKEN")):
                return challenge
            return "Invalid verify_token", 403

        elif request.method == "POST":
            # Verify Meta's signature over the raw body before trusting it.
            raw = request.get_data()
            sig_ok = _verify_signature(raw, request.headers.get("X-Hub-Signature-256"))
            if sig_ok is False:
                logger.warning("Rejected webhook: invalid X-Hub-Signature-256")
                return "Invalid signature", 403
            if sig_ok is None and not _sig_warning_shown:
                logger.warning(
                    "INSTA_APP_SECRET is not set: webhook signatures are NOT verified"
                )
                _sig_warning_shown = True

            body = request.get_json(silent=True) or {}
            logger.info(f"POST request received with body: {body}")

            # Validate webhook payload
            if not body.get("object") == "instagram":
                return "Invalid object type", 400

            # Instagram batches events: every entry and every messaging item
            # must be handled. Reading entry[0].messaging[0] dropped the rest.
            for entry in body.get("entry", []):
                entry_time = entry.get("time")
                for messaging in entry.get("messaging", []):
                    try:
                        _handle_messaging(messaging, entry_time)
                    except (KeyError, IndexError) as e:
                        logger.error(f"Malformed messaging event: {e}")
                    except Exception:
                        logger.exception("Unexpected error handling messaging event")

            return "EVENT_RECEIVED", 200

        return "Method not allowed", 405

    except Exception as exc:
        logger.exception("Webhook error: %s", exc)
        _notify(f"reel-finder: webhook error: {str(exc)[:150]}")
        return "Internal error", 500


def _handle_messaging(messaging, created_time):
    sender_id = (messaging.get("sender") or {}).get("id")
    message = messaging.get("message") or {}
    mid = message.get("mid")

    # Ice breaker taps arrive as postbacks, not messages.
    postback = messaging.get("postback") or {}
    if postback and not message:
        payload = (postback.get("payload") or "").strip()
        if not sender_id or sender_id == os.environ.get("IG_ID"):
            return
        pb_mid = postback.get("mid") or f"pb:{sender_id}:{int(time.time() * 1000)}"
        if not _claim(pb_mid):
            return
        try:
            logger.info(f"Postback {payload} from {sender_id}")
            if payload in ONBOARD_TEXT or payload.startswith("ONBOARD_"):
                ensure_account(sender_id)
                onboarding_reply(sender_id, payload)
            _finish(pb_mid, True)
        except Exception:
            _release(pb_mid)
            logger.exception(f"Failed to handle postback {payload}")
        return

    if not sender_id or not mid:
        logger.warning("Messaging event without sender or mid; ignored")
        return

    # Skip messages from ourselves
    if sender_id == os.environ.get("IG_ID"):
        return

    # Claim before doing anything: a duplicate delivery loses the race here.
    if not _claim(mid):
        logger.info(f"Skipping already claimed message {mid}")
        return

    try:
        _dispatch(sender_id, message, mid, created_time)
    except Exception:
        _release(mid)
        logger.exception(f"Failed to dispatch mid {mid}")


_LINK_ATTACHMENTS = {"ig_reel", "reel", "ig_post", "share", "story", "ig_story", "story_mention"}
_MEDIA_ATTACHMENTS = {"image", "video", "audio", "file", "media"}
_CDN_HOSTS = ("fbsbx.com", "fbcdn.net", "cdninstagram.com", "scontent")


def _link_kind(url):
    """permalink: permanent instagram.com page. cdn: downloadable media URL."""
    if not url:
        return "none"
    low = url.lower()
    if "instagram.com/" in low and any(p in low for p in ("/reel/", "/p/", "/stories/", "/tv/")):
        return "permalink"
    if any(h in low for h in _CDN_HOSTS):
        return "cdn"
    return "other"


def _attachment_context(sender_id, mid, attachment, created_time):
    """Normalise any Instagram attachment into an ingestion context.

    Meta sends shares (ig_reel, ig_post, legacy share, stories) as permalinks
    and direct media as CDN URLs; both need to land in the same shape.
    """
    att_type = (attachment or {}).get("type")
    payload = (attachment or {}).get("payload") or {}

    if att_type not in _LINK_ATTACHMENTS and att_type not in _MEDIA_ATTACHMENTS \
            and att_type != "ephemeral":
        return None

    url = (payload.get("url") or "").strip()
    kind = _link_kind(url)
    caption = (payload.get("title") or payload.get("caption") or "").strip()

    return {
        "sender_id": sender_id,
        "mid": mid,
        "attachment_type": att_type,
        "reel_id": payload.get("reel_video_id"),
        "post_id": payload.get("ig_post_media_id"),
        "media_id": payload.get("reel_video_id") or payload.get("ig_post_media_id") or payload.get("id"),
        "caption": caption,
        "url": url,
        "link": url,
        "link_kind": kind,
        # A permalink serves HTML, not media; only CDN/direct-media URLs can
        # actually be downloaded and watched.
        "can_download": bool(url) and (kind == "cdn" or att_type in _MEDIA_ATTACHMENTS),
        "created_time": created_time,
    }


def _forward_to_hermes(sender_id, text, mid):
    """Admin-only: send the text to the local Hermes bridge."""
    url = (os.environ.get("BRIDGE_URL") or "").rstrip("/")
    token = os.environ.get("BRIDGE_TOKEN") or ""
    payload = text.split(" ", 1)[1].strip() if " " in text else ""
    if not payload:
        send_error_message(sender_id, "Usage: h <message for Hermes>")
        _finish(mid, True)
        return
    if not url or not token:
        send_error_message(sender_id, "The Hermes bridge is not configured.")
        _finish(mid, True)
        return
    try:
        resp = requests.post(f"{url}/ask", headers={"X-Bridge-Token": token},
                             json={"igsid": sender_id, "text": payload, "mid": mid},
                             timeout=20)
        if resp.status_code in (200, 202):
            send_error_message(sender_id, "Sent to Hermes. I'll reply here when it's done.")
        elif resp.status_code == 429:
            send_error_message(sender_id, "Hermes is busy with an earlier message. Try again shortly.")
        elif resp.status_code == 401:
            send_error_message(sender_id, "The bridge token is wrong. Check BRIDGE_TOKEN.")
        else:
            send_error_message(sender_id, "The bridge refused that message.")
    except requests.RequestException:
        send_error_message(sender_id, "The Hermes bridge is unreachable.")
    _finish(mid, True)


def _handle_link_code(sender_id, text, mid):
    """A LINK-XXXX code proves the DM sender owns the site account."""
    site = (os.environ.get("PUBLIC_SITE_URL") or "https://reelmark.saadsaiyed.com").rstrip("/")
    token = os.environ.get("SERVICE_TOKEN")
    ok = False
    if token:
        try:
            resp = requests.post(f"{site}/internal/link",
                                 headers={"X-Service-Token": token},
                                 json={"igsid": sender_id, "code": text.strip()},
                                 timeout=15)
            ok = resp.status_code == 200
            if not ok:
                logger.info(f"Link code refused: {resp.status_code}")
        except requests.RequestException as exc:
            logger.warning(f"Link code call failed: {exc}")

    if ok:
        ensure_account(sender_id)
        grant_credits(sender_id, FREE_REELS_LINKED, "link", ref=f"link:{sender_id}")
        send_error_message(
            sender_id,
            f"Linked. You have {credit_balance(sender_id)} reels to save. "
            f"Your account: {site}/account",
        )
    else:
        send_error_message(
            sender_id,
            f"That code didn't work. Get a fresh one at {site}/account",
        )
    _finish(mid, True)


def _dispatch(sender_id, message, mid, created_time):
    text = (message.get("text") or "").strip()
    attachments = message.get("attachments") or []

    if attachments:
        allowed, limit_message = check_user_limit(sender_id, "reel")
        if not allowed:
            logger.info(f"Reel limit reached for {sender_id}")
            send_error_message(sender_id, limit_message)
            _finish(mid, True)
            return

        context = None
        for attachment in attachments:
            context = _attachment_context(sender_id, mid, attachment, created_time)
            if context is not None:
                break

        if context is None:
            logger.info(f"Ignoring unsupported attachment types for mid {mid}")
        elif context["attachment_type"] == "ephemeral":
            send_error_message(sender_id, "That media disappears after viewing, so I can't save it.")
            _finish(mid, True)
            return
        else:
            # A reel forwarded together with a message: carry the text through
            # so it can be saved as context or answered as a question.
            context["user_note"] = text
            ensure_account(sender_id)
            mark_reel_seen(sender_id)
            start_typing(sender_id)
            if text:
                record_message(sender_id, "user", text, mid)
            executor.submit(_run_tracked, handle_attachment, context, mid=mid)
            send_reaction(sender_id, mid, "love")

            unprocessed_mids.delete_many({"sender_id": sender_id})
            unprocessed_mids.insert_one({
                "sender_id": sender_id,
                "message": context["caption"],
                "user_note": text,
                "mid": mid,
                "reel_id": context["reel_id"],
                "media_id": context["media_id"],
                "attachment_type": context["attachment_type"],
                "link": context["url"],
                "link_kind": context["link_kind"],
                "created_time": created_time,
            })
            return

    if text:
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS]
        allowed, limit_message = check_user_limit(sender_id, "text")
        if not allowed:
            logger.info(f"Text limit reached for {sender_id}")
            send_error_message(sender_id, limit_message)
            _finish(mid, True)
            return
        replied_to_mid = (message.get("reply_to") or {}).get("mid")

        # Quick-reply taps arrive as a payload, sometimes with the button title
        # as text. Handle both, before the model sees anything.
        qr = (message.get("quick_reply") or {}).get("payload") or ""
        code = qr if qr in ONBOARD_TEXT else (text if text in ONBOARD_TEXT else "")
        if code:
            ensure_account(sender_id)
            onboarding_reply(sender_id, code)
            record_message(sender_id, "user", text or code, mid)
            _finish(mid, True)
            return

        if not code and text.lower().strip() in ("start", "menu", "help") and not is_admin(sender_id):
            ensure_account(sender_id)
            welcome(sender_id)
            record_message(sender_id, "user", text, mid)
            _finish(mid, True)
            return

        existing = accounts.find_one({"sender_id": sender_id}, {"_id": 1})
        ensure_account(sender_id)
        if not existing and not is_admin(sender_id):
            welcome(sender_id)
            record_message(sender_id, "user", text, mid)
            _finish(mid, True)
            return

        if is_admin(sender_id) and text.lower().startswith(("h ", "hermes ")):
            ensure_account(sender_id)
            _forward_to_hermes(sender_id, text, mid)
            return
        if text.lower().startswith("link-"):
            ensure_account(sender_id)
            _handle_link_code(sender_id, text, mid)
            return
        ensure_account(sender_id)
        start_typing(sender_id)
        record_message(sender_id, "user", text, mid)
        executor.submit(
            _run_tracked,
            handle_text_with_gemini_agent,
            sender_id, text, mid, created_time, replied_to_mid,
            mid=mid,
        )
        return

    send_error_message(
        sender_id, "Unsupported attachment type. Please send an Instagram reel or post."
    )
    _finish(mid, True)


def handle_search(sender_id, search_query, mid):
    """Background worker: answer a search command. Returns True on success."""
    try:
        response = send_similar_reel(sender_id, search_query)
        if isinstance(response, dict) and response.get("error"):
            logger.error(f"Search error for mid {mid}: {response.get('error')}")
            # send_similar_reel already told the user what went wrong.
            return False

        logger.info(f"Search response for mid {mid}: {response}")
        send_reaction(sender_id, mid, "love")
        return True
    except Exception as exc:
        logger.exception(f"Error in handle_search for mid {mid}: {exc}")
        send_error_message(sender_id, "Error processing search")
        return False


def handle_attachment(context):
    """Describe a share, or fall back to its caption when the media is unreachable."""
    sender_id = context.get("sender_id")
    mid = context.get("mid")
    url = context.get("url") or ""
    caption = (context.get("caption") or "").strip()
    can_download = bool(context.get("can_download"))
    created_time = context.get("created_time")

    def fail(message):
        # The reel never made it into the index, so drop the "describe your
        # last reel" pointer too; otherwise the next text message attaches
        # context to a reel that does not exist.
        unprocessed_mids.delete_many({"mid": mid})
        send_error_message(sender_id, message)
        return False

    # Refuse before any download or model call when the account is empty.
    if credit_balance(sender_id) < 1:
        return fail(out_of_credits_message(sender_id))

    try:
        description = ""
        source = "caption"
        link_kind = context.get("link_kind") or "none"
        result = ""
        resolution_error = None

        if can_download and url:
            result = run_gemini(url, context.get("attachment_type") in ("ig_reel", "reel"))

        elif link_kind == "permalink" and url:
            # Instagram serves permalinks as HTML; yt-dlp resolves the real media.
            path, resolution_error = resolve_permalink(url)
            if path:
                try:
                    result = describe_media_file(path)
                except Exception as exc:
                    logger.exception(f"Gemini failed on resolved media for mid {mid}")
                    _notify(f"reel-finder: Gemini failed on {url[:80]}: {str(exc)[:120]}")
            elif resolution_error == "rate_limited":
                logger.warning(f"Instagram rate-limited or blocked yt-dlp for {url}")
                _notify("reel-finder: Instagram blocked/rate-limited a download "
                        f"({url[:100]}) — falling back to caption")
            elif resolution_error == "restricted":
                logger.info(f"Post is audience-restricted for mid {mid}: {url}")
            else:
                logger.warning(f"Could not resolve permalink for mid {mid}: {url}")
                _notify(f"reel-finder: could not resolve permalink {url[:100]}")

        if result and result not in ("Gemini API quota exceeded", "Error running Gemini"):
            description = result.strip()
            source = "gemini"
        elif result == "Gemini API quota exceeded":
            logger.warning(f"Gemini quota exceeded for mid {mid}")
            _notify("reel-finder: Gemini quota exceeded while describing a reel")
        elif result:
            logger.warning(f"Gemini failed for {context.get('attachment_type')} "
                           f"mid {mid}: {result}")

        # Permalinks cannot be downloaded (Instagram serves HTML), so the
        # caption is the searchable text we keep instead of failing.
        if not description and caption:
            description = caption

        if not description:
            logger.warning(
                f"No description available for mid {mid}: type={context.get('attachment_type')} "
                f"can_download={can_download} url={url[:60]}"
            )
            return fail("I couldn't read that one. Try forwarding it again, or reply with what it was about.")

        note = (context.get("user_note") or "").strip()
        intent = classify_note(note, description) if note else None
        if intent:
            logger.info(f"Note classified as {intent} for mid {mid}")

        payload = {
            "sender_id": sender_id,
            "message": description,
            "mid": mid,
            "reel_id": context.get("reel_id") or context.get("post_id") or context.get("media_id"),
            "media_id": context.get("media_id"),
            "attachment_type": context.get("attachment_type"),
            "caption": caption,
            "user_note": note,
            # Question reels stay indexed (they are the context for the answer)
            # but are marked so the library can exclude them later.
            "saved_by": "question" if intent == "question" else "forward",
            "link": url,
            "link_kind": context.get("link_kind") or _link_kind(url),
            "description_source": source,
            "created_time": created_time,
        }

        response = store_embeddings(sender_id, [payload])
        if response.get("error"):
            logger.error(
                "Error storing embeddings for mid %s: %s", mid, response.get("error")
            )
            return fail("Error storing data, try again later")

        # The reel is in the index; charge for it now. A replay is a no-op.
        debit_credit(sender_id, mid)

        if intent == "question":
            # Answer from the description we just built; the reel stays indexed.
            answer_question(sender_id, note, description, reply_to_mid=mid)
            return True
        if note and intent == "statement":
            store_embeddings(sender_id, [{
                "sender_id": sender_id,
                "message": note,
                "mid": mid,
                "reel_id": payload["reel_id"],
                "media_id": payload["media_id"],
                "caption": caption,
                "user_note": note,
                "saved_by": "forward",
                "link": url,
                "link_kind": payload["link_kind"],
                "description_source": "user_note",
                "created_time": created_time,
            }])

        if source == "gemini":
            # The index keeps the full scene description; the DM gets one line.
            try:
                reply = one_sentence_summary(description)
            except Exception:
                logger.warning("Summary failed; sending the first sentence")
                reply = description.split(".")[0][:240]
            send_error_message(sender_id, reply or description[:240])
        else:
            if resolution_error == "restricted":
                reason = ("Instagram marks this post as restricted, so it won't show the video "
                          "to me without a logged-in session.")
            elif resolution_error == "rate_limited":
                reason = "Instagram is rate-limiting me right now, so I couldn't open the video."
            elif link_kind == "permalink":
                reason = "I couldn't download this one, so I only have its caption."
            else:
                reason = "I couldn't watch this one, so I only have its caption."

            send_error_message(
                sender_id,
                f"{reason}\n\nSaved caption:\n{description}\n\n"
                f"Reply to this message with what it was about and I'll attach your "
                f"description to it.",
            )
        return True
    except Exception as exc:
        logger.exception("Exception in handle_attachment: %s", exc)
        return fail("Internal error processing your reel")


# new home rout that shows where I am
@app.route("/", methods=["GET", "POST"])
def home():
    # HEAD is served by Flask for GET; request.method is "HEAD" there and used
    # to leave `code` unbound, so initialise it up front.
    code = None
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        logging.info(f"POST request received with data: {payload}")
        code = payload.get("code")
        if code:
            code = code.split("?code=")[-1]
    else:
        logging.info("GET request received for home page")
        code = request.args.get("code")

    if not code:
        logging.info("No code provided in GET request")
        return render_template("index.html", login_link=os.environ.get("LOGIN_URL", "#"))

    logging.info(f"code received: {code}")
    client_id = os.environ.get("INSTA_CLIENT_ID")
    client_secret = os.environ.get("INSTA_CLIENT_SECRET")
    redirect_uri = os.environ.get("INSTA_REDIRECT_URI")
    grant_type = "authorization_code"

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": grant_type,
        "redirect_uri": redirect_uri,
        "code": code,
    }

    response = requests.post("https://api.instagram.com/oauth/access_token", data=payload)
    json_response = response.json()
    logging.info(f"Instagram OAuth response: {json_response}")

    # Store short-lived token temporarily
    short_lived_token = json_response.get("access_token")
    user_id = json_response.get("user_id")

    # Exchange short-lived token for long-lived token (60 days)
    long_lived_response = exchange_for_long_lived_token(
        short_lived_token, client_id, client_secret
    )
    if long_lived_response.get("error"):
        logging.error(
            f"Failed to exchange for long-lived token: {long_lived_response.get('error')}"
        )
        return {"error": "Failed to obtain long-lived token"}, 400

    long_lived_token = long_lived_response.get("access_token")
    expires_in = long_lived_response.get(
        "expires_in", 60 * 24 * 60 * 60
    )  # Default 60 days in seconds

    # Store long-lived token in database
    creds.delete_many({})
    from datetime import timedelta

    creds.insert_one(
        {
            "access_token": long_lived_token,
            "user_id": user_id,
            "expires_in": expires_in,
            "created_at": datetime.now(),
            "expires_at": datetime.now() + timedelta(seconds=expires_in),
            "token_type": "long_lived",
        }
    )

    if request.method == "POST":
        return {"message": "Long-lived token obtained and stored successfully"}, 200

    return render_template("index.html", login_link=None)

@app.route("/oauth/start", methods=["GET"])
def oauth_start():
    """Start the operator token flow. Only this route may set the state cookie."""
    client_id = os.environ.get("INSTA_CLIENT_ID")
    redirect_uri = os.environ.get("INSTA_REDIRECT_URI")
    if not client_id or not redirect_uri:
        return {"error": "INSTA_CLIENT_ID and INSTA_REDIRECT_URI are required"}, 503

    state = secrets.token_urlsafe(24)
    scopes = os.environ.get("INSTA_SCOPES") or (
        "instagram_business_basic,instagram_business_manage_messages"
    )
    authorize = (
        "https://www.instagram.com/oauth/authorize"
        f"?force_reauth=true&client_id={client_id}"
        f"&redirect_uri={redirect_uri}&response_type=code"
        f"&scope={scopes}&state={state}"
    )
    response = redirect(authorize)
    response.set_cookie(
        "oauth_state", state, max_age=600, httponly=True, secure=True,
        samesite="Lax", path="/",
    )
    return response


@app.route("/callback", methods=["GET"])
def callback():
    """
    OAuth callback route - handles redirect from Instagram login.
    Automatically exchanges authorization code for long-lived token.
    Displays all token details including expiration info from Meta.
    """
    try:
        # Get authorization code from URL parameters
        code = request.args.get("code")

        # Without a matching state cookie anyone holding a valid code could
        # swap the bot's token. The cookie is only set by /oauth/start.
        state = request.args.get("state") or ""
        expected_state = request.cookies.get("oauth_state") or ""
        if not state or not expected_state or not hmac.compare_digest(state, expected_state):
            logger.warning("OAuth callback rejected: state missing or mismatched")
            return {"error": "invalid or missing state", "status": "failed"}, 403

        if not code:
            logger.error("No authorization code in callback")
            return {
                "error": "No authorization code received from Instagram",
                "status": "failed",
            }, 400

        logger.info(f"OAuth callback received with code: {code[:20]}...")

        # Get credentials from environment
        client_id = os.environ.get("INSTA_CLIENT_ID")
        client_secret = os.environ.get("INSTA_CLIENT_SECRET")
        redirect_uri = os.environ.get("INSTA_REDIRECT_URI")

        if not all([client_id, client_secret, redirect_uri]):
            logger.error("Missing Instagram credentials in environment")
            return {
                "error": "Server configuration error - missing credentials",
                "status": "failed",
            }, 500

        # Step 1: Exchange code for short-lived access token
        logger.info("Step 1: Exchanging authorization code for short-lived token...")
        short_lived_payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        }

        short_lived_response = requests.post(
            "https://api.instagram.com/oauth/access_token", data=short_lived_payload
        )
        short_lived_data = short_lived_response.json()

        if short_lived_response.status_code != 200 or short_lived_data.get("error"):
            logger.error(f"Failed to get short-lived token: {short_lived_data}")
            return {
                "error": "Failed to obtain short-lived token from Instagram",
                "details": short_lived_data.get("error", {}),
                "status": "failed",
            }, 400

        short_lived_token = short_lived_data.get("access_token")
        user_id = short_lived_data.get("user_id")

        # Only the configured bot account may become the stored token.
        expected_ig_id = os.environ.get("IG_ID")
        if expected_ig_id and str(user_id) != str(expected_ig_id):
            logger.error(
                f"OAuth callback rejected: account {user_id} is not the configured {expected_ig_id}"
            )
            return {
                "error": "this Instagram account is not the configured bot account",
                "status": "failed",
            }, 403

        logger.info(f"✓ Short-lived token obtained for user: {user_id}")
        logger.debug(f"Short-lived response: {short_lived_data}")

        # Step 2: Exchange short-lived token for long-lived token (60 days)
        logger.info("Step 2: Exchanging short-lived token for long-lived token...")
        long_lived_response = exchange_for_long_lived_token(
            short_lived_token, client_id, client_secret
        )

        if long_lived_response.get("error"):
            logger.error(
                f"Failed to exchange for long-lived token: {long_lived_response}"
            )
            return {
                "error": "Failed to exchange for long-lived token",
                "details": long_lived_response.get("error", {}),
                "status": "failed",
            }, 400

        long_lived_token = long_lived_response.get("access_token")
        expires_in = long_lived_response.get("expires_in")  # In seconds

        logger.info(f"✓ Long-lived token obtained")
        logger.debug(f"Long-lived response: {long_lived_response}")

        # Step 3: Calculate expiration details
        from datetime import timedelta

        expires_in_seconds = expires_in if expires_in else 60 * 24 * 60 * 60
        expires_in_days = expires_in_seconds / (24 * 3600)
        expires_at = datetime.now() + timedelta(seconds=expires_in_seconds)

        logger.info(
            f"Token expires in: {expires_in_days:.1f} days ({expires_in_seconds} seconds)"
        )

        # Step 4: Store long-lived token in database with full metadata
        creds.delete_many({})
        token_record = {
            "access_token": long_lived_token,
            "user_id": user_id,
            "expires_in": expires_in_seconds,
            "expires_in_days": expires_in_days,
            "created_at": datetime.now(),
            "expires_at": expires_at,
            "token_type": "long_lived",
            "meta_response": long_lived_response,  # Store full Meta response
            "short_lived_response": short_lived_data,  # Also store short-lived for reference
        }

        inserted = creds.insert_one(token_record)
        logger.info(f"✓ Token stored in database with ID: {inserted.inserted_id}")

        # Step 5: Return comprehensive response with all details
        response_data = {
            "status": "success",
            "message": "Long-lived token obtained and stored successfully",
            "user_id": user_id,
            "token_type": "long_lived",
            "access_token": long_lived_token[:20]
            + "..."
            + long_lived_token[-20:],  # Masked for display
            "expires_in": {
                "seconds": expires_in_seconds,
                "days": round(expires_in_days, 1),
                "hours": round(expires_in_seconds / 3600, 1),
            },
            "expiration": {
                "expires_at": expires_at.isoformat(),
                "expires_at_readable": expires_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            },
            "meta_response": {
                "access_token_length": len(long_lived_token),
                "expires_in": expires_in,
                "user_id": long_lived_response.get("user_id"),
                "token_type": long_lived_response.get("token_type"),
            },
            "database": {
                "stored_at": datetime.now().isoformat(),
                "record_id": str(inserted.inserted_id),
            },
        }

        logger.info("=" * 80)
        logger.info("TOKEN EXCHANGE COMPLETED SUCCESSFULLY")
        logger.info("=" * 80)
        logger.info(f"User ID: {user_id}")
        logger.info(f"Token Type: Long-lived (60 days)")
        logger.info(
            f"Expires In: {expires_in_days:.1f} days ({expires_in_seconds} seconds)"
        )
        logger.info(f"Expires At: {expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info(f"Token Length: {len(long_lived_token)} characters")
        logger.info("=" * 80)

        return response_data, 200

    except Exception as e:
        logger.exception(f"Unexpected error in callback: {e}")
        return {
            "error": "Internal server error during token exchange",
            "details": str(e),
            "status": "failed",
        }, 500

@app.route("/refresh-token", methods=["POST"])
@require_admin
def refresh_token():
    """
    Force a token refresh / report remaining validity.
    The background health loop also does this automatically a week before expiry.
    """
    try:
        token_doc = creds.find_one()
        if not token_doc:
            return {"error": "No token stored in database"}, 400

        result = refresh_long_lived_token(force=True)
        if result.get("error"):
            return {"error": result["error"]}, 400

        return {
            "message": "Token refreshed successfully",
            "expires_in_days": result.get("days_remaining"),
        }, 200

    except Exception as e:
        logger.exception(f"Error refreshing token: {e}")
        return {"error": str(e)}, 500

@app.route("/token-status", methods=["GET"])
@require_admin
def token_status():
    """
    Get the current status and expiration time of the stored token.
    """
    try:
        cred = list(creds.find())
        if not cred:
            return {"status": "no_token", "message": "No token stored in database"}, 404

        token_doc = cred[0]
        expires_at = token_doc.get("expires_at")
        created_at = token_doc.get("created_at")
        user_id = token_doc.get("user_id")

        if expires_at:
            time_until_expiry = (expires_at - datetime.now()).total_seconds()
            status = "valid" if time_until_expiry > 0 else "expired"
            days_remaining = time_until_expiry / (24 * 3600)
        else:
            status = "unknown"
            days_remaining = None

        return {
            "status": status,
            "user_id": user_id,
            "created_at": created_at.isoformat() if created_at else None,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "days_remaining": days_remaining,
            "last_refreshed_at": token_doc.get("last_refreshed_at").isoformat()
            if token_doc.get("last_refreshed_at")
            else None,
        }, 200

    except Exception as e:
        logger.exception(f"Error getting token status: {e}")
        return {"error": str(e)}, 500

@app.route("/conversations/<conversation_id>")
@require_admin
def messages(conversation_id):
    """Developer utility: import a DM history and backfill the vector store.

    Previously this wrote JSON files into the container's cwd and crashed with
    NameError whenever no reel+description pair was found.
    """
    try:
        messages = []
        url = f"{GRAPH}/{conversation_id}/messages"
        params = {
            "fields": "attachments,id,message,from,to,created_time,reactions,shares",
            **_auth_params(),
        }
        for _ in range(10):  # bounded pagination
            page = requests.get(url, params=params, timeout=30).json()
            messages.extend(page.get("data") or [])
            url = (page.get("paging") or {}).get("next")
            params = None
            if not url:
                break

        if not messages:
            return {"error": "No messages returned for that conversation"}, 404

        imported = []
        for i, message in enumerate(messages):
            if (message.get("from") or {}).get("username") == "reel_sync_ai":
                continue
            shares = message.get("shares") or {}
            link = ((shares.get("data") or [{}])[0]).get("link") if shares else None
            if not link or i == 0:
                continue
            previous = messages[i - 1]
            previous_sender = previous.get("from") or {}
            if previous_sender.get("username") == "reel_sync_ai" or previous.get("shares"):
                continue
            if not previous.get("message"):
                continue
            imported.append({
                "id": previous.get("id"),
                "sender_id": previous_sender.get("id"),
                "link": link,
                "link_kind": "permalink" if _is_permalink(link) else "cdn",
                "message": previous.get("message"),
                "timestamp": int(
                    datetime.fromisoformat(previous["created_time"]).timestamp() * 1000
                ),
            })

        if not imported:
            return {"message": "No reel + description pairs found",
                    "scanned": len(messages)}, 200

        collection_name = imported[0]["sender_id"]
        for row in imported:
            store_embeddings(collection_name, [row])

        return {"message": "Imported", "pairs": len(imported),
                "collection": collection_name}, 200
    except Exception as exc:
        logger.exception(f"Error importing conversation: {exc}")
        return {"error": str(exc)}, 500

def _token_warmer():
    """Keep the token cache warm so the typing bubble never waits on Mongo."""
    while True:
        try:
            get_access_token()
        except Exception:
            pass
        time.sleep(45)


def _daily_digest_loop():
    """Send the usage digest once a day at or after DIGEST_HOUR (default 9)."""
    hour = int(os.environ.get("DIGEST_HOUR") or 9)
    while True:
        try:
            admin = os.environ.get("ADMIN_IGSID")
            if admin and datetime.now().hour >= hour:
                day = datetime.now().strftime("%Y-%m-%d")
                marker = f"daily_summary:{day}"
                try:
                    stats.insert_one({"_id": marker, "claimed_at": datetime.now()})
                except DuplicateKeyError:
                    pass
                else:
                    try:
                        text = daily_summary(day)
                        send_error_message(admin, text)
                        _notify(text.replace("\n", " | "))
                        logger.info(f"Daily digest sent for {day}")
                    except Exception:
                        stats.delete_one({"_id": marker})
                        raise
        except Exception:
            logger.warning("Daily digest failed", exc_info=True)
        time.sleep(900)


if os.environ.get("ADMIN_IGSID"):
    threading.Thread(target=_daily_digest_loop, daemon=True).start()

threading.Thread(target=_token_warmer, daemon=True).start()
threading.Thread(target=set_ice_breakers, daemon=True).start()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)), debug=True)