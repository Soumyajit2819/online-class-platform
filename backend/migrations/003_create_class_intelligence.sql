-- Apply after 001_create_join_requests.sql and 002_create_recordings.sql.
-- Only classes created after this migration are persisted; no history is invented.
DO $$
BEGIN
  IF to_regclass('public.recordings') IS NULL THEN
    RAISE EXCEPTION 'Migration 003 requires public.recordings from migration 002';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'recordings' AND column_name = 'recording_id'
  ) THEN
    RAISE EXCEPTION 'Migration 003 requires recordings.recording_id from migration 002';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint c
    WHERE c.conrelid = 'public.recordings'::regclass
      AND c.contype IN ('p', 'u')
      AND cardinality(c.conkey) = 1
      AND c.conkey[1] = (
        SELECT a.attnum FROM pg_attribute a
        WHERE a.attrelid = 'public.recordings'::regclass AND a.attname = 'recording_id'
      )
  ) THEN
    RAISE EXCEPTION 'Migration 003 requires recordings.recording_id to be unique';
  END IF;
  IF to_regclass('public.classes') IS NOT NULL
     OR to_regclass('public.class_enrollments') IS NOT NULL
     OR to_regclass('public.class_meetings') IS NOT NULL
     OR to_regclass('public.meeting_notes') IS NOT NULL THEN
    RAISE EXCEPTION 'A Class Intelligence table name already exists; inspect the existing schema before applying migration 003';
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.classes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  room_code TEXT NOT NULL UNIQUE,
  class_name TEXT NOT NULL,
  teacher_identity TEXT NOT NULL,
  teacher_key_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
  UNIQUE (id, room_code)
);

CREATE TABLE IF NOT EXISTS public.class_enrollments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  class_id UUID NOT NULL REFERENCES public.classes(id) ON DELETE CASCADE,
  student_identity TEXT NOT NULL,
  student_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('authorized', 'revoked')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (class_id, student_identity)
);

CREATE TABLE IF NOT EXISTS public.class_meetings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  class_id UUID NOT NULL,
  room_code TEXT NOT NULL,
  -- The current app has no authoritative teacher-connected event on the backend.
  -- This records room creation time, not the exact time the teacher joined LiveKit.
  room_created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'ended', 'processing', 'ready', 'failed', 'unavailable')),
  recording_id TEXT REFERENCES public.recordings(recording_id) ON DELETE SET NULL,
  transcript_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (transcript_status IN ('pending', 'processing', 'ready', 'failed', 'unavailable')),
  transcript_error TEXT,
  FOREIGN KEY (class_id, room_code)
    REFERENCES public.classes(id, room_code) ON DELETE CASCADE,
  UNIQUE (room_code, room_created_at)
);

CREATE TABLE IF NOT EXISTS public.meeting_notes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  -- Preserve permanent notes if an administrator attempts to delete a meeting.
  meeting_id UUID NOT NULL UNIQUE REFERENCES public.class_meetings(id) ON DELETE RESTRICT,
  english_notes TEXT,
  bengali_notes TEXT,
  status TEXT NOT NULL DEFAULT 'processing'
    CHECK (status IN ('processing', 'ready', 'failed', 'unavailable')),
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS class_enrollments_student_status_idx
  ON public.class_enrollments (student_identity, status);
CREATE INDEX IF NOT EXISTS class_meetings_class_started_idx
  ON public.class_meetings (class_id, room_created_at DESC);
CREATE INDEX IF NOT EXISTS meeting_notes_status_idx
  ON public.meeting_notes (status, updated_at DESC);

ALTER TABLE public.classes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.class_enrollments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.class_meetings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.meeting_notes ENABLE ROW LEVEL SECURITY;

-- Browser clients never access these tables directly. FastAPI validates the
-- existing teacher identity/key or Google identity/enrollment, then uses the
-- backend-only service role. service_role bypasses RLS, so these grants/RLS do
-- not replace FastAPI authorization checks.
REVOKE ALL PRIVILEGES ON public.classes, public.class_enrollments,
  public.class_meetings, public.meeting_notes FROM PUBLIC, anon, authenticated;
GRANT ALL PRIVILEGES ON public.classes, public.class_enrollments,
  public.class_meetings, public.meeting_notes TO service_role;

