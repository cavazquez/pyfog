<#
.SYNOPSIS
  Read-only PyFog Windows inventory agent.

.DESCRIPTION
  Emits the versioned PyFog inventory JSON and optionally submits it over HTTPS.  The script
  never captures or restores disks.  Authentication is supplied through an environment variable
  and is only placed in the HTTP Authorization header; it is never included in the JSON or logs.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)][string]$Server,
    [Parameter(Mandatory = $false)][guid]$HostId,
    [Parameter(Mandatory = $false)][string]$Output
)

$ErrorActionPreference = "Stop"

function Get-StableDiskId {
    param([object]$Disk)
    if ($Disk.SerialNumber) {
        return "serial:" + ([string]$Disk.SerialNumber).Trim()
    }
    throw "El disco no tiene serial para una identidad estable."
}

function Get-MacAddress {
    param([object]$Adapter)
    $compact = ([string]$Adapter.MacAddress).Replace("-", "")
    if ($compact -notmatch '^[0-9A-Fa-f]{12}$') {
        return $null
    }
    $octets = [regex]::Matches($compact, '.{2}') | ForEach-Object { $_.Value }
    return ($octets -join ':').ToLowerInvariant()
}

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$computer = Get-CimInstance -ClassName Win32_ComputerSystem
$bios = Get-CimInstance -ClassName Win32_BIOS
$warnings = @()
$firmware = "bios"
try {
    $firmware_type = [int](Get-ItemPropertyValue -Path "HKLM:\SYSTEM\CurrentControlSet\Control" -Name PEFirmwareType)
    if ($firmware_type -eq 2) { $firmware = "uefi" }
    elseif ($firmware_type -ne 1) { $warnings += "firmware_detection_unavailable" }
} catch {
    $warnings += "firmware_detection_unavailable"
}
$disks = @(
    Get-CimInstance -ClassName Win32_DiskDrive | ForEach-Object {
        [ordered]@{
            name = "PhysicalDrive$($_.Index)"
            stable_id = Get-StableDiskId $_
            size_bytes = [int64]$_.Size
            model = ([string]$_.Model).Trim()
            serial_number = ([string]$_.SerialNumber).Trim()
            wwn = ""
            transport = ([string]$_.InterfaceType).Trim().ToLowerInvariant()
            logical_sector_bytes = 512
            partition_table = $null
            removable = $false
        }
    }
)
$interfaces = @(
    Get-CimInstance -ClassName Win32_NetworkAdapterConfiguration -Filter "IPEnabled = True" |
        ForEach-Object {
            $mac = Get-MacAddress $_
            if ($mac) {
                [ordered]@{
                    name = ([string]$_.Description).Substring(0, [Math]::Min(100, $_.Description.Length))
                    mac_address = $mac
                    state = "up"
                }
            }
        }
)
if ($interfaces.Count -eq 0) {
    throw "No se encontró una interfaz con MAC válida."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "El agente Windows requiere un sistema operativo de 64 bits."
}
$architecture = "x86_64"

$payload = [ordered]@{
    schema_version = 1
    report_id = ([guid]::NewGuid()).ToString()
    collected_at = [DateTime]::UtcNow.ToString("o")
    hostname = $env:COMPUTERNAME
    firmware = $firmware
    os = [ordered]@{
        name = ([string]$os.Caption).Trim()
        id = "windows"
        version = ([string]$os.Version).Trim()
    }
    kernel = ([string]$os.Version).Trim()
    architecture = $architecture
    cpu = [ordered]@{
        model = ([string](Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1 -ExpandProperty Name)).Trim()
        logical_cores = [int](Get-CimInstance -ClassName Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
    }
    memory = [ordered]@{ total_bytes = [int64]$computer.TotalPhysicalMemory }
    system = [ordered]@{
        manufacturer = ([string]$computer.Manufacturer).Trim()
        model = ([string]$computer.Model).Trim()
        serial_number = ([string]$bios.SerialNumber).Trim()
    }
    disks = $disks
    interfaces = $interfaces
    warnings = $warnings
}

$json = $payload | ConvertTo-Json -Depth 8 -Compress
if ($Output) {
    $parent = Split-Path -Parent $Output
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    Set-Content -Path $Output -Value $json -Encoding UTF8 -NoNewline
}
if ($Server) {
    if (-not $HostId) { throw "-HostId es obligatorio al usar -Server." }
    $token = $env:PYFOG_INVENTORY_TOKEN
    if (-not $token -or $token.Length -gt 256 -or $token -match '[\r\n]') {
        throw "Definí PYFOG_INVENTORY_TOKEN sin imprimirlo en la consola."
    }
    if ($Server -notmatch '^https://[^/]+/?$') { throw "-Server debe ser una URL HTTPS sin ruta." }
    Invoke-RestMethod -Method Post -Uri "$($Server.TrimEnd('/'))/api/v1/hosts/$HostId/inventory" `
        -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" -Body $json | Out-Null
}
