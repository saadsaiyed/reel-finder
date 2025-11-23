VERSION="1.2.4"
import requests, os, secrets, uuid, time, json, asyncio
import logging
from flask import Flask, request
from flask_cors import CORS
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams
from qdrant_client.http.models import Filter, FieldCondition, MatchValue
from pymongo.mongo_client import MongoClient
from dotenv import load_dotenv
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from functions import gemini
from flask import render_template

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
pymongo_logger = logging.getLogger("pymongo")
pymongo_logger.setLevel(logging.WARNING)

logger.info(f"Launching version {VERSION}")

load_dotenv(override=True)

# DB Connection
db_connection_string = os.getenv("DB_CONNECTION_STRING")
if db_connection_string is None or db_connection_string == "":
    logger.error("Please set the 'DB_CONNECTION_STRING' environment variable.")
    exit(1)

client = MongoClient(str(db_connection_string))
users = client["master"]["users"]
if "users" not in client["master"].list_collection_names():
    client["master"].create_collection(name="users", capped=False, autoIndexId=True)
    logger.info(f"Created collection users.")
processed = client["master"]["processed_mids"]
if "processed_mids" not in client["master"].list_collection_names():
    client["master"].create_collection(name="processed_mids", capped=False)
    logger.info(f"Created collection processed_mids.")
creds = client["master"]["creds"]
if "creds" not in client["master"].list_collection_names():
    client["master"].create_collection(name="creds", capped=False, autoIndexId=True)
    logger.info(f"Created collection creds.")

# Fask config
app = Flask(__name__)
CORS(app=app)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(16))  # Use env variable if available
app.config['DEBUG'] = os.environ.get("FLASK_DEBUG", "False").lower() == "true"

qdrant_client = QdrantClient(url=os.environ.get("QDRANT_URL"), api_key=os.environ.get("QDRANT_API_KEY"))
EMBEDDING_MODEL = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
logger.info(f"Finished Loading Embedding Model")
executor = ThreadPoolExecutor(max_workers=5)
logger.info(f"Finished Executor")

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    """Handle Instagram webhook verification and message processing."""
    try:
        if request.method == 'GET':
            verify_token = str(request.args.get('hub.verify_token'))
            challenge = request.args.get('hub.challenge')
            logger.info(f"GET request received with verify_token: {verify_token} and challenge: {challenge}")
            if verify_token == str(os.getenv('WEBHOOK_VERIFY_TOKEN')):
                return challenge
            return 'Invalid verify_token', 403

        elif request.method == 'POST':
            body = request.get_json()
            logger.info(f"POST request received with body: {body}")
            
            # Validate webhook payload
            if not body.get('object') == 'instagram':
                return 'Invalid object type', 400
                
            try:
                messaging = body['entry'][0]['messaging'][0]
                sender_id = messaging['sender']['id']
                
                # Skip messages from ourselves
                if sender_id == os.environ.get('IG_ID'):
                    return 'EVENT_RECEIVED', 200
                    
                mid = messaging['message']['mid']
                created_time = body['entry'][0].get('time')
                
                # Check for duplicate/already processed message
                if processed.find_one({"mid": mid}):
                    logger.info(f"Skipping already processed message {mid}")
                    return 'EVENT_RECEIVED', 200
                
                message = messaging.get('message', {})
                
                # Handle text messages
                if text := message.get('text'):
                    text = text.lower()
                    if text.startswith("search"):
                        search_query = text.split("search", 1)[1].strip()
                        executor.submit(handle_search, sender_id, search_query, mid)
                        return 'EVENT_RECEIVED', 200
                        
                    # Handle description for previous reel
                    user = users.find_one({"sender_id": sender_id})
                    if not user:
                        # THIS COULD BE THE ISSUE FOR SERVER KEEP GETTING WEBHOOK REQUEST FROM INSTA
                        time.sleep(5)  # Brief retry
                        user = users.find_one({"sender_id": sender_id})
                    
                    if not user:
                        send_error_message(sender_id, "If you want to search for a similar reel, please use the command `search <your query>`")
                        return 'EVENT_RECEIVED', 200
                        
                    current_time = int(datetime.now().timestamp() * 1000)
                    if current_time - user.get("created_time", 0) > 1000 * 60 * 60:
                        send_error_message(sender_id, "Too late to process your last reel. Please try to send the reel again with your message within 1hr.")
                        send_error_message(sender_id, "If you want to search for a similar reel, please use the command `search <your query>`")
                        users.delete_one({"sender_id": sender_id})
                        return 'EVENT_RECEIVED', 200
                        
                    executor.submit(handle_reel_description, sender_id, user, text, mid)
                    return 'EVENT_RECEIVED', 200
                
                # Handle attachments (reels)
                if attachments := message.get('attachments'):
                    attachment = attachments[0]
                    if attachment.get('type') == 'ig_reel':
                        url = attachment['payload'].get('url', '')
                        context = {
                            "sender_id": sender_id,
                            "mid": mid,
                            "reel_id": attachment['payload'].get('reel_video_id'),
                            "created_time": created_time,
                            "url": url
                        }
                        executor.submit(handle_attachment, context)
                        return 'EVENT_RECEIVED', 200
                
                # Unhandled message type
                logger.warning(f"Unhandled message type for mid {mid}")
                return 'EVENT_RECEIVED', 200
                
            except (KeyError, IndexError) as e:
                logger.error(f"Malformed webhook payload: {e}")
                return 'Malformed payload', 400
                
        return 'Method not allowed', 405
        
    except Exception as exc:
        logger.exception("Webhook error: %s", exc)
        try:
            if 'sender_id' in locals():
                send_error_message(sender_id, "Internal error processing your message")
        except:
            pass
        return 'Internal error', 500

