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

    // Immediate re-poll when a run starts (dispatched by ConsoleView after invoke_runnable)
    // or when a run ends (dispatched by the Python bridge)
    const onUpdate = () => { if (!cancelled) poll() }

    poll()
    const id = setInterval(poll, POLL_MS)
    window.addEventListener('runspec:in_flight_updated', onUpdate)
    window.addEventListener('runspec:run_end', onUpdate)
    return () => {
      cancelled = true
      clearInterval(id)
      window.removeEventListener('runspec:in_flight_updated', onUpdate)
      window.removeEventListener('runspec:run_end', onUpdate)
    }
  }, [])

  return inFlight
}
