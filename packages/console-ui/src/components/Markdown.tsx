import { type CSSProperties } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import { MONO_FONT } from '../ThemeContext'

// Renders an assistant message as markdown. Only the model's natural-language
// prose flows through here — runnable stdout / JSON / tool output stays in the
// monospace <pre> blocks elsewhere in OutputPanel. Code *fences* the model emits
// still render monospace (that's what a markdown code block is), so quoted JSON
// looks right while headings / lists / **bold** / tables format properly.
//
// Margins are squeezed so a one-line answer doesn't get block spacing, and the
// element set is constrained to what an LLM actually produces in chat.

/** The longest line prose may run to. */
export const PROSE_MEASURE = '75ch'

interface MarkdownProps {
  children: string
  color?: string
  fontSize?: number
}

// Build the component overrides once per (color) — keeps spacing tight and
// styles readable in the dark console. Tables get borders; links open in the
// system browser (target=_blank) since this runs inside a webview.
function components(color: string): Components {
  const border = '#3a3a3a'
  const codeBg = 'rgba(127,127,127,0.18)'
  // Prose keeps a readable line however wide the card is; tables and code
  // blocks are left uncapped so wide data uses the room.
  const measure = PROSE_MEASURE
  const tight: CSSProperties = { margin: '0 0 6px', maxWidth: measure }
  return {
    p: ({ children }) => <p style={{ ...tight, color }}>{children}</p>,
    h1: ({ children }) => <h1 style={{ margin: '6px 0 4px', fontSize: '1.3em', maxWidth: measure, color }}>{children}</h1>,
    h2: ({ children }) => <h2 style={{ margin: '6px 0 4px', fontSize: '1.2em', maxWidth: measure, color }}>{children}</h2>,
    h3: ({ children }) => <h3 style={{ margin: '6px 0 4px', fontSize: '1.1em', maxWidth: measure, color }}>{children}</h3>,
    h4: ({ children }) => <h4 style={{ margin: '6px 0 4px', fontSize: '1em', fontWeight: 700, maxWidth: measure, color }}>{children}</h4>,
    ul: ({ children }) => <ul style={{ margin: '0 0 6px', paddingLeft: 22, maxWidth: measure, color }}>{children}</ul>,
    ol: ({ children }) => <ol style={{ margin: '0 0 6px', paddingLeft: 22, maxWidth: measure, color }}>{children}</ol>,
    li: ({ children }) => <li style={{ margin: '1px 0', color }}>{children}</li>,
    a: ({ children, href }) => (
      <a href={href} target="_blank" rel="noreferrer" style={{ color: '#4ea1ff' }}>{children}</a>
    ),
    strong: ({ children }) => <strong style={{ color, fontWeight: 700 }}>{children}</strong>,
    em: ({ children }) => <em style={{ color }}>{children}</em>,
    hr: () => <hr style={{ border: 'none', borderTop: `1px solid ${border}`, margin: '8px 0' }} />,
    blockquote: ({ children }) => (
      <blockquote style={{ margin: '0 0 6px', paddingLeft: 10, maxWidth: measure, borderLeft: `3px solid ${border}`, opacity: 0.85 }}>
        {children}
      </blockquote>
    ),
    code: ({ className, children }) => {
      // Fenced blocks carry a language- className; inline code does not.
      const fenced = /language-/.test(className || '')
      if (fenced) {
        return (
          <pre style={{
            margin: '0 0 6px', padding: '8px 10px', background: codeBg,
            borderRadius: 4, overflowX: 'auto',
            fontFamily: MONO_FONT, fontSize: '0.92em', whiteSpace: 'pre',
          }}>
            <code>{children}</code>
          </pre>
        )
      }
      return (
        <code style={{
          background: codeBg, borderRadius: 3, padding: '1px 4px',
          fontFamily: MONO_FONT, fontSize: '0.92em',
        }}>{children}</code>
      )
    },
    // A table scrolls sideways rather than squeezing: the prose's
    // break-word let it split "web-01" and "10.20.0.10" mid-word. Short values
    // (hosts, IPs, versions) stay on one line; only real sentences wrap, at
    // spaces. The first column stays put while the rest scrolls (index.css).
    table: ({ children }) => (
      <div className="rs-md-table" style={{ overflowX: 'auto', margin: '0 0 6px' }}>
        <table style={{ borderCollapse: 'collapse', fontSize: '0.95em', wordBreak: 'normal', overflowWrap: 'normal' }}>{children}</table>
      </div>
    ),
    th: ({ children }) => (
      <th style={{ border: `1px solid ${border}`, padding: '3px 8px', textAlign: 'left', color, whiteSpace: 'nowrap' }}>{children}</th>
    ),
    td: ({ children }) => (
      <td style={{ border: `1px solid ${border}`, padding: '3px 8px', color, whiteSpace: keepOnOneLine(children) ? 'nowrap' : undefined, minWidth: keepOnOneLine(children) ? undefined : '20ch' }}>{children}</td>
    ),
  }
}

/** A cell's text: short or a single token (a host, an IP, a version) keeps
 *  to one line. */
function cellText(node: unknown): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(cellText).join('')
  if (node && typeof node === 'object' && 'props' in node) return cellText((node as { props: { children?: unknown } }).props.children)
  return ''
}
export function keepOnOneLine(children: unknown): boolean {
  const t = cellText(children).trim()
  return t.length <= 32 || !/\s/.test(t)
}

export function Markdown({ children, color = '#d4d4d4', fontSize = 13 }: MarkdownProps) {
  return (
    <div style={{ fontSize, lineHeight: 1.6, color, wordBreak: 'break-word' }}>
      {/* remark-breaks: render a single newline as a line break (CommonMark
          collapses them to spaces), so the agent's prose keeps its line breaks. */}
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkBreaks]} components={components(color)}>
        {children}
      </ReactMarkdown>
    </div>
  )
}
