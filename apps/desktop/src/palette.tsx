import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import '@/styles/globals.css';

import { Palette } from '@/app/Palette';

/**
 * Palette entry point. Kept deliberately bare.
 *
 * No store, no transport, no connection bootstrap — anything imported here is
 * parsed before the palette can paint, and R2 budgets that at well under 100 ms.
 * When the palette needs backend data it will request it *after* first paint.
 */

const container = document.getElementById('root');
if (container === null) throw new Error('Missing #root');

createRoot(container).render(
  <StrictMode>
    <Palette />
  </StrictMode>,
);