def handle_search(sender_id, search_query, mid):
    """Background worker: Process search request and send similar reel."""
    try:
        response = send_similar_reel(sender_id, search_query)
        # response can be a dict with error info or a requests.Response on success
        if isinstance(response, dict) and response.get('error'):
            logger.error(f"Search error for mid {mid}: {response.get('error')}")
            send_error_message(sender_id, "Error finding similar reel")
            return

        if isinstance(response, requests.Response):
            # Try to parse JSON error if present
            try:
                json_resp = response.json()
                if isinstance(json_resp, dict) and json_resp.get('error'):
                    logger.error(f"Search error for mid {mid}: {json_resp.get('error')}")
                    send_error_message(sender_id, "Error finding similar reel")
                    return
            except ValueError:
                # Non-JSON response: treat non-2xx as error
                if not (200 <= response.status_code < 300):
                    logger.error(f"Search failed for mid {mid}: HTTP {response.status_code}")
                    send_error_message(sender_id, "Error finding similar reel")
                    return
        elif not isinstance(response, dict):
            # Unknown response type
            logger.error(f"Unexpected response type for mid {mid}: {type(response)}")
            send_error_message(sender_id, "Error finding similar reel")
            return

        logger.info(f"Search response for mid {mid}: {response}")
        processed.insert_one({"mid": mid, "type": "search", "timestamp": int(datetime.now().timestamp() * 1000)})
        send_reaction(sender_id, mid, "love")
    except Exception as exc:
        logger.exception(f"Error in handle_search for mid {mid}: {exc}")
        send_error_message(sender_id, "Error processing search")

def handle_reel_description(sender_id, user, text, mid):
    """Background worker: Process text description for previously sent reel."""
    try:
        user["message"] = text
        id = user.pop("_id", None)
        response = store_embeddings(sender_id, [user])
        if response.get("error"):
            logger.error(f"Error storing embeddings for mid {mid}: {response.get('error')}")
            send_error_message(sender_id, "Error storing your description")
            return
            
        users.delete_one({"_id": id})
        processed.insert_one({"mid": mid, "type": "description", "timestamp": int(datetime.now().timestamp() * 1000)})
        send_reaction(sender_id, mid, "love")
    except Exception as exc:
        logger.exception(f"Error in handle_reel_description for mid {mid}: {exc}")
        send_error_message(sender_id, "Error processing your description")

