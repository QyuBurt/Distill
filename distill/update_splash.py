"""The install window of a self-update, as a PowerShell script (Windows PowerShell 5.1 ships with every Windows 10/11).

Distill has quit by the time the new files go in, so the window cannot be Distill's own. This script draws the same picture
as ui/update_screen.py (just the logo, floating: grey, filling with its colours as the update goes in; no frame, no text) at
the same place on the screen, waits for Distill to exit, extracts the new version, and exits 0. Anything unexpected makes it
exit 1, and the batch file that started it then does the swap the plain way (see updater._script), so a problem here can
never leave an install half done.

The window is a layered window whose picture is pushed with UpdateLayeredWindow, which is what gives the logo its soft edges
over whatever is behind it (a colour key would leave a hard, fringed outline).

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
    Add-Type -ReferencedAssemblies 'System.Drawing' -TypeDefinition @'
using System;
using System.Drawing;
using System.Runtime.InteropServices;
namespace Distill {
    public static class Win {
        [StructLayout(LayoutKind.Sequential)] public struct POINT { public int x; public int y; }
        [StructLayout(LayoutKind.Sequential)] public struct SIZE { public int cx; public int cy; }
        [StructLayout(LayoutKind.Sequential, Pack = 1)] public struct BLEND { public byte Op; public byte Flags; public byte Alpha; public byte Format; }
        [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
        [DllImport("user32.dll", EntryPoint = "GetWindowLongW")] static extern int GetWindowLong(IntPtr h, int i);
        [DllImport("user32.dll", EntryPoint = "SetWindowLongW")] static extern int SetWindowLong(IntPtr h, int i, int v);
        [DllImport("user32.dll")] static extern bool UpdateLayeredWindow(IntPtr hwnd, IntPtr hdcDst, ref POINT pptDst, ref SIZE psize, IntPtr hdcSrc, ref POINT pptSrc, int crKey, ref BLEND pblend, int dwFlags);
        [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr h);
        [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr h, IntPtr dc);
        [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleDC(IntPtr dc);
        [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr dc);
        [DllImport("gdi32.dll")] static extern IntPtr SelectObject(IntPtr dc, IntPtr o);
        [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr o);
        public static void MakeLayered(IntPtr hwnd) {
            SetWindowLong(hwnd, -20, GetWindowLong(hwnd, -20) | 0x80000);
        }
        // Show `bmp` (32-bit with alpha) as the whole window, with its top left at x, y on the screen.
        public static void Push(IntPtr hwnd, Bitmap bmp, int x, int y) {
            IntPtr screen = GetDC(IntPtr.Zero);
            IntPtr mem = CreateCompatibleDC(screen);
            IntPtr hbmp = IntPtr.Zero;
            IntPtr old = IntPtr.Zero;
            try {
                hbmp = bmp.GetHbitmap(Color.FromArgb(0));
                old = SelectObject(mem, hbmp);
                POINT dst = new POINT();
                dst.x = x;
                dst.y = y;
                SIZE size = new SIZE();
                size.cx = bmp.Width;
                size.cy = bmp.Height;
                POINT src = new POINT();
                BLEND blend = new BLEND();
                blend.Op = 0;
                blend.Flags = 0;
                blend.Alpha = 255;
                blend.Format = 1;
                UpdateLayeredWindow(hwnd, screen, ref dst, ref size, mem, ref src, 0, ref blend, 2);
            } finally {
                if (old != IntPtr.Zero) { SelectObject(mem, old); }
                if (hbmp != IntPtr.Zero) { DeleteObject(hbmp); }
                DeleteDC(mem);
                ReleaseDC(IntPtr.Zero, screen);
            }
        }
    }
}
'@
    try {
        # Without this Windows scales the window itself and it lands in the wrong place on a zoomed display.
        [void][Distill.Win]::SetProcessDPIAware()
    } catch { }

    $s = Get-Content -LiteralPath $Spec -Raw -Encoding UTF8 | ConvertFrom-Json
    Log ('start, kind=' + $s.kind)

    # ---- what to draw, exactly where Distill's own update screen drew it: the window is the logo ----
    $script:W = [int]$s.w
    $script:H = [int]$s.h

    # The logo in colour, and the same picture in grey (the same weights the app uses; the transparent corners are kept).
    $src = [System.Drawing.Image]::FromFile([string]$s.logo)
    $script:colour = [System.Drawing.Bitmap]::new($script:W, $script:H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($script:colour)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.DrawImage($src, 0, 0, $script:W, $script:H)
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
    $script:grey = [System.Drawing.Bitmap]::new($script:W, $script:H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($script:grey)
    $g.DrawImage($script:colour, [System.Drawing.Rectangle]::new(0, 0, $script:W, $script:H), 0, 0, $script:W, $script:H, [System.Drawing.GraphicsUnit]::Pixel, $attr)
    $g.Dispose()

    # One frame is drawn here and then handed to the window whole.
    $script:frame = [System.Drawing.Bitmap]::new($script:W, $script:H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $script:gfx = [System.Drawing.Graphics]::FromImage($script:frame)
    $script:gfx.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias

    # ---- the window ----
    $script:wx = [int]$s.x
    $script:wy = [int]$s.y
    $script:start = [double]$s.start
    $script:shown = $script:start
    $script:target = $script:start
    $script:phase = 0.0
    $script:sw = [System.Diagnostics.Stopwatch]::StartNew()
    $script:lastFrame = -100

    $form = New-Object System.Windows.Forms.Form
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
    $form.ShowInTaskbar = $false
    $form.Text = 'Updating Distill'
    $form.TopMost = $true
    $form.Bounds = [System.Drawing.Rectangle]::new($script:wx, $script:wy, $script:W, $script:H)
    $hwnd = $form.Handle   # creates the window (still hidden), so it can be made see-through before anything is shown
    [Distill.Win]::MakeLayered($hwnd)

    # Draw the logo: grey, with the colour from the bottom up to the fill line and a small ripple on that edge (as in the app).
    function Render {
        $g = $script:gfx
        $g.Clear([System.Drawing.Color]::Transparent)
        $lw = $script:W; $lh = $script:H
        $f = $script:shown
        $g.DrawImage($script:grey, 0, 0, $lw, $lh)
        if ($f -ge 0.999) {
            $g.DrawImage($script:colour, 0, 0, $lw, $lh)
        } elseif ($f -gt 0) {
            $y = $lh * (1.0 - $f)
            $amp = 4.0 * [math]::Sqrt([math]::Sin([math]::PI * $f)) * ($lw / 240.0)
            $pts = New-Object 'System.Collections.Generic.List[System.Drawing.PointF]'
            $pts.Add([System.Drawing.PointF]::new([single]0, [single]$lh))
            for ($x = 0; $x -le $lw; $x += 4) {
                $wy = $y + $amp * [math]::Sin(2.0 * [math]::PI * $x / ($lw * 0.6) + $script:phase)
                $pts.Add([System.Drawing.PointF]::new([single]$x, [single]$wy))
            }
            $pts.Add([System.Drawing.PointF]::new([single]$lw, [single]$lh))
            $path = [System.Drawing.Drawing2D.GraphicsPath]::new()
            $path.AddPolygon($pts.ToArray())
            $g.SetClip($path)
            $g.DrawImage($script:colour, 0, 0, $lw, $lh)
            $g.ResetClip()
            $path.Dispose()
        }
        [Distill.Win]::Push($hwnd, $script:frame, $script:wx, $script:wy)
    }

    # Redraw about 30 times a second, easing the shown fill towards the real one, and let the window process its messages.
    function Pump([bool]$force = $false) {
        if (-not $force -and ($script:sw.ElapsedMilliseconds - $script:lastFrame) -lt 33) { return }
        $script:lastFrame = $script:sw.ElapsedMilliseconds
        $d = $script:target - $script:shown
        if ([math]::Abs($d) -lt 0.001) { $script:shown = $script:target } else { $script:shown += $d * 0.2 }
        $script:phase += 0.12
        Render
        [System.Windows.Forms.Application]::DoEvents()
    }

    Render             # the first picture goes in while the window is still hidden, so no blank box ever shows
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
