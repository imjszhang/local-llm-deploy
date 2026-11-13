import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import type { Snapshot, AgentEvent, Session } from './types'

// ── Colour palette ────────────────────────────────────────────────────────────
const AGENT_COLORS: Record<string, number> = {
  'claude-code': 0xe07050,
  'codex': 0x8b5cf6,
  'archived': 0x4a5568,
}

// Floor layout
const FLOORS: Array<{ y: number; label: string; agentType: string }> = [
  { y: 0,  label: '档案室',     agentType: 'archive' },
  { y: 5,  label: 'Claude Code', agentType: 'claude-code' },
  { y: 10, label: 'Codex',       agentType: 'codex' },
  { y: 15, label: '预留',        agentType: 'reserved' },
]

const DESK_OFFSETS: Array<[number, number]> = [
  [-4, -3], [0, -3], [4, -3],
  [-4,  1], [0,  1], [4,  1],
]

interface CharacterGroup extends THREE.Group {
  userData: {
    sessionId: string
    agentType: string
    state: 'active' | 'idle' | 'archived'
    tool: string | null
    animTime: number
    // references to animatable parts
    head: THREE.Mesh
    body: THREE.Mesh
    rightArm: THREE.Mesh
    leftArm: THREE.Mesh
    pointLight?: THREE.PointLight
    desk?: THREE.Group
  }
}

export class AgentsScene {
  private renderer: THREE.WebGLRenderer
  private scene: THREE.Scene
  private camera: THREE.PerspectiveCamera
  private controls: OrbitControls
  private clock = new THREE.Clock()
  private raycaster = new THREE.Raycaster()
  private pointer = new THREE.Vector2()
  private container: HTMLElement

  // session map: sessionId → CharacterGroup
  private characters = new Map<string, CharacterGroup>()
  // mesh uuid → sessionId (for raycasting)
  private meshToSession = new Map<string, string>()
  // stored session data
  private sessionData = new Map<string, Session & { agentType: string }>()

  public onSelectSession: ((sessionId: string, agentType: string) => void) | null = null

  constructor(container: HTMLElement) {
    this.container = container

    // Renderer
    this.renderer = new THREE.WebGLRenderer({ antialias: true })
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    this.renderer.setSize(container.clientWidth, container.clientHeight)
    this.renderer.shadowMap.enabled = true
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping
    this.renderer.toneMappingExposure = 0.9
    container.appendChild(this.renderer.domElement)

    // Scene
    this.scene = new THREE.Scene()
    this.scene.background = new THREE.Color(0x0a0e12)
    this.scene.fog = new THREE.Fog(0x0a0e12, 30, 80)

    // Camera
    this.camera = new THREE.PerspectiveCamera(50, container.clientWidth / container.clientHeight, 0.1, 200)
    this.camera.position.set(0, 18, 28)
    this.camera.lookAt(0, 6, 0)

    // Lights
    const ambient = new THREE.AmbientLight(0xffffff, 0.5)
    this.scene.add(ambient)

    const sun = new THREE.DirectionalLight(0xfff4e0, 1.5)
    sun.position.set(10, 20, 10)
    sun.castShadow = true
    sun.shadow.mapSize.set(2048, 2048)
    sun.shadow.camera.near = 1
    sun.shadow.camera.far = 60
    sun.shadow.camera.left = -20
    sun.shadow.camera.right = 20
    sun.shadow.camera.top = 20
    sun.shadow.camera.bottom = -5
    sun.shadow.bias = -0.0005
    this.scene.add(sun)

    // Accent lights for each floor
    for (let i = 0; i < 4; i++) {
      const y = i * 5
      const accentLight = new THREE.PointLight(0x4a9eff, 0.4, 15)
      accentLight.position.set(-7, y + 3.5, -5)
      this.scene.add(accentLight)

      const accentLight2 = new THREE.PointLight(0x5f7adb, 0.3, 12)
      accentLight2.position.set(7, y + 3.5, -5)
      this.scene.add(accentLight2)
    }

    // Rim light from behind
    const rimLight = new THREE.DirectionalLight(0x6ea8ff, 0.6)
    rimLight.position.set(-8, 12, -10)
    this.scene.add(rimLight)

    // Controls
    this.controls = new OrbitControls(this.camera, this.renderer.domElement)
    this.controls.enableDamping = true
    this.controls.dampingFactor = 0.05
    this.controls.target.set(0, 6, 0)
    this.controls.minPolarAngle = 0.2
    this.controls.maxPolarAngle = 1.2
    this.controls.minDistance = 8
    this.controls.maxDistance = 60
    this.controls.update()

    // Build floors
    this.buildBuilding()

    // Events
    window.addEventListener('resize', this.onResize)
    this.renderer.domElement.addEventListener('click', this.onClick)
  }