def handle_attachment(context):
    """Background worker: run Gemini, store embeddings, send messages/reactions and mark mid processed."""
    sender_id = context.get("sender_id")
    mid = context.get("mid")
    url = context.get("url")
    reel_id = context.get("reel_id")
    created_time = context.get("created_time")
    try:
        # idempotency: skip if this mid already processed
        if processed.find_one({"mid": mid}):
            logger.info(f"Skipping already processed mid: {mid}")
            return

        title = run_gemini(url)
        if title == "Gemini API quota exceeded":
            logger.error("Gemini API quota exceeded for URL: %s", url)
            send_error_message(sender_id, "Gemini API quota exceeded, try again later")
            # mark as failed_quota to avoid immediate reprocessing
            processed.insert_one({"mid": mid, "status": "failed_quota", "timestamp": int(datetime.now().timestamp() * 1000)})
            return
        if title == "Error running Gemini":
            logger.error("Error running Gemini for URL: %s", url)
            send_error_message(sender_id, "Error processing your reel, try again later")
            return

        payload = {
            "sender_id": sender_id,
            "message": title,
            "mid": mid,
            "reel_id": reel_id,
            "link": url,
            "created_time": created_time
        }

        response = store_embeddings(sender_id, [payload])
        if response.get("error"):
            logger.error("Error storing embeddings for mid %s: %s", mid, response.get("error"))
            send_error_message(sender_id, "Error storing data, try again later")
            return

        users.delete_many({"sender_id": sender_id})
        users.insert_one(payload)

        # mark processed
        processed.insert_one({"mid": mid, "timestamp": int(datetime.now().timestamp() * 1000)})

        # notify user and react
        send_error_message(sender_id, title)
        send_reaction(sender_id, mid, "love")
    except Exception as exc:
        logger.exception("Exception in handle_attachment: %s", exc)
        send_error_message(sender_id, "Internal error processing your reel")

def process_gemini_result(future, context):
    sender_id = context["sender_id"]
    mid = context["mid"]
    reel_id = context["reel_id"]
    created_time = context["created_time"]
    url = context["url"]
    try:
        title = future.result()  # Get the result of the completed task
        if title == "Gemini API quota exceeded":
            logger.error("Gemini API quota exceeded. Skipping this task.")
            send_error_message(sender_id, "Gemini API quota exceeded")
            return
        if title == "Error running Gemini":
            logger.error("Error running Gemini for this task.")
            send_error_message(sender_id, "Error running Gemini")
            return
        logger.info(f"Gemini response: {title}")
        payload = {
            "sender_id": sender_id,
            "message": title,
            "mid": mid,
            "reel_id": reel_id,
            "link": url,
            "created_time": created_time
        }
        response = store_embeddings(sender_id, [payload])
        if response.get("error"):
            logger.error(f"Error storing embeddings: {response.get('error')}")
            send_error_message(sender_id, "Error processing attachment")
            return
        users.delete_many({"sender_id": sender_id})
        users.insert_one(payload)
        send_error_message(sender_id, title)
        send_reaction(sender_id, mid, "love")
    except Exception as e:
        logger.exception("Error in process_gemini_result: %s", e)
        send_error_message(sender_id, str(e))

def store_embeddings(collection_name, messages):
    try:
        try:
            qdrant_client.get_collection(collection_name)
        except:
            qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=384, distance=Distance.COSINE)
            )
    
        embeddings_list = []
        for message in messages:
            embedding = EMBEDDING_MODEL.embed_query(message.get("message"))
            embeddings_list.append({
                "id": int(uuid.uuid4().int % (10**12)),  # Generate unique 12-digit ID
                "vector": embedding,
                "payload": message
            })
        logger.info(f"Embeddings list: {embeddings_list}")

        qdrant_client.upsert(
            collection_name=collection_name,
            points=embeddings_list
        )
        return {"message": "Embeddings stored successfully"}
    except requests.RequestException as exc:
        logger.error(f"Error in store_embeddings: {exc}")
        send_error_message(collection_name, str(exc))
        return {"error": f"Error storing embeddings: {exc}"}

