-- Isolated Exams database foundation. Apply after the repository's current
-- migration 007; this migration intentionally does not alter existing tables.
-- The FastAPI server uses service_role after authorization. Browser roles get
-- no direct access to exam data (including answer keys and private storage keys).

DO $$
BEGIN
  IF to_regclass('public.exams') IS NOT NULL
     OR to_regclass('public.exam_questions') IS NOT NULL
     OR to_regclass('public.exam_question_options') IS NOT NULL
     OR to_regclass('public.exam_question_rubrics') IS NOT NULL
     OR to_regclass('public.exam_allowed_students') IS NOT NULL
     OR to_regclass('public.exam_attempts') IS NOT NULL
     OR to_regclass('public.exam_attempt_sessions') IS NOT NULL
     OR to_regclass('public.exam_answers') IS NOT NULL
     OR to_regclass('public.exam_audio_answers') IS NOT NULL
     OR to_regclass('public.exam_ai_evaluations') IS NOT NULL
     OR to_regclass('public.exam_manual_reviews') IS NOT NULL
     OR to_regclass('public.exam_violations') IS NOT NULL
     OR to_regclass('public.exam_results') IS NOT NULL
     OR to_regclass('public.exam_rankings') IS NOT NULL
     OR to_regclass('public.exam_artifacts') IS NOT NULL
     OR to_regclass('public.exam_notification_jobs') IS NOT NULL THEN
    RAISE EXCEPTION 'An Exams table already exists; inspect schema before applying migration 008';
  END IF;
END $$;

CREATE TABLE public.exams (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  public_token_hash TEXT NOT NULL UNIQUE CHECK (public_token_hash ~ '^[0-9a-f]{64}$'),
  title TEXT NOT NULL CHECK (length(trim(title)) > 0),
  description TEXT,
  instructions TEXT,
  scheduled_start_at TIMESTAMPTZ,
  duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
  status TEXT NOT NULL DEFAULT 'DRAFT'
    CHECK (status IN ('DRAFT','SCHEDULED','ACTIVE','SUBMISSION_CLOSED','EVALUATING','MANUAL_REVIEW','FINALIZED','CANCELLED')),
  protected_mode_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  maximum_violations INTEGER NOT NULL DEFAULT 0 CHECK (maximum_violations >= 0),
  require_fullscreen BOOLEAN NOT NULL DEFAULT FALSE,
  detect_visibility_change BOOLEAN NOT NULL DEFAULT TRUE,
  detect_orientation_change BOOLEAN NOT NULL DEFAULT FALSE,
  restrict_copy_paste BOOLEAN NOT NULL DEFAULT FALSE,
  autosave_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  automatic_submission_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  allow_list_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finalized_at TIMESTAMPTZ
);

CREATE TABLE public.exam_questions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  exam_id UUID NOT NULL REFERENCES public.exams(id) ON DELETE RESTRICT,
  question_number INTEGER NOT NULL CHECK (question_number > 0),
  question_text TEXT NOT NULL CHECK (length(trim(question_text)) > 0),
  question_type TEXT NOT NULL CHECK (question_type IN ('MCQ','SHORT_ANSWER','LONG_ANSWER','AUDIO_ANSWER')),
  max_marks NUMERIC(9,2) NOT NULL CHECK (max_marks >= 0),
  is_required BOOLEAN NOT NULL DEFAULT TRUE,
  answer_input_mode TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (exam_id, question_number),
  UNIQUE (id, exam_id)
);

CREATE TABLE public.exam_question_options (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  question_id UUID NOT NULL REFERENCES public.exam_questions(id) ON DELETE RESTRICT,
  option_order INTEGER NOT NULL CHECK (option_order > 0),
  option_text TEXT NOT NULL CHECK (length(trim(option_text)) > 0),
  is_correct BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (question_id, option_order),
  UNIQUE (id, question_id)
);

