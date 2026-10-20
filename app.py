"""
app.py — Streamlit frontend.

The Streamlit UI is synchronous, while the agent uses async Gemini calls.

To avoid "Event loop is closed" errors, this file keeps one dedicated
asyncio event loop alive in a background thread for the Streamlit session.
The loop is NOT destroyed when the user logs out, so the Gemini async
connection can continue using the same event loop after re-login.

Conversation state is persisted in the database, so logging out and logging
back in does not lose the conversation.
"""

import asyncio
import threading
from concurrent.futures import Future

import streamlit as st

import auth
from agent import run_turn
from database import init_db
from rate_limiter import RateLimitExceeded, rate_limit_status


st.set_page_config(
    page_title="Multi-Session Agent",
    page_icon="🤖",
)

init_db()


# =========================================================
# Persistent background asyncio loop
# =========================================================

class AsyncLoopRunner:
    """
    Keeps one asyncio event loop alive in a dedicated background thread.

    Streamlit can rerun the app many times, but the loop continues to live.
    This prevents async clients such as Gemini from being attached to a
    loop that gets destroyed after every Streamlit interaction.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
        )
        self.thread.start()

    def _run_loop(self):
        """Run the event loop forever in the background thread."""
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro):
        """
        Submit a coroutine to the persistent background event loop
        and wait for its result.
        """
        future: Future = asyncio.run_coroutine_threadsafe(
            coro,
            self.loop,
        )

        return future.result()


@st.cache_resource
def _get_async_runner():
    """
    Create one persistent AsyncLoopRunner for the Streamlit app.

    cache_resource keeps it alive across Streamlit reruns.
    """
    return AsyncLoopRunner()


def _run_async(coro):
    """
    Run the coroutine using the persistent background event loop.
    """
    runner = _get_async_runner()
    return runner.run(coro)


# =========================================================
# Session state
# =========================================================

def _init_session_state():
    st.session_state.setdefault(
        "jwt_token",
        None,
    )

    st.session_state.setdefault(
        "user_payload",
        None,
    )

    st.session_state.setdefault(
        "chat_display",
        [],
    )


# =========================================================
# Restore persisted conversation
# =========================================================

def _restore_chat_display(session_jti: str):
    """
    Load conversation history from the database and rebuild the
    visible Streamlit chat.
    """

    from database import load_conversation_state

    state = load_conversation_state(session_jti)

    st.session_state["chat_display"] = [
        (message["role"], message["content"])
        for message in state.get("messages", [])
    ]


# =========================================================
# Login / Registration page
# =========================================================

def login_page():

    st.title("🤖 Multi-Session Agent — Sign in")

    tab_login, tab_register = st.tabs(
        ["Log in", "Register"]
    )

    # -----------------------------------------------------
    # Login
    # -----------------------------------------------------

    with tab_login:

        with st.form("login_form"):

            username = st.text_input(
                "Username"
            )

            password = st.text_input(
                "Password",
                type="password",
            )

            submitted = st.form_submit_button(
                "Log in"
            )

        if submitted:

            try:
                token = auth.login(
                    username,
                    password,
                )

                payload = auth.verify_token(
                    token
                )

                st.session_state["jwt_token"] = token

                st.session_state["user_payload"] = payload

                # Restore conversation from DB.
                _restore_chat_display(
                    payload["jti"]
                )

                st.rerun()

            except auth.AuthError as e:

                st.error(str(e))

    # -----------------------------------------------------
    # Registration
    # -----------------------------------------------------

    with tab_register:

        with st.form("register_form"):

            new_username = st.text_input(
                "Choose a username"
            )

            new_password = st.text_input(
                "Choose a password (min 8 chars)",
                type="password",
            )

            reg_submitted = st.form_submit_button(
                "Create account"
            )

        if reg_submitted:

            try:

                auth.register(
                    new_username,
                    new_password,
                )

                st.success(
                    "Account created — you can log in now."
                )

            except auth.AuthError as e:

                st.error(str(e))


# =========================================================
# Chat page
# =========================================================

def chat_page():

    payload = st.session_state["user_payload"]

    session_jti = payload["jti"]

    # -----------------------------------------------------
    # Sidebar
    # -----------------------------------------------------

    with st.sidebar:

        st.write(
            f"Signed in as **{payload['username']}**"
        )

        st.caption(
            f"Rate limiter backend: {rate_limit_status()}"
        )

        if st.button("Log out"):

            try:
                auth.logout(
                    st.session_state["jwt_token"]
                )
            except Exception:
                pass

            # Clear authentication state.
            st.session_state["jwt_token"] = None

            st.session_state["user_payload"] = None

            st.session_state["chat_display"] = []

            # IMPORTANT:
            # Do NOT close the AsyncLoopRunner here.
            #
            # It is intentionally kept alive so the Gemini async
            # client does not end up attached to a closed event loop.

            st.rerun()

    # -----------------------------------------------------
    # Page title
    # -----------------------------------------------------

    st.title("🤖 Agent")

    # -----------------------------------------------------
    # Display existing conversation
    # -----------------------------------------------------

    for role, text in st.session_state["chat_display"]:

        with st.chat_message(
            "assistant"
            if role == "model"
            else "user"
        ):

            st.markdown(text)

    # -----------------------------------------------------
    # New message
    # -----------------------------------------------------

    user_input = st.chat_input(
        "Ask the agent something..."
    )

    if not user_input:
        return

    # -----------------------------------------------------
    # Verify JWT
    # -----------------------------------------------------

    try:

        payload = auth.verify_token(
            st.session_state["jwt_token"]
        )

    except auth.AuthError as e:

        st.error(
            f"Session no longer valid: {e}"
        )

        st.session_state["jwt_token"] = None

        st.session_state["user_payload"] = None

        st.session_state["chat_display"] = []

        st.rerun()

        return

    # -----------------------------------------------------
    # Show user message
    # -----------------------------------------------------

    st.session_state["chat_display"].append(
        ("user", user_input)
    )

    with st.chat_message("user"):

        st.markdown(user_input)

    # -----------------------------------------------------
    # Call agent
    # -----------------------------------------------------

    with st.chat_message("assistant"):

        with st.spinner("Thinking..."):

            try:

                reply = _run_async(
                    run_turn(
                        user_id=int(payload["sub"]),
                        session_jti=payload["jti"],
                        user_message=user_input,
                    )
                )

                st.markdown(reply)

                st.session_state["chat_display"].append(
                    ("model", reply)
                )

            except RateLimitExceeded as e:

                st.warning(
                    "Rate limit hit — try again in "
                    f"about {e.retry_after_seconds:.0f}s."
                )

            except Exception as e:

                st.error(
                    f"Something went wrong: {e}"
                )


# =========================================================
# Main
# =========================================================

def main():

    _init_session_state()

    if st.session_state["jwt_token"] is None:

        login_page()

    else:

        chat_page()


if __name__ == "__main__":
    main()