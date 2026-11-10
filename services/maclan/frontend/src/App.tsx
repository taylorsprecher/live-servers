import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  BandwidthSample,
  BluetoothSettings,
  Device,
  DeviceAction,
  NetworkConfig,
  NetworkStatus,
  PingResult,
  TelemetryEvent,
} from './types'

// MacLAN Phase 1 (complete) — macOS Network Settings-style UI.
// Increment 2 finishes Phase 1: TCP/IP panel (DHCP/manual + cable
// plug/unplug simulation), Bluetooth power/discoverability controls,
// and a live bandwidth graph streaming from the telemetry SSE endpoint.
// Bluetooth diagnostics (HCI/sniffer) remain Phase 2; the Ping Tester,
// Bandwidth Test, and RF Spectrum Analyzer panels were upgraded/added
// 2026-11-10 from the MacLAN1ZilTB spec and prototype.

const NAV = [
  { id: 'overview', label: 'Overview', icon: '🌐' },
  { id: 'tcpip', label: 'TCP/IP', icon: '🔌' },
  { id: 'devices', label: 'Devices', icon: '🖥️' },
  { id: 'ping', label: 'Ping Tester', icon: '📡' },
  { id: 'bandwidth', label: 'Bandwidth Test', icon: '📊' },
  { id: 'spectrum', label: 'RF Spectrum', icon: '📻' },
  { id: 'telemetry', label: 'Telemetry', icon: '🛰️' },
] as const

type TabId = (typeof NAV)[number]['id']

// Public prefix the app is served under ("" at the root, "/maclan" in the
// gateway deployment). Vite inlines it from the build's base setting, so
// every API call and the SSE stream resolve under the same prefix the UI
// was loaded from — no hard-coded paths.
const BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

function url(path: string): string {
  return path.startsWith('/') ? BASE + path : path
}

async function api<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(url(path), opts)
  if (!res.ok) throw new Error(`${path} -> ${res.status}`)
  return res.json() as Promise<T>
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

