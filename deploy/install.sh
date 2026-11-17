#!/usr/bin/env bash
# One-time installer for Ubuntu 22.04 / 24.04 with an NVIDIA GPU.
# Usage:  sudo DINSTAR_IP=192.168.1.50 ./deploy/install.sh
set -euo pipefail

: "${DINSTAR_IP:?set DINSTAR_IP=<your Dinstar LAN IP>}"
APP=/opt/gsm-ai-caller
SRC="$(cd "$(dirname "$0")/.." && pwd)"
LLM_MODEL="${LLM_MODEL:-qwen2.5:7b-instruct}"
VOICE="${VOICE:-hi_IN-pratham-medium}"

echo "==> System packages"
apt-get update
apt-get install -y asterisk python3-venv python3-pip curl sqlite3 rsync openssl

echo "==> App files -> $APP"
mkdir -p "$APP"/{data,voices}
rsync -a --exclude .git --exclude data "$SRC"/ "$APP"/
id -u aicaller >/dev/null 2>&1 || useradd -r -d "$APP" -s /usr/sbin/nologin aicaller

if [ ! -f "$APP/config.yaml" ]; then
  AMI_SECRET=$(openssl rand -hex 16); WEB_PASS=$(openssl rand -base64 12)
  sed -e "s/CHANGE_ME_AMI_SECRET/$AMI_SECRET/" -e "s#CHANGE_ME_WEB_PASSWORD#$WEB_PASS#" \
      "$APP/config.example.yaml" > "$APP/config.yaml"
  echo "$WEB_PASS" > "$APP/data/web_password.txt"; chmod 600 "$APP/data/web_password.txt"
fi
AMI_SECRET=$(grep ami_secret "$APP/config.yaml" | awk '{print $2}')

echo "==> Asterisk config"
for f in pjsip extensions manager rtp; do
  [ -f /etc/asterisk/$f.conf ] && [ ! -f /etc/asterisk/$f.conf.orig ] && cp /etc/asterisk/$f.conf /etc/asterisk/$f.conf.orig
done
sed "s/DINSTAR_IP/$DINSTAR_IP/g" "$APP/asterisk/pjsip.conf" > /etc/asterisk/pjsip.conf
cp "$APP/asterisk/extensions.conf" /etc/asterisk/extensions.conf
sed "s/AMI_SECRET/$AMI_SECRET/" "$APP/asterisk/manager.conf" > /etc/asterisk/manager.conf
cp "$APP/asterisk/rtp.conf" /etc/asterisk/rtp.conf
systemctl restart asterisk; sleep 3
if ! asterisk -rx "module show like audiosocket" | grep -q app_audiosocket; then
  echo "!! Your Asterisk package has no AudioSocket module. Install Asterisk 20+ from source"
  echo "!! (https://docs.asterisk.org) with app_audiosocket enabled, then re-run this script."
  exit 1
fi

echo "==> Python environment (Whisper, Piper)"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -U pip
"$APP/venv/bin/pip" install -r "$APP/requirements.txt"

echo "==> Piper voice $VOICE"
lang=${VOICE%%-*}; name=$(echo "$VOICE" | cut -d- -f2); q=$(echo "$VOICE" | cut -d- -f3)
base="https://huggingface.co/rhasspy/piper-voices/resolve/main/${lang%%_*}/$lang/$name/$q/$VOICE"
[ -f "$APP/voices/$VOICE.onnx" ] || { curl -fL -o "$APP/voices/$VOICE.onnx" "$base.onnx"; curl -fL -o "$APP/voices/$VOICE.onnx.json" "$base.onnx.json"; }

echo "==> Ollama + $LLM_MODEL (runs locally)"
command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
systemctl enable --now ollama; sleep 3
ollama pull "$LLM_MODEL"

echo "==> Services"
chown -R aicaller:aicaller "$APP"
cp "$APP"/deploy/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gsm-ai-agent gsm-ai-dialer gsm-ai-web

echo
echo "Done. Control panel: http://$(hostname -I | awk '{print $1}'):8080  user: admin  password: $(cat $APP/data/web_password.txt 2>/dev/null)"
echo "Check the Dinstar trunk:  asterisk -rx 'pjsip show endpoint dinstar'"
echo "Test call:  sudo -u aicaller $APP/venv/bin/python -m caller --config $APP/config.yaml test-call 98XXXXXXXX --name Test"
