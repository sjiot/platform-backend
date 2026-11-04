# SEWOO SLK-TS100: change the driver's custom paper ("User" form) height from 210mm to 135mm
# so each receipt is cut at ~13.5cm instead of 21cm.
#
# The custom size lives in the registry value
#   HKLM\SYSTEM\CurrentControlSet\Control\Print\Printers\SEWOO SLK-TS100\PrinterDriverData\FORMS
# (width/height stored as 32-bit microns: 72000 / 210000). PrintTicket and DEVMODE changes are
# overridden by the driver from this value, so the registry value itself has to change.
#
# Run on the kiosk as administrator (kioskadmin over SSH):
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\SJIOT\set-receipt-paper.ps1
# Backup of the old value is written to C:\SJIOT\FORMS.backup.bin. To restore:
#   Set-ItemProperty -Path $k -Name FORMS -Type Binary -Value ([IO.File]::ReadAllBytes('C:\SJIOT\FORMS.backup.bin'))
param([int]$NewHeightMicrons = 135000, [int]$OldHeightMicrons = 210000)

$k = 'HKLM:\SYSTEM\CurrentControlSet\Control\Print\Printers\SEWOO SLK-TS100\PrinterDriverData'
$b = (Get-ItemProperty $k).FORMS
[IO.File]::WriteAllBytes('C:\SJIOT\FORMS.backup.bin', $b)

function Find([byte[]]$h, [byte[]]$n) {
  for ($i = 0; $i -le $h.Length - $n.Length; $i++) {
    $ok = $true
    for ($j = 0; $j -lt $n.Length; $j++) { if ($h[$i + $j] -ne $n[$j]) { $ok = $false; break } }
    if ($ok) { return $i }
  }
  return -1
}

$old = [BitConverter]::GetBytes([int]$OldHeightMicrons)
$new = [BitConverter]::GetBytes([int]$NewHeightMicrons)
$i = Find $b $old
Write-Output "height $OldHeightMicrons found at byte $i"
if ($i -lt 0) { Write-Output 'old height not found - nothing changed'; exit 1 }
for ($j = 0; $j -lt 4; $j++) { $b[$i + $j] = $new[$j] }

# cosmetic: the display text " x 210 mm" -> " x 135 mm" (same length, UTF-16)
$oldTxt = [Text.Encoding]::Unicode.GetBytes(("{0} mm" -f ($OldHeightMicrons / 1000)))
$newTxt = [Text.Encoding]::Unicode.GetBytes(("{0} mm" -f ($NewHeightMicrons / 1000)))
$i2 = Find $b $oldTxt
if ($i2 -ge 0 -and $oldTxt.Length -eq $newTxt.Length) { for ($j = 0; $j -lt $oldTxt.Length; $j++) { $b[$i2 + $j] = $newTxt[$j] } }

Set-ItemProperty -Path $k -Name FORMS -Value $b -Type Binary
Write-Output 'registry updated, restarting Print Spooler'
Restart-Service Spooler
Start-Sleep -Seconds 3

Add-Type -AssemblyName System.Printing
$t = (New-Object System.Printing.LocalPrintServer).GetPrintQueue('SEWOO SLK-TS100').DefaultPrintTicket
Write-Output ('default paper now: {0:N1} x {1:N1} mm' -f ($t.PageMediaSize.Width / 96 * 25.4), ($t.PageMediaSize.Height / 96 * 25.4))