  // ── Building ─────────────────────────────────────────────────────────────

  private buildBuilding() {
    const floorMat = new THREE.MeshStandardMaterial({
      color: 0x1a2026,
      roughness: 0.8,
      metalness: 0.05,
    })
    const wallMat = new THREE.MeshStandardMaterial({
      color: 0x1e2630,
      roughness: 0.75,
      metalness: 0.08,
    })
    const glassMat = new THREE.MeshPhysicalMaterial({
      color: 0x1e3a5f,
      transparent: true,
      opacity: 0.12,
      roughness: 0.1,
      metalness: 0.1,
      transmission: 0.9,
      thickness: 0.5,
    })

    for (const floor of FLOORS) {
      const fy = floor.y

      // Slab
      const slab = new THREE.Mesh(new THREE.BoxGeometry(16, 0.3, 12), floorMat)
      slab.position.set(0, fy, 0)
      slab.receiveShadow = true
      this.scene.add(slab)

      // Ceiling
      const ceiling = new THREE.Mesh(new THREE.BoxGeometry(16, 0.15, 12), floorMat.clone())
      ceiling.position.set(0, fy + 4.5, 0)
      this.scene.add(ceiling)

      // Back wall
      const back = new THREE.Mesh(new THREE.BoxGeometry(16, 4.5, 0.2), wallMat)
      back.position.set(0, fy + 2.4, -6)
      back.receiveShadow = true
      this.scene.add(back)

      // Wall panels for visual interest
      for (let i = -3; i <= 3; i++) {
        const panelMat = new THREE.MeshStandardMaterial({
          color: 0x242d38,
          roughness: 0.7,
          metalness: 0.1,
        })
        const panel = new THREE.Mesh(new THREE.BoxGeometry(1.8, 3.5, 0.08), panelMat)
        panel.position.set(i * 2.2, fy + 2.4, -5.9)
        panel.receiveShadow = true
        this.scene.add(panel)
      }

      // Side walls
      for (const sx of [-8, 8]) {
        const side = new THREE.Mesh(new THREE.BoxGeometry(0.2, 4.5, 12), wallMat.clone())
        side.position.set(sx, fy + 2.4, 0)
        side.receiveShadow = true
        this.scene.add(side)
      }

      // Glass front panels
      for (let i = -2; i <= 2; i++) {
        const glass = new THREE.Mesh(new THREE.BoxGeometry(2.5, 3.5, 0.05), glassMat)
        glass.position.set(i * 3, fy + 2.2, 6)
        this.scene.add(glass)
      }

      // Floor label
      this.addFloorLabel(floor.label, 0, fy + 0.25, -5.6)
    }

    // Ground plane with gradient
    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(60, 60),
      new THREE.MeshStandardMaterial({
        color: 0x0d1318,
        roughness: 0.95,
        metalness: 0.02,
      })
    )
    ground.rotation.x = -Math.PI / 2
    ground.position.y = -0.16
    ground.receiveShadow = true
    this.scene.add(ground)
  }

  private addFloorLabel(text: string, x: number, y: number, z: number) {
    const canvas = document.createElement('canvas')
    canvas.width = 512
    canvas.height = 80
    const ctx = canvas.getContext('2d')!
    ctx.fillStyle = 'transparent'
    ctx.clearRect(0, 0, 512, 80)
    ctx.font = 'bold 36px system-ui, sans-serif'
    ctx.fillStyle = 'rgba(148, 163, 184, 0.6)'
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(text, 256, 40)

    const tex = new THREE.CanvasTexture(canvas)
    const mat = new THREE.MeshBasicMaterial({ map: tex, transparent: true, side: THREE.DoubleSide })
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(6, 0.9), mat)
    plane.position.set(x, y, z)
    this.scene.add(plane)
  }

  // ── Character factory ────────────────────────────────────────────────────

  private buildCharacter(color: number): CharacterGroup {
    const group = new THREE.Group() as CharacterGroup

    // Enhanced materials with better shading
    const baseMat = new THREE.MeshStandardMaterial({
      color,
      roughness: 0.7,
      metalness: 0.1,
      flatShading: true, // preserves low-poly faceted look
    })

    const highlightColor = new THREE.Color(color).multiplyScalar(1.3)
    const accentMat = new THREE.MeshStandardMaterial({
      color: highlightColor,
      roughness: 0.6,
      metalness: 0.15,
      flatShading: true,
    })

    // Head - larger sphere with more personality
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.26, 8, 6), baseMat.clone())
    head.position.set(0, 1.36, 0)
    head.castShadow = true
    head.receiveShadow = true
    group.add(head)

    // Eyes - two small white spheres
    const eyeMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.3, metalness: 0.1 })
    const pupilMat = new THREE.MeshStandardMaterial({ color: 0x1a1a1a, roughness: 0.5 })

    const leftEye = new THREE.Mesh(new THREE.SphereGeometry(0.05, 6, 5), eyeMat)
    leftEye.position.set(-0.09, 1.42, 0.22)
    group.add(leftEye)
    const leftPupil = new THREE.Mesh(new THREE.SphereGeometry(0.025, 5, 4), pupilMat)
    leftPupil.position.set(-0.09, 1.42, 0.25)
    group.add(leftPupil)

    const rightEye = new THREE.Mesh(new THREE.SphereGeometry(0.05, 6, 5), eyeMat)
    rightEye.position.set(0.09, 1.42, 0.22)
    group.add(rightEye)
    const rightPupil = new THREE.Mesh(new THREE.SphereGeometry(0.025, 5, 4), pupilMat)
    rightPupil.position.set(0.09, 1.42, 0.25)
    group.add(rightPupil)

    // Body - rounded corners with beveled edges
    const body = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.6, 0.32, 1, 2, 1), baseMat.clone())
    body.position.set(0, 0.75, 0)
    body.castShadow = true
    body.receiveShadow = true
    group.add(body)

    // Torso accent - vest/shirt detail
    const vestGeo = new THREE.BoxGeometry(0.42, 0.5, 0.05)
    const vest = new THREE.Mesh(vestGeo, accentMat.clone())
    vest.position.set(0, 0.77, 0.16)
    group.add(vest)

    // Arms with rounded shoulders
    const leftShoulder = new THREE.Mesh(new THREE.SphereGeometry(0.12, 6, 5), baseMat.clone())
    leftShoulder.position.set(-0.32, 0.98, 0)
    leftShoulder.castShadow = true
    group.add(leftShoulder)

    const leftArm = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.42, 0.14, 1, 2, 1), baseMat.clone())
    leftArm.position.set(-0.32, 0.72, 0)
    leftArm.rotation.z = 0.12
    leftArm.castShadow = true
    group.add(leftArm)

    const rightShoulder = new THREE.Mesh(new THREE.SphereGeometry(0.12, 6, 5), baseMat.clone())
    rightShoulder.position.set(0.32, 0.98, 0)
    rightShoulder.castShadow = true
    group.add(rightShoulder)

    const rightArm = new THREE.Mesh(new THREE.BoxGeometry(0.14, 0.42, 0.14, 1, 2, 1), baseMat.clone())
    rightArm.position.set(0.32, 0.72, 0)
    rightArm.rotation.z = -0.12
    rightArm.castShadow = true
    group.add(rightArm)

    // Legs with hip joints
    const legL = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.48, 0.16, 1, 2, 1), baseMat.clone())
    legL.position.set(-0.14, 0.28, 0)
    legL.castShadow = true
    group.add(legL)

    const legR = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.48, 0.16, 1, 2, 1), baseMat.clone())
    legR.position.set(0.14, 0.28, 0)
    legR.castShadow = true
    group.add(legR)

    // Feet - small rounded boxes
    const footMat = baseMat.clone()
    const footL = new THREE.Mesh(new THREE.BoxGeometry(0.18, 0.06, 0.24), footMat)
    footL.position.set(-0.14, 0.03, 0.04)
    footL.castShadow = true
    group.add(footL)

    const footR = new THREE.Mesh(new THREE.BoxGeometry(0.18, 0.06, 0.24), footMat)
    footR.position.set(0.14, 0.03, 0.04)
    footR.castShadow = true
    group.add(footR)

    group.userData = {
      sessionId: '',
      agentType: '',
      state: 'active',
      tool: null,
      animTime: Math.random() * Math.PI * 2,
      head,
      body,
      rightArm,
      leftArm,
    }

    return group
  }

  private addDesk(fx: number, fz: number, fy: number, color: number) {
    const group = new THREE.Group()
    const deskMat = new THREE.MeshStandardMaterial({
      color: 0x2a3540,
      roughness: 0.6,
      metalness: 0.05
    })
    const monMat = new THREE.MeshStandardMaterial({
      color: 0x111820,
      roughness: 0.4,
      metalness: 0.15
    })

    // Desk top with rounded edges
    const desk = new THREE.Mesh(new THREE.BoxGeometry(1.8, 0.1, 1.0), deskMat)
    desk.position.set(fx, fy + 0.84, fz)
    desk.castShadow = true
    desk.receiveShadow = true
    group.add(desk)

    // Desk legs - sleeker design
    const legMat = new THREE.MeshStandardMaterial({
      color: 0x1e2a33,
      roughness: 0.5,
      metalness: 0.3
    })
    for (const [lx, lz] of [[-0.75, -0.4], [0.75, -0.4], [-0.75, 0.4], [0.75, 0.4]] as [number,number][]) {
      const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.05, 0.8, 8), legMat)
      leg.position.set(fx + lx, fy + 0.44, fz + lz)
      leg.castShadow = true
      group.add(leg)
    }

    // Monitor stand - cylindrical for modern look
    const stand = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.06, 0.5, 8), monMat)
    stand.position.set(fx, fy + 1.13, fz - 0.35)
    stand.castShadow = true
    group.add(stand)

    // Monitor frame
    const monitor = new THREE.Mesh(new THREE.BoxGeometry(1.2, 0.8, 0.04), monMat)
    monitor.position.set(fx, fy + 1.6, fz - 0.38)
    monitor.castShadow = true
    monitor.receiveShadow = true
    group.add(monitor)

    // Monitor bezel (thin border)
    const bezelMat = new THREE.MeshStandardMaterial({
      color: 0x0a0d12,
      roughness: 0.3,
      metalness: 0.4
    })
    const bezel = new THREE.Mesh(new THREE.BoxGeometry(1.24, 0.84, 0.02), bezelMat)
    bezel.position.set(fx, fy + 1.6, fz - 0.37)
    group.add(bezel)

    // Screen with glow
    const screenGlow = color !== AGENT_COLORS['archived'] ? color : 0x223344
    const screenMat = new THREE.MeshStandardMaterial({
      color: screenGlow,
      emissive: new THREE.Color(screenGlow),
      emissiveIntensity: 0.4,
      roughness: 0.9,
      metalness: 0.0
    })
    const screen = new THREE.Mesh(new THREE.BoxGeometry(1.08, 0.68, 0.01), screenMat)
    screen.position.set(fx, fy + 1.6, fz - 0.35)
    group.add(screen)

    // Screen content - subtle code lines
    const canvas = document.createElement('canvas')
    canvas.width = 256
    canvas.height = 192
    const ctx = canvas.getContext('2d')!
    ctx.fillStyle = new THREE.Color(screenGlow).multiplyScalar(0.3).getStyle()
    ctx.fillRect(0, 0, 256, 192)
    ctx.strokeStyle = new THREE.Color(screenGlow).multiplyScalar(0.8).getStyle()
    ctx.lineWidth = 2
    for (let i = 0; i < 8; i++) {
      const y = 20 + i * 22
      const len = 60 + Math.random() * 140
      ctx.beginPath()
      ctx.moveTo(16, y)
      ctx.lineTo(16 + len, y)
      ctx.stroke()
    }
    const screenTex = new THREE.CanvasTexture(canvas)
    const screenDetailMat = new THREE.MeshBasicMaterial({
      map: screenTex,
      transparent: true,
      opacity: 0.6
    })
    const screenDetail = new THREE.Mesh(new THREE.PlaneGeometry(1.06, 0.66), screenDetailMat)
    screenDetail.position.set(fx, fy + 1.6, fz - 0.34)
    group.add(screenDetail)

    // Chair - ergonomic office chair
    const chairMat = new THREE.MeshStandardMaterial({
      color: 0x1e2a33,
      roughness: 0.7,
      metalness: 0.1
    })
    const seat = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.08, 0.6, 2, 1, 2), chairMat)
    seat.position.set(fx, fy + 0.5, fz + 0.65)
    seat.castShadow = true
    group.add(seat)

    const chairBack = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.7, 0.08, 2, 3, 1), chairMat)
    chairBack.position.set(fx, fy + 0.88, fz + 0.92)
    chairBack.castShadow = true
    group.add(chairBack)

    // Chair armrests
    const armL = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.04, 0.35), chairMat)
    armL.position.set(fx - 0.38, fy + 0.55, fz + 0.65)
    group.add(armL)
    const armR = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.04, 0.35), chairMat)
    armR.position.set(fx + 0.38, fy + 0.55, fz + 0.65)
    group.add(armR)

    // Chair base with wheels
    const baseMat = new THREE.MeshStandardMaterial({
      color: 0x0f1419,
      roughness: 0.3,
      metalness: 0.5
    })
    const base = new THREE.Mesh(new THREE.CylinderGeometry(0.35, 0.35, 0.04, 6), baseMat)
    base.position.set(fx, fy + 0.1, fz + 0.65)
    group.add(base)

    this.scene.add(group)
    return group
  }

  // ── Character management ─────────────────────────────────────────────────

  private getFloorY(agentType: string, isArchive: boolean): number {
    if (isArchive) return FLOORS[0].y
    const floor = FLOORS.find(f => f.agentType === agentType)
    return floor ? floor.y : FLOORS[1].y
  }

  private nextDeskOffset(): [number, number] {
    const used = new Set([...this.characters.values()].map(char => `${char.position.x},${char.position.z}`))
    for (const [ox, oz] of DESK_OFFSETS) {
      if (!used.has(`${ox},${oz + 0.55}`)) return [ox, oz]
    }
    return DESK_OFFSETS[this.characters.size % DESK_OFFSETS.length]
  }

  private placeCharacter(char: CharacterGroup, sessionId: string, agentType: string, archived: boolean) {
    const fy = this.getFloorY(agentType, archived)
    const [ox, oz] = this.nextDeskOffset()

    // Build desk at position
    const color = archived ? AGENT_COLORS['archived'] : (AGENT_COLORS[agentType] ?? 0x4a5568)
    char.userData.desk = this.addDesk(ox, oz, fy, color)

    char.position.set(ox, fy + 0.3, oz + 0.55)
    char.rotation.y = Math.PI // face monitor
    char.userData.sessionId = sessionId
    char.userData.agentType = agentType
    char.userData.state = archived ? 'archived' : 'active'

    if (archived) {
      char.traverse(obj => {
        if ((obj as THREE.Mesh).isMesh) {
          ((obj as THREE.Mesh).material as THREE.MeshLambertMaterial).transparent = true;
          ((obj as THREE.Mesh).material as THREE.MeshLambertMaterial).opacity = 0.45
        }
      })
    } else {
      const light = new THREE.PointLight(color, 0.8, 3)
      light.position.set(ox, fy + 3.2, oz)
      this.scene.add(light)
      char.userData.pointLight = light
    }

    // register meshes for raycasting
    char.traverse(obj => {
      if ((obj as THREE.Mesh).isMesh) {
        this.meshToSession.set(obj.uuid, sessionId)
      }
    })

    this.scene.add(char)
    this.characters.set(sessionId, char)
  }

  private removeCharacter(sessionId: string) {
    const char = this.characters.get(sessionId)
    if (!char) return
    if (char.userData.pointLight) this.scene.remove(char.userData.pointLight)
    if (char.userData.desk) this.scene.remove(char.userData.desk)
    char.traverse(obj => {
      if ((obj as THREE.Mesh).isMesh) this.meshToSession.delete(obj.uuid)
    })
    this.scene.remove(char)
    this.characters.delete(sessionId)
    this.sessionData.delete(sessionId)
  }

  private paintCharacter(char: CharacterGroup, agentType: string, archived: boolean) {
    const color = archived ? AGENT_COLORS['archived'] : (AGENT_COLORS[agentType] ?? 0x4a5568)
    char.userData.state = archived ? 'archived' : 'active'
    char.userData.tool = archived ? null : char.userData.tool
    char.traverse(obj => {
      if ((obj as THREE.Mesh).isMesh) {
        const material = (obj as THREE.Mesh).material as THREE.MeshLambertMaterial
        material.transparent = archived
        material.opacity = archived ? 0.45 : 1
        material.color.setHex(color)
      }
    })
    if (archived && char.userData.pointLight) {
      this.scene.remove(char.userData.pointLight)
      char.userData.pointLight = undefined
    } else if (!archived && !char.userData.pointLight) {
      const light = new THREE.PointLight(color, 0.8, 3)
      light.position.set(char.position.x, char.position.y + 2.9, char.position.z - 0.55)
      this.scene.add(light)
      char.userData.pointLight = light
    }
  }

  // ── Public API ────────────────────────────────────────────────────────────

  applySnapshot(snapshot: Snapshot) {
    const next = new Map<string, Session & { agentType: string }>()
    for (const agentState of snapshot.agents) {
      for (const session of agentState.sessions) {
        next.set(session.session, { ...session, agentType: agentState.agent })
      }
    }
    for (const sessionId of this.characters.keys()) {
      if (!next.has(sessionId)) this.removeCharacter(sessionId)
    }
    for (const [sessionId, session] of next) {
      const archived = session.state === 'idle'
      let char = this.characters.get(sessionId)
      if (!char) {
        const color = archived ? AGENT_COLORS['archived'] : (AGENT_COLORS[session.agentType] ?? 0x4a5568)
        char = this.buildCharacter(color)
        this.placeCharacter(char, sessionId, session.agentType, archived)
      } else if ((char.userData.state === 'archived') !== archived) {
        this.paintCharacter(char, session.agentType, archived)
      }
      char.userData.tool = session.tool
      this.sessionData.set(sessionId, session)
    }

    this.container.dispatchEvent(new CustomEvent('agents-session-count', { detail: this.characters.size }))
  }

  applyEvents(events: AgentEvent[]) {
    for (const ev of events) {
      if (ev.type === 'task_start') {
        if (!this.characters.has(ev.session)) {
          const color = AGENT_COLORS[ev.agent] ?? 0x4a5568
          const char = this.buildCharacter(color)
          this.placeCharacter(char, ev.session, ev.agent, false)
        }
        const char = this.characters.get(ev.session)
        if (char) {
          char.userData.state = 'active'
          char.userData.tool = null
        }
        const stored = this.sessionData.get(ev.session)
        if (stored && ev.task) stored.task = ev.task
        else if (!stored) {
          this.sessionData.set(ev.session, {
            session: ev.session, project: ev.project,
            state: 'active', task: ev.task ?? '',
            tool: null, target: null, ts: ev.ts,
            agentType: ev.agent,
          })
        }
        this.container.dispatchEvent(new CustomEvent('agents-session-count', { detail: this.characters.size }))
      } else if (ev.type === 'tool_call') {
        const char = this.characters.get(ev.session)
        if (char) char.userData.tool = ev.tool ?? null
        const stored = this.sessionData.get(ev.session)
        if (stored) {
          stored.tool = ev.tool ?? null
          stored.target = ev.target ?? null
          stored.ts = ev.ts
        }
      } else if (ev.type === 'task_complete') {
        const char = this.characters.get(ev.session)
        if (char) {
          char.userData.state = 'archived'
          char.userData.tool = null
          if (char.userData.pointLight) {
            this.scene.remove(char.userData.pointLight)
            char.userData.pointLight = undefined
          }
          char.traverse(obj => {
            if ((obj as THREE.Mesh).isMesh) {
              ((obj as THREE.Mesh).material as THREE.MeshLambertMaterial).transparent = true;
              ((obj as THREE.Mesh).material as THREE.MeshLambertMaterial).opacity = 0.45;
              ((obj as THREE.Mesh).material as THREE.MeshLambertMaterial).color.setHex(AGENT_COLORS['archived'])
            }
          })
        }
        const stored = this.sessionData.get(ev.session)
        if (stored) stored.state = 'idle'
      }
    }
  }

  getSessionData(sessionId: string): (Session & { agentType: string }) | null {
    return this.sessionData.get(sessionId) ?? null
  }

  // ── Animation ─────────────────────────────────────────────────────────────

  private animateCharacter(char: CharacterGroup, t: number, _dt: number) {
    const { state, tool } = char.userData
    char.userData.animTime = t

    if (state === 'archived') return

    const bob = Math.sin(t * 1.5) * 0.04

    if (state === 'active') {
      if (tool === 'Bash') {
        // Typing: body sways, arms bob fast
        char.userData.body.position.y = 0.77 + Math.sin(t * 6) * 0.02
        char.userData.rightArm.rotation.x = Math.sin(t * 6) * 0.3
        char.userData.leftArm.rotation.x  = Math.sin(t * 6 + Math.PI) * 0.3
        char.userData.head.rotation.z = Math.sin(t * 2) * 0.05
      } else if (tool === 'Read') {
        // Reading: head tilts
        char.userData.head.rotation.z = Math.sin(t * 1.2) * 0.18
        char.userData.body.position.y = 0.77 + bob
      } else if (tool === 'Edit' || tool === 'Write') {
        // Writing: right arm back and forth
        char.userData.rightArm.rotation.x = Math.sin(t * 3) * 0.5
        char.userData.body.position.y = 0.77 + bob
      } else if (tool === 'WebFetch') {
        // Looking around
        char.userData.head.rotation.y = Math.sin(t * 0.8) * 0.4
        char.userData.head.position.y = 1.32 + Math.abs(Math.sin(t * 0.8)) * 0.05
        char.userData.body.position.y = 0.77 + bob
      } else {
        // Default idle
        char.userData.body.position.y = 0.77 + bob
        char.userData.head.rotation.z = Math.sin(t * 0.7) * 0.04
      }
    } else {
      // idle bob
      char.userData.body.position.y = 0.77 + bob
    }
  }

  // ── Resize / click ────────────────────────────────────────────────────────

  private onResize = () => {
    const w = this.container.clientWidth
    const h = this.container.clientHeight
    this.camera.aspect = w / h
    this.camera.updateProjectionMatrix()
    this.renderer.setSize(w, h)
  }

  private onClick = (e: MouseEvent) => {
    const rect = this.renderer.domElement.getBoundingClientRect()
    this.pointer.x =  ((e.clientX - rect.left) / rect.width)  * 2 - 1
    this.pointer.y = -((e.clientY - rect.top)  / rect.height) * 2 + 1

    this.raycaster.setFromCamera(this.pointer, this.camera)
    const meshes: THREE.Object3D[] = []
    this.scene.traverse(obj => {
      if ((obj as THREE.Mesh).isMesh && this.meshToSession.has(obj.uuid)) meshes.push(obj)
    })
    const hits = this.raycaster.intersectObjects(meshes)
    if (hits.length > 0) {
      const sessionId = this.meshToSession.get(hits[0].object.uuid)
      if (sessionId && this.onSelectSession) {
        const char = this.characters.get(sessionId)
        this.onSelectSession(sessionId, char?.userData.agentType ?? '')
      }
    }
  }

  // ── Render loop ───────────────────────────────────────────────────────────

  start() {
    const loop = () => {
      requestAnimationFrame(loop)
      const dt = this.clock.getDelta()
      const elapsed = this.clock.getElapsedTime()

      for (const char of this.characters.values()) {
        this.animateCharacter(char, elapsed + char.userData.animTime, dt)
      }

      this.controls.update()
      this.renderer.render(this.scene, this.camera)
    }
    loop()
  }
}