CREATE TABLE public.exam_question_rubrics (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  question_id UUID NOT NULL UNIQUE REFERENCES public.exam_questions(id) ON DELETE RESTRICT,
  reference_answer TEXT,
  marking_criteria TEXT,
  max_marks NUMERIC(9,2) CHECK (max_marks >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (reference_answer IS NOT NULL OR marking_criteria IS NOT NULL OR max_marks IS NOT NULL)
);

CREATE TABLE public.exam_allowed_students (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  exam_id UUID NOT NULL REFERENCES public.exams(id) ON DELETE RESTRICT,
  google_sub TEXT NOT NULL CHECK (length(trim(google_sub)) > 0),
  display_name TEXT,
  email TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (exam_id, google_sub)
);

CREATE TABLE public.exam_attempts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  exam_id UUID NOT NULL REFERENCES public.exams(id) ON DELETE RESTRICT,
  google_sub TEXT NOT NULL CHECK (length(trim(google_sub)) > 0),
  student_display_name TEXT,
  student_email TEXT,
  status TEXT NOT NULL DEFAULT 'NOT_STARTED'
    CHECK (status IN ('NOT_STARTED','ACTIVE','SUBMITTED','AUTO_SUBMITTED','TERMINATED','EVALUATING','MANUAL_REVIEW','FINALIZED')),
  started_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  submitted_at TIMESTAMPTZ,
  last_activity_at TIMESTAMPTZ,
  violation_count INTEGER NOT NULL DEFAULT 0 CHECK (violation_count >= 0),
  active_session_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (exam_id, google_sub),
  UNIQUE (id, exam_id),
  UNIQUE (id, exam_id, google_sub),
  CHECK (expires_at IS NULL OR started_at IS NOT NULL),
  CHECK (submitted_at IS NULL OR started_at IS NOT NULL),
  CHECK (expires_at IS NULL OR expires_at >= started_at)
);

CREATE TABLE public.exam_attempt_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  attempt_id UUID NOT NULL REFERENCES public.exam_attempts(id) ON DELETE RESTRICT,
  session_identifier_hash TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','ENDED','REPLACED')),
  CHECK ((status = 'ACTIVE') = (ended_at IS NULL)),
  UNIQUE (id, attempt_id),
  UNIQUE (attempt_id, session_identifier_hash)
);
ALTER TABLE public.exam_attempts ADD CONSTRAINT exam_attempts_active_session_fk
  FOREIGN KEY (active_session_id, id) REFERENCES public.exam_attempt_sessions(id, attempt_id)
  DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE public.exam_answers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  attempt_id UUID NOT NULL,
  exam_id UUID NOT NULL,
  question_id UUID NOT NULL,
  answer_text TEXT,
  selected_option_id UUID,
  answer_version BIGINT NOT NULL DEFAULT 1 CHECK (answer_version > 0),
  saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  submitted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (attempt_id, exam_id) REFERENCES public.exam_attempts(id, exam_id) ON DELETE RESTRICT,
  FOREIGN KEY (question_id, exam_id) REFERENCES public.exam_questions(id, exam_id) ON DELETE RESTRICT,
  FOREIGN KEY (selected_option_id, question_id) REFERENCES public.exam_question_options(id, question_id) ON DELETE RESTRICT,
  UNIQUE (attempt_id, question_id),
  CHECK (selected_option_id IS NULL OR answer_text IS NULL)
);

CREATE TABLE public.exam_audio_answers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  attempt_id UUID NOT NULL,
  exam_id UUID NOT NULL,
  question_id UUID NOT NULL,
  storage_key TEXT NOT NULL,
  audio_mime_type TEXT NOT NULL,
  file_size_bytes BIGINT NOT NULL CHECK (file_size_bytes > 0),
  duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms > 0),
  transcription_status TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (transcription_status IN ('PENDING','PROCESSING','READY','FAILED','UNAVAILABLE')),
  transcript TEXT,
  uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  transcribed_at TIMESTAMPTZ,
  delete_after TIMESTAMPTZ,
  retained_at TIMESTAMPTZ,
  FOREIGN KEY (attempt_id, exam_id) REFERENCES public.exam_attempts(id, exam_id) ON DELETE RESTRICT,
  FOREIGN KEY (question_id, exam_id) REFERENCES public.exam_questions(id, exam_id) ON DELETE RESTRICT,
  UNIQUE (attempt_id, question_id),
  UNIQUE (storage_key),
  CHECK ((transcription_status = 'READY') = (transcript IS NOT NULL AND transcribed_at IS NOT NULL))
);

