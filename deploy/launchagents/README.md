# launchd job backups (macOS)

This folder is a version-controlled backup of every `ai.renaiss.*` /
`com.renaiss.*` job in `~/Library/LaunchAgents/`. The jobs that actually run
live under `~/Library/LaunchAgents/`; these are restore copies only.

## Redacted fields

The `BNB_RPC` value (keyed BSC RPC nodes) in `ai.renaiss.onchain.plist` and
`ai.renaiss.livepool.plist` is replaced with
`__SET_BNB_RPC_COMMA_SEPARATED__`. After restoring, set it back to your real
comma-separated node list; otherwise on-chain sync falls back to public nodes,
which are slower and more rate-limited.

## Restore / Install

```bash
# 1) Copy into LaunchAgents (set BNB_RPC back on the two files above first)
cp deploy/launchagents/ai.renaiss.*.plist  ~/Library/LaunchAgents/
cp deploy/launchagents/com.renaiss.*.plist ~/Library/LaunchAgents/

# 2) Load a single job
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.renaiss.healthcheck.plist

# 3) Trigger once / restart
launchctl kickstart -k gui/$(id -u)/ai.renaiss.healthcheck
```
