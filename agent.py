import os, logging, re
from google import genai
from functions import (
    store_embeddings,
    send_error_message,
    send_reaction,
    qdrant_client,
    find_point_by_mid,
    get_recent_conversations,
    recent_transcript,
    gemini_generate,
    budget_available,
    account_message,
    unprocessed_mids,
    processed,
)
from prompt import (ROUTER_PROMPT, ANSWER_QUESTION)
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# Model names deprecate per project; keep them configurable.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"

logger = logging.getLogger(__name__)


def _route_text(text, reel_context, history):
    """One call decides intent *and* writes the reply, so chit-chat costs one call."""
    prompt = (ROUTER_PROMPT
              .replace("{REEL}", (reel_context or "none")[:1500])
              .replace("{HISTORY}", (history or "none")[:3000])
              .replace("{text}", text))
    response = gemini_generate(prompt, model=GEMINI_MODEL)
    body = ((response.text if response else "") or "").strip()
    if not body:
        return None, ""
    intent_match = re.search(r"INTENT:\s*(context|question|account|chat)", body, re.I)
    answer_match = re.search(r"ANSWER:\s*(.*)", body, re.S | re.I)
    intent = intent_match.group(1).lower() if intent_match else None
    answer = answer_match.group(1).strip() if answer_match else ""
    # Guard against the model echoing the label block instead of a reply.
    if answer.upper().startswith("INTENT:"):
        answer = ""
    return intent, answer


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


def answer_question(user_id, question, context, reply_to_mid=None):
    """Answers questions based on the reel description and the chat transcript."""
    try:
        prompt = ANSWER_QUESTION
        history = recent_transcript(user_id) or get_recent_conversations(user_id)
        prompt = prompt.replace("{REEL_DESCRIPTION}", context if context else "No Context Available")
        prompt = prompt.replace("{CHAT_HISTORY}", history)
        prompt = prompt.replace("{question}", question)
        logging.info(f"Answer Question Prompt: {prompt[:400]}")
        response = gemini_generate(prompt, model=GEMINI_MODEL)
        if response is None:
            send_error_message(user_id, "I'm at capacity right now. Try again shortly.")
            return False
        logging.info(f"Gemini response: {(response.text or '')[:200]}")
        send_error_message(user_id, response.text, reply_to_mid=reply_to_mid)
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
                # The user replied to one of the bot's own messages: that mid
                # belongs to the reply, not to the reel. If a recently forwarded
                # reel is still pending, attach the text to it instead of failing.
                pending = unprocessed_mids.find_one({"sender_id": user_id})
                if pending:
                    logger.info("Reply target unknown; attaching context to the last forwarded reel")
                    return save_context_to_last_reel(user_id, text, mid, created_time)

                logger.warning(f"No points found for replied-to MID: {replied_to_mid}")
                send_error_message(user_id, "I don't have a recent reel to attach that to. Forward the reel again.")
                return False
            found_payload = found_point.payload
            logger.info(f"Found point with payload: {found_payload}")

        # Check if the message is a question or context to save
        intent, answer = _route_text(
            text,
            found_payload.get("message") if found_payload else None,
            recent_transcript(user_id),
        )
        logging.info(f"Router intent: {intent}")

        if intent == "context":
            if found_payload:
                return save_context_to_replied_reel(user_id, found_payload, text, mid, created_time)
            return save_context_to_last_reel(user_id, text, mid, created_time)

        if intent == "account":
            # Balance and link are built in code, never written by the model.
            send_error_message(user_id, account_message(user_id), reply_to_mid=mid)
            return True

        if intent in ("question", "chat"):
            reply = answer or (
                "I'm not sure what to say to that. Send me a reel, ask me a question, "
                "or use `search <what you remember>`."
            )
            send_error_message(user_id, reply, reply_to_mid=mid)
            return True

        send_error_message(
            user_id,
            "I'm at capacity right now. Try again in a few minutes."
            if not budget_available()
            else "I didn't catch that. Send a reel, ask me something, or use `search <what you remember>`.",
        )
        return False
    except Exception as e:
        logger.exception(f"Error in handle_text_with_gemini_agent: {e}")
        try:
            from app import _notify
            _notify(f"reel-finder: AI handler error: {str(e)[:150]}")
        except Exception:
            pass
        send_error_message(user_id, "Error processing your message with AI")
        return False
