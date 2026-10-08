# NetScope — Network Discovery & Configuration (Assignment 4)

โปรแกรมเว็บ (Python + หน้าเว็บ ไม่ต้องใช้ Node) สำหรับ Cisco IOS / IOS-XE บนอุปกรณ์จริงหรือ EVE-NG

| ความต้องการของโจทย์ | ทำที่ไหน |
|---|---|
| Initial Config ผ่าน UI (Console → hostname, user, IP, SSH) | แท็บ **Initial Config** |
| Auto Discovery → สร้าง Topology บน Dashboard | แท็บ **Dashboard** → ปุ่ม *Auto Discovery* |
| สั่ง config ผ่าน prompt (IP, no shutdown, RIP, EIGRP, OSPF, static) + แสดงคำสั่งให้ยืนยันก่อน submit | กล่อง **Prompt** บน Dashboard → *Preview* → *ยืนยัน* |
| Ping หากันเจอ | แท็บ **Ping Test** (ping จาก router จริง + Reachability Matrix) |
| คลิก node → รูปอุปกรณ์ + port up/down | คลิกที่ node ใน Topology (Front panel, อัปเดตอัตโนมัติทุก 10 วินาที, คลิก port เพื่อ shutdown/no shutdown ได้) |

## ติดตั้งและรัน (Windows)

ต้องมี Python 3.10+ แล้วดับเบิลคลิก `start.bat` (หรือ `python -m venv .venv`, `pip install -r requirements.txt`, `python run.py`)
เปิด <http://localhost:8080> — โปรแกรม bind ที่ 127.0.0.1 เท่านั้นโดยค่าเริ่มต้น (เพราะเก็บรหัสผ่านอุปกรณ์) ใช้ `--host 0.0.0.0` ถ้าจำเป็นจริงๆ

## เตรียม EVE-NG

1. ใส่อุปกรณ์ Cisco (IOSv / IOL / CSR ฯลฯ) อย่างน้อย 3 ตัว ต่อสายระหว่างกัน และ **ต่อพอร์ต management (เช่น Gi0/0) เข้า Cloud (`pnet0` หรือ NAT/`pnet9`)** เพื่อให้เครื่องที่รันโปรแกรมเข้าถึง IP ของอุปกรณ์ได้ — IP management ต้องอยู่ subnet เดียวกับเครื่องคุณ/ EVE-NG
2. Start node ทั้งหมด คลิกขวา node → จะเห็น console `telnet://<IP EVE-NG>:<port>` (หรือใช้ปุ่ม *โหลดโหนด* ในโปรแกรมให้ดึง port จาก EVE-NG API ให้)
3. อุปกรณ์จริง: ใช้ Serial (COM) ที่แท็บ Initial Config ได้เช่นกัน

## ขั้นตอนใช้งาน (สาธิต)

1. **Initial Config** (ทำทีละอุปกรณ์ × อย่างน้อย 3): ใส่ host/port ของ console, hostname, SSH user/password, management IP → *ส่งค่า Initial Config* ตรวจคำสั่งใน popup แล้วยืนยัน
   โปรแกรมจะต่อ console, ตอบ initial dialog, ตั้ง hostname/domain/user/enable secret/IP mgmt, สร้าง RSA key, เปิด SSH v2, เปิด CDP/LLDP, no shutdown ทุกพอร์ต, `write memory` แล้วลอง SSH เข้าจริงเพื่อยืนยันและลงทะเบียนอุปกรณ์
