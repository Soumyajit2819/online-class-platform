'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import Link from 'next/link'
import Hls from 'hls.js'
import { api, Recording, RecordingMeetingNotes } from '@/lib/api'
import PasscodeGate from '@/components/PasscodeGate'

// ---------------------------------------------------------------------------
// Countdown hook
// ---------------------------------------------------------------------------
function useCountdown(expiresAt: string) {
  const calc = () => {
    const diff = new Date(expiresAt).getTime() - Date.now()
    if (diff <= 0) return { expired: true, text: 'Expired', pct: 0, urgent: false }
    const totalMs = 20 * 60 * 60 * 1000
    const pct     = Math.max(0, Math.min(100, (diff / totalMs) * 100))
    const h = Math.floor(diff / 3600000)
    const m = Math.floor((diff % 3600000) / 60000)
    const s = Math.floor((diff % 60000) / 1000)
    const text = h > 0 ? `${h}h ${m}m left` : m > 0 ? `${m}m ${s}s left` : `${s}s left`
    return { expired: false, text, pct, urgent: diff < 2 * 3600000 }
  }
  const [state, setState] = useState(calc)
  useEffect(() => {
    setState(calc())
    const id = setInterval(() => { const n = calc(); setState(n); if (n.expired) clearInterval(id) }, 1000)
    return () => clearInterval(id)
  }, [expiresAt])
  return state
}

function notesFilename(className: string, language: 'english' | 'bengali') {
  const safeClassName = className
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, '-')
    .replace(/[. ]+$/g, '')
    .trim() || 'Class'
  const safeName = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(safeClassName)
    ? `_${safeClassName}`
    : safeClassName
  const languageLabel = language === 'english' ? 'English' : 'Bengali'
  return `${safeName} - ${languageLabel} Notes.txt`
}

// ---------------------------------------------------------------------------
// Single recording card
// ---------------------------------------------------------------------------
function RecordingCard({ recording, onDownload, downloading }: {
  recording: Recording
  onDownload: (recording: Recording) => void
  downloading: boolean
}) {
  const countdown = useCountdown(recording.expires_at)
  const videoRef = useRef<HTMLVideoElement>(null)

  useEffect(() => {
    const video = videoRef.current
    if (!video || !recording.playback_url) return
    const source = api.recordingPlaybackUrl(recording.playback_url)
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = source
      return () => { video.removeAttribute('src'); video.load() }
    }
    if (!Hls.isSupported()) return
    const hls = new Hls()
    hls.loadSource(source)
    hls.attachMedia(video)
    return () => hls.destroy()
  }, [recording.playback_url])

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
            <span className="capitalize">{recording.status === 'available' ? '▶ Ready to play' : recording.status === 'failed' ? '⚠ Recording failed' : '⏳ Finalizing recording'}</span>
          </div>
        </div>

      </div>

      {!countdown.expired && recording.playback_url && (
        <video ref={videoRef} controls playsInline className="mt-4 w-full rounded-lg bg-black aspect-video" />
      )}

      {!countdown.expired && recording.status === 'available' && (
        <div className="mt-4 flex justify-end">
          <button
            onClick={() => onDownload(recording)}
            disabled={downloading}
            className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-blue-300 text-white font-medium rounded-lg transition-colors text-sm"
          >
            {downloading ? 'Preparing download…' : 'Download MP4'}
          </button>
        </div>
      )}

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
  const [unlocked, setUnlocked] = useState(false)
  const handleUnauthorized = useCallback(() => {
    sessionStorage.removeItem('recordings_access_token')
    setUnlocked(false)
  }, [])

  if (!unlocked) {
    return (
      <PasscodeGate
        title="Recordings Access"
        description="Enter the passcode to view and download class recordings"
        icon="📹"
        onVerify={async (passcode) => {
          return api.verifyRecordingsPasscode(passcode)
        }}
        onSuccess={() => setUnlocked(true)}
      />
    )
  }

  return <RecordingsList onUnauthorized={handleUnauthorized} />
}

