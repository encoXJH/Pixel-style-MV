[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Lyrics,
    [Parameter(Mandatory = $true, Position = 1)]
    [string]$Audio,
    [string]$OutputDir,
    [string]$Alignment,
    [string]$Style
)

$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $SkillRoot ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$Requirements = Join-Path $SkillRoot "requirements.txt"
$Generator = Join-Path $PSScriptRoot "mv.py"

function Test-PyLauncher([string]$Name) {
    $ErrorActionPreference = "Continue"
    & py $Name -c "import sys" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Test-CurrentPython {
    $ErrorActionPreference = "Continue"
    & python -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" 2>$null
    return ($LASTEXITCODE -eq 0)
}

foreach ($Tool in @("ffmpeg", "ffprobe")) {
    if (-not (Get-Command $Tool -ErrorAction SilentlyContinue)) {
        throw "Missing $Tool. Install FFmpeg and add ffmpeg and ffprobe to PATH."
    }
}

if (-not (Test-Path $VenvPython -PathType Leaf)) {
    Write-Host "First run: creating an isolated Python 3.12 environment..."
    $Created = $false
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($Launcher in @("-3.12", "-V:Astral/CPython3.12")) {
            if (Test-PyLauncher $Launcher) {
                & py $Launcher -m venv $Venv
                if ($LASTEXITCODE -ne 0) { throw "Failed to create the Python 3.12 virtual environment." }
                $Created = $true
                break
            }
        }
    }
    if (-not $Created -and (Get-Command uv -ErrorAction SilentlyContinue)) {
        & uv venv --python 3.12 $Venv
        if ($LASTEXITCODE -ne 0) {
            & uv python install 3.12
            if ($LASTEXITCODE -ne 0) { throw "Unable to prepare Python 3.12." }
            & uv venv --python 3.12 $Venv
            if ($LASTEXITCODE -ne 0) { throw "Failed to create the Python 3.12 virtual environment." }
        }
        $Created = $true
    }
    if (-not $Created -and (Get-Command python -ErrorAction SilentlyContinue)) {
        if (Test-CurrentPython) {
            & python -m venv $Venv
            if ($LASTEXITCODE -ne 0) { throw "Failed to create the Python 3.12 virtual environment." }
            $Created = $true
        }
    }
    if (-not $Created) {
        throw "Python 3.12 is required. Install Python 3.12 or uv, then retry."
    }
}

$Hash = (Get-FileHash $Requirements -Algorithm SHA256).Hash.ToLowerInvariant()
$HashFile = Join-Path $Venv ".requirements.sha256"
$InstalledHash = if (Test-Path $HashFile -PathType Leaf) {
    (Get-Content $HashFile -Raw).Trim()
} else {
    ""
}
if ($Hash -ne $InstalledHash) {
    Write-Host "Installing locked dependencies..."
    & $VenvPython -m pip install --disable-pip-version-check -r $Requirements
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check the network and Python 3.12 environment." }
    Set-Content -Path $HashFile -Value $Hash -Encoding Ascii -NoNewline
}

$GeneratorArgs = @($Generator, "--lyrics", $Lyrics, "--audio", $Audio)
if ($OutputDir) { $GeneratorArgs += @("--output-dir", $OutputDir) }
if ($Alignment) { $GeneratorArgs += @("--alignment", $Alignment) }
if ($Style) { $GeneratorArgs += @("--style", $Style) }

& $VenvPython @GeneratorArgs
exit $LASTEXITCODE
