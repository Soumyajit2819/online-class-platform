import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import exam_students, exams, main
from app.config import settings


class Query:
    def __init__(self, db, table):
        self.db, self.table_name, self.filters = db, table, []
        self.columns = '*'
        self.operation='select';self.values=None
    def select(self, columns='*'): self.columns = columns; return self
    def update(self,values):self.operation='update';self.values=values;return self
    def eq(self, key, value): self.filters.append((key, str(value))); return self
    def limit(self, *_args): return self
    def order(self, *_args, **_kwargs): return self
    def execute(self):
        rows = self.db.tables.setdefault(self.table_name, [])
        result = [dict(row) for row in rows if all(str(row.get(k)) == v for k,v in self.filters)]
        if self.operation=='update':
            for row in rows:
                if all(str(row.get(k))==v for k,v in self.filters):row.update(self.values)
            return type('Result',(),{'data':result})()
        if self.columns != '*':
            columns = [name.strip() for name in self.columns.split(',')]
            result = [{key:row[key] for key in columns if key in row} for row in result]
        return type('Result', (), {'data':result})()


class Rpc:
    def __init__(self, db, name, params): self.db, self.name, self.params = db, name, params
    def execute(self):
        result = getattr(self.db, self.name)(self.params)
        return type('Result', (), {'data':result})()


class PrivateAudioStorage:
    def __init__(self): self.objects={}
    def from_(self,_bucket): return self
    def upload(self,key,data,_options): self.objects[key]=bytes(data)
    def download(self,key): return self.objects[key]
    def remove(self,keys):
        for key in keys:self.objects.pop(key,None)


