
'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { api } from '@/lib/api'
import PasscodeGate from '@/components/PasscodeGate'

export default function TeacherPage() {
  const [unlocked, setUnlocked] = useState(false)

  if (!unlocked) {
    return (
      <PasscodeGate
        title="Teacher Access"
        description="Enter the teacher passcode to create and manage classes"
        icon="👨‍🏫"
        onVerify={async (passcode) => {
          await api.verifyTeacherPasscode(passcode)
          return true
        }}
        onSuccess={() => setUnlocked(true)}
      />
    )
  }

  return <TeacherForm />
}

function TeacherForm() {
  const router = useRouter()
  const [teacherName, setTeacherName]         = useState('')
  const [className, setClassName]             = useState('')
  const [meetingPasscode, setMeetingPasscode] = useState('')
  const [maxParticipants]                     = useState(50)
  const [micPolicy, setMicPolicy]             = useState<'allowed' | 'muted_by_default' | 'locked'>('allowed')
  const [cameraPolicy, setCameraPolicy]       = useState<'allowed' | 'off_by_default' | 'locked'>('allowed')
  const [loading, setLoading]                 = useState(false)
  const [error, setError]                     = useState('')
  const [createdRoom, setCreatedRoom]         = useState<{
    room_code: string; room_name: string; token: string; livekit_url: string
  } | null>(null)
  const [showPasscode, setShowPasscode] = useState(false)

  const handleCreateClass = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const response = await api.createRoom({
        teacher_name: teacherName,
        room_name: className,
        meeting_passcode: meetingPasscode,
        max_participants: maxParticipants,
        student_microphone_policy: micPolicy,
        student_camera_policy: cameraPolicy,
      })
      setCreatedRoom(response)
    } catch (err: any) {
      setError(err.message || 'Failed to create class')
    } finally {
      setLoading(false)
    }
  }

  const handleEnterClassroom = () => {
    if (!createdRoom) return
    sessionStorage.setItem('livekit_token', createdRoom.token)
    sessionStorage.setItem('livekit_url', createdRoom.livekit_url)
    sessionStorage.setItem('room_code', createdRoom.room_code)
    sessionStorage.setItem('room_name', createdRoom.room_name)
    sessionStorage.setItem('is_teacher', 'true')
    router.push(`/class/${createdRoom.room_code}`)
  }

  const handleCopyDetails = () => {
    if (!createdRoom) return
    const text = `Class: ${createdRoom.room_name}\nRoom Code: ${createdRoom.room_code}\nMeeting Passcode: ${meetingPasscode}`
    navigator.clipboard.writeText(text)
    alert('Class details copied!')
  }

  if (createdRoom) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center bg-gradient-to-br from-blue-50 to-indigo-100 p-4 sm:p-8">
        <div className="max-w-md w-full bg-white rounded-lg shadow-xl p-5 sm:p-8">
          <h2 className="text-xl sm:text-2xl font-bold text-gray-900 mb-6 text-center">Class Created! ✅</h2>
          <div className="space-y-4 mb-6">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Class Name</label>
              <div className="px-4 py-2 bg-gray-50 border border-gray-300 rounded-md break-words">{createdRoom.room_name}</div>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Room Code</label>
              <div className="px-4 py-2 bg-gray-50 border border-gray-300 rounded-md font-mono text-lg break-all">{createdRoom.room_code}</div>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Meeting Passcode</label>
              <div className="relative">
                <div className="px-4 py-2 min-h-[44px] bg-gray-50 border border-gray-300 rounded-md font-mono text-lg pr-20 break-all">
                  {showPasscode ? meetingPasscode : '••••••••'}
                </div>
                <button
                  type="button"
                  onClick={() => setShowPasscode(!showPasscode)}
                  className="absolute right-1 top-1/2 -translate-y-1/2 px-3 py-2 text-sm text-blue-600 hover:text-blue-800"
                >
                  {showPasscode ? 'Hide' : 'Show'}
                </button>
              </div>
            </div>
            <div className="text-sm text-gray-600 bg-blue-50 p-3 rounded-md">
              Share <strong>Room Code</strong> and <strong>Meeting Passcode</strong> with students.
            </div>
          </div>
          <div className="space-y-3">
            <button onClick={handleEnterClassroom}
              className="w-full py-3 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-lg transition-colors">
              Enter Classroom
            </button>
            <button onClick={handleCopyDetails}
              className="w-full py-3 bg-gray-600 hover:bg-gray-700 text-white font-semibold rounded-lg transition-colors">
              Copy Class Details
            </button>
          </div>
        </div>
      </main>
    )
  }

  return (
    <main className="min-h-screen flex flex-col items-center justify-center bg-gradient-to-br from-blue-50 to-indigo-100 p-4 sm:p-8">
      <div className="max-w-md w-full bg-white rounded-lg shadow-xl p-5 sm:p-8">
        <h2 className="text-xl sm:text-2xl font-bold text-gray-900 mb-6 text-center">Create New Class</h2>
        <form onSubmit={handleCreateClass} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Teacher Name</label>
            <input type="text" value={teacherName} onChange={e => setTeacherName(e.target.value)}
              autoComplete="off" data-lpignore="true"
              className="w-full px-4 py-2.5 sm:py-2 bg-white text-black caret-black placeholder:text-gray-400 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              placeholder="Your name" required />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Class Name</label>
            <input type="text" value={className} onChange={e => setClassName(e.target.value)}
              autoComplete="off" data-lpignore="true"
              className="w-full px-4 py-2.5 sm:py-2 bg-white text-black caret-black placeholder:text-gray-400 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              placeholder="e.g. DBMS - Normalization" required />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Meeting Passcode</label>
            <input type="text" value={meetingPasscode} onChange={e => setMeetingPasscode(e.target.value)}
              autoComplete="off" data-lpignore="true" data-form-type="other"
              className="w-full px-4 py-2.5 sm:py-2 bg-white text-black caret-black placeholder:text-gray-400 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              placeholder="Passcode for students to join" required />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Student Microphones</label>
            <select value={micPolicy} onChange={e => setMicPolicy(e.target.value as any)}
              className="w-full px-4 py-2.5 sm:py-2 bg-white text-black border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500">
              <option value="allowed">Allowed</option>
              <option value="muted_by_default">Muted by Default</option>
              <option value="locked">Locked</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Student Cameras</label>
            <select value={cameraPolicy} onChange={e => setCameraPolicy(e.target.value as any)}
              className="w-full px-4 py-2.5 sm:py-2 bg-white text-black border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500">
              <option value="allowed">Allowed</option>
              <option value="off_by_default">Off by Default</option>
              <option value="locked">Locked</option>
            </select>
          </div>
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded-md text-red-700 text-sm break-words">{error}</div>
          )}
          <button type="submit" disabled={loading}
            className="w-full py-3 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 text-white font-semibold rounded-lg transition-colors">
            {loading ? 'Creating...' : 'Create Class'}
          </button>
        </form>
        <button onClick={() => window.location.href = '/'}
          className="w-full mt-4 py-3 sm:py-2 text-gray-600 hover:text-gray-800">
          Back to Home
        </button>
      </div>
    </main>
  )
}
