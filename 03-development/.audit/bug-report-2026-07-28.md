# Bug Hunt Report — 2026-07-28

> Gate 3 `adversarial_review` 對 9 個 target module（3 high-risk + 6 standard）
> 進行 12 HR × 3-lens + 6 STD × general 的對抗性審查。共找到 **3 個 HIGH** 確
> 認 bug（T-04、T-05、T-07 mitigation 缺口），皆已 RED→GREEN 修復並 commit。

## 掃描摘要

| Module | Lens | 嚴重度 | 狀態 |
|---|---|---|---|
| taskq.cli | concurrency | **HIGH** | resolved (`e8805d0`) |
| taskq.store | concurrency | **HIGH** | resolved (`e8805d0`) |
| taskq.executor | correctness | **HIGH** | resolved (`e8805d0`) |
| taskq.cli | correctness | low | 註記 |
| taskq.executor / store / breaker / cache | resilience / general | 無 bug | — |

`raw_count=3`、`confirmed_count=3`、`refuted_count=0`。

## 確認 Bugs（severity 降序）

### F1 — `taskq.cli` `_cmd_submit` TOCTOU on `--name` (T-07 spoofing mitigation gap)

- **位置**: `03-development/src/taskq/cli.py:174-196`
- **問題**: `_cmd_submit` 對 `--name` uniqueness 做 read-modify-write，卻只用
  `threading.Lock()`（`store.STORE_LOCK`），**process-local**——無法防止兩個獨立
  `python -m taskq submit --name X` process 同時通過 name check 後各寫各的，最後
  一個 write 贏、其他 task 被靜默丟棄。
- **證據**: 10 個並行 subprocess submit 同名 → pre-fix 接受 2 個（期望 1 個）；
  RED repro `test_bh_f1_concurrent_submit_unique_names`。
- **修復**: commit `e8805d0` 在 `_cmd_submit` 內部包 `with store._file_lock(...)`
  —— `fcntl.flock(LOCK_EX)` 跨 process serialisation。
- **驗證**: post-fix 接受 1/10、拒絕 9/10，符合 SPEC §7。

### F2 — `taskq.store` `_atomic_write_json` 跨 process race (T-05 corruption / data loss)

- **位置**: `03-development/src/taskq/store.py:51-64`（pre-fix）
- **問題**: tmp 檔名只用 8-hex suffix（32 bits）並在 `finally: tmp.unlink()` 無條
  件刪除。兩個 process 撞到同一個 tmp 名時，one 的 `finally.unlink` 移除另一個
  還沒 `os.replace` 的 tmp → `FileNotFoundError` + tasks.json 被整個刪掉。
- **證據**: 50 process `multiprocessing.Pool` 預期 50 筆 → pre-fix tasks.json 消
  失、`FileNotFoundError: .../.tasks.json.a0ff0ca5.tmp -> .../tasks.json`；20
  thread probe 5/20 commands 靜默丟失。RED repro `test_bh_f2_concurrent_submits_no_data_loss`。
- **修復**: commit `e8805d0` — tmp suffix 改 16-hex、新增 `store._file_lock` 對
  `.tasks.json.lock` `LOCK_EX`、`_atomic_write_json` 移除自我鎖（避免 reentry 死
  鎖，呼叫端負責持鎖）、`update_task` 同時持 `STORE_LOCK` + `_file_lock`。
- **驗證**: 20-thread probe post-fix 0/20 丟失，tasks.json 合法 JSON。

### F3 — `taskq.executor` stdout/stderr 寫盤未脫敏 (T-04 information disclosure)

- **位置**: `03-development/src/taskq/executor.py:155-160`
- **問題**: `_execute` 把 `result.stdout` / `result.stderr` 原樣 `_tail` 後丟進
  tasks.json 與 cache.json，沒有任何 secret 過濾；任何含 `sk-*` API key、
  `token=...`、Bearer token、`password=...` 的輸出都會被持久化到
  `$TASKQ_HOME/{tasks.json,cache.json}`。
- **證據**: probe `python3 -c "print('sk-ZXAMPLE0123456789ABCDEF0123456789')"`
  跑完後 `tasks.json.stdout_tail` 與 `cache.json` entry 同樣保留原文。RED repro
  `test_bh_f3_subprocess_output_redacted`。
- **修復**: commit `e8805d0` — 新增 `_SECRET_PATTERNS`（sk-*、token=*、Bearer、
  password=*）與 `_redact()` helper，於 TimeoutExpired 與成功路徑都套用。
- **驗證**: post-fix stdout_tail = `[REDACTED]\n`。

## 宣告威脅模型逐項驗證

| Threat | Category | Mitigation Effective? | 證據 |
|---|---|---|---|
| T-01 | tampering | **TRUE** | `shlex.split` + `shell=False`；`;\|$&><\`` blacklist 防禦縱深 |
| T-02 | elevation_of_privilege | **TRUE** | 全 src 無 `shell=True`；唯一 `shell=False` 在 executor.py:142 |
| T-03 | denial_of_service | **TRUE** | `timeout=N` 觸發 `TimeoutExpired` → status='timeout'；ThreadPool `with` block + `fut.result()` 保證 thread 回收 |
| T-04 | information_disclosure | **FALSE → fixed** | pre-fix 原文持久化；post-fix `_redact()` |
| T-05 | tampering | **FALSE → fixed** | pre-fix 50 並發 tasks.json 被刪；post-fix flock + 16-hex |
| T-06 | repudiation | TRUE (hypothetical) | 無 v1→v2 migration 路徑，故暫無 backup 需求；medium severity 留觀 |
| T-07 | spoofing | **FALSE → fixed** | pre-fix 多 process TOCTOU；post-fix `_file_lock` 序列化 |

## 反駁清單（無）

此次 hunt 未發現 false positive；3 個 HIGH 全部 2/2 verifier 確認。

## 修復優先順序

1. **F1 + F2（同一 commit `e8805d0`）** — 必修，影響 NFR-08 / NFR-09。
2. **F3（同一 commit `e8805d0`）** — 必修，影響 NFR-02。
3. T-06 — 加 v1→v2 migration 時實作 backup；目前無 migration 路徑故不擋。

## 掃描方法

- CRG graph build: 沿用既有（targeting manifest 內 `declared=2`，CRG hubs/mutation
  survivors 為 0）。
- Lens 分配: high-risk × {correctness, concurrency, resilience}；standard ×
  {general}。每個 finding 由 refuter + confirmer 兩 verifier 獨立確認；需 2/2
  is_real 或 1/2 + 具體行號引用。
- Anti-fabrication: 每個 finding 都附 RED repro test，先證明 pre-fix 行為再驗
  證 post-fix GREEN。63 / 63 tests pass（含 3 個新 repro）。

## 結論

Gate 3 `adversarial_review` 通過：所有 confirmed critical/high finding 皆為
`resolved` 狀態（`fix_commit=e8805d044357b5a5c0ec04fa1f39339535e3fa91` + repro_test
路徑）。medium/low 留檔追蹤不擋 gate。