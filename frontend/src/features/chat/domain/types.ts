import type { ChatControls } from '../../monitor/api/types'

export interface Parameters {
  temperature?: number
  top_p?: number
  max_tokens?: number
  seed?: number
  thinking?: boolean
  reasoning_effort?: string
  reasoning_budget_tokens?: number
}
export interface WireMessage { role: 'system' | 'user' | 'assistant'; content: string }
export interface RequestSnapshot { model: string; backend: string; parameters: Parameters; messages: WireMessage[]; chat_controls?: ChatControls | null }
export interface Answer {
  id: string
  content: string
  reasoning: string
  status: 'waiting' | 'streaming' | 'complete' | 'stopped' | 'error'
  adopted: boolean
  error?: string
  finishReason?: string
  startedAt: number
  firstContentMs?: number
  durationMs?: number
  usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number }
  request: RequestSnapshot
}
export interface Turn { id: string; user: string; answers: Answer[]; selected: number }
export interface Session { id: string; title: string; model: string; system: string; parameters: Parameters; turns: Turn[]; draft: string }
