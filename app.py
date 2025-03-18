import requests, os, secrets, uuid, time, json
import logging
from flask import Flask, request
from flask_cors import CORS
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams
from qdrant_client.http.models import Filter, FieldCondition
from pymongo.mongo_client import MongoClient
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

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


# Fask config
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
                            # Have a logic of Using AI to describe the reel
                            if not attachment.get('payload').get('title', '') == '': store_embeddings(sender_id, [payload])
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
        query_filter = Filter(
            must=[
                FieldCondition(
                    key='sender_id',
                    match="1303011334296915"
                )
            ]
        ),
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

@app.route("/conversations/<conversation_id>")
def messages(conversation_id):
    response = requests.get(f'https://graph.instagram.com/v22.0/{conversation_id}/messages?fields=attachments,id,message,from,to,created_time,reactions,shares&access_token={os.environ.get("INSTA_ACCESS_TOKEN")}')
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

        logger.debug(f"File name: {file_name}")

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
    
    logger.debug("Starting Qdrant")
    collection_name = prev_message.get("from").get("id")
    qdrant_client = QdrantClient(url=os.environ.get("QDRANT_URL"), api_key=os.environ.get("QDRANT_API_KEY"))
    try:
        qdrant_client.get_collection(collection_name)
    except:
        qdrant_client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=384, distance=Distance.COSINE)
        )
    logger.debug(f"Found Collection {collection_name}")
    embeddings_list = []
    for message in embedding_msg:
        embedding = EMBEDDING_MODEL.embed_query(message.get("message"))
        embeddings_list.append({
            "id": int(uuid.uuid4().int % (10**12)),  # Generate unique 12-digit ID
            "vector": embedding,
            "payload": message
        })

    logger.debug(f"Setting embeddings")
    qdrant_client.upsert(
        collection_name=collection_name,
        points=embeddings_list
    )
    print("done")


    return "DONE", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