def get_similar_messages(collection_name, text):
    try:
        embedding = EMBEDDING_MODEL.embed_query(text)
        response = qdrant_client.query_points(
            collection_name=collection_name,
            query=embedding,
            # query_filter = Filter(
            #     must=[
            #         FieldCondition(
            #             key='sender_id',
            #             match=MatchValue(value=collection_name)  # Wrap the value in MatchValue
            #         )
            #     ]
            # ),
            limit=1
        )
        return response.points
    except requests.RequestException as exc:
        logger.error(f"Error in get_similar_messages: {exc}")
        send_error_message(collection_name, str(exc))
        return {"error": f"Error retrieving similar messages: {exc}"}

def send_similar_reel(sender_id, text):
    try:
        logger.info(f"Started send_similar_reel")
        response = get_similar_messages(collection_name=sender_id, text=text)
        if not response or not response[0].payload:
            logger.info("No results found.")
            send_error_message(sender_id, "No similar reels found. Try a different search query.")
            return {"error": "No similar messages found."}

        # ToDo: send thumbs up reaction to the message
        
        link = response[0].payload.get("link", "No link available")
        url = f"https://graph.instagram.com/v22.0/me/messages?access_token={get_access_token()}"
        payload = {
            "recipient": {"id": sender_id},
            "message": {
                "attachment": {
                    "type": "video",
                    "payload": {"url": link}
                }
            }
        }
        
        logger.info(f"Request ready to send to URL `{url}` with payload: {payload}")
        response = requests.post(url, json=payload)
        
        if response.status_code != 200:
            if response.json().get('error').get('error_subcode'):
                # ToDO: Implement logic to send reel in chunks if error_subcode indicates that
                logging.error("Implement 'Sending reel in chunks logic'")
                
            raise requests.RequestException(f"Error sending similar reel response: {response.json().get('error', {}).get('message', 'Unknown error')}")            
        return response
    except requests.RequestException as exc:
        send_error_message(sender_id, str(exc))
        logger.error(f"Error in send_similar_reel: {exc}")
        return {"error": f"Error sending similar reel response: {exc}"}
        
def send_error_message(sender_id, error_message):
    url = f"https://graph.instagram.com/v22.0/me/messages?access_token={get_access_token()}"
    payload = {
        "recipient": {"id": sender_id},
        "message": {
            "text": error_message
        }
    }
    logger.info(f"Request ready to send to URL `{url}` with payload: {payload}")
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        logger.error(f"Error sending error message: {response.json()['error']['message']}")

def send_reaction(sender_id, message_id, reaction_type=os.getenv("DEFAULT_REACTION_TYPE", "love")):
    url = f"https://graph.instagram.com/v22.0/me/messages/?access_token={get_access_token()}"
    payload = {
        "recipient": {"id": sender_id},
        "sender_action": "react", # Or set to unreact to remove the reaction
        "payload": {
            "message_id": message_id,
            "reaction": reaction_type # Omit if removing a reaction
        }
    }
    response = requests.post(url, json=payload)
    if response.status_code != 200:
        logger.error(f"Error sending reaction: {response.json()['error']['message']}")

    return response.json()

def exchange_for_long_lived_token(short_lived_token, client_id, client_secret):
    """
    Exchange short-lived token for long-lived token (60 days validity).
    Uses Instagram Graph API ig_exchange_token endpoint.
    """
    try:
        url = "https://graph.instagram.com/v22.0/oauth/access_token"
        payload = {
            "grant_type": "ig_exchange_token",
            "client_secret": client_secret,
            "access_token": short_lived_token
        }
        response = requests.post(url, params=payload)
        response_data = response.json()
        
        if response.status_code != 200 or response_data.get("error"):
            logger.error(f"Token exchange failed: {response_data}")
            return {"error": response_data.get("error", {}).get("message", "Unknown error")}
        
        logger.info("Successfully exchanged short-lived token for long-lived token")
        return response_data
    except Exception as e:
        logger.exception(f"Error exchanging token: {e}")
        return {"error": str(e)}

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

