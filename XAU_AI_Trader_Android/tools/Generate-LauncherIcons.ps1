# Packaging only: scale the approved image intact; never redraw or crop it.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$project = Split-Path $PSScriptRoot -Parent
$asset = Join-Path (Split-Path $project -Parent) 'branding/dardania_xautrade_icon.png'
$source = [System.Drawing.Image]::FromFile($asset)
try {
    foreach ($density in @(@('mdpi',1),@('hdpi',1.5),@('xhdpi',2),@('xxhdpi',3),@('xxxhdpi',4))) {
        $name=$density[0]; $scale=[double]$density[1]
        $folder=Join-Path $project "app/src/main/res/mipmap-$name"
        New-Item -ItemType Directory -Force $folder | Out-Null
        foreach ($kind in @('ic_launcher_foreground','ic_launcher','ic_launcher_round')) {
            $foreground=$kind -eq 'ic_launcher_foreground'
            $size=[int]($(if($foreground){108}else{48})*$scale)
            $art=[int]($(if($foreground){48}else{32})*$scale)
            $bitmap=[System.Drawing.Bitmap]::new($size,$size)
            $graphics=[System.Drawing.Graphics]::FromImage($bitmap)
            try {
                $graphics.Clear($(if($foreground){[System.Drawing.Color]::Transparent}else{[System.Drawing.Color]::FromArgb(7,10,15)}))
                $graphics.InterpolationMode=[System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $graphics.PixelOffsetMode=[System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                if($kind -eq 'ic_launcher_round') {
                    $graphics.Clear([System.Drawing.Color]::Transparent)
                    $path=[System.Drawing.Drawing2D.GraphicsPath]::new()
                    $path.AddEllipse(0,0,$size,$size); $graphics.SetClip($path)
                    $brush=[System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb(7,10,15))
                    $graphics.FillRectangle($brush,0,0,$size,$size); $brush.Dispose(); $path.Dispose()
                }
                $ratio=[Math]::Min($art/$source.Width,$art/$source.Height)
                $width=[int]($source.Width*$ratio); $height=[int]($source.Height*$ratio)
                $rect=[System.Drawing.Rectangle]::new([int](($size-$width)/2),[int](($size-$height)/2),$width,$height)
                $graphics.DrawImage($source,$rect)
                $bitmap.Save((Join-Path $folder "$kind.png"),[System.Drawing.Imaging.ImageFormat]::Png)
            } finally { $graphics.Dispose(); $bitmap.Dispose() }
        }
    }
} finally { $source.Dispose() }
