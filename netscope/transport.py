"""Byte transports (SSH shell / Telnet console / Serial console) and an IOS CLI session on top of them."""
from __future__ import annotations

import re
import select
import socket
import time
from typing import Callable

from .parsers import find_errors, strip_syslog

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240


class CliError(Exception):
    pass


class AuthError(CliError):
    pass


# --------------------------------------------------------------------------- transports
class SSHTransport:
    def __init__(self, host: str, port: int, username: str, password: str, timeout: float = 10):
        import paramiko
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self.client.connect(host, port=port, username=username, password=password, timeout=timeout,
                                banner_timeout=timeout, auth_timeout=timeout, look_for_keys=False,
                                allow_agent=False)
        except paramiko.AuthenticationException as e:
            raise AuthError(f"SSH authentication failed for {username}@{host}") from e
        except Exception as e:
            raise CliError(f"SSH connect to {host}:{port} failed: {e}") from e
        self.chan = self.client.invoke_shell(width=511, height=2000)

    def write(self, data: bytes) -> None:
        self.chan.send(data)

    def read(self, timeout: float) -> bytes:
        end = time.time() + timeout
        while time.time() < end:
            if self.chan.recv_ready():
                return self.chan.recv(65535)
            if self.chan.closed or self.chan.exit_status_ready():
                if self.chan.recv_ready():
                    return self.chan.recv(65535)
                raise CliError("SSH session closed by device")
            time.sleep(0.02)
        return b""

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass


