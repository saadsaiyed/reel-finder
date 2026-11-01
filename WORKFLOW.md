# Reel-Finder Application Logic Workflow

This document provides a detailed breakdown of the code logic for various scenarios handled by the `reel-finder` application.

## Core Scenarios

### Scenario 1: Webhook Verification

This scenario occurs when Instagram sends a `GET` request to the `/webhook` endpoint to verify the webhook's authenticity.

1.  **Request:** An HTTP `GET` request is made to `/webhook`.
    *   **Payload:** The request includes `hub.verify_token` and `hub.challenge` as query parameters.
2.  **Processing (`app.py:webhook`)**:
    *   The function extracts `hub.verify_token` and `hub.challenge` from the request arguments.
    *   It compares the received `hub.verify_token` with the `WEBHOOK_VERIFY_TOKEN` stored in the environment variables.
    *   **If the tokens match:** The function returns the `hub.challenge` value to Instagram, successfully verifying the webhook.
    *   **If the tokens do not match:** It returns an "Invalid verify_token" error with a `403 Forbidden` status.

---

### Scenario 2: Receiving an Instagram Message

This scenario covers all `POST` requests sent to the `/webhook` endpoint, which represent incoming messages from a user.

1.  **Request:** An HTTP `POST` request is made to `/webhook`.
    *   **Payload:** A JSON object containing the message details.
2.  **Initial Processing (`app.py:webhook`)**:
    *   The JSON body is parsed.
    *   **Validation:** It checks if `body.object` is `"instagram"`. If not, it returns a `400 Bad Request`.
    *   It extracts key information like `sender_id`, message `mid`, and the `message` object.
    *   **Self-Message Skip:** It checks if the `sender_id` matches the application's own `IG_ID`. If so, it returns `200 OK` to ignore its own messages.
    *   **Duplicate Check:** It queries the `processed` MongoDB collection to see if the message `mid` has already been handled. If found, it logs and returns `200 OK`.

#### Sub-scenario 2.1: Receiving a Reel/Post without a message

The user shares an Instagram Reel or Post directly to the bot.

1.  **Attachment Check (`app.py:webhook`)**:
    *   The code checks for an `attachments` key in the `message` object.
    *   It verifies that the attachment `type` is `ig_reel` or `ig_post`.
2.  **Background Processing (`app.py:handle_attachment`)**:
    *   A background job is submitted to the `ThreadPoolExecutor` to run the `handle_attachment` function. This immediately frees up the server to respond to Instagram.
    *   **Immediate User Feedback:**
        *   A "love" reaction is sent to the user's message (`functions.py:send_reaction`).
        *   Any previous temporary user data for that `sender_id` is cleared from the `users` collection.
        *   A new entry is created in the `users` collection to temporarily store context about this reel (e.g., `mid`, `reel_id`, `link`) for up to 1 hour, in case the user sends a follow-up message with a description.
3.  **Gemini AI Processing (`functions.py:run_gemini` -> `gemini`)**:
    *   Inside `handle_attachment`, the `run_gemini` function is called.
    *   The reel's video/image is downloaded from the `url`.
    *   The file type is detected (`detect_file_type`).
    *   The file is uploaded to the Google Gemini API.
    *   A prompt is sent to Gemini to generate a textual description of the reel's content.
    *   **Error Handling:** If the Gemini API quota is exceeded or another error occurs, an error message is sent to the user.
4.  **Embedding and Storage (`functions.py:store_embeddings`)**:
    *   The generated text description from Gemini is used as the `message` in a new payload.
    *   This payload is passed to `store_embeddings`.
    *   The function generates a vector embedding of the text using a Hugging Face model (`all-MiniLM-L6-v2`).
    *   The embedding and the payload (containing the reel link, description, etc.) are upserted into the user's specific Qdrant collection (named after their `sender_id`).
5.  **Finalization**:
    *   The message `mid` is added to the `processed` MongoDB collection.
    *   The Gemini-generated description is sent back to the user as a message (`functions.py:send_error_message` is used for sending any text, not just errors).
    *   A final "love" reaction is sent.

#### Sub-scenario 2.2: Receiving a text message (as a reply to a reel)