class StudentDb:
    def __init__(self, *, start=None, duration=60, allow_list=False, questions=None):
        self.now = datetime.now(timezone.utc)
        self.token = 'exam-share-token-' + 'x' * 32
        self.exam_id = str(uuid4())
        self.tables = {
            'exams':[{'id':self.exam_id,'public_token_hash':hashlib.sha256(self.token.encode()).hexdigest(),
                'title':'Physics','description':'Unit test','instructions':'Read every question.',
                'custom_instructions_enabled':False,'custom_instructions':None,
                'scheduled_start_at':(start or self.now-timedelta(minutes=7)).isoformat(),
                'duration_minutes':duration,'status':'SCHEDULED','allow_list_enabled':allow_list,
                'maximum_violations':3,'protected_mode_enabled':True,'require_fullscreen':True,
                'detect_visibility_change':True,'detect_orientation_change':False,'restrict_copy_paste':True,
                'autosave_enabled':True,'automatic_submission_enabled':True}],
            'exam_allowed_students':[], 'exam_attempts':[], 'exam_questions':questions or [],
            'exam_question_options':[], 'exam_answers':[], 'exam_audio_answers':[], 'exam_violations':[],
        }
        for q in self.tables['exam_questions']:
            if q.get('question_type') == 'MCQ':
                self.tables['exam_question_options'].extend(q.pop('options', []))
        self.db_time = self.now
        self.storage=PrivateAudioStorage()
    def table(self, name): return Query(self,name)
    def rpc(self, name, params): return Rpc(self,name,params)
    def _exam(self, p): return next((e for e in self.tables['exams'] if e['public_token_hash']==p['p_exam_token_hash']),None)
    def start_student_exam_attempt(self,p):
        exam=self._exam(p)
        if not exam: return {'error':'exam_not_found'}
        now=self.db_time
        existing=next((a for a in self.tables['exam_attempts'] if a['exam_id']==exam['id'] and a['google_sub']==p['p_google_sub']),None)
        if existing:
            if existing['status']=='ACTIVE' and datetime.fromisoformat(existing['expires_at'])<=now:
                existing.update(status='AUTO_SUBMITTED',submitted_at=now.isoformat())
            existing['attempt_token_hash']=p['p_attempt_token_hash']
            return {'attempt':dict(existing),'server_time':now.isoformat(),'remaining_seconds':max(0,int((datetime.fromisoformat(existing['expires_at'])-now).total_seconds())),'resumed':True}
        start=datetime.fromisoformat(exam['scheduled_start_at'])
        if exam['status'] not in ('SCHEDULED','ACTIVE'): return {'error':'exam_unavailable'}
        if start>now: return {'error':'not_started'}
        expires=start+timedelta(minutes=exam['duration_minutes'])
        if expires<=now: return {'error':'exam_ended'}
        if exam['allow_list_enabled'] and not any(x['exam_id']==exam['id'] and x['google_sub']==p['p_google_sub'] for x in self.tables['exam_allowed_students']): return {'error':'not_eligible'}
        exam['status']='ACTIVE'
        attempt={'id':str(uuid4()),'exam_id':exam['id'],'google_sub':p['p_google_sub'],'student_display_name':p['p_student_display_name'],'status':'ACTIVE','started_at':now.isoformat(),'expires_at':expires.isoformat(),'submitted_at':None,'violation_count':0,'attempt_token_hash':p['p_attempt_token_hash']}
        self.tables['exam_attempts'].append(attempt)
        return {'attempt':dict(attempt),'server_time':now.isoformat(),'remaining_seconds':int((expires-now).total_seconds()),'resumed':False}
    def _attempt(self,p):
        exam=self._exam(p)
        if not exam: return None,None
        attempt=next((a for a in self.tables['exam_attempts'] if a['exam_id']==exam['id'] and a['google_sub']==p['p_google_sub'] and a['attempt_token_hash']==p['p_attempt_token_hash']),None)
        return exam,attempt
    def get_student_exam_attempt(self,p):
        exam,attempt=self._attempt(p)
        if not exam:return {'error':'exam_not_found'}
        if not attempt:return {'error':'attempt_not_found'}
        if attempt['status']=='ACTIVE' and datetime.fromisoformat(attempt['expires_at'])<=self.db_time:
            attempt.update(status='AUTO_SUBMITTED',submitted_at=self.db_time.isoformat())
        return {'attempt':dict(attempt),'server_time':self.db_time.isoformat(),'remaining_seconds':max(0,int((datetime.fromisoformat(attempt['expires_at'])-self.db_time).total_seconds()))}
    def save_student_exam_answer(self,p):
        exam,attempt=self._attempt(p)
        if not attempt:return {'error':'attempt_not_found'}
        if attempt['status']=='ACTIVE' and datetime.fromisoformat(attempt['expires_at'])<=self.db_time:
            attempt.update(status='AUTO_SUBMITTED',submitted_at=self.db_time.isoformat()); return {'error':'attempt_expired'}
        if attempt['status']!='ACTIVE':return {'error':'attempt_submitted'}
        question=next((q for q in self.tables['exam_questions'] if q['id']==p['p_question_id'] and q['exam_id']==exam['id']),None)
        if not question:return {'error':'question_not_found'}
        if question['question_type']=='MCQ':
            if p['p_answer_text'] is not None or not any(o['id']==p['p_selected_option_id'] and o['question_id']==question['id'] for o in self.tables['exam_question_options']): return {'error':'invalid_answer'}
        elif p['p_selected_option_id'] is not None or question['question_type'] not in ('TEXT_AUDIO_ANSWER','SHORT_ANSWER','LONG_ANSWER'):return {'error':'invalid_answer'}
        row=next((a for a in self.tables['exam_answers'] if a['attempt_id']==attempt['id'] and a['question_id']==question['id']),None)
        if row is None:
            row={'attempt_id':attempt['id'],'exam_id':exam['id'],'question_id':question['id'],'answer_version':0};self.tables['exam_answers'].append(row)
        row.update(answer_text=p['p_answer_text'],selected_option_id=p['p_selected_option_id'],answer_version=row['answer_version']+1,saved_at=self.db_time.isoformat())
        return {'answer':{k:row[k] for k in ('question_id','answer_text','selected_option_id','answer_version','saved_at')}}
    def submit_student_exam_attempt(self,p):
        exam,attempt=self._attempt(p)
        if not attempt:return {'error':'attempt_not_found'}
        if attempt['status']=='ACTIVE':
            expired=datetime.fromisoformat(attempt['expires_at'])<=self.db_time
            attempt.update(status='AUTO_SUBMITTED' if expired else 'SUBMITTED',submitted_at=self.db_time.isoformat())
        return {'attempt':dict(attempt),'server_time':self.db_time.isoformat(),'remaining_seconds':0}
    def set_student_exam_answer_method(self,p):
        exam,attempt=self._attempt(p)
        if not attempt:return {'error':'attempt_not_found'}
        answer=next((a for a in self.tables['exam_answers'] if a['attempt_id']==attempt['id'] and a['question_id']==p['p_question_id']),None)
        if answer is None:
            answer={'attempt_id':attempt['id'],'exam_id':exam['id'],'question_id':p['p_question_id'],'answer_text':None}
            self.tables['exam_answers'].append(answer)
        answer['answer_method']=p['p_answer_method']
        return {'question_id':p['p_question_id'],'answer_method':p['p_answer_method']}
    def save_student_exam_audio_answer(self,p):
        exam,attempt=self._attempt(p)
        if not attempt:return {'error':'attempt_not_found'}
        question=next((q for q in self.tables['exam_questions'] if q['id']==p['p_question_id']),None)
        if not question:return {'error':'question_not_found'}
        audio=next((a for a in self.tables['exam_audio_answers'] if a['attempt_id']==attempt['id'] and a['question_id']==p['p_question_id']),None)
        old_key=audio['storage_key'] if audio else None
        if audio is None:
            audio={'attempt_id':attempt['id'],'exam_id':exam['id'],'question_id':p['p_question_id']};self.tables['exam_audio_answers'].append(audio)
        audio.update(storage_key=p['p_storage_key'],audio_mime_type=p['p_audio_mime_type'],file_size_bytes=p['p_file_size_bytes'],duration_ms=p['p_duration_ms'],transcription_status='READY',transcript=p['p_transcript'])
        answer=next((a for a in self.tables['exam_answers'] if a['attempt_id']==attempt['id'] and a['question_id']==p['p_question_id']),None)
        if answer is None:
            answer={'attempt_id':attempt['id'],'exam_id':exam['id'],'question_id':p['p_question_id'],'answer_text':None};self.tables['exam_answers'].append(answer)
        answer['answer_method']=p['p_answer_method']
        return {'old_storage_key':old_key,'question_id':p['p_question_id'],'answer_method':p['p_answer_method'],'transcript':p['p_transcript'],'transcription_status':'READY','duration_ms':p['p_duration_ms']}
    def delete_student_exam_audio_answer(self,p):
        _,attempt=self._attempt(p)
        audio=next((a for a in self.tables['exam_audio_answers'] if a['attempt_id']==attempt['id'] and a['question_id']==p['p_question_id']),None)
        if audio:self.tables['exam_audio_answers'].remove(audio)
        return {'storage_key':audio['storage_key'] if audio else None,'deleted':bool(audio)}
    def heartbeat_student_exam_attempt(self,p):
        exam,attempt=self._attempt(p)
        if not attempt:return {'error':'attempt_not_found'}
        if attempt['status']=='ACTIVE' and datetime.fromisoformat(attempt['expires_at'])<=self.db_time:
            attempt.update(status='AUTO_SUBMITTED',submitted_at=self.db_time.isoformat())
        return {'status':attempt['status'],'server_time':self.db_time.isoformat(),
            'remaining_seconds':max(0,int((datetime.fromisoformat(attempt['expires_at'])-self.db_time).total_seconds())),
            'violation_count':attempt.get('violation_count',0)}
    def record_student_exam_violation(self,p):
        exam,attempt=self._attempt(p)
        if not attempt:return {'error':'attempt_not_found'}
        count=attempt.get('violation_count',0)+1;attempt['violation_count']=count
        maximum=exam['maximum_violations'];terminated=bool(maximum and count>=maximum)
        if terminated:attempt.update(status='TERMINATED',submitted_at=self.db_time.isoformat())
        self.tables['exam_violations'].append({'attempt_id':attempt['id'],'violation_type':p['p_violation_type'],'sequence_number':count})
        return {'status':attempt['status'],'violation_count':count,'maximum_violations':maximum,'terminated':terminated}