-- Atomic creation: either both ownership and meeting records exist or neither does.
CREATE OR REPLACE FUNCTION public.create_class_with_meeting(
  p_room_code TEXT,
  p_class_name TEXT,
  p_teacher_identity TEXT,
  p_teacher_key_hash TEXT,
  p_room_created_at TIMESTAMPTZ
) RETURNS TABLE(created_class_id UUID, created_meeting_id UUID)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_class_id UUID;
  v_meeting_id UUID;
BEGIN
  INSERT INTO public.classes (room_code, class_name, teacher_identity, teacher_key_hash)
  VALUES (p_room_code, p_class_name, p_teacher_identity, p_teacher_key_hash)
  ON CONFLICT (room_code) DO NOTHING
  RETURNING id INTO v_class_id;

  IF v_class_id IS NULL THEN
    SELECT id INTO v_class_id FROM public.classes
    WHERE room_code = p_room_code AND class_name = p_class_name
      AND teacher_identity = p_teacher_identity AND teacher_key_hash = p_teacher_key_hash;
    IF v_class_id IS NULL THEN
      RAISE EXCEPTION 'Room code already belongs to a different class';
    END IF;

    SELECT id INTO v_meeting_id FROM public.class_meetings
    WHERE class_id = v_class_id AND room_code = p_room_code
      AND room_created_at = p_room_created_at;
    IF v_meeting_id IS NOT NULL THEN
      RETURN QUERY SELECT v_class_id, v_meeting_id;
      RETURN;
    END IF;
  END IF;

  INSERT INTO public.class_meetings (class_id, room_code, room_created_at)
  VALUES (v_class_id, p_room_code, p_room_created_at)
  ON CONFLICT (room_code, room_created_at) DO NOTHING
  RETURNING id INTO v_meeting_id;

  IF v_meeting_id IS NULL THEN
    SELECT id INTO v_meeting_id FROM public.class_meetings
    WHERE class_id = v_class_id AND room_code = p_room_code
      AND room_created_at = p_room_created_at;
  END IF;

  RETURN QUERY SELECT v_class_id, v_meeting_id;
END;
$$;

-- Idempotently record an end after the LiveKit teardown response has been sent.
CREATE OR REPLACE FUNCTION public.end_class_intelligence_meeting(
  p_room_code TEXT,
  p_teacher_identity TEXT,
  p_teacher_key_hash TEXT,
  p_recording_id TEXT DEFAULT NULL
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_class_id UUID;
  v_meeting_id UUID;
BEGIN
  SELECT id INTO v_class_id
  FROM public.classes
  WHERE room_code = p_room_code
    AND teacher_identity = p_teacher_identity
    AND teacher_key_hash = p_teacher_key_hash
  FOR UPDATE;

  IF v_class_id IS NULL THEN
    RAISE EXCEPTION 'Class ownership could not be verified';
  END IF;

  UPDATE public.classes SET status = 'ended' WHERE id = v_class_id;

  SELECT id INTO v_meeting_id
  FROM public.class_meetings
  WHERE class_id = v_class_id AND ended_at IS NULL
  ORDER BY room_created_at DESC
  LIMIT 1;

  IF v_meeting_id IS NULL THEN
    SELECT id INTO v_meeting_id FROM public.class_meetings
    WHERE class_id = v_class_id ORDER BY room_created_at DESC LIMIT 1;
    IF v_meeting_id IS NULL THEN
      RAISE EXCEPTION 'No meeting exists for this class';
    END IF;
    RETURN TRUE;
  END IF;

  UPDATE public.class_meetings
  SET ended_at = now(), status = 'ended',
      transcript_status = 'unavailable',
      transcript_error = 'No transcription provider is configured.',
      recording_id = COALESCE(p_recording_id, recording_id)
  WHERE id = v_meeting_id;

  INSERT INTO public.meeting_notes (meeting_id, status, error)
  VALUES (v_meeting_id, 'unavailable', 'No transcription and notes providers are configured.')
  ON CONFLICT (meeting_id) DO NOTHING;

  RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION public.create_class_with_meeting(TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_class_with_meeting(TEXT, TEXT, TEXT, TEXT, TIMESTAMPTZ)
  TO service_role;
REVOKE ALL ON FUNCTION public.end_class_intelligence_meeting(TEXT, TEXT, TEXT, TEXT)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.end_class_intelligence_meeting(TEXT, TEXT, TEXT, TEXT)
  TO service_role;
