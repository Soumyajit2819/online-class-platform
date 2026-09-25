import os
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import exams, main
from app.config import settings
from app.exam_auth import create_exam_management_token


class Query:
    def __init__(self, table, storage):
        self.table_name, self.storage = table, storage
        self.operation, self.values, self.filters = 'select', None, []

    def select(self, *_args): return self
    def insert(self, values): self.operation, self.values = 'insert', values; return self
    def update(self, values): self.operation, self.values = 'update', values; return self
    def delete(self): self.operation = 'delete'; return self
    def eq(self, key, value): self.filters.append((key, str(value))); return self
    def order(self, *_args, **_kwargs): return self
    def limit(self, *_args): return self
    def execute(self):
        rows = self.storage.setdefault(self.table_name, [])
        matched = [row for row in rows if all(str(row.get(key)) == value for key, value in self.filters)]
        if self.operation == 'insert':
            values = self.values if isinstance(self.values, list) else [self.values]
            inserted = []
            for value in values:
                row = dict(value); row.setdefault('id', str(uuid4())); rows.append(row); inserted.append(dict(row))
            return type('Result', (), {'data': inserted})()
        if self.operation == 'update':
            for row in matched: row.update(self.values)
            return type('Result', (), {'data': [dict(row) for row in matched]})()
        if self.operation == 'delete':
            self.storage[self.table_name] = [row for row in rows if row not in matched]
            return type('Result', (), {'data': matched})()
        return type('Result', (), {'data': [dict(row) for row in matched]})()


class FakeSupabase:
    def __init__(self): self.storage = FakeStorage(self); self.storage_objects = {}
    def table(self, name): return Query(name, self.storage)
    def rpc(self, name, params): return FakeRpc(self, name, params)


class FakeStorage(dict):
    def __init__(self, db): super().__init__(); self.db, self.bucket = db, None
    def from_(self, bucket): self.bucket = bucket; return self
    def remove(self, keys):
        objects = self.db.storage_objects.setdefault(self.bucket, set())
        for key in keys: objects.discard(key)
        return {'data': []}


class FakeRpc:
    def __init__(self, db, name, params): self.db, self.name, self.params = db, name, params
    def execute(self):
        if self.name != 'delete_exam_and_dependents': raise AssertionError(self.name)
        exam_id = self.params['p_exam_id']
        tables = self.db.storage
        if not any(row.get('id') == exam_id for row in tables.get('exams', [])):
            return type('Result', (), {'data': {'error':'exam_not_found'}})()
        attempts = {row['id'] for row in tables.get('exam_attempts', []) if row.get('exam_id') == exam_id}
        questions = {row['id'] for row in tables.get('exam_questions', []) if row.get('exam_id') == exam_id}
        answers = {row['id'] for row in tables.get('exam_answers', []) if row.get('exam_id') == exam_id}
        results = {row['id'] for row in tables.get('exam_results', []) if row.get('exam_id') == exam_id}
        artifacts = {row['id'] for row in tables.get('exam_artifacts', []) if row.get('exam_id') == exam_id}
        audio_keys = [row['storage_key'] for row in tables.get('exam_audio_answers', []) if row.get('exam_id') == exam_id]
        artifact_keys = [row['storage_key'] for row in tables.get('exam_artifacts', []) if row.get('exam_id') == exam_id]
        related = {
            'exam_notification_jobs': lambda row: row.get('result_id') in results or row.get('artifact_id') in artifacts,
            'exam_artifacts': lambda row: row.get('exam_id') == exam_id,
            'exam_results': lambda row: row.get('exam_id') == exam_id,
            'exam_ai_evaluations': lambda row: row.get('answer_id') in answers,
            'exam_manual_reviews': lambda row: row.get('answer_id') in answers,
            'exam_audio_answers': lambda row: row.get('exam_id') == exam_id,
            'exam_answers': lambda row: row.get('exam_id') == exam_id,
            'exam_violations': lambda row: row.get('attempt_id') in attempts,
            'exam_attempt_sessions': lambda row: row.get('attempt_id') in attempts,
            'exam_attempts': lambda row: row.get('exam_id') == exam_id,
            'exam_allowed_students': lambda row: row.get('exam_id') == exam_id,
            'exam_question_rubrics': lambda row: row.get('question_id') in questions,
            'exam_question_options': lambda row: row.get('question_id') in questions,
            'exam_questions': lambda row: row.get('exam_id') == exam_id,
            'exams': lambda row: row.get('id') == exam_id,
        }
        for table, predicate in related.items():
            tables[table] = [row for row in tables.get(table, []) if not predicate(row)]
        return type('Result', (), {'data': {'deleted':True,'audio_storage_keys':audio_keys,
            'artifact_storage_keys':artifact_keys}})()


