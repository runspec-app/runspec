import { useEffect, useState } from 'react'
import { bridge, type Runnable } from '../bridge'
import { CommandInput } from '../components/CommandInput'
import { OutputPanel, useInvocationBlocks } from '../components/OutputPanel'

export function ConsoleView() {
  const [runnables, setRunnables] = useState<Runnable[]>([])
  const [inputHistory, setInputHistory] = useState<string[]>([])
  const { blocks, addBlock } = useInvocationBlocks()

  useEffect(() => {
    bridge.get_runnables('local').then(setRunnables)
  }, [])

  const handleRunRunnable = async (runnable: Runnable, args: Record<string, unknown>) => {
    const label = `/${runnable.name} on ${runnable.host}`
    setInputHistory(h => [...h, `/${runnable.name}`])
    const id = await bridge.invoke_runnable(runnable.host, runnable.name, args)
    addBlock({ id, type: 'run', label, lines: [], done: false })
  }

  const handleSendChat = async (message: string) => {
    setInputHistory(h => [...h, message])
    const id = await bridge.send_chat(message)
    addBlock({ id, type: 'chat', label: message, lines: [], done: false })
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', gap: 12 }}>
      <OutputPanel blocks={blocks} />
      <CommandInput
        runnables={runnables}
        onRunRunnable={handleRunRunnable}
        onSendChat={handleSendChat}
        history={inputHistory}
      />
    </div>
  )
}
