# Navigate ZCode sidebar to a conversation by title (UIA InvokePattern), verify the switch by
# reading the top header bar Text (shows the ACTIVE conversation title verbatim), then SetFocus
# the bottom-most Edit (chat input). ASCII source only; all non-ASCII payloads arrive as
# UTF-16 code-unit lists (decimal, comma separated).
#
# Exit codes:
#   0 OK: header verified (or already active) AND input Edit focused
#   1 no process / no main window
#   2 no candidate row, or ambiguous (even after project-header disambiguation)
#   3 invoked but header did not become the target title within ~3s
#   4 no task rows visible (accessibility tree not ready yet) - retriable
#   5 navigation verified but no Edit found (caller should run plain focus script)
#   6 input box has a non-empty draft (checked only with -DraftCheck); message NOT typed
param(
  [string]$ProcName = 'ZCode',
  [string]$TitleCP = '',
  [string]$ProjectCP = '',
  [string]$ItemClassRegex = 'task-item',
  [string]$GroupClassRegex = 'space-y-2',
  [string]$HeaderClassRegex = 'min-w-12',
  [string]$Log = '',
  [switch]$DryRun,
  [switch]$DraftCheck
)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function FromCP([string]$s) {
  if ([string]::IsNullOrEmpty($s)) { return '' }
  $t = ''
  foreach ($u in $s.Split(',')) {
    $u = $u.Trim()
    if ($u -ne '') { $t += [char][Convert]::ToUInt32($u) }
  }
  return $t
}
function Norm([string]$s) {
  if ($null -eq $s) { return '' }
  return (($s -replace '\s+', ' ').Trim()).ToLowerInvariant()
}
function LogLine([string]$m) {
  if ($Log -eq '') { return }
  try {
    $pre = ''
    if (-not (Test-Path $Log)) { $pre = [char]0xFEFF }
    [System.IO.File]::AppendAllText($Log, $pre + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $m + [Environment]::NewLine, [Text.Encoding]::UTF8)
  } catch { }
}

$title = Norm (FromCP $TitleCP)
$project = Norm (FromCP $ProjectCP)
if ($title -eq '') { LogLine 'BADARG empty TitleCP'; exit 10 }

$proc = Get-Process -Name $ProcName -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq $ProcName } | Select-Object -First 1
if ($null -eq $proc) { $proc = Get-Process -Name $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1 }
if ($null -eq $proc) { LogLine 'NOPROC'; exit 1 }
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
$main = $null
for ($w = 0; $w -lt $wins.Count; $w++) {
  if ($wins.Item($w).Current.BoundingRectangle.Width -gt 800) { $main = $wins.Item($w) }
}
if ($null -eq $main) { LogLine 'NOWIN'; exit 1 }