def run_gemini(url):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(gemini(url))
    except Exception as e:
        # Handle Gemini API quota/resource exhaustion
        if hasattr(e, 'response') and getattr(e.response, 'status_code', None) == 429:
            logger.error(f"Gemini API quota exceeded: {e}")
            return "Gemini API quota exceeded"
        logger.error(f"Error in run_gemini: {e}")
        return "Error running Gemini"

# new home rout that shows where I am
@app.route('/', methods=["GET", "POST"])
def home():
    if request.method == "GET":
        logging.info("GET request received for home page")
        code = request.args.get("code")
    elif request.method == "POST":
        logging.info(f"POST request received with data: {request.json}")
        code = request.json.get("code")
        code = code.split("?code=")[-1]
    
    if not code or code=="":
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
        "code": code
    }

    response = requests.post("https://api.instagram.com/oauth/access_token", data=payload)
    json_response = response.json()
    logging.info(f"Instagram OAuth response: {json_response}")
    
    # Store short-lived token temporarily
    short_lived_token = json_response.get("access_token")
    user_id = json_response.get("user_id")
    
    # Exchange short-lived token for long-lived token (60 days)
    long_lived_response = exchange_for_long_lived_token(short_lived_token, client_id, client_secret)
    if long_lived_response.get("error"):
        logging.error(f"Failed to exchange for long-lived token: {long_lived_response.get('error')}")
        return {"error": "Failed to obtain long-lived token"}, 400
    
    long_lived_token = long_lived_response.get("access_token")
    expires_in = long_lived_response.get("expires_in", 60 * 24 * 60 * 60)  # Default 60 days in seconds
    
    # Store long-lived token in database
    creds.delete_many({})
    from datetime import timedelta
    creds.insert_one({
        "access_token": long_lived_token,
        "user_id": user_id,
        "expires_in": expires_in,
        "created_at": datetime.now(),
        "expires_at": datetime.now() + timedelta(seconds=expires_in),
        "token_type": "long_lived"
    })
    
    if request.method == "POST":
        return {"message": "Long-lived token obtained and stored successfully"}, 200
    
    return render_template("index.html", login_link=None)

