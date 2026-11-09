// Shared types for the MacLAN front end (Increment 2 — TypeScript).
// Shapes mirror the FastAPI backend's JSON responses.

export interface NetInterface {
  id: number
  name: string
  kind: 'ethernet' | 'bluetooth' | 'wifi' | 'thunderbolt' | string
  status: string
  detail: string | null
}

export interface NetworkSummary {
  primary_interface: string
  ip_address: string | null
  subnet_mask: string | null
  router: string | null
  dns: string[]
  tcpip_mode: 'dhcp' | 'manual'
  cable_connected: boolean
  bluetooth_power: boolean
  devices_total: number
  devices_connected: number
  link_speed_mbps: number
  note: string
}

export interface NetworkStatus {
  interfaces: NetInterface[]
  summary: NetworkSummary
}

export interface Device {
  id: number
  name: string
  kind: 'bluetooth' | 'lan' | string
  role: string
  mac: string | null
  ip: string | null
  state: 'unpaired' | 'paired' | 'connected' | string
  battery: number | null
  last_seen: number | null
}

export type DeviceAction = 'pair' | 'connect' | 'disconnect' | 'unpair'

export interface TelemetryEvent {
  id: number
  ts: number
  source: string
  metric: string
  value: number | null
  payload: string | null // JSON-encoded string, as stored by the backend
}

export interface EffectiveNetwork {
  ip_address: string | null
  subnet_mask: string | null
  router: string | null
  dns: string[]
  link_speed_mbps: number
  state: string
}

export interface BluetoothSettings {
  power: boolean
  discoverable: boolean
  name: string
}

export interface NetworkConfig {
  tcpip: {
    mode: 'dhcp' | 'manual'
    manual: {
      ip_address: string | null
      subnet_mask: string | null
      router: string | null
      dns: string[]
    }
    dhcp_lease: {
      ip_address: string
      leased_at: number | null
    }
  }
  cable_connected: boolean
  bluetooth: BluetoothSettings
  effective: EffectiveNetwork
}

export interface BandwidthSample {
  ts: number
  down_mbps: number
  up_mbps: number
}

export interface PingResult {
  target: string
  sent: number
  received: number
  loss_pct: number
  times_ms: number[]
  avg_ms: number
  simulated: boolean
}
