"""Google-authenticated student access and attempt APIs for Exams."""

import asyncio
import hashlib
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from .config import settings

router = APIRouter(prefix='/api/exams/student', tags=['Student Exams'])

SYSTEM_EXAM_INSTRUCTIONS = """EXAM INSTRUCTIONS

BEFORE STARTING
• Read all instructions carefully before starting the exam.
• The questions will be available after you agree to start the exam.
• Once the exam starts, the timer will run according to the scheduled exam time.
• If you join late, you will receive only the remaining time.
• The server controls the official exam time.

EXAM SECURITY
• Attempt the exam from the protected exam screen. Fullscreen/protected mode may be requested after you agree and start.
• Leaving the protected exam screen may be recorded as a violation. Tab/window visibility changes and focus loss may be detected where supported by the browser.
• Copy and paste may be restricted during the exam. Text selection and context-menu actions may be restricted where supported.
• Navigation, reload, and attempts to leave the exam are handled according to the exam security rules.
• Mobile orientation changes may be detected where supported. Multiple active exam sessions may be prevented or detected.
• Browser security is best effort. The system cannot control the operating system, other devices, or activity outside the browser.

ANSWER SAVING
• Answers are automatically saved. MCQ selections and text answers autosave; audio answers are uploaded and saved securely.
• If the page is refreshed, the existing attempt should resume where permitted. Previously saved answers should not be lost because of a normal refresh or temporary network interruption.

SUBMISSION
• You may submit the exam before the timer expires. When the official exam time expires, the exam will be automatically submitted.
• After submission, answers cannot be changed. Reaching the configured maximum security violations may terminate or auto-submit the attempt according to exam settings.

IMPORTANT
• The server is authoritative for exam timing, attempts, answers, and submission.
• Do not close the exam until you have submitted or the exam has automatically submitted."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


async def _student(response: Response, authorization: str | None = Header(default=None)) -> dict[str, str]:
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Sign in with Google to continue')
    credential = authorization[7:]
    # Reuse the exact GIS ID-token audience/signature verification used by class joins.
    from .main import verify_google_credential
    return await asyncio.to_thread(verify_google_credential, credential)


def _client():
    from supabase import create_client
    if not settings.validate_supabase_db():
        raise HTTPException(503, 'Exam service is unavailable')
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)


async def _run(operation):
    try:
        return await asyncio.to_thread(operation)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, 'Exam service is temporarily unavailable') from exc


def _get_exam(client, exam_token: str):
    token_hash = _digest(exam_token)
    rows = client.table('exams').select(
        'id,title,description,instructions,custom_instructions_enabled,custom_instructions,scheduled_start_at,duration_minutes,status,allow_list_enabled,public_token_hash,protected_mode_enabled,require_fullscreen,detect_visibility_change,detect_orientation_change,restrict_copy_paste,autosave_enabled,automatic_submission_enabled,maximum_violations'
    ).eq('public_token_hash', token_hash).limit(1).execute().data or []
    if not rows:
        raise HTTPException(404, 'Exam link is invalid or no longer available')
    return rows[0], token_hash


def _status(exam: dict, now: datetime):
    if exam['status'] == 'CANCELLED':
        return 'CANCELLED', 'This exam has been cancelled.'
    if exam['status'] == 'DRAFT':
        return 'UNAVAILABLE', 'This exam is not open for student access.'
    if exam['status'] == 'SUBMISSION_CLOSED':
        return 'ENDED', 'This exam is closed. Existing attempts follow their server-set timer.'
    if exam['status'] not in ('SCHEDULED', 'ACTIVE'):
        return 'UNAVAILABLE', 'This exam is not open for student access.'
    start = datetime.fromisoformat(exam['scheduled_start_at'].replace('Z', '+00:00'))
    if start.tzinfo is None or start.utcoffset() is None:
        start = start.replace(tzinfo=timezone.utc)
    end = start.timestamp() + int(exam['duration_minutes']) * 60
    if now < start:
        return 'NOT_STARTED', 'Exam has not started yet.'
    if now.timestamp() >= end:
        return 'ENDED', 'Exam has ended.'
    return 'AVAILABLE', None


def _rpc_data(client, name: str, params: dict):
    result = client.rpc(name, params).execute()
    data = result.data
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise RuntimeError('Unexpected exam RPC response')
    return data


def _raise_rpc_error(error: str):
    responses = {
        'exam_not_found': (404, 'Exam link is invalid or no longer available'),
        'exam_unavailable': (410, 'This exam is not open for student access'),
        'not_started': (425, 'Exam has not started yet'),
        'exam_ended': (410, 'Exam has ended'),
        'not_eligible': (403, 'You are not authorized to take this exam'),
        'audio_unsupported': (409, 'Audio answers will be available in a later update'),
        'invalid_answer_method': (422, 'Choose a valid answer method for this question'),
        'audio_answer_missing': (409, 'Save an audio recording before selecting audio as your answer'),
        'text_answer_missing': (409, 'Enter and save a typed answer before selecting both methods'),
        'invalid_violation_type': (422, 'Unsupported exam security event'),
        'transcription_failed': (502, 'Audio transcription failed. Your previous saved answer is unchanged; retry with this or another recording.'),
        'invalid_exam_configuration': (409, 'This exam is not correctly configured for student access'),
        'attempt_not_found': (404, 'Exam attempt not found'),
        'attempt_expired': (410, 'Exam time has expired; your attempt was submitted automatically'),
        'attempt_submitted': (409, 'This exam attempt has already been submitted'),
        'question_not_found': (404, 'Question does not belong to this exam'),
        'invalid_answer': (422, 'Answer does not match the question type'),
        'unsupported_question': (409, 'This question type is not available yet'),
    }
    status, detail = responses.get(error, (400, 'The exam request could not be completed'))
    raise HTTPException(status, detail)


def _safe_attempt(attempt: dict):
    return {key:attempt.get(key) for key in ('status','started_at','expires_at','submitted_at','violation_count')}


def _public_exam_payload(exam: dict):
    return {
        'title': exam['title'], 'description': exam.get('description'),
        'scheduled_start_at': exam.get('scheduled_start_at'),
        'duration_minutes': exam['duration_minutes'], 'status': exam['status'],
    }


@router.get('/{exam_token}')
async def public_exam_info(exam_token: str, response: Response):
    """Unauthenticated preview contains only identifying and schedule details."""
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    def read():
        exam, _ = _get_exam(_client(), exam_token)
        state, reason = _status(exam, datetime.now(timezone.utc))
        return {**_public_exam_payload(exam), 'availability': state, 'message': reason}
    return await _run(read)


@router.post('/{exam_token}/access')
async def authenticated_exam_access(exam_token: str, student=Depends(_student)):
    """Return instructions/eligibility without question content or answer keys."""
    def read():
        client = _client(); exam, _ = _get_exam(client, exam_token)
        state, reason = _status(exam, datetime.now(timezone.utc))
        if state == 'AVAILABLE' and exam['status'] == 'SCHEDULED':
            any_attempt = client.table('exam_attempts').select('id').eq('exam_id', exam['id']).limit(1).execute().data or []
            if not any_attempt:
                client.table('exams').update({
                    'status':'ACTIVE','updated_at':datetime.now(timezone.utc).isoformat(),
                }).eq('id',exam['id']).eq('status','SCHEDULED').execute()
                exam['status']='ACTIVE'
        eligible = True
        if exam['allow_list_enabled']:
            eligible = bool(client.table('exam_allowed_students').select('id').eq('exam_id', exam['id'])
                            .eq('google_sub', student['sub']).limit(1).execute().data or [])
            if not eligible: reason = 'You are not authorized to take this exam.'
        previous = client.table('exam_attempts').select('status,expires_at,submitted_at').eq(
            'exam_id', exam['id']).eq('google_sub', student['sub']).limit(1).execute().data or []
        questions = client.table('exam_questions').select('id,question_type,max_marks').eq('exam_id', exam['id']).execute().data or []
        return {
            **_public_exam_payload(exam), 'system_instructions': SYSTEM_EXAM_INSTRUCTIONS,
            'custom_instructions_enabled': bool(exam.get('custom_instructions_enabled')),
            'custom_instructions': exam.get('custom_instructions') if exam.get('custom_instructions_enabled') else None,
            'availability': state, 'message': reason, 'eligible': eligible,
            'question_count': len(questions), 'total_marks': sum(float(q['max_marks']) for q in questions),
            'audio_supported': True,
            'existing_attempt': previous[0] if previous else None,
            'student_display_name': student['name'],
            'protected_mode_enabled': bool(exam.get('protected_mode_enabled')),
            'require_fullscreen': bool(exam.get('require_fullscreen')),
            'detect_visibility_change': bool(exam.get('detect_visibility_change')),
            'detect_orientation_change': bool(exam.get('detect_orientation_change')),
            'restrict_copy_paste': bool(exam.get('restrict_copy_paste')),
            'autosave_enabled': bool(exam.get('autosave_enabled', True)),
            'automatic_submission_enabled': bool(exam.get('automatic_submission_enabled', True)),
            'maximum_violations': int(exam.get('maximum_violations') or 0),
        }
    return await _run(read)


@router.post('/{exam_token}/attempt')
async def start_or_resume_attempt(exam_token: str, student=Depends(_student)):
    raw_attempt_token = secrets.token_urlsafe(32)
    def start():
        client = _client(); _, exam_token_hash = _get_exam(client, exam_token)
        response = _rpc_data(client, 'start_student_exam_attempt', {
            'p_exam_token_hash': exam_token_hash,
            'p_google_sub': student['sub'],
            'p_student_display_name': student['name'],
            'p_attempt_token_hash': _digest(raw_attempt_token),
        })
        if response.get('error'): _raise_rpc_error(response['error'])
        attempt = response.get('attempt')
        if not isinstance(attempt, dict): raise RuntimeError('Attempt response is incomplete')
        attempt.pop('attempt_token_hash', None)
        return {**response, 'attempt': _safe_attempt(attempt), 'attempt_token': raw_attempt_token}
    return await _run(start)


@router.get('/{exam_token}/attempt')
async def resume_attempt(exam_token: str, attempt_token: str = Header(alias='X-Exam-Attempt-Token'), student=Depends(_student)):
    return await _run(lambda: _resume_sync(exam_token, attempt_token, student))


def _resume_sync(exam_token: str, attempt_token: str, student: dict):
    client = _client(); _, exam_token_hash = _get_exam(client, exam_token)
    data = _rpc_data(client, 'get_student_exam_attempt', {
        'p_exam_token_hash': exam_token_hash, 'p_google_sub': student['sub'],
        'p_attempt_token_hash': _digest(attempt_token),
    })
    if data.get('error'): _raise_rpc_error(data['error'])
    attempt = data.get('attempt')
    if not isinstance(attempt, dict): raise RuntimeError('Attempt response is incomplete')
    attempt.pop('attempt_token_hash', None)
    questions = client.table('exam_questions').select('id,question_number,question_text,question_type,max_marks,is_required').eq('exam_id', attempt['exam_id']).order('question_number').execute().data or []
    safe_questions = []
    for question in questions:
        if question['question_type'] == 'AUDIO_ANSWER': continue
        item = {key: question[key] for key in ('id','question_number','question_text','question_type','max_marks','is_required')}
        item['options'] = client.table('exam_question_options').select('id,option_order,option_text').eq('question_id', question['id']).order('option_order').execute().data or [] if question['question_type'] == 'MCQ' else []
        safe_questions.append(item)
    answers = client.table('exam_answers').select('question_id,answer_text,selected_option_id,answer_method,answer_version,saved_at').eq('attempt_id', attempt['id']).execute().data or []
    audio_answers = client.table('exam_audio_answers').select('question_id,audio_mime_type,duration_ms,transcription_status,transcript').eq('attempt_id', attempt['id']).execute().data or []
    return {'attempt':_safe_attempt(attempt),'server_time':data['server_time'],'remaining_seconds':data['remaining_seconds'],'questions':safe_questions,'answers':answers,'audio_answers':audio_answers}


class AnswerSave(BaseModel):
    question_id: UUID
    answer_text: str | None = Field(default=None, max_length=20000)
    selected_option_id: UUID | None = None
    answer_method: str | None = None


@router.put('/{exam_token}/attempt/answers')
async def save_answer(exam_token: str, payload: AnswerSave,
                      attempt_token: str = Header(alias='X-Exam-Attempt-Token'), student=Depends(_student)):
    def save():
        client = _client(); _, exam_token_hash = _get_exam(client, exam_token)
        data = _rpc_data(client, 'save_student_exam_answer', {
            'p_exam_token_hash': exam_token_hash, 'p_google_sub': student['sub'],
            'p_attempt_token_hash': _digest(attempt_token), 'p_question_id': str(payload.question_id),
            'p_answer_text': payload.answer_text, 'p_selected_option_id': str(payload.selected_option_id) if payload.selected_option_id else None,
        })
        if data.get('error'): _raise_rpc_error(data['error'])
        if payload.answer_method:
            method = _rpc_data(client, 'set_student_exam_answer_method', {
                'p_exam_token_hash':exam_token_hash,'p_google_sub':student['sub'],
                'p_attempt_token_hash':_digest(attempt_token),'p_question_id':str(payload.question_id),
                'p_answer_method':payload.answer_method,
            })
            if method.get('error'): _raise_rpc_error(method['error'])
        return data
    return await _run(save)


MAX_EXAM_AUDIO_BYTES = 15 * 1024 * 1024
ALLOWED_EXAM_AUDIO_TYPES = {'audio/webm','audio/ogg','audio/mp4','audio/mpeg','audio/wav','audio/x-wav'}
EXAM_AUDIO_SUFFIX = {'audio/webm':'.webm','audio/ogg':'.ogg','audio/mp4':'.m4a',
                     'audio/mpeg':'.mp3','audio/wav':'.wav','audio/x-wav':'.wav'}


@router.put('/{exam_token}/attempt/questions/{question_id}/audio')
async def upload_exam_audio(exam_token: str, question_id: UUID,
    file: UploadFile = File(...), duration_ms: int | None = Form(default=None),
    answer_method: str = Form(default='AUDIO'),
    attempt_token: str = Header(alias='X-Exam-Attempt-Token'), student=Depends(_student)):
    mime = (file.content_type or '').lower().split(';',1)[0].strip()
    if mime not in ALLOWED_EXAM_AUDIO_TYPES:
        raise HTTPException(415, 'Use a supported audio recording format (WebM, Ogg, MP4, MP3, or WAV).')
    contents = await file.read(MAX_EXAM_AUDIO_BYTES + 1)
    if not contents or len(contents) > MAX_EXAM_AUDIO_BYTES:
        raise HTTPException(413, 'Audio recording must be smaller than 15 MB.')
    if duration_ms is not None and not 0 < duration_ms <= 180000:
        raise HTTPException(422, 'Audio recording must be 3 minutes or shorter.')
    if answer_method not in ('AUDIO','BOTH'):
        raise HTTPException(422, 'Audio recording must be selected as Audio or Both.')

    from .sarvam_stt import SarvamBatchSTTProvider
    provider = SarvamBatchSTTProvider()
    if not provider.validate_configuration():
        raise HTTPException(503, 'Audio transcription is not configured. Ask the exam administrator to set SARVAM_API_KEY.')

    async def store_and_transcribe():
        client = _client(); _, token_hash = _get_exam(client, exam_token)
        # Validate ownership and active server timer before sending audio to STT.
        attempt_data = _rpc_data(client, 'get_student_exam_attempt', {
            'p_exam_token_hash':token_hash,'p_google_sub':student['sub'],
            'p_attempt_token_hash':_digest(attempt_token),
        })
        if attempt_data.get('error'): _raise_rpc_error(attempt_data['error'])
        attempt = attempt_data.get('attempt') or {}
        questions = client.table('exam_questions').select('question_type').eq('id',str(question_id)).eq('exam_id',attempt.get('exam_id')).limit(1).execute().data or []
        if not questions or questions[0]['question_type']!='TEXT_AUDIO_ANSWER':
            raise HTTPException(422, 'Audio answers are available only for Text / Audio Answer questions.')
        with tempfile.TemporaryDirectory(prefix='exam-audio-') as temp_dir:
            audio_path = Path(temp_dir) / f"answer{EXAM_AUDIO_SUFFIX[mime]}"
            audio_path.write_bytes(contents)
            try:
                transcript = await provider.transcribe(audio_path, {'language':'auto'})
            except Exception as exc:
                raise HTTPException(502, 'Audio transcription failed. Your typed answer and previously saved audio are unchanged; retry this recording.') from exc
        object_key = f"{attempt['id']}/{question_id}/{secrets.token_hex(16)}"
        storage = client.storage.from_(settings.EXAM_AUDIO_BUCKET)
        uploaded = False
        try:
            storage.upload(object_key, contents, {'content-type':mime,'upsert':'false'})
            uploaded = True
            result = _rpc_data(client, 'save_student_exam_audio_answer', {
                'p_exam_token_hash':token_hash,'p_google_sub':student['sub'],
                'p_attempt_token_hash':_digest(attempt_token),'p_question_id':str(question_id),
                'p_storage_key':object_key,'p_audio_mime_type':mime,'p_file_size_bytes':len(contents),
                'p_duration_ms':duration_ms,'p_transcript':transcript.text,
                'p_language_code':transcript.language,'p_answer_method':answer_method,
            })
            if result.get('error'): _raise_rpc_error(result['error'])
        except Exception:
            if uploaded:
                try: storage.remove([object_key])
                except Exception: pass
            raise
        old_key=result.get('old_storage_key')
        if old_key and old_key!=object_key:
            try: storage.remove([old_key])
            except Exception: pass
        return {key:result.get(key) for key in ('question_id','answer_method','transcript','transcription_status','duration_ms')}

    try:
        result = await store_and_transcribe()
        return result
    finally:
        await file.close()


@router.get('/{exam_token}/attempt/questions/{question_id}/audio')
async def get_exam_audio(exam_token: str, question_id: UUID,
    response: Response, attempt_token: str = Header(alias='X-Exam-Attempt-Token'),
    student=Depends(_student)):
    def read_audio():
        client=_client(); _, token_hash=_get_exam(client,exam_token)
        data=_rpc_data(client,'get_student_exam_attempt',{
            'p_exam_token_hash':token_hash,'p_google_sub':student['sub'],
            'p_attempt_token_hash':_digest(attempt_token),
        })
        if data.get('error'): _raise_rpc_error(data['error'])
        attempt=data['attempt']
        rows=client.table('exam_audio_answers').select('storage_key,audio_mime_type').eq('attempt_id',attempt['id']).eq('question_id',str(question_id)).limit(1).execute().data or []
        if not rows: raise HTTPException(404,'No saved audio answer was found.')
        content=client.storage.from_(settings.EXAM_AUDIO_BUCKET).download(rows[0]['storage_key'])
        return content,rows[0]['audio_mime_type']
    content,mime=await _run(read_audio)
    response.headers['Cache-Control']='private, no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    return Response(content=content,media_type=mime,headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})


class AudioDelete(BaseModel):
    question_id: UUID


class ViolationInput(BaseModel):
    violation_type: str


@router.post('/{exam_token}/attempt/violations')
async def record_violation(exam_token: str, payload: ViolationInput,
    attempt_token: str = Header(alias='X-Exam-Attempt-Token'), student=Depends(_student)):
    def write():
        client=_client(); _,token_hash=_get_exam(client,exam_token)
        result=_rpc_data(client,'record_student_exam_violation',{
            'p_exam_token_hash':token_hash,'p_google_sub':student['sub'],
            'p_attempt_token_hash':_digest(attempt_token),'p_violation_type':payload.violation_type,
            'p_metadata':{},
        })
        if result.get('error'): _raise_rpc_error(result['error'])
        return result
    return await _run(write)


@router.post('/{exam_token}/attempt/heartbeat')
async def heartbeat(exam_token: str, attempt_token: str = Header(alias='X-Exam-Attempt-Token'),
    student=Depends(_student)):
    def beat():
        client=_client(); _,token_hash=_get_exam(client,exam_token)
        result=_rpc_data(client,'heartbeat_student_exam_attempt',{
            'p_exam_token_hash':token_hash,'p_google_sub':student['sub'],
            'p_attempt_token_hash':_digest(attempt_token),
        })
        if result.get('error'): _raise_rpc_error(result['error'])
        return result
    return await _run(beat)


@router.delete('/{exam_token}/attempt/audio')
async def delete_exam_audio(exam_token: str, payload: AudioDelete,
    attempt_token: str = Header(alias='X-Exam-Attempt-Token'),student=Depends(_student)):
    def delete():
        client=_client(); _,token_hash=_get_exam(client,exam_token)
        result=_rpc_data(client,'delete_student_exam_audio_answer',{
            'p_exam_token_hash':token_hash,'p_google_sub':student['sub'],
            'p_attempt_token_hash':_digest(attempt_token),'p_question_id':str(payload.question_id)})
        if result.get('error'): _raise_rpc_error(result['error'])
        key=result.get('storage_key')
        if key:
            try: client.storage.from_(settings.EXAM_AUDIO_BUCKET).remove([key])
            except Exception: raise HTTPException(503,'The audio answer could not be removed. Try again.')
        return {'deleted':result.get('deleted',False)}
    return await _run(delete)


@router.post('/{exam_token}/attempt/submit')
async def submit_attempt(exam_token: str, attempt_token: str = Header(alias='X-Exam-Attempt-Token'), student=Depends(_student)):
    def submit():
        client = _client(); _, exam_token_hash = _get_exam(client, exam_token)
        data = _rpc_data(client, 'submit_student_exam_attempt', {
            'p_exam_token_hash': exam_token_hash, 'p_google_sub': student['sub'],
            'p_attempt_token_hash': _digest(attempt_token),
        })
        if data.get('error'): _raise_rpc_error(data['error'])
        attempt = data.get('attempt')
        if not isinstance(attempt, dict): raise RuntimeError('Attempt response is incomplete')
        attempt.pop('attempt_token_hash', None)
        return {'attempt':_safe_attempt(attempt), 'server_time':data['server_time'],
                'status':attempt['status'], 'submitted_at':attempt['submitted_at']}
    return await _run(submit)
