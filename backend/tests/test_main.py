import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch, AsyncMock
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app
from app.livekit_service import session_state, livekit_service
from app.models import MicrophonePolicy, CameraPolicy


client = TestClient(app)


class TestHealthEndpoint:
    """Tests for the health check endpoint."""
    
    def test_health_check_returns_ok(self):
        """Test that health check returns status ok."""
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestCreateRoom:
    """Tests for teacher room creation endpoint."""
    
    def test_create_room_missing_teacher_name(self):
        """Test that creating room without teacher name fails."""
        response = client.post("/api/teacher/create-room", json={
            "room_name": "Test Class",
            "meeting_passcode": "test123"
        })
        assert response.status_code == 422
    
    def test_create_room_missing_class_name(self):
        """Test that creating room without class name fails."""
        response = client.post("/api/teacher/create-room", json={
            "teacher_name": "John",
            "meeting_passcode": "test123"
        })
        assert response.status_code == 422
    
    def test_create_room_missing_passcode(self):
        """Test that creating room without passcode fails."""
        response = client.post("/api/teacher/create-room", json={
            "teacher_name": "John",
            "room_name": "Test Class"
        })
        assert response.status_code == 422
    
    def test_create_room_empty_teacher_name(self):
        """Test that empty teacher name is rejected."""
        response = client.post("/api/teacher/create-room", json={
            "teacher_name": "",
            "room_name": "Test Class",
            "meeting_passcode": "test123"
        })
        assert response.status_code == 400
    
    def test_create_room_invalid_max_participants(self):
        """Test that invalid max participants is rejected."""
        response = client.post("/api/teacher/create-room", json={
            "teacher_name": "John",
            "room_name": "Test Class",
            "meeting_passcode": "test123",
            "max_participants": 100
        })
        assert response.status_code == 400
    
    @patch('app.main.livekit_service.create_room', new_callable=AsyncMock)
    def test_create_room_success(self, mock_create_room):
        """Test successful room creation."""
        mock_create_room.return_value = True
        
        response = client.post("/api/teacher/create-room", json={
            "teacher_name": "John",
            "room_name": "Test Class",
            "meeting_passcode": "test123"
        })
        
        assert response.status_code == 200
        data = response.json()
        assert "room_code" in data
        assert "token" in data
        assert "livekit_url" in data
        assert data["room_name"] == "Test Class"


class TestJoinRoom:
    """Tests for student room joining endpoint."""
    
    def test_join_room_missing_student_name(self):
        """Test that joining without student name fails."""
        response = client.post("/api/student/join-room", json={
            "room_code": "TEST123",
            "meeting_passcode": "test123"
        })
        assert response.status_code == 422
    
    def test_join_room_missing_room_code(self):
        """Test that joining without room code fails."""
        response = client.post("/api/student/join-room", json={
            "student_name": "Alice",
            "meeting_passcode": "test123"
        })
        assert response.status_code == 422
    
    def test_join_room_missing_passcode(self):
        """Test that joining without passcode fails."""
        response = client.post("/api/student/join-room", json={
            "student_name": "Alice",
            "room_code": "TEST123"
        })
        assert response.status_code == 422
    
    def test_join_room_nonexistent_class(self):
        """Test that joining non-existent class fails."""
        response = client.post("/api/student/join-room", json={
            "student_name": "Alice",
            "room_code": "NONEXISTENT",
            "meeting_passcode": "test123"
        })
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
    
    @patch('app.main.livekit_service.create_room', new_callable=AsyncMock)
    @patch('app.main.livekit_service.get_participants', new_callable=AsyncMock)
    def test_join_room_success(self, mock_get_participants, mock_create_room):
        """Test successful room joining."""
        mock_create_room.return_value = True
        mock_get_participants.return_value = []
        
        # First create a room
        create_response = client.post("/api/teacher/create-room", json={
            "teacher_name": "John",
            "room_name": "Test Class",
            "meeting_passcode": "test123"
        })
        room_code = create_response.json()["room_code"]
        
        # Then join it
        join_response = client.post("/api/student/join-room", json={
            "student_name": "Alice",
            "room_code": room_code,
            "meeting_passcode": "test123"
        })
        
        assert join_response.status_code == 200
        data = join_response.json()
        assert "token" in data
        assert "livekit_url" in data
    
    @patch('app.main.livekit_service.create_room', new_callable=AsyncMock)
    def test_join_room_wrong_passcode(self, mock_create_room):
        """Test that wrong passcode is rejected."""
        mock_create_room.return_value = True
        
        # Create a room
        create_response = client.post("/api/teacher/create-room", json={
            "teacher_name": "John",
            "room_name": "Test Class",
            "meeting_passcode": "correct123"
        })
        room_code = create_response.json()["room_code"]
        
        # Try to join with wrong passcode
        join_response = client.post("/api/student/join-room", json={
            "student_name": "Alice",
            "room_code": room_code,
            "meeting_passcode": "wrong123"
        })
        
        assert join_response.status_code == 403


