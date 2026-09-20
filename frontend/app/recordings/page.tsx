'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { api, Recording } from '@/lib/api'

export default function RecordingsPage() {
  const [recordings, setRecordings] = useState<Recording[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    fetchRecordings()
  }, [])

  const fetchRecordings = async () => {
    setLoading(true)
    setError('')
    try {
      const response = await api.getAllRecordings()
      setRecordings(response.recordings)
    } catch (err: any) {
      setError(err.message || 'Failed to fetch recordings')
    } finally {
      setLoading(false)
    }
  }

  const handleDownload = async (recordingId: string) => {
    try {
      const result = await api.getRecordingDownloadUrl(recordingId)
      if (result.download_url) {
        // Open download URL in new tab
        window.open(result.download_url, '_blank')
      } else {
        alert('Download URL not available yet. Please try again later.')
      }
    } catch (err: any) {
      alert(err.message || 'Failed to get download URL')
    }
  }

  const formatDate = (dateString: string) => {
    const date = new Date(dateString)
    return date.toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  }

  const getExpiryStatus = (expiresAt: string) => {
    const now = new Date()
    const expiry = new Date(expiresAt)
    const hoursLeft = Math.floor((expiry.getTime() - now.getTime()) / (1000 * 60 * 60))
    
    if (hoursLeft < 0) {
      return { text: 'Expired', color: 'text-red-600' }
    } else if (hoursLeft < 24) {
      return { text: `${hoursLeft} hours left`, color: 'text-orange-600' }
    } else {
      const daysLeft = Math.floor(hoursLeft / 24)
      return { text: `${daysLeft} day${daysLeft > 1 ? 's' : ''} left`, color: 'text-green-600' }
    }
  }

  const formatDuration = (seconds: number) => {
    const hours = Math.floor(seconds / 3600)
    const minutes = Math.floor((seconds % 3600) / 60)
    const secs = seconds % 60
    
    if (hours > 0) {
      return `${hours}h ${minutes}m ${secs}s`
    } else if (minutes > 0) {
      return `${minutes}m ${secs}s`
    } else {
      return `${secs}s`
    }
  }

  return (
    <main className="min-h-screen bg-gradient-to-br from-purple-50 to-indigo-100 p-8">
      <div className="max-w-6xl mx-auto">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-3xl font-bold text-gray-900">Class Recordings</h1>
            <p className="text-gray-600 mt-1">Download your class recordings (available for 3 days)</p>
          </div>
          <div className="flex gap-4">
            <button
              onClick={fetchRecordings}
              className="px-4 py-2 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
            >
              Refresh
            </button>
            <Link
              href="/"
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors"
            >
              Back to Home
            </Link>
          </div>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-20">
            <div className="text-gray-600">Loading recordings...</div>
          </div>
        ) : error ? (
          <div className="bg-red-50 border border-red-200 rounded-lg p-6 text-center">
            <p className="text-red-700">{error}</p>
            <button
              onClick={fetchRecordings}
              className="mt-4 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg"
            >
              Try Again
            </button>
          </div>
        ) : recordings.length === 0 ? (
          <div className="bg-white rounded-lg shadow-md p-12 text-center">
            <div className="text-6xl mb-4">📹</div>
            <h2 className="text-xl font-semibold text-gray-800 mb-2">No Recordings Available</h2>
            <p className="text-gray-600">
              Class recordings will appear here after your teacher ends a recorded session.
            </p>
          </div>
        ) : (
          <div className="grid gap-6">
            {recordings.map((recording) => {
              const expiryStatus = getExpiryStatus(recording.expires_at)
              return (
                <div
                  key={recording.recording_id}
                  className="bg-white rounded-lg shadow-md hover:shadow-lg transition-shadow p-6"
                >
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <h3 className="text-xl font-semibold text-gray-900 mb-2">
                        {recording.class_name}
                      </h3>
                      <div className="space-y-1 text-sm text-gray-600">
                        <p>
                          <span className="font-medium">Teacher:</span> {recording.teacher_name}
                        </p>
                        <p>
                          <span className="font-medium">Recorded:</span> {formatDate(recording.started_at)}
                        </p>
                        {recording.duration_seconds > 0 && (
                          <p>
                            <span className="font-medium">Duration:</span> {formatDuration(recording.duration_seconds)}
                          </p>
                        )}
                        <p className={`font-medium ${expiryStatus.color}`}>
                          {expiryStatus.text}
                        </p>
                      </div>
                    </div>
                    <div className="flex flex-col items-end gap-3">
                      <span
                        className={`px-3 py-1 rounded-full text-sm font-medium ${
                          recording.status === 'available'
                            ? 'bg-green-100 text-green-800'
                            : recording.status === 'failed'
                            ? 'bg-red-100 text-red-800'
                            : 'bg-yellow-100 text-yellow-800'
                        }`}
                      >
                        {recording.status.charAt(0).toUpperCase() + recording.status.slice(1)}
                      </span>
                      {recording.status === 'available' && (
                        <button
                          onClick={() => handleDownload(recording.recording_id)}
                          className="px-6 py-2 bg-blue-600 hover:bg-blue-700 text-white font-medium rounded-lg transition-colors flex items-center gap-2"
                        >
                          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                          </svg>
                          Download
                        </button>
                      )}
                    </div>
                  </div>
                  
                  {recording.status === 'available' && (
                    <div className="mt-4 pt-4 border-t border-gray-100">
                      <p className="text-xs text-gray-500">
                        ⏰ Recording will be available for download until {formatDate(recording.expires_at)}
                      </p>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}

        <div className="mt-8 bg-blue-50 border border-blue-200 rounded-lg p-6">
          <h3 className="font-semibold text-blue-900 mb-2">ℹ️ About Recordings</h3>
          <ul className="text-sm text-blue-800 space-y-1">
            <li>• Recordings are automatically saved when the teacher ends a recorded class</li>
            <li>• Recordings are available for <strong>3 days</strong> after the class ends</li>
            <li>• Download recordings before they expire</li>
            <li>• Recordings include both video and audio from all participants</li>
          </ul>
        </div>
      </div>
    </main>
  )
}
