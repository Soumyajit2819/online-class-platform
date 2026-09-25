"""Deterministic one-page PDF rendering and private artifact persistence."""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timezone

from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

from .config import settings


def _pdf_canvas(output, size):
    return canvas.Canvas(output, pagesize=size, invariant=1, pageCompression=1)


def _utc(value):
    if not value:
        return '—'
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    except (TypeError, ValueError):
        return '—'


def _duration(seconds):
    seconds = max(0, int(seconds or 0))
    return f'{seconds // 60}m {seconds % 60}s'


def _pdf_text(value) -> str:
    """Keep built-in Helvetica rendering safe for names unsupported by WinAnsi."""
    return str(value).encode('cp1252', errors='replace').decode('cp1252')


def render_grade_card(exam: dict, result: dict, attempt: dict) -> bytes:
    output = io.BytesIO()
    page = A4
    pdf = _pdf_canvas(output, page)
    width, height = page
    pdf.setTitle(f"Grade Card - {exam.get('title', 'Exam')}")
    pdf.setAuthor('Online Class Platform')
    pdf.setFont('Helvetica-Bold', 22)
    pdf.drawCentredString(width / 2, height - 88, 'GRADE CARD')
    pdf.setStrokeColorRGB(.25, .35, .65)
    pdf.setLineWidth(2)
    pdf.line(55, height - 108, width - 55, height - 108)
    student = attempt.get('student_display_name') or 'Student'
    lines = [
        ('Student name', student),
        ('Student identity', attempt.get('student_email') or result.get('google_sub') or 'Authenticated Google account'),
        ('Exam', exam.get('title') or 'Exam'),
        ('Total marks', f"{result.get('total_marks', 0)} / {result.get('maximum_marks', 0)}"),
        ('Percentage', f"{float(result.get('percentage') or 0):.2f}%"),
        ('Grade', str(result.get('grade') or '—')),
        ('Rank', str(result.get('rank') or '—')),
        ('Completion time', _duration(result.get('completion_time_seconds'))),
        ('Submitted at', _utc(attempt.get('submitted_at'))),
        ('Final status', 'FINALIZED'),
    ]
    y = height - 155
    for label, value in lines:
        pdf.setFont('Helvetica-Bold', 10)
        pdf.setFillColorRGB(.25, .30, .40)
        pdf.drawString(65, y, label)
        pdf.setFont('Helvetica', 12)
        pdf.setFillColorRGB(.05, .08, .14)
        pdf.drawString(205, y, _pdf_text(value)[:100])
        y -= 42
    pdf.setFont('Helvetica', 9)
    pdf.setFillColorRGB(.4, .45, .52)
    pdf.drawCentredString(width / 2, 48, 'Issued after teacher finalization · Results are immutable')
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def render_rank_card(exam: dict, results: list[dict], attempts_by_id: dict[str, dict]) -> bytes:
    output = io.BytesIO()
    page = landscape(A4)
    pdf = _pdf_canvas(output, page)
    width, height = page
    pdf.setTitle(f"Final Rank Card - {exam.get('title', 'Exam')}")
    pdf.setAuthor('Online Class Platform')
    pdf.setFont('Helvetica-Bold', 18)
    pdf.drawCentredString(width / 2, height - 34, 'FINAL RANK CARD')
    pdf.setFont('Helvetica', 9)
    pdf.drawCentredString(width / 2, height - 50, f"{exam.get('title', 'Exam')} · Exam date {_utc(exam.get('scheduled_start_at'))}")
    top = height - 75
    bottom = 28
    row_count = max(1, len(results))
    row_height = min(16, (top - bottom) / row_count)
    font_size = max(4.5, min(9, row_height * .56))
    columns = [32, 70, 310, 400, 480, 545, 615, width - 32]
    headers = ['Rank', 'Student', 'Marks', 'Percent', 'Grade', 'Time', 'Submitted']
    pdf.setFillColorRGB(.91, .93, .97)
    pdf.rect(24, top - row_height + 2, width - 48, row_height, fill=1, stroke=0)
    pdf.setFillColorRGB(.12, .16, .25)
    pdf.setFont('Helvetica-Bold', font_size)
    for index, label in enumerate(headers):
        pdf.drawString(columns[index], top - row_height * .70, label)
    y = top - row_height
    for result in results:
        attempt = attempts_by_id.get(result['attempt_id'], {})
        student = _pdf_text(attempt.get('student_display_name') or 'Student').replace('\n', ' ')
        values = [
            str(result.get('rank') or '—'), student,
            f"{result.get('total_marks', 0)}/{result.get('maximum_marks', 0)}",
            f"{float(result.get('percentage') or 0):.2f}%", str(result.get('grade') or '—'),
            _duration(result.get('completion_time_seconds')),
            _utc(attempt.get('submitted_at')),
        ]
        y -= row_height
        pdf.setFont('Helvetica', font_size)
        pdf.setFillColorRGB(.08, .10, .15)
        for index, value in enumerate(values):
            text = _pdf_text(value).replace('\n', ' ')
            max_chars = max(4, int((columns[index + 1] - columns[index] - 8) / max(font_size * .52, 1)))
            pdf.drawString(columns[index], y + row_height * .28, text[:max_chars])
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9_-]+', '-', value).strip('-')
    return cleaned[:64] or 'student'


