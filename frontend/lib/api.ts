const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
let recordingsAccessToken: string | null = null;

export interface CreateRoomRequest {
  teacher_name: string;
  room_name: string;
  meeting_passcode: string;
  max_participants?: number;
  student_microphone_policy?: 'allowed' | 'muted_by_default' | 'locked';
  student_camera_policy?: 'allowed' | 'off_by_default' | 'locked';
}

export interface JoinRoomRequest {
  student_name: string;
  room_code: string;
  meeting_passcode: string;
}

export interface RoomResponse {
  room_code: string;
  room_name: string;
  token: string;
  livekit_url: string;
  invite_code?: string;
  teacher_access_key?: string;
}

export interface JoinRequest {
  request_id: string;
  room_code: string;
  student_name: string;
  status: 'WAITING' | 'APPROVED' | 'REJECTED' | 'CANCELLED';
  created_at: string;
  updated_at: string;
  decided_at: string | null;
}

export interface CreateJoinRequestData {
  student_name: string;
  meeting_passcode: string;
  room_code?: string;
  invite_code?: string;
  session_id: string;
}

export interface ClassInfo {
  room_code: string;
  class_name: string;
  teacher_name: string;
  is_locked: boolean;
  is_ended: boolean;
  max_participants: number;
  student_microphone_policy: 'allowed' | 'muted_by_default' | 'locked';
  student_camera_policy: 'allowed' | 'off_by_default' | 'locked';
}

export interface Participant {
  identity: string;
  name: string;
  role: 'teacher' | 'student';
  state: string;
  metadata: string;
}

export interface ParticipantsResponse {
  room_code: string;
  participants: Participant[];
  microphone_restrictions: Record<string, MicrophoneRestriction>;
  count: number;
  max_participants: number;
}

export interface MicrophoneRestriction {
  restricted: boolean;
  mode?: 'TIMED' | 'UNTIL_TEACHER';
  expires_at?: string | null;
  updated_at?: string;
  server_time: string;
  remaining_seconds?: number | null;
}

export interface ModerationRequest {
  room_code: string;
  teacher_identity: string;
  target_identity?: string;
}

export interface SetPolicyRequest {
  room_code: string;
  teacher_identity: string;
  microphone_policy?: 'allowed' | 'muted_by_default' | 'locked';
  camera_policy?: 'allowed' | 'off_by_default' | 'locked';
}

export interface StartRecordingRequest {
  room_code: string;
  teacher_identity: string;
}

export interface StopRecordingRequest {
  room_code: string;
  recording_id: string;
  teacher_identity: string;
}

export interface Recording {
  recording_id: string;
  egress_id: string;
  room_code: string;
  class_name: string;
  teacher_name: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  expires_at: string;
  hours_left: number;
  playback_url: string | null;
}

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

