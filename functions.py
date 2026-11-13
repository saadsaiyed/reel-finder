import os, logging, time, asyncio, hashlib, hmac, re, tempfile, threading, requests
from google import genai
from pymongo import ReturnDocument
import uuid
import yt_dlp
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams
from pymongo import MongoClient
from datetime import datetime, timedelta

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Instagram API with Instagram Login -> graph.instagram.com
# Instagram API with Facebook Login -> graph.facebook.com (Page access token)
GRAPH_BASE = (os.environ.get("GRAPH_BASE") or "https://graph.instagram.com").rstrip("/")
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v22.0")
GRAPH = f"{GRAPH_BASE}/{GRAPH_VERSION}"

# Model names deprecate per project; keep them configurable.
GEMINI_VISION_MODEL = os.environ.get("GEMINI_VISION_MODEL") or "gemini-3.5-flash"

# Initialize database connections
db_connection_string = os.getenv("DB_CONNECTION_STRING")
if not db_connection_string:
    logger.error("DB_CONNECTION_STRING not set")
    
db_client = MongoClient(str(db_connection_string))
creds = db_client["master"]["creds"]
users = db_client["master"]["users"]
processed = db_client["master"]["processed_mids"]
unprocessed_mids = db_client["master"]["unprocessed_mids"]
messages_col = db_client["master"]["messages"]
accounts = db_client["master"]["accounts"]
credit_ledger = db_client["master"]["credit_ledger"]


# --------------------------------------------------------------------------
# Conversation transcript and account seam.
# --------------------------------------------------------------------------

TRANSCRIPT_TURNS = 20

# Freemium allowance. One reel saved costs one credit.
FREE_REELS_ANON = int(os.environ.get("FREE_REELS_ANON") or 10)
FREE_REELS_LINKED = int(os.environ.get("FREE_REELS_LINKED") or 10)

# Abuse limits. Every one is env-tunable so you can change them in CasaOS
# without a rebuild.
TEXT_PER_HOUR = int(os.environ.get("LIMIT_TEXT_PER_HOUR") or 30)
REELS_PER_DAY = int(os.environ.get("LIMIT_REELS_PER_DAY") or 40)
GEMINI_DAILY_CALL_CAP = int(os.environ.get("GEMINI_DAILY_CALL_CAP") or 800)
MAX_MESSAGE_CHARS = int(os.environ.get("MAX_MESSAGE_CHARS") or 1500)

stats = db_client["master"]["stats"]


def _today():
    return datetime.utcnow().strftime("%Y-%m-%d")


def budget_available():
    doc = stats.find_one({"_id": f"gemini_calls:{_today()}"}) or {}
    return int(doc.get("count", 0)) < GEMINI_DAILY_CALL_CAP


def gemini_generate(contents, model=None):
    """Single funnel for Gemini calls so the global daily budget is enforced."""
    if not budget_available():
        logger.error("Gemini daily call budget exhausted; refusing the call")
        return None
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model=model or GEMINI_VISION_MODEL, contents=contents
    )
    stats.update_one({"_id": f"gemini_calls:{_today()}"}, {"$inc": {"count": 1}}, upsert=True)
    return response


def check_user_limit(sender_id, kind):
    """Per-sender quota. kind is 'text' (hourly) or 'reel' (daily).

    Returns (allowed, message_for_the_user).
    """
    now = datetime.now()
    doc = accounts.find_one({"sender_id": sender_id}) or {}

    if kind == "reel":
        day = now.strftime("%Y-%m-%d")
        used = int(doc.get("reels_day_used", 0)) if doc.get("reels_day") == day else 0
        if used >= REELS_PER_DAY:
            return False, (f"You've reached today's limit of {REELS_PER_DAY} reels. "
                           "Send the rest tomorrow.")
        if doc.get("reels_day") == day:
            accounts.update_one({"sender_id": sender_id}, {"$inc": {"reels_day_used": 1}})
        else:
            accounts.update_one({"sender_id": sender_id},
                                {"$set": {"reels_day": day, "reels_day_used": 1}}, upsert=True)
        return True, ""

    hour = now.strftime("%Y-%m-%dT%H")
    used = int(doc.get("text_hour_used", 0)) if doc.get("text_hour") == hour else 0
    if used >= TEXT_PER_HOUR:
        return False, (f"That's {TEXT_PER_HOUR} messages in an hour, which is my limit. "
                       "Give me a few minutes.")
    if doc.get("text_hour") == hour:
        accounts.update_one({"sender_id": sender_id}, {"$inc": {"text_hour_used": 1}})
    else:
        accounts.update_one({"sender_id": sender_id},
                            {"$set": {"text_hour": hour, "text_hour_used": 1}}, upsert=True)
    return True, ""