2. **Dashboard → Auto Discovery**: SSH เข้าทุกตัว อ่าน `show cdp/lldp neighbors detail` + `show ip interface brief` สร้าง node/link (ถ้าติ๊ก "ตามหา neighbor" จะลอง login neighbor ใหม่ที่เจอด้วย credential เดียวกัน)
3. **Prompt** (1 บรรทัด = 1 งาน, ใช้ภาษาไทย/อังกฤษได้):

   ```
   no shutdown all interfaces on R1
   set ip 10.0.0.1/24 on g0/1 of R1                 # ใส่ no shutdown ให้อัตโนมัติ
   auto address links from 10.10.0.0/16              # แบ่ง /30 ให้ทุก link ที่ค้นพบ
   enable ospf area 0 on all devices                 # network มาจาก IP ที่ discovery พบ
   ospf 1 area 0 on R1 R2 network 10.0.0.0/24
   rip on R1 network 10.0.0.0
   eigrp 100 on R2 network 10.0.0.0/24
   static route 192.168.2.0/24 via 10.0.0.2 on R1
   default route via 10.0.0.1 on R3
   ping 10.10.0.2 from R1
   show ip route on R1
   ```
   กด *Preview* → ระบบแสดงคำสั่ง IOS แยกตามอุปกรณ์ (แก้ไขในกล่องได้) → กด **ยืนยันและส่ง** จึงจะส่งไปจริง (server ปฏิเสธ request ที่ไม่มี `approved` และบล็อกคำสั่งอันตราย เช่น `reload`, `erase`, `delete`)
4. **Ping Test**: ping จากอุปกรณ์ไปยัง IP/ชื่ออุปกรณ์ หรือกด *รันทดสอบทั้งหมด* ได้ตาราง Reachability Matrix (เขียว = ping ผ่าน)
5. คลิก node ใน Topology → รูปด้านหน้าอุปกรณ์ พอร์ตเขียว = up/up, แดง = down, เทา = administratively down

## โหมดจำลอง (Demo) — ลองโดยไม่ต้องมีอุปกรณ์

กดปุ่ม **🧪 Demo** มุมบนขวา แล้วเลือกจุดเริ่มต้น (Cisco IOS จำลอง 4 เครื่อง: R1, R2, R3 + SW1 สวิตช์):
- **🧪 เริ่มจากเปล่า (แนะนำ):** อุปกรณ์เป็นค่าโรงงาน ไม่มีอะไรลงทะเบียน คุณลองเองทุกขั้น — ไปที่ Initial Config จะมีกล่อง "อุปกรณ์จำลอง" กด **ใช้ค่านี้** เพื่อกรอก console/hostname/IP ให้ → ส่ง Initial Config (ทำครบ 3–4 เครื่อง) → Auto Discovery → สั่ง config ด้วย Prompt → Ping Test
- **⚡ ตั้งค่าให้แล้ว:** ทำ Initial Config + Discovery ให้อัตโนมัติ (~1 นาที) เห็น topology ทันที

แถบ DEMO ใต้ topology มีปุ่มลัดแต่ละขั้น และ **รีเซ็ตเป็นเปล่า** (คืนค่าโรงงาน ลบออกจากรายการ — เริ่มทดสอบใหม่ได้ทุกเมื่อ) / **ตั้งค่าให้อัตโนมัติ** / **ออก**
ใช้ code ชุดเดียวกับอุปกรณ์จริงทุกส่วน (ต่างแค่ console/SSH ปลายทาง) ต้องว่าง port `127.0.0.1:2301-2304` และ `127.0.0.11-14:22` (ปิด NetScope ตัวอื่น / `tools/fake_lab.py` ที่เปิดค้างไว้ก่อน) — IOS จำลองรองรับเฉพาะคำสั่งที่โปรแกรมนี้ใช้ ไม่ใช่ IOS เต็ม

## ทดสอบแบบ manual (fake_lab แยกหน้าต่าง)

