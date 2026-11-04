// Settings → Credentials helper. A new operator's first hurdle is the
// credential ↔ env-var convention (label "Jira" + token → JIRA_TOKEN/JIRA_PAT,
// picked up only by a runnable that declares one of those names). These pieces
// turn it around: what the discovered runnables need (with a one-click "add"
// pre-filled to match), what each saved credential is used by, and a live
// "picked up by" preview while editing. Backed by bridge.credential_guide /
// credential_matches (credguide.py) — metadata only, never a secret.
import { useEffect, useState } from 'react'
import { Button, Switch, Tag, Tooltip, Typography } from 'antd'
import { CheckCircleOutlined, PlusOutlined, WarningOutlined } from '@ant-design/icons'
import {
  bridge,
  type Credential,
  type CredentialBindingNeed,
  type CredentialFeatureRef,
  type CredentialGuide,
  type CredentialNeed,
  type CredentialRunnableUse,
} from '../bridge'
import { MONO_FONT, RADIUS } from '../ThemeContext'

const { Text } = Typography

/** Values the credential dialog is opened with when adding from a suggestion. */
export interface CredentialPrefill {
  label: string
  kind: Credential['kind']
  username_arg?: string
  secret_arg?: string
  scope_runnables?: string[]
}

const KIND_ADD_LABEL: Record<string, string> = {
  token: 'Add token',
  userpass: 'Add username + password',
  password: 'Add password',
}

const box = {
  padding: '8px 10px',
  borderRadius: RADIUS.sm,
  border: '1px solid var(--rs-border)',
} as const

/** "local, web-01 +3" — short host list for a chip line. */
export function hostSummary(hosts: string[], max = 3): string {
  if (hosts.length <= max) return hosts.join(', ')
  return `${hosts.slice(0, max).join(', ')} +${hosts.length - max}`
}

function Names({ names }: { names: string[] }) {
  return (
    <>
      {names.map((n, i) => (
        <span key={n}>{i > 0 && ' '}<code style={{ fontSize: 12 }}>{n}</code></span>
      ))}
    </>
  )
}

function Covered({ by }: { by: CredentialNeed['covered_by'] }) {
  if (!by.length) return <Tag color="orange" style={{ margin: 0, fontSize: 11 }}>not set up</Tag>
  const missingSecret = by.every(c => !c.has_secret)
  return (
    <Tooltip title={missingSecret ? 'The credential exists but has no secret stored yet.' : undefined}>
      <Tag color={missingSecret ? 'gold' : 'green'} icon={missingSecret ? <WarningOutlined /> : <CheckCircleOutlined />} style={{ margin: 0, fontSize: 11 }}>
        {missingSecret ? 'no secret: ' : 'set up: '}{by.map(c => c.label).join(', ')}
      </Tag>
    </Tooltip>
  )
}

function NeedCard({ need, onAdd }: { need: CredentialNeed; onAdd: (p: CredentialPrefill) => void }) {
  return (
    <div style={box} data-testid={`cred-need-${need.prefix}`}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Text strong style={{ fontSize: 13 }}>{need.label}</Text>
        <Covered by={need.covered_by} />
        <Text type="secondary" style={{ fontSize: 12 }}>
          read by {need.runnables.map(r => `${r.name} (${hostSummary(r.hosts)})`).join(', ')}
        </Text>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 6 }}>
        {need.options.map(o => (
          <div key={o.kind} style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
            <Button
              size="small"
              icon={<PlusOutlined />}
              onClick={() => onAdd({ label: need.label, kind: o.kind })}
              style={{ flexShrink: 0 }}
            >
              {KIND_ADD_LABEL[o.kind] ?? 'Add'}
            </Button>
            <div style={{ minWidth: 0, fontSize: 12 }}>
              <div>sets <Names names={o.matches} /></div>
              {o.hint && <Text type="secondary" style={{ fontSize: 12 }}>{o.hint}</Text>}
            </div>
          </div>
        ))}
      </div>
      {need.other_env.length > 0 && (
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
          Also reads <Names names={need.other_env} />. A credential doesn't set these; put them in the venv's Env files if the default isn't right.
        </Text>
      )}
    </div>
  )
}

