# Set the SEWOO SLK-TS100 receipt printer's custom paper size ("User0000000256") to 72 x 135 mm
# so each receipt is cut at ~13.5 cm instead of the driver default 21 cm.
# Run on the kiosk as an administrator (e.g. kioskadmin over SSH):
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\SJIOT\set-receipt-paper.ps1
# A backup of the previous print ticket is written to C:\SJIOT\ticket.backup.xml.
param(
  [string]$PrinterName = 'SEWOO SLK-TS100',
  [int]$HeightMicrons = 135000
)
$pt = (Get-PrintConfiguration -PrinterName $PrinterName).PrintTicketXml
$pt | Set-Content -Encoding UTF8 'C:\SJIOT\ticket.backup.xml'
$new = [regex]::Replace($pt,
  '(?s)(<psf:Option name="ns0000:User0000000256">.*?MediaSizeHeight.*?<psf:Value xsi:type="xsd:integer">)\d+',
  '${1}' + $HeightMicrons)
if ($new -eq $pt) { Write-Output 'no change made (custom size option not found)'; exit 1 }
Set-PrintConfiguration -PrinterName $PrinterName -PrintTicketXml $new
Add-Type -AssemblyName System.Printing
$t = (New-Object System.Printing.LocalPrintServer).GetPrintQueue($PrinterName).DefaultPrintTicket
Write-Output ('default paper now: {0:N1} x {1:N1} mm' -f ($t.PageMediaSize.Width/96*25.4), ($t.PageMediaSize.Height/96*25.4))
