import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Drawer, Empty, List, Popconfirm, Tag, Tooltip, Typography, message } from 'antd'
import { CloseOutlined, LoadingOutlined, PlusOutlined, ClockCircleOutlined, HistoryOutlined, DeleteOutlined } from '@ant-design/icons'
import { bridge, type ChatConversation, type ConversationMeta, type InFlightRecord, type Runnable, type TriggerCard } from '../bridge'
import { useFeatures } from '../features'
import { OutputPanel, useInvocationBlocks, type RerunData, type InvocationBlock, type BlockSegment } from '../components/OutputPanel'
import { useIsDark, MONO_FONT, RADIUS } from '../ThemeContext'
import { useSessions } from '../useSessions'
import { SessionStrip } from '../components/SessionStrip'
import { cardLabel, sessionStatus, type Session } from '../sessions'
import { READ_ONLY_SESSION_HINT } from '../components/CommandInput'

const { Text } = Typography

// A finished rule run's result text → run-block lines + exit code. The autonomy
// gate prefixes a non-zero exit as "[exit N]" on the first line. Shared by the
// live trigger_card reconcile and the finished trigger_note mirror.
function runBodyToLines(
  blockId: string,
  text: string,
): { lines: { id: string; line: string; stream: 'stdout' }[]; exitCode: number } {
  let body = text || ''
  let exitCode = 0
  const m = /^\[exit (\d+)\]\n?/.exec(body)
  if (m) {
    exitCode = Number(m[1])
    body = body.slice(m[0].length)
  }
  const lines = body
    ? body.split('\n').map(line => ({ id: blockId, line, stream: 'stdout' as const }))
    : []
  return { lines, exitCode }
}

// Schedule cards whose run is a runnable invocation (not an agent turn) render
// as run blocks filled from the card's terminal result, like rule triggers.
const SCHEDULE_RUN_TYPES: ReadonlySet<string> = new Set(['runnable', 'saved_run', 'eval'])

// Whether a run card is a plain run (run block) or an agent turn (chat block).
function cardIsRun(card: TriggerCard, kind: 'trigger' | 'schedule'): boolean {
  if (kind === 'schedule') return SCHEDULE_RUN_TYPES.has(card.event_type)
  return String(card.detail?.mode ?? 'agent') === 'rule'
}

// The Console block label for a run card. Trigger cards share the chip label
// (`/tool` for a rule run, "Trigger <name> · <event>" for an agent run); a
// schedule run reads best as its summary ("/backup-db on db01").
function cardBlockLabel(card: TriggerCard, kind: 'trigger' | 'schedule'): string {
  if (kind === 'schedule' && card.summary) return card.summary
  return cardLabel(card, kind)
}

// Render a saved conversation's provider-agnostic transcript back into chat
// blocks the OutputPanel can show — one block per turn (label = the user's
// message; tool calls + the assistant's prose as segments). Read-only: every
// block is `done`, nothing streams. Every block belongs to the session whose
// id == the conversation id (the backend resumes it under that id).
export function conversationToBlocks(conv: ChatConversation): InvocationBlock[] {
  return (conv.turns || []).map((t, i) => {
    const startedAt = t.ts || conv.updated || new Date().toISOString()
    // An operator-initiated manual run — render it as a read-only run card
    // (reusing the runnable-run renderer), tagged so it's clearly the operator's
    // own action and marked as never part of the agent's context.
    if (t.kind === 'operator_run') {
      const lines = String(t.output || '')
        .split('\n')
        .map((line, j) => ({ id: `${conv.id}:${i}:${j}`, line, stream: 'stdout' as const }))
      return {
        id: `${conv.id}:${i}`,
        sessionId: conv.id,
        type: 'run' as const,
        label: `/${t.runnable || 'run'}${t.host ? ` on ${t.host}` : ''}`,
        startedAt,
        lines,
        segments: [],
        currentText: '',
        done: true,
        exitCode: typeof t.exit_code === 'number' ? t.exit_code : undefined,
        durationMs: typeof t.duration_ms === 'number' ? t.duration_ms : undefined,
        operatorOnly: true,
        inContext: false,
      }
    }
    const segments: BlockSegment[] = []
    for (const tool of t.tools || []) {
      segments.push({ kind: 'tool', entry: { name: tool.name, input: tool.input || {}, output: tool.output, running: false, operatorDecision: tool.operator_decision } })
    }
    if (t.assistant && t.assistant.trim()) segments.push({ kind: 'text', text: t.assistant })
    const u = (t.usage || undefined) as Record<string, number> | undefined
    return {
      id: `${conv.id}:${i}`,
      sessionId: conv.id,
      type: 'chat' as const,
      label: t.user || '(no message)',
      startedAt,
      lines: [],
      segments,
      currentText: '',
      done: true,
      model: t.model,
      tokenUsage: u ? { input: u.input || 0, output: u.output || 0, cacheRead: u.cache_read || 0, cacheCreation: u.cache_creation || 0 } : undefined,
    }
  })
}

