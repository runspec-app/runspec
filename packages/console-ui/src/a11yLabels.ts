/**
 * Names for the few antd controls that render without one: a table column's
 * filter button, the pager's previous/next buttons, an alert's close button
 * and an input's clear button. Antd gives no prop for these, so one observer
 * (useAntA11yLabels, mounted once in App) names them as they appear. Only
 * elements with no aria-label are touched.
 */
import { useEffect } from 'react'

function name(el: Element, label: string) {
  if (!el.getAttribute('aria-label')) el.setAttribute('aria-label', label)
}

/** Name the unnamed antd controls under `root`. Pure DOM, for tests. */
export function labelAntInternals(root: ParentNode): void {
  root.querySelectorAll('.ant-table-filter-trigger').forEach(el => {
    const th = el.closest('th')
    const col = (th?.querySelector('.ant-table-column-title')?.textContent ?? th?.textContent ?? '').trim()
    name(el, col ? `Filter ${col}` : 'Filter')
  })
  root.querySelectorAll('.ant-pagination-prev button, .ant-pagination-prev .ant-pagination-item-link').forEach(el => name(el, 'Previous page'))
  root.querySelectorAll('.ant-pagination-next button, .ant-pagination-next .ant-pagination-item-link').forEach(el => name(el, 'Next page'))
  root.querySelectorAll('.ant-alert-close-icon').forEach(el => name(el, 'Close'))
  root.querySelectorAll('.ant-input-clear-icon').forEach(el => {
    name(el, 'Clear')
    if (!el.getAttribute('role')) el.setAttribute('role', 'button')
  })
}

/** Keep naming them as the page changes (batched to one pass per frame). */
export function useAntA11yLabels(): void {
  useEffect(() => {
    let queued = 0
    const run = () => { queued = 0; labelAntInternals(document.body) }
    run()
    const obs = new MutationObserver(() => { if (!queued) queued = requestAnimationFrame(run) })
    obs.observe(document.body, { childList: true, subtree: true })
    return () => { obs.disconnect(); if (queued) cancelAnimationFrame(queued) }
  }, [])
}
