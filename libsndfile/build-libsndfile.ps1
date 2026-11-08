# Build a single self-contained ARM64 Windows libsndfile.dll (format deps linked
# in statically) via vcpkg, and drop it into libsndfile/windows-arm64/ so source
# runs and ARM builds pick it up. Run once; it's independent of the app version.
#
#   powershell -ExecutionPolicy Bypass -File .\libsndfile\build-libsndfile.ps1
#
# Requires vcpkg (https://github.com/microsoft/vcpkg) on PATH or VCPKG_ROOT set,
# plus the MSVC ARM64 build tools (cross-compiles fine from an x64 machine).
$ErrorActionPreference = 'Stop'

$vcpkg = (Get-Command vcpkg -ErrorAction SilentlyContinue).Source
if (-not $vcpkg -and $env:VCPKG_ROOT) { $vcpkg = Join-Path $env:VCPKG_ROOT 'vcpkg.exe' }
if (-not $vcpkg -or -not (Test-Path $vcpkg)) {
    Write-Host "vcpkg not found. Install it and put 'vcpkg' on PATH (or set VCPKG_ROOT)."
    Write-Host "  https://github.com/microsoft/vcpkg"
    exit 1
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
if ($LASTEXITCODE -ne 0) { Write-Host "vcpkg build failed."; exit 1 }

$root = Split-Path -Parent $vcpkg
$built = Join-Path $root "installed\$triplet\bin\sndfile.dll"
if (-not (Test-Path $built)) { Write-Host "Built DLL not found at $built"; exit 1 }

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

Write-Host ""
Write-Host "Done -> $dest\libsndfile.dll (single self-contained DLL + licenses)."
Write-Host "Now just run main.py or rebuild; ARM mp3/flac/ogg support is enabled."