function fmtWhen(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

interface ConsoleViewProps {
  inFlight: InFlightRecord[]
  pendingChat?: string | null
  onChatSent?: () => void
}

function elapsed(startedAt: string): string {
  const ms = Date.now() - new Date(startedAt).getTime()
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  return `${m}m ${s % 60}s`
}

function InFlightStrip({ inFlight }: { inFlight: InFlightRecord[] }) {
  const isDark = useIsDark()
  const [, setTick] = useState(0)
  const ref = useRef<ReturnType<typeof setInterval> | undefined>(undefined)
  useEffect(() => {
    ref.current = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(ref.current)
  }, [])

  if (inFlight.length === 0) return null

  const bg     = isDark ? '#0d1f0d' : '#f6ffed'
  const border = isDark ? '#1a3a1a' : '#b7eb8f'
  const nameCol = isDark ? '#d4d4d4' : '#1a1a1a'

  const handleCancel = async (id: string) => {
    await bridge.cancel_invocation(id)
  }

  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap', gap: 8,
      padding: '8px 12px',
      background: bg, border: `1px solid ${border}`, borderRadius: RADIUS.sm,
      marginBottom: 4,
    }}>
      {inFlight.map(r => {
        // Built-in agent tools (repo_*, outlook_*, denylist, …) show their label
        // verbatim ("repo_sync: runspec"); a runnable run keeps the /name form.
        const isTool = r.kind === 'builtin_tool'
        return (
        <span key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <LoadingOutlined style={{ color: '#52c41a', fontSize: 12 }} spin />
          <Text style={{ fontFamily: MONO_FONT, fontSize: 13, color: nameCol }}>{isTool ? r.runnable : `/${r.runnable}`}</Text>
          {isTool && (
            <Tag color="green" style={{ fontSize: 12, margin: 0 }}>agent</Tag>
          )}
          {r.trigger === 'schedule' && (
            <Tooltip title="Ran on a schedule">
              <ClockCircleOutlined style={{ color: 'var(--rs-text-muted)', fontSize: 12 }} />
            </Tooltip>
          )}
          {r.host && <Tag style={{ fontSize: 12, margin: 0 }}>{r.host}</Tag>}
          {r.group && <Tag color="blue" style={{ fontSize: 12, margin: 0 }}>{r.group}</Tag>}
          <Text type="secondary" style={{ fontSize: 12 }}>{r.operator}</Text>
          <Text style={{ fontSize: 12, color: '#52c41a' }}>{elapsed(r.startedAt)}</Text>
          <Popconfirm
            title="Cancel this run?"
            onConfirm={() => handleCancel(r.id)}
            okText="Cancel run" okButtonProps={{ danger: true }}
            cancelText="Keep running"
            placement="bottom"
          >
            <Button size="small" type="text" danger icon={<CloseOutlined style={{ fontSize: 11 }} />}
              style={{ padding: '0 4px', height: 18, lineHeight: 1 }} />
          </Popconfirm>
        </span>
        )
      })}
    </div>
  )
}

