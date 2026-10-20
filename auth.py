"""
auth.py — Authentication and authorization.

Flow:
    register(username, password)
        -> creates a User row with a bcrypt password hash

    login(username, password)
        -> verifies password
        -> creates a new JWT
        -> creates an AgentSession row
        -> returns the JWT

    verify_token(token)
        -> validates JWT signature and expiry
        -> checks the database session record
        -> returns the decoded payload

    logout(token)
        -> revokes the exact JWT session

Important design:
    - JWT `jti` identifies ONE LOGIN SESSION.
    - `user_id` identifies the USER.
    - Conversation history is persisted using `user_id`, not `jti`.

This means:
    Login 1 -> JTI A -> User 1 -> conversation
    Logout
    Login 2 -> JTI B -> User 1 -> SAME conversation

Multiple login sessions can still exist for the same user.
Logging out one session revokes only that JWT.
"""

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from config import settings
from database import (
    create_session_record,
    create_user,
    get_user_by_username,
    is_session_valid,
    revoke_session,
)


class AuthError(Exception):
    """Raised for failed authentication operations."""
    pass


def _hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(
        password.encode("utf-8"),
        password_hash.encode("utf-8"),
    )


def register(username: str, password: str):
    """
    Register a new user.

    Passwords are never stored as plaintext.
    """

    if not username or not username.strip():
        raise AuthError("Username cannot be empty.")

    username = username.strip()

    if get_user_by_username(username) is not None:
        raise AuthError("Username already taken.")

    if len(password) < 8:
        raise AuthError(
            "Password must be at least 8 characters."
        )

    return create_user(
        username=username,
        password_hash=_hash_password(password),
    )


def login(username: str, password: str) -> str:
    """
    Authenticate a user and issue a new JWT.

    IMPORTANT:
    Every login gets a NEW JTI.

    That is intentional.
    JTI represents the login session, not the conversation.
    """

    username = username.strip()

    user = get_user_by_username(username)

    if user is None:
        raise AuthError(
            "Invalid username or password."
        )

    if not _verify_password(
        password,
        user.password_hash,
    ):
        raise AuthError(
            "Invalid username or password."
        )

    # New ID for THIS login session.
    jti = str(uuid.uuid4())

    now = datetime.now(timezone.utc)

    expires_at = (
        now
        + timedelta(
            minutes=settings.jwt_expiry_minutes
        )
    )

    payload = {
        "sub": str(user.id),
        "username": user.username,
        "jti": jti,
        "iat": now,
        "exp": expires_at,
    }

    token = jwt.encode(
        payload,
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    # Save this login session so it can later be revoked.
    create_session_record(
        user_id=user.id,
        jti=jti,
        expires_at=expires_at,
    )

    return token


def verify_token(token: str) -> dict:
    """
    Validate a JWT and confirm that its DB session is still active.
    """

    if not token:
        raise AuthError(
            "No authentication token provided."
        )

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )

    except jwt.ExpiredSignatureError:
        raise AuthError(
            "Session expired — please log in again."
        )

    except jwt.InvalidTokenError:
        raise AuthError(
            "Invalid session token."
        )

    jti = payload.get("jti")

    if not jti:
        raise AuthError(
            "Invalid session token: missing JTI."
        )

    if not is_session_valid(jti):
        raise AuthError(
            "Session has been revoked or is no longer valid."
        )

    return payload


def logout(token: str) -> None:
    """
    Revoke the exact JWT session.

    This does NOT delete conversation history.

    Therefore logging out and logging back in will still recover
    the user's previous conversation.
    """

    if not token:
        return

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={
                # We still want to revoke an expired token if possible.
                "verify_exp": False,
            },
        )

        jti = payload.get("jti")

        if jti:
            revoke_session(jti)

    except jwt.InvalidTokenError:
        # Token is already unusable.
        pass