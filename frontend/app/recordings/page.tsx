'use client'

import { useEffect, useState, useCallback } from 'react'
import Link from 'next/link'
import { api, Recording } from '@/lib/api'

// ---------------------------------------------------------------------------
// Countdown hook — re-renders every second
// ---------------------------------------------------------------------------
function useCountdown(expiresAt: string) {
  const calc = () => {
    const diff = new Date(expiresAt).getTime() - Date.now()
    if (diff <= 0) return { expired: true, text: 'Expired', pct: 0, urgent: false }

    const totalMs = 20 * 60 * 60 * 1000   // 20 hours in ms
    const pct     = Math.max(0, Math.min(100, (diff / totalMs) * 100))

    const h = Math.floor(diff / 3600000)
    const m = Math.floor((diff % 3600000) / 60000)
    const s = Math.floor((diff % 60000) / 1000)

    const text = h > 0
      ? `${h}h ${m}m left`
      : m > 0
        ? `${m}m ${s}s left`
        : `${s}s left`

    return { expired: false, text, pct, urgent: diff < 2 * 3600000 } // urgent if < 2h
  }

  const [state, setState] = useState(calc)

  useEffect(() => {
    setState(calc())
    const id = setInterval(() => {
      const next = calc()
      setState(next)
      if (next.expired) clearInterval(id)
    }, 1000)
    return () => clearInterval(id)
  }, [expiresAt])

  return state
}