def _q(db, question_type='MCQ'):
    question={'id':str(uuid4()),'exam_id':db.exam_id,'question_number':1,'question_text':'Choose carefully',
        'question_type':question_type,'max_marks':2,'is_required':True,'evaluation_mode':'AI_ALLOWED',
        'reference_answer':'SECRET','marking_criteria':'SECRET RUBRIC'}
    if question_type=='MCQ':
        question['options']=[{'id':str(uuid4()),'question_id':question['id'],'option_order':1,'option_text':'Right','is_correct':True},
            {'id':str(uuid4()),'question_id':question['id'],'option_order':2,'option_text':'Wrong','is_correct':False}]
        db.tables['exam_question_options'].extend(question['options'])
    return question


def test_server_availability_distinguishes_draft_not_started_available_closed_and_cancelled():
    now=datetime(2026,9,24,14,0,tzinfo=timezone.utc)
    start=(now+timedelta(minutes=7)).isoformat()
    base={'status':'SCHEDULED','scheduled_start_at':start,'duration_minutes':60}
    assert exam_students._status({**base,'status':'DRAFT'},now)[0]=='UNAVAILABLE'
    assert exam_students._status(base,now)==('NOT_STARTED','Exam has not started yet.')
    late_start=now-timedelta(minutes=7)
    assert exam_students._status({**base,'scheduled_start_at':late_start.isoformat()},now)[0]=='AVAILABLE'
    assert exam_students._status({**base,'scheduled_start_at':(now-timedelta(minutes=61)).isoformat()},now)[0]=='ENDED'
    assert exam_students._status({**base,'status':'SUBMISSION_CLOSED'},now)[0]=='ENDED'
    assert exam_students._status({**base,'status':'CANCELLED'},now)[0]=='CANCELLED'


