# Focus the chat input box (biggest window of the target process, bottom-most Edit element).
# ASCII only - no encoding issues. Exit 1 if not found.
param([string]$ProcName = "ZCode")
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$proc = Get-Process -Name $ProcName -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -eq $ProcName } | Select-Object -First 1
if ($null -eq $proc) { $proc = Get-Process -Name $ProcName -ErrorAction SilentlyContinue | Select-Object -First 1 }
if ($null -eq $proc) { exit 1 }
$root = [System.Windows.Automation.AutomationElement]::RootElement
$procCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)
$wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $procCond)
$main = $null
for ($w = 0; $w -lt $wins.Count; $w++) {
    if ($wins.Item($w).Current.BoundingRectangle.Width -gt 800) { $main = $wins.Item($w) }
}
if ($null -eq $main) {
    if ($wins.Count -gt 0) { $main = $wins.Item(0) } else { exit 1 }
}
$editCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Edit)
$edits = $main.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)
if ($edits.Count -eq 0) { exit 1 }
# prefer the lowest edit (chat input sits at the bottom)
$target = $null
$bestY = -1
for ($i = 0; $i -lt $edits.Count; $i++) {
    $y = $edits.Item($i).Current.BoundingRectangle.Y
    if ($y -gt $bestY) { $bestY = $y; $target = $edits.Item($i) }
}
$target.SetFocus()
exit 0
