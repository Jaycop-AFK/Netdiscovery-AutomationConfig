'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

const API = '/api';
const api = async (path, options = {}) => {
  const response = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json' }, ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
};
const esc = (v) => String(v ?? '');

function Icon({ name, size = 18 }) {
  const common = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.7, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true };
  const paths = {
    grid: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
    router: <><path d="M4 8h16M4 16h16"/><path d="m8 5-4 3 4 3M16 13l4 3-4 3"/><circle cx="12" cy="8" r="1"/><circle cx="12" cy="16" r="1"/></>,
    search: <><circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 5 5"/></>,
    terminal: <><path d="m4 6 6 6-6 6M12 18h8"/></>,
    activity: <><path d="M3 12h4l3-8 4 16 3-8h4"/></>,
    plus: <><path d="M12 5v14M5 12h14"/></>,
    refresh: <><path d="M20 7v5h-5"/><path d="M4 17v-5h5"/><path d="M5.8 9A7 7 0 0 1 18 6l2 6M4 12l2 6a7 7 0 0 0 12.2-3"/></>,
    ping: <><path d="M3 12h4l2-5 4 10 2-5h6"/></>,
    close: <><path d="m6 6 12 12M18 6 6 18"/></>,
  };
  return <svg {...common}>{paths[name] || paths.grid}</svg>;
}

function Modal({ title, eyebrow, onClose, children, wide = false }) {
  return <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}><section className={`modal ${wide ? 'modal-wide' : ''}`} role="dialog" aria-modal="true"><header className="modal-head"><div><div className="eyebrow">{eyebrow}</div><h2>{title}</h2></div><button className="icon-button" onClick={onClose} aria-label="Close"><Icon name="close" /></button></header>{children}</section></div>;
}

function DeviceIcon({ kind = 'Router' }) {
  return <span className={`device-glyph ${kind.toLowerCase() === 'router' ? 'router-glyph' : ''}`}><Icon name={kind.toLowerCase() === 'router' ? 'router' : 'grid'} size={21} /></span>;
}

function Topology({ devices, discovery, onSelect }) {
  const found = discovery.flatMap((r) => (r.peers || []).map((peer) => ({ from: r.device, to: peer.name, peer })));
  const names = [...new Set([...devices.map((d) => d.name), ...found.map((e) => e.to)])];
  const points = names.map((name, i) => ({ name, device: devices.find((d) => d.name === name), x: names.length < 2 ? 50 : 12 + i * (76 / (names.length - 1)), y: i % 2 ? 67 : 38 }));
  const byName = new Map(points.map((p) => [p.name, p]));
  return <div className="topology" aria-label="Network topology">
    <div className="topo-grid" />
    <svg className="topo-lines" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">{found.map((edge, i) => { const a = byName.get(edge.from), b = byName.get(edge.to); return a && b ? <line key={`${edge.from}-${edge.to}-${i}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} /> : null; })}</svg>
    {points.map((p) => <button key={p.name} className="topo-node" style={{ left: `${p.x}%`, top: `${p.y}%` }} onClick={() => onSelect(p.device || { name: p.name, kind: 'Discovered neighbor', host: found.find((e) => e.to === p.name)?.peer.host, interfaces: [] })}>
      <DeviceIcon kind={p.device?.kind || 'Router'} /><b>{p.name}</b><small>{p.device?.host || found.find((e) => e.to === p.name)?.peer.host || 'CDP neighbor'}</small><span className={p.device?.status === 'online' ? 'node-live' : 'node-pending'}>{p.device?.status === 'online' ? '● online' : p.device ? '● unverified' : '● discovered'}</span>
    </button>)}
    {!points.length && <div className="topo-empty"><span className="empty-orbit">⌁</span><b>Topology is waiting for discovery</b><small>Add at least three devices, then run Auto Discovery.</small></div>}
    <div className="topo-legend"><span><i className="green-dot" />Online</span><span><i className="amber-dot" />Unverified</span><span>CDP neighbor links</span></div>
  </div>;
}

function InterfaceDiagram({ device, interfaces }) {
  if (!interfaces?.length) return <div className="empty-interfaces">Interface status is unavailable. Run discovery over SSH to collect `show ip interface brief`.</div>;
  return <div className="chassis"><div className="chassis-face"><div className="chassis-brand">{device.name}<small>{device.kind} · front panel</small></div><div className="port-grid">{interfaces.map((port) => <div className={`physical-port ${port.status === 'up' ? 'port-up' : 'port-down'}`} key={port.name} title={`${port.name} · ${port.status}`}><span className="port-led"/><b>{port.name}</b><small>{port.status || 'unknown'}</small></div>)}</div></div><div className="chassis-ears" /></div>;
}