export function ConsoleView({ inFlight, pendingChat, onChatSent }: ConsoleViewProps) {
  const { blocks, addBlock, upsertBlock, updateBlock, clearBlocks, hydrateBlocks, removeSession, setBlockInContext } = useInvocationBlocks()
  // `[console] ai` off-switch: the Console stays (it is the output pane for every
  // run) but its chat affordances — conversation History, "Ask the assistant",
  // the include-in-context toggle — go.
  const { ai } = useFeatures()
  const addBlockRef = useRef(addBlock)
  addBlockRef.current = addBlock
  // upsert/update keyed by id — used to render live trigger runs (which arrive as
  // runspec:trigger_card events) without duplicating, and to fold their finished
  // mirror onto the same block. Kept in refs so the []-dep listener effects don't
  // close over stale callbacks.
  const upsertBlockRef = useRef(upsertBlock)
  upsertBlockRef.current = upsertBlock
  const updateBlockRef = useRef(updateBlock)
  updateBlockRef.current = updateBlock
  // Current blocks, for the send handlers (which live in []-dep effects and would
  // otherwise close over a stale array) — used to gather ticked run cards.
  const blocksRef = useRef(blocks)
  blocksRef.current = blocks
  // Run-card ids whose output has already been folded into a sent chat message,
  // so a ticked result rides along exactly once (not re-injected every turn).
  const stagedRef = useRef<Set<string>>(new Set())

  // A trigger / schedule fired: mirror its run into the Console as a live block
  // so all agent activity — and its Approve/Deny gate — has one home (the
  // Console tab), rather than living only in the Triggers / Schedules tabs
  // (hidden when AI is off, leaving a confirm-gated run unapprovable). The
  // block is keyed by card_id, which is also the run's session id and its
  // chat id, so the already-global streaming/confirm handlers in
  // useInvocationBlocks attribute token/tool/run_end/tool_confirm to it, and
  // the chip row shows it as its own read-only session. First sighting creates
  // the block; later card events reconcile terminal state only —
  // segments/currentText belong to the streamed handlers.
  const applyCard = useCallback((card: TriggerCard, kind: 'trigger' | 'schedule') => {
    if (!card?.card_id) return
    const id = card.card_id
    const isRun = cardIsRun(card, kind)
    upsertBlockRef.current(id, () => ({
      id,
      sessionId: id,
      type: isRun ? 'run' : 'chat',
      label: cardBlockLabel(card, kind),
      startedAt: new Date((card.created_at || Date.now() / 1000) * 1000).toISOString(),
      lines: [],
      segments: [],
      currentText: '',
      done: false,
      trigger: { name: card.trigger, source: card.source },
    }))
    if (card.column === 'done' || card.column === 'failed') {
      const failed = card.column === 'failed'
      updateBlockRef.current(id, b => {
        const patch: Partial<InvocationBlock> = { done: true, exitCode: failed ? 1 : 0 }
        // A plain run whose output never streamed line-by-line — fill from the
        // card's terminal result/error. Agent blocks keep their streamed
        // segments (run_end, keyed by the same id, already marked them done).
        if (b.type === 'run' && b.lines.length === 0) {
          if (failed) {
            const err = String(card.detail?.error ?? '')
            patch.lines = err ? [{ id, line: err, stream: 'stderr' as const }] : []
          } else {
            const { lines, exitCode } = runBodyToLines(id, String(card.detail?.result ?? ''))
            patch.lines = lines
            patch.exitCode = exitCode
          }
        }
        return { ...b, ...patch }
      })
    }
  }, [])

  // The live session list + selection (chip row). Seeds from the backend and
  // both run boards (cold start replays every card through applyCard), folds
  // the runspec:session / *_card events, and drops a closed session's blocks.
  const sessionsApi = useSessions({ blocksRef, onSessionRemoved: removeSession, onCard: applyCard })
  const { sessions, selectedId, selected, pendingConfirmIds, select, newChat, close, ensureInteractive, adopt } = sessionsApi
  const sessionsRef = useRef(sessionsApi)
  sessionsRef.current = sessionsApi
  // The transcript shows the selected session's blocks only — so the activity
  // indicator, the pending confirm gates and the composer's Stop/Send are per
  // session by construction.
  const visible = selectedId ? blocks.filter(b => b.sessionId === selectedId) : []

  // Build the context preamble from run cards the operator ticked "in context"
  // that haven't been staged yet, marking them staged. Returns '' when none —
  // the passive sibling of the "Ask the assistant" button: instead of firing an
  // immediate turn, a ticked run result rides along with the next typed message.
  const buildStagedPreamble = (): string => {
    const parts: string[] = []
    for (const b of blocksRef.current) {
      if (b.type !== 'run' || b.inContext !== true || stagedRef.current.has(b.id)) continue
      const raw = b.lines.map(l => l.line).join('\n').trim()
      if (!raw) continue
      parts.push(`--- Output from ${b.label} ---\n${raw}`)
      stagedRef.current.add(b.id)
    }
    if (!parts.length) return ''
    return `Context from tools I ran (for reference):\n\n${parts.join('\n\n')}\n\n`
  }
  const buildStagedPreambleRef = useRef(buildStagedPreamble)
  buildStagedPreambleRef.current = buildStagedPreamble

  // Saved-conversation browser (chat history) state.
  const [convOpen, setConvOpen] = useState(false)
  const [convs, setConvs] = useState<ConversationMeta[]>([])
  const [convLoading, setConvLoading] = useState(false)
  // The reopened-and-resumed conversation, if any — new messages continue it.
  const [viewing, setViewing] = useState<{ id: string; title: string } | null>(null)

  const refreshConvs = async () => {
    setConvLoading(true)
    try {
      setConvs(await bridge.list_conversations())
    } catch {
      setConvs([])
    } finally {
      setConvLoading(false)
    }
  }

  const openConversations = async () => {
    setConvOpen(true)
    await refreshConvs()
  }

  // Reopen a saved conversation: resume it on the backend as a session whose
  // id == the conversation id (history rebuilt as text), render its transcript
  // into that session and select it. Already open ⇒ just select it (its live
  // blocks are the truth — don't re-hydrate over them).
  const openConversation = async (meta: ConversationMeta) => {
    try {
      const res = await bridge.resume_conversation(meta.id)
      if (!res.ok) { message.error(res.error || 'Could not resume conversation'); return }
      const sid = res.session || meta.id
      if (res.already_open && sessionsRef.current.sessions.some(s => s.id === sid)) {
        select(sid)
        setConvOpen(false)
        return
      }
      const conv = await bridge.load_conversation(meta.id)
      hydrateBlocks(conversationToBlocks(conv).map(b => ({ ...b, sessionId: sid })), sid)
      adopt({
        id: sid, kind: 'chat', label: conv.title || meta.title, interactive: true,
        created_at: Date.now() / 1000, state: 'idle', turn: '', turns: conv.turns?.length ?? 0,
      })
      setViewing({ id: meta.id, title: conv.title || meta.title })
      setConvOpen(false)
      if (res.compacted) {
        message.info(res.summarized
          ? 'Resumed — older turns were summarized to fit context (the saved transcript is unchanged).'
          : 'Resumed — older turns were condensed to fit context (the saved transcript is unchanged).')
      }
    } catch {
      message.error('Could not open conversation')
    }
  }

  const deleteConversation = async (id: string) => {
    await bridge.delete_conversation(id)
    // The deleted conversation's live session (same id) goes with it.
    if (sessionsRef.current.sessions.some(s => s.id === id)) await close(id)
    if (viewing?.id === id) setViewing(null)
    await refreshConvs()
  }

  // Open another interactive session and switch to it. The previous session
  // keeps running (its turn, its blocks, its chip) — nothing is cleared.
  const handleNewChat = async () => {
    await newChat()
    setViewing(null)
  }

  // AI off: the Console is the run-output pane; "Clear" replaces the session
  // (backend) and drops its blocks. Without a session, just empty the pane.
  const handleClear = async () => {
    const sid = selectedId
    if (!sid) { clearBlocks(); return }
    try {
      const res = await bridge.clear_chat(sid)
      removeSession(res?.replaced || sid)
      if (res?.session) adopt(res.session)
    } catch {
      removeSession(sid)
    }
  }

  // Tell the command bar which chat turn (if any) is in flight in the selected
  // session, so its send button can become a Stop control while the assistant
  // is working — and whether that session is a read-only automation one.
  const activeChatId = visible.find(b => b.type === 'chat' && !b.done)?.id ?? null
  const readOnly = !!selected && !selected.interactive
  useEffect(() => {
    window.dispatchEvent(new CustomEvent('runspec:chat_active', { detail: { id: activeChatId, session: selectedId, readOnly } }))
  }, [activeChatId, selectedId, readOnly])

  // Where operator work (runs, reruns, saved runs, runbooks) lands: the current
  // interactive session, auto-created and selected when there is none.
  const targetSession = async (): Promise<string | null> => {
    const sid = await sessionsRef.current.ensureInteractive()
    if (!sid) message.error('Could not open a chat session')
    return sid
  }

  // Invoked from the App-level command bar
  useEffect(() => {
    const onInvoke = async (e: Event) => {
      const { runnable, args, commandPath = [], credentialIds } = (e as CustomEvent).detail as { runnable: Runnable; args: Record<string, unknown>; commandPath?: string[]; credentialIds?: string[] }
      const cmd = commandPath.length > 0 ? `${runnable.name} ${commandPath.join(' ')}` : runnable.name
      const label = `/${cmd} on ${runnable.host}`
      const sessionId = await targetSession()
      if (!sessionId) return
      const id = await bridge.invoke_runnable(runnable.host, runnable.name, args, commandPath, runnable.group, credentialIds && credentialIds.length ? credentialIds : undefined)
      addBlockRef.current({ id, sessionId, type: 'run', label, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false, rerunData: { host: runnable.host, runnable: runnable.name, args, commandPath } })
      window.dispatchEvent(new CustomEvent('runspec:in_flight_updated'))
    }
    window.addEventListener('runspec:invoke_runnable', onInvoke)
    return () => window.removeEventListener('runspec:invoke_runnable', onInvoke)
  }, [])

  // Chat sent from the App-level command bar — into the selected interactive
  // session (created if there is none). An automation session is read-only:
  // the composer already refuses; this is the belt to that brace.
  useEffect(() => {
    const onChat = async (e: Event) => {
      const { message: text } = (e as CustomEvent).detail as { message: string }
      const sel = sessionsRef.current.selected
      if (sel && !sel.interactive) { message.info(READ_ONLY_SESSION_HINT, 2); return }
      const sessionId = sel?.id ?? await sessionsRef.current.newChat()
      if (!sessionId) { message.error('Could not open a chat session'); return }
      // Fold in any ticked run-card output; the card label stays the typed
      // message so the transcript reads cleanly.
      const outgoing = buildStagedPreambleRef.current() + text
      const id = await bridge.send_chat(outgoing, undefined, sessionId)
      addBlockRef.current({ id, sessionId, type: 'chat', label: text, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false })
      window.dispatchEvent(new CustomEvent('runspec:in_flight_updated'))
    }
    window.addEventListener('runspec:send_chat', onChat)
    return () => window.removeEventListener('runspec:send_chat', onChat)
  }, [])

  // A runbook fired from the `$` menu's run modal: the bridge renders the body with
  // the operator's values + tags the turn with the runbook, then runs it like a chat
  // turn. We show the rendered message as the chat block label.
  useEffect(() => {
    const onRunRunbook = async (e: Event) => {
      const { runbookId, values, extra } = (e as CustomEvent).detail as {
        runbookId: string; values: Record<string, unknown>; extra: string
      }
      const sessionId = await targetSession()
      if (!sessionId) return
      const res = await bridge.run_runbook(runbookId, values, extra, sessionId)
      if (res.error || !res.chat_id) {
        message.error(res.error || 'Could not start runbook')
        return
      }
      addBlockRef.current({ id: res.chat_id, sessionId, type: 'chat', label: res.message || `$${runbookId}`, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false })
      window.dispatchEvent(new CustomEvent('runspec:in_flight_updated'))
    }
    window.addEventListener('runspec:run_runbook', onRunRunbook)
    return () => window.removeEventListener('runspec:run_runbook', onRunRunbook)
  }, [])

  // A saved run fired from the Forms lane / run modal: the bridge validates the
  // param values, resolves the preset's stored targeting (group fan-out, hosts
  // failover, selected hosts), and invokes — returning one {host, inv_id} per
  // run. We render one run block per host; output streams to each via the
  // normal runspec:output / runspec:run_end routing (the bridge pre-allocated
  // the ids).
  useEffect(() => {
    const onRunSavedRun = async (e: Event) => {
      const { id, values } = (e as CustomEvent).detail as {
        id: string; values: Record<string, unknown>
      }
      const sessionId = await targetSession()
      if (!sessionId) return
      const res = await bridge.run_saved_run(id, values)
      if (res.error || !res.runs?.length) {
        message.error(res.error || 'Could not start saved run')
        return
      }
      for (const run of res.runs) {
        addBlockRef.current({
          id: run.inv_id, sessionId, type: 'run',
          label: `/${res.runnable} on ${run.host}`,
          startedAt: new Date().toISOString(),
          lines: [], segments: [], currentText: '', done: false,
        })
      }
      window.dispatchEvent(new CustomEvent('runspec:in_flight_updated'))
    }
    window.addEventListener('runspec:run_saved_run', onRunSavedRun)
    return () => window.removeEventListener('runspec:run_saved_run', onRunSavedRun)
  }, [])

  // The finished post_to_chat mirror (runspec:trigger_note). With the live block
  // already present (same id — the firing card's), fold this onto it only if it's
  // empty (a rule run whose lines never streamed); an agent block that already
  // streamed segments is left as-is. When no live block exists (card evicted, or
  // a legacy caller that passed no card_id), create the finished mirror as before.
  useEffect(() => {
    const onTriggerNote = (e: Event) => {
      const { id, kind, label, text } = (e as CustomEvent).detail as {
        id?: string; kind?: string; label?: string; text?: string
      }
      if (!text && !label) return
      const blockId = id || `trigger-${Date.now()}`
      if (blocksRef.current.some(b => b.id === blockId)) {
        updateBlockRef.current(blockId, b => {
          if (kind === 'run' && b.type === 'run' && b.lines.length === 0) {
            const { lines, exitCode } = runBodyToLines(blockId, text || '')
            return { ...b, lines, done: true, exitCode }
          }
          return { ...b, done: true }
        })
        return
      }
      // No live session for this mirror — it lands in the current chat.
      void (async () => {
        const sessionId = await sessionsRef.current.ensureInteractive()
        if (!sessionId) return
        const startedAt = new Date().toISOString()
        if (kind === 'run') {
          const { lines, exitCode } = runBodyToLines(blockId, text || '')
          addBlockRef.current({
            id: blockId, sessionId, type: 'run', label: label || 'trigger run',
            startedAt, lines, segments: [], currentText: '',
            done: true, exitCode,
          })
        } else {
          addBlockRef.current({
            id: blockId, sessionId, type: 'chat', label: label || 'trigger',
            startedAt, lines: [], segments: [{ kind: 'text', text: text || '' }],
            currentText: '', done: true,
          })
        }
      })()
    }
    window.addEventListener('runspec:trigger_note', onTriggerNote)
    return () => window.removeEventListener('runspec:trigger_note', onTriggerNote)
  }, [])

  // Rerun dispatched by History → App → here
  useEffect(() => {
    const onRerun = async (e: Event) => {
      const { host, runnable, args, commandPath = [] } = (e as CustomEvent).detail as RerunData
      const cmd = commandPath.length > 0 ? `${runnable} ${commandPath.join(' ')}` : runnable
      const label = `/${cmd} on ${host} (rerun)`
      const sessionId = await targetSession()
      if (!sessionId) return
      const id = await bridge.invoke_runnable(host, runnable, args, commandPath)
      addBlockRef.current({ id, sessionId, type: 'run', label, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false, rerunData: { host, runnable, args, commandPath } })
      window.dispatchEvent(new CustomEvent('runspec:in_flight_updated'))
    }
    window.addEventListener('runspec:rerun', onRerun)
    return () => window.removeEventListener('runspec:rerun', onRerun)
  }, [])

  // Ask LLM from History / a card's "Ask the assistant": a deliberate chat
  // send, so it goes to the current interactive session (switching to it).
  const sendChat = async (message: string) => {
    const sessionId = await targetSession()
    if (!sessionId) return
    const outgoing = buildStagedPreambleRef.current() + message
    const id = await bridge.send_chat(outgoing, undefined, sessionId)
    addBlockRef.current({ id, sessionId, type: 'chat', label: message, startedAt: new Date().toISOString(), lines: [], segments: [], currentText: '', done: false })
  }
  const sendChatRef = useRef(sendChat)
  sendChatRef.current = sendChat

  useEffect(() => {
    if (pendingChat) {
      sendChatRef.current(pendingChat)
      onChatSent?.()
    }
  }, [pendingChat, onChatSent])

  // These three are handed to the memoised BlockCard (via OutputPanel), so they
  // are useCallback-stabilised — otherwise every streamed frame that re-renders
  // ConsoleView would hand each card a fresh callback and defeat the memo. Their
  // closures are all stable (window dispatch, a ref, the stabilised
  // setBlockInContext + blocksRef).
  const handleRerun = useCallback((data: RerunData) => {
    window.dispatchEvent(new CustomEvent('runspec:rerun', { detail: data }))
  }, [])

  const handleAskLlm = useCallback((text: string) => sendChatRef.current(text), [])

  // The card's "in context" checkbox. Update local state, then apply the effect:
  // a chat card toggles the backend's context filter (reversible); a run card is
  // passive — ticking just stages its output into the next chat send.
  const handleToggleContext = useCallback((id: string, inContext: boolean) => {
    setBlockInContext(id, inContext)
    const blk = blocksRef.current.find(b => b.id === id)
    if (blk?.type === 'chat') {
      bridge.set_turn_included(id, inContext).catch(() => message.error('Could not update context'))
    }
  }, [setBlockInContext])

  // Chip status: a parked confirm on one of the session's blocks (or a card
  // column that needs a human) ⇒ waiting; an in-flight block ⇒ running.
  const statusOf = useCallback((s: Session) => sessionStatus(s, blocks, pendingConfirmIds), [blocks, pendingConfirmIds])
  const hasBlocks = useCallback((id: string) => blocks.some(b => b.sessionId === id), [blocks])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0, gap: 8 }}>
      <InFlightStrip inFlight={inFlight} />
      <div style={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 6 }}>
        <SessionStrip
          sessions={sessions}
          selectedId={selectedId}
          statusOf={statusOf}
          hasBlocks={hasBlocks}
          onSelect={select}
          onClose={id => { void close(id) }}
        />
        {viewing && viewing.id === selectedId && (
          <Tag color="blue" style={{ fontSize: 12, maxWidth: '40%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flexShrink: 0 }}>
            Resumed: {viewing.title} — your next message continues it
          </Tag>
        )}
        {visible.length > 0 && (
          <>
            <Button
              size="small" type="text"
              onClick={() => window.dispatchEvent(new CustomEvent('runspec:collapse_all', { detail: { collapsed: true } }))}
              style={{ fontSize: 12, color: 'var(--rs-text-muted)' }}
            >
              Collapse all
            </Button>
            <Button
              size="small" type="text"
              onClick={() => window.dispatchEvent(new CustomEvent('runspec:collapse_all', { detail: { collapsed: false } }))}
              style={{ fontSize: 12, color: 'var(--rs-text-muted)' }}
            >
              Expand all
            </Button>
          </>
        )}
        {ai && (
          <Tooltip title="Browse and reopen past conversations">
            <Button size="small" type="text" icon={<HistoryOutlined />} onClick={openConversations} style={{ fontSize: 12, color: 'var(--rs-text-muted)' }}>
              History
            </Button>
          </Tooltip>
        )}
        {ai ? (
          <Tooltip title="Open another chat — this one keeps running">
            <Button size="small" icon={<PlusOutlined />} onClick={handleNewChat} style={{ flexShrink: 0 }}>
              New chat
            </Button>
          </Tooltip>
        ) : visible.length > 0 && (
          <Tooltip title="Clear the run output">
            <Button size="small" icon={<PlusOutlined />} onClick={handleClear} style={{ flexShrink: 0 }}>
              Clear
            </Button>
          </Tooltip>
        )}
      </div>
      <OutputPanel blocks={visible} onRerun={handleRerun} onAskLlm={ai ? handleAskLlm : undefined} onToggleContext={ai ? handleToggleContext : undefined} />

      <Drawer
        title="Conversations"
        placement="right"
        width={420}
        open={convOpen}
        onClose={() => setConvOpen(false)}
        styles={{ body: { padding: '8px 0' } }}
      >
        {convs.length === 0 && !convLoading ? (
          <Empty description="No saved conversations yet" style={{ marginTop: 40 }} />
        ) : (
          <List
            loading={convLoading}
            dataSource={convs}
            renderItem={(c) => (
              <List.Item
                style={{ padding: '8px 16px', cursor: 'pointer' }}
                onClick={() => openConversation(c)}
                actions={[
                  <Popconfirm
                    key="del"
                    title="Delete this conversation?"
                    onConfirm={(e) => { e?.stopPropagation(); deleteConversation(c.id) }}
                    onCancel={(e) => e?.stopPropagation()}
                    okText="Delete" okButtonProps={{ danger: true }}
                  >
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={(e) => e.stopPropagation()} />
                  </Popconfirm>,
                ]}
              >
                <List.Item.Meta
                  title={
                    <Text
                      style={{ fontSize: 14 }}
                      editable={{
                        onChange: (v) => { const t = v.trim(); if (t && t !== c.title) { bridge.rename_conversation(c.id, t).then(refreshConvs) } },
                        triggerType: ['icon'],
                      }}
                    >
                      {c.title || 'Untitled'}
                    </Text>
                  }
                  description={
                    <div>
                      <Text type="secondary" style={{ fontSize: 12 }} ellipsis>{c.preview}</Text>
                      <div style={{ fontSize: 11, color: 'var(--rs-text-muted)', marginTop: 2 }}>
                        {fmtWhen(c.updated)} · {c.turn_count} message{c.turn_count === 1 ? '' : 's'}
                      </div>
                    </div>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Drawer>
    </div>
  )
}
