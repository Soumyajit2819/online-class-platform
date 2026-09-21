'use client'

import { useEffect, useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'
import { api, JoinRequest } from '@/lib/api'

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
  const [studentName, setStudentName] = useState('')
  const [roomCode, setRoomCode] = useState('')
  const [meetingPasscode, setMeetingPasscode] = useState('')
  const [request, setRequest] = useState<JoinRequest | null>(null)
  const [sessionId, setSessionId] = useState('')
  const [className, setClassName] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const requestStorageKey = useMemo(() => `class_join_request_${inviteCode || roomCode.toUpperCase()}`, [inviteCode, roomCode])

  useEffect(() => {
    const id = getSessionId()
    setSessionId(id)
    if (inviteCode) {
      api.getInviteInfo(inviteCode).then(info => {
        setRoomCode(info.room_code)
        setClassName(info.class_name)
      }).catch((err: Error) => setError(err.message || 'This class link is invalid or no longer available.'))
    }
  }, [inviteCode])

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
      sessionStorage.setItem('livekit_token', response.token)
      sessionStorage.setItem('livekit_url', response.livekit_url)
      sessionStorage.setItem('room_code', response.room_code)
      sessionStorage.setItem('room_name', response.room_name)
      sessionStorage.setItem('is_teacher', 'false')
      router.push(`/class/${response.room_code}`)
    }).catch((err: Error) => !cancelled && setError(err.message || 'Unable to enter the classroom. Please try again.'))
    return () => { cancelled = true }
  }, [request, router, sessionId])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!sessionId) return
    setLoading(true); setError('')
    try {
      const result = await api.createJoinRequest({
        student_name: studentName, meeting_passcode: meetingPasscode,
        room_code: roomCode, invite_code: inviteCode, session_id: sessionId,
      })
      sessionStorage.setItem(requestStorageKey, result.request_id)
      setRequest(result)
    } catch (err: any) {
      setError(err.message || 'Unable to request entry to this class.')
    } finally { setLoading(false) }
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
        <form onSubmit={submit} className="space-y-4">
          <Field label="Student Name" value={studentName} onChange={setStudentName} placeholder="Enter your name" />
          {!inviteCode && <Field label="Room Code" value={roomCode} onChange={value => setRoomCode(value.toUpperCase())} placeholder="e.g., ABC123" />}
          <Field label="Meeting Passcode" value={meetingPasscode} onChange={setMeetingPasscode} placeholder="Enter the meeting passcode" type="password" />
          {error && <div className="p-3 bg-red-50 border border-red-200 rounded-md text-red-700 text-sm break-words">{error}</div>}
          <button type="submit" disabled={loading || !!error && !!inviteCode && !roomCode} className="w-full py-3 bg-green-600 hover:bg-green-700 disabled:bg-green-400 text-white font-semibold rounded-lg shadow transition-colors">
            {loading ? 'Requesting entry...' : 'Request to Join'}
          </button>
        </form>
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