class TestModerationEndpoints:
    """Tests for teacher moderation endpoints."""
    
    @patch('app.main.livekit_service.create_room', new_callable=AsyncMock)
    @patch('app.main.livekit_service.mute_all_students', new_callable=AsyncMock)
    @patch('app.main.livekit_service.get_participants', new_callable=AsyncMock)
    def test_mute_all_students_as_teacher(self, mock_get_participants, mock_mute_all, mock_create_room):
        """Test that teacher can mute all students."""
        mock_create_room.return_value = True
        mock_get_participants.return_value = []
        mock_mute_all.return_value = None
        
        # Create a room
        create_response = client.post("/api/teacher/create-room", json={
            "teacher_name": "John",
            "room_name": "Test Class",
            "meeting_passcode": "test123"
        })
        room_code = create_response.json()["room_code"]
        teacher_identity = session_state.get_teacher_identity(room_code)
        
        # Mute all students
        mute_response = client.post("/api/teacher/mute-all", json={
            "room_code": room_code,
            "teacher_identity": teacher_identity
        })
        
        assert mute_response.status_code == 200
    
    @patch('app.main.livekit_service.create_room', new_callable=AsyncMock)
    def test_non_teacher_cannot_mute_all(self, mock_create_room):
        """Test that non-teachers cannot mute all students."""
        mock_create_room.return_value = True
        
        # Create a room
        create_response = client.post("/api/teacher/create-room", json={
            "teacher_name": "John",
            "room_name": "Test Class",
            "meeting_passcode": "test123"
        })
        room_code = create_response.json()["room_code"]
        
        # Try to mute all as non-teacher
        mute_response = client.post("/api/teacher/mute-all", json={
            "room_code": room_code,
            "teacher_identity": "fake_teacher_identity"
        })
        
        assert mute_response.status_code == 403


class TestParticipantLimit:
    """Tests for participant limit configuration."""
    
    @patch('app.main.livekit_service.create_room', new_callable=AsyncMock)
    def test_default_max_participants_is_50(self, mock_create_room):
        """Test that default max participants is 50."""
        mock_create_room.return_value = True
        
        response = client.post("/api/teacher/create-room", json={
            "teacher_name": "John",
            "room_name": "Test Class",
            "meeting_passcode": "test123"
        })
        
        room_code = response.json()["room_code"]
        class_info = session_state.get_class(room_code)
        
        assert class_info["max_participants"] == 50
    
    @patch('app.main.livekit_service.create_room', new_callable=AsyncMock)
    def test_can_set_lower_max_participants(self, mock_create_room):
        """Test that max participants can be set lower than 50."""
        mock_create_room.return_value = True
        
        response = client.post("/api/teacher/create-room", json={
            "teacher_name": "John",
            "room_name": "Test Class",
            "meeting_passcode": "test123",
            "max_participants": 20
        })
        
        room_code = response.json()["room_code"]
        class_info = session_state.get_class(room_code)
        
        assert class_info["max_participants"] == 20


class TestTokenGeneration:
    """Tests for LiveKit token generation."""
    
    def test_generate_room_code(self):
        """Test that room code is generated."""
        code = livekit_service.generate_room_code()
        assert len(code) > 0
        assert isinstance(code, str)
    
    def test_generate_livekit_room_name(self):
        """Test that LiveKit room name is generated."""
        name = livekit_service.generate_livekit_room_name()
        assert name.startswith("class_")
        assert len(name) > 6
    
    def test_hash_passcode(self):
        """Test that passcode is hashed correctly."""
        hash1 = livekit_service.hash_passcode("test123")
        hash2 = livekit_service.hash_passcode("test123")
        hash3 = livekit_service.hash_passcode("test456")
        
        # Same passcode should produce same hash
        assert hash1 == hash2
        # Different passcode should produce different hash
        assert hash1 != hash3
        # Hash should be hex string
        assert all(c in '0123456789abcdef' for c in hash1)
