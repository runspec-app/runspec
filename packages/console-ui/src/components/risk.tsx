/**
 * The risk ladder: one confirmation pattern per level of risk, so the same
 * kind of action asks the same way on every screen.
 *
 *   routine      Reversible, one place. No confirmation; the result shows
 *                next to the control.
 *   local        One place, but not simply undone: delete a saved item, stop
 *                a run, discard edits. The dialog, naming what goes; no
 *                targets list needed.
 *   fleet        Reaches many hosts, or turns a safety off, but can be put
 *                back. A dialog that names the exact action and every target,
 *                with Cancel focused so a reflexive Enter backs out.
 *   destructive  Can't be taken back: deletes data or removes a venv. The
 *                same dialog, with what will be lost shown first, and OK stays
 *                off until you type the confirmation word.
 *
 * Copying files onto hosts is fleet: the dialog lists every file and where it
 * goes, so you see what an overwrite would replace before it happens.
 *
 * Use `useRiskConfirm()` and render its holder once:
 *
 *   const [ask, riskHolder] = useRiskConfirm()
 *   if (!(await ask({ level: 'fleet', title: …, action: …, targets })).ok) return
 *
 * Deprovision's typed-name dialog predates this file and already follows the
 * destructive rule.
 */
import type { MouseEvent, ReactElement, ReactNode } from 'react'
import { cloneElement, useEffect, useRef, useState } from 'react'
import { Button, Checkbox, Input, Modal, Typography, message, theme } from 'antd'
import { ExclamationCircleOutlined, WarningOutlined } from '@ant-design/icons'
import { MONO_FONT, RADIUS, statusText, textMuted, useIsDark } from '../ThemeContext'

export type RiskLevel = 'routine' | 'local' | 'fleet' | 'destructive'

export interface RiskAsk {
  level: Exclude<RiskLevel, 'routine'>
  /** A question naming the action: "Block every runnable on 12 hosts?" */
  title: string
  /** Exactly what will happen: the command, the rule, the files. Shown in a
   *  monospace box so it reads as the thing being approved. */
  action: ReactNode
  /** Every host, venv or window it reaches. */
  targets?: string[]
  /** One sentence on the consequence and how to undo it, if you can. */
  consequence?: ReactNode
  /** The OK button's words: "Disable all", "Send to 14". */
  okText: string
  /** destructive only: the word to type. Defaults to the target count. */
  typeToConfirm?: string
  /** Offer "don't ask again" with this label (fleet only). */
  rememberLabel?: string
  /** The Cancel button's words, when "Cancel" would be ambiguous (e.g. for
   *  "Stop this run?": "Keep running"). */
  cancelText?: string
  /** OK in the danger colour (the default). False for an ask that doesn't
   *  remove or stop anything ("Discard and reload" isn't danger; "Delete" is). */
  danger?: boolean
}

export interface RiskAnswer { ok: boolean; remember: boolean }

const SHOWN_TARGETS = 12

/** "web-01, web-02 and 3 more". Pure, for tests. */
export function targetList(targets: string[], shown = SHOWN_TARGETS): string {
  if (targets.length <= shown) return targets.join(', ')
  return `${targets.slice(0, shown).join(', ')} and ${targets.length - shown} more`
}

/** What a destructive ask makes you type: the word given, else the one
 *  target's name, else the number of targets (never a bare "1", which is
 *  typed by reflex). */
export function confirmWord(ask: RiskAsk): string {
  if (ask.typeToConfirm) return ask.typeToConfirm
  const t = ask.targets ?? []
  if (t.length === 1) return t[0]
  return t.length ? String(t.length) : 'delete'
}

