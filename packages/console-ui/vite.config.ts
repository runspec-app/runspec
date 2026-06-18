/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    // Output directly into the Python package so `pip install` bundles it
    outDir: '../python/runspec-console/runspec_console/dist',
    emptyOutDir: true,
  },
  test: {
    // jsdom so component tests can render React + assert on the DOM. Pure-logic
    // tests run fine under it too. setup.ts polyfills the browser APIs antd
    // reaches for (matchMedia / ResizeObserver) that jsdom doesn't ship.
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
})
