# GSM AI Caller — Windows AI calling agent for Dinstar UC2000-VG-32G

Bulk AI voice calls through your own SIM cards, from **one Windows PC**.
No cloud APIs, no Asterisk, no per-minute AI fees — only your SIM tariff.

```
Dinstar UC2000-VG-32G (32 SIMs)  ◄──SIP + RTP (office LAN)──►  Windows PC: start.bat
                                                                 ├─ SIP/RTP engine (built in, talks to Dinstar directly)
                                                                 ├─ Whisper  — hears the caller (Hindi/English/Hinglish)
                                                                 ├─ Ollama + Qwen 2.5 7B — decides what to say
                                                                 ├─ Piper or Windows Hindi voice — speaks
                                                                 └─ Web panel http://localhost:8080
```

## 1. What you need

| Item | Recommendation |
|---|---|
| PC | Windows 10/11 64-bit, 8+ core CPU, 32 GB RAM, SSD with 40 GB free |
| GPU | NVIDIA **RTX 4060 Ti 16 GB** (~3–4 calls at once) or **RTX 3090/4090 24 GB** (~8–10). Install the latest NVIDIA driver. |
| Network | PC and Dinstar on the same office network, **wired**, both with **fixed IP addresses** |

The gateway has 32 SIM ports, but how many AI calls run at once is limited by the GPU.
Without an NVIDIA GPU it still runs, but only 1 slow call at a time.

## 2. Install (one time)

1. Copy the `gsm-ai-caller` folder to e.g. `C:\gsm-ai-caller`.
2. Double-click **`setup.bat`** → allow Administrator → type the Dinstar's IP address.
   It installs Python 3.12 and Ollama (via winget), the speech models, the Hindi voice, the AI model (~5 GB),
   opens the Windows Firewall ports (UDP 5060, UDP 10000–10999, TCP 8080) and creates a desktop shortcut.
3. At the end it runs a Dinstar check. "OK" means the PC can talk to the gateway.

The web panel password is shown at the end and saved in `data\web_password.txt`.

## 3. Dinstar UC2000-VG-32G settings

Open the Dinstar web page (`http://<dinstar-ip>`, default `admin`/`admin` — change it). Menu names vary a little by firmware:

1. **Network**: fixed LAN IP, e.g. `192.168.1.50`.
2. **SIP settings / Service mode**: **Trunk gateway mode** (peer-to-peer, **no registration**).
3. **IP Trunk** (also called *SIP Trunk* / *IP Profile*): add one trunk
   - Remote IP: **your Windows PC's IP**, port **5060**, UDP
   - Codecs: **G.711A (PCMA)** first, then **G.711U (PCMU)**
4. **Port Group**: all 32 ports in one group, selection **cyclic / round-robin** (spreads calls over the SIMs).
5. **Routing → IP→Tel**: from the IP trunk → to the port group. No number manipulation
   (the PC sends 10-digit numbers; set `dial_prefix: "0"` in `config.yaml` if your SIMs need it).
6. **Routing → Tel→IP** (optional, customers calling back are answered by the AI): from the port group → to the IP trunk.
7. **Per port**: set a daily call limit and a minimum gap between calls. This protects the SIMs from operator blocking.

Then double-click **`check.bat`**: it should say `OK — Dinstar answered`.

## 4. Daily use

1. Double-click **GSM AI Caller** on the desktop (or `start.bat`). Keep the black window open — it shows every call live.
2. The panel opens at **http://localhost:8080** (other office PCs: `http://<this-pc-ip>:8080`).
3. **New campaign** → name, call script, contacts CSV (`phone,name,...`; any column can be used in the script as `{column}`) → **Start**.
4. Results per contact: `interested`, `not_interested`, `callback`, `wrong_number`, `dnc`, `no_response`, `busy`,
   `unreachable:*`, a one-line summary and the full transcript. **Export CSV** for your team.

- Calls only go out within `calling_hours` (default 10:00–19:00). Busy/no-answer are retried up to 3 times, 60 min apart.
- Anyone who says "don't call me" goes on the do-not-call list and is never dialled again.
- First try **`test-call.bat`** with your own number.

Example Hindi script: `scripts\sample_loan_script.md`. Example CSV: `scripts\sample_contacts.csv`.
Logs: `logs\caller.log`.

## 5. Tuning (`config.yaml`, then restart start.bat)

| Problem | Fix |
|---|---|
| AI cuts the caller off | `vad.end_silence_ms: 900` |
| AI slow to reply | `stt.model: medium`, `llm.model: qwen2.5:3b-instruct` (run `ollama pull qwen2.5:3b-instruct`), fewer `max_concurrent_calls` |
| Background noise interrupts the AI | `vad.bargein_ms: 500`, `vad.webrtc_aggressiveness: 3` |
| Want a different voice | `tts.engine: windows` uses Windows' own Hindi voice (Settings → Time & language → Speech → add **Hindi** voices), `windows_voice: Kalpana` or `Hemant` |
| Mostly English callers | `stt.language: null` and an English voice |
| No audio / one-way audio | Check firewall (re-run setup.bat), both codecs G.711 on the Dinstar, and that `sip.local_ip` is the PC's LAN IP |
| Dinstar trunk needs a password | set `sip.username` / `sip.password` |

## 6. Compliance (India)

Commercial/promotional calls from ordinary 10-digit SIMs break TRAI TCCCPR 2018; operators detect SIM-gateway patterns
and block numbers, and penalties apply. Use this for customers who opted in (enquiries, orders, payment reminders,
callbacks), scrub against DND, keep calling hours, and use a DLT-registered 140-series line for promotional campaigns.

## For developers

```bash
pip install -r requirements.txt pytest pytest-asyncio
pytest   # includes a simulated Dinstar over real UDP: answer, busy, ring timeout, hang-up, inbound
```
Code: `caller/sip.py` (SIP + RTP), `caller/g711.py`, `caller/agent.py` (conversation), `caller/dialer.py`,
`caller/engines.py` (Whisper / Ollama / Piper / Windows voices), `caller/web.py` (panel), `caller/db.py` (SQLite).