function RiskDialog({ ask, cover, onDone }: { ask: RiskAsk; cover: boolean; onDone: (a: RiskAnswer) => void }) {
  const isDark = useIsDark()
  const st = statusText(isDark)
  const { token } = theme.useToken()
  const [typed, setTyped] = useState('')
  const [remember, setRemember] = useState(false)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const typeRef = useRef<HTMLInputElement | null>(null)
  const destructive = ask.level === 'destructive'
  const word = confirmWord(ask)
  const ready = !destructive || typed.trim() === word

  // Cancel (or the type-to-confirm field) takes focus, never OK.
  useEffect(() => {
    const t = setTimeout(() => (destructive ? typeRef.current?.focus() : cancelRef.current?.focus()), 60)
    return () => clearTimeout(t)
  }, [destructive])

  const targets = ask.targets ?? []
  const cancel = () => onDone({ ok: false, remember: false })
  const title = (
    <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      {destructive
        ? <WarningOutlined aria-hidden style={{ color: st.error }} />
        : <ExclamationCircleOutlined aria-hidden style={{ color: st.warn }} />}
      {ask.title}
    </span>
  )
  const footer = (
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
      <Button ref={cancelRef} onClick={cancel}>{ask.cancelText ?? 'Cancel'}</Button>
      <Button type="primary" danger={ask.danger !== false} disabled={!ready} data-testid="risk-ok"
        icon={ask.danger !== false ? <WarningOutlined aria-hidden /> : undefined}
        onClick={() => onDone({ ok: true, remember })}>
        {ask.okText}
      </Button>
    </div>
  )
  const body = (
    <>
      <div data-testid="risk-action" style={{
        fontFamily: MONO_FONT, fontSize: 13, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        maxHeight: cover ? 72 : 200, overflow: 'auto', padding: '6px 10px', margin: '4px 0 8px',
        background: 'var(--rs-surface-sunken)', border: '1px solid var(--rs-border)', borderRadius: RADIUS.sm,
      }}>
        {ask.action}
      </div>
      {targets.length > 0 && (
        <div style={{ fontSize: 13, marginBottom: 10 }}>
          <span style={{ fontWeight: 600 }}>On {targets.length}:</span>{' '}
          <span style={{ fontFamily: MONO_FONT }}>{targetList(targets)}</span>
        </div>
      )}
      {ask.consequence && <div style={{ fontSize: 13, color: textMuted(isDark), marginBottom: 10 }}>{ask.consequence}</div>}
      {destructive && (
        <label style={{ display: 'block', fontSize: 13 }}>
          This can't be undone. Type <Typography.Text code>{word}</Typography.Text> to confirm.
          <Input ref={el => { typeRef.current = el?.input ?? null }} aria-label={`Type ${word} to confirm`}
            value={typed} onChange={e => setTyped(e.target.value)} style={{ marginTop: 6, fontFamily: MONO_FONT }}
            onPressEnter={() => { if (ready) onDone({ ok: true, remember: false }) }} />
        </label>
      )}
      {!destructive && ask.rememberLabel && (
        <Checkbox checked={remember} onChange={e => setRemember(e.target.checked)}>{ask.rememberLabel}</Checkbox>
      )}
    </>
  )

  if (cover) {
    // A small window (the Send to terminals bar) has no room for a dialog:
    // the ask covers the whole window instead, same content and focus rules.
    return (
      <div role="alertdialog" aria-modal="true" aria-label={ask.title}
        onKeyDown={e => { if (e.key === 'Escape') cancel() }}
        style={{
          position: 'fixed', inset: 0, zIndex: 1000, display: 'flex', flexDirection: 'column',
          padding: '8px 16px', background: token.colorBgElevated, color: token.colorText,
        }}>
        <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 2 }}>{title}</div>
        <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>{body}</div>
        <div style={{ paddingTop: 6 }}>{footer}</div>
      </div>
    )
  }
  return (
    <Modal open width={520} title={title} onCancel={cancel} footer={footer}>
      {body}
    </Modal>
  )
}

/** The ladder's confirmation, as a hook: `ask(...)` resolves when the person
 *  answers; render the holder once in the component. */
export function useRiskConfirm(opts: { cover?: boolean } = {}): [(ask: RiskAsk) => Promise<RiskAnswer>, ReactNode] {
  const [pending, setPending] = useState<{ ask: RiskAsk; resolve: (a: RiskAnswer) => void; opener: HTMLElement | null } | null>(null)
  // Remember what had focus (the button, or a field in the window underneath)
  // and give focus back when the ask closes. Otherwise it fell to <body>, and
  // Esc no longer closed the window the ask was opened from.
  const ask = (a: RiskAsk) => new Promise<RiskAnswer>(resolve => {
    const opener = document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null
    setPending({ ask: a, resolve, opener })
  })
  const holder = pending
    ? <RiskDialog ask={pending.ask} cover={!!opts.cover} onDone={a => {
        const opener = pending.opener
        pending.resolve(a)
        setPending(null)
        if (opener) setTimeout(() => { if (opener.isConnected) opener.focus() }, 0)
      }} />
    : null
  return [ask, holder]
}

/** The ladder's confirmation around one control, a drop-in for antd's
 *  Popconfirm: the child's click asks first, and `onConfirm` runs only on OK.
 *  Each instance holds its own dialog, so it works inside lists and table
 *  cells where a hook can't be called per row. Wrap the button itself, not a
 *  Tooltip around it (a Tooltip doesn't pass the click through). */
export function AskFirst({ ask, onConfirm, disabled, children }: {
  ask: RiskAsk
  onConfirm: () => void | Promise<void>
  disabled?: boolean
  children: ReactElement<{ onClick?: (e: MouseEvent<HTMLElement>) => void }>
}) {
  const [confirm, holder] = useRiskConfirm()
  const own = children.props.onClick
  return (
    <>
      {cloneElement(children, {
        onClick: async (e: MouseEvent<HTMLElement>) => {
          own?.(e)
          if (disabled) return
          const a = await confirm(ask)
          if (a.ok) await onConfirm()
        },
      })}
      {holder}
    </>
  )
}

// ── Remembered consent ─────────────────────────────────────────────────────
// The one way to skip a confirmation: you answer once and tick "don't ask
// again", which holds for that exact action on that exact set of targets. A
// different target set asks again. Kept per viewer in localStorage (a
// convenience; losing it only means being asked again). Settings →
// Confirmations lists each one, with Forget.

