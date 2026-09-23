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
    // Ship ONE app bundle: disable code-splitting so no separate chunk is emitted.
    // The console is served on a cold launch by a local HTTP server that briefly
    // contends for the GIL with startup work, and a dynamically-imported chunk
    // that lost that race threw "failed to fetch dynamically imported module" and
    // blanked the window. With a single bundle there is no on-demand chunk to fail
    // — the whole failure class is gone, at the cost of a larger first-paint parse
    // (an accepted trade for reliability). React.lazy/<Suspense> stay in the source
    // and keep their lazy-*mount* benefit (a tab's tree instantiates only on first
    // visit); only the runtime *fetch* is removed.
    rollupOptions: {
      output: {
        codeSplitting: false,
      },
    },
    // The single app bundle is intentionally >500 kB; don't warn about it.
    chunkSizeWarningLimit: 6000,
  },
  test: {
    // jsdom so component tests can render React + assert on the DOM. Pure-logic
    // tests run fine under it too. setup.ts polyfills the browser APIs antd
    // reaches for (matchMedia / ResizeObserver) that jsdom doesn't ship.
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
  },
})
