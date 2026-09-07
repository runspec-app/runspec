/**
 * SoundSelect: lists registered named sounds + built-in cues, reports the chosen
 * value through onChange, and auditions the current pick via preview_sound.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { SoundSelect } from './SoundSelect'

const { getSounds, previewSound } = vi.hoisted(() => ({
  getSounds: vi.fn(),
  previewSound: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'get_sounds') return getSounds
      if (p === 'preview_sound') return previewSound
      return vi.fn()
    },
  }),
}))

beforeEach(() => {
  getSounds.mockReset().mockResolvedValue([
    { id: 'deploy-done', label: 'Deploy done', file: '/x/d.mp3', exists: true },
  ])
  previewSound.mockReset().mockResolvedValue(true)
})

it('lists a named sound and a built-in cue, and reports the pick', async () => {
  const onChange = vi.fn()
  await act(async () => { render(<SoundSelect onChange={onChange} />) })
  await waitFor(() => expect(getSounds).toHaveBeenCalled())

  await act(async () => { await userEvent.click(screen.getByRole('combobox')) })
  // the registered sound shows by its label…
  await screen.findByText('Deploy done')
  // …and the built-in cues are offered too (getByText throws if absent).
  expect(screen.getAllByText('confirm').length).toBeGreaterThan(0)

  await act(async () => { await userEvent.click(screen.getByText('Deploy done')) })
  expect(onChange).toHaveBeenCalledWith('sound:deploy-done')
})

it('previews the current value and disables the button when empty', async () => {
  const { rerender } = render(<SoundSelect value="" />)
  await waitFor(() => expect(getSounds).toHaveBeenCalled())
  // No value → preview button disabled.
  const btn = () => screen.getByRole('button') as HTMLButtonElement
  expect(btn().disabled).toBe(true)

  rerender(<SoundSelect value="confirm" />)
  expect(btn().disabled).toBe(false)
  await act(async () => { await userEvent.click(btn()) })
  expect(previewSound).toHaveBeenCalledWith('confirm')
})
