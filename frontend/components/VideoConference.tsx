'use client'

import { useEffect, useState, useCallback } from 'react'
import {
  LiveKitRoom,
  VideoConference,
  RoomAudioRenderer,
  ControlBar,
  GridLayout,
  ParticipantTile,
  useTracks,
  useParticipants,
  useRoomContext,
  useLocalParticipant,
} from '@livekit/components-react'
import '@livekit/components-styles'
import { Track, RoomEvent, Participant, Room } from 'livekit-client'
import { api, Participant as ApiParticipant, JoinRequest } from '@/lib/api'

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
      audio={true}
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
    } catch (err) {
      console.error('Failed to fetch participants:', err)
    }
  }, [roomCode])

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

      {/* Video grid */}
      <div className="shrink-0 min-w-0 overflow-hidden p-2 sm:p-4 h-[50vh] min-h-[240px] sm:h-[55vh] lg:h-auto lg:min-h-0 lg:shrink lg:col-start-1 lg:row-start-2">
        <GridLayout tracks={tracks} className="h-full">
          <ParticipantTile className="rounded-lg overflow-hidden" />
        </GridLayout>
      </div>

      {/* Control bar */}
      <div className="shrink-0 bg-gray-800 border-t border-gray-700 lg:col-span-full lg:row-start-3">
        <div className="[&_.lk-control-bar]:flex-wrap [&_.lk-control-bar]:justify-center">
          <ControlBar
            variation="verbose"
            controls={{
              camera: !isTeacher && cameraPolicy === 'locked' ? false : true,
              microphone: !isTeacher && micPolicy === 'locked' ? false : true,
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

function TeacherControls({
  roomCode,
  teacherIdentity,
  teacherAccessKey,
  participants,
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

  const handleMuteStudent = async (studentIdentity: string) => {
    try {
      await api.muteParticipant({
        room_code: roomCode,
        teacher_identity: teacherIdentity,
        target_identity: studentIdentity,
      })
      onRefresh()
    } catch (err: any) {
      alert(err.message || 'Failed to mute student')
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
                <div className="flex gap-2 mt-2">
                  <button
                    onClick={() => handleMuteStudent(participant.identity)}
                    className="flex-1 sm:flex-none px-3 py-2 lg:px-2 lg:py-1 bg-gray-600 hover:bg-gray-500 text-white text-xs rounded transition-colors"
                  >
                    Mute
                  </button>
                  <button
                    onClick={() => handleRemoveStudent(participant.identity)}
                    className="flex-1 sm:flex-none px-3 py-2 lg:px-2 lg:py-1 bg-red-600 hover:bg-red-700 text-white text-xs rounded transition-colors"
                  >
                    Remove
                  </button>
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
