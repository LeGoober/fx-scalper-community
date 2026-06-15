import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

function normalizeBasePath(value) {
  const raw = String(value || '/').trim()
  if (!raw || raw === '/') {
    return '/'
  }
  const withLeadingSlash = raw.startsWith('/') ? raw : `/${raw}`
  return withLeadingSlash.endsWith('/') ? withLeadingSlash : `${withLeadingSlash}/`
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget = env.VITE_API_PROXY_TARGET || 'http://localhost:5001'
  const base = normalizeBasePath(env.VITE_PUBLIC_BASE_PATH || '/')

  return {
    base,
    plugins: [vue()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      proxy: {
        '/api': proxyTarget,
      },
    },
  }
})
