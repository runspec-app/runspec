import { useCallback, useEffect, useRef, useState } from 'react'
import type { TransferEnd, TransferProgress } from '../../bridge'

// One copy in the Transfers strip. Progress and the end come over the
// runspec:transfer_progress / runspec:transfer_end events, matched by id.
export interface TransferItem {
  id: string
  title: string
  route: string
  filter?: string
  // Where the copy lands, so the pane showing that folder refreshes and
  // selects the new files when it finishes.
  destKey?: string
  destDir?: string
  names?: string[]
  startedAt: number
  file?: string
  index?: number
  count?: number
  transferred: number
  total: number
  indeterminate?: boolean
  hosts?: Record<string, { transferred: number; total: number; done?: boolean; ok?: boolean; error?: string | null }>
  done?: boolean
  ok?: boolean
  error?: string | null
  errors: string[]
}

// How many finished rows to keep before the oldest drops off.
const KEEP_DONE = 8

export function useTransfers(onFinished?: (item: TransferItem) => void) {
  const [items, setItems] = useState<TransferItem[]>([])
  const finished = useRef(onFinished)
  finished.current = onFinished
  // An end can, in principle, beat the start call's reply back to us: keep it
  // until add() learns the id.
  const known = useRef(new Set<string>())
  const early = useRef(new Map<string, TransferEnd>())

  useEffect(() => {
    const onProgress = (e: Event) => {
      const d = (e as CustomEvent).detail as TransferProgress
      setItems(prev => prev.map(it => {
        if (it.id !== d.id) return it
        if (d.error) return { ...it, errors: [...it.errors, `${d.host ? `${d.host} · ` : ''}${d.file}: ${d.error}`] }
        if (d.host) {
          return { ...it, hosts: { ...(it.hosts ?? {}), [d.host]: { transferred: d.transferred, total: d.total } } }
        }
        return { ...it, file: d.file, index: d.index, count: d.count, transferred: d.transferred, total: d.total, indeterminate: d.indeterminate }
      }))
    }
    const onEnd = (e: Event) => {
      const d = (e as CustomEvent).detail as TransferEnd
      if (!known.current.has(d.id)) { early.current.set(d.id, d); return }
      applyEnd(d)
    }
    window.addEventListener('runspec:transfer_progress', onProgress)
    window.addEventListener('runspec:transfer_end', onEnd)
    return () => {
      window.removeEventListener('runspec:transfer_progress', onProgress)
      window.removeEventListener('runspec:transfer_end', onEnd)
    }
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  const applyEnd = (d: TransferEnd) => {
    setItems(prev => {
        let hit: TransferItem | null = null
        const next = prev.map(it => {
          if (it.id !== d.id) return it
          const hosts = d.hosts
            ? Object.fromEntries(d.hosts.map(r => [r.host, { ...(it.hosts?.[r.host] ?? { transferred: 0, total: 0 }), done: true, ok: r.ok, error: r.error }]))
            : it.hosts
          hit = { ...it, hosts, done: true, ok: d.ok, error: d.error }
          return hit
        })
        if (hit) queueMicrotask(() => finished.current?.(hit as TransferItem))
        return trim(next)
      })
  }

  const add = useCallback((item: Omit<TransferItem, 'transferred' | 'total' | 'errors' | 'startedAt'>) => {
    known.current.add(item.id)
    setItems(prev => trim([...prev, { ...item, transferred: 0, total: 0, errors: [], startedAt: Date.now() }]))
    const end = early.current.get(item.id)
    if (end) { early.current.delete(item.id); applyEnd(end) }
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  const clearFinished = useCallback(() => setItems(prev => prev.filter(it => !it.done)), [])

  return { items, add, clearFinished }
}

function trim(items: TransferItem[]): TransferItem[] {
  const done = items.filter(it => it.done)
  if (done.length <= KEEP_DONE) return items
  const drop = new Set(done.slice(0, done.length - KEEP_DONE).map(it => it.id))
  return items.filter(it => !drop.has(it.id))
}

export function percent(it: TransferItem): number {
  if (it.done) return 100
  if (it.hosts && Object.keys(it.hosts).length) {
    const rows = Object.values(it.hosts)
    const t = rows.reduce((a, r) => a + r.total, 0)
    return t > 0 ? Math.round((rows.reduce((a, r) => a + r.transferred, 0) / t) * 100) : 0
  }
  return it.total > 0 && !it.indeterminate ? Math.round((it.transferred / it.total) * 100) : 0
}
