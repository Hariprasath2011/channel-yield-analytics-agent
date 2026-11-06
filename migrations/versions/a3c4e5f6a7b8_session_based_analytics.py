"""session_based_analytics

Revision ID: a3c4e5f6a7b8
Revises: 92f1a8c3d4e5
Create Date: 2026-11-05 19:25:00.000000

Adds singleton coordinator, session lifecycle, and temporary credentials:
  - analytics_session_locks
  - analytics_sessions
  - analytics_session_credentials
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c4e5f6a7b8'
down_revision: Union[str, None] = '92f1a8c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. analytics_session_locks (singleton lock table)
    op.create_table(
        'analytics_session_locks',
        sa.Column('lock_key', sa.String(length=64), nullable=False),
        sa.Column('locked_by_session_id', sa.String(length=36), nullable=True),
        sa.Column('acquired_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('lock_key'),
    )

    # Seed singleton coordinator row
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "INSERT INTO analytics_session_locks (lock_key, locked_by_session_id, acquired_at, heartbeat_at) "
            "VALUES ('global_analytics_session', NULL, NULL, NULL)"
        )
    )

    # 2. analytics_sessions (session lifecycle table)
    op.create_table(
        'analytics_sessions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('session_token_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='SESSION_INITIALIZING'),
        sa.Column('data_center', sa.String(length=8), nullable=False, server_default='com'),
        sa.Column('zoho_org_id', sa.String(length=64), nullable=True),
        sa.Column('oauth_state_hash', sa.String(length=64), nullable=True),
        sa.Column('encrypted_auth_url', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('processing_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('termination_reason', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_token_hash', name='uq_analytics_session_token_hash'),
        sa.UniqueConstraint('oauth_state_hash', name='uq_analytics_session_oauth_state_hash'),
    )
    op.create_index('ix_analytics_sessions_status', 'analytics_sessions', ['status'], unique=False)
    op.create_index('ix_analytics_sessions_expires_at', 'analytics_sessions', ['expires_at'], unique=False)

    # Partial unique index for PostgreSQL to enforce at most one non-terminal session at the database level
    if conn.dialect.name == 'postgresql':
        op.create_index(
            'uq_analytics_sessions_single_active',
            'analytics_sessions',
            [sa.text('(1)')],
            unique=True,
            postgresql_where=sa.text("status IN ('SESSION_INITIALIZING', 'AWAITING_ZOHO_AUTHORIZATION', 'ACTIVE', 'PROCESSING', 'ENDING')")
        )

    # 3. analytics_session_credentials (isolated temporary token storage)
    op.create_table(
        'analytics_session_credentials',
        sa.Column('session_id', sa.String(length=36), nullable=False),
        sa.Column('encrypted_access_token', sa.Text(), nullable=False),
        sa.Column('encrypted_refresh_token', sa.Text(), nullable=True),
        sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['analytics_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('session_id'),
    )

    # 4. analytics_session_audit_logs (privacy-safe audit trail)
    op.create_table(
        'analytics_session_audit_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('session_id', sa.String(length=36), nullable=True),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('event_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('details_json', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_analytics_session_audit_logs_session_id', 'analytics_session_audit_logs', ['session_id'], unique=False)


def downgrade() -> None:
    conn = op.get_bind()
    op.drop_index('ix_analytics_session_audit_logs_session_id', table_name='analytics_session_audit_logs')
    op.drop_table('analytics_session_audit_logs')
    if conn.dialect.name == 'postgresql':
        op.drop_index('uq_analytics_sessions_single_active', table_name='analytics_sessions')
    op.drop_table('analytics_session_credentials')
    op.drop_index('ix_analytics_sessions_expires_at', table_name='analytics_sessions')
    op.drop_index('ix_analytics_sessions_status', table_name='analytics_sessions')
    op.drop_table('analytics_sessions')
    op.drop_table('analytics_session_locks')