def ensure_account(sender_id):
    """Create the account on first contact and grant the anonymous allowance."""
    try:
        doc = accounts.find_one_and_update(
            {"sender_id": sender_id},
            {
                "$setOnInsert": {"plan": "free", "status": "active",
                                 "created_at": datetime.now(),
                                 "credits": FREE_REELS_ANON},
                "$set": {"last_seen": datetime.now()},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        # Accounts created before credits existed get the same allowance once.
        accounts.update_one(
            {"sender_id": sender_id, "credits": {"$exists": False}},
            {"$set": {"credits": FREE_REELS_ANON}},
        )
        return doc
    except Exception:
        logger.warning("Could not upsert account record", exc_info=True)
        return None


def credit_balance(sender_id):
    doc = accounts.find_one({"sender_id": sender_id}, {"credits": 1})
    return int((doc or {}).get("credits", 0))


def _log_credit(sender_id, delta, reason, ref, balance):
    try:
        credit_ledger.insert_one({
            "sender_id": sender_id, "delta": delta, "reason": reason,
            "ref": ref, "balance_after": balance, "created_at": datetime.now(),
        })
    except Exception:
        logger.warning("Could not write credit ledger row", exc_info=True)


def debit_credit(sender_id, ref=None):
    """Charge one credit atomically.

    Returns the new balance, the current balance if this ref was already
    charged, or False when the account is empty.
    """
    if ref and credit_ledger.find_one({"sender_id": sender_id, "ref": ref,
                                       "delta": -1}):
        return credit_balance(sender_id)
    doc = accounts.find_one_and_update(
        {"sender_id": sender_id, "credits": {"$gte": 1}},
        {"$inc": {"credits": -1}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        return False
    balance = int(doc.get("credits", 0))
    _log_credit(sender_id, -1, "consume", ref, balance)
    return balance


def grant_credits(sender_id, units, reason, ref=None):
    """Add credits. Idempotent when ref is supplied."""
    if ref and credit_ledger.find_one({"sender_id": sender_id, "ref": ref,
                                       "reason": reason}):
        return credit_balance(sender_id)
    doc = accounts.find_one_and_update(
        {"sender_id": sender_id},
        {"$inc": {"credits": int(units)}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    balance = int(doc.get("credits", 0))
    _log_credit(sender_id, int(units), reason, ref, balance)
    return balance


def buy_link(sender_id, ttl=24 * 3600):
    """Signed, short-lived upgrade link so a forwarded DM cannot probe others."""
    base = os.environ.get("PUBLIC_SITE_URL") or "https://reelmark.saadsaiyed.com"
    secret = os.environ.get("FLASK_SECRET_KEY") or ""
    expires = int(time.time()) + ttl
    sig = hmac.new(secret.encode(), f"{sender_id}:{expires}".encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{base}/upgrade?u={sender_id}&e={expires}&s={sig}"


def account_message(sender_id):
    """Deterministic account reply; the model never writes the balance or URL."""
    doc = accounts.find_one({"sender_id": sender_id}) or {}
    plan = doc.get("plan") or "free"
    credits = int(doc.get("credits", 0))
    noun = "reel" if credits == 1 else "reels"
    return (f"You have {credits} {noun} left on the {plan} plan.\n"
            "Search and questions are free and unlimited.\n"
            f"Top up or change plan: {buy_link(sender_id)}")


def out_of_credits_message(sender_id):
    return ("You're out of reels for now. Search and questions stay free.\n"
            f"Get more reels: {buy_link(sender_id)}")


# --------------------------------------------------------------------------
# Admin control from the DM, and the daily digest.
# --------------------------------------------------------------------------

def admin_igsid():
    return os.environ.get("ADMIN_IGSID") or ""


def is_admin(sender_id):
    return bool(admin_igsid()) and str(sender_id) == str(admin_igsid())


def daily_summary(day=None):
    """Usage digest for one day (local date). Deterministic, no model call."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    start = datetime.strptime(day, "%Y-%m-%d")
    end = start + timedelta(days=1)
    window = {"created_at": {"$gte": start, "$lt": end}}

    reels = credit_ledger.count_documents({**window, "delta": -1})
    senders = len(credit_ledger.distinct("sender_id", {**window, "delta": -1}))
    new_accounts = accounts.count_documents({"created_at": {"$gte": start, "$lt": end}})
    calls = (stats.find_one({"_id": f"gemini_calls:{day}"}) or {}).get("count", 0)
    total_accounts = accounts.count_documents({})
    total_credits = sum(
        int((d or {}).get("credits", 0))
        for d in accounts.find({}, {"credits": 1})
    )
    label = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b")
    return (f"Reelmark — {label}\n"
            f"Reels saved: {reels}\n"
            f"Active senders: {senders}\n"
            f"New accounts: {new_accounts}\n"
            f"Credits spent: {reels}\n"
            f"Gemini calls: {calls}\n"
            f"Accounts total: {total_accounts} ({total_credits} credits held)")


def admin_help():
    return ("Admin commands:\n"
            "#summary - today's usage\n"
            "#say <igsid> <text> - send a DM as the bot\n"
            "#grant <igsid> <n> - add credits\n"
            "#who <igsid> - account details\n"
            "#recent - last 5 active senders")


def admin_command(sender_id, text):
    """Handle an operator command. Returns the reply, or None if not a command."""
    if not is_admin(sender_id):
        return None
    text = (text or "").strip()
    if not text.startswith("#"):
        return None

    parts = text.split(maxsplit=3)
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("#help", "#?"):
        return admin_help()

    if cmd == "#summary":
        return daily_summary()

    if cmd == "#recent":
        rows = list(credit_ledger.find({"delta": -1}, {"sender_id": 1, "created_at": 1})
                    .sort("_id", -1).limit(5))
        if not rows:
            return "No reels saved yet."
        return "Recent senders:\n" + "\n".join(
            f"{r['sender_id']} — {r.get('created_at', '')}" for r in rows
        )

    if cmd == "#say" and len(args) >= 2:
        target = args[0]
        message = text.split(args[0], 1)[1].strip()
        if not message:
            return "Usage: #say <igsid> <text>"
        ok = send_error_message(target, message)
        record_message(sender_id, "bot", f"[admin -> {target}] {message}")
        return "Sent." if ok else "Send failed, check the logs."

    if cmd == "#grant" and len(args) >= 2:
        try:
            units = int(args[1])
        except ValueError:
            return "Usage: #grant <igsid> <n>"
        balance = grant_credits(args[0], units, "admin_grant",
                                ref=f"admin:{sender_id}:{int(time.time())}")
        return f"Granted {units}. Balance is now {balance}."

    if cmd == "#who" and args:
        doc = accounts.find_one({"sender_id": args[0]}) or {}
        if not doc:
            return f"No account for {args[0]}."
        return (f"{doc.get('sender_id')}\n"
                f"plan: {doc.get('plan')}\n"
                f"credits: {doc.get('credits')}\n"
                f"seen: {doc.get('last_seen')}\n"
                f"reels today: {doc.get('reels_day_used')}")

    return "Unknown command. Try #help."


def mark_reel_seen(sender_id):
    """Record that a reel just arrived, so text can wait for its description."""
    try:
        accounts.update_one({"sender_id": sender_id},
                            {"$set": {"last_reel_at": datetime.now()}}, upsert=True)
    except Exception:
        logger.warning("Could not mark last_reel_at", exc_info=True)


def reel_seen_recently(sender_id, seconds=180):
    doc = accounts.find_one({"sender_id": sender_id}, {"last_reel_at": 1}) or {}
    ts = doc.get("last_reel_at")
    return bool(ts) and (datetime.now() - ts).total_seconds() <= seconds


def send_quick_replies(sender_id, text, buttons):
    """Onboarding buttons. Each button sends its payload back as a message."""
    body = {
        "recipient": {"id": sender_id},
        "message": {
            "text": text,
            "quick_replies": [
                {"content_type": "text", "title": title[:20], "payload": payload}
                for title, payload in buttons
            ],
        },
    }
    try:
        resp = requests.post(f"{GRAPH}/me/messages", params=_auth_params(),
                             json=body, timeout=20)
        if resp.status_code != 200:
            logger.warning("quick replies rejected: %s %s", resp.status_code, resp.text[:160])
            return send_error_message(sender_id, text)
        return True
    except requests.RequestException as exc:
        logger.warning("quick replies failed: %s", exc)
        return send_error_message(sender_id, text)


ONBOARD_BUTTONS = [
    ("How it works", "ONBOARD_HOW"),
    ("Send my first reel", "ONBOARD_SEND"),
    ("Search", "ONBOARD_SEARCH"),
    ("Pricing", "ONBOARD_PRICE"),
]

ONBOARD_TEXT = {
    "ONBOARD_HOW": (
        "Three steps. Forward any reel to me, I write down what happens in it, "
        "then you describe it later and I send it back.\n\n"
        "Search is free forever. Saving costs one reel from your balance, and you start with ten."
    ),
    "ONBOARD_SEND": (
        "Open the reel, tap the share arrow, and pick @reelmark_ai. "
        "I'll read it and tell you what I saw."
    ),
    "ONBOARD_SEARCH": (
        "Type `search` and then whatever you remember, however vague. "
        "For example: `search the pasta place with the orange awning`."
    ),
    "ONBOARD_PRICE": (
        "Ten reels free, plus ten more when you link an account on the site. "
        "Then $3.99 for the first month (20 reels), $9.99 for 40 a month, "
        "$19.99 for 100. Search never costs anything."
    ),
}


def welcome(sender_id):
    return send_quick_replies(
        sender_id,
        "I keep the reels you send me, so you can find them later by describing them. "
        "Search is free forever. Where do you want to start?",
        ONBOARD_BUTTONS,
    )


def onboarding_reply(sender_id, code):
    """Answer a quick-reply tap, then re-offer the buttons."""
    body = ONBOARD_TEXT.get(code)
    if not body:
        return False
    return send_quick_replies(sender_id, body, ONBOARD_BUTTONS)


def record_message(sender_id, role, text, mid=None):
    """Append to the per-sender transcript, keeping the newest 20 turns."""
    text = (text or "").strip()
    if not text:
        return
    try:
        messages_col.insert_one({
            "sender_id": sender_id,
            "role": role,
            "text": text[:4000],
            "mid": mid,
            "created_at": datetime.now(),
        })
        stale = [d["_id"] for d in
                 messages_col.find({"sender_id": sender_id}, {"_id": 1})
                 .sort("_id", -1).skip(TRANSCRIPT_TURNS)]
        if stale:
            messages_col.delete_many({"_id": {"$in": stale}})
    except Exception:
        logger.warning("Could not record transcript line", exc_info=True)


def recent_transcript(sender_id, limit=TRANSCRIPT_TURNS):
    """Last N turns as 'User: ...' / 'Bot: ...' lines, oldest first."""
    try:
        rows = list(messages_col.find({"sender_id": sender_id}, {"_id": 0})
                    .sort("_id", -1).limit(limit))
    except Exception:
        logger.warning("Could not read transcript", exc_info=True)
        return ""
    lines = [f"{'User' if r.get('role') == 'user' else 'Bot'}: {r.get('text', '')}"
             for r in reversed(rows)]
    return "\n".join(lines)


def _note_is_question(text):
    """Cheap fallback when the model call is unavailable."""
    low = (text or "").strip().lower()
    if not low:
        return False
    if low.endswith("?"):
        return True
    return low.split()[0] in {"who", "what", "when", "where", "why", "how",
                              "is", "are", "was", "were", "do", "does", "did",
                              "can", "could", "should", "would", "will"}


def classify_note(text, description=""):
    """Is the message with a reel a statement (save it) or a question (answer it)?"""
    text = (text or "").strip()
    if not text:
        return "statement"
    try:
        prompt = ("Classify the user's message about an Instagram reel they just sent. "
                  "Reply with exactly one word: question or statement.\n\n"
                  f"Reel description: {(description or '')[:1500]}\n\n"
                  f"User message: {text}")
        response = gemini_generate(prompt, model=os.environ.get("GEMINI_MODEL"))
        verdict = ((response.text if response else "") or "").strip().lower()
        if "question" in verdict:
            return "question"
        if "statement" in verdict:
            return "statement"
    except Exception as exc:
        logger.warning(f"Note classification failed; using heuristic: {exc}")
    return "question" if _note_is_question(text) else "statement"

# Initialize Qdrant client
qdrant_client = QdrantClient(url=os.environ.get("QDRANT_URL"), api_key=os.environ.get("QDRANT_API_KEY"))

# Initialize embedding model
EMBEDDING_MODEL = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")


def detect_file_type(response):
    """Detect file type from response headers or magic bytes."""
    # Check Content-Type header first
    content_type = response.headers.get('content-type', '').lower()
    
    if 'video' in content_type:
        return 'video', 'mp4'
    elif 'image' in content_type:
        return 'image', 'jpg'
    
    # Fall back to magic bytes detection
    content = response.content[:12]
    
    # Video magic bytes
    if content.startswith(b'\x00\x00\x00\x18ftypisom'):  # MP4
        return 'video', 'mp4'
    elif content.startswith(b'\x00\x00\x00\x20ftyp'):  # MP4 variant
        return 'video', 'mp4'
    elif content.startswith(b'ftypmp42'):  # MP4 variant
        return 'video', 'mp4'
    
    # Image magic bytes
    elif content.startswith(b'\xFF\xD8\xFF'):  # JPEG
        return 'image', 'jpg'
    elif content.startswith(b'\x89PNG\r\n\x1a\n'):  # PNG
        return 'image', 'png'
    elif content.startswith(b'GIF87a') or content.startswith(b'GIF89a'):  # GIF
        return 'image', 'gif'
    elif content.startswith(b'WEBP'):  # WebP
        return 'image', 'webp'
    
    # Default to mp4 if unknown
    logger.warning(f"Could not determine file type from Content-Type: {content_type}, magic bytes: {content.hex()[:20]}... - defaulting to mp4")
    return 'video', 'mp4'

DEFAULT_PROMPT = ("With simple texts only and no `here you go...` or `following is:...` types "
                  "of statements, for each scene in this video, generate captions that describe "
                  "the scene along with any spoken text placed in quotation marks without "
                  "timestamp. Provide your explanation. Only respond with what is asked. "
                  "\nExample: A guy tasting something spicy and can't control his emotions and tears up.")

SUMMARY_PROMPT = ("Rewrite this video description as ONE short sentence of at most 25 words. "
                  "No timestamps, no scene numbers, no preamble, no quotes around the sentence.\n\n")


def _first_sentence(text):
    """Strip leading timestamps and return the first sentence, capped."""
    cleaned = re.sub(r"^\s*\d{1,2}:\d{2}(?:\s*[-\u2013]\s*\d{1,2}:\d{2})?\s*:?\s*", "", (text or "").strip())
    cleaned = " ".join(cleaned.split())
    match = re.match(r"(.+?[.!?])(\s|$)", cleaned)
    sentence = (match.group(1) if match else cleaned).strip()
    return sentence[:240]


def one_sentence_summary(text):
    """Collapse a scene-by-scene description into one sentence for the DM reply.

    The full description stays in the vector index; only the reply is shortened.
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        response = gemini_generate(SUMMARY_PROMPT + text[:4000],
                                   model=os.environ.get("GEMINI_MODEL"))
        sentence = _first_sentence((response.text if response else "") or "")
        if sentence:
            return sentence
    except Exception as exc:
        logger.warning(f"Summary failed; using first sentence instead: {exc}")
    return _first_sentence(text)


def describe_media_file(filename):
    """Upload one local media file to Gemini and return its description.

    Raises on failure so callers can distinguish quota from a bad file. The
    temp file is always removed.
    """
    if not budget_available():
        raise RuntimeError("daily gemini budget exhausted")
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    try:
        media = client.files.upload(file=filename)
        logger.debug(f"Completed upload: {media.uri}")

        while media.state.name == "PROCESSING":
            time.sleep(2)
            media = client.files.get(name=media.name)

        if media.state.name == "FAILED":
            raise ValueError("Gemini could not process the file")

        prompt = os.environ.get("GEMINI_PROMPT", DEFAULT_PROMPT)
        response = gemini_generate([media, prompt], model=GEMINI_VISION_MODEL)
        if response is None:
            raise RuntimeError("daily gemini budget exhausted")
        return response.text or ""
    finally:
        try:
            os.remove(filename)
        except OSError:
            pass


async def gemini(url, is_reel=True):
    """Download a CDN media URL and describe it. Returns "" if it is not media."""
    if not url:
        return ""

    logger.debug(f"Downloading file from: {url}")
    response = requests.get(url)
    if response.status_code != 200:
        logger.error(f"Failed to download file: {response.status_code}")
        return ""

    # A permalink returns Instagram's web page, not media. Never upload HTML
    # to Gemini: it burns a request and always fails processing.
    head = response.content[:512].lstrip().lower()
    if response.headers.get("content-type", "").lower().startswith("text/html") \
            or head.startswith(b"<!doctype") or head.startswith(b"<html"):
        logger.warning(f"URL returned a web page, not media: {url}")
        return ""

    # Detect file type
    file_type, extension = detect_file_type(response)
    # Unique temp file per download: the thread pool processes reels concurrently
    # and a fixed name would let one reel overwrite another mid-upload.
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{extension}',
                                         prefix='reel_', dir='/tmp')
    filename = handle.name
    handle.close()

    logger.info(f"Detected {file_type} file ({extension}): {filename}")

    with open(filename, 'wb') as f:
        f.write(response.content)
    logger.debug(f"File downloaded and saved: {filename} ({len(response.content)} bytes)")

    return describe_media_file(filename)


def _is_block_error(message):
    low = (message or "").lower()
    return any(k in low for k in (
        "429", "too many requests", "rate limit", "login required",
        "checkpoint", "blocked", "403 forbidden", "not available",
    ))


def _is_restricted_error(message):
    low = (message or "").lower()
    return any(k in low for k in (
        "isn't available to everyone", "is not available to everyone",
        "restricted", "age-restricted", "sensitive content",
    ))


def resolve_permalink(url):
    """Download the media behind an Instagram permalink with yt-dlp.

    Returns (path, error_kind): error_kind is None on success, "rate_limited"
    when Instagram refused the request, "failed" otherwise. Instagram serves
    permalinks as HTML, so this is the only way to actually watch a shared reel.
    """
    tmpdir = tempfile.mkdtemp(prefix="ytdl_", dir="/tmp")
    opts = {
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "format": "3/b[ext=mp4]/b",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 1,
        "socket_timeout": 20,
        "max_filesize": 150 * 1024 * 1024,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)

        if not os.path.exists(path):
            candidates = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)
                          if not f.endswith((".part", ".ytdl"))]
            path = candidates[0] if candidates else ""

        if not path or not os.path.exists(path):
            logger.error(f"yt-dlp produced no file for {url}")
            return None, "failed"

        logger.info(f"yt-dlp resolved {url} -> {os.path.basename(path)} "
                    f"({os.path.getsize(path)} bytes)")
        return path, None
    except Exception as exc:
        message = str(exc)
        if _is_restricted_error(message):
            kind = "restricted"
        elif _is_block_error(message):
            kind = "rate_limited"
        else:
            kind = "failed"
        logger.warning(f"yt-dlp failed ({kind}) for {url}: {message[:200]}")
        return None, kind


def resolve_media_url(url):
    """Return a fresh direct media URL for a permalink, without downloading.

    Instagram's CDN URLs are signed and short-lived, so this has to run at send
    time. Returns (url, error_kind).
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "3/b[ext=mp4]/b",
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        direct = info.get("url")
        if not direct:
            for fmt in reversed(info.get("formats") or []):
                if fmt.get("url"):
                    direct = fmt["url"]
                    break
        if not direct:
            return None, "failed"
        return direct, None
    except Exception as exc:
        message = str(exc)
        if _is_restricted_error(message):
            kind = "restricted"
        elif _is_block_error(message):
            kind = "rate_limited"
        else:
            kind = "failed"
        logger.warning(f"yt-dlp URL resolve failed ({kind}) for {url}: {message[:160]}")
        return None, kind


def _point_id(mid, text):
    """Deterministic id: re-ingesting the same mid+text overwrites instead of
    duplicating, and two reels can no longer collide (uuid4 % 10^12 could)."""
    digest = hashlib.sha256(f"{mid or ''}:{text or ''}".encode("utf-8")).hexdigest()
    return int(digest[:15], 16)


def _is_permalink(link):
    return bool(link) and ("instagram.com/reel/" in link or "instagram.com/p/" in link)


def _ensure_collection(collection_name):
    try:
        qdrant_client.get_collection(collection_name)
        return
    except Exception:
        pass
    qdrant_client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE),
    )
    logger.info(f"Created new collection: {collection_name}")
    try:
        # Makes find_point_by_mid a filtered lookup instead of a full scroll.
        qdrant_client.create_payload_index(
            collection_name=collection_name,
            field_name="mid",
            field_schema="keyword",
        )
    except Exception as exc:
        logger.warning(f"Could not create mid payload index: {exc}")


def store_embeddings(collection_name, messages):
    try:
        _ensure_collection(collection_name)

        embeddings_list = []
        for message in messages:
            embedding = EMBEDDING_MODEL.embed_query(message.get("message"))
            embeddings_list.append({
                "id": _point_id(message.get("mid"), message.get("message")),
                "vector": embedding,
                "payload": message
            })

        qdrant_client.upsert(
            collection_name=collection_name,
            points=embeddings_list
        )
        logger.info(f"Stored {len(embeddings_list)} point(s) in collection {collection_name}")
        return {"message": "Embeddings stored successfully"}
    except Exception as exc:
        # qdrant-client raises its own exception types, not requests exceptions.
        logger.exception(f"Error in store_embeddings: {exc}")
        return {"error": f"Error storing embeddings: {exc}"}


def get_similar_messages(collection_name, text, limit=1):
    try:
        embedding = EMBEDDING_MODEL.embed_query(text)
        response = qdrant_client.query_points(
            collection_name=collection_name,
            query=embedding,
            limit=limit
        )
        return response.points
    except Exception as exc:
        logger.exception(f"Error in get_similar_messages: {exc}")
        return {"error": f"Error retrieving similar messages: {exc}"}


def _send_video(sender_id, link, reply_to_mid=None):
    body = {
        "recipient": {"id": sender_id},
        "message": {"attachment": {"type": "video", "payload": {"url": link}}},
    }
    if reply_to_mid:
        # Instagram expects reply_to at the top level; inside `message` it is
        # rejected with "Invalid keys".
        body["reply_to"] = {"mid": reply_to_mid}

    response = requests.post(
        f"{GRAPH}/me/messages",
        params=_auth_params(),
        json=body,
        timeout=30,
    )
    ok = response.status_code == 200
    if not ok:
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {"message": response.text[:200]}
        logger.error(f"Send API rejected reel ({response.status_code}): {error}")
    return ok


def saved_reel_count(sender_id):
    """How many reels this sender has indexed. 0 also means no collection."""
    try:
        info = qdrant_client.get_collection(sender_id)
        return int(getattr(info, "points_count", 0) or 0)
    except Exception:
        return 0


def send_similar_reel(sender_id, text):
    try:
        logger.info("Started send_similar_reel")
        if saved_reel_count(sender_id) == 0:
            send_error_message(
                sender_id,
                "You haven't saved any reels yet. Forward one to me and I'll remember it, "
                "then search works on your own library.",
            )
            return {"message": "Empty library"}
        results = get_similar_messages(collection_name=sender_id, text=text)
        if isinstance(results, dict) and results.get("error"):
            send_error_message(sender_id, "I couldn't search just now. Try again in a moment.")
            return results
        if not isinstance(results, list):
            return {"error": "Error retrieving similar messages."}
        if not results:
            logger.info("No results found.")
            send_error_message(sender_id, "No similar reels found. Try a different search query.")
            return {"error": "No similar messages found."}

        payload = results[0].payload or {}
        description = payload.get("message") or ""
        link = payload.get("permalink") or payload.get("link") or ""
        link_kind = payload.get("link_kind") or ("permalink" if _is_permalink(link) else "cdn")
        # Replying to the user's original forward keeps the video in context.
        original_mid = payload.get("mid")

        # A permalink opens the reel inside Instagram; it is not a media URL. Get
        # a fresh signed CDN URL and send the video inline so the user does not
        # have to open a browser.
        if link and link_kind == "permalink":
            media_url, resolve_error = resolve_media_url(link)
            if media_url and _send_video(sender_id, media_url, reply_to_mid=original_mid):
                return {"message": "Reel sent inline"}
            if resolve_error in ("rate_limited", "restricted"):
                logger.info(f"Inline send unavailable ({resolve_error}); sending link for {link}")
            body = f"Closest match: {description}\n\n{link}" if description else f"Closest match:\n{link}"
            send_error_message(sender_id, body.strip(), reply_to_mid=original_mid)
            return {"message": "Link sent"}

        if link and _send_video(sender_id, link, reply_to_mid=original_mid):
            return {"message": "Reel sent"}

        if description:
            body = f"Closest match: {description}"
            if link:
                body += f"\n\n{link}"
            else:
                body += "\n\n(This one's media link has expired, so here is the description.)"
            send_error_message(sender_id, body, reply_to_mid=original_mid)
            return {"message": "Description fallback sent"}

        if link:
            send_error_message(sender_id, f"Closest match:\n{link}", reply_to_mid=original_mid)
            return {"message": "Link only"}

        send_error_message(sender_id, "Found a match but couldn't resend it. Try another search.")
        return {"error": "No resendable match"}
    except Exception as exc:
        logger.exception(f"Error in send_similar_reel: {exc}")
        send_error_message(sender_id, "Error finding similar reel")
        return {"error": f"Error sending similar reel response: {exc}"}

def split_message(text, max_length=1000):
    """
    Split a long message into multiple chunks at word/sentence boundaries.
    Tries to break at space or period, whichever is closer to max_length.
    Returns a list of message chunks.
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    remaining = text
    
    while len(remaining) > max_length:
        # Find the best break point within the last 200 chars of the max_length window
        search_start = max(0, max_length - 200)
        search_end = max_length
        
        # Look for period first (preferred)
        last_period = remaining.rfind('.', search_start, search_end)
        last_space = remaining.rfind(' ', search_start, search_end)
        
        # Choose the closest break point to max_length
        if last_period >= search_start:
            break_point = last_period + 1  # Include the period
        elif last_space >= search_start:
            break_point = last_space + 1  # Include the space
        else:
            # Fallback: just break at max_length
            break_point = max_length
        
        chunks.append(remaining[:break_point].strip())
        remaining = remaining[break_point:].strip()
    
    if remaining:
        chunks.append(remaining)
    
    logger.debug(f"Split message into {len(chunks)} chunks: {[len(c) for c in chunks]}")
    return chunks

def _messaging_token():
    """Token used for Send API calls.

    Instagram Login deployments use the stored Instagram user token.
    Facebook Login deployments use a Page access token derived from a
    system-user token, supplied as INSTA_MESSAGING_TOKEN.
    """
    return os.environ.get("INSTA_MESSAGING_TOKEN") or get_access_token() or ""


def _auth_params():
    """Graph API query params: the token plus appsecret_proof when configured.

    appsecret_proof is an HMAC of the access token keyed by the app secret, so
    a leaked token is useless without the secret as well.
    """
    token = _messaging_token()
    params = {"access_token": token}
    secret = os.environ.get("INSTA_APP_SECRET")
    if secret and token:
        params["appsecret_proof"] = hmac.new(
            secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    return params


def send_sender_action(sender_id, action):
    """typing_on / typing_off. Failures are logged, never raised."""
    try:
        requests.post(
            f"{GRAPH}/me/messages",
            params=_auth_params(),
            json={"recipient": {"id": sender_id}, "sender_action": action},
            timeout=10,
        )
    except Exception as exc:
        logger.debug(f"sender_action {action} failed: {exc}")


_typing_stops = {}


def start_typing(sender_id, max_seconds=120):
    """Show the typing bubble until stop_typing or the cap, whichever comes first."""
    stop = threading.Event()
    _typing_stops[str(sender_id)] = stop

    def loop():
        waited = 0
        while not stop.is_set() and waited < max_seconds:
            send_sender_action(sender_id, "typing_on")
            stop.wait(7)
            waited += 7
        # If stop_typing already cleared it, do not send a second off.
        if not stop.is_set():
            send_sender_action(sender_id, "typing_off")
        _typing_stops.pop(str(sender_id), None)

    threading.Thread(target=loop, daemon=True).start()


def stop_typing(sender_id):
    event = _typing_stops.pop(str(sender_id), None)
    if event is not None:
        event.set()
        send_sender_action(sender_id, "typing_off")


def send_error_message(sender_id, error_message, reply_to_mid=None):
    """Send error message(s) to user, splitting into multiple messages if needed."""
    stop_typing(sender_id)
    # Split message if it exceeds 1000 characters
    message_chunks = split_message(error_message, max_length=1000)

    for i, chunk in enumerate(message_chunks):
        payload = {
            "recipient": {"id": sender_id},
            "message": {"text": chunk},
        }
        if reply_to_mid and i == 0:
            payload["reply_to"] = {"mid": reply_to_mid}
        logger.info(f"Sending message chunk {i+1}/{len(message_chunks)} ({len(chunk)} chars)")
        response = requests.post(
            f"{GRAPH}/me/messages",
            params=_auth_params(),
            json=payload,
            timeout=30,
        )
        if response.status_code != 200:
            try:
                detail = response.json()["error"]["message"]
            except (ValueError, KeyError):
                detail = response.text[:200]
            logger.error(f"Error sending message chunk {i+1}: {detail}")
            return False

        record_message(sender_id, "bot", chunk)

        # Small delay between messages to avoid rate limiting
        if i < len(message_chunks) - 1:
            time.sleep(0.2)
    
    return True

def send_reaction(sender_id, message_id, reaction_type=os.getenv("DEFAULT_REACTION_TYPE", "love")):
    payload = {
        "recipient": {"id": sender_id},
        "sender_action": "react", # Or set to unreact to remove the reaction
        "payload": {
            "message_id": message_id,
            "reaction": reaction_type # Omit if removing a reaction
        }
    }
    response = requests.post(
        f"{GRAPH}/me/messages/",
        params=_auth_params(),
        json=payload,
        timeout=30,
    )
    if response.status_code != 200:
        try:
            detail = response.json()["error"]["message"]
        except (ValueError, KeyError):
            detail = response.text[:200]
        logger.error(f"Error sending reaction: {detail}")

    try:
        return response.json()
    except ValueError:
        return {}

def exchange_for_long_lived_token(short_lived_token, client_id, client_secret):
    """
    Exchange short-lived token for long-lived token (60 days validity).
    Uses Instagram API ig_exchange_token endpoint.
    Note: Must use api.instagram.com, NOT graph.instagram.com
    """
    try:
        # IMPORTANT: Use api.instagram.com for token exchange, not graph.instagram.com
        url = f"https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret={client_secret}&access_token={short_lived_token}"
        # Never log the URL: it carries the token and the app secret.
        logger.debug("Exchanging short-lived token for a long-lived token")
        
        response = requests.get(url)
        response_data = response.json()
        
        logger.debug(f"Token exchange response status: {response.status_code}")
        logger.debug(f"Token exchange response: {response_data}")
        
        if response.status_code != 200 or response_data.get("error"):
            logger.error(f"Token exchange failed: {response_data}")
            return {"error": response_data.get("error", {}).get("message", "Unknown error")}
        
        logger.info("Successfully exchanged short-lived token for long-lived token")
        return response_data
    except Exception as e:
        logger.exception(f"Error exchanging token: {e}")
        return {"error": str(e)}

def refresh_long_lived_token(force=False, min_days=30):
    """Roll the stored long-lived token forward.

    Instagram long-lived tokens live 60 days and can be refreshed any time
    after they are 24 hours old. We refresh from day 30, which leaves a month
    of daily attempts before expiry instead of a single last-minute window.
    """
    token_doc = creds.find_one()
    if not token_doc:
        return {"error": "No token stored in database"}

    # Facebook system-user / Page tokens do not expire and cannot be refreshed
    # through ig_refresh_token; refreshing them is a no-op, not a failure.
    if os.environ.get("INSTA_MESSAGING_TOKEN") or os.environ.get("INSTA_ACCESS_TOKEN", "").startswith("EAA"):
        return {"ok": True, "refreshed": False, "permanent": True}

    expires_at = token_doc.get("expires_at")
    if not force and isinstance(expires_at, datetime):
        days_left = (expires_at - datetime.now()).total_seconds() / 86400
        if days_left > min_days:
            return {"ok": True, "refreshed": False, "days_remaining": round(days_left, 1)}

    try:
        response = requests.post(
            "https://graph.instagram.com/v22.0/refresh_access_token",
            params={
                "grant_type": "ig_refresh_token",
                "access_token": token_doc.get("access_token"),
            },
            timeout=30,
        )
        data = response.json()
    except Exception as exc:
        logger.exception(f"Error refreshing token: {exc}")
        return {"error": str(exc)}

    if response.status_code != 200 or data.get("error"):
        message = (data.get("error") or {}).get("message", "Unknown error")
        logger.error(f"Token refresh failed: {message}")
        return {"error": message}

    new_expires_in = data.get("expires_in", 60 * 24 * 3600)
    creds.update_one(
        {"_id": token_doc["_id"]},
        {"$set": {
            "access_token": data.get("access_token"),
            "expires_in": new_expires_in,
            "created_at": datetime.now(),
            "expires_at": datetime.now() + timedelta(seconds=new_expires_in),
            "last_refreshed_at": datetime.now(),
        }},
    )
    logger.info(f"Long-lived token refreshed; valid for {new_expires_in / 86400:.1f} days")
    return {"ok": True, "refreshed": True,
            "days_remaining": round(new_expires_in / 86400, 1)}

_token_cache = {"token": None, "at": 0.0}
_TOKEN_TTL = 60


def get_access_token():
    """
    Retrieves the Instagram access token from the database or environment variable.
    Checks expiration and logs warning if token is expiring soon.
    If not found, returns env var as fallback.

    Cached for a minute: this runs on every Graph call, including the typing
    indicator, and a Mongo round trip there is visible as a laggy bubble.
    """
    now = time.time()
    if _token_cache["token"] and now - _token_cache["at"] < _TOKEN_TTL:
        return _token_cache["token"]

    token = None
    try:
        cred = list(creds.find().limit(1))
        if cred:
            token_doc = cred[0]
            token = token_doc.get("access_token")
            expires_at = token_doc.get("expires_at")

            # Warn if token expiring soon (within 24 hours)
            if expires_at:
                time_until_expiry = (expires_at - datetime.now()).total_seconds()
                if time_until_expiry < 86400:  # Less than 24 hours
                    logger.warning(f"Access token expiring soon: {time_until_expiry / 3600:.1f} hours")
    except Exception as e:
        logger.error(f"Error retrieving access token from database: {e}")

    if not token:
        token = os.getenv("INSTA_ACCESS_TOKEN")

    if token:
        _token_cache["token"] = token
        _token_cache["at"] = now
    return token

def run_gemini(url, is_reel):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(gemini(url, is_reel))
    except Exception as e:
        # Handle Gemini API quota/resource exhaustion
        if hasattr(e, 'response') and getattr(e.response, 'status_code', None) == 429:
            logger.error(f"Gemini API quota exceeded: {e}")
            return "Gemini API quota exceeded"
        logger.error(f"Error in run_gemini: {e}")
        return "Error running Gemini"
    
def find_point_by_mid(collection_name, target_mid):
    """Find a point by payload mid.

    Uses the payload index when the collection has one; falls back to a
    paginated scroll for collections created before the index existed.
    """
    if not target_mid:
        return None

    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        points, _ = qdrant_client.scroll(
            collection_name=collection_name,
            scroll_filter=Filter(
                must=[FieldCondition(key="mid", match=MatchValue(value=target_mid))]
            ),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if points:
            logger.info(f"Found point for MID {target_mid[:40]} via payload index")
            return points[0]
        return None
    except Exception as exc:
        logger.debug(f"Indexed mid lookup unavailable ({exc}); falling back to scroll")

    try:
        logger.info(f"Searching for MID {target_mid[:50]}... in collection {collection_name}")
        offset = None
        while True:
            points, next_offset = qdrant_client.scroll(
                collection_name=collection_name,
                offset=offset,
                limit=100,  # Fetch 100 at a time
                with_payload=True,
                with_vectors=False
            )

            for point in points:
                if (point.payload or {}).get("mid") == target_mid:
                    logger.info(f"Found matching point for MID: {target_mid[:50]}...")
                    return point

            if next_offset is None or len(points) == 0:
                break

            offset = next_offset

        logger.warning(f"No point found with MID: {target_mid[:50]}...")
        return None

    except Exception as e:
        logger.exception(f"Error finding point by MID: {e}")
        return None

def get_recent_conversations(sender_id, limit=25):
    """Returns the last `limit` non-empty messages as 'User: ...' / 'Bot: ...' lines."""
    try:
        response = requests.get(
            f"{GRAPH}/me/conversations",
            params={"user_id": sender_id, **_auth_params()},
            timeout=30,
        )
        conversations = response.json().get("data") or []
        if not conversations:
            return ""
        conversation_id = conversations[0].get("id")

        response = requests.get(
            f"{GRAPH}/{conversation_id}/messages",
            params={
                "fields": "id,message,from,created_time",
                **_auth_params(),
            },
            timeout=30,
        )
        messages = (response.json().get("data") or [])[::-1]  # oldest first

        lines = []
        for message in messages:
            body = (message.get("message") or "").strip()
            if not body:
                continue
            sender = "User" if (message.get("from") or {}).get("id") == sender_id else "Bot"
            lines.append(f"{sender}: {body}")

        return "\n".join(lines[-limit:])
    except Exception as exc:
        logger.exception(f"Error in get_recent_conversations: {exc}")
        return ""