@app.route('/callback', methods=["GET"])
def callback():
    """
    OAuth callback route - handles redirect from Instagram login.
    Automatically exchanges authorization code for long-lived token.
    Displays all token details including expiration info from Meta.
    """
    try:
        # Get authorization code from URL parameters
        code = request.args.get("code")
        
        if not code:
            logger.error("No authorization code in callback")
            return {
                "error": "No authorization code received from Instagram",
                "status": "failed"
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
                "status": "failed"
            }, 500
        
        # Step 1: Exchange code for short-lived access token
        logger.info("Step 1: Exchanging authorization code for short-lived token...")
        short_lived_payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code
        }
        
        short_lived_response = requests.post(
            "https://api.instagram.com/oauth/access_token",
            data=short_lived_payload
        )
        short_lived_data = short_lived_response.json()
        
        if short_lived_response.status_code != 200 or short_lived_data.get("error"):
            logger.error(f"Failed to get short-lived token: {short_lived_data}")
            return {
                "error": "Failed to obtain short-lived token from Instagram",
                "details": short_lived_data.get("error", {}),
                "status": "failed"
            }, 400
        
        short_lived_token = short_lived_data.get("access_token")
        user_id = short_lived_data.get("user_id")
        
        logger.info(f"✓ Short-lived token obtained for user: {user_id}")
        logger.debug(f"Short-lived response: {short_lived_data}")
        
        # Step 2: Exchange short-lived token for long-lived token (60 days)
        logger.info("Step 2: Exchanging short-lived token for long-lived token...")
        long_lived_response = exchange_for_long_lived_token(
            short_lived_token,
            client_id,
            client_secret
        )
        
        if long_lived_response.get("error"):
            logger.error(f"Failed to exchange for long-lived token: {long_lived_response}")
            return {
                "error": "Failed to exchange for long-lived token",
                "details": long_lived_response.get("error", {}),
                "status": "failed"
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
        
        logger.info(f"Token expires in: {expires_in_days:.1f} days ({expires_in_seconds} seconds)")
        
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
            "short_lived_response": short_lived_data  # Also store short-lived for reference
        }
        
        inserted = creds.insert_one(token_record)
        logger.info(f"✓ Token stored in database with ID: {inserted.inserted_id}")
        
        # Step 5: Return comprehensive response with all details
        response_data = {
            "status": "success",
            "message": "Long-lived token obtained and stored successfully",
            "user_id": user_id,
            "token_type": "long_lived",
            "access_token": long_lived_token[:20] + "..." + long_lived_token[-20:],  # Masked for display
            "expires_in": {
                "seconds": expires_in_seconds,
                "days": round(expires_in_days, 1),
                "hours": round(expires_in_seconds / 3600, 1)
            },
            "expiration": {
                "expires_at": expires_at.isoformat(),
                "expires_at_readable": expires_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            },
            "meta_response": {
                "access_token_length": len(long_lived_token),
                "expires_in": expires_in,
                "user_id": long_lived_response.get("user_id"),
                "token_type": long_lived_response.get("token_type")
            },
            "database": {
                "stored_at": datetime.now().isoformat(),
                "record_id": str(inserted.inserted_id)
            }
        }
        
        logger.info("=" * 80)
        logger.info("TOKEN EXCHANGE COMPLETED SUCCESSFULLY")
        logger.info("=" * 80)
        logger.info(f"User ID: {user_id}")
        logger.info(f"Token Type: Long-lived (60 days)")
        logger.info(f"Expires In: {expires_in_days:.1f} days ({expires_in_seconds} seconds)")
        logger.info(f"Expires At: {expires_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info(f"Token Length: {len(long_lived_token)} characters")
        logger.info("=" * 80)
        
        return response_data, 200
    
    except Exception as e:
        logger.exception(f"Unexpected error in callback: {e}")
        return {
            "error": "Internal server error during token exchange",
            "details": str(e),
            "status": "failed"
        }, 500

@app.route('/refresh-token', methods=["POST"])
def refresh_token():
    """
    Refresh the long-lived token before it expires.
    Should be called every 50 days or when token is close to expiration.
    """
    try:
        cred = list(creds.find())
        if not cred:
            return {"error": "No token stored in database"}, 400
        
        token_doc = cred[0]
        access_token = token_doc.get("access_token")
        user_id = token_doc.get("user_id")
        expires_at = token_doc.get("expires_at")
        
        # Check how much time is left
        if expires_at:
            time_until_expiry = (expires_at - datetime.now()).total_seconds()
            if time_until_expiry > 24 * 60 * 60:  # More than 24 hours left
                return {"message": f"Token still valid for {time_until_expiry / (24 * 3600):.1f} days"}, 200
        
        # Refresh token using refresh endpoint
        url = "https://graph.instagram.com/v22.0/refresh_access_token"
        payload = {
            "grant_type": "ig_refresh_token",
            "access_token": access_token
        }
        response = requests.post(url, params=payload)
        response_data = response.json()
        
        if response.status_code != 200 or response_data.get("error"):
            logger.error(f"Token refresh failed: {response_data}")
            return {"error": response_data.get("error", {}).get("message", "Unknown error")}, 400
        
        new_token = response_data.get("access_token")
        new_expires_in = response_data.get("expires_in", 60 * 24 * 60 * 60)
        
        # Update token in database
        from datetime import timedelta
        creds.update_one(
            {"_id": token_doc.get("_id")},
            {"$set": {
                "access_token": new_token,
                "expires_in": new_expires_in,
                "created_at": datetime.now(),
                "expires_at": datetime.now() + timedelta(seconds=new_expires_in),
                "last_refreshed_at": datetime.now()
            }}
        )
        
        logger.info("Successfully refreshed long-lived token")
        return {"message": "Token refreshed successfully", "expires_in_days": new_expires_in / (24 * 3600)}, 200
    
    except Exception as e:
        logger.exception(f"Error refreshing token: {e}")
        return {"error": str(e)}, 500

@app.route('/token-status', methods=["GET"])
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
            "last_refreshed_at": token_doc.get("last_refreshed_at").isoformat() if token_doc.get("last_refreshed_at") else None
        }, 200
    
    except Exception as e:
        logger.exception(f"Error getting token status: {e}")
        return {"error": str(e)}, 500

