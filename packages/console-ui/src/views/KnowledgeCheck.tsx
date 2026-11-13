// The wiki check on the Knowledge tab's Tools view: one line that says what in
// the team wiki needs a look (backend: runspec_console/wiki_check.py), opening
// to the findings. A finding about a page opens that page; the "no page" one
// lists the tools so you can start one.

import { useState } from 'react'
import { Tag, Tooltip, Typography } from 'antd'
import { CheckCircleOutlined, DownOutlined, RightOutlined, SafetyCertificateOutlined } from '@ant-design/icons'

import type { WikiCheck } from '../bridge'
import { MONO_FONT, RADIUS, surfaces, textColor, textMuted, textSecondary, useAccent, useIsDark } from '../ThemeContext'

const { Text } = Typography

const LABEL: Record<string, string> = {
  clash: 'out of date',
  stale: 'due a check',
  duplicate: 'repeated',
  gap: 'without a page',
}
const HELP: Record<string, string> = {
  clash: 'A page mentions a --flag its runnable’s runspec.toml doesn’t have.',
  stale: 'A page past its stale_after date, or marked deprecated while the tool is still installed.',
  duplicate: 'The same bullet on more than one page; they drift apart over time.',
  gap: 'Installed runnables the team has no page for yet.',
}

/** "1 out of date · 3 without a page" — the non-zero counts, or '' when tidy. */
export function checkSummary(check: WikiCheck): string {
  return (['clash', 'stale', 'duplicate', 'gap'] as const)
    .filter(k => check.summary[k] > 0)
    .map(k => `${check.summary[k]} ${LABEL[k]}`)
    .join(' · ')
}

export function WikiCheckPanel({ check, onOpen }: {
  check: WikiCheck | null
  // Open a page (entity like 'runnable:cron' / 'term:bau') or a tool by name.
  onOpen: (target: string) => void
}) {
  const isDark = useIsDark()
  const accent = useAccent()
  const sf = surfaces(isDark)
  const [open, setOpen] = useState(false)
  if (!check) return null
  const summary = checkSummary(check)
  if (!summary) {
    return (
      <div data-testid="wiki-check" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: textMuted(isDark) }}>
        <CheckCircleOutlined style={{ color: '#52c41a' }} /> Wiki check: nothing needs a look.
      </div>
    )
  }
  return (
    <div data-testid="wiki-check" style={{ borderRadius: RADIUS.md, border: `1px solid ${sf.border}`, background: sf.raised }}>
      <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open}
        style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: '7px 12px', border: 0, background: 'transparent', cursor: 'pointer', font: 'inherit', color: textColor(isDark), textAlign: 'left' }}>
        {open ? <DownOutlined style={{ fontSize: 11 }} /> : <RightOutlined style={{ fontSize: 11 }} />}
        <SafetyCertificateOutlined style={{ color: textMuted(isDark) }} />
        <Text strong style={{ color: textColor(isDark) }}>Wiki check</Text>
        <Text style={{ color: textSecondary(isDark) }}>{summary}</Text>
      </button>
      {open && (
        <ul style={{ margin: 0, padding: '0 12px 10px 34px', display: 'grid', gap: 6 }}>
          {check.findings.map((f, i) => (
            <li key={i} style={{ color: textColor(isDark) }}>
              <Tooltip title={HELP[f.kind]}><Tag bordered={false} style={{ marginRight: 6 }}>{LABEL[f.kind]}</Tag></Tooltip>
              {f.kind === 'gap' && f.names ? (
                <span>
                  {f.names.length} installed tool{f.names.length === 1 ? '' : 's'} with no page:{' '}
                  {f.names.slice(0, 12).map((n, j) => (
                    <span key={n}>
                      {j > 0 && ', '}
                      <a role="button" onClick={() => onOpen(n)} style={{ fontFamily: MONO_FONT, color: accent }}>{n}</a>
                    </span>
                  ))}
                  {f.names.length > 12 && ` and ${f.names.length - 12} more`}
                </span>
              ) : (
                <span>
                  {f.message}{' '}
                  {f.entity && <a role="button" onClick={() => onOpen(f.entity)} style={{ color: accent }}>Open page</a>}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