// ---------------------------------------------------------------------------
// Single recording card
// ---------------------------------------------------------------------------
function RecordingCard({ recording, onDownload }: {
  recording: Recording
  onDownload: (id: string) => void
}) {
  const countdown = useCountdown(recording.expires_at)

  const barColor = countdown.urgent
    ? 'bg-red-500'
    : countdown.pct > 50
      ? 'bg-green-500'
      : 'bg-yellow-500'

  const formatDate = (d: string) =>
    new Date(d).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })

  const formatSize = (mb: number) =>
    mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb} MB`

  return (
    <div className={`bg-white rounded-xl shadow-md hover:shadow-lg transition-shadow p-6 border-l-4 ${
      countdown.expired ? 'border-gray-300 opacity-50' : countdown.urgent ? 'border-red-500' : 'border-blue-500'
    }`}>
      <div className="flex items-start justify-between gap-4">
        {/* Info */}
        <div className="flex-1 min-w-0">
          <h3 className="text-xl font-semibold text-gray-900 truncate">
            {recording.class_name}
          </h3>
          <p className="text-sm text-gray-500 mt-1">
            Teacher: <span className="font-medium text-gray-700">{recording.teacher_name}</span>
          </p>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm text-gray-500">
            <span>🗓 {formatDate(recording.started_at)}</span>
            {recording.file_size_mb > 0 && (
              <span>💾 {formatSize(recording.file_size_mb)}</span>
            )}
          </div>
        </div>

        {/* Download button */}
        <div className="flex flex-col items-end gap-2 flex-shrink-0">
          {!countdown.expired ? (
            <button
              onClick={() => onDownload(recording.recording_id)}
              className="px-5 py-2 bg-blue-600 hover:bg-blue-700 text-white font-medium rounded-lg transition-colors flex items-center gap-2 whitespace-nowrap"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
              Download MP4
            </button>
          ) : (
            <span className="px-4 py-2 bg-gray-100 text-gray-500 rounded-lg text-sm">
              Link Expired
            </span>
          )}
        </div>
      </div>

      {/* Countdown bar */}
      {!countdown.expired && (
        <div className="mt-4">
          <div className="flex justify-between items-center mb-1">
            <span className={`text-sm font-medium ${
              countdown.urgent ? 'text-red-600' : 'text-gray-600'
            }`}>
              {countdown.urgent ? '⚠️ ' : '⏰ '}
              {countdown.text}
            </span>
            <span className="text-xs text-gray-400">
              Available for 20 hours after recording
            </span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2">
            <div
              className={`h-2 rounded-full transition-all duration-1000 ${barColor}`}
              style={{ width: `${countdown.pct}%` }}
            />
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export default function RecordingsPage() {
  const [recordings, setRecordings]   = useState<Recording[]>([])
  const [loading, setLoading]         = useState(true)
  const [error, setError]             = useState('')
  const [downloading, setDownloading] = useState<string | null>(null)
  const [toast, setToast]             = useState('')

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(''), 3000)
  }

  const fetchRecordings = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await api.getAllRecordings()
      // Filter out already-expired ones client-side too
      const valid = res.recordings.filter(
        r => new Date(r.expires_at).getTime() > Date.now()
      )
      setRecordings(valid)
    } catch (err: any) {
      setError(err.message || 'Failed to fetch recordings')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchRecordings()
    // Refresh list every 60s to catch newly available recordings
    const id = setInterval(fetchRecordings, 60000)
    return () => clearInterval(id)
  }, [fetchRecordings])

  const handleDownload = async (recordingId: string) => {
    setDownloading(recordingId)
    try {
      const result = await api.getRecordingDownloadUrl(recordingId)
      if (result.download_url) {
        // Create hidden link and click it to trigger download
        const a = document.createElement('a')
        a.href     = result.download_url
        a.download = `class-recording-${recordingId}.mp4`
        a.target   = '_blank'
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        showToast('Download started!')
      } else {
        showToast('Download URL not available. Try again shortly.')
      }
    } catch (err: any) {
      showToast(err.message || 'Download failed')
    } finally {
      setDownloading(null)
    }
  }

  return (
    <main className="min-h-screen bg-gradient-to-br from-slate-50 to-blue-50 p-8">
      {/* Toast notification */}
      {toast && (
        <div className="fixed top-4 right-4 z-50 bg-gray-900 text-white px-6 py-3 rounded-lg shadow-lg text-sm">
          {toast}
        </div>
      )}

      <div className="max-w-4xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-3xl font-bold text-gray-900">Class Recordings</h1>
            <p className="text-gray-500 mt-1">
              Downloads available for <span className="font-semibold text-blue-600">20 hours</span> after recording ends
            </p>
          </div>
          <div className="flex gap-3">
            <button
              onClick={fetchRecordings}
              disabled={loading}
              className="px-4 py-2 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 text-sm transition-colors disabled:opacity-50"
            >
              {loading ? 'Refreshing...' : '↻ Refresh'}
            </button>
            <Link
              href="/"
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm transition-colors"
            >
              ← Home
            </Link>
          </div>
        </div>

        {/* Info banner */}
        <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 mb-6 flex gap-3">
          <span className="text-2xl">ℹ️</span>
          <div className="text-sm text-blue-800">
            <p className="font-semibold mb-1">About Recordings</p>
            <ul className="space-y-0.5 text-blue-700">
              <li>• Recordings are saved automatically when class ends</li>
              <li>• Available to download for <strong>20 hours</strong> only</li>
              <li>• Download the MP4 file to your device before the timer expires</li>
              <li>• After 20 hours the file is permanently deleted from our servers</li>
            </ul>
          </div>
        </div>

        {/* Content */}
        {loading && recordings.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-24 text-gray-400">
            <div className="w-10 h-10 border-4 border-blue-300 border-t-blue-600 rounded-full animate-spin mb-4" />
            Loading recordings...
          </div>
        ) : error ? (
          <div className="bg-red-50 border border-red-200 rounded-xl p-6 text-center">
            <p className="text-red-700 mb-3">{error}</p>
            <button
              onClick={fetchRecordings}
              className="px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg text-sm"
            >
              Try Again
            </button>
          </div>
        ) : recordings.length === 0 ? (
          <div className="bg-white rounded-xl shadow-md p-16 text-center">
            <div className="text-6xl mb-4">📹</div>
            <h2 className="text-xl font-semibold text-gray-800 mb-2">No Recordings Available</h2>
            <p className="text-gray-500 text-sm max-w-sm mx-auto">
              Recordings appear here after your teacher starts and stops recording during a class.
              Links expire after 20 hours.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            <p className="text-sm text-gray-500">{recordings.length} recording{recordings.length !== 1 ? 's' : ''} available</p>
            {recordings.map(rec => (
              <RecordingCard
                key={rec.recording_id}
                recording={rec}
                onDownload={handleDownload}
              />
            ))}
          </div>
        )}
      </div>
    </main>
  )
}
