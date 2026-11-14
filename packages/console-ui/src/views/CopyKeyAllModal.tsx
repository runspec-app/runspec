import { useEffect, useMemo, useRef, useState } from 'react'
import { Modal, Button, Typography, Input, Checkbox, message } from 'antd'
import { KeyOutlined } from '@ant-design/icons'
import { bridge } from '../bridge'
import { MONO_FONT, RADIUS } from '../ThemeContext'
import { StatusIcon } from '../components/kit'

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
  const [filter, setFilter] = useState('')

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
      setFilter('')
    }
    wasOpen.current = open
  }, [open, hosts])

  // A fleet can be a hundred hosts: filter by name, and pick all, none or
  // just the ones not connected yet.
  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase()
    return q ? hosts.filter(h => h.name.toLowerCase().includes(q)) : hosts
  }, [hosts, filter])
  const pick = (names: string[], on: boolean) =>
    setSelected(prev => (on ? [...new Set([...prev, ...names])] : prev.filter(n => !names.includes(n))))
  const options = useMemo(
    () => shown.map(h => ({
      label: h.connected ? `${h.name} (already connected)` : h.name,
      value: h.name,
    })),
    [shown],
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
          <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <Input size="small" allowClear placeholder="Filter hosts" aria-label="Filter hosts" value={filter}
              onChange={e => setFilter(e.target.value)} style={{ width: 180 }} />
            <Button size="small" type="link" style={{ padding: 0 }} onClick={() => pick(shown.map(h => h.name), true)}>
              {filter.trim() ? `All shown (${shown.length})` : `All (${hosts.length})`}
            </Button>
            <Button size="small" type="link" style={{ padding: 0 }}
              onClick={() => pick(shown.filter(h => !h.connected).map(h => h.name), true)}>
              Not connected ({shown.filter(h => !h.connected).length})
            </Button>
            <Button size="small" type="link" style={{ padding: 0 }} onClick={() => pick(shown.map(h => h.name), false)}>None</Button>
            <Text type="secondary" style={{ fontSize: 12, marginLeft: 'auto' }}>{selected.length} of {hosts.length} selected</Text>
          </div>
          <div style={{ maxHeight: 200, overflowY: 'auto', border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm, padding: '8px 10px' }}>
            <Checkbox.Group
              value={selected.filter(n => shown.some(h => h.name === n))}
              // The group only knows the shown hosts: keep the hidden picks.
              onChange={vals => setSelected(prev => [
                ...prev.filter(n => !shown.some(h => h.name === n)),
                ...(vals as string[]),
              ])}
              style={{ display: 'flex', flexDirection: 'column', gap: 4 }}
              options={options}
            />
            {shown.length === 0 && <Text type="secondary" style={{ fontSize: 12 }}>No host matches.</Text>}
          </div>
          </>
        )}

        <Input.Password
          autoFocus
          value={password}
          onChange={e => setPassword(e.target.value)}
          // Enter doesn't fire at every host: the Copy button does, on purpose.
          // (Moving focus to it on Enter isn't enough: the same keypress then
          // lands on the button and clicks it.)
          onPressEnter={e => e.preventDefault()}
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
              return (
                <div key={h.host} style={{ display: 'flex', alignItems: 'flex-start', gap: 6 }}>
                  <StatusIcon status={h.ok ? 'ok' : 'failed'} style={{ fontSize: 13, marginTop: 2 }} />
                  <span style={{ fontFamily: MONO_FONT, fontSize: 12, flexShrink: 0 }}>{h.host}</span>
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
