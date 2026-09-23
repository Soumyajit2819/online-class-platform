-- Durable AI notes jobs and retrieval metadata. Apply after 005_meeting_transcripts.sql.
-- Extend the meeting_notes table created by migration 003; do not replace it.

ALTER TABLE public.meeting_notes
  ADD COLUMN IF NOT EXISTS provider TEXT,
  ADD COLUMN IF NOT EXISTS model TEXT,
  ADD COLUMN IF NOT EXISTS source_transcript_id UUID,
  ADD COLUMN IF NOT EXISTS error_code TEXT,
  ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 8,
  ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS lease_token UUID,
  ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

ALTER TABLE public.class_meetings
  ADD COLUMN IF NOT EXISTS notes_status TEXT NOT NULL DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS notes_error TEXT;

ALTER TABLE public.class_meetings
  DROP CONSTRAINT IF EXISTS class_meetings_notes_status_check,
  ADD CONSTRAINT class_meetings_notes_status_check
    CHECK (notes_status IN ('pending', 'processing', 'ready', 'failed', 'unavailable'));

ALTER TABLE public.meeting_notes
  DROP CONSTRAINT IF EXISTS meeting_notes_status_check,
  DROP CONSTRAINT IF EXISTS meeting_notes_attempts_check,
  DROP CONSTRAINT IF EXISTS meeting_notes_max_attempts_check,
  DROP CONSTRAINT IF EXISTS meeting_notes_ready_content_check;

ALTER TABLE public.meeting_notes
  ALTER COLUMN status SET DEFAULT 'pending',
  ADD CONSTRAINT meeting_notes_status_check
    CHECK (status IN ('pending', 'processing', 'ready', 'failed', 'unavailable')),
  ADD CONSTRAINT meeting_notes_attempts_check CHECK (attempts >= 0),
  ADD CONSTRAINT meeting_notes_max_attempts_check CHECK (max_attempts > 0),
  ADD CONSTRAINT meeting_notes_ready_content_check
    CHECK (status <> 'ready' OR
      (english_notes IS NOT NULL AND length(english_notes) > 0
       AND bengali_notes IS NOT NULL AND length(bengali_notes) > 0));

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid = 'public.meeting_notes'::regclass
      AND conname = 'meeting_notes_source_transcript_id_fkey'
  ) THEN
    ALTER TABLE public.meeting_notes
      ADD CONSTRAINT meeting_notes_source_transcript_id_fkey
      FOREIGN KEY (source_transcript_id)
      REFERENCES public.meeting_transcripts(id) ON DELETE SET NULL;
  END IF;
END $$;

-- Existing Phase 1 rows were placeholders created before transcript jobs existed.
UPDATE public.meeting_notes
SET error_code = 'provider_unconfigured'
WHERE status = 'unavailable' AND error_code IS NULL;

UPDATE public.meeting_notes n
SET source_transcript_id = t.id
FROM public.meeting_transcripts t
WHERE t.meeting_id = n.meeting_id AND n.source_transcript_id IS NULL;

UPDATE public.class_meetings m
SET notes_status = n.status,
    notes_error = n.error
FROM public.meeting_notes n
WHERE n.meeting_id = m.id;

-- At class end, notes are pending until a ready transcript is available. The
-- notes scheduler marks them provider_unconfigured only once eligible.
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
DECLARE v_class_id UUID; v_meeting_id UUID;
BEGIN
  SELECT id INTO v_class_id
  FROM public.classes
  WHERE room_code = p_room_code
    AND teacher_identity = p_teacher_identity
    AND teacher_key_hash = p_teacher_key_hash
  FOR UPDATE;
  IF v_class_id IS NULL THEN RAISE EXCEPTION 'Class ownership could not be verified'; END IF;

  UPDATE public.classes SET status = 'ended' WHERE id = v_class_id;
  SELECT id INTO v_meeting_id
  FROM public.class_meetings
  WHERE class_id = v_class_id AND ended_at IS NULL
  ORDER BY room_created_at DESC LIMIT 1;
  IF v_meeting_id IS NULL THEN
    SELECT id INTO v_meeting_id FROM public.class_meetings
    WHERE class_id = v_class_id ORDER BY room_created_at DESC LIMIT 1;
    IF v_meeting_id IS NULL THEN RAISE EXCEPTION 'No meeting exists for this class'; END IF;
    RETURN TRUE;
  END IF;

  UPDATE public.class_meetings
  SET ended_at = now(), status = 'ended',
      transcript_status = 'unavailable',
      transcript_error = 'No transcription provider is configured.',
      notes_status = 'pending', notes_error = NULL,
      recording_id = COALESCE(p_recording_id, recording_id)
  WHERE id = v_meeting_id;

  INSERT INTO public.meeting_notes (meeting_id, status, error, error_code)
  VALUES (v_meeting_id, 'pending', NULL, NULL)
  ON CONFLICT (meeting_id) DO NOTHING;
  RETURN TRUE;
