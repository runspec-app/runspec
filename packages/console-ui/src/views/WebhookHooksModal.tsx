/**
 * WebhookHooksModal — one venv's webhooks, edited from the console:
 *  - Hooks: every hook the venv's runspec-scheduler has. Console-made hooks are
 *    edited as TOML, tried against a body (or the hook's last one) without
 *    running anything, saved with a new token / basic-auth password (shown
 *    once: the host keeps only a hash) and removed. Hooks deployed into
 *    `hooks.d` are read-only here.
 *  - Listener & mail: `[hooks]` (port, TLS, limits, approval windows) and
 *    `[smtp]` (who gets mailed when an approval goes unanswered).
 * Everything runs the venv's own `runspec-scheduler hooks …` as its owner.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert, Button, Checkbox, Input, InputNumber, Modal, Select, Spin, Switch, Tabs, Typography, message,
} from 'antd'
import { PlusOutlined, CopyOutlined, ExperimentOutlined } from '@ant-design/icons'
import { bridge } from '../bridge'
import type {
  WebhookHook, WebhookHooksList, WebhookSaveResult, WebhookSettings, WebhookSmtp, WebhookTarget, WebhookTestResult,
} from '../bridge'
import { useIsDark, textColor, textMuted, textSecondary, textWarning, MONO_FONT, RADIUS } from '../ThemeContext'
import { StatusTag, RowActions, EmptyState } from '../components/kit'
import { deliveryStatus, runLine } from '../webhooks'

const { Text } = Typography

export const NEW_HOOK_TOML = `[[hook]]
name = "new-hook"
description = "What sends this, and what it's for"
# Fields read from the body (dotted paths): usable in match and args.
fields = { status = "status" }
# Saving the hook is the approval: its runs go ahead without asking (manual
# runnables are always refused). For a person to approve each run, add:
# require_approval = true
# To answer the sender with the run's exit code and output (runspec-scheduler
# 0.12.0), waiting up to 25s for it to finish, add:
# respond = "output"

[[hook.rule]]
name = "default"
match = { status = "^firing$" }
run = "runnable-name"
args = { }
`

/** The hook name a TOML text declares, or ''. */
export function tomlName(text: string): string {
  return /^\s*name\s*=\s*"([^"]*)"/m.exec(text)?.[1] ?? ''
}

/** Whether the TOML sets a basic-auth username (a new password needs one). */
export function hasUsername(text: string): boolean {
  return /^\s*username\s*=\s*"[^"]+"/m.test(text)
}

const HOOK_KEYS = [
  'enabled', 'port', 'bind', 'base_path', 'cert', 'key', 'max_body', 'keep',
  'rate_per_minute', 'trusted_proxies', 'approve_within', 'expect_claim_within', 'max_runs',
] as const
const SMTP_KEYS = ['relay', 'port', 'from', 'to', 'starttls'] as const

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null)
}

/** Only the keys that changed, so a save never rewrites what you didn't touch. */
export function changed<T extends Record<string, unknown>>(before: T, after: T, keys: readonly string[]): Partial<T> {
  const out: Record<string, unknown> = {}
  for (const k of keys) if (!same(before[k], after[k])) out[k] = after[k]
  return out as Partial<T>
}

function TestResults({ result }: { result: WebhookTestResult }) {
  const isDark = useIsDark()
  if (!result.ok) return <Alert type="warning" showIcon message={`The test didn't run: ${result.error || 'no answer'}`} />
  return (
    <div data-testid="webhook-test-result" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <Text style={{ fontSize: 12, color: textMuted(isDark) }}>
        Tried against {result.source || 'the body'}: {result.items.length} item{result.items.length === 1 ? '' : 's'}. Nothing ran.
      </Text>
      {result.items.map((it, i) => {
        const st = deliveryStatus({ ...it, id: i, hook: '', received: null, source: '', rule: it.rule ?? '', runnable: it.runnable ?? '', command: it.command ?? [], args: it.args ?? {}, autonomy: it.autonomy ?? '', reason: it.reason ?? '' })
        const line = runLine(it)
        return (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: 12 }}>
            <StatusTag tone={st.tone}>{it.state === 'pending' ? 'would wait for approval' : it.state === 'waiting' ? 'would run' : st.label}</StatusTag>
            {it.rule && <span style={{ color: textSecondary(isDark) }}>rule <span style={{ fontFamily: MONO_FONT }}>{it.rule}</span></span>}
            {line && <code style={{ fontFamily: MONO_FONT, color: textColor(isDark) }}>{line}</code>}
            {it.reason && <span style={{ color: textSecondary(isDark) }}>{it.reason}</span>}
          </div>
        )
      })}
    </div>
  )
}

