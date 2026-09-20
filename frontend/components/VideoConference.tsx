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
import { api, Participant as ApiParticipant } from '@/lib/api'

interface VideoConferenceProps {
  token: string
  serverUrl: string
  roomCode: string
  roomName: string
  isTeacher: boolean
}

export default function VideoConferenceComponent({
  token,
  serverUrl,
  roomCode,
  roomName,
  isTeacher,
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
      className="h-screen flex flex-col"
    >
      <ClassroomContent
        roomCode={roomCode}
        roomName={roomName}
        isTeacher={isTeacher}
      />
    </LiveKitRoom>
  )
}

function ClassroomContent({
  roomCode,
  roomName,
  isTeacher,
}: {
  roomCode: string
  roomName: string
  isTeacher: boolean
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
    <div className="h-screen flex flex-col bg-gray-900">
      {/* Header */}
      <div className="bg-gray-800 border-b border-gray-700 px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <h1 className="text-lg font-semibold text-white">{roomName}</h1>
          <span className="text-sm text-gray-400">
            Participants: {participants.length}/50
          </span>
          {isLocked && (
            <span className="px-2 py-1 bg-yellow-900 text-yellow-200 text-xs rounded-full">
              🔒 Class Locked
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-sm text-gray-400">
            Room Code: <span className="font-mono text-white">{roomCode}</span>
          </span>
        </div>
      </div>

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Video grid */}
        <div className="flex-1 p-4">
          <GridLayout tracks={tracks} className="h-full">
            <ParticipantTile className="rounded-lg overflow-hidden" />
          </GridLayout>
        </div>

        {/* Sidebar - Teacher controls or participant list */}
        <div className="w-80 bg-gray-800 border-l border-gray-700 flex flex-col">
          {isTeacher ? (
            <TeacherControls
              roomCode={roomCode}
              teacherIdentity={teacherIdentity}
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
      </div>

      {/* Control bar */}
      <div className="bg-gray-800 border-t border-gray-700">
        <ControlBar
          variation="verbose"
          controls={{
            camera: !isTeacher && cameraPolicy === 'locked' ? false : true,
            microphone: !isTeacher && micPolicy === 'locked' ? false : true,
            screenShare: false,
            leave: true,
            chat: false,
          }}
        />
        {!isTeacher && (
          <div className="text-center py-2">
            <button
              onClick={handleLeave}
              className="px-6 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors"
            >
              Leave Class
            </button>
          </div>
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
    <div className="flex-1 overflow-y-auto p-4">
      <h2 className="text-lg font-semibold text-white mb-4">Teacher Controls</h2>

      {/* Quick actions */}
      <div className="space-y-2 mb-6">
        <button
          onClick={isRecording ? handleStopRecording : handleStartRecording}
          className={`w-full py-2 ${isRecording ? 'bg-red-700 hover:bg-red-800 animate-pulse' : 'bg-red-600 hover:bg-red-700'} text-white rounded-lg transition-colors text-sm`}
        >
          {isRecording ? '⏹ Stop Recording' : '⏺ Start Recording'}
        </button>
        <button
          onClick={handleMuteAll}
          className="w-full py-2 bg-orange-600 hover:bg-orange-700 text-white rounded-lg transition-colors text-sm"
        >
          🎤 Mute All
        </button>
        <button
          onClick={handleDisableAllCameras}
          className="w-full py-2 bg-orange-600 hover:bg-orange-700 text-white rounded-lg transition-colors text-sm"
        >
          📹 Disable All Cameras
        </button>
        <button
          onClick={handleLockClass}
          className={`w-full py-2 ${isLocked ? 'bg-green-600 hover:bg-green-700' : 'bg-yellow-600 hover:bg-yellow-700'} text-white rounded-lg transition-colors text-sm`}
        >
          {isLocked ? '🔓 Unlock Class' : '🔒 Lock Class'}
        </button>
      </div>

      {/* Policies */}
      <div className="mb-6">
        <h3 className="text-sm font-medium text-gray-300 mb-2">🎤 Student Microphones</h3>
        <select
          value={micPolicy}
          onChange={(e) => handleMicPolicyChange(e.target.value as any)}
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm"
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
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm"
        >
          <option value="allowed">Allowed</option>
          <option value="off_by_default">Off by Default</option>
          <option value="locked">Locked</option>
        </select>
      </div>

      {/* Participants */}
      <div className="mb-6">
        <h3 className="text-sm font-medium text-gray-300 mb-2">
          👥 Participants ({participants.length}/50)
        </h3>
        <div className="space-y-2">
          {participants.map((participant) => (
            <div
              key={participant.identity}
              className="p-3 bg-gray-700 rounded-lg"
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-white font-medium">
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
                    className="px-2 py-1 bg-gray-600 hover:bg-gray-500 text-white text-xs rounded transition-colors"
                  >
                    Mute
                  </button>
                  <button
                    onClick={() => handleRemoveStudent(participant.identity)}
                    className="px-2 py-1 bg-red-600 hover:bg-red-700 text-white text-xs rounded transition-colors"
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

function ParticipantList({
  participants,
  studentIdentity,
}: {
  participants: ApiParticipant[]
  studentIdentity: string
}) {
  return (
    <div className="flex-1 overflow-y-auto p-4">
      <h2 className="text-lg font-semibold text-white mb-4">
        Participants ({participants.length}/50)
      </h2>
      <div className="space-y-2">
        {participants.map((participant) => (
          <div
            key={participant.identity}
            className="p-3 bg-gray-700 rounded-lg"
          >
            <div className="flex items-center justify-between">
              <span className="text-white font-medium">
                {participant.name}
                <span className="ml-2 text-xs text-gray-400">
                  {participant.role === 'teacher' ? '— Teacher' : '— Student'}
                </span>
              </span>
              {participant.identity === studentIdentity && (
                <span className="text-xs text-blue-400">(You)</span>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