@pytest.fixture
def student_setup(monkeypatch):
    db=StudentDb(questions=[])
    monkeypatch.setattr(exam_students,'_client',lambda:db)
    def verify(credential):
        if credential=='bad-google': raise HTTPException(401,'Google sign-in failed')
        return {'sub':credential,'name':'Student Name'}
    monkeypatch.setattr(main,'verify_google_credential',verify)
    return db


@pytest.mark.asyncio
async def test_student_endpoints_require_google_identity(student_setup):
    db=student_setup
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        access=await c.post(f'/api/exams/student/{db.token}/access')
        attempt=await c.get(f'/api/exams/student/{db.token}/attempt',headers={'X-Exam-Attempt-Token':'x'*43})
        bad=await c.post(f'/api/exams/student/{db.token}/access',headers={'Authorization':'Bearer bad-google'})
    assert access.status_code==401 and attempt.status_code==401 and bad.status_code==401


@pytest.mark.asyncio
async def test_public_link_preview_has_no_private_instructions_or_internal_hash(student_setup):
    db=student_setup
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        response=await c.get(f'/api/exams/student/{db.token}')
    assert response.status_code==200
    assert 'title' in response.json() and 'instructions' not in response.json()
    assert 'public_token_hash' not in response.text and 'exam_id' not in response.text
    assert response.headers['cache-control']=='no-store'


@pytest.mark.asyncio
async def test_open_access_and_allow_list_enforcement(student_setup):
    db=student_setup
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        open_access=await c.post(f'/api/exams/student/{db.token}/access',headers={'Authorization':'Bearer any-google-sub'})
        db.tables['exams'][0]['allow_list_enabled']=True
        denied=await c.post(f'/api/exams/student/{db.token}/access',headers={'Authorization':'Bearer unknown-sub'})
        db.tables['exam_allowed_students'].append({'exam_id':db.exam_id,'google_sub':'listed-sub'})
        allowed=await c.post(f'/api/exams/student/{db.token}/access',headers={'Authorization':'Bearer listed-sub'})
    assert open_access.status_code==200 and open_access.json()['eligible'] is True
    assert denied.status_code==200 and denied.json()['eligible'] is False
    assert allowed.status_code==200 and allowed.json()['eligible'] is True
    assert 'listed-sub' not in denied.text


