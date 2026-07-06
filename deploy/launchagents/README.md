# launchd 排程備份（macOS）

此資料夾是 `~/Library/LaunchAgents/` 裡所有 `ai.renaiss.*` / `com.renaiss.*`
job 的版控備份。真正跑的是 `~/Library/LaunchAgents/` 下的檔案，這裡只是還原用副本。

## ⚠️ 已脫敏欄位
`ai.renaiss.onchain.plist` 與 `ai.renaiss.livepool.plist` 的 `BNB_RPC`（帶 key 的
BSC RPC 節點）已被替換為 `__SET_BNB_RPC_COMMA_SEPARATED__`。**還原後必須手動填回**
真實的逗號分隔節點清單，否則鏈上同步會退化成公共節點（慢、易被限流）。

## 還原 / 安裝
```bash
# 1) 複製到 LaunchAgents（記得先把上面兩支的 BNB_RPC 填回）
cp deploy/launchagents/ai.renaiss.*.plist  ~/Library/LaunchAgents/
cp deploy/launchagents/com.renaiss.*.plist ~/Library/LaunchAgents/

# 2) 載入（單一 job）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.renaiss.healthcheck.plist

# 3) 立即觸發一次 / 重啟
launchctl kickstart -k gui/$(id -u)/ai.renaiss.healthcheck
```
