import React from 'react'
import ReactDOM from 'react-dom/client'
import './index.css'
import App from './App'
import { installDispatchTap } from './devbus'

// Tap window.dispatchEvent before anything renders so the Dev tab captures every
// `runspec:*` event from the very first one. Module-scope (not an effect) + an
// internal idempotency guard keep it safe under StrictMode.
installDispatchTap()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
