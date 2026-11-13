import { describe, it, expect } from 'vitest'
import { describeCron } from './cronText'

describe('describeCron', () => {
  it.each([
    ['0 * * * *', 'Every hour'],
    ['15 * * * *', 'Every hour at :15'],
    ['*/5 * * * *', 'Every 5 minutes'],
    ['* * * * *', 'Every minute'],
    ['0 */6 * * *', 'Every 6 hours at :00'],
    ['0 2 * * *', 'Daily at 02:00'],
    ['30 9 * * 1-5', 'Weekdays at 09:30'],
    ['0 3 * * 0', 'Sundays at 03:00'],
    ['0 3 * * 7', 'Sundays at 03:00'],
    ['0 8 * * 1,3', 'Mondays and Wednesdays at 08:00'],
    ['0 4 1 * *', 'Monthly on the 1st at 04:00'],
    ['0 4 22 * *', 'Monthly on the 22nd at 04:00'],
    ['0 9,17 * * *', 'Daily at 09:00, 17:00'],
    ['@daily', 'Daily at 00:00'],
    ['@reboot', 'When the host starts'],
    ['15 6 * * mon-fri', 'Weekdays at 06:15'],
    ['0 7 * * MON', 'Mondays at 07:00'],
    ['0 7 * * 1-4', 'Monday to Thursday at 07:00'],
  ])('%s → %s', (expr, text) => {
    expect(describeCron(expr)).toBe(text)
  })

  it.each(['', '0 0 1 1 *', '0-10 * * * *', 'nonsense', '0 25 * * *', '0 4 1 * 1'])('gives up on %j', expr => {
    expect(describeCron(expr)).toBeNull()
  })
})
