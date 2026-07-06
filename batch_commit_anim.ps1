# Batch commit animation folders.
# Usage: powershell -ExecutionPolicy Bypass -File batch_commit_anim.ps1 -StartBatch 2 -BatchSize 200

param(
	[int]$StartBatch = 1,
	[int]$BatchSize = 200
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$animBase = Get-ChildItem "generated_files/plugins" -Directory | ForEach-Object {
	if (Test-Path (Join-Path $_.FullName "1\pet.png")) { $_.FullName }
} | Select-Object -First 1

if (-not $animBase) { throw "animation folder not found (expected */1/pet.png)" }

$animRel = $animBase.Substring((Resolve-Path ".").Path.Length + 1) -replace '\\', '/'
$allDirs = Get-ChildItem -LiteralPath $animBase -Directory | Sort-Object { try { [int]$_.Name } catch { [int]::MaxValue } }, { $_.Name }
$totalBatches = [math]::Ceiling($allDirs.Count / $BatchSize)

Write-Host "path=$animRel dirs=$($allDirs.Count) batches=$totalBatches start=$StartBatch"

for ($batchIndex = $StartBatch; $batchIndex -le $totalBatches; $batchIndex++) {
	$start = ($batchIndex - 1) * $BatchSize
	$end = [math]::Min($start + $BatchSize - 1, $allDirs.Count - 1)
	$batchDirs = $allDirs[$start..$end]
	$firstId = $batchDirs[0].Name
	$lastId = $batchDirs[-1].Name

	$toAdd = @()
	foreach ($d in $batchDirs) {
		$rel = "$animRel/$($d.Name)"
		$tracked = @(git ls-files "$rel/" 2>$null)
		if ($tracked.Count -eq 0) { $toAdd += $d }
	}
	if ($toAdd.Count -eq 0) {
		Write-Host "skip batch $batchIndex (tracked)"
		continue
	}

	Write-Host "batch $batchIndex/$totalBatches pets $firstId-$lastId count=$($toAdd.Count)"

	foreach ($d in $toAdd) {
		git add -- "$animRel/$($d.Name)/"
	}

	$commitPath = Join-Path $PSScriptRoot "commit_msg.txt"
	$body = "anim batch $batchIndex/$totalBatches pets $firstId-$lastId"
	[System.IO.File]::WriteAllText($commitPath, $body, [System.Text.UTF8Encoding]::new($false))
	git commit -F $commitPath
	Remove-Item $commitPath -Force

	Write-Host "pushing..."
	git -c http.proxy= -c https.proxy= push origin main
	git lfs push origin main --all
	Write-Host "done: $(git log -1 --oneline)"
}

Write-Host "finished"