The user replies directly to a message (likely the bot's "description processed" message or the original reel message) to add or correct the context.

1.  **Text Check (`app.py:webhook`)**:
    *   The code detects a `text` field in the `message` object.
    *   It identifies that the message is a reply by checking for `message.reply_to.mid`.
2.  **Agent Handling (`app.py:webhook` -> `agent.py:handle_text_with_gemini_agent`)**:
    *   The request is passed to `handle_text_with_gemini_agent`.
    *   Since `replied_to_mid` is present, the agent calls `save_context_to_replied_reel`.
3.  **Context Saving (`agent.py:save_context_to_replied_reel`)**:
    *   The function searches the user's Qdrant collection for the point that has the `mid` matching `replied_to_mid`.
    *   **If found:** It creates a new payload using the user's new `text` but keeps the `reel_id` and `link` from the original reel. This new payload is then passed to `store_embeddings` to be saved in Qdrant, effectively adding a new description for the same reel.
    *   **If not found:** An error message is sent to the user.

#### Sub-scenario 2.3: Receiving a text message (as a new message)

The user sends a text message that is not a reply. The system assumes this text is a description for the *last reel* they sent.

1.  **Agent Handling (`agent.py:handle_text_with_gemini_agent`)**:
    *   The agent sees no `replied_to_mid` and the text does not start with "search".
    *   It calls `save_context_to_last_reel`.
2.  **Context Saving (`agent.py:save_context_to_last_reel`)**:
    *   It looks for the user's last reel information in the temporary `users` MongoDB collection.
    *   **Time Check:** It verifies if the last reel was sent within the last hour. If it's too old, it sends an error message and deletes the temporary data.
    *   **If valid:** It combines the new `text` with the stored reel data (`link`, `reel_id`, etc.) and calls `store_embeddings` to save it to Qdrant. The temporary data in the `users` collection is then deleted.

#### Sub-scenario 2.4: Receiving a search command

The user sends a message like "search <query>".

1.  **Agent Handling (`agent.py:handle_text_with_gemini_agent`)**:
    *   The agent detects that the text starts with "search".
    *   It extracts the `search_query` and calls `app.py:handle_search`.
2.  **Search Execution (`app.py:handle_search` -> `functions.py:send_similar_reel`)**:
    *   The `search_query` is used to find the most similar reel.
    *   `get_similar_messages` is called, which generates an embedding for the search query and uses it to search the user's Qdrant collection.
    *   The top matching reel's `link` is retrieved from the payload.
    *   The reel is sent back to the user via the Instagram API.
    *   A "love" reaction is sent to the search message.

#### Sub-scenario 2.5: Receiving a question

The user sends a longer text message that is interpreted as a general question.

1.  **Agent Handling (`agent.py:handle_text_with_gemini_agent`)**:
    *   The agent uses a simple heuristic: if the message is longer than 10 words and doesn't meet other criteria, it's considered a question.
    *   It calls `answer_question`.
2.  **Response (`agent.py:answer_question`)**:
    *   Currently, this function sends a canned response indicating the feature is under development.

#### Sub-scenario 2.6: Receiving an unsupported attachment

The user sends an attachment that is not an `ig_reel` or `ig_post`.

1.  **Attachment Check (`app.py:webhook`)**:
    *   The code iterates through attachments but finds no supported types.
2.  **Response**:
    *   An error message "Unsupported attachment type. Please send an Instagram reel." is sent to the user.

#### Sub-scenario 2.7: Duplicate Message

The application receives a message `mid` that it has already processed.

1.  **Duplicate Check (`app.py:webhook`)**:
    *   The function queries the `processed` MongoDB collection for the `mid`.
2.  **Action**:
    *   If the `mid` exists, the function immediately logs that it's skipping the message and returns a `200 OK` response. This prevents re-processing the same message, which is crucial for idempotency.

---

## Authentication Flow

This flow handles obtaining and managing the Instagram API access token.

### Scenario 3: Initial Authentication (`/` and `/callback`)

1.  **Initiation (`/`)**:
    *   A user (or developer) navigates to the root URL (`/`).
    *   If no authorization `code` is present, it renders `index.html` with a link to the Instagram login page (`LOGIN_URL`).
2.  **Instagram Redirect (`/callback`)**:
    *   After the user logs in and authorizes the app, Instagram redirects them to `/callback` with an authorization `code`.
3.  **Token Exchange (`/callback`)**:
    *   The server receives the `code`.
    *   It makes a `POST` request to Instagram's `oauth/access_token` endpoint to exchange the `code` for a **short-lived access token**.
    *   It then immediately calls `exchange_for_long_lived_token` which makes a `GET` request to `graph.instagram.com/access_token` to exchange the short-lived token for a **long-lived access token** (valid for 60 days).
4.  **Storage**:
    *   The long-lived token, its expiration date, and other metadata are stored in the `creds` MongoDB collection.
    *   A success message with token details is returned as a JSON response.

### Scenario 4: Refreshing the Access Token (`/refresh-token`)

This endpoint should be called periodically (e.g., every 50 days) to keep the token valid.

1.  **Request:** A `POST` request is made to `/refresh-token`.
2.  **Processing**:
    *   It retrieves the current long-lived token from the `creds` collection.
    *   It makes a `POST` request to the `graph.instagram.com/refresh_access_token` endpoint.
    *   Instagram returns a new long-lived token (valid for another 60 days).
    *   The `creds` collection is updated with the new token and its new expiration date.

### Scenario 5: Checking Token Status (`/token-status`)

1.  **Request:** A `GET` request is made to `/token-status`.
2.  **Processing**:
    *   It retrieves the token from the `creds` collection.
    *   It calculates the time remaining until expiration and returns a JSON object containing the status (`valid`, `expired`), `days_remaining`, and other metadata.

---

## Manual Operations

### Scenario 6: Manual Conversation Import (`/conversations/<conversation_id>`)

This is a developer utility to manually import and process a full conversation history.

1.  **Request:** A `GET` request is made to `/conversations/<conversation_id>`.
2.  **Processing**:
    *   It uses the `get_access_token` to paginate through all messages in the specified Instagram conversation.
    *   It saves the full message history to a local JSON file.
    *   It then specifically looks for a pattern: a message containing a reel (`shares`) that is preceded by a text message from a user (not the bot).
    *   For each match, it creates a payload combining the text message and the reel link.
    *   These payloads are used to generate embeddings and are bulk-upserted into the user's Qdrant collection.

---

## Error Handling

*   **Webhook Errors (`app.py:webhook`)**: The main webhook function is wrapped in a `try...except` block. If any unexpected error occurs, it logs the exception and attempts to send a generic "Internal error" message to the user.
*   **Malformed Payloads**: Specific `KeyError` and `IndexError` exceptions are caught to handle malformed webhook data from Instagram, returning a `400 Bad Request`.
*   **Function-Specific Errors**: Functions like `send_similar_reel` or `store_embeddings` have their own `try...except` blocks to catch request exceptions or other errors, log them, and send a specific error message to the user (e.g., "Error finding similar reel").
*   **Gemini API Errors**: `run_gemini` specifically checks for quota exhaustion errors (`429`) and other exceptions, returning distinct error messages.
*   **Long Messages**: `send_error_message` uses a helper function `split_message` to automatically break any message longer than 1000 characters into multiple smaller messages to comply with Instagram's limits.
