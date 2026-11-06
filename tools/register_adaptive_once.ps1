$ErrorActionPreference='Stop'
$name='TradingBot-AdaptiveAI-OneShot'
if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) { throw 'existing_one_shot_requires_inspection' }
$repo='/mnt/c/Users/user/Documents/Codex/2026-10-30/google-cloud-shell-google-cloud-cli/work/paper-pair-selection'
$now=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$boundary=([math]::Floor($now/14400)+1)*14400
$when=[DateTimeOffset]::FromUnixTimeSeconds([long]($boundary+120)).LocalDateTime
$codeHash=(& wsl.exe --cd $repo --exec /usr/bin/python3 -B -c 'from research.adaptive_auto_once import code_hash;print(code_hash())' | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $codeHash -notmatch '^[0-9a-f]{64}$') { throw 'one_shot_code_hash_unavailable' }
$action=New-ScheduledTaskAction -Execute "$env:WINDIR\System32\wsl.exe" -Argument ("--cd $repo --exec /usr/bin/python3 -B -m research.adaptive_auto_once "+([long]($boundary*1000))+" $codeHash")
$trigger=New-ScheduledTaskTrigger -Once -At $when
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
$task=Get-ScheduledTask -TaskName $name
if ($task.State -eq 'Disabled' -or $task.Triggers.Count -ne 1 -or $task.Actions.Count -ne 1) { throw 'one_shot_registration_unverified' }
Write-Output ('ONE_SHOT_REGISTERED boundary_utc='+[DateTimeOffset]::FromUnixTimeSeconds([long]$boundary).ToString('u')+' start_local='+$when.ToString('yyyy-MM-dd HH:mm:ss'))
