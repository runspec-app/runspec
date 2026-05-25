import { Drawer, Form, Input, Divider, Typography } from 'antd'

const { Title, Text } = Typography

interface SettingsDrawerProps {
  open: boolean
  onClose: () => void
}

export function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  return (
    <Drawer title="Settings" placement="right" width={420} open={open} onClose={onClose}>
      <Title level={5} style={{ marginTop: 0 }}>LLM / API</Title>
      <Form layout="vertical" size="small">
        <Form.Item label="API base URL">
          <Input placeholder="https://api.anthropic.com" />
        </Form.Item>
        <Form.Item label="API key">
          <Input.Password placeholder="sk-ant-..." />
        </Form.Item>
        <Form.Item label="Model">
          <Input placeholder="claude-opus-4-7" />
        </Form.Item>
      </Form>

      <Divider />

      <Title level={5}>SSH</Title>
      <Form layout="vertical" size="small">
        <Form.Item label="Default username">
          <Input placeholder="your-username" />
        </Form.Item>
        <Form.Item label="Private key path">
          <Input placeholder="~/.ssh/runspec-console_ed25519" />
        </Form.Item>
        <Form.Item label="Password (optional)">
          <Input.Password placeholder="Leave blank to use key auth" />
        </Form.Item>
      </Form>

      <Divider />

      <Text type="secondary" style={{ fontSize: 12 }}>
        Settings are saved locally. API keys and passwords are stored in the OS keychain.
      </Text>
    </Drawer>
  )
}
