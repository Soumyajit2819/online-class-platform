'use client'

import Link from 'next/link'

export default function Home() {
  return (
    <main className="min-h-screen flex flex-col items-center justify-center bg-gradient-to-br from-blue-50 to-indigo-100 p-8">
      <div className="max-w-4xl w-full text-center">
        <h1 className="text-5xl font-bold text-gray-900 mb-4">
          Online Class Platform
        </h1>
        <p className="text-xl text-gray-600 mb-12">
          Live classroom powered by LiveKit
        </p>
        
        <div className="flex flex-col sm:flex-row gap-6 justify-center">
          <Link
            href="/teacher"
            className="px-8 py-4 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-lg shadow-lg transition-all duration-200 transform hover:scale-105"
          >
            Teacher
          </Link>
          <Link
            href="/student"
            className="px-8 py-4 bg-green-600 hover:bg-green-700 text-white font-semibold rounded-lg shadow-lg transition-all duration-200 transform hover:scale-105"
          >
            Student
          </Link>
        </div>
        
        <div className="mt-16 p-6 bg-white rounded-lg shadow-md">
          <h2 className="text-2xl font-semibold text-gray-800 mb-4">
            How It Works
          </h2>
          <div className="grid md:grid-cols-2 gap-6 text-left">
            <div>
              <h3 className="font-semibold text-blue-600 mb-2">For Teachers</h3>
              <ol className="list-decimal list-inside text-gray-600 space-y-1">
                <li>Create a new class</li>
                <li>Set a meeting passcode</li>
                <li>Share the room code with students</li>
                <li>Control audio/video settings</li>
                <li>Moderate the class</li>
              </ol>
            </div>
            <div>
              <h3 className="font-semibold text-green-600 mb-2">For Students</h3>
              <ol className="list-decimal list-inside text-gray-600 space-y-1">
                <li>Get the room code from your teacher</li>
                <li>Enter your name and room code</li>
                <li>Join the live class</li>
                <li>Participate with audio/video</li>
              </ol>
            </div>
          </div>
        </div>
        
        <p className="mt-8 text-sm text-gray-500">
          Maximum 50 participants per class
        </p>
      </div>
    </main>
  )
}