END;
$$;

CREATE INDEX IF NOT EXISTS meeting_notes_due_idx
  ON public.meeting_notes (next_attempt_at, lease_until)
  WHERE status IN ('pending', 'processing');

ALTER TABLE public.meeting_notes ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON public.meeting_notes FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.meeting_notes TO service_role;

CREATE OR REPLACE FUNCTION public.enqueue_ready_meeting_notes(
  p_provider_ready BOOLEAN DEFAULT FALSE,
  p_limit INTEGER DEFAULT 50
) RETURNS INTEGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_changed INTEGER;
BEGIN
  WITH eligible AS (
    SELECT m.id AS meeting_id, t.id AS transcript_id
    FROM public.class_meetings m
    JOIN public.meeting_transcripts t ON t.meeting_id = m.id
    WHERE m.ended_at IS NOT NULL
      AND t.status = 'ready'
      AND t.transcript_text IS NOT NULL
      AND length(btrim(t.transcript_text)) > 0
    ORDER BY m.ended_at
    LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 50), 100))
  ), upserted AS (
    INSERT INTO public.meeting_notes (
      meeting_id, source_transcript_id, status, error_code, error
    )
    SELECT meeting_id, transcript_id,
      CASE WHEN p_provider_ready THEN 'pending' ELSE 'unavailable' END,
      CASE WHEN p_provider_ready THEN NULL ELSE 'provider_unconfigured' END,
      CASE WHEN p_provider_ready THEN NULL ELSE 'No notes provider is configured.' END
    FROM eligible
    ON CONFLICT (meeting_id) DO UPDATE SET
      source_transcript_id = EXCLUDED.source_transcript_id,
      status = CASE WHEN p_provider_ready THEN 'pending' ELSE 'unavailable' END,
      error_code = CASE WHEN p_provider_ready THEN NULL ELSE 'provider_unconfigured' END,
      error = CASE WHEN p_provider_ready THEN NULL ELSE 'No notes provider is configured.' END,
      lease_until = NULL, lease_token = NULL, next_attempt_at = now(), updated_at = now()
    WHERE (p_provider_ready
           AND meeting_notes.status = 'unavailable'
           AND meeting_notes.error_code = 'provider_unconfigured')
       OR (NOT p_provider_ready
           AND meeting_notes.status = 'pending'
           AND meeting_notes.lease_token IS NULL)
    RETURNING meeting_id, status, error
  )
  UPDATE public.class_meetings m
  SET notes_status = u.status, notes_error = u.error
  FROM upserted u
  WHERE m.id = u.meeting_id;

  GET DIAGNOSTICS v_changed = ROW_COUNT;
  RETURN v_changed;
END;
$$;

CREATE OR REPLACE FUNCTION public.claim_meeting_notes_jobs(
  p_batch_size INTEGER DEFAULT 3,
  p_lease_seconds INTEGER DEFAULT 600
) RETURNS SETOF public.meeting_notes
LANGUAGE sql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
  WITH stale_exhausted AS (
    UPDATE public.meeting_notes
    SET status = 'failed', error_code = 'retry_exhausted',
        error = 'Notes generation retries were exhausted after a worker lease expired.',
        lease_until = NULL, lease_token = NULL, updated_at = now()
    WHERE attempts >= max_attempts
      AND (status = 'pending' OR
           (status = 'processing' AND (lease_until IS NULL OR lease_until < now())))
    RETURNING meeting_id
  ), sync_exhausted AS (
    UPDATE public.class_meetings m
    SET notes_status = 'failed',
        notes_error = 'Notes generation retries were exhausted after a worker lease expired.'
    FROM stale_exhausted s
    WHERE m.id = s.meeting_id
    RETURNING m.id
  ), candidates AS (
    SELECT id FROM public.meeting_notes
    WHERE next_attempt_at <= now() AND attempts < max_attempts
      AND (status = 'pending' OR
           (status = 'processing' AND (lease_until IS NULL OR lease_until < now())))
    ORDER BY next_attempt_at, created_at
    LIMIT GREATEST(1, LEAST(COALESCE(p_batch_size, 3), 20))
    FOR UPDATE SKIP LOCKED
  ), claimed AS (
    UPDATE public.meeting_notes n
    SET status = 'processing', attempts = attempts + 1,
        lease_until = now() + make_interval(secs => GREATEST(60, LEAST(COALESCE(p_lease_seconds, 600), 3600))),
        lease_token = gen_random_uuid(), updated_at = now()
    FROM candidates
    WHERE n.id = candidates.id
    RETURNING n.*
  ), synchronized AS (
    UPDATE public.class_meetings m
    SET notes_status = 'processing', notes_error = NULL
    FROM claimed c
    WHERE m.id = c.meeting_id
    RETURNING c.*
  )
  SELECT * FROM synchronized;
