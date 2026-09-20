'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { api } from '@/lib/api'

export default function TeacherPage() {
  const router = useRouter()
  const [teacherName, setTeacherName] = useState('')
  const [className, setClassName] = useState('')
  const [meetingPasscode, setMeetingPasscode] = useState('')
  const [maxParticipants] = useState(50)
  const [micPolicy, setMicPolicy] = useState<'allowed' | 'muted_by_default' | 'locked'>('allowed')
  const [cameraPolicy, setCameraPolicy] = useState<'allowed' | 'off_by_default' | 'locked'>('allowed')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [createdRoom, setCreatedRoom] = useState<{
    room_code: string
    room_name: string
    token: string
    livekit_url: string
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
    if (createdRoom) {
      sessionStorage.setItem('livekit_token', createdRoom.token)
      sessionStorage.setItem('livekit_url', createdRoom.livekit_url)
      sessionStorage.setItem('room_code', createdRoom.room_code)
      sessionStorage.setItem('room_name', createdRoom.room_name)
      sessionStorage.setItem('is_teacher', 'true')
      router.push(`/class/${createdRoom.room_code}`)
    }
  }

  const handleCopyDetails = () => {
    if (createdRoom) {
      const details = `Class: ${createdRoom.room_name}\nRoom Code: ${createdRoom.room_code}\nMeeting Passcode: ${meetingPasscode}`
      navigator.clipboard.writeText(details)
      alert('Class details copied to clipboard!')
    }
  }

  if (createdRoom) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center bg-gradient-to-br from-blue-50 to-indigo-100 p-8">
        <div className="max-w-md w-full bg-white rounded-lg shadow-xl p-8">
          <h2 className="text-2xl font-bold text-gray-900 mb-6 text-center">
            Class Created Successfully!
          </h2>
          
          <div className="space-y-4 mb-6">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Class Name
              </label>
              <div className="px-4 py-2 bg-gray-50 border border-gray-300 rounded-md">
                {createdRoom.room_name}
              </div>
            </div>
            
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Room Code
              </label>
              <div className="px-4 py-2 bg-gray-50 border border-gray-300 rounded-md font-mono text-lg">
                {createdRoom.room_code}
              </div>
            </div>
            
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Meeting Passcode
              </label>
              <div className="relative">
                <div className="px-4 py-2 bg-gray-50 border border-gray-300 rounded-md font-mono text-lg pr-20">
                  {showPasscode ? meetingPasscode : '••••••••'}
                </div>
                <button
                  type="button"
                  onClick={() => setShowPasscode(!showPasscode)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-sm text-blue-600 hover:text-blue-800"
                >
                  {showPasscode ? 'Hide' : 'Show'}
                </button>
              </div>
            </div>
            
            <div className="text-sm text-gray-600 bg-blue-50 p-3 rounded-md">
              <p className="font-medium mb-1">Share these details with your students:</p>
              <ul className="list-disc list-inside space-y-1">
                <li>Room Code: <span className="font-mono">{createdRoom.room_code}</span></li>
                <li>Meeting Passcode</li>
              </ul>
            </div>
          </div>
          
          <div className="space-y-3">
            <button
              onClick={handleEnterClassroom}
              className="w-full py-3 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-lg shadow transition-colors"
            >
              Enter Classroom
            </button>
            <button
              onClick={handleCopyDetails}
              className="w-full py-3 bg-gray-600 hover:bg-gray-700 text-white font-semibold rounded-lg shadow transition-colors"
            >
              Copy Class Details
            </button>
            <button
              onClick={() => {
                setCreatedRoom(null)
                setTeacherName('')
                setClassName('')
                setMeetingPasscode('')
              }}
              className="w-full py-2 text-gray-600 hover:text-gray-800"
            >
              Create Another Class
            </button>
          </div>
        </div>
      </main>
    )
  }

  return (
    <main className="min-h-screen flex flex-col items-center justify-center bg-gradient-to-br from-blue-50 to-indigo-100 p-8">
      <div className="max-w-md w-full bg-white rounded-lg shadow-xl p-8">
        <h2 className="text-2xl font-bold text-gray-900 mb-6 text-center">
          Create New Class
        </h2>
        
        <form onSubmit={handleCreateClass} className="space-y-4">
          <div>
            <label htmlFor="teacherName" className="block text-sm font-medium text-gray-700 mb-1">
              Teacher Name
            </label>
            <input
              type="text"
              id="teacherName"
              value={teacherName}
              onChange={(e) => setTeacherName(e.target.value)}
              className="w-full px-4 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              placeholder="Enter your name"
              required
            />
          </div>
          
          <div>
            <label htmlFor="className" className="block text-sm font-medium text-gray-700 mb-1">
              Class Name
            </label>
            <input
              type="text"
              id="className"
              value={className}
              onChange={(e) => setClassName(e.target.value)}
              className="w-full px-4 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              placeholder="e.g., DBMS - Normalization"
              required
            />
          </div>
          
          <div>
            <label htmlFor="meetingPasscode" className="block text-sm font-medium text-gray-700 mb-1">
              Meeting Passcode
            </label>
            <input
              type="text"
              id="meetingPasscode"
              value={meetingPasscode}
              onChange={(e) => setMeetingPasscode(e.target.value)}
              className="w-full px-4 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              placeholder="e.g., DBMS123"
              required
            />
            <p className="mt-1 text-sm text-gray-500">
              Students will need this passcode to join
            </p>
          </div>
          
          <div>
            <label htmlFor="maxParticipants" className="block text-sm font-medium text-gray-700 mb-1">
              Maximum Participants
            </label>
            <input
              type="number"
              id="maxParticipants"
              value={maxParticipants}
              disabled
              className="w-full px-4 py-2 border border-gray-300 rounded-md bg-gray-50 text-gray-500"
            />
            <p className="mt-1 text-sm text-gray-500">
              Fixed at 50 for MVP
            </p>
          </div>
          
          <div>
            <label htmlFor="micPolicy" className="block text-sm font-medium text-gray-700 mb-1">
              Student Microphones
            </label>
            <select
              id="micPolicy"
              value={micPolicy}
              onChange={(e) => setMicPolicy(e.target.value as any)}
              className="w-full px-4 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            >
              <option value="allowed">Allowed</option>
              <option value="muted_by_default">Muted by Default</option>
              <option value="locked">Locked</option>
            </select>
          </div>
          
          <div>
            <label htmlFor="cameraPolicy" className="block text-sm font-medium text-gray-700 mb-1">
              Student Cameras
            </label>
            <select
              id="cameraPolicy"
              value={cameraPolicy}
              onChange={(e) => setCameraPolicy(e.target.value as any)}
              className="w-full px-4 py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            >
              <option value="allowed">Allowed</option>
              <option value="off_by_default">Off by Default</option>
              <option value="locked">Locked</option>
            </select>
          </div>
          
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded-md text-red-700 text-sm">
              {error}
            </div>
          )}
          
          <button
            type="submit"
            disabled={loading}
            className="w-full py-3 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 text-white font-semibold rounded-lg shadow transition-colors"
          >
            {loading ? 'Creating...' : 'Create Class'}
          </button>
        </form>
        
        <button
          onClick={() => window.location.href = '/'}
          className="w-full mt-4 py-2 text-gray-600 hover:text-gray-800"
        >
          Back to Home
        </button>
      </div>
    </main>
  )
}
