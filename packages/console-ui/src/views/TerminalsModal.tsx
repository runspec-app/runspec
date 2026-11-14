/**
 * TerminalsModal — open a PuTTY SSH terminal on several hosts at once.
 *
 * The bulk twin of the host row's "Open SSH terminal". Lists every configured
 * remote host (not just the active profile's working set — PuTTY makes its own
 * connection, so a dormant host opens fine), with quick-pick by group and by
 * profile, and an "Arrange" choice (grid / compact grid / side by side / stacked / cascade /
 * don't arrange) that lays the new windows out on the console's monitor
 * (Windows, best-effort; remembered per machine). "Open the Send to terminals
 * bar" (also remembered) opens the bar that types one command into many of them.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Modal, Alert, Button, Checkbox, Select, Space, Typography, message } from 'antd'
import { CodeOutlined } from '@ant-design/icons'
import { bridge, type HostProfile, type JumpHost, type LaunchTerminalsResult } from '../bridge'
import {
  layoutHelp,
  GRID_MAX, LAYOUT_OPTIONS, addToSelection, hostsInGroup, hostsInProfile, launchWithLayout, readLayoutPref,
  readSendBarPref, writeLayoutPref, writeSendBarPref, type TerminalLayout,
} from './terminals'
import { MONO_FONT, textMuted, useIsDark } from '../ThemeContext'
import { useRiskConfirm } from '../components/risk'

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
  const [sendBar, setSendBar] = useState<boolean>(readSendBarPref)
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
          {h.name}
          {!connectedSet.has(h.name) && <span style={{ color: textMuted(isDark), fontFamily: 'inherit' }}> · not connected</span>}
        </span>
      ),
    })),
    [jumpHosts, connectedSet, isDark],
  )
  const allConnected = jumpHosts.map(h => h.name).filter(n => connectedSet.has(n))

  const [askRisk, riskHolder] = useRiskConfirm()
  const launch = async () => {
    if (!selected.length) return
    // Opening more windows than fit a readable grid is a lot to undo by hand.
    if (selected.length > GRID_MAX) {
      const a = await askRisk({
        level: 'fleet',
        title: `Open ${selected.length} terminal windows?`,
        action: `Open ${selected.length} PuTTY windows on this screen`,
        targets: selected,
        consequence: `More than ${GRID_MAX} windows are too small to tile, so they may be cascaded.`,
        okText: `Open ${selected.length}`,
      })
      if (!a.ok) return
    }
    setBusy(true)
    writeLayoutPref(layout)
    writeSendBarPref(sendBar)
    try {
      const res = await launchWithLayout(selected, layout, sendBar)
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
          {selected.length ? `Open ${selected.length} terminal${selected.length === 1 ? '' : 's'}` : 'Open terminals'}
        </Button>,
      ]}
    >
      {riskHolder}
      <Space direction="vertical" style={{ width: '100%' }} size={10}>
        <Space wrap size={6}>
          <Select
            size="small"
            aria-label="Add a group"
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
            aria-label="Add a profile"
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
        <Text style={{ fontSize: 12, color: textMuted(isDark) }} data-testid="layout-help">
          {layoutHelp(layout)}
          {layout !== 'none' && layout !== 'cascade' && (selected.length > GRID_MAX
            ? ` With more than ${GRID_MAX} windows they may be cascaded instead.`
            : ' If the windows would be too small to read, a grid or cascade is used instead.')}
        </Text>
        <div>
          <Checkbox checked={sendBar} onChange={e => setSendBar(e.target.checked)}>
            Open the Send to terminals bar
          </Checkbox>
          <div style={{ fontSize: 12, color: textMuted(isDark), marginLeft: 24 }}>
            Type a command once and it's typed into every ticked terminal. Windows only.
          </div>
        </div>
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
