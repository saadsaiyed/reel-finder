import os, logging, time, asyncio, requests
from google import genai
import uuid
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams
from pymongo import MongoClient
from datetime import datetime

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize database connections
db_connection_string = os.getenv("DB_CONNECTION_STRING")
if not db_connection_string:
    logger.error("DB_CONNECTION_STRING not set")
    
db_client = MongoClient(str(db_connection_string))
creds = db_client["master"]["creds"]
users = db_client["master"]["users"]
processed = db_client["master"]["processed_mids"]

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

async def gemini(url, is_reel=True):
    if url == "":
        return ""

    logger.debug(f"Downloading file from: {url}")

    # Download the file
    response = requests.get(url)
    if response.status_code != 200:
        logger.error(f"Failed to download file: {response.status_code}")
        return ""
    
    # Detect file type
    file_type, extension = detect_file_type(response)
    filename = f"temp_video.{extension}" if file_type == 'video' else f"temp_image.{extension}"
    
    logger.info(f"Detected {file_type} file ({extension}): {filename}")
    
    # Save the file
    try:
        with open(filename, 'wb') as f:
            f.write(response.content)
        logger.debug(f"File downloaded and saved: {filename} ({len(response.content)} bytes)")
    except Exception as e:
        logger.error(f"Failed to save file: {e}")
        return ""

    # Create a new event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

    logger.debug("Uploading file to Gemini...")
    try:
        video_file = client.files.upload(file=filename)
        logger.debug(f"Completed upload: {video_file.uri}")
    except Exception as e:
        logger.error(f"Failed to upload file: {e}")
        try:
            os.remove(filename)
        except:
            pass
        return ""

    # Wait until the file is processed
    while video_file.state.name == "PROCESSING":
        print('.', end='', flush=True)
        await asyncio.sleep(1)
        video_file = client.files.get(name=video_file.name)

    if video_file.state.name == "FAILED":
        logger.error(f"File processing failed: {video_file.state.name}")
        try:
            os.remove(filename)
        except:
            pass
        raise ValueError(video_file.state.name)

    logger.debug('File processed successfully')

    # Generate content from the file
    prompt = os.environ.get("GEMINI_PROMPT", "With simple texts only and no `here you go...` or `following is:...` types of statements, for each scene in this video, generate captions that describe the scene along with any spoken text placed in quotation marks without timestamp. Provide your explanation. Only respond with what is asked. \nExample: A guy tasting something spicy and can't control his emotions and tears up.")
    
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                video_file,
                prompt
            ]
        )
    except Exception as e:
        logger.error(f"Failed to generate content: {e}")
        try:
            os.remove(filename)
        except:
            pass
        return ""

    # Clean up temp file
    try:
        os.remove(filename)
        logger.debug(f"Temp file deleted: {filename}")
    except Exception as e:
        logger.warning(f"Failed to delete temp file: {e}")

    return response.text

def store_embeddings(collection_name, messages):
    try:
        try:
            qdrant_client.get_collection(collection_name)
        except:
            qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=384, distance=Distance.COSINE)
            )
            logger.info(f"Created new collection: {collection_name}")
    
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

def send_error_message(sender_id, error_message):
    """Send error message(s) to user, splitting into multiple messages if needed."""
    # Split message if it exceeds 1000 characters
    message_chunks = split_message(error_message, max_length=1000)
    
    for i, chunk in enumerate(message_chunks):
        url = f"https://graph.instagram.com/v22.0/me/messages?access_token={get_access_token()}"
        payload = {
            "recipient": {"id": sender_id},
            "message": {
                "text": chunk
            }
        }
        logger.info(f"Sending message chunk {i+1}/{len(message_chunks)} ({len(chunk)} chars)")
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            logger.error(f"Error sending message chunk {i+1}: {response.json()['error']['message']}")
            return False
        
        # Small delay between messages to avoid rate limiting
        if i < len(message_chunks) - 1:
            time.sleep(0.2)
    
    return True

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
    Uses Instagram API ig_exchange_token endpoint.
    Note: Must use api.instagram.com, NOT graph.instagram.com
    """
    try:
        # IMPORTANT: Use api.instagram.com for token exchange, not graph.instagram.com
        url = f"https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret={client_secret}&access_token={short_lived_token}"
        logger.debug(f"Token exchange request to: {url}")
        logger.debug(f"Token exchange payload keys: grant_type, client_secret, access_token")
        
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
        return loop.run_until_.complete(gemini(url, is_reel))
    except Exception as e:
        # Handle Gemini API quota/resource exhaustion
        if hasattr(e, 'response') and getattr(e.response, 'status_code', None) == 429:
            logger.error(f"Gemini API quota exceeded: {e}")
            return "Gemini API quota exceeded"
        logger.error(f"Error in run_gemini: {e}")
        return "Error running Gemini"