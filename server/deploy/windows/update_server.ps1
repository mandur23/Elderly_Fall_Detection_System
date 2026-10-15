<#
자동 배포: GitHub에 올라온 새 코드를 서버 PC에 반영한다.

  1. 지금 브랜치의 원격(origin)에 새 커밋이 있는지 본다. 없으면 아무것도 하지 않는다.
  2. 이 PC에서 고친 파일이 있거나, 원격 기록이 이어지지 않거나(fast-forward 불가), 브랜치가 아닌 커밋을 직접
     꺼내 둔 상태(detached HEAD)면 건드리지 않는다. 이런 안내는 상황이 바뀔 때 한 번만 기록한다.
  3. 새 커밋을 임시 작업 폴더(data\update_test, git worktree)에 꺼내 server 의존성을 맞추고, 지금 설정 파일을
     새 코드로 읽어 보고, shared·server 시험을 돌린다. 실행 중인 코드(저장소 폴더)는 이때까지 그대로다.
  4. 통과하면 저장소를 새 커밋으로 옮기고(fast-forward) 실행 중인 서버를 끈다. start_server.bat가 곧 새 코드로
     다시 켠다. 단, 서버가 낙상 확인 중이면(/api/health의 state가 NORMAL이 아님) 15초마다 다시 보며 최대
     -WaitMinutes(10)분 기다리고, 그래도 바쁘면 data\update_restart_pending.txt를 남겨 다음 실행 때 재시작만 다시 한다.
  5. 시험이 실패하면 패키지를 지금 코드의 requirements로 되돌리고, 그 커밋을 data\update_failed_commit.txt에 적어
     더 새 커밋이 올라올 때까지 다시 시험하지 않는다(다시 시험하려면 이 파일을 지운다). 서버는 그대로 둔다.

  update_server.ps1             한 번 확인·반영
  update_server.ps1 -DryRun     새 코드가 있는지만 확인
기록: data\logs\update.log. install_autostart.bat -WithUpdate로 10분마다 돌게 등록할 수 있다.
#>
param(
    [string]$Remote = "origin",
    [string]$Branch = "",
    [switch]$DryRun,
    [int]$WaitMinutes = 10,   # 낙상 확인이 끝나기를 기다리는 최대 시간(분)
    [int]$Port = 0            # 0이면 FALL_SERVER_PORT, 설정 파일의 port, 8000 순으로 정한다
)
# Windows PowerShell 5.1은 git·python이 stderr에 쓰는 안내(warning 등)를 'Stop'에서 오류로 멈추므로
# Continue로 두고, 외부 명령은 종료 코드($LASTEXITCODE)로 성공을 판단한다.
$ErrorActionPreference = "Continue"
# 작업 스케줄러에서 사람 없이 돌므로 git이 비밀번호·로그인 창을 기다리며 멈추지 않게 한다.
$env:GIT_TERMINAL_PROMPT = "0"
$env:GCM_INTERACTIVE = "never"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..") -ErrorAction Stop).Path
Set-Location $root
if (-not $env:GIT_SSH_COMMAND -and -not $env:GIT_SSH -and -not (& git.exe config --get core.sshCommand 2>$null)) {
    $env:GIT_SSH_COMMAND = "ssh -o BatchMode=yes"   # ssh 주소를 쓸 때도 암호 질문 없이 실패하게
}
$python = if ($env:FALL_PYTHON) { $env:FALL_PYTHON.Trim('"') } else { Join-Path $root ".venv\Scripts\python.exe" }
$data = Join-Path $root "data"
New-Item -ItemType Directory -Force (Join-Path $data "logs") -ErrorAction Stop | Out-Null
$log = Join-Path $data "logs\update.log"
$pendingFile = Join-Path $data "update_restart_pending.txt"   # 미룬 재시작: 커밋, 저장소를 옮긴 시각
$failedFile = Join-Path $data "update_failed_commit.txt"      # 시험에 실패해 건너뛰는 커밋, 실패 종류
$noticeFile = Join-Path $data "update_notice.txt"             # 마지막으로 기록한 안내(같은 안내 반복 방지)
$testTree = Join-Path $data "update_test"                     # 새 커밋을 시험하는 임시 작업 폴더
if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) { Move-Item $log "$log.old" -Force }

