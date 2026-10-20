"""
database.py — Persistence layer.

Authentication/session design:
    AgentSession
        -> one row per JWT/login session
        -> identified by JTI
        -> used for authentication/revocation

Conversation design:
    ConversationState
        -> one durable conversation per USER
        -> identified by user_id
        -> survives logout/login
        -> therefore a new JWT/JTI does not create a new conversation

This gives us:

    User 1
       |
       +---- Login/JTI A
       |
       +---- Login/JTI B
       |
       +---- ConversationState
                |
                +---- messages/history

Synchronous SQLAlchemy is intentionally retained.
Async callers such as agent.py use asyncio.to_thread().
"""

from datetime import datetime, timezone
import json

from sqlalchemy import (
    create_engine,
    String,
    Integer,
    DateTime,
    Boolean,
    Text,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from config import settings


# =========================================================
# SQLAlchemy base
# =========================================================

class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# =========================================================
# User
# =========================================================

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    username: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
    )

    password_hash: Mapped[str] = mapped_column(
        String(255)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
    )

    sessions: Mapped[list["AgentSession"]] = relationship(
        back_populates="user"
    )


# =========================================================
# Agent session
# =========================================================

class AgentSession(Base):
    """
    One row per issued JWT.

    JTI identifies a LOGIN SESSION.

    It is intentionally different every time a user logs in.
    """

    __tablename__ = "agent_sessions"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    jti: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime
    )

    is_revoked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    user: Mapped["User"] = relationship(
        back_populates="sessions"
    )


# =========================================================
# Conversation state
# =========================================================

class ConversationState(Base):
    """
    Durable conversation history.

    IMPORTANT:
    ConversationState belongs to USER, not JWT SESSION.

    That means:

        logout
        login again

    does NOT create a new conversation.

    The unique constraint prevents a user from accidentally
    getting multiple conversation rows.
    """

    __tablename__ = "conversation_state"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            name="uq_conversation_state_user",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    # Kept for compatibility/debugging.
    #
    # For new records we store:
    #     user:<user_id>
    #
    # Old records may contain a previous JWT JTI.
    session_jti: Mapped[str] = mapped_column(
        String(128),
        index=True,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        index=True,
    )

    state_json: Mapped[str] = mapped_column(
        Text,
        default="{}",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
    )


# =========================================================
# Database engine
# =========================================================

_connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {}
)

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(
    bind=engine,
    expire_on_commit=False,
)


# =========================================================
# Initialize DB
# =========================================================

def init_db() -> None:
    """
    Create tables that do not exist yet.
    """

    Base.metadata.create_all(
        bind=engine
    )


# =========================================================
# User helpers
# =========================================================

def get_user_by_username(
    username: str,
) -> User | None:

    with SessionLocal() as db:

        return (
            db.query(User)
            .filter(
                User.username == username
            )
            .first()
        )


def create_user(
    username: str,
    password_hash: str,
) -> User:

    with SessionLocal() as db:

        user = User(
            username=username,
            password_hash=password_hash,
        )

        db.add(user)
        db.commit()
        db.refresh(user)

        return user


# =========================================================
# Session helpers
# =========================================================

def create_session_record(
    user_id: int,
    jti: str,
    expires_at: datetime,
) -> AgentSession:

    with SessionLocal() as db:

        record = AgentSession(
            user_id=user_id,
            jti=jti,
            expires_at=expires_at,
        )

        db.add(record)
        db.commit()
        db.refresh(record)

        return record


def is_session_valid(
    jti: str,
) -> bool:

    with SessionLocal() as db:

        record = (
            db.query(AgentSession)
            .filter(
                AgentSession.jti == jti
            )
            .first()
        )

        if record is None:
            return False

        if record.is_revoked:
            return False

        expires_at = record.expires_at

        # SQLite may return a naive datetime.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(
                tzinfo=timezone.utc
            )

        return expires_at > _utcnow()


def revoke_session(
    jti: str,
) -> None:

    with SessionLocal() as db:

        record = (
            db.query(AgentSession)
            .filter(
                AgentSession.jti == jti
            )
            .first()
        )

        if record:

            record.is_revoked = True

            db.commit()


# =========================================================
# Conversation helpers
# =========================================================

def _get_user_id_from_jti(
    db,
    session_jti: str,
) -> int | None:
    """
    Convert a JWT JTI into the stable user_id.

    JTI is temporary.
    user_id is stable.

    This is the bridge that allows existing agent.py code to
    continue passing session_jti while conversation storage
    actually belongs to the user.
    """

    session = (
        db.query(AgentSession)
        .filter(
            AgentSession.jti == session_jti
        )
        .first()
    )

    if session is None:
        return None

    return session.user_id


def load_conversation_state(
    session_jti: str,
) -> dict:
    """
    Load conversation state for the USER associated with the
    supplied JWT JTI.

    Therefore:

        JTI A -> User 1 -> conversation
        JTI B -> User 1 -> SAME conversation
    """

    with SessionLocal() as db:

        # -------------------------------------------------
        # Step 1:
        # Find the stable user ID from this login's JTI.
        # -------------------------------------------------

        user_id = _get_user_id_from_jti(
            db,
            session_jti,
        )

        if user_id is None:

            # Invalid/unknown session.
            return {
                "messages": []
            }

        # -------------------------------------------------
        # Step 2:
        # Find conversation by user_id.
        # -------------------------------------------------

        record = (
            db.query(ConversationState)
            .filter(
                ConversationState.user_id == user_id
            )
            .first()
        )

        if record is None:

            return {
                "messages": []
            }

        # -------------------------------------------------
        # Step 3:
        # Safely decode JSON.
        # -------------------------------------------------

        try:

            state = json.loads(
                record.state_json
            )

        except (json.JSONDecodeError, TypeError):

            return {
                "messages": []
            }

        # Make sure messages exists.
        if not isinstance(state, dict):
            state = {}

        if "messages" not in state:
            state["messages"] = []

        # -------------------------------------------------
        # Step 4:
        # Migrate old records.
        #
        # Your old database may contain a previous JTI here.
        # New records use user:<id>.
        # -------------------------------------------------

        canonical_key = f"user:{user_id}"

        if record.session_jti != canonical_key:

            record.session_jti = canonical_key

            db.commit()

        return state


def save_conversation_state(
    session_jti: str,
    user_id: int,
    state: dict,
) -> None:
    """
    Save conversation state for the USER.

    session_jti is accepted for compatibility with existing
    agent.py, but durable ownership is user_id.
    """

    with SessionLocal() as db:

        # -------------------------------------------------
        # Always use the stable user-based key.
        # -------------------------------------------------

        canonical_key = f"user:{user_id}"

        # -------------------------------------------------
        # Find the conversation by user_id.
        # -------------------------------------------------

        record = (
            db.query(ConversationState)
            .filter(
                ConversationState.user_id == user_id
            )
            .first()
        )

        payload = json.dumps(
            state,
            ensure_ascii=False,
        )

        if record is None:

            # First conversation for this user.
            record = ConversationState(
                session_jti=canonical_key,
                user_id=user_id,
                state_json=payload,
            )

            db.add(record)

        else:

            # Existing conversation.
            record.session_jti = canonical_key
            record.state_json = payload
            record.updated_at = _utcnow()

        db.commit()