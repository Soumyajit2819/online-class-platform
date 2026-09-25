-- Atomically remove one exam's relational data graph. Existing exam foreign
-- keys use ON DELETE RESTRICT, so deletion is explicit and ordered here.
CREATE OR REPLACE FUNCTION public.delete_exam_and_dependents(p_exam_id UUID)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE
  v_exam_id UUID;
  v_audio_storage_keys TEXT[];
  v_artifact_storage_keys TEXT[];
  v_attempt_ids UUID[];
  v_result_ids UUID[];
  v_artifact_ids UUID[];
  v_question_ids UUID[];
  v_answer_ids UUID[];
BEGIN
  SELECT id INTO v_exam_id FROM public.exams WHERE id=p_exam_id FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;

  -- Migration 012 protects finalized result/evaluation rows from mutation.
  -- This parent state change is inside the same transaction and only permits
  -- their deletion as part of this explicit whole-exam deletion.
  UPDATE public.exams SET status='CANCELLED' WHERE id=p_exam_id AND status='FINALIZED';

  SELECT COALESCE(array_agg(id),ARRAY[]::UUID[]) INTO v_attempt_ids
    FROM public.exam_attempts WHERE exam_id=p_exam_id;
  SELECT COALESCE(array_agg(id),ARRAY[]::UUID[]) INTO v_result_ids
    FROM public.exam_results WHERE exam_id=p_exam_id;
  SELECT COALESCE(array_agg(id),ARRAY[]::UUID[]) INTO v_artifact_ids
    FROM public.exam_artifacts WHERE exam_id=p_exam_id;
  SELECT COALESCE(array_agg(id),ARRAY[]::UUID[]) INTO v_question_ids
    FROM public.exam_questions WHERE exam_id=p_exam_id;
  SELECT COALESCE(array_agg(id),ARRAY[]::UUID[]) INTO v_answer_ids
    FROM public.exam_answers WHERE exam_id=p_exam_id;
  SELECT COALESCE(array_agg(storage_key),ARRAY[]::TEXT[]) INTO v_audio_storage_keys
    FROM public.exam_audio_answers WHERE exam_id=p_exam_id;
  SELECT COALESCE(array_agg(storage_key),ARRAY[]::TEXT[]) INTO v_artifact_storage_keys
    FROM public.exam_artifacts WHERE exam_id=p_exam_id;

  -- Dependents first, following the current RESTRICT foreign keys.
  DELETE FROM public.exam_notification_jobs
    WHERE result_id=ANY(v_result_ids) OR artifact_id=ANY(v_artifact_ids);
  DELETE FROM public.exam_artifacts WHERE exam_id=p_exam_id;
  DELETE FROM public.exam_results WHERE exam_id=p_exam_id;
  DELETE FROM public.exam_ai_evaluations WHERE answer_id=ANY(v_answer_ids);
  DELETE FROM public.exam_manual_reviews WHERE answer_id=ANY(v_answer_ids);
  DELETE FROM public.exam_audio_answers WHERE exam_id=p_exam_id;
  DELETE FROM public.exam_answers WHERE exam_id=p_exam_id;
  DELETE FROM public.exam_violations WHERE attempt_id=ANY(v_attempt_ids);

  -- Break the circular attempt/session FK before deleting the sessions.
  UPDATE public.exam_attempts SET active_session_id=NULL WHERE id=ANY(v_attempt_ids);
  DELETE FROM public.exam_attempt_sessions WHERE attempt_id=ANY(v_attempt_ids);
  DELETE FROM public.exam_attempts WHERE exam_id=p_exam_id;

  DELETE FROM public.exam_allowed_students WHERE exam_id=p_exam_id;
  DELETE FROM public.exam_question_rubrics WHERE question_id=ANY(v_question_ids);
  DELETE FROM public.exam_question_options WHERE question_id=ANY(v_question_ids);
  DELETE FROM public.exam_questions WHERE exam_id=p_exam_id;
  DELETE FROM public.exams WHERE id=p_exam_id;

  RETURN jsonb_build_object(
    'deleted',TRUE,
    'audio_storage_keys',to_jsonb(v_audio_storage_keys),
    'artifact_storage_keys',to_jsonb(v_artifact_storage_keys)
  );
END;
$$;

REVOKE ALL ON FUNCTION public.delete_exam_and_dependents(UUID) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.delete_exam_and_dependents(UUID) TO service_role;
