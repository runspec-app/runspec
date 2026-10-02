/**
 * TerminalsModal — open a PuTTY SSH terminal on several hosts at once.
 *
 * The bulk twin of the host row's "Open SSH terminal". Lists every configured
 * remote host (not just the active profile's working set — PuTTY makes its own
 * connection, so a dormant host opens fine), with quick-pick by group and by
 * profile, and an "Arrange" choice (grid / compact grid / side by side / stacked / cascade /
 * don't arrange) that lays the new windows out on the console's monitor
 * (Windows, best-effort; remembered per machine).
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Modal, Alert, Button, Select, Space, Typography, message } from 'antd'
import { CodeOutlined } from '@ant-design/icons'
import { bridge, type HostProfile, type JumpHost, type LaunchTerminalsResult } from '../bridge'
import {
  GRID_MAX, LAYOUT_OPTIONS, addToSelection, hostsInGroup, hostsInProfile, launchWithLayout, readLayoutPref,
  writeLayoutPref, type TerminalLayout,
} from './terminals'
import { MONO_FONT, textMuted, useIsDark } from '../ThemeContext'

const { Text } = Typography

export interface TerminalsModalProps {
  open: boolean
  onClose: () => void
  /** Hosts to pre-select when the dialog opens. */
  initial?: string[]
  /** Names of hosts the console currently has a connection to — shown as a
   *  green dot and offered by "All connected". */
  connected: string[]
}

export function TerminalsModal({ open, onClose, initial, connected }: TerminalsModalProps) {
  const isDark = useIsDark()
  const [jumpHosts, setJumpHosts] = useState<JumpHost[]>([])
  const [profiles, setProfiles] = useState<HostProfile[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [layout, setLayout] = useState<TerminalLayout>(readLayoutPref)
  const [busy, setBusy] = useState(false)
  const [errors, setErrors] = useState<LaunchTerminalsResult['errors']>([])

  // Reset only on the closed→open transition, so a parent re-render (status
  // polling hands us fresh arrays) doesn't wipe the operator's picks.
  const wasOpen = useRef(false)
  useEffect(() => {
    if (open && !wasOpen.current) {
      setSelected(initial ?? [])
      setErrors([])
      bridge.get_jump_hosts().then(setJumpHosts).catch(() => setJumpHosts([]))
      bridge.get_profiles().then(setProfiles).catch(() => setProfiles([]))
    }
    wasOpen.current = open
  }, [open, initial])

  const connectedSet = useMemo(() => new Set(connected), [connected])
  const groups = useMemo(
    () => [...new Set(jumpHosts.map(h => h.group ?? ''))].sort((a, b) => a.localeCompare(b)),
    [jumpHosts],
  )
  const hostOptions = useMemo(
    () => jumpHosts.map(h => ({
      value: h.name,
      label: (
        <span style={{ fontFamily: MONO_FONT }}>
          <span style={{ color: connectedSet.has(h.name) ? '#52c41a' : '#595959', marginRight: 6 }}>●</span>
          {h.name}
        </span>
      ),
    })),
    [jumpHosts, connectedSet],
  )
  const allConnected = jumpHosts.map(h => h.name).filter(n => connectedSet.has(n))

  const launch = async () => {
    if (!selected.length) return
    setBusy(true)
    writeLayoutPref(layout)
    try {
      const res = await launchWithLayout(selected, layout)
      if (res.errors.length) {
        setErrors(res.errors)
        if (res.opened.length) message.success(`Opened ${res.opened.length} terminal${res.opened.length === 1 ? '' : 's'}`)
      } else {
        message.success(`Opened ${res.opened.length} terminal${res.opened.length === 1 ? '' : 's'}`)
        onClose()
      }
    } catch (err) {
      message.error((err as Error)?.message || 'could not open terminals')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onCancel={onClose}
      title={<span><CodeOutlined /> Open SSH terminals</span>}
      width={560}
      footer={[
        <Button key="cancel" onClick={onClose}>Cancel</Button>,
        <Button
          key="open"
          type="primary"
          loading={busy}
          disabled={!selected.length}
          onClick={launch}
        >
          Open {selected.length || ''} terminal{selected.length === 1 ? '' : 's'}
        </Button>,
      ]}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={10}>
        <Space wrap size={6}>
          <Select
            size="small"
            placeholder="Add a group…"
            value={null}
            style={{ minWidth: 150 }}
            popupMatchSelectWidth={false}
            disabled={!groups.length}
            options={groups.map(g => ({ value: g, label: g || 'Ungrouped' }))}
            onChange={(g: string) => setSelected(prev => addToSelection(prev, hostsInGroup(jumpHosts, g)))}
          />
          <Select
            size="small"
            placeholder="Add a profile…"
            value={null}
            style={{ minWidth: 150 }}
            popupMatchSelectWidth={false}
            disabled={!profiles.length}
            options={profiles.map(p => ({ value: p.name, label: p.name }))}
            onChange={(p: string) => setSelected(prev => addToSelection(prev, hostsInProfile(jumpHosts, p)))}
          />
          <Button size="small" disabled={!allConnected.length} onClick={() => setSelected(prev => addToSelection(prev, allConnected))}>
            All connected ({allConnected.length})
          </Button>
          <Button size="small" disabled={!selected.length} onClick={() => setSelected([])}>Clear</Button>
        </Space>
        <Select
          mode="multiple"
          aria-label="Hosts"
          placeholder="Pick hosts"
          value={selected}
          onChange={setSelected}
          options={hostOptions}
          optionFilterProp="value"
          style={{ width: '100%' }}
          maxTagCount="responsive"
        />
        <Space size={8} align="center">
          <Text style={{ fontSize: 13 }}>Arrange windows on this screen</Text>
          <Select
            size="small"
            aria-label="Arrange windows"
            value={layout}
            onChange={setLayout}
            options={LAYOUT_OPTIONS}
            style={{ minWidth: 140 }}
            popupMatchSelectWidth={false}
          />
        </Space>
        {layout !== 'none' && layout !== 'cascade' && (
          <Text style={{ fontSize: 12, color: textMuted(isDark) }}>
            {layout === 'columns' ? 'Full-height windows in one row, good for an ultrawide.'
              : layout === 'rows' ? 'Full-width windows stacked top to bottom.'
                : layout === 'compact' ? 'Terminal-sized windows packed from the top-left corner, the same size on any screen.'
                  : 'A near-square grid that fills the screen.'}
            {' '}If the windows would be too small to read, a grid or cascade is used instead.
          </Text>
        )}
        {layout === 'grid' && selected.length > GRID_MAX && (
          <Text style={{ fontSize: 12, color: textMuted(isDark) }}>
            With more than {GRID_MAX} windows a grid cell gets too small to read, so they may be cascaded instead.
          </Text>
        )}
        {errors.length > 0 && (
          <Alert
            type="warning"
            showIcon
            message={`${errors.length} host${errors.length === 1 ? '' : 's'} could not open`}
            description={
              <div style={{ fontSize: 12 }}>
                {errors.map(e => (
                  <div key={e.host}><span style={{ fontFamily: MONO_FONT }}>{e.host}</span>: {e.error}</div>
                ))}
              </div>
            }
          />
        )}
      </Space>
    </Modal>
  )
}
