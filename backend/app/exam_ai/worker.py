"""Durable lease-based exam evaluation and manually requested notification workers."""

import asyncio
from datetime import datetime, timedelta, timezone

from ..config import settings
from .openrouter import OpenRouterEvaluator
from .provider import EvaluationFailure, EvaluationInput


def _client():
    from supabase import create_client
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
        return None
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


def _data(result):
    value = getattr(result, 'data', None)
    return value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])


def _load_evaluation_context(client, answer_id: str) -> EvaluationInput:
    answers = _data(client.table('exam_answers').select(
        'id,attempt_id,exam_id,question_id,answer_text,answer_method'
    ).eq('id', answer_id).limit(1).execute())
    if not answers:
        raise EvaluationFailure('Student answer is no longer available')
    answer = answers[0]
    questions = _data(client.table('exam_questions').select(
        'question_text,question_type,max_marks,evaluation_mode'
    ).eq('id', answer['question_id']).eq('exam_id', answer['exam_id']).limit(1).execute())
    exams = _data(client.table('exams').select('title').eq('id', answer['exam_id']).limit(1).execute())
    if not questions or not exams:
        raise EvaluationFailure('Exam question is no longer available')
    question = questions[0]
    if question['question_type'] != 'TEXT_AUDIO_ANSWER' or question['evaluation_mode'] != 'AI_ALLOWED':
        raise EvaluationFailure('This answer is not eligible for AI evaluation')
    method = answer.get('answer_method')
    typed = (answer.get('answer_text') or '').strip()
    audio_rows = _data(client.table('exam_audio_answers').select(
        'transcription_status,transcript'
    ).eq('attempt_id', answer['attempt_id']).eq('question_id', answer['question_id']).limit(1).execute())
    transcript = ''
    if audio_rows and audio_rows[0].get('transcription_status') == 'READY':
        transcript = (audio_rows[0].get('transcript') or '').strip()
    if method == 'TEXT' and typed:
        student_answer = typed
    elif method == 'AUDIO' and transcript:
        student_answer = f"Audio transcript:\n{transcript}"
    elif method == 'BOTH' and typed and transcript:
        student_answer = f"Typed answer:\n{typed}\n\nAudio transcript:\n{transcript}"
    else:
        raise EvaluationFailure('Selected answer method is missing a saved answer or transcript')
    rubric_rows = _data(client.table('exam_question_rubrics').select(
        'reference_answer,marking_criteria'
    ).eq('question_id', answer['question_id']).limit(1).execute())
    rubric = rubric_rows[0] if rubric_rows else {}
    return EvaluationInput(
        exam_name=exams[0]['title'], question_text=question['question_text'],
        max_marks=float(question['max_marks']), student_answer=student_answer,
        reference_answer=rubric.get('reference_answer'), rubric=rubric.get('marking_criteria'),
    )


async def process_ai_batch(provider_factory=OpenRouterEvaluator):
    client = _client()
    if client is None:
        return 0
    claimed = await asyncio.to_thread(lambda: _data(client.rpc(
        'claim_exam_ai_evaluation_batch', {'p_batch_size': 5, 'p_lease_seconds': 90}
    ).execute()))
    processed = 0
    for job in claimed:
        processed += 1
        lease = job.get('lease_token')
        try:
            if settings.EXAM_AI_PROVIDER.strip().lower() != 'openrouter':
                raise EvaluationFailure('Configured Exam AI provider is not supported')
            provider = provider_factory()
            item = await asyncio.to_thread(_load_evaluation_context, client, job['answer_id'])
            result = await provider.evaluate(item)
            manual = result.manual_required or result.confidence < settings.EXAM_AI_CONFIDENCE_THRESHOLD
            values = {
                'provider': provider.name, 'model': provider.model, 'marks': result.marks,
                'max_marks': result.max_marks, 'confidence': result.confidence,
                'explanation': result.reason, 'manual_required': manual,
                'raw_response': result.raw_response, 'status': 'COMPLETE',
                'lease_until': None, 'lease_token': None, 'last_error': None,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
        except Exception as exc:
            is_retryable = isinstance(exc, EvaluationFailure) and exc.retryable
            attempts = int(job.get('attempts') or 1)
            max_attempts = int(job.get('max_attempts') or 4)
            retry = is_retryable and attempts < max_attempts
            safe_error = str(exc)[:500] if isinstance(exc, EvaluationFailure) else 'Evaluation processing failed'
            values = {
                'status': 'PENDING' if retry else 'FAILED', 'manual_required': True,
                'marks': None, 'confidence': None, 'explanation': None,
                'last_error': safe_error, 'lease_until': None, 'lease_token': None,
                'next_attempt_at': (datetime.now(timezone.utc) + timedelta(seconds=min(300, 5 * (2 ** max(attempts - 1, 0)))).isoformat()
                    if retry else job.get('next_attempt_at')),
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
        await asyncio.to_thread(lambda: client.table('exam_ai_evaluations').update(values)
            .eq('id', job['id']).eq('status', 'PROCESSING').eq('lease_token', lease).execute())
    return processed


class WhatsAppSender:
    """Explicit provider boundary. No adapter is currently configured or faked."""
    async def send_document(self, destination: str, document: bytes, filename: str, caption: str) -> bool:
        raise NotImplementedError


def whatsapp_sender():
    # A concrete adapter must be added when a real provider is selected.
    return None


async def process_notification_batch():
    client = _client()
    if client is None:
        return 0
    jobs = await asyncio.to_thread(lambda: _data(client.rpc(
        'claim_exam_notification_batch', {'p_batch_size': 5, 'p_lease_seconds': 90}
    ).execute()))
    for job in jobs:
        # No provider adapter exists yet. Record an honest terminal failure;
        # the teacher's explicit request remains visible and may be retried later.
        message = 'WhatsApp delivery is unavailable: no provider adapter is configured.'
        await asyncio.to_thread(lambda job=job: client.table('exam_notification_jobs').update({
            'status': 'FAILED', 'last_error': message, 'completed_at': None,
            'lease_until': None, 'lease_token': None,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }).eq('id', job['id']).eq('status', 'PROCESSING').eq('lease_token', job.get('lease_token')).execute())
    return len(jobs)


async def exam_jobs_worker():
    while True:
        try:
            await process_ai_batch()
            await process_notification_batch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never include provider headers, prompts, or answer text in logs.
            print(f"[EXAM-JOBS] worker cycle failed: {type(exc).__name__}")
        await asyncio.sleep(3)
