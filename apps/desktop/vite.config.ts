import { fileURLToPath, URL } from 'node:url';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
// `defineConfig` from vitest/config, because the `test` key is a Vitest
// extension not present on Vite's own `UserConfig`. `loadEnv` only exists on
// vite itself.
import { loadEnv } from 'vite';
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
export default defineConfig(({ mode }) => {
  // `scripts/dev.sh` writes .env.local before starting Vite, so the sidecar's
  // ephemeral port is known by the time this runs.
  const env = loadEnv(mode, process.cwd(), 'VITE_');
  const sidecar = env['VITE_MITTA_BASE_URL'];

  /**
   * Proxy the API through the dev server rather than letting the browser talk
   * to the sidecar directly.
   *
   * The sidecar deliberately ships **no CORS middleware** — in the real
   * application the only client is the Tauri webview, which is same-origin
   * through the shell, and adding CORS would be adding a way in (see
   * `api/app.py`). A browser on `http://localhost:1420` calling
   * `http://127.0.0.1:<port>` is cross-origin and would simply be blocked.
   *
   * Proxying makes every dev request same-origin, so the client code is
   * byte-identical in development and in the shipped app, and the security
   * posture is unchanged. Nothing dev-specific leaks past `getRuntimeInfo`.
   */
  const proxy =
    sidecar === undefined
      ? undefined
      : {
          '/v1': { target: sidecar, changeOrigin: false, ws: true },
          '/health': { target: sidecar, changeOrigin: false },
        };

  return {
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
      // Explicit IPv4. Vite's default resolves `localhost` to ::1 on macOS, and
      // the sidecar binds 127.0.0.1 — two loopbacks that are not the same origin,
      // which is a confusing way to spend an afternoon.
      host: '127.0.0.1',
      port: 1420,
      strictPort: true,
      ...(proxy === undefined ? {} : { proxy }),
    },
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
    },
  };
});
