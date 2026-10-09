/**
 * render_diagram's in-app renderer: SVG sizing helpers, request validation, and
 * the `runspec:render_diagram` listener answering through the bridge. Real
 * rasterising needs a browser canvas, so Mermaid is stubbed on globalThis (where
 * its browser script puts it); the actual render path was checked in headless
 * Chromium.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { diagramRenderResult, parse, render } = vi.hoisted(() => ({
  diagramRenderResult: vi.fn(),
  parse: vi.fn(),
  render: vi.fn(),
}))

vi.mock('./bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => (p === 'diagram_render_result' ? diagramRenderResult : vi.fn()),
  }),
}))

import {
  svgSize,
  fixSvgSize,
  clampScale,
  renderDiagram,
  installDiagramRenderer,
  loadMermaid,
  _resetForTests,
} from './diagramRender'

const g = globalThis as { mermaid?: unknown }

beforeEach(() => {
  _resetForTests()
  diagramRenderResult.mockReset()
  parse.mockReset()
  render.mockReset()
  g.mermaid = { initialize: vi.fn(), parse, render }
})

describe('svg helpers', () => {
  it('reads the size from the viewBox, rounding up', () => {
    expect(svgSize('<svg viewBox="0 0 174.7 388.3" width="100%">')).toEqual({ width: 175, height: 389 })
    expect(svgSize('<svg viewBox="-8 -8 100 50">')).toEqual({ width: 100, height: 50 })
    expect(svgSize('<svg width="100%">')).toBeNull()
  })

  it('pins the root svg to an explicit size', () => {
    const out = fixSvgSize('<svg id="d" width="100%" style="max-width: 175px;" viewBox="0 0 175 389"><g/></svg>', 175, 389)
    expect(out).toBe('<svg id="d" viewBox="0 0 175 389" width="175" height="389"><g/></svg>')
  })

  it('defaults to 2x and shrinks huge diagrams under the bitmap limit', () => {
    expect(clampScale(undefined, 400, 300)).toBe(2)
    expect(clampScale(3, 400, 300)).toBe(3)
    expect(clampScale(10, 400, 300)).toBe(4)
    expect(clampScale(2, 8000, 100)).toBeLessThan(1.1)
  })
})

describe('renderDiagram', () => {
  it('refuses other kinds and empty source without loading Mermaid', async () => {
    expect(await renderDiagram({ request_id: 'a', kind: 'plantuml', source: 'x' })).toEqual({
      ok: false,
      error: "can't draw plantuml diagrams (only mermaid)",
    })
    expect(await renderDiagram({ request_id: 'b', source: '  ' })).toEqual({ ok: false, error: 'the diagram source is empty' })
    expect(parse).not.toHaveBeenCalled()
  })

  it("returns Mermaid's syntax error", async () => {
    parse.mockRejectedValue(new Error('Parse error on line 2'))
    expect(await renderDiagram({ request_id: 'c', source: 'graph TD\n A-->' })).toEqual({
      ok: false,
      error: 'Parse error on line 2',
    })
    expect(render).not.toHaveBeenCalled()
  })

  it('refuses an SVG with HTML labels instead of tainting the canvas', async () => {
    parse.mockResolvedValue(true)
    render.mockResolvedValue({ svg: '<svg viewBox="0 0 10 10"><foreignObject/></svg>' })
    const out = await renderDiagram({ request_id: 'd', source: 'journey' })
    expect(out.ok).toBe(false)
    expect(out.ok === false && out.error).toMatch(/HTML labels/)
  })
})

describe('installDiagramRenderer', () => {
  it('answers each request once through the bridge, even if installed twice', async () => {
    parse.mockRejectedValue(new Error('bad'))
    installDiagramRenderer()
    installDiagramRenderer()
    window.dispatchEvent(
      new CustomEvent('runspec:render_diagram', { detail: { request_id: 'r1', kind: 'mermaid', source: 'x' } }),
    )
    await vi.waitFor(() => expect(diagramRenderResult).toHaveBeenCalledTimes(1))
    expect(diagramRenderResult).toHaveBeenCalledWith('r1', { ok: false, error: 'bad' })
  })
})

describe('loadMermaid', () => {
  it('adds the script once and retries after a failed load', async () => {
    delete g.mermaid
    const first = loadMermaid('/assets/mermaid.js')
    expect(loadMermaid('/assets/mermaid.js')).toBe(first)
    const scripts = () => Array.from(document.head.querySelectorAll('script[src="/assets/mermaid.js"]'))
    expect(scripts()).toHaveLength(1)
    scripts()[0].dispatchEvent(new Event('error'))
    await expect(first).rejects.toThrow(/could not load the Mermaid renderer/)
    expect(scripts()).toHaveLength(0)

    const second = loadMermaid('/assets/mermaid.js')
    const api = { initialize: vi.fn(), parse, render }
    g.mermaid = api
    scripts()[0].dispatchEvent(new Event('load'))
    await expect(second).resolves.toBe(api)
  })
})