@pytest.mark.asyncio
async def test_system_instructions_are_always_server_controlled_and_custom_section_is_optional(student_setup):
    db=student_setup; exam=db.tables['exams'][0]
    exam['instructions']='legacy text cannot replace mandatory instructions'
    exam['custom_instructions_enabled']=False;exam['custom_instructions']='should be hidden'
    headers={'Authorization':'Bearer student-sub'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        default=await c.post(f'/api/exams/student/{db.token}/access',headers=headers)
        exam['custom_instructions_enabled']=True;exam['custom_instructions']='Bring a calculator.'
        customized=await c.post(f'/api/exams/student/{db.token}/access',headers=headers)
    assert 'server controls the official exam time' in default.json()['system_instructions']
    assert default.json()['custom_instructions'] is None
    assert customized.json()['system_instructions']==default.json()['system_instructions']
    assert customized.json()['custom_instructions']=='Bring a calculator.'
    assert 'legacy text cannot replace' not in customized.text


@pytest.mark.asyncio
async def test_start_rejected_before_start_but_unified_audio_question_is_supported(student_setup):
    db=student_setup
    headers={'Authorization':'Bearer student-sub'}
    db.tables['exams'][0]['scheduled_start_at']=(db.now+timedelta(minutes=30)).isoformat()
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        early=await c.post(f'/api/exams/student/{db.token}/attempt',headers=headers)
        assert early.status_code==425 and not db.tables['exam_attempts']
        db.tables['exams'][0]['scheduled_start_at']=(db.now-timedelta(minutes=1)).isoformat()
        audio=_q(db,'TEXT_AUDIO_ANSWER'); db.tables['exam_questions'].append(audio)
        access=await c.post(f'/api/exams/student/{db.token}/access',headers=headers)
        started=await c.post(f'/api/exams/student/{db.token}/attempt',headers=headers)
    assert access.status_code==200 and access.json()['audio_supported'] is True
    assert started.status_code==200 and len(db.tables['exam_attempts'])==1


@pytest.mark.asyncio
async def test_allow_list_is_checked_again_when_starting(student_setup):
    db=student_setup; db.tables['exams'][0]['allow_list_enabled']=True
    db.tables['exam_questions'].append(_q(db))
    db.tables['exam_allowed_students'].append({'exam_id':db.exam_id,'google_sub':'permitted-sub'})
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        denied=await c.post(f'/api/exams/student/{db.token}/attempt',headers={'Authorization':'Bearer other-sub'})
        accepted=await c.post(f'/api/exams/student/{db.token}/attempt',headers={'Authorization':'Bearer permitted-sub'})
    assert denied.status_code==403
    assert accepted.status_code==200 and len(db.tables['exam_attempts'])==1


@pytest.mark.asyncio
async def test_late_start_uses_scheduled_end_and_duplicate_start_resumes(student_setup):
    db=student_setup; question=_q(db); db.tables['exam_questions'].append(question)
    scheduled_start=db.now-timedelta(minutes=7)
    db.tables['exams'][0]['scheduled_start_at']=scheduled_start.isoformat()
    headers={'Authorization':'Bearer student-sub'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        first=await c.post(f'/api/exams/student/{db.token}/attempt',headers=headers)
        second=await c.post(f'/api/exams/student/{db.token}/attempt',headers=headers)
    one,two=first.json(),second.json()
    assert len(db.tables['exam_attempts'])==1
    assert one['attempt']['started_at']==two['attempt']['started_at'] and two['resumed'] is True
    assert 'id' not in one['attempt'] and 'google_sub' not in one['attempt']
    assert 'attempt_token_hash' not in one
    assert len(db.tables['exam_attempts'][0]['attempt_token_hash'])==64
    assert datetime.fromisoformat(one['attempt']['expires_at'])==scheduled_start+timedelta(minutes=60)
    assert 52*60 <= one['remaining_seconds'] <= 54*60


@pytest.mark.asyncio
async def test_expired_exam_cannot_create_attempt_at_exact_scheduled_expiry(student_setup):
    db=student_setup; db.tables['exam_questions'].append(_q(db))
    scheduled_start=datetime(2026,9,24,22,16,tzinfo=timezone(timedelta(hours=5,minutes=30)))
    db.tables['exams'][0]['scheduled_start_at']=scheduled_start.isoformat()
    db.tables['exams'][0]['duration_minutes']=5
    db.db_time=datetime(2026,9,24,16,51,tzinfo=timezone.utc)
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        expired=await c.post(f'/api/exams/student/{db.token}/attempt',headers={'Authorization':'Bearer student-sub'})
    assert expired.status_code==410
    assert expired.json()['detail']=='Exam has ended'
    assert db.tables['exam_attempts']==[]


@pytest.mark.asyncio
async def test_manually_submitted_attempt_remains_submitted_after_exam_expiry(student_setup):
    db=student_setup; question=_q(db); db.tables['exam_questions'].append(question)
    start=datetime(2026,9,24,16,46,tzinfo=timezone.utc)
    db.tables['exams'][0].update(scheduled_start_at=start.isoformat(),duration_minutes=5)
    submitted_at=start+timedelta(minutes=2)
    db.db_time=start+timedelta(minutes=8)
    attempt={'id':str(uuid4()),'exam_id':db.exam_id,'google_sub':'student-sub',
        'student_display_name':'Student Name','status':'SUBMITTED','started_at':(start+timedelta(seconds=30)).isoformat(),
        'expires_at':(start+timedelta(minutes=5)).isoformat(),'submitted_at':submitted_at.isoformat(),
        'violation_count':0,'attempt_token_hash':'old-hash'}
    db.tables['exam_attempts'].append(attempt)
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        resumed=await c.post(f'/api/exams/student/{db.token}/attempt',headers={'Authorization':'Bearer student-sub'})
        write=await c.put(f'/api/exams/student/{db.token}/attempt/answers',headers={
            'Authorization':'Bearer student-sub','X-Exam-Attempt-Token':resumed.json()['attempt_token']},
            json={'question_id':question['id'],'selected_option_id':question['options'][0]['id']})
    assert resumed.status_code==200
    assert resumed.json()['attempt']['status']=='SUBMITTED'
    assert resumed.json()['attempt']['submitted_at']==submitted_at.isoformat()
    assert write.status_code==409


@pytest.mark.asyncio
async def test_attempt_payload_is_owner_bound_and_never_exposes_answer_key_or_rubric(student_setup):
    db=student_setup; question=_q(db); db.tables['exam_questions'].append(question)
    headers={'Authorization':'Bearer student-sub'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        started=(await c.post(f'/api/exams/student/{db.token}/attempt',headers=headers)).json()
        own=await c.get(f'/api/exams/student/{db.token}/attempt',headers={**headers,'X-Exam-Attempt-Token':started['attempt_token']})
        other=await c.get(f'/api/exams/student/{db.token}/attempt',headers={'Authorization':'Bearer other-sub','X-Exam-Attempt-Token':started['attempt_token']})
    assert own.status_code==200
    serialized=own.text.lower()
    assert 'is_correct' not in serialized and 'reference_answer' not in serialized and 'marking_criteria' not in serialized
    assert 'google_sub' not in serialized and 'attempt_token_hash' not in serialized
    assert question['options'][0]['id'] in serialized
    assert other.status_code==404


@pytest.mark.asyncio
async def test_answer_saves_are_idempotent_and_owned_and_submission_locks_them(student_setup):
    db=student_setup; question=_q(db); db.tables['exam_questions'].append(question)
    headers={'Authorization':'Bearer student-sub'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        start=(await c.post(f'/api/exams/student/{db.token}/attempt',headers=headers)).json()
        attempt_headers={**headers,'X-Exam-Attempt-Token':start['attempt_token']}
        mcq={'question_id':question['id'],'selected_option_id':question['options'][0]['id']}
        saved=await c.put(f'/api/exams/student/{db.token}/attempt/answers',json=mcq,headers=attempt_headers)
        repeated=await c.put(f'/api/exams/student/{db.token}/attempt/answers',json=mcq,headers=attempt_headers)
        submitted=await c.post(f'/api/exams/student/{db.token}/attempt/submit',headers=attempt_headers)
        duplicate=await c.post(f'/api/exams/student/{db.token}/attempt/submit',headers=attempt_headers)
        late_save=await c.put(f'/api/exams/student/{db.token}/attempt/answers',json=mcq,headers=attempt_headers)
    assert saved.status_code==200 and repeated.status_code==200
    assert len(db.tables['exam_answers'])==1 and db.tables['exam_answers'][0]['answer_version']==2
    assert submitted.json()['status']=='SUBMITTED' and duplicate.json()['status']=='SUBMITTED'
    assert late_save.status_code==409


@pytest.mark.asyncio
async def test_text_answers_save_and_expiry_auto_submits(student_setup):
    db=student_setup; question=_q(db,'TEXT_AUDIO_ANSWER'); db.tables['exam_questions'].append(question)
    headers={'Authorization':'Bearer student-sub'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        start=(await c.post(f'/api/exams/student/{db.token}/attempt',headers=headers)).json()
        attempt_headers={**headers,'X-Exam-Attempt-Token':start['attempt_token']}
        save=await c.put(f'/api/exams/student/{db.token}/attempt/answers',headers=attempt_headers,json={'question_id':question['id'],'answer_text':'My answer','answer_method':'TEXT'})
        db.db_time=datetime.fromisoformat(start['attempt']['expires_at'])+timedelta(seconds=1)
        late=await c.put(f'/api/exams/student/{db.token}/attempt/answers',headers=attempt_headers,json={'question_id':question['id'],'answer_text':'Too late'})
        resumed=await c.get(f'/api/exams/student/{db.token}/attempt',headers=attempt_headers)
    assert save.status_code==200
    assert len(db.tables['exam_answers'])==1
    assert db.tables['exam_answers'][0]['question_id']==question['id']
    assert db.tables['exam_answers'][0]['answer_text']=='My answer'
    assert db.tables['exam_answers'][0]['answer_method']=='TEXT'
    assert late.status_code==410
    assert resumed.json()['attempt']['status']=='AUTO_SUBMITTED'
    assert resumed.json()['answers'][0]['answer_text']=='My answer'
    assert resumed.json()['answers'][0]['answer_method']=='TEXT'


@pytest.mark.asyncio
async def test_typed_answer_survives_student_submit_and_appears_in_teacher_review(student_setup,monkeypatch):
    from app.exam_auth import create_exam_management_token
    db=student_setup; question=_q(db,'TEXT_AUDIO_ANSWER'); db.tables['exam_questions'].append(question)
    monkeypatch.setattr(settings,'EXAM_MANAGEMENT_TOKEN_SECRET','m'*40)
    monkeypatch.setattr(exams,'_client',lambda:db)
    student_headers={'Authorization':'Bearer student-sub'}
    teacher_headers={'Authorization':f'Bearer {create_exam_management_token()}'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        started=(await c.post(f'/api/exams/student/{db.token}/attempt',headers=student_headers)).json()
        attempt_headers={**student_headers,'X-Exam-Attempt-Token':started['attempt_token']}
        saved=await c.put(f'/api/exams/student/{db.token}/attempt/answers',headers=attempt_headers,
            json={'question_id':question['id'],'answer_text':'Final typed response','answer_method':'TEXT'})
        submitted=await c.post(f'/api/exams/student/{db.token}/attempt/submit',headers=attempt_headers)
        teacher_review=await c.get(f'/api/exams/{db.exam_id}/attempts',headers=teacher_headers)
    assert saved.status_code==200 and submitted.status_code==200
    assert submitted.json()['status']=='SUBMITTED'
    assert len(db.tables['exam_answers'])==1
    reviewed=teacher_review.json()['attempts'][0]
    assert reviewed['status']=='SUBMITTED'
    assert reviewed['questions'][0]['answer_text']=='Final typed response'
    assert reviewed['questions'][0]['answer_method']=='TEXT'


@pytest.mark.asyncio
async def test_audio_upload_transcribes_keeps_private_playback_and_preserves_typed_answer(student_setup,monkeypatch):
    from types import SimpleNamespace
    from app import sarvam_stt
    db=student_setup; question=_q(db,'TEXT_AUDIO_ANSWER'); db.tables['exam_questions'].append(question)
    class Provider:
        failing=False
        def validate_configuration(self):return True
        async def transcribe(self,_path,_metadata):
            if self.failing:raise RuntimeError('provider unavailable')
            return SimpleNamespace(text='বাংলা English transcript',language='bn-IN')
    provider=Provider()
    monkeypatch.setattr(sarvam_stt,'SarvamBatchSTTProvider',lambda:provider)
    headers={'Authorization':'Bearer student-sub'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        started=(await c.post(f'/api/exams/student/{db.token}/attempt',headers=headers)).json()
        attempt_headers={**headers,'X-Exam-Attempt-Token':started['attempt_token']}
        typed=await c.put(f'/api/exams/student/{db.token}/attempt/answers',headers=attempt_headers,
            json={'question_id':question['id'],'answer_text':'My typed response','answer_method':'TEXT'})
        saved=await c.put(f'/api/exams/student/{db.token}/attempt/questions/{question["id"]}/audio',headers=attempt_headers,
            data={'duration_ms':'2450','answer_method':'BOTH'},files={'file':('response.webm',b'private-audio','audio/webm')})
        streamed=await c.get(f'/api/exams/student/{db.token}/attempt/questions/{question["id"]}/audio',headers=attempt_headers)
        provider.failing=True
        failed=await c.put(f'/api/exams/student/{db.token}/attempt/questions/{question["id"]}/audio',headers=attempt_headers,
            data={'duration_ms':'1800','answer_method':'AUDIO'},files={'file':('retry.webm',b'retry-audio','audio/webm')})
    assert typed.status_code==200 and saved.status_code==200
    assert saved.json()['transcript']=='বাংলা English transcript'
    assert streamed.status_code==200 and streamed.content==b'private-audio'
    assert streamed.headers['cache-control']=='private, no-store'
    assert failed.status_code==502
    assert db.tables['exam_answers'][0]['answer_text']=='My typed response'
    assert db.tables['exam_answers'][0]['answer_method']=='BOTH'
    assert len(db.tables['exam_audio_answers'])==1 and db.tables['exam_audio_answers'][0]['transcription_status']=='READY'
    assert db.tables['exam_audio_answers'][0]['transcript']=='বাংলা English transcript'
    assert db.tables['exam_audio_answers'][0]['storage_key'] in db.storage.objects
    assert len(db.storage.objects)==1


@pytest.mark.asyncio
async def test_heartbeat_and_violation_limit_are_server_authoritative(student_setup):
    db=student_setup; db.tables['exams'][0]['maximum_violations']=2
    db.tables['exam_questions'].append(_q(db))
    headers={'Authorization':'Bearer student-sub'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        started=(await c.post(f'/api/exams/student/{db.token}/attempt',headers=headers)).json()
        attempt_headers={**headers,'X-Exam-Attempt-Token':started['attempt_token']}
        first=await c.post(f'/api/exams/student/{db.token}/attempt/violations',headers=attempt_headers,json={'violation_type':'TAB_HIDDEN'})
        beat=await c.post(f'/api/exams/student/{db.token}/attempt/heartbeat',headers=attempt_headers)
        second=await c.post(f'/api/exams/student/{db.token}/attempt/violations',headers=attempt_headers,json={'violation_type':'FULLSCREEN_EXIT'})
    assert first.json()['violation_count']==1 and first.json()['status']=='ACTIVE'
    assert beat.json()['status']=='ACTIVE' and beat.json()['remaining_seconds']>0
    assert second.json()['terminated'] is True and second.json()['status']=='TERMINATED'
    assert db.tables['exam_attempts'][0]['status']=='TERMINATED'


@pytest.mark.asyncio
async def test_attempt_token_cannot_be_used_for_another_exam_or_with_another_identity(student_setup):
    db=student_setup; db.tables['exam_questions'].append(_q(db))
    headers={'Authorization':'Bearer student-sub'}
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        started=(await c.post(f'/api/exams/student/{db.token}/attempt',headers=headers)).json()
        token=started['attempt_token']
        wrong_identity=await c.get(f'/api/exams/student/{db.token}/attempt',headers={'Authorization':'Bearer other-sub','X-Exam-Attempt-Token':token})
        wrong_exam=await c.get(f'/api/exams/student/not-the-exam-token/attempt',headers={**headers,'X-Exam-Attempt-Token':token})
    assert wrong_identity.status_code==404 and wrong_exam.status_code==404


@pytest.mark.asyncio
async def test_teacher_api_rejects_student_google_credential(student_setup, monkeypatch):
    monkeypatch.setattr(settings,'EXAM_MANAGEMENT_TOKEN_SECRET','m'*40)
    async with AsyncClient(transport=ASGITransport(app=main.app),base_url='http://test') as c:
        response=await c.get('/api/exams',headers={'Authorization':'Bearer student-sub'})
    assert response.status_code==401
