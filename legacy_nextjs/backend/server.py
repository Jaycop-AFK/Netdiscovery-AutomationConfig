from __future__ import annotations
import json, os, re, sqlite3, subprocess, threading, time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / 'netscope.db'
HOST, PORT = '0.0.0.0', int(os.getenv('NETSCOPE_PORT', '8080'))
SESSIONS: dict[int, dict] = {}
LOCK = threading.Lock()


def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS devices(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,
            connection TEXT NOT NULL, host TEXT NOT NULL, username TEXT, status TEXT NOT NULL,
            last_seen TEXT, created_at TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS activity(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, detail TEXT NOT NULL,
            level TEXT NOT NULL, created_at TEXT NOT NULL)''')

def log(title, detail, level='info'):
    with db() as c: c.execute('INSERT INTO activity(title,detail,level,created_at) VALUES(?,?,?,?)', (title, detail, level, now()))

def device(row):
    x = dict(row)
    x['credentials_in_memory'] = x['id'] in SESSIONS
    return x

def all_devices():
    with db() as c: return [device(x) for x in c.execute('SELECT * FROM devices ORDER BY id')]

def json_body(handler):
    n = int(handler.headers.get('Content-Length', '0'))
    try: return json.loads(handler.rfile.read(n) or b'{}')
    except json.JSONDecodeError: raise ValueError('Invalid JSON')

def ssh_test(host, username, password, port=22):
    try:
        import paramiko
    except ImportError: return False, 'à¸¢à¸±à¸‡à¹„à¸¡à¹ˆà¹„à¸”à¹‰à¸•à¸´à¸”à¸•à¸±à¹‰à¸‡ paramiko'
    client = paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, port=port, username=username, password=password, timeout=8, look_for_keys=False, allow_agent=False)
        _, stdout, _ = client.exec_command('show version', timeout=8)
        output = stdout.read().decode(errors='replace')[:200]
        return True, output or 'SSH connected'
    except Exception as e: return False, str(e)
    finally: client.close()

def ssh_commands(host, username, password, commands, port=22):
    import paramiko
    client = paramiko.SSHClient(); client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, timeout=10, look_for_keys=False, allow_agent=False)
    shell = client.invoke_shell(); time.sleep(.8)
    if shell.recv_ready(): shell.recv(65535)
    for command in commands:
        shell.send(command.rstrip() + '\n'); time.sleep(.45)
    time.sleep(1.2); output = b''
    while shell.recv_ready(): output += shell.recv(65535)
    client.close(); return output.decode(errors='replace')[-10000:]

def console_commands(port, baud, commands):
    import serial
    with serial.Serial(port, baudrate=baud, timeout=1) as ser:
        for command in commands: ser.write((command.rstrip() + '\r\n').encode()); time.sleep(.4)
        time.sleep(1); return ser.read(10000).decode(errors='replace')

def run_ping(host):
    flag = '-n' if os.name == 'nt' else '-c'
    try:
        p = subprocess.run(['ping', flag, '4', host], capture_output=True, text=True, timeout=15)
        return {'reachable': p.returncode == 0, 'output': p.stdout[-4000:] or p.stderr[-1000:], 'returncode': p.returncode}
    except Exception as e: return {'reachable': False, 'output': str(e), 'returncode': -1}

def parse_cdp(output):
    peers = []
    for block in re.split(r'\n\s*\n', output):
        name = re.search(r'Device ID:\s*([^\s,]+)', block, re.I)
        ip = re.search(r'IP address:\s*([0-9.]+)', block, re.I)
        if name: peers.append({'name': name.group(1), 'host': ip.group(1) if ip else '', 'raw': block[:300]})
    return peers

def parse_interfaces(output):
    interfaces = []
    for line in output.splitlines():
        # Cisco IOS: Interface IP-Address OK? Method Status Protocol
        match = re.match(r'^\s*(\S+)\s+([\d.]+|unassigned)\s+\S+\s+\S+\s+(\S+)\s+(\S+)\s*$', line, re.I)
        if match and match.group(1).lower() != 'interface':
            interfaces.append({'name': match.group(1), 'ip': match.group(2),
                               'status': 'up' if match.group(3).lower() == 'up' and match.group(4).lower() == 'up' else 'down',
                               'protocol': match.group(4)})
    return interfaces

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def send_json(self, code, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.send_header('Content-Length', str(len(raw))); self.send_header('Access-Control-Allow-Origin', '*'); self.end_headers(); self.wfile.write(raw)
    def do_OPTIONS(self): self.send_response(204); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('Access-Control-Allow-Headers','Content-Type'); self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS'); self.end_headers()
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/health': return self.send_json(200, {'ok': True, 'service': 'netscope', 'time': now()})
        if path == '/api/devices': return self.send_json(200, {'devices': all_devices()})
        if path == '/api/activity':
            with db() as c: rows = [dict(x) for x in c.execute('SELECT * FROM activity ORDER BY id DESC LIMIT 30')]
            return self.send_json(200, {'activity': rows})
        if path == '/' or path == '/index.html': return self.file(ROOT / 'index.html', 'text/html; charset=utf-8')
        if path.startswith('/'): return self.file(ROOT / path.lstrip('/'), None)
        self.send_json(404, {'error':'not found'})
    def file(self, path, content_type):
        try: data = path.read_bytes()
        except FileNotFoundError: return self.send_json(404, {'error':'not found'})
        import mimetypes; content_type = content_type or (mimetypes.guess_type(str(path))[0] or 'application/octet-stream'); self.send_response(200); self.send_header('Content-Type', content_type); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        path = urlparse(self.path).path
        try: payload = json_body(self)
        except ValueError as e: return self.send_json(400, {'error': str(e)})
        try:
            if path == '/api/devices': return self.create_device(payload)
            m = re.match(r'^/api/devices/(\d+)/config$', path)
            if m: return self.config(int(m.group(1)), payload)
            m = re.match(r'^/api/devices/(\d+)/test$', path)
            if m: return self.test(int(m.group(1)))
            if path == '/api/discovery/run': return self.discovery()
            if path == '/api/ping': return self.ping(payload)
            return self.send_json(404, {'error':'not found'})
        except Exception as e:
            return self.send_json(500, {'error': str(e)})
    def create_device(self, p):
        required = ['name','kind','connection','host']
        if any(not str(p.get(k,'')).strip() for k in required): return self.send_json(400, {'error':'à¸à¸£à¸­à¸à¸‚à¹‰à¸­à¸¡à¸¹à¸¥à¸­à¸¸à¸›à¸à¸£à¸“à¹Œà¹ƒà¸«à¹‰à¸„à¸£à¸š'})
        name, kind, conn, host = [str(p[k]).strip() for k in required]
        username, password = str(p.get('username','')), str(p.get('password',''))
        ok, detail = False, 'Console not verified'
        if conn.upper() == 'SSH':
            ok, detail = ssh_test(host, username, password, int(p.get('port',22)))
        else:
            try:
                import serial
                with serial.Serial(host, baudrate=int(p.get('baud',9600)), timeout=1): pass
                ok, detail = True, 'Serial console port opened'
            except Exception as e: detail = str(e)
        status = 'online' if ok else 'offline'
        with db() as c:
            cur = c.execute('INSERT INTO devices(name,kind,connection,host,username,status,last_seen,created_at) VALUES(?,?,?,?,?,?,?,?)', (name, kind, conn, host, username, status, now() if ok else None, now()))
            did = cur.lastrowid
        with LOCK: SESSIONS[did] = {'username':username,'password':password,'port':int(p.get('port',22)),'serial_port':host,'baud':int(p.get('baud',9600))}
        log('Initial config ' + ('verified' if ok else 'saved'), f'{name} Â· {conn} Â· {detail}', 'success' if ok else 'warning')
        return self.send_json(201, {'device': next(x for x in all_devices() if x['id']==did), 'connected':ok, 'detail':detail})
    def row(self, did):
        with db() as c: return c.execute('SELECT * FROM devices WHERE id=?',(did,)).fetchone()
    def test(self, did):
        row = self.row(did)
        if not row: return self.send_json(404, {'error':'device not found'})
        s = SESSIONS.get(did, {})
        if row['connection'].upper() == 'SSH': ok, detail = ssh_test(row['host'], s.get('username',row['username']), s.get('password',''), s.get('port',22))
        else: ok, detail = True, 'Console port registered; ready to send'
        with db() as c: c.execute('UPDATE devices SET status=?,last_seen=? WHERE id=?', ('online' if ok else 'offline', now() if ok else row['last_seen'], did))
        return self.send_json(200, {'ok':ok,'detail':detail})
    def config(self, did, p):
        row = self.row(did); cmds = p.get('commands')
        if not row or not isinstance(cmds,list) or not cmds: return self.send_json(400, {'error':'device à¸«à¸£à¸·à¸­ commands à¹„à¸¡à¹ˆà¸–à¸¹à¸à¸•à¹‰à¸­à¸‡'})
        if not p.get('approved'): return self.send_json(403, {'error':'à¸•à¹‰à¸­à¸‡à¸¢à¸·à¸™à¸¢à¸±à¸™à¸„à¸³à¸ªà¸±à¹ˆà¸‡à¸à¹ˆà¸­à¸™ submit'})
        s = SESSIONS.get(did, {})
        if row['connection'].upper() == 'SSH': output = ssh_commands(row['host'], s.get('username',row['username']), s.get('password',''), cmds, s.get('port',22))
        else: output = console_commands(s.get('serial_port',row['host']), s.get('baud',9600), cmds)
        log('Config applied', f"{row['name']} Â· {len(cmds)} commands", 'success')
        return self.send_json(200, {'ok':True,'output':output})
    def discovery(self):
        results=[]
        for row in [dict(x) for x in db().execute('SELECT * FROM devices').fetchall()]:
            s = SESSIONS.get(row['id'], {})
            try:
                if row['connection'].upper() != 'SSH': results.append({'device':row['name'],'peers':[],'interfaces':[],'detail':'Console discovery requires a readable CDP prompt'}); continue
                out = ssh_commands(row['host'], s.get('username',row['username']), s.get('password',''), ['terminal length 0','show cdp neighbors detail','show ip interface brief'], s.get('port',22))
                peers = parse_cdp(out); interfaces = parse_interfaces(out)
                results.append({'device':row['name'],'peers':peers,'interfaces':interfaces})
                with db() as c: c.execute('UPDATE devices SET status=?,last_seen=? WHERE id=?',('online',now(),row['id']))
            except Exception as e: results.append({'device':row['name'],'peers':[],'error':str(e)})
        log('Auto discovery completed', f'{len(all_devices())} managed devices scanned', 'success')
        return self.send_json(200, {'results':results,'devices':all_devices()})
    def ping(self,p):
        host = str(p.get('host','')).strip()
        if not host: return self.send_json(400, {'error':'host required'})
        result = run_ping(host); log('Ping test '+('passed' if result['reachable'] else 'failed'), host, 'success' if result['reachable'] else 'warning'); return self.send_json(200,result)

if __name__ == '__main__':
    init_db(); log('Backend started', f'Listening on http://localhost:{PORT}')
    print(f'NetScope backend listening on http://localhost:{PORT}')
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
