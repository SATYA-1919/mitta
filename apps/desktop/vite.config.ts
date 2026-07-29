import { fileURLToPath, URL } from 'node:url';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
// From vitest/config, not vite: the `test` key is a Vitest extension of the
// Vite config type and is not present on Vite's own `UserConfig`.
import { defineConfig } from 'vitest/config';

/**
 * Two entry points, two bundles.
 *
 * The command palette must open in well under 100 ms (R2), and that budget is a
 * structural constraint rather than something to optimise toward later. Giving
 * the palette its own entry means it cannot accidentally pull in the chat
 * renderer, the memory explorer or the monitor charts — if it could, no amount
 * of tuning would win the budget back.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    target: 'safari18',
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        palette: fileURLToPath(new URL('./palette.html', import.meta.url)),
      },
    },
  },
  server: {
    port: 1420,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
