"""PostgreSQL-backed singleton session coordinator for temporary Zoho Recruit analytics."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Tuple

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import (
    NotFoundError,
    OAuthError,
    RateLimitError,
    ValidationError,
)
from app.core.logging import get_logger
from app.integrations.zoho.client import DATACENTER_DOMAINS, ZohoRecruitClient
from app.integrations.zoho.crypto import decrypt_token, encrypt_token
from app.persistence.models import (
    AnalyticsSession,
    AnalyticsSessionAuditLog,
    AnalyticsSessionCredential,
    AnalyticsSessionLock,
)

logger = get_logger(__name__)

GLOBAL_LOCK_ID = 8492049182938491  # 64-bit PostgreSQL advisory lock ID
INACTIVITY_TIMEOUT_MINUTES = 30
MAX_PROCESSING_TIMEOUT_MINUTES = 5
BUSY_MESSAGE = "Another analytics session is currently active. Please try again later."
LOCK_KEY = "global_analytics_session"
VALID_DATA_CENTERS = set(DATACENTER_DOMAINS.keys())


class SessionManager:
    """Manages the lifecycle, locking, and credentials of the single active analytics session."""

    _pending_auth_urls: dict[str, str] = {}
    _cleanup_thread: Optional[threading.Thread] = None
    _cleanup_stop_event: Optional[threading.Event] = None

    @staticmethod
    def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    @classmethod
    def _now_utc(cls) -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _hash_token(cls, val: str) -> str:
        return hashlib.sha256(val.encode("utf-8")).hexdigest()

    @classmethod
    def _build_zoho_auth_url(cls, raw_state: str, data_center: str = "com") -> str:
        settings = get_settings()
        redirect_uri = settings.zoho_redirect_uri or "https://channel-yield-analytics-agent.onrender.com/v2/integrations/zoho/callback"
        client = ZohoRecruitClient(
            client_id=settings.zoho_client_id or "dummy_client_id",
            client_secret=settings.zoho_client_secret or "dummy_client_secret",
            redirect_uri=redirect_uri,
            data_center=data_center,
        )
        return client.get_authorization_url(state=raw_state)

    @classmethod
    def _acquire_advisory_lock(cls, db: Session) -> None:
        """Acquire transaction-level cluster advisory lock on PostgreSQL."""
        if db.bind and db.bind.dialect.name == "postgresql":
            db.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": GLOBAL_LOCK_ID},
            )

    @classmethod
    def _get_or_create_lock_row(cls, db: Session) -> AnalyticsSessionLock:
        lock_row = (
            db.query(AnalyticsSessionLock)
            .filter_by(lock_key=LOCK_KEY)
            .with_for_update()
            .first()
        )
        if not lock_row:
            lock_row = AnalyticsSessionLock(
                lock_key=LOCK_KEY,
                locked_by_session_id=None,
                acquired_at=None,
                heartbeat_at=None,
            )
            db.add(lock_row)
            db.flush()
        return lock_row

    @classmethod
    def acquire_session(cls, db: Session, data_center: str = "com") -> dict[str, Any]:
        """Atomically acquire the global session lock and start OAuth authorization."""
        dc = (data_center or "com").lower().strip()
        if dc not in VALID_DATA_CENTERS:
            dc = "com"

        now_utc = cls._now_utc()
        cls._acquire_advisory_lock(db)
        lock_row = cls._get_or_create_lock_row(db)

        # Find existing active or pending session
        active_session = (
            db.query(AnalyticsSession)
            .filter(
                AnalyticsSession.status.in_([
                    "SESSION_INITIALIZING",
                    "AWAITING_ZOHO_AUTHORIZATION",
                    "ACTIVE",
                    "PROCESSING",
                    "ENDING",
                ])
            )
            .with_for_update()
            .first()
        )

        if active_session:
            # Check for inactivity expiry
            if cls._to_utc(active_session.expires_at) <= now_utc:
                logger.info(
                    "session.reclaiming_expired_session",
                    expired_session_id=active_session.id,
                )
                cls._cleanup_and_expire_session(db, active_session, reason="inactivity_timeout")
            elif active_session.status == "AWAITING_ZOHO_AUTHORIZATION":
                # Reuse existing pending authorization without generating a new state
                auth_url = None
                if active_session.encrypted_auth_url and dc == active_session.data_center:
                    try:
                        auth_url = decrypt_token(active_session.encrypted_auth_url)
                    except Exception:
                        auth_url = None

                if not auth_url:
                    if dc != active_session.data_center:
                        active_session.data_center = dc
                    raw_oauth_state = f"{dc}_{secrets.token_urlsafe(32)}"
                    active_session.oauth_state_hash = cls._hash_token(raw_oauth_state)
                    auth_url = cls._build_zoho_auth_url(raw_oauth_state, dc)
                    active_session.encrypted_auth_url = encrypt_token(auth_url)

                active_session.last_activity_at = now_utc
                active_session.expires_at = now_utc + timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)
                db.commit()
                cls._pending_auth_urls[active_session.id] = auth_url

                return {
                    "status": "awaiting_authorization",
                    "session_id": active_session.id,
                    "authorization_url": auth_url,
                    "expires_in_minutes": INACTIVITY_TIMEOUT_MINUTES,
                }
            else:
                return {
                    "status": "busy",
                    "message": BUSY_MESSAGE,
                    "active_session_status": active_session.status,
                    "expires_at": active_session.expires_at.isoformat(),
                }

        # Create new session
        session_id = str(uuid.uuid4())
        session_token = secrets.token_urlsafe(32)
        session_token_hash = cls._hash_token(session_token)

        raw_oauth_state = f"{dc}_{secrets.token_urlsafe(32)}"
        oauth_state_hash = cls._hash_token(raw_oauth_state)

        # Build authorization URL and encrypt it for worker-safe reuse without raw state DB storage
        auth_url = cls._build_zoho_auth_url(raw_oauth_state, dc)
        enc_auth_url = encrypt_token(auth_url)

        expires_at = now_utc + timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)
        new_session = AnalyticsSession(
            id=session_id,
            session_token_hash=session_token_hash,
            status="AWAITING_ZOHO_AUTHORIZATION",
            data_center=dc,
            oauth_state_hash=oauth_state_hash,
            encrypted_auth_url=enc_auth_url,
            created_at=now_utc,
            last_activity_at=now_utc,
            expires_at=expires_at,
        )
        db.add(new_session)

        lock_row.locked_by_session_id = session_id
        lock_row.acquired_at = now_utc
        lock_row.heartbeat_at = now_utc

        cls._record_audit_event(db, session_id, "session_acquired", {"data_center": dc})
        db.commit()

        cls._pending_auth_urls[session_id] = auth_url

        logger.info(
            "session.acquired",
            session_id=session_id,
            data_center=dc,
        )

        return {
            "status": "awaiting_authorization",
            "session_id": session_id,
            "data_center": dc,
            "authorization_url": auth_url,
            "expires_in_minutes": INACTIVITY_TIMEOUT_MINUTES,
            "expires_at": expires_at.isoformat(),
            "instructions": (
                "Open this link in your browser to sign in to Zoho Recruit and grant access: "
                f"[Connect Zoho Recruit]({auth_url})\n\n"
                "Once authorization is complete, return here and ask me to run your analysis."
            ),
        }

    @classmethod
    def get_pending_auth_url(cls, db: Session) -> dict[str, Any]:
        """Retrieve the pending authorization URL if a session is awaiting sign-in."""
        now_utc = cls._now_utc()
        session = (
            db.query(AnalyticsSession)
            .filter(AnalyticsSession.status == "AWAITING_ZOHO_AUTHORIZATION")
            .filter(AnalyticsSession.expires_at > now_utc)
            .first()
        )
        if not session or not session.oauth_state_hash:
            return {
                "status": "not_pending",
                "message": "No session is currently awaiting Zoho authorization.",
            }

        auth_url = ""
        if session.encrypted_auth_url:
            try:
                auth_url = decrypt_token(session.encrypted_auth_url)
            except Exception:
                auth_url = ""

        return {
            "status": "awaiting_authorization",
            "session_id": session.id,
            "data_center": session.data_center,
            "authorization_url": auth_url,
            "expires_at": session.expires_at.isoformat(),
        }

    @classmethod
    def bind_oauth_credentials(
        cls,
        db: Session,
        raw_state: str,
        code: str,
        client: Optional[ZohoRecruitClient] = None,
    ) -> AnalyticsSession:
        """Validate state, exchange authorization code, and bind encrypted tokens to the session."""
        now_utc = cls._now_utc()
        state_hash = cls._hash_token(raw_state)

        cls._acquire_advisory_lock(db)

        session = (
            db.query(AnalyticsSession)
            .filter(AnalyticsSession.oauth_state_hash == state_hash)
            .with_for_update()
            .first()
        )

        if not session:
            logger.warning("session.oauth_state_not_found_or_already_used")
            raise OAuthError("This authorization request has expired or has already been used.")

        if session.status != "AWAITING_ZOHO_AUTHORIZATION":
            logger.warning("session.oauth_invalid_state_transition", status=session.status)
            raise OAuthError("Session is not awaiting authorization.")

        if cls._to_utc(session.expires_at) <= now_utc:
            cls._cleanup_and_expire_session(db, session, reason="oauth_timeout")
            raise OAuthError("This authorization request has timed out.")

        # Invalidate OAuth state immediately (one-time consumption)
        session.oauth_state_hash = None
        session.encrypted_auth_url = None
        cls._pending_auth_urls.pop(session.id, None)

        settings = get_settings()
        dc = session.data_center or "com"
        zoho_client = client or ZohoRecruitClient(
            client_id=settings.zoho_client_id or "",
            client_secret=settings.zoho_client_secret or "",
            redirect_uri=settings.zoho_redirect_uri or "",
            data_center=dc,
        )

        # Exchange authorization code for tokens
        token_data = zoho_client.exchange_authorization_code(code)
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = int(token_data.get("expires_in", 3600))

        if not access_token:
            session.status = "FAILED"
            session.termination_reason = "token_exchange_missing_access_token"
            cls._record_audit_event(
                db, session.id, "oauth_exchange_failed", {"reason": "missing_access_token"}
            )
            db.commit()
            raise OAuthError("Zoho token exchange failed.")

        # Encrypt tokens with Fernet
        enc_access = encrypt_token(access_token)
        enc_refresh = encrypt_token(refresh_token) if refresh_token else None
        token_expires_at = now_utc + timedelta(seconds=expires_in)

        # Create or update credentials
        cred = (
            db.query(AnalyticsSessionCredential)
            .filter_by(session_id=session.id)
            .first()
        )
        if cred:
            cred.encrypted_access_token = enc_access
            cred.encrypted_refresh_token = enc_refresh
            cred.token_expires_at = token_expires_at
            cred.updated_at = now_utc
        else:
            cred = AnalyticsSessionCredential(
                session_id=session.id,
                encrypted_access_token=enc_access,
                encrypted_refresh_token=enc_refresh,
                token_expires_at=token_expires_at,
                created_at=now_utc,
                updated_at=now_utc,
            )
            db.add(cred)

        # Transition session to ACTIVE
        session.status = "ACTIVE"
        session.last_activity_at = now_utc
        session.expires_at = now_utc + timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)

        # Attempt to capture zoho org id safely if available
        try:
            org_info = zoho_client.get_organization_info(access_token)
            if org_info and isinstance(org_info, dict):
                session.zoho_org_id = str(org_info.get("id") or org_info.get("org_id") or "")[:64]
        except Exception:
            pass

        cls._record_audit_event(
            db, session.id, "oauth_completed", {"data_center": dc, "has_refresh": bool(refresh_token)}
        )
        db.commit()
        logger.info("session.oauth_bound_active", session_id=session.id, data_center=dc)
        return session

    @classmethod
    def verify_and_touch_session(
        cls, db: Session
    ) -> Tuple[AnalyticsSession, ZohoRecruitClient, str]:
        """Verify the active session on every tool call, refresh token if needed, and extend expiry."""
        now_utc = cls._now_utc()

        # Query active session
        session = (
            db.query(AnalyticsSession)
            .filter(
                AnalyticsSession.status.in_([
                    "SESSION_INITIALIZING",
                    "AWAITING_ZOHO_AUTHORIZATION",
                    "ACTIVE",
                    "PROCESSING",
                    "ENDING",
                ])
            )
            .with_for_update()
            .first()
        )

        if not session:
            raise NotFoundError("No active analytics session found. Please call 'start_analytics_session' to begin.")

        # Check for expiry
        if cls._to_utc(session.expires_at) <= now_utc:
            cls._cleanup_and_expire_session(db, session, reason="inactivity_timeout")
            raise OAuthError("Analytics session has expired due to 30 minutes of inactivity. Please start a new session.")

        if session.status == "AWAITING_ZOHO_AUTHORIZATION":
            raise ValidationError("Analytics session is awaiting Zoho sign-in. Please complete authorization first.")

        if session.status != "ACTIVE" and session.status != "PROCESSING":
            raise ValidationError(f"Session is in '{session.status}' state and not ready for queries.")

        # Load credentials
        cred = (
            db.query(AnalyticsSessionCredential)
            .filter_by(session_id=session.id)
            .with_for_update()
            .first()
        )
        if not cred:
            cls._cleanup_and_expire_session(db, session, reason="missing_credentials")
            raise OAuthError("Session credentials are missing; please restart the analytics session.")

        # Refresh access token if expiring within 60s
        settings = get_settings()
        dc = session.data_center or "com"
        client = ZohoRecruitClient(
            client_id=settings.zoho_client_id or "",
            client_secret=settings.zoho_client_secret or "",
            redirect_uri=settings.zoho_redirect_uri or "",
            data_center=dc,
        )

        access_token = decrypt_token(cred.encrypted_access_token)
        token_exp = cred.token_expires_at
        if token_exp and token_exp.tzinfo is None:
            token_exp = token_exp.replace(tzinfo=timezone.utc)

        if not access_token or (token_exp and token_exp <= now_utc + timedelta(seconds=60)):
            refresh_token = decrypt_token(cred.encrypted_refresh_token) if cred.encrypted_refresh_token else None
            if not refresh_token:
                cls._cleanup_and_expire_session(db, session, reason="token_expired_no_refresh")
                raise OAuthError("Zoho access token expired and no refresh token is available.")

            logger.info("session.refreshing_access_token", session_id=session.id)
            refreshed = client.refresh_access_token(refresh_token)
            access_token = refreshed["access_token"]
            expires_in = int(refreshed.get("expires_in", 3600))

            cred.encrypted_access_token = encrypt_token(access_token)
            cred.token_expires_at = now_utc + timedelta(seconds=expires_in)
            cred.updated_at = now_utc

        # Touch session: extend 30m inactivity window
        session.last_activity_at = now_utc
        session.expires_at = now_utc + timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)
        db.commit()

        return session, client, access_token

    @classmethod
    def terminate_session(
        cls, db: Session, reason: str = "explicit_termination"
    ) -> dict[str, Any]:
        """Immediately terminate active session, revoke Zoho grant, and purge credentials."""
        now_utc = cls._now_utc()
        cls._acquire_advisory_lock(db)
        lock_row = cls._get_or_create_lock_row(db)

        session = (
            db.query(AnalyticsSession)
            .filter(
                AnalyticsSession.status.in_([
                    "SESSION_INITIALIZING",
                    "AWAITING_ZOHO_AUTHORIZATION",
                    "ACTIVE",
                    "PROCESSING",
                    "ENDING",
                ])
            )
            .with_for_update()
            .first()
        )

        if not session:
            return {
                "status": "idle",
                "message": "No active analytics session is currently running.",
            }

        # Transition to ENDING to block concurrent queries
        session.status = "ENDING"
        db.commit()

        # Perform cleanup and finalize
        cls._cleanup_and_expire_session(db, session, reason=reason)
        return {
            "status": "terminated",
            "message": "Analytics session ended successfully. All temporary credentials and tokens have been purged.",
        }

    @classmethod
    def cleanup_expired_sessions(cls, db: Session) -> int:
        """Scan and terminate all non-terminal sessions that exceeded their expiry.

        Safe across multiple workers: serializes via advisory lock and coordinator row lock.
        Cannot delete credentials belonging to a newly acquired session because newly acquired
        sessions have expires_at > now_utc.
        """
        now_utc = cls._now_utc()
        cls._acquire_advisory_lock(db)
        lock_row = cls._get_or_create_lock_row(db)

        expired_sessions = (
            db.query(AnalyticsSession)
            .filter(
                AnalyticsSession.status.in_([
                    "SESSION_INITIALIZING",
                    "AWAITING_ZOHO_AUTHORIZATION",
                    "ACTIVE",
                    "PROCESSING",
                    "ENDING",
                ]),
                AnalyticsSession.expires_at <= now_utc,
            )
            .with_for_update()
            .all()
        )

        cleaned_count = 0
        for sess in expired_sessions:
            cls._cleanup_and_expire_session(db, sess, reason="background_inactivity_cleanup")
            cleaned_count += 1

        return cleaned_count

    @classmethod
    def start_background_cleanup_task(
        cls, db_factory: Callable[[], Session], interval_seconds: int = 60
    ) -> None:
        """Start a daemon thread running periodic expiry sweeps."""
        if cls._cleanup_thread and cls._cleanup_thread.is_alive():
            return

        cls._cleanup_stop_event = threading.Event()

        def _worker():
            while not cls._cleanup_stop_event.is_set():
                try:
                    with db_factory() as db:
                        cls.cleanup_expired_sessions(db)
                except Exception as exc:
                    logger.warning("session.background_cleanup_worker_error", error=type(exc).__name__)
                cls._cleanup_stop_event.wait(interval_seconds)

        cls._cleanup_thread = threading.Thread(target=_worker, name="session_expiry_sweeper", daemon=True)
        cls._cleanup_thread.start()

    @classmethod
    def stop_background_cleanup_task(cls) -> None:
        """Stop the background cleanup daemon."""
        if cls._cleanup_stop_event:
            cls._cleanup_stop_event.set()
        if cls._cleanup_thread:
            cls._cleanup_thread.join(timeout=2.0)
            cls._cleanup_thread = None
            cls._cleanup_stop_event = None

    @classmethod
    def _record_audit_event(
        cls,
        db: Session,
        session_id: Optional[str],
        event_type: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record a privacy-safe audit event.

        Strictly avoids storing candidate PII, recruitment records,
        tokens, or raw OAuth secrets. Only sanitized operational metadata is recorded.
        """
        try:
            payload = json.dumps(details) if details and isinstance(details, dict) else None
            audit = AnalyticsSessionAuditLog(
                session_id=session_id,
                event_type=event_type,
                event_time=cls._now_utc(),
                details_json=payload,
            )
            db.add(audit)
            db.flush()
        except Exception as exc:
            logger.warning("session.audit_log_failed", error_type=type(exc).__name__)

    @classmethod
    def _attempt_zoho_token_revocation(
        cls, refresh_token: Optional[str], data_center: str
    ) -> None:
        """Best-effort remote token revocation with Zoho Accounts."""
        if not refresh_token:
            return
        dc = (data_center or "com").lower().strip()
        domain = DATACENTER_DOMAINS.get(dc, "accounts.zoho.com")
        url = f"https://{domain}/oauth/v2/token/revoke"
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(url, data={"token": refresh_token})
                logger.info(
                    "session.zoho_revocation_attempted",
                    status_code=resp.status_code,
                    data_center=dc,
                )
        except Exception as exc:
            # Revocation failure must never block local credential deletion
            logger.warning(
                "session.zoho_revocation_network_error",
                error_type=type(exc).__name__,
                data_center=dc,
            )

    @classmethod
    def _cleanup_and_expire_session(
        cls, db: Session, session: AnalyticsSession, reason: str
    ) -> None:
        """Unconditionally delete local credentials, revoke grant, and release locks."""
        now_utc = cls._now_utc()

        # 1. Fetch refresh token for remote revocation before deleting
        refresh_token = None
        try:
            cred = (
                db.query(AnalyticsSessionCredential)
                .filter_by(session_id=session.id)
                .first()
            )
            if cred and cred.encrypted_refresh_token:
                refresh_token = decrypt_token(cred.encrypted_refresh_token)
        except Exception:
            pass

        # 2. Attempt best-effort remote revocation
        if refresh_token:
            cls._attempt_zoho_token_revocation(refresh_token, session.data_center)

        # 3. Unconditionally delete local credentials strictly for this session
        try:
            db.query(AnalyticsSessionCredential).filter_by(
                session_id=session.id
            ).delete()
        except Exception as e:
            logger.error("session.delete_credentials_failed", error=type(e).__name__)

        # 4. Release global lock row if locked by this session
        lock_row = (
            db.query(AnalyticsSessionLock)
            .filter_by(lock_key=LOCK_KEY)
            .first()
        )
        if lock_row and lock_row.locked_by_session_id == session.id:
            lock_row.locked_by_session_id = None
            lock_row.acquired_at = None
            lock_row.heartbeat_at = now_utc

        # 5. Mark session terminal state and wipe secrets
        terminal_status = "EXPIRED" if ("timeout" in reason or "cleanup" in reason) else "IDLE"
        session.status = terminal_status
        session.ended_at = now_utc
        session.termination_reason = reason
        session.oauth_state_hash = None
        session.encrypted_auth_url = None
        cls._pending_auth_urls.pop(session.id, None)

        cls._record_audit_event(
            db,
            session.id,
            "session_expired" if terminal_status == "EXPIRED" else "session_terminated",
            {"reason": reason, "data_center": session.data_center, "had_credentials": bool(refresh_token)},
        )

        db.commit()
        logger.info(
            "session.cleaned_up",
            session_id=session.id,
            reason=reason,
            final_status=terminal_status,
        )

    @classmethod
    def get_status(cls, db: Session) -> dict[str, Any]:
        """Query current session status and remaining time."""
        now_utc = cls._now_utc()
        session = (
            db.query(AnalyticsSession)
            .filter(
                AnalyticsSession.status.in_([
                    "SESSION_INITIALIZING",
                    "AWAITING_ZOHO_AUTHORIZATION",
                    "ACTIVE",
                    "PROCESSING",
                    "ENDING",
                ])
            )
            .first()
        )

        if not session:
            return {
                "status": "IDLE",
                "is_active": False,
                "message": "No analytics session is currently active.",
            }

        # Check if expired
        if cls._to_utc(session.expires_at) <= now_utc:
            cls._cleanup_and_expire_session(db, session, reason="inactivity_timeout")
            return {
                "status": "IDLE",
                "is_active": False,
                "message": "Previous session expired due to inactivity. System is now IDLE.",
            }

        remaining_seconds = max(0, int((cls._to_utc(session.expires_at) - now_utc).total_seconds()))
        remaining_minutes = remaining_seconds // 60

        auth_url = ""
        if session.status == "AWAITING_ZOHO_AUTHORIZATION" and session.encrypted_auth_url:
            try:
                auth_url = decrypt_token(session.encrypted_auth_url)
            except Exception:
                auth_url = ""

        return {
            "status": session.status,
            "is_active": session.status in ("ACTIVE", "PROCESSING"),
            "session_id": session.id,
            "data_center": session.data_center,
            "zoho_org_id": session.zoho_org_id,
            "authorization_url": auth_url,
            "created_at": session.created_at.isoformat(),
            "last_activity_at": session.last_activity_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
            "remaining_minutes": remaining_minutes,
        }
