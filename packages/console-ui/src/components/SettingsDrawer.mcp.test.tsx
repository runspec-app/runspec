/**
 * Tests for the MCP settings tab (external MCP servers). Mocks the bridge so we
 * assert the tab loads config + status + credential options, renders per-server
 * connection state, and saves the edited server list back through save_mcp_config.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { McpTab } from './SettingsDrawer'

const { getMcpConfig, saveMcpConfig, mcpStatus, mcpReload, mcpCredentialOptions, mcpInstallVenv } = vi.hoisted(() => ({
  getMcpConfig: vi.fn(),
  saveMcpConfig: vi.fn(),
  mcpStatus: vi.fn(),
  mcpReload: vi.fn(),
  mcpCredentialOptions: vi.fn(),
  mcpInstallVenv: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (prop === 'get_mcp_config') return getMcpConfig
        if (prop === 'save_mcp_config') return saveMcpConfig
        if (prop === 'mcp_status') return mcpStatus
        if (prop === 'mcp_reload') return mcpReload
        if (prop === 'mcp_credential_options') return mcpCredentialOptions
        if (prop === 'mcp_install_venv') return mcpInstallVenv
        return vi.fn()
      },
    },
  ),
}))

const STATUS = {
  available: true,
  started: true,
  tool_count: 2,
  servers: [
    { name: 'files', transport: 'stdio', connected: true, tool_count: 2, error: '', credential: '', proxy: '' },
    { name: 'search', transport: 'http', connected: false, tool_count: 0, error: 'connection refused', credential: 'svc', proxy: 'http://p:8080' },
  ],
}

describe('McpTab', () => {
  beforeEach(() => {
    getMcpConfig.mockReset().mockResolvedValue({
      enabled: true,
      call_timeout: 60,
      connect_timeout: 20,
      servers: [
        { name: 'files', transport: 'stdio', command: 'uvx', args: ['fs'], enabled: true, autonomy: '' },
        { name: 'search', transport: 'http', url: 'https://h/mcp', credential: 'svc', proxy: 'http://p:8080', enabled: true, autonomy: 'autonomous' },
      ],
    })
    mcpStatus.mockReset().mockResolvedValue(STATUS)
    mcpReload.mockReset().mockResolvedValue(STATUS)
    mcpCredentialOptions.mockReset().mockResolvedValue([{ id: 'svc', label: 'Service', kind: 'token' }])
    saveMcpConfig.mockReset().mockResolvedValue(STATUS)
    mcpInstallVenv.mockReset().mockResolvedValue({ ok: true, server: 'codebase-graph', command: '/x/mcp-venvs/codebase-graph/bin/codebase-memory-mcp', status: STATUS })
  })

  it('installs a stdio MCP server into a venv from the dialog', async () => {
    render(<McpTab />)
    await screen.findByText('files')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('button', { name: /Install into a venv/ })) })
    // Fill the server name + pip package and install.
    const name = await screen.findByPlaceholderText('codebase-graph')
    const pkg = screen.getByPlaceholderText('codebase-memory-mcp')
    await act(async () => {
      await user.type(name, 'codebase-graph')
      await user.type(pkg, 'codebase-memory-mcp')
    })
    await act(async () => { await user.click(screen.getByRole('button', { name: /Install$/ })) })
    await waitFor(() => expect(mcpInstallVenv).toHaveBeenCalled())
    const payload = mcpInstallVenv.mock.calls[0][0]
    expect(payload).toMatchObject({ name: 'codebase-graph', package: 'codebase-memory-mcp', autonomy: 'confirm' })
    // On success it reloads the config so the new server shows up.
    await waitFor(() => expect(getMcpConfig.mock.calls.length).toBeGreaterThan(1))
  })

  it('surfaces the venv scripts when the launcher is not found', async () => {
    mcpInstallVenv.mockReset().mockResolvedValue({ ok: false, error: "the launcher 'wrong' isn't in the venv", scripts: ['codebase-memory-mcp', 'cmm'] })
    render(<McpTab />)
    await screen.findByText('files')
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('button', { name: /Install into a venv/ })) })
    await act(async () => {
      await user.type(await screen.findByPlaceholderText('codebase-graph'), 'x')
      await user.type(screen.getByPlaceholderText('codebase-memory-mcp'), 'some-pkg')
      await user.click(screen.getByRole('button', { name: /Install$/ }))
    })
    await waitFor(() => expect(mcpInstallVenv).toHaveBeenCalled())
    // The venv's available scripts are shown so the operator can retry.
    await screen.findByText(/Installed, but the launcher wasn't found/)
    await screen.findByText('cmm')
  })

  it('loads config + status and renders per-server connection state', async () => {
    render(<McpTab />)
    await waitFor(() => expect(getMcpConfig).toHaveBeenCalled())
    await screen.findByText('files')
    await screen.findByText('search')
    // connected server shows a tool count; the failed one shows an error tag.
    await screen.findByText(/connected · 2 tools/)
    await screen.findByText('error')
  })

  it('reconnect calls mcp_reload', async () => {
    render(<McpTab />)
    await screen.findByText('files')
    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: /Reconnect/ }))
    })
    await waitFor(() => expect(mcpReload).toHaveBeenCalled())
  })

  it('removing a server saves the shortened list', async () => {
    render(<McpTab />)
    await screen.findByText('search')
    const user = userEvent.setup()
    // Each row has a delete icon; open the confirm on the second row and confirm.
    const deletes = document.querySelectorAll('.anticon-delete')
    await act(async () => { await user.click(deletes[1]) })
    await act(async () => { await user.click(await screen.findByRole('button', { name: 'Remove' })) })
    await waitFor(() => expect(saveMcpConfig).toHaveBeenCalled())
    const payload = saveMcpConfig.mock.calls[saveMcpConfig.mock.calls.length - 1][0]
    expect(payload.servers.map((s: { name: string }) => s.name)).toEqual(['files'])
  })

  it('editing a timeout persists it on blur', async () => {
    render(<McpTab />)
    await screen.findByText('files')
    const user = userEvent.setup()
    const connect = screen.getByRole('spinbutton', { name: /connect timeout/i })
    await act(async () => {
      await user.clear(connect)
      await user.type(connect, '45')
      await user.tab()  // blur commits
    })
    await waitFor(() => expect(saveMcpConfig).toHaveBeenCalled())
    const payload = saveMcpConfig.mock.calls[saveMcpConfig.mock.calls.length - 1][0]
    expect(payload.connect_timeout).toBe(45)
  })

  it('edits a server\'s credential-backed secret headers and saves them', async () => {
    // A remote server that needs two PATs, one per header, each from a credential.
    getMcpConfig.mockReset().mockResolvedValue({
      enabled: true, call_timeout: 60, connect_timeout: 20,
      servers: [
        { name: 'atlassian', transport: 'http', url: 'https://h/mcp', enabled: true, autonomy: 'confirm',
          header_credentials: [
            { header: 'X-Jira-PAT', credential: 'jira' },
            { header: 'X-Confluence-PAT', credential: 'conf' },
          ] },
      ],
    })
    mcpCredentialOptions.mockReset().mockResolvedValue([
      { id: 'jira', label: 'Jira PAT', kind: 'token' },
      { id: 'conf', label: 'Confluence PAT', kind: 'token' },
    ])
    render(<McpTab />)
    await screen.findByText('atlassian')
    const user = userEvent.setup()
    // Open the editor for the row and confirm the stored bindings are shown.
    await act(async () => { await user.click(document.querySelector('.anticon-edit')!) })
    expect(await screen.findByDisplayValue('X-Jira-PAT')).toBeTruthy()
    expect(screen.getByDisplayValue('X-Confluence-PAT')).toBeTruthy()
    // Save round-trips header_credentials (names + ids, no secret) to the backend.
    await act(async () => { await user.click(document.querySelector('.ant-modal-footer .ant-btn-primary')!) })
    await waitFor(() => expect(saveMcpConfig).toHaveBeenCalled())
    const payload = saveMcpConfig.mock.calls[saveMcpConfig.mock.calls.length - 1][0]
    expect(payload.servers[0].header_credentials).toEqual([
      { header: 'X-Jira-PAT', credential: 'jira' },
      { header: 'X-Confluence-PAT', credential: 'conf' },
    ])
  })

  it('unsetting a server credential saves an empty credential', async () => {
    render(<McpTab />)
    await screen.findByText('search')
    const user = userEvent.setup()
    // Open the editor for the 'search' row, which has credential 'svc'.
    const edits = document.querySelectorAll('.anticon-edit')
    await act(async () => { await user.click(edits[1]) })
    // The Credential select shows the current value; clear it via the × button.
    const selected = await screen.findByText('Service (token)')
    const clearBtn = selected.closest('.ant-select')!.querySelector('.ant-select-clear')!
    await act(async () => { await user.click(clearBtn) })
    // Save round-trips an empty credential (the id is removed on the backend).
    await act(async () => { await user.click(document.querySelector('.ant-modal-footer .ant-btn-primary')!) })
    await waitFor(() => expect(saveMcpConfig).toHaveBeenCalled())
    const payload = saveMcpConfig.mock.calls[saveMcpConfig.mock.calls.length - 1][0]
    const search = payload.servers.find((s: { name: string }) => s.name === 'search')
    expect(search.credential).toBe('')
  })

  it('offers an explicit “None” option to unset the credential', async () => {
    render(<McpTab />)
    await screen.findByText('search')
    const user = userEvent.setup()
    const edits = document.querySelectorAll('.anticon-edit')
    await act(async () => { await user.click(edits[1]) })
    // Open the Credential dropdown and pick the explicit blank entry.
    const selected = await screen.findByText('Service (token)')
    const selector = selected.closest('.ant-select')!.querySelector('.ant-select-selector')!
    await act(async () => { await user.click(selector) })
    const noneOption = document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title="None"]')!
    await act(async () => { await user.click(noneOption) })
    await act(async () => { await user.click(document.querySelector('.ant-modal-footer .ant-btn-primary')!) })
    await waitFor(() => expect(saveMcpConfig).toHaveBeenCalled())
    const payload = saveMcpConfig.mock.calls[saveMcpConfig.mock.calls.length - 1][0]
    const search = payload.servers.find((s: { name: string }) => s.name === 'search')
    expect(search.credential).toBe('')
  })

  it('toggling a row\'s inline enable switch persists just that server', async () => {
    render(<McpTab />)
    await screen.findByText('search')
    const user = userEvent.setup()
    // The per-row switch is labelled "Enable <name>"; toggling 'search' off.
    const rowSwitch = screen.getByRole('switch', { name: 'Enable search' })
    await act(async () => { await user.click(rowSwitch) })
    await waitFor(() => expect(saveMcpConfig).toHaveBeenCalled())
    const payload = saveMcpConfig.mock.calls[saveMcpConfig.mock.calls.length - 1][0]
    const files = payload.servers.find((s: { name: string }) => s.name === 'files')
    const search = payload.servers.find((s: { name: string }) => s.name === 'search')
    expect(search.enabled).toBe(false)
    expect(files.enabled).toBe(true)  // the other server is untouched
  })

  it('saves a per-server timeout override entered in the editor', async () => {
    render(<McpTab />)
    await screen.findByText('search')
    const user = userEvent.setup()
    const edits = document.querySelectorAll('.anticon-edit')
    await act(async () => { await user.click(edits[1]) })  // 'search' row
    const call = await screen.findByRole('spinbutton', { name: /call timeout override/i })
    await act(async () => {
      await user.clear(call)
      await user.type(call, '120')
    })
    await act(async () => { await user.click(document.querySelector('.ant-modal-footer .ant-btn-primary')!) })
    await waitFor(() => expect(saveMcpConfig).toHaveBeenCalled())
    const payload = saveMcpConfig.mock.calls[saveMcpConfig.mock.calls.length - 1][0]
    const search = payload.servers.find((s: { name: string }) => s.name === 'search')
    expect(search.call_timeout).toBe(120)
    // The untouched connect override stays null (inherit the section default).
    expect(search.connect_timeout).toBeNull()
  })

  it('toggling the section enable persists it', async () => {
    render(<McpTab />)
    await screen.findByText('Enable external MCP servers')
    const user = userEvent.setup()
    const sectionSwitch = screen.getAllByRole('switch')[0]
    await act(async () => { await user.click(sectionSwitch) })
    await waitFor(() => expect(saveMcpConfig).toHaveBeenCalled())
    const payload = saveMcpConfig.mock.calls[saveMcpConfig.mock.calls.length - 1][0]
    expect(payload.enabled).toBe(false)
  })
})
