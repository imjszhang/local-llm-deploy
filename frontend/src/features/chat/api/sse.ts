/** Incremental SSE decoding. Network chunks are never assumed to be event boundaries. */
export class SSEDecoder {
  private buffer = ''
  private data: string[] = []
  private size = 0
  constructor(private readonly receive: (data: string) => void, private readonly limit = 1024 * 1024) {}
  push(text: string) {
    this.buffer += text
    let index: number
    while ((index = this.buffer.search(/[\r\n]/)) >= 0) {
      if (this.buffer[index] === '\r' && index === this.buffer.length - 1) break
      const line = this.buffer.slice(0, index)
      const count = this.buffer[index] === '\r' && this.buffer[index + 1] === '\n' ? 2 : 1
      this.buffer = this.buffer.slice(index + count)
      this.line(line)
    }
    if (this.buffer.length + this.size > this.limit) throw new Error('流事件超过大小限制')
  }
  private line(line: string) {
    if (!line) {
      if (this.data.length) this.receive(this.data.join('\n'))
      this.data = []; this.size = 0
      return
    }
    if (line.startsWith(':')) return
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'data') {
      this.size += value.length
      if (this.size > this.limit) throw new Error('流事件超过大小限制')
      this.data.push(value)
    }
  }
  finish() {
    if (this.buffer.endsWith('\r')) this.push('\n')
    // SSE dispatch requires a blank line. An incomplete final event is not trusted.
    if (this.buffer || this.data.length) throw new Error('响应在完整事件结束前中断')
  }
}
