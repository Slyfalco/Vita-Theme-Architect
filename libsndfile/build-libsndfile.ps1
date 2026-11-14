# Build a single self-contained ARM64 Windows libsndfile.dll (format deps linked
# in statically) via vcpkg, and drop it into libsndfile/windows-arm64/ so source
# runs and ARM builds pick it up. Run once; it's independent of the app version.
#
#   powershell -ExecutionPolicy Bypass -File .\libsndfile\build-libsndfile.ps1
#
# Requires vcpkg (https://github.com/microsoft/vcpkg) on PATH or VCPKG_ROOT set,
# plus the MSVC ARM64 build tools (cross-compiles fine from an x64 machine).
param([switch]$KeepVcpkg)   # -KeepVcpkg: don't delete an auto-bootstrapped vcpkg afterwards
$ErrorActionPreference = 'Stop'

# Find vcpkg; if it isn't installed, bootstrap a THROWAWAY copy (needs git) that
# we delete again after the build, so nothing is left behind.
$ownVcpkg = $false
$tmpVcpkgRoot = $null
$vcpkg = (Get-Command vcpkg -ErrorAction SilentlyContinue).Source
if (-not $vcpkg -and $env:VCPKG_ROOT) { $vcpkg = Join-Path $env:VCPKG_ROOT 'vcpkg.exe' }
if (-not $vcpkg -or -not (Test-Path $vcpkg)) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "Neither vcpkg nor git is available - can't auto-build libsndfile."
        Write-Host "  Install git (or vcpkg) and re-run.  https://github.com/microsoft/vcpkg"
        exit 1
    }
    $tmpVcpkgRoot = Join-Path $env:TEMP ("vcpkg-vta-" + [guid]::NewGuid().ToString('N').Substring(0,8))
    Write-Host "vcpkg not found - bootstrapping a temporary copy at $tmpVcpkgRoot (removed afterwards)..."
    git clone --depth 1 https://github.com/microsoft/vcpkg "$tmpVcpkgRoot"
    if ($LASTEXITCODE -ne 0) { Write-Host "git clone of vcpkg failed."; exit 1 }
    & (Join-Path $tmpVcpkgRoot 'bootstrap-vcpkg.bat') -disableMetrics | Out-Host
    $vcpkg = Join-Path $tmpVcpkgRoot 'vcpkg.exe'
    if (-not (Test-Path $vcpkg)) {
        Write-Host "vcpkg bootstrap failed."
        Remove-Item "$tmpVcpkgRoot" -Recurse -Force -ErrorAction SilentlyContinue
        exit 1
    }
    $ownVcpkg = $true
}

function Remove-TempVcpkg {
    # Leave no trace: delete a vcpkg we bootstrapped ourselves (not the user's own).
    if ($ownVcpkg -and -not $KeepVcpkg -and $tmpVcpkgRoot -and (Test-Path $tmpVcpkgRoot)) {
        Write-Host "Removing temporary vcpkg ($tmpVcpkgRoot) ..."
        Remove-Item "$tmpVcpkgRoot" -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$triplet = 'arm64-windows-mixed'
$overlay = Join-Path $here 'triplets'
New-Item -ItemType Directory -Force -Path $overlay | Out-Null

# Mixed triplet: dependencies static, libsndfile itself a DLL => one self-
# contained libsndfile.dll with FLAC/ogg/vorbis/opus/mpg123 linked inside.
@'
set(VCPKG_TARGET_ARCHITECTURE arm64)
set(VCPKG_CRT_LINKAGE dynamic)
set(VCPKG_LIBRARY_LINKAGE static)
if(PORT STREQUAL "libsndfile")
    set(VCPKG_LIBRARY_LINKAGE dynamic)
endif()
'@ | Set-Content -Path (Join-Path $overlay "$triplet.cmake") -Encoding ascii

Write-Host "Building libsndfile:$triplet (this takes a while the first time)..."
& $vcpkg install "libsndfile:$triplet" --overlay-triplets="$overlay"
if ($LASTEXITCODE -ne 0) { Write-Host "vcpkg build failed."; Remove-TempVcpkg; exit 1 }

$root = Split-Path -Parent $vcpkg
$built = Join-Path $root "installed\$triplet\bin\sndfile.dll"
if (-not (Test-Path $built)) { Write-Host "Built DLL not found at $built"; Remove-TempVcpkg; exit 1 }

$dest = Join-Path $here 'windows-arm64'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item $built (Join-Path $dest 'libsndfile.dll') -Force

# Copy each component's license (vcpkg stores them as share/<port>/copyright).
$share = Join-Path $root "installed\$triplet\share"
if (Test-Path $share) {
    Get-ChildItem -Path $share -Recurse -Filter copyright -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $dest ("LICENSE-" + $_.Directory.Name + ".txt")) -Force
    }
}

$placeholder = Join-Path $dest 'PLACE_DLL_HERE.txt'
if (Test-Path $placeholder) { Remove-Item $placeholder -Force }

# DLL is copied out; remove the throwaway vcpkg (if we bootstrapped it).
Remove-TempVcpkg

Write-Host ""
Write-Host "Done -> $dest\libsndfile.dll (single self-contained DLL + licenses)."
Write-Host "Now just run main.py or rebuild; ARM mp3/flac/ogg support is enabled."
