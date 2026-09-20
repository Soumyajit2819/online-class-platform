'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import dynamic from 'next/dynamic'

// Dynamically import VideoConference to avoid SSR issues with LiveKit
const VideoConference = dynamic(
  () => import('@/components/VideoConference'),
  { ssr: false }
)

export default function ClassroomPage({
  params,
}: {
  params: { roomCode: string }
}) {
  const router = useRouter()
  const [token, setToken] = useState<string | null>(null)
  const [livekitUrl, setLivekitUrl] = useState<string | null>(null)
  const [roomName, setRoomName] = useState<string>('')
  const [isTeacher, setIsTeacher] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // Get token and other data from session storage
    const storedToken = sessionStorage.getItem('livekit_token')
    const storedUrl = sessionStorage.getItem('livekit_url')
    const storedRoomName = sessionStorage.getItem('room_name')
    const storedIsTeacher = sessionStorage.getItem('is_teacher')

    if (!storedToken || !storedUrl) {
      setError('No active session found. Please join or create a class.')
      return
    }

    setToken(storedToken)
    setLivekitUrl(storedUrl)
    setRoomName(storedRoomName || 'Class')
    setIsTeacher(storedIsTeacher === 'true')
  }, [])

  if (error) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center bg-gray-900 p-8">
        <div className="max-w-md w-full bg-gray-800 rounded-lg shadow-xl p-8 text-center">
          <h2 className="text-2xl font-bold text-white mb-4">Error</h2>
          <p className="text-gray-300 mb-6">{error}</p>
          <button
            onClick={() => router.push('/')}
            className="px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors"
          >
            Go to Home
          </button>
        </div>
      </main>
    )
  }

  if (!token || !livekitUrl) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-gray-900">
        <div className="text-white">Loading classroom...</div>
      </main>
    )
  }

  return (
    <VideoConference
      token={token}
      serverUrl={livekitUrl}
      roomCode={params.roomCode}
      roomName={roomName}
      isTeacher={isTeacher}
    />
  )
}
