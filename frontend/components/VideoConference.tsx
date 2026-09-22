'use client'

import { useEffect, useState, useCallback } from 'react'
import {
  LiveKitRoom,
  RoomAudioRenderer,
  ControlBar,
  ParticipantTile,
  useTracks,
  useParticipants,
  useRoomContext,
  useLocalParticipant,
} from '@livekit/components-react'
import '@livekit/components-styles'
import { Track, RoomEvent, Participant, Room } from 'livekit-client'
import { api, Participant as ApiParticipant, JoinRequest, MicrophoneRestriction } from '@/lib/api'

interface VideoConferenceProps {
  token: string
  serverUrl: string
  roomCode: string
  roomName: string
  isTeacher: boolean
  teacherAccessKey: string
}

export default function VideoConferenceComponent({
  token,
  serverUrl,
  roomCode,
  roomName,
  isTeacher,
  teacherAccessKey,
}: VideoConferenceProps) {
  const [error, setError] = useState<string | null>(null)

  return (
    <LiveKitRoom
      video={true}
      // Students always begin with their microphone off. Authorization to
      // publish remains server-side and the control bar enables it manually.
      audio={isTeacher}
      token={token}
      serverUrl={serverUrl}
      connect={true}
      onError={(error) => {
        console.error('LiveKit error:', error)
        setError('Failed to connect to the classroom. Please try again.')
      }}
      className="min-h-screen lg:h-screen flex flex-col"
    >
      <ClassroomContent
        roomCode={roomCode}
        roomName={roomName}
        isTeacher={isTeacher}
        teacherAccessKey={teacherAccessKey}
      />
    </LiveKitRoom>
  )
}

