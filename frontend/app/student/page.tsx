'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { api } from '@/lib/api'

export default function StudentPage() {
  const router = useRouter()
  const [studentName, setStudentName] = useState('')
  const [roomCode, setRoomCode] = useState('')
  const [meetingPasscode, setMeetingPasscode] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleJoinClass = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)

    try {
      const response = await api.joinRoom({
        student_name: studentName,
        room_code: roomCode.toUpperCase(),
        meeting_passcode: meetingPasscode,
      })
      
      sessionStorage.setItem('livekit_token', response.token)
      sessionStorage.setItem('livekit_url', response.livekit_url)
      sessionStorage.setItem('room_code', response.room_code)
      sessionStorage.setItem('room_name', response.room_name)
      sessionStorage.setItem('is_teacher', 'false')
      
      router.push(`/class/${response.room_code}`)
    } catch (err: any) {
      setError(err.message || 'Failed to join class')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="min-h-screen flex flex-col items-center justify-center bg-gradient-to-br from-green-50 to-emerald-100 p-4 sm:p-8">
      <div className="max-w-md w-full bg-white rounded-lg shadow-xl p-5 sm:p-8">
        <h2 className="text-xl sm:text-2xl font-bold text-gray-900 mb-6 text-center">
          Join a Class
        </h2>
        
        <form onSubmit={handleJoinClass} className="space-y-4">
          <div>
            <label htmlFor="studentName" className="block text-sm font-medium text-gray-700 mb-1">
              Student Name
            </label>
            <input
              type="text"
              id="studentName"
              value={studentName}
              onChange={(e) => setStudentName(e.target.value)}
              className="w-full px-4 py-2.5 sm:py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-green-500 focus:border-green-500"
              placeholder="Enter your name"
              required
            />
          </div>
          
          <div>
            <label htmlFor="roomCode" className="block text-sm font-medium text-gray-700 mb-1">
              Room Code
            </label>
            <input
              type="text"
              id="roomCode"
              value={roomCode}
              onChange={(e) => setRoomCode(e.target.value.toUpperCase())}
              className="w-full px-4 py-2.5 sm:py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-green-500 focus:border-green-500 font-mono uppercase"
              placeholder="e.g., ABC123"
              required
            />
            <p className="mt-1 text-sm text-gray-500">
              Get this from your teacher
            </p>
          </div>
          
          <div>
            <label htmlFor="meetingPasscode" className="block text-sm font-medium text-gray-700 mb-1">
              Meeting Passcode
            </label>
            <input
              type="password"
              id="meetingPasscode"
              value={meetingPasscode}
              onChange={(e) => setMeetingPasscode(e.target.value)}
              className="w-full px-4 py-2.5 sm:py-2 border border-gray-300 rounded-md focus:ring-2 focus:ring-green-500 focus:border-green-500"
              placeholder="Enter the meeting passcode"
              required
            />
            <p className="mt-1 text-sm text-gray-500">
              Provided by your teacher
            </p>
          </div>
          
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded-md text-red-700 text-sm break-words">
              {error}
            </div>
          )}
          
          <button
            type="submit"
            disabled={loading}
            className="w-full py-3 bg-green-600 hover:bg-green-700 disabled:bg-green-400 text-white font-semibold rounded-lg shadow transition-colors"
          >
            {loading ? 'Joining...' : 'Join Class'}
          </button>
        </form>
        
        <button
          onClick={() => window.location.href = '/'}
          className="w-full mt-4 py-3 sm:py-2 text-gray-600 hover:text-gray-800"
        >
          Back to Home
        </button>
      </div>
    </main>
  )
}