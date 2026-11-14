export interface HubService {
  id: string
  name: string
  kind: 'comfyui' | 'stdio' | 'http'
  enabled: boolean
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  allowed_tools?: string[]
  [key: string]: unknown
}
export interface HubConfig {
  schema_version: number
  revision: number
  listen: { host: string; port: number; public_url: string }
  allowed_origins: string[]
  services: HubService[]
  [key: string]: unknown
}
export interface HubDevice {
  id: string
  name: string
  services: string[]
  permissions: string[]
  revoked?: boolean
  revoked_at?: number | string | null
  created_at?: number | string
  last_seen_at?: number | string | null
}
export interface HubTask {
  id: string
  service?: string
  owner?: string
  device_id?: string
  tool?: string
  status: string
  prompt_id?: string
  created_at?: number | string
  error?: string
  result?: unknown
}
export interface HubRuntime {
  status: string
  pid?: number | null
  host?: string
  port?: number
  healthy?: boolean | null
  available?: boolean
  management?: string
  config_revision?: number
  applied_revision?: number
  config_error?: string
  error?: string
  services?: Array<{ id?: string; service_id?: string; status?: string; available?: boolean; tool_count?: number; error?: string; bridge?: string; remote?: { online?: boolean; version?: string; error?: string } }>
}
export interface HubConnection { service_id: string; url: string; transport: string }
export interface HubSnapshot {
  config: HubConfig
  runtime: HubRuntime
  devices: HubDevice[]
  tasks: HubTask[]
  connections: HubConnection[]
}
export interface HubOperation { id: string; action: string; status: 'running' | 'succeeded' | 'failed'; error?: string }
export interface HubTool { name: string; description?: string; inputSchema?: unknown }
