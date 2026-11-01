# Test VM — 2026-11-01

User authorized one test VM, not bot migration or operation. Project `gen-lang-client-0854183882` display name is now `TradingBot`; ID and billing link are unchanged. Compute Engine and Billing Budget APIs were enabled. No Firebase or Cloud Run resources changed.

## Configuration and access

`tradingbot-paper-test`, `europe-west1-b`: standard non-preemptible e2-micro (shared 2 vCPU, 1 GiB), Ubuntu 24.04 LTS standard image `ubuntu-2404-noble-amd64-v20261024`, 20 GiB pd-standard boot disk (auto-delete), one ephemeral external IPv4, no attached service account or OAuth scopes. Automatic restart enabled. VM and disk are billable; no snapshots, NAT or Pro license added.

Instance-tagged firewall allows TCP 22 from IAP `35.235.240.0/20` at priority 900 and denies other inbound at priority 1000, ahead of default rules. Effective firewall inspected; direct TCP 22, 80 and 443 timed out, authenticated IAP SSH succeeded. Project SSH keys blocked, existing VM-level public key used. Password, keyboard-interactive and root SSH login disabled. System service user `tradingbot` has no login shell. Host ED25519 fingerprint, obtained from authenticated Compute serial console: `SHA256:pqJH5EcYU8Tn0XCq92b/l6uKmSBkMn7ySR9lLpX5ccc`. Keep the local private key protected.

Existing Windows SDK/PuTTY access using pinned host and IAP:

```powershell
$g = Join-Path $env:LOCALAPPDATA 'Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd'
$key = Join-Path $env:USERPROFILE '.ssh\google_compute_engine'
$ppk = Join-Path $env:USERPROFILE '.ssh\google_compute_engine.ppk'
& $g compute ssh codex@tradingbot-paper-test --project=gen-lang-client-0854183882 --zone=europe-west1-b --tunnel-through-iap --ssh-key-file=$key --plain --ssh-flag=-i --ssh-flag=$ppk --ssh-flag=-hostkey --ssh-flag='SHA256:pqJH5EcYU8Tn0XCq92b/l6uKmSBkMn7ySR9lLpX5ccc'
```

## Cost and budget

Estimated USD per 730 hours, no credits, before taxes or other project resources: compute $6.73 (0.25 vCPU at $0.02399337/vCPU-hour and 1 GiB at $0.00321609/GiB-hour); 20 GiB disk $0.80; external IPv4 $3.65 at $0.005/hour; 1 GiB chargeable egress assumed $0.12. Total **about $11.30/month**, or ~$11.50 at 744 hours. Disk estimate conservatively assumes no free allowance. Rates and egress destinations vary; more traffic, logging, taxes or other resources add cost. [Compute pricing](https://cloud.google.com/compute/vm-instance-pricing), [disk pricing](https://cloud.google.com/compute/disks-image-pricing), [network pricing](https://cloud.google.com/vpc/network-pricing).

Billing budget `TradingBot VM monthly 15 USD` covers the entire project per calendar month, with actual-spend alerts at 50%, 90%, 100%, and forecast at 100%. Default billing-role emails enabled; delivery not tested. Alerts may be delayed and **do not cap or stop spending**. No Pub/Sub channel created. [Budget documentation](https://cloud.google.com/billing/docs/how-to/budgets).

## Validation and limits

At ~08:13 UTC 2026-11-01 public Spot Testnet time, BTCUSDT price and 15m candles returned HTTP 200; public USD-M futures time, mark price, 15m candles and funding returned HTTP 200. This is not proof of eligibility for real trading or future availability. [Spot Testnet rules](https://github.com/binance/binance-spot-api-docs/blob/master/testnet/general-info.md) specify virtual funds and resets. Binance's [Belgium announcement](https://www.binance.com/en-NG/support/announcement/detail/c30118f692ca4805a6aba93059c570f7) describes certain resumed services, not blanket futures eligibility. Account and regional trading rights remain unverified; do not bypass restrictions.

Independent checkout `/home/codex/Trading_Bot_vm_check` is at `623d3fce0d07a82f5784e403e435bbd365130fe4`, clean after checks. Full Linux suite passed **247/247** and both synthetic offline Spot and futures demos completed. No Telegram, trading processes, real or Testnet orders ran. A VM reboot changed boot ID; pinned IAP SSH worked afterwards and the service user and SSH restrictions persisted. After reboot ~627 MiB memory available, no swap, ~15.91 GiB disk free. This idle measurement does not prove that two bots, monitoring and backups fit in 1 GiB under peak load. No state, secrets or positions were transferred. Migration, singleton protection, external backup design, delivery of budget alerts and long-term runtime capacity remain to verify separately.
