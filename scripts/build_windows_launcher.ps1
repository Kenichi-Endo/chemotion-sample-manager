$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw 'The .NET Framework C# compiler was not found.'
}
$output = Join-Path $root 'launchers\windows\Chemotion Sample Manager.exe'
$source = Join-Path $PSScriptRoot 'windows-launcher.cs'
& $compiler /nologo /target:winexe /platform:x64 /reference:System.Windows.Forms.dll "/out:$output" $source
if ($LASTEXITCODE -ne 0) {
    throw "The launcher build failed with exit code $LASTEXITCODE."
}