function Collect {
  $all = $main.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  $rows = @(); $heads = @()
  for ($i = 0; $i -lt $all.Count; $i++) {
    $e = $all.Item($i)
    if ($e.Current.ControlType.ProgrammaticName -ne 'ControlType.ListItem') { continue }
    $cls = $e.Current.ClassName
    $r = $e.Current.BoundingRectangle
    if ($cls -match $ItemClassRegex) {
      $rows += ,@{ El = $e; Name = (Norm $e.Current.Name); Rect = $r }
    } elseif ($cls -match $GroupClassRegex) {
      $heads += ,@{ Name = (Norm $e.Current.Name); Rect = $r }
    }
  }
  return @{ All = $all; Rows = $rows; Heads = $heads }
}
function HeaderOf($row, $heads) {
  foreach ($h in $heads) {
    if ($h.Rect.Top -le $row.Rect.Top -and $h.Rect.Bottom -ge $row.Rect.Bottom -and
        ($h.Rect.Bottom - $h.Rect.Top) -gt ($row.Rect.Bottom - $row.Rect.Top)) { return $h.Name }
  }
  return ''
}
function Pick($data) {
  $cands = @()
  foreach ($row in $data.Rows) {
    if ($row.Name.Contains($title)) { $cands += ,$row }
  }
  if ($cands.Count -eq 0) { return @{ List = @(); Why = 'no-candidate' } }
  if ($cands.Count -gt 1 -and $project -ne '') {
    $c2 = @()
    foreach ($row in $cands) {
      $h = HeaderOf $row $data.Heads
      if ($h.Contains($project)) { $c2 += ,$row }
    }
    if ($c2.Count -ge 1) { $cands = $c2 }
  }
  if ($cands.Count -gt 1) { return @{ List = $cands; Why = 'ambiguous' } }
  return @{ List = $cands; Why = 'ok' }
}
# Active conversation title as shown in the top header bar (verbatim, no time suffix).
# Thresholds are WINDOW-RELATIVE (measured offsets: title at +282/+14 from window origin,
# sidebar items end by +241) so the window can sit anywhere on any monitor.
function HeaderTitle {
  $wb = $main.Current.BoundingRectangle
  $yMax = $wb.Y + 150
  $xMin = $wb.X + 250
  $tc = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Text)
  $texts = $main.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tc)
  for ($i = 0; $i -lt $texts.Count; $i++) {
    $e = $texts.Item($i)
    $r = $e.Current.BoundingRectangle
    if ($r.Y -lt $yMax -and $r.X -gt $xMin -and $e.Current.ClassName -match $HeaderClassRegex) {
      $n = $e.Current.Name
      if (-not [string]::IsNullOrWhiteSpace($n)) { return (Norm $n) }
    }
  }
  return ''
}
$script:FocusedEdit = $null
function FocusBottomEdit {
  $editCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Edit)
  $edits = $main.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)
  if ($edits.Count -eq 0) { return $false }
  $target = $null; $bestY = -1
  for ($i = 0; $i -lt $edits.Count; $i++) {
    $y = $edits.Item($i).Current.BoundingRectangle.Y
    if ($y -gt $bestY) { $bestY = $y; $target = $edits.Item($i) }
  }
  try { $target.SetFocus(); $script:FocusedEdit = $target; return $true } catch { return $false }
}
# True when the input box carries no draft. Measured empty state is a single newline
# (Value/TextPattern both readable on this React contenteditable), hence Trim().
# If the read patterns are unavailable we degrade to "empty" (never block sending).
function DraftLooksEmpty {
  if ($null -eq $script:FocusedEdit) { return $true }
  $v = ''
  try {
    $vp = [System.Windows.Automation.ValuePattern]$script:FocusedEdit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
    $v = $vp.Current.Value
  } catch { return $true }
  $t = ''
  try {
    $tp = [System.Windows.Automation.TextPattern]$script:FocusedEdit.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern)
    $t = $tp.DocumentRange.GetText(200)
  } catch { }
  return ((($v + ' ' + $t).Trim()).Length -eq 0)
}

$data = Collect
if ($data.Rows.Count -eq 0) { LogLine 'NO-ROWS tree not ready'; exit 4 }
$pick = Pick $data
LogLine ("cands=" + $pick.List.Count + " why=" + $pick.Why + " title_len=" + $title.Length)
if ($pick.Why -ne 'ok') { exit 2 }
$row = $pick.List[0]

$pre = HeaderTitle
if ($DryRun) {
  LogLine ("DRY ok (1 candidate) headerMatchPre=" + ($pre -eq $title))
  exit 0
}

if ($pre -eq $title) {
  LogLine 'ALREADY-ACTIVE (header equals title)'
  if (-not (FocusBottomEdit)) { exit 5 }
  if ($DraftCheck -and -not (DraftLooksEmpty)) { LogLine 'DRAFT-NONEMPTY abort (already-active path)'; exit 6 }
  exit 0
}

try {
  $p = [System.Windows.Automation.InvokePattern]$row.El.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
  $p.Invoke()
} catch {
  LogLine ('INVOKE-FAIL ' + $_.Exception.Message)
  exit 3
}

$verified = $false
for ($k = 0; $k -lt 10; $k++) {
  Start-Sleep -Milliseconds 300
  if ((HeaderTitle) -eq $title) { $verified = $true; break }
}
if (-not $verified) {
  LogLine ('VERIFY-FAIL header now: ' + (HeaderTitle))
  exit 3
}
LogLine 'HEADER-VERIFIED'
if (-not (FocusBottomEdit)) { exit 5 }
if ($DraftCheck -and -not (DraftLooksEmpty)) { LogLine 'DRAFT-NONEMPTY abort'; exit 6 }
LogLine 'NAV-OK input focused'
exit 0
