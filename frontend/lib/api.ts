const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

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
  count: number;
  max_participants: number;
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

  // Student endpoints
  async joinRoom(data: JoinRoomRequest): Promise<RoomResponse> {
    return fetchApi('/api/student/join-room', {
      method: 'POST',
      body: JSON.stringify(data),
    });
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
