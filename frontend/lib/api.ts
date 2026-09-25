const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
function recordingsAccessToken(): string | null {
  return typeof window === 'undefined'
    ? null
    : sessionStorage.getItem('recordings_access_token');
}
function examManagementAccessToken(): string | null {
  return typeof window === 'undefined'
    ? null
    : sessionStorage.getItem('exam_management_access_token');
}

export interface CreateRoomRequest {
  teacher_name: string;
  room_name: string;
  meeting_passcode?: string;
  max_participants?: number;
  student_microphone_policy?: 'allowed' | 'muted_by_default' | 'locked';
  student_camera_policy?: 'allowed' | 'off_by_default' | 'locked';
}

export interface JoinRoomRequest {
  student_name: string;
  room_code: string;
  meeting_passcode?: string;
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
  google_credential: string;
  meeting_passcode?: string;
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
  meeting_passcode_required?: boolean;
}

export interface ClassroomChatMessage {
  id: string;
  name: string;
  text: string;
  created_at: string;
}

export interface ClassroomChatAction {
  action: 'snapshot' | 'set_enabled' | 'send_message';
  teacher_identity?: string;
  teacher_access_key?: string;
  join_request_id?: string;
  session_id?: string;
  enabled?: boolean;
  text?: string;
}

export interface ClassroomChatState {
  enabled: boolean;
  messages: ClassroomChatMessage[];
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

export interface EndClassRequest {
  room_code: string;
  teacher_identity: string;
  teacher_access_key: string;
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

export interface RecordingMeetingNotes {
  meeting_id: string;
  room_code: string;
  class_name: string;
  meeting_started_at: string;
  ended_at: string | null;
  notes_status: 'pending' | 'processing' | 'ready' | 'failed' | 'unavailable' | string;
  english_notes: string | null;
  bengali_notes: string | null;
  error_code: string | null;
  updated_at: string | null;
  completed_at: string | null;
  recording_ids: string[];
}

export type ExamStatus = 'DRAFT' | 'SCHEDULED' | 'ACTIVE' | 'SUBMISSION_CLOSED' | 'EVALUATING' | 'MANUAL_REVIEW' | 'FINALIZED' | 'CANCELLED';
export type ExamQuestionType = 'MCQ' | 'TEXT_AUDIO_ANSWER';
export interface ExamQuestionOption { id?: string; option_order?: number; option_text?: string; text?: string; is_correct: boolean }
export interface ExamQuestion {
  id: string; question_number: number; question_text: string; question_type: ExamQuestionType;
  max_marks: number; is_required: boolean; evaluation_mode: 'AI_ALLOWED' | 'MANUAL_ONLY';
  options: ExamQuestionOption[]; reference_answer?: string | null; marking_criteria?: string | null;
}
export interface ManagedExam {
  id: string; title: string; description: string | null; instructions: string;
  custom_instructions_enabled: boolean; custom_instructions: string | null;
  scheduled_start_at: string; duration_minutes: number; status: ExamStatus;
  maximum_violations: number; protected_mode_enabled: boolean; require_fullscreen: boolean;
  detect_visibility_change: boolean; detect_orientation_change: boolean; restrict_copy_paste: boolean;
  autosave_enabled: boolean; automatic_submission_enabled: boolean; allow_list_enabled: boolean;
  created_at: string; updated_at: string; question_count?: number; questions?: ExamQuestion[];
  allowed_students?: Array<{ google_sub: string; display_name?: string; email?: string }>;
  public_token?: string;
}
export interface ManagedExamAttempt {
  attempt_id: string;
  student: { google_sub: string; display_name: string | null; email: string | null };
  status: string;
  started_at: string | null;
  expires_at: string | null;
  submitted_at: string | null;
  completion_seconds: number | null;
  violation_count: number;
  questions: Array<{
    id: string; question_number: number; question_text: string; question_type: ExamQuestionType;
    max_marks: number; is_required: boolean; evaluation_mode: 'AI_ALLOWED'|'MANUAL_ONLY';
    reference_answer: string | null; marking_criteria: string | null;
    answer_text: string | null; answer_method: string | null;
    selected_option: { id: string; option_order: number; option_text: string } | null;
    mcq_marks: number | null;
    saved_at: string | null;
    audio: { audio_mime_type: string; duration_ms: number | null; transcription_status: string;
      transcript: string | null; uploaded_at: string; transcribed_at: string | null } | null;
    manual_review: { id: string; marks: number; max_marks: number; teacher_comments: string | null;
      review_status: string; updated_at: string } | null;
    ai_evaluation: { id: string; provider: string; model: string; marks: number | null;
      max_marks: number; confidence: number | null; explanation: string | null; manual_required: boolean;
      status: string; last_error: string | null; updated_at: string } | null;
  }>;
}
export interface FinalExamResult {
  id: string; attempt_id: string; total_marks: number; maximum_marks: number; percentage: number;
  grade: string; rank: number; completion_time_seconds: number; finalized_at: string;
  student: { display_name: string | null; email: string | null; submitted_at: string | null };
}

export interface CreateManagedExamRequest {
  title: string;
  description: string | null;
  custom_instructions_enabled: boolean;
  custom_instructions: string | null;
  scheduled_start_at: string;
  duration_minutes: number;
  maximum_violations: number;
  protected_mode_enabled: boolean;
  require_fullscreen: boolean;
  detect_visibility_change: boolean;
  detect_orientation_change: boolean;
  restrict_copy_paste: boolean;
  autosave_enabled: boolean;
  automatic_submission_enabled: boolean;
  allow_list_enabled: boolean;
}
export interface StudentExamPreview {
  title: string; description: string | null; scheduled_start_at: string | null;
  duration_minutes: number; status: string; availability: string; message: string | null;
}
export interface StudentExamAccess extends StudentExamPreview {
  system_instructions: string; custom_instructions_enabled: boolean; custom_instructions: string | null;
  protected_mode_enabled: boolean; require_fullscreen: boolean; detect_visibility_change: boolean;
  detect_orientation_change: boolean; restrict_copy_paste: boolean; autosave_enabled: boolean;
  automatic_submission_enabled: boolean; maximum_violations: number;
  eligible: boolean; question_count: number; total_marks: number;
  audio_supported: boolean; existing_attempt: { status: string; expires_at: string | null; submitted_at: string | null } | null;
  student_display_name: string;
}
export interface StudentExamQuestion {
  id: string; question_number: number; question_text: string;
  question_type: 'MCQ' | 'TEXT_AUDIO_ANSWER'; max_marks: number;
  is_required: boolean; options: Array<{ id: string; option_order: number; option_text: string }>;
}
export interface StudentExamAnswer {
  question_id: string; answer_text: string | null; selected_option_id: string | null;
  answer_method: 'MCQ' | 'TEXT' | 'AUDIO' | 'BOTH'; answer_version: number; saved_at: string;
}
export interface StudentExamAudioAnswer {
  question_id: string; audio_mime_type: string; duration_ms: number | null;
  transcription_status: string; transcript: string | null;
}
export interface StudentExamAttemptPayload {
  attempt: { status: string; started_at: string; expires_at: string; submitted_at: string | null; violation_count?:number };
  server_time: string; remaining_seconds: number; questions: StudentExamQuestion[]; answers: StudentExamAnswer[];
  audio_answers: StudentExamAudioAnswer[];
}

class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = 'ApiError';
  }
}

