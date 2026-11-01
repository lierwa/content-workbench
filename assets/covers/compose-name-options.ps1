Add-Type -AssemblyName System.Drawing
$basePath = Join-Path $PSScriptRoot 'common-v8.png'
$contentPath = Join-Path $PSScriptRoot 'common-v8-composited-v2.png'
$cases = @(
    @{ Id='A'; Label='贴边横排'; Size=40; Tracking=9; Weight='Bold'; Treatment='plain'; Color='#94A3E8' },
    @{ Id='B'; Label='小号宽字距'; Size=32; Tracking=14; Weight='Regular'; Treatment='plain'; Color='#94A3E8' },
    @{ Id='C'; Label='短线衔接'; Size=36; Tracking=7; Weight='Bold'; Treatment='rule'; Color='#94A3E8' },
    @{ Id='D'; Label='细框标签'; Size=32; Tracking=8; Weight='Bold'; Treatment='outline'; Color='#94A3E8' }
)
$sheetPath = Join-Path $PSScriptRoot 'name-options-comparison.png'
$manifestPath = Join-Path $PSScriptRoot 'name-options.json'
foreach ($path in (@($sheetPath, $manifestPath) + @($cases | ForEach-Object { Join-Path $PSScriptRoot "name-option-$($_.Id).png" }))) {
    if (Test-Path -LiteralPath $path) { throw "Output already exists: $path" }
}
$base = [System.Drawing.Bitmap]::new($basePath)
$content = [System.Drawing.Bitmap]::new($contentPath)
$header = $base.Clone([System.Drawing.Rectangle]::new(0,0,$base.Width,111),$base.PixelFormat)
$sheet = [System.Drawing.Bitmap]::new(1860,1330)
$sheetGraphics = [System.Drawing.Graphics]::FromImage($sheet)
$sheetGraphics.Clear([System.Drawing.ColorTranslator]::FromHtml('#F3F5F9'))
$sheetGraphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$sheetGraphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$labelFont = [System.Drawing.Font]::new('Microsoft YaHei',28,[System.Drawing.FontStyle]::Bold,[System.Drawing.GraphicsUnit]::Pixel)
$detailFont = [System.Drawing.Font]::new('Microsoft YaHei',20,[System.Drawing.FontStyle]::Regular,[System.Drawing.GraphicsUnit]::Pixel)
$labelBrush = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#202B40'))
$detailBrush = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#68758B'))
$format = [System.Drawing.StringFormat]::GenericTypographic.Clone()
$format.FormatFlags = [System.Drawing.StringFormatFlags]::NoWrap
$sheetGraphics.DrawString('薄闻录 · 右侧名字排版对比',$labelFont,$labelBrush,28,20)
$records = @()

try {
    for ($i=0; $i -lt $cases.Count; $i++) {
        $case = $cases[$i]
        $image = [System.Drawing.Bitmap]::new($content)
        $g = [System.Drawing.Graphics]::FromImage($image)
        $g.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
        $g.DrawImageUnscaled($header,0,0)
        $g.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceOver
        $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
        $font = [System.Drawing.Font]::new('Microsoft YaHei',[single]$case.Size,
            [System.Enum]::Parse([System.Drawing.FontStyle],$case.Weight),[System.Drawing.GraphicsUnit]::Pixel)
        $brush = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml($case.Color))
        $linePen = [System.Drawing.Pen]::new($brush.Color,2)
        $framePen = [System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml('#7088D9'),2)
        try {
            $characters = @('薄','闻','录')
            $widths = @($characters | ForEach-Object { $g.MeasureString($_,$font,1000,$format).Width })
            $height = $g.MeasureString('薄闻录',$font,1000,$format).Height
            $total = ($widths | Measure-Object -Sum).Sum + 2 * $case.Tracking
            $right = $image.Width * 0.77
            $centerY = 73
            $textRight = $right
            if ($case.Treatment -eq 'outline') { $textRight -= 18 }
            $x = $textRight - $total
            $y = $centerY - $height / 2
            $textLeft = $x
            for ($j=0; $j -lt $characters.Count; $j++) {
                $g.DrawString($characters[$j],$font,$brush,[single]$x,[single]$y,$format)
                $x += $widths[$j] + $case.Tracking
            }
            if ($case.Treatment -eq 'rule') {
                $g.DrawLine($linePen,[single]($textLeft-64),[single]$centerY,[single]($textLeft-22),[single]$centerY)
            }
            if ($case.Treatment -eq 'outline') {
                $g.DrawRectangle($framePen,[single]($textLeft-18),[single]($y-5),[single]($total+36),[single]($height+10))
            }
            $file = Join-Path $PSScriptRoot "name-option-$($case.Id).png"
            $image.Save($file,[System.Drawing.Imaging.ImageFormat]::Png)
            $panelX = 28 + ($i % 2) * 930
            $panelY = 82 + [Math]::Floor($i / 2) * 615
            $sheetGraphics.DrawString("$($case.Id)  $($case.Label)",$labelFont,$labelBrush,[single]$panelX,[single]$panelY)
            $sheetGraphics.DrawImage($image,[System.Drawing.Rectangle]::new($panelX,($panelY+48),874,397))
            $sheetGraphics.DrawString('名字局部',$detailFont,$detailBrush,[single]$panelX,[single]($panelY+466))
            $sheetGraphics.DrawImage($image,[System.Drawing.Rectangle]::new(($panelX+130),($panelY+463),680,123),
                [System.Drawing.Rectangle]::new(970,20,580,105),[System.Drawing.GraphicsUnit]::Pixel)
            $records += [pscustomobject]@{
                id=$case.Id; label=$case.Label; file=[System.IO.Path]::GetFileName($file)
                font_size=$case.Size; tracking=$case.Tracking; weight=$case.Weight
                treatment=$case.Treatment; color=$case.Color; right_ratio=0.77; center_y=73
            }
        } finally {
            $framePen.Dispose(); $linePen.Dispose(); $brush.Dispose(); $font.Dispose(); $g.Dispose(); $image.Dispose()
        }
    }
    $sheet.Save($sheetPath,[System.Drawing.Imaging.ImageFormat]::Png)
    $records | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    [pscustomobject]@{comparison=$sheetPath;variants=$records} | ConvertTo-Json -Depth 4 -Compress
} finally {
    $format.Dispose(); $detailBrush.Dispose(); $labelBrush.Dispose(); $detailFont.Dispose(); $labelFont.Dispose()
    $sheetGraphics.Dispose(); $sheet.Dispose(); $header.Dispose(); $content.Dispose(); $base.Dispose()
}
