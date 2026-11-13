export interface Session {
  session: string
  project: string
  state: 'active' | 'idle'
  task: string
  tool: string | null
  target: string | null
  ts: string
}

export interface AgentState {
  agent: string
  sessions: Session[]
}

export interface Snapshot {
  schema_version: number
  agents: AgentState[]
  generation: number
}

export type ToolName = 'Bash' | 'Read' | 'Edit' | 'Write' | 'WebFetch' | 'Agent' | string

export interface AgentEvent {
  agent: string
  session: string
  project: string
  type: 'task_start' | 'tool_call' | 'token_usage' | 'task_complete'
  task?: string
  tool?: ToolName
  target?: string
  tokens?: Record<string, number>
  ts: string
}

export type WsMessage =
  | { type: 'snapshot'; data: Snapshot }
  | { type: 'events'; events: AgentEvent[]; generation: number }