function StatusDot({ status }: { status: string }) {
  const color = status === 'connected' ? '#30d158' : status === 'paired' ? '#ffd60a' : '#636366'
  return <span className="dot" style={{ background: color }} />
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  label: string
}) {
  return (
    <label className="switchrow">
      <span className="switch">
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        <span className="slider" />
      </span>
      {label}
    </label>
  )
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------
function Overview({ status }: { status: NetworkStatus | null }) {
  if (!status) return <p className="muted">Loading network status…</p>
  const s = status.summary
  return (
    <div>
      <div className="cards">
        <div className="card"><h4>Primary Interface</h4><p>{s.primary_interface}</p></div>
        <div className="card"><h4>IP Address</h4><p>{s.ip_address ?? '—'}</p></div>
        <div className="card"><h4>Router</h4><p>{s.router ?? '—'}</p></div>
        <div className="card"><h4>TCP/IP Mode</h4><p className="cap">{s.tcpip_mode}</p></div>
        <div className="card"><h4>Ethernet Cable</h4><p>{s.cable_connected ? 'Plugged in' : 'Unplugged'}</p></div>
        <div className="card"><h4>Devices Connected</h4><p>{s.devices_connected} / {s.devices_total}</p></div>
      </div>
      <h3>Network Interfaces</h3>
      <table className="tbl">
        <thead><tr><th></th><th>Interface</th><th>Status</th><th>Detail</th></tr></thead>
        <tbody>
          {status.interfaces.map((i) => (
            <tr key={i.id}>
              <td><StatusDot status={i.status} /></td>
              <td>{i.name}</td>
              <td className="cap">{i.status}</td>
              <td className="muted">{i.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">{s.note}</p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// TCP/IP panel — DHCP/manual configuration + cable simulation (Phase 1)
// ---------------------------------------------------------------------------
function TcpIpPanel({
  config,
  onConfig,
  onRefresh,
}: {
  config: NetworkConfig
  onConfig: (c: NetworkConfig) => void
  onRefresh: () => void
}) {
  const [mode, setMode] = useState<'dhcp' | 'manual'>(config.tcpip.mode)
  const [ip, setIp] = useState(config.tcpip.manual.ip_address ?? '')
  const [mask, setMask] = useState(config.tcpip.manual.subnet_mask ?? '255.255.255.0')
  const [router, setRouter] = useState(config.tcpip.manual.router ?? '')
  const [dns, setDns] = useState((config.tcpip.manual.dns ?? []).join(', '))
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setMode(config.tcpip.mode)
    setIp(config.tcpip.manual.ip_address ?? '')
    setMask(config.tcpip.manual.subnet_mask ?? '255.255.255.0')
    setRouter(config.tcpip.manual.router ?? '')
    setDns((config.tcpip.manual.dns ?? []).join(', '))
  }, [config])

  const run = async (fn: () => Promise<NetworkConfig>, ok: string) => {
    setBusy(true)
    setMsg(null)
    try {
      onConfig(await fn())
      onRefresh()
      setMsg(ok)
    } catch (e) {
      setMsg(`⚠️ ${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  const applyTcpIp = () =>
    run(
      () =>
        api<NetworkConfig>('/api/network/config', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            mode,
            ip_address: mode === 'manual' ? ip : null,
            subnet_mask: mode === 'manual' ? mask : null,
            router: mode === 'manual' ? router : null,
            dns: mode === 'manual' ? dns.split(',').map((d) => d.trim()).filter(Boolean) : [],
          }),
        }),
      'TCP/IP configuration applied.',
    )

  const toggleCable = () =>
    run(
      () => post<NetworkConfig>('/api/network/cable', { connected: !config.cable_connected }),
      config.cable_connected ? 'Cable unplugged.' : 'Cable plugged in.',
    )

  const renewLease = () =>
    run(() => post<NetworkConfig>('/api/network/dhcp/renew'), 'DHCP lease renewed.')

  const eff = config.effective
  return (
    <div>
      <h3>TCP/IP — USB Ethernet</h3>

      <div className="card block">
        <h4>Ethernet Cable (simulated)</h4>
        <div className="row">
          <StatusDot status={config.cable_connected ? 'connected' : 'inactive'} />
          <span>{config.cable_connected ? 'Cable plugged in · link 1 Gbps' : 'Cable unplugged · no link'}</span>
          <button onClick={toggleCable} disabled={busy}>
            {config.cable_connected ? 'Unplug cable' : 'Plug in cable'}
          </button>
        </div>
      </div>

      <div className="card block">
        <h4>Configure IPv4</h4>
        <div className="seg">
          <label>
            <input type="radio" name="ipmode" checked={mode === 'dhcp'} onChange={() => setMode('dhcp')} />
            Using DHCP
          </label>
          <label>
            <input type="radio" name="ipmode" checked={mode === 'manual'} onChange={() => setMode('manual')} />
            Manually
          </label>
        </div>

        {mode === 'dhcp' ? (
          <div>
            <p className="muted">
              Current lease: <span className="mono">{config.tcpip.dhcp_lease.ip_address}</span>
              {config.tcpip.dhcp_lease.leased_at &&
                <> · leased {new Date(config.tcpip.dhcp_lease.leased_at * 1000).toLocaleTimeString()}</>}
            </p>
            <button onClick={renewLease} disabled={busy || !config.cable_connected}>
              Renew DHCP Lease
            </button>
            {!config.cable_connected && <span className="muted small"> (cable unplugged)</span>}
          </div>
        ) : (
          <div className="formgrid">
            <label>IP Address
              <input value={ip} onChange={(e) => setIp(e.target.value)} placeholder="192.168.0.50" />
            </label>
            <label>Subnet Mask
              <input value={mask} onChange={(e) => setMask(e.target.value)} placeholder="255.255.255.0" />
            </label>
            <label>Router
              <input value={router} onChange={(e) => setRouter(e.target.value)} placeholder="192.168.0.1" />
            </label>
            <label>DNS Servers (comma-separated)
              <input value={dns} onChange={(e) => setDns(e.target.value)} placeholder="192.168.0.1, 1.1.1.1" />
            </label>
          </div>
        )}
        <div className="row top">
          <button onClick={applyTcpIp} disabled={busy}>Apply</button>
          {msg && <span className="small">{msg}</span>}
        </div>
      </div>

      <div className="card block">
        <h4>Effective Addressing</h4>
        <table className="tbl">
          <tbody>
            <tr><td className="muted">State</td><td className="cap">{eff.state.replace('_', ' ')}</td></tr>
            <tr><td className="muted">IP Address</td><td className="mono">{eff.ip_address ?? '—'}</td></tr>
            <tr><td className="muted">Subnet Mask</td><td className="mono">{eff.subnet_mask ?? '—'}</td></tr>
            <tr><td className="muted">Router</td><td className="mono">{eff.router ?? '—'}</td></tr>
            <tr><td className="muted">DNS</td><td className="mono">{eff.dns.join(', ') || '—'}</td></tr>
          </tbody>
        </table>
      </div>
      <p className="muted small">All state is simulated and persisted by the backend (SQLite network_config).</p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Devices + Bluetooth radio controls
// ---------------------------------------------------------------------------
function BluetoothControls({
  bluetooth,
  onUpdate,
}: {
  bluetooth: BluetoothSettings
  onUpdate: (patch: Partial<BluetoothSettings>) => Promise<void>
}) {
  const [name, setName] = useState(bluetooth.name)
  useEffect(() => setName(bluetooth.name), [bluetooth.name])
  return (
    <div className="card block">
      <h4>Bluetooth Radio</h4>
      <div className="row wrap">
        <Toggle
          checked={bluetooth.power}
          onChange={(v) => void onUpdate({ power: v })}
          label={bluetooth.power ? 'Bluetooth On' : 'Bluetooth Off'}
        />
        <Toggle
          checked={bluetooth.discoverable}
          onChange={(v) => void onUpdate({ discoverable: v })}
          label="Discoverable"
        />
        <label className="inline">
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} disabled={!bluetooth.power} />
        </label>
        <button
          onClick={() => void onUpdate({ name })}
          disabled={!bluetooth.power || name.trim() === bluetooth.name}
        >
          Save name
        </button>
      </div>
      {!bluetooth.power && (
        <p className="muted small">Radio off — connected devices were dropped to “paired”, and pairing is blocked.</p>
      )}
      {bluetooth.power && (
        <p className="muted small">
          This device is {bluetooth.discoverable ? `discoverable as “${bluetooth.name}”` : 'not discoverable'}.
        </p>
      )}
    </div>
  )
}

function Devices({
  devices,
  bluetooth,
  onAction,
  onBluetooth,
}: {
  devices: Device[]
  bluetooth: BluetoothSettings
  onAction: (d: Device, a: DeviceAction) => void
  onBluetooth: (patch: Partial<BluetoothSettings>) => Promise<void>
}) {
  const nextAction = (d: Device): DeviceAction =>
    d.state === 'unpaired' ? 'pair' : d.state === 'paired' ? 'connect' : 'disconnect'
  return (
    <div>
      <h3>LAN &amp; Bluetooth Devices</h3>
      <BluetoothControls bluetooth={bluetooth} onUpdate={onBluetooth} />
      <table className="tbl">
        <thead>
          <tr><th></th><th>Name</th><th>Kind</th><th>Role</th><th>IP</th><th>MAC</th><th>Battery</th><th>State</th><th></th></tr>
        </thead>
        <tbody>
          {devices.map((d) => {
            const btOff = d.kind === 'bluetooth' && !bluetooth.power
            return (
              <tr key={d.id} className={btOff ? 'dim' : undefined}>
                <td><StatusDot status={btOff ? 'inactive' : d.state} /></td>
                <td>{d.name}</td>
                <td className="cap">{d.kind}</td>
                <td>{d.role}</td>
                <td className="mono">{d.ip ?? '—'}</td>
                <td className="mono">{d.mac}</td>
                <td>{d.battery != null ? `${d.battery}%` : '—'}</td>
                <td className="cap">{btOff ? 'radio off' : d.state}</td>
                <td>
                  {d.kind === 'bluetooth' ? (
                    <>
                      <button onClick={() => onAction(d, nextAction(d))} disabled={btOff}>
                        {nextAction(d)}
                      </button>{' '}
                      {d.state !== 'unpaired' && (
                        <button className="ghost" onClick={() => onAction(d, 'unpair')} disabled={btOff}>
                          unpair
                        </button>
                      )}
                    </>
                  ) : (
                    <span className="muted small">—</span>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="muted small">Actions are simulated state transitions stored by the backend (SQLite).</p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Ping tester — continuous ICMP echo session (upgraded 2026-11-10 from the
// MacLAN1ZilTB spec/prototype): Start/Stop, quick-ping shortcuts, live
// sent/received/loss and min/avg/max counters. Probes come from the
// backend's simulated ping endpoint at 1/sec; the Ethernet cable state
// rules reachability — unplugged means timeouts, and a fresh start is
// refused with the classic sendto error. Leaving the tab unmounts the
// panel, and the unmount cleanup clears the probe timer.
// ---------------------------------------------------------------------------
function PingTester({ cableConnected, gateway }: { cableConnected: boolean; gateway: string }) {
  const [target, setTarget] = useState('192.168.0.1')
  const [running, setRunning] = useState(false)
  const [lines, setLines] = useState<string[]>([])
  const [sent, setSent] = useState(0)
  const [recv, setRecv] = useState(0)
  const [times, setTimes] = useState<number[]>([])
  const acc = useRef({ seq: 0, sent: 0, recv: 0, times: [] as number[] })
  const timer = useRef<number | null>(null)
  const cable = useRef(cableConnected)
  cable.current = cableConnected
  const targetRef = useRef('192.168.0.1')
  const termRef = useRef<HTMLPreElement>(null)

  const clearTimer = () => {
    if (timer.current !== null) {
      window.clearInterval(timer.current)
      timer.current = null
    }
  }
  useEffect(() => clearTimer, [])
  useEffect(() => {
    const el = termRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines])

  const syncStats = () => {
    setSent(acc.current.sent)
    setRecv(acc.current.recv)
    setTimes([...acc.current.times])
  }

  const stop = (finalize: boolean) => {
    clearTimer()
    setRunning(false)
    if (finalize && acc.current.sent > 0) {
      const a = acc.current
      const lossPct = ((a.sent - a.recv) / a.sent) * 100
      const extra = [
        `--- ${targetRef.current} ping statistics ---`,
        `${a.sent} packets transmitted, ${a.recv} packets received, ${lossPct.toFixed(1)}% packet loss`,
      ]
      if (a.times.length) {
        const avg = a.times.reduce((x, y) => x + y, 0) / a.times.length
        extra.push(
          `round-trip min/avg/max = ${Math.min(...a.times).toFixed(2)}/${avg.toFixed(2)}/${Math.max(...a.times).toFixed(2)} ms  (simulated)`,
        )
      }
      setLines((prev) => [...prev, ...extra])
    }
  }

  const start = (t: string) => {
    stop(false)
    const host = t.trim() || '192.168.0.1'
    targetRef.current = host
    setTarget(host)
    acc.current = { seq: 0, sent: 0, recv: 0, times: [] }
    syncStats()
    if (!cable.current) {
      // Prototype behavior: with the cable out, ping refuses to start and
      // the terminal shows the classic sendto error instead.
      setLines(['ping: sendto: Network is unreachable (cable unplugged)'])
      return
    }
    setLines([`PING ${host} (${host}): 56 data bytes`])
    setRunning(true)
    timer.current = window.setInterval(async () => {
      const a = acc.current
      const seq = a.seq++
      a.sent += 1
      if (!cable.current || Math.random() < 0.04) {
        setLines((prev) => [...prev.slice(-199), `Request timeout for icmp_seq ${seq}`])
        syncStats()
        return
      }
      try {
        const r = await post<PingResult>('/api/diagnostics/ping', { target: host, count: 1 })
        const t = r.times_ms[0]
        a.recv += 1
        a.times.push(t)
        setLines((prev) => [
          ...prev.slice(-199),
          `64 bytes from ${host}: icmp_seq=${seq} ttl=64 time=${t.toFixed(2)} ms`,
        ])
      } catch {
        setLines((prev) => [...prev.slice(-199), `Request timeout for icmp_seq ${seq}`])
      }
      syncStats()
    }, 1000)
  }

  const loss = sent ? (((sent - recv) / sent) * 100).toFixed(1) : '0.0'
  const fmtT = (v: number) => (times.length ? v.toFixed(2) : '—')
  return (
    <div id="pingPanel">
      <h3>Ping Tester</h3>
      <div className="row">
        <input value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Ping target" disabled={running} />
        <button onClick={() => start(target)} disabled={running}>Start Ping</button>
        <button className="ghost" onClick={() => stop(true)} disabled={!running}>Stop</button>
        <button className="ghost" onClick={() => start(gateway)}>Ping Gateway</button>
        <button className="ghost" onClick={() => start('1.1.1.1')}>Ping Cloudflare (1.1.1.1)</button>
        <button
          className="ghost"
          onClick={() =>
            setLines(running ? [`PING ${targetRef.current} (${targetRef.current}): 56 data bytes`] : [])
          }
        >
          Clear
        </button>
        <span className="muted small">{running ? 'Running — 1 probe/sec (simulated)' : 'Continuous ICMP echo simulation'}</span>
      </div>
      <div className="cards">
        <div className="card"><h4>Sent</h4><p id="pingSent">{sent}</p></div>
        <div className="card"><h4>Received</h4><p id="pingRecv">{recv}</p></div>
        <div className="card"><h4>Packet Loss</h4><p id="pingLoss">{loss}%</p></div>
        <div className="card">
          <h4>Min / Avg / Max</h4>
          <p id="pingStats">
            {fmtT(Math.min(...times))} / {fmtT(times.reduce((x, y) => x + y, 0) / (times.length || 1))} / {fmtT(Math.max(...times))} ms
          </p>
        </div>
      </div>
      <pre className="terminal pingterm" ref={termRef}>
        {lines.length ? lines.join('\n') : 'Results will appear here — press Start Ping.'}
      </pre>
      <p className="muted small">
        Simulated echo replies served by the backend diagnostics endpoint, with occasional loss.
        Unplug the cable in the TCP/IP tab and every probe times out.
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Bandwidth test — live graph streaming from the telemetry SSE endpoint,
// plus a Run Test sequence (added 2026-11-10 from the MacLAN1ZilTB spec):
// a semicircular gauge and a simulated 2.5 GbE profile whose download
// phase eases to the prototype's 2,382 Mbps result, then an upload phase
// to 2,340 Mbps, then decay back to the ambient stream. While a test
// phase runs, the local 250 ms sampler owns the series and SSE appends
// are skipped; jitter eases to ≈0.45 ms under test, ≈1.5 ms ambient.
// ---------------------------------------------------------------------------
const MAX_SAMPLES = 90
const BW_PHASE_LABELS = {
  idle: 'Idle — ambient traffic',
  download: 'Testing download…',
  upload: 'Testing upload…',
  done: 'Test complete',
} as const
type BwPhase = keyof typeof BW_PHASE_LABELS

function fmtRate(mbps: number): string {
  return mbps >= 1000 ? `${(mbps / 1000).toFixed(2)} Gbps` : `${mbps.toFixed(0)} Mbps`
}

function Bandwidth({ cableConnected }: { cableConnected: boolean }) {
  const [samples, setSamples] = useState<BandwidthSample[]>([])
  const [streaming, setStreaming] = useState(false)
  const [streamError, setStreamError] = useState(false)
  const [phase, setPhase] = useState<BwPhase>('idle')
  const [phaseNote, setPhaseNote] = useState<string | null>(null)
  const [jitter, setJitter] = useState(1.4)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const gaugeRef = useRef<HTMLCanvasElement>(null)
  const phaseRef = useRef<BwPhase>('idle')
  phaseRef.current = phase

  // Seed the graph from recent persisted bandwidth telemetry, then stream.
  useEffect(() => {
    let es: EventSource | null = null
    let cancelled = false
    ;(async () => {
      try {
        const rows = await api<TelemetryEvent[]>('/api/telemetry?metric=bandwidth&limit=60')
        if (cancelled) return
        const history = rows
          .map((r) => {
            try {
              return r.payload ? (JSON.parse(r.payload) as BandwidthSample) : null
            } catch {
              return null
            }
          })
          .filter((s): s is BandwidthSample => s !== null)
          .reverse()
        setSamples(history.slice(-MAX_SAMPLES))
      } catch {
        /* history is best-effort; the stream below is the live source */
      }
      if (cancelled) return
      es = new EventSource(url('/api/telemetry/stream'))
      es.onopen = () => setStreaming(true)
      es.onmessage = (ev) => {
        if (phaseRef.current === 'download' || phaseRef.current === 'upload') return
        try {
          const sample = JSON.parse(ev.data) as BandwidthSample
          setSamples((prev) => [...prev.slice(-(MAX_SAMPLES - 1)), sample])
          setJitter((j) => Math.max(0.1, j + (1.5 - j) * 0.05 + (Math.random() * 0.4 - 0.2)))
        } catch {
          /* ignore malformed frames */
        }
      }
      es.onerror = () => {
        setStreaming(false)
        setStreamError(true)
      }
    })()
    return () => {
      cancelled = true
      es?.close()
    }
  }, [])

  // Test driver: while a phase runs, a 250 ms sampler eases the series
  // toward the phase target (26 ticks ≈ 6.5 s per phase).
  useEffect(() => {
    if (phase !== 'download' && phase !== 'upload') return
    let ticks = 0
    const iv = window.setInterval(() => {
      ticks += 1
      setSamples((prev) => {
        const last = prev[prev.length - 1] ?? { ts: 0, down_mbps: 24, up_mbps: 9 }
        const downT = phase === 'download' ? 2382 : 18
        const upT = phase === 'download' ? 14 : 2340
        const down = Math.max(0, last.down_mbps + (downT - last.down_mbps) * 0.14 + (Math.random() * 26 - 13))
        const up = Math.max(0, last.up_mbps + (upT - last.up_mbps) * 0.14 + (Math.random() * 18 - 9))
        return [
          ...prev.slice(-(MAX_SAMPLES - 1)),
          { ts: Date.now() / 1000, down_mbps: Math.round(down * 10) / 10, up_mbps: Math.round(up * 10) / 10 },
        ]
      })
      setJitter((j) => Math.max(0.1, j + (0.45 - j) * 0.06 + (Math.random() * 0.2 - 0.1)))
      if (ticks >= 26) {
        window.clearInterval(iv)
        if (phase === 'download') {
          setPhase('upload')
        } else {
          setPhase('done')
          post('/api/telemetry', { source: 'maclan-frontend', metric: 'bandwidth_test', value: 2340 }).catch(() => {})
          window.setTimeout(() => setPhase((p) => (p === 'done' ? 'idle' : p)), 5000)
        }
      }
    }, 250)
    return () => window.clearInterval(iv)
  }, [phase])

  // Draw the graph whenever samples change.
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || samples.length === 0) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const W = canvas.width
    const H = canvas.height
    ctx.clearRect(0, 0, W, H)
    const peak = Math.max(100, ...samples.map((s) => s.down_mbps)) * 1.15
    const x = (i: number) => (i / (MAX_SAMPLES - 1)) * (W - 8) + 4
    const y = (v: number) => H - 18 - (v / peak) * (H - 36)

    ctx.strokeStyle = '#3a3a40'
    ctx.fillStyle = '#a1a1a6'
    ctx.font = '11px ui-monospace, monospace'
    ctx.lineWidth = 1
    for (let f = 0; f <= 1.0001; f += 0.25) {
      const gy = y(peak * f)
      ctx.beginPath()
      ctx.moveTo(4, gy)
      ctx.lineTo(W - 4, gy)
      ctx.stroke()
      ctx.fillText(`${Math.round(peak * f)}`, 6, gy - 3)
    }
    const series = (key: 'down_mbps' | 'up_mbps', color: string, fill: boolean) => {
      ctx.beginPath()
      samples.forEach((s, i) => {
        const px = x(i + (MAX_SAMPLES - samples.length))
        const py = y(s[key])
        if (i === 0) ctx.moveTo(px, py)
        else ctx.lineTo(px, py)
      })
      ctx.strokeStyle = color
      ctx.lineWidth = 2
      ctx.stroke()
      if (fill) {
        const lastX = x(MAX_SAMPLES - 1)
        const firstX = x(MAX_SAMPLES - samples.length)
        ctx.lineTo(lastX, H - 18)
        ctx.lineTo(firstX, H - 18)
        ctx.closePath()
        ctx.fillStyle = 'rgba(48, 209, 88, 0.12)'
        ctx.fill()
      }
    }
    series('down_mbps', '#30d158', true)
    series('up_mbps', '#0a84ff', false)
  }, [samples])

  // Draw the gauge whenever samples or phase change.
  useEffect(() => {
    const canvas = gaugeRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const cur = samples[samples.length - 1]
    const upload = phase === 'upload'
    const active = cur ? (upload ? cur.up_mbps : cur.down_mbps) : 0
    const W = canvas.width
    const H = canvas.height
    const cx = W / 2
    const cy = H - 16
    const R = Math.min(W / 2 - 16, H - 40)
    const frac = Math.max(0, Math.min(1, active / 2500))
    const ang = (f: number) => Math.PI * (1 + f)
    ctx.clearRect(0, 0, W, H)
    ctx.lineCap = 'round'
    ctx.beginPath()
    ctx.arc(cx, cy, R, Math.PI, 2 * Math.PI)
    ctx.strokeStyle = '#3a3a40'
    ctx.lineWidth = 13
    ctx.stroke()
    if (frac > 0.004) {
      ctx.beginPath()
      ctx.arc(cx, cy, R, Math.PI, ang(frac))
      ctx.strokeStyle = upload ? '#0a84ff' : '#30d158'
      ctx.stroke()
    }
    ctx.fillStyle = '#a1a1a6'
    ctx.font = '11px ui-monospace, monospace'
    ctx.textAlign = 'center'
    for (let v = 0; v <= 2500; v += 500) {
      const a = ang(v / 2500)
      ctx.fillText(v === 0 ? '0' : (v / 1000).toFixed(1), cx + (R + 14) * Math.cos(a), cy + (R + 14) * Math.sin(a) + 3)
      ctx.strokeStyle = '#636366'
      ctx.lineWidth = 2
      ctx.beginPath()
      ctx.moveTo(cx + (R - 10) * Math.cos(a), cy + (R - 10) * Math.sin(a))
      ctx.lineTo(cx + (R - 20) * Math.cos(a), cy + (R - 20) * Math.sin(a))
      ctx.stroke()
    }
    ctx.fillText('Gbps', cx, cy - R - 8)
    const na = ang(frac)
    ctx.strokeStyle = '#f5f5f7'
    ctx.lineWidth = 3
    ctx.beginPath()
    ctx.moveTo(cx, cy)
    ctx.lineTo(cx + (R - 24) * Math.cos(na), cy + (R - 24) * Math.sin(na))
    ctx.stroke()
    ctx.beginPath()
    ctx.arc(cx, cy, 5, 0, 2 * Math.PI)
    ctx.fillStyle = '#f5f5f7'
    ctx.fill()
    ctx.textAlign = 'left'
  }, [samples, phase])

  const runTest = () => {
    if (phase === 'download' || phase === 'upload') return
    if (!cableConnected) {
      // Prototype behavior: the test refuses to run with the cable out.
      setPhaseNote('No link — cable unplugged')
      return
    }
    setPhaseNote(null)
    setPhase('download')
  }

  const current = samples[samples.length - 1]
  const testing = phase === 'download' || phase === 'upload'
  return (
    <div id="bandwidthPanel">
      <h3>Bandwidth Test</h3>
      <div className="cards">
        <div className="card"><h4>Download</h4><p>{current ? fmtRate(current.down_mbps) : '—'}</p></div>
        <div className="card"><h4>Upload</h4><p>{current ? fmtRate(current.up_mbps) : '—'}</p></div>
        <div className="card"><h4>Jitter</h4><p>{jitter.toFixed(2)} ms</p></div>
        <div className="card"><h4>Status</h4><p>{phaseNote ?? BW_PHASE_LABELS[phase]}</p></div>
        <div className="card">
          <h4>Stream</h4>
          <p>{streaming ? 'Live' : streamError ? 'Reconnecting…' : 'Connecting…'}</p>
        </div>
      </div>
      <div className="card block">
        <h4>Speed Test — 2.5 Gbps Ethernet (simulated)</h4>
        <div className="gaugewrap">
          <canvas ref={gaugeRef} width={300} height={175} className="gauge" />
          <div>
            <p style={{ fontSize: 30, fontWeight: 700, margin: 0 }}>
              {current ? fmtRate(phase === 'upload' ? current.up_mbps : current.down_mbps) : '—'}
            </p>
            <p className="muted small">{phase === 'upload' ? 'Upload' : 'Download'}</p>
            <button onClick={runTest} disabled={testing}>Run Test</button>
            <p className="muted small">
              Runs a download phase, then an upload phase.
              <br />
              Values ramp and settle like a real 2.5 GbE link.
            </p>
          </div>
        </div>
      </div>
      <div className="graphwrap">
        <canvas ref={canvasRef} width={840} height={240} className="bw" />
        <div className="legend small">
          <span><span className="dot" style={{ background: '#30d158' }} /> Download</span>
          <span><span className="dot" style={{ background: '#0a84ff' }} /> Upload</span>
          <span className="muted">Ambient: 1 sample/sec via SSE · test phases sample at 4/sec (simulated)</span>
        </div>
      </div>
      {samples.length === 0 && <p className="muted">Waiting for the first samples from the stream…</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// RF Spectrum Analyzer — 2.4 GHz band scan (added 2026-11-10 from the
// MacLAN1ZilTB spec/prototype): 40 BLE bins, 2402–2480 MHz in 2 MHz steps,
// over a ≈ -95 dBm noise floor. Gaussian humps model Wi-Fi channels 1/6/11
// and the narrow BLE advertising channels 37/38/39 (2402/2426/2480 MHz);
// connected Bluetooth devices light up their own bins (prototype
// behavior). Bar color sweeps cyan → warm with power; a peak-hold trace
// rides on top. Simulated data only; the scan interval is cleared on
// unmount, so it runs only while this panel is open.
// ---------------------------------------------------------------------------
const RF_BINS = 40
const RF_F0 = 2402
const RF_STEP = 2
const RF_TOP = -30
const RF_BOT = -100
const RF_HUMPS = [
  { bin: 0, amp: 34, width: 0.9, wobble: 0.9 },
  { bin: 5, amp: 22, width: 5.0, wobble: 0.25 },
  { bin: 12, amp: 31, width: 0.9, wobble: 1.1 },
  { bin: 17.5, amp: 28, width: 5.0, wobble: 0.35 },
  { bin: 30, amp: 20, width: 5.0, wobble: 0.2 },
  { bin: 39, amp: 38, width: 0.9, wobble: 1.3 },
]
function rfShape(i: number, frame: number): number {
  let v = -95 + Math.sin(frame * 0.02 + i * 0.35) * 1.2
  for (const h of RF_HUMPS) {
    const d = (i - h.bin) / h.width
    const breathe = 0.82 + 0.18 * Math.sin(frame * 0.045 * h.wobble + h.bin)
    v += h.amp * breathe * Math.exp(-(d * d))
  }
  return v
}

function Spectrum({ devices }: { devices: Device[] }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const peaks = useRef<number[]>(Array.from({ length: RF_BINS }, () => -100))
  const frame = useRef(0)
  const devicesRef = useRef(devices)
  devicesRef.current = devices
  const [peakText, setPeakText] = useState('—')
  const [floorText, setFloorText] = useState('—')

  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    const W = canvas.width
    const H = canvas.height
    const padL = 38
    const padR = 8
    const padT = 10
    const padB = 22
    const pw = W - padL - padR
    const ph = H - padT - padB
    const Y = (db: number) => padT + ((RF_TOP - db) / (RF_TOP - RF_BOT)) * ph
    ctx.clearRect(0, 0, W, H)
    ctx.strokeStyle = '#3a3a40'
    ctx.fillStyle = '#a1a1a6'
    ctx.font = '11px ui-monospace, monospace'
    ctx.lineWidth = 1
    for (let db = -40; db >= RF_BOT; db -= 20) {
      const gy = Y(db)
      ctx.beginPath()
      ctx.moveTo(padL, gy)
      ctx.lineTo(W - padR, gy)
      ctx.stroke()
      ctx.fillText(String(db), 8, gy + 3)
    }
    ctx.fillText('dBm', 8, padT + 3)
    const devState = (name: string) => devicesRef.current.find((x) => x.name === name)?.state
    const boosts: Record<number, number> = {}
    if (devState('AirPods Max') === 'connected') {
      boosts[12] = -42
      boosts[28] = -42
    }
    if (devState('1ZilTB Phone Connection') === 'connected') {
      boosts[24] = -35
      boosts[5] = -35
    }
    if (devState('MX Master 3S') === 'connected') boosts[36] = -58
    const binW = pw / RF_BINS
    const vals: number[] = []
    let peakDb = RF_BOT
    let peakBin = 0
    for (let i = 0; i < RF_BINS; i++) {
      let v = rfShape(i, frame.current) + (Math.random() * 5 - 2.5)
      if (boosts[i] !== undefined) v = Math.max(v, boosts[i] + (Math.random() * 10 - 5))
      if (Math.random() < 0.04) v += 8 + Math.random() * 10
      v = Math.max(RF_BOT, Math.min(RF_TOP, v))
      vals.push(v)
      peaks.current[i] = Math.max(v, peaks.current[i] - 0.3)
      if (v > peakDb) {
        peakDb = v
        peakBin = i
      }
      const x = padL + i * binW
      const norm = Math.max(0, Math.min(1, (v + 100) / 70))
      ctx.fillStyle = `hsla(${195 - norm * 140}, 85%, 55%, 0.85)`
      ctx.fillRect(x + 0.5, Y(v), binW - 1, Y(RF_BOT) - Y(v))
      ctx.fillStyle = '#ffd60a'
      ctx.fillRect(x + 0.5, Y(peaks.current[i]), binW - 1, 2)
    }
    ctx.fillStyle = '#a1a1a6'
    for (let i = 0; i < RF_BINS; i += 5) ctx.fillText(String(RF_F0 + i * RF_STEP), padL + i * binW - 4, H - 6)
    ctx.fillText('MHz', W - 32, H - 6)
    const floor = vals.slice().sort((a, b) => a - b).slice(0, 10).reduce((a, b) => a + b, 0) / 10
    setPeakText(`${RF_F0 + peakBin * RF_STEP} MHz · ${peakDb.toFixed(0)} dBm`)
    setFloorText(`${floor.toFixed(0)} dBm`)
  }, [])

  useEffect(() => {
    draw()
    const iv = window.setInterval(() => {
      frame.current += 1
      draw()
    }, 90)
    return () => window.clearInterval(iv)
  }, [draw])

  return (
    <div id="spectrumPanel">
      <h3>RF Spectrum Analyzer — 2.4 GHz</h3>
      <div className="cards">
        <div className="card"><h4>Peak Signal</h4><p>{peakText}</p></div>
        <div className="card"><h4>Noise Floor</h4><p>{floorText}</p></div>
        <div className="card"><h4>Band</h4><p>2402–2480 MHz</p></div>
        <div className="card"><h4>Bins</h4><p>40 × 2 MHz</p></div>
      </div>
      <div className="graphwrap">
        <canvas ref={canvasRef} width={840} height={260} className="bw" />
        <div className="legend small">
          <span><span className="dot" style={{ background: '#4cc3ff' }} /> RSSI per bin (color = power)</span>
          <span><span className="dot" style={{ background: '#ffd60a' }} /> Peak hold</span>
          <button
            className="ghost"
            onClick={() => {
              peaks.current.fill(-100)
              draw()
            }}
          >
            Reset peak hold
          </button>
          <span className="muted">
            Simulated scan — BLE advertising channels 37/38/39 spike above the Wi-Fi humps, and connected
            Bluetooth devices light up their own bins (try Devices).
          </span>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Telemetry viewer
// ---------------------------------------------------------------------------
function Telemetry({
  events,
  onRefresh,
  onSendTest,
}: {
  events: TelemetryEvent[]
  onRefresh: () => void
  onSendTest: () => void
}) {
  return (
    <div>
      <h3>Telemetry Events</h3>
      <div className="row">
        <button onClick={onSendTest}>Send test event</button>
        <button onClick={onRefresh}>Refresh</button>
      </div>
      {events.length === 0 ? (
        <p className="muted">No events yet. The UI posts a <code>ui_heartbeat</code> on load.</p>
      ) : (
        <table className="tbl">
          <thead><tr><th>ID</th><th>Time</th><th>Source</th><th>Metric</th><th>Value</th></tr></thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.id}>
                <td>{e.id}</td>
                <td className="mono">{new Date(e.ts * 1000).toLocaleTimeString()}</td>
                <td>{e.source}</td>
                <td>{e.metric}</td>
                <td>{e.value ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// App shell
// ---------------------------------------------------------------------------
export default function App() {
  const [tab, setTab] = useState<TabId>('overview')
  const [status, setStatus] = useState<NetworkStatus | null>(null)
  const [config, setConfig] = useState<NetworkConfig | null>(null)
  const [devices, setDevices] = useState<Device[]>([])
  const [events, setEvents] = useState<TelemetryEvent[]>([])
  const [error, setError] = useState<string | null>(null)

  const loadAll = useCallback(async () => {
    try {
      const [st, cfg, dv, ev] = await Promise.all([
        api<NetworkStatus>('/api/network/status'),
        api<NetworkConfig>('/api/network/config'),
        api<Device[]>('/api/devices'),
        api<TelemetryEvent[]>('/api/telemetry?limit=25'),
      ])
      setStatus(st)
      setConfig(cfg)
      setDevices(dv)
      setEvents(ev)
      setError(null)
    } catch {
      setError('Backend unreachable — is FastAPI running on :8000?')
    }
  }, [])

  useEffect(() => {
    loadAll()
    post('/api/telemetry', {
      source: 'maclan-frontend',
      metric: 'ui_heartbeat',
      value: 1,
    }).catch(() => {})
  }, [loadAll])

  const onAction = async (device: Device, action: DeviceAction) => {
    try {
      await post(`/api/devices/${device.id}/${action}`)
    } catch (e) {
      setError((e as Error).message)
    }
    loadAll()
  }

  const onBluetooth = async (patch: Partial<BluetoothSettings>) => {
    try {
      const cfg = await api<NetworkConfig>('/api/bluetooth', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      })
      setConfig(cfg)
    } catch (e) {
      setError((e as Error).message)
    }
    loadAll()
  }

  const sendTest = async () => {
    await post('/api/telemetry', {
      source: 'maclan-frontend',
      metric: 'manual_test',
      value: Math.random(),
    })
    loadAll()
  }

  return (
    <div className="window">
      <aside className="sidebar">
        <div className="traffic"><span /><span /><span /></div>
        <h1>MacLAN</h1>
        <p className="muted small">Network Settings · Phase 1 ✓</p>
        <nav>
          {NAV.map((n) => (
            <button
              key={n.id}
              className={tab === n.id ? 'nav active' : 'nav'}
              onClick={() => setTab(n.id)}
            >
              <span>{n.icon}</span> {n.label}
            </button>
          ))}
        </nav>
        <div className="sidefoot muted small">
          Backend: FastAPI · SQLite<br />
          Phase 2 · RF spectrum live<br />
          Cloud: 1ZilTBCloud (later phase)
        </div>
      </aside>
      <main className="panel">
        {error && <div className="error">{error}</div>}
        {tab === 'overview' && <Overview status={status} />}
        {tab === 'tcpip' &&
          (config ? (
            <TcpIpPanel config={config} onConfig={setConfig} onRefresh={loadAll} />
          ) : (
            <p className="muted">Loading configuration…</p>
          ))}
        {tab === 'devices' &&
          (config ? (
            <Devices
              devices={devices}
              bluetooth={config.bluetooth}
              onAction={onAction}
              onBluetooth={onBluetooth}
            />
          ) : (
            <p className="muted">Loading devices…</p>
          ))}
        {tab === 'ping' && (
          <PingTester
            cableConnected={config?.cable_connected ?? true}
            gateway={config?.effective.router ?? '192.168.0.1'}
          />
        )}
        {tab === 'bandwidth' && <Bandwidth cableConnected={config?.cable_connected ?? true} />}
        {tab === 'spectrum' && <Spectrum devices={devices} />}
        {tab === 'telemetry' && (
          <Telemetry events={events} onRefresh={loadAll} onSendTest={sendTest} />
        )}
      </main>
    </div>
  )
}
