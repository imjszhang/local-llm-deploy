import { connectAgents } from './client'
import { AgentsScene } from './scene'
import { createUI } from './ui'
import type { Snapshot } from './types'

const container = document.getElementById('agents-app')!
container.style.cssText = 'position:fixed;inset:0;background:#0a0e12;overflow:hidden;'

const scene = new AgentsScene(container)
const ui = createUI(container)

let selected: string | null = null
scene.onSelectSession = (sessionId: string) => {
  selected = sessionId
  ui.showPanel(scene.getSessionData(sessionId))
}

connectAgents({
  onSnapshot(snapshot: Snapshot) {
    scene.applySnapshot(snapshot)
    ui.showPanel(selected ? scene.getSessionData(selected) : null)
  },
  onStatus(status) {
    ui.updateStatus(status)
  },
})

scene.start()