function ClassroomContent({
  roomCode,
  roomName,
  isTeacher,
  teacherAccessKey,
}: {
  roomCode: string
  roomName: string
  isTeacher: boolean
  teacherAccessKey: string
}) {
  const room = useRoomContext()
  const participants = useParticipants()
  const localParticipant = useLocalParticipant()
  const tracks = useTracks(
    [
      { source: Track.Source.Camera, withPlaceholder: true },
      { source: Track.Source.ScreenShare, withPlaceholder: false },
    ],
    { onlySubscribed: false }
  )

  const [participantsList, setParticipantsList] = useState<ApiParticipant[]>([])
  const [microphoneRestrictions, setMicrophoneRestrictions] = useState<Record<string, MicrophoneRestriction>>({})
  const [myMicrophoneRestriction, setMyMicrophoneRestriction] = useState<MicrophoneRestriction | null>(null)
  const [isLocked, setIsLocked] = useState(false)
  const [isEnded, setIsEnded] = useState(false)
  const [micPolicy, setMicPolicy] = useState<'allowed' | 'muted_by_default' | 'locked'>('allowed')
  const [cameraPolicy, setCameraPolicy] = useState<'allowed' | 'off_by_default' | 'locked'>('allowed')
  const [isRecording, setIsRecording] = useState(false)
  const [activeRecordingId, setActiveRecordingId] = useState<string | null>(null)

  const teacherIdentity = localParticipant.localParticipant?.identity || ''

  // Fetch participants from API
  const fetchParticipants = useCallback(async () => {
    try {
      const response = await api.getParticipants(roomCode)
      setParticipantsList(response.participants)
      setMicrophoneRestrictions(response.microphone_restrictions || {})
    } catch (err) {
      console.error('Failed to fetch participants:', err)
    }
  }, [roomCode])

  useEffect(() => {
    const identity = localParticipant.localParticipant?.identity
    if (isTeacher || !identity) return
    let active = true
    const fetchRestriction = async () => {
      try {
        const restriction = await api.getStudentMicrophoneRestriction(roomCode, identity)
        if (!active) return
        setMyMicrophoneRestriction(restriction)
        if (restriction.restricted) await localParticipant.localParticipant?.setMicrophoneEnabled(false)
      } catch {
        // Preserve normal controls during a temporary status-polling failure.
      }
    }
    fetchRestriction()
    const timer = window.setInterval(fetchRestriction, 2500)
    return () => { active = false; window.clearInterval(timer) }
  }, [isTeacher, localParticipant.localParticipant, roomCode])

  // Fetch class info
  const fetchClassInfo = useCallback(async () => {
    try {
      const info = await api.getClassInfo(roomCode)
      setIsLocked(info.is_locked)
      setIsEnded(info.is_ended)
      setMicPolicy(info.student_microphone_policy)
      setCameraPolicy(info.student_camera_policy)
    } catch (err) {
      console.error('Failed to fetch class info:', err)
    }
  }, [roomCode])

  // Handle class ended event
  useEffect(() => {
    if (isEnded) {
      alert('The class has ended.')
      window.location.href = '/'
    }
  }, [isEnded])

  // Listen for room disconnection
  useEffect(() => {
    const handleDisconnected = () => {
      console.log('Disconnected from room')
    }

    const handleParticipantDisconnected = (participant: Participant) => {
      console.log('Participant disconnected:', participant.identity)
      fetchParticipants()
    }

    const handleParticipantConnected = (participant: Participant) => {
      console.log('Participant connected:', participant.identity)
      fetchParticipants()
    }

    room.on(RoomEvent.Disconnected, handleDisconnected)
    room.on(RoomEvent.ParticipantDisconnected, handleParticipantDisconnected)
    room.on(RoomEvent.ParticipantConnected, handleParticipantConnected)

    return () => {
      room.off(RoomEvent.Disconnected, handleDisconnected)
      room.off(RoomEvent.ParticipantDisconnected, handleParticipantDisconnected)
      room.off(RoomEvent.ParticipantConnected, handleParticipantConnected)
    }
  }, [room, fetchParticipants])

  // Initial fetch
  useEffect(() => {
    fetchParticipants()
    fetchClassInfo()
    const interval = setInterval(fetchParticipants, 5000)
    return () => clearInterval(interval)
  }, [fetchParticipants, fetchClassInfo])

  const handleLeave = () => {
    room.disconnect()
    window.location.href = '/'
  }

  return (
    // Mobile/tablet: vertical stack (header → video → controls → sidebar), page scrolls.
    // Desktop (lg+): grid with header on top, video + sidebar in the middle, controls at the bottom.
    <div className="min-h-screen lg:h-screen flex flex-col lg:grid lg:grid-cols-[minmax(0,1fr)_20rem] lg:grid-rows-[auto_minmax(0,1fr)_auto] bg-gray-900 overflow-x-hidden lg:overflow-visible">
      {/* Header */}
      <div className="shrink-0 bg-gray-800 border-b border-gray-700 px-3 sm:px-4 py-2 sm:py-3 flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between sm:gap-4 lg:col-span-full lg:row-start-1">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 sm:gap-x-4 min-w-0">
          <h1 className="text-base sm:text-lg font-semibold text-white break-words min-w-0 max-w-full">{roomName}</h1>
          <span className="text-xs sm:text-sm text-gray-400 whitespace-nowrap">
            Participants: {participants.length}/50
          </span>
          {isLocked && (
            <span className="px-2 py-1 bg-yellow-900 text-yellow-200 text-xs rounded-full whitespace-nowrap">
              🔒 Class Locked
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-xs sm:text-sm text-gray-400">
            Room Code: <span className="font-mono text-white break-all">{roomCode}</span>
          </span>
        </div>
      </div>

      {/* The custom layout intentionally does not use GridLayout pagination.
          Its fixed-height mobile container can otherwise make later pages
          inaccessible. Every LiveKit track remains mounted in this scrollable grid. */}
      <div className="shrink-0 min-w-0 overflow-hidden p-2 sm:p-4 h-[50vh] min-h-[240px] sm:h-[55vh] lg:h-auto lg:min-h-0 lg:shrink lg:col-start-1 lg:row-start-2">
        <ParticipantVideoLayout tracks={tracks} />
      </div>

      {/* Control bar */}
      <div className="shrink-0 bg-gray-800 border-t border-gray-700 lg:col-span-full lg:row-start-3">
        {!isTeacher && myMicrophoneRestriction?.restricted && (
          <MicrophoneRestrictionNotice restriction={myMicrophoneRestriction} />
        )}
        <div className="[&_.lk-control-bar]:flex-wrap [&_.lk-control-bar]:justify-center">
          <ControlBar
            variation="verbose"
            controls={{
              camera: !isTeacher && cameraPolicy === 'locked' ? false : true,
              microphone: !isTeacher && (micPolicy === 'locked' || myMicrophoneRestriction?.restricted) ? false : true,
              screenShare: true,
              leave: true,
              chat: false,
            }}
          />
        </div>
        {!isTeacher && (
          <div className="text-center py-2 px-4">
            <button
              onClick={handleLeave}
              className="w-full sm:w-auto px-6 py-3 sm:py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors"
            >
              Leave Class
            </button>
          </div>
        )}
      </div>

      {/* Sidebar - Teacher controls or participant list
          Below the controls on mobile/tablet, beside the video on desktop */}
      <div className="flex-1 flex flex-col bg-gray-800 border-t border-gray-700 lg:border-t-0 lg:border-l lg:min-h-0 lg:overflow-hidden lg:col-start-2 lg:row-start-2">
        {isTeacher ? (
          <TeacherControls
            roomCode={roomCode}
            teacherIdentity={teacherIdentity}
            teacherAccessKey={teacherAccessKey}
            participants={participantsList}
            microphoneRestrictions={microphoneRestrictions}
            isLocked={isLocked}
            isRecording={isRecording}
            activeRecordingId={activeRecordingId}
            micPolicy={micPolicy}
            cameraPolicy={cameraPolicy}
            onLockChange={setIsLocked}
            onRecordingChange={(recording, recordingId) => {
              setIsRecording(recording)
              setActiveRecordingId(recordingId)
            }}
            onMicPolicyChange={setMicPolicy}
            onCameraPolicyChange={setCameraPolicy}
            onRefresh={fetchParticipants}
            onEndClass={() => setIsEnded(true)}
          />
        ) : (
          <ParticipantList
            participants={participantsList}
            studentIdentity={teacherIdentity}
          />
        )}
      </div>

      {/* Audio renderer */}
      <RoomAudioRenderer />
    </div>
  )
}

type VideoTrackReference = ReturnType<typeof useTracks>

function trackId(track: VideoTrackReference[number]) {
  return `${track.participant.identity}:${track.source}`
}

function ParticipantVideoLayout({ tracks }: { tracks: VideoTrackReference }) {
  const [pinnedTrackId, setPinnedTrackId] = useState<string | null>(null)
  const pinnedTrack = pinnedTrackId ? tracks.find((track) => trackId(track) === pinnedTrackId) : undefined
  const otherTracks = pinnedTrack ? tracks.filter((track) => trackId(track) !== pinnedTrackId) : tracks

  // A reconnect, departure, or unpublished camera/screen share removes that
  // track from useTracks. Clear the viewer-local pin instead of leaving an
  // empty focused area behind.
  useEffect(() => {
    if (pinnedTrackId && !pinnedTrack) setPinnedTrackId(null)
  }, [pinnedTrackId, pinnedTrack])

  const renderTile = (track: VideoTrackReference[number], prominent = false) => {
    const id = trackId(track)
    const isPinned = id === pinnedTrackId
    return (
      <div key={id} className={`relative min-w-0 aspect-video ${prominent ? 'h-full' : ''}`}>
        <ParticipantTile trackRef={track} className="h-full w-full rounded-lg overflow-hidden" />
        <button
          type="button"
          aria-label={isPinned ? `Unpin ${track.participant.name || track.participant.identity}` : `Pin ${track.participant.name || track.participant.identity}`}
          aria-pressed={isPinned}
          onClick={() => setPinnedTrackId(isPinned ? null : id)}
          className="absolute right-2 top-2 z-10 rounded-md bg-black/70 px-2 py-1 text-xs font-medium text-white shadow hover:bg-black/90 focus:outline-none focus:ring-2 focus:ring-blue-400"
        >
          {isPinned ? 'Unpin' : 'Pin'}
        </button>
      </div>
    )
  }

  if (pinnedTrack) {
    return (
      <div className="flex h-full min-h-0 flex-col gap-2">
        <div className="min-h-0 flex-1">{renderTile(pinnedTrack, true)}</div>
        {otherTracks.length > 0 && (
          <div className="max-h-[42%] shrink-0 overflow-y-auto overscroll-contain pr-1">
            <div className="grid grid-cols-1 min-[380px]:grid-cols-2 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-2">
              {otherTracks.map((track) => renderTile(track))}
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto overscroll-contain pr-1">
      <div className="grid grid-cols-1 min-[380px]:grid-cols-2 sm:grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-2">
        {tracks.map((track) => renderTile(track))}
      </div>
    </div>
  )
}

function TeacherControls({
  roomCode,
  teacherIdentity,
  teacherAccessKey,
  participants,
  microphoneRestrictions,
  isLocked,
  isRecording,
  activeRecordingId,
  micPolicy,
  cameraPolicy,
  onLockChange,
  onRecordingChange,
  onMicPolicyChange,
  onCameraPolicyChange,
  onRefresh,
  onEndClass,
}: {
  roomCode: string
  teacherIdentity: string
  teacherAccessKey: string
  participants: ApiParticipant[]
  microphoneRestrictions: Record<string, MicrophoneRestriction>
  isLocked: boolean
  isRecording: boolean
  activeRecordingId: string | null
  micPolicy: 'allowed' | 'muted_by_default' | 'locked'
  cameraPolicy: 'allowed' | 'off_by_default' | 'locked'
  onLockChange: (locked: boolean) => void
  onRecordingChange: (recording: boolean, recordingId: string | null) => void
  onMicPolicyChange: (policy: 'allowed' | 'muted_by_default' | 'locked') => void
  onCameraPolicyChange: (policy: 'allowed' | 'off_by_default' | 'locked') => void
  onRefresh: () => void
  onEndClass: () => void
}) {
  const students = participants.filter(p => p.role === 'student')

  const handleMuteAll = async () => {
    if (!confirm('Mute all students?')) return
    try {
      await api.muteAllStudents({
        room_code: roomCode,
        teacher_identity: teacherIdentity,
      })
      onRefresh()
    } catch (err: any) {
      alert(err.message || 'Failed to mute all students')
    }
  }

  const handleDisableAllCameras = async () => {
    if (!confirm('Disable all student cameras?')) return
    try {
      await api.disableAllCameras({
        room_code: roomCode,
        teacher_identity: teacherIdentity,
      })
      onRefresh()
    } catch (err: any) {
      alert(err.message || 'Failed to disable cameras')
    }
  }

  const handleLockClass = async () => {
    try {
      if (isLocked) {
        await api.unlockClass({
          room_code: roomCode,
          teacher_identity: teacherIdentity,
        })
        onLockChange(false)
      } else {
        await api.lockClass({
          room_code: roomCode,
          teacher_identity: teacherIdentity,
        })
        onLockChange(true)
      }
    } catch (err: any) {
      alert(err.message || 'Failed to lock/unlock class')
    }
  }

  const handleMicPolicyChange = async (policy: 'allowed' | 'muted_by_default' | 'locked') => {
    try {
      await api.setMicrophonePolicy({
        room_code: roomCode,
        teacher_identity: teacherIdentity,
        microphone_policy: policy,
      })
      onMicPolicyChange(policy)
    } catch (err: any) {
      alert(err.message || 'Failed to update microphone policy')
    }
  }

  const handleCameraPolicyChange = async (policy: 'allowed' | 'off_by_default' | 'locked') => {
    try {
      await api.setCameraPolicy({
        room_code: roomCode,
        teacher_identity: teacherIdentity,
        camera_policy: policy,
      })
      onCameraPolicyChange(policy)
    } catch (err: any) {
      alert(err.message || 'Failed to update camera policy')
    }
  }

  const [muteMenuFor, setMuteMenuFor] = useState<string | null>(null)

  const handleMuteStudent = async (studentIdentity: string, durationMinutes?: 1 | 5 | 10 | 15 | 30) => {
    try {
      await api.restrictStudentMicrophone({
        room_code: roomCode,
        teacher_identity: teacherIdentity,
        target_identity: studentIdentity,
        duration_minutes: durationMinutes,
      })
      setMuteMenuFor(null)
      onRefresh()
    } catch (err: any) {
      alert(err.message || 'Failed to mute student')
    }
  }

  const handleUnmuteStudent = async (studentIdentity: string) => {
    try {
      await api.unrestrictStudentMicrophone({ room_code: roomCode, teacher_identity: teacherIdentity, target_identity: studentIdentity })
      onRefresh()
    } catch (err: any) {
      alert(err.message || 'Failed to unmute student')
    }
  }

  const handleRemoveStudent = async (studentIdentity: string) => {
    if (!confirm('Remove this student from the class?')) return
    try {
      await api.removeParticipant({
        room_code: roomCode,
        teacher_identity: teacherIdentity,
        target_identity: studentIdentity,
      })
      onRefresh()
    } catch (err: any) {
      alert(err.message || 'Failed to remove student')
    }
  }

  const handleEndClass = async () => {
    if (!confirm('End this class for everyone? This cannot be undone.')) return
    try {
      // Stop recording if active
      if (isRecording && activeRecordingId) {
        await api.stopRecording({
          room_code: roomCode,
          recording_id: activeRecordingId,
          teacher_identity: teacherIdentity,
        })
      }
      
      await api.endClass({
        room_code: roomCode,
        teacher_identity: teacherIdentity,
      })
      onEndClass()
      window.location.href = '/'
    } catch (err: any) {
      alert(err.message || 'Failed to end class')
    }
  }

  const handleStartRecording = async () => {
    if (!confirm('Start recording this class?')) return
    try {
      const result = await api.startRecording({
        room_code: roomCode,
        teacher_identity: teacherIdentity,
      })
      onRecordingChange(true, result.recording_id)
      alert('Recording started successfully!')
    } catch (err: any) {
      alert(err.message || 'Failed to start recording')
    }
  }

  const handleStopRecording = async () => {
    if (!activeRecordingId) return
    if (!confirm('Stop recording? The recording will be saved and available for download.')) return
    try {
      const result = await api.stopRecording({
        room_code: roomCode,
        recording_id: activeRecordingId,
        teacher_identity: teacherIdentity,
      })
      onRecordingChange(false, null)
      alert('Recording stopped and saved successfully!')
    } catch (err: any) {
      alert(err.message || 'Failed to stop recording')
    }
  }

  return (
    <div className="flex-1 overflow-y-auto p-3 sm:p-4">
      <h2 className="text-lg font-semibold text-white mb-4">Teacher Controls</h2>

      {/* Quick actions */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 gap-2 mb-6">
        <button
          onClick={isRecording ? handleStopRecording : handleStartRecording}
          className={`w-full py-3 lg:py-2 ${isRecording ? 'bg-red-700 hover:bg-red-800 animate-pulse' : 'bg-red-600 hover:bg-red-700'} text-white rounded-lg transition-colors text-sm`}
        >
          {isRecording ? '⏹ Stop Recording' : '⏺ Start Recording'}
        </button>
        <button
          onClick={handleMuteAll}
          className="w-full py-3 lg:py-2 bg-orange-600 hover:bg-orange-700 text-white rounded-lg transition-colors text-sm"
        >
          🎤 Mute All
        </button>
        <button
          onClick={handleDisableAllCameras}
          className="w-full py-3 lg:py-2 bg-orange-600 hover:bg-orange-700 text-white rounded-lg transition-colors text-sm"
        >
          📹 Disable All Cameras
        </button>
        <button
          onClick={handleLockClass}
          className={`w-full py-3 lg:py-2 ${isLocked ? 'bg-green-600 hover:bg-green-700' : 'bg-yellow-600 hover:bg-yellow-700'} text-white rounded-lg transition-colors text-sm`}
        >
          {isLocked ? '🔓 Unlock Class' : '🔒 Lock Class'}
        </button>
      </div>

      {/* Policies */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 sm:gap-x-4">
        <div className="mb-6">
          <h3 className="text-sm font-medium text-gray-300 mb-2">🎤 Student Microphones</h3>
          <select
            value={micPolicy}
            onChange={(e) => handleMicPolicyChange(e.target.value as any)}
            className="w-full px-3 py-3 lg:py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-base lg:text-sm"
          >
            <option value="allowed">Allowed</option>
            <option value="muted_by_default">Muted by Default</option>
            <option value="locked">Locked</option>
          </select>
        </div>

        <div className="mb-6">
          <h3 className="text-sm font-medium text-gray-300 mb-2">📹 Student Cameras</h3>
          <select
            value={cameraPolicy}
            onChange={(e) => handleCameraPolicyChange(e.target.value as any)}
            className="w-full px-3 py-3 lg:py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-base lg:text-sm"
          >
            <option value="allowed">Allowed</option>
            <option value="off_by_default">Off by Default</option>
            <option value="locked">Locked</option>
          </select>
        </div>
      </div>

      {/* Participants */}
      <JoinRequests roomCode={roomCode} teacherIdentity={teacherIdentity} teacherAccessKey={teacherAccessKey} />

      <div className="mb-6">
        <h3 className="text-sm font-medium text-gray-300 mb-2">
          👥 Participants ({participants.length}/50)
        </h3>
        <div className="space-y-2 max-h-72 overflow-y-auto lg:max-h-none lg:overflow-visible">
          {participants.map((participant) => (
            <div
              key={participant.identity}
              className="p-3 bg-gray-700 rounded-lg"
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-white font-medium min-w-0 break-words">
                  {participant.name}
                  <span className="ml-2 text-xs text-gray-400">
                    {participant.role === 'teacher' ? '— Teacher' : '— Student'}
                  </span>
                </span>
              </div>
              {participant.role === 'student' && (
                <div className="mt-2">
                  {microphoneRestrictions[participant.identity]?.restricted && <MicrophoneRestrictionLabel restriction={microphoneRestrictions[participant.identity]} />}
                  <div className="flex gap-2 mt-2">
                  {microphoneRestrictions[participant.identity]?.restricted ? (
                    <button onClick={() => handleUnmuteStudent(participant.identity)} className="flex-1 sm:flex-none px-3 py-2 lg:px-2 lg:py-1 bg-green-600 hover:bg-green-700 text-white text-xs rounded transition-colors">Unmute</button>
                  ) : (
                    <div className="relative flex-1 sm:flex-none">
                      <button onClick={() => setMuteMenuFor(muteMenuFor === participant.identity ? null : participant.identity)} className="w-full px-3 py-2 lg:px-2 lg:py-1 bg-gray-600 hover:bg-gray-500 text-white text-xs rounded transition-colors">Mute</button>
                      {muteMenuFor === participant.identity && <MuteDurationMenu onSelect={(minutes) => handleMuteStudent(participant.identity, minutes)} />}
                    </div>
                  )}
                  <button
                    onClick={() => handleRemoveStudent(participant.identity)}
                    className="flex-1 sm:flex-none px-3 py-2 lg:px-2 lg:py-1 bg-red-600 hover:bg-red-700 text-white text-xs rounded transition-colors"
                  >
                    Remove
                  </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* End class */}
      <button
        onClick={handleEndClass}
        className="w-full py-3 bg-red-600 hover:bg-red-700 text-white font-semibold rounded-lg transition-colors"
      >
        🛑 End Class
      </button>
    </div>
  )
}

function MuteDurationMenu({ onSelect }: { onSelect: (minutes?: 1 | 5 | 10 | 15 | 30) => void }) {
  const options: Array<{ label: string; minutes?: 1 | 5 | 10 | 15 | 30 }> = [
    { label: 'Mute for 1 minute', minutes: 1 }, { label: 'Mute for 5 minutes', minutes: 5 },
    { label: 'Mute for 10 minutes', minutes: 10 }, { label: 'Mute for 15 minutes', minutes: 15 },
    { label: 'Mute for 30 minutes', minutes: 30 }, { label: 'Mute until teacher unmutes' },
  ]
  return <div className="absolute z-20 left-0 bottom-full mb-1 w-52 rounded-lg bg-gray-900 border border-gray-600 shadow-xl overflow-hidden">
    <div className="px-3 py-2 text-xs font-medium text-gray-300">Mute for</div>
    {options.map(option => <button key={option.label} onClick={() => onSelect(option.minutes)} className="block w-full px-3 py-2 text-left text-xs text-white hover:bg-gray-700">{option.label}</button>)}
  </div>
}

function MicrophoneRestrictionLabel({ restriction }: { restriction: MicrophoneRestriction }) {
  const [, setTick] = useState(0)
  useEffect(() => { const timer = window.setInterval(() => setTick(tick => tick + 1), 1000); return () => window.clearInterval(timer) }, [])
  if (restriction.mode === 'UNTIL_TEACHER' || !restriction.expires_at) return <p className="text-xs text-yellow-300">🔒 Muted until teacher unmutes</p>
  const remaining = remainingRestrictionSeconds(restriction)
  return <p className="text-xs text-yellow-300">🔇 Muted · {formatRemaining(remaining)} remaining</p>
}

function MicrophoneRestrictionNotice({ restriction }: { restriction: MicrophoneRestriction }) {
  const [, setTick] = useState(0)
  useEffect(() => { const timer = window.setInterval(() => setTick(tick => tick + 1), 1000); return () => window.clearInterval(timer) }, [])
  const untilTeacher = restriction.mode === 'UNTIL_TEACHER' || !restriction.expires_at
  const remaining = untilTeacher ? 0 : remainingRestrictionSeconds(restriction)
  return <div className="mx-3 mt-3 rounded-lg border border-yellow-700 bg-yellow-950 px-3 py-2 text-center text-sm text-yellow-100">
    <div>{untilTeacher ? '🔒 Your microphone has been muted by the teacher.' : '🔇 Your microphone has been muted by the teacher.'}</div>
    <div className="mt-1 text-xs text-yellow-200">{untilTeacher ? 'Your microphone will remain muted until the teacher unmutes you.' : `You can unmute after ${formatRemaining(remaining)}.`}</div>
  </div>
}

function remainingRestrictionSeconds(restriction: MicrophoneRestriction) {
  if (!restriction.expires_at) return 0
  // expires_at is an offset-aware ISO-8601 UTC value from the backend. Date
  // parses it as an absolute instant, avoiding local-timezone interpretation.
  return Math.max(0, Math.ceil((new Date(restriction.expires_at).getTime() - Date.now()) / 1000))
}

function formatRemaining(totalSeconds: number) {
  const minutes = Math.floor(totalSeconds / 60).toString().padStart(2, '0')
  const seconds = (totalSeconds % 60).toString().padStart(2, '0')
  return `${minutes}:${seconds}`
}

function JoinRequests({ roomCode, teacherIdentity, teacherAccessKey }: { roomCode: string; teacherIdentity: string; teacherAccessKey: string }) {
  const [requests, setRequests] = useState<JoinRequest[]>([])
  const [error, setError] = useState('')
  const [handling, setHandling] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!teacherIdentity) return
    try {
      const response = await api.getWaitingJoinRequests(roomCode, teacherIdentity)
      setRequests(response.requests)
      setError('')
    } catch {
      setError('Unable to load join requests. Retrying...')
    }
  }, [roomCode, teacherIdentity])

  useEffect(() => {
    refresh()
    const timer = window.setInterval(refresh, 2500)
    return () => window.clearInterval(timer)
  }, [refresh])

  const decide = async (requestId: string, allowed: boolean) => {
    setHandling(requestId)
    try {
      const data = { room_code: roomCode, teacher_identity: teacherIdentity, teacher_access_key: teacherAccessKey, request_id: requestId }
      if (allowed) await api.approveJoinRequest(data)
      else await api.rejectJoinRequest(data)
      await refresh()
    } catch {
      setError(`Unable to ${allowed ? 'approve' : 'reject'} this student. Please try again.`)
    } finally { setHandling(null) }
  }

  return (
    <div className="mb-6">
      <h3 className="text-sm font-medium text-gray-300 mb-2">🚪 Join Requests ({requests.length})</h3>
      {error && <p className="mb-2 text-xs text-amber-300">{error}</p>}
      {requests.length === 0 ? <p className="text-sm text-gray-400">No students are waiting.</p> : (
        <div className="space-y-2">
          {requests.map(request => <div key={request.request_id} className="p-3 bg-gray-700 rounded-lg">
            <div className="text-white font-medium break-words">{request.student_name}</div>
            <div className="text-xs text-gray-400 mb-2">Waiting…</div>
            <div className="flex gap-2"><button disabled={handling === request.request_id} onClick={() => decide(request.request_id, true)} className="flex-1 px-3 py-2 bg-green-600 hover:bg-green-700 disabled:bg-green-800 text-white text-xs rounded">Allow</button><button disabled={handling === request.request_id} onClick={() => decide(request.request_id, false)} className="flex-1 px-3 py-2 bg-red-600 hover:bg-red-700 disabled:bg-red-800 text-white text-xs rounded">Reject</button></div>
          </div>)}
        </div>
      )}
    </div>
  )
}

function ParticipantList({
  participants,
  studentIdentity,
}: {
  participants: ApiParticipant[]
  studentIdentity: string
}) {
  return (
    <div className="flex-1 overflow-y-auto p-3 sm:p-4">
      <h2 className="text-lg font-semibold text-white mb-4">
        Participants ({participants.length}/50)
      </h2>
      <div className="space-y-2 max-h-72 overflow-y-auto lg:max-h-none lg:overflow-visible">
        {participants.map((participant) => (
          <div
            key={participant.identity}
            className="p-3 bg-gray-700 rounded-lg"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-white font-medium min-w-0 break-words">
                {participant.name}
                <span className="ml-2 text-xs text-gray-400">
                  {participant.role === 'teacher' ? '— Teacher' : '— Student'}
                </span>
              </span>
              {participant.identity === studentIdentity && (
                <span className="text-xs text-blue-400 shrink-0">(You)</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