@app.route('/reaction', methods=["GET"])
def reaction():
    return send_reaction(sender_id=2369537610112817, message_id="aWdfZAG1faXRlbToxOklHTWVzc2FnZAUlEOjE3ODQxNDQ3MTkzMTQzMTMyOjM0MDI4MjM2Njg0MTcxMDMwMTI0NDI1OTgxOTQwOTIxNDA0MTg3ODozMjI1NTU4NDg4NTg2ODg0MTkxOTY0Nzg5NjQ5NDQwNzY4MAZDZD", reaction_type="LIKE")

@app.route("/conversations/<conversation_id>")
def messages(conversation_id): 
    response = requests.get(f'https://graph.instagram.com/v22.0/{conversation_id}/messages?fields=attachments,id,message,from,to,created_time,reactions,shares&access_token={get_access_token()}')
    response = response.json()
    messages = response.get("data")
    file_name = f'{messages[0].get("from").get("username")} {messages[0].get("to").get("data")[0].get("username")}'
    with open(f"{file_name}.json", 'w') as f:
        f.write(json.dumps(messages))
    while "paging" in response: 
        if response.get("paging").get("next"):
            next_url = response.get("paging").get("next")
            response = requests.get(next_url).json()
            messages.extend(response.get("data"))
        else: 
            with open(f"{file_name}.json", 'w') as f:
                f.write(json.dumps(messages))
            break

        logger.info(f"File name: {file_name}")

    print(len(messages))
    embedding_msg=[]
    for i, message in enumerate(messages):
        if message.get("from").get("username") == "reel_sync_ai":
            continue
        if message.get("shares"):
            link = message["shares"]["data"][0]["link"]
            if i>0 and not messages[i-1].get("from").get("username") == "reel_sync_ai" and not messages[i-1].get("shares"):
                prev_message = messages[i-1]
                embedding_msg.append({
                    "id" : prev_message.get("id"),
                    "sender_id": prev_message.get("from").get("id"),
                    "link" : link,
                    "message" : prev_message.get("message"),
                    "timestamp" : int(datetime.fromisoformat(prev_message.get("created_time")).timestamp() * 1000)
                })

    with open(f"{file_name} embeddings.json", 'w') as f:
        f.write(json.dumps(embedding_msg))
    
    logger.info("Starting Qdrant")
    collection_name = prev_message.get("from").get("id")
    qdrant_client = QdrantClient(url=os.environ.get("QDRANT_URL"), api_key=os.environ.get("QDRANT_API_KEY"))
    try:
        qdrant_client.get_collection(collection_name)
    except:
        qdrant_client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=384, distance=Distance.COSINE)
        )
    logger.info(f"Found Collection {collection_name}")
    embeddings_list = []
    for message in embedding_msg:
        embedding = EMBEDDING_MODEL.embed_query(message.get("message"))
        embeddings_list.append({
            "id": int(uuid.uuid4().int % (10**12)),  # Generate unique 12-digit ID
            "vector": embedding,
            "payload": message
        })

    logger.info(f"Setting embeddings")
    qdrant_client.upsert(
        collection_name=collection_name,
        points=embeddings_list
    )
    print("done")

    return "DONE", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)), debug=True)

