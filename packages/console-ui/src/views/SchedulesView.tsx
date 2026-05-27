import { useEffect, useState } from 'react'
import { Table, Tag, Button } from 'antd'
import { DeleteOutlined } from '@ant-design/icons'
import { bridge, type Schedule } from '../bridge'

export function SchedulesView() {
  const [schedules, setSchedules] = useState<Schedule[]>([])

  useEffect(() => {
    bridge.get_schedules().then(setSchedules)
  }, [])

  const handleDelete = async (id: string) => {
    await bridge.delete_schedule(id)
    setSchedules(s => s.filter(x => x.id !== id))
  }

  const columns = [
    { title: 'Runnable', dataIndex: 'runnable', key: 'runnable', render: (n: string) => <code>/{n}</code> },
    { title: 'Host', dataIndex: 'host', key: 'host', render: (h: string) => <Tag>{h}</Tag> },
    { title: 'Schedule', dataIndex: 'schedule', key: 'schedule' },
    { title: 'Next run', dataIndex: 'nextRun', key: 'nextRun' },
    {
      title: '', key: 'actions',
      render: (_: unknown, s: Schedule) => (
        <Button danger size="small" icon={<DeleteOutlined />} onClick={() => handleDelete(s.id)} />
      )
    },
  ]

  return (
    <div>
      <Table dataSource={schedules} columns={columns} rowKey="id" size="small" />
    </div>
  )
}
