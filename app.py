import requests, os, secrets, uuid, time
from flask import Flask, request
from flask_cors import CORS
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams
from pymongo.mongo_client import MongoClient

from dotenv import load_dotenv
load_dotenv()

# Create a new client and connect to the server
if os.getenv("DB_CONNECTION_STRING") is None or os.getenv("DB_CONNECTION_STRING") == "":
    print("Please set the 'DB_CONNECTION_STRING' environment variable.")
    exit(1)
client = MongoClient(os.getenv("DB_CONNECTION_STRING"))

db = client["master"]
user_collection_name = "users"

# Create collection if it doesn't exist
users = db[user_collection_name]
if user_collection_name not in db.list_collection_names():
    db.create_collection(name=user_collection_name, capped=False, autoIndexId=True)
    print("Created collection '{}'.\n".format(user_collection_name))
else:
    print("Using collection: '{}'.\n".format(user_collection_name))


app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(16))  # Use env variable if available
app.config['DEBUG'] = os.environ.get("FLASK_DEBUG")
WEBHOOK_VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN')

CORS(app=app)

EMBEDDING_MODEL = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
qdrant_client = QdrantClient(url=os.environ.get("QDRANT_URL"),api_key=os.environ.get("QDRANT_API_KEY"))

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        verify_token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if verify_token == WEBHOOK_VERIFY_TOKEN:
            return challenge
        else:
            return 'Invalid Request'
    elif request.method == 'POST':
        body = request.get_json()
        if body.get('object') == 'instagram' and not body.get('entry')[0].get('messaging')[0].get('sender').get('id') == os.environ.get('IG_ID'):
            for entry in body.get('entry'):
                for messaging in entry.get('messaging'):
                    created_time = entry.get('time')
                    sender_id = messaging.get('sender').get('id')
                    mid = messaging.get('message').get('mid')
                    
                    if messaging.get('message') and messaging.get('message').get('text'):
                        text = messaging.get('message').get('text').lower()
                        if text.startswith("search"):
                            # split the text and get the search query
                            search_query = text.split("search")[1].strip()
                            print("Search initiated")
                            response = send_similar_reel(sender_id, search_query)
                            return 'EVENT_RECEIVED', 200
                        else:
                            user = users.find_one({"sender_id": sender_id})
                            if user is None:
                                time.sleep(5)
                                user = users.find_one({"sender_id": sender_id})
                                if user is None:
                                    return 'CANT_FIND_REEL', 400
                            user["message"] = text
                            id = user.pop("_id", None)
                            print("User: ", user)
                            store_embeddings(sender_id, [user])
                            users.delete_one( { "_id" : id })
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
                            print("Reel attachment received")
                            print("payload: ",payload)
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
    print(embeddings_list[0])

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
    return response.points

def send_similar_reel(sender_id, text):
    response = get_similar_messages(collection_name=sender_id, text=text)
    if not response or not response[0].payload:
        print("No results found.")
        return {"error": "No similar messages found."}

    link = response[0].payload.get("link", "No link available")
    url = f"https://graph.instagram.com/v22.0/me/messages?access_token={os.environ.get('INSTA_ACCESS_TOKEN')}"
    response = requests.post(url, json={
        "recipient": {"id": sender_id},
        "message": {"text": link}
    })

    return response.json()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

