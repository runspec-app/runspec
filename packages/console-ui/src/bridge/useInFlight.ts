import { useEffect, useState } from 'react'
import { bridge, type InFlightRecord } from './index'

const POLL_MS = 5000

export function useInFlight() {
  const [inFlight, setInFlight] = useState<InFlightRecord[]>([])

  useEffect(() => {
    let cancelled = false

    const poll = async () => {
      try {
        const result = await bridge.get_in_flight()
        if (!cancelled) setInFlight(result)
      } catch {
        // host unreachable — keep last known state
      }
    }

    poll()
    const id = setInterval(poll, POLL_MS)
    return () => { cancelled = true; clearInterval(id) }
  }, [])

  return inFlight
}
