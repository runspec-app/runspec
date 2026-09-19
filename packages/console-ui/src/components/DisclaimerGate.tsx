import { useState } from 'react'
import { Modal, Button, message } from 'antd'
import { bridge, type Disclaimer } from '../bridge'
import { Markdown } from './Markdown'
import { useIsDark, textColor, textMuted } from '../ThemeContext'

// The first-use AI-use disclaimer gate: a blocking, non-dismissable modal shown
// over the whole app when a deployment requires acknowledgement
// ([console.disclaimer], off by default, hard-off when [console] ai = false — the
// backend decides all of that; this only renders `needs_ack`). The modal itself
// has no X, no Esc, no backdrop close and a single Acknowledge button (records it,
// then the app is usable). To *decline*, the operator closes the app the normal
// way — the native window's close button, which already pops the app's
// `confirm_close` "are you sure?" guard. We deliberately don't add an in-app close
// button: it would call window.destroy(), which bypasses that native guard.
// See docs/design/console-disclaimer.md.

interface DisclaimerGateProps {
  disclaimer: Disclaimer
  onAcknowledged: () => void
}

export function DisclaimerGate({ disclaimer, onAcknowledged }: DisclaimerGateProps) {
  const isDark = useIsDark()
  const [busy, setBusy] = useState(false)

  const acknowledge = async () => {
    setBusy(true)
    try {
      const res = await bridge.acknowledge_disclaimer()
      if (res && res.ok && !res.needs_ack) {
        onAcknowledged()
      } else {
        // The write failed — keep the gate up rather than letting the operator in
        // without a recorded acknowledgement.
        message.error('Could not record your acknowledgement. Please try again.')
      }
    } catch {
      message.error('Could not record your acknowledgement. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={disclaimer.needs_ack}
      title={disclaimer.title || 'Notice'}
      closable={false}       // no X
      maskClosable={false}   // backdrop click does nothing
      keyboard={false}       // Esc does nothing
      centered
      width={560}
      footer={
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
          <Button type="primary" loading={busy} onClick={acknowledge}>
            {disclaimer.acknowledge_label || 'I acknowledge'}
          </Button>
          <span style={{ fontSize: 12, color: textMuted(isDark) }}>
            To decline, close the window.
          </span>
        </div>
      }
    >
      <div style={{ maxHeight: '60vh', overflow: 'auto' }}>
        <Markdown color={textColor(isDark)} fontSize={14}>
          {disclaimer.text}
        </Markdown>
      </div>
    </Modal>
  )
}
