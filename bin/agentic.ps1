# Agentic OS command wrapper (PowerShell). Add this bin directory to PATH,
# then: agentic project list
$entry = Join-Path (Split-Path $PSScriptRoot -Parent) ".agentic\run"
py $entry @args
exit $LASTEXITCODE