class TelnetTransport:
    """Raw telnet client; used for the EVE-NG console (telnet://eve-ip:3xxxx) and for vty telnet."""

    def __init__(self, host: str, port: int = 23, timeout: float = 10):
        try:
            self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        except OSError as e:
            raise CliError(f"Telnet connect to {host}:{port} failed: {e}") from e
        self._pending = b""

    def write(self, data: bytes) -> None:
        self.sock.sendall(data.replace(b"\xff", b"\xff\xff"))

    def read(self, timeout: float) -> bytes:
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b""
        raw = self.sock.recv(65535)
        if not raw:
            raise CliError("Telnet session closed by remote end")
        return self._strip_iac(self._pending + raw)

    def _strip_iac(self, data: bytes) -> bytes:
        out, i, n = bytearray(), 0, len(data)
        self._pending = b""
        while i < n:
            b = data[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= n:
                self._pending = data[i:]
                break
            cmd = data[i + 1]
            if cmd == IAC:
                out.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= n:
                    self._pending = data[i:]
                    break
                opt = data[i + 2]
                if cmd == DO:
                    self.sock.sendall(bytes([IAC, WONT, opt]))
                elif cmd == WILL:
                    self.sock.sendall(bytes([IAC, DONT, opt]))
                i += 3
            elif cmd == SB:
                end = data.find(bytes([IAC, SE]), i)
                if end < 0:
                    self._pending = data[i:]
                    break
                i = end + 2
            else:
                i += 2
        return bytes(out)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass


class SerialTransport:
    def __init__(self, port: str, baud: int = 9600):
        try:
            import serial
            self.ser = serial.Serial(port, baudrate=int(baud), timeout=0)
        except Exception as e:
            raise CliError(f"Cannot open serial port {port}: {e}") from e

    def write(self, data: bytes) -> None:
        self.ser.write(data)

    def read(self, timeout: float) -> bytes:
        end = time.time() + timeout
        while time.time() < end:
            n = self.ser.in_waiting
            if n:
                return self.ser.read(n)
            time.sleep(0.02)
        return b""

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- CLI session
PROMPT_RE = re.compile(r"^[\w.\-/]+(?:\([^)\s]+\))?[>#]$")
DEFAULT_ANSWERS = [
    (re.compile(r"\[confirm\]\s*$", re.I), ""),
    (re.compile(r"\[yes/no\]:?\s*$", re.I), "yes"),
    (re.compile(r"\[startup-config\]\?\s*$", re.I), ""),
    (re.compile(r"modulus \[\d+\]:\s*$", re.I), "2048"),
    (re.compile(r"Destination filename \[.*\]\?\s*$", re.I), ""),
]


def _clean(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    text = re.sub(r"( ?)\x08+ *\x08*", "", text) if "\x08" in text else text
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


class CliSession:
    def __init__(self, transport, log: Callable[[str], None] | None = None):
        self.t = transport
        self.log = log or (lambda s: None)
        self.prompt = ""

    # -- low level
    def send(self, line: str = "") -> None:
        self.t.write((line + "\r").encode())

    def _drain(self, wait: float = 0.05) -> None:
        while self.t.read(wait):
            pass

    def _collect(self, timeout: float, answers=None, idle_nudge: float = 4.0) -> str:
        rules = list(answers or []) + DEFAULT_ANSWERS
        end = time.time() + timeout
        buf, last_nudge, last_data = "", time.time(), time.time()
        while time.time() < end:
            chunk = self.t.read(0.15)
            if chunk:
                buf += _clean(chunk.decode("utf-8", "replace"))
                last_data = last_nudge = time.time()
                last = buf.rsplit("\n", 1)[-1]
                if "--More--" in last:
                    buf = buf.rsplit("--More--", 1)[0]
                    self.t.write(b" ")
                    continue
                for rx, reply in rules:
                    if rx.search(last):
                        self.send(reply)
                        buf += "\n"
                        break
                continue
            last = buf.rsplit("\n", 1)[-1].strip()
            if PROMPT_RE.match(last):
                self.prompt = last
                return buf
            if time.time() - last_nudge > idle_nudge and time.time() - last_data > idle_nudge:
                self.send("")
                last_nudge = time.time()
        raise CliError(f"Timeout waiting for device prompt (last output: {buf[-200:]!r})")

    # -- login / mode handling
    def wake(self, username: str | None = None, password: str | None = None,
             enable_password: str | None = None, timeout: float = 90) -> None:
        """Bring a console/vty to privileged EXEC, answering the initial dialog / login prompts."""
        end = time.time() + timeout
        buf, quiet, pw_tries, enable_sent = "", 0, 0, False
        self.send("")
        while time.time() < end:
            chunk = self.t.read(0.5)
            if chunk:
                buf += _clean(chunk.decode("utf-8", "replace"))
                buf = buf[-3000:]
                quiet = 0
                continue
            quiet += 1
            last = buf.rsplit("\n", 1)[-1].strip()
            low = buf[-400:].lower()
            reply = None
            if "% bad passwords" in low or "authentication failed" in low or "login invalid" in low:
                raise AuthError("Login rejected by device (check username/password)")
            if last.endswith("[yes/no]:") and "initial configuration dialog" in low:
                reply = "no"
            elif "terminate autoinstall" in low and last.endswith(":"):
                reply = "yes"
            elif "--More--" in last:
                reply = " "
            elif last.lower().endswith("username:") or last.lower().endswith("login:"):
                if username is None:
                    raise AuthError("Device asks for a username but none was provided")
                reply = username
            elif last.lower().endswith("password:"):
                pw_tries += 1
                if pw_tries > 3:
                    raise AuthError("Enable password rejected - fill in the device's existing enable password"
                                    if enable_sent else "Password rejected by device")
                if enable_sent:
                    if not enable_password:
                        # Do not send blank passwords repeatedly.  This is a
                        # common EVE/IOS state after a previous partial setup;
                        # return an actionable error immediately.
                        raise AuthError("Device asks for an existing enable password — expand 'Console มีรหัสผ่านอยู่แล้ว?' and fill Existing enable password")
                    reply = enable_password
                else:
                    if password is None:
                        raise AuthError("Device asks for a password but none was provided")
                    reply = password
            elif PROMPT_RE.match(last):
                if "(" in last:
                    reply = "end"
                elif last.endswith(">"):
                    reply, enable_sent = "enable", True
                else:
                    self.prompt = last
                    self._drain(0.3)
                    return
            elif "press return" in low and quiet >= 1:
                reply = ""
            elif quiet >= 3:
                reply = ""
            if reply is not None:
                buf = ""
                self.send(reply)
                quiet = 0
        raise CliError("Timed out waiting for a usable prompt (is the device booted? is another console attached?)")

    def prepare(self, timeout: float = 5) -> None:
        for c in ("terminal length 0", "terminal width 0"):
            self.run(c, timeout=timeout)

    # -- command execution
    def run(self, cmd: str, timeout: float = 30, answers=None) -> str:
        self._drain()
        self.send(cmd)
        raw = self._collect(timeout, answers)
        lines = raw.split("\n")
        if lines and lines[0].strip().endswith(cmd.strip()[-20:]):
            lines = lines[1:]
        if lines and PROMPT_RE.match(lines[-1].strip()):
            lines = lines[:-1]
        return strip_syslog("\n".join(lines)).strip("\n")

    def run_many(self, cmds: list[str], timeout: float = 30) -> dict[str, str]:
        return {c: self.run(c, timeout) for c in cmds}

    def config(self, commands: list[str], answers=None, progress: Callable[[str], None] | None = None) -> dict:
        """Apply commands in global config mode. Returns {'output': str, 'errors': [str]}."""
        out, errors = [], []
        out.append(self.run("configure terminal", 20))
        for cmd in commands:
            if not cmd.strip():
                continue
            if progress:
                progress(cmd)
            slow = cmd.startswith(("crypto key generate", "crypto key zeroize"))
            before = self.prompt
            o = self.run(cmd, timeout=240 if slow else 30, answers=answers)
            out.append(f"{before} {cmd}\n{o}".rstrip())
            errors += [f"{cmd}  →  {e}" for e in find_errors(o)]
        self.run("end", 15)
        return {"output": "\n".join(out), "errors": errors}

    def save(self) -> str:
        return self.run("write memory", 60)

    def close(self) -> None:
        self.t.close()
