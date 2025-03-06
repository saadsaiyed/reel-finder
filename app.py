import requests, os, secrets, json, hashlib
from flask import Flask, render_template, Blueprint, session, redirect, request, jsonify
from flask_cors import CORS
from flask_session import Session
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# Set secret key for session
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(16))  # Use env variable if available
app.config['DEBUG'] = os.environ.get("FLASK_DEBUG")
WEBHOOK_VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN')

CORS(app=app)

EMBEDDING_MODEL = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
qdrant_client = QdrantClient(
    url=os.environ.get("QDRANT_URL"),
    api_key=os.environ.get("QDRANT_API_KEY")
)

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
        if body.get('object') == 'page':
            for entry in body.get('entry'):
                for messaging_event in entry.get('messaging'):
                    if messaging_event.get('message'):
                        # Handle the message event
                        response = respond_to_webhook(messaging_event)
                        print(response)
                        return 'EVENT_RECEIVED'
        return 'EVENT_RECEIVED'
    return 'Invalid Request'

def respond_to_webhook(data):
    data = data.get("entry")[0].get("messaging")[0]
    sender_id = data.get("sender").get("id")
    message = data.get("message").get("text")
    return send_similar_reel(sender_id, message)

@app.route("/")
def main():
    APP_ID = os.environ.get("INSTA_APP_ID")
    APP_SECRET = os.environ.get("INSTA_APP_SECRET")
    REDIRECT_URI = os.environ.get("REDIRECT_URI")
    
    # Just for testing
    token = request.args.get("token")
    if token:
        session["access_token"] = token
        session.modified = True
        return redirect("/conversations")

    code = request.args.get("code")
    if code:
        response = requests.post(f"https://api.instagram.com/oauth/access_token", data={
            "client_id": APP_ID,
            "client_secret": APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code": code
        })
        
        short_live_access_token = response.json().get("access_token")
        response = requests.get(f"https://graph.instagram.com/access_token?client_id={APP_ID}&client_secret={APP_SECRET}&grant_type=ig_exchange_token&access_token={short_live_access_token}")
        
        long_live_access_token = response.json().get("access_token")
        session.permanent = True 
        session["access_token"] = long_live_access_token
        session.modified = True
        
        return redirect("/conversations")
    return f"""
    <h1>Instagram Login</h1>
    <a href="{os.environ.get("LOGIN_URL")}">Login with Instagram</a>
    """

@app.route("/conversations")
def conversations():
    response = requests.get(f"https://graph.instagram.com/v22.0/me/conversations?platform=instagram&access_token={os.environ.get('INSTA_ACCESS_TOKEN')}")
    conversations = response.json().get("data")
    return_text = ""
    for conversation in conversations:
        conv_id = conversation.get("id")
        response = requests.get(f'https://graph.instagram.com/v22.0/{conv_id}/messages?fields=attachments,id,message,from,to,created_time,reactions,shares&access_token={os.environ.get("INSTA_ACCESS_TOKEN")}')
        messages = response.json()
        
        # reverse the array
        messages["data"] = messages["data"][::-1]

        for message in messages["data"]:
            if message.get("shares"):
                for link in message["shares"]["data"]:
                    return_text += f"""
                        <div>
                            <h3>{message.get("from").get("username")}</h3>
                            <p>{message.get("message")}</p>
                            <a href="{link.get("link")}" target="_blank">{link.get("link")}</a>
                        </div><br/>
                        """
            else:
                return_text += f"""
                    <div>
                        <h3>{message.get("from").get("username")}</h3>
                        <p>{message.get("message")}</p>
                    </div><br/>
                    """
        return_text += f"{conv_id}<hr/>"
    return return_text

@app.route("/conversations/<conversation_id>")
def messages(conversation_id):
    response = requests.get(f'https://graph.instagram.com/v22.0/{conversation_id}/messages?fields=attachments,id,message,from,to,created_time,reactions,shares&access_token={os.environ.get("INSTA_ACCESS_TOKEN")}')
    response = response.json()
    messages = response.get("data")
    
    # save this json in data.json
    with open('data.json', 'w') as f:
        f.write(json.dumps(messages))
    if "paging" in response:
        while response.get("paging"):
            if response.get("paging").get("next"):
                next_url = response.get("paging").get("next")
                response = requests.get(next_url)
                messages.append(response.json().get("data"))

                with open('data.json', 'a') as f:
                    f.write(json.dumps(response.json().get("data")))
            else:
                break
    
    print(messages)

    return_text = ""
    embedding_msg=[]
    msg={}
    for message in messages:
        print(message)
        if message.get("shares"):
            for link in message["shares"]["data"]:
                return_text += f"""
                    <div>
                        <h3>{message.get("from").get("username")}</h3>
                        <p>{message.get("message")}</p>
                        <a href="{link.get("link")}" target="_blank">{link.get("link")}</a>
                    </div><br/>
                    """
                msg["link"] = link.get("link")
        else:
            return_text += f"""
                <div>
                    <h3>{message.get("from").get("username")}</h3>
                    <p>{message.get("message")}</p>
                </div><br/>
                """
            msg["message"]= message.get("message")
            msg["username"]= message.get("from").get("username")
            msg["id"]= message.get("id")
            msg["created_time"]= message.get("created_time")
            embedding_msg.append(msg)
    return_text += "<hr/>"
    print(embedding_msg)
    store_embeddings(message.get("from").get("id"), msg)
    return return_text

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
        text = message.get("message")
        embedding = EMBEDDING_MODEL.embed_query(text)

        embeddings_list.append({
            "id": int(hashlib.sha256(message.get("id").encode()).hexdigest(), 16) % (10**12),
            "vector": embedding,
            "payload": {
                "message": text,
                "username": message.get("username"),
                "link": message.get("link"),
                "created_time": message.get("created_time")
            }
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

while False:
    response = get_similar_messages("saiyed_saad7", input("Enter text: "))
    for point in response:  # Each result is a ScoredPoint object
        score = point.score
        payload = point.payload

        message = payload.get("message", "No message")
        link = payload.get("link", "No link")

        print(f"Score: {score:.4f}\nMessage: {message}\nLink: {link}\n{'-'*50}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080)))

