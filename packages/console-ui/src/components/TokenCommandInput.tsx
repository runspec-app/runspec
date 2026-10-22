import { useState, type CSSProperties } from 'react'
import { Button, Input, Space, Typography } from 'antd'
import { bridge } from '../bridge'
import { MONO_FONT } from '../ThemeContext'

const { Text } = Typography

interface TokenCommandInputProps {
  value?: string
  onChange?: (value: string) => void
  placeholder?: string
  disabled?: boolean
  style?: CSSProperties
}

export interface TokenCommandCheck {
  command: string
  executable: string
  found: boolean
  searched: string[]
  error?: string
}

// Swap the command's program for `path`, keeping its arguments. The program is a
// leading "quoted string" or the text up to the first space (as the backend's
// prepare_api_key_command reads it); a path with spaces is quoted.
export function withExecutable(command: string, path: string): string {
  const cmd = command.trim()
  let rest = ''
  if (cmd.startsWith('"')) {
    const end = cmd.indexOf('"', 1)
    rest = end > 0 ? cmd.slice(end + 1) : ''
  } else {
    const m = /^\S+/.exec(cmd)
    rest = m ? cmd.slice(m[0].length) : ''
  }
  const exe = /\s/.test(path) ? `"${path}"` : path
  return exe + rest
}

// One line saying what a token command would run, from check_api_key_command.
export function describeCheck(r: TokenCommandCheck): string {
  if (r.error) return `Couldn't check: ${r.error}`
  if (r.found) return `Runs ${r.command}`
  const where = r.searched.length ? r.searched.join('; ') : 'no folders'
  return `${r.executable || 'The program'} was not found. Looked in ${where}, then PATH. Use Browse… to pick it.`
}

// A token / API-key command field (`[llm] api_key_command`, the telemetry key
// commands): the command text plus Browse… to pick the helper program and Check
// to see what it resolves to — without running it, since its output is a secret.
export function TokenCommandInput({ value, onChange, placeholder, disabled, style }: TokenCommandInputProps) {
  const [check, setCheck] = useState<TokenCommandCheck | null>(null)
  const text = value ?? ''

  const set = (next: string) => {
    setCheck(null)
    onChange?.(next)
  }

  const browse = async () => {
    const path = await bridge.browse_executable().catch(() => '')
    if (path) set(withExecutable(text, path))
  }

  const runCheck = async () => {
    try {
      setCheck(await bridge.check_api_key_command(text))
    } catch (e) {
      setCheck({ command: text, executable: '', found: false, searched: [], error: String(e) })
    }
  }

  return (
    <div style={style}>
      <Space.Compact block>
        <Input
          value={text}
          onChange={e => set(e.target.value)}
          placeholder={placeholder}
          disabled={disabled}
          style={{ fontFamily: MONO_FONT }}
        />
        <Button onClick={browse} disabled={disabled}>Browse…</Button>
        <Button onClick={runCheck} disabled={disabled || !text.trim()}>Check</Button>
      </Space.Compact>
      {check && (
        <Text
          type={check.found ? 'success' : 'danger'}
          style={{ display: 'block', fontSize: 12, marginTop: 4, fontFamily: check.found ? MONO_FONT : undefined, wordBreak: 'break-all' }}
        >
          {describeCheck(check)}
        </Text>
      )}
    </div>
  )
}
