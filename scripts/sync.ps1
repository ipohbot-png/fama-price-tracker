# FAMA Price Tracker - local sync (run by Windows Task Scheduler).
#
# 1. Pulls the latest cloud-scraped data into this PC's clone.
# 2. Safety net: if the newest archived date is more than 2 days old
#    (GitHub Actions broken or disabled), scrapes FAMA directly from
#    this PC, rebuilds the dashboard data, commits and pushes.
#
# Log: logs\sync.log (kept out of git).

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$logDir = Join-Path $repo "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir "sync.log"

function Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
}

Log "--- sync start ---"

git pull --rebase origin master *>&1 | ForEach-Object { Log "pull: $_" }
if ($LASTEXITCODE -ne 0) {
    Log "pull FAILED (exit $LASTEXITCODE) - likely offline; will still check staleness."
}

# Newest archived price date = lexicographically last CSV name.
$newest = Get-ChildItem (Join-Path $repo "data\daily") -Filter "*.csv" |
          Sort-Object Name | Select-Object -Last 1
if ($null -eq $newest) {
    Log "no archive files found - aborting."
    Log "--- sync end ---"
    exit 1
}
$newestDate = [datetime]::ParseExact($newest.BaseName, "yyyy-MM-dd", $null)
$ageDays = ((Get-Date).Date - $newestDate.Date).Days
Log ("newest archived date: {0} ({1} day(s) old)" -f $newest.BaseName, $ageDays)

if ($ageDays -gt 2) {
    Log "STALE - cloud scraper appears down. Scraping locally as backup."
    python -m scraper.scrape *>&1 | ForEach-Object { Log "scrape: $_" }
    $scrapeExit = $LASTEXITCODE
    Log "scrape exit code: $scrapeExit"
    if ($scrapeExit -eq 0 -or $scrapeExit -eq 1) {
        python -m scraper.aggregate *>&1 | Select-Object -Last 3 | ForEach-Object { Log "aggregate: $_" }
        git add data site/data
        git diff --cached --quiet -- ':!site/data/meta.json'
        if ($LASTEXITCODE -ne 0) {
            git commit -m ("data: local backup scrape {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm")) *>&1 |
                ForEach-Object { Log "commit: $_" }
            foreach ($i in 1..3) {
                git pull --rebase origin master *>&1 | ForEach-Object { Log "pre-push pull: $_" }
                git push origin master *>&1 | ForEach-Object { Log "push: $_" }
                if ($LASTEXITCODE -eq 0) { Log "push OK"; break }
                Log "push attempt $i failed; retrying..."
                Start-Sleep -Seconds (10 * $i)
            }
        } else {
            Log "local scrape produced no new data beyond meta timestamp."
            git reset -q
        }
    } else {
        Log "local scrape failed (exit $scrapeExit) - nothing committed."
    }
}

Log "--- sync end ---"
