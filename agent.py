"""
agent.py — Agent execution, state persistence, and rate limiting.

The agent:
- Uses Gemini asynchronously.
- Loads and persists conversation state.
- Applies rate limiting before the LLM call.
- Keeps synchronous DB work off the event loop using asyncio.to_thread().
"""

import asyncio
import google.generativeai as genai

from config import settings
from database import load_conversation_state, save_conversation_state
from rate_limiter import check_rate_limit, RateLimitExceeded



# Validate and configure Gemini


if not settings.gemini_api_key:
    raise ValueError(
        "Gemini API key is missing. "
        "Check your .env file and config.py."
    )

print("Gemini API key loaded:", bool(settings.gemini_api_key))

genai.configure(api_key=settings.gemini_api_key)


class AgentError(Exception):
    """Raised when the agent/LLM call fails."""
    pass


def _build_model():
    """Create and return the configured Gemini model."""
    return genai.GenerativeModel(settings.gemini_model)


async def run_turn(
    user_id: int,
    session_jti: str,
    user_message: str
) -> str:
    """
    Runs one agent turn for the given user message.

    Steps:
    1. Check rate limit.
    2. Load previous conversation state.
    3. Send the message to Gemini.
    4. Save updated conversation state.
    5. Return the model's reply.

    Raises:
        RateLimitExceeded: When the user exceeds the rate limit.
        AgentError: When the Gemini call fails.
    """

    # 1. Rate limit BEFORE calling the LLM.
    check_rate_limit(key=f"user:{user_id}")

    # 2. Load previous conversation state.
    state = await asyncio.to_thread(
        load_conversation_state,
        session_jti
    )

    messages = state.get("messages", [])

    # 3. Convert stored messages into Gemini history format.
    history = [
        {
            "role": message["role"],
            "parts": [message["content"]]
        }
        for message in messages
    ]

    model = _build_model()
    chat = model.start_chat(history=history)

    try:
        response = await chat.send_message_async(user_message)

    except Exception as exc:
        raise AgentError(
            f"Agent call failed: {exc}"
        ) from exc

    # Make sure the model actually returned text.
    try:
        reply_text = response.text
    except Exception as exc:
        raise AgentError(
            "Gemini returned a response without readable text."
        ) from exc

    # 4. Save the new conversation turn.
    messages.append({
        "role": "user",
        "content": user_message
    })

    messages.append({
        "role": "model",
        "content": reply_text
    })

    state["messages"] = messages

    await asyncio.to_thread(
        save_conversation_state,
        session_jti,
        user_id,
        state
    )

    # 5. Return assistant response.
    return reply_text