function SecretsShownOnce({ saved, onDone }: { saved: WebhookSaveResult; onDone: () => void }) {
  const s = saved.secrets ?? {}
  return (
    <Alert
      type="warning" showIcon data-testid="webhook-secrets"
      message="Copy these now: they won't be shown again"
      description={
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {saved.url && <div>URL <Text copyable code style={{ fontFamily: MONO_FONT }}>{saved.url}</Text></div>}
          {s.token && <div>Token <Text copyable code style={{ fontFamily: MONO_FONT }}>{s.token}</Text></div>}
          {s.password && <div>Password <Text copyable code style={{ fontFamily: MONO_FONT }}>{s.password}</Text></div>}
          <div>{saved.note || 'Only a hash is stored on the host. Put it in the sending app now.'}</div>
          <div><Button size="small" onClick={onDone}>I've copied them</Button></div>
        </div>
      }
    />
  )
}

function HookEditor({ target, hook, onSaved, onRemoved }: {
  target: WebhookTarget
  hook: WebhookHook | null   // null = a new hook
  onSaved: (name: string) => void
  onRemoved: () => void
}) {
  const isDark = useIsDark()
  const readOnly = !!hook && hook.source !== 'console'
  const [text, setText] = useState(hook?.toml || NEW_HOOK_TOML)
  const [newToken, setNewToken] = useState(!hook)
  const [newPassword, setNewPassword] = useState(false)
  const [body, setBody] = useState('')
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<WebhookTestResult | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [shown, setShown] = useState<WebhookSaveResult | null>(null)
  const dirty = text !== (hook?.toml || NEW_HOOK_TOML)

  useEffect(() => {
    setText(hook?.toml || NEW_HOOK_TOML); setNewToken(!hook); setNewPassword(false); setTest(null); setError('')
  }, [hook])

  const runTest = async () => {
    setTesting(true)
    try { setTest(await bridge.webhook_hook_test({ ...target, text, body })) } finally { setTesting(false) }
  }
  const save = async () => {
    setSaving(true); setError('')
    try {
      const r = await bridge.webhook_hook_save({ ...target, text, new_token: newToken, new_password: newPassword })
      if (!r.ok) { setError(r.error || "The host didn't save it."); return }
      if (r.secrets && Object.keys(r.secrets).length) setShown(r)
      else message.success(`Saved ${r.hook ?? tomlName(text)} on ${target.host} / ${target.venv}.`)
      onSaved(r.hook ?? tomlName(text))
    } finally { setSaving(false) }
  }
  const remove = async () => {
    const name = hook?.name ?? ''
    const r = await bridge.webhook_hook_remove({ ...target, name })
    if (!r.ok) { setError(r.error || `Couldn't remove ${name}.`); return }
    message.success(`Removed ${name}. Senders now get 404.`)
    onRemoved()
  }
  const copy = () => {
    void navigator.clipboard?.writeText(text).then(() => message.success('Copied as TOML.'), () => message.error("Couldn't reach the clipboard."))
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, minWidth: 0 }}>
      {shown && <SecretsShownOnce saved={shown} onDone={() => setShown(null)} />}
      {readOnly && (
        <Text style={{ fontSize: 12, color: textSecondary(isDark) }}>
          Deployed from <span style={{ fontFamily: MONO_FONT }}>{hook?.file}</span>
          {hook?.managed_by ? ` (managed by ${hook.managed_by})` : ''}. Change it in that file; you can still test it here.
        </Text>
      )}
      {hook?.overridden && (
        <Alert type="info" showIcon message="A deployed hook with this name wins over this one, so this one isn't used." />
      )}
      {hook?.url && !readOnly && (
        <Text style={{ fontSize: 12, color: textMuted(isDark) }}>
          Receives at <Text copyable style={{ fontFamily: MONO_FONT, fontSize: 12 }}>{hook.url}</Text>
          {hook.auth.length ? ` · auth: ${hook.auth.join(' + ')}` : ' · no auth set'}
        </Text>
      )}
      <Input.TextArea
        aria-label="Hook as TOML" value={text} readOnly={readOnly} onChange={e => setText(e.target.value)}
        autoSize={{ minRows: 12, maxRows: 26 }} spellCheck={false}
        style={{ fontFamily: MONO_FONT, fontSize: 13, borderRadius: RADIUS.sm }}
      />
      {!readOnly && (
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', fontSize: 13 }}>
          <Checkbox checked={newToken} onChange={e => setNewToken(e.target.checked)}>
            {hook?.auth.includes('token') ? 'Replace the token' : 'Make a token'}
          </Checkbox>
          <Checkbox checked={newPassword} disabled={!hasUsername(text)} onChange={e => setNewPassword(e.target.checked)}>
            {hasUsername(text) ? 'Make a basic-auth password' : 'Make a basic-auth password (set username first)'}
          </Checkbox>
        </div>
      )}
      <details>
        <summary style={{ cursor: 'pointer', fontSize: 13, color: textSecondary(isDark) }}>Try it with a body</summary>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 6 }}>
          <Input.TextArea aria-label="Test body" value={body} onChange={e => setBody(e.target.value)}
            autoSize={{ minRows: 3, maxRows: 10 }} spellCheck={false}
            placeholder={hook ? 'Paste a JSON body, or leave empty to use the last one this hook received' : 'Paste a JSON body to try'}
            style={{ fontFamily: MONO_FONT, fontSize: 12 }} />
          <div>
            <Button size="small" icon={<ExperimentOutlined />} loading={testing} disabled={!hook && !body.trim()} onClick={() => { void runTest() }}>
              Test{dirty ? ' as edited' : ''}
            </Button>
          </div>
          {test && <TestResults result={test} />}
        </div>
      </details>
      {error && <Alert type="error" showIcon message={error} />}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        {!readOnly && (
          <Button type="primary" loading={saving} onClick={() => { void save() }} disabled={!tomlName(text)}>
            {hook ? 'Save' : 'Add hook'}
          </Button>
        )}
        <Button icon={<CopyOutlined />} onClick={copy}>Copy as TOML</Button>
        {hook && !readOnly && (
          <RowActions testId="hook-more" more={[{
            key: 'remove', label: 'Remove hook', danger: true, onClick: () => { void remove() },
            confirm: {
              title: 'Remove this hook?',
              what: <span style={{ fontFamily: MONO_FONT }}>{hook.name} on {target.host} / {target.venv}</span>,
              consequence: 'Senders get 404 from then on. Deliveries already received stay in the history.',
              okText: 'Remove',
            },
          }]} />
        )}
        {!readOnly && !tomlName(text) && <Text type="secondary" style={{ fontSize: 12 }}>Give it a name = "…" first.</Text>}
      </div>
    </div>
  )
}

