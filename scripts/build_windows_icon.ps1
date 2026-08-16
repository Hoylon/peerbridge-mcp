param(
    [string]$Source,
    [string]$PngOutput,
    [string]$IcoOutput
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$supportRoot = Join-Path $projectRoot 'src\peerbridge_mcp\release_support'
if (-not $Source) {
    $Source = Join-Path $supportRoot 'peerbridge-logo-source-owner-20260816.png'
}
if (-not $PngOutput) {
    $PngOutput = Join-Path $supportRoot 'peerbridge-icon.png'
}
if (-not $IcoOutput) {
    $IcoOutput = Join-Path $supportRoot 'peerbridge-icon.ico'
}

Add-Type -AssemblyName System.Drawing
[System.IO.Directory]::CreateDirectory((Split-Path -Parent $PngOutput)) | Out-Null
[System.IO.Directory]::CreateDirectory((Split-Path -Parent $IcoOutput)) | Out-Null

function New-CleanLogoBitmap([string]$Path) {
    $sourceBitmap = [System.Drawing.Bitmap]::new($Path)
    try {
        $clean = [System.Drawing.Bitmap]::new(
            $sourceBitmap.Width,
            $sourceBitmap.Height,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )
        $minX = $sourceBitmap.Width
        $minY = $sourceBitmap.Height
        $maxX = -1
        $maxY = -1
        for ($y = 0; $y -lt $sourceBitmap.Height; $y++) {
            for ($x = 0; $x -lt $sourceBitmap.Width; $x++) {
                $pixel = $sourceBitmap.GetPixel($x, $y)
                $minimumChannel = [Math]::Min($pixel.R, [Math]::Min($pixel.G, $pixel.B))
                $alpha = if ($pixel.A -eq 0) { 0 } else { 255 - $minimumChannel }
                if ($alpha -le 4) {
                    $clean.SetPixel($x, $y, [System.Drawing.Color]::Transparent)
                    continue
                }
                # Undo antialias blending against the owner's white source canvas.
                $red = [Math]::Min(255, [Math]::Max(
                    0,
                    [int][Math]::Round(255 + (($pixel.R - 255) * 255.0 / $alpha))
                ))
                $green = [Math]::Min(255, [Math]::Max(
                    0,
                    [int][Math]::Round(255 + (($pixel.G - 255) * 255.0 / $alpha))
                ))
                $blue = [Math]::Min(255, [Math]::Max(
                    0,
                    [int][Math]::Round(255 + (($pixel.B - 255) * 255.0 / $alpha))
                ))
                $clean.SetPixel(
                    $x,
                    $y,
                    [System.Drawing.Color]::FromArgb($alpha, $red, $green, $blue)
                )
                $minX = [Math]::Min($minX, $x)
                $minY = [Math]::Min($minY, $y)
                $maxX = [Math]::Max($maxX, $x)
                $maxY = [Math]::Max($maxY, $y)
            }
        }
        if ($maxX -lt $minX -or $maxY -lt $minY) {
            $clean.Dispose()
            throw 'The source image contains no visible logo pixels.'
        }
        return [pscustomobject]@{
            Bitmap = $clean
            Bounds = [System.Drawing.Rectangle]::new(
                $minX,
                $minY,
                $maxX - $minX + 1,
                $maxY - $minY + 1
            )
        }
    }
    finally {
        $sourceBitmap.Dispose()
    }
}

function New-SquareLogoBitmap($CleanLogo, [int]$Size, [double]$PaddingRatio) {
    $canvas = [System.Drawing.Bitmap]::new(
        $Size,
        $Size,
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
    )
    $graphics = [System.Drawing.Graphics]::FromImage($canvas)
    try {
        $graphics.Clear([System.Drawing.Color]::Transparent)
        $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
        $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
        $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
        $available = [Math]::Max(1, [int]($Size * (1.0 - (2.0 * $PaddingRatio))))
        $scale = [Math]::Min(
            $available / [double]$CleanLogo.Bounds.Width,
            $available / [double]$CleanLogo.Bounds.Height
        )
        $width = [Math]::Max(1, [int][Math]::Round($CleanLogo.Bounds.Width * $scale))
        $height = [Math]::Max(1, [int][Math]::Round($CleanLogo.Bounds.Height * $scale))
        $left = [int](($Size - $width) / 2)
        $top = [int](($Size - $height) / 2)
        $destination = [System.Drawing.Rectangle]::new($left, $top, $width, $height)
        $graphics.DrawImage(
            $CleanLogo.Bitmap,
            $destination,
            $CleanLogo.Bounds,
            [System.Drawing.GraphicsUnit]::Pixel
        )
    }
    finally {
        $graphics.Dispose()
    }
    return $canvas
}

$cleanLogo = New-CleanLogoBitmap $Source
try {
    $master = New-SquareLogoBitmap $cleanLogo 512 0.07
    try {
        $master.Save($PngOutput, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $master.Dispose()
    }

    $frames = @()
    foreach ($size in @(16, 20, 24, 32, 40, 48, 64, 128, 256)) {
        $bitmap = New-SquareLogoBitmap $cleanLogo $size 0.07
        try {
            $stream = [System.IO.MemoryStream]::new()
            try {
                $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
                $frames += [pscustomobject]@{ Size = $size; Bytes = $stream.ToArray() }
            }
            finally {
                $stream.Dispose()
            }
        }
        finally {
            $bitmap.Dispose()
        }
    }

    $output = [System.IO.File]::Open(
        $IcoOutput,
        [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    $writer = [System.IO.BinaryWriter]::new($output)
    try {
        $writer.Write([uint16]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]$frames.Count)
        $offset = 6 + (16 * $frames.Count)
        foreach ($frame in $frames) {
            $dimension = if ($frame.Size -eq 256) { 0 } else { $frame.Size }
            $writer.Write([byte]$dimension)
            $writer.Write([byte]$dimension)
            $writer.Write([byte]0)
            $writer.Write([byte]0)
            $writer.Write([uint16]1)
            $writer.Write([uint16]32)
            $writer.Write([uint32]$frame.Bytes.Length)
            $writer.Write([uint32]$offset)
            $offset += $frame.Bytes.Length
        }
        foreach ($frame in $frames) {
            $writer.Write([byte[]]$frame.Bytes)
        }
    }
    finally {
        $writer.Dispose()
        $output.Dispose()
    }
}
finally {
    $cleanLogo.Bitmap.Dispose()
}

[pscustomobject]@{
    Source = (Resolve-Path -LiteralPath $Source).Path
    SourceSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $Source).Hash.ToLowerInvariant()
    Png = (Resolve-Path -LiteralPath $PngOutput).Path
    PngSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $PngOutput).Hash.ToLowerInvariant()
    Ico = (Resolve-Path -LiteralPath $IcoOutput).Path
    IcoSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $IcoOutput).Hash.ToLowerInvariant()
    IcoFrames = $frames.Count
} | ConvertTo-Json
