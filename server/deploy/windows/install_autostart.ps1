<#
낙상 감지 서버를 Windows에 로그인할 때 자동으로 켜지게 작업 스케줄러에 등록한다(현재 사용자).

  install_autostart.bat                 로그인 때 서버 자동 실행 등록
  install_autostart.bat -WithUpdate     + 10분마다 GitHub의 새 코드를 받아 시험 후 재시작(자동 배포)
  install_autostart.bat -Firewall       + Windows 방화벽에서 8000 포트 허용(개인 네트워크만, 관리자 권한 필요)
  install_autostart.bat -Remove         등록한 작업 지우기

안내 음성(Windows 합성 음성)이 사용자 세션에서 동작하므로 '로그인할 때' 실행한다. PC가 켜지면 자동 로그인되게
해 두면 정전 뒤에도 서버가 다시 켜진다.
#>
param(
    [switch]$WithUpdate,
    [switch]$Firewall,
    [switch]$Remove,
    [int]$UpdateMinutes = 10,
    [int]$Port = 8000
)
$ErrorActionPreference = "Stop"
$startTask = "FallDetection-Server"
$updateTask = "FallDetection-Update"

if ($Remove) {
    foreach ($name in $startTask, $updateTask) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Host "자동 실행·자동 업데이트 등록을 지웠습니다."
    return
}

# 서버: 시간 제한 없음. 작업 스케줄러 기본 우선순위(7)는 '낮음 이하'라 영상 처리가 밀리므로 보통(4)으로 켠다.
$serverSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -Priority 4

$bat = Join-Path $PSScriptRoot "start_server.bat"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c start `"FallServer`" /normal /min `"$bat`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
Register-ScheduledTask -TaskName $startTask -Action $action -Trigger $trigger -Settings $serverSettings `
    -Description "낙상 감지 PC 서버 (start_server.bat)" -Force | Out-Null
Write-Host "등록: Windows 로그인 때 서버 자동 실행 ($startTask)"

if ($WithUpdate) {
    $ps = Join-Path $PSScriptRoot "update_server.ps1"
    $updateAction = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps`""
    $updateTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes $UpdateMinutes)
    # 업데이트: 한 번에 30분까지만 돈다(git·pip가 멈춰도 다음 업데이트를 막지 않게). 이전 실행이 아직 돌고 있으면
    # (낙상 확인이 끝나기를 기다리는 중 등) 새 실행은 건너뛴다.
    $updateSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $updateTask -Action $updateAction -Trigger $updateTrigger -Settings $updateSettings `
        -Description "낙상 감지 서버 자동 업데이트 (update_server.ps1)" -Force | Out-Null
    Write-Host "등록: $UpdateMinutes분마다 자동 업데이트 ($updateTask). 기록: data\logs\update.log"
}

if ($Firewall) {
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) {
        Write-Warning "방화벽 규칙은 관리자 권한이 필요합니다. 관리자 PowerShell에서 -Firewall만 다시 실행하세요."
    } else {
        $rule = "FallDetection Server $Port"
        Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
        New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $Port `
            -Profile Private -Action Allow | Out-Null
        Write-Host "등록: 방화벽에서 $Port 포트 허용 (개인 네트워크만)"
    }
}

Write-Host "지금 바로 켜려면 start_server.bat를 실행하거나 PC에 다시 로그인하세요."
