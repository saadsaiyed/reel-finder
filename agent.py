import os
import google.generativeai as genai
from pymongo import MongoClient
from functions import (
    store_embeddings,
    send_error_message,
    send_reaction,
    qdrant_client,
)
import logging

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
logger = logging.getLogger(__name__)

# Initialize database collections locally to avoid circular imports
db_connection_string = os.getenv("DB_CONNECTION_STRING")
db_client = MongoClient(str(db_connection_string))
users = db_client["master"]["users"]
processed = db_client["master"]["processed_mids"]


def save_context_to_replied_reel(user_id, replied_to_mid, text, mid, created_time):
    """Save the text as a description for the reel associated with replied_to_mid."""
    try:
        def find_point_by_mid(collection_name, target_mid):
            """Find a point in Qdrant by MID without requiring an index. Manually iterates through all points."""
            try:
                logger.info(f"Searching for MID {target_mid[:50]}... in collection {collection_name}")
                
                offset = 0
                all_mids = []  # Track all MIDs for debugging
                while True:
                    points, next_offset = qdrant_client.scroll(
                        collection_name=collection_name,
                        offset=offset,
                        limit=100,  # Fetch 100 at a time
                        with_payload=True,
                        with_vectors=False
                    )
                    
                    logger.debug(f"Scrolled batch: {len(points)} points, next_offset: {next_offset}")
                    
                    for point in points:
                        point_mid = point.payload.get("mid")
                        all_mids.append(point_mid)
                        
                        if point_mid == target_mid:
                            logger.info(f"Found matching point for MID: {target_mid[:50]}...")
                            return point
                    
                    if next_offset is None or len(points) == 0:
                        break
                    
                    offset = next_offset
                
                logger.warning(f"No point found with MID: {target_mid[:50]}...")
                logger.info(f"Available MIDs in collection ({len(all_mids)} total):")
                for mid_ in all_mids[:10]:
                    logger.info(f"  - {mid_[:50]}... (full: {mid_})")
                return None
                
            except Exception as e:
                logger.exception(f"Error finding point by MID: {e}")
                return None

        found_point = find_point_by_mid(user_id, replied_to_mid)
        
        if not found_point:
            logger.warning(f"No points found for replied-to MID: {replied_to_mid}")
            send_error_message(user_id, "Cannot add context to the message you replied to. Reel Not Found using reply_to.")
            return

        found_payload = found_point.payload
        logger.info(f"Found point with payload: {found_payload}")
        
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
        user = users.find_one({"sender_id": user_id})
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
                "Too late to process your last reel. Please try to send the reel again with your message within 1hr.",
            )
            send_error_message(
                user_id,
                "If you want to search for a similar reel, please use the command `search <your query>`",
            )
            users.delete_one({"sender_id": user_id})
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

        users.delete_one({"_id": id})
        processed.insert_one(
            {"mid": mid, "type": "description", "timestamp": int(datetime.now().timestamp() * 1000)}
        )
        send_reaction(user_id, mid, "love")
    except Exception as e:
        logger.exception(f"Error in save_context_to_last_reel: {e}")
        send_error_message(user_id, "Error processing your description")

def answer_question(user_id, question):
    """Answers questions based on the history of text or reel context sent before."""
    try:
        # For now, just a simple response
        send_error_message(user_id, "I am still under development, but I am learning to answer your questions based on our conversation. Ask me anything!")
    except Exception as e:
        logger.exception(f"Error in answer_question: {e}")
        send_error_message(user_id, "Error answering your question")

def handle_text_with_gemini_agent(user_id, text, mid, created_time, replied_to_mid=None):
    """
    Handles incoming text messages with a Gemini-powered AI agent.
    """
    try:
        if replied_to_mid:
            save_context_to_replied_reel(
                user_id, replied_to_mid, text, mid, created_time
            )
            return

        if text.startswith("search"):
            from app import handle_search

            search_query = text.split("search", 1)[1].strip()
            handle_search(user_id, search_query, mid)
            return

        # Simple logic to differentiate between saving context and asking a question
        if len(text.split()) > 10:  # If the message is long, assume it's a question
            answer_question(user_id, text)
        else:
            save_context_to_last_reel(user_id, text, mid, created_time)

    except Exception as e:
        logger.exception(f"Error in handle_text_with_gemini_agent: {e}")
        send_error_message(user_id, "Error processing your message with AI")
