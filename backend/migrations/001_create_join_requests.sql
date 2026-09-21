-- Apply through the existing Supabase SQL editor/migration workflow.
-- The active-class service remains the source of truth for live sessions;
-- this table provides durable audit/history storage for join requests.
CREATE TABLE IF NOT EXISTS join_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id TEXT UNIQUE NOT NULL,
  room_code TEXT NOT NULL,
  student_name TEXT NOT NULL,
  session_id_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('WAITING', 'APPROVED', 'REJECTED', 'CANCELLED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS join_requests_room_status_idx
  ON join_requests (room_code, status, created_at DESC);
