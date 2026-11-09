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
// Bluetooth diagnostics (HCI/RF/sniffer) remain Phase 2.

const NAV = [
  { id: 'overview', label: 'Overview', icon: '🌐' },
  { id: 'tcpip', label: 'TCP/IP', icon: '🔌' },
  { id: 'devices', label: 'Devices', icon: '🖥️' },
  { id: 'ping', label: 'Ping Tester', icon: '📡' },
  { id: 'bandwidth', label: 'Bandwidth Meter', icon: '📊' },
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
// Ping tester
// ---------------------------------------------------------------------------
function PingTester() {
  const [target, setTarget] = useState('192.168.0.1')
  const [result, setResult] = useState<PingResult | null>(null)
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    try {
      setResult(await post<PingResult>('/api/diagnostics/ping', { target, count: 4 }))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div>
      <h3>Ping Tester</h3>
      <div className="row">
        <input value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Ping target" />
        <button onClick={run} disabled={busy}>{busy ? 'Pinging…' : 'Ping'}</button>
      </div>
      {result && (
        <pre className="terminal">
{`PING ${result.target}
${result.times_ms.map((t, i) => `64 bytes from ${result.target}: icmp_seq=${i} time=${t} ms`).join('\n')}
--- ${result.target} ping statistics ---
${result.sent} packets transmitted, ${result.received} received, ${result.loss_pct}% packet loss
avg = ${result.avg_ms} ms  (simulated)`}
        </pre>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Bandwidth meter — live graph streaming from the telemetry SSE endpoint
// ---------------------------------------------------------------------------
const MAX_SAMPLES = 90

function Bandwidth() {
  const [samples, setSamples] = useState<BandwidthSample[]>([])
  const [streaming, setStreaming] = useState(false)
  const [streamError, setStreamError] = useState(false)
  const canvasRef = useRef<HTMLCanvasElement>(null)

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
        try {
          const sample = JSON.parse(ev.data) as BandwidthSample
          setSamples((prev) => [...prev.slice(-(MAX_SAMPLES - 1)), sample])
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

  const current = samples[samples.length - 1]
  return (
    <div>
      <h3>Bandwidth Meter</h3>
      <div className="cards">
        <div className="card"><h4>Download</h4><p>{current ? `${current.down_mbps} Mbps` : '—'}</p></div>
        <div className="card"><h4>Upload</h4><p>{current ? `${current.up_mbps} Mbps` : '—'}</p></div>
        <div className="card">
          <h4>Stream</h4>
          <p>{streaming ? 'Live' : streamError ? 'Reconnecting…' : 'Connecting…'}</p>
        </div>
        <div className="card"><h4>Samples</h4><p>{samples.length}</p></div>
      </div>
      <div className="graphwrap">
        <canvas ref={canvasRef} width={840} height={240} className="bw" />
        <div className="legend small">
          <span><span className="dot" style={{ background: '#30d158' }} /> Download</span>
          <span><span className="dot" style={{ background: '#0a84ff' }} /> Upload</span>
          <span className="muted">1 sample/sec via SSE · persisted as telemetry (metric=bandwidth)</span>
        </div>
      </div>
      {samples.length === 0 && <p className="muted">Waiting for the first samples from the stream…</p>}
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
          Phase 2 diagnostics next<br />
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
        {tab === 'ping' && <PingTester />}
        {tab === 'bandwidth' && <Bandwidth />}
        {tab === 'telemetry' && (
          <Telemetry events={events} onRefresh={loadAll} onSendTest={sendTest} />
        )}
      </main>
    </div>
  )
}
