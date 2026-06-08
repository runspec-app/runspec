import { useState } from 'react'
import { Modal, Button, Typography, Input, message } from 'antd'
import { KeyOutlined } from '@ant-design/icons'
import { bridge } from '../bridge'

const { Text } = Typography

// Copy this machine's SSH public key to a host's authorized_keys over a one-off
// password connection — the in-app equivalent of ssh-copy-id. Lets a freshly
// added host switch to key auth without leaving for PuTTY. The password lives
// only in local state and is cleared the moment it's handed off.
export function CopyKeyModal({
  open,
  host,
  onClose,
  onDone,
}: {
  open: boolean
  host: string
  onClose: () => void
  onDone?: () => void
}) {
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const close = () => { setPassword(''); onClose() }

  const submit = async () => {
    if (!password.trim()) return
    setSubmitting(true)
    try {
      const res = await bridge.copy_ssh_id(host, password)
      setPassword('')  // don't retain the password after handing it off
      if (res.ok) {
        message.success(res.message)
        onDone?.()
        onClose()
      } else {
        message.error(res.message)
      }
    } catch (e) {
      message.error(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Modal
      open={open}
      onCancel={close}
      width={460}
      title={<span style={{ display: 'flex', alignItems: 'center', gap: 8 }}><KeyOutlined /> Copy SSH public key</span>}
      footer={
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <Button onClick={close}>Cancel</Button>
          <Button type="primary" loading={submitting} disabled={!password.trim()} onClick={submit}>Copy key</Button>
        </div>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <Text style={{ fontSize: 12 }}>
          Authorize this machine on <Text strong style={{ fontFamily: 'monospace' }}>{host}</Text> by adding its public key to the host's <Text code>authorized_keys</Text>.
        </Text>
        <Input.Password
          autoFocus
          value={password}
          onChange={e => setPassword(e.target.value)}
          onPressEnter={submit}
          placeholder="SSH password"
          autoComplete="new-password"
        />
        <Text type="secondary" style={{ fontSize: 11 }}>
          Connects once with your password to install the key. The password is not stored — afterwards the host connects with the key alone.
        </Text>
      </div>
    </Modal>
  )
}
