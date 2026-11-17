# GSM AI Caller - one-time Windows setup. Run setup.bat (it starts this as Administrator).
$ErrorActionPreference = "Stop"
$App = $PSScriptRoot
Set-Location $App
$LlmModel = "qwen2.5:7b-instruct"
$Voice = "hi_IN-pratham-medium"

function Step($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

Write-Host "GSM AI Caller setup (Dinstar UC2000-VG-32G)" -ForegroundColor Green
$DinstarIP = Read-Host "Dinstar gateway IP address (e.g. 192.168.1.50)"
if ($DinstarIP -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw "That is not an IP address: $DinstarIP" }

Step "Python 3.12"
$py = $null
try { & py -3.12 --version | Out-Null; $py = "py -3.12" } catch {}
if (-not $py) {
    winget install -e --id Python.Python.3.12 --scope machine --accept-package-agreements --accept-source-agreements
    Refresh-Path
}
& py -3.12 --version

Step "Ollama (runs the AI model locally)"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $env:Path += ";$env:LOCALAPPDATA\Programs\Ollama"
}

Step "NVIDIA GPU"
$gpu = $false
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
    $gpu = $true
} else {
    Write-Warning "No NVIDIA GPU/driver found. It will run on CPU: much slower, 1 call at a time. Install the NVIDIA driver for real use."
}

Step "Python packages (several GB the first time)"
if (-not (Test-Path "$App\venv")) { & py -3.12 -m venv "$App\venv" }
$Pip = "$App\venv\Scripts\python.exe"
& $Pip -m pip install --upgrade pip
& $Pip -m pip install -r "$App\requirements.txt"
& $Pip -m pip install "piper-tts>=1.2"
& $Pip -c "import piper.voice"
$piperOk = ($LASTEXITCODE -eq 0)
if (-not $piperOk) { Write-Warning "Piper voice not available on this PC - using the built-in Windows Hindi voice instead." }

Step "Hindi voice ($Voice)"
New-Item -ItemType Directory -Force "$App\voices", "$App\data", "$App\logs" | Out-Null
$base = "https://huggingface.co/rhasspy/piper-voices/resolve/main/hi/hi_IN/pratham/medium/$Voice"
foreach ($ext in ".onnx", ".onnx.json") {
    if (-not (Test-Path "$App\voices\$Voice$ext")) { Invoke-WebRequest "$base$ext" -OutFile "$App\voices\$Voice$ext" }
}

Step "config.yaml"
if (-not (Test-Path "$App\config.yaml")) {
    $pass = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 12 | ForEach-Object { [char]$_ })
    $cfg = Get-Content "$App\config.example.yaml" -Raw -Encoding UTF8
    $cfg = $cfg -replace 'gateway_ip: 192\.168\.1\.50', "gateway_ip: $DinstarIP" -replace 'CHANGE_ME_WEB_PASSWORD', $pass
    if (-not $gpu) {
        $cfg = $cfg -replace 'model: large-v3', 'model: small' -replace 'device: cuda', 'device: cpu' `
                    -replace 'compute_type: float16', 'compute_type: int8' -replace 'max_concurrent_calls: 4', 'max_concurrent_calls: 1'
    }
    if (-not $piperOk) { $cfg = $cfg -replace 'engine: piper ', 'engine: windows' }
    [IO.File]::WriteAllText("$App\config.yaml", $cfg)
    [IO.File]::WriteAllText("$App\data\web_password.txt", $pass)
}

Step "AI model $LlmModel (about 5 GB)"
Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden -ErrorAction SilentlyContinue
Start-Sleep 3
& ollama pull $LlmModel

Step "Windows Firewall (SIP 5060, audio 10000-10999, panel 8080)"
$rules = @(
    @{n = "GSM AI Caller SIP"; p = "UDP"; port = "5060"},
    @{n = "GSM AI Caller RTP"; p = "UDP"; port = "10000-10999"},
    @{n = "GSM AI Caller Panel"; p = "TCP"; port = "8080"}
)
foreach ($r in $rules) {
    Get-NetFirewallRule -DisplayName $r.n -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $r.n -Direction Inbound -Protocol $r.p -LocalPort $r.port -Action Allow -Profile Any | Out-Null
}

Step "Desktop shortcut"
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$([Environment]::GetFolderPath('CommonDesktopDirectory'))\GSM AI Caller.lnk")
$s.TargetPath = "$App\start.bat"; $s.WorkingDirectory = $App; $s.Save()

Step "Checking the Dinstar"
& $Pip -m caller check

Write-Host "`nSetup finished." -ForegroundColor Green
Write-Host "Start:   double-click 'GSM AI Caller' on the desktop (or start.bat)"
Write-Host "Panel:   http://localhost:8080   user: admin   password: $(Get-Content "$App\data\web_password.txt")"
Write-Host "Test:    test-call.bat"