function BindingCard({ b, onAdd }: { b: CredentialBindingNeed; onAdd: (p: CredentialPrefill) => void }) {
  return (
    <div style={box} data-testid={`cred-binding-${b.runnable}-${b.arg}`}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <Text strong style={{ fontSize: 13 }}>{b.runnable}</Text>
        <Covered by={b.covered_by} />
        <Text type="secondary" style={{ fontSize: 12 }}>on {hostSummary(b.hosts)}</Text>
      </div>
      <Text style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
        Asks for <code>--{b.arg}</code>{b.username_arg && <> (and <code>--{b.username_arg}</code>)</>}, which reads no environment variable. A credential reaches it only by naming the argument.
      </Text>
      {b.hint && <Text type="secondary" style={{ fontSize: 12, display: 'block' }}>{b.hint}</Text>}
      <Button
        size="small"
        icon={<PlusOutlined />}
        style={{ marginTop: 6 }}
        onClick={() => onAdd({
          label: b.label,
          kind: b.username_arg ? 'userpass' : 'password',
          secret_arg: b.arg,
          username_arg: b.username_arg || undefined,
          scope_runnables: [b.runnable],
        })}
      >
        Add credential for --{b.arg}
      </Button>
    </div>
  )
}

/** Right-hand column: what the discovered runnables need. */
export function CredentialNeedsPanel({ guide, onAdd }: { guide: CredentialGuide | null; onAdd: (p: CredentialPrefill) => void }) {
  const [showCovered, setShowCovered] = useState(false)
  if (!guide) return <Text type="secondary" style={{ fontSize: 13 }}>Reading runnables…</Text>
  const needs = showCovered ? guide.needs : guide.needs.filter(n => !n.covered_by.length)
  const bindings = showCovered ? guide.bindings : guide.bindings.filter(b => !b.covered_by.length)
  const coveredCount = guide.needs.length + guide.bindings.length - (guide.needs.filter(n => !n.covered_by.length).length + guide.bindings.filter(b => !b.covered_by.length).length)
  const nothing = guide.needs.length === 0 && guide.bindings.length === 0
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, marginBottom: 6 }}>
        <Text strong style={{ fontSize: 14 }}>What your runnables need</Text>
        {coveredCount > 0 && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12 }}>
            <Switch size="small" checked={showCovered} onChange={setShowCovered} aria-label="Show set up" />
            Show set up ({coveredCount})
          </span>
        )}
      </div>
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 10 }}>
        From the runnables found on {guide.hosts.length ? hostSummary(guide.hosts, 4) : 'no connected host'}. A credential reaches a runnable when its label matches the names the runnable reads, so each <b>Add</b> fills in a label that matches.
      </Text>
      {nothing && (
        <Text type="secondary" style={{ fontSize: 13 }}>
          No runnable on a connected host reads a credential from the environment.
        </Text>
      )}
      {!nothing && needs.length === 0 && bindings.length === 0 && (
        <Text type="secondary" style={{ fontSize: 13 }}>
          <CheckCircleOutlined style={{ color: '#52c41a' }} /> Every credential your runnables read is set up.
        </Text>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {needs.map(n => <NeedCard key={n.prefix} need={n} onAdd={onAdd} />)}
        {bindings.map(b => <BindingCard key={`${b.runnable}:${b.arg}`} b={b} onAdd={onAdd} />)}
      </div>
    </div>
  )
}

