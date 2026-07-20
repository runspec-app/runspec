import { Select, Input } from 'antd'
import type { Credential, AdHocEndpoint } from '../bridge'

// One transfer/relay endpoint that isn't a configured fleet host: an SSH host
// reached through a configured jump, authenticated by key/agent (or a saved
// credential / a one-shot password). Shared by the Transfer dialog's source and
// destination pickers so the two stay in lockstep.
export interface AdHocState {
  jump: string
  target: string
  user: string
  credentialId: string
  password: string
}

export const emptyAdHoc = (): AdHocState => ({ jump: '', target: '', user: '', credentialId: '', password: '' })

// A target hostname is all that's required — a jump is optional (used only when
// the console can't reach the target directly).
export const adHocValid = (a: AdHocState): boolean => !!a.target.trim()

// Build the descriptor start_transfer / a RelaySource consumes. The password is
// carried for the single connection only; the backend strips it before logging.
export function adHocToEndpoint(a: AdHocState): AdHocEndpoint {
  return {
    kind: 'adhoc',
    host: a.target.trim(),
    jump: a.jump,
    user: a.user.trim() || undefined,
    password: a.password || undefined,
    credential_id: a.credentialId || undefined,
  }
}

export function AdHocHostFields({ value, onChange, hostOptions, creds }: {
  value: AdHocState
  onChange: (v: AdHocState) => void
  hostOptions: { label: string; value: string; disabled?: boolean }[]
  creds: Credential[]
}) {
  const set = (patch: Partial<AdHocState>) => onChange({ ...value, ...patch })
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      <Input
        size="small"
        value={value.target}
        onChange={e => set({ target: e.target.value })}
        placeholder="target hostname"
        style={{ width: 180, fontFamily: 'monospace' }}
      />
      <Select
        size="small"
        value={value.jump || undefined}
        onChange={v => set({ jump: v ?? '' })}
        placeholder="jump host (optional)"
        style={{ minWidth: 150 }}
        allowClear
        options={hostOptions}
      />
      <Input
        size="small"
        value={value.user}
        onChange={e => set({ user: e.target.value })}
        placeholder="user (optional)"
        style={{ width: 120 }}
      />
      {creds.length > 0 && (
        <Select
          size="small"
          value={value.credentialId || undefined}
          onChange={v => set({ credentialId: v ?? '', password: v ? '' : value.password })}
          placeholder="saved credential"
          style={{ width: 170 }}
          allowClear
          options={creds.map(c => ({ label: c.label, value: c.id }))}
        />
      )}
      <Input.Password
        size="small"
        value={value.password}
        onChange={e => set({ password: e.target.value })}
        placeholder={value.credentialId ? 'using saved credential' : 'password — only if your key fails'}
        disabled={!!value.credentialId}
        autoComplete="new-password"
        style={{ width: 220 }}
      />
    </div>
  )
}
