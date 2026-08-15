/// <reference types="vitest" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

// The FastAPI backend in dev. Override with VITE_API_TARGET when running the
// backend in Docker on a different port.
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:4020';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },

  server: {
    port: 5173,
    strictPort: true,
    host: true,
    proxy: {
      // Live captions is a persistent websocket, not a request/response poll
      // like everything else under /api -- it needs its own entry with
      // ws:true, and it must come before the generic /api entry below or
      // that one (ws:false) intercepts it first: Vite matches proxy keys in
      // declaration order and stops at the first prefix match.
      '/api/live-caption': {
        target: API_TARGET,
        changeOrigin: true,
        ws: true,
      },
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        ws: false,
        configure: (proxy) => {
          // The job progress stream must not be buffered or the progress bar
          // arrives in one lump at the end.
          proxy.on('proxyRes', (proxyRes, req) => {
            if ((req.url ?? '').includes('/stream')) {
              proxyRes.headers['cache-control'] = 'no-cache, no-transform';
              proxyRes.headers['x-accel-buffering'] = 'no';
            }
          });
        },
      },
    },
  },

  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
    target: 'es2020',
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom', 'react-router-dom'],
          query: ['@tanstack/react-query'],
          markdown: ['marked', 'dompurify'],
        },
      },
    },
  },

  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/__tests__/**/*.test.{ts,tsx}'],
  },
});