```
.venv\Scripts\python tools\fake_lab.py        # จำลอง R1,R2,R3,SW1: console telnet 127.0.0.1:2301-2304, SSH 127.0.0.11-14:22
.venv\Scripts\python run.py
```
Initial Config: host `127.0.0.1`, port `2301`, mgmt IP `127.0.0.11`, prefix `8` (R2 → 2302/127.0.0.12, R3 → 2303/127.0.0.13)
Unit test: `.venv\Scripts\python -m pytest tests` · ทดสอบ routing ครบ RIP/EIGRP/OSPF/static: `.venv\Scripts\python tests\e2e_routing_protocols.py` · · ทดสอบครบ flow: `.venv\Scripts\python tests\e2e_fake_lab.py` (ต้องรัน fake_lab ก่อน)

## ตรวจจับ COM / ทดสอบ console

เลือก **Serial (COM)** ที่แท็บ Initial Config โปรแกรมจะสแกน COM port ให้อัตโนมัติ (ตัวที่เป็นสาย console แบบ FTDI/Prolific/CP210x/CH340 จะมี ★ และเลือกให้) กด ⟳ เพื่อสแกนใหม่ — baud เริ่มต้น 9600 ตามมาตรฐาน Cisco
ปุ่ม **ทดสอบ console** ต่อ console แล้วบอกว่าเจออะไร (prompt, รุ่น, IOS, ยังเป็นค่าโรงงานไหม) และเติม hostname/ประเภทอุปกรณ์ให้ โดยยังไม่แก้ config

## สั่งงานด้วย AI (OpenRouter)

กล่อง **Prompt** ส่งโจทย์ให้ AI วางแผนทันที ไม่มีสวิตช์เปิด/ปิด — ใส่ OpenRouter API key ได้ 2 ทาง:
- **ไฟล์ `.env` (แนะนำ):** คัดลอก `.env.example` เป็น `.env` ในโฟลเดอร์โปรเจกต์ แล้วใส่ `OPENROUTER_API_KEY=sk-or-...` (และ `OPENROUTER_MODEL=...` ถ้าต้องการ) เปิดโปรแกรมใหม่ก็ใช้ได้เลย ไฟล์ `.env` ถูก git-ignore ไว้
- หรือกรอกที่ปุ่ม ⚙/⚠ มุมกล่อง Prompt (เก็บใน `data/settings.json`) — ค่าที่กรอกในหน้าเว็บมีผลก่อน `.env` / ตัวแปรแวดล้อมของระบบ

model เริ่มต้น `openai/gpt-4o-mini` โมเดลเล็กๆ ก็เพียงพอ
1. พิมพ์โจทย์เป็นภาษาธรรมดา เช่น "ทำให้ R1 กับ R2 คุยกันได้ด้วย OSPF แล้ว ping ทดสอบ" แล้วกด **Ctrl+Enter** (หรือปุ่ม 🤖 ให้ AI วางแผน)
2. AI ดูอุปกรณ์/interface/link ที่ discovery เจอ แล้ววางแผนทั้งชุด
3. หน้า Preview แสดง **สรุปแผน** + คำสั่ง IOS แยกตามอุปกรณ์ (แก้ไขในกล่องได้) + **คำเตือน** ถ้า AI อ้าง interface ที่ไม่มีจริง / แตะ port management / IP ผิดรูป
4. กด **ยืนยันและส่ง** จึงจะส่งไปจริง ไม่ยืนยันก็ไม่มีอะไรเกิดขึ้นกับอุปกรณ์
5. **วนแก้อัตโนมัติ:** ใน Preview มีช่อง 🔁 "ถ้ามี error หรือ ping ไม่ผ่าน ให้ AI แก้แล้วลองใหม่อัตโนมัติ (สูงสุด 3 รอบ)" (ติ๊กไว้ให้เมื่อเป็นแผนจาก AI) ระบบจะ ส่งคำสั่ง → ตรวจผล → ถ้า error หรือ **ping 0%** (รอ routing converge ให้ก่อนบนอุปกรณ์จริง) ส่งโจทย์เดิม + คำสั่งที่ส่ง + error + สถานะล่าสุดให้ AI → ส่งแผนแก้ → ตรวจซ้ำ จนผ่านหรือครบรอบ แสดงให้เห็นทุกรอบ หยุดเองถ้า AI เสนอแผนซ้ำ/ไม่มีแผนแก้ และคำสั่งอันตรายยังถูกกรองทุกรอบ
6. ถ้าไม่ได้ติ๊กแล้วมี error จะมีกล่อง **🤖 ให้ AI แก้ไข**: *แก้ → ดู Preview ก่อนส่ง* (ยืนยันทีละรอบ) หรือ *แก้และทดสอบอัตโนมัติ* พิมพ์คำสั่งเพิ่มเติมให้ AI ได้ เช่น "ไม่ต้องใช้ BGP" (คำสั่งที่สำเร็จไปแล้วไม่ถูก rollback)
7. **ปัญหาการเชื่อมต่อ** (SSH ต่อไม่ได้ ฯลฯ) ไม่ใช่เรื่องของคำสั่ง — ระบบจะไม่ส่งให้ AI แต่บอกวิธีตรวจสอบ (โหมดจำลอง: ถ้าโปรแกรมถูกรีสตาร์ท อุปกรณ์จำลองเดิมจะถูกล้างออกอัตโนมัติ ให้กด 🧪 Demo ใหม่)

