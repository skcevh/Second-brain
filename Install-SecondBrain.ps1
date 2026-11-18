#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Client', 'Server', 'Both')][string]$Role,
    [string]$State = (Join-Path $env:USERPROFILE 'AMIROS\SecondBrain'),
    [string]$Inbox,
    [string]$Python,
    [ValidateRange(1,60)][int]$Minutes = 5,
    [string]$OllamaModel,
    [switch]$NoSchedule,
    [switch]$EnablePst
)
$ErrorActionPreference = 'Stop'
if (!$Python) { $Python = (Get-Command python.exe -CommandType Application | Select-Object -First 1).Source }
$Python = (Get-Item -LiteralPath $Python).FullName
& $Python -c "import sys,sqlite3; assert sys.version_info >= (3,11); sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')"
if ($LASTEXITCODE) { throw 'Python 3.11 of hoger met SQLite FTS5 is vereist.' }

function Write-JsonFile($Object, [string]$Path) {
    [IO.File]::WriteAllText($Path, ($Object | ConvertTo-Json -Depth 15), (New-Object Text.UTF8Encoding($false)))
}
function Assert-LocalRuntimePath([string]$Path) {
    & $Python -c 'from brain_core import outside_git; import sys; print(outside_git(sys.argv[1]))' $Path
    if ($LASTEXITCODE) { throw 'Gegevenspad moet absoluut en buiten elke Git-checkout liggen.' }
}
Push-Location $PSScriptRoot
try {
    if ($State.StartsWith('\\')) { throw 'Gebruik lokale NTFS-opslag.' }
    $State = Assert-LocalRuntimePath $State
    if ((Get-Volume -DriveLetter $State.Substring(0,1)).FileSystem -ne 'NTFS') { throw 'NTFS is vereist.' }
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $sid = $current.User.Value
    if (!(Test-Path -LiteralPath $State)) {
        New-Item -ItemType Directory -Path $State -Force | Out-Null
    }
    $acl = Get-Acl -LiteralPath $State
    $trusted = @($sid, 'S-1-5-18', 'S-1-5-32-544' | Select-Object -Unique)
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    $unexpected = @($rules | Where-Object {
        $_.IdentityReference.Value -notin $trusted -or $_.FileSystemRights -ne 'FullControl' -or
        $_.AccessControlType -ne 'Allow' -or $_.InheritanceFlags -ne 'ContainerInherit, ObjectInherit'
    })
    if (!$acl.AreAccessRulesProtected -or $rules.Count -ne $trusted.Count -or $unexpected.Count) {
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
        foreach ($identity in $trusted) {
            $entry = New-Object Security.AccessControl.FileSystemAccessRule(
                (New-Object Security.Principal.SecurityIdentifier($identity)),
                'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
            )
            $acl.AddAccessRule($entry)
        }
        Set-Acl -LiteralPath $State -AclObject $acl
    }

    $app = Join-Path $State 'app'
    $null = New-Item -ItemType Directory -Path $app -Force
    foreach ($file in 'secondbrain.py','brain_core.py','brain_sources.py','brain_store.py','Read-Outlook.ps1') {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $app -Force
    }
    if ($EnablePst) {
        $requirements = Join-Path $PSScriptRoot 'config\pst-requirements.txt'
        & $Python -m pip install --no-deps --only-binary ':all:' --require-hashes --target (Join-Path $app 'vendor') -r $requirements
        if ($LASTEXITCODE) { throw 'PST-import vereist CPython 3.12 x64. De optionele installatie is mislukt.' }
    }
    $cli = Join-Path $State 'brain.cmd'
    $command = '"' + $Python + '" -X utf8 "' + (Join-Path $app 'secondbrain.py') + '" --state "' + $State + '"'
    [IO.File]::WriteAllLines($cli, @('@echo off', "$command %*"), [Text.Encoding]::Default)
    if ($Role -ne 'Client') {
        $server = Join-Path $State 'server.json'
        if (!(Test-Path -LiteralPath $server)) {
            if (!$Inbox) { $Inbox = Join-Path $State 'inbox' }
            if ($Inbox.StartsWith('\\')) { throw 'Serverinbox moet een lokaal pad zijn.' }
            $Inbox = Assert-LocalRuntimePath $Inbox
            if (!(Test-Path -LiteralPath $Inbox)) {
                if ($Inbox -ine (Join-Path $State 'inbox')) {
                    throw 'Maak een externe inbox eerst aan met Enable-SecondBrainInbox.ps1.'
                }
                New-Item -ItemType Directory -Path $Inbox | Out-Null
            }
            $config = Get-Content (Join-Path $PSScriptRoot 'config\server.example.json') -Raw | ConvertFrom-Json
            $config | Add-Member -NotePropertyName inbox -NotePropertyValue $Inbox
            if ($OllamaModel) { $config.ollama_model = $OllamaModel }
            Write-JsonFile $config $server
        } elseif (!$Inbox) {
            $Inbox = (Get-Content -LiteralPath $server -Raw | ConvertFrom-Json).inbox
        }
    }
    if ($Role -ne 'Server') {
        $client = Join-Path $State 'client.json'
        if (!(Test-Path -LiteralPath $client)) {
            $config = Get-Content (Join-Path $PSScriptRoot 'config\client.example.json') -Raw | ConvertFrom-Json
            if ($Inbox) { $config.inbox = $Inbox }
            Write-JsonFile $config $client
        }
        foreach ($source in 'chatgpt','copilot-vscode','outlook') {
            $null = New-Item -ItemType Directory -Path (Join-Path $State "imports\$source") -Force
        }
    }

    if (!$NoSchedule) {
        $taskName = "AMIROS Second Brain ($sid)"
        $argument = '-X utf8 "{0}" --state "{1}" run' -f (Join-Path $app 'secondbrain.py'), $State
        $action = New-ScheduledTaskAction -Execute $Python -Argument $argument -WorkingDirectory $State
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
        $logon = New-ScheduledTaskTrigger -AtLogOn -User $sid
        $principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
        $options = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 4)
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($trigger, $logon) -Principal $principal -Settings $options -Force | Out-Null
    }
    & $Python -X utf8 (Join-Path $app 'secondbrain.py') --state $State run
    if ($LASTEXITCODE) { throw 'De eerste taak is niet volledig geslaagd. Controleer status.' }
    Write-Host "Second Brain CLI: $cli"
} finally {
    Pop-Location
}