function Log([string]$message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $log -Value $line -Encoding UTF8
    Write-Host $line
}

# 같은 안내를 10분마다 되풀이해 기록하지 않는다. 정상으로 돌아오면(Clear-Notice) 다음에 다시 기록한다.
function Write-Notice([string]$key, [string]$message) {
    $previous = if (Test-Path $noticeFile) { [IO.File]::ReadAllText($noticeFile).Trim() } else { "" }
    if ($previous -ne $key) {
        [IO.File]::WriteAllText($noticeFile, $key)
        Log $message
    }
}

function Clear-Notice { Remove-Item $noticeFile -Force -ErrorAction SilentlyContinue }

function Short([string]$hash) { if ($hash.Length -gt 7) { $hash.Substring(0, 7) } else { $hash } }

function Invoke-Git {
    # 표준 출력만 결과로 쓴다. stderr(경고·진행 안내)가 섞이면 'git status' 결과를 수정 파일로 잘못 볼 수 있다.
    $all = & git.exe @args 2>&1
    $stdout = @($all | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"
    if ($LASTEXITCODE -ne 0) {
        $stderr = @($all | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" }) -join " "
        throw "git $($args -join ' ') 실패: $stderr"
    }
    return $stdout.Trim()
}

function Invoke-Python([string[]]$arguments) {
    # cmd로 실행해 파이썬 출력을 그대로(UTF-8) 기록 파일에 붙인다. 지금 위치(Push-Location)에서 실행한다.
    # 종료 코드를 돌려준다. 공백·괄호가 있는 인자는 따옴표로 감싼다(인자 안에 큰따옴표는 쓰지 않는다).
    # 명령 줄은 환경변수로 넘긴다. 따옴표가 든 인자를 직접 넘기면 PowerShell 버전마다 따옴표를 다르게 덧붙여 깨진다.
    $quoted = foreach ($a in $arguments) { if ($a -match '[\s&()^|<>;]') { "`"$a`"" } else { $a } }
    $env:FALL_UPDATE_COMMAND = "`"$python`" " + ($quoted -join " ") + " >> `"$log`" 2>&1"
    cmd.exe /d /c "%FALL_UPDATE_COMMAND%" | Out-Null
    $code = $LASTEXITCODE
    Remove-Item Env:FALL_UPDATE_COMMAND -ErrorAction SilentlyContinue
    return $code
}

function Install-Requirements([string]$tree) {
    return Invoke-Python @("-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                           "-r", (Join-Path $tree "server\requirements.txt"))
}

function Get-ConfigPath {
    $config = if ($env:FALL_SERVER_CONFIG) { $env:FALL_SERVER_CONFIG.Trim('"') } else { "server\config.toml" }
    if (-not [IO.Path]::IsPathRooted($config)) { $config = Join-Path $root $config }
    return $config
}

# 서버 포트: -Port, 환경변수 FALL_SERVER_PORT, 설정 파일의 port, 기본 8000 순 (server/config.py와 같은 순서).
function Get-ServerPort {
    if ($Port -gt 0) { return $Port }
    if ($env:FALL_SERVER_PORT -match '^\s*(\d+)\s*$') { return [int]$Matches[1] }
    $config = Get-ConfigPath
    if (Test-Path -LiteralPath $config) {
        foreach ($line in [IO.File]::ReadAllLines($config)) {
            if ($line -match '^\s*port\s*=\s*(\d+)') { return [int]$Matches[1] }
        }
    }
    return 8000
}
$healthUrl = "http://127.0.0.1:$(Get-ServerPort)/api/health"

# 서버의 낙상 확인 상태(NORMAL·SUSPECT·ALARM 등). 서버가 없거나(연결 거부) 상태를 알 수 없는 응답이면 "",
# 응답이 늦으면(바쁘거나 멈춤) "TIMEOUT".
function Get-ServerState {
    try {
        $request = [System.Net.WebRequest]::Create($healthUrl)
        $request.Proxy = $null
        $request.Timeout = 10000
        $request.ReadWriteTimeout = 10000
        $response = $request.GetResponse()
        try {
            $body = (New-Object System.IO.StreamReader($response.GetResponseStream())).ReadToEnd()
        } finally {
            $response.Close()
        }
    } catch {
        $e = $_.Exception
        while ($e -and -not ($e -is [System.Net.WebException])) { $e = $e.InnerException }
        if ($e -and $e.Status -eq [System.Net.WebExceptionStatus]::Timeout) { return "TIMEOUT" }
        return ""
    }
    try { $state = (ConvertFrom-Json $body).state } catch { return "" }
    if ($state) { return [string]$state }
    return ""
}

# 이 설치의 파이썬으로 $since 전에 켜진 서버(python -m server). Windows 가상환경의 python.exe는 실제 파이썬을
# 자식 프로세스로 띄우므로 자식도 함께 돌려준다.
function Get-OldServers([datetime]$since) {
    if (-not (Test-Path -LiteralPath $python)) { return @{ Servers = @(); Children = @() } }
    $target = (Resolve-Path -LiteralPath $python).Path
    $all = @(Get-CimInstance Win32_Process)
    $servers = @($all | Where-Object {
        $_.ExecutablePath -eq $target -and $_.CommandLine -match '\s-m\s+server(\s|$)' -and $_.CreationDate -lt $since })
    $ids = @($servers | ForEach-Object { $_.ProcessId })
    $children = @($all | Where-Object { $ids -contains $_.ParentProcessId })
    return @{ Servers = $servers; Children = $children }
}

function Set-Pending([string]$commit, [datetime]$since) {
    [IO.File]::WriteAllText($pendingFile, "$commit`n$($since.Ticks)")
}

function Get-Pending {
    if (-not (Test-Path $pendingFile)) { return $null }
    $parts = [IO.File]::ReadAllText($pendingFile).Split("`n")
    try { $since = New-Object DateTime ([long]$parts[1].Trim()) } catch { $since = (Get-Item $pendingFile).LastWriteTime }
    return @{ Commit = $parts[0].Trim(); Since = $since }
}

# 저장소를 $commit으로 옮긴 뒤($since) 이전 코드로 실행 중인 서버를 끈다. start_server.bat가 새 코드로 다시 켠다.
# 낙상 확인 중이면 끝날 때까지 기다리고, -WaitMinutes 안에 끝나지 않으면 다음 실행으로 미룬다(대기 기록 유지).
function Restart-Server([string]$commit, [datetime]$since) {
    if ((Get-OldServers $since).Servers.Count -eq 0) {
        Remove-Item $pendingFile -Force -ErrorAction SilentlyContinue
        Log "$(Short $commit) 반영 완료. 이전 코드로 실행 중인 서버가 없습니다(다음에 켤 때 새 코드로 실행)."
        return $true
    }
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    $state = Get-ServerState
    while ($state -and $state -ne "NORMAL") {
        Write-Notice "busy:$commit" ("서버가 낙상 확인 중(상태 $state)이라 $(Short $commit) 재시작을 미루고 기다립니다. " +
            "확인이 끝나면(NORMAL) 다시 시작합니다(기록: data\update_restart_pending.txt).")
        if ((Get-Date) -ge $deadline) { return $false }
        Start-Sleep -Seconds 15
        $state = Get-ServerState
    }
    $old = Get-OldServers $since
    foreach ($p in $old.Servers + $old.Children) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Remove-Item $pendingFile -Force -ErrorAction SilentlyContinue
    Clear-Notice
    $shown = if ($state) { $state } else { "응답 없음" }
    Log "$(Short $commit)로 서버를 다시 시작합니다 (서버 상태 $shown, 끈 프로세스 $($old.Servers.Count + $old.Children.Count)개)."
    for ($i = 0; $i -lt 24; $i++) {
        Start-Sleep -Seconds 5
        if (Get-ServerState) {
            Log "서버가 새 코드로 다시 응답합니다."
            return $true
        }
    }
    Log "경고: 2분이 지나도 서버가 응답하지 않습니다. start_server.bat 창과 data\logs\server-console.log를 확인하세요."
    return $true
}

function Remove-TestTree {
    if (Test-Path -LiteralPath $testTree) {
        & git.exe worktree remove --force $testTree 2>&1 | Out-Null
        if (Test-Path -LiteralPath $testTree) { Remove-Item -LiteralPath $testTree -Recurse -Force -ErrorAction SilentlyContinue }
    }
    & git.exe worktree prune 2>&1 | Out-Null
}

# 새 커밋을 임시 작업 폴더에 꺼내 시험한다. 저장소 폴더(실행 중인 코드)는 건드리지 않는다.
# 통과하면 $null, 실패하면 @{ Kind = pip|config|test; Message = ... }.
function Test-Commit([string]$commit) {
    Remove-TestTree
    try {
        Invoke-Git worktree add --detach --force $testTree $commit | Out-Null
        if ((Install-Requirements $testTree) -ne 0) {
            return @{ Kind = "pip"; Message = "패키지 설치(pip install) 실패" }
        }
        Push-Location $testTree
        try {
            $config = Get-ConfigPath
            if (Test-Path -LiteralPath $config) {
                $check = "import sys;from server.config import load_settings;load_settings(sys.argv[1])"
                if ((Invoke-Python @("-c", $check, $config)) -ne 0) {
                    return @{ Kind = "config"; Message = "새 코드가 지금 설정 파일($config)을 읽지 못함" }
                }
            }
            $failedSuites = @(foreach ($suite in "shared", "server") {
                if ((Invoke-Python @("-m", "unittest", "discover", "-s", "$suite\tests", "-t", ".")) -ne 0) { $suite }
            })
            if ($failedSuites.Count) {
                return @{ Kind = "test"; Message = "$($failedSuites -join ', ') 시험 실패" }
            }
        } finally {
            Pop-Location
        }
        return $null
    } finally {
        Remove-TestTree
    }
}

# 같은 저장소의 업데이트가 이미 돌고 있으면(직접 실행과 예약 실행이 겹침) 기다리지 않고 끝낸다.
$lock = $null
try {
    $sha = [Security.Cryptography.SHA256]::Create()
    $id = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))[0..7] | ForEach-Object { $_.ToString("x2") })
    $lock = New-Object System.Threading.Mutex($false, "Global\FallDetection-Update-$id")
    $owned = $false
    try { $owned = $lock.WaitOne(0) } catch { $owned = $true }   # 강제로 끝난 이전 실행의 잠금은 넘겨받는다
    if (-not $owned) {
        Write-Host "다른 업데이트가 실행 중이라 끝냅니다."
        exit 0
    }
} catch {
    $lock = $null   # 잠금을 못 만들어도 업데이트는 한다
}

try {
    $head = Invoke-Git rev-parse HEAD

    # 지난번에 서버가 바빠 미뤄 둔 재시작이 있으면 그것만 다시 한다.
    $pending = Get-Pending
    if ($pending) {
        if ($pending.Commit -eq $head) {
            [void](Restart-Server $head $pending.Since)
            exit 0
        }
        Remove-Item $pendingFile -Force -ErrorAction SilentlyContinue
        Log "재시작 대기 기록($(Short $pending.Commit))이 지금 코드($(Short $head))와 달라 지웠습니다."
    }

    $current = Invoke-Git rev-parse --abbrev-ref HEAD
    if ($current -eq "HEAD") {
        Write-Notice "detached" ("브랜치가 아닌 커밋을 직접 꺼내 둔 상태(detached HEAD)라 자동 업데이트하지 않습니다. " +
            "'git switch <브랜치>'로 돌아가면 다시 동작합니다.")
        exit 0
    }
    if (-not $Branch) { $Branch = $current }
    if (Invoke-Git status --porcelain --untracked-files=no) {
        Write-Notice "local-changes" "이 PC에서 고친 파일이 있어 업데이트하지 않습니다 (git status 확인)."
        exit 0
    }
    try {
        # 느린 연결이 1분 넘게 멈춰 있으면 끊는다.
        Invoke-Git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 fetch --quiet $Remote $Branch | Out-Null
    } catch {
        Write-Notice "fetch" "새 코드를 받지 못했습니다(네트워크·인증 확인). 받을 수 있을 때까지 다시 기록하지 않습니다: $_"
        exit 1
    }
    $old = $head
    $new = Invoke-Git rev-parse "$Remote/$Branch"
    if ($old -eq $new) {
        Clear-Notice
        exit 0
    }
    & git.exe merge-base --is-ancestor $new HEAD 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Clear-Notice   # 이 PC가 원격보다 앞서 있다(아직 올리지 않은 커밋). 받을 것이 없다.
        exit 0
    }
    & git.exe merge-base --is-ancestor HEAD $new 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Notice "diverged:$new" ("원격 $Remote/$Branch 기록이 이 PC와 이어지지 않아(강제 push 등) 자동 업데이트하지 않습니다. " +
            "직접 확인하세요.")
        exit 1
    }
    if (Test-Path $failedFile) {
        $failed = [IO.File]::ReadAllText($failedFile).Trim().Split(" ")
        # 시험에 실패한 커밋은 다시 시험하지 않는다. 패키지 설치 실패(네트워크 등)만 1시간 뒤 다시 해 본다.
        if ($failed[0] -eq $new -and -not ($failed.Count -gt 1 -and $failed[1] -eq "pip" -and
                (Get-Item $failedFile).LastWriteTime -lt (Get-Date).AddHours(-1))) {
            exit 0
        }
    }

    Log "새 코드: $(Short $old) -> $(Short $new) ($Remote/$Branch)"
    if ($DryRun) {
        Log "(-DryRun) 반영하지 않고 멈춥니다."
        exit 0
    }

    $env:PYTHONUTF8 = "1"
    $problem = Test-Commit $new
    if ($problem) {
        # 새 requirements로 바뀐 패키지를 지금 코드의 requirements로 되돌린다. 서버는 지금 코드로 계속 동작한다.
        $restore = if ((Install-Requirements $root) -ne 0) { " 패키지를 되돌리는 pip install도 실패했습니다." } else { "" }
        [IO.File]::WriteAllText($failedFile, "$new $($problem.Kind)")
        Log ("$($problem.Message) (자세한 내용은 위 기록): $(Short $new)는 반영하지 않고 더 새 커밋이 올라올 때까지 " +
             "건너뜁니다(다시 시험하려면 data\update_failed_commit.txt를 지움). 서버는 $(Short $old) 코드로 계속 동작합니다.$restore")
        exit 1
    }

    $since = Get-Date
    Invoke-Git merge --ff-only --quiet $new | Out-Null
    Remove-Item $failedFile -Force -ErrorAction SilentlyContinue
    Clear-Notice
    Set-Pending $new $since
    Log "시험 통과: 저장소를 $(Short $new)로 옮겼습니다."
    [void](Restart-Server $new $since)
} catch {
    Log "업데이트 오류: $_"
    exit 1
} finally {
    if ($lock) { try { $lock.ReleaseMutex() } catch { } }
}
