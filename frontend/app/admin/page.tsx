'use client'

import { useState, useRef, useEffect } from 'react'
import Link from 'next/link'
import { api } from '@/lib/api'

// ---------------------------------------------------------------------------
// Admin login gate
// ---------------------------------------------------------------------------
function AdminLogin({ onLogin }: { onLogin: (pw: string) => void }) {
  const [password, setPassword] = useState('')
  const [loading, setLoading]   = useState(false)
  const [error, setError]       = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!password.trim()) { setError('Password required'); return }
    setLoading(true); setError('')
    try {
      await api.adminLogin(password)
      onLogin(password)
    } catch (err: any) {
      setError(err.message || 'Invalid password')
      setPassword('')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950 p-8">
      <div className="w-full max-w-sm bg-gray-900 rounded-2xl shadow-2xl p-8 border border-gray-700">
        <div className="text-center mb-6">
          <div className="text-5xl mb-3">🛡️</div>
          <h1 className="text-2xl font-bold text-white">Admin Dashboard</h1>
          <p className="text-gray-400 text-sm mt-1">Restricted access</p>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
          <input
            ref={inputRef}
            type="password"
            value={password}
            onChange={e => setPassword(e.target.value)}
            // All auto-fill disabled
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            data-form-type="other"
            data-lpignore="true"
            data-1p-ignore="true"
            className="w-full px-4 py-3 bg-gray-800 border border-gray-600 rounded-lg text-white placeholder-gray-500 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            placeholder="Admin password"
          />
          {error && (
            <div className="p-3 bg-red-900/50 border border-red-700 rounded-lg text-red-300 text-sm">
              ⚠️ {error}
            </div>
          )}
          <button type="submit" disabled={loading || !password.trim()}
            className="w-full py-3 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white font-semibold rounded-lg transition-colors">
            {loading ? 'Verifying...' : 'Login'}
          </button>
        </form>
        <div className="mt-4 text-center">
          <Link href="/" className="text-gray-500 hover:text-gray-300 text-sm">← Back to Home</Link>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Passcode update form
// ---------------------------------------------------------------------------
function UpdatePasscodeCard({
  title,
  icon,
  description,
  adminPassword,
  onUpdate,
}: {
  title: string
  icon: string
  description: string
  adminPassword: string
  onUpdate: (adminPw: string, newPasscode: string) => Promise<void>
}) {
  const [newPasscode, setNewPasscode]     = useState('')
  const [confirmPasscode, setConfirm]     = useState('')
  const [loading, setLoading]             = useState(false)
  const [success, setSuccess]             = useState('')
  const [error, setError]                 = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(''); setSuccess('')

    if (!newPasscode.trim()) { setError('Passcode required'); return }
    if (newPasscode.length < 6) { setError('Passcode must be at least 6 characters'); return }
    if (newPasscode !== confirmPasscode) { setError('Passcodes do not match'); return }

    setLoading(true)
    try {
      await onUpdate(adminPassword, newPasscode)
      setSuccess('Passcode updated successfully!')
      setNewPasscode('')
      setConfirm('')
    } catch (err: any) {
      setError(err.message || 'Failed to update passcode')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="bg-gray-800 rounded-xl p-6 border border-gray-700">
      <div className="flex items-center gap-3 mb-4">
        <span className="text-3xl">{icon}</span>
        <div>
          <h3 className="text-lg font-semibold text-white">{title}</h3>
          <p className="text-gray-400 text-sm">{description}</p>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-3">
        <div>
          <label className="block text-sm text-gray-400 mb-1">New Passcode</label>
          <input
            type="password"
            value={newPasscode}
            onChange={e => setNewPasscode(e.target.value)}
            autoComplete="new-password"
            autoCorrect="off"
            data-form-type="other"
            data-lpignore="true"
            data-1p-ignore="true"
            className="w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white placeholder-gray-500 focus:ring-2 focus:ring-blue-500 text-sm"
            placeholder="Min 6 characters"
          />
        </div>
        <div>
          <label className="block text-sm text-gray-400 mb-1">Confirm New Passcode</label>
          <input
            type="password"
            value={confirmPasscode}
            onChange={e => setConfirm(e.target.value)}
            autoComplete="new-password"
            autoCorrect="off"
            data-form-type="other"
            data-lpignore="true"
            data-1p-ignore="true"
            className="w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white placeholder-gray-500 focus:ring-2 focus:ring-blue-500 text-sm"
            placeholder="Confirm passcode"
          />
        </div>

        {error   && <p className="text-red-400 text-sm">⚠️ {error}</p>}
        {success && <p className="text-green-400 text-sm">✅ {success}</p>}

        <button type="submit" disabled={loading}
          className="w-full py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 text-white font-medium rounded-lg transition-colors text-sm">
          {loading ? 'Updating...' : 'Update Passcode'}
        </button>
      </form>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Admin dashboard
// ---------------------------------------------------------------------------
function AdminDashboard({ adminPassword }: { adminPassword: string }) {
  const [status, setStatus] = useState<{
    teacher_passcode_set: boolean
    recordings_passcode_set: boolean
    admin_password_set: boolean
  } | null>(null)

  useEffect(() => {
    api.getPasscodeStatus().then(setStatus).catch(() => {})
  }, [])

  const handleUpdateTeacher = async (adminPw: string, newPasscode: string) => {
    await api.updateTeacherPasscode(adminPw, newPasscode)
    const s = await api.getPasscodeStatus(); setStatus(s)
  }

  const handleUpdateRecordings = async (adminPw: string, newPasscode: string) => {
    await api.updateRecordingsPasscode(adminPw, newPasscode)
    const s = await api.getPasscodeStatus(); setStatus(s)
  }

  return (
    <div className="min-h-screen bg-gray-950 p-8">
      <div className="max-w-2xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-3xl font-bold text-white">Admin Dashboard</h1>
            <p className="text-gray-400 mt-1">Manage access passcodes</p>
          </div>
          <Link href="/"
            className="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg text-sm border border-gray-700 transition-colors">
            ← Home
          </Link>
        </div>

        {/* Status cards */}
        {status && (
          <div className="grid grid-cols-3 gap-4 mb-8">
            {[
              { label: 'Teacher Passcode', set: status.teacher_passcode_set },
              { label: 'Recordings Passcode', set: status.recordings_passcode_set },
              { label: 'Admin Password', set: status.admin_password_set },
            ].map(item => (
              <div key={item.label} className="bg-gray-800 rounded-xl p-4 border border-gray-700 text-center">
                <div className={`text-2xl mb-1 ${item.set ? 'text-green-400' : 'text-red-400'}`}>
                  {item.set ? '✅' : '⚠️'}
                </div>
                <p className="text-gray-300 text-sm font-medium">{item.label}</p>
                <p className={`text-xs mt-1 ${item.set ? 'text-green-500' : 'text-red-500'}`}>
                  {item.set ? 'Configured' : 'Not Set'}
                </p>
              </div>
            ))}
          </div>
        )}

        {/* Passcode update cards */}
        <div className="space-y-6">
          <UpdatePasscodeCard
            title="Teacher Access Passcode"
            icon="👨‍🏫"
            description="Required to open the teacher / create class page"
            adminPassword={adminPassword}
            onUpdate={handleUpdateTeacher}
          />
          <UpdatePasscodeCard
            title="Recordings Access Passcode"
            icon="📹"
            description="Required to view and download class recordings"
            adminPassword={adminPassword}
            onUpdate={handleUpdateRecordings}
          />
        </div>

        {/* Security note */}
        <div className="mt-8 bg-yellow-900/30 border border-yellow-700/50 rounded-xl p-4">
          <p className="text-yellow-300 text-sm font-semibold mb-2">🔐 Security Notes</p>
          <ul className="text-yellow-200/80 text-xs space-y-1">
            <li>• Passcodes are stored as SHA-256 hashes in Supabase — never as plain text</li>
            <li>• Admin password is stored in backend <code className="bg-gray-800 px-1 rounded">.env</code> file only</li>
            <li>• After 5 failed attempts, users are locked out for 30 seconds</li>
            <li>• No passcode is ever auto-saved or auto-filled by browsers on this page</li>
            <li>• Change passcodes regularly for better security</li>
          </ul>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page entry point
// ---------------------------------------------------------------------------
export default function AdminPage() {
  const [adminPassword, setAdminPassword] = useState('')

  if (!adminPassword) {
    return <AdminLogin onLogin={setAdminPassword} />
  }

  return <AdminDashboard adminPassword={adminPassword} />
}