const hookKey = (h: WebhookHook) => `${h.source}:${h.name}`

function HooksTab({ target, list, onChanged }: { target: WebhookTarget; list: WebhookHooksList; onChanged: () => void }) {
  const isDark = useIsDark()
  const hooks = useMemo(() => list.hooks ?? [], [list])
  const [selected, setSelected] = useState<string>(hooks[0] ? hookKey(hooks[0]) : '__new')
  const [justSaved, setJustSaved] = useState('')
  useEffect(() => {
    // After a save, select what was saved; after a remove, the first hook.
    if (justSaved && hooks.some(h => h.source === 'console' && h.name === justSaved)) { setSelected(`console:${justSaved}`); setJustSaved(''); return }
    if (selected !== '__new' && !hooks.some(h => hookKey(h) === selected)) setSelected(hooks[0] ? hookKey(hooks[0]) : '__new')
  }, [hooks, selected, justSaved])
  const hook = hooks.find(h => hookKey(h) === selected) ?? null
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(180px, 220px) 1fr', gap: 16, minHeight: 360 }}>
      <nav aria-label="Hooks" style={{ display: 'flex', flexDirection: 'column', gap: 2, borderRight: '1px solid var(--rs-divider)', paddingRight: 8 }}>
        {hooks.map(h => {
          const active = hookKey(h) === selected
          return (
            <button key={hookKey(h)} type="button" onClick={() => setSelected(hookKey(h))} aria-current={active}
              className="rs-host-row"
              style={{
                display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 2, padding: '6px 8px', border: 'none', borderRadius: RADIUS.sm,
                background: active ? 'var(--rs-hover)' : 'none', cursor: 'pointer', textAlign: 'left',
              }}>
              <span style={{ fontFamily: MONO_FONT, fontSize: 13, color: textColor(isDark), fontWeight: active ? 600 : 400 }}>{h.name}</span>
              <span style={{ fontSize: 11, color: textMuted(isDark) }}>
                {[h.source === 'console' ? 'made here' : 'deployed', !h.enabled ? 'off' : '', h.overridden ? 'not used' : '', h.auth.length ? '' : 'no auth']
                  .filter(Boolean).join(' · ')}
              </span>
            </button>
          )
        })}
        {!hooks.length && <Text style={{ fontSize: 12, color: textMuted(isDark), padding: '6px 8px' }}>No hooks yet.</Text>}
        <Button size="small" type="text" icon={<PlusOutlined />} onClick={() => setSelected('__new')}
          style={{ justifyContent: 'flex-start', marginTop: 6, color: 'var(--rs-text-secondary)' }}>New hook</Button>
      </nav>
      <HookEditor key={selected} target={target} hook={selected === '__new' ? null : hook}
        onSaved={name => { setJustSaved(name); onChanged() }} onRemoved={onChanged} />
    </div>
  )
}

