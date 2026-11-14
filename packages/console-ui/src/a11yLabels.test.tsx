import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { Alert, Input, Table } from 'antd'

import { labelAntInternals } from './a11yLabels'

describe('labelAntInternals', () => {
  it('names table filters, pager arrows, alert close and input clear', () => {
    const { container } = render(
      <div>
        <Table
          rowKey="k"
          dataSource={Array.from({ length: 30 }, (_, i) => ({ k: i, host: `h${i}` }))}
          columns={[{ title: 'Host', dataIndex: 'host', filters: [{ text: 'h1', value: 'h1' }], onFilter: () => true }]}
          pagination={{ pageSize: 10 }}
        />
        <Alert message="note" closable />
        <Input allowClear defaultValue="x" />
      </div>,
    )
    labelAntInternals(container)
    expect(container.querySelector('.ant-table-filter-trigger')?.getAttribute('aria-label')).toBe('Filter Host')
    expect(container.querySelector('.ant-pagination-prev button')?.getAttribute('aria-label')).toBe('Previous page')
    expect(container.querySelector('.ant-pagination-next button')?.getAttribute('aria-label')).toBe('Next page')
    expect(container.querySelector('.ant-alert-close-icon')?.getAttribute('aria-label')).toBe('Close')
    expect(container.querySelector('.ant-input-clear-icon')?.getAttribute('aria-label')).toBe('Clear')
  })

  it('leaves an existing name alone', () => {
    const { container } = render(<Alert message="note" closable />)
    container.querySelector('.ant-alert-close-icon')!.setAttribute('aria-label', 'Dismiss note')
    labelAntInternals(container)
    expect(container.querySelector('.ant-alert-close-icon')?.getAttribute('aria-label')).toBe('Dismiss note')
  })
})