- ถ้ายังไม่มี key หรือ AI เรียกไม่สำเร็จ ระบบใช้ parser ในตัว (เข้าใจเฉพาะรูปแบบเช่น `set ip 10.0.0.1/24 on g0/1 of R1`) และบอกเหตุผลใน Preview
- ส่งให้ AI เฉพาะ ชื่ออุปกรณ์ + ชื่อ/IP interface + link — **ไม่ส่งรหัสผ่าน**
- ผลของ AI ถูกตรวจ: ต้องเป็นอุปกรณ์ที่มีอยู่, ตัดคำสั่ง `reload/erase/delete/username/enable secret/crypto/line vty/con` ทิ้ง, exec ได้แค่ show/ping/traceroute
- key ที่กรอกในหน้าเว็บเก็บใน `data/settings.json` (ข้อความธรรมดา, อยู่ใน .gitignore) — ถ้าใช้ `.env` ก็ไม่ต้องเก็บที่นี่

## แชร์ให้เพื่อนเข้ามาลอง (Public port / Dev Tunnel)

> ⚠️ อย่าเปิด port สาธารณะตรงๆ ด้วย `start.bat` ธรรมดา — โปรแกรมสั่ง config อุปกรณ์ในแล็บของคุณและใช้ OpenRouter key ได้ ใครได้ลิงก์ก็ควบคุมได้ โปรแกรมจึงปฏิเสธคำขอที่มาจากภายนอก (Host ไม่ใช่ localhost / มี header ของ proxy) ถ้ายังไม่ตั้งรหัสผ่าน

