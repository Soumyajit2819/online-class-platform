'use client'

import { useState, useRef, useEffect } from 'react'

interface PasscodeGateProps {
  title: string
  description: string
  icon: string
  onVerify: (passcode: string) => Promise<boolean>
  onSuccess: () => void
}

export default function PasscodeGate({
  title,
  description,
  icon,
  onVerify,
  onSuccess,
}: PasscodeGateProps) {
  const [passcode, setPasscode]   = useState('')
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState('')
  const [attempts, setAttempts]   = useState(0)
  const [locked, setLocked]       = useState(false)
  const [lockTimer, setLockTimer] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  // Focus input on mount
  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  // Countdown timer when locked out
  useEffect(() => {
    if (lockTimer <= 0) {
      if (locked) setLocked(false)
      return
    }
    const id = setInterval(() => {
      setLockTimer(t => {
        if (t <= 1) {
          setLocked(false)
          clearInterval(id)
          return 0
        }
        return t - 1
      })
    }, 1000)
    return () => clearInterval(id)
  }, [lockTimer])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (locked || loading) return
    if (!passcode.trim()) {
      setError('Please enter the passcode')
      return
    }

    setLoading(true)
    setError('')

    try {
      const ok = await onVerify(passcode)
      if (ok) {
        onSuccess()
      } else {
        // Should not reach here — onVerify throws on failure
        setError('Invalid passcode')
      }
    } catch (err: any) {
      const newAttempts = attempts + 1
      setAttempts(newAttempts)
      setPasscode('')

      // Lock out after 5 failed attempts for 30 seconds
      if (newAttempts >= 5) {
        setLocked(true)
        setLockTimer(30)
        setError('Too many failed attempts. Try again in 30 seconds.')
        setAttempts(0)
      } else {
        setError(`Invalid passcode. ${5 - newAttempts} attempt${5 - newAttempts !== 1 ? 's' : ''} remaining.`)
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-slate-900 to-slate-800 p-8">
      <div className="w-full max-w-sm">
        {/* Card */}
        <div className="bg-white rounded-2xl shadow-2xl p-8">
          <div className="text-center mb-6">
            <div className="text-5xl mb-3">{icon}</div>
            <h1 className="text-2xl font-bold text-gray-900">{title}</h1>
            <p className="text-gray-500 text-sm mt-2">{description}</p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Access Passcode
              </label>
              <input
                ref={inputRef}
                type="password"
                value={passcode}
                onChange={e => setPasscode(e.target.value)}
                disabled={locked || loading}
                // Security: disable all browser auto-fill / password managers
                autoComplete="off"
                autoCorrect="off"
                autoCapitalize="off"
                spellCheck={false}
                data-form-type="other"
                data-lpignore="true"
                data-1p-ignore="true"
                className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:bg-gray-100 disabled:cursor-not-allowed text-gray-900 placeholder-gray-400"
                placeholder={locked ? `Locked (${lockTimer}s)` : '••••••••'}
              />
            </div>

            {error && (
              <div className={`p-3 rounded-lg text-sm ${
                locked
                  ? 'bg-red-50 border border-red-200 text-red-800'
                  : 'bg-red-50 border border-red-200 text-red-700'
              }`}>
                {locked ? '🔒 ' : '⚠️ '}{error}
              </div>
            )}

            <button
              type="submit"
              disabled={locked || loading || !passcode.trim()}
              className="w-full py-3 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed text-white font-semibold rounded-lg transition-colors"
            >
              {loading ? (
                <span className="flex items-center justify-center gap-2">
                  <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  Verifying...
                </span>
              ) : locked ? (
                `Locked (${lockTimer}s)`
              ) : (
                'Enter'
              )}
            </button>
          </form>

          {/* Attempts indicator */}
          {attempts > 0 && !locked && (
            <div className="mt-4 flex justify-center gap-1">
              {[...Array(5)].map((_, i) => (
                <div
                  key={i}
                  className={`w-2 h-2 rounded-full ${
                    i < attempts ? 'bg-red-500' : 'bg-gray-200'
                  }`}
                />
              ))}
            </div>
          )}
        </div>

        <p className="text-center text-gray-500 text-xs mt-4">
          Contact your administrator if you don't have the passcode
        </p>
      </div>
    </div>
  )
}
