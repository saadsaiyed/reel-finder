import os, logging, time, asyncio, hashlib, hmac, tempfile, requests
from google import genai
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


def describe_media_file(filename):
    """Upload one local media file to Gemini and return its description.

    Raises on failure so callers can distinguish quota from a bad file. The
    temp file is always removed.
    """
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
        response = client.models.generate_content(
            model=GEMINI_VISION_MODEL, contents=[media, prompt]
        )
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
        kind = "rate_limited" if _is_block_error(str(exc)) else "failed"
        logger.warning(f"yt-dlp failed ({kind}) for {url}: {str(exc)[:200]}")
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


def _send_video(sender_id, link):
    response = requests.post(
        f"{GRAPH}/me/messages",
        params=_auth_params(),
        json={
            "recipient": {"id": sender_id},
            "message": {"attachment": {"type": "video", "payload": {"url": link}}},
        },
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


def send_similar_reel(sender_id, text):
    try:
        logger.info("Started send_similar_reel")
        results = get_similar_messages(collection_name=sender_id, text=text)
        if isinstance(results, dict) and results.get("error"):
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

        # Stored links are usually signed Instagram CDN URLs that expire. Try the
        # link, but never leave the user with a dead attachment: fall back to text.
        if link and _send_video(sender_id, link):
            return {"message": "Reel sent"}

        if description:
            send_error_message(
                sender_id,
                f"Closest match: {description}\n\n"
                f"(I couldn't resend the original reel — Instagram's media link has expired.)",
            )
            return {"message": "Description fallback sent"}

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


def send_error_message(sender_id, error_message):
    """Send error message(s) to user, splitting into multiple messages if needed."""
    # Split message if it exceeds 1000 characters
    message_chunks = split_message(error_message, max_length=1000)

    for i, chunk in enumerate(message_chunks):
        payload = {
            "recipient": {"id": sender_id},
            "message": {"text": chunk},
        }
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

def get_access_token():
    """
    Retrieves the Instagram access token from the database or environment variable.
    Checks expiration and logs warning if token is expiring soon.
    If not found, returns env var as fallback.
    """
    try:
        cred = list(creds.find())
        if cred:
            token_doc = cred[0]
            access_token = token_doc.get("access_token")
            expires_at = token_doc.get("expires_at")
            
            # Warn if token expiring soon (within 24 hours)
            if expires_at:
                time_until_expiry = (expires_at - datetime.now()).total_seconds()
                if time_until_expiry < 86400:  # Less than 24 hours
                    logger.warning(f"Access token expiring soon: {time_until_expiry / 3600:.1f} hours")
            
            return access_token
    except Exception as e:
        logger.error(f"Error retrieving access token from database: {e}")
    
    # Fallback to environment variable
    return os.getenv("INSTA_ACCESS_TOKEN")

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