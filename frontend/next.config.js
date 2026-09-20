/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  // Allow backend API URL to be set at build time or runtime
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000',
  },

  // Security headers
  async headers() {
    return [
      {
        source: '/(.*)',
        headers: [
          { key: 'X-Content-Type-Options',    value: 'nosniff' },
          { key: 'X-Frame-Options',           value: 'DENY' },
          { key: 'X-XSS-Protection',          value: '1; mode=block' },
          { key: 'Referrer-Policy',           value: 'strict-origin-when-cross-origin' },
          // Required for camera and microphone in the classroom
          { key: 'Permissions-Policy',        value: 'camera=*, microphone=*, display-capture=*' },
        ],
      },
    ]
  },
}

module.exports = nextConfig
