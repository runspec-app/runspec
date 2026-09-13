import { useEffect, useMemo, useRef, useState } from 'react'
import { Modal, Button, Typography, Input, Checkbox, message } from 'antd'
import { KeyOutlined } from '@ant-design/icons'
import { bridge } from '../bridge'
import { MONO_FONT, RADIUS } from '../ThemeContext'

const { Text } = Typography

export interface BulkKeyHost {
  name: string
  connected: boolean
}

interface PerHostResult {
  host: string
  ok: boolean
  connected: boolean
  error: string
}

// Bulk equivalent of CopyKeyModal — authorize this machine on many hosts at once
// with a single password. Built for initial fleet bootstrap: when hosts arrive
// from a Config-Sync pull none of them trust our key yet, so they all show
// disconnected and would otherwise need the per-host dialog one at a time. The
// disconnected hosts are pre-selected (the ones still missing the key); already-
// connected hosts can be opted in but aren't by default. The password lives only
// in local state and is cleared the moment it's handed off.
export function CopyKeyAllModal({
  open,
  hosts,
  onClose,
  onDone,
}: {
  open: boolean
  hosts: BulkKeyHost[]
  onClose: () => void
  onDone?: () => void
}) {
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [results, setResults] = useState<PerHostResult[] | null>(null)

  // Reset only on the closed→open transition — pre-select the disconnected
  // hosts and clear any prior password/results. This must NOT re-run on every
  // `hosts` identity change: the parent recomputes the `hosts` array on each of
  // its renders (connection-status polling, or a host flipping connected after
  // a failed first attempt), and resetting on that would wipe the password the
  // operator is mid-way through typing.
  const wasOpen = useRef(false)
  useEffect(() => {
    if (open && !wasOpen.current) {
      setSelected(hosts.filter(h => !h.connected).map(h => h.name))
      setPassword('')
      setResults(null)
    }
    wasOpen.current = open
  }, [open, hosts])

  const options = useMemo(
    () => hosts.map(h => ({
      label: h.connected ? `${h.name} (already connected)` : h.name,
      value: h.name,
    })),
    [hosts],
  )

  const close = () => { setPassword(''); onClose() }

  const submit = async () => {
    if (!password.trim() || selected.length === 0) return
    setSubmitting(true)
    setResults(null)
    try {
      const res = await bridge.copy_ssh_id_all(password, selected)
      setPassword('')  // don't retain the password after handing it off
      setResults(res.per_host)
      if (res.ok) {
        const allOk = res.per_host.every(h => h.ok)
        if (allOk) {
          message.success(res.message)
          onDone?.()
        } else {
          message.warning(res.message)
          onDone?.()  // some hosts flipped — refresh the sidebar
        }
      } else {
        message.error(res.message)
      }
    } catch (e) {
      message.error(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      open={open}
      onCancel={close}
      width={500}
      title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><KeyOutlined /> Copy SSH public key to all hosts</span>}
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button onClick={close}>Close</Button>
          <Button
            type="primary"
            loading={submitting}
            disabled={!password.trim() || selected.length === 0}
            onClick={submit}
          >
            Copy to {selected.length} host{selected.length !== 1 ? 's' : ''}
          </Button>
        </div>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <Text style={{ fontSize: 13 }}>
          Authorize this machine on the selected hosts by adding its public key to each host's <Text code>authorized_keys</Text>. One password is tried against every host — ideal for a fleet that shares a bootstrap credential.
        </Text>

        {hosts.length === 0 ? (
          <Text type="secondary" style={{ fontSize: 13 }}>No remote hosts configured.</Text>
        ) : (
          <div style={{ maxHeight: 160, overflowY: 'auto', border: '1px solid #303030', borderRadius: RADIUS.sm, padding: '8px 10px' }}>
            <Checkbox.Group
              value={selected}
              onChange={vals => setSelected(vals as string[])}
              style={{ display: 'flex', flexDirection: 'column', gap: 4 }}
              options={options}
            />
          </div>
        )}

        <Input.Password
          autoFocus
          value={password}
          onChange={e => setPassword(e.target.value)}
          onPressEnter={submit}
          placeholder="SSH password"
          autoComplete="new-password"
          disabled={hosts.length === 0}
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          Connects once to each host with this password to install the key. The password is not stored — afterwards each host connects with the key alone. Hosts that use a different password will fail; retry those individually from the host's own Copy key action.
        </Text>

        {results && results.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 3, marginTop: 2 }}>
            {results.map(h => {
              const icon = h.ok ? '✓' : '✗'
              const color = h.ok ? '#52c41a' : '#ff4d4f'
              return (
                <div key={h.host} style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
                  <span style={{ color, fontFamily: MONO_FONT, fontSize: 12, flexShrink: 0 }}>{icon} {h.host}</span>
                  {h.error && <Text type="secondary" style={{ fontSize: 11 }}>{h.error}</Text>}
                </div>
              )
            })}
          </div>
        )}
      </div>
    </Modal>
  )
}