type SettingsForm = WebhookSettings & { smtp: WebhookSmtp }

/** Where a venv's listener port starts when none is set (the docs' example). */
export const DEFAULT_WEBHOOK_PORT = 8977

/** The first port from {@link DEFAULT_WEBHOOK_PORT} up that no other venv on
 *  the host uses. */
export function suggestPort(taken: number[]): number {
  const used = new Set(taken)
  let port = DEFAULT_WEBHOOK_PORT
  while (used.has(port) && port < 65535) port++
  return port
}

/** The line under the Port field. */
export function portHint(taken: number[], clash: boolean): string {
  if (clash) return 'Another venv on this host uses this port: each venv needs its own.'
  if (!taken.length) return 'Each venv on a host needs its own port.'
  const list = [...new Set(taken)].sort((a, b) => a - b)
  return `Other venvs on this host use ${list.join(', ')}.`
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  const isDark = useIsDark()
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 13, color: textSecondary(isDark) }}>
      <span>{label}</span>
      {children}
      {hint && <span style={{ fontSize: 11, color: textMuted(isDark) }}>{hint}</span>}
    </label>
  )
}

function SettingsTab({ target, list, onChanged, takenPorts = [] }: {
  target: WebhookTarget; list: WebhookHooksList; onChanged: () => void; takenPorts?: number[]
}) {
  const isDark = useIsDark()
  const before: SettingsForm = useMemo(() => ({ ...(list.settings ?? {}), smtp: { ...(list.smtp ?? {}) } }), [list])
  const suggested = suggestPort(takenPorts)
  const [form, setForm] = useState<SettingsForm>(before)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => setForm(before), [before])
  const set = (patch: Partial<WebhookSettings>) => setForm(f => ({ ...f, ...patch }))
  const setSmtp = (patch: Partial<WebhookSmtp>) => setForm(f => ({ ...f, smtp: { ...f.smtp, ...patch } }))
  const hooks = changed(before as Record<string, unknown>, form as Record<string, unknown>, HOOK_KEYS)
  const smtp = changed((before.smtp ?? {}) as Record<string, unknown>, (form.smtp ?? {}) as Record<string, unknown>, SMTP_KEYS)
  const dirty = Object.keys(hooks).length + Object.keys(smtp).length > 0
  const clash = !!form.port && takenPorts.includes(form.port)
  const save = async () => {
    if (form.enabled && !form.port) { setError('Pick a port for the listener first.'); return }
    setSaving(true); setError('')
    try {
      const r = await bridge.webhook_settings({ ...target, hooks, smtp })
      if (!r.ok) { setError(r.error || "The host didn't save them."); return }
      message.success(`Saved on ${target.host} / ${target.venv}. The daemon picks them up by itself.`)
      onChanged()
    } finally { setSaving(false) }
  }
  const grid = { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 12 } as const
  const h = (t: string) => <h3 style={{ fontSize: 13, fontWeight: 600, margin: '4px 0 0', color: textColor(isDark) }}>{t}</h3>
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* What the host says now: hidden while there are unsaved changes, so
          an edited form and the saved state never contradict each other. */}
      {list.settings?.problem && !dirty && <Alert type="warning" showIcon message={list.settings.problem} />}
      <label style={{ display: 'inline-flex', alignItems: 'center', gap: 10, alignSelf: 'flex-start', cursor: 'pointer' }}>
        <Switch checked={!!form.enabled} aria-label="Take webhooks"
          onChange={on => set(on && !form.port ? { enabled: on, port: suggested } : { enabled: on })} />
        <span style={{ fontSize: 14, fontWeight: 500, color: textColor(isDark) }}>
          {form.enabled ? 'Taking webhooks' : 'Not taking webhooks'}
        </span>
        {form.enabled !== !!before.enabled && (
          <span data-testid="webhook-unsaved" style={{ fontSize: 12, color: textWarning(isDark) }}>not saved yet</span>
        )}
      </label>
      {h('Listener')}
      <div style={grid}>
        <Field label="Port" hint={portHint(takenPorts, clash)}>
          <InputNumber min={1} max={65535} precision={0} value={form.port || null} placeholder={String(suggested)}
            status={clash ? 'warning' : undefined} onChange={v => set({ port: v ?? undefined })} style={{ width: '100%' }} />
        </Field>
        <Field label="Bind address" hint="127.0.0.1 = this host only; plain HTTP is only allowed there"><Input value={form.bind} onChange={e => set({ bind: e.target.value })} /></Field>
        <Field label="Path prefix" hint="Behind a reverse proxy, e.g. /runspec"><Input value={form.base_path} onChange={e => set({ base_path: e.target.value })} /></Field>
        <Field label="TLS certificate"><Input value={form.cert} onChange={e => set({ cert: e.target.value })} style={{ fontFamily: MONO_FONT }} /></Field>
        <Field label="TLS key"><Input value={form.key} onChange={e => set({ key: e.target.value })} style={{ fontFamily: MONO_FONT }} /></Field>
        <Field label="Trusted proxies" hint="Addresses whose X-Forwarded-For is believed">
          <Select mode="tags" value={form.trusted_proxies ?? []} onChange={v => set({ trusted_proxies: v })} tokenSeparators={[',', ' ']} />
        </Field>
      </div>
      {h('Limits and approvals')}
      <div style={grid}>
        <Field label="Approve within" hint="A waiting delivery expires after this, e.g. 15m"><Input value={String(form.approve_within ?? '')} onChange={e => set({ approve_within: e.target.value })} /></Field>
        <Field label="Mail if unclaimed after" hint="Nobody following? Mail the list below after this"><Input value={String(form.expect_claim_within ?? '')} onChange={e => set({ expect_claim_within: e.target.value })} /></Field>
        <Field label="Keep deliveries for" hint="e.g. 30d"><Input value={String(form.keep ?? '')} onChange={e => set({ keep: e.target.value })} /></Field>
        <Field label="Largest body (bytes)"><InputNumber min={1} value={form.max_body} onChange={v => set({ max_body: v ?? undefined })} style={{ width: '100%' }} /></Field>
        <Field label="Requests a minute"><InputNumber min={1} value={form.rate_per_minute} onChange={v => set({ rate_per_minute: v ?? undefined })} style={{ width: '100%' }} /></Field>
        <Field label="Runs at once"><InputNumber min={1} value={form.max_runs} onChange={v => set({ max_runs: v ?? undefined })} style={{ width: '100%' }} /></Field>
      </div>
      {h('Mail for unanswered approvals')}
      <div style={grid}>
        <Field label="SMTP relay"><Input value={form.smtp.relay} onChange={e => setSmtp({ relay: e.target.value })} /></Field>
        <Field label="Port"><InputNumber min={1} max={65535} value={form.smtp.port} onChange={v => setSmtp({ port: v ?? undefined })} style={{ width: '100%' }} /></Field>
        <Field label="From"><Input value={form.smtp.from} onChange={e => setSmtp({ from: e.target.value })} /></Field>
        <Field label="To"><Select mode="tags" value={form.smtp.to ?? []} onChange={v => setSmtp({ to: v })} tokenSeparators={[',', ' ']} /></Field>
        <Field label="STARTTLS"><Switch checked={!!form.smtp.starttls} onChange={on => setSmtp({ starttls: on })} style={{ alignSelf: 'flex-start' }} /></Field>
      </div>
      {error && <Alert type="error" showIcon message={error} />}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <Button type="primary" disabled={!dirty} loading={saving} onClick={() => { void save() }}>Save</Button>
        {dirty && <Button onClick={() => setForm(before)}>Undo changes</Button>}
        {dirty && <span style={{ fontSize: 12, color: textSecondary(isDark) }}>Unsaved: Save sends them to {target.host} / {target.venv}.</span>}
      </div>
    </div>
  )
}