@pytest.fixture(autouse=True)
def auth_config(monkeypatch):
    monkeypatch.setattr(settings, 'EXAM_MANAGEMENT_TOKEN_SECRET', 't' * 40)


def future_time():
    return (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()


@pytest.mark.asyncio
async def test_teacher_api_requires_scoped_management_token():
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://test') as client:
        missing = await client.get('/api/exams')
        invalid = await client.get('/api/exams', headers={'Authorization': 'Bearer invalid'})
    assert missing.status_code == 401
    assert invalid.status_code == 401


@pytest.mark.asyncio
async def test_create_exam_and_reject_bad_title_duration_and_past_schedule(monkeypatch):
    db = FakeSupabase(); monkeypatch.setattr(exams, '_client', lambda: db)
    headers = {'Authorization': f'Bearer {create_exam_management_token()}'}
    body = {'title':'Algebra', 'instructions':'Read carefully', 'scheduled_start_at':future_time(), 'duration_minutes':60}
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://test') as client:
        created = await client.post('/api/exams', json=body, headers=headers)
        missing_title = await client.post('/api/exams', json={**body, 'title':''}, headers=headers)
        bad_duration = await client.post('/api/exams', json={**body, 'duration_minutes':0}, headers=headers)
        past = await client.post('/api/exams', json={**body, 'scheduled_start_at':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()}, headers=headers)
    assert created.status_code == 200
    payload = created.json()
    assert payload['status'] == 'DRAFT'
    assert payload['public_token']
    assert 'public_token_hash' not in payload
    assert missing_title.status_code == 422
    assert bad_duration.status_code == 422
    assert past.status_code == 422


@pytest.mark.asyncio
async def test_exam_delete_removes_only_selected_exam_graph_and_requires_management_auth(monkeypatch):
    db = FakeSupabase(); monkeypatch.setattr(exams, '_client', lambda: db)
    headers = {'Authorization': f'Bearer {create_exam_management_token()}'}
    body = {'title':'Deletion integration test', 'scheduled_start_at':future_time(), 'duration_minutes':30}
    other_exam = str(uuid4()); attempt_id = str(uuid4()); session_id = str(uuid4())
    question_id = str(uuid4()); option_id = str(uuid4()); answer_id = str(uuid4())
    result_id = str(uuid4()); artifact_id = str(uuid4())
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url='http://test') as client:
        created = await client.post('/api/exams', json=body, headers=headers)
        assert created.status_code == 200
        exam_id = created.json()['id']
        listed = await client.get('/api/exams', headers=headers)
        viewed = await client.get(f'/api/exams/{exam_id}', headers=headers)
        assert any(row['id'] == exam_id for row in listed.json()['exams'])
        assert viewed.status_code == 200 and viewed.json()['title'] == body['title']

        db.storage['exams'].append({'id':other_exam,'title':'Keep this exam','status':'DRAFT'})
        db.storage['exam_questions'] = [
            {'id':question_id,'exam_id':exam_id}, {'id':str(uuid4()),'exam_id':other_exam},
        ]
        db.storage['exam_question_options'] = [{'id':option_id,'question_id':question_id}]
        db.storage['exam_question_rubrics'] = [{'question_id':question_id}]
        db.storage['exam_attempts'] = [{'id':attempt_id,'exam_id':exam_id,'active_session_id':session_id}]
        db.storage['exam_attempt_sessions'] = [{'id':session_id,'attempt_id':attempt_id}]
        db.storage['exam_answers'] = [{'id':answer_id,'exam_id':exam_id,'attempt_id':attempt_id,'question_id':question_id}]
        db.storage['exam_ai_evaluations'] = [{'id':str(uuid4()),'answer_id':answer_id}]
        db.storage['exam_manual_reviews'] = [{'id':str(uuid4()),'answer_id':answer_id}]
        db.storage['exam_audio_answers'] = [{'exam_id':exam_id,'attempt_id':attempt_id,'question_id':question_id,'storage_key':'audio/test'}]
        db.storage['exam_violations'] = [{'attempt_id':attempt_id}]
        db.storage['exam_results'] = [{'id':result_id,'exam_id':exam_id,'attempt_id':attempt_id}]
        db.storage['exam_artifacts'] = [{'id':artifact_id,'exam_id':exam_id,'result_id':result_id,'storage_key':'pdf/test'}]
        db.storage['exam_notification_jobs'] = [{'result_id':result_id,'artifact_id':artifact_id}]
        db.storage['exam_allowed_students'] = [{'exam_id':exam_id,'google_sub':'google-test'}]
        db.storage['classes'] = [{'id':'class-keep'}]
        db.storage['recordings'] = [{'id':'recording-keep'}]
        db.storage['meeting_notes'] = [{'id':'notes-keep'}]
        db.storage['google_users'] = [{'id':'user-keep'}]
        db.storage_objects = {'exam-audio-private':{'audio/test'},'exam-artifacts-private':{'pdf/test'}}

        unauthorized = await client.delete(f'/api/exams/{exam_id}')
        cancelled = await client.get(f'/api/exams/{exam_id}', headers=headers)
        assert unauthorized.status_code == 401
        assert cancelled.status_code == 200  # No request means the confirmation was cancelled/no deletion.

        deleted = await client.delete(f'/api/exams/{exam_id}', headers=headers)
        missing = await client.delete(f'/api/exams/{uuid4()}', headers=headers)
        remaining = await client.get('/api/exams', headers=headers)

    assert deleted.status_code == 200 and deleted.json()['deleted'] is True
    assert deleted.json()['storage_cleanup_complete'] is True
    assert missing.status_code == 404
    assert all(row['id'] != exam_id for row in remaining.json()['exams'])
    for table in ('exam_questions','exam_question_options','exam_question_rubrics','exam_attempts',
                  'exam_attempt_sessions','exam_answers','exam_ai_evaluations','exam_manual_reviews',
                  'exam_audio_answers','exam_violations','exam_results','exam_artifacts',
                  'exam_notification_jobs','exam_allowed_students'):
        assert all(row.get('exam_id') != exam_id and row.get('attempt_id') != attempt_id
                   and row.get('question_id') != question_id and row.get('answer_id') != answer_id
                   and row.get('result_id') != result_id and row.get('artifact_id') != artifact_id
                   for row in db.storage.get(table, [])), table
    assert db.storage['exams'] == [{'id':other_exam,'title':'Keep this exam','status':'DRAFT'}]
    assert db.storage['exam_questions'][0]['exam_id'] == other_exam
    assert db.storage['classes'] == [{'id':'class-keep'}]
    assert db.storage['recordings'] == [{'id':'recording-keep'}]
    assert db.storage['meeting_notes'] == [{'id':'notes-keep'}]
    assert db.storage['google_users'] == [{'id':'user-keep'}]
    assert db.storage_objects == {'exam-audio-private':set(),'exam-artifacts-private':set()}


