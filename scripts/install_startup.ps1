<#
SoloShip 登录自启安装脚本（P05-B）

默认只打印将要执行的动作，**不实际注册**。确认无误后加 -Approve 才会注册。
运行级别固定为 Limited（普通用户），不使用管理员权限运行开发任务。
脚本内不包含、也不读取任何 GitHub Token。

用法：
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_startup.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Approve
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Uninstall -Approve
#>
[CmdletBinding()]
param(
    [string]$Root = 'E:\Documents\AI-Developer\repositories\SoloShip',
    [string]$TaskName = 'SoloShip Supervisor',
    [switch]$Approve,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$startScript = Join-Path $Root 'scripts\soloship_start.ps1'
$userId = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }

Write-Host '=== SoloShip Supervisor 登录自启 ===' -ForegroundColor Cyan
Write-Host "  任务名   : $TaskName"
Write-Host "  用户     : $userId（RunLevel=Limited，普通用户）"
Write-Host "  触发     : 用户登录时（AtLogOn）"
Write-Host "  动作     : powershell.exe -File `"$startScript`" -Root `"$Root`""
Write-Host '  凭据     : 不写入任何 Token；发布时由环境变量提供'
Write-Host ''

if ($Uninstall) {
    if (-not $Approve) {
        Write-Host '[预览] 将执行：Unregister-ScheduledTask -TaskName ' -NoNewline
        Write-Host "'$TaskName' -Confirm:`$false"
        Write-Host '未加 -Approve，未做任何更改。'
        exit 0
    }
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Host "任务不存在：$TaskName"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除计划任务：$TaskName" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path -LiteralPath $startScript)) {
    Write-Error "启动脚本缺失: $startScript"
    exit 5
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`" -Root `"$Root`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited

if (-not $Approve) {
    Write-Host '[预览] 将要注册的计划任务：' -ForegroundColor Yellow
    Write-Host ("  Action : {0} {1}" -f $action.Execute, $action.Arguments)
    Write-Host  "  Trigger: AtLogOn ($userId)"
    Write-Host  "  Principal: RunLevel=Limited, LogonType=Interactive"
    Write-Host  "  Settings : StartWhenAvailable, MultipleInstances=IgnoreNew, 无执行时限"
    Write-Host ''
    Write-Host '未加 -Approve，未做任何更改。确认无误后重新运行并加 -Approve。' -ForegroundColor Yellow
    exit 0
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description 'SoloShip Supervisor：用户登录后后台接收 ChatGPT Work 任务（普通用户权限）' | Out-Null

$info = Get-ScheduledTask -TaskName $TaskName
Write-Host ("已注册：{0}（State={1}, RunLevel={2}）" -f $info.TaskName, $info.State, $info.Principal.RunLevel) -ForegroundColor Green
Write-Host '验证：'
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "  py -3 bridge\supervisor.py status --root `"$Root`""
Write-Host '撤销：'
Write-Host "  powershell -File scripts\install_startup.ps1 -Uninstall -Approve"
exit 0
