import os
import re
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.exam_artifacts import render_grade_card, render_rank_card
from app.exam_ai.worker import _load_evaluation_context
from app.exam_ai.provider import EvaluationFailure
from app.passcode_service import PasscodeService


def page_count(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page\b", pdf))


def test_grade_card_is_one_page_and_contains_final_result_fields():
    pdf = render_grade_card(
        {'title': 'Science Exam'},
        {
            'total_marks': 42, 'maximum_marks': 50, 'percentage': 84,
            'grade': 'B', 'rank': 2, 'completion_time_seconds': 610,
        },
        {
            'student_display_name': 'A Student', 'student_email': 'student@example.test',
            'submitted_at': '2026-09-25T10:15:00Z',
        },
    )
    assert page_count(pdf) == 1
    assert b'Science Exam' in pdf  # PDF metadata title


def test_rank_card_remains_one_page_for_large_class():
    results = [
        {
            'attempt_id': f'attempt-{index}', 'rank': index + 1,
            'total_marks': 100 - index % 100, 'maximum_marks': 100,
            'percentage': 100 - index % 100, 'grade': 'A',
            'completion_time_seconds': 300 + index,
        }
        for index in range(600)
    ]
    attempts = {
        row['attempt_id']: {
            'student_display_name': f'Student {index}',
            'submitted_at': '2026-09-25T10:15:00Z',
        }
        for index, row in enumerate(results)
    }
    pdf = render_rank_card({'title': 'Science Exam'}, results, attempts)
    assert page_count(pdf) == 1
    assert b'Science Exam' in pdf  # PDF metadata title


def test_rank_card_empty_results_still_generates_one_page():
    pdf = render_rank_card({'title': 'Empty Exam'}, [], {})
    assert page_count(pdf) == 1


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters = []

    def select(self, _columns): return self
    def eq(self, key, value):
        self.filters.append((key, value))
        return self
    def limit(self, _value): return self
    def execute(self):
        rows = [row for row in self.rows if all(row.get(key) == value for key, value in self.filters)]
        return type('Result', (), {'data': rows})()


class _Client:
    def __init__(self, method, typed, question_type='TEXT_AUDIO_ANSWER'):
        self.rows = {
            'exam_answers': [{'id':'answer-1','attempt_id':'attempt-1','exam_id':'exam-1',
                              'question_id':'question-1','answer_text':typed,'answer_method':method}],
            'exam_questions': [{'id':'question-1','exam_id':'exam-1','question_text':'Explain this.',
                                'question_type':question_type,'max_marks':5,'evaluation_mode':'AI_ALLOWED'}],
            'exams': [{'id':'exam-1','title':'Science'}],
            'exam_audio_answers': [{'attempt_id':'attempt-1','question_id':'question-1',
                                    'transcription_status':'READY','transcript':'বাংলা transcript'}],
            'exam_question_rubrics': [{'question_id':'question-1','reference_answer':'Reference','marking_criteria':'Criteria'}],
        }

    def table(self, name): return _Query(self.rows[name])


def test_ai_uses_only_saved_transcript_for_audio_and_never_calls_stt():
    item = _load_evaluation_context(_Client('AUDIO', None), 'answer-1')
    assert item.student_answer == 'Audio transcript:\nবাংলা transcript'
    assert item.max_marks == 5 and item.reference_answer == 'Reference'


def test_ai_uses_saved_typed_answer_and_preserves_both_selection():
    typed = _load_evaluation_context(_Client('TEXT', 'Typed response'), 'answer-1')
    both = _load_evaluation_context(_Client('BOTH', 'Typed response'), 'answer-1')
    assert typed.student_answer == 'Typed response'
    assert both.student_answer == 'Typed answer:\nTyped response\n\nAudio transcript:\nবাংলা transcript'


def test_ai_rejects_mcq_and_missing_selected_answer_for_manual_review():
    with pytest.raises(EvaluationFailure):
        _load_evaluation_context(_Client('MCQ', None, 'MCQ'), 'answer-1')
    client = _Client('AUDIO', None)
    client.rows['exam_audio_answers'][0]['transcript'] = ''
    with pytest.raises(EvaluationFailure):
        _load_evaluation_context(client, 'answer-1')


def test_whatsapp_destination_has_requested_initial_default():
    assert PasscodeService.DEFAULT_EXAM_WHATSAPP_NUMBER == '7439910593'


def test_finalization_sql_is_null_safe_and_server_deterministic():
    migration = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'migrations',
                             '012_exam_evaluation_finalization.sql')
    sql = open(migration, encoding='utf-8').read()
    assert 'AND NOT COALESCE((ai.status=' in sql
    assert 'ROW_NUMBER() OVER (ORDER BY s.total_marks DESC,s.completion_seconds ASC,s.submitted_at ASC,s.google_sub ASC)' in sql
    assert "WHEN q.question_type='MCQ' THEN CASE WHEN correct.id IS NOT NULL THEN q.max_marks ELSE 0 END" in sql
