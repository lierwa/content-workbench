$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv-win\Scripts\python.exe'
$taskData = Join-Path $taskRoot 'data\single-pass-v2'
$taskDist = Join-Path $taskRoot 'frontend\dist\index.html'
$taskUrl = 'http://127.0.0.1:18765/'
$taskMutex = [System.Threading.Mutex]::new($false, 'Local\ContentWorkbench18765Startup')
$taskAcquired = $false

try {
    $taskAcquired = $taskMutex.WaitOne(0)
    if (-not $taskAcquired) { throw 'Another workbench startup is in progress. Please wait.' }
    if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) { throw 'Missing .venv-win Python. Install project dependencies first.' }
    if (-not (Test-Path -LiteralPath $taskData -PathType Container)) { throw 'Missing data/single-pass-v2. The existing workbench data was not found.' }
    if (-not (Test-Path -LiteralPath $taskDist -PathType Leaf)) { throw 'Missing frontend build. Run npm.cmd --prefix frontend run build.' }

    $taskListening = @(Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue)
    if ($taskListening.Count) {
        if ($taskListening.Count -ne 1 -or $taskListening[0].LocalAddress -ne '127.0.0.1') { throw 'Port 18765 is occupied by another listener.' }
        $taskOwner = Get-CimInstance Win32_Process -Filter "ProcessId = $($taskListening[0].OwningProcess)"
        $taskParent = if ($taskOwner) { Get-CimInstance Win32_Process -Filter "ProcessId = $($taskOwner.ParentProcessId)" } else { $null }
        if (-not $taskOwner -or -not $taskParent -or $taskParent.ExecutablePath -ine $taskPython -or $taskOwner.CommandLine -notlike '*-m single_pass*--port 18765*--data-dir data/single-pass-v2*') {
            throw 'Port 18765 is occupied by a process that is not this workbench.'
        }
        Write-Host "Workbench is already running: $taskUrl"
        exit 0
    }

    $taskLogs = Join-Path $taskRoot 'runs'
    New-Item -ItemType Directory -Force -Path $taskLogs | Out-Null
    $taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $taskOut = Join-Path $taskLogs "workbench-start-$taskStamp.out.log"
    $taskErr = Join-Path $taskLogs "workbench-start-$taskStamp.err.log"
    $taskProcess = Start-Process -FilePath $taskPython -ArgumentList '-u','-m','single_pass','--port','18765','--data-dir','data/single-pass-v2' -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput $taskOut -RedirectStandardError $taskErr -PassThru
    for ($taskAttempt = 0; $taskAttempt -lt 40; $taskAttempt++) {
        Start-Sleep -Milliseconds 250
        $taskProcess.Refresh()
        if ($taskProcess.HasExited) { throw "Workbench did not start. See $taskErr" }
        try {
            $taskActive = @(Get-NetTCPConnection -LocalPort 18765 -State Listen -ErrorAction SilentlyContinue)
            if ($taskActive.Count -ne 1 -or $taskActive[0].LocalAddress -ne '127.0.0.1') { continue }
            $taskOwner = Get-CimInstance Win32_Process -Filter "ProcessId = $($taskActive[0].OwningProcess)"
            if (-not $taskOwner -or $taskOwner.ParentProcessId -ne $taskProcess.Id) { throw 'Port 18765 was claimed by another process.' }
            $taskResponse = Invoke-RestMethod -Uri ($taskUrl + 'api/production') -TimeoutSec 1
            if ($null -ne $taskResponse.jobs -and $null -ne $taskResponse.accounts) {
                Write-Host "Workbench started: $taskUrl"
                exit 0
            }
        } catch {
            if ($_.Exception.Message -eq 'Port 18765 was claimed by another process.') { throw }
        }
    }
    throw "Workbench did not become ready. See $taskErr"
} catch {
    Write-Host "ERROR: $($_.Exception.Message)"
    exit 1
} finally {
    if ($taskAcquired) { $taskMutex.ReleaseMutex() }
    $taskMutex.Dispose()
}
