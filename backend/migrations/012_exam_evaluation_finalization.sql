-- Durable Exams AI evaluation jobs and immutable deterministic final results.
-- Migrations 008-011 are already applied and intentionally remain unchanged.

ALTER TABLE public.exam_ai_evaluations
  ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 4 CHECK (max_attempts > 0),
  ADD COLUMN next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ADD COLUMN lease_until TIMESTAMPTZ,
  ADD COLUMN lease_token UUID,
  ADD COLUMN last_error TEXT;

CREATE INDEX exam_ai_evaluations_due_idx
  ON public.exam_ai_evaluations (next_attempt_at, created_at)
  WHERE status IN ('PENDING','PROCESSING');
CREATE UNIQUE INDEX exam_ai_evaluations_one_active_job_idx
  ON public.exam_ai_evaluations (answer_id)
  WHERE status IN ('PENDING','PROCESSING');

ALTER TABLE public.exam_notification_jobs
  ADD COLUMN lease_until TIMESTAMPTZ,
  ADD COLUMN lease_token UUID,
  ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0);

INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('exam-artifacts-private','exam-artifacts-private',FALSE,10485760,ARRAY['application/pdf'])
ON CONFLICT (id) DO UPDATE SET public=FALSE, file_size_limit=10485760,
  allowed_mime_types=EXCLUDED.allowed_mime_types;

CREATE OR REPLACE FUNCTION public.enqueue_exam_ai_evaluations_after_submission()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
BEGIN
  IF NEW.status IN ('SUBMITTED','AUTO_SUBMITTED','TERMINATED')
     AND OLD.status='ACTIVE' THEN
    INSERT INTO public.exam_ai_evaluations
      (answer_id,provider,model,max_marks,status,manual_required,next_attempt_at)
    SELECT a.id,'pending','pending',q.max_marks,'PENDING',FALSE,now()
    FROM public.exam_answers a
    JOIN public.exam_questions q ON q.id=a.question_id AND q.exam_id=a.exam_id
    LEFT JOIN public.exam_audio_answers aa
      ON aa.attempt_id=a.attempt_id AND aa.question_id=a.question_id
    WHERE a.attempt_id=NEW.id
      AND q.question_type='TEXT_AUDIO_ANSWER'
      AND q.evaluation_mode='AI_ALLOWED'
      AND ((a.answer_method='TEXT' AND NULLIF(btrim(a.answer_text),'') IS NOT NULL)
        OR (a.answer_method='AUDIO' AND aa.transcription_status='READY' AND NULLIF(btrim(aa.transcript),'') IS NOT NULL)
        OR (a.answer_method='BOTH' AND NULLIF(btrim(a.answer_text),'') IS NOT NULL
          AND aa.transcription_status='READY' AND NULLIF(btrim(aa.transcript),'') IS NOT NULL))
      AND NOT EXISTS (SELECT 1 FROM public.exam_ai_evaluations prior
        WHERE prior.answer_id=a.id AND prior.status='COMPLETE');
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER exam_attempt_queue_ai_evaluation
  AFTER UPDATE OF status ON public.exam_attempts
  FOR EACH ROW EXECUTE FUNCTION public.enqueue_exam_ai_evaluations_after_submission();

CREATE OR REPLACE FUNCTION public.claim_exam_ai_evaluation_batch(
  p_batch_size INTEGER DEFAULT 5, p_lease_seconds INTEGER DEFAULT 90
) RETURNS SETOF public.exam_ai_evaluations
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
BEGIN
  RETURN QUERY
  WITH candidates AS (
    SELECT id FROM public.exam_ai_evaluations
    WHERE attempts < max_attempts AND next_attempt_at <= now()
      AND (status='PENDING' OR (status='PROCESSING' AND lease_until < now()))
    ORDER BY next_attempt_at,created_at
    FOR UPDATE SKIP LOCKED
    LIMIT GREATEST(1,LEAST(COALESCE(p_batch_size,5),50))
  )
  UPDATE public.exam_ai_evaluations e
  SET status='PROCESSING',attempts=e.attempts+1,
      lease_until=now()+make_interval(secs=>GREATEST(30,LEAST(COALESCE(p_lease_seconds,90),600))),
      lease_token=gen_random_uuid(),updated_at=now()
  FROM candidates c WHERE e.id=c.id
  RETURNING e.*;
