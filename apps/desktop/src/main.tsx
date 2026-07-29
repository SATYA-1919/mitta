import { StrictMode, useEffect } from 'react';
import { createRoot } from 'react-dom/client';

import '@/styles/globals.css';

import { MainWindow } from '@/app/MainWindow';
import { connect } from '@/app/connection';

function Root() {
  useEffect(() => {
    // No teardown. The connection belongs to the window, not to this component,
    // and `connect()` is memoised so a second mount reuses the first socket
    // rather than opening a rival one.
    //
    // Disposing here is what broke Send. StrictMode remounts in development,
    // the cleanup ran while the first bootstrap was still in flight, and the
    // late `dispose()` detached a transport the second bootstrap had already
    // installed. The window stayed connected and stopped being able to send.
    void connect();
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
