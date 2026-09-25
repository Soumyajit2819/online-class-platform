"""Teacher-only Exams creation and scheduling API."""

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, constr

from .config import settings
from .exam_auth import require_exam_management
from . import exam_artifacts
from .passcode_service import passcode_service

router = APIRouter(prefix="/api/exams", tags=["Exams management"],
                    dependencies=[Depends(require_exam_management)])
ExamStatus = Literal['DRAFT','SCHEDULED','ACTIVE','SUBMISSION_CLOSED','EVALUATING','MANUAL_REVIEW','FINALIZED','CANCELLED']
QuestionType = Literal['MCQ','TEXT_AUDIO_ANSWER']
EvaluationMode = Literal['AI_ALLOWED','MANUAL_ONLY']


def _client():
    from supabase import create_client
    if not settings.validate_supabase_db():
        raise HTTPException(503, 'Exams database is unavailable')
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


async def _db(operation):
    try:
        return await asyncio.to_thread(operation)
    except HTTPException:
        raise
    except Exception as exc:
        # Avoid returning database details to the management browser.
        raise HTTPException(503, 'Exams database operation failed') from exc


def _exam_or_404(client, exam_id: UUID):
    rows = client.table('exams').select('*').eq('id', str(exam_id)).limit(1).execute().data or []
    if not rows:
        raise HTTPException(404, 'Exam not found')
    return rows[0]


def _editable(client, exam_id: UUID):
    exam = _exam_or_404(client, exam_id)
    if exam['status'] not in ('DRAFT', 'SCHEDULED'):
        raise HTTPException(409, 'This exam can no longer be edited')
    attempts = client.table('exam_attempts').select('id').eq('exam_id', str(exam_id)).limit(1).execute().data or []
    if attempts:
        raise HTTPException(409, 'An exam with attempts can no longer be edited')
    return exam


def _lifecycle_status(exam: dict, now: datetime) -> str:
    """Derive scheduled lifecycle from the persisted UTC instant and server clock."""
    status = exam.get('status')
    if status == 'CANCELLED' or status == 'DRAFT':
        return status
    if status not in ('SCHEDULED', 'ACTIVE'):
        return status
    raw_start = exam.get('scheduled_start_at')
    if not raw_start:
        return 'SCHEDULED'
    start = datetime.fromisoformat(raw_start.replace('Z', '+00:00')) if isinstance(raw_start, str) else raw_start
    if start.tzinfo is None or start.utcoffset() is None:
        start = start.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc) if now.tzinfo and now.utcoffset() is not None else now.replace(tzinfo=timezone.utc)
    if now_utc < start:
        return 'SCHEDULED'
    expiry = start + timedelta(minutes=int(exam['duration_minutes']))
    return 'SUBMISSION_CLOSED' if now_utc >= expiry else 'ACTIVE'


def _refresh_lifecycle(client, exam: dict, now: datetime | None = None) -> dict:
    """Persist stale scheduled/active labels when a management API reads them."""
    now = now or datetime.now(timezone.utc)
    now_utc = now.astimezone(timezone.utc) if now.tzinfo and now.utcoffset() is not None else now.replace(tzinfo=timezone.utc)
    derived = _lifecycle_status(exam, now)
    if derived != exam.get('status') and exam.get('status') in ('SCHEDULED', 'ACTIVE'):
        client.table('exams').update({
            'status': derived,
            'updated_at': now_utc.isoformat(),
        }).eq('id', exam['id']).eq('status', exam['status']).execute()
        exam = {**exam, 'status': derived}
    return exam


def _timestamp(value: datetime | None, *, must_be_future: bool = False) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(422, 'Scheduled start must include a timezone')
    normalized = value.astimezone(timezone.utc)
    if must_be_future and normalized <= datetime.now(timezone.utc):
        raise HTTPException(422, 'Scheduled start must be in the future')
    return normalized.isoformat()


class ExamCreate(BaseModel):
    title: constr(strip_whitespace=True, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    instructions: str | None = Field(default=None, max_length=10000)
    custom_instructions_enabled: bool = False
    custom_instructions: str | None = Field(default=None, max_length=10000)
    scheduled_start_at: datetime
    duration_minutes: int = Field(gt=0, le=1440)
    maximum_violations: int = Field(default=3, ge=0, le=100)
    protected_mode_enabled: bool = True
    require_fullscreen: bool = True
    detect_visibility_change: bool = True
    detect_orientation_change: bool = False
    restrict_copy_paste: bool = True
    autosave_enabled: bool = True
    automatic_submission_enabled: bool = True
    allow_list_enabled: bool = False


class ExamPatch(BaseModel):
    title: constr(strip_whitespace=True, min_length=1, max_length=200) | None = None
    description: str | None = Field(default=None, max_length=10000)
    instructions: str | None = Field(default=None, max_length=10000)
    custom_instructions_enabled: bool | None = None
    custom_instructions: str | None = Field(default=None, max_length=10000)
    scheduled_start_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=1440)
    maximum_violations: int | None = Field(default=None, ge=0, le=100)
    protected_mode_enabled: bool | None = None
    require_fullscreen: bool | None = None
    detect_visibility_change: bool | None = None
    detect_orientation_change: bool | None = None
    restrict_copy_paste: bool | None = None
    autosave_enabled: bool | None = None
    automatic_submission_enabled: bool | None = None
    allow_list_enabled: bool | None = None


