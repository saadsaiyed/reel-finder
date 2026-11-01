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
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

logger = logging.getLogger(__name__)


def save_context_to_replied_reel(user_id, found_payload, text, mid, created_time):
    """Save the text as a description for the reel associated with replied_to_mid."""
    try:
        response = store_embeddings(
            user_id,
            [
                {
                    "sender_id": user_id,
                    "message": text,
                    "mid": mid,
                    "reel_id": found_payload.get("reel_id"),
                    "link": found_payload.get("link"),
                    "link_kind": found_payload.get("link_kind", "cdn"),
                    "created_time": created_time,
                }
            ],
        )
        if response.get("error"):
            logger.error(f"Error storing reply context: {response['error']}")
            send_error_message(user_id, "Error saving your description, try again later")
            return False
        send_reaction(user_id, mid, "love")
        return True
    except Exception as e:
        logger.exception(f"Error in save_context_to_replied_reel: {e}")
        send_error_message(user_id, f"Error processing reply: {str(e)}")
        return False


def save_context_to_last_reel(user_id, text, mid, created_time):
    """Save the text as a description for the last reel the user sent."""
    try:
        user = unprocessed_mids.find_one({"sender_id": user_id})
        if not user:
            send_error_message(
                user_id,
                "If you want to search for a similar reel, please use the command `search <your query>`",
            )
            return False

        from datetime import datetime

        current_time = int(datetime.now().timestamp() * 1000)
        try:
            sent_at = int(user.get("created_time", 0))
        except (TypeError, ValueError):
            sent_at = 0
        if current_time - sent_at > 1000 * 60 * 60:
            send_error_message(
                user_id,
                """Too late to process your last reel. Please try to send the context again by replying to the reel you want to describe.
                OR
                If you want to search for a similar reel, please use the command `search <your query>`""",
            )
            unprocessed_mids.delete_many({"sender_id": user_id})
            return False

        user["message"] = text
        id = user.pop("_id", None)
        response = store_embeddings(user_id, [user])
        if response.get("error"):
            logger.error(
                f"Error storing embeddings for mid {mid}: {response.get('error')}"
            )
            send_error_message(user_id, "Error storing your description")
            return False

        unprocessed_mids.delete_one({"_id": id})
        send_reaction(user_id, mid, "love")
        return True
    except Exception as e:
        logger.exception(f"Error in save_context_to_last_reel: {e}")
        send_error_message(user_id, "Error processing your description")
        return False


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
        return True
    except Exception as e:
        logger.exception(f"Error in answer_question: {e}")
        send_error_message(user_id, "Error answering your question")
        return False


def handle_text_with_gemini_agent(user_id, text, mid, created_time, replied_to_mid=None):
    """Routes an incoming text message. Returns True when handled successfully."""
    try:
        from app import handle_search

        lowered = text.lower()
        if lowered.startswith("search"):
            search_query = lowered.split("search", 1)[1].strip()
            if not search_query:
                send_error_message(user_id, "Use `search <what you remember>` so I know what to look for.")
                return False
            return handle_search(user_id, search_query, mid)

        found_payload = None
        if replied_to_mid:
            found_point = find_point_by_mid(user_id, replied_to_mid)
            if not found_point:
                logger.warning(f"No points found for replied-to MID: {replied_to_mid}")
                send_error_message(user_id, "Replied to message not found. Cannot process your request.")
                return False
            found_payload = found_point.payload
            logger.info(f"Found point with payload: {found_payload}")

        # Check if the message is a question or context to save
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=AGENT_PROMPT.replace("{text}", text)
        )
        verdict = (response.text or "").strip().lower()
        logging.info(f"Gemini intent verdict: {verdict}")

        if "question" in verdict:
            return answer_question(
                user_id, text, found_payload.get("message") if found_payload else None
            )
        if "context" in verdict:
            if found_payload:
                return save_context_to_replied_reel(user_id, found_payload, text, mid, created_time)
            return save_context_to_last_reel(user_id, text, mid, created_time)

        send_error_message(user_id, "Could not determine if your message is a question or context to save.")
        return False
    except Exception as e:
        logger.exception(f"Error in handle_text_with_gemini_agent: {e}")
        send_error_message(user_id, "Error processing your message with AI")
        return False