$$;

CREATE OR REPLACE FUNCTION public.renew_meeting_notes_lease(
  p_id UUID, p_lease_token UUID, p_lease_seconds INTEGER DEFAULT 600
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_updated INTEGER;
BEGIN
  UPDATE public.meeting_notes
  SET lease_until = now() + make_interval(secs => GREATEST(60, LEAST(COALESCE(p_lease_seconds, 600), 3600))),
      updated_at = now()
  WHERE id = p_id AND status = 'processing' AND lease_token = p_lease_token;
  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated = 1;
END;
$$;

CREATE OR REPLACE FUNCTION public.finish_meeting_notes_job(
  p_id UUID,
  p_lease_token UUID,
  p_status TEXT,
  p_english_notes TEXT DEFAULT NULL,
  p_bengali_notes TEXT DEFAULT NULL,
  p_provider TEXT DEFAULT NULL,
  p_model TEXT DEFAULT NULL,
  p_source_transcript_id UUID DEFAULT NULL,
  p_error_code TEXT DEFAULT NULL,
  p_error TEXT DEFAULT NULL,
  p_next_attempt_at TIMESTAMPTZ DEFAULT now(),
  p_completed_at TIMESTAMPTZ DEFAULT NULL
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_meeting_id UUID; v_updated INTEGER;
BEGIN
  IF p_status NOT IN ('pending', 'ready', 'failed', 'unavailable') THEN
    RAISE EXCEPTION 'Invalid notes job state';
  END IF;
  UPDATE public.meeting_notes
  SET status = p_status,
      english_notes = COALESCE(p_english_notes, english_notes),
      bengali_notes = COALESCE(p_bengali_notes, bengali_notes),
      provider = COALESCE(p_provider, provider),
      model = COALESCE(p_model, model),
      source_transcript_id = COALESCE(p_source_transcript_id, source_transcript_id),
      error_code = p_error_code, error = p_error,
      next_attempt_at = COALESCE(p_next_attempt_at, now()),
      completed_at = p_completed_at,
      lease_until = NULL, lease_token = NULL, updated_at = now()
  WHERE id = p_id AND status = 'processing' AND lease_token = p_lease_token
  RETURNING meeting_id INTO v_meeting_id;
  GET DIAGNOSTICS v_updated = ROW_COUNT;
  IF v_updated = 0 THEN RETURN FALSE; END IF;

  UPDATE public.class_meetings
  SET notes_status = p_status, notes_error = p_error
  WHERE id = v_meeting_id;
  RETURN TRUE;
END;
$$;

REVOKE ALL ON FUNCTION public.enqueue_ready_meeting_notes(BOOLEAN, INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.enqueue_ready_meeting_notes(BOOLEAN, INTEGER) TO service_role;
REVOKE ALL ON FUNCTION public.claim_meeting_notes_jobs(INTEGER, INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_meeting_notes_jobs(INTEGER, INTEGER) TO service_role;
REVOKE ALL ON FUNCTION public.renew_meeting_notes_lease(UUID, UUID, INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.renew_meeting_notes_lease(UUID, UUID, INTEGER) TO service_role;
REVOKE ALL ON FUNCTION public.finish_meeting_notes_job(UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, UUID, TEXT, TEXT, TIMESTAMPTZ, TIMESTAMPTZ)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.finish_meeting_notes_job(UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, UUID, TEXT, TEXT, TIMESTAMPTZ, TIMESTAMPTZ)
  TO service_role;
REVOKE ALL ON FUNCTION public.end_class_intelligence_meeting(TEXT, TEXT, TEXT, TEXT)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.end_class_intelligence_meeting(TEXT, TEXT, TEXT, TEXT)
  TO service_role;
