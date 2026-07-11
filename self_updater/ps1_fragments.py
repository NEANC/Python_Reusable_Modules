#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""PowerShell 脚本片段生成函数。"""

import textwrap


def generate_common_base_functions_ps1() -> str:
    """生成 Helper 与 Update 共享的基础 PowerShell 函数片段。"""
    return textwrap.dedent(r"""
        function Normalize-IniValue($value) {
            if ($null -eq $value) { return "" }
            return ([string]$value) -replace "(`r`n|`n|`r)", " "
        }

        function Assert-NotEmpty($name, $value) {
            if ([string]::IsNullOrWhiteSpace($value)) {
                throw "missing required ini value: $name"
            }
        }

        function Write-Log($level, $message) {
            try {
                $line = "{0} -> {1} | {2} | {3}" -f $scriptTag, (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $level, $message
                Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
            } catch {}
        }
    """)


def generate_common_state_functions_ps1() -> str:
    """生成 Helper 与 Update 共享的 INI 状态读写 PowerShell 函数片段。"""
    return textwrap.dedent(r"""
        function Read-IniValue($section, $key) {
            try {
                $content = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 -ErrorAction Stop
                $sectionEsc = [regex]::Escape("[$section]")
                $keyEsc = [regex]::Escape($key)
                $sectionPattern = "(?ms)^$sectionEsc\s*\r?\n(.*?)(?=^\s*\[|\z)"
                if ($content -match $sectionPattern) {
                    $keyPattern = "(?m)^$keyEsc\s*=\s*(.*?)[\r\t ]*$"
                    if ($matches[1] -match $keyPattern) { return $matches[1] }
                }
            } catch {}
            return ""
        }

        function Write-IniValue($section, $key, $value) {
            try {
                $value = Normalize-IniValue $value
                $lines = @(Get-Content -LiteralPath $stateFile -Encoding UTF8 -ErrorAction Stop)

                $out = New-Object System.Collections.Generic.List[string]
                $inSection = $false
                $sectionFound = $false
                $keyWritten = $false
                $keyEsc = [regex]::Escape($key)

                foreach ($line in $lines) {
                    if ($line -match '^\s*\[(.+?)\]\s*$') {
                        if ($inSection -and -not $keyWritten) {
                            $out.Add("$key = $value")
                            $keyWritten = $true
                        }
                        $inSection = ($matches[1] -eq $section)
                        if ($inSection) { $sectionFound = $true }
                        $out.Add($line)
                        continue
                    }

                    if ($inSection -and -not $keyWritten -and $line -match "^\s*$keyEsc\s*=") {
                        $out.Add("$key = $value")
                        $keyWritten = $true
                        continue
                    }

                    $out.Add($line)
                }

                if (-not $sectionFound) {
                    if ($out.Count -gt 0 -and $out[-1].Trim() -ne '') { $out.Add("") }
                    $out.Add("[$section]")
                    $out.Add("$key = $value")
                } elseif ($inSection -and -not $keyWritten) {
                    $out.Add("$key = $value")
                }

                $tmp = "$stateFile.tmp"
                [System.IO.File]::WriteAllLines($tmp, [string[]]$out.ToArray())
                Move-Item -LiteralPath $tmp -Destination $stateFile -Force
            } catch {
                Write-Log "ERROR" "Write-IniValue failed: $($_.Exception.Message)"
            }
        }

        function Set-UpdateStatus($state, $step, $message, $progress, $level) {
            $message = Normalize-IniValue $message
            if ($state) { Write-IniValue "State" "state" $state }
            if ($step) { Write-IniValue "State" "current_step" $step }
            if ($null -ne $progress) { Write-IniValue "State" "progress" "$progress" }
            if ($level) { Write-IniValue "State" "level" $level }
            Write-IniValue "State" "message" $message
            Write-IniValue "State" "updated_at" (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff')
            if ($level -eq "ERROR") { Write-IniValue "State" "last_error" $message }
            Write-Log $level $message
            try {
                Write-Host ("[{0}] [{1}] {2} - {3}" -f (Get-Date -Format "HH:mm:ss"), $level, $step, $message)
            } catch {}
        }
    """)