/** The "used by" line under a saved credential's row. */
export function CredentialUsageLine({
  usage,
  onFix,
  onOpenTab,
}: {
  usage: CredentialGuide['usage'][string] | undefined
  onFix: (hint: { label: string; kind: Credential['kind'] }) => void
  onOpenTab?: (tab: string) => void
}) {
  if (!usage) return null
  const { runnables, features, hint } = usage
  if (!runnables.length && !features.length) {
    return (
      <div style={{ fontSize: 12, marginTop: 4, paddingLeft: 21 }}>
        <Text type="warning" style={{ fontSize: 12 }}>
          <WarningOutlined /> Not used by any runnable or feature.
        </Text>
        {hint && (
          <>
            {' '}<Text type="secondary" style={{ fontSize: 12 }}>Probably meant <b>{hint.label}</b> ({hint.kind}): {hint.why}.</Text>
            {' '}<Button size="small" type="link" style={{ padding: 0, height: 'auto', fontSize: 12 }} onClick={() => onFix(hint)}>Fix…</Button>
          </>
        )}
      </div>
    )
  }
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center', marginTop: 4, paddingLeft: 21 }}>
      <Text type="secondary" style={{ fontSize: 12 }}>Used by</Text>
      {runnables.map(r => (
        <Tooltip key={r.name} title={whyTitle(r)}>
          <Tag style={{ margin: 0, fontSize: 11 }}>{r.name} · {r.hosts.length === 1 ? r.hosts[0] : `${r.hosts.length} hosts`}</Tag>
        </Tooltip>
      ))}
      {features.map(f => (
        <Tag
          key={f.feature}
          color="blue"
          style={{ margin: 0, fontSize: 11, cursor: f.tab && onOpenTab ? 'pointer' : undefined }}
          onClick={f.tab && onOpenTab ? () => onOpenTab(f.tab) : undefined}
        >
          {f.feature}
        </Tag>
      ))}
    </div>
  )
}

function whyTitle(r: CredentialRunnableUse): string {
  const where = `on ${r.hosts.join(', ')}`
  if (r.via === 'env') return `${r.name} declares ${r.envs.join(', ')} (${where})`
  if (r.via === 'arg') return `${r.name} has the argument this credential binds (${where})`
  return `matches this credential's host/runnable scope (${where})`
}

/** Feature settings that point at a credential that's gone or has no secret. */
export function CredentialFeatureProblems({ features, onOpenTab }: { features: CredentialFeatureRef[]; onOpenTab?: (tab: string) => void }) {
  const bad = features.filter(f => f.status !== 'ok')
  if (!bad.length) return null
  return (
    <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 4 }}>
      {bad.map(f => (
        <Text key={`${f.feature}:${f.credential}`} type="warning" style={{ fontSize: 12 }}>
          <WarningOutlined /> {f.feature} uses credential <code>{f.credential}</code>, which {f.status === 'missing' ? "doesn't exist" : 'has no secret stored'}.
          {f.tab && onOpenTab && <> <Button size="small" type="link" style={{ padding: 0, height: 'auto', fontSize: 12 }} onClick={() => onOpenTab(f.tab)}>Open {f.feature.split(' (')[0]}</Button></>}
        </Text>
      ))}
    </div>
  )
}

/** Live "picked up by" preview in the credential dialog (debounced). */
export function CredentialMatchPreview({ draft }: { draft: Partial<Credential> }) {
  const [matches, setMatches] = useState<CredentialRunnableUse[] | null>(null)
  const key = JSON.stringify([draft.label, draft.kind, draft.username_arg, draft.secret_arg, draft.scope_hosts, draft.scope_runnables])
  useEffect(() => {
    let live = true
    const t = setTimeout(() => {
      bridge.credential_matches(draft)
        .then(m => { if (live) setMatches(m) })
        .catch(() => { if (live) setMatches(null) })
    }, 250)
    return () => { live = false; clearTimeout(t) }
  }, [key])
  if (matches === null) return null
  if (!matches.length) {
    return (
      <Text type="warning" style={{ fontSize: 12, display: 'block', marginTop: 4 }} data-testid="cred-match-none">
        <WarningOutlined /> No runnable found on a connected host reads these names, so it won't be picked up automatically. Check <b>What your runnables need</b> for the label to use.
      </Text>
    )
  }
  return (
    <div style={{ fontSize: 12, marginTop: 4 }} data-testid="cred-match-list">
      <Text type="secondary" style={{ fontSize: 12 }}>Picked up by </Text>
      {matches.map((m, i) => (
        <span key={m.name}>
          {i > 0 && ', '}
          <Tooltip title={whyTitle(m)}><b style={{ fontFamily: MONO_FONT }}>{m.name}</b></Tooltip>
          <Text type="secondary" style={{ fontSize: 12 }}> ({hostSummary(m.hosts)})</Text>
        </span>
      ))}
    </div>
  )
}
