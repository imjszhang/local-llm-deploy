/** Publish agents build. Never empty the project's static root. */
import { createHash } from 'node:crypto'
import { readFile, writeFile, mkdir, readdir, rename } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve, dirname } from 'node:path'
import { gzipSync } from 'node:zlib'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const targetIndex = process.argv.indexOf('--target')
if (targetIndex !== -1 && !process.argv[targetIndex + 1]) throw new Error('--target requires a directory')
const target = targetIndex === -1 ? resolve(frontend, '../static') : resolve(process.argv[targetIndex + 1])
const dist = resolve(frontend, 'dist-agents')
const check = process.argv.includes('--check')
const assets = (await readdir(resolve(dist, 'agent-assets'))).sort()
if (assets.some(name => !/^[\w.-]+\.(js|css|svg|woff2?)$/.test(name))) throw new Error('Unexpected build asset')
const names = [...assets.map(name => `agent-assets/${name}`), 'agents.html']
const files = []
const contents = new Map()
let javascript = 0, css = 0
for (const name of names) {
  const data = await readFile(resolve(dist, name))
  const compressed = gzipSync(data).byteLength
  if (name.endsWith('.js')) javascript += compressed
  if (name.endsWith('.css')) css += compressed
  files.push({ path: name, sha256: createHash('sha256').update(data).digest('hex'), bytes: data.byteLength, gzip_bytes: compressed })
  contents.set(name, data)
}
if (javascript > 320 * 1024 || css > 20 * 1024) throw new Error(`Resource budget exceeded: JS ${javascript}, CSS ${css} gzip bytes`)
const manifest = JSON.stringify({ schema_version: 1, entry: 'agents.html', files, gzip: { javascript, css } }, null, 2) + '\n'
contents.set('agent-manifest.json', Buffer.from(manifest))
for (const name of [...names.filter(name => name !== 'agents.html'), 'agent-manifest.json', 'agents.html']) {
  const data = contents.get(name)
  if (check) {
    let actual
    try { actual = await readFile(resolve(target, name)) } catch { throw new Error(`Missing generated asset: ${name}. Run npm run build:agents.`) }
    if (!actual.equals(data)) throw new Error(`Stale generated asset: ${name}. Run npm run build:agents.`)
  } else {
    await mkdir(dirname(resolve(target, name)), { recursive: true })
    await writeFile(resolve(target, name + '.tmp'), data)
    await rename(resolve(target, name + '.tmp'), resolve(target, name))
  }
}
console.log(`${check ? 'Verified' : 'Published'} ${files.length} files; gzip JS ${(javascript / 1024).toFixed(1)} KB, CSS ${(css / 1024).toFixed(1)} KB`)
