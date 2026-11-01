# 키오스크 PC(Windows)에서 실행하는 원격 업데이트 스크립트.
# GitHub Release(태그 v*)에 올라온 최신 zip을 받아 설치 폴더를 교체하고 앱을 다시 켭니다.
# Linux 에서:  ssh 사용자@PC주소 "powershell -ExecutionPolicy Bypass -File C:\SJIOT\update-kiosk.ps1"
# 필요: GitHub CLI(gh) 설치 + `gh auth login` 1회 (비공개 저장소면 필수)
# 주의: 한글 때문에 UTF-8 BOM 으로 저장되어 있습니다(Windows PowerShell 5.1 이 BOM 없으면 깨뜨림).
param(
  [string]$Repo       = "geumkyung06/SJIOT",
  [string]$InstallDir = "C:\SJIOT\kiosk",          # 앱(exe+dll+data)이 있는 폴더 — 실제 경로로 맞추세요
  [string]$ExeName    = "clicky_keyring.exe",
  [string]$TaskName   = "SJIOT-Kiosk"
)
$ErrorActionPreference = "Stop"
$proc   = [IO.Path]::GetFileNameWithoutExtension($ExeName)
$work   = Join-Path $env:TEMP "sjiot-update"
$backup = "$InstallDir.bak"
$appStopped  = $false   # 앱을 종료했는가 -> 실패 시 다시 켜야 함
$canRollback = $false   # 이번 실행에서 백업을 끝냈는가 -> 그때만 복구 (이전 업데이트의 낡은 백업으로 되돌리면 안 됨)

function Start-Kiosk {
  # SSH 세션에서 직접 실행하면 화면에 안 뜨므로, 로그인된 사용자 세션에서 뜨는 예약 작업으로 실행
  if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    $action    = New-ScheduledTaskAction -Execute (Join-Path $InstallDir $ExeName) -WorkingDirectory $InstallDir
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
    # 기본값은 3일 뒤 강제 종료 + 배터리 사용 시 미실행이라 키오스크에 맞게 해제
    $settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
  }
  Start-ScheduledTask -TaskName $TaskName
}

try {
  Write-Host "[1/5] 최신 릴리즈 다운로드"
  if (Test-Path $work) { Remove-Item $work -Recurse -Force }
  New-Item -ItemType Directory $work | Out-Null
  gh release download --repo $Repo --pattern "*.zip" --dir $work --clobber
  $zip = Get-ChildItem $work -Filter *.zip | Select-Object -First 1
  if (-not $zip) { throw "zip 을 받지 못했습니다 (릴리즈/gh 로그인 확인)" }
  Expand-Archive $zip.FullName -DestinationPath "$work\new" -Force
  if (-not (Test-Path "$work\new\$ExeName")) { throw "zip 안에 $ExeName 이 없습니다" }

  Write-Host "[2/5] 앱 종료"
  $appStopped = $true
  Stop-Process -Name $proc -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2

  Write-Host "[3/5] 기존 버전 백업 -> $backup"
  if (Test-Path $backup) { Remove-Item $backup -Recurse -Force }
  if (Test-Path $InstallDir) {
    Copy-Item $InstallDir $backup -Recurse -Force
    $canRollback = $true
  }

  Write-Host "[4/5] 새 버전 설치"
  New-Item -ItemType Directory $InstallDir -Force | Out-Null
  robocopy "$work\new" $InstallDir /MIR /NFL /NDL /NJH /NJS | Out-Null   # robocopy 0~7 은 성공
  if ($LASTEXITCODE -ge 8) { throw "복사 실패 (robocopy $LASTEXITCODE)" }

  Write-Host "[5/5] 앱 실행"
  Start-Kiosk
  Write-Host "완료: $($zip.Name)"
}
catch {
  Write-Host "실패: $_"
  if ($canRollback) {
    Write-Host "백업으로 복구합니다"
    robocopy $backup $InstallDir /MIR /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { Write-Host "복구 실패 (robocopy $LASTEXITCODE) - $backup 확인 필요" }
  }
  if ($appStopped) { Start-Kiosk }
  exit 1
}