async function fetchApi<T>(
  endpoint: string,
  options: RequestInit = {},
  showDetailedValidation = false
): Promise<T> {
  const url = `${API_URL}${endpoint}`;
  
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(endpoint.startsWith('/api/record') && recordingsAccessToken()
        ? { Authorization: `Bearer ${recordingsAccessToken()}` }
        : {}),
      ...options.headers,
    },
  });

  if (!response.ok) {
    let errorMessage = 'An error occurred';
    try {
      const errorData = await response.json();
      const detail = errorData.detail;
      if (typeof detail === 'string') errorMessage = detail;
      else if (showDetailedValidation && Array.isArray(detail)) {
        const messages = detail.map((item: { loc?: Array<string | number>; msg?: string; message?: string }) => {
          const field = item.loc?.filter(part => part !== 'body').join('.')
          const text = item.msg || item.message
          return text ? (field ? `${field}: ${text}` : text) : ''
        }).filter(Boolean)
        errorMessage = messages.join(' · ') || errorMessage
      } else if (detail && typeof detail === 'object') {
        const fieldMessage = showDetailedValidation && detail.field && detail.message ? `${detail.field}: ${detail.message}` : ''
        const messages = Array.isArray(detail.errors) ? detail.errors.map((item: { message?: string }) => item.message).filter(Boolean) : [];
        errorMessage = fieldMessage || messages.join(' · ') || detail.message || errorMessage;
      }
    } catch {
      errorMessage = response.statusText;
    }
    throw new ApiError(response.status, errorMessage);
  }

  return response.json();
}