CREATE TABLE public.exam_ai_evaluations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  answer_id UUID NOT NULL REFERENCES public.exam_answers(id) ON DELETE RESTRICT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  marks NUMERIC(9,2) CHECK (marks >= 0),
  max_marks NUMERIC(9,2) NOT NULL CHECK (max_marks >= 0),
  confidence NUMERIC(5,4) CHECK (confidence BETWEEN 0 AND 1),
  explanation TEXT,
  manual_required BOOLEAN NOT NULL DEFAULT FALSE,
  raw_response JSONB CHECK (raw_response IS NULL OR jsonb_typeof(raw_response) IN ('object','array')),
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','PROCESSING','COMPLETE','FAILED','REJECTED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (marks IS NULL OR marks <= max_marks)
);

CREATE TABLE public.exam_manual_reviews (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  answer_id UUID NOT NULL REFERENCES public.exam_answers(id) ON DELETE RESTRICT,
  reviewer_session_id TEXT,
  marks NUMERIC(9,2) NOT NULL CHECK (marks >= 0),
  max_marks NUMERIC(9,2) NOT NULL CHECK (max_marks >= 0),
  teacher_comments TEXT,
  review_status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (review_status IN ('DRAFT','SUBMITTED','FINALIZED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finalized_at TIMESTAMPTZ,
  CHECK (marks <= max_marks),
  CHECK ((review_status = 'FINALIZED') = (finalized_at IS NOT NULL))
);

CREATE TABLE public.exam_violations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  attempt_id UUID NOT NULL REFERENCES public.exam_attempts(id) ON DELETE RESTRICT,
  violation_type TEXT NOT NULL CHECK (violation_type IN ('FULLSCREEN_EXIT','TAB_HIDDEN','WINDOW_BLUR','ORIENTATION_CHANGE','NAVIGATION_ATTEMPT','MULTIPLE_SESSION','HEARTBEAT_TIMEOUT','OTHER')),
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
  UNIQUE (attempt_id, sequence_number)
);

CREATE TABLE public.exam_results (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  exam_id UUID NOT NULL,
  attempt_id UUID NOT NULL UNIQUE,
  google_sub TEXT NOT NULL CHECK (length(trim(google_sub)) > 0),
  total_marks NUMERIC(11,2) NOT NULL CHECK (total_marks >= 0),
  maximum_marks NUMERIC(11,2) NOT NULL CHECK (maximum_marks >= 0),
  percentage NUMERIC(7,4) NOT NULL CHECK (percentage BETWEEN 0 AND 100),
  grade TEXT,
  completion_time_seconds BIGINT NOT NULL CHECK (completion_time_seconds >= 0),
  rank INTEGER CHECK (rank IS NULL OR rank > 0),
  finalized_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  FOREIGN KEY (attempt_id, exam_id, google_sub) REFERENCES public.exam_attempts(id, exam_id, google_sub) ON DELETE RESTRICT,
  UNIQUE (exam_id, google_sub),
  UNIQUE (exam_id, rank),
  CHECK (total_marks <= maximum_marks),
  CHECK ((maximum_marks = 0 AND percentage = 0) OR
         (maximum_marks > 0 AND percentage = round(total_marks * 100.0 / maximum_marks, 4)))
);

-- Rank is stored on finalized results and can be derived/rebuilt in a single
-- deterministic query (marks DESC, completion_time_seconds ASC); a second
-- ranking table would duplicate authoritative values.

CREATE TABLE public.exam_artifacts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  exam_id UUID NOT NULL REFERENCES public.exams(id) ON DELETE RESTRICT,
  result_id UUID REFERENCES public.exam_results(id) ON DELETE RESTRICT,
  artifact_type TEXT NOT NULL CHECK (artifact_type IN ('GRADE_CARD','RANK_CARD','OTHER')),
  storage_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','READY','FAILED','EXPIRED')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.exam_notification_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  result_id UUID REFERENCES public.exam_results(id) ON DELETE RESTRICT,
  artifact_id UUID REFERENCES public.exam_artifacts(id) ON DELETE RESTRICT,
  destination_type TEXT NOT NULL CHECK (destination_type IN ('EMAIL','WHATSAPP','OTHER')),
  destination_ref TEXT,
  job_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','PROCESSING','COMPLETE','FAILED','CANCELLED')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error TEXT,
  scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (result_id IS NOT NULL OR artifact_id IS NOT NULL),
  CHECK ((status = 'COMPLETE') = (completed_at IS NOT NULL))
);