function RecordingsList({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [recordings, setRecordings]   = useState<Recording[]>([])
  const [meetings, setMeetings] = useState<RecordingMeetingNotes[]>([])
  const [loading, setLoading]         = useState(true)
  const [notesLoading, setNotesLoading] = useState(true)
  const [error, setError]             = useState('')
  const [notesError, setNotesError] = useState('')
  const [downloading, setDownloading] = useState<string | null>(null)
  const [downloadError, setDownloadError] = useState('')
  const [viewingNotes, setViewingNotes] = useState<string | null>(null)

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
      if (err.status === 401) onUnauthorized()
      setError(err.message || 'Failed to fetch recordings')
    } finally {
      setLoading(false)
    }
  }, [onUnauthorized])

  const fetchMeetingNotes = useCallback(async () => {
    setNotesLoading(true)
    setNotesError('')
    try {
      const result = await api.getRecordingsMeetingNotes()
      setMeetings(result.meetings)
    } catch (err: any) {
      if (err.status === 401) onUnauthorized()
      setNotesError(err.message || 'Failed to fetch Meeting Notes')
    } finally {
      setNotesLoading(false)
    }
  }, [onUnauthorized])

  const refreshAll = useCallback(() => {
    void fetchRecordings()
    void fetchMeetingNotes()
  }, [fetchRecordings, fetchMeetingNotes])

  useEffect(() => {
    refreshAll()
    // Refresh recordings and persistent notes together.
    const id = setInterval(refreshAll, 60000)
    return () => clearInterval(id)
  }, [refreshAll])

  const handleDownload = async (recording: Recording) => {
    if (downloading) return
    setDownloading(recording.recording_id)
    setDownloadError('')
    try {
      const { blob, filename } = await api.downloadRecording(recording.recording_id)
      const href = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = href
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(href)
    } catch (err: any) {
      setDownloadError(err.message || 'Download failed. Please try again.')
    } finally {
      setDownloading(null)
    }
  }

  const handleNotesDownload = async (meeting: RecordingMeetingNotes, language: 'english' | 'bengali') => {
    const key = `notes-${meeting.meeting_id}-${language}`
    setDownloading(key)
    setDownloadError('')
    try {
      // Revalidate the expiring recordings token for each notes download.
      const result = await api.getMeetingNotesDownload(meeting.meeting_id, language)
      const blob = new Blob([`\uFEFF${result.content}`], { type: 'text/plain;charset=utf-8' })
      const href = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = href
      link.download = notesFilename(result.class_name, language)
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(href), 1000)
    } catch (err: any) {
      if (err.status === 401) onUnauthorized()
      setDownloadError(err.message || 'Notes download failed. Please try again.')
    } finally {
      setDownloading(null)
    }
  }

  const formatMeetingDate = (date: string) => new Date(date).toLocaleString('en-GB', {
    day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit',
  })

  const recordingsById = new Map(recordings.map(recording => [recording.recording_id, recording]))
  const recordingsByMeeting = new Map(meetings.map(meeting => [
    meeting.meeting_id,
    meeting.recording_ids.map(id => recordingsById.get(id))
      .filter((recording): recording is Recording => Boolean(recording)),
  ]))
  const displayedRecordingIds = new Set(
    Array.from(recordingsByMeeting.values()).flat().map(recording => recording.recording_id)
  )

  return (
    <main className="min-h-screen bg-gradient-to-br from-slate-50 to-blue-50 p-8">
      <div className="max-w-4xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-3xl font-bold text-gray-900">Class Recordings</h1>
            <p className="text-gray-500 mt-1">
              Recordings are available for <span className="font-semibold text-blue-600">20 hours</span>
            </p>
          </div>
          <div className="flex gap-3">
            <button
              onClick={refreshAll}
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
              <li>• Available to play for <strong>20 hours</strong> only</li>
              <li>• Playback continues seamlessly across HLS segments</li>
              <li>• After 20 hours the file is permanently deleted from our servers</li>
            </ul>
          </div>
        </div>

        {downloadError && (
          <div className="mb-6 bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">
            {downloadError}
          </div>
        )}

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
          <div className="bg-white rounded-xl shadow-md p-8 text-center text-gray-500">
            No recordings are currently available. Recordings are listed here during their 20-hour availability window.
          </div>
        ) : null}

        <section className="mt-10">
          <h2 className="text-2xl font-bold text-gray-900 mb-4">Classes and Meeting Notes</h2>
          {notesError && (
            <div className="mb-4 bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">{notesError}</div>
          )}
          {notesLoading ? (
            <div className="bg-white rounded-xl shadow-md p-8 text-center text-gray-500">Loading Meeting Notes...</div>
          ) : meetings.length === 0 ? (
            <div className="bg-white rounded-xl shadow-md p-8 text-center text-gray-500">
              No class meetings or Meeting Notes are available yet.
            </div>
          ) : (
            <div className="space-y-5">
              {meetings.map(meeting => {
                const meetingRecordings = recordingsByMeeting.get(meeting.meeting_id) || []
                const notesReady = meeting.notes_status === 'ready'
                  && Boolean(meeting.english_notes?.trim())
                  && Boolean(meeting.bengali_notes?.trim())
                const isViewing = viewingNotes === meeting.meeting_id
                return (
                  <article key={meeting.meeting_id} className="bg-white rounded-xl shadow-md p-6">
                    <div className="border-b border-gray-100 pb-4 mb-4">
                      <h3 className="text-xl font-semibold text-gray-900">{meeting.class_name}</h3>
                      <p className="mt-1 text-sm text-gray-500">{formatMeetingDate(meeting.meeting_started_at)}</p>
                    </div>

                    <div className="grid gap-5 md:grid-cols-2">
                      <section aria-label="Recording">
                        <h4 className="font-semibold text-gray-800 mb-3">🎥 Recording</h4>
                        {meetingRecordings.length ? (
                          <div className="space-y-4">
                            {meetingRecordings.map(recording => (
                              <RecordingCard key={recording.recording_id} recording={recording}
                                onDownload={handleDownload} downloading={downloading === recording.recording_id} />
                            ))}
                          </div>
                        ) : (
                          <p className="text-sm text-gray-500 rounded-lg bg-gray-50 p-3">
                            Recording expired or unavailable. Meeting Notes remain available.
                          </p>
                        )}
                      </section>

                      <section aria-label="Meeting Notes">
                        <h4 className="font-semibold text-gray-800 mb-3">📝 Meeting Notes</h4>
                        {notesReady ? (
                          <div className="space-y-3">
                            <div className="flex flex-wrap gap-2">
                              <button onClick={() => handleNotesDownload(meeting, 'english')}
                                disabled={downloading === `notes-${meeting.meeting_id}-english`}
                                className="rounded-lg border border-blue-200 px-3 py-2 text-sm text-blue-700 hover:bg-blue-50">
                                {downloading === `notes-${meeting.meeting_id}-english` ? 'Preparing…' : `📄 ${meeting.class_name} - English Notes.txt`}
                              </button>
                              <button onClick={() => handleNotesDownload(meeting, 'bengali')}
                                disabled={downloading === `notes-${meeting.meeting_id}-bengali`}
                                className="rounded-lg border border-blue-200 px-3 py-2 text-sm text-blue-700 hover:bg-blue-50">
                                {downloading === `notes-${meeting.meeting_id}-bengali` ? 'Preparing…' : `📄 ${meeting.class_name} - Bengali Notes.txt`}
                              </button>
                            </div>
                            <button onClick={() => setViewingNotes(isViewing ? null : meeting.meeting_id)}
                              aria-expanded={isViewing}
                              className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700">
                              {isViewing ? 'Hide Notes' : 'View Notes'}
                            </button>
                            {isViewing && (
                              <div className="space-y-4">
                                <section>
                                  <h5 className="font-semibold text-gray-800">English</h5>
                                  <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-4 text-sm font-sans text-gray-800">{meeting.english_notes}</pre>
                                </section>
                                <section lang="bn">
                                  <h5 className="font-semibold text-gray-800">বাংলা</h5>
                                  <pre className="mt-2 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-4 text-sm font-sans text-gray-800 [font-family:Arial,'Noto_Sans_Bengali',sans-serif]">{meeting.bengali_notes}</pre>
                                </section>
                              </div>
                            )}
                          </div>
                        ) : (
                          <p className="text-sm text-gray-500 rounded-lg bg-gray-50 p-3">
                            {meeting.notes_status === 'pending' || meeting.notes_status === 'processing'
                              ? 'Meeting Notes are being prepared.'
                              : 'Meeting Notes are not available for this class yet.'}
                          </p>
                        )}
                      </section>
                    </div>
                  </article>
                )
              })}
            </div>
          )}
        </section>

        {recordings.length > 0 && recordings.some(recording => !displayedRecordingIds.has(recording.recording_id)) && (
          <section className="mt-10">
            <h2 className="text-xl font-bold text-gray-900 mb-4">Other Available Recordings</h2>
            <div className="space-y-4">
              {recordings.filter(recording => !displayedRecordingIds.has(recording.recording_id)).map(recording => (
                <RecordingCard key={recording.recording_id} recording={recording}
                  onDownload={handleDownload} downloading={downloading === recording.recording_id} />
              ))}
            </div>
          </section>
        )}
      </div>
    </main>
  )
}
