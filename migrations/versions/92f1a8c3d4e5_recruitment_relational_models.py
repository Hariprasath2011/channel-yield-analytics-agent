"""recruitment_relational_models

Revision ID: 92f1a8c3d4e5
Revises: 81e098bcb28a
Create Date: 2026-11-04 12:45:00.000000

Adds canonical relational recruitment entities:
  - source_channels
  - candidates
  - job_openings
  - applications
  - application_stage_events
  - recruitment_activities
  - recruitment_costs
  - sync_logs
  - analytics_snapshots
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '92f1a8c3d4e5'
down_revision: Union[str, None] = '81e098bcb28a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. source_channels
    op.create_table(
        'source_channels',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('channel_name', sa.String(length=128), nullable=False),
        sa.Column('raw_source_value', sa.String(length=128), nullable=False),
        sa.Column('category', sa.String(length=64), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'raw_source_value', name='uq_source_channels_tenant_raw'),
    )
    with op.batch_alter_table('source_channels', schema=None) as batch_op:
        batch_op.create_index('ix_source_channels_tenant_channel', ['tenant_id', 'channel_name'], unique=False)

    # 2. candidates
    op.create_table(
        'candidates',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('zoho_candidate_id', sa.String(length=128), nullable=False),
        sa.Column('full_name', sa.String(length=256), nullable=True),
        sa.Column('masked_email', sa.String(length=320), nullable=True),
        sa.Column('candidate_status', sa.String(length=64), nullable=True),
        sa.Column('lead_source', sa.String(length=128), nullable=True),
        sa.Column('channel_id', sa.String(length=36), nullable=True),
        sa.Column('created_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['channel_id'], ['source_channels.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'zoho_candidate_id', name='uq_candidates_tenant_zoho_id'),
    )
    with op.batch_alter_table('candidates', schema=None) as batch_op:
        batch_op.create_index('ix_candidates_tenant_status', ['tenant_id', 'candidate_status'], unique=False)

    # 3. job_openings
    op.create_table(
        'job_openings',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('zoho_job_opening_id', sa.String(length=128), nullable=False),
        sa.Column('posting_title', sa.String(length=256), nullable=False),
        sa.Column('job_opening_status', sa.String(length=64), nullable=False),
        sa.Column('department', sa.String(length=128), nullable=True),
        sa.Column('number_of_positions', sa.Integer(), nullable=True),
        sa.Column('opened_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('target_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'zoho_job_opening_id', name='uq_job_openings_tenant_zoho_id'),
    )
    with op.batch_alter_table('job_openings', schema=None) as batch_op:
        batch_op.create_index('ix_job_openings_tenant_status', ['tenant_id', 'job_opening_status'], unique=False)

    # 4. applications
    op.create_table(
        'applications',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('zoho_application_id', sa.String(length=128), nullable=False),
        sa.Column('candidate_id', sa.String(length=36), nullable=True),
        sa.Column('job_opening_id', sa.String(length=36), nullable=True),
        sa.Column('channel_id', sa.String(length=36), nullable=True),
        sa.Column('application_status', sa.String(length=64), nullable=False),
        sa.Column('current_stage', sa.String(length=64), nullable=False),
        sa.Column('source_channel', sa.String(length=128), nullable=False),
        sa.Column('attribution_method', sa.String(length=32), nullable=False),
        sa.Column('attribution_confidence', sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('joined_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('application_outcome', sa.String(length=32), nullable=False),
        sa.Column('rejection_reason', sa.String(length=256), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['channel_id'], ['source_channels.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['job_opening_id'], ['job_openings.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'zoho_application_id', name='uq_applications_tenant_zoho_id'),
    )
    with op.batch_alter_table('applications', schema=None) as batch_op:
        batch_op.create_index('ix_applications_tenant_cand', ['tenant_id', 'candidate_id'], unique=False)
        batch_op.create_index('ix_applications_tenant_channel', ['tenant_id', 'source_channel'], unique=False)
        batch_op.create_index('ix_applications_tenant_req', ['tenant_id', 'job_opening_id'], unique=False)

    # 5. application_stage_events
    op.create_table(
        'application_stage_events',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('application_id', sa.String(length=36), nullable=False),
        sa.Column('zoho_event_id', sa.String(length=128), nullable=True),
        sa.Column('stage', sa.String(length=64), nullable=False),
        sa.Column('previous_stage', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=64), nullable=True),
        sa.Column('entered_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('exited_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_seconds', sa.Integer(), nullable=True),
        sa.Column('duration_days', sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column('performed_by', sa.String(length=128), nullable=True),
        sa.Column('comments', sa.Text(), nullable=True),
        sa.Column('rejection_reason', sa.String(length=256), nullable=True),
        sa.Column('interview_outcome', sa.String(length=64), nullable=True),
        sa.Column('offer_status', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'application_id', 'stage', 'entered_at', name='uq_stage_events_tenant_app_stage_entered'),
    )
    with op.batch_alter_table('application_stage_events', schema=None) as batch_op:
        batch_op.create_index('ix_stage_events_tenant_app', ['tenant_id', 'application_id'], unique=False)
        batch_op.create_index('ix_stage_events_tenant_stage', ['tenant_id', 'stage'], unique=False)

    # 6. recruitment_activities
    op.create_table(
        'recruitment_activities',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('application_id', sa.String(length=36), nullable=True),
        sa.Column('candidate_id', sa.String(length=36), nullable=True),
        sa.Column('zoho_activity_id', sa.String(length=128), nullable=True),
        sa.Column('activity_type', sa.String(length=64), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('performed_by', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('recruitment_activities', schema=None) as batch_op:
        batch_op.create_index('ix_activities_tenant_app', ['tenant_id', 'application_id'], unique=False)

    # 7. recruitment_costs
    op.create_table(
        'recruitment_costs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('channel_id', sa.String(length=36), nullable=True),
        sa.Column('job_opening_id', sa.String(length=36), nullable=True),
        sa.Column('source_channel', sa.String(length=128), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=8), nullable=False, server_default='USD'),
        sa.Column('period_start', sa.DateTime(timezone=True), nullable=True),
        sa.Column('period_end', sa.DateTime(timezone=True), nullable=True),
        sa.Column('description', sa.String(length=256), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['channel_id'], ['source_channels.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['job_opening_id'], ['job_openings.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('recruitment_costs', schema=None) as batch_op:
        batch_op.create_index('ix_costs_tenant_channel', ['tenant_id', 'source_channel'], unique=False)
        batch_op.create_index('ix_costs_tenant_req', ['tenant_id', 'job_opening_id'], unique=False)

    # 8. sync_logs
    op.create_table(
        'sync_logs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('sync_job_id', sa.String(length=36), nullable=True),
        sa.Column('entity_type', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('records_read', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('records_upserted', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('records_failed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['sync_job_id'], ['sync_jobs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('sync_logs', schema=None) as batch_op:
        batch_op.create_index('ix_sync_logs_tenant_job', ['tenant_id', 'sync_job_id'], unique=False)

    # 9. analytics_snapshots
    op.create_table(
        'analytics_snapshots',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('cohort_start', sa.String(length=10), nullable=False),
        sa.Column('cohort_end', sa.String(length=10), nullable=False),
        sa.Column('requisition_id', sa.String(length=128), nullable=True),
        sa.Column('as_of_date', sa.String(length=10), nullable=False),
        sa.Column('metrics_payload', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('analytics_snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_snapshots_tenant_scope', ['tenant_id', 'cohort_start', 'cohort_end'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('analytics_snapshots', schema=None) as batch_op:
        batch_op.drop_index('ix_snapshots_tenant_scope')
    op.drop_table('analytics_snapshots')

    with op.batch_alter_table('sync_logs', schema=None) as batch_op:
        batch_op.drop_index('ix_sync_logs_tenant_job')
    op.drop_table('sync_logs')

    with op.batch_alter_table('recruitment_costs', schema=None) as batch_op:
        batch_op.drop_index('ix_costs_tenant_req')
        batch_op.drop_index('ix_costs_tenant_channel')
    op.drop_table('recruitment_costs')

    with op.batch_alter_table('recruitment_activities', schema=None) as batch_op:
        batch_op.drop_index('ix_activities_tenant_app')
    op.drop_table('recruitment_activities')

    with op.batch_alter_table('application_stage_events', schema=None) as batch_op:
        batch_op.drop_index('ix_stage_events_tenant_stage')
        batch_op.drop_index('ix_stage_events_tenant_app')
    op.drop_table('application_stage_events')

    with op.batch_alter_table('applications', schema=None) as batch_op:
        batch_op.drop_index('ix_applications_tenant_req')
        batch_op.drop_index('ix_applications_tenant_channel')
        batch_op.drop_index('ix_applications_tenant_cand')
    op.drop_table('applications')

    with op.batch_alter_table('job_openings', schema=None) as batch_op:
        batch_op.drop_index('ix_job_openings_tenant_status')
    op.drop_table('job_openings')

    with op.batch_alter_table('candidates', schema=None) as batch_op:
        batch_op.drop_index('ix_candidates_tenant_status')
    op.drop_table('candidates')

    with op.batch_alter_table('source_channels', schema=None) as batch_op:
        batch_op.drop_index('ix_source_channels_tenant_channel')
    op.drop_table('source_channels')