async function examResponseError(response: Response): Promise<ApiError> {
  let text = response.statusText || 'The exam request could not be completed.'
  try {
    const payload = await response.json(); const detail = payload.detail
    if (typeof detail === 'string') text = detail
    else if (detail && typeof detail === 'object') text = detail.message || detail.field && detail.message && `${detail.field}: ${detail.message}` || text
  } catch { /* use the status text */ }
  return new ApiError(response.status, text)
}

function audioExtension(mime: string): string {
  return mime.includes('ogg') ? 'ogg' : mime.includes('mp4') ? 'm4a' : mime.includes('mpeg') ? 'mp3' : mime.includes('wav') ? 'wav' : 'webm'
}

async function fetchExamApi<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const token = examManagementAccessToken();
  if (!token) throw new ApiError(401, 'Enter the Exams management password again.');
  try {
    return await fetchApi<T>(endpoint, {
      ...options,
      headers: { ...options.headers, Authorization: `Bearer ${token}` },
    }, true);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      if (typeof window !== 'undefined') {
        sessionStorage.removeItem('exam_management_access_token');
        window.dispatchEvent(new Event('exam-management-unauthorized'));
      }
    }
    throw error;
  }
}

async function fetchStudentExamApi<T>(endpoint: string, googleCredential: string,
                                      options: RequestInit = {}, attemptToken?: string): Promise<T> {
  if (!googleCredential) throw new ApiError(401, 'Sign in with Google to continue.');
  return fetchApi<T>(endpoint, {
    ...options,
    headers: {
      ...options.headers,
      Authorization: `Bearer ${googleCredential}`,
      ...(attemptToken ? { 'X-Exam-Attempt-Token': attemptToken } : {}),
    },
  });
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

  async getStudentMicrophoneRestriction(roomCode: string, studentIdentity: string, requestId: string, sessionId: string): Promise<MicrophoneRestriction> {
    const query = `request_id=${encodeURIComponent(requestId)}&session_id=${encodeURIComponent(sessionId)}`;
    return fetchApi(`/api/class/${encodeURIComponent(roomCode)}/microphone-restriction/${encodeURIComponent(studentIdentity)}?${query}`);
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

  async endClass(data: EndClassRequest): Promise<{ success: boolean; message: string }> {
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

  async getRecordingsMeetingNotes(): Promise<{ meetings: RecordingMeetingNotes[]; total: number }> {
    return fetchApi('/api/recordings/meeting-notes');
  },

  async getMeetingNotesDownload(meetingId: string, language: 'english' | 'bengali'): Promise<{ class_name: string; content: string }> {
    return fetchApi(`/api/recordings/meeting-notes/${encodeURIComponent(meetingId)}/download/${language}`);
  },

  async downloadRecording(recordingId: string): Promise<{ blob: Blob; filename: string }> {
    const token = recordingsAccessToken()
    const response = await fetch(`${API_URL}/api/recordings/${encodeURIComponent(recordingId)}/download`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!response.ok) {
      let message = 'Download failed'
      try { message = (await response.json()).detail || message } catch { message = response.statusText || message }
      throw new ApiError(response.status, message)
    }
    const disposition = response.headers.get('Content-Disposition') || ''
    const match = disposition.match(/filename="?([^";]+)"?/i)
    return { blob: await response.blob(), filename: match?.[1] || `class-recording-${recordingId}.mp4` }
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

  async getInviteInfo(inviteCode: string): Promise<{ room_code: string; class_name: string; teacher_name: string; meeting_passcode_required: boolean }> {
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
    return result;
  },

  async verifyExamManagementPassword(password: string): Promise<{ token: string; scope: 'exam_management'; expires_in: number }> {
    const result = await fetchApi<{ token: string; scope: 'exam_management'; expires_in: number }>('/api/exams/auth', {
      method: 'POST',
      body: JSON.stringify({ password }),
    });
    if (typeof window !== 'undefined') {
      sessionStorage.setItem('exam_management_access_token', result.token);
    }
    return result;
  },

  getExamManagementAuthorization(): string | null {
    const token = examManagementAccessToken();
    return token ? `Bearer ${token}` : null;
  },

  async getManagedExams(): Promise<{ exams: ManagedExam[] }> {
    return fetchExamApi('/api/exams');
  },
  async createManagedExam(data: CreateManagedExamRequest): Promise<ManagedExam> {
    return fetchExamApi('/api/exams', { method: 'POST', body: JSON.stringify(data) });
  },
  async getManagedExam(id: string): Promise<ManagedExam> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}`);
  },
  async getManagedExamAttempts(id: string): Promise<{ exam_status: ExamStatus; attempts: ManagedExamAttempt[] }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/attempts`);
  },
  async finalizeManagedExam(id: string): Promise<{ status: 'FINALIZED'; results: FinalExamResult[]; artifacts: { grade_card_count: number; rank_card_ready: boolean } | null; artifacts_error: string | null }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/finalize`, { method: 'POST' });
  },
  async getManagedExamResults(id: string): Promise<{ results: FinalExamResult[] }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/results`);
  },
  async sendManagedExamGradeCards(id: string): Promise<{ status: string; provider_configured: boolean; message: string; jobs: Array<{ id: string }> }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/notifications/grade-cards`, { method: 'POST' });
  },
  async sendManagedExamRankCard(id: string): Promise<{ status: string; provider_configured: boolean; message: string; jobs: Array<{ id: string }> }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/notifications/rank-card`, { method: 'POST' });
  },
  async getManagedExamNotificationStatus(id: string): Promise<{ jobs: Array<{ id: string; job_type: string; status: string; attempts: number; last_error: string | null; created_at: string; completed_at: string | null }>; provider_configured: boolean }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/notifications`);
  },
  async getManagedExamRankCard(id: string, download = false): Promise<Blob> {
    const token = examManagementAccessToken();
    if (!token) throw new ApiError(401, 'Enter the Exams management password again.');
    const response = await fetch(`${API_URL}/api/exams/${encodeURIComponent(id)}/artifacts/rank-card?download=${download}`, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) { const error = await examResponseError(response); if (error.status === 401 || error.status === 403) { sessionStorage.removeItem('exam_management_access_token'); window.dispatchEvent(new Event('exam-management-unauthorized')); } throw error; }
    return response.blob();
  },
  async getManagedExamGradeCard(id: string, resultId: string): Promise<Blob> {
    const token = examManagementAccessToken();
    if (!token) throw new ApiError(401, 'Enter the Exams management password again.');
    const response = await fetch(`${API_URL}/api/exams/${encodeURIComponent(id)}/artifacts/grade-cards/${encodeURIComponent(resultId)}`, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) { const error = await examResponseError(response); if (error.status === 401 || error.status === 403) { sessionStorage.removeItem('exam_management_access_token'); window.dispatchEvent(new Event('exam-management-unauthorized')); } throw error; }
    return response.blob();
  },
  async downloadManagedExamGradeCards(id: string): Promise<Blob> {
    const token = examManagementAccessToken();
    if (!token) throw new ApiError(401, 'Enter the Exams management password again.');
    const response = await fetch(`${API_URL}/api/exams/${encodeURIComponent(id)}/artifacts/grade-cards.zip`, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) { const error = await examResponseError(response); if (error.status === 401 || error.status === 403) { sessionStorage.removeItem('exam_management_access_token'); window.dispatchEvent(new Event('exam-management-unauthorized')); } throw error; }
    return response.blob();
  },
  async refreshManagedExamArtifacts(id: string): Promise<{ grade_card_count: number; rank_card_ready: boolean }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/artifacts/refresh`, { method: 'POST' });
  },
  async saveManagedExamEvaluation(examId: string, attemptId: string, questionId: string, data: { marks: number; teacher_feedback: string | null }): Promise<{ manual_review: NonNullable<ManagedExamAttempt['questions'][number]['manual_review']> }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(examId)}/attempts/${encodeURIComponent(attemptId)}/questions/${encodeURIComponent(questionId)}/evaluation`, { method:'PUT', body:JSON.stringify(data) });
  },
  async getManagedExamAudio(examId: string, attemptId: string, questionId: string): Promise<Blob> {
    const token=examManagementAccessToken();
    if(!token)throw new ApiError(401,'Enter the Exams management password again.');
    const response=await fetch(`${API_URL}/api/exams/${encodeURIComponent(examId)}/attempts/${encodeURIComponent(attemptId)}/questions/${encodeURIComponent(questionId)}/audio`,{headers:{Authorization:`Bearer ${token}`}});
    if(!response.ok){const error=await examResponseError(response);if(error.status===401||error.status===403){sessionStorage.removeItem('exam_management_access_token');window.dispatchEvent(new Event('exam-management-unauthorized'))}throw error}
    return response.blob();
  },
  async updateManagedExam(id: string, data: Record<string, unknown>): Promise<ManagedExam> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(data) });
  },
  async deleteManagedExam(id: string): Promise<{ deleted: boolean; storage_cleanup_complete?: boolean; storage_cleanup_pending?: number }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}`, { method: 'DELETE' });
  },
  async cancelManagedExam(id: string): Promise<{ status: ExamStatus }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
  },
  async addManagedQuestion(id: string, data: Record<string, unknown>): Promise<ExamQuestion> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/questions`, { method: 'POST', body: JSON.stringify(data) });
  },
  async updateManagedQuestion(id: string, questionId: string, data: Record<string, unknown>): Promise<ExamQuestion> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/questions/${encodeURIComponent(questionId)}`, { method: 'PATCH', body: JSON.stringify(data) });
  },
  async deleteManagedQuestion(id: string, questionId: string): Promise<{ deleted: boolean }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/questions/${encodeURIComponent(questionId)}`, { method: 'DELETE' });
  },
  async reorderManagedQuestions(id: string, questionIds: string[]): Promise<{ question_ids: string[] }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/questions/reorder`, { method: 'POST', body: JSON.stringify({ question_ids: questionIds }) });
  },
  async getAllowedStudents(id: string): Promise<{ students: Array<{ google_sub: string; display_name?: string; email?: string }> }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/allowed-students`);
  },
  async addAllowedStudent(id: string, data: { google_sub: string; display_name?: string; email?: string }): Promise<unknown> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/allowed-students`, { method: 'POST', body: JSON.stringify(data) });
  },
  async removeAllowedStudent(id: string, sub: string): Promise<{ deleted: boolean }> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/allowed-students/${encodeURIComponent(sub)}`, { method: 'DELETE' });
  },
  async scheduleManagedExam(id: string): Promise<ManagedExam> {
    return fetchExamApi(`/api/exams/${encodeURIComponent(id)}/schedule`, { method: 'POST' });
  },

  async getStudentExamPreview(examToken: string): Promise<StudentExamPreview> {
    return fetchApi(`/api/exams/student/${encodeURIComponent(examToken)}`);
  },
  async getStudentExamAccess(examToken: string, googleCredential: string): Promise<StudentExamAccess> {
    return fetchStudentExamApi(`/api/exams/student/${encodeURIComponent(examToken)}/access`, googleCredential, { method: 'POST' });
  },
  async startOrResumeStudentExam(examToken: string, googleCredential: string): Promise<{ attempt: StudentExamAttemptPayload['attempt']; attempt_token: string; resumed: boolean; remaining_seconds: number }> {
    return fetchStudentExamApi(`/api/exams/student/${encodeURIComponent(examToken)}/attempt`, googleCredential, { method: 'POST' });
  },
  async getStudentExamAttempt(examToken: string, googleCredential: string, attemptToken: string): Promise<StudentExamAttemptPayload> {
    return fetchStudentExamApi(`/api/exams/student/${encodeURIComponent(examToken)}/attempt`, googleCredential, {}, attemptToken);
  },
  async saveStudentExamAnswer(examToken: string, googleCredential: string, attemptToken: string, data: { question_id: string; answer_text?: string | null; selected_option_id?: string | null; answer_method?: 'MCQ'|'TEXT'|'AUDIO'|'BOTH' }): Promise<unknown> {
    return fetchStudentExamApi(`/api/exams/student/${encodeURIComponent(examToken)}/attempt/answers`, googleCredential, { method: 'PUT', body: JSON.stringify(data) }, attemptToken);
  },
  async uploadStudentExamAudio(examToken: string, googleCredential: string, attemptToken: string, questionId: string, audio: Blob, durationMs: number, answerMethod: 'AUDIO'|'BOTH'): Promise<{ question_id:string; answer_method:'AUDIO'|'BOTH'; transcript:string; transcription_status:string; duration_ms:number|null }> {
    const token = googleCredential;
    if (!token) throw new ApiError(401, 'Sign in with Google to continue.');
    const form = new FormData(); form.append('file', audio, `answer.${audioExtension(audio.type)}`);
    form.append('duration_ms', String(durationMs)); form.append('answer_method', answerMethod);
    const response = await fetch(`${API_URL}/api/exams/student/${encodeURIComponent(examToken)}/attempt/questions/${encodeURIComponent(questionId)}/audio`, {
      method:'PUT', body:form, headers:{Authorization:`Bearer ${token}`,'X-Exam-Attempt-Token':attemptToken},
    });
    if (!response.ok) throw await examResponseError(response);
    return response.json();
  },
  async getStudentExamAudio(examToken:string, googleCredential:string, attemptToken:string, questionId:string):Promise<Blob> {
    const response=await fetch(`${API_URL}/api/exams/student/${encodeURIComponent(examToken)}/attempt/questions/${encodeURIComponent(questionId)}/audio`, {
      headers:{Authorization:`Bearer ${googleCredential}`,'X-Exam-Attempt-Token':attemptToken},
    });
    if (!response.ok) throw await examResponseError(response);
    return response.blob();
  },
  async deleteStudentExamAudio(examToken:string, googleCredential:string, attemptToken:string, questionId:string):Promise<{deleted:boolean}> {
    return fetchStudentExamApi(`/api/exams/student/${encodeURIComponent(examToken)}/attempt/audio`, googleCredential, {method:'DELETE',body:JSON.stringify({question_id:questionId})}, attemptToken);
  },
  async recordStudentExamViolation(examToken:string, googleCredential:string, attemptToken:string, violationType:string):Promise<{status:string;violation_count:number;maximum_violations:number;terminated:boolean}> {
    return fetchStudentExamApi(`/api/exams/student/${encodeURIComponent(examToken)}/attempt/violations`, googleCredential, {method:'POST',body:JSON.stringify({violation_type:violationType})}, attemptToken);
  },
  async heartbeatStudentExam(examToken:string, googleCredential:string, attemptToken:string):Promise<{status:string;remaining_seconds:number;violation_count:number}> {
    return fetchStudentExamApi(`/api/exams/student/${encodeURIComponent(examToken)}/attempt/heartbeat`, googleCredential, {method:'POST'}, attemptToken);
  },
  async submitStudentExam(examToken: string, googleCredential: string, attemptToken: string): Promise<{ attempt: StudentExamAttemptPayload['attempt']; status: string; submitted_at: string }> {
    return fetchStudentExamApi(`/api/exams/student/${encodeURIComponent(examToken)}/attempt/submit`, googleCredential, { method: 'POST' }, attemptToken);
  },

  clearExamManagementSession(): void {
    if (typeof window !== 'undefined') sessionStorage.removeItem('exam_management_access_token');
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

  async updateExamManagementPassword(adminPassword: string, newPassword: string): Promise<{ success: boolean; message: string }> {
    return fetchApi('/api/admin/update-exam-management-password', {
      method: 'POST',
      body: JSON.stringify({ admin_password: adminPassword, new_passcode: newPassword }),
    });
  },

  async getPasscodeStatus(): Promise<{ teacher_passcode_set: boolean; recordings_passcode_set: boolean; exam_management_password_set: boolean; admin_password_set: boolean }> {
    return fetchApi('/api/admin/passcode-status');
  },

  async getExamWhatsAppNumber(adminPassword: string): Promise<{ number: string }> {
    return fetchApi('/api/admin/exam-whatsapp-number', { method: 'POST', body: JSON.stringify({ password: adminPassword }) });
  },

  async updateExamWhatsAppNumber(adminPassword: string, number: string): Promise<{ number: string }> {
    return fetchApi('/api/admin/exam-whatsapp-number', { method: 'PUT', body: JSON.stringify({ admin_password: adminPassword, number }) });
  },

  // Class info endpoints
  async getClassInfo(roomCode: string): Promise<ClassInfo> {
    return fetchApi(`/api/class/${roomCode}`);
  },

  async classroomChat(roomCode: string, data: ClassroomChatAction): Promise<ClassroomChatState> {
    return fetchApi(`/api/class/${encodeURIComponent(roomCode)}/chat`, {
      method: 'POST', body: JSON.stringify(data),
    });
  },

  async getParticipants(roomCode: string): Promise<ParticipantsResponse> {
    return fetchApi(`/api/class/${roomCode}/participants`);
  },
};

export { ApiError };
