param(
    [string]$BrandText = '薄闻录',
    [string]$MainText = '热点精选 · 科技简报',
    [string]$BasePath = (Join-Path $PSScriptRoot 'common-v8.png'),
    [string]$OutputPath = (Join-Path $PSScriptRoot 'common-v8-composited-v2.png')
)

Add-Type -AssemblyName System.Drawing
if (Test-Path -LiteralPath $OutputPath) { throw "Output already exists: $OutputPath" }
if ([string]::IsNullOrWhiteSpace($BrandText) -or [string]::IsNullOrWhiteSpace($MainText)) {
    throw 'Both text fields are required.'
}
$source = [System.Drawing.Image]::FromFile($BasePath)
$bitmap = [System.Drawing.Bitmap]::new($source)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$format = [System.Drawing.StringFormat]::new()
$format.Alignment = [System.Drawing.StringAlignment]::Center
$format.LineAlignment = [System.Drawing.StringAlignment]::Center
$format.FormatFlags = [System.Drawing.StringFormatFlags]::NoWrap

function Add-FittedText($Text, $Box, $MaximumSize, $Color, $Style = [System.Drawing.FontStyle]::Bold) {
    $font = $null
    $brush = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml($Color))
    try {
        for ($size = [int]$MaximumSize; $size -ge 12; $size--) {
            if ($null -ne $font) { $font.Dispose() }
            $font = [System.Drawing.Font]::new('Microsoft YaHei', [single]$size,
                $Style, [System.Drawing.GraphicsUnit]::Pixel)
            $measure = $graphics.MeasureString($Text, $font)
            if ($measure.Width -le $Box.Width -and $measure.Height -le $Box.Height) { break }
        }
        if ($size -lt 12) { throw 'Text does not fit the reserved area.' }
        $graphics.DrawString($Text, $font, $brush, $Box, $format)
        return $size
    } finally {
        if ($null -ne $font) { $font.Dispose() }
        $brush.Dispose()
    }
}

try {
    $w = $bitmap.Width
    $h = $bitmap.Height
    $brandBox = [System.Drawing.RectangleF]::new($w * 0.63, $h * 0.012, $w * 0.18, $h * 0.105)
    $mainBox = [System.Drawing.RectangleF]::new($w * 0.13, $h * 0.367, $w * 0.74, $h * 0.26)
    $brandSize = Add-FittedText $BrandText $brandBox ($h * 0.045) '#94A3E8' ([System.Drawing.FontStyle]::Regular)
    $mainSize = Add-FittedText $MainText $mainBox ($h * 0.166) '#0000BA'
    $bitmap.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
    [pscustomobject]@{
        path = $OutputPath
        width = $w
        height = $h
        brand = $BrandText
        main = $MainText
        brand_font_px = $brandSize
        brand_color = '#94A3E8'
        brand_style = 'Regular'
        main_font_px = $mainSize
    } | ConvertTo-Json -Compress
} finally {
    $format.Dispose()
    $graphics.Dispose()
    $bitmap.Dispose()
    $source.Dispose()
}
