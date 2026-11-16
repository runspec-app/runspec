// Pop-up messages must actually render under React 19 (they silently didn't).
import { describe, it, expect } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { message } from 'antd'
import { installAntdRender } from './antdRender'

describe('installAntdRender()', () => {
  it('makes antd static messages appear in the page', async () => {
    installAntdRender()
    message.success('Saved the schedule')
    await waitFor(() => expect(screen.getByText('Saved the schedule')).toBeTruthy())
    expect(document.querySelector('.ant-message')).not.toBeNull()
  })
})
