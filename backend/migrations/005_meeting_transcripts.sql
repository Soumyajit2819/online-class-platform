-- Permanent, meeting-scoped transcripts and durable transcription work state.
-- Apply after 004_recording_finalization.sql.

CREATE TABLE IF NOT EXISTS public.meeting_transcripts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  meeting_id UUID NOT NULL UNIQUE
    REFERENCES public.class_meetings(id) ON DELETE RESTRICT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'ready', 'failed', 'unavailable')),
  transcript_text TEXT,
  language TEXT,
  provider TEXT,
  model TEXT,
  segments JSONB,
  source_recording_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  error_code TEXT,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 8 CHECK (max_attempts > 0),
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_until TIMESTAMPTZ,
  lease_token UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  CHECK ((status = 'ready') = (transcript_text IS NOT NULL AND length(transcript_text) > 0))
);

CREATE INDEX IF NOT EXISTS meeting_transcripts_due_idx
  ON public.meeting_transcripts (next_attempt_at, lease_until)
  WHERE status IN ('pending', 'processing');

ALTER TABLE public.meeting_transcripts ENABLE ROW LEVEL SECURITY;
REVOKE ALL PRIVILEGES ON public.meeting_transcripts FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE ON public.meeting_transcripts TO service_role;

-- A meeting is eligible only after it ended and every associated recording
-- has passed Phase 2A finalization. One transcript row covers all sessions.
CREATE OR REPLACE FUNCTION public.enqueue_ready_meeting_transcripts(
  p_provider_ready BOOLEAN DEFAULT FALSE,
  p_limit INTEGER DEFAULT 50
) RETURNS INTEGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_changed INTEGER;
BEGIN
  WITH eligible AS (
    SELECT m.id AS meeting_id
    FROM public.class_meetings m
    WHERE m.ended_at IS NOT NULL
      AND EXISTS (
        SELECT 1 FROM public.meeting_recordings mr
        WHERE mr.class_meeting_id = m.id
      )
      AND NOT EXISTS (
        SELECT 1
        FROM public.meeting_recordings mr
        JOIN public.recordings r ON r.recording_id = mr.recording_id
        LEFT JOIN public.recording_processing_jobs j ON j.recording_id = r.recording_id
        WHERE mr.class_meeting_id = m.id
          AND (r.status <> 'available' OR j.recording_id IS NULL)
      )
    ORDER BY m.ended_at
    LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 50), 100))
  ), upserted AS (
    INSERT INTO public.meeting_transcripts (meeting_id, status, error_code, error)
    SELECT meeting_id,
           CASE WHEN p_provider_ready THEN 'pending' ELSE 'unavailable' END,
           CASE WHEN p_provider_ready THEN NULL ELSE 'provider_unconfigured' END,
           CASE WHEN p_provider_ready THEN NULL ELSE 'No registered speech-to-text provider is configured.' END
    FROM eligible
    ON CONFLICT (meeting_id) DO UPDATE SET
      status = 'pending', error_code = NULL, error = NULL,
      attempts = 0, next_attempt_at = now(), lease_until = NULL,
      lease_token = NULL, updated_at = now()
    WHERE p_provider_ready
      AND meeting_transcripts.status = 'unavailable'
      AND meeting_transcripts.error_code = 'provider_unconfigured'
    RETURNING meeting_id, status
  )
  UPDATE public.class_meetings m
  SET transcript_status = u.status, transcript_error = NULL
  FROM upserted u
  WHERE m.id = u.meeting_id;

  GET DIAGNOSTICS v_changed = ROW_COUNT;
  RETURN v_changed;
END;
$$;

