// Draws a Mermaid diagram to a PNG inside the console's own web view, for the
// agent's `render_diagram` tool. The backend dispatches `runspec:render_diagram`
// ({request_id, kind, source, scale?}); this renders it and answers through
// `bridge.diagram_render_result`.
//
// Mermaid (~3.5 MB) is NOT part of the app bundle: the build emits its prebuilt
// browser script as a separate static asset (the `?url` import) and it is
// loaded with a <script> tag the first time a diagram is drawn. The app ships as
// one bundle on purpose (see vite.config.ts — a lazily fetched chunk once blanked
// the window on a cold launch); a script that fails to load here only fails that
// one render, with a clear error, and the next call retries.
//
// The SVG is rasterised on a canvas, which only works when the SVG has no HTML
// (`foreignObject`) in it — so Mermaid is told to draw labels as plain SVG text
// (`htmlLabels: false`). A diagram type that still emits HTML comes back as a
// clear error instead of a tainted-canvas exception.
import type { Mermaid } from 'mermaid'
import mermaidUrl from 'mermaid/dist/mermaid.min.js?url'

import { bridge } from './bridge'

export interface DiagramRequest {
  request_id: string
  kind?: string
  source: string
  scale?: number
}

export type DiagramResult =
  | { ok: true; png_base64: string; width: number; height: number }
  | { ok: false; error: string }

// Padding (CSS px) around the drawing, and the largest bitmap edge we produce —
// big enough for a dense diagram at 2x, small enough to stay under browser
// canvas limits.
const PAD = 16
const MAX_EDGE = 8192

// The drawing's natural size from its viewBox (Mermaid sets width="100%" and a
// max-width style, so the viewBox is the reliable source).
export function svgSize(svg: string): { width: number; height: number } | null {
  const m = svg.match(/viewBox="\s*([-\d.eE]+)[\s,]+([-\d.eE]+)[\s,]+([\d.eE]+)[\s,]+([\d.eE]+)\s*"/)
  if (!m) return null
  const width = Math.ceil(parseFloat(m[3]))
  const height = Math.ceil(parseFloat(m[4]))
  if (!(width > 0 && height > 0)) return null
  return { width, height }
}

// Pin the root <svg> to an explicit pixel size so it draws at that size as an
// <img> (Mermaid's width="100%" / max-width style would otherwise collapse it).
export function fixSvgSize(svg: string, width: number, height: number): string {
  return svg.replace(/<svg\b([^>]*)>/, (_whole, attrs: string) => {
    const cleaned = attrs
      .replace(/\swidth="[^"]*"/, '')
      .replace(/\sheight="[^"]*"/, '')
      .replace(/\sstyle="[^"]*"/, '')
    return `<svg${cleaned} width="${width}" height="${height}">`
  })
}

export function clampScale(scale: number | undefined, width: number, height: number): number {
  const wanted = Number.isFinite(scale) && (scale as number) > 0 ? Math.min(scale as number, 4) : 2
  const fit = MAX_EDGE / Math.max(width + PAD * 2, height + PAD * 2)
  return Math.max(0.25, Math.min(wanted, fit))
}

let renderSeq = 0

let loading: Promise<Mermaid> | null = null

function globalMermaid(): Mermaid | undefined {
  return (globalThis as { mermaid?: Mermaid }).mermaid
}

// Load the Mermaid browser script once; a failed load is forgotten so the next
// render tries again.
export function loadMermaid(src: string = mermaidUrl): Promise<Mermaid> {
  const ready = globalMermaid()
  if (ready) return Promise.resolve(ready)
  if (!loading) {
    loading = new Promise<Mermaid>((resolve, reject) => {
      const script = document.createElement('script')
      script.src = src
      script.async = true
      script.onload = () => {
        const m = globalMermaid()
        if (m) resolve(m)
        else {
          loading = null
          reject(new Error('the Mermaid renderer loaded but did not start'))
        }
      }
      script.onerror = () => {
        loading = null
        script.remove()
        reject(new Error('could not load the Mermaid renderer — try again'))
      }
      document.head.appendChild(script)
    })
  }
  return loading
}

async function renderSvg(source: string): Promise<string> {
  const mermaid = await loadMermaid()
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: 'default',
    htmlLabels: false,
    flowchart: { htmlLabels: false },
  })
  // parse() first so a syntax error is reported as Mermaid's own message
  // rather than an "error" diagram drawn into the PNG.
  await mermaid.parse(source)
  renderSeq += 1
  const { svg } = await mermaid.render(`rs-diagram-${renderSeq}`, source)
  return svg
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('the diagram SVG could not be loaded as an image'))
    img.src = src
  })
}

async function rasterise(svg: string, scale?: number): Promise<{ png_base64: string; width: number; height: number }> {
  const size = svgSize(svg)
  if (!size) throw new Error('Mermaid returned an SVG without a size')
  if (svg.includes('<foreignObject')) {
    throw new Error('this diagram type draws HTML labels, which can\'t be turned into a PNG here')
  }
  const sized = fixSvgSize(svg, size.width, size.height)
  const img = await loadImage('data:image/svg+xml;charset=utf-8,' + encodeURIComponent(sized))
  const k = clampScale(scale, size.width, size.height)
  const canvas = document.createElement('canvas')
  canvas.width = Math.round((size.width + PAD * 2) * k)
  canvas.height = Math.round((size.height + PAD * 2) * k)
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('no 2D canvas available')
  ctx.fillStyle = '#ffffff' // Confluence pages are white; a transparent PNG reads badly in dark viewers
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  ctx.scale(k, k)
  ctx.drawImage(img, PAD, PAD, size.width, size.height)
  const url = canvas.toDataURL('image/png')
  return { png_base64: url.slice(url.indexOf(',') + 1), width: canvas.width, height: canvas.height }
}

function messageOf(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) return String((err as { message: unknown }).message)
  return String(err)
}

export async function renderDiagram(req: DiagramRequest): Promise<DiagramResult> {
  const kind = (req.kind || 'mermaid').toLowerCase()
  if (kind !== 'mermaid') return { ok: false, error: `can't draw ${kind} diagrams (only mermaid)` }
  if (!req.source || !req.source.trim()) return { ok: false, error: 'the diagram source is empty' }
  try {
    const svg = await renderSvg(req.source)
    return { ok: true, ...(await rasterise(svg, req.scale)) }
  } catch (err) {
    return { ok: false, error: messageOf(err).trim() || 'Mermaid could not draw the diagram' }
  }
}

// Listen for the backend's render requests. Idempotent, so StrictMode's double
// mount (or a hot reload) never answers a request twice.
let installed = false

export function installDiagramRenderer(target: Window = window): void {
  if (installed) return
  installed = true
  target.addEventListener('runspec:render_diagram', (ev: Event) => {
    const req = (ev as CustomEvent<DiagramRequest>).detail
    if (!req || !req.request_id) return
    void renderDiagram(req).then((result) => bridge.diagram_render_result(req.request_id, result))
  })
}

export function _resetForTests(): void {
  installed = false
  renderSeq = 0
  loading = null
}