def persist_exam_artifacts(client, exam_id: str) -> dict:
    """Generate or refresh all final cards into the private storage bucket."""
    exam_rows = client.table('exams').select('id,title,scheduled_start_at,status').eq('id', exam_id).limit(1).execute().data or []
    if not exam_rows or exam_rows[0].get('status') != 'FINALIZED':
        raise ValueError('Grade cards are available only after exam finalization')
    exam = exam_rows[0]
    results = client.table('exam_results').select('*').eq('exam_id', exam_id).order('rank').execute().data or []
    attempts = client.table('exam_attempts').select(
        'id,student_display_name,student_email,submitted_at'
    ).eq('exam_id', exam_id).execute().data or []
    by_id = {row['id']: row for row in attempts}
    bucket = client.storage.from_(settings.EXAM_ARTIFACTS_BUCKET)
    for result in results:
        attempt = by_id.get(result['attempt_id'], {})
        content = render_grade_card(exam, result, attempt)
        key = f"{exam_id}/grade-cards/{result['id']}.pdf"
        bucket.upload(key, content, {'content-type': 'application/pdf', 'upsert': 'true'})
        existing = client.table('exam_artifacts').select('id').eq('exam_id', exam_id).eq(
            'result_id', result['id']).eq('artifact_type', 'GRADE_CARD').limit(1).execute().data or []
        values = {'exam_id': exam_id, 'result_id': result['id'], 'artifact_type': 'GRADE_CARD',
                  'storage_key': key, 'status': 'READY', 'updated_at': datetime.now(timezone.utc).isoformat()}
        if existing:
            client.table('exam_artifacts').update(values).eq('id', existing[0]['id']).execute()
        else:
            client.table('exam_artifacts').insert(values).execute()
    rank_bytes = render_rank_card(exam, results, by_id)
    rank_key = f"{exam_id}/rank-card/final.pdf"
    bucket.upload(rank_key, rank_bytes, {'content-type': 'application/pdf', 'upsert': 'true'})
    existing = client.table('exam_artifacts').select('id').eq('exam_id', exam_id).is_('result_id', 'null').eq(
        'artifact_type', 'RANK_CARD').limit(1).execute().data or []
    values = {'exam_id': exam_id, 'result_id': None, 'artifact_type': 'RANK_CARD',
              'storage_key': rank_key, 'status': 'READY', 'updated_at': datetime.now(timezone.utc).isoformat()}
    if existing:
        client.table('exam_artifacts').update(values).eq('id', existing[0]['id']).execute()
    else:
        client.table('exam_artifacts').insert(values).execute()
    return {'grade_card_count': len(results), 'rank_card_ready': True}


def download_grade_cards_zip(client, exam_id: str) -> bytes:
    rows = client.table('exam_artifacts').select('result_id,storage_key,status').eq(
        'exam_id', exam_id).eq('artifact_type', 'GRADE_CARD').eq('status', 'READY').execute().data or []
    results = client.table('exam_results').select('id,rank').eq('exam_id', exam_id).execute().data or []
    rank = {row['id']: row.get('rank') for row in results}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            content = client.storage.from_(settings.EXAM_ARTIFACTS_BUCKET).download(row['storage_key'])
            archive.writestr(f"grade-card-rank-{rank.get(row['result_id'], 'unknown')}.pdf", content)
    return output.getvalue()
