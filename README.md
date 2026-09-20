# Online Class Platform

LiveKit-based online classroom MVP for live video classes with teacher moderation controls.

## Features

### Core Features
- **Live Video Classes**: Real-time video and audio using LiveKit Cloud
- **Teacher Controls**: Create classes, set meeting passcodes, manage students
- **Student Access**: Join classes with room code and passcode
- **Maximum 50 Participants**: Configurable limit per class

### Teacher Moderation
- **Mute All Students**: One-click mute all student microphones
- **Disable All Cameras**: Turn off all student cameras
- **Remove Participants**: Remove disruptive students from class
- **Lock Class**: Prevent new students from joining
- **End Class**: End the session for everyone
- **Microphone Policy**: Allowed, Muted by Default, or Locked
- **Camera Policy**: Allowed, Off by Default, or Locked

### Security
- Server-side token generation (LIVEKIT_API_SECRET never exposed to frontend)
- Meeting passcode validation
- Teacher-only moderation endpoints (verified server-side)
- Temporary blocked participant list
- Session state management

## Prerequisites

- **Python 3.9+** - [Download](https://www.python.org/downloads/)
- **Node.js 18+** - [Download](https://nodejs.org/)
- **npm or yarn** - Comes with Node.js
- **LiveKit Cloud Account** - [Sign up](https://cloud.livekit.io/)

## Installation

### 1. Clone the Repository

```bash
cd online-class-platform
```

### 2. Backend Setup

```bash
cd backend

# Create virtual environment
python -m venv venv

# Activate virtual environment
# macOS/Linux:
source venv/bin/activate
# Windows:
# venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create .env file with your LiveKit credentials
# Copy .env.example to .env and fill in your values:
# LIVEKIT_URL=wss://your-project.livekit.cloud
# LIVEKIT_API_KEY=your_api_key
# LIVEKIT_API_SECRET=your_api_secret
# FRONTEND_URL=http://localhost:3000
```

**Important**: Never commit `.env` to version control. The LiveKit API secret must remain server-side only.

### 3. Frontend Setup

```bash
cd ../frontend

# Install dependencies
npm install

# The frontend .env.local is already configured to connect to localhost:8000
# NEXT_PUBLIC_API_URL=http://localhost:8000
```

## Running the Application

### Start the Backend

```bash
cd backend

# Activate virtual environment if not already active
source venv/bin/activate  # macOS/Linux

# Start FastAPI server
uvicorn app.main:app --reload --port 8000
```

Backend will run at: http://localhost:8000  
API docs available at: http://localhost:8000/docs

### Start the Frontend

```bash
cd frontend

# Start Next.js development server
npm run dev
```

Frontend will run at: http://localhost:3000

## How to Use

### Teacher Flow

1. Open http://localhost:3000
2. Click **"Teacher"**
3. Enter:
   - Teacher Name (e.g., "John")
   - Class Name (e.g., "DBMS - Normalization")
   - Meeting Passcode (e.g., "DBMS123")
   - Choose student microphone/camera policies (optional)
4. Click **"Create Class"**
5. You'll receive:
   - Room Code (e.g., "ABC123")
   - Meeting Passcode (shown/hidden toggle)
6. Click **"Enter Classroom"** to join
7. Share the **Room Code** and **Meeting Passcode** with students

### Student Flow

1. Open http://localhost:3000
2. Click **"Student"**
3. Enter:
   - Student Name (e.g., "Alice")
   - Room Code (from teacher)
   - Meeting Passcode (from teacher)
4. Click **"Join Class"**
5. Allow camera and microphone access when prompted

### Testing with Two Browser Windows

1. Open first browser window as **Teacher**
2. Create a class and note the Room Code and Passcode
3. Open second browser window (or incognito) as **Student**
4. Join with the Room Code and Passcode
5. Verify both can see and hear each other

### Teacher Moderation Controls

Once in the classroom, teachers can:

- **🎤 Mute All**: Mute all student microphones
- **📹 Disable All Cameras**: Turn off all student cameras
- **🔒 Lock Class**: Prevent new students from joining
- **🎤 Student Microphones Policy**:
  - Allowed: Students control their mic
  - Muted by Default: Students join muted, can unmute
  - Locked: Students cannot publish audio
- **📹 Student Cameras Policy**:
  - Allowed: Students control their camera
  - Off by Default: Students join with camera off, can enable
  - Locked: Students cannot publish video
- **Participants List**: View all participants with controls to:
  - Mute individual students
  - Remove students from class
- **🛑 End Class**: End session for everyone

## Environment Variables

### Backend (`backend/.env`)

```env
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret
FRONTEND_URL=http://localhost:3000
```

**Security Notes**:
- `LIVEKIT_API_SECRET` must NEVER be exposed to the frontend
- Never create `NEXT_PUBLIC_LIVEKIT_API_SECRET` in frontend
- Never commit actual secrets to version control
- Use `.env.example` files as templates only

### Frontend (`frontend/.env.local`)

```env
NEXT_PUBLIC_API_URL=http://localhost:8000
```

## Project Structure

```
online-class-platform/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI application and endpoints
│   │   ├── config.py            # Environment configuration
│   │   ├── livekit_service.py   # LiveKit integration and session state
│   │   └── models.py            # Pydantic models
│   ├── tests/
│   │   ├── __init__.py
│   │   └── test_main.py         # Backend tests
│   ├── requirements.txt
│   ├── .env                     # Your LiveKit credentials (not in git)
│   ├── .env.example             # Template file
│   └── .gitignore
│
├── frontend/
│   ├── app/
│   │   ├── layout.tsx           # Root layout
│   │   ├── page.tsx             # Landing page
│   │   ├── globals.css
│   │   ├── teacher/
│   │   │   └── page.tsx         # Teacher create class page
│   │   ├── student/
│   │   │   └── page.tsx         # Student join class page
│   │   └── class/
│   │       └── [roomCode]/
│   │           └── page.tsx     # Classroom page
│   ├── components/
│   │   └── VideoConference.tsx  # LiveKit video conference component
│   ├── lib/
│   │   └── api.ts               # API client
│   ├── package.json
│   ├── tsconfig.json
│   ├── tailwind.config.js
│   ├── next.config.js
│   ├── .env.local               # API URL (not in git)
│   ├── .env.example             # Template file
│   └── .gitignore
│
├── README.md
└── .gitignore
```

## API Endpoints

### Health Check
- `GET /api/health` - Health check endpoint

### Teacher Endpoints
- `POST /api/teacher/create-room` - Create a new class
- `POST /api/teacher/mute-all` - Mute all students
- `POST /api/teacher/disable-all-cameras` - Disable all student cameras
- `POST /api/teacher/remove-participant` - Remove a student
- `POST /api/teacher/mute-participant` - Mute a specific student
- `POST /api/teacher/lock-class` - Lock the class
- `POST /api/teacher/unlock-class` - Unlock the class
- `POST /api/teacher/set-microphone-policy` - Set microphone policy
- `POST /api/teacher/set-camera-policy` - Set camera policy
- `POST /api/teacher/end-class` - End the class
- `POST /api/teacher/unblock-participant` - Unblock a removed student

### Student Endpoints
- `POST /api/student/join-room` - Join a class

### Class Info Endpoints
- `GET /api/class/{room_code}` - Get class information
- `GET /api/class/{room_code}/participants` - Get participant list

## Running Tests

```bash
cd backend

# Activate virtual environment
source venv/bin/activate

# Run tests
pytest tests/ -v
```

## Current Limitations

This MVP intentionally does **not** include:

- Database persistence (session state is in-memory)
- User authentication system
- Recordings
- Recording downloads
- Recording deletion/retention
- Batch enrollment codes
- Payment integration
- Admin dashboard
- AI agents
- Telephony integration
- Mobile application
- Production deployment configuration

These features will be added in future iterations after the core LiveKit video MVP is validated.

## LiveKit Configuration

This project uses LiveKit Cloud. You need:

1. A LiveKit Cloud account
2. A LiveKit project
3. API Key and API Secret from your project dashboard

Get started at: https://cloud.livekit.io/

The 50-participant limit is enforced server-side and configured through LiveKit's room creation API.

## Security Architecture

```
Browser (Frontend)
    ↓
Next.js Application
    ↓
FastAPI Backend (localhost:8000)
    ↓
LiveKit Token Generation (server-side only)
    ↓
LiveKit Cloud (video infrastructure)
    ↓
Participants connect directly to LiveKit for media
```

**Key Security Points**:
- `LIVEKIT_API_SECRET` never leaves the backend
- Frontend receives only short-lived participant tokens
- Teacher moderation is verified server-side
- Meeting passcodes are hashed server-side
- Blocked participants cannot rejoin the same session

## Troubleshooting

### Backend won't start
- Check Python version: `python --version` (need 3.9+)
- Ensure virtual environment is activated
- Verify `.env` file exists with valid credentials
- Check LiveKit credentials are correct

### Frontend won't start
- Check Node.js version: `node --version` (need 18+)
- Try deleting `node_modules` and running `npm install` again

### Can't join class
- Verify Room Code is correct (case-insensitive)
- Check Meeting Passcode matches exactly
- Ensure class hasn't ended or been locked
- Check if class is full (50 participants)

### Audio/Video not working
- Allow browser permissions for camera/microphone
- Check if another application is using the camera
- Try a different browser (Chrome recommended)
- Verify LiveKit Cloud project is active

### Teacher controls not working
- Ensure you joined as teacher
- Check backend logs for errors
- Verify LiveKit API credentials have correct permissions

## License

MIT

## Contributing

This is an MVP project. Contributions welcome after core functionality is validated.