async function fetchApi<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${API_URL}${endpoint}`;
  
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(endpoint.startsWith('/api/record') && recordingsAccessToken
        ? { Authorization: `Bearer ${recordingsAccessToken}` }
        : {}),
      ...options.headers,
    },
  });

  if (!response.ok) {
    let errorMessage = 'An error occurred';
    try {
      const errorData = await response.json();
      errorMessage = errorData.detail || errorMessage;
    } catch {
      errorMessage = response.statusText;
    }
    throw new ApiError(response.status, errorMessage);
  }

  return response.json();
}

export const api = {
  // Health check
  async healthCheck(): Promise<{ status: string }> {
    return fetchApi('/api/health');
  },

  // Teacher endpoints
  async createRoom(data: CreateRoomRequest): Promise<RoomResponse> {
    return fetchApi('/api/teacher/create-room', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async muteAllStudents(data: ModerationRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/mute-all', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async disableAllCameras(data: ModerationRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/disable-all-cameras', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async removeParticipant(data: ModerationRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/remove-participant', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async muteParticipant(data: ModerationRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/mute-participant', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async restrictStudentMicrophone(data: ModerationRequest & { duration_minutes?: 1 | 5 | 10 | 15 | 30 }): Promise<MicrophoneRestriction> {
    return fetchApi('/api/teacher/restrict-student-microphone', { method: 'POST', body: JSON.stringify(data) });
  },

  async unrestrictStudentMicrophone(data: ModerationRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/unrestrict-student-microphone', { method: 'POST', body: JSON.stringify(data) });
  },

  async getStudentMicrophoneRestriction(roomCode: string, studentIdentity: string): Promise<MicrophoneRestriction> {
    return fetchApi(`/api/class/${encodeURIComponent(roomCode)}/microphone-restriction/${encodeURIComponent(studentIdentity)}`);
  },

  async setMicrophonePolicy(data: SetPolicyRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/set-microphone-policy', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async setCameraPolicy(data: SetPolicyRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/set-camera-policy', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async lockClass(data: ModerationRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/lock-class', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async unlockClass(data: ModerationRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/unlock-class', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async endClass(data: ModerationRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/end-class', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async unblockParticipant(data: ModerationRequest): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/teacher/unblock-participant', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  // Recording endpoints
  async startRecording(data: StartRecordingRequest): Promise<{ success: boolean; recording_id: string; egress_id: string; status: string }> {
    return fetchApi('/api/teacher/start-recording', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async stopRecording(data: StopRecordingRequest): Promise<{ success: boolean; recording_id: string; status: string; download_url: string | null }> {
    return fetchApi('/api/teacher/stop-recording', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async getClassRecordings(roomCode: string): Promise<{ room_code: string; class_name: string; recordings: Recording[] }> {
    return fetchApi(`/api/class/${roomCode}/recordings`);
  },

  async getAllRecordings(): Promise<{ recordings: Recording[]; total: number }> {
    return fetchApi('/api/recordings');
  },

  async getRecordingStatus(recordingId: string): Promise<{ success: boolean; recording: Recording }> {
    return fetchApi(`/api/recording/${recordingId}`);
  },

  // Student endpoints
  async joinRoom(data: JoinRoomRequest): Promise<RoomResponse> {
    return fetchApi('/api/student/join-room', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async getInviteInfo(inviteCode: string): Promise<{ room_code: string; class_name: string; teacher_name: string }> {
    return fetchApi(`/api/join/${encodeURIComponent(inviteCode)}`);
  },

  async createJoinRequest(data: CreateJoinRequestData): Promise<JoinRequest> {
    return fetchApi('/api/student/join-requests', { method: 'POST', body: JSON.stringify(data) });
  },

  async getJoinRequest(requestId: string, sessionId: string): Promise<JoinRequest> {
    return fetchApi(`/api/student/join-requests/${encodeURIComponent(requestId)}?session_id=${encodeURIComponent(sessionId)}`);
  },

  async getApprovedJoinToken(requestId: string, sessionId: string): Promise<RoomResponse> {
    return fetchApi(`/api/student/join-requests/${encodeURIComponent(requestId)}/token`, {
      method: 'POST', body: JSON.stringify({ session_id: sessionId }),
    });
  },

  async getWaitingJoinRequests(roomCode: string, teacherIdentity: string): Promise<{ requests: JoinRequest[] }> {
    return fetchApi(`/api/teacher/${encodeURIComponent(roomCode)}/join-requests?teacher_identity=${encodeURIComponent(teacherIdentity)}`);
  },

  async approveJoinRequest(data: { room_code: string; teacher_identity: string; teacher_access_key: string; request_id: string }): Promise<JoinRequest> {
    return fetchApi('/api/teacher/approve-join-request', { method: 'POST', body: JSON.stringify(data) });
  },

  async rejectJoinRequest(data: { room_code: string; teacher_identity: string; teacher_access_key: string; request_id: string }): Promise<JoinRequest> {
    return fetchApi('/api/teacher/reject-join-request', { method: 'POST', body: JSON.stringify(data) });
  },

  // Passcode verification
  async verifyTeacherPasscode(passcode: string): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/auth/verify-teacher-passcode', {
      method: 'POST',
      body: JSON.stringify({ passcode }),
    });
  },

  async verifyRecordingsPasscode(passcode: string): Promise<{ success: boolean; message: string; access_token: string }> {
    const result = await fetchApi<{ success: boolean; message: string; access_token: string }>('/api/auth/verify-recordings-passcode', {
      method: 'POST',
      body: JSON.stringify({ passcode }),
    });
    recordingsAccessToken = result.access_token;
    return result;
  },

  recordingPlaybackUrl(path: string): string {
    return path.startsWith('http') ? path : `${API_URL}${path}`;
  },

  // Admin endpoints
  async adminLogin(password: string): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/admin/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    });
  },

  async updateTeacherPasscode(adminPassword: string, newPasscode: string): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/admin/update-teacher-passcode', {
      method: 'POST',
      body: JSON.stringify({ admin_password: adminPassword, new_passcode: newPasscode }),
    });
  },

  async updateRecordingsPasscode(adminPassword: string, newPasscode: string): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/admin/update-recordings-passcode', {
      method: 'POST',
      body: JSON.stringify({ admin_password: adminPassword, new_passcode: newPasscode }),
    });
  },

  async getPasscodeStatus(): Promise<{ teacher_passcode_set: boolean; recordings_passcode_set: boolean; admin_password_set: boolean }> {
    return fetchApi('/api/admin/passcode-status');
  },

  // Class info endpoints
  async getClassInfo(roomCode: string): Promise<ClassInfo> {
    return fetchApi(`/api/class/${roomCode}`);
  },

  async getParticipants(roomCode: string): Promise<ParticipantsResponse> {
    return fetchApi(`/api/class/${roomCode}/participants`);
  },
};

export { ApiError };
