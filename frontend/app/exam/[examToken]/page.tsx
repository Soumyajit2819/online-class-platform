'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import Script from 'next/script'
import { useParams } from 'next/navigation'
import { api, ApiError, StudentExamAccess, StudentExamAnswer, StudentExamAudioAnswer, StudentExamAttemptPayload, StudentExamPreview } from '@/lib/api'

declare global {
  interface Window {
    google?: { accounts: { id: {
      initialize: (options: { client_id: string; callback: (response: { credential: string }) => void; ux_mode?: 'popup' | 'redirect' }) => void
      renderButton: (element: HTMLElement, options: { theme: 'outline'; size: 'large'; shape: 'rectangular'; text: 'continue_with'; width: number }) => void
    } } }
  }
}

type Phase = 'loading' | 'login' | 'instructions' | 'exam' | 'submitted' | 'unavailable'
type AnswerValue = { answer_text: string | null; selected_option_id: string | null; answer_method: 'MCQ'|'TEXT'|'AUDIO'|'BOTH' }
type SaveState = 'saved' | 'saving' | 'error'

export default function StudentExamPage() {
  const params = useParams<{ examToken: string }>()
  const examToken = params.examToken
  const storageKey = `student_exam_attempt:${examToken}`
  const [preview, setPreview] = useState<StudentExamPreview | null>(null)
  const [access, setAccess] = useState<StudentExamAccess | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [credential, setCredential] = useState('')
  const [attempt, setAttempt] = useState<StudentExamAttemptPayload | null>(null)
  const [answers, setAnswers] = useState<Record<string, AnswerValue>>({})
  const [saveStates, setSaveStates] = useState<Record<string, SaveState>>({})
  const [activeIndex, setActiveIndex] = useState(0)
  const [remaining, setRemaining] = useState(0)
  const [googleLoaded, setGoogleLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [violationCount, setViolationCount] = useState(0)
  const [protectionWarning, setProtectionWarning] = useState('')
  const [savedAudioUrls, setSavedAudioUrls] = useState<Record<string,string>>({})
  const [audioPending, setAudioPending] = useState(false)
  const googleButton = useRef<HTMLDivElement>(null)
  const deadline = useRef(0)
  const timeoutMap = useRef(new Map<string, number>())
  const queued = useRef(new Map<string, AnswerValue>())
  const saving = useRef(new Set<string>())
  const inFlight = useRef(new Map<string, Promise<void>>())
  const latestAnswers = useRef(new Map<string, AnswerValue>())
  const failedSaves = useRef(new Set<string>())
  const saveQuestionRef = useRef<(questionId:string,value:AnswerValue)=>Promise<void>>(async () => {})
  const credentialRef = useRef('')
  const attemptTokenRef = useRef('')
  const submittedRef = useRef(false)
  const requestedAudio = useRef(new Set<string>())
  const savedAudioUrlsRef = useRef(new Map<string,string>())
  const lastViolationAt = useRef(0)
  const pendingAudioRef = useRef(new Set<string>())
  const googleClientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID

  const reportAudioPending = useCallback((questionId: string, pending: boolean) => {
    if (pending) pendingAudioRef.current.add(questionId)
    else pendingAudioRef.current.delete(questionId)
    setAudioPending(pendingAudioRef.current.size > 0)
  }, [])

  useEffect(() => {
    let cancelled = false
    api.getStudentExamPreview(examToken).then(data => {
      if (!cancelled) { setPreview(data); setPhase('login') }
    }).catch(e => { if (!cancelled) { setError(message(e)); setPhase('unavailable') } })
    return () => { cancelled = true }
  }, [examToken])

  const installAttempt = useCallback((data: StudentExamAttemptPayload) => {
    setAttempt(data)
    const initial: Record<string, AnswerValue> = {}
    for (const answer of data.answers) initial[answer.question_id] = {
      answer_text: answer.answer_text, selected_option_id: answer.selected_option_id,
      answer_method: answer.answer_method,
    }
    setAnswers(initial)
    setViolationCount(data.attempt.violation_count || 0)
    const left = Math.max(0, data.remaining_seconds)
    deadline.current = performance.now() + left * 1000
    setRemaining(left)
    if (data.attempt.status === 'ACTIVE' && left > 0) setPhase('exam')
    else {
      setPhase('submitted'); submittedRef.current = true
      setInfo(data.attempt.status === 'TERMINATED'
        ? 'The attempt was submitted after reaching the exam security violation limit.'
        : data.attempt.status === 'AUTO_SUBMITTED'
          ? 'Time expired. Your exam was submitted automatically.'
          : 'Your exam has already been submitted.')
    }
  }, [])

  useEffect(() => {
    if (phase!=='exam' || !attempt || !credentialRef.current || !attemptTokenRef.current) return
    let cancelled=false
    for (const audio of attempt.audio_answers) {
      if (audio.transcription_status!=='READY' || requestedAudio.current.has(audio.question_id)) continue
      requestedAudio.current.add(audio.question_id)
      void api.getStudentExamAudio(examToken,credentialRef.current,attemptTokenRef.current,audio.question_id)
        .then(blob=>{if(cancelled)return;const url=URL.createObjectURL(blob);savedAudioUrlsRef.current.set(audio.question_id,url);setSavedAudioUrls(old=>({...old,[audio.question_id]:url}))})
        .catch(()=>{requestedAudio.current.delete(audio.question_id)})
    }
    return ()=>{cancelled=true}
  },[phase,attempt?.audio_answers,examToken])

  const openExisting = useCallback(async (googleCredential: string) => {
    const started = await api.startOrResumeStudentExam(examToken, googleCredential)
    sessionStorage.setItem(storageKey, started.attempt_token)
    attemptTokenRef.current = started.attempt_token
    const data = await api.getStudentExamAttempt(examToken, googleCredential, started.attempt_token)
    installAttempt(data)
  }, [examToken, installAttempt, storageKey])

  const authenticate = useCallback(async (googleCredential: string) => {
    setBusy(true); setError(''); setInfo('')
    credentialRef.current = googleCredential
    setCredential(googleCredential)
    try {
      const result = await api.getStudentExamAccess(examToken, googleCredential)
      setAccess(result)
      if (!result.audio_supported) {
        setError('Audio answers will be available in a later update. This exam cannot be started yet.')
        setPhase('unavailable')
      } else if (!result.eligible) {
        setError(result.message || 'You are not authorized to take this exam.')
        setPhase('unavailable')
      } else if (result.existing_attempt) {
        await openExisting(googleCredential)
      } else if (result.availability === 'NOT_STARTED') {
        setInfo(`Exam has not started yet. It is scheduled for ${formatDate(result.scheduled_start_at)}.`)
        setPhase('instructions')
      } else if (result.availability === 'ENDED') {
        setInfo('Exam has ended. No new attempt can be started.')
        setPhase('unavailable')
      } else if (result.availability !== 'AVAILABLE') {
        setInfo(result.message || 'This exam is not open for student access.')
        setPhase('unavailable')
      } else {
        setPhase('instructions')
      }
    } catch (e) { setError(message(e)); setPhase('login') }
    finally { setBusy(false) }
  }, [examToken, openExisting])

  useEffect(() => {
    const element = googleButton.current
    if (!googleLoaded || !googleClientId || !window.google || !element) return
    element.replaceChildren()
    window.google.accounts.id.initialize({
      client_id: googleClientId,
      callback: response => {
        if (!response.credential) setError('Google sign-in did not complete. Please try again.')
        else void authenticate(response.credential)
      },
      ux_mode: 'popup',
    })
    window.google.accounts.id.renderButton(element, { theme:'outline', size:'large', shape:'rectangular', text:'continue_with', width:Math.max(220,Math.min(400,Math.floor(element.clientWidth || 320))) })
  }, [googleLoaded, googleClientId, authenticate])

  const start = async () => {
    if (!credentialRef.current || !access?.eligible || access.availability !== 'AVAILABLE') return
    setBusy(true); setError('')
    try {
      await openExisting(credentialRef.current)
      if(access?.require_fullscreen&&document.fullscreenElement===null) {
        try { await document.documentElement.requestFullscreen() }
        catch { setProtectionWarning('Fullscreen could not be enabled. Use the fullscreen button below if your browser permits it.') }
      }
    }
    catch (e) { setError(message(e)) }
    finally { setBusy(false) }
  }

  const submit = useCallback(async (automatic = false) => {
    if (!credentialRef.current || !attemptTokenRef.current || submittedRef.current) return
    if (!automatic && pendingAudioRef.current.size) {
      setError('Save or discard the audio recording preview before submitting. Your saved answers are unchanged.')
      return
    }
    submittedRef.current = true
    setBusy(true)
    if (!automatic && !window.confirm('Are you sure you want to submit your exam? You will not be able to change your answers after submission.')) {
      submittedRef.current = false; setBusy(false); return
    }
    try {
      for (const id of Array.from(timeoutMap.current.values())) window.clearTimeout(id)
      timeoutMap.current.clear()
      // Flush every answer changed during this attempt, even if its debounce callback
      // already ran while a newer state update was being scheduled.
      const pendingIds = new Set([...Array.from(latestAnswers.current.keys()), ...Array.from(queued.current.keys()), ...Array.from(inFlight.current.keys()), ...Array.from(failedSaves.current)])
      await Promise.all(Array.from(pendingIds).map(id => saveQuestionRef.current(id, queued.current.get(id) || latestAnswers.current.get(id)!)))
      if (!automatic && failedSaves.current.size) {
        setError('Some answers could not be saved. Check your connection and retry before submitting.')
        submittedRef.current = false
        return
      }
      const result = await api.submitStudentExam(examToken, credentialRef.current, attemptTokenRef.current)
      setInfo(result.status === 'AUTO_SUBMITTED' ? 'Time expired. Your exam was submitted automatically.' : 'Your exam has been submitted.')
      setPhase('submitted')
      setAttempt(current => current ? { ...current, attempt: result.attempt } : current)
    } catch (e) {
      setError(message(e))
      submittedRef.current = false
      if (e instanceof ApiError && e.status === 401) {
        setInfo('Google sign-in expired. Sign in again to resume your saved attempt.')
        setPhase('login')
      } else if (automatic) {
        setInfo('Time expired. Reconnect to confirm automatic submission.')
      }
    } finally { setBusy(false) }
  }, [examToken])

  const saveQuestion = useCallback(async (questionId: string, value: AnswerValue): Promise<void> => {
    const running = inFlight.current.get(questionId)
    if (running) {
      queued.current.set(questionId, value)
      await running
      const pending = queued.current.get(questionId)
      if (pending) await saveQuestionRef.current(questionId, pending)
      return
    }
    saving.current.add(questionId)
    const run = (async () => {
      let current: AnswerValue | undefined = value
      try {
        while (current) {
          queued.current.delete(questionId)
          setSaveStates(old => ({ ...old, [questionId]:'saving' }))
          await api.saveStudentExamAnswer(examToken, credentialRef.current, attemptTokenRef.current, {
            question_id: questionId, answer_text: current.answer_text,
            selected_option_id: current.selected_option_id, answer_method: current.answer_method,
          })
          failedSaves.current.delete(questionId)
          setSaveStates(old => ({ ...old, [questionId]:'saved' }))
          current = queued.current.get(questionId)
        }
      } catch (e) {
        failedSaves.current.add(questionId)
        queued.current.delete(questionId)
        setSaveStates(old => ({ ...old, [questionId]:'error' }))
        setError(message(e))
        if (e instanceof ApiError && e.status === 410) {
          setInfo('Time expired. Your exam was submitted automatically.')
          setPhase('submitted'); submittedRef.current = true
        } else if (e instanceof ApiError && e.status === 401) {
          setInfo('Google sign-in expired. Sign in again to resume your saved attempt.')
          setPhase('login')
        }
      }
    })()
    inFlight.current.set(questionId, run)
    try { await run } finally {
      inFlight.current.delete(questionId)
      saving.current.delete(questionId)
    }
  }, [examToken])
  saveQuestionRef.current = saveQuestion

  function changeAnswer(questionId: string, value: AnswerValue, immediate = false) {
    if (phase !== 'exam') return
    setAnswers(old => ({ ...old, [questionId]:value }))
    latestAnswers.current.set(questionId, value)
    queued.current.set(questionId, value)
    const oldTimer = timeoutMap.current.get(questionId)
    if (oldTimer) window.clearTimeout(oldTimer)
    const timer = window.setTimeout(() => {
      timeoutMap.current.delete(questionId)
      void saveQuestion(questionId, queued.current.get(questionId) || value)
    }, immediate ? 0 : 600)
    timeoutMap.current.set(questionId, timer)
  }

  const selectAnswerMethod = (questionId:string, method:AnswerValue['answer_method']) => {
    const current=answers[questionId] || {answer_text:null,selected_option_id:null,answer_method:'TEXT' as const}
    changeAnswer(questionId,{...current,answer_method:method},true)
  }

  const applyUploadedAudio = (questionId:string, result:{answer_method:'AUDIO'|'BOTH';transcript:string;duration_ms:number|null}, url:string, mime:string) => {
    const current=answers[questionId] || {answer_text:null,selected_option_id:null,answer_method:'TEXT' as const}
    const next={...current,answer_method:result.answer_method}
    queued.current.delete(questionId); failedSaves.current.delete(questionId)
    latestAnswers.current.set(questionId,next); setAnswers(old=>({...old,[questionId]:next}))
    setSaveStates(old=>({...old,[questionId]:'saved'}))
    requestedAudio.current.add(questionId)
    setAttempt(old=>old?{...old,audio_answers:[...old.audio_answers.filter(a=>a.question_id!==questionId),{
      question_id:questionId,audio_mime_type:mime,duration_ms:result.duration_ms,
      transcription_status:'READY',transcript:result.transcript,
    }]}:old)
    const previous=savedAudioUrlsRef.current.get(questionId);if(previous)URL.revokeObjectURL(previous)
    savedAudioUrlsRef.current.set(questionId,url)
    setSavedAudioUrls(old=>({...old,[questionId]:url}))
  }

  const removeSavedAudio = (questionId:string) => {
    setAttempt(old=>old?{...old,audio_answers:old.audio_answers.filter(a=>a.question_id!==questionId)}:old)
    const previous=savedAudioUrlsRef.current.get(questionId);if(previous)URL.revokeObjectURL(previous)
    savedAudioUrlsRef.current.delete(questionId)
    requestedAudio.current.delete(questionId)
    setSavedAudioUrls(old=>{const next={...old};delete next[questionId];return next})
    const current=answers[questionId] || {answer_text:null,selected_option_id:null,answer_method:'TEXT' as const}
    if(current.answer_method==='AUDIO'||current.answer_method==='BOTH') selectAnswerMethod(questionId,'TEXT')
  }

  useEffect(() => {
    if (phase !== 'exam') return
    const update = () => {
      const value = Math.max(0, Math.ceil((deadline.current - performance.now()) / 1000))
      setRemaining(value)
      if (value <= 0) void submit(true)
    }
    update()
    const timer = window.setInterval(update, 250)
    const sync = window.setInterval(async () => {
      if (!credentialRef.current || !attemptTokenRef.current || submittedRef.current) return
      try {
        const heartbeat = await api.heartbeatStudentExam(examToken, credentialRef.current, attemptTokenRef.current)
        deadline.current = performance.now() + Math.max(0,heartbeat.remaining_seconds) * 1000
        setRemaining(Math.max(0,heartbeat.remaining_seconds)); setViolationCount(heartbeat.violation_count)
        if (heartbeat.status !== 'ACTIVE') {
          const result = await api.getStudentExamAttempt(examToken, credentialRef.current, attemptTokenRef.current)
          installAttempt(result)
        }
      } catch (e) {
        if (e instanceof ApiError && e.status === 410) { setPhase('submitted'); setInfo('Time expired. Your exam was submitted automatically.') }
        else if (e instanceof ApiError && e.status === 401) { setInfo('Google sign-in expired. Sign in again to resume your attempt.'); setPhase('login') }
      }
    }, 20000)
    return () => { window.clearInterval(timer); window.clearInterval(sync) }
  }, [phase, examToken, installAttempt, submit])

  useEffect(() => {
    if(phase!=='exam'||!attempt||!access)return
    const report=async(type:string)=>{
      if(!credentialRef.current||!attemptTokenRef.current||submittedRef.current)return
      const now=Date.now(),last=lastViolationAt.current
      if(now-last<3000)return
      lastViolationAt.current=now
      try {
        const result=await api.recordStudentExamViolation(examToken,credentialRef.current,attemptTokenRef.current,type)
        setViolationCount(result.violation_count)
        if(result.terminated){submittedRef.current=true;setAttempt(old=>old?{...old,attempt:{...old.attempt,status:'TERMINATED'}}:old);setInfo('The attempt was submitted after reaching the exam security violation limit.');setPhase('submitted')}
        else setProtectionWarning(`Security event recorded (${result.violation_count}${result.maximum_violations?`/${result.maximum_violations}`:''}). Stay on the exam screen.`)
      } catch { setProtectionWarning('A security event occurred but could not be recorded because the connection is unavailable.') }
    }
    const onVisibility=()=>{if(document.visibilityState==='hidden'&&access.detect_visibility_change)void report('TAB_HIDDEN')}
    const onBlur=()=>void report('WINDOW_BLUR')
    const onFullscreen=()=>{if(access.require_fullscreen&&document.fullscreenElement===null)void report('FULLSCREEN_EXIT')}
    const onOrientation=()=>{if(access.detect_orientation_change)void report('ORIENTATION_CHANGE')}
    const onBeforeUnload=(event:BeforeUnloadEvent)=>{event.preventDefault();event.returnValue=''}
    const restrict=(event:Event)=>{event.preventDefault()}
    document.addEventListener('visibilitychange',onVisibility)
    document.addEventListener('fullscreenchange',onFullscreen)
    window.addEventListener('blur',onBlur)
    window.addEventListener('orientationchange',onOrientation)
    window.addEventListener('beforeunload',onBeforeUnload)
    if(access.restrict_copy_paste){document.addEventListener('copy',restrict);document.addEventListener('cut',restrict);document.addEventListener('paste',restrict);document.addEventListener('contextmenu',restrict)}
    if(access.protected_mode_enabled){document.addEventListener('selectstart',restrict)}
    const channel=typeof BroadcastChannel!=='undefined'?new BroadcastChannel(`exam-attempt:${examToken}`):null
    const onOtherSession=()=>void report('MULTIPLE_SESSION')
    channel?.addEventListener('message',onOtherSession);channel?.postMessage({active:true})
    return()=>{
      document.removeEventListener('visibilitychange',onVisibility);document.removeEventListener('fullscreenchange',onFullscreen)
      window.removeEventListener('blur',onBlur);window.removeEventListener('orientationchange',onOrientation);window.removeEventListener('beforeunload',onBeforeUnload)
      document.removeEventListener('copy',restrict);document.removeEventListener('cut',restrict);document.removeEventListener('paste',restrict);document.removeEventListener('contextmenu',restrict);document.removeEventListener('selectstart',restrict)
      channel?.removeEventListener('message',onOtherSession);channel?.close()
    }
  },[phase,attempt,access,examToken])

  useEffect(() => {
    if (phase !== 'instructions' || access?.availability !== 'NOT_STARTED' || !credentialRef.current) return
    let cancelled = false
    const refreshAvailability = async () => {
      try {
        const fresh = await api.getStudentExamAccess(examToken, credentialRef.current)
        if (!cancelled) {
          setAccess(fresh)
          if (fresh.availability === 'AVAILABLE') setInfo('The exam is now open. Review the instructions, then start when ready.')
          else if (fresh.availability === 'ENDED') { setPhase('unavailable'); setInfo('Exam has ended.') }
        }
      } catch (e) {
        if (!cancelled && e instanceof ApiError && e.status === 401) {
          setInfo('Google sign-in expired. Sign in again to continue.')
          setPhase('login')
        }
      }
    }
    const interval = window.setInterval(() => { void refreshAvailability() }, 15000)
    return () => { cancelled = true; window.clearInterval(interval) }
  }, [phase, access?.availability, examToken])

  useEffect(() => () => {
    for (const id of Array.from(timeoutMap.current.values())) window.clearTimeout(id)
    for(const url of Array.from(savedAudioUrlsRef.current.values()))URL.revokeObjectURL(url)
  }, [])

  const questions = attempt?.questions || []
  const question = questions[activeIndex]
  const answeredCount = questions.filter(q => Boolean(answers[q.id]?.answer_text?.trim() || answers[q.id]?.selected_option_id || attempt?.audio_answers.some(a => a.question_id===q.id && a.transcription_status==='READY'))).length

  if (!preview && phase === 'loading') return <Centered><div className="animate-pulse text-slate-500">Loading exam…</div></Centered>
  if (phase === 'unavailable' && !preview) return <Centered><MessageCard title="Exam link unavailable" message={error || 'This link is invalid or expired.'} /></Centered>

  return <main className="min-h-screen bg-[#f4f6fa] text-slate-900">
    <header className="border-b border-slate-200 bg-white"><div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-4"><Link href="/" className="text-xs font-semibold tracking-widest text-slate-400">ONLINE CLASS PLATFORM</Link><div className="flex items-center gap-3">{phase==='exam'&&access?.require_fullscreen&&document.fullscreenElement===null&&<button onClick={()=>void document.documentElement.requestFullscreen().catch(()=>setProtectionWarning('Fullscreen is not available in this browser session.'))} className="rounded-lg border px-3 py-2 text-xs font-semibold">Enter fullscreen</button>}{phase === 'exam' && <div className={`rounded-xl px-4 py-2 text-center font-mono text-lg font-bold tabular-nums ${remaining < 300 ? 'bg-rose-50 text-rose-700' : 'bg-slate-900 text-white'}`} aria-live="polite">{formatClock(remaining)}</div>}</div></div></header>
    <div className="mx-auto max-w-5xl px-4 py-6 sm:py-10">
      {(error || info) && <div className={`mb-5 rounded-xl border px-4 py-3 text-sm ${error ? 'border-rose-200 bg-rose-50 text-rose-800' : 'border-blue-200 bg-blue-50 text-blue-800'}`}>{error || info}<button onClick={() => { setError(''); if (info) setInfo('') }} className="float-right pl-3 font-bold">×</button></div>}
      {phase==='exam'&&protectionWarning&&<div role="status" className="mb-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">{protectionWarning}<span className="ml-2 text-xs">Recorded events: {violationCount}{access?.maximum_violations?` / ${access.maximum_violations}`:''}</span></div>}

      {preview && phase === 'login' && <div className="mx-auto max-w-xl overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm"><div className="bg-slate-950 px-7 py-8 text-white"><span className="rounded-full bg-white/10 px-3 py-1 text-xs font-semibold text-indigo-200">EXAM ACCESS</span><h1 className="mt-5 text-3xl font-semibold">{preview.title}</h1>{preview.description && <p className="mt-2 text-slate-300">{preview.description}</p>}</div><div className="p-7"><div className="grid grid-cols-2 gap-3"><Info label="Scheduled start" value={preview.scheduled_start_at ? formatDate(preview.scheduled_start_at) : 'Not scheduled'} /><Info label="Duration" value={`${preview.duration_minutes} minutes`} /></div><div className="mt-5 rounded-xl bg-slate-50 p-4 text-sm text-slate-600">{preview.availability === 'NOT_STARTED' ? 'This exam is scheduled for a later time.' : preview.availability === 'ENDED' ? 'The scheduled exam time has ended. Sign in to resume an existing attempt if you have one.' : preview.message || 'Sign in with Google to check your exam access.'}</div>{googleClientId ? <><p className="mt-6 text-center text-sm text-slate-600">Sign in with Google to verify your identity and eligibility.</p><div ref={googleButton} className="mt-4 flex min-h-11 justify-center"/><Script src="https://accounts.google.com/gsi/client" strategy="afterInteractive" onLoad={() => setGoogleLoaded(true)} onError={() => setError('Google sign-in could not load. Check your connection and try again.')} /></> : <p className="mt-5 rounded-lg bg-amber-50 p-3 text-sm text-amber-800">Google sign-in is not configured for this site.</p>}</div></div>}

      {preview && access && phase === 'instructions' && <div className="mx-auto max-w-3xl"><div className="mb-5"><p className="text-sm font-semibold uppercase tracking-widest text-indigo-600">Before you begin</p><h1 className="mt-2 text-3xl font-semibold">{access.title}</h1></div><div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm sm:p-8"><div className="grid gap-3 sm:grid-cols-3"><Info label="Questions" value={String(access.question_count)} /><Info label="Total marks" value={String(access.total_marks)} /><Info label="Duration" value={`${access.duration_minutes} minutes`} /></div><div className="mt-5 grid gap-3 sm:grid-cols-2"><Info label="Scheduled start" value={access.scheduled_start_at ? formatDate(access.scheduled_start_at) : 'Not scheduled'} /><Info label="Signed in as" value={access.student_display_name} /></div><section className="mt-6 rounded-2xl bg-slate-50 p-5"><h2 className="font-semibold">System Exam Instructions</h2><p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-slate-700">{access.system_instructions}</p></section>{access.custom_instructions_enabled && access.custom_instructions && <section className="mt-4 rounded-2xl border border-indigo-100 bg-indigo-50/50 p-5"><h2 className="font-semibold">Optional Teacher Instructions</h2><p className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-slate-700">{access.custom_instructions}</p></section>}<section className="mt-5"><h2 className="font-semibold">Please note</h2><ul className="mt-3 space-y-2 text-sm text-slate-600">{['Your answers will be autosaved as you work.','The timer starts only after you click “I Agree & Start Exam”.','Once started, you will have only the time remaining before the scheduled exam end.','Submitting is final. You cannot change answers after submission.','Keep this exam page open while you work.'].map(x => <li key={x} className="flex gap-2"><span className="text-indigo-600">•</span>{x}</li>)}</ul></section>{!access.audio_supported && <p className="mt-5 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">This exam includes an audio question. Audio answers will be available in a later update, so this exam cannot be started yet.</p>}{access.availability === 'NOT_STARTED' && <p className="mt-5 rounded-xl border border-blue-200 bg-blue-50 p-3 text-sm text-blue-800">Exam has not started yet. Scheduled start: {access.scheduled_start_at ? formatDate(access.scheduled_start_at) : 'Not scheduled'}.</p>}{access.availability === 'ENDED' && <p className="mt-5 rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">Exam has ended.</p>}<button onClick={() => void start()} disabled={busy || !access.eligible || !access.audio_supported || access.availability !== 'AVAILABLE'} className="mt-6 w-full rounded-xl bg-indigo-600 px-5 py-3.5 font-semibold text-white shadow-sm hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300">{busy ? 'Preparing your attempt…' : 'I Agree & Start Exam'}</button></div></div>}

      {phase === 'exam' && attempt && question && <div className="grid gap-5 lg:grid-cols-[220px_minmax(0,1fr)]"><aside className="h-fit rounded-2xl border border-slate-200 bg-white p-4"><div className="flex items-center justify-between"><h2 className="font-semibold">Questions</h2><span className="text-xs text-slate-500">{answeredCount}/{questions.length} answered</span></div><div className="mt-4 grid grid-cols-5 gap-2 lg:grid-cols-4">{questions.map((q,i) => <button key={q.id} onClick={() => setActiveIndex(i)} disabled={audioPending} className={`relative h-10 rounded-lg border text-sm font-semibold ${activeIndex===i?'border-indigo-600 bg-indigo-600 text-white':answers[q.id]?.answer_text?.trim() || answers[q.id]?.selected_option_id?'border-emerald-200 bg-emerald-50 text-emerald-800':'border-slate-200 bg-white text-slate-500'}`}>{i+1}</button>)}</div><div className="mt-4 text-xs text-slate-500">Autosave: <span className="font-medium text-emerald-700">{question && saveStates[question.id] === 'saving' ? 'Saving…' : question && saveStates[question.id] === 'error' ? 'Not saved' : 'On'}</span></div><button onClick={() => void submit(false)} disabled={busy} className="mt-5 w-full rounded-lg border border-rose-200 px-3 py-2.5 text-sm font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50">Submit exam</button></aside><section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm sm:p-8"><div className="flex flex-wrap items-center justify-between gap-3"><span className="text-sm font-semibold uppercase tracking-wider text-indigo-600">Question {question.question_number} of {questions.length}</span><span className="rounded-full bg-slate-100 px-3 py-1 text-sm font-medium text-slate-600">{question.max_marks} marks</span></div><h1 className="mt-6 whitespace-pre-wrap text-xl font-semibold leading-relaxed">{question.question_text}</h1>{question.question_type === 'MCQ' ? <div className="mt-6 space-y-3">{question.options.map(option => <label key={option.id} className={`flex cursor-pointer items-start gap-3 rounded-xl border p-4 transition ${answers[question.id]?.selected_option_id===option.id?'border-indigo-300 bg-indigo-50':'border-slate-200 hover:border-slate-300'}`}><input type="radio" name={`question-${question.id}`} checked={answers[question.id]?.selected_option_id===option.id} onChange={() => changeAnswer(question.id,{answer_text:null,selected_option_id:option.id,answer_method:'MCQ'},true)} className="mt-1 accent-indigo-600"/><span>{option.option_text}</span></label>)}</div> : <div className="mt-6 space-y-5"><label className="block text-sm font-semibold">Type Answer<textarea rows={9} maxLength={20000} value={answers[question.id]?.answer_text || ''} onChange={e => changeAnswer(question.id,{answer_text:e.target.value,selected_option_id:null,answer_method:answers[question.id]?.answer_method || 'TEXT'})} onBlur={() => { const value=answers[question.id]; if(value) changeAnswer(question.id,value,true) }} className="mt-2 w-full resize-y rounded-xl border border-slate-200 p-4 text-base font-normal outline-none focus:border-indigo-400 focus:ring-2 focus:ring-indigo-100" placeholder="Type your answer here…"/></label><AudioAnswerEditor examToken={examToken} credential={credentialRef.current} attemptToken={attemptTokenRef.current} questionId={question.id} textAnswer={answers[question.id]?.answer_text || ''} answerMethod={answers[question.id]?.answer_method || 'TEXT'} audio={attempt.audio_answers.find(a=>a.question_id===question.id) || null} savedAudioUrl={savedAudioUrls[question.id] || ''} onMethod={method=>selectAnswerMethod(question.id,method)} onUploaded={(result,url,mime)=>applyUploadedAudio(question.id,result,url,mime)} onPendingChange={reportAudioPending} onDelete={async()=>{try{await api.deleteStudentExamAudio(examToken,credentialRef.current,attemptTokenRef.current,question.id);removeSavedAudio(question.id)}catch(e){setError(message(e))}}}/></div>}<div className="mt-7 flex items-center justify-between gap-3 border-t border-slate-100 pt-5"><button disabled={activeIndex===0 || audioPending} onClick={() => setActiveIndex(i=>Math.max(0,i-1))} className="rounded-lg border px-4 py-2.5 text-sm font-semibold disabled:opacity-40">← Previous</button><span className="text-xs text-slate-500">{saveStates[question.id]==='saving'?'Saving…':saveStates[question.id]==='error'?'Save failed':saveStates[question.id]==='saved'?'Saved':'Changes save automatically'}</span>{activeIndex < questions.length - 1 ? <button disabled={audioPending} onClick={() => setActiveIndex(i=>Math.min(questions.length-1,i+1))} className="rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-40">Next →</button> : <button onClick={() => void submit(false)} className="rounded-lg bg-rose-700 px-4 py-2.5 text-sm font-semibold text-white">End &amp; Submit</button>}</div></section></div>}

      {phase === 'submitted' && <Centered><MessageCard title={attempt?.attempt.status === 'AUTO_SUBMITTED' ? 'Time is up' : 'Exam submitted'} message={info || 'Your answers have been submitted. You can close this page now.'} /></Centered>}
      {phase === 'unavailable' && preview && <Centered><MessageCard title={error.includes('authorized') ? 'Not authorized' : preview.title} message={error || info || preview.message || 'This exam is unavailable.'} /></Centered>}
    </div>
  </main>
}

function AudioAnswerEditor({examToken,credential,attemptToken,questionId,textAnswer,answerMethod,audio,savedAudioUrl,onMethod,onUploaded,onDelete,onPendingChange}:{
  examToken:string; credential:string; attemptToken:string; questionId:string; textAnswer:string;
  answerMethod:AnswerValue['answer_method']; audio:StudentExamAudioAnswer|null; savedAudioUrl:string;
  onMethod:(method:AnswerValue['answer_method'])=>void;
  onUploaded:(result:{answer_method:'AUDIO'|'BOTH';transcript:string;duration_ms:number|null},url:string,mime:string)=>void;
  onDelete:()=>Promise<void>;
  onPendingChange:(questionId:string,pending:boolean)=>void;
}) {
  const [recording,setRecording]=useState(false)
  const [pending,setPending]=useState<Blob|null>(null)
  const [previewUrl,setPreviewUrl]=useState('')
  const [durationMs,setDurationMs]=useState(0)
  const [includeTyped,setIncludeTyped]=useState(false)
  const [uploading,setUploading]=useState(false)
  const [error,setError]=useState('')
  const recorder=useRef<MediaRecorder|null>(null)
  const stream=useRef<MediaStream|null>(null)
  const chunks=useRef<Blob[]>([])
  const startedAt=useRef(0)
  const maxDurationTimer=useRef<number|undefined>(undefined)

  function clearPending() {
    setPending(null); setDurationMs(0); setError('')
    onPendingChange(questionId,false)
    setPreviewUrl(old=>{if(old)URL.revokeObjectURL(old);return ''})
  }
  async function startRecording() {
    setError('')
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder==='undefined') {
      setError('This browser does not support audio recording. You can still type your answer.'); onPendingChange(questionId,false); return
    }
    onPendingChange(questionId,true)
    try {
      const source=await navigator.mediaDevices.getUserMedia({audio:true})
      stream.current=source; chunks.current=[]
      const media=new MediaRecorder(source)
      recorder.current=media; startedAt.current=Date.now()
      media.ondataavailable=event=>{if(event.data.size)chunks.current.push(event.data)}
      media.onstop=()=>{
        if(maxDurationTimer.current)window.clearTimeout(maxDurationTimer.current)
        const blob=new Blob(chunks.current,{type:media.mimeType||'audio/webm'})
        source.getTracks().forEach(track=>track.stop()); stream.current=null
        recorder.current=null;setRecording(false)
        if(!blob.size){setError('No audio was captured. Please try recording again.');onPendingChange(questionId,false);return}
        setDurationMs(Math.min(180000,Date.now()-startedAt.current));setPending(blob)
        setPreviewUrl(URL.createObjectURL(blob))
      }
      media.start();setRecording(true)
      maxDurationTimer.current=window.setTimeout(()=>{if(media.state==='recording')media.stop()},180000)
    } catch {
      onPendingChange(questionId,false)
      setError('Microphone access was not granted. Check browser permission and try again; your typed answer is unchanged.')
    }
  }
  function stopRecording() { if(recorder.current?.state==='recording') recorder.current.stop() }
  async function useRecording() {
    if(!pending)return
    setUploading(true);setError('');onPendingChange(questionId,true)
    const method=includeTyped&&textAnswer.trim()?'BOTH':'AUDIO'
    try {
      const result=await api.uploadStudentExamAudio(examToken,credential,attemptToken,questionId,pending,durationMs,method)
      const url=URL.createObjectURL(pending)
      setPending(null);setPreviewUrl('');setError('')
      onPendingChange(questionId,false)
      onUploaded(result,url,pending.type||'audio/webm')
    } catch(e) {
      setError(`${e instanceof Error?e.message:'Audio could not be saved.'} Your typed answer and previously saved audio are unchanged. Retry this recording or record again.`)
    } finally {setUploading(false)}
  }
  useEffect(()=>()=>{if(maxDurationTimer.current)window.clearTimeout(maxDurationTimer.current);stream.current?.getTracks().forEach(track=>track.stop());if(recorder.current?.state==='recording')recorder.current.stop();if(previewUrl)URL.revokeObjectURL(previewUrl)},[previewUrl])

  const saved=Boolean(audio?.transcription_status==='READY')
  return <section className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">Record Audio</h2><span className="text-xs text-slate-500">Text and audio are kept separately.</span></div>
    {(saved||textAnswer.trim())&&<div className="mt-3"><p className="mb-2 text-sm font-medium">Answer method used for evaluation</p><div className="flex flex-wrap gap-2">
      {textAnswer.trim()&&<MethodButton active={answerMethod==='TEXT'} onClick={()=>onMethod('TEXT')}>Typed answer</MethodButton>}
      {saved&&<MethodButton active={answerMethod==='AUDIO'} onClick={()=>onMethod('AUDIO')}>Audio transcript</MethodButton>}
      {saved&&textAnswer.trim()&&<MethodButton active={answerMethod==='BOTH'} onClick={()=>onMethod('BOTH')}>Both answers</MethodButton>}
    </div><p className="mt-2 text-xs text-slate-500">Selected: {answerMethod==='BOTH'?'typed answer and audio transcript':answerMethod==='AUDIO'?'audio transcript':'typed answer'}.</p></div>}
    {saved&&<div className="mt-4 rounded-xl border bg-white p-3"><div className="flex items-center justify-between gap-2"><b className="text-sm">Saved audio answer</b><span className="text-xs text-emerald-700">Transcribed and saved</span></div>{savedAudioUrl?<audio className="mt-2 w-full" controls preload="metadata" src={savedAudioUrl}/>:<p className="mt-2 text-xs text-slate-500">Loading your private saved recording…</p>}<p className="mt-2 text-sm"><b>Transcript:</b> {audio?.transcript || 'Transcript unavailable.'}</p><button type="button" onClick={()=>void onDelete()} className="mt-2 text-sm font-semibold text-rose-700">Clear saved audio</button></div>}
    {!recording&&!pending&&<button type="button" onClick={()=>void startRecording()} className="mt-4 rounded-lg bg-indigo-600 px-4 py-2.5 text-sm font-semibold text-white">Record Audio</button>}
    {recording&&<div className="mt-4 flex items-center gap-3"><span className="animate-pulse text-sm font-semibold text-rose-700">● Recording</span><button type="button" onClick={stopRecording} className="rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white">Stop</button></div>}
    {pending&&<div className="mt-4 rounded-xl border border-indigo-200 bg-white p-4"><p className="font-semibold">Preview before saving · {formatDuration(durationMs)}</p><p className="mt-1 text-xs text-slate-500">Listen to the complete recording. It has not been selected or uploaded yet.</p><audio className="mt-3 w-full" controls preload="metadata" src={previewUrl}/>{Boolean(textAnswer.trim())&&<label className="mt-3 flex items-center gap-2 text-sm"><input type="checkbox" checked={includeTyped} onChange={e=>setIncludeTyped(e.target.checked)}/>Submit both this audio transcript and my typed answer</label>}<div className="mt-4 flex flex-wrap gap-2"><button type="button" disabled={uploading} onClick={()=>void useRecording()} className="rounded-lg bg-emerald-700 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-50">{uploading?'Uploading and transcribing…':'Use This Recording'}</button><button type="button" disabled={uploading} onClick={()=>{clearPending();void startRecording()}} className="rounded-lg border border-slate-300 px-4 py-2.5 text-sm font-semibold">Record Again</button></div></div>}
    {error&&<p role="alert" className="mt-3 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
    <p className="mt-3 text-xs text-slate-500">Audio is private to this attempt. Saving uploads it securely and creates a transcript for later evaluation.</p>
  </section>
}

function MethodButton({active,onClick,children}:{active:boolean;onClick:()=>void;children:React.ReactNode}) {return <button type="button" onClick={onClick} aria-pressed={active} className={`rounded-lg border px-3 py-2 text-sm font-medium ${active?'border-indigo-500 bg-indigo-50 text-indigo-800':'border-slate-300 bg-white text-slate-700'}`}>{children}</button>}
function formatDuration(milliseconds:number) {const seconds=Math.floor(milliseconds/1000);return `${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`}

function formatDate(value:string | null) { if (!value) return 'Not scheduled'; const d=new Date(value); return Number.isNaN(d.valueOf())?'Not scheduled':d.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) }
function formatClock(total:number) { const h=Math.floor(total/3600), m=Math.floor((total%3600)/60), s=total%60; return `${h ? `${String(h).padStart(2,'0')}:` : ''}${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` }
function message(e:unknown) { return e instanceof Error ? e.message : 'Something went wrong. Please try again.' }
function Info({label,value}:{label:string;value:string}) { return <div className="rounded-xl border border-slate-100 bg-white p-3"><p className="text-xs text-slate-400">{label}</p><p className="mt-1 text-sm font-semibold text-slate-800">{value}</p></div> }
function Centered({children}:{children:React.ReactNode}) { return <main className="flex min-h-screen items-center justify-center bg-slate-50 px-4">{children}</main> }
function MessageCard({title,message}:{title:string;message:string}) { return <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-8 text-center shadow-sm"><div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-indigo-50 text-xl text-indigo-700">✓</div><h1 className="mt-4 text-2xl font-semibold">{title}</h1><p className="mt-2 text-slate-600">{message}</p><Link href="/" className="mt-6 inline-block text-sm font-semibold text-indigo-600">Back to home</Link></div> }
