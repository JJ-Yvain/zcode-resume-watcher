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
#   7 focused Edit is not the chat composer (class mismatch) - caller must NOT type
#   8 composer identified but SetFocus did not land (focus held by another control/app) - same veto:
#     UIA SetFocus can be silently ignored and the caller's WM_CHAR follows the REAL focus
#     (2026-10-11 misfire: resume text landing in a browser-panel URL / viewport field)
#   Notes: 3 also covers InvokePattern failure (not only "header did not change"), and 5 also
#   covers SetFocus failing on a found Edit. -DryRun exits 0 as soon as exactly one candidate
#   row is found: it never clicks and never verifies, so 0 there means "would try", not "verified".
#   On a refusal a one-line CANDIDATES dump (visible+focusable Edits with class/y/value length) is
#   appended to the log so "could not identify the input box" is explainable after the fact.
param(
  [string]$ProcName = 'ZCode',
  [string]$TitleCP = '',
  [string]$ProjectCP = '',
  [string]$ItemClassRegex = 'task-item',
  [string]$GroupClassRegex = 'space-y-2',
  [string]$HeaderClassRegex = 'min-w-12',
  [string]$ComposerClassRegex = 'min-h-10',
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
# Window pick: finite rects only (a minimized Chromium window reports an infinite rect),
# prefer width > 800 (skip the 250x64 alias mini-window), then fall back to the largest area so a
# user-narrowed window is not a hard failure - the header/focus verification still decides.
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
if ($null -eq $main) { LogLine 'NOWIN (no finite window rect; minimized?)'; exit 1 }

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
$script:FocusedIsComposer = $false
$script:ComposerRuleBroken = $false
$script:FocusLanded = $false
$script:FocusDetail = ''
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
function FocusBottomEdit {
  $editCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Edit)
  $edits = $main.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)
  if ($edits.Count -eq 0) { return $false }
  # Identity first, position second. Right after a session switch the tree holds extra Edits:
  # plugin-panel inputs (URL / viewport, often NON-EMPTY) and offscreen leftovers. A naive
  # "bottom-most visible Edit" can therefore pick a panel field, and the draft check then reads
  # THAT value and blocks the resume (measured 2026-10-10 10:13:26: panel URL read as "draft").
  # Pass 1: visible+focusable Edit whose class matches the composer signature -> that is the chat
  # input box, OK to focus AND to draft-check. Pass 2 (fallback): visible+focusable bottom-most,
  # focused but flagged as NOT the composer -> caller must not type / must not trust the draft read.
  $target = $null; $bestY = $null; $isComposer = $false
  $hasIdentityRule = ($ComposerClassRegex -ne '')
  try {
    foreach ($pass in 1, 2) {
      # No identity rule = no way to tell the composer apart -> never claim identity (fail closed)
      if ($pass -eq 1 -and (-not $hasIdentityRule)) { continue }
      for ($i = 0; $i -lt $edits.Count; $i++) {
        $e = $edits.Item($i)
        if ($e.Current.IsOffscreen -or (-not $e.Current.IsKeyboardFocusable)) { continue }
        if ($pass -eq 1 -and ($e.Current.ClassName -notmatch $ComposerClassRegex)) { continue }
        $y = $e.Current.BoundingRectangle.Y
        # $null sentinel, not -1: monitors left of / above the primary give negative coordinates
        if ($null -eq $bestY -or $y -gt $bestY) { $bestY = $y; $target = $e; $isComposer = ($pass -eq 1) }
      }
      if ($null -ne $target) { break }
    }
  } catch {
    # UIA property read / stale element / invalid regex -> "not identified", never type
    $target = $null
    $isComposer = $false
    $script:ComposerRuleBroken = $true
  }
  if ($null -eq $target) { DumpEdits 'no-edit' $edits; return $false }
  if (-not $isComposer) { DumpEdits 'not-composer' $edits }
  try {
    $target.SetFocus()
    $script:FocusedEdit = $target
    $script:FocusedIsComposer = $isComposer
  } catch { return $false }
  # Landing check (same rule as uia_focus_input.ps1): the caller's WM_CHAR follows the REAL focus,
  # so the picked control must really hold it - UIA SetFocus can be silently ignored, and then the
  # text lands wherever the focus actually is (browser-panel URL / viewport field, another app).
  $landed = $false
  $tgtId = ''
  try { $tgtId = ($target.Current.RuntimeId -join ',') } catch { $tgtId = '' }
  try {
    for ($k = 0; $k -lt 8; $k++) {
      $fe = [System.Windows.Automation.AutomationElement]::FocusedElement
      if ($null -ne $fe) {
        $feId = ''
        try { $feId = ($fe.Current.RuntimeId -join ',') } catch { $feId = '' }
        if ($tgtId -ne '' -and $feId -eq $tgtId) { $landed = $true; break }
        if ($ComposerClassRegex -ne '' -and ($fe.Current.ClassName -match $ComposerClassRegex)) { $landed = $true; break }
        $p = $fe
        for ($up = 0; $up -lt 6; $up++) {
          $p = $walker.GetParent($p)
          if ($null -eq $p) { break }
          $pid2 = ''
          try { $pid2 = ($p.Current.RuntimeId -join ',') } catch { $pid2 = '' }
          if ($tgtId -ne '' -and $pid2 -eq $tgtId) { $landed = $true; break }
        }
        if ($landed) { break }
      }
      Start-Sleep -Milliseconds 150
    }
  } catch { $landed = $false }
  $script:FocusLanded = $landed
  if (-not $landed) {
    $fcls = '(unreadable)'
    try { $fcls = [System.Windows.Automation.AutomationElement]::FocusedElement.Current.ClassName } catch { }
    $script:FocusDetail = ('focused_class=' + $fcls + ' target_class=' + $target.Current.ClassName)
  }
  return $true
}
# True when the input box carries no draft. Measured empty state is a single newline
# (Value/TextPattern both readable on this React contenteditable), hence Trim().
# If the read patterns are unavailable we degrade to "empty" (never block sending).
function NotComposerHint {
  # When no chat composer is found, look for an "answer/approval" shaped input: it means the
  # session is waiting for its HUMAN, not dead. Sending the resume phrase as an answer would
  # pollute the conversation, so this only adds a diagnostic hint - the verdict stays exit 7.
  $editCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Edit)
  $edits = $main.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)
  for ($i = 0; $i -lt $edits.Count; $i++) {
    $e = $edits.Item($i)
    if ($e.Current.IsOffscreen) { continue }
    if ($e.Current.ClassName -match 'min-h-16|resize-none') {
      return ' (looks like an ANSWER/approval box - this session may be waiting for YOU, not dead)'
    }
  }
  return ''
}
function DumpEdits([string]$why, $list) {
  # One line into navigate.log: which Edits were visible+focusable when identity failed, with the
  # value LENGTH only (never the value itself - a panel URL is user data we do not need here).
  $parts = @()
  $vis = 0
  for ($i = 0; $i -lt $list.Count; $i++) {
    $e = $list.Item($i)
    try { $off = $e.Current.IsOffscreen; $foc = $e.Current.IsKeyboardFocusable } catch { continue }
    if ((-not $off) -and $foc) { $vis++ }
  }
  for ($i = 0; $i -lt $list.Count; $i++) {
    if ($parts.Count -ge 6) { break }
    $e = $list.Item($i)
    try { $off = $e.Current.IsOffscreen; $foc = $e.Current.IsKeyboardFocusable } catch { continue }
    if ($off -or (-not $foc)) { continue }
    $r = $e.Current.BoundingRectangle
    $vlen = -1
    try {
      $vp = [System.Windows.Automation.ValuePattern]$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
      $vlen = $vp.Current.Value.Length
    } catch { }
    $parts += ('[y=' + [int]$r.Y + ' cls=' + $e.Current.ClassName + ' vlen=' + $vlen + ']')
  }
  if ($parts.Count -eq 0) { $parts += '(no visible+focusable Edit)' }
  LogLine ('CANDIDATES ' + $why + ' total=' + $list.Count + ' visibleFocusable=' + $vis +
           ' ' + ($parts -join ' '))
}
function DraftLooksEmpty {
  if ($null -eq $script:FocusedEdit) { return $true }
  $v = ''
  try {
    $vp = [System.Windows.Automation.ValuePattern]$script:FocusedEdit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
    $v = $vp.Current.Value
  } catch {
    # Read channel unavailable -> degrade to "empty" (never block sending) but SAY SO in the log,
    # so a UI change that kills the draft check is visible instead of silent (review 2026-10-10).
    LogLine 'DRAFT-READ-UNAVAILABLE ValuePattern (treated as empty, never blocks)'
    return $true
  }
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
  if (-not (FocusBottomEdit)) { if ($script:ComposerRuleBroken) { LogLine 'COMPOSER-RULE-BROKEN'; exit 7 } ; exit 5 }
  if (-not $script:FocusedIsComposer) { LogLine ('COMPOSER-NOT-IDENTIFIED' + (NotComposerHint)); exit 7 }
  if (-not $script:FocusLanded) { LogLine ('FOCUS-NOT-LANDED ' + $script:FocusDetail); exit 8 }
  if ($DraftCheck -and -not (DraftLooksEmpty)) { LogLine 'DRAFT-NONEMPTY abort (already-active path)'; exit 6 }
  LogLine ('FOCUS-VERIFIED cls=' + $script:FocusedEdit.Current.ClassName)
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
if (-not (FocusBottomEdit)) { if ($script:ComposerRuleBroken) { LogLine 'COMPOSER-RULE-BROKEN'; exit 7 } ; exit 5 }
if (-not $script:FocusedIsComposer) { LogLine 'COMPOSER-NOT-IDENTIFIED'; exit 7 }
if (-not $script:FocusLanded) { LogLine ('FOCUS-NOT-LANDED ' + $script:FocusDetail); exit 8 }
if ($DraftCheck -and -not (DraftLooksEmpty)) { LogLine 'DRAFT-NONEMPTY abort'; exit 6 }
LogLine ('NAV-OK input focused cls=' + $script:FocusedEdit.Current.ClassName)
exit 0
