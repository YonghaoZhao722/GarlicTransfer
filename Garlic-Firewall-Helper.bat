@echo off
setlocal DisableDelayedExpansion
title GarlicTransfer - Temporary Firewall Helper
set "GARLIC_HELPER_SELF=%~f0"
set "GARLIC_HELPER_MODE=%~1"
set "GARLIC_HELPER_PID=%~2"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:GARLIC_HELPER_SELF); $m='# POWERSHELL'+' PAYLOAD'; & ([scriptblock]::Create($t.Substring($t.IndexOf($m)+$m.Length)))"
set "GARLIC_HELPER_RESULT=%ERRORLEVEL%"
if not "%GARLIC_HELPER_RESULT%"=="0" pause
exit /b %GARLIC_HELPER_RESULT%
# POWERSHELL PAYLOAD
# Garlic Firewall Helper v1.1a - Windows PowerShell 5.1 / Windows 10 or 11.
# Run beside GarlicTransfer.exe in the ORIGINAL application folder.
# No EXE is modified. GarlicTransfer is NOT launched by the elevated worker.
# /cleanup removes this helper's rules for this exact executable path.
# /help displays instructions. No downloads or third-party tools are needed.

$ErrorActionPreference = 'Stop'
$worker = $null
$sessionDir = $null
$mutex = $null
$ownsMutex = $false
$app = $null
$appHandedOff = $false
$resultCode = 1

function Write-JsonFile {
    param([string]$Path, $Value)
    $temp = $Path + '.tmp'
    $json = ConvertTo-Json -InputObject $Value -Compress -Depth 4
    [IO.File]::WriteAllText($temp, $json, [Text.Encoding]::UTF8)
    [IO.File]::Move($temp, $Path)
}

