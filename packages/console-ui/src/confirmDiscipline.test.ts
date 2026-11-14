/**
 * Every confirmation goes through the shared dialog (components/risk.tsx):
 * useRiskConfirm, or AskFirst around one control. antd's Popconfirm made a
 * fleet-wide key replacement look like a tooltip, and its static
 * Modal.confirm/info/… don't render under React 19 at all (the ask, and the
 * action behind it, silently never happened). Neither may come back.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const SRC = __dirname

function files(dir: string): string[] {
  return readdirSync(dir).flatMap(name => {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) return files(p)
    return /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [p] : []
  })
}

describe('confirmation discipline', () => {
  it('no Popconfirm and no static antd Modal.confirm/info/warning/error/success', () => {
    const bad: string[] = []
    for (const p of files(SRC)) {
      const lines = readFileSync(p, 'utf8').split('\n')
      lines.forEach((l, i) => {
        if (/^\s*(\/\/|\*)/.test(l)) return
        if (/<Popconfirm\b|\bModal\.(confirm|info|warning|error|success)\(/.test(l)) {
          bad.push(`${relative(SRC, p).split(String.fromCharCode(92)).join('/')}:${i + 1}`)
        }
      })
    }
    expect(bad).toEqual([])
  })
})
