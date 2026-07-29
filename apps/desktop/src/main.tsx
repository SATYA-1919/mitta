import { StrictMode, useEffect } from 'react';
import { createRoot } from 'react-dom/client';

import '@/styles/globals.css';

import { MainWindow } from '@/app/MainWindow';
import { connect, type Connection } from '@/app/connection';

function Root() {
  useEffect(() => {
    let connection: Connection | null = null;
    let cancelled = false;

    void connect().then((result) => {
      // StrictMode double-invokes effects in development; without this guard the
      // second run would leak the first connection's socket and metrics listener.
      if (cancelled) {
        result?.dispose();
        return;
      }
      connection = result;
    });

    return () => {
      cancelled = true;
      connection?.dispose();
    };
  }, []);

  return <MainWindow />;
}

const container = document.getElementById('root');
if (container === null) throw new Error('Missing #root');

createRoot(container).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
