import React from 'react'
import ReactDOM from 'react-dom/client'
import './index.css'
import App from './App'
import { SendBarApp } from './views/SendBar'
import { installDispatchTap } from './devbus'
import { installDiagramRenderer } from './diagramRender'
import { ErrorBoundary } from './components/ErrorBoundary'

// The Send to terminals bar is a second, small window on the same page
// (bridge_termsend.open_send_bar): it renders only the bar, none of the app.
const isSendBar = window.location.hash === '#send-bar'

// Tap window.dispatchEvent before anything renders so the Dev tab captures every
// `runspec:*` event from the very first one. Module-scope (not an effect) + an
// internal idempotency guard keep it safe under StrictMode.
if (!isSendBar) {
  installDispatchTap()
  // Answer the agent's render_diagram tool (Mermaid → PNG in this web view).
  installDiagramRenderer()
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      {isSendBar ? <SendBarApp /> : <App />}
    </ErrorBoundary>
  </React.StrictMode>
)
