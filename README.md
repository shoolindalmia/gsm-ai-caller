# GSM AI Caller — self-hosted AI calling agent for Dinstar UC2000-VG-32G

Bulk AI voice calls through your own SIM cards. **Everything runs on your own PC** — no cloud APIs,
no per-minute AI fees. Only your SIM tariff.

```
Dinstar UC2000-VG-32G (32 SIMs) ──SIP (LAN)──► Asterisk ──AudioSocket──► AI agent
                                                  ▲                     Whisper (hear) → Qwen/Llama via Ollama (think) → Piper (speak)
                                         dialer (AMI) ◄── SQLite ──► web panel :8080 (CSV upload, start/pause, results, transcripts)
```

## 1. Hardware

| Item | Recommendation |
|---|---|
| PC | Ubuntu 22.04/24.04, 8+ cores, 32 GB RAM, SSD |
| GPU | NVIDIA **RTX 4060 Ti 16 GB** (~3–4 simultaneous calls) or **RTX 3090/4090 24 GB** (~8–10) |
| Network | Same LAN as the Dinstar, wired, **static IPs** for both |

Your gateway has 32 ports, but simultaneous AI calls are limited by the GPU. Set
`dialer.max_concurrent_calls` to what your GPU handles (start with 3, raise while reply delay stays < ~1.5 s).

## 2. Install on the PC

```bash
git clone <your repo> gsm-ai-caller && cd gsm-ai-caller
sudo DINSTAR_IP=192.168.1.50 ./deploy/install.sh
```

This installs Asterisk, Python, faster-whisper, Piper (Hindi voice `hi_IN-pratham-medium`),
Ollama + `qwen2.5:7b-instruct`, and starts three services: `gsm-ai-agent`, `gsm-ai-dialer`, `gsm-ai-web`.
Install the NVIDIA driver + CUDA 12 first (`sudo ubuntu-drivers install`, reboot).

## 3. Configure the Dinstar UC2000-VG-32G

Open the Dinstar web panel (`http://<dinstar-ip>`, default admin/admin — change it). Menu names differ slightly by firmware:

1. **Network** → set a static LAN IP (e.g. 192.168.1.50).
2. **SIP / Service Mode** → choose **Trunk Gateway / Peer-to-peer (no register)**.
3. **IP Trunk** (or *SIP Trunk / IP Profile*) → add trunk:
   - Remote address: **IP of your Asterisk PC**, port **5060**, transport UDP
   - Codecs: **G.711A (PCMA)** first, then G.711U
4. **Port Group** → put all 32 ports in one group, selection **cyclic/round-robin** (spreads calls across SIMs).
5. **Routing → IP→Tel**: source = the IP trunk above, destination = the port group, number manipulation: none
   (the dialer sends 10-digit numbers; set `dial_prefix` in config if your SIMs need `0`).
6. **Routing → Tel→IP** (optional, for customers calling back): source = port group, destination = IP trunk.
7. Per port: set a **daily call/minute limit** and a **min interval between calls** — this protects SIMs from operator blocking.

Check from the PC: `sudo asterisk -rx "pjsip show endpoint dinstar"` should show the contact as **Avail**.

## 4. Use it

- Panel: `http://<pc-ip>:8080` (user `admin`, password in `/opt/gsm-ai-caller/data/web_password.txt`).
- Create a campaign: name, call script, CSV with `phone,name,...` columns. Any column can be used in the script as `{column}`.
- Press **Start**. The dialer calls only within `calling_hours`, retries busy/no-answer up to `max_attempts`.
- Results per contact: `interested`, `not_interested`, `callback`, `wrong_number`, `dnc`, `no_response`, `unreachable:*`,
  plus a one-line summary and full transcript. Export to CSV.
- People who say "don't call me" are added to the DNC list automatically and never dialled again.

Single test call:
```bash
sudo -u aicaller /opt/gsm-ai-caller/venv/bin/python -m caller --config /opt/gsm-ai-caller/config.yaml test-call 98XXXXXXXX --name Ritesh
journalctl -fu gsm-ai-agent      # live conversation log
```

See `scripts/sample_loan_script.md` for an example Hindi script.

## 5. Tuning

| Problem | Fix (config.yaml) |
|---|---|
| AI cuts the caller off | raise `vad.end_silence_ms` to 900 |
| AI slow to reply | `stt.model: medium`, smaller LLM (`qwen2.5:3b-instruct`), fewer concurrent calls |
| AI stops talking because of background noise | raise `vad.bargein_ms` to 500, `webrtc_aggressiveness: 3` |
| Robotic Hindi voice | `tts.engine: parler` (AI4Bharat Indic Parler-TTS, install the optional package, GPU) |
| Mostly English callers | `stt.language: null`, Piper voice `en_IN-...`/`en_US-...` via `VOICE=` at install |

## 6. Compliance (India)

Commercial/promotional calls from ordinary 10-digit SIMs violate TRAI TCCCPR 2018; operators detect SIM-box
patterns and block numbers, and penalties apply. Use this for customers who opted in (enquiries, orders,
payment reminders, callbacks), scrub against DND, keep calling hours, and for promotional campaigns use a
DLT-registered 140-series line. The software works the same with a SIP trunk instead of the Dinstar.

## Development

```bash
pip install -r requirements.txt pytest pytest-asyncio
pytest            # simulated Asterisk + AMI, no GPU needed
```