def test_delete_rpc_explicitly_respects_fk_order_without_touching_other_domains():
    migration = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'migrations',
                             '013_delete_exam_and_dependents.sql')
    sql = open(migration, encoding='utf-8').read()
    expected_order = [
        'DELETE FROM public.exam_notification_jobs', 'DELETE FROM public.exam_artifacts',
        'DELETE FROM public.exam_results', 'DELETE FROM public.exam_ai_evaluations',
        'DELETE FROM public.exam_manual_reviews', 'DELETE FROM public.exam_audio_answers',
        'DELETE FROM public.exam_answers', 'DELETE FROM public.exam_violations',
        'UPDATE public.exam_attempts SET active_session_id=NULL',
        'DELETE FROM public.exam_attempt_sessions', 'DELETE FROM public.exam_attempts',
        'DELETE FROM public.exam_allowed_students', 'DELETE FROM public.exam_question_rubrics',
        'DELETE FROM public.exam_question_options', 'DELETE FROM public.exam_questions',
        'DELETE FROM public.exams',
    ]
    offsets = [sql.index(token) for token in expected_order]
    assert offsets == sorted(offsets)
    assert 'google_users' not in sql and 'recordings' not in sql and 'meeting_notes' not in sql


@pytest.mark.asyncio
async def test_exam_can_be_scheduled_without_teacher_supplied_system_instructions(monkeypatch):
    db=FakeSupabase(); exam_id=str(uuid4())
    db.storage['exams']=[{'id':exam_id,'title':'Algebra','description':None,'instructions':None,
        'custom_instructions_enabled':False,'custom_instructions':None,'scheduled_start_at':future_time(),
        'duration_minutes':45,'allow_list_enabled':False,'status':'DRAFT'}]
    question_id=str(uuid4())
    db.storage['exam_questions']=[{'id':question_id,'exam_id':exam_id,'question_number':1,
        'question_type':'MCQ','max_marks':5}]
    db.storage['exam_question_options']=[{'id':str(uuid4()),'question_id':question_id,'option_text':'A','is_correct':True},
        {'id':str(uuid4()),'question_id':question_id,'option_text':'B','is_correct':False}]
    monkeypatch.setattr(exams,'_client',lambda:db)
    headers={'Authorization':f'Bearer {create_exam_management_token()}'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as client:
        scheduled=await client.post(f'/api/exams/{exam_id}/schedule',headers=headers)
    assert scheduled.status_code==200
    assert scheduled.json()['status']=='SCHEDULED'
    assert scheduled.json()['custom_instructions_enabled'] is False


def test_question_configuration_validates_mcq_and_accepts_unified_text_audio():
    with pytest.raises(Exception) as too_few:
        exams.QuestionInput(question_text='Pick one', question_type='MCQ', max_marks=1, options=[]).validate_configuration()
    assert getattr(too_few.value, 'detail', '')['message'].startswith('MCQs require')
    with pytest.raises(Exception) as multi_correct:
        exams.QuestionInput(question_text='Pick one', question_type='MCQ', max_marks=1,
            options=[{'text':'A','is_correct':True},{'text':'B','is_correct':True}]).validate_configuration()
    assert getattr(multi_correct.value, 'detail', '')['message'].startswith('Select exactly one')
    exams.QuestionInput(question_text='Explain', question_type='TEXT_AUDIO_ANSWER', max_marks=4).validate_configuration()
    with pytest.raises(Exception):
        exams.QuestionInput(question_text='Speak', question_type='AUDIO_ANSWER', max_marks=4)


def test_active_exam_configuration_is_locked_and_draft_is_editable():
    db = FakeSupabase()
    db.storage['exams'] = [
        {'id':str(uuid4()),'status':'DRAFT'},
        {'id':str(uuid4()),'status':'SCHEDULED'},
        {'id':str(uuid4()),'status':'ACTIVE'},
        {'id':str(uuid4()),'status':'FINALIZED'},
    ]
    assert exams._editable(db, __import__('uuid').UUID(db.storage['exams'][0]['id']))['status'] == 'DRAFT'
    assert exams._editable(db, __import__('uuid').UUID(db.storage['exams'][1]['id']))['status'] == 'SCHEDULED'
    for row in db.storage['exams'][2:]:
        with pytest.raises(Exception) as locked:
            exams._editable(db, __import__('uuid').UUID(row['id']))
        assert locked.value.status_code == 409


def test_any_attempt_locks_draft_and_scheduled_editing():
    db = FakeSupabase()
    exam_id = str(uuid4())
    db.storage['exams'] = [{'id':exam_id,'status':'SCHEDULED'}]
    db.storage['exam_attempts'] = [{'id':str(uuid4()),'exam_id':exam_id}]
    with pytest.raises(Exception) as locked:
        exams._editable(db, __import__('uuid').UUID(exam_id))
    assert locked.value.status_code == 409


def test_exam_lifecycle_is_derived_from_utc_schedule_boundaries():
    local_start = datetime.fromisoformat('2026-09-24T22:16:00+05:30')
    utc_start = datetime(2026, 9, 24, 16, 46, tzinfo=timezone.utc)
    exam = {'status':'ACTIVE','scheduled_start_at':local_start.isoformat(),'duration_minutes':5}
    assert exams._lifecycle_status({**exam,'status':'SCHEDULED'},utc_start-timedelta(microseconds=1)) == 'SCHEDULED'
    assert exams._lifecycle_status({**exam,'status':'SCHEDULED'},utc_start) == 'ACTIVE'
    assert exams._lifecycle_status(exam,utc_start+timedelta(minutes=4,seconds=59)) == 'ACTIVE'
    assert exams._lifecycle_status(exam,utc_start+timedelta(minutes=5)) == 'SUBMISSION_CLOSED'
    assert exams._lifecycle_status(exam,utc_start+timedelta(minutes=8)) == 'SUBMISSION_CLOSED'
    assert exams._lifecycle_status({**exam,'status':'CANCELLED'},utc_start) == 'CANCELLED'


@pytest.mark.asyncio
async def test_teacher_exam_list_refreshes_stale_active_status_from_server_schedule(monkeypatch):
    db=FakeSupabase()
    exam_id=str(uuid4())
    db.storage['exams']=[{'id':exam_id,'title':'astro','status':'ACTIVE',
        'scheduled_start_at':'2026-09-24T16:46:00+00:00','duration_minutes':5,
        'allow_list_enabled':False,'created_at':'2026-09-24T00:00:00+00:00',
        'updated_at':'2026-09-24T00:00:00+00:00'}]
    monkeypatch.setattr(exams,'_client',lambda:db)
    monkeypatch.setattr(exams,'datetime',FixedDateTime(datetime(2026,9,24,16,51,tzinfo=timezone.utc)))
    headers={'Authorization':f'Bearer {create_exam_management_token()}'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as client:
        response=await client.get('/api/exams',headers=headers)
    assert response.status_code==200
    assert response.json()['exams'][0]['status']=='SUBMISSION_CLOSED'
    assert db.storage['exams'][0]['status']=='SUBMISSION_CLOSED'


class FixedDateTime:
    """datetime test double that pins now() but retains standard parsing methods."""
    def __new__(cls, fixed):
        from datetime import datetime as RealDateTime
        class PinnedDateTime(RealDateTime):
            @classmethod
            def now(inner_cls, tz=None): return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
        return PinnedDateTime


@pytest.mark.asyncio
async def test_teacher_attempt_review_returns_responses_and_requires_management_auth(monkeypatch):
    db=FakeSupabase(); exam_id=str(uuid4()); attempt_id=str(uuid4()); question_id=str(uuid4()); option_id=str(uuid4()); text_question_id=str(uuid4())
    db.storage['exams']=[{'id':exam_id,'title':'astro','status':'SUBMISSION_CLOSED',
        'scheduled_start_at':'2026-09-24T16:46:00+00:00','duration_minutes':5}]
    db.storage['exam_questions']=[{'id':question_id,'exam_id':exam_id,'question_number':1,
        'question_text':'What is 2 + 2?','question_type':'MCQ','max_marks':2,'is_required':True},
        {'id':text_question_id,'exam_id':exam_id,'question_number':2,
        'question_text':'Explain your method.','question_type':'TEXT_AUDIO_ANSWER','max_marks':3,'is_required':True}]
    db.storage['exam_question_options']=[{'id':option_id,'question_id':question_id,
        'option_order':1,'option_text':'4','is_correct':True}]
    db.storage['exam_attempts']=[{'id':attempt_id,'exam_id':exam_id,'google_sub':'verified-google-sub',
        'student_display_name':'Test Student','student_email':'student@example.test','status':'SUBMITTED',
        'started_at':'2026-09-24T16:46:30+00:00','expires_at':'2026-09-24T16:51:00+00:00',
        'submitted_at':'2026-09-24T16:48:00+00:00','violation_count':0}]
    db.storage['exam_answers']=[{'attempt_id':attempt_id,'exam_id':exam_id,'question_id':question_id,
        'answer_text':None,'selected_option_id':option_id,'answer_method':'MCQ','saved_at':'2026-09-24T16:47:00+00:00'},
        {'attempt_id':attempt_id,'exam_id':exam_id,'question_id':text_question_id,
        'answer_text':'I combined two pairs.','selected_option_id':None,'answer_method':'BOTH','saved_at':'2026-09-24T16:47:30+00:00'}]
    db.storage['exam_audio_answers']=[{'attempt_id':attempt_id,'exam_id':exam_id,'question_id':text_question_id,
        'storage_key':'private/object-key','audio_mime_type':'audio/webm','duration_ms':2400,
        'transcription_status':'READY','transcript':'I combined two pairs.','uploaded_at':'2026-09-24T16:47:25+00:00',
        'transcribed_at':'2026-09-24T16:47:26+00:00'}]
    monkeypatch.setattr(exams,'_client',lambda:db)
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as client:
        unauthorized=await client.get(f'/api/exams/{exam_id}/attempts')
        response=await client.get(f'/api/exams/{exam_id}/attempts',headers={
            'Authorization':f'Bearer {create_exam_management_token()}'})
    assert unauthorized.status_code==401
    assert response.status_code==200
    attempt=response.json()['attempts'][0]
    assert attempt['status']=='SUBMITTED'
    assert attempt['student']=={'google_sub':'verified-google-sub','display_name':'Test Student','email':'student@example.test'}
    assert attempt['submitted_at']=='2026-09-24T16:48:00+00:00'
    assert attempt['completion_seconds']==90
    assert attempt['questions'][0]['selected_option']['option_text']=='4'
    assert attempt['questions'][1]['answer_text']=='I combined two pairs.'
    assert attempt['questions'][1]['audio']['transcript']=='I combined two pairs.'
    assert 'is_correct' not in response.text and 'storage_key' not in response.text


@pytest.mark.asyncio
async def test_teacher_can_save_manual_marks_and_feedback_without_schema_change(monkeypatch):
    db=FakeSupabase(); exam_id=str(uuid4()); attempt_id=str(uuid4()); question_id=str(uuid4()); answer_id=str(uuid4())
    db.storage['exams']=[{'id':exam_id,'title':'astro','status':'SUBMISSION_CLOSED',
        'scheduled_start_at':'2026-09-24T16:46:00+00:00','duration_minutes':5}]
    db.storage['exam_questions']=[{'id':question_id,'exam_id':exam_id,'question_number':2,
        'question_text':'Explain your method.','question_type':'TEXT_AUDIO_ANSWER','max_marks':5,
        'is_required':True,'evaluation_mode':'MANUAL_ONLY'}]
    db.storage['exam_question_rubrics']=[{'question_id':question_id,'reference_answer':'A reference.',
        'marking_criteria':'Award marks for clear reasoning.'}]
    db.storage['exam_attempts']=[{'id':attempt_id,'exam_id':exam_id,'google_sub':'verified-sub',
        'student_display_name':'Student','status':'SUBMITTED','started_at':'2026-09-24T16:46:20+00:00',
        'expires_at':'2026-09-24T16:51:00+00:00','submitted_at':'2026-09-24T16:47:15+00:00'}]
    db.storage['exam_answers']=[{'id':answer_id,'attempt_id':attempt_id,'exam_id':exam_id,
        'question_id':question_id,'answer_method':'TEXT','answer_text':'Student response.',
        'selected_option_id':None,'saved_at':'2026-09-24T16:47:00+00:00'}]
    db.storage['exam_manual_reviews']=[]
    monkeypatch.setattr(exams,'_client',lambda:db)
    headers={'Authorization':f'Bearer {create_exam_management_token()}'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as client:
        unauthorized=await client.put(f'/api/exams/{exam_id}/attempts/{attempt_id}/questions/{question_id}/evaluation',json={'marks':4,'teacher_feedback':'Good'},)
        saved=await client.put(f'/api/exams/{exam_id}/attempts/{attempt_id}/questions/{question_id}/evaluation',headers=headers,json={'marks':4,'teacher_feedback':'Good reasoning.'})
        updated=await client.put(f'/api/exams/{exam_id}/attempts/{attempt_id}/questions/{question_id}/evaluation',headers=headers,json={'marks':4.5,'teacher_feedback':'Clear and complete.'})
        too_many=await client.put(f'/api/exams/{exam_id}/attempts/{attempt_id}/questions/{question_id}/evaluation',headers=headers,json={'marks':5.1})
        review=await client.get(f'/api/exams/{exam_id}/attempts',headers=headers)
    assert unauthorized.status_code==401
    assert saved.status_code==200 and updated.status_code==200
    assert too_many.status_code==422
    assert len(db.storage['exam_manual_reviews'])==1
    item=review.json()['attempts'][0]['questions'][0]
    assert item['answer_text']=='Student response.'
    assert item['reference_answer']=='A reference.'
    assert item['marking_criteria']=='Award marks for clear reasoning.'
    assert item['manual_review']['marks']==4.5
    assert item['manual_review']['teacher_comments']=='Clear and complete.'


@pytest.mark.asyncio
async def test_teacher_audio_playback_uses_authorized_private_storage(monkeypatch):
    db=FakeSupabase(); exam_id=str(uuid4()); attempt_id=str(uuid4()); question_id=str(uuid4())
    class Storage:
        def from_(self,_bucket): return self
        def download(self,key): return b'private audio bytes' if key=='attempt/private-audio' else b''
    db.storage_client=Storage()
    db.table=lambda name: Query(name,db.storage)
    db.storage['exam_attempts']=[{'id':attempt_id,'exam_id':exam_id}]
    db.storage['exam_audio_answers']=[{'attempt_id':attempt_id,'exam_id':exam_id,
        'question_id':question_id,'storage_key':'attempt/private-audio','audio_mime_type':'audio/webm'}]
    # Teacher playback reads from the same private bucket used by student playback.
    from app import config
    monkeypatch.setattr(config.settings,'EXAM_AUDIO_BUCKET','exam-audio-private')
    from types import SimpleNamespace
    monkeypatch.setattr(exams,'_client',lambda:SimpleNamespace(table=db.table,storage=db.storage_client))
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as client:
        unauthorized=await client.get(f'/api/exams/{exam_id}/attempts/{attempt_id}/questions/{question_id}/audio')
        response=await client.get(f'/api/exams/{exam_id}/attempts/{attempt_id}/questions/{question_id}/audio',headers={
            'Authorization':f'Bearer {create_exam_management_token()}'})
    assert unauthorized.status_code==401
    assert response.status_code==200 and response.content==b'private audio bytes'
    assert response.headers['cache-control']=='private, no-store'