def generate_move_with_retry_ps1() -> str:
    """生成 Helper 与 Update 共享的 PowerShell 文件移动重试函数片段。"""
    return textwrap.dedent(r"""
        function Move-WithRetry($src, $dst, $timeoutSec) {
            $deadline = (Get-Date).AddSeconds($timeoutSec)
            $lastError = $null
            while ((Get-Date) -lt $deadline) {
                try {
                    Move-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
                    return
                } catch {
                    $lastError = $_.Exception.Message
                    Start-Sleep -Milliseconds 1000
                }
            }
            throw "Move failed after retry: $src -> $dst ; $lastError"
        }
    """)


def generate_helper_argument_functions_ps1() -> str:
    """生成 Helper 专用的 PowerShell 参数转义函数片段。"""
    return textwrap.dedent(r"""
        function Quote-Arg($arg) {
            if ($null -eq $arg) { return '""' }
            $s = [string]$arg
            $s = $s -replace '\\(?=")', '\\'
            $s = $s -replace '"', '\"'
            if ($s -match '\s' -or $s -eq '') {
                return '"' + $s + '"'
            }
            return $s
        }
    """)


def generate_helper_retry_functions_ps1() -> str:
    """生成 Helper 专用的 PowerShell 重试配置读取函数片段。"""
    return textwrap.dedent(r"""
        function Get-RetryOrDefault($name, $default) {
            $val = Read-IniValue "Retry" $name
            if ($val -match '^\d+$') { return [int]$val }
            return $default
        }
    """)


def generate_helper_file_cleanup_functions_ps1() -> str:
    """生成 Helper 专用的 PowerShell 文件清理重试函数片段。"""
    return textwrap.dedent(r"""
        function Remove-WithRetry($path, $timeoutSec) {
            $deadline = (Get-Date).AddSeconds($timeoutSec)
            $lastError = $null
            while ((Get-Date) -lt $deadline) {
                try {
                    if (Test-Path -LiteralPath $path) {
                        Remove-Item -LiteralPath $path -Force -ErrorAction Stop
                    }
                    return
                } catch {
                    $lastError = $_.Exception.Message
                    Start-Sleep -Milliseconds 1000
                }
            }
            throw "Remove failed after retry: $path ; $lastError"
        }
    """)


