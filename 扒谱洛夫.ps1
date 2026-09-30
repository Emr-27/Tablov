param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Error "缺少项目 Python 环境：$pythonPath。请按 README 创建 .venv。"
    exit 3
}
$oldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    & $pythonPath -B -m harmonica_transcriber @Arguments
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $oldPythonPath
}