class OptionInput(BaseModel):
    text: constr(strip_whitespace=True, min_length=1, max_length=2000)
    is_correct: bool = False


class QuestionInput(BaseModel):
    question_text: constr(strip_whitespace=True, min_length=1, max_length=10000)
    question_type: QuestionType
    max_marks: float = Field(gt=0, le=100000, allow_inf_nan=False)
    is_required: bool = True
    evaluation_mode: EvaluationMode = 'MANUAL_ONLY'
    reference_answer: str | None = Field(default=None, max_length=10000)
    marking_criteria: str | None = Field(default=None, max_length=10000)
    options: list[OptionInput] = Field(default_factory=list, max_length=100)

    def validate_configuration(self):
        if self.question_type == 'MCQ':
            if len(self.options) < 2:
                raise HTTPException(422, {'field': 'options', 'message': 'MCQs require at least two options'})
            if sum(option.is_correct for option in self.options) != 1:
                raise HTTPException(422, {'field': 'options', 'message': 'Select exactly one correct option'})
            if self.evaluation_mode != 'MANUAL_ONLY':
                raise HTTPException(422, {'field': 'evaluation_mode', 'message': 'MCQs are evaluated deterministically'})
        elif self.options:
            raise HTTPException(422, {'field': 'options', 'message': 'Only MCQ questions can have options'})


class ReorderInput(BaseModel):
    question_ids: list[UUID] = Field(min_length=1, max_length=500)