const CONSENT_STORE = 'runspec.consent'

/** One remembered answer: its key, what it was ("Run backup on prod-1") and
 *  when it was given. */
export interface Consent { key: string; label: string; at: number }

/** The key for one action on one set of targets: order-independent. Pure. */
export function consentKey(kind: string, targets: string[]): string {
  return `${kind}:${[...new Set(targets)].sort().join(',')}`
}

/** Every remembered answer, oldest first. Earlier versions stored bare keys;
 *  those read back with the key as their label. */
export function listConsents(): Consent[] {
  try {
    const v = JSON.parse(localStorage.getItem(CONSENT_STORE) ?? '[]')
    if (!Array.isArray(v)) return []
    return v.flatMap((x): Consent[] => {
      if (typeof x === 'string') return [{ key: x, label: x, at: 0 }]
      if (x && typeof x.key === 'string') return [{ key: x.key, label: String(x.label ?? x.key), at: Number(x.at) || 0 }]
      return []
    })
  } catch { return [] }
}

function writeConsents(all: Consent[]) {
  try { localStorage.setItem(CONSENT_STORE, JSON.stringify(all.slice(-200))) } catch { /* storage unavailable: it will just ask again */ }
}

/** A remembered answer holds for this long, then the confirmation asks again
 *  (and offers "don't ask again" afresh). */
export const CONSENT_DAYS = 30
const CONSENT_MS = CONSENT_DAYS * 24 * 60 * 60 * 1000

/** Still in force? Answers saved before expiry existed (no time) count as
 *  expired, so they ask once more. Pure, for tests. */
export function consentLive(c: Consent, now = Date.now()): boolean {
  return c.at > 0 && now - c.at < CONSENT_MS
}

export function hasConsent(key: string): boolean { return listConsents().some(c => c.key === key && consentLive(c)) }

export function rememberConsent(key: string, label = key): void {
  writeConsents([...listConsents().filter(c => c.key !== key), { key, label, at: Date.now() }])
}

/** Forget one remembered answer: that confirmation asks again. */
export function forgetConsent(key: string): void {
  writeConsents(listConsents().filter(c => c.key !== key))
}

/** How many answers are remembered. */
export function consentCount(): number { return listConsents().length }

export function forgetConsents(): void {
  try { localStorage.removeItem(CONSENT_STORE) } catch { /* nothing to forget */ }
}

/** What a remembered answer is called in Settings: the question, without its
 *  question mark, and where it went. Pure. */
export function consentLabel(a: RiskAsk): string {
  const what = a.title.replace(/\?$/, '')
  const where = a.targets?.length ? ` on ${targetList(a.targets, 4)}` : ''
  return what.includes(' on ') ? what : `${what}${where}`
}

/** Ask unless this exact action on these targets was remembered; offer
 *  "don't ask again" and remember a ticked OK. Destructive asks are never
 *  remembered. Resolves true to go ahead. */
export async function askRemembered(
  ask: (a: RiskAsk) => Promise<RiskAnswer>, key: string, a: RiskAsk,
): Promise<boolean> {
  const rememberable = a.level !== 'destructive'
  if (rememberable && hasConsent(key)) {
    // Skipping a confirmation is said, not silent: the operator sees that a
    // remembered answer acted for them, and where to take it back.
    message.open({
      key: `consent-${key}`, type: 'info', duration: 5,
      content: `${consentLabel(a)}: done without asking, because you chose "don't ask again" (Settings → Confirmations).`,
    })
    return true
  }
  const r = await ask(rememberable ? { ...a, rememberLabel: a.rememberLabel ?? "Don't ask again for this" } : a)
  if (r.ok && r.remember && rememberable) rememberConsent(key, consentLabel(a))
  return r.ok
}

/** Settings → Confirmations: every remembered answer, each with Forget, and
 *  one button to make everything ask again. */
export function RememberedAnswers() {
  const isDark = useIsDark()
  const [list, setList] = useState(() => listConsents().filter(c => consentLive(c)))
  const refresh = () => setList(listConsents().filter(c => consentLive(c)))
  const until = (c: Consent) => new Date(c.at + CONSENT_MS).toLocaleDateString()
  if (!list.length) {
    return <div style={{ fontSize: 13, color: textMuted(isDark) }}>Every confirmation asks. Tick "don't ask again" on one to skip it next time.</div>
  }
  return (
    <div data-testid="remembered-answers" style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13 }}>
      {list.slice().reverse().map(c => (
        <div key={c.key} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={c.label}>{c.label}</span>
          <span style={{ fontSize: 12, color: textMuted(isDark), flexShrink: 0 }}>until {until(c)}</span>
          <Button size="small" aria-label={`Forget: ${c.label}`} onClick={() => { forgetConsent(c.key); refresh() }}>Forget</Button>
        </div>
      ))}
      <div style={{ marginTop: 6 }}>
        <Button size="small" onClick={() => { forgetConsents(); refresh() }}>Ask every time again</Button>
      </div>
    </div>
  )
}