END $$;

CREATE OR REPLACE FUNCTION public.claim_exam_notification_batch(
  p_batch_size INTEGER DEFAULT 10, p_lease_seconds INTEGER DEFAULT 90
) RETURNS SETOF public.exam_notification_jobs
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
BEGIN
  RETURN QUERY
  WITH candidates AS (
    SELECT id FROM public.exam_notification_jobs
    WHERE scheduled_at<=now() AND attempts<max_attempts
      AND (status='PENDING' OR (status='PROCESSING' AND lease_until<now()))
    ORDER BY scheduled_at,created_at
    FOR UPDATE SKIP LOCKED
    LIMIT GREATEST(1,LEAST(COALESCE(p_batch_size,10),50))
  )
  UPDATE public.exam_notification_jobs j
  SET status='PROCESSING',attempts=j.attempts+1,
      lease_until=now()+make_interval(secs=>GREATEST(30,LEAST(COALESCE(p_lease_seconds,90),600))),
      lease_token=gen_random_uuid(),started_at=COALESCE(j.started_at,now()),updated_at=now()
  FROM candidates c WHERE j.id=c.id RETURNING j.*;
END $$;

CREATE OR REPLACE FUNCTION public.finalize_exam_results(
  p_exam_id UUID, p_confidence_threshold NUMERIC DEFAULT 0.75
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_exam public.exams%ROWTYPE; v_unresolved INTEGER;
BEGIN
  SELECT * INTO v_exam FROM public.exams WHERE id=p_exam_id FOR UPDATE;
  IF NOT FOUND THEN RETURN jsonb_build_object('error','exam_not_found'); END IF;
  IF v_exam.status='FINALIZED' THEN
    RETURN jsonb_build_object('status','FINALIZED','already_finalized',TRUE);
  END IF;
  IF v_exam.status NOT IN ('SUBMISSION_CLOSED','MANUAL_REVIEW','EVALUATING') THEN
    RETURN jsonb_build_object('error','exam_not_closed');
  END IF;
  IF EXISTS (SELECT 1 FROM public.exam_attempts WHERE exam_id=p_exam_id AND status='ACTIVE') THEN
    RETURN jsonb_build_object('error','active_attempts');
  END IF;

  SELECT count(*) INTO v_unresolved
  FROM public.exam_attempts a
  JOIN public.exam_questions q ON q.exam_id=a.exam_id
  LEFT JOIN public.exam_answers ans ON ans.attempt_id=a.id AND ans.question_id=q.id
  LEFT JOIN public.exam_audio_answers aa ON aa.attempt_id=a.id AND aa.question_id=q.id
  LEFT JOIN LATERAL (
    SELECT r.review_status FROM public.exam_manual_reviews r
    WHERE r.answer_id=ans.id ORDER BY r.updated_at DESC LIMIT 1
  ) mr ON TRUE
  LEFT JOIN LATERAL (
    SELECT e.status,e.marks,e.confidence,e.manual_required,e.max_marks
    FROM public.exam_ai_evaluations e WHERE e.answer_id=ans.id
    ORDER BY e.updated_at DESC,e.created_at DESC LIMIT 1
  ) ai ON TRUE
  WHERE a.exam_id=p_exam_id AND a.status IN ('SUBMITTED','AUTO_SUBMITTED','TERMINATED','MANUAL_REVIEW')
    AND q.question_type='TEXT_AUDIO_ANSWER'
    AND ((ans.id IS NULL AND q.is_required)
      OR (ans.id IS NOT NULL AND (q.is_required
          OR (ans.answer_method='TEXT' AND NULLIF(btrim(ans.answer_text),'') IS NOT NULL)
          OR (ans.answer_method='AUDIO' AND aa.transcription_status='READY' AND NULLIF(btrim(aa.transcript),'') IS NOT NULL)
          OR (ans.answer_method='BOTH' AND NULLIF(btrim(ans.answer_text),'') IS NOT NULL
            AND aa.transcription_status='READY' AND NULLIF(btrim(aa.transcript),'') IS NOT NULL))
        AND COALESCE(mr.review_status,'') NOT IN ('SUBMITTED','FINALIZED')
        AND NOT COALESCE((ai.status='COMPLETE' AND ai.marks IS NOT NULL
          AND ai.max_marks=q.max_marks AND ai.confidence>=p_confidence_threshold
          AND ai.manual_required=FALSE),FALSE)));
  IF v_unresolved>0 THEN
    RETURN jsonb_build_object('error','unresolved_evaluations','count',v_unresolved);
  END IF;

  WITH attempt_scores AS (
    SELECT a.id AS attempt_id,a.exam_id,a.google_sub,a.submitted_at,
      GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (a.submitted_at-a.started_at))))::BIGINT AS completion_seconds,
      COALESCE(SUM(q.max_marks),0)::NUMERIC(11,2) AS maximum_marks,
      COALESCE(SUM(CASE
        WHEN q.question_type='MCQ' THEN CASE WHEN correct.id IS NOT NULL THEN q.max_marks ELSE 0 END
        WHEN ans.id IS NULL THEN 0
        WHEN mr.review_status IN ('SUBMITTED','FINALIZED') THEN mr.marks
        WHEN ai.status='COMPLETE' AND ai.manual_required=FALSE
          AND ai.confidence>=p_confidence_threshold AND ai.max_marks=q.max_marks THEN ai.marks
        ELSE 0 END),0)::NUMERIC(11,2) AS total_marks
    FROM public.exam_attempts a
    JOIN public.exam_questions q ON q.exam_id=a.exam_id
    LEFT JOIN public.exam_answers ans ON ans.attempt_id=a.id AND ans.question_id=q.id
    LEFT JOIN public.exam_question_options correct
      ON correct.id=ans.selected_option_id AND correct.question_id=q.id AND correct.is_correct
    LEFT JOIN LATERAL (SELECT r.review_status,r.marks FROM public.exam_manual_reviews r
      WHERE r.answer_id=ans.id ORDER BY r.updated_at DESC LIMIT 1) mr ON TRUE
    LEFT JOIN LATERAL (SELECT e.status,e.marks,e.confidence,e.manual_required,e.max_marks
      FROM public.exam_ai_evaluations e WHERE e.answer_id=ans.id
      ORDER BY e.updated_at DESC,e.created_at DESC LIMIT 1) ai ON TRUE
    WHERE a.exam_id=p_exam_id AND a.status IN ('SUBMITTED','AUTO_SUBMITTED','TERMINATED','MANUAL_REVIEW')
    GROUP BY a.id,a.exam_id,a.google_sub,a.submitted_at,a.started_at
  ), ranked AS (
    SELECT s.*, CASE WHEN s.maximum_marks=0 THEN 0
      ELSE ROUND(s.total_marks*100.0/s.maximum_marks,4) END AS percentage,
      ROW_NUMBER() OVER (ORDER BY s.total_marks DESC,s.completion_seconds ASC,s.submitted_at ASC,s.google_sub ASC) AS rank
    FROM attempt_scores s
  )
  INSERT INTO public.exam_results
    (exam_id,attempt_id,google_sub,total_marks,maximum_marks,percentage,grade,completion_time_seconds,rank,finalized_at)
  SELECT p_exam_id,r.attempt_id,r.google_sub,r.total_marks,r.maximum_marks,r.percentage,
    CASE WHEN r.percentage>=90 THEN 'A' WHEN r.percentage>=80 THEN 'B'
         WHEN r.percentage>=70 THEN 'C' WHEN r.percentage>=60 THEN 'D' ELSE 'F' END,
    r.completion_seconds,r.rank,now()
  FROM ranked r ON CONFLICT (attempt_id) DO NOTHING;

  UPDATE public.exam_manual_reviews mr SET review_status='FINALIZED',finalized_at=now(),updated_at=now()
  FROM public.exam_answers ans JOIN public.exam_attempts a ON a.id=ans.attempt_id
  WHERE mr.answer_id=ans.id AND a.exam_id=p_exam_id AND mr.review_status='SUBMITTED';
  UPDATE public.exam_attempts SET status='FINALIZED',updated_at=now()
    WHERE exam_id=p_exam_id AND status IN ('SUBMITTED','AUTO_SUBMITTED','TERMINATED','MANUAL_REVIEW');
  UPDATE public.exams SET status='FINALIZED',finalized_at=now(),updated_at=now() WHERE id=p_exam_id;
  RETURN jsonb_build_object('status','FINALIZED','already_finalized',FALSE);