export default function Home() {
  const [devices, setDevices] = useState([]), [discovery, setDiscovery] = useState([]), [activity, setActivity] = useState([]);
  const [backend, setBackend] = useState(false), [notice, setNotice] = useState(''), [busy, setBusy] = useState('');
  const [initialOpen, setInitialOpen] = useState(false), [selectedDevice, setSelectedDevice] = useState(null), [confirmOpen, setConfirmOpen] = useState(false);
  const [target, setTarget] = useState(''), [operation, setOperation] = useState('ip'), [params, setParams] = useState('GigabitEthernet0/1 10.10.10.1/24');
  const [newDevice, setNewDevice] = useState({ name: '', kind: 'Router', connection: 'SSH', host: '', username: 'admin', password: '', port: '22', baud: '9600' });
  const [pingHost, setPingHost] = useState('');
  const load = useCallback(async () => { try { const [d, a] = await Promise.all([api('/devices'), api('/activity')]); setDevices(d.devices || []); setActivity(a.activity || []); setBackend(true); setNotice(''); } catch (e) { setBackend(false); setNotice(`Backend is not reachable: ${e.message}`); } }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { if (devices.length && !devices.some((d) => String(d.id) === target)) setTarget(String(devices[0].id)); }, [devices, target]);
  const logs = useMemo(() => activity.slice(0, 5), [activity]);
  const interfaceCount = discovery.flatMap((r) => r.interfaces || []);
  const upCount = interfaceCount.filter((p) => p.status === 'up').length;
  const commandList = useMemo(() => {
    const bits = params.trim().split(/[;,\s]+/).filter(Boolean), iface = bits[0] || 'GigabitEthernet0/1', cidr = bits[1] || '10.10.10.1/24', [ip, prefix = '24'] = cidr.split('/');
    const mask = (n) => { const m = (0xffffffff << (32 - Number(n))) >>> 0; return [24,16,8,0].map((s) => (m >>> s) & 255).join('.'); };
    const network = ip.split('.').map((v, i) => i < 3 ? v : '0').join('.');
    if (operation === 'ip') return [`interface ${iface}`, `ip address ${ip} ${mask(prefix)}`, 'no shutdown', 'exit'];
    if (operation === 'up') return [`interface ${iface}`, 'no shutdown', 'exit'];
    if (operation === 'ospf') return [`router ospf ${bits[0] || '1'}`, `network ${bits[1] || network} ${bits[2] || '0.0.0.255'} area ${bits[3] || '0'}`];
    if (operation === 'rip') return ['router rip', 'version 2', `network ${bits[0] || network}`, 'no auto-summary'];
    if (operation === 'eigrp') return [`router eigrp ${bits[0] || '100'}`, `network ${bits[1] || network} ${bits[2] || '0.0.0.255'}`, 'no auto-summary'];
    return [`ip route ${bits[0] || '10.10.20.0'} ${bits[1] || '255.255.255.0'} ${bits[2] || '10.10.10.2'}`];
  }, [operation, params]);
  const toast = (message) => { setNotice(message); window.setTimeout(() => setNotice(''), 4500); };
  const doDiscovery = async () => { if (devices.length < 3) return toast(`Add at least 3 devices first. Current: ${devices.length}.`); setBusy('discovery'); try { const r = await api('/discovery/run', { method: 'POST', body: '{}' }); setDiscovery(r.results || []); const peerCount = (r.results || []).reduce((n, row) => n + (row.peers || []).length, 0); toast(`Discovery finished · ${peerCount} CDP neighbors found.`); await load(); } catch (e) { toast(`Discovery failed: ${e.message}`); } finally { setBusy(''); } };
  const addDevice = async (e) => { e.preventDefault(); setBusy('initial'); try { const r = await api('/devices', { method: 'POST', body: JSON.stringify({ ...newDevice, port: Number(newDevice.port), baud: Number(newDevice.baud) }) }); setInitialOpen(false); setNewDevice((v) => ({ ...v, password: '' })); toast(r.connected ? `${r.device.name} connected.` : `${r.device.name} saved; connection could not be verified.`); await load(); } catch (error) { toast(`Initial config failed: ${error.message}`); } finally { setBusy(''); } };
  const sendConfig = async () => { if (!target) return toast('Add a device through Initial Config first.'); setBusy('config'); try { const r = await api(`/devices/${target}/config`, { method: 'POST', body: JSON.stringify({ commands: commandList, approved: true }) }); setConfirmOpen(false); toast(`Commands submitted. Review device output in the browser console.`); if (r.output) console.info(r.output); await load(); } catch (e) { toast(`Config failed: ${e.message}`); } finally { setBusy(''); } };
  const doPing = async (host) => { if (!host?.trim()) return toast('Enter a destination address.'); setBusy('ping'); try { const r = await api('/ping', { method: 'POST', body: JSON.stringify({ host: host.trim() }) }); toast(r.reachable ? `Ping passed · ${host}` : `Ping failed · ${host}`); await load(); } catch (e) { toast(`Ping failed: ${e.message}`); } finally { setBusy(''); } };
  const totalPeers = discovery.reduce((n, row) => n + (row.peers || []).length, 0);
  return <main className="app-shell">
    <aside className="sidebar"><a className="brand" href="#top"><span className="brand-mark"><i /></span><span>NetScope<small>DISCOVERY LAB</small></span></a><div className="side-group"><span className="side-label">WORKSPACE</span><a className="side-link active" href="#top"><Icon name="grid"/> Overview</a><a className="side-link" href="#topology"><Icon name="router"/> Topology</a><a className="side-link" href="#devices"><Icon name="search"/> Devices</a><a className="side-link" href="#config"><Icon name="terminal"/> Config console</a><a className="side-link" href="#activity"><Icon name="activity"/> Activity</a></div><div className="sidebar-foot"><span className={`connection-led ${backend ? 'is-live' : ''}`} id="sideDot"/><div><b>{backend ? 'Backend connected' : 'Backend offline'}</b><small>Local network bridge · :8080</small></div><span className="session-chip">LAB</span></div></aside>
    <section className="main-content" id="top"><header className="topbar"><div><div className="eyebrow">NETWORK OPERATIONS · LAB 01</div><h1>Discovery dashboard</h1><p>Map, inspect and configure your network from one workspace.</p></div><div className="top-actions"><button className="btn btn-quiet" onClick={load} aria-label="Refresh"><Icon name="refresh"/></button><button className="btn btn-soft" onClick={() => setInitialOpen(true)}><Icon name="plus"/> Initial config</button><button className="btn btn-primary" onClick={doDiscovery} disabled={busy==='discovery'}><Icon name="search"/>{busy==='discovery'?'Discovering…':'Run auto discovery'}</button></div></header>
      {notice && <div className={`notice-bar ${backend?'':'notice-error'}`} role="status">{notice}<button onClick={() => setNotice('')} aria-label="Dismiss"><Icon name="close" size={15}/></button></div>}
      <section className="metrics"><article className="metric-card"><div className="metric-label">Managed devices <span className="metric-glyph orange">⌁</span></div><strong>{String(devices.length).padStart(2,'0')}</strong><small>{devices.length>=3?'Ready for discovery':'Add 3+ devices to begin'}</small></article><article className="metric-card"><div className="metric-label">Interfaces up <span className="metric-glyph mint">⌁</span></div><strong>{discovery.length?`${upCount}<em> / ${interfaceCount.length}</em>`:'—'}</strong><small>{discovery.length?'From latest device scan':'Awaiting interface scan'}</small></article><article className="metric-card"><div className="metric-label">CDP neighbors <span className="metric-glyph blue">⌘</span></div><strong>{discovery.length?String(totalPeers).padStart(2,'0'):'—'}</strong><small>{discovery.length?'Live discovery results':'Not discovered yet'}</small></article><article className="metric-card"><div className="metric-label">Last discovery <span className="metric-glyph amber">◷</span></div><strong className="metric-time">{discovery.length?'Complete':'—'}</strong><small>{discovery.length?'Current session':'Waiting for first run'}</small></article></section>
      <section className="dashboard-grid"><article className="panel topology-panel" id="topology"><header className="panel-head"><div><div className="panel-title">Live network topology</div><p>Click a device to inspect its interfaces and status.</p></div><span className={`glass-pill ${discovery.length?'is-live':''}`}><i/>{discovery.length?'DISCOVERED':'NOT SCANNED'}</span></header><Topology devices={devices} discovery={discovery} onSelect={setSelectedDevice}/><footer className="topology-foot"><span><b>{devices.length+totalPeers}</b> nodes on map</span><span><b>{totalPeers}</b> neighbor links</span><span>Discovery source <b>CDP / SSH</b></span></footer></article>
        <div className="right-column"><article className="panel devices-panel" id="devices"><header className="panel-head"><div><div className="panel-title">Managed devices</div><p>Initial config and connection state</p></div><button className="icon-button small" onClick={() => setInitialOpen(true)} aria-label="Add device"><Icon name="plus"/></button></header><div className="device-list">{devices.length?devices.map((d)=><button className="device-row" key={d.id} onClick={()=>setSelectedDevice(d)}><DeviceIcon kind={d.kind}/><span className="device-meta"><b>{d.name}<i>{d.kind}</i></b><small>{d.connection} · {d.host}</small></span><span className={`status-light ${d.status==='online'?'online':''}`} title={d.status}/></button>):<div className="empty-list">No devices configured yet.</div>}</div><button className="text-action" onClick={()=>setInitialOpen(true)}><Icon name="plus" size={14}/> Add a device</button></article>
          <article className="panel activity-panel" id="activity"><header className="panel-head"><div><div className="panel-title">Recent activity</div><p>Events from this lab session</p></div><span className="activity-pulse"/></header><div className="activity-list">{logs.length?logs.map((item)=><div className="activity-item" key={item.id}><span className="activity-icon">↗</span><span><b>{item.title}</b><small>{item.detail}</small></span><time>{new Date(item.created_at).toLocaleTimeString('th-TH',{hour:'2-digit',minute:'2-digit'})}</time></div>):<div className="empty-list">No activity recorded.</div>}</div></article></div>
      </section>
        <section className="lower-grid"><article className="panel config-panel" id="config"><header className="panel-head"><div><div className="panel-title">Configuration console</div><p>Build commands, review them, then explicitly approve submission.</p></div><span className="approval-tag">APPROVAL REQUIRED</span></header><div className="console-layout"><div className="config-form"><label>Target device<select value={target} onChange={(e)=>setTarget(e.target.value)}>{devices.map((d)=><option key={d.id} value={d.id}>{d.name} · {d.connection}</option>)}{!devices.length&&<option value="">No devices available</option>}</select></label><div className="form-pair"><label>Operation<select value={operation} onChange={(e)=>{const op=e.target.value;setOperation(op);setParams({ip:'GigabitEthernet0/1 10.10.10.1/24',up:'GigabitEthernet0/1',ospf:'1 10.10.10.0 0.0.0.255 0',rip:'10.10.10.0',eigrp:'100 10.10.10.0 0.0.0.255',static:'10.10.20.0 255.255.255.0 10.10.10.2'}[op]);}}><option value="ip">Assign interface IP</option><option value="up">No shutdown interface</option><option value="ospf">Configure OSPF</option><option value="rip">Configure RIP</option><option value="eigrp">Configure EIGRP</option><option value="static">Add static route</option></select></label><label>Parameters<input value={params} onChange={(e)=>setParams(e.target.value)} placeholder="Interface, IP/prefix, or routing values"/></label></div><div className="form-hint">For IP: <code>GigabitEthernet0/1 10.10.10.1/24</code> · For OSPF: <code>1 10.10.10.0 0.0.0.255 0</code></div><button className="btn btn-primary preview-button" onClick={()=>setConfirmOpen(true)} disabled={!devices.length}><Icon name="terminal"/> Preview commands</button></div><pre className="command-preview"><span className="comment-line">! Target: {devices.find(d=>String(d.id)===target)?.name||'select device'}</span>{commandList.map((line,i)=><span className="command-line" key={`${line}-${i}`}>{line}</span>)}</pre></div></article>
        <article className="panel ping-panel"><header className="panel-head"><div><div className="panel-title">Connectivity test</div><p>Ping a device or endpoint from the backend host.</p></div><span className="ping-symbol"><Icon name="ping"/></span></header><label>Destination address<input value={pingHost} onChange={(e)=>setPingHost(e.target.value)} onKeyDown={(e)=>e.key==='Enter'&&doPing(pingHost)} placeholder="10.10.20.1"/></label><button className="btn btn-soft ping-button" disabled={busy==='ping'} onClick={()=>doPing(pingHost)}><Icon name="ping"/>{busy==='ping'?'Testing…':'Run ping test'}</button><small className="ping-note">Sends four packets using the server’s system ping utility.</small></article></section>
      <footer className="page-footer"><span>NETSCOPE <i>·</i> LOCAL LAB WORKSPACE</span><span>Network changes are submitted only after operator approval.</span></footer>
    </section>

    {initialOpen&&<Modal title="Connect a network device" eyebrow="ONBOARDING · INITIAL CONFIG" onClose={()=>setInitialOpen(false)}><p className="modal-copy">Register the device connection through SSH or a local serial console before discovery.</p><form className="modal-form" onSubmit={addDevice}><div className="field-pair"><label>Device name<input required value={newDevice.name} onChange={(e)=>setNewDevice({...newDevice,name:e.target.value})} placeholder="R3-DIST"/></label><label>Device type<select value={newDevice.kind} onChange={(e)=>setNewDevice({...newDevice,kind:e.target.value})}><option>Router</option><option>Switch</option></select></label></div><div className="field-pair"><label>Connection<select value={newDevice.connection} onChange={(e)=>setNewDevice({...newDevice,connection:e.target.value})}><option>SSH</option><option>Console</option></select></label><label>{newDevice.connection==='SSH'?'Management IP':'COM port'}<input required value={newDevice.host} onChange={(e)=>setNewDevice({...newDevice,host:e.target.value})} placeholder={newDevice.connection==='SSH'?'10.10.10.1':'COM3'}/></label></div><div className="field-pair">{newDevice.connection==='SSH'?<><label>Username<input value={newDevice.username} onChange={(e)=>setNewDevice({...newDevice,username:e.target.value})}/></label><label>Password<input type="password" value={newDevice.password} onChange={(e)=>setNewDevice({...newDevice,password:e.target.value})} autoComplete="new-password"/></label></>:<><label>Baud rate<input type="number" value={newDevice.baud} onChange={(e)=>setNewDevice({...newDevice,baud:e.target.value})}/></label><label>Console mode<span className="readonly-field">Local serial port</span></label></>}</div>{newDevice.connection==='SSH'&&<label>SSH port<input type="number" value={newDevice.port} onChange={(e)=>setNewDevice({...newDevice,port:e.target.value})}/></label>}<div className="secure-note">Credentials stay in backend process memory and are not stored in the device database.</div><div className="modal-actions"><button type="button" className="btn btn-quiet" onClick={()=>setInitialOpen(false)}>Cancel</button><button className="btn btn-primary" disabled={busy==='initial'}>{busy==='initial'?'Connecting…':'Initialize & connect'}</button></div></form></Modal>}
    {confirmOpen&&<Modal title="Review configuration" eyebrow="OPERATOR APPROVAL" onClose={()=>setConfirmOpen(false)}><p className="modal-copy">These commands will be sent to <b>{devices.find(d=>String(d.id)===target)?.name}</b>. Confirm the interface, addressing, and routing values before submitting.</p><pre className="confirm-commands">{commandList.join('\n')}</pre><div className="modal-actions"><button className="btn btn-quiet" onClick={()=>setConfirmOpen(false)}>Back to edit</button><button className="btn btn-primary" onClick={sendConfig} disabled={busy==='config'}>{busy==='config'?'Submitting…':'Approve & submit'}</button></div></Modal>}
    {selectedDevice&&<Modal title={selectedDevice.name} eyebrow="DEVICE INSPECTION" onClose={()=>setSelectedDevice(null)} wide><p className="modal-copy">{selectedDevice.kind||'Discovered neighbor'} · {selectedDevice.connection||'CDP neighbor'} · {selectedDevice.host||'Management address not reported'}</p><InterfaceDiagram device={selectedDevice} interfaces={discovery.find((r)=>r.device===selectedDevice.name)?.interfaces||selectedDevice.interfaces||[]}/><div className="modal-actions"><button className="btn btn-soft" onClick={()=>doPing(selectedDevice.host)} disabled={!selectedDevice.host}><Icon name="ping"/> Ping device</button><button className="btn btn-primary" onClick={()=>{setSelectedDevice(null);document.getElementById('config').scrollIntoView({behavior:'smooth'});if(selectedDevice.id)setTarget(String(selectedDevice.id));}}>Open config console</button></div></Modal>}
    <div className={`toast ${notice?'toast-visible':''}`} role="status">{notice}</div>
  </main>;
}