1. รัน **`.\start.bat --share`** (PowerShell ต้องมี `.\` นำหน้า) (หรือ `python run.py --share`) — จะพิมพ์ **รหัสเข้าใช้งาน** แบบสุ่มให้ (หรือกำหนดเองด้วย `NETSCOPE_PASSWORD` ใน `.env`)
2. **ngrok (แนะนำ):** รัน `.\start.bat --ngrok` — โปรแกรมเปิด ngrok ให้เอง แล้วพิมพ์ **PUBLIC LINK + ACCESS CODE** ออกมา (ต้องติดตั้ง ngrok และรัน `ngrok config add-authtoken <token>` ครั้งเดียว) หรือรันแยก: `.\start.bat --share` แล้วเปิดอีกเทอร์มินัล `ngrok http 8080` ngrok แบบฟรีมีหน้าเตือนก่อนเข้า (กด Visit Site)
   หรือใช้ VS Code → แท็บ **Ports** → Forward port `8080` → คลิกขวา **Port Visibility → Public** (หรือ `devtunnel host -p 8080 --allow-anonymous`) แล้วส่งลิงก์ `https://....devtunnels.ms` + รหัสให้เพื่อน
3. เพื่อนเปิดลิงก์ → ใส่รหัส → ใช้ปุ่ม **🧪 Demo** ลองทุกฟังก์ชัน (Initial Config → Discovery → Prompt/AI → Ping → คลิก node)

โหมด `--share` ปลอดภัยโดยออกแบบ: ต้องมีรหัส (ผิดเกิน 8 ครั้ง/นาทีถูกบล็อก, cookie HttpOnly), **ใช้ได้เฉพาะอุปกรณ์จำลอง** (เพิ่มอุปกรณ์จริง / console จริง / COM / EVE-NG / เปลี่ยน API key ถูกบล็อกฝั่ง server), ใช้โฟลเดอร์ข้อมูลแยก `data_share/` (ไม่ปนกับอุปกรณ์จริงของคุณ), จำกัด AI 40 ครั้ง/ชั่วโมง (`NETSCOPE_AI_LIMIT`) และ bind ทั้ง IPv4+IPv6 เพื่อให้ tunnel ต่อได้ — อุปกรณ์จำลองเป็นของส่วนกลางร่วมกัน ถ้าเพื่อนหลายคนใช้พร้อมกันจะเห็นการเปลี่ยนแปลงของกันและกัน (ปุ่ม *รีเซ็ตเป็นเปล่า* ล้างได้)
ถ้าได้ **502** แปลว่า tunnel ต่อโปรแกรมไม่ได้ — เช็กว่าโปรแกรมรันอยู่ (ช่อง Running Process ใน Ports ต้องไม่ว่าง) และ port ตรงกัน ปิดแชร์: ปิดโปรแกรม / ลบ port ใน Ports

## โครงสร้าง

```
run.py, start.bat      ตัวรัน
netscope/transport.py  SSH / Telnet console / Serial + ตัวจับ prompt ของ IOS (initial dialog, login, enable, --More--, [confirm])
netscope/initconfig.py สร้าง/ส่ง Initial Config ทาง console แล้วตรวจ SSH
netscope/discovery.py  CDP/LLDP crawl → topology.json
netscope/prompt.py     prompt (ไทย/อังกฤษ) → คำสั่ง IOS ต่ออุปกรณ์
netscope/connectivity.py ping จากอุปกรณ์ + matrix
netscope/ai.py         (ไม่บังคับ) ให้ AI ผ่าน OpenRouter แปลบรรทัดที่ parser แปลไม่ได้
netscope/safety.py     รายการคำสั่งที่ห้ามส่ง
netscope/auth.py       รหัสเข้าใช้งาน/โหมดแชร์/โควตา AI
netscope/eve.py        ดึงรายการ node/console port จาก EVE-NG REST API
netscope/server.py     HTTP API + งานเบื้องหลัง (job) + static
web/                   Dashboard (vanilla JS, SVG)
tools/fake_lab.py      IOS จำลองสำหรับทดสอบ
legacy_nextjs/         ต้นแบบ Next.js เดิม (ไม่ใช้แล้ว)
```

## ข้อควรทราบ

- รหัสผ่านอุปกรณ์เก็บเป็นข้อความธรรมดาใน `data/inventory.json` (สำหรับ lab) — อย่า commit/แชร์ไฟล์นี้
- SSH รับ host key ใหม่อัตโนมัติ (เหมาะกับ lab) และ paramiko ถูกปักที่ 3.x เพราะ IOS รุ่นเก่าใน EVE-NG ต้องใช้ algorithm แบบ legacy (SHA-1)
- Discovery เห็นเฉพาะ link ที่ขึ้น (up) และมี CDP/LLDP; neighbor ที่ login ไม่ได้จะแสดงเป็น node สีเหลือง (unmanaged)
- ออกแบบสำหรับ Cisco IOS-style CLI เท่านั้น
