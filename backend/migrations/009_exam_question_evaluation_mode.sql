-- Persist the teacher's intended evaluation path for descriptive/audio questions.
ALTER TABLE public.exam_questions
  ADD COLUMN evaluation_mode TEXT NOT NULL DEFAULT 'MANUAL_ONLY'
    CHECK (evaluation_mode IN ('AI_ALLOWED','MANUAL_ONLY'));
