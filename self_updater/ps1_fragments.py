#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""PowerShell 脚本片段生成函数。"""

import textwrap


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
