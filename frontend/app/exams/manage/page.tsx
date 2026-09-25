'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import PasscodeGate from '@/components/PasscodeGate'
import { api, ExamQuestion, ExamQuestionType, FinalExamResult, ManagedExam, ManagedExamAttempt } from '@/lib/api'

const blankExam = () => ({
  title: '', description: '', instructions: '', custom_instructions_enabled: false, custom_instructions: '',
  scheduled_start_at: localDateTime(new Date(Date.now() + 60 * 60 * 1000)),
  duration_minutes: 60, maximum_violations: 3, protected_mode_enabled: true,
  require_fullscreen: true, detect_visibility_change: true, detect_orientation_change: false,
  restrict_copy_paste: true, autosave_enabled: true, automatic_submission_enabled: true,
  allow_list_enabled: false,
})

export default function ExamsManagementPage() {
  const [unlocked, setUnlocked] = useState(false)
  const [ready, setReady] = useState(false)
  const [exams, setExams] = useState<ManagedExam[]>([])
  const [selected, setSelected] = useState<ManagedExam | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ManagedExam | null>(null)
  const [deletingExamId, setDeletingExamId] = useState<string | null>(null)
  const [view, setView] = useState<'list' | 'edit' | 'review'>('list')
  const [form, setForm] = useState(blankExam)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [shareUrl, setShareUrl] = useState('')
  const [question, setQuestion] = useState<QuestionDraft | null>(null)
  const [student, setStudent] = useState({ google_sub: '', display_name: '', email: '' })
  const [allowedStudents, setAllowedStudents] = useState<Array<{ google_sub: string; display_name?: string; email?: string }>>([])
  const [attempts, setAttempts] = useState<ManagedExamAttempt[]>([])
  const [results, setResults] = useState<FinalExamResult[]>([])
  const [notificationJobs, setNotificationJobs] = useState<Array<{ id: string; job_type: string; status: string; attempts: number; last_error: string | null; created_at: string; completed_at: string | null }>>([])

  const onUnauthorized = useCallback(() => {
    setUnlocked(false)
    setView('list')
    setSelected(null)
    setError('Your Exams management session expired. Enter the password again.')
  }, [])

  useEffect(() => {
    setUnlocked(Boolean(sessionStorage.getItem('exam_management_access_token')))
    setReady(true)
    window.addEventListener('exam-management-unauthorized', onUnauthorized)
    return () => window.removeEventListener('exam-management-unauthorized', onUnauthorized)
  }, [onUnauthorized])

  const refresh = useCallback(async () => {
    setLoading(true)
    try { setExams((await api.getManagedExams()).exams); setError('') }
    catch (e) { setError(message(e)) }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { if (unlocked) void refresh() }, [unlocked, refresh])

  const selectedExamId = selected?.id
  useEffect(() => {
    if (!unlocked || view === 'edit') return
    const refreshFromServer = async () => {
      try {
        const listing = await api.getManagedExams()
        setExams(listing.exams)
        if (view === 'review' && selectedExamId) {
          const [exam, result] = await Promise.all([
            api.getManagedExam(selectedExamId),
            api.getManagedExamAttempts(selectedExamId),
          ])
          setSelected(current => current?.id === selectedExamId ? exam : current)
          setAttempts(result.attempts)
          if (exam.status === 'FINALIZED') {
            const [finalResults, notifications] = await Promise.all([
              api.getManagedExamResults(selectedExamId), api.getManagedExamNotificationStatus(selectedExamId),
            ])
            setResults(finalResults.results); setNotificationJobs(notifications.jobs)
          } else { setResults([]); setNotificationJobs([]) }
        }
      } catch { /* request helpers surface expired management sessions through the auth event */ }
    }
    const timer = window.setInterval(() => { void refreshFromServer() }, 15000)
    return () => window.clearInterval(timer)
  }, [unlocked, view, selectedExamId])

  async function openExam(id: string, next: 'edit' | 'review' = 'edit') {
    setLoading(true); setError(''); setNotice('')
    try {
      const exam = await api.getManagedExam(id)
      const attemptData = next === 'review' ? await api.getManagedExamAttempts(id) : null
      setSelected(exam)
      setAttempts(attemptData?.attempts || [])
      if (exam.status === 'FINALIZED') {
        const [finalResults, notifications] = await Promise.all([
          api.getManagedExamResults(id), api.getManagedExamNotificationStatus(id),
        ])
        setResults(finalResults.results); setNotificationJobs(notifications.jobs)
      } else { setResults([]); setNotificationJobs([]) }
      setForm({
        title: exam.title, description: exam.description || '', instructions: '',
        custom_instructions_enabled: exam.custom_instructions_enabled || false,
        custom_instructions: exam.custom_instructions || '',
        scheduled_start_at: localDateTime(new Date(exam.scheduled_start_at)),
        duration_minutes: exam.duration_minutes, maximum_violations: exam.maximum_violations,
        protected_mode_enabled: exam.protected_mode_enabled, require_fullscreen: exam.require_fullscreen,
        detect_visibility_change: exam.detect_visibility_change, detect_orientation_change: exam.detect_orientation_change,
        restrict_copy_paste: exam.restrict_copy_paste, autosave_enabled: exam.autosave_enabled,
        automatic_submission_enabled: exam.automatic_submission_enabled, allow_list_enabled: exam.allow_list_enabled,
      })
      setAllowedStudents(exam.allowed_students || [])
      setView(next)
    } catch (e) { setError(message(e)) }
    finally { setLoading(false) }
  }

  async function createExam(event: React.FormEvent) {
    event.preventDefault(); setLoading(true); setError('')
    try {
      const created = await api.createManagedExam({
        title: form.title.trim(),
        description: form.description.trim() || null,
        custom_instructions_enabled: form.custom_instructions_enabled,
        custom_instructions: form.custom_instructions_enabled ? form.custom_instructions.trim() || null : null,
        scheduled_start_at: new Date(form.scheduled_start_at).toISOString(),
        duration_minutes: form.duration_minutes,
        maximum_violations: form.maximum_violations,
        protected_mode_enabled: form.protected_mode_enabled,
        require_fullscreen: form.require_fullscreen,
        detect_visibility_change: form.detect_visibility_change,
        detect_orientation_change: form.detect_orientation_change,
        restrict_copy_paste: form.restrict_copy_paste,
        autosave_enabled: form.autosave_enabled,
        automatic_submission_enabled: form.automatic_submission_enabled,
        allow_list_enabled: form.allow_list_enabled,
      })
      setNotice(created.public_token ? 'Draft created. The private student link is available below; share it after scheduling.' : 'Exam draft created.')
      if (created.public_token) setShareUrl(`${window.location.origin}/exam/${encodeURIComponent(created.public_token)}`)
      await refresh(); await openExam(created.id)
    } catch (e) { setError(message(e)) }
    finally { setLoading(false) }
  }

  async function saveExam() {
    if (!selected) return
    setLoading(true); setError(''); setNotice('')
    try {
      const saved = await api.updateManagedExam(selected.id, {
        ...form, instructions: undefined,
        custom_instructions: form.custom_instructions_enabled ? form.custom_instructions.trim() || null : null,
        scheduled_start_at: new Date(form.scheduled_start_at).toISOString(),
      })
      setSelected({ ...selected, ...saved }); setNotice('Exam settings saved.'); await refresh()
    } catch (e) { setError(message(e)) }
    finally { setLoading(false) }
  }

  async function removeQuestion(q: ExamQuestion) {
    if (!selected || !window.confirm(`Delete question ${q.question_number}?`)) return
    try { await api.deleteManagedQuestion(selected.id, q.id); await openExam(selected.id) }
    catch (e) { setError(message(e)) }
  }

  async function addStudent(event: React.FormEvent) {
    event.preventDefault(); if (!selected) return
    try {
      await api.addAllowedStudent(selected.id, student)
      setAllowedStudents((await api.getAllowedStudents(selected.id)).students)
      setStudent({ google_sub: '', display_name: '', email: '' }); setNotice('Allowed student added.')
    } catch (e) { setError(message(e)) }
  }

  async function schedule() {
    if (!selected) return
    setLoading(true); setError('')
    try { await api.scheduleManagedExam(selected.id); setNotice('Exam scheduled successfully.'); await refresh(); await openExam(selected.id, 'review') }
    catch (e) { setError(message(e)) }
    finally { setLoading(false) }
  }

  async function finalize() {
    if (!selected || !window.confirm('Finalize this exam? Results and evaluations will become immutable.')) return
    setLoading(true); setError(''); setNotice('')
    try {
      const response = await api.finalizeManagedExam(selected.id)
      setResults(response.results)
      if (response.artifacts_error) {
        setNotice('Exam finalized with server-calculated results. PDF generation did not complete; use the card actions to retry generation.')
      } else {
        setNotice('Exam finalized. Final marks and ranks are calculated by the server.')
      }
      await refresh(); await openExam(selected.id, 'review')
    } catch (e) { setError(message(e)) }
    finally { setLoading(false) }
  }

  async function sendWhatsApp(kind: 'grade-cards' | 'rank-card') {
    if (!selected) return
    setError(''); setNotice('')
    try {
      const response = kind === 'grade-cards' ? await api.sendManagedExamGradeCards(selected.id) : await api.sendManagedExamRankCard(selected.id)
      setNotice(response.message)
      setNotificationJobs((await api.getManagedExamNotificationStatus(selected.id)).jobs)
    } catch (e) { setError(message(e)) }
  }

  async function downloadGradeCards() {
    if (!selected) return
    try {
      const blob = await api.downloadManagedExamGradeCards(selected.id)
      downloadBlob(blob, `exam-${selected.id}-grade-cards.zip`)
    } catch (e) { setError(message(e)) }
  }

  async function deleteExam() {
    if (!deleteTarget || deletingExamId) return
    const target = deleteTarget
    setDeletingExamId(target.id); setError(''); setNotice('')
    try {
      const result = await api.deleteManagedExam(target.id)
      setExams(current => current.filter(exam => exam.id !== target.id))
      setDeleteTarget(null)
      setNotice(result.storage_cleanup_complete === false
        ? 'Exam deleted successfully. Some private files could not be removed; contact support with the server logs.'
        : 'Exam deleted successfully.')
    } catch (e) {
      setError(message(e))
    } finally {
      setDeletingExamId(null)
    }
  }

  async function viewGradeCard(resultId: string) {
    if (!selected) return
    const popup = window.open('about:blank', '_blank')
    try {
      const blob = await api.getManagedExamGradeCard(selected.id, resultId)
      const url = URL.createObjectURL(blob)
      if (popup) popup.location.replace(url)
      else { URL.revokeObjectURL(url); setError('Your browser blocked the grade card window. Allow pop-ups and try again.') }
    } catch (e) { popup?.close(); setError(message(e)) }
  }

  async function viewRankCard(download: boolean) {
    if (!selected) return
    const popup = download ? null : window.open('about:blank', '_blank')
    try {
      const blob = await api.getManagedExamRankCard(selected.id, download)
      const url = URL.createObjectURL(blob)
      if (download) { downloadBlob(blob, `exam-${selected.id}-rank-card.pdf`); URL.revokeObjectURL(url) }
      else if (popup) popup.location.replace(url)
      else { URL.revokeObjectURL(url); setError('Your browser blocked the rank card window. Allow pop-ups and try again.') }
    } catch (e) { popup?.close(); setError(message(e)) }
  }

  // A new draft has no selected record yet, but its form must still be editable.
  const editable = !selected || selected.status === 'DRAFT' || selected.status === 'SCHEDULED'
  if (!ready) return <div className="min-h-screen bg-slate-50" />
  if (!unlocked) return <PasscodeGate title="Exams Management" description="Enter the shared Exams management password to manage exams." icon="📝" onVerify={async (password) => { await api.verifyExamManagementPassword(password); return true }} onSuccess={() => { setError(''); setUnlocked(true) }} />

  return <main className="min-h-screen bg-[#f5f7fb] text-slate-900">
    <header className="bg-slate-950 text-white">
      <div className="mx-auto flex max-w-7xl items-center justify-between px-5 py-5">
        <div><Link href="/" className="text-xs text-slate-400 hover:text-white">ONLINE CLASS PLATFORM</Link><h1 className="mt-1 text-2xl font-semibold">Exams management</h1></div>
        <button onClick={() => { api.clearExamManagementSession(); setUnlocked(false) }} className="rounded-lg border border-slate-700 px-3 py-2 text-sm hover:bg-slate-800">Lock dashboard</button>
      </div>
    </header>
    <div className="mx-auto max-w-7xl px-5 py-8">
      {error && <Alert tone="error" onClose={() => setError('')}>{error}</Alert>}
      {notice && <Alert tone="success" onClose={() => setNotice('')}>{notice}</Alert>}
      {deleteTarget && <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/60 p-4" role="presentation">
        <section role="dialog" aria-modal="true" aria-labelledby="delete-exam-title" aria-describedby="delete-exam-warning" className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl">
          <div className="flex items-start gap-4"><span aria-hidden="true" className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-rose-100 text-xl text-rose-700">!</span><div><h2 id="delete-exam-title" className="text-xl font-semibold text-slate-950">Delete Exam?</h2><p className="mt-1 font-medium text-slate-700">{deleteTarget.title}</p></div></div>
          <p id="delete-exam-warning" className="mt-4 text-sm leading-relaxed text-slate-600">This will permanently delete this exam and all of its attempts, answers, evaluations, submissions, and related exam data. This action cannot be undone.</p>
          {error && <p role="alert" className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
          <div className="mt-6 flex justify-end gap-3"><button type="button" disabled={deletingExamId !== null} onClick={() => setDeleteTarget(null)} className="rounded-lg border border-slate-300 px-4 py-2.5 text-sm font-semibold text-slate-700 disabled:opacity-50">Cancel</button><button type="button" disabled={deletingExamId !== null} onClick={() => void deleteExam()} className="rounded-lg bg-rose-700 px-4 py-2.5 text-sm font-bold text-white hover:bg-rose-800 disabled:cursor-not-allowed disabled:opacity-50">{deletingExamId === deleteTarget.id ? 'Deleting…' : 'Delete Exam Permanently'}</button></div>
        </section>
      </div>}
      {shareUrl && <div className="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-3 text-sm"><span className="font-medium text-indigo-900">Private student link</span><a href={shareUrl} target="_blank" rel="noreferrer" className="break-all font-semibold text-indigo-700 underline">{shareUrl}</a></div>}
      {view === 'list' ? <>
        <div className="mb-7 flex flex-wrap items-end justify-between gap-4">
          <div><p className="text-sm font-medium uppercase tracking-[.14em] text-indigo-600">Teacher workspace</p><h2 className="mt-1 text-3xl font-semibold tracking-tight">Your exams</h2><p className="mt-2 text-slate-500">Create, configure, and schedule exams from one place.</p></div>
          <button onClick={() => { setForm(blankExam()); setSelected(null); setView('edit'); setNotice(''); setShareUrl(''); setError('') }} className="rounded-xl bg-indigo-600 px-5 py-3 font-semibold text-white shadow-sm hover:bg-indigo-700">＋ Create exam</button>
        </div>
        <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4"><h3 className="font-semibold">All exams <span className="ml-2 rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500">{exams.length}</span></h3><button onClick={() => void refresh()} className="text-sm font-medium text-indigo-600">Refresh</button></div>
          {loading && !exams.length ? <div className="p-10 text-center text-slate-500">Loading exams…</div> : exams.length === 0 ? <div className="px-6 py-16 text-center"><div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-indigo-50 text-2xl">✦</div><h3 className="text-lg font-semibold">Start with your first exam</h3><p className="mx-auto mt-2 max-w-md text-sm text-slate-500">Add the schedule, build questions and answer keys, then review before publishing.</p><button onClick={() => { setForm(blankExam()); setSelected(null); setView('edit') }} className="mt-5 rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white">Create an exam</button></div> : <div className="divide-y divide-slate-100">{exams.map(exam => <ExamRow key={exam.id} exam={exam} onEdit={() => void openExam(exam.id)} onReview={() => void openExam(exam.id, 'review')} onCancel={async () => { try { await api.cancelManagedExam(exam.id); await refresh() } catch (e) { setError(message(e)) } }} onDelete={() => { setError(''); setDeleteTarget(exam) }} />)}</div>}
        </section>
      </> : <>
        <button onClick={() => { setView('list'); setQuestion(null); void refresh() }} className="mb-5 text-sm font-medium text-slate-500 hover:text-slate-900">← All exams</button>
        <div className="mb-6 flex flex-wrap items-start justify-between gap-4"><div><p className="text-sm text-indigo-600">{selected ? `Exam · ${selected.status}` : 'New exam · Draft'}</p><h2 className="mt-1 text-3xl font-semibold">{view === 'review' ? 'Review and schedule' : selected ? 'Configure exam' : 'Create an exam'}</h2></div>{selected && (selected.status === 'FINALIZED' ? <span className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-2 text-sm font-medium text-slate-500">Finalized · editing locked</span> : <button onClick={() => void openExam(selected.id, view === 'edit' ? 'review' : 'edit')} className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold">{view === 'edit' ? 'Review exam →' : '← Edit exam'}</button>)}</div>
        {view === 'edit' ? <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
          <div className="space-y-6">
            <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm"><SectionTitle number="01" title="Exam details" subtitle="Give students the context and schedule." />
              <form onSubmit={selected ? e => { e.preventDefault(); void saveExam() } : createExam} className="mt-6 grid gap-4 sm:grid-cols-2">
                <Field label="Exam title *" className="sm:col-span-2"><input required maxLength={200} value={form.title} disabled={!editable} onChange={e => setForm({ ...form, title: e.target.value })} className={inputClass} placeholder="e.g. Algebra — Chapter 4" /></Field>
                <Field label="Description"><textarea maxLength={10000} rows={2} value={form.description} disabled={!editable} onChange={e => setForm({ ...form, description: e.target.value })} className={inputClass} placeholder="A short overview (optional)" /></Field>
                <div className="sm:col-span-2 rounded-xl border border-indigo-100 bg-indigo-50/50 p-4"><label className="flex items-center gap-2 text-sm font-semibold text-slate-800"><input type="checkbox" checked={form.custom_instructions_enabled} disabled={!editable} onChange={e => setForm({ ...form, custom_instructions_enabled:e.target.checked })} />Customize Instructions</label><p className="ml-6 mt-1 text-xs text-slate-500">Mandatory system instructions always appear and cannot be removed.</p>{form.custom_instructions_enabled && <Field label="Optional teacher instructions" className="mt-3"><textarea maxLength={10000} rows={4} value={form.custom_instructions} disabled={!editable} onChange={e => setForm({ ...form, custom_instructions:e.target.value })} className={inputClass} placeholder="Add exam-specific information for students…" /></Field>}</div>
                <Field label="Scheduled start *"><input required type="datetime-local" value={form.scheduled_start_at} disabled={!editable} onChange={e => setForm({ ...form, scheduled_start_at: e.target.value })} className={inputClass} /></Field>
                <Field label="Duration (minutes) *"><input required type="number" min={1} max={1440} value={form.duration_minutes} disabled={!editable} onChange={e => setForm({ ...form, duration_minutes: Number(e.target.value) })} className={inputClass} /></Field>
                <Field label="Maximum violations"><input type="number" min={0} max={100} value={form.maximum_violations} disabled={!editable} onChange={e => setForm({ ...form, maximum_violations: Number(e.target.value) })} className={inputClass} /></Field>
                {!selected && <div className="sm:col-span-2"><button disabled={loading} className="rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">{loading ? 'Creating…' : 'Create draft exam'}</button><p className="mt-2 text-xs text-slate-400">Total marks are calculated from the marks assigned to each question after creating the draft. The timer starts only when a student explicitly begins an attempt.</p></div>}
                {selected && <div className="sm:col-span-2"><button disabled={loading || !editable} className="rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">Save exam details</button></div>}
              </form>
            </section>
            {selected && <>
              <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm"><div className="flex items-start justify-between gap-3"><SectionTitle number="02" title="Questions" subtitle="Build each prompt, marking setup, and answer key." /><button disabled={!editable} onClick={() => setQuestion(emptyQuestion())} className="shrink-0 rounded-lg bg-slate-900 px-3 py-2 text-sm font-semibold text-white disabled:opacity-40">＋ Add question</button></div>
                <div className="mt-5 space-y-3">{(selected.questions || []).length === 0 ? <p className="rounded-xl bg-slate-50 p-6 text-center text-sm text-slate-500">No questions yet. Add your first question.</p> : selected.questions!.map(q => <div key={q.id} className="flex items-start justify-between gap-4 rounded-xl border border-slate-200 p-4"><div><div className="flex flex-wrap items-center gap-2"><span className="text-xs font-semibold text-indigo-600">Q{q.question_number}</span><Tag>{q.question_type.replace('_',' ')}</Tag><Tag>{q.max_marks} marks</Tag></div><p className="mt-2 font-medium">{q.question_text}</p>{q.question_type === 'MCQ' && <p className="mt-1 text-sm text-slate-500">{q.options.length} options · correct answer configured</p>}</div><div className="flex shrink-0 gap-2"><button disabled={!editable} onClick={() => setQuestion(questionFrom(q))} className="rounded-lg border px-3 py-1.5 text-sm disabled:opacity-40">Edit</button><button disabled={!editable} onClick={() => void removeQuestion(q)} className="rounded-lg border border-rose-200 px-3 py-1.5 text-sm text-rose-600 disabled:opacity-40">Delete</button></div></div>)}</div>
                {(selected.questions || []).length > 1 && editable && <div className="mt-4 flex flex-wrap gap-2">{selected.questions!.map((q, i) => <div className="flex gap-1" key={q.id}><span className="self-center text-xs text-slate-400">{q.question_number}</span><button disabled={i === 0} onClick={() => void reorder(selected, i, i - 1, openExam, setError)} className="rounded border px-2 disabled:opacity-30" title="Move up">↑</button><button disabled={i === selected.questions!.length - 1} onClick={() => void reorder(selected, i, i + 1, openExam, setError)} className="rounded border px-2 disabled:opacity-30" title="Move down">↓</button></div>)}</div>}
              </section>
              {question && <QuestionEditor key={question.id || 'new'} initial={question} onClose={() => setQuestion(null)} onSave={async data => { try { if (question.id) await api.updateManagedQuestion(selected.id, question.id, data); else await api.addManagedQuestion(selected.id, data); setQuestion(null); await openExam(selected.id) } catch (e) { setError(message(e)) } }} />}
              <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm"><SectionTitle number="03" title="Student access" subtitle="Google sub is the identity key. Name and email are display details." />
                <div className="mt-5 grid gap-3 sm:grid-cols-2"><label className={`cursor-pointer rounded-xl border p-4 ${!form.allow_list_enabled ? 'border-indigo-300 bg-indigo-50' : ''}`}><input type="radio" checked={!form.allow_list_enabled} disabled={!editable} onChange={() => setForm({ ...form, allow_list_enabled: false })} /> <b className="ml-2 text-sm">Open access</b><p className="ml-6 mt-1 text-xs text-slate-500">Any authenticated Google account can participate.</p></label><label className={`cursor-pointer rounded-xl border p-4 ${form.allow_list_enabled ? 'border-indigo-300 bg-indigo-50' : ''}`}><input type="radio" checked={form.allow_list_enabled} disabled={!editable} onChange={() => setForm({ ...form, allow_list_enabled: true })} /> <b className="ml-2 text-sm">Allowed students only</b><p className="ml-6 mt-1 text-xs text-slate-500">Only listed Google sub values are eligible.</p></label></div>
                {form.allow_list_enabled && <><form onSubmit={addStudent} className="mt-4 grid gap-3 rounded-xl bg-slate-50 p-4 sm:grid-cols-3"><Field label="Google sub *"><input required value={student.google_sub} disabled={!editable} onChange={e => setStudent({ ...student, google_sub: e.target.value })} className={inputClass} /></Field><Field label="Display name"><input value={student.display_name} disabled={!editable} onChange={e => setStudent({ ...student, display_name: e.target.value })} className={inputClass} /></Field><Field label="Email"><input type="email" value={student.email} disabled={!editable} onChange={e => setStudent({ ...student, email: e.target.value })} className={inputClass} /></Field><button disabled={!editable} className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40 sm:col-span-3">Add allowed student</button></form><div className="mt-3 divide-y rounded-xl border">{allowedStudents.map(s => <div key={s.google_sub} className="flex items-center justify-between gap-3 px-4 py-3 text-sm"><div><b>{s.display_name || s.google_sub}</b>{s.email && <span className="ml-2 text-slate-500">{s.email}</span>}<div className="text-xs text-slate-400">sub: {s.google_sub}</div></div><button disabled={!editable} onClick={async () => { try { await api.removeAllowedStudent(selected.id, s.google_sub); setAllowedStudents((await api.getAllowedStudents(selected.id)).students) } catch (e) { setError(message(e)) } }} className="text-rose-600 disabled:opacity-40">Remove</button></div>)}{allowedStudents.length === 0 && <p className="p-4 text-sm text-slate-500">No students added yet.</p>}</div></>}
              </section>
              <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm"><SectionTitle number="04" title="Exam protection" subtitle="These settings are configuration only in this phase." /><div className="mt-4 grid gap-2 sm:grid-cols-2">{protectionFields.map(([key, label]) => <label key={key} className="flex items-center gap-3 rounded-lg bg-slate-50 px-3 py-3 text-sm"><input type="checkbox" checked={Boolean(form[key as keyof typeof form])} disabled={!editable} onChange={e => setForm({ ...form, [key]: e.target.checked })} />{label}</label>)}</div>{selected && <button onClick={() => void saveExam()} disabled={loading || !editable} className="mt-4 rounded-lg border border-slate-300 px-4 py-2 text-sm font-semibold disabled:opacity-40">Save settings</button>}</section>
            </>}
          </div>
          <aside className="h-fit rounded-2xl bg-slate-900 p-5 text-white"><p className="text-sm font-semibold text-indigo-300">Publish checklist</p><div className="mt-4 space-y-3 text-sm">{[[Boolean(form.title.trim()),'Exam title'],[Boolean(form.scheduled_start_at),'Scheduled start'],[Boolean(selected?.questions?.length),'At least one question'],[!form.allow_list_enabled || allowedStudents.length > 0,'Student access configured']].map(([ok,label]) => <div key={String(label)} className="flex items-center gap-2"><span className={ok ? 'text-emerald-400' : 'text-slate-500'}>{ok ? '✓' : '○'}</span><span className={ok ? '' : 'text-slate-400'}>{label}</span></div>)}</div>{selected && <button onClick={() => setView('review')} className="mt-5 w-full rounded-lg bg-white px-4 py-2.5 text-sm font-semibold text-slate-900">Review exam →</button>}</aside>
        </div> : selected && <Review exam={selected} form={form} students={allowedStudents} attempts={attempts} results={results} notificationJobs={notificationJobs} onViewGradeCard={id => void viewGradeCard(id)} onEdit={() => setView('edit')} onSchedule={() => void schedule()} onFinalize={() => void finalize()} onViewRankCard={() => void viewRankCard(false)} onDownloadRankCard={() => void viewRankCard(true)} onDownloadGradeCards={() => void downloadGradeCards()} onSendGradeCards={() => void sendWhatsApp('grade-cards')} onSendRankCard={() => void sendWhatsApp('rank-card')} onCancel={async () => { try { await api.cancelManagedExam(selected.id); await refresh(); await openExam(selected.id, 'review') } catch (e) { setError(message(e)) } }} loading={loading} />}
      </>}
    </div>
  </main>
}

type QuestionDraft = { id?: string; question_text: string; question_type: ExamQuestionType; max_marks: number; is_required: boolean; evaluation_mode: 'AI_ALLOWED'|'MANUAL_ONLY'; reference_answer: string; marking_criteria: string; options: Array<{ text: string; is_correct: boolean }> }
function emptyQuestion(): QuestionDraft { return { question_text: '', question_type: 'MCQ', max_marks: 1, is_required: true, evaluation_mode: 'MANUAL_ONLY', reference_answer: '', marking_criteria: '', options: [{ text: '', is_correct: true }, { text: '', is_correct: false }] } }
function questionFrom(q: ExamQuestion): QuestionDraft { return { id:q.id, question_text:q.question_text, question_type:q.question_type, max_marks:q.max_marks, is_required:q.is_required, evaluation_mode:q.evaluation_mode || 'MANUAL_ONLY', reference_answer:q.reference_answer || '', marking_criteria:q.marking_criteria || '', options:q.options.map(o => ({ text:o.option_text || '', is_correct:o.is_correct })) } }
function QuestionEditor({ initial, onClose, onSave }: { initial: QuestionDraft; onClose:()=>void; onSave:(data:Record<string,unknown>)=>void }) {
  const [draft, setDraft] = useState(initial); const [error, setError] = useState('')
  const mcq = draft.question_type === 'MCQ'
  function submit(e: React.FormEvent) { e.preventDefault(); if (mcq && (draft.options.length < 2 || draft.options.some(o => !o.text.trim()) || draft.options.filter(o => o.is_correct).length !== 1)) { setError('Add at least two non-empty options and select exactly one correct answer.'); return } if (draft.max_marks <= 0) { setError('Maximum marks must be greater than zero.'); return } setError(''); onSave({ question_text:draft.question_text, question_type:draft.question_type, max_marks:draft.max_marks, is_required:draft.is_required, evaluation_mode:mcq ? 'MANUAL_ONLY' : draft.evaluation_mode, reference_answer:draft.reference_answer || null, marking_criteria:draft.marking_criteria || null, options:mcq ? draft.options : [] }) }
  return <div className="fixed inset-0 z-50 flex items-end justify-center bg-slate-950/50 p-0 sm:items-center sm:p-5"><form onSubmit={submit} className="max-h-[92vh] w-full max-w-2xl overflow-y-auto rounded-t-2xl bg-white p-6 shadow-xl sm:rounded-2xl"><div className="flex items-start justify-between"><div><p className="text-xs font-semibold uppercase tracking-wider text-indigo-600">Question builder</p><h3 className="mt-1 text-xl font-semibold">{draft.id ? 'Edit question' : 'Add a question'}</h3></div><button type="button" onClick={onClose} className="text-2xl text-slate-400">×</button></div>{error && <Alert tone="error">{error}</Alert>}<div className="mt-5 grid gap-4 sm:grid-cols-2"><Field label="Question type"><select value={draft.question_type} onChange={e => setDraft({ ...draft, question_type:e.target.value as ExamQuestionType })} className={inputClass}><option value="MCQ">MCQ</option><option value="TEXT_AUDIO_ANSWER">Text / Audio Answer</option></select></Field><Field label="Maximum marks"><input type="number" min="0.01" step="0.01" value={draft.max_marks} onChange={e => setDraft({ ...draft, max_marks:Number(e.target.value) })} className={inputClass} /></Field><Field label="Question text" className="sm:col-span-2"><textarea required maxLength={10000} rows={3} value={draft.question_text} onChange={e => setDraft({ ...draft, question_text:e.target.value })} className={inputClass} /></Field><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={draft.is_required} onChange={e => setDraft({ ...draft, is_required:e.target.checked })} />Required question</label>{!mcq && <Field label="Evaluation mode"><select value={draft.evaluation_mode} onChange={e => setDraft({ ...draft, evaluation_mode:e.target.value as QuestionDraft['evaluation_mode'] })} className={inputClass}><option value="MANUAL_ONLY">Manual only</option><option value="AI_ALLOWED">AI allowed (Qwen/OpenRouter advisory)</option></select></Field>}</div>{mcq ? <div className="mt-5"><div className="mb-2 flex justify-between"><h4 className="font-semibold">Answer options</h4><span className="text-xs text-slate-500">Choose one correct answer</span></div><div className="space-y-2">{draft.options.map((o,i) => <div key={i} className="flex items-center gap-3"><input type="radio" name="correct-option" checked={o.is_correct} onChange={() => setDraft({ ...draft, options:draft.options.map((x,j) => ({ ...x, is_correct:j===i })) })} /><input required value={o.text} onChange={e => setDraft({ ...draft, options:draft.options.map((x,j) => j===i ? {...x,text:e.target.value} : x) })} className={inputClass} placeholder={`Option ${i+1}`} /><button type="button" onClick={() => setDraft({ ...draft, options:draft.options.filter((_,j)=>j!==i) })} className="text-rose-600">Remove</button></div>)}</div><button type="button" onClick={() => setDraft({ ...draft, options:[...draft.options,{text:'',is_correct:false}] })} className="mt-3 text-sm font-semibold text-indigo-600">＋ Add option</button></div> : <div className="mt-5 grid gap-4"><Field label="Reference / teacher answer (optional)"><textarea rows={2} maxLength={10000} value={draft.reference_answer} onChange={e => setDraft({ ...draft, reference_answer:e.target.value })} className={inputClass} /></Field><Field label="Marking rubric (optional)"><textarea rows={2} maxLength={10000} value={draft.marking_criteria} onChange={e => setDraft({ ...draft, marking_criteria:e.target.value })} className={inputClass} /></Field></div>}<div className="mt-6 flex justify-end gap-2"><button type="button" onClick={onClose} className="rounded-lg border px-4 py-2 text-sm">Cancel</button><button className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white">Save question</button></div></form></div>
}

function Review({ exam, form, students, attempts, results, notificationJobs, onViewGradeCard, onEdit, onSchedule, onFinalize, onViewRankCard, onDownloadRankCard, onDownloadGradeCards, onSendGradeCards, onSendRankCard, onCancel, loading }: { exam:ManagedExam; form:ReturnType<typeof blankExam>; students:Array<{google_sub:string;display_name?:string;email?:string}>; attempts:ManagedExamAttempt[]; results:FinalExamResult[]; notificationJobs:Array<{id:string;job_type:string;status:string;attempts:number;last_error:string|null;created_at:string;completed_at:string|null}>; onViewGradeCard:(resultId:string)=>void; onEdit:()=>void; onSchedule:()=>void; onFinalize:()=>void; onViewRankCard:()=>void; onDownloadRankCard:()=>void; onDownloadGradeCards:()=>void; onSendGradeCards:()=>void; onSendRankCard:()=>void; onCancel:()=>void; loading:boolean }) {
  const finalized = exam.status === 'FINALIZED'
  const canFinalize = ['SUBMISSION_CLOSED','EVALUATING','MANUAL_REVIEW'].includes(exam.status)
  const [showGradeCards, setShowGradeCards] = useState(false)
  return <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]"><div className="space-y-5">
    <section className="rounded-2xl border bg-white p-6"><SectionTitle number="01" title={exam.title} subtitle={exam.description || 'No description provided.'} /><p className="mt-5 rounded-xl bg-indigo-50 p-4 text-sm">Mandatory system exam instructions are always shown to students.</p>{form.custom_instructions_enabled && form.custom_instructions && <div className="mt-3 whitespace-pre-wrap rounded-xl bg-slate-50 p-4 text-sm"><b>Optional teacher instructions</b><p className="mt-2">{form.custom_instructions}</p></div>}<div className="mt-4 grid gap-3 text-sm sm:grid-cols-3"><div><span className="text-slate-400">Scheduled</span><p className="font-medium">{formatDate(exam.scheduled_start_at)}</p></div><div><span className="text-slate-400">Duration</span><p className="font-medium">{exam.duration_minutes} minutes</p></div><div><span className="text-slate-400">Violations</span><p className="font-medium">{exam.maximum_violations} allowed</p></div></div><div className="mt-4 flex flex-wrap gap-2">{protectionFields.filter(([key]) => Boolean(form[key as keyof typeof form])).map(([,label]) => <Tag key={label}>{label}</Tag>)}</div></section>
    <section className="rounded-2xl border bg-white p-6"><SectionTitle number="02" title="Questions" subtitle={`${exam.questions?.length || 0} questions · ${exam.questions?.reduce((sum,q)=>sum+Number(q.max_marks),0) || 0} total marks`} /><div className="mt-4 space-y-3">{exam.questions?.map(q => <div key={q.id} className="rounded-xl border p-4"><div className="flex justify-between gap-4"><div><span className="text-xs font-semibold text-indigo-600">QUESTION {q.question_number}</span><p className="mt-1 font-medium">{q.question_text}</p></div><Tag>{q.max_marks} marks</Tag></div><p className="mt-2 text-xs text-slate-500">{q.question_type === 'MCQ' ? 'MCQ' : 'Text / Audio Answer'} · {q.is_required ? 'Required' : 'Optional'} · {q.evaluation_mode}</p>{q.options?.length > 0 && <div className="mt-3 grid gap-2 sm:grid-cols-2">{q.options.map(o => <p key={o.id} className={`rounded-lg px-3 py-2 text-sm ${o.is_correct ? 'bg-emerald-50 text-emerald-800' : 'bg-slate-50'}`}>{o.is_correct ? '✓ ' : ''}{o.option_text}</p>)}</div>}<p className="mt-2 text-xs text-slate-500">{q.reference_answer || q.marking_criteria ? 'Teacher reference/rubric configured' : 'No rubric/reference answer'}</p></div>)}</div></section>
    <section className="rounded-2xl border bg-white p-6"><SectionTitle number="03" title="Students" subtitle={form.allow_list_enabled ? `${students.length} allowed student${students.length===1?'':'s'}` : 'Open to any eligible authenticated Google account'} />{form.allow_list_enabled && <div className="mt-3 space-y-2">{students.map(s => <p key={s.google_sub} className="rounded-lg bg-slate-50 px-3 py-2 text-sm">{s.display_name || s.google_sub}{s.email ? ` · ${s.email}` : ''}</p>)}</div>}</section>
    <section className="rounded-2xl border bg-white p-6"><SectionTitle number="04" title="Attempts and student responses" subtitle={`${attempts.length} attempt${attempts.length===1?'':'s'} · status and saved answers`} />
      {attempts.length === 0 ? <p className="mt-4 rounded-lg bg-slate-50 p-4 text-sm text-slate-500">No student attempts yet.</p> : <div className="mt-4 space-y-4">{attempts.map(attempt => <article key={attempt.attempt_id} className="rounded-xl border border-slate-200 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3"><div><h4 className="font-semibold">{attempt.student.display_name || 'Student'}</h4><p className="mt-0.5 text-xs text-slate-500">{attempt.student.email || attempt.student.google_sub}</p></div><Tag>{attempt.status}</Tag></div>
        <div className="mt-3 grid gap-2 text-xs text-slate-500 sm:grid-cols-3"><p>Started: {attempt.started_at ? formatDate(attempt.started_at) : '—'}</p><p>Submitted: {attempt.submitted_at ? formatDate(attempt.submitted_at) : 'Not submitted'}</p><p>Completion: {attempt.completion_seconds === null ? '—' : formatDuration(attempt.completion_seconds)}</p></div>
        <div className="mt-4 space-y-3">{attempt.questions.map(q => <AttemptQuestionReview key={q.id} examId={exam.id} attemptId={attempt.attempt_id} question={q} canEvaluate={!finalized && ['SUBMITTED','AUTO_SUBMITTED','TERMINATED','MANUAL_REVIEW'].includes(attempt.status)} />)}</div>
      </article>)}</div>}
    </section>
    <section className="rounded-2xl border bg-white p-6"><SectionTitle number="05" title="Grade Cards" subtitle={finalized ? 'Server-calculated final results and PDF artifacts.' : 'Available after teacher finalization.'} />
      {!finalized && <p className="mt-4 rounded-lg bg-slate-50 p-3 text-sm text-slate-500">Finalize the exam after all required descriptive answers have an accepted AI evaluation or teacher mark. Grade card and WhatsApp actions are unavailable until then.</p>}
      <div className="mt-4 flex flex-wrap gap-2">
        <button disabled={!finalized} onClick={() => setShowGradeCards(value => !value)} className="rounded-lg border px-3 py-2 text-sm font-semibold disabled:opacity-40">View Grade Cards</button>
        <button disabled={!finalized} onClick={onViewRankCard} className="rounded-lg border px-3 py-2 text-sm font-semibold disabled:opacity-40">View Rank Card</button>
        <button disabled={!finalized} onClick={onDownloadGradeCards} className="rounded-lg border px-3 py-2 text-sm font-semibold disabled:opacity-40">Download Grade Cards</button>
        <button disabled={!finalized} onClick={onDownloadRankCard} className="rounded-lg border px-3 py-2 text-sm font-semibold disabled:opacity-40">Download Rank Card</button>
      </div>
      {finalized && <div className="mt-4 flex flex-wrap gap-2"><button onClick={onSendGradeCards} className="rounded-lg bg-emerald-700 px-3 py-2 text-sm font-semibold text-white">Send Grade Cards to WhatsApp</button><button onClick={onSendRankCard} className="rounded-lg bg-emerald-700 px-3 py-2 text-sm font-semibold text-white">Send Rank Card to WhatsApp</button><span className="self-center text-xs text-slate-500">Manual action only. No WhatsApp provider is currently configured.</span></div>}
      {showGradeCards && finalized && <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[700px] text-left text-sm"><thead><tr className="border-b text-xs text-slate-500"><th className="py-2">Rank</th><th>Student</th><th>Marks</th><th>Percentage</th><th>Grade</th><th>Time</th><th>Card</th></tr></thead><tbody>{results.map(row => <tr key={row.id} className="border-b last:border-0"><td className="py-2">{row.rank}</td><td>{row.student.display_name || 'Student'}{row.student.email && <span className="block text-xs text-slate-500">{row.student.email}</span>}</td><td>{row.total_marks} / {row.maximum_marks}</td><td>{Number(row.percentage).toFixed(2)}%</td><td>{row.grade}</td><td>{formatDuration(row.completion_time_seconds)}</td><td><button onClick={() => onViewGradeCard(row.id)} className="rounded border px-2 py-1 text-xs font-semibold">View PDF</button></td></tr>)}</tbody></table></div>}
      {notificationJobs.length > 0 && <div className="mt-4 space-y-2">{notificationJobs.map(job => <p key={job.id} className="rounded-lg bg-slate-50 p-3 text-xs"><b>{job.job_type.replaceAll('_',' ')} · {job.status}</b>{job.last_error && <span className="ml-2 text-rose-700">{job.last_error}</span>}</p>)}</div>}
    </section>
  </div><aside className="h-fit rounded-2xl bg-slate-900 p-5 text-white"><p className="text-lg font-semibold">{exam.status === 'SUBMISSION_CLOSED' ? 'Exam closed' : 'Exam status: ' + exam.status}</p><p className="mt-2 text-sm leading-relaxed text-slate-300">The exam lifecycle is refreshed by the server from the scheduled start and duration. Student attempt submissions remain stored separately.</p><div className="mt-5 grid gap-2"><button onClick={onEdit} disabled={finalized} className="rounded-lg border border-slate-700 px-4 py-2.5 text-sm font-semibold disabled:opacity-40">Edit exam</button>{exam.status === 'DRAFT' && <button onClick={onSchedule} disabled={loading} className="rounded-lg bg-indigo-500 px-4 py-2.5 text-sm font-semibold hover:bg-indigo-400 disabled:opacity-40">{loading ? 'Scheduling…' : 'Publish / Schedule'}</button>}{['DRAFT','SCHEDULED'].includes(exam.status) && <button onClick={onCancel} className="rounded-lg px-4 py-2 text-sm text-rose-300">Cancel exam</button>}{!finalized && <button onClick={onFinalize} disabled={!canFinalize || loading} className="rounded-lg bg-emerald-600 px-4 py-2.5 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-40">{loading ? 'Finalizing…' : 'Finalize Exam'}</button>}{finalized && <p className="rounded-lg bg-emerald-900/50 p-3 text-sm text-emerald-200">Finalized results are immutable.</p>}</div></aside></div>
}

function formatDuration(seconds:number) { const minutes=Math.floor(seconds/60); const remainder=seconds%60; return minutes ? `${minutes}m ${remainder}s` : `${remainder}s` }
function downloadBlob(blob:Blob, filename:string) { const url=URL.createObjectURL(blob); const link=document.createElement('a'); link.href=url; link.download=filename; link.click(); window.setTimeout(()=>URL.revokeObjectURL(url),1000) }

function AttemptQuestionReview({examId,attemptId,question,canEvaluate}:{examId:string;attemptId:string;question:ManagedExamAttempt['questions'][number];canEvaluate:boolean}) {
  const [audioUrl,setAudioUrl]=useState('')
  const [audioLoading,setAudioLoading]=useState(false)
  useEffect(()=>()=>{if(audioUrl)URL.revokeObjectURL(audioUrl)},[audioUrl])
  async function loadAudio(){setAudioLoading(true);try{const blob=await api.getManagedExamAudio(examId,attemptId,question.id);setAudioUrl(old=>{if(old)URL.revokeObjectURL(old);return URL.createObjectURL(blob)})}catch(e){window.alert(message(e))}finally{setAudioLoading(false)}}
  const methods=[question.answer_text?.trim()?'Typed':'',question.audio?'Audio transcript':''].filter(Boolean)
  return <div className="rounded-lg bg-slate-50 p-3"><p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Question {question.question_number} · {question.max_marks} marks</p><p className="mt-1 text-sm font-medium">{question.question_text}</p><p className="mt-2 text-xs text-slate-600">Answer method: {methods.length?methods.join(' + '):'No answer provided'}{question.answer_method?` · selected for evaluation: ${question.answer_method}`:''}</p>{question.question_type==='MCQ'&&<div className="mt-2 rounded-lg border bg-white p-3 text-sm"><p><b>Evaluation:</b> Automatic</p><p className="mt-1"><b>Selected answer:</b> {question.selected_option?.option_text||'No answer'}</p><p className="mt-1"><b>Marks:</b> {question.mcq_marks||0} / {question.max_marks}</p></div>}{question.selected_option&&<p className="mt-2 text-sm"><span className="font-medium">Selected option:</span> {question.selected_option.option_text}</p>}{question.answer_text&&<p className="mt-2 whitespace-pre-wrap text-sm"><span className="font-medium">Typed answer:</span> {question.answer_text}</p>}{question.audio&&<div className="mt-2 text-sm"><p><span className="font-medium">Audio answer:</span> {question.audio.transcription_status}{question.audio.duration_ms?` · ${(question.audio.duration_ms/1000).toFixed(1)} sec`:''} · {question.audio.audio_mime_type}</p>{question.audio.transcript&&<p className="mt-1 whitespace-pre-wrap"><span className="font-medium">Transcript:</span> {question.audio.transcript}</p>}{audioUrl?<audio className="mt-2 w-full" controls src={audioUrl}/>:<button type="button" disabled={audioLoading} onClick={()=>void loadAudio()} className="mt-2 rounded-lg border px-3 py-1.5 text-xs font-semibold">{audioLoading?'Loading private audio…':'Load private recording'}</button>}</div>}{!question.selected_option&&!question.answer_text&&!question.audio&&<p className="mt-2 text-sm italic text-slate-400">No answer saved</p>}{question.question_type==='TEXT_AUDIO_ANSWER'&&<><div className="mt-3 rounded-lg border border-amber-200 bg-white p-3 text-sm"><p className="font-semibold">Teacher reference and rubric</p><p className="mt-2 whitespace-pre-wrap"><span className="font-medium">Reference answer:</span> {question.reference_answer||'Not provided'}</p><p className="mt-2 whitespace-pre-wrap"><span className="font-medium">Marking criteria:</span> {question.marking_criteria||'Not provided'}</p></div><ManualEvaluation examId={examId} attemptId={attemptId} questionId={question.id} maxMarks={question.max_marks} existing={question.manual_review} aiEvaluation={question.ai_evaluation} disabled={!canEvaluate}/></>}</div>
}

function ManualEvaluation({examId,attemptId,questionId,maxMarks,existing,aiEvaluation,disabled}:{examId:string;attemptId:string;questionId:string;maxMarks:number;existing:ManagedExamAttempt['questions'][number]['manual_review'];aiEvaluation:ManagedExamAttempt['questions'][number]['ai_evaluation'];disabled:boolean}) {
  const [marks,setMarks]=useState(existing?String(existing.marks):aiEvaluation?.marks != null ? String(aiEvaluation.marks) : '0')
  const [feedback,setFeedback]=useState(existing?.teacher_comments||'')
  const [saving,setSaving]=useState(false)
  const [saved,setSaved]=useState(Boolean(existing))
  const [error,setError]=useState('')
  async function save(){setSaving(true);setError('');try{const result=await api.saveManagedExamEvaluation(examId,attemptId,questionId,{marks:Number(marks),teacher_feedback:feedback.trim()||null});setMarks(String(result.manual_review.marks));setFeedback(result.manual_review.teacher_comments||'');setSaved(true)}catch(e){setError(message(e))}finally{setSaving(false)}}
  return <div className="mt-3 rounded-lg border border-indigo-100 bg-white p-3">{aiEvaluation&&<div className="rounded-lg border border-violet-200 bg-violet-50 p-3 text-sm"><p className="font-semibold">AI Evaluation · advisory</p><p className="mt-1">Status: {aiEvaluation.status}{aiEvaluation.manual_required?' · Manual review required':''}</p>{aiEvaluation.marks!==null&&<p className="mt-1">AI marks: {aiEvaluation.marks} / {aiEvaluation.max_marks}</p>}{aiEvaluation.confidence!==null&&<p className="mt-1">Confidence: {Number(aiEvaluation.confidence).toFixed(2)}</p>}{aiEvaluation.explanation&&<p className="mt-2 whitespace-pre-wrap">Reason: {aiEvaluation.explanation}</p>}{aiEvaluation.last_error&&<p className="mt-2 text-rose-700">{aiEvaluation.last_error}</p>}</div>}<div className="mt-3 flex items-center justify-between"><b className="text-sm">Teacher evaluation · authoritative when saved</b>{saved&&<span className="text-xs text-emerald-700">Saved</span>}</div><div className="mt-3 grid gap-3 sm:grid-cols-[140px_1fr]"><label className="text-xs font-medium text-slate-600">Marks (max {maxMarks})<input type="number" min="0" max={maxMarks} step="0.01" value={marks} disabled={disabled||saving} onChange={e=>{setMarks(e.target.value);setSaved(false)}} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm"/></label><label className="text-xs font-medium text-slate-600">Teacher feedback<textarea rows={2} maxLength={10000} value={feedback} disabled={disabled||saving} onChange={e=>{setFeedback(e.target.value);setSaved(false)}} className="mt-1 w-full rounded-lg border px-3 py-2 text-sm"/></label></div>{error&&<p role="alert" className="mt-2 text-xs text-rose-700">{error}</p>}<button type="button" disabled={disabled||saving} onClick={()=>void save()} className="mt-3 rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">{saving?'Saving…':'Save evaluation'}</button>{disabled&&<p className="mt-2 text-xs text-slate-500">Evaluations are locked after exam finalization.</p>}</div>
}

function ExamRow({ exam, onEdit, onReview, onCancel, onDelete }: { exam:ManagedExam; onEdit:()=>void; onReview:()=>void; onCancel:()=>void; onDelete:()=>void }) {
  return <div className="flex flex-wrap items-center justify-between gap-4 px-5 py-4"><div className="min-w-[240px] flex-1"><div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">{exam.title}</h3><Status status={exam.status}/></div><p className="mt-1 text-sm text-slate-500">{formatDate(exam.scheduled_start_at)} <span className="mx-1 text-slate-300">·</span> {exam.duration_minutes} min <span className="mx-1 text-slate-300">·</span> {exam.question_count || 0} questions <span className="mx-1 text-slate-300">·</span> {exam.allow_list_enabled ? 'Allowed students' : 'Open access'}</p><p className="mt-1 text-xs text-slate-400">Updated {formatDate(exam.updated_at)}</p></div><div className="flex gap-2"><button onClick={onReview} className="rounded-lg border border-slate-200 px-3 py-2 text-sm font-medium">View</button>{['DRAFT','SCHEDULED'].includes(exam.status) && <button onClick={onEdit} className="rounded-lg border border-indigo-200 px-3 py-2 text-sm font-medium text-indigo-700">Edit</button>}{exam.status === 'SCHEDULED' && <button onClick={onCancel} className="rounded-lg border border-rose-200 px-3 py-2 text-sm text-rose-600">Cancel</button>}<button onClick={onDelete} className="rounded-lg border border-rose-200 px-3 py-2 text-sm font-semibold text-rose-700 hover:bg-rose-50">Delete</button></div></div>
}

const protectionFields: Array<[string,string]> = [['protected_mode_enabled','Protected exam mode'],['require_fullscreen','Require fullscreen'],['detect_visibility_change','Detect tab/visibility changes'],['detect_orientation_change','Detect orientation changes'],['restrict_copy_paste','Restrict copy and paste'],['autosave_enabled','Autosave'],['automatic_submission_enabled','Auto-submit on expiry']]
const inputClass = 'w-full rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-sm outline-none transition focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100 disabled:bg-slate-100 disabled:text-slate-500'
function Field({label, children, className=''}:{label:string;children:React.ReactNode;className?:string}) { return <label className={`block text-sm font-medium text-slate-700 ${className}`}><span className="mb-1.5 block">{label}</span>{children}</label> }
function SectionTitle({number,title,subtitle}:{number:string;title:string;subtitle:string}) { return <div className="flex gap-3"><span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-indigo-50 text-xs font-bold text-indigo-600">{number}</span><div><h3 className="font-semibold">{title}</h3><p className="mt-0.5 text-sm text-slate-500">{subtitle}</p></div></div> }
function Tag({children}:{children:React.ReactNode}) { return <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">{children}</span> }
function Status({status}:{status:string}) { const color = status==='DRAFT'?'bg-slate-100 text-slate-600':status==='SCHEDULED'?'bg-blue-50 text-blue-700':status==='ACTIVE'?'bg-emerald-50 text-emerald-700':status==='CANCELLED'?'bg-rose-50 text-rose-700':'bg-violet-50 text-violet-700'; return <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${color}`}>{status.replace('_',' ')}</span> }
function Alert({children,tone,onClose}:{children:React.ReactNode;tone:'error'|'success';onClose?:()=>void}) { return <div className={`mb-4 flex items-start justify-between gap-4 rounded-xl border p-3 text-sm ${tone==='error'?'border-rose-200 bg-rose-50 text-rose-800':'border-emerald-200 bg-emerald-50 text-emerald-800'}`}><span>{children}</span>{onClose && <button onClick={onClose} className="opacity-60">×</button>}</div> }
function message(error:unknown) { return error instanceof Error ? error.message : 'Something went wrong. Please try again.' }
function formatDate(value:string) { const d=new Date(value); return Number.isNaN(d.valueOf())?'Not scheduled':d.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) }
function localDateTime(date:Date) { const adjusted = new Date(date.getTime() - date.getTimezoneOffset() * 60000); return adjusted.toISOString().slice(0,16) }
async function reorder(exam:ManagedExam, from:number, to:number, open:(id:string)=>Promise<void>, setError:(value:string)=>void) { const ids=exam.questions!.map(q=>q.id); const [moved]=ids.splice(from,1); ids.splice(to,0,moved); try { await api.reorderManagedQuestions(exam.id,ids); await open(exam.id) } catch(e) { setError(message(e)) } }
