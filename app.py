import requests, os, secrets, uuid, time
import logging
from flask import Flask, request
from flask_cors import CORS
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams
from pymongo.mongo_client import MongoClient
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# Create a new client and connect to the server
db_connection_string = os.getenv("DB_CONNECTION_STRING")
if db_connection_string is None or db_connection_string == "":
    logger.error("Please set the 'DB_CONNECTION_STRING' environment variable.")
    exit(1)

client = MongoClient(str(db_connection_string))

db = client["master"]
user_collection_name = "users"

# Create collection if it doesn't exist
users = db[user_collection_name]
if user_collection_name not in db.list_collection_names():
    db.create_collection(name=user_collection_name, capped=False, autoIndexId=True)
    logger.info(f"Created collection '{user_collection_name}'.")

app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(16))  # Use env variable if available
app.config['DEBUG'] = os.environ.get("FLASK_DEBUG")
WEBHOOK_VERIFY_TOKEN = str(os.getenv('WEBHOOK_VERIFY_TOKEN'))

CORS(app=app)

EMBEDDING_MODEL = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
qdrant_client = QdrantClient(url=os.environ.get("QDRANT_URL"), api_key=os.environ.get("QDRANT_API_KEY"))

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        verify_token = str(request.args.get('hub.verify_token'))
        challenge = request.args.get('hub.challenge')
        logger.info(f"GET request received with verify_token: {verify_token} and challenge: {challenge}")
        if verify_token == WEBHOOK_VERIFY_TOKEN:
            return challenge
        else:
            return 'Invalid Request'
    elif request.method == 'POST':
        body = request.get_json()
        logger.info(f"POST request received with body: {body}")
        if body.get('object') == 'instagram' and not body.get('entry')[0].get('messaging')[0].get('sender').get('id') == os.environ.get('IG_ID'):
            for entry in body.get('entry'):
                for messaging in entry.get('messaging'):
                    created_time = entry.get('time')
                    sender_id = messaging.get('sender').get('id')
                    mid = messaging.get('message').get('mid')
                    # ToDo: Handle message reactions

                    if messaging.get('message') and messaging.get('message').get('text'):
                        text = messaging.get('message').get('text').lower()
                        logger.info(f"Text message received: {text}")
                        if text.startswith("search"):
                            search_query = text.split("search")[1].strip()
                            logger.info(f"Search initiated with query: {search_query}")
                            response = send_similar_reel(sender_id, search_query)
                            logger.info(f"Search response: {response}")
                            return 'EVENT_RECEIVED', 200
                        else:
                            user = users.find_one({"sender_id": sender_id})
                            if user is None:
                                time.sleep(5)
                                user = users.find_one({"sender_id": sender_id})
                                if user is None:
                                    logger.error("Cannot find reel for sender_id: {sender_id}")
                                    return 'CANT_FIND_REEL', 400
                            user["message"] = text
                            id = user.pop("_id", None)
                            logger.info(f"User found: {user}")
                            store_embeddings(sender_id, [user])
                            users.delete_one({"_id": id})
                            pass
                        return 'EVENT_RECEIVED'
                    elif messaging.get('message') and messaging.get('message').get('attachments'):
                        attachment = messaging.get('message').get('attachments')[0]
                        if attachment.get('type') == 'ig_reel':
                            payload = {
                                "sender_id": sender_id,
                                "message": attachment.get('payload').get('title', ''),
                                "mid": mid,
                                "reel_id": attachment.get('payload').get('reel_video_id'),
                                "link": attachment.get('payload').get('url'),
                                "created_time": created_time
                            }
                            logger.info(f"Reel attachment received with payload: {payload}")
                            store_embeddings(sender_id, [payload])
                            users.insert_one(payload)
                            return 'EVENT_RECEIVED'
                        return 'EVENT_RECEIVED'
        return 'EVENT_RECEIVED'
    return 'Invalid Request'

def store_embeddings(collection_name, messages):
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

def get_similar_messages(collection_name, text):
    embedding = EMBEDDING_MODEL.embed_query(text)
    response = qdrant_client.query_points(
        collection_name=collection_name,
        query=embedding,
        limit=1
    )
    logger.info(f"Similar messages response: {response}")
    return response.points

def send_similar_reel(sender_id, text):
    logger.info(f"Started send_similar_reel")
    response = get_similar_messages(collection_name=sender_id, text=text)
    if not response or not response[0].payload:
        logger.info("No results found.")
        return {"error": "No similar messages found."}
    # ToDo: send thumbs up reaction to the message
    
    link = response[0].payload.get("link", "No link available")
    url = f"https://graph.instagram.com/v22.0/me/messages?access_token={os.environ.get('INSTA_ACCESS_TOKEN')}"
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
        logger.error(f"Error sending similar reel response")
        return {"error": "Error sending similar reel response"}
    return "Successfully sent similar reel response"

# new home rout that shows where I am
@app.route('/')
def home():
    return f"I am running on {app.config['ENV']} environment."

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

