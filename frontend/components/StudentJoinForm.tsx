'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import Script from 'next/script'
import { api, JoinRequest } from '@/lib/api'

declare global {
  interface Window {
    google?: {
      accounts: { id: {
        initialize: (options: { client_id: string; callback: (response: { credential: string }) => void; ux_mode?: 'popup' | 'redirect' }) => void
        renderButton: (element: HTMLElement, options: { theme: 'outline'; size: 'large'; shape: 'rectangular'; text: 'continue_with'; width: number }) => void
      } }
    }
  }
}

function getSessionId() {
  const key = 'class_join_session_id'
  let id = sessionStorage.getItem(key)
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}-${Math.random()}`
    sessionStorage.setItem(key, id)
  }
  return id
}

export default function StudentJoinForm({ inviteCode }: { inviteCode?: string }) {
  const router = useRouter()
  const [roomCode, setRoomCode] = useState('')
  const [meetingPasscode, setMeetingPasscode] = useState('')
  const [request, setRequest] = useState<JoinRequest | null>(null)
  const [sessionId, setSessionId] = useState('')
  const [className, setClassName] = useState('')
  const [roomInfoLoaded, setRoomInfoLoaded] = useState(false)
  const [passcodeRequired, setPasscodeRequired] = useState(false)
  const [googleLoaded, setGoogleLoaded] = useState(false)
  const [googleCredential, setGoogleCredential] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const googleButtonRef = useRef<HTMLDivElement>(null)
  const googleClientId = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID
  const requestStorageKey = useMemo(() => `class_join_request_${inviteCode || roomCode.toUpperCase()}`, [inviteCode, roomCode])

  useEffect(() => {
    const id = getSessionId()
    setSessionId(id)
    if (inviteCode) {
      api.getInviteInfo(inviteCode).then(info => {
        setRoomCode(info.room_code)
        setClassName(info.class_name)
        setPasscodeRequired(info.meeting_passcode_required === true)
        setRoomInfoLoaded(true)
      }).catch((err: Error) => setError(err.message || 'This class link is invalid or no longer available.'))
    }
  }, [inviteCode])

  const validateRoomCode = async () => {
    const code = roomCode.trim().toUpperCase()
    if (!code) {
      setError('Enter a room code.')
      return
    }
    setLoading(true)
    setError('')
    setRoomInfoLoaded(false)
    try {
      const info = await api.getClassInfo(code)
      if (info.is_ended) throw new Error('This class has ended.')
      if (info.is_locked) throw new Error('Class is locked. New students cannot join.')
      if (typeof info.meeting_passcode_required !== 'boolean') throw new Error('Unable to verify this class’s password settings. Please try again.')
      setRoomCode(code)
      setClassName(info.class_name)
      setPasscodeRequired(info.meeting_passcode_required)
      setRoomInfoLoaded(true)
    } catch (err: any) {
      setError(err.message || 'Class not found or no longer available.')
    } finally {
      setLoading(false)
    }
  }

  const createRequest = useCallback(async (credential: string, passcode: string) => {
    if (!sessionId) return
    setLoading(true); setError('')
    try {
      const result = await api.createJoinRequest({
        google_credential: credential, meeting_passcode: passcode || undefined,
        room_code: roomCode, invite_code: inviteCode, session_id: sessionId,
      })
      sessionStorage.setItem(requestStorageKey, result.request_id)
      setRequest(result)
      setGoogleCredential('')
    } catch (err: any) {
      setError(err.message || 'Unable to request entry to this class.')
    } finally { setLoading(false) }
  }, [inviteCode, requestStorageKey, roomCode, sessionId])

  const handleGoogleCredential = useCallback((credential: string) => {
    setGoogleCredential(credential)
    setError('')
    if (!passcodeRequired) void createRequest(credential, '')
  }, [createRequest, passcodeRequired])

  useEffect(() => {
    const element = googleButtonRef.current
    if (!googleLoaded || !googleClientId || !window.google || !element || !roomInfoLoaded || !sessionId) return
    element.replaceChildren()
    window.google.accounts.id.initialize({
      client_id: googleClientId,
      callback: response => {
        if (!response.credential) {
          setError('Google sign-in did not complete. Please try again.')
          return
        }
        handleGoogleCredential(response.credential)
      },
      ux_mode: 'popup',
    })
    window.google.accounts.id.renderButton(element, {
      theme: 'outline', size: 'large', shape: 'rectangular', text: 'continue_with',
      width: Math.max(220, Math.min(400, Math.floor(element.clientWidth || 320))),
    })
  }, [googleLoaded, googleClientId, handleGoogleCredential, roomInfoLoaded, sessionId])

  useEffect(() => {
    if (!sessionId) return
    const previousId = sessionStorage.getItem(requestStorageKey)
    if (!previousId) return
    api.getJoinRequest(previousId, sessionId).then(setRequest).catch(() => sessionStorage.removeItem(requestStorageKey))
  }, [requestStorageKey, sessionId])

  useEffect(() => {
    if (!request || !sessionId || request.status !== 'WAITING') return
    let cancelled = false
    const check = async () => {
      try {
        const latest = await api.getJoinRequest(request.request_id, sessionId)
        if (!cancelled) {
          setRequest(latest)
          setError('')
        }
      } catch (err: any) {
        if (!cancelled) setError('Connection temporarily unavailable. Reconnecting...')
      }
    }
    const timer = window.setInterval(check, 2500)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [request, sessionId])

  useEffect(() => {
    if (!request || request.status !== 'APPROVED' || !sessionId) return
    let cancelled = false
    api.getApprovedJoinToken(request.request_id, sessionId).then(response => {
      if (cancelled) return
      sessionStorage.setItem('approved_join_request_id', request.request_id)
      sessionStorage.setItem('approved_join_session_id', sessionId)
      sessionStorage.setItem('livekit_token', response.token)
      sessionStorage.setItem('livekit_url', response.livekit_url)
      sessionStorage.setItem('room_code', response.room_code)
      sessionStorage.setItem('room_name', response.room_name)
      sessionStorage.setItem('is_teacher', 'false')
      router.push(`/class/${response.room_code}`)
    }).catch((err: Error) => !cancelled && setError(err.message || 'Unable to enter the classroom. Please try again.'))
    return () => { cancelled = true }
  }, [request, router, sessionId])

  const submitPasscode = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!googleCredential) { setError('Continue with Google before entering the meeting passcode.'); return }
    await createRequest(googleCredential, meetingPasscode)
  }

  if (request?.status === 'REJECTED') {
    return <StatusCard title="Request declined" message="Your request to join this class was declined." error />
  }
  if (request?.status === 'APPROVED') {
    return <StatusCard title="You’re in!" message="Connecting you to the classroom..." />
  }
  if (request?.status === 'WAITING') {
    return <StatusCard title="Waiting for the teacher" message="Your request has been sent. You’ll enter automatically when the teacher lets you in." note={error} />
  }

  return (
    <main className="min-h-screen flex flex-col items-center justify-center bg-gradient-to-br from-green-50 to-emerald-100 p-4 sm:p-8">
      <div className="max-w-md w-full bg-white rounded-lg shadow-xl p-5 sm:p-8">
        <h2 className="text-xl sm:text-2xl font-bold text-gray-900 mb-2 text-center">Join a Class</h2>
        {className && <p className="text-center text-gray-600 mb-5 break-words">{className}</p>}
        {!googleCredential ? (
          <div className="space-y-4">
            {!inviteCode && <>
              <Field label="Room Code" value={roomCode} onChange={value => { setRoomCode(value.toUpperCase()); setRoomInfoLoaded(false); setClassName(''); setPasscodeRequired(false) }} placeholder="e.g., ABC123" />
              {!roomInfoLoaded && <button type="button" onClick={() => void validateRoomCode()} disabled={loading} className="w-full py-3 bg-green-600 hover:bg-green-700 disabled:bg-green-400 text-white font-semibold rounded-lg shadow transition-colors">{loading ? 'Checking class…' : 'Continue'}</button>}
            </>}
            {googleClientId && roomInfoLoaded && sessionId ? <>
              <p className="text-center text-sm text-gray-600">Sign in to identify yourself to the teacher.</p>
              <div ref={googleButtonRef} className="flex min-h-10 w-full justify-center" />
              <p className="text-center text-xs text-gray-500">If you close the Google sign-in window, you can try again.</p>
              <Script src="https://accounts.google.com/gsi/client" strategy="afterInteractive" onLoad={() => setGoogleLoaded(true)} onError={() => setError('Google sign-in could not load. Check your connection and try again.')} />
              {googleLoaded && !window.google && <p className="text-sm text-red-700">Google sign-in is unavailable in this browser.</p>}
            </> : roomInfoLoaded && !googleClientId ? <p className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">Google sign-in is not configured for this site.</p> : inviteCode && !roomInfoLoaded ? <p className="text-center text-sm text-gray-600">Loading class details…</p> : null}
            {error && <div className="p-3 bg-red-50 border border-red-200 rounded-md text-red-700 text-sm break-words">{error}</div>}
            {loading && roomInfoLoaded && <p className="text-center text-sm text-gray-600">Sending your request…</p>}
          </div>
        ) : passcodeRequired ? (
          <form onSubmit={submitPasscode} className="space-y-4">
            <p className="rounded-md bg-green-50 p-3 text-sm text-green-800">Google sign-in complete. Enter the meeting password to request entry.</p>
            <Field label="Meeting Passcode" value={meetingPasscode} onChange={setMeetingPasscode} placeholder="Enter the meeting passcode" type="password" />
            {error && <div className="p-3 bg-red-50 border border-red-200 rounded-md text-red-700 text-sm break-words">{error}</div>}
            <button type="submit" disabled={loading} className="w-full py-3 bg-green-600 hover:bg-green-700 disabled:bg-green-400 text-white font-semibold rounded-lg shadow transition-colors">{loading ? 'Requesting entry...' : 'Continue to Waiting Room'}</button>
          </form>
        ) : null}
        <button onClick={() => router.push('/')} className="w-full mt-4 py-3 sm:py-2 text-gray-600 hover:text-gray-800">Back to Home</button>
      </div>
    </main>
  )
}

function Field({ label, value, onChange, placeholder, type = 'text' }: { label: string; value: string; onChange: (value: string) => void; placeholder: string; type?: string }) {
  return <div><label className="block text-sm font-medium text-gray-700 mb-1">{label}</label><input type={type} value={value} onChange={e => onChange(e.target.value)} className="w-full px-4 py-2.5 sm:py-2 bg-white text-black border border-gray-300 rounded-md focus:ring-2 focus:ring-green-500 focus:border-green-500" placeholder={placeholder} required /></div>
}

function StatusCard({ title, message, note, error }: { title: string; message: string; note?: string; error?: boolean }) {
  return <main className="min-h-screen flex items-center justify-center bg-gradient-to-br from-green-50 to-emerald-100 p-4"><div className="max-w-md w-full bg-white rounded-lg shadow-xl p-8 text-center"><div className="text-4xl mb-4">{error ? '🙁' : '⏳'}</div><h1 className="text-2xl font-bold text-gray-900 mb-3">{title}</h1><p className="text-gray-600">{message}</p>{note && <p className="mt-4 text-sm text-amber-700">{note}</p>}</div></main>
}
