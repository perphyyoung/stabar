# 临时测量脚本:启动目标程序,统计启动耗时、内存、CPU、线程与句柄。用完即删。
param(
    [Parameter(Mandatory = $true)][string]$Label,
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$ProcName,
    [string]$ArgLine = "",
    [string]$WorkDir = "",
    [int]$IdleSeconds = 30
)

if ($WorkDir -eq "") { $WorkDir = Split-Path -Parent $Exe }
$cores = [Environment]::ProcessorCount
$exclude = @(Get-Process -Name $ProcName -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)

function Get-NewProcs {
    @(Get-Process -Name $ProcName -ErrorAction SilentlyContinue |
        Where-Object { $exclude -notcontains $_.Id })
}

function Get-Agg {
    $procs = @(Get-NewProcs)
    $ws = 0.0; $priv = 0.0; $thr = 0; $hnd = 0; $cpu = 0.0
    foreach ($p in $procs) {
        try {
            $ws += $p.WorkingSet64
            $priv += $p.PrivateMemorySize64
            $thr += $p.Threads.Count
            $hnd += $p.HandleCount
            $cpu += $p.TotalProcessorTime.TotalMilliseconds
        } catch { }
    }
    [pscustomobject]@{
        Ws      = [math]::Round($ws / 1MB, 1)
        Private = [math]::Round($priv / 1MB, 1)
        Threads = $thr
        Handles = $hnd
        CpuMs   = [math]::Round($cpu, 1)
        Count   = $procs.Count
    }
}

$t0 = Get-Date
Start-Process -FilePath $Exe -ArgumentList $ArgLine -WorkingDirectory $WorkDir | Out-Null

# 两个就绪信号:WS 越过 30MB(Qt 已加载) 与 主窗口句柄出现
$tReady = -1
$tWindow = -1
while (((Get-Date) - $t0).TotalSeconds -lt 45) {
    Start-Sleep -Milliseconds 40
    $procs = @(Get-NewProcs)
    if ($procs.Count -eq 0) { continue }
    if ($tReady -lt 0) {
        $ws = 0.0
        foreach ($p in $procs) { try { $ws += $p.WorkingSet64 } catch { } }
        if ($ws / 1MB -ge 30) { $tReady = [math]::Round(((Get-Date) - $t0).TotalMilliseconds) }
    }
    if ($tWindow -lt 0 -and ($procs | Where-Object { $_.MainWindowHandle -ne 0 })) {
        $tWindow = [math]::Round(((Get-Date) - $t0).TotalMilliseconds)
    }
    if ($tReady -ge 0 -and $tWindow -ge 0) { break }
}

Start-Sleep -Seconds 3
$start = Get-Agg
$w1 = Get-Date
$peakWs = $start.Ws
$wsList = @($start.Ws)
$elapsed = 0.0
while ($elapsed -lt $IdleSeconds) {
    Start-Sleep -Seconds 2
    $elapsed = ((Get-Date) - $w1).TotalSeconds
    $s = Get-Agg
    $wsList += $s.Ws
    if ($s.Ws -gt $peakWs) { $peakWs = $s.Ws }
}
$end = Get-Agg
$cpuIdle = [math]::Round($end.CpuMs - $start.CpuMs, 1)
$idleMs = ((Get-Date) - $w1).TotalMilliseconds
$cpuPctCore = [math]::Round($cpuIdle / $idleMs * 100, 3)
$wsAvg = [math]::Round(($wsList | Measure-Object -Average).Average, 1)

"===== $Label ====="
"进程数             : $($start.Count)"
"启动 -> WS>=30MB   : ${tReady} ms"
"启动 -> 窗口出现   : ${tWindow} ms"
"稳定后 WS / 私有   : $($start.Ws) MB / $($start.Private) MB"
"空闲 ${IdleSeconds}s WS 均值/峰值 : ${wsAvg} MB / ${peakWs} MB"
"线程 / 句柄        : $($start.Threads) / $($start.Handles)"
"空闲 ${IdleSeconds}s CPU 时间     : ${cpuIdle} ms"
"折算 CPU           : ${cpuPctCore}% (单核)  =  $([math]::Round($cpuPctCore / $cores, 3))% (整机 $cores 核)"
""

# 收尾:强杀整棵进程树并确认真的退出了。
# 只调一次 Stop-Process 不够——残留进程会锁住 dist 目录,导致下次 pyinstaller 打包失败。
for ($i = 0; $i -lt 20; $i++) {
    $alive = @(Get-NewProcs)
    if ($alive.Count -eq 0) { break }
    foreach ($p in $alive) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 250
}
$leftover = @(Get-NewProcs)
if ($leftover.Count -gt 0) {
    "警告: 进程未能结束,可能锁住 dist 目录 -> PID $(($leftover | ForEach-Object { $_.Id }) -join ', ')"
} else {
    "收尾: 测量进程已全部退出"
}
