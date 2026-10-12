-- AI Workstation V1 schema (spec §23). Mirrors the SQLite store used by default.

CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS machines (id TEXT PRIMARY KEY, provider TEXT, provider_vm_id TEXT, name TEXT,
  status TEXT, ip TEXT, agent_endpoint TEXT, desktop_endpoint TEXT, created_at DOUBLE PRECISION, updated_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, user_id TEXT, prompt TEXT, machine_id TEXT, status TEXT,
  result TEXT, created_at DOUBLE PRECISION, updated_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, machine_id TEXT, task_id TEXT, status TEXT,
  started_at DOUBLE PRECISION, ended_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS machine_actions (id TEXT PRIMARY KEY, session_id TEXT, action_type TEXT, payload JSONB,
  result JSONB, screenshot TEXT, status TEXT, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS agent_events (id TEXT PRIMARY KEY, session_id TEXT, type TEXT, step INTEGER, data JSONB, ts DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, path TEXT, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS workspace_files (id TEXT PRIMARY KEY, session_id TEXT, path TEXT, size INTEGER, created_at DOUBLE PRECISION);
CREATE INDEX IF NOT EXISTS ix_events_session ON agent_events(session_id, ts);
CREATE INDEX IF NOT EXISTS ix_actions_session ON machine_actions(session_id, created_at);