class AllowedStudentInput(BaseModel):
    google_sub: constr(strip_whitespace=True, min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class ManualEvaluationInput(BaseModel):
    marks: float = Field(ge=0, allow_inf_nan=False)
    teacher_feedback: str | None = Field(default=None, max_length=10000)


def _question_view(client, row):
    options = client.table('exam_question_options').select('id,option_order,option_text,is_correct').eq('question_id', row['id']).order('option_order').execute().data or []
    rubrics = client.table('exam_question_rubrics').select('reference_answer,marking_criteria').eq('question_id', row['id']).limit(1).execute().data or []
    rubric = rubrics[0] if rubrics else {}
    return {**row, 'options': options, **rubric}


@router.post('')
async def create_exam(payload: ExamCreate):
    start = _timestamp(payload.scheduled_start_at, must_be_future=True)
    token = secrets.token_urlsafe(32)
    record = payload.model_dump(exclude={'scheduled_start_at'})
    record['instructions'] = None
    if not record['custom_instructions_enabled']:
        record['custom_instructions'] = None
    record.update(scheduled_start_at=start, public_token_hash=hashlib.sha256(token.encode()).hexdigest(), status='DRAFT')
    record['created_at'] = record['updated_at'] = datetime.now(timezone.utc).isoformat()
    def write():
        client = _client()
        row = client.table('exams').insert(record).execute().data[0]
        # Return the public token only once at creation; only its hash is stored.
        row.pop('public_token_hash', None)
        return {**row, 'public_token': token}
    return await _db(write)


@router.get('')
async def list_exams():
    def read():
        client = _client()
        rows = client.table('exams').select('id,title,status,scheduled_start_at,duration_minutes,allow_list_enabled,created_at,updated_at').order('created_at', desc=True).execute().data or []
        for row in rows:
            row.update(_refresh_lifecycle(client, row))
            row['question_count'] = len(client.table('exam_questions').select('id').eq('exam_id', row['id']).execute().data or [])
        return rows
    return {'exams': await _db(read)}


@router.get('/{exam_id}')
async def get_exam(exam_id: UUID):
    def read():
        client = _client(); exam = _exam_or_404(client, exam_id)
        exam = _refresh_lifecycle(client, exam)
        exam.pop('public_token_hash', None)
        exam['questions'] = [_question_view(client, question) for question in
            (client.table('exam_questions').select('*').eq('exam_id', str(exam_id)).order('question_number').execute().data or [])]
        exam['allowed_students'] = client.table('exam_allowed_students').select('google_sub,display_name,email').eq('exam_id', str(exam_id)).order('created_at').execute().data or []
        return exam
    return await _db(read)


@router.patch('/{exam_id}')
async def patch_exam(exam_id: UUID, payload: ExamPatch):
    changes = payload.dict(exclude_unset=True)
    if not changes:
        raise HTTPException(422, 'At least one field is required')
    for field in ('title', 'instructions', 'scheduled_start_at', 'duration_minutes',
                  'maximum_violations', 'protected_mode_enabled', 'require_fullscreen',
                  'detect_visibility_change', 'detect_orientation_change', 'restrict_copy_paste',
                  'autosave_enabled', 'automatic_submission_enabled', 'allow_list_enabled'):
        if field in changes and changes[field] is None:
            raise HTTPException(422, {'field': field, 'message': 'This field cannot be empty'})
    if 'scheduled_start_at' in changes:
        changes['scheduled_start_at'] = _timestamp(changes['scheduled_start_at'], must_be_future=True)
    def write():
        client = _client(); _editable(client, exam_id)
        changes['updated_at'] = datetime.now(timezone.utc).isoformat()
        return client.table('exams').update(changes).eq('id', str(exam_id)).execute().data[0]
    return await _db(write)


def _save_question(client, exam_id: UUID, question_id: UUID | None, payload: QuestionInput):
    payload.validate_configuration()
    response = client.rpc('save_exam_question_configuration', {
        'p_exam_id':str(exam_id), 'p_question_id':str(question_id) if question_id else None,
        'p_question_text':payload.question_text, 'p_question_type':payload.question_type,
        'p_max_marks':payload.max_marks, 'p_is_required':payload.is_required,
        'p_evaluation_mode':payload.evaluation_mode,
        'p_reference_answer':payload.reference_answer, 'p_marking_criteria':payload.marking_criteria,
        'p_options':[option.model_dump() for option in payload.options],
    }).execute().data
    if isinstance(response, list) and response and isinstance(response[0], dict): response = response[0]
    if not isinstance(response, dict): raise HTTPException(503, 'Question could not be saved')
    errors = {'exam_not_found':(404,'Exam not found'), 'question_not_found':(404,'Question not found'),
              'configuration_locked':(409,'An exam with attempts can no longer be edited'),
              'invalid_question_config':(422,'Question configuration is invalid')}
    if response.get('error'):
        status, detail = errors.get(response['error'],(400,'Question could not be saved'))
        raise HTTPException(status,detail)
    return response


@router.post('/{exam_id}/questions')
async def create_question(exam_id: UUID, payload: QuestionInput):
    return await _db(lambda: _save_question(_client(), exam_id, None, payload))


@router.patch('/{exam_id}/questions/{question_id}')
async def update_question(exam_id: UUID, question_id: UUID, payload: QuestionInput):
    return await _db(lambda: _save_question(_client(), exam_id, question_id, payload))


@router.delete('/{exam_id}/questions/{question_id}')
async def delete_question(exam_id: UUID, question_id: UUID):
    def delete():
        client = _client()
        response = client.rpc('delete_exam_question_configuration', {
            'p_exam_id':str(exam_id),'p_question_id':str(question_id),
        }).execute().data
        if isinstance(response, list) and response and isinstance(response[0], dict): response=response[0]
        errors = {'exam_not_found':(404,'Exam not found'),'question_not_found':(404,'Question not found'),
                  'configuration_locked':(409,'An exam with attempts can no longer be edited')}
        if isinstance(response,dict) and response.get('error'):
            status,detail=errors.get(response['error'],(400,'Question could not be deleted'))
            raise HTTPException(status,detail)
        return {'deleted':True}
    return await _db(delete)


@router.post('/{exam_id}/questions/reorder')
async def reorder_questions(exam_id: UUID, payload: ReorderInput):
    def write():
        client = _client(); desired=[str(qid) for qid in payload.question_ids]
        response=client.rpc('reorder_exam_questions',{'p_exam_id':str(exam_id),'p_question_ids':desired}).execute().data
        if isinstance(response,list) and response and isinstance(response[0],dict): response=response[0]
        errors={'exam_not_found':(404,'Exam not found'),'configuration_locked':(409,'An exam with attempts can no longer be edited'),
                'invalid_question_order':(422,'question_ids must contain every exam question exactly once')}
        if isinstance(response,dict) and response.get('error'):
            status,detail=errors.get(response['error'],(400,'Question order could not be saved'))
            raise HTTPException(status,detail)
        return response
    return await _db(write)


def _validate_publish(client, exam_id: UUID, exam):
    errors = []
    if not exam.get('title','').strip(): errors.append({'field':'title','message':'Title is required'})
    try: _timestamp(datetime.fromisoformat(exam['scheduled_start_at'].replace('Z','+00:00')), must_be_future=True)
    except (ValueError, AttributeError, HTTPException): errors.append({'field':'scheduled_start_at','message':'Scheduled start must be a valid future time'})
    if not exam.get('duration_minutes') or exam['duration_minutes'] <= 0: errors.append({'field':'duration_minutes','message':'Duration must be positive'})
    if exam.get('allow_list_enabled') and not (client.table('exam_allowed_students').select('id').eq('exam_id', str(exam_id)).limit(1).execute().data or []):
        errors.append({'field':'allowed_students','message':'Add at least one allowed student or switch to open access'})
    questions = client.table('exam_questions').select('*').eq('exam_id', str(exam_id)).order('question_number').execute().data or []
    if not questions: errors.append({'field':'questions','message':'Add at least one question'})
    for q in questions:
        if float(q['max_marks']) <= 0: errors.append({'field':f"question:{q['id']}:max_marks",'message':'Marks must be positive'})
        if q['question_type'] == 'MCQ':
            options = client.table('exam_question_options').select('id,is_correct,option_text').eq('question_id', q['id']).execute().data or []
            if len(options) < 2 or sum(bool(o['is_correct']) for o in options) != 1 or any(not o['option_text'].strip() for o in options):
                errors.append({'field':f"question:{q['id']}:options",'message':'MCQs need at least two non-empty options and exactly one correct answer'})
    if errors: raise HTTPException(422, {'message':'Exam is not ready to schedule','errors':errors})


@router.post('/{exam_id}/schedule')
async def schedule_exam(exam_id: UUID):
    def write():
        client = _client(); exam = _exam_or_404(client, exam_id)
        if exam['status'] != 'DRAFT': raise HTTPException(409, 'Only draft exams can be scheduled')
        _validate_publish(client, exam_id, exam)
        return client.table('exams').update({'status':'SCHEDULED','updated_at':datetime.now(timezone.utc).isoformat()}).eq('id', str(exam_id)).execute().data[0]
    return await _db(write)


@router.post('/{exam_id}/cancel')
async def cancel_exam(exam_id: UUID):
    def write():
        client = _client(); exam = _exam_or_404(client, exam_id)
        if exam['status'] not in ('DRAFT','SCHEDULED'): raise HTTPException(409, 'Only draft or scheduled exams can be cancelled')
        client.table('exams').update({'status':'CANCELLED','updated_at':datetime.now(timezone.utc).isoformat()}).eq('id', str(exam_id)).execute()
        return {'status':'CANCELLED'}
    return await _db(write)


@router.delete('/{exam_id}')
async def delete_exam(exam_id: UUID):
    def delete():
        client = _client()
        result = client.rpc('delete_exam_and_dependents', {'p_exam_id':str(exam_id)}).execute().data
        if isinstance(result, list) and result and isinstance(result[0], dict):
            result = result[0]
        if not isinstance(result, dict):
            raise HTTPException(503, 'Exam deletion could not be completed')
        if result.get('error') == 'exam_not_found':
            raise HTTPException(404, 'Exam not found')
        if result.get('deleted') is not True:
            raise HTTPException(503, 'Exam deletion could not be completed')

        cleanup_complete = True
        cleanup_pending = 0
        for bucket_name, keys in (
            (settings.EXAM_AUDIO_BUCKET, result.get('audio_storage_keys') or []),
            (settings.EXAM_ARTIFACTS_BUCKET, result.get('artifact_storage_keys') or []),
        ):
            if not keys:
                continue
            try:
                client.storage.from_(bucket_name).remove(keys)
            except Exception as exc:
                cleanup_complete = False
                cleanup_pending += len(keys)
                # Keys and credentials are private; only log the failure class.
                print(f'[EXAM-DELETE] private storage cleanup failed bucket={bucket_name} error={type(exc).__name__}')
        return {'deleted':True, 'storage_cleanup_complete':cleanup_complete,
                'storage_cleanup_pending':cleanup_pending}
    return await _db(delete)


@router.get('/{exam_id}/allowed-students')
async def list_allowed_students(exam_id: UUID):
    def read():
        client = _client(); _exam_or_404(client, exam_id)
        return client.table('exam_allowed_students').select('google_sub,display_name,email,created_at').eq('exam_id', str(exam_id)).order('created_at').execute().data or []
    return {'students':await _db(read)}


@router.post('/{exam_id}/allowed-students')
async def add_allowed_student(exam_id: UUID, payload: AllowedStudentInput):
    def write():
        client = _client(); exam = _editable(client, exam_id)
        if not exam['allow_list_enabled']: raise HTTPException(409, 'Enable allowed-student mode before adding students')
        result = client.table('exam_allowed_students').upsert({'exam_id':str(exam_id), **payload.dict()}, on_conflict='exam_id,google_sub').execute().data
        return result[0]
    return await _db(write)


@router.delete('/{exam_id}/allowed-students/{student_sub}')
async def remove_allowed_student(exam_id: UUID, student_sub: str):
    def delete():
        client = _client(); _editable(client, exam_id)
        client.table('exam_allowed_students').delete().eq('exam_id', str(exam_id)).eq('google_sub', student_sub).execute()
        return {'deleted':True}
    return await _db(delete)


@router.get('/{exam_id}/review')
async def review_exam(exam_id: UUID):
    # Management endpoint only; correct-answer data never appears in public/student APIs.
    return await get_exam(exam_id)


@router.get('/{exam_id}/attempts')
async def list_exam_attempts(exam_id: UUID):
    """Teacher-only attempt and response view. Router dependency enforces management auth."""
    def read():
        client = _client()
        exam = _refresh_lifecycle(client, _exam_or_404(client, exam_id))
        questions = client.table('exam_questions').select(
            'id,question_number,question_text,question_type,max_marks,is_required,evaluation_mode'
        ).eq('exam_id', str(exam_id)).order('question_number').execute().data or []
        attempts = client.table('exam_attempts').select(
            'id,google_sub,student_display_name,student_email,status,started_at,expires_at,submitted_at,violation_count'
        ).eq('exam_id', str(exam_id)).order('started_at', desc=True).execute().data or []
        answers = client.table('exam_answers').select(
            'id,attempt_id,question_id,answer_text,selected_option_id,answer_method,saved_at'
        ).eq('exam_id', str(exam_id)).execute().data or []
        audio_rows = client.table('exam_audio_answers').select(
            'attempt_id,question_id,audio_mime_type,duration_ms,transcription_status,transcript,uploaded_at,transcribed_at'
        ).eq('exam_id', str(exam_id)).execute().data or []
        answer_map = {(row['attempt_id'], row['question_id']): row for row in answers}
        audio_map = {(row['attempt_id'], row['question_id']): row for row in audio_rows}
        ai_rows = client.table('exam_ai_evaluations').select(
            'id,answer_id,provider,model,marks,max_marks,confidence,explanation,manual_required,status,last_error,updated_at'
        ).execute().data or []
        ai_by_answer = {}
        for evaluation in ai_rows:
            previous = ai_by_answer.get(evaluation['answer_id'])
            if previous is None or (evaluation.get('updated_at') or '') > (previous.get('updated_at') or ''):
                ai_by_answer[evaluation['answer_id']] = evaluation
        result = []
        for attempt in attempts:
            attempt_id = attempt['id']
            response_questions = []
            for question in questions:
                qid = question['id']
                answer = answer_map.get((attempt_id, qid), {})
                audio = audio_map.get((attempt_id, qid))
                rubric_rows = client.table('exam_question_rubrics').select(
                    'reference_answer,marking_criteria'
                ).eq('question_id', qid).limit(1).execute().data or []
                review_rows = client.table('exam_manual_reviews').select(
                    'id,marks,max_marks,teacher_comments,review_status,updated_at'
                ).eq('answer_id', answer['id']).order('updated_at', desc=True).limit(1).execute().data or [] if answer.get('id') else []
                selected_option = None
                mcq_marks = 0 if question['question_type'] == 'MCQ' else None
                if answer.get('selected_option_id'):
                    options = client.table('exam_question_options').select(
                        'id,option_order,option_text'
                    ).eq('question_id', qid).order('option_order').execute().data or []
                    selected_row = next((option for option in options if option['id'] == answer['selected_option_id']), None)
                    selected_option = ({
                        key: selected_row.get(key) for key in ('id','option_order','option_text')
                    } if selected_row else None)
                    if selected_row and question['question_type'] == 'MCQ' and selected_row.get('is_correct'):
                        mcq_marks = float(question['max_marks'])
                response_questions.append({
                    **question,
                    'reference_answer': rubric_rows[0].get('reference_answer') if rubric_rows else None,
                    'marking_criteria': rubric_rows[0].get('marking_criteria') if rubric_rows else None,
                    'answer_text': answer.get('answer_text'),
                    'answer_method': answer.get('answer_method'),
                    'selected_option': selected_option,
                    'mcq_marks': mcq_marks,
                    'saved_at': answer.get('saved_at'),
                    'audio': ({key: audio.get(key) for key in (
                        'audio_mime_type','duration_ms','transcription_status','transcript','uploaded_at','transcribed_at'
                    )} if audio else None),
                    'manual_review': review_rows[0] if review_rows else None,
                    'ai_evaluation': ai_by_answer.get(answer.get('id')),
                })
            started = attempt.get('started_at')
            submitted = attempt.get('submitted_at')
            completion_seconds = None
            if started and submitted:
                start_dt = datetime.fromisoformat(started.replace('Z', '+00:00'))
                submitted_dt = datetime.fromisoformat(submitted.replace('Z', '+00:00'))
                if start_dt.tzinfo is None: start_dt = start_dt.replace(tzinfo=timezone.utc)
                if submitted_dt.tzinfo is None: submitted_dt = submitted_dt.replace(tzinfo=timezone.utc)
                completion_seconds = max(0, int((submitted_dt - start_dt).total_seconds()))
            result.append({
                'attempt_id': attempt_id,
                'student': {
                    'google_sub': attempt.get('google_sub'),
                    'display_name': attempt.get('student_display_name'),
                    'email': attempt.get('student_email'),
                },
                'status': attempt.get('status'),
                'started_at': started,
                'expires_at': attempt.get('expires_at'),
                'submitted_at': submitted,
                'completion_seconds': completion_seconds,
                'violation_count': attempt.get('violation_count', 0),
                'questions': response_questions,
            })
        return {'exam_status': exam['status'], 'attempts': result}
    return await _db(read)


@router.get('/{exam_id}/attempts/{attempt_id}/questions/{question_id}/audio')
async def teacher_exam_audio(exam_id: UUID, attempt_id: UUID, question_id: UUID):
    """Stream a private student recording to an authenticated Exams manager."""
    def read():
        client = _client()
        attempt = client.table('exam_attempts').select('id,exam_id').eq(
            'id', str(attempt_id)).eq('exam_id', str(exam_id)).limit(1).execute().data or []
        if not attempt: raise HTTPException(404, 'Exam attempt not found')
        audio = client.table('exam_audio_answers').select(
            'storage_key,audio_mime_type'
        ).eq('attempt_id', str(attempt_id)).eq('exam_id', str(exam_id)).eq(
            'question_id', str(question_id)).limit(1).execute().data or []
        if not audio: raise HTTPException(404, 'No saved audio answer was found')
        content = client.storage.from_(settings.EXAM_AUDIO_BUCKET).download(audio[0]['storage_key'])
        return content, audio[0]['audio_mime_type']
    content, mime = await _db(read)
    return Response(content=content, media_type=mime, headers={
        'Cache-Control':'private, no-store', 'X-Content-Type-Options':'nosniff',
        'Content-Disposition':'inline',
    })


@router.put('/{exam_id}/attempts/{attempt_id}/questions/{question_id}/evaluation')
async def save_manual_evaluation(exam_id: UUID, attempt_id: UUID, question_id: UUID,
                                payload: ManualEvaluationInput):
    """Save an editable manual evaluation using the existing review schema."""
    def write():
        client = _client()
        attempt = client.table('exam_attempts').select('id,status').eq(
            'id', str(attempt_id)).eq('exam_id', str(exam_id)).limit(1).execute().data or []
        if not attempt: raise HTTPException(404, 'Exam attempt not found')
        exam = _exam_or_404(client, exam_id)
        if exam.get('status') == 'FINALIZED':
            raise HTTPException(409, 'Finalized exam evaluations are immutable')
        if attempt[0]['status'] not in ('SUBMITTED','AUTO_SUBMITTED','TERMINATED','FINALIZED','MANUAL_REVIEW'):
            raise HTTPException(409, 'Manual evaluation is available after the attempt is submitted')
        question = client.table('exam_questions').select('id,question_type,max_marks').eq(
            'id', str(question_id)).eq('exam_id', str(exam_id)).limit(1).execute().data or []
        if not question: raise HTTPException(404, 'Question not found')
        q = question[0]
        if q['question_type'] != 'TEXT_AUDIO_ANSWER':
            raise HTTPException(409, 'Manual evaluation is available for Text / Audio Answer questions')
        max_marks = float(q['max_marks'])
        if payload.marks > max_marks:
            raise HTTPException(422, {'field':'marks','message':f'Marks cannot exceed {max_marks:g}'})
        answers = client.table('exam_answers').select('id').eq('attempt_id', str(attempt_id)).eq(
            'exam_id', str(exam_id)).eq('question_id', str(question_id)).limit(1).execute().data or []
        if answers:
            answer_id = answers[0]['id']
        else:
            # A teacher can mark an unanswered question as zero without inventing a student response.
            answer = client.table('exam_answers').insert({
                'attempt_id':str(attempt_id),'exam_id':str(exam_id),'question_id':str(question_id),
                'answer_method':'TEXT','answer_text':None,'selected_option_id':None,
                'answer_version':1,'saved_at':datetime.now(timezone.utc).isoformat(),
            }).execute().data or []
            if not answer: raise HTTPException(503, 'The unanswered question could not be prepared for review')
            answer_id = answer[0]['id']
        latest = client.table('exam_manual_reviews').select('id,review_status').eq(
            'answer_id', str(answer_id)).order('updated_at', desc=True).limit(1).execute().data or []
        values = {
            'answer_id':str(answer_id),'reviewer_session_id':None,'marks':payload.marks,
            'max_marks':max_marks,'teacher_comments':payload.teacher_feedback,
            'review_status':'SUBMITTED','updated_at':datetime.now(timezone.utc).isoformat(),
        }
        if latest and latest[0]['review_status'] in ('DRAFT','SUBMITTED'):
            row = client.table('exam_manual_reviews').update(values).eq('id',latest[0]['id']).execute().data or []
        else:
            values['created_at'] = values['updated_at']
            row = client.table('exam_manual_reviews').insert(values).execute().data or []
        if not row: raise HTTPException(503, 'Manual evaluation could not be saved')
        return {'manual_review':{key:row[0].get(key) for key in (
            'id','marks','max_marks','teacher_comments','review_status','updated_at'
        )}}
    return await _db(write)


def _finalized_results(client, exam_id: UUID):
    exam = _exam_or_404(client, exam_id)
    if exam.get('status') != 'FINALIZED':
        raise HTTPException(409, 'Finalize the exam before viewing results or cards')
    rows = client.table('exam_results').select('*').eq('exam_id', str(exam_id)).order('rank').execute().data or []
    attempts = client.table('exam_attempts').select('id,student_display_name,student_email,submitted_at').eq(
        'exam_id', str(exam_id)).execute().data or []
    by_id = {row['id']: row for row in attempts}
    return exam, [{**row, 'student': {
        'display_name': by_id.get(row['attempt_id'], {}).get('student_display_name'),
        'email': by_id.get(row['attempt_id'], {}).get('student_email'),
        'submitted_at': by_id.get(row['attempt_id'], {}).get('submitted_at'),
    }} for row in rows]


@router.post('/{exam_id}/finalize')
async def finalize_exam(exam_id: UUID):
    def finalize():
        client = _client()
        exam = _refresh_lifecycle(client, _exam_or_404(client, exam_id))
        result = client.rpc('finalize_exam_results', {
            'p_exam_id': str(exam_id),
            'p_confidence_threshold': settings.EXAM_AI_CONFIDENCE_THRESHOLD,
        }).execute().data
        if isinstance(result, list) and result and isinstance(result[0], dict): result = result[0]
        if not isinstance(result, dict): raise HTTPException(503, 'Exam finalization could not be completed')
        errors = {
            'exam_not_found': (404, 'Exam not found'),
            'exam_not_closed': (409, 'Exam must be closed before finalization'),
            'active_attempts': (409, 'Wait for all active attempts to submit before finalizing'),
            'unresolved_evaluations': (409, 'Required answers still need teacher evaluation'),
        }
        if result.get('error'):
            status, message = errors.get(result['error'], (409, 'Exam could not be finalized'))
            raise HTTPException(status, {'message': message, 'unresolved_count': result.get('count')})
        artifacts = None
        artifact_error = None
        try:
            artifacts = exam_artifacts.persist_exam_artifacts(client, str(exam_id))
        except Exception as exc:
            artifact_error = type(exc).__name__
        _, rows = _finalized_results(client, exam_id)
        return {'status': 'FINALIZED', 'results': rows, 'artifacts': artifacts,
                'artifacts_error': artifact_error}
    return await _db(finalize)


@router.get('/{exam_id}/results')
async def get_exam_results(exam_id: UUID):
    return await _db(lambda: {'results': _finalized_results(_client(), exam_id)[1]})


@router.post('/{exam_id}/artifacts/refresh')
async def refresh_exam_artifacts(exam_id: UUID):
    def refresh():
        client = _client(); _finalized_results(client, exam_id)
        try:
            return exam_artifacts.persist_exam_artifacts(client, str(exam_id))
        except Exception as exc:
            raise HTTPException(503, 'Final grade cards could not be generated') from exc
    return await _db(refresh)


@router.get('/{exam_id}/artifacts/grade-cards.zip')
async def download_exam_grade_cards(exam_id: UUID):
    def build():
        client = _client(); _finalized_results(client, exam_id)
        try:
            exam_artifacts.persist_exam_artifacts(client, str(exam_id))
            return exam_artifacts.download_grade_cards_zip(client, str(exam_id))
        except Exception as exc:
            raise HTTPException(503, 'Grade cards are temporarily unavailable') from exc
    content = await _db(build)
    return Response(content, media_type='application/zip', headers={
        'Content-Disposition': f'attachment; filename="exam-{exam_id}-grade-cards.zip"',
        'Cache-Control': 'private, no-store',
    })


@router.get('/{exam_id}/artifacts/grade-cards/{result_id}')
async def get_exam_grade_card(exam_id: UUID, result_id: UUID, download: bool = False):
    def read():
        client = _client()
        _finalized_results(client, exam_id)
        result = client.table('exam_results').select('id').eq(
            'id', str(result_id)).eq('exam_id', str(exam_id)).limit(1).execute().data or []
        if not result:
            raise HTTPException(404, 'Grade card not found')
        try:
            exam_artifacts.persist_exam_artifacts(client, str(exam_id))
            row = client.table('exam_artifacts').select('storage_key').eq('exam_id', str(exam_id)).eq(
                'result_id', str(result_id)).eq('artifact_type', 'GRADE_CARD').eq(
                'status', 'READY').limit(1).execute().data or []
            if not row:
                raise HTTPException(404, 'Grade card is not available')
            return client.storage.from_(settings.EXAM_ARTIFACTS_BUCKET).download(row[0]['storage_key'])
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, 'Grade card is temporarily unavailable') from exc
    content = await _db(read)
    disposition = 'attachment' if download else 'inline'
    return Response(content, media_type='application/pdf', headers={
        'Content-Disposition': f'{disposition}; filename="exam-{exam_id}-result-{result_id}.pdf"',
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
    })


