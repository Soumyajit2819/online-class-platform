-- Student attempt credentials and atomic, server-authoritative attempt operations.
ALTER TABLE public.exam_attempts
  ADD COLUMN attempt_token_hash TEXT UNIQUE
    CHECK (attempt_token_hash IS NULL OR attempt_token_hash ~ '^[0-9a-f]{64}$');

CREATE OR REPLACE FUNCTION public.start_student_exam_attempt(
  p_exam_token_hash TEXT,
  p_google_sub TEXT,
  p_student_display_name TEXT,
  p_attempt_token_hash TEXT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_exam public.exams%ROWTYPE; v_attempt public.exam_attempts%ROWTYPE; v_now TIMESTAMPTZ; v_created BOOLEAN := FALSE;
BEGIN
  SELECT * INTO v_exam FROM public.exams
    WHERE public_token_hash = p_exam_token_hash FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  v_now := clock_timestamp();
  IF EXISTS (SELECT 1 FROM public.exam_questions WHERE exam_id=v_exam.id AND question_type='AUDIO_ANSWER') THEN
    RETURN jsonb_build_object('error','audio_unsupported');
  END IF;

  SELECT * INTO v_attempt FROM public.exam_attempts
    WHERE exam_id = v_exam.id AND google_sub = p_google_sub FOR UPDATE;
  IF FOUND THEN
    v_now := clock_timestamp();
    IF v_attempt.status = 'ACTIVE' AND v_attempt.expires_at <= v_now THEN
      UPDATE public.exam_attempts SET status = 'AUTO_SUBMITTED', submitted_at = v_now,
        attempt_token_hash = p_attempt_token_hash, last_activity_at = v_now, updated_at = v_now
        WHERE id = v_attempt.id RETURNING * INTO v_attempt;
    ELSE
      UPDATE public.exam_attempts SET attempt_token_hash = p_attempt_token_hash,
        last_activity_at = CASE WHEN status = 'ACTIVE' THEN v_now ELSE last_activity_at END,
        updated_at = v_now WHERE id = v_attempt.id RETURNING * INTO v_attempt;
    END IF;
    RETURN jsonb_build_object('attempt',to_jsonb(v_attempt),'server_time',v_now,
      'remaining_seconds',GREATEST(0,floor(extract(epoch FROM (v_attempt.expires_at-v_now)))::INTEGER),
      'resumed',TRUE);
  END IF;

  IF v_exam.status NOT IN ('SCHEDULED','ACTIVE') THEN RETURN jsonb_build_object('error','exam_unavailable'); END IF;
  IF v_exam.scheduled_start_at IS NULL OR v_exam.scheduled_start_at > v_now THEN RETURN jsonb_build_object('error','not_started'); END IF;
  IF v_exam.scheduled_start_at + make_interval(mins => v_exam.duration_minutes) <= v_now THEN
    RETURN jsonb_build_object('error','exam_ended');
  END IF;
  IF v_exam.allow_list_enabled AND NOT EXISTS (
    SELECT 1 FROM public.exam_allowed_students WHERE exam_id=v_exam.id AND google_sub=p_google_sub
  ) THEN RETURN jsonb_build_object('error','not_eligible'); END IF;
  IF NOT EXISTS (SELECT 1 FROM public.exam_questions WHERE exam_id=v_exam.id)
     OR EXISTS (SELECT 1 FROM public.exam_questions WHERE exam_id=v_exam.id AND max_marks <= 0)
     OR EXISTS (
       SELECT 1 FROM public.exam_questions q
       WHERE q.exam_id=v_exam.id AND q.question_type='MCQ'
         AND ((SELECT count(*) FROM public.exam_question_options o WHERE o.question_id=q.id) < 2
              OR (SELECT count(*) FROM public.exam_question_options o WHERE o.question_id=q.id AND o.is_correct) <> 1)
     ) THEN
    RETURN jsonb_build_object('error','invalid_exam_configuration');
  END IF;
  v_now := clock_timestamp();
  IF v_exam.scheduled_start_at + make_interval(mins => v_exam.duration_minutes) <= v_now THEN
    RETURN jsonb_build_object('error','exam_ended');
  END IF;
  IF v_exam.status = 'SCHEDULED' THEN
    UPDATE public.exams SET status='ACTIVE',updated_at=v_now WHERE id=v_exam.id;
  END IF;
  INSERT INTO public.exam_attempts (exam_id,google_sub,student_display_name,status,started_at,
      expires_at,last_activity_at,attempt_token_hash)
    VALUES (v_exam.id,p_google_sub,p_student_display_name,'ACTIVE',v_now,
      v_exam.scheduled_start_at + make_interval(mins => v_exam.duration_minutes),v_now,p_attempt_token_hash)
    ON CONFLICT (exam_id,google_sub) DO NOTHING
    RETURNING * INTO v_attempt;
  v_created := FOUND;
  IF NOT FOUND THEN
    SELECT * INTO v_attempt FROM public.exam_attempts WHERE exam_id=v_exam.id AND google_sub=p_google_sub FOR UPDATE;
    UPDATE public.exam_attempts SET attempt_token_hash=p_attempt_token_hash,updated_at=v_now
      WHERE id=v_attempt.id RETURNING * INTO v_attempt;
  END IF;
  RETURN jsonb_build_object('attempt',to_jsonb(v_attempt),'server_time',v_now,
    'remaining_seconds',GREATEST(0,floor(extract(epoch FROM (v_attempt.expires_at-v_now)))::INTEGER),
    'resumed',NOT v_created);
END;
$$;

-- Teacher question/options/rubric writes stay transactional with a start lock.
CREATE OR REPLACE FUNCTION public.save_exam_question_configuration(
  p_exam_id UUID, p_question_id UUID, p_question_text TEXT, p_question_type TEXT,
  p_max_marks NUMERIC, p_is_required BOOLEAN, p_evaluation_mode TEXT,
  p_reference_answer TEXT, p_marking_criteria TEXT, p_options JSONB
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_exam public.exams%ROWTYPE; v_question public.exam_questions%ROWTYPE;
  v_number INTEGER; v_option_count INTEGER; v_correct_count INTEGER; v_options JSONB; v_rubric JSONB;
BEGIN
  SELECT * INTO v_exam FROM public.exams WHERE id=p_exam_id FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  IF v_exam.status NOT IN ('DRAFT','SCHEDULED') OR EXISTS (
    SELECT 1 FROM public.exam_attempts WHERE exam_id=p_exam_id
  ) THEN RETURN jsonb_build_object('error','configuration_locked'); END IF;
  IF p_question_text IS NULL OR length(btrim(p_question_text))=0 OR p_max_marks IS NULL OR p_max_marks<=0
     OR p_question_type IS NULL OR p_question_type NOT IN ('MCQ','SHORT_ANSWER','LONG_ANSWER','AUDIO_ANSWER')
     OR p_evaluation_mode IS NULL OR p_evaluation_mode NOT IN ('AI_ALLOWED','MANUAL_ONLY')
     OR p_options IS NULL OR jsonb_typeof(p_options)<>'array' THEN
    RETURN jsonb_build_object('error','invalid_question_config');
  END IF;
  SELECT count(*),count(*) FILTER (WHERE item->>'is_correct'='true')
    INTO v_option_count,v_correct_count FROM jsonb_array_elements(p_options) AS items(item);
  IF p_question_type='MCQ' AND (v_option_count<2 OR v_correct_count<>1 OR p_evaluation_mode<>'MANUAL_ONLY'
      OR EXISTS (SELECT 1 FROM jsonb_array_elements(p_options) AS items(item)
        WHERE length(btrim(COALESCE(item->>'text','')))=0 OR jsonb_typeof(item->'is_correct')<>'boolean')) THEN
    RETURN jsonb_build_object('error','invalid_question_config');
  END IF;
  IF p_question_type<>'MCQ' AND v_option_count<>0 THEN
    RETURN jsonb_build_object('error','invalid_question_config');
  END IF;
  IF p_question_id IS NULL THEN
    SELECT COALESCE(max(question_number),0)+1 INTO v_number FROM public.exam_questions WHERE exam_id=p_exam_id;
    INSERT INTO public.exam_questions (exam_id,question_number,question_text,question_type,max_marks,is_required,evaluation_mode)
      VALUES (p_exam_id,v_number,btrim(p_question_text),p_question_type,p_max_marks,p_is_required,p_evaluation_mode)
      RETURNING * INTO v_question;
  ELSE
    UPDATE public.exam_questions SET question_text=btrim(p_question_text),question_type=p_question_type,
      max_marks=p_max_marks,is_required=p_is_required,evaluation_mode=p_evaluation_mode,updated_at=now()
      WHERE id=p_question_id AND exam_id=p_exam_id RETURNING * INTO v_question;
    IF NOT FOUND THEN RETURN jsonb_build_object('error','question_not_found'); END IF;
    DELETE FROM public.exam_question_options WHERE question_id=p_question_id;
    DELETE FROM public.exam_question_rubrics WHERE question_id=p_question_id;
  END IF;
  IF v_option_count>0 THEN
    INSERT INTO public.exam_question_options (question_id,option_order,option_text,is_correct)
    SELECT v_question.id,ordinality::INTEGER,btrim(item->>'text'),(item->>'is_correct')::BOOLEAN
      FROM jsonb_array_elements(p_options) WITH ORDINALITY AS items(item,ordinality);
  END IF;
  IF p_reference_answer IS NOT NULL OR p_marking_criteria IS NOT NULL THEN
    INSERT INTO public.exam_question_rubrics (question_id,reference_answer,marking_criteria,max_marks)
      VALUES (v_question.id,p_reference_answer,p_marking_criteria,p_max_marks)
      RETURNING to_jsonb(exam_question_rubrics) INTO v_rubric;
  END IF;
  SELECT COALESCE(jsonb_agg(jsonb_build_object('id',id,'option_order',option_order,
      'option_text',option_text,'is_correct',is_correct) ORDER BY option_order),'[]'::JSONB)
    INTO v_options FROM public.exam_question_options WHERE question_id=v_question.id;
  RETURN to_jsonb(v_question) || jsonb_build_object('options',v_options,
      'reference_answer',v_rubric->'reference_answer','marking_criteria',v_rubric->'marking_criteria');
END;
$$;

CREATE OR REPLACE FUNCTION public.delete_exam_question_configuration(p_exam_id UUID,p_question_id UUID)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_exam public.exams%ROWTYPE; v_offset INTEGER;
BEGIN
  SELECT * INTO v_exam FROM public.exams WHERE id=p_exam_id FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  IF v_exam.status NOT IN ('DRAFT','SCHEDULED') OR EXISTS (SELECT 1 FROM public.exam_attempts WHERE exam_id=p_exam_id) THEN
    RETURN jsonb_build_object('error','configuration_locked');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.exam_questions WHERE id=p_question_id AND exam_id=p_exam_id) THEN
    RETURN jsonb_build_object('error','question_not_found');
  END IF;
  DELETE FROM public.exam_question_options WHERE question_id=p_question_id;
  DELETE FROM public.exam_question_rubrics WHERE question_id=p_question_id;
  DELETE FROM public.exam_questions WHERE id=p_question_id AND exam_id=p_exam_id;
  SELECT COALESCE(max(question_number),0)+count(*)+10 INTO v_offset
    FROM public.exam_questions WHERE exam_id=p_exam_id;
  UPDATE public.exam_questions SET question_number=question_number+v_offset WHERE exam_id=p_exam_id;
  WITH ordered AS (
    SELECT id,row_number() OVER (ORDER BY question_number)::INTEGER AS position
    FROM public.exam_questions WHERE exam_id=p_exam_id
  )
  UPDATE public.exam_questions q SET question_number=ordered.position,updated_at=now()
    FROM ordered WHERE q.id=ordered.id;
  RETURN jsonb_build_object('deleted',TRUE);
END;
$$;

CREATE OR REPLACE FUNCTION public.reorder_exam_questions(p_exam_id UUID,p_question_ids UUID[])
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_exam public.exams%ROWTYPE; v_count INTEGER; v_offset INTEGER;
BEGIN
  SELECT * INTO v_exam FROM public.exams WHERE id=p_exam_id FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  IF v_exam.status NOT IN ('DRAFT','SCHEDULED') OR EXISTS (SELECT 1 FROM public.exam_attempts WHERE exam_id=p_exam_id) THEN
    RETURN jsonb_build_object('error','configuration_locked');
  END IF;
  SELECT count(*) INTO v_count FROM public.exam_questions WHERE exam_id=p_exam_id;
  IF COALESCE(cardinality(p_question_ids),0)<>v_count OR
     (SELECT count(DISTINCT question_id) FROM unnest(p_question_ids) AS ids(question_id))<>v_count OR
     EXISTS (SELECT 1 FROM unnest(p_question_ids) AS ids(question_id) LEFT JOIN public.exam_questions q
       ON q.id=ids.question_id AND q.exam_id=p_exam_id WHERE q.id IS NULL) THEN
    RETURN jsonb_build_object('error','invalid_question_order');
  END IF;
  SELECT COALESCE(max(question_number),0)+cardinality(p_question_ids)+10 INTO v_offset
    FROM public.exam_questions WHERE exam_id=p_exam_id;
  WITH ordering AS (
    SELECT question_id,ordinality::INTEGER AS position
    FROM unnest(p_question_ids) WITH ORDINALITY AS items(question_id,ordinality)
  )
  UPDATE public.exam_questions q SET question_number=v_offset+ordering.position
    FROM ordering WHERE q.id=ordering.question_id AND q.exam_id=p_exam_id;
  WITH ordering AS (
    SELECT question_id,ordinality::INTEGER AS position
    FROM unnest(p_question_ids) WITH ORDINALITY AS items(question_id,ordinality)
  )
  UPDATE public.exam_questions q SET question_number=ordering.position,updated_at=now()
    FROM ordering WHERE q.id=ordering.question_id AND q.exam_id=p_exam_id;
  RETURN jsonb_build_object('question_ids',to_jsonb(p_question_ids));
END;
$$;

CREATE OR REPLACE FUNCTION public.get_student_exam_attempt(
  p_exam_token_hash TEXT, p_google_sub TEXT, p_attempt_token_hash TEXT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_exam_id UUID; v_attempt public.exam_attempts%ROWTYPE; v_now TIMESTAMPTZ;
BEGIN
  SELECT id INTO v_exam_id FROM public.exams WHERE public_token_hash=p_exam_token_hash;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  IF EXISTS (SELECT 1 FROM public.exam_questions WHERE exam_id=v_exam_id AND question_type='AUDIO_ANSWER') THEN
    RETURN jsonb_build_object('error','audio_unsupported');
  END IF;
  SELECT * INTO v_attempt FROM public.exam_attempts WHERE exam_id=v_exam_id
    AND google_sub=p_google_sub AND attempt_token_hash=p_attempt_token_hash FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','attempt_not_found'); END IF;
  v_now := clock_timestamp();
  IF v_attempt.status='ACTIVE' AND v_attempt.expires_at <= v_now THEN
    UPDATE public.exam_attempts SET status='AUTO_SUBMITTED',submitted_at=v_now,last_activity_at=v_now,updated_at=v_now
      WHERE id=v_attempt.id RETURNING * INTO v_attempt;
  ELSE
    UPDATE public.exam_attempts SET last_activity_at=v_now,updated_at=v_now
      WHERE id=v_attempt.id RETURNING * INTO v_attempt;
  END IF;
  RETURN jsonb_build_object('attempt',to_jsonb(v_attempt),'server_time',v_now,
    'remaining_seconds',GREATEST(0,floor(extract(epoch FROM (v_attempt.expires_at-v_now)))::INTEGER));
END;
$$;

CREATE OR REPLACE FUNCTION public.save_student_exam_answer(
  p_exam_token_hash TEXT, p_google_sub TEXT, p_attempt_token_hash TEXT,
  p_question_id UUID, p_answer_text TEXT, p_selected_option_id UUID
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_exam_id UUID; v_attempt public.exam_attempts%ROWTYPE; v_question public.exam_questions%ROWTYPE;
  v_answer public.exam_answers%ROWTYPE; v_now TIMESTAMPTZ;
BEGIN
  SELECT id INTO v_exam_id FROM public.exams WHERE public_token_hash=p_exam_token_hash;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  SELECT * INTO v_attempt FROM public.exam_attempts WHERE exam_id=v_exam_id AND google_sub=p_google_sub
    AND attempt_token_hash=p_attempt_token_hash FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','attempt_not_found'); END IF;
  v_now := clock_timestamp();
  IF v_attempt.status='ACTIVE' AND v_attempt.expires_at <= v_now THEN
    UPDATE public.exam_attempts SET status='AUTO_SUBMITTED',submitted_at=v_now,last_activity_at=v_now,updated_at=v_now WHERE id=v_attempt.id;
    RETURN jsonb_build_object('error','attempt_expired');
  END IF;
  IF v_attempt.status <> 'ACTIVE' THEN RETURN jsonb_build_object('error','attempt_submitted'); END IF;
  SELECT * INTO v_question FROM public.exam_questions WHERE id=p_question_id AND exam_id=v_exam_id;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','question_not_found'); END IF;
  IF v_question.question_type='MCQ' THEN
    IF p_answer_text IS NOT NULL OR p_selected_option_id IS NULL OR NOT EXISTS (
      SELECT 1 FROM public.exam_question_options WHERE id=p_selected_option_id AND question_id=p_question_id
    ) THEN RETURN jsonb_build_object('error','invalid_answer'); END IF;
  ELSIF v_question.question_type IN ('SHORT_ANSWER','LONG_ANSWER') THEN
    IF p_selected_option_id IS NOT NULL THEN RETURN jsonb_build_object('error','invalid_answer'); END IF;
  ELSE RETURN jsonb_build_object('error','unsupported_question');
  END IF;
  v_now := clock_timestamp();
  IF v_attempt.expires_at <= v_now THEN
    UPDATE public.exam_attempts SET status='AUTO_SUBMITTED',submitted_at=v_now,last_activity_at=v_now,updated_at=v_now WHERE id=v_attempt.id;
    RETURN jsonb_build_object('error','attempt_expired');
  END IF;
  INSERT INTO public.exam_answers (attempt_id,exam_id,question_id,answer_text,selected_option_id,answer_version,saved_at,created_at,updated_at)
    VALUES (v_attempt.id,v_exam_id,p_question_id,p_answer_text,p_selected_option_id,1,v_now,v_now,v_now)
    ON CONFLICT (attempt_id,question_id) DO UPDATE SET answer_text=EXCLUDED.answer_text,
      selected_option_id=EXCLUDED.selected_option_id,answer_version=exam_answers.answer_version+1,
      saved_at=v_now,updated_at=v_now
    RETURNING * INTO v_answer;
  UPDATE public.exam_attempts SET last_activity_at=v_now,updated_at=v_now WHERE id=v_attempt.id;
  RETURN jsonb_build_object('answer',jsonb_build_object('question_id',v_answer.question_id,
    'answer_text',v_answer.answer_text,'selected_option_id',v_answer.selected_option_id,
    'answer_version',v_answer.answer_version,'saved_at',v_answer.saved_at));
END;
$$;

CREATE OR REPLACE FUNCTION public.submit_student_exam_attempt(
  p_exam_token_hash TEXT, p_google_sub TEXT, p_attempt_token_hash TEXT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_exam_id UUID; v_attempt public.exam_attempts%ROWTYPE; v_now TIMESTAMPTZ;
BEGIN
  SELECT id INTO v_exam_id FROM public.exams WHERE public_token_hash=p_exam_token_hash;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  SELECT * INTO v_attempt FROM public.exam_attempts WHERE exam_id=v_exam_id AND google_sub=p_google_sub
    AND attempt_token_hash=p_attempt_token_hash FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','attempt_not_found'); END IF;
  v_now := clock_timestamp();
  IF v_attempt.status='ACTIVE' THEN
    UPDATE public.exam_attempts SET status=CASE WHEN expires_at <= v_now THEN 'AUTO_SUBMITTED' ELSE 'SUBMITTED' END,
      submitted_at=v_now,last_activity_at=v_now,updated_at=v_now WHERE id=v_attempt.id RETURNING * INTO v_attempt;
  END IF;
  RETURN jsonb_build_object('attempt',to_jsonb(v_attempt),'server_time',v_now,'remaining_seconds',0);
END;
$$;

-- Serialize student starts with management edits, and reject configuration
-- writes after the first attempt even if they race a teacher API pre-check.
CREATE OR REPLACE FUNCTION public.guard_exam_configuration_after_attempt()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_temp
AS $$
DECLARE v_exam_id UUID; v_question_id UUID;
BEGIN
  IF TG_TABLE_NAME = 'exams' THEN
    IF TG_OP='DELETE' THEN v_exam_id := OLD.id; ELSE v_exam_id := NEW.id; END IF;
    IF TG_OP='UPDATE' AND
       (to_jsonb(NEW)-'status'-'updated_at'-'finalized_at') IS NOT DISTINCT FROM
       (to_jsonb(OLD)-'status'-'updated_at'-'finalized_at') THEN
      RETURN NEW;
    END IF;
  ELSIF TG_TABLE_NAME = 'exam_allowed_students' THEN
    IF TG_OP='DELETE' THEN v_exam_id := OLD.exam_id; ELSE v_exam_id := NEW.exam_id; END IF;
  ELSE
    IF TG_TABLE_NAME='exam_questions' THEN
      IF TG_OP='DELETE' THEN v_exam_id := OLD.exam_id; ELSE v_exam_id := NEW.exam_id; END IF;
    ELSE
      IF TG_OP='DELETE' THEN v_question_id := OLD.question_id; ELSE v_question_id := NEW.question_id; END IF;
    END IF;
    IF v_question_id IS NOT NULL THEN
      SELECT exam_id INTO v_exam_id FROM public.exam_questions WHERE id=v_question_id;
    END IF;
  END IF;

  IF v_exam_id IS NULL THEN
    IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
  END IF;
  PERFORM id FROM public.exams WHERE id=v_exam_id FOR UPDATE;
  IF EXISTS (SELECT 1 FROM public.exam_attempts WHERE exam_id=v_exam_id) THEN
    RAISE EXCEPTION 'Exam configuration is locked after the first attempt'
      USING ERRCODE='55000';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END;
$$;

CREATE TRIGGER exams_configuration_guard
  BEFORE UPDATE OR DELETE ON public.exams
  FOR EACH ROW EXECUTE FUNCTION public.guard_exam_configuration_after_attempt();
CREATE TRIGGER exam_questions_configuration_guard
  BEFORE INSERT OR UPDATE OR DELETE ON public.exam_questions
  FOR EACH ROW EXECUTE FUNCTION public.guard_exam_configuration_after_attempt();
CREATE TRIGGER exam_question_options_configuration_guard
  BEFORE INSERT OR UPDATE OR DELETE ON public.exam_question_options
  FOR EACH ROW EXECUTE FUNCTION public.guard_exam_configuration_after_attempt();
CREATE TRIGGER exam_question_rubrics_configuration_guard
  BEFORE INSERT OR UPDATE OR DELETE ON public.exam_question_rubrics
  FOR EACH ROW EXECUTE FUNCTION public.guard_exam_configuration_after_attempt();
CREATE TRIGGER exam_allowed_students_configuration_guard
  BEFORE INSERT OR UPDATE OR DELETE ON public.exam_allowed_students
  FOR EACH ROW EXECUTE FUNCTION public.guard_exam_configuration_after_attempt();

REVOKE ALL ON FUNCTION public.start_student_exam_attempt(TEXT,TEXT,TEXT,TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.get_student_exam_attempt(TEXT,TEXT,TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.save_student_exam_answer(TEXT,TEXT,TEXT,UUID,TEXT,UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.submit_student_exam_attempt(TEXT,TEXT,TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.guard_exam_configuration_after_attempt() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.save_exam_question_configuration(UUID,UUID,TEXT,TEXT,NUMERIC,BOOLEAN,TEXT,TEXT,TEXT,JSONB) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.delete_exam_question_configuration(UUID,UUID) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.reorder_exam_questions(UUID,UUID[]) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.start_student_exam_attempt(TEXT,TEXT,TEXT,TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.get_student_exam_attempt(TEXT,TEXT,TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.save_student_exam_answer(TEXT,TEXT,TEXT,UUID,TEXT,UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.submit_student_exam_attempt(TEXT,TEXT,TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.guard_exam_configuration_after_attempt() TO service_role;
GRANT EXECUTE ON FUNCTION public.save_exam_question_configuration(UUID,UUID,TEXT,TEXT,NUMERIC,BOOLEAN,TEXT,TEXT,TEXT,JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.delete_exam_question_configuration(UUID,UUID) TO service_role;
GRANT EXECUTE ON FUNCTION public.reorder_exam_questions(UUID,UUID[]) TO service_role;
