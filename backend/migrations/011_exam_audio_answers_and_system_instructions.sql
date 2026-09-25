-- Extend Exams with a single descriptive question type, selected answer method,
-- private audio-answer storage, and optional teacher instructions. Existing
-- answer rows and recordings are preserved during legacy type conversion.

ALTER TABLE public.exams
  ADD COLUMN custom_instructions_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN custom_instructions TEXT;

UPDATE public.exams
SET custom_instructions_enabled = TRUE,
    custom_instructions = instructions
WHERE instructions IS NOT NULL AND length(btrim(instructions)) > 0;

ALTER TABLE public.exam_questions
  DROP CONSTRAINT exam_questions_question_type_check;

-- Migration-only compatibility rewrite: 010's guard protects teacher edits,
-- but must not block this non-destructive conversion for exams with attempts.
ALTER TABLE public.exam_questions DISABLE TRIGGER exam_questions_configuration_guard;
UPDATE public.exam_questions
SET question_type = 'TEXT_AUDIO_ANSWER'
WHERE question_type IN ('SHORT_ANSWER','LONG_ANSWER','AUDIO_ANSWER');
ALTER TABLE public.exam_questions ENABLE TRIGGER exam_questions_configuration_guard;

ALTER TABLE public.exam_questions
  ADD CONSTRAINT exam_questions_question_type_check
  CHECK (question_type IN ('MCQ','TEXT_AUDIO_ANSWER'));

ALTER TABLE public.exam_answers
  ADD COLUMN answer_method TEXT NOT NULL DEFAULT 'TEXT'
  CHECK (answer_method IN ('MCQ','TEXT','AUDIO','BOTH'));

ALTER TABLE public.exam_audio_answers ADD COLUMN transcript_language TEXT;

UPDATE public.exam_answers a SET answer_method='MCQ'
FROM public.exam_questions q
WHERE a.question_id=q.id AND q.question_type='MCQ';

UPDATE public.exam_answers a SET answer_method='AUDIO'
FROM public.exam_audio_answers aa
WHERE aa.attempt_id=a.attempt_id AND aa.question_id=a.question_id;

-- Legacy recordings are retained in the existing table; this bucket is private
-- and accessible only through the authenticated Exams API using service_role.
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('exam-audio-private','exam-audio-private',FALSE,15728640,
        ARRAY['audio/webm','audio/ogg','audio/mp4','audio/mpeg','audio/wav','audio/x-wav'])
ON CONFLICT (id) DO UPDATE SET public=FALSE, file_size_limit=15728640,
  allowed_mime_types=EXCLUDED.allowed_mime_types;

-- Keep the existing transactional question writer while adapting its accepted
-- type list to the new two-type model.
DO $$
DECLARE definition TEXT;
BEGIN
  SELECT pg_get_functiondef('public.save_exam_question_configuration(uuid,uuid,text,text,numeric,boolean,text,text,text,jsonb)'::regprocedure)
    INTO definition;
  IF position('p_question_type NOT IN (''MCQ'',''SHORT_ANSWER'',''LONG_ANSWER'',''AUDIO_ANSWER'')' IN definition)=0 THEN
    RAISE EXCEPTION 'Could not find the legacy question type check in save_exam_question_configuration';
  END IF;
  definition := replace(definition,
    'p_question_type NOT IN (''MCQ'',''SHORT_ANSWER'',''LONG_ANSWER'',''AUDIO_ANSWER'')',
    'p_question_type NOT IN (''MCQ'',''TEXT_AUDIO_ANSWER'')');
  definition := replace(definition,
    'IF p_question_type<>''MCQ'' AND v_option_count<>0 THEN',
    'IF p_question_type<>''MCQ'' AND v_option_count<>0 THEN');
  EXECUTE definition;
END $$;

-- The existing answer writer continues to enforce attempt ownership, timing,
-- question membership, and MCQ option integrity. Extend only its descriptive
-- type branch; answer method is written by the guarded RPC below.
DO $$
DECLARE definition TEXT;
BEGIN
  SELECT pg_get_functiondef('public.save_student_exam_answer(text,text,text,uuid,text,uuid)'::regprocedure)
    INTO definition;
  IF position('ELSIF v_question.question_type IN (''SHORT_ANSWER'',''LONG_ANSWER'') THEN' IN definition)=0 THEN
    RAISE EXCEPTION 'Could not find the legacy descriptive answer branch in save_student_exam_answer';
  END IF;
  definition := replace(definition,
    'ELSIF v_question.question_type IN (''SHORT_ANSWER'',''LONG_ANSWER'') THEN',
    'ELSIF v_question.question_type=''TEXT_AUDIO_ANSWER'' THEN');
  EXECUTE definition;
END $$;

