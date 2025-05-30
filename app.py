VERSION="1.2.2"
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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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


# Fask config
app = Flask(__name__)
CORS(app=app)

app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(16))  # Use env variable if available
app.config['DEBUG'] = True

qdrant_client = QdrantClient(url=os.environ.get("QDRANT_URL"), api_key=os.environ.get("QDRANT_API_KEY"))
EMBEDDING_MODEL = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
logger.info(f"Finished Loading Embedding Model")
executor = ThreadPoolExecutor(max_workers=5)
logger.info(f"Finished Executor")

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    try:
        created_time = None
        sender_id = None
        mid = None
        future_to_context = {}
        if request.method == 'GET':
            verify_token = str(request.args.get('hub.verify_token'))
            challenge = request.args.get('hub.challenge')
            logger.info(f"GET request received with verify_token: {verify_token} and challenge: {challenge}")
            if verify_token == str(os.getenv('WEBHOOK_VERIFY_TOKEN')):
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
                            if text.startswith("search"):
                                search_query = text.split("search")[1].strip()
                                response = send_similar_reel(sender_id, search_query)
                                if response.get('error'):
                                    return 'Error processing search', 500
                                logger.info(f"Search response: {response.json()}")
                                return 'EVENT_RECEIVED', 200
                            else:
                                user = users.find_one({"sender_id": sender_id})
                                if user is None:
                                    time.sleep(5)
                                    user = users.find_one({"sender_id": sender_id})
                                    if user is None:
                                        logger.error("Cannot find reel for sender_id: {sender_id}")
                                        send_error_message(sender_id, "If you want to search for a similar reel, please use the command `search <your query>`")
                                        return 'CANT_FIND_REEL', 400
                                
                                current_time = int(datetime.now().timestamp() * 1000)
                                if user.get("created_time") and current_time - user.get("created_time") < 1000 * 60 * 60:
                                    logger.info(f"Too late to process message for sender_id: {sender_id}")
                                    send_error_message(sender_id, "Too late to process your last reel. Please try to send the reel again with your message within 1hr.")
                                    send_error_message(sender_id, "If you want to search for a similar reel, please use the command `search <your query>`")
                                    return 'EVENT_RECEIVED', 400
                                
                                user["message"] = text
                                id = user.pop("_id", None)
                                store_embeddings(sender_id, [user])
                                users.delete_one({"_id": id})
                                send_reaction(sender_id, mid, "love")
                            return 'EVENT_RECEIVED'
                        elif messaging.get('message') and messaging.get('message').get('attachments'):
                            attachment = messaging.get('message').get('attachments')[0]
                            if attachment.get('type') == 'ig_reel':
                                url = attachment.get('payload').get('url', '')
                                future.append(executor.submit(run_gemini, url))
                                future_to_context[future] = {
                                    "sender_id": sender_id,
                                    "mid": mid,
                                    "reel_id": attachment.get('payload').get('reel_video_id'),
                                    "created_time": created_time,
                                    "url": url
                                }

                for future in as_completed(future_to_context):
                    try:
                        context = future_to_context[future]
                        sender_id = context["sender_id"]
                        mid = context["mid"]
                        reel_id = context["reel_id"]
                        created_time = context["created_time"]
                        url = context["url"]
                        
                        title = future.result()  # Get the result of the completed task
                        if title == "Gemini API quota exceeded":
                            logger.error("Gemini API quota exceeded. Skipping this task.")
                            send_error_message(sender_id, "Gemini API quota exceeded")
                            continue
                        if title == "Error running Gemini":
                            logger.error("Error running Gemini for this task.")
                            send_error_message(sender_id, "Error running Gemini")
                            continue
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
                            return 'Error processing attachment', 500
                        users.delete_many({"sender_id": sender_id})
                        users.insert_one(payload)
                        send_error_message(sender_id, title)
                        send_reaction(sender_id, mid, "love")
                    except Exception as e:
                        logger.error(f"Error in task: {e}")
                        send_error_message(sender_id, str(e))
                        return 'Error processing attachment', 500
            return 'EVENT_RECEIVED'
        return 'Invalid Request'
    except requests.RequestException as exc:
        logging.error("Failed to update data: %s", exc)
        send_error_message(sender_id, str(exc))

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
    url = f"https://graph.instagram.com/v22.0/me/messages?access_token={os.environ.get('INSTA_ACCESS_TOKEN')}"
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

def send_reaction(sender_id, message_id, reaction_type="love"):
    url = f"https://graph.instagram.com/v22.0/me/messages/?access_token={os.environ.get('INSTA_ACCESS_TOKEN')}"
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
    if request.method == "POST":
        code = request.json.get("code")
        logging.info(f"code URL: {code}")
        code = code.split("?code=")[-1]
        logging.info(f"code received: {code}")
        client_id = os.environ.get("CLIENT_ID")
        client_secret = os.environ.get("CLIENT_SECRET")
        redirect_uri = os.environ.get("REDIRECT_URI")
        grant_type = "authorization_code"

        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": grant_type,
            "redirect_uri": redirect_uri,
            "code": code
        }

        response = requests.post("https://api.instagram.com/oauth/access_token", data=payload)
        logging.info(f"Instagram OAuth response: {response}")
        json_response = response.json()
        logging.info(f"Instagram OAuth response: {json_response}")
        
        return {"message": f"Access_Token {json_response['access_token']}"}, 200
    
    login_link = os.environ.get("LOGIN_URL", "#")
    return render_template("index.html", login_link=login_link)

@app.route('/reaction', methods=["GET"])
def reaction():
    return send_reaction(sender_id=2369537610112817, message_id="aWdfZAG1faXRlbToxOklHTWVzc2FnZAUlEOjE3ODQxNDQ3MTkzMTQzMTMyOjM0MDI4MjM2Njg0MTcxMDMwMTI0NDI1OTgxOTQwOTIxNDA0MTg3ODozMjI1NTU4NDg4NTg2ODg0MTkxOTY0Nzg5NjQ5NDQwNzY4MAZDZD", reaction_type="LIKE")

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


