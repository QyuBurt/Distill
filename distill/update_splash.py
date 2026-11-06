"""The install window of a self-update, as a PowerShell script (Windows PowerShell 5.1 ships with every Windows 10/11).

Distill has quit by the time the new files go in, so the window cannot be Distill's own. This script draws the same picture
as ui/update_screen.py (the grey logo filling with its colours, the percentage, one line of text) at the same place on the
screen, waits for Distill to exit, extracts the new version with a running percentage, and exits 0. Anything unexpected makes
it exit 1, and the batch file that started it then does the swap the plain way (see updater._script), so a problem here can
never leave an install half done.

The text must stay pure ASCII: PowerShell 5.1 reads a script file without a byte-order mark as ANSI."""
from __future__ import annotations

SCRIPT = r'''param([Parameter(Mandatory = $true)][string]$Spec)
$ErrorActionPreference = 'Stop'
$log = Join-Path $env:TEMP 'distill-update.log'
function Log([string]$m) {
    try { Add-Content -LiteralPath $log -Value ((Get-Date -Format 'HH:mm:ss') + ' splash: ' + $m) } catch { }
}
$form = $null
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    try {
        # Without this Windows scales the window itself and it lands in the wrong place on a zoomed display.
        Add-Type -Namespace Distill -Name Dpi -MemberDefinition '[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
        [void][Distill.Dpi]::SetProcessDPIAware()
    } catch { }

    $s = Get-Content -LiteralPath $Spec -Raw -Encoding UTF8 | ConvertFrom-Json
    Log ('start, kind=' + $s.kind)

    function Col([string]$hex) { return [System.Drawing.ColorTranslator]::FromHtml($hex) }
    $script:keep = @()
    function Fam([string]$path, [string]$fallback) {
        try {
            if ($path -and (Test-Path -LiteralPath $path)) {
                $c = New-Object System.Drawing.Text.PrivateFontCollection
                $c.AddFontFile($path)
                $script:keep += $c
                return $c.Families[0]
            }
        } catch { }
        return [System.Drawing.FontFamily]::new($fallback)
    }

    # ---- what to draw, exactly where Distill's own update screen drew it ----
    $script:W = [int]$s.w
    $script:H = [int]$s.h
    $script:lx = [int]$s.logo_rect.x
    $script:ly = [int]$s.logo_rect.y
    $script:lw = [int]$s.logo_rect.w
    $script:lh = [int]$s.logo_rect.h
    $script:pctRect = [System.Drawing.RectangleF]::new([single]$s.pct_rect.x, [single]$s.pct_rect.y, [single]$s.pct_rect.w, [single]$s.pct_rect.h)
    $script:staRect = [System.Drawing.RectangleF]::new([single]$s.status_rect.x, [single]$s.status_rect.y, [single]$s.status_rect.w, [single]$s.status_rect.h)
    $script:bg = Col $s.bg
    $script:penBorder = [System.Drawing.Pen]::new((Col $s.border))
    $script:brTitle = [System.Drawing.SolidBrush]::new((Col $s.title))
    $script:brMuted = [System.Drawing.SolidBrush]::new((Col $s.fg3))
    $script:fmt = [System.Drawing.StringFormat]::new()
    $script:fmt.Alignment = [System.Drawing.StringAlignment]::Center
    $script:fmt.LineAlignment = [System.Drawing.StringAlignment]::Center
    $script:fmt.FormatFlags = [System.Drawing.StringFormatFlags]::NoWrap
    $script:pctFont = [System.Drawing.Font]::new((Fam $s.display_font 'Segoe UI'), [single]$s.pct_px, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
    $script:staFont = [System.Drawing.Font]::new((Fam $s.text_font 'Segoe UI'), [single]$s.status_px, [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Pixel)
    $script:statusText = [string]$s.status_text

    # The logo in colour, and the same picture in grey (the same weights the app uses; the transparent corners are kept).
    $src = [System.Drawing.Image]::FromFile([string]$s.logo)
    $script:colour = [System.Drawing.Bitmap]::new($script:lw, $script:lh, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($script:colour)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.DrawImage($src, 0, 0, $script:lw, $script:lh)
    $g.Dispose()
    $src.Dispose()
    $m = [single[][]]@(
        @(0.34375, 0.34375, 0.34375, 0, 0),
        @(0.5, 0.5, 0.5, 0, 0),
        @(0.15625, 0.15625, 0.15625, 0, 0),
        @(0, 0, 0, 1, 0),
        @(0, 0, 0, 0, 1))
    $attr = [System.Drawing.Imaging.ImageAttributes]::new()
    $attr.SetColorMatrix([System.Drawing.Imaging.ColorMatrix]::new($m))
    $script:grey = [System.Drawing.Bitmap]::new($script:lw, $script:lh, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($script:grey)
    $g.DrawImage($script:colour, [System.Drawing.Rectangle]::new(0, 0, $script:lw, $script:lh), 0, 0, $script:lw, $script:lh, [System.Drawing.GraphicsUnit]::Pixel, $attr)
    $g.Dispose()

    # ---- the window ----
    $script:start = [double]$s.start
    $script:shown = $script:start
    $script:target = $script:start
    $script:phase = 0.0
    $script:sw = [System.Diagnostics.Stopwatch]::StartNew()
    $script:lastFrame = -100

    $form = New-Object System.Windows.Forms.Form
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
    $form.Text = 'Updating Distill'
    $form.BackColor = $script:bg
    $form.TopMost = $true
    $form.Bounds = [System.Drawing.Rectangle]::new([int]$s.x, [int]$s.y, $script:W, $script:H)
    [void]$form.GetType().GetProperty('DoubleBuffered', [System.Reflection.BindingFlags]'Instance,NonPublic').SetValue($form, $true, $null)

    $form.Add_Paint({
        param($sender, $e)
        $g = $e.Graphics
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::ClearTypeGridFit
        $g.DrawRectangle($script:penBorder, 0, 0, $script:W - 1, $script:H - 1)
        $lx = $script:lx; $ly = $script:ly; $lw = $script:lw; $lh = $script:lh
        $f = $script:shown
        $g.DrawImage($script:grey, $lx, $ly, $lw, $lh)
        if ($f -ge 0.999) {
            $g.DrawImage($script:colour, $lx, $ly, $lw, $lh)
        } elseif ($f -gt 0) {
            # the colour, from the bottom up to the fill line, with a small ripple on the edge (as in the app)
            $y = $ly + $lh * (1.0 - $f)
            $amp = 4.0 * [math]::Sqrt([math]::Sin([math]::PI * $f)) * ($lw / 240.0)
            $pts = New-Object 'System.Collections.Generic.List[System.Drawing.PointF]'
            $pts.Add([System.Drawing.PointF]::new([single]$lx, [single]($ly + $lh)))
            for ($x = 0; $x -le $lw; $x += 4) {
                $wy = $y + $amp * [math]::Sin(2.0 * [math]::PI * $x / ($lw * 0.6) + $script:phase)
                $pts.Add([System.Drawing.PointF]::new([single]($lx + $x), [single]$wy))
            }
            $pts.Add([System.Drawing.PointF]::new([single]($lx + $lw), [single]($ly + $lh)))
            $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
            $path.AddPolygon($pts.ToArray())
            $g.SetClip($path)
            $g.DrawImage($script:colour, $lx, $ly, $lw, $lh)
            $g.ResetClip()
            $path.Dispose()
        }
        if ($f -ge 0.999) { $p = 100 } else { $p = [math]::Min(99, [math]::Floor($f * 100 + 0.000001)) }
        $g.DrawString(([string]$p + '%'), $script:pctFont, $script:brTitle, $script:pctRect, $script:fmt)
        $g.DrawString($script:statusText, $script:staFont, $script:brMuted, $script:staRect, $script:fmt)
    })

    # Redraw about 30 times a second, easing the shown fill towards the real one, and let the window process its messages.
    function Pump([bool]$force = $false) {
        if (-not $force -and ($script:sw.ElapsedMilliseconds - $script:lastFrame) -lt 33) { return }
        $script:lastFrame = $script:sw.ElapsedMilliseconds
        $d = $script:target - $script:shown
        if ([math]::Abs($d) -lt 0.001) { $script:shown = $script:target } else { $script:shown += $d * 0.2 }
        $script:phase += 0.12
        $form.Invalidate()
        [System.Windows.Forms.Application]::DoEvents()
    }

    $form.Show()
    Pump $true
    Set-Content -LiteralPath ([string]$s.ready) -Value 'ok'   # Distill waits for this before it quits, so the two windows overlap
    Log 'window shown'

    # ---- wait for Distill to exit (its exe cannot be opened for writing while it runs) ----
    $deadline = (Get-Date).AddSeconds(120)
    while ($true) {
        try {
            $fs = [System.IO.File]::Open([string]$s.exe, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            $fs.Close()
            break
        } catch { }
        if ((Get-Date) -gt $deadline) { throw 'gave up waiting for Distill to exit' }
        Start-Sleep -Milliseconds 50
        Pump
    }
    Log 'installing'

    # ---- install: the fill goes from where the download left it to 100% ----
    if ($s.kind -eq 'portable') {
        $zip = [System.IO.Compression.ZipFile]::OpenRead([string]$s.payload)
        try {
            $dest = [System.IO.Path]::GetFullPath([string]$s.app_dir)
            if (-not $dest.EndsWith('\')) { $dest = $dest + '\' }
            $total = [double]0
            foreach ($en in $zip.Entries) { $total += $en.Length }
            if ($total -le 0) { $total = 1 }
            $done = [double]0
            $buf = New-Object byte[] 262144
            foreach ($en in $zip.Entries) {
                $name = $en.FullName.Replace('/', '\')
                if (-not $name) { continue }
                $full = [System.IO.Path]::GetFullPath((Join-Path $dest $name))
                if (-not $full.StartsWith($dest, [System.StringComparison]::OrdinalIgnoreCase)) { throw ('unsafe path in the update: ' + $name) }
                if ($name.EndsWith('\')) { [void][System.IO.Directory]::CreateDirectory($full); continue }
                [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($full))
                $in = $en.Open()
                $out = [System.IO.File]::Create($full)
                try {
                    while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                        $out.Write($buf, 0, $n)
                        $done += $n
                        $script:target = $script:start + (1.0 - $script:start) * ($done / $total)
                        Pump
                    }
                } finally {
                    $out.Dispose()
                    $in.Dispose()
                }
            }
        } finally {
            $zip.Dispose()
        }
    } else {
        Move-Item -LiteralPath ([string]$s.payload) -Destination ([string]$s.exe) -Force
    }

    # ---- done: a moment of full colour, then hand back to the batch file, which starts Distill ----
    $script:target = 1.0
    $script:statusText = 'Starting Distill'
    $t = [System.Diagnostics.Stopwatch]::StartNew()
    while ($t.ElapsedMilliseconds -lt 700) { Start-Sleep -Milliseconds 20; Pump }
    $form.Close()
    Log 'done'
    exit 0
} catch {
    Log ($_.Exception.ToString())
    try { if ($form) { $form.Close() } } catch { }
    exit 1
}
'''
