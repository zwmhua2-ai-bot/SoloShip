<#
SoloShip Supervisor 启动脚本（供 Windows 任务计划程序调用）

职责：把 Supervisor 以普通用户权限拉起（后台运行），然后立即返回。
本脚本不写入、不读取任何 GitHub Token；凭据只在发布环节由环境变量提供。
#>
[CmdletBinding()]
param(
    [string]$Root = 'C:\Users\hua\SoloShip-P00'
)

$ErrorActionPreference = 'Stop'

$supervisor = Join-Path $Root 'bridge\supervisor.py'
if (-not (Test-Path -LiteralPath $supervisor)) {
    Write-Error "找不到 Supervisor: $supervisor"
    exit 5
}

$py = 'py'
$cmd = Get-Command py -ErrorAction SilentlyContinue
if (-not $cmd) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = 'python' } else {
        Write-Error '未找到 Python（py/python）'
        exit 5
    }
}

$logDir = Join-Path $Root '.control\supervisor\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$bootLog = Join-Path $logDir 'start-script.log'

"$(Get-Date -Format o) 启动 Supervisor（用户: $env:USERNAME, 管理员: $false）" |
    Out-File -LiteralPath $bootLog -Append -Encoding utf8

# supervisor.py 在被重定向时一律以 UTF-8 写 stdout（见 force_utf8_stdio），
# 这里把本进程解码子进程输出的编码也设成 UTF-8，否则中文会被按 cp936 解错，
# 落进 start-script.log 就是乱码。仅影响本进程的流解码，不改任何环境变量。
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
}
catch {
    # 无控制台句柄时忽略：不影响退出码与单实例判断
}

# start 自身会做单实例检查：已有实例时退出码 3，这里原样透出，不重复启动。
# 注意：不能用 `*>>` —— PowerShell 5.1 的文件重定向固定写 UTF-16LE，会和本脚本
# 自己用 Out-File -Encoding utf8 写的行混在同一文件里，变成编码大杂烩。
# 这里逐行落盘（仍是增量写入，进程卡住也不丢已产出的行），保证整段新增内容都是 UTF-8。
& $py -3 $supervisor start --root $Root 2>&1 | ForEach-Object {
    $_ | Out-File -LiteralPath $bootLog -Append -Encoding utf8
}
$rc = $LASTEXITCODE

# 3 = 已有实例在运行（单实例锁生效）。对登录自启来说这是正常结果，
# 映射为 0，避免任务计划程序把「重复触发」记成失败。
if ($rc -eq 3) {
    "$(Get-Date -Format o) 已有 Supervisor 在运行，本次不重复启动（start 退出码 3 → 视为成功）" |
        Out-File -LiteralPath $bootLog -Append -Encoding utf8
    exit 0
}

"$(Get-Date -Format o) supervisor start 退出码 $rc" | Out-File -LiteralPath $bootLog -Append -Encoding utf8
exit $rc
