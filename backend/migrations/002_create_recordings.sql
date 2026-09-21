-- Durable, recording-level metadata for private HLS recordings.
CREATE TABLE IF NOT EXISTS recordings (
  recording_id TEXT PRIMARY KEY,
  room_code TEXT NOT NULL,
  class_name TEXT NOT NULL,
  teacher_name TEXT NOT NULL,
  storage_prefix TEXT NOT NULL UNIQUE,
  playlist_key TEXT NOT NULL,
  egress_id TEXT UNIQUE,
  started_at TIMESTAMPTZ NOT NULL,
  ended_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('starting', 'recording', 'processing', 'available', 'failed'))
);

CREATE INDEX IF NOT EXISTS recordings_expiry_idx ON recordings (expires_at);
CREATE INDEX IF NOT EXISTS recordings_room_started_idx ON recordings (room_code, started_at DESC);