def generate_helper_lifecycle_functions_ps1() -> str:
    """生成 Helper 专用的 PowerShell 更新生命周期函数片段。"""
    return textwrap.dedent(r"""
        function Commit-Update {
            try {
                $backup = Read-IniValue "Files" "backup_file"
                Write-IniValue "Retry" "retry_count" "0"
                Write-IniValue "State" "last_error" ""
                Write-IniValue "State" "state" "verified"
                if ($backup -and (Test-Path -LiteralPath $backup)) {
                    Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
                }
                if (Test-Path -LiteralPath $lockFile) {
                    Remove-Item -LiteralPath $lockFile -Force -ErrorAction SilentlyContinue
                }
                Write-Log "INFO" "update committed"
            } catch {
                Write-Log "WARN" "Commit-Update failed: $($_.Exception.Message)"
            }
        }

        function Restore-Backup($reason) {
            Set-UpdateStatus "rollback" "rollback_start" "准备回滚：$reason" 80 "ERROR"
            try {
                $target = Read-IniValue "Files" "target"
                $backup = Read-IniValue "Files" "backup_file"

                Assert-NotEmpty "Files.target" $target
                Assert-NotEmpty "Files.backup_file" $backup

                if (!(Test-Path -LiteralPath $backup)) {
                    Set-UpdateStatus "failed_disabled" "rollback_no_backup" "备份文件不存在: $backup" 100 "ERROR"
                    if (Test-Path -LiteralPath $target) {
                        Start-NormalAppVisible $target @('--update-failed')
                    }
                    exit 2
                }

                if (Test-Path -LiteralPath $target) {
                    Remove-WithRetry $target 30
                }
                Move-WithRetry $backup $target 60
                Set-UpdateStatus "rollback_done" "rollback_done" "已恢复旧版本：$reason" 100 "ERROR"

                $retry = Get-RetryOrDefault "retry_count" 0
                $max   = Get-RetryOrDefault "max_retry" 3
                $retry++
                Write-IniValue "Retry" "retry_count" "$retry"

                if ($retry -lt $max) {
                    Start-NormalAppVisible $target @('--retry-update')
                } else {
                    Set-UpdateStatus "failed_disabled" "retry_limit_reached" "更新失败次数达到上限，已禁用本版本更新" 100 "ERROR"
                    Start-NormalAppVisible $target @('--update-failed')
                }
                exit 1
            } catch {
                Set-UpdateStatus "failed_disabled" "rollback_failed" "回滚失败: $($_.Exception.Message)" 100 "ERROR"
                exit 3
            }
        }

        function Start-ProcWait($filePath, [string[]]$argList, $timeoutSec, [bool]$resetPyInstallerEnv = $false) {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $filePath
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.WorkingDirectory = Split-Path -Parent $filePath
            $argsArr = @($argList | ForEach-Object { Quote-Arg $_ })
            $psi.Arguments = if ($argsArr.Count -gt 0) { $argsArr -join ' ' } else { '' }

            if ($resetPyInstallerEnv) {
                $psi.EnvironmentVariables["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
                foreach ($k in @("_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL",
                                 "_PYI_APPLICATION_HOME_DIR", "_PYI_SPLASH_IPC",
                                 "_PYI_LINUX_PROCESS_NAME")) {
                    if ($psi.EnvironmentVariables.ContainsKey($k)) {
                        $psi.EnvironmentVariables.Remove($k)
                    }
                }
            }

            $proc = [System.Diagnostics.Process]::Start($psi)
            if ($proc.WaitForExit($timeoutSec * 1000)) {
                return $proc.ExitCode
            }
            try {
                if (-not $proc.HasExited) {
                    $proc.Kill()
                    $proc.WaitForExit(5000) | Out-Null
                }
            } catch {}
            return -1
        }

        function Start-NormalAppVisible($filePath, [string[]]$argList = @()) {
            $workDir = Split-Path -Parent $filePath

            $oldReset = [Environment]::GetEnvironmentVariable("PYINSTALLER_RESET_ENVIRONMENT", "Process")
            $oldPyi = @{}
            $pyiKeys = @("_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL",
                         "_PYI_APPLICATION_HOME_DIR", "_PYI_SPLASH_IPC",
                         "_PYI_LINUX_PROCESS_NAME")
            foreach ($k in $pyiKeys) {
                $oldPyi[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
            }

            try {
                [Environment]::SetEnvironmentVariable("PYINSTALLER_RESET_ENVIRONMENT", "1", "Process")
                foreach ($k in $pyiKeys) {
                    [Environment]::SetEnvironmentVariable($k, $null, "Process")
                }

                $argsArr = @($argList | ForEach-Object { Quote-Arg $_ })
                $argString = if ($argsArr.Count -gt 0) { $argsArr -join ' ' } else { '' }

                $startArgs = @{
                    FilePath = $filePath
                    WorkingDirectory = $workDir
                    WindowStyle = 'Normal'
                }
                if ($argString) {
                    $startArgs.ArgumentList = $argString
                }
                Start-Process @startArgs
            }
            finally {
                [Environment]::SetEnvironmentVariable("PYINSTALLER_RESET_ENVIRONMENT", $oldReset, "Process")
                foreach ($k in $pyiKeys) {
                    [Environment]::SetEnvironmentVariable($k, $oldPyi[$k], "Process")
                }
            }
        }
    """)


def generate_sha256_function_ps1() -> str:
    """生成 PowerShell SHA256 多路径回退函数片段。"""
    return textwrap.dedent(r"""
        function Get-SHA256($filePath) {
            $lastError = $null

            $stream = $null
            $sha256 = $null
            try {
                $stream = [System.IO.File]::OpenRead($filePath)
                $sha256 = [System.Security.Cryptography.SHA256]::Create()
                $hash = $sha256.ComputeHash($stream)
                return [BitConverter]::ToString($hash).Replace('-', '').ToLowerInvariant()
            } catch {
                $lastError = $_.Exception.Message
            } finally {
                if ($sha256) { $sha256.Dispose() }
                if ($stream) { $stream.Dispose() }
            }

            try {
                if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
                    return (Get-FileHash -Algorithm SHA256 -LiteralPath $filePath -ErrorAction Stop).Hash.ToLowerInvariant()
                }
            } catch {
                $lastError = $_.Exception.Message
            }

            try {
                $certOutput = & certutil.exe -hashfile $filePath SHA256 2>&1
                if ($LASTEXITCODE -ne 0) {
                    throw ($certOutput -join "`n")
                }
                foreach ($line in $certOutput) {
                    $hex = $line -replace '\s', ''
                    if ($hex -match '^[0-9A-Fa-f]{64}$') {
                        return $hex.ToLowerInvariant()
                    }
                }
                throw "certutil output did not contain a SHA256 hash"
            } catch {
                $lastError = $_.Exception.Message
            }

            throw "Get-SHA256 failed: $lastError"
        }
    """)