-- Terminally failed recordings cannot yield a complete whole-class transcript.
CREATE OR REPLACE FUNCTION public.mark_untranscribable_meetings(p_limit INTEGER DEFAULT 50)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_changed INTEGER;
BEGIN
  WITH affected AS (
    SELECT DISTINCT m.id AS meeting_id
    FROM public.class_meetings m
    JOIN public.meeting_recordings mr ON mr.class_meeting_id = m.id
    JOIN public.recordings r ON r.recording_id = mr.recording_id
    WHERE m.ended_at IS NOT NULL AND r.status = 'failed'
    ORDER BY m.id
    LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 50), 100))
  ), upserted AS (
    INSERT INTO public.meeting_transcripts (meeting_id, status, error_code, error)
    SELECT meeting_id, 'unavailable', 'recording_failed',
           'At least one class recording session did not finalize successfully.'
    FROM affected
    ON CONFLICT (meeting_id) DO UPDATE SET
      status = 'unavailable', error_code = 'recording_failed',
      error = 'At least one class recording session did not finalize successfully.',
      lease_until = NULL, lease_token = NULL, updated_at = now()
    WHERE meeting_transcripts.status NOT IN ('ready', 'processing', 'pending')
    RETURNING meeting_id, status, error
  )
  UPDATE public.class_meetings m
  SET transcript_status = u.status, transcript_error = u.error
  FROM upserted u
  WHERE m.id = u.meeting_id;

  GET DIAGNOSTICS v_changed = ROW_COUNT;
  RETURN v_changed;
END;
$$;

CREATE OR REPLACE FUNCTION public.claim_meeting_transcript_jobs(
  p_batch_size INTEGER DEFAULT 5,
  p_lease_seconds INTEGER DEFAULT 600
) RETURNS SETOF public.meeting_transcripts
LANGUAGE sql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
  WITH stale_exhausted AS (
    UPDATE public.meeting_transcripts
    SET status = 'failed', error_code = 'retry_exhausted',
        error = 'Speech-to-text retries were exhausted after a worker lease expired.',
        lease_until = NULL, lease_token = NULL, updated_at = now()
    WHERE status = 'processing'
      AND (lease_until IS NULL OR lease_until < now())
      AND attempts >= max_attempts
    RETURNING meeting_id
  ), sync_exhausted AS (
    UPDATE public.class_meetings m
    SET transcript_status = 'failed',
        transcript_error = 'Speech-to-text retries were exhausted after a worker lease expired.'
    FROM stale_exhausted s
    WHERE m.id = s.meeting_id
    RETURNING m.id
  ), candidates AS (
    SELECT id
    FROM public.meeting_transcripts
    WHERE next_attempt_at <= now()
      AND ((status = 'pending' AND attempts < max_attempts)
        OR (status = 'processing' AND attempts < max_attempts
            AND (lease_until IS NULL OR lease_until < now())))
    ORDER BY next_attempt_at, created_at
    LIMIT GREATEST(1, LEAST(COALESCE(p_batch_size, 5), 20))
    FOR UPDATE SKIP LOCKED
  )
  UPDATE public.meeting_transcripts t
  SET status = 'processing', attempts = attempts + 1,
      lease_until = now() + make_interval(secs => GREATEST(60, LEAST(COALESCE(p_lease_seconds, 600), 3600))),
      lease_token = gen_random_uuid(), updated_at = now()
  FROM candidates
  WHERE t.id = candidates.id
  RETURNING t.*;
$$;

CREATE OR REPLACE FUNCTION public.renew_meeting_transcript_lease(
  p_id UUID, p_lease_token UUID, p_lease_seconds INTEGER DEFAULT 600
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_updated INTEGER;
BEGIN
  UPDATE public.meeting_transcripts
  SET lease_until = now() + make_interval(secs => GREATEST(60, LEAST(COALESCE(p_lease_seconds, 600), 3600))),
      updated_at = now()
  WHERE id = p_id AND status = 'processing' AND lease_token = p_lease_token;
  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated = 1;
END;
$$;

REVOKE ALL ON FUNCTION public.enqueue_ready_meeting_transcripts(BOOLEAN, INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.enqueue_ready_meeting_transcripts(BOOLEAN, INTEGER) TO service_role;
REVOKE ALL ON FUNCTION public.mark_untranscribable_meetings(INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.mark_untranscribable_meetings(INTEGER) TO service_role;
REVOKE ALL ON FUNCTION public.claim_meeting_transcript_jobs(INTEGER, INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_meeting_transcript_jobs(INTEGER, INTEGER) TO service_role;
REVOKE ALL ON FUNCTION public.renew_meeting_transcript_lease(UUID, UUID, INTEGER)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.renew_meeting_transcript_lease(UUID, UUID, INTEGER) TO service_role;
