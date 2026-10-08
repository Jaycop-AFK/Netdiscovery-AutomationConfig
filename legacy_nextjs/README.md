# NetScope Network Discovery Lab

Next.js App Router UI with a local Python device bridge for Cisco IOS-compatible equipment and EVE-NG labs.

## Requirements

- Node.js 20.9 or newer
- Python 3.11 or newer
- SSH access for network discovery and device configuration; USB/serial drivers for Console sessions

## Start on Windows

Run `.\start.ps1` from this directory. It installs the JavaScript and Python dependencies when needed, starts the local device bridge on port 8080, then starts the Next.js development server on port 3000.

Open `http://localhost:3000`.

You can also start the services separately:

```powershell
npm install
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe backend\server.py
```

In another terminal, run `npm run dev`.

## Workflow

1. Add at least three devices in **Initial config** with SSH credentials or a serial COM port.
2. Run **Auto Discovery**. The bridge reads CDP neighbors and `show ip interface brief` over SSH.
3. Click a topology node to inspect the reported interface state.
4. Choose a target and operation in the config console. Review the preview and explicitly approve before commands are sent.
5. Run a ping to an IP address or hostname from the host running the backend.

## Notes

- Device credentials stay in backend process memory and are not written to SQLite. Restarting the backend clears them.
- Serial devices can be registered and configured, but CDP and interface collection currently require SSH.
- The topology contains only managed devices and CDP peers actually returned by discovery; it does not infer links for unreported devices.
- This is an IOS-style lab tool. Review every command before approval. Paramiko currently accepts unknown SSH host keys for lab convenience; production use should verify host keys.
