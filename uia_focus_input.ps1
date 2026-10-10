# Focus the chat input box (biggest window of the target process).
# Identity first, position second: prefer the bottom-most visible+focusable Edit whose ClassName
# matches the composer signature (-ComposerClass); only fall back to any visible+focusable Edit to
# report exit 7 (NOT the composer) so callers never type into a plugin panel field.
# ASCII only - no encoding issues.
# Exit 0 = focused. 1 = no process / no main window. 3 = no visible+focusable Edit in the tree
# (composer not mounted yet: session switch in flight, or composer hidden behind an overlay).
# 4 = SetFocus threw. 7 = an Edit was focused but it is NOT the chat composer (class mismatch,
# e.g. a plugin panel field) - callers must NOT type into it.
# Callers must NOT type unless the exit code is 0 - see FOCUS_VERDICTS in the .py.
param(
  [string]$ProcName = "ZCode",
  [string]$ComposerClass = "min-h-10"
)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$proc = Get-Process -Name $ProcName -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq $ProcName } | Select-Object -First 1
if ($null -eq $proc) { $proc = Get-Process -Name $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1 }
if ($null -eq $proc) { exit 1 }
$root = [System.Windows.Automation.AutomationElement]::RootElement
$procCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $procCond)
# Same window pick as uia_navigate_input.ps1: finite rects only, width > 800 preferred,
# largest area as the fallback (never the 250x64 alias mini-window just because it is first).
$main = $null; $bestArea = -1.0; $fallback = $null; $fallbackArea = -1.0
for ($w = 0; $w -lt $wins.Count; $w++) {
    $e = $wins.Item($w); $r = $e.Current.BoundingRectangle
    if ([double]::IsNaN($r.Width) -or [double]::IsInfinity($r.Width) -or
        [double]::IsNaN($r.Height) -or [double]::IsInfinity($r.Height)) { continue }
    $area = $r.Width * $r.Height
    if (($r.Width -gt 800) -and ($area -gt $bestArea)) { $bestArea = $area; $main = $e }
    if ($area -gt $fallbackArea) { $fallbackArea = $area; $fallback = $e }
}
if ($null -eq $main) { $main = $fallback }
if ($null -eq $main) { exit 1 }
$editCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Edit)
$edits = $main.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)
# Identity first, position second (same two-pass rule as uia_navigate_input.ps1):
# pass 1 = visible+focusable Edit whose class matches the composer signature -> exit 0;
# pass 2 = fallback visible+focusable bottom-most -> focused but reported as exit 7 so the
# caller refuses to type instead of dumping keys into a plugin panel field.
$target = $null
$bestY = $null
$isComposer = $false
$identityBroken = $false
$hasIdentityRule = ($ComposerClass -ne '')
try {
    foreach ($pass in 1, 2) {
        # No identity rule = no way to tell the composer apart -> never claim identity (fail closed).
        if ($pass -eq 1 -and (-not $hasIdentityRule)) { continue }
        for ($i = 0; $i -lt $edits.Count; $i++) {
            $e = $edits.Item($i)
            if ($e.Current.IsOffscreen -or (-not $e.Current.IsKeyboardFocusable)) { continue }
            if ($pass -eq 1 -and ($e.Current.ClassName -notmatch $ComposerClass)) { continue }
            $y = $e.Current.BoundingRectangle.Y
            # $null sentinel, not -1: a window on a monitor left of / above the primary has
            # legitimately negative coordinates and must not be filtered out.
            if ($null -eq $bestY -or $y -gt $bestY) { $bestY = $y; $target = $e; $isComposer = ($pass -eq 1) }
        }
        if ($null -ne $target) { break }
    }
} catch {
    # UIA property read / stale element / invalid regex -> "not identified", never type
    $target = $null
    $isComposer = $false
    $identityBroken = $true
}
if ($null -eq $target) {
    if ($identityBroken) { exit 7 }
    exit 3
}
try { $target.SetFocus() } catch { exit 4 }
if (-not $isComposer) { exit 7 }
exit 0