CREATE OR REPLACE FUNCTION public.set_student_exam_answer_method(
  p_exam_token_hash TEXT, p_google_sub TEXT, p_attempt_token_hash TEXT,
  p_question_id UUID, p_answer_method TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_exam_id UUID; v_attempt public.exam_attempts%ROWTYPE; v_question public.exam_questions%ROWTYPE;
  v_now TIMESTAMPTZ; v_answer public.exam_answers%ROWTYPE;
BEGIN
  SELECT id INTO v_exam_id FROM public.exams WHERE public_token_hash=p_exam_token_hash;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  SELECT * INTO v_attempt FROM public.exam_attempts WHERE exam_id=v_exam_id AND google_sub=p_google_sub
    AND attempt_token_hash=p_attempt_token_hash FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','attempt_not_found'); END IF;
  v_now:=clock_timestamp();
  IF v_attempt.status<>'ACTIVE' THEN RETURN jsonb_build_object('error','attempt_submitted'); END IF;
  IF v_attempt.expires_at<=v_now THEN
    UPDATE public.exam_attempts SET status='AUTO_SUBMITTED',submitted_at=v_now,updated_at=v_now WHERE id=v_attempt.id;
    RETURN jsonb_build_object('error','attempt_expired');
  END IF;
  SELECT * INTO v_question FROM public.exam_questions WHERE id=p_question_id AND exam_id=v_exam_id;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','question_not_found'); END IF;
  IF v_question.question_type='MCQ' THEN
    IF p_answer_method<>'MCQ' THEN RETURN jsonb_build_object('error','invalid_answer_method'); END IF;
  ELSIF v_question.question_type='TEXT_AUDIO_ANSWER' THEN
    IF p_answer_method NOT IN ('TEXT','AUDIO','BOTH') THEN RETURN jsonb_build_object('error','invalid_answer_method'); END IF;
    IF p_answer_method IN ('AUDIO','BOTH') AND NOT EXISTS (
      SELECT 1 FROM public.exam_audio_answers WHERE attempt_id=v_attempt.id AND question_id=p_question_id
        AND transcription_status='READY'
    ) THEN RETURN jsonb_build_object('error','audio_answer_missing'); END IF;
    IF p_answer_method='BOTH' AND NOT EXISTS (
      SELECT 1 FROM public.exam_answers WHERE attempt_id=v_attempt.id AND question_id=p_question_id
        AND answer_text IS NOT NULL AND length(btrim(answer_text))>0
    ) THEN RETURN jsonb_build_object('error','text_answer_missing'); END IF;
  ELSE RETURN jsonb_build_object('error','unsupported_question'); END IF;
  INSERT INTO public.exam_answers (attempt_id,exam_id,question_id,answer_method,saved_at,created_at,updated_at)
    VALUES (v_attempt.id,v_exam_id,p_question_id,p_answer_method,v_now,v_now,v_now)
    ON CONFLICT (attempt_id,question_id) DO UPDATE SET answer_method=EXCLUDED.answer_method,
      saved_at=v_now,updated_at=v_now
    RETURNING * INTO v_answer;
  RETURN jsonb_build_object('question_id',v_answer.question_id,'answer_method',v_answer.answer_method);
END $$;

CREATE OR REPLACE FUNCTION public.save_student_exam_audio_answer(
  p_exam_token_hash TEXT, p_google_sub TEXT, p_attempt_token_hash TEXT,
  p_question_id UUID, p_storage_key TEXT, p_audio_mime_type TEXT,
  p_file_size_bytes BIGINT, p_duration_ms INTEGER, p_transcript TEXT,
  p_language_code TEXT, p_answer_method TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_exam_id UUID; v_attempt public.exam_attempts%ROWTYPE; v_question public.exam_questions%ROWTYPE;
  v_now TIMESTAMPTZ; v_answer public.exam_audio_answers%ROWTYPE; v_old_key TEXT;
BEGIN
  SELECT id INTO v_exam_id FROM public.exams WHERE public_token_hash=p_exam_token_hash;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  SELECT * INTO v_attempt FROM public.exam_attempts WHERE exam_id=v_exam_id AND google_sub=p_google_sub
    AND attempt_token_hash=p_attempt_token_hash FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','attempt_not_found'); END IF;
  v_now:=clock_timestamp();
  IF v_attempt.status<>'ACTIVE' THEN RETURN jsonb_build_object('error','attempt_submitted'); END IF;
  IF v_attempt.expires_at<=v_now THEN
    UPDATE public.exam_attempts SET status='AUTO_SUBMITTED',submitted_at=v_now,updated_at=v_now WHERE id=v_attempt.id;
    RETURN jsonb_build_object('error','attempt_expired');
  END IF;
  SELECT * INTO v_question FROM public.exam_questions WHERE id=p_question_id AND exam_id=v_exam_id;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','question_not_found'); END IF;
  IF v_question.question_type<>'TEXT_AUDIO_ANSWER' THEN RETURN jsonb_build_object('error','unsupported_question'); END IF;
  IF p_answer_method NOT IN ('AUDIO','BOTH') OR p_transcript IS NULL OR length(btrim(p_transcript))=0 THEN
    RETURN jsonb_build_object('error','transcription_failed');
  END IF;
  IF p_answer_method='BOTH' AND NOT EXISTS (
    SELECT 1 FROM public.exam_answers WHERE attempt_id=v_attempt.id AND question_id=p_question_id
      AND answer_text IS NOT NULL AND length(btrim(answer_text))>0
  ) THEN RETURN jsonb_build_object('error','text_answer_missing'); END IF;
  SELECT storage_key INTO v_old_key FROM public.exam_audio_answers
    WHERE attempt_id=v_attempt.id AND question_id=p_question_id FOR UPDATE;
  INSERT INTO public.exam_audio_answers (attempt_id,exam_id,question_id,storage_key,audio_mime_type,
      file_size_bytes,duration_ms,transcription_status,transcript,transcript_language,transcribed_at,uploaded_at)
    VALUES (v_attempt.id,v_exam_id,p_question_id,p_storage_key,p_audio_mime_type,p_file_size_bytes,
      p_duration_ms,'READY',p_transcript,p_language_code,v_now,v_now)
    ON CONFLICT (attempt_id,question_id) DO UPDATE SET storage_key=EXCLUDED.storage_key,
      audio_mime_type=EXCLUDED.audio_mime_type,file_size_bytes=EXCLUDED.file_size_bytes,
      duration_ms=EXCLUDED.duration_ms,transcription_status='READY',transcript=EXCLUDED.transcript,
      transcript_language=EXCLUDED.transcript_language,transcribed_at=v_now,uploaded_at=v_now,delete_after=NULL
    RETURNING * INTO v_answer;
  INSERT INTO public.exam_answers (attempt_id,exam_id,question_id,answer_method,saved_at,created_at,updated_at)
    VALUES (v_attempt.id,v_exam_id,p_question_id,p_answer_method,v_now,v_now,v_now)
    ON CONFLICT (attempt_id,question_id) DO UPDATE SET answer_method=EXCLUDED.answer_method,
      saved_at=v_now,updated_at=v_now;
  UPDATE public.exam_attempts SET last_activity_at=v_now,updated_at=v_now WHERE id=v_attempt.id;
  RETURN jsonb_build_object('old_storage_key',v_old_key,'question_id',v_answer.question_id,
    'answer_method',p_answer_method,'transcript',v_answer.transcript,'transcription_status','READY',
    'duration_ms',v_answer.duration_ms);
END $$;

CREATE OR REPLACE FUNCTION public.heartbeat_student_exam_attempt(
  p_exam_token_hash TEXT,p_google_sub TEXT,p_attempt_token_hash TEXT
) RETURNS JSONB LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_exam_id UUID; v_attempt public.exam_attempts%ROWTYPE; v_now TIMESTAMPTZ;
BEGIN
  SELECT id INTO v_exam_id FROM public.exams WHERE public_token_hash=p_exam_token_hash;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  SELECT * INTO v_attempt FROM public.exam_attempts WHERE exam_id=v_exam_id AND google_sub=p_google_sub
    AND attempt_token_hash=p_attempt_token_hash FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','attempt_not_found'); END IF;
  v_now:=clock_timestamp();
  IF v_attempt.status='ACTIVE' AND v_attempt.expires_at<=v_now THEN
    UPDATE public.exam_attempts SET status='AUTO_SUBMITTED',submitted_at=v_now,updated_at=v_now WHERE id=v_attempt.id RETURNING * INTO v_attempt;
  ELSIF v_attempt.status='ACTIVE' THEN
    UPDATE public.exam_attempts SET last_activity_at=v_now,updated_at=v_now WHERE id=v_attempt.id RETURNING * INTO v_attempt;
  END IF;
  RETURN jsonb_build_object('status',v_attempt.status,'server_time',v_now,
    'remaining_seconds',GREATEST(0,floor(extract(epoch FROM (v_attempt.expires_at-v_now)))::INTEGER),
    'violation_count',v_attempt.violation_count);
END $$;

CREATE OR REPLACE FUNCTION public.record_student_exam_violation(
  p_exam_token_hash TEXT,p_google_sub TEXT,p_attempt_token_hash TEXT,p_violation_type TEXT,p_metadata JSONB DEFAULT '{}'::JSONB
) RETURNS JSONB LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_exam_id UUID; v_exam public.exams%ROWTYPE; v_attempt public.exam_attempts%ROWTYPE;
  v_now TIMESTAMPTZ; v_count INTEGER; v_status TEXT;
BEGIN
  IF p_violation_type NOT IN ('FULLSCREEN_EXIT','TAB_HIDDEN','WINDOW_BLUR','ORIENTATION_CHANGE','NAVIGATION_ATTEMPT','MULTIPLE_SESSION','OTHER') THEN
    RETURN jsonb_build_object('error','invalid_violation_type');
  END IF;
  SELECT * INTO v_exam FROM public.exams WHERE public_token_hash=p_exam_token_hash;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  SELECT * INTO v_attempt FROM public.exam_attempts WHERE exam_id=v_exam.id AND google_sub=p_google_sub
    AND attempt_token_hash=p_attempt_token_hash FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','attempt_not_found'); END IF;
  v_now:=clock_timestamp();
  IF v_attempt.status<>'ACTIVE' THEN RETURN jsonb_build_object('error','attempt_submitted'); END IF;
  IF v_attempt.expires_at<=v_now THEN
    UPDATE public.exam_attempts SET status='AUTO_SUBMITTED',submitted_at=v_now,updated_at=v_now WHERE id=v_attempt.id RETURNING status,violation_count INTO v_status,v_count;
    RETURN jsonb_build_object('status',v_status,'violation_count',v_count,'terminated',TRUE);
  END IF;
  v_count:=v_attempt.violation_count+1;
  INSERT INTO public.exam_violations (attempt_id,violation_type,occurred_at,sequence_number,metadata)
    VALUES (v_attempt.id,p_violation_type,v_now,v_count,COALESCE(p_metadata,'{}'::JSONB));
  v_status:=CASE WHEN v_exam.maximum_violations>0 AND v_count>=v_exam.maximum_violations THEN 'TERMINATED' ELSE 'ACTIVE' END;
  UPDATE public.exam_attempts SET violation_count=v_count,status=v_status,
    submitted_at=CASE WHEN v_status='TERMINATED' THEN v_now ELSE submitted_at END,
    last_activity_at=v_now,updated_at=v_now WHERE id=v_attempt.id;
  RETURN jsonb_build_object('status',v_status,'violation_count',v_count,'maximum_violations',v_exam.maximum_violations,
    'terminated',v_status='TERMINATED');
END $$;

CREATE OR REPLACE FUNCTION public.delete_student_exam_audio_answer(
  p_exam_token_hash TEXT, p_google_sub TEXT, p_attempt_token_hash TEXT, p_question_id UUID
) RETURNS JSONB LANGUAGE plpgsql SECURITY INVOKER SET search_path=public,pg_temp AS $$
DECLARE v_exam_id UUID; v_attempt public.exam_attempts%ROWTYPE; v_key TEXT; v_now TIMESTAMPTZ;
BEGIN
  SELECT id INTO v_exam_id FROM public.exams WHERE public_token_hash=p_exam_token_hash;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  SELECT * INTO v_attempt FROM public.exam_attempts WHERE exam_id=v_exam_id AND google_sub=p_google_sub
    AND attempt_token_hash=p_attempt_token_hash FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','attempt_not_found'); END IF;
  v_now:=clock_timestamp();
  IF v_attempt.status<>'ACTIVE' OR v_attempt.expires_at<=v_now THEN RETURN jsonb_build_object('error','attempt_submitted'); END IF;
  DELETE FROM public.exam_audio_answers WHERE attempt_id=v_attempt.id AND question_id=p_question_id RETURNING storage_key INTO v_key;
  UPDATE public.exam_answers SET answer_method=CASE WHEN answer_text IS NOT NULL THEN 'TEXT' ELSE 'TEXT' END,
    updated_at=v_now WHERE attempt_id=v_attempt.id AND question_id=p_question_id AND answer_method IN ('AUDIO','BOTH');
  RETURN jsonb_build_object('storage_key',v_key,'deleted',v_key IS NOT NULL);
END $$;

REVOKE ALL ON FUNCTION public.set_student_exam_answer_method(TEXT,TEXT,TEXT,UUID,TEXT) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.save_student_exam_audio_answer(TEXT,TEXT,TEXT,UUID,TEXT,TEXT,BIGINT,INTEGER,TEXT,TEXT,TEXT) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.delete_student_exam_audio_answer(TEXT,TEXT,TEXT,UUID) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.heartbeat_student_exam_attempt(TEXT,TEXT,TEXT) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.record_student_exam_violation(TEXT,TEXT,TEXT,TEXT,JSONB) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.set_student_exam_answer_method(TEXT,TEXT,TEXT,UUID,TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.save_student_exam_audio_answer(TEXT,TEXT,TEXT,UUID,TEXT,TEXT,BIGINT,INTEGER,TEXT,TEXT,TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.delete_student_exam_audio_answer(TEXT,TEXT,TEXT,UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.heartbeat_student_exam_attempt(TEXT,TEXT,TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.record_student_exam_violation(TEXT,TEXT,TEXT,TEXT,JSONB) TO service_role;