function Invoke-FirewallWorker {
    param($Config)
    $ErrorActionPreference = 'Stop'
    $ruleNames = @()
    $failure = $null
    $cleanupFailures = @()
    $appWatch = $null

    function Get-IdentityProcess {
        param([int]$ProcessId, [long]$StartTicks)
        try {
            $p = [Diagnostics.Process]::GetProcessById($ProcessId)
            if ($p.StartTime.ToUniversalTime().Ticks -eq $StartTicks -and -not $p.HasExited) {
                return $p
            }
            $p.Dispose()
        } catch { }
        return $null
    }

    try {
        Import-Module NetSecurity -ErrorAction Stop
        if ($Config.Mode -eq 'Cleanup') {
            # Scope recovery to our own group, never Garlic's existing rules.
            $oldRules = @(Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop |
                Where-Object { $_.Group -eq $Config.RuleGroup })
            if ($oldRules.Count -gt 0) {
                $oldRules | Remove-NetFirewallRule -ErrorAction Stop
            }
        } else {
            $ports = @{ TCP = @('1004', '1005'); UDP = @('1006') }
            foreach ($protocol in @('TCP', 'UDP')) {
                $name = 'GarlicHelper-' + $Config.SessionId + '-' + $protocol
                $ruleNames += $name
                $rule = @{
                    Name = $name
                    DisplayName = 'GarlicTransfer temporary access (' + $protocol + ')'
                    Group = $Config.RuleGroup
                    Description = 'Temporary local-network transfer ports for Garlic and its Python backend. Created by Garlic-Firewall-Helper.bat.'
                    PolicyStore = 'PersistentStore'
                    Direction = 'Inbound'
                    Action = 'Allow'
                    Enabled = 'True'
                    Profile = 'Any'
                    Protocol = $protocol
                    LocalPort = $ports[$protocol]
                    RemoteAddress = 'LocalSubnet'
                    EdgeTraversalPolicy = 'Block'
                }
                $null = New-NetFirewallRule @rule
            }
            Write-JsonFile (Join-Path $Config.SessionDir 'ready.json') @{ Ready = $true }

            # Wait for the normal-user launcher to hand over Garlic's identity.
            $deadline = [DateTime]::UtcNow.AddSeconds(90)
            $handoffFile = Join-Path $Config.SessionDir 'app.json'
            while (-not (Test-Path -LiteralPath $handoffFile)) {
                if (Test-Path -LiteralPath (Join-Path $Config.SessionDir 'cancel')) {
                    throw 'The launcher cancelled before GarlicTransfer started.'
                }
                $supervisor = Get-IdentityProcess $Config.SupervisorId $Config.SupervisorTicks
                if ($null -eq $supervisor) { throw 'The launcher exited before GarlicTransfer started.' }
                $supervisor.Dispose()
                if ([DateTime]::UtcNow -gt $deadline) { throw 'Timed out waiting for GarlicTransfer to start.' }
                Start-Sleep -Milliseconds 250
            }

            $identity = [IO.File]::ReadAllText($handoffFile) | ConvertFrom-Json
            $appWatch = Get-IdentityProcess ([int]$identity.ProcessId) ([long]$identity.StartTicks)
            if ($null -ne $appWatch) {
                if (-not [string]::Equals($appWatch.MainModule.FileName, $Config.ExePath,
                        [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'The launched process does not match GarlicTransfer.exe.'
                }
                # Watch Garlic directly. Closing the launcher after handoff is OK.
                $appWatch.WaitForExit()
            }
        }
    } catch {
        $failure = $_.Exception.Message
    } finally {
        if ($null -ne $appWatch) { $appWatch.Dispose() }
        foreach ($name in $ruleNames) {
            try {
                $existing = @(Get-NetFirewallRule -PolicyStore PersistentStore -Name $name -ErrorAction SilentlyContinue)
                if ($existing.Count -gt 0) { $existing | Remove-NetFirewallRule -ErrorAction Stop }
            } catch { $cleanupFailures += $_.Exception.Message }
        }
        if ($cleanupFailures.Count -gt 0) {
            $failure = ($failure, ('Firewall cleanup failed: ' + ($cleanupFailures -join '; ')) |
                Where-Object { $_ }) -join ' '
        }
        try {
            Write-JsonFile (Join-Path $Config.SessionDir 'result.json') @{ Success = [string]::IsNullOrEmpty($failure); Error = $failure }
        } catch { }
        # If the launcher was closed, reclaim our own temporary status files.
        $supervisor = Get-IdentityProcess $Config.SupervisorId $Config.SupervisorTicks
        if ($null -eq $supervisor) {
            Remove-Item -LiteralPath $Config.SessionDir -Recurse -Force -ErrorAction SilentlyContinue
        } else { $supervisor.Dispose() }
    }
    if ([string]::IsNullOrEmpty($failure)) { exit 0 } else { exit 1 }
}

function Get-ListenerLines {
    # These are local socket checks, not a test of reachability from the PS5.
    # Do not collect command lines, config.ini, account IDs, or oracle secrets.
    try {
        $tcp = @(Get-NetTCPConnection -State Listen -LocalPort 1004,1005 -ErrorAction SilentlyContinue)
        $udp = @(Get-NetUDPEndpoint -LocalPort 1006 -ErrorAction SilentlyContinue)
        foreach ($port in @(1004, 1005, 1006)) {
            $protocol = $(if ($port -eq 1006) { 'UDP' } else { 'TCP' })
            $endpoints = $(if ($port -eq 1006) { $udp } else { $tcp })
            $boundSockets = @($endpoints | Where-Object { $_.LocalPort -eq $port })
            if ($boundSockets.Count -eq 0) {
                $protocol + ' ' + $port + ': NOT LISTENING'
            } else {
                $owners = foreach ($endpoint in $boundSockets) {
                    $owner = Get-Process -Id $endpoint.OwningProcess -ErrorAction SilentlyContinue
                    $name = $(if ($null -ne $owner) { $owner.ProcessName } else { 'unknown' })
                    $endpoint.LocalAddress + ' / ' + $name + ' / PID ' + $endpoint.OwningProcess
                }
                $protocol + ' ' + $port + ': BOUND [' + (($owners | Sort-Object -Unique) -join '; ') + ']'
            }
        }
    } catch {
        'Listener check unavailable: ' + $_.Exception.Message
    }
}

try {
    Write-Host ''
    Write-Host 'GarlicTransfer - Temporary Firewall Helper v1.1a' -ForegroundColor Cyan
    $mode = $env:GARLIC_HELPER_MODE
    if ($mode -in @('/help', '-help', '/?', '-h')) {
        Write-Host @'

SETUP
  Easiest: open Garlic Settings and click Allow local network access.
  Accept the Windows UAC prompt; the helper attaches to the running app.
  Manual alternative: close Garlic and double-click this BAT beside its EXE.
  Garlic runs as your current user. Close Garlic normally when finished.

WHAT IT ALLOWS
  Inbound TCP 1004/1005 and UDP 1006, from LocalSubnet, on any Windows
  network profile. Rules apply to ANY process using these three local ports.
  This includes Garlic's Python backend and is not restricted to the UI EXE.
  Existing rules, outbound policy, and global firewall settings are untouched.
  A managed/third-party firewall or explicit block rule may still block traffic.

CLEANUP
  The elevated worker watches Garlic and removes its two rules when it exits.
  Keep this window open until it says Garlic started. After that, the worker
  continues watching Garlic even if you close this window.
  A power failure, restart, or forced termination of the worker can leave rules.
  With Garlic closed, run:  Garlic-Firewall-Helper.bat /cleanup
  Recovery removes only rules created by this helper for the current EXE path.
  If moving the app after an interruption, clean up from the old folder first.

NOTES
  Windows 10/11 and built-in Windows PowerShell 5.1 are required.
  Keep Garlic's original resources, DLLs, and subfolders beside its EXE.
  This helper grants PC firewall access; it does not start PS5 Oracle listeners.
  Click Pair this PS5 once (experimental) to start the pairing listener.
  Discover consoles alone does not make the PC a Data Transfer source.
  Pairing needs TCP 1004 and UDP 1006; upload also uses TCP 1005.
  The console window reports socket owners when its listener status changes.
  BOUND means a process owns the port, not that the PS5 can reach it.
'@
        $resultCode = 0
    } else {
        $isCleanup = $mode -in @('/cleanup', '-cleanup')
        $isAttach = $mode -in @('/attach', '-attach')
        if ($mode -and -not $isCleanup -and -not $isAttach) {
            throw 'Unknown option. Use no option to launch, /attach from Settings, /cleanup for recovery, or /help.'
        }
        $attachPid = 0
        if ($isAttach -and (-not [int]::TryParse($env:GARLIC_HELPER_PID, [ref]$attachPid) -or $attachPid -le 0)) {
            throw 'The Settings launcher did not provide a valid GarlicTransfer process ID.'
        }
        $folder = [IO.Path]::GetDirectoryName($env:GARLIC_HELPER_SELF)
        $exePath = [IO.Path]::GetFullPath((Join-Path $folder 'GarlicTransfer.exe'))
        $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
        if (-not $isCleanup -and $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'Double-click this BAT normally, without Run as administrator. It requests elevation separately for firewall rules.'
        }
        if (-not $isCleanup -and -not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
            throw 'Put this BAT in the same folder as GarlicTransfer.exe, then run it again.'
        }
        # Stable path identity is used only for the mutex and our recovery group.
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $hash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($exePath.ToUpperInvariant()))).Replace('-', '')
        } finally { $sha.Dispose() }
        $ruleGroup = 'GarlicTransfer.Helper.v1.' + $hash
        $mutex = [Threading.Mutex]::new($false, ('Local\GarlicTransfer.Helper.' + $hash))
        try { $ownsMutex = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
        if (-not $ownsMutex) { throw 'A helper is already running for this GarlicTransfer folder.' }

        if (-not $isAttach) {
            foreach ($p in @(Get-Process -Name 'GarlicTransfer' -ErrorAction SilentlyContinue)) {
                try { $runningPath = $p.Path } catch { $runningPath = $null }
                if ([string]::Equals($runningPath, $exePath, [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Close GarlicTransfer first, then run this helper again.'
                }
            }
        }

        $sessionId = [Guid]::NewGuid().ToString('N')
        $sessionDir = Join-Path ([IO.Path]::GetTempPath()) ('GarlicFirewall-' + $sessionId)
        $null = New-Item -Path $sessionDir -ItemType Directory
        $statusPath = Join-Path $sessionDir 'result.json'
        $self = [Diagnostics.Process]::GetCurrentProcess()
        $config = @{
            ExePath = $exePath
            RuleGroup = $ruleGroup
            SessionId = $sessionId
            SessionDir = $sessionDir
            SupervisorId = $PID
            SupervisorTicks = $self.StartTime.ToUniversalTime().Ticks
            Mode = $(if ($isCleanup) { 'Cleanup' } else { 'Launch' })
        }
        $self.Dispose()
        $configJson = ConvertTo-Json -InputObject $config -Compress
        $config64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($configJson))
        # Include the shared writer in the separate elevated process.
        $workerCode = 'function Write-JsonFile {' + ${function:Write-JsonFile}.ToString() + "}`r`n" +
            'function Invoke-FirewallWorker {' + ${function:Invoke-FirewallWorker}.ToString() + "}`r`n" +
            '$config = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(''' + $config64 + ''')) | ConvertFrom-Json; Invoke-FirewallWorker $config'
        $encodedWorker = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($workerCode))
        if ($encodedWorker.Length -gt 30000) { throw 'The helper command exceeds the Windows command-line limit.' }

        Write-Host ('Application: ' + $exePath)
        Write-Host 'Windows will request administrator permission for firewall rules only.'
        $worker = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs -WindowStyle Hidden `
            -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encodedWorker) -PassThru

        if (-not $isCleanup) {
            $deadline = [DateTime]::UtcNow.AddSeconds(90)
            while (-not (Test-Path -LiteralPath (Join-Path $sessionDir 'ready.json'))) {
                $worker.Refresh()
                if ($worker.HasExited) { throw 'The firewall helper could not create access rules.' }
                if ([DateTime]::UtcNow -gt $deadline) { throw 'Timed out preparing firewall access.' }
                Start-Sleep -Milliseconds 250
            }
            if ($isAttach) {
                Write-Host 'Local-network firewall access enabled. Connecting to GarlicTransfer...' -ForegroundColor Green
                $app = Get-Process -Id $attachPid -ErrorAction Stop
                if (-not [string]::Equals($app.MainModule.FileName, $exePath, [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'The Settings launcher process does not match GarlicTransfer.exe.'
                }
            } else {
                Write-Host 'Local-network firewall access enabled. Starting GarlicTransfer...' -ForegroundColor Green
                $app = Start-Process -FilePath $exePath -WorkingDirectory $folder -PassThru
            }
            try {
                $appTicks = $app.StartTime.ToUniversalTime().Ticks
                Write-JsonFile (Join-Path $sessionDir 'app.json') @{ ProcessId = $app.Id; StartTicks = $appTicks }
                $appHandedOff = $true
            } catch {
                throw 'GarlicTransfer exited before monitoring could start. Keep its original application files together.'
            }
            Write-Host 'Firewall access is active. Close Garlic normally when finished; rules will be removed automatically.'
            Write-Host 'In Garlic, start Pair this PS5 once to advertise the pairing listener.' -ForegroundColor Cyan
            Write-Host 'Pairing uses TCP 1004 + UDP 1006. TCP 1005 is used for upload.'
            Write-Host 'Discover consoles alone does not start the PC pairing listener.'
            Write-Host 'Listening on these ports is not proof the PS5 can reach them.'
            try {
                foreach ($nic in @(Get-NetIPConfiguration -ErrorAction Stop)) {
                    foreach ($address in @($nic.IPv4Address)) {
                        if ($address.IPAddress -and $address.IPAddress -notlike '127.*') {
                            Write-Host ('PC IPv4: ' + $nic.InterfaceAlias + ' = ' + $address.IPAddress)
                        }
                    }
                }
            } catch { }
            $lastListenerState = $null
            $nextCheck = [DateTime]::MinValue
            while (-not $app.WaitForExit(1000)) {
                if ([DateTime]::UtcNow -ge $nextCheck) {
                    $listenerLines = @(Get-ListenerLines)
                    $listenerState = $listenerLines -join "`n"
                    if ($listenerState -ne $lastListenerState) {
                        Write-Host ''
                        Write-Host ('Local listeners at ' + (Get-Date -Format 'HH:mm:ss')) -ForegroundColor Cyan
                        foreach ($line in $listenerLines) { Write-Host $line }
                        $lastListenerState = $listenerState
                    }
                    $nextCheck = [DateTime]::UtcNow.AddSeconds(5)
                }
            }
            Write-Host 'Garlic closed. Removing temporary firewall access...'
        }

        if (-not $worker.WaitForExit(20000)) {
            throw 'Firewall cleanup has not finished. Close Garlic and run this BAT with /cleanup if needed.'
        }
        if (-not (Test-Path -LiteralPath $statusPath)) { throw 'Could not verify firewall cleanup. Run this BAT with /cleanup.' }
        $status = [IO.File]::ReadAllText($statusPath) | ConvertFrom-Json
        if (-not $status.Success) { throw ([string]$status.Error) }
        Write-Host 'Done. This helper''s firewall rules have been removed.' -ForegroundColor Green
        $resultCode = 0
    }
} catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    if ($sessionDir -and -not $appHandedOff) {
        try { [IO.File]::WriteAllText((Join-Path $sessionDir 'cancel'), '') } catch { }
    }
    if ($null -ne $worker) {
        try {
            if (-not $appHandedOff) { $null = $worker.WaitForExit(10000) }
            if (Test-Path -LiteralPath $statusPath) {
                $status = [IO.File]::ReadAllText($statusPath) | ConvertFrom-Json
                if ($status.Error) { Write-Host ([string]$status.Error) -ForegroundColor Yellow }
            }
        } catch { }
    }
    Write-Host 'If Windows permission was cancelled, run again and accept the UAC prompt.'
    Write-Host 'For leftover helper rules: close Garlic, then run this BAT with /cleanup.'
} finally {
    if ($null -ne $app) { $app.Dispose() }
    if ($sessionDir) {
        $canRemove = $null -eq $worker
        if ($null -ne $worker) {
            try { $worker.Refresh(); $canRemove = $worker.HasExited } catch { }
            $worker.Dispose()
        }
        if ($canRemove) { Remove-Item -LiteralPath $sessionDir -Recurse -Force -ErrorAction SilentlyContinue }
    }
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
exit $resultCode
