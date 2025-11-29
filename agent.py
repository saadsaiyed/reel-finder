import os, logging
from google import genai
from functions import (
    store_embeddings,
    send_error_message,
    send_reaction,
    qdrant_client,
    find_point_by_mid,
    get_recent_conversations,
    unprocessed_mids,
    processed,
)
from prompt import (AGENT_PROMPT, ANSWER_QUESTION)
print(os.environ.get("GEMINI_API_KEY"))
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

logger = logging.getLogger(__name__)


def save_context_to_replied_reel(user_id, found_payload, text, mid, created_time):
    """Save the text as a description for the reel associated with replied_to_mid."""
    try:        
        store_embeddings(
            user_id,
            [
                {
                    "sender_id": user_id,
                    "message": text,
                    "mid": mid,
                    "reel_id": found_payload.get("reel_id"),
                    "link": found_payload.get("link"),
                    "created_time": created_time,
                }
            ],
        )
        send_reaction(user_id, mid, "love")
    except Exception as e:
        logger.exception(f"Error in save_context_to_replied_reel: {e}")
        send_error_message(user_id, f"Error processing reply: {str(e)}")

def save_context_to_last_reel(user_id, text, mid, created_time):
    """Save the text as a description for the last reel the user sent."""
    try:
        user = unprocessed_mids.find_one({"sender_id": user_id})
        if not user:
            send_error_message(
                user_id,
                "If you want to search for a similar reel, please use the command `search <your query>`",
            )
            return

        from datetime import datetime

        current_time = int(datetime.now().timestamp() * 1000)
        if current_time - user.get("created_time", 0) > 1000 * 60 * 60:
            send_error_message(
                user_id,
                """Too late to process your last reel. Please try to send the context again by replying to the reel you want to describe.
                OR
                If you want to search for a similar reel, please use the command `search <your query>`""",
            )
            unprocessed_mids.delete_many({"sender_id": user_id})
            return

        user["message"] = text
        id = user.pop("_id", None)
        response = store_embeddings(user_id, [user])
        if response.get("error"):
            logger.error(
                f"Error storing embeddings for mid {mid}: {response.get('error')}"
            )
            send_error_message(user_id, "Error storing your description")
            return

        unprocessed_mids.delete_one({"_id": id})
        processed.insert_one(
            {"mid": mid, "type": "description", "timestamp": int(datetime.now().timestamp() * 1000)}
        )
        send_reaction(user_id, mid, "love")
    except Exception as e:
        logger.exception(f"Error in save_context_to_last_reel: {e}")
        send_error_message(user_id, "Error processing your description")

def answer_question(user_id, question, context):
    """Answers questions based on the history of text or reel context sent before."""
    try:
        prompt = ANSWER_QUESTION
        prompt = prompt.replace("{REEL_DESCRIPTION}", context if context else "No Context Available")
        prompt = prompt.replace("{CHAT_HISTORY}", get_recent_conversations(user_id))
        prompt = prompt.replace("{question}", question)
        logging.info(f"Answer Question Prompt: {prompt}")
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        logging.info(f"Gemini response: {response.text}")
        send_error_message(user_id, response.text)
    except Exception as e:
        logger.exception(f"Error in answer_question: {e}")
        send_error_message(user_id, "Error answering your question")

def handle_text_with_gemini_agent(user_id, text, mid, created_time, replied_to_mid=None):
    """
    Handles incoming text messages with a Gemini-powered AI agent.
    """
    try:
        from app import handle_search
        text = text.lower()
        if text.startswith("search"):
            search_query = text.split("search", 1)[1].strip()

            handle_search(user_id, search_query, mid)
            return

        if replied_to_mid:
            found_point = find_point_by_mid(user_id, replied_to_mid)
            if not found_point:
                logger.warning(f"No points found for replied-to MID: {replied_to_mid}")
                send_error_message(user_id, "Replied to message not found. Cannot process your request.")
                return
            found_payload = found_point.payload
            logger.info(f"Fount points with payload: {found_payload}")

        # Check if the message is a question or context to save
        response = client.models.generate_content(model="gemini-2.5-flash", contents=AGENT_PROMPT.replace("{text}", text))
        logging.info(f"Gemini response: {response.text}")
        
        if "question" in response.text.lower():
            answer_question(user_id, text, found_payload.get("message") if replied_to_mid else None)
        elif "context" in response.text.lower():
            if replied_to_mid:
                save_context_to_replied_reel(user_id, found_payload, text, mid, created_time)
            else:
                save_context_to_last_reel(user_id, text, mid, created_time)
        else:
            send_error_message(user_id, "Could not determine if your message is a question or context to save.")
        return
    except Exception as e:
        logger.exception(f"Error in handle_text_with_gemini_agent: {e}")
        send_error_message(user_id, "Error processing your message with AI")