export function WebhookHooksModal({ target, onClose, startTab = 'hooks', takenPorts = [] }: {
  target: WebhookTarget; onClose: () => void; startTab?: 'hooks' | 'settings'
  /** Ports other venvs on this host listen on (or are set to), so a new one is picked clear of them. */
  takenPorts?: number[]
}) {
  const [list, setList] = useState<WebhookHooksList | null>(null)
  const [tab, setTab] = useState<string>(startTab)
  const { host, venv } = target
  // Keyed on the names, not the object: the parent builds a fresh target on
  // every render, and re-reading the host each time reset the form mid-typing.
  const load = useCallback(() => {
    bridge.webhook_hooks({ host, venv }).then(setList, (e: unknown) => setList({ ok: false, error: String(e) }))
  }, [host, venv])
  useEffect(() => { load() }, [load])
  return (
    <Modal
      open onCancel={onClose} footer={null} width={1040} destroyOnHidden
      title={<span>Webhooks on <span style={{ fontFamily: MONO_FONT }}>{target.host} / {target.venv}</span></span>}
    >
      {!list ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 40 }}><Spin /></div>
      ) : !list.ok ? (
        <EmptyState message={`Couldn't read this venv's webhooks: ${list.error || 'no answer'}. It needs runspec-scheduler 0.11.0 or later.`}
          action={{ label: 'Try again', onClick: () => { setList(null); load() } }} />
      ) : (
        <Tabs activeKey={tab} onChange={setTab} items={[
          { key: 'hooks', label: `Hooks (${list.hooks?.length ?? 0})`, children: <HooksTab target={target} list={list} onChanged={load} /> },
          { key: 'settings', label: 'Listener & mail', children: <SettingsTab target={target} list={list} onChanged={load} takenPorts={takenPorts} /> },
        ]} />
      )}
    </Modal>
  )
}
