#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$List,
    [string]$StoreId,
    [string]$Since
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
try {
    $outlook = [Runtime.InteropServices.Marshal]::GetActiveObject('Outlook.Application')
} catch {
    [Console]::Error.WriteLine('Open klassieke Outlook met een ingesteld profiel.')
    exit 3
}
$mapi = $outlook.GetNamespace('MAPI')
if ($List) {
    foreach ($store in $mapi.Stores) {
        [pscustomobject]@{ name = $store.DisplayName; id = $store.StoreID } | ConvertTo-Json -Compress
    }
    exit 0
}
$store = $mapi.Stores | Where-Object { $_.StoreID -eq $StoreId } | Select-Object -First 1
if (!$store) { [Console]::Error.WriteLine('Outlook-store ontbreekt.'); exit 3 }
$sinceDate = if ($Since) { [DateTimeOffset]::Parse($Since).LocalDateTime } else { [datetime]::MinValue }
$script:failed = 0
function Read-Folder($Folder) {
    try {
        if ($Folder.DefaultItemType -eq 0) {
            $items = $Folder.Items
            $items.Sort('[LastModificationTime]', $true)
            for ($index = 1; $index -le $items.Count; $index++) {
                $item = $null
                try {
                    $item = $items.Item($index)
                    if ($item.LastModificationTime -lt $sinceDate) { break }
                    if ($item.Class -ne 43 -or $item.Sensitivity -ge 2 -or
                        $item.MessageClass -match 'SMIME|RPMSG') { continue }
                    $internetId = $null
                    try {
                        $internetId = $item.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/0x1035001F')
                    } catch {
                        # Drafts and legacy items may only expose the stable MAPI ID.
                    }
                    [pscustomobject]@{
                        internet_id = $internetId; id = $item.EntryID; subject = $item.Subject
                        sender = $item.SenderName; to = $item.To; cc = $item.CC; folder = $Folder.FolderPath
                        body = $item.Body; sent = $item.SentOn.ToUniversalTime().ToString('o')
                        modified = $item.LastModificationTime.ToUniversalTime().ToString('o')
                    } | ConvertTo-Json -Compress
                } catch {
                    $script:failed++
                } finally {
                    if ($null -ne $item) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($item) }
                }
            }
        }
        foreach ($child in $Folder.Folders) { Read-Folder $child }
    } catch {
        $script:failed++
    }
}
Read-Folder ($store.GetRootFolder())
if ($script:failed) {
    [Console]::Error.WriteLine("Outlook: $script:failed items of mappen konden niet gelezen worden.")
    exit 4
}
