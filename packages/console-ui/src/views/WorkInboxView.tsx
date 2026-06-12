import { Button, Empty, List, Space, Tag, Typography } from 'antd'
import { CheckOutlined, CloseOutlined, ExportOutlined, BellOutlined, CommentOutlined, ThunderboltOutlined } from '@ant-design/icons'
import type { WorkInbox } from '../bridge/useWorkInbox'
import type { WorkItem, WorkUrgency } from '../bridge/workInbox'

const { Text } = Typography

// One list of everything needing the operator, projected from the Triggers,
// Rooms/helpdesk, and chat confirm-gate surfaces (see workInbox.ts). A pure
// renderer over the root-level useWorkInbox() store; actions route back to the
// owning bridge methods, and "Open" deep-links to the owning tab.

const URGENCY_META: Record<WorkUrgency, { label: string; color: string }> = {
  blocking: { label: 'Needs approval', color: 'red' },
  actionable: { label: 'Action needed', color: 'gold' },
  fyi: { label: 'In progress', color: 'default' },
}

const KIND_ICON = {
  confirm: <ThunderboltOutlined />,
  thread: <CommentOutlined />,
  trigger: <BellOutlined />,
}

const ORDER: WorkUrgency[] = ['blocking', 'actionable', 'fyi']
const SECTION_TITLE: Record<WorkUrgency, string> = {
  blocking: 'Needs your approval',
  actionable: 'Action needed',
  fyi: 'In progress',
}

export function WorkInboxView({ inbox, onOpen }: { inbox: WorkInbox; onOpen: (item: WorkItem) => void }) {
  const { items, resolveConfirm, claimThread } = inbox

  if (items.length === 0) {
    return (
      <div style={{ paddingTop: 64 }}>
        <Empty description="Nothing needs you right now" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </div>
    )
  }

  const actionsFor = (item: WorkItem) => {
    const open = (
      <Button key="open" size="small" icon={<ExportOutlined />} onClick={() => onOpen(item)}>Open</Button>
    )
    if (item.kind === 'confirm' && item.requestId) {
      return [
        <Button key="approve" size="small" type="primary" icon={<CheckOutlined />}
          onClick={() => resolveConfirm(item.requestId!, true)}>Approve</Button>,
        <Button key="deny" size="small" danger icon={<CloseOutlined />}
          onClick={() => resolveConfirm(item.requestId!, false)}>Deny</Button>,
        open,
      ]
    }
    if (item.kind === 'thread' && item.room && item.urgency === 'actionable') {
      return [
        <Button key="claim" size="small" type="primary"
          onClick={() => claimThread(item.room!)}>Claim</Button>,
        open,
      ]
    }
    return [open]
  }

  return (
    <div style={{ maxWidth: 760, margin: '0 auto', padding: '8px 4px 24px' }}>
      {ORDER.map(tier => {
        const tierItems = items.filter(i => i.urgency === tier)
        if (tierItems.length === 0) return null
        return (
          <div key={tier} style={{ marginBottom: 20 }}>
            <Text type="secondary" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5 }}>
              {SECTION_TITLE[tier]} · {tierItems.length}
            </Text>
            <List
              size="small"
              dataSource={tierItems}
              rowKey={i => i.key}
              renderItem={item => (
                <List.Item actions={actionsFor(item)}>
                  <List.Item.Meta
                    avatar={KIND_ICON[item.kind]}
                    title={
                      <Space size={8}>
                        <span>{item.title}</span>
                        <Tag color={URGENCY_META[item.urgency].color} style={{ marginInlineEnd: 0 }}>
                          {URGENCY_META[item.urgency].label}
                        </Tag>
                      </Space>
                    }
                    description={item.subtitle ? <Text type="secondary" style={{ fontSize: 12 }}>{item.subtitle}</Text> : undefined}
                  />
                </List.Item>
              )}
            />
          </div>
        )
      })}
    </div>
  )
}
