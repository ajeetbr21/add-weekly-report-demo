"""initial schema: tasks, journal, agents, tools, approvals, memory (pgvector), learning, reports,
schedules, webhooks, system/audit events.

Revision ID: 0001
Revises: 
Create Date: 2026-09-29 19:35:16.380146
"""
from __future__ import annotations

import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.config import get_settings

EMBED_DIM = get_settings().embedding_dimensions

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # human-readable task ids: TASK-<year>-<seq>
    op.execute("CREATE SEQUENCE IF NOT EXISTS task_id_seq START 1")
    op.create_table('agents',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('capabilities', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('keywords', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('tools', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('instructions', sa.Text(), nullable=False),
    sa.Column('model_preference', sa.String(length=32), nullable=False),
    sa.Column('memory_policy', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agents'))
    )
    op.create_table('audit_logs',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('target', sa.String(length=300), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs'))
    )
    op.create_index('ix_audit_logs_ts', 'audit_logs', ['timestamp'], unique=False)
    op.create_table('projects',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('keywords', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_projects')),
    sa.UniqueConstraint('slug', name=op.f('uq_projects_slug'))
    )
    op.create_table('sessions',
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('runtime', sa.String(length=32), nullable=False),
    sa.Column('letta_agent_id', sa.String(length=100), nullable=True),
    sa.Column('state', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_sessions'))
    )
    op.create_table('system_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.Column('level', sa.String(length=16), nullable=False),
    sa.Column('component', sa.String(length=64), nullable=False),
    sa.Column('event', sa.String(length=100), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_system_events'))
    )
    op.create_index('ix_system_events_ts', 'system_events', ['timestamp'], unique=False)
    op.create_table('tools',
    sa.Column('id', sa.String(length=200), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('provider', sa.String(length=64), nullable=False),
    sa.Column('toolkit', sa.String(length=64), nullable=True),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('capabilities', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('input_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('output_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('risk_level', sa.String(length=16), nullable=False),
    sa.Column('requires_approval', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tools'))
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('external_id', sa.String(length=200), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=True),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('source', 'external_id', name='uq_users_source_external')
    )
    op.create_table('scheduled_tasks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('request', sa.Text(), nullable=False),
    sa.Column('schedule_type', sa.String(length=16), nullable=False),
    sa.Column('cron_expression', sa.String(length=100), nullable=True),
    sa.Column('run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('timezone', sa.String(length=64), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_task_id', sa.String(length=32), nullable=True),
    sa.Column('run_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_scheduled_tasks_project_id_projects'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_scheduled_tasks'))
    )
    op.create_index('ix_scheduled_due', 'scheduled_tasks', ['enabled', 'next_run_at'], unique=False)
    op.create_table('tasks',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('parent_task_id', sa.String(length=32), nullable=True),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('session_key', sa.String(length=200), nullable=True),
    sa.Column('original_request', sa.Text(), nullable=False),
    sa.Column('normalized_request', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('selected_agent', sa.String(length=64), nullable=True),
    sa.Column('plan', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('session', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('retry_count', sa.Integer(), nullable=False),
    sa.Column('max_retries', sa.Integer(), nullable=False),
    sa.Column('current_step', sa.Integer(), nullable=False),
    sa.Column('locked_by', sa.String(length=100), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('idempotency_key', sa.String(length=300), nullable=True),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['parent_task_id'], ['tasks.id'], name=op.f('fk_tasks_parent_task_id_tasks'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_tasks_project_id_projects'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_tasks_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tasks')),
    sa.UniqueConstraint('idempotency_key', name=op.f('uq_tasks_idempotency_key'))
    )
    op.create_index('ix_tasks_parent', 'tasks', ['parent_task_id'], unique=False)
    op.create_index('ix_tasks_project_created', 'tasks', ['project_id', 'created_at'], unique=False)
    op.create_index('ix_tasks_session_key', 'tasks', ['session_key'], unique=False)
    op.create_index('ix_tasks_status_priority_created', 'tasks', ['status', 'priority', 'created_at'], unique=False)
    op.create_table('agent_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=False),
    sa.Column('step_index', sa.Integer(), nullable=True),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=200), nullable=True),
    sa.Column('model_provider', sa.String(length=64), nullable=True),
    sa.Column('runtime', sa.String(length=64), nullable=True),
    sa.Column('iterations', sa.Integer(), nullable=False),
    sa.Column('prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('completion_tokens', sa.Integer(), nullable=False),
    sa.Column('output_summary', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_agent_runs_task_id_tasks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_runs'))
    )
    op.create_index('ix_agent_runs_started', 'agent_runs', ['started_at'], unique=False)
    op.create_index('ix_agent_runs_task', 'agent_runs', ['task_id'], unique=False)
    op.create_table('approvals',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=False),
    sa.Column('step_index', sa.Integer(), nullable=True),
    sa.Column('agent_id', sa.String(length=64), nullable=True),
    sa.Column('tool_id', sa.String(length=200), nullable=False),
    sa.Column('action_summary', sa.Text(), nullable=False),
    sa.Column('risk_level', sa.String(length=16), nullable=False),
    sa.Column('arguments', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('arguments_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_by', sa.String(length=200), nullable=True),
    sa.Column('decision_channel', sa.String(length=32), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_approvals_task_id_tasks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_approvals'))
    )
    op.create_index('ix_approvals_status', 'approvals', ['status'], unique=False)
    op.create_index('ix_approvals_task', 'approvals', ['task_id'], unique=False)
    op.create_table('feedback',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('channel', sa.String(length=32), nullable=False),
    sa.Column('rating', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('processed', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_feedback_task_id_tasks'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_feedback_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_feedback'))
    )
    op.create_index('ix_feedback_task', 'feedback', ['task_id'], unique=False)
    op.create_table('memories',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('type', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=True),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('agent_id', sa.String(length=64), nullable=True),
    sa.Column('tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('importance', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('last_accessed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('usage_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_memories_project_id_projects'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_memories_task_id_tasks'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memories'))
    )
    op.create_index('ix_memories_hash_scope', 'memories', ['content_hash', 'project_id'], unique=False)
    op.create_index('ix_memories_project', 'memories', ['project_id'], unique=False)
    op.create_index('ix_memories_task', 'memories', ['task_id'], unique=False)
    op.create_index('ix_memories_type_status', 'memories', ['type', 'status'], unique=False)
    op.create_table('reports',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('objective', sa.Text(), nullable=False),
    sa.Column('executive_summary', sa.Text(), nullable=False),
    sa.Column('final_status', sa.String(length=32), nullable=False),
    sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('markdown', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_reports_project_id_projects'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_reports_task_id_tasks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reports')),
    sa.UniqueConstraint('task_id', name='uq_reports_task_id')
    )
    op.create_table('task_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('event_type', sa.String(length=48), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=True),
    sa.Column('step_index', sa.Integer(), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_task_events_task_id_tasks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_task_events'))
    )
    op.create_index('ix_task_events_task_id_id', 'task_events', ['task_id', 'id'], unique=False)
    op.create_index('ix_task_events_type_ts', 'task_events', ['event_type', 'timestamp'], unique=False)
    op.create_table('task_steps',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=False),
    sa.Column('step_index', sa.Integer(), nullable=False),
    sa.Column('agent_id', sa.String(length=64), nullable=False),
    sa.Column('objective', sa.Text(), nullable=False),
    sa.Column('depends_on', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('checkpoint', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_task_steps_task_id_tasks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_task_steps')),
    sa.UniqueConstraint('task_id', 'step_index', name='uq_task_steps_task_step')
    )
    op.create_table('webhook_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('event_id', sa.String(length=200), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('signature_valid', sa.Boolean(), nullable=True),
    sa.Column('task_id', sa.String(length=32), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_webhook_events_task_id_tasks'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_events')),
    sa.UniqueConstraint('source', 'event_id', name='uq_webhook_events_source_event')
    )
    op.create_table('learning_candidates',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=True),
    sa.Column('feedback_id', sa.UUID(), nullable=True),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('agent_id', sa.String(length=64), nullable=True),
    sa.Column('original_feedback', sa.Text(), nullable=False),
    sa.Column('normalized_lesson', sa.Text(), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('domain_tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('requires_approval', sa.Boolean(), nullable=False),
    sa.Column('validation_notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('validated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('usage_count', sa.Integer(), nullable=False),
    sa.Column('success_count', sa.Integer(), nullable=False),
    sa.Column('failure_count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['feedback_id'], ['feedback.id'], name=op.f('fk_learning_candidates_feedback_id_feedback'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_learning_candidates_project_id_projects'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_learning_candidates_task_id_tasks'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_learning_candidates'))
    )
    op.create_index('ix_learning_candidates_status', 'learning_candidates', ['status'], unique=False)
    op.create_table('memory_embeddings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('memory_id', sa.UUID(), nullable=False),
    sa.Column('model', sa.String(length=200), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=EMBED_DIM), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['memory_id'], ['memories.id'], name=op.f('fk_memory_embeddings_memory_id_memories'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_memory_embeddings')),
    sa.UniqueConstraint('memory_id', 'model', name='uq_memory_embeddings_memory_model')
    )
    op.create_table('skills',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('domain_tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('steps', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('source_lesson_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('memory_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('usage_count', sa.Integer(), nullable=False),
    sa.Column('success_count', sa.Integer(), nullable=False),
    sa.Column('failure_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['memory_id'], ['memories.id'], name=op.f('fk_skills_memory_id_memories'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_skills')),
    sa.UniqueConstraint('name', name=op.f('uq_skills_name'))
    )
    op.create_table('tool_executions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=False),
    sa.Column('step_index', sa.Integer(), nullable=True),
    sa.Column('agent_id', sa.String(length=64), nullable=True),
    sa.Column('tool_id', sa.String(length=200), nullable=False),
    sa.Column('arguments', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('arguments_hash', sa.String(length=64), nullable=False),
    sa.Column('risk_level', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('approval_id', sa.UUID(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['approval_id'], ['approvals.id'], name=op.f('fk_tool_executions_approval_id_approvals'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_tool_executions_task_id_tasks'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tool_executions'))
    )
    op.create_index('ix_tool_exec_started', 'tool_executions', ['started_at'], unique=False)
    op.create_index('ix_tool_exec_task', 'tool_executions', ['task_id'], unique=False)
    op.create_table('lessons',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('candidate_id', sa.UUID(), nullable=True),
    sa.Column('memory_id', sa.UUID(), nullable=True),
    sa.Column('project_id', sa.UUID(), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('domain_tags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('usage_count', sa.Integer(), nullable=False),
    sa.Column('success_count', sa.Integer(), nullable=False),
    sa.Column('failure_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['candidate_id'], ['learning_candidates.id'], name=op.f('fk_lessons_candidate_id_learning_candidates'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['memory_id'], ['memories.id'], name=op.f('fk_lessons_memory_id_memories'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_lessons_project_id_projects'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lessons'))
    )
    # approximate-nearest-neighbour index for cosine similarity search (pgvector HNSW supports <= 2000 dims)
    if EMBED_DIM <= 2000:
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_memory_embeddings_hnsw ON memory_embeddings "
            "USING hnsw (embedding vector_cosine_ops)"
        )


def downgrade() -> None:
    op.drop_table('lessons')
    op.drop_index('ix_tool_exec_task', table_name='tool_executions')
    op.drop_index('ix_tool_exec_started', table_name='tool_executions')
    op.drop_table('tool_executions')
    op.drop_table('skills')
    op.drop_table('memory_embeddings')
    op.drop_index('ix_learning_candidates_status', table_name='learning_candidates')
    op.drop_table('learning_candidates')
    op.drop_table('webhook_events')
    op.drop_table('task_steps')
    op.drop_index('ix_task_events_type_ts', table_name='task_events')
    op.drop_index('ix_task_events_task_id_id', table_name='task_events')
    op.drop_table('task_events')
    op.drop_table('reports')
    op.drop_index('ix_memories_type_status', table_name='memories')
    op.drop_index('ix_memories_task', table_name='memories')
    op.drop_index('ix_memories_project', table_name='memories')
    op.drop_index('ix_memories_hash_scope', table_name='memories')
    op.drop_table('memories')
    op.drop_index('ix_feedback_task', table_name='feedback')
    op.drop_table('feedback')
    op.drop_index('ix_approvals_task', table_name='approvals')
    op.drop_index('ix_approvals_status', table_name='approvals')
    op.drop_table('approvals')
    op.drop_index('ix_agent_runs_task', table_name='agent_runs')
    op.drop_index('ix_agent_runs_started', table_name='agent_runs')
    op.drop_table('agent_runs')
    op.drop_index('ix_tasks_status_priority_created', table_name='tasks')
    op.drop_index('ix_tasks_session_key', table_name='tasks')
    op.drop_index('ix_tasks_project_created', table_name='tasks')
    op.drop_index('ix_tasks_parent', table_name='tasks')
    op.drop_table('tasks')
    op.drop_index('ix_scheduled_due', table_name='scheduled_tasks')
    op.drop_table('scheduled_tasks')
    op.drop_table('users')
    op.drop_table('tools')
    op.drop_index('ix_system_events_ts', table_name='system_events')
    op.drop_table('system_events')
    op.drop_table('sessions')
    op.drop_table('projects')
    op.drop_index('ix_audit_logs_ts', table_name='audit_logs')
    op.drop_table('audit_logs')
    op.drop_table('agents')
    op.execute("DROP SEQUENCE IF EXISTS task_id_seq")