END $$;

CREATE OR REPLACE FUNCTION public.prevent_finalized_exam_result_mutation()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE v_exam_id UUID;
        v_answer_id UUID;
BEGIN
  IF TG_OP='DELETE' THEN
    IF TG_TABLE_NAME='exam_results' THEN v_exam_id:=OLD.exam_id;
    ELSE v_answer_id:=OLD.answer_id;
    END IF;
  ELSE
    IF TG_TABLE_NAME='exam_results' THEN v_exam_id:=NEW.exam_id;
    ELSE v_answer_id:=NEW.answer_id;
    END IF;
  END IF;
  IF TG_TABLE_NAME='exam_manual_reviews' OR TG_TABLE_NAME='exam_ai_evaluations' THEN
    SELECT a.exam_id INTO v_exam_id FROM public.exam_answers ans
    JOIN public.exam_attempts a ON a.id=ans.attempt_id
    WHERE ans.id=v_answer_id;
  END IF;
  -- Serialize this guard with finalize_exam_results' parent-row FOR UPDATE.
  PERFORM 1 FROM public.exams WHERE id=v_exam_id AND status='FINALIZED' FOR KEY SHARE;
  IF FOUND THEN
    RAISE EXCEPTION 'Finalized exam results and evaluations are immutable' USING ERRCODE='55000';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER exam_results_finalized_immutable
  BEFORE INSERT OR UPDATE OR DELETE ON public.exam_results
  FOR EACH ROW EXECUTE FUNCTION public.prevent_finalized_exam_result_mutation();
CREATE TRIGGER exam_manual_reviews_finalized_immutable
  BEFORE INSERT OR UPDATE OR DELETE ON public.exam_manual_reviews
  FOR EACH ROW EXECUTE FUNCTION public.prevent_finalized_exam_result_mutation();
CREATE TRIGGER exam_ai_evaluations_finalized_immutable
  BEFORE INSERT OR UPDATE OR DELETE ON public.exam_ai_evaluations
  FOR EACH ROW EXECUTE FUNCTION public.prevent_finalized_exam_result_mutation();

REVOKE ALL ON FUNCTION public.enqueue_exam_ai_evaluations_after_submission() FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.claim_exam_ai_evaluation_batch(INTEGER,INTEGER) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.claim_exam_notification_batch(INTEGER,INTEGER) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.finalize_exam_results(UUID,NUMERIC) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.prevent_finalized_exam_result_mutation() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.claim_exam_ai_evaluation_batch(INTEGER,INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.claim_exam_notification_batch(INTEGER,INTEGER) TO service_role;
GRANT EXECUTE ON FUNCTION public.finalize_exam_results(UUID,NUMERIC) TO service_role;
