// antd 5's pop-ups that live outside the React tree — message, notification
// and the static Modal.confirm — mount themselves with the legacy
// ReactDOM.render, which React 19 removed. Without this they never appear
// (antd only logs "antd v5 support React is 16 ~ 18"), so every "Saved",
// every error toast and every toast-borne Undo was silently invisible.
// antd's documented fix for React 19: hand it a createRoot-based render.

import { unstableSetRender } from 'antd'
import { createRoot, type Root } from 'react-dom/client'

type Host = (Element | DocumentFragment) & { _rsAntdRoot?: Root }

let installed = false

/** Point antd's out-of-tree rendering at React 19's createRoot. Idempotent;
 *  call once before the app renders. */
export function installAntdRender(): void {
  if (installed) return
  installed = true
  unstableSetRender((node, container) => {
    const host = container as Host
    host._rsAntdRoot ??= createRoot(container)
    const root = host._rsAntdRoot
    root.render(node)
    return async () => {
      // Let the pop-up's own leave animation settle before unmounting.
      await new Promise(resolve => setTimeout(resolve, 0))
      root.unmount()
      delete host._rsAntdRoot
    }
  })
}
