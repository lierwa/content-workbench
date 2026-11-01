Add-Type -AssemblyName System.Drawing
$basePath = Join-Path $PSScriptRoot 'common-v8.png'
$contentPath = Join-Path $PSScriptRoot 'common-v8-composited-v2.png'
$subtlePath = Join-Path $PSScriptRoot 'common-v8-subtle-brand.png'
$cleanPath = Join-Path $PSScriptRoot 'common-v8-no-brand.png'
$boardPath = Join-Path $PSScriptRoot 'subtle-or-no-brand-comparison.png'
foreach ($path in @($subtlePath, $cleanPath, $boardPath)) {
    if (Test-Path -LiteralPath $path) { throw "Output already exists: $path" }
}
$base = [System.Drawing.Bitmap]::new($basePath)
$content = [System.Drawing.Bitmap]::new($contentPath)
$header = $base.Clone([System.Drawing.Rectangle]::new(0,0,$base.Width,111),$base.PixelFormat)
$clean = [System.Drawing.Bitmap]::new($content)
$g = [System.Drawing.Graphics]::FromImage($clean)
try {
    $g.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
    $g.DrawImageUnscaled($header,0,0)
} finally { $g.Dispose() }
$subtle = [System.Drawing.Bitmap]::new($clean)
$g = [System.Drawing.Graphics]::FromImage($subtle)
$format = [System.Drawing.StringFormat]::GenericTypographic.Clone()
$format.FormatFlags = [System.Drawing.StringFormatFlags]::NoWrap
$font = [System.Drawing.Font]::new('Microsoft YaHei',34,[System.Drawing.FontStyle]::Italic,[System.Drawing.GraphicsUnit]::Pixel)
$brush = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#3961E9'))
$pen = [System.Drawing.Pen]::new($brush.Color,2)
try {
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $characters = @('薄','闻','录')
    $widths = @($characters | ForEach-Object { $g.MeasureString($_,$font,1000,$format).Width })
    $height = $g.MeasureString('薄闻录',$font,1000,$format).Height
    $width = ($widths | Measure-Object -Sum).Sum + 10
    $x = 1432 - $width
    $y = 70 - $height / 2
    $g.DrawLine($pen,[single]($x-34),83,[single]($x-20),57)
    for ($i=0; $i -lt $characters.Count; $i++) {
        $g.DrawString($characters[$i],$font,$brush,[single]$x,[single]$y,$format)
        $x += $widths[$i] + 5
    }
    $subtle.Save($subtlePath,[System.Drawing.Imaging.ImageFormat]::Png)
    $clean.Save($cleanPath,[System.Drawing.Imaging.ImageFormat]::Png)
} finally {
    $pen.Dispose(); $brush.Dispose(); $font.Dispose(); $format.Dispose(); $g.Dispose()
}
$board = [System.Drawing.Bitmap]::new(1080,1114)
$g = [System.Drawing.Graphics]::FromImage($board)
$labelFont = [System.Drawing.Font]::new('Microsoft YaHei',24,[System.Drawing.FontStyle]::Regular,[System.Drawing.GraphicsUnit]::Pixel)
$labelBrush = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml('#263247'))
try {
    $g.Clear([System.Drawing.ColorTranslator]::FromHtml('#F3F5F9'))
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.DrawString('A  弱化名字 · 斜体 / 斜杠',$labelFont,$labelBrush,28,22)
    $g.DrawImage($subtle,[System.Drawing.Rectangle]::new(28,67,1024,465))
    $g.DrawString('B  去掉名字',$labelFont,$labelBrush,28,579)
    $g.DrawImage($clean,[System.Drawing.Rectangle]::new(28,624,1024,465))
    $board.Save($boardPath,[System.Drawing.Imaging.ImageFormat]::Png)
    Write-Output $boardPath
} finally {
    $labelBrush.Dispose(); $labelFont.Dispose(); $g.Dispose(); $board.Dispose()
    $subtle.Dispose(); $clean.Dispose(); $header.Dispose(); $content.Dispose(); $base.Dispose()
}
