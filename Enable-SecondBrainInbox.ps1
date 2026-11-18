#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string[]]$ClientAccount,
    [Parameter(Mandatory)][string]$ArchiveAccount,
    [string]$Path = (Join-Path $env:ProgramData 'AMIROS.SecondBrain.Inbox')
)
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ine 'AMIROS-SERVER') { throw 'Voer dit script uit op AMIROS-SERVER.' }
if ($Path -notmatch '^[a-z]:\\' -or (Test-Path -LiteralPath $Path)) { throw 'Kies een nieuwe lokale NTFS-inboxmap.' }
$name = 'AMIROS-SecondBrain$'
if (Get-SmbShare -Name $name -ErrorAction SilentlyContinue) { throw 'De inboxshare bestaat al.' }
$config = Get-SmbServerConfiguration
if (!$config.EnableSMB2Protocol -or !$config.RejectUnencryptedAccess) {
    throw 'SMB 3 met verplichte versleuteling is vereist; bestaand serverbeleid blijft actief.'
}
$admin = (New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')).Translate([Security.Principal.NTAccount]).Value
$full = @($ArchiveAccount, $admin | Select-Object -Unique)
$system = (New-Object Security.Principal.SecurityIdentifier('S-1-5-18')).Translate([Security.Principal.NTAccount]).Value
$accounts = @{}
foreach ($account in @($full + $ClientAccount + $system | Select-Object -Unique)) {
    $identity = (New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier])
    if ($identity.Value -in 'S-1-1-0','S-1-5-2','S-1-5-11','S-1-5-32-545','S-1-5-7') {
        throw 'Gebruik individuele, vertrouwde Windows-accounts.'
    }
    $accounts[$account] = $identity
}
$probe = Get-Item -LiteralPath (Split-Path -Parent $Path)
for ($directory = $probe; $directory; $directory = $directory.Parent) {
    if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Inboxpad mag geen junction bevatten.' }
    if (Test-Path -LiteralPath (Join-Path $directory.FullName '.git')) { throw 'Inbox moet buiten Git liggen.' }
}
if ((Get-Volume -DriveLetter $Path.Substring(0, 1)).FileSystem -ne 'NTFS') { throw 'NTFS is vereist.' }

New-Item -ItemType Directory -Path $Path | Out-Null
$acl = Get-Acl -LiteralPath $Path
$acl.SetAccessRuleProtection($true, $false)
foreach ($account in $accounts.Keys) {
    $rights = if ($account -in ($full + $system)) { 'FullControl' } else { 'Modify' }
    $entry = New-Object Security.AccessControl.FileSystemAccessRule(
        $accounts[$account], $rights, 'ContainerInherit,ObjectInherit', 'None', 'Allow'
    )
    $acl.AddAccessRule($entry)
}
Set-Acl -LiteralPath $Path -AclObject $acl
$params = @{
    Name = $name; Path = $Path; EncryptData = $true; FullAccess = $full
    FolderEnumerationMode = 'AccessBased'; CachingMode = 'None'
}
$change = @($ClientAccount | Where-Object { $_ -notin $full })
if ($change.Count) { $params.ChangeAccess = $change }
New-SmbShare @params | Select-Object Name, Path, EncryptData
Write-Host "Stel server.json inbox in op $Path en gebruik \\$env:COMPUTERNAME\$name op clients."