CREATE INDEX exam_questions_exam_order_idx ON public.exam_questions (exam_id, question_number);
CREATE INDEX exam_options_question_order_idx ON public.exam_question_options (question_id, option_order);
CREATE INDEX exam_allowed_students_sub_idx ON public.exam_allowed_students (google_sub, exam_id);
CREATE INDEX exam_attempts_exam_status_idx ON public.exam_attempts (exam_id, status, created_at DESC);
CREATE INDEX exam_attempts_student_idx ON public.exam_attempts (google_sub, created_at DESC);
CREATE INDEX exam_sessions_attempt_heartbeat_idx ON public.exam_attempt_sessions (attempt_id, last_heartbeat_at DESC);
CREATE UNIQUE INDEX exam_sessions_one_active_idx ON public.exam_attempt_sessions (attempt_id) WHERE status = 'ACTIVE';
CREATE INDEX exam_answers_attempt_idx ON public.exam_answers (attempt_id);
CREATE INDEX exam_audio_transcription_due_idx ON public.exam_audio_answers (transcription_status, uploaded_at)
  WHERE transcription_status IN ('PENDING','PROCESSING');
CREATE INDEX exam_ai_evaluations_answer_idx ON public.exam_ai_evaluations (answer_id, created_at DESC);
CREATE INDEX exam_manual_reviews_status_idx ON public.exam_manual_reviews (review_status, updated_at DESC);
CREATE INDEX exam_violations_attempt_time_idx ON public.exam_violations (attempt_id, occurred_at);
CREATE INDEX exam_results_leaderboard_idx ON public.exam_results (exam_id, total_marks DESC, completion_time_seconds ASC);
CREATE INDEX exam_artifacts_exam_idx ON public.exam_artifacts (exam_id, created_at DESC);
CREATE INDEX exam_notification_jobs_due_idx ON public.exam_notification_jobs (scheduled_at, created_at)
  WHERE status = 'PENDING';

COMMENT ON COLUMN public.exams.public_token_hash IS
  'SHA-256 hex digest of a cryptographically random public token; only the digest is persisted.';
COMMENT ON TABLE public.exam_questions IS
  'Question edits and marking configuration are service-layer restricted to pre-release exams; once any attempt starts, content must be immutable.';
COMMENT ON TABLE public.exam_results IS
  'Final marks are derived from deterministic/manual grading; AI evaluation rows are advisory only. Rank tie-break order is marks DESC then completion_time_seconds ASC.';

ALTER TABLE public.exams ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_questions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_question_options ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_question_rubrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_allowed_students ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_attempt_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_audio_answers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_ai_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_manual_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_violations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_notification_jobs ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON public.exams, public.exam_questions, public.exam_question_options,
  public.exam_question_rubrics, public.exam_allowed_students, public.exam_attempts,
  public.exam_attempt_sessions, public.exam_answers, public.exam_audio_answers,
  public.exam_ai_evaluations, public.exam_manual_reviews, public.exam_violations,
  public.exam_results, public.exam_artifacts, public.exam_notification_jobs
  FROM PUBLIC, anon, authenticated;
GRANT ALL PRIVILEGES ON public.exams, public.exam_questions, public.exam_question_options,
  public.exam_question_rubrics, public.exam_allowed_students, public.exam_attempts,
  public.exam_attempt_sessions, public.exam_answers, public.exam_audio_answers,
  public.exam_ai_evaluations, public.exam_manual_reviews, public.exam_violations,
  public.exam_results, public.exam_artifacts, public.exam_notification_jobs TO service_role;