@router.get('/{exam_id}/artifacts/rank-card')
async def get_exam_rank_card(exam_id: UUID, download: bool = False):
    def read():
        client = _client(); _finalized_results(client, exam_id)
        try:
            exam_artifacts.persist_exam_artifacts(client, str(exam_id))
            row = client.table('exam_artifacts').select('storage_key').eq('exam_id', str(exam_id)).eq(
                'artifact_type', 'RANK_CARD').eq('status', 'READY').limit(1).execute().data or []
            if not row: raise HTTPException(404, 'Final rank card is not available')
            return client.storage.from_(settings.EXAM_ARTIFACTS_BUCKET).download(row[0]['storage_key'])
        except HTTPException: raise
        except Exception as exc: raise HTTPException(503, 'Final rank card is temporarily unavailable') from exc
    content = await _db(read)
    disposition = 'attachment' if download else 'inline'
    return Response(content, media_type='application/pdf', headers={
        'Content-Disposition': f'{disposition}; filename="exam-{exam_id}-rank-card.pdf"',
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
    })


def _queue_whatsapp_jobs(client, exam_id: UUID, job_type: str):
    _, results = _finalized_results(client, exam_id)
    artifacts = client.table('exam_artifacts').select('id,result_id,artifact_type,status').eq(
        'exam_id', str(exam_id)).eq('status', 'READY').execute().data or []
    destination = passcode_service.get_exam_whatsapp_number()
    if job_type == 'GRADE_CARD':
        artifact_by_result = {row['result_id']: row for row in artifacts if row['artifact_type'] == 'GRADE_CARD'}
        items = [(row, artifact_by_result.get(row['id'])) for row in results]
    else:
        rank = next((row for row in artifacts if row['artifact_type'] == 'RANK_CARD'), None)
        items = [(None, rank)]
    queued = []
    for result, artifact in items:
        if not artifact:
            continue
        values = {
            'result_id': result['id'] if result else None,
            'artifact_id': artifact['id'], 'destination_type': 'WHATSAPP',
            'destination_ref': destination, 'job_type': f'MANUAL_{job_type}_WHATSAPP',
            'status': 'PENDING', 'attempts': 0,
            'scheduled_at': datetime.now(timezone.utc).isoformat(),
            'created_at': datetime.now(timezone.utc).isoformat(),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        created = client.table('exam_notification_jobs').insert(values).execute().data or []
        if created: queued.append(created[0])
    return queued


@router.post('/{exam_id}/notifications/grade-cards')
async def send_grade_cards_whatsapp(exam_id: UUID):
    def queue():
        client = _client(); _finalized_results(client, exam_id)
        exam_artifacts.persist_exam_artifacts(client, str(exam_id))
        return _queue_whatsapp_jobs(client, exam_id, 'GRADE_CARD')
    jobs = await _db(queue)
    return {'status': 'SENDING' if jobs else 'FAILED', 'provider_configured': False,
            'message': 'Queued for delivery status check.' if jobs else 'No finalized grade cards are available.' if not settings.EXAM_WHATSAPP_PROVIDER else 'No supported WhatsApp provider adapter is installed.',
            'jobs': jobs}


@router.post('/{exam_id}/notifications/rank-card')
async def send_rank_card_whatsapp(exam_id: UUID):
    def queue():
        client = _client(); _finalized_results(client, exam_id)
        exam_artifacts.persist_exam_artifacts(client, str(exam_id))
        return _queue_whatsapp_jobs(client, exam_id, 'RANK_CARD')
    jobs = await _db(queue)
    return {'status': 'SENDING' if jobs else 'FAILED', 'provider_configured': False,
            'message': 'Queued for delivery status check.' if jobs else 'Final rank card is unavailable.',
            'jobs': jobs}


@router.get('/{exam_id}/notifications')
async def get_exam_notification_status(exam_id: UUID):
    def read():
        client = _client(); _, results = _finalized_results(client, exam_id)
        jobs = []
        for result in results:
            jobs.extend(client.table('exam_notification_jobs').select(
                'id,job_type,status,attempts,last_error,created_at,completed_at'
            ).eq('result_id', result['id']).order('created_at', desc=True).execute().data or [])
        artifacts = client.table('exam_artifacts').select('id').eq('exam_id', str(exam_id)).eq(
            'artifact_type', 'RANK_CARD').execute().data or []
        for artifact in artifacts:
            jobs.extend(client.table('exam_notification_jobs').select(
                'id,job_type,status,attempts,last_error,created_at,completed_at'
            ).eq('artifact_id', artifact['id']).order('created_at', desc=True).execute().data or [])
        return {'jobs': jobs, 'provider_configured': False}
    return await _db(read)
