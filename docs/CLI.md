# CLI：按使用情境操作

[返回導航](README.md) · [安裝指南](INSTALL.md) · [配置指南](CONFIGURATION.md)

在 MRP 專案根目錄操作並啟用 Python venv。當前命令入口：`midi-render`。

## 1. 快速找到指令

| 我想做什麼 | 命令 |
| --- | --- |
| 查看所有子命令 | `midi-render --help` |
| 查看 Render 的所有參數 | `midi-render render --help` |
| 確認資源、外掛和 Helper | `midi-render doctor` |
| 看單一 MIDI 有哪些 Track、Channel、Program | `midi-render inspect song.mid` |
| 掃描資料集需要哪些樂器 | `midi-render scan ./midis` |
| 渲染整首歌 | `midi-render render song.mid` |
| 只渲染 Track 6 | `midi-render render song.mid --track 6` |
| 批次離線渲染 | `midi-render batch ./midis` |
| 使用自己的 TOML | `midi-render --config config/patches.local.toml render song.mid` |

**語法規則：**

```text
midi-render [--config CONFIG.toml] COMMAND [COMMAND_OPTIONS]
```

`--config` 是頂層選項，**不能放到** `midi-render render` 後面。

## 2. 第一輪檢查：doctor / inspect / scan

```bash
midi-render doctor
midi-render inspect ./midis/song.mid
midi-render inspect ./midis/song.mid --all-tracks
midi-render scan ./midis
midi-render scan ./midis --include-melody
```

- `doctor`：顯示資源路徑是否存在、修改版 sfizz 的 API 探測、Helper 與 FluidSynth 情況；有些錯誤**只是列印狀態**，不能光憑 exit code 判定全部 OK。
- `inspect`：解析原始 MIDI 音軌及其音色；`--all-tracks` 顯示沒有音符的軌道。新版可將一個實體 Track 的多 Channel 展開成多個 Logical Stem，顯示如 `02/01`。
- `scan`：遞迴蒐集 `.mid`、`.midi`，統計樂器、音軌、Note 與 Warning。它**不會驗證所有樂器都有 SFZ**，仍需結合 `doctor` 判斷資源覆蓋。

## 3. 單曲渲染（render）

```bash
midi-render render ./midis/song.mid \
  --output ./renders/song.wav \
  --include-melody \
  --concurrency 5
```

省略 `--output` 時，預設輸出為 `renders/final/<MIDI-name>.wav`。要檢查單軌音色、Gain、Amp、EQ：

```bash
midi-render render ./midis/song.mid \
  --track 6 \
  --work-dir ./renders/debug-track-6 \
  --verbose
```

`--track N` 使用 **MIDI 實體 Track 索引，從 0 開始**。新版一條 Track 可以拆為多個 Channel，但 `--track` 的現行選取實作只返回匹配的**第一個 Logical Stem**；它不是獨立的 `--channel` 選取介面。若要驗證多 Channel 全部輸出，應使用整首渲染。`--track` 也會繞過整曲 Master（便於直接調音色）。

單曲模式預設**不重用整曲 RAW**；指定 `--track` 時才會嘗試重用匹配的 RAW Artifact。`--rebuild-raw` 可強制跳過命中。

## 4. 批次渲染（batch）

```bash
midi-render batch ./midis \
  --output-dir ./renders/final-batch \
  --work-root ./renders/work-batch \
  --state-db ./renders/render-state.sqlite3 \
  --active-songs 24 \
  --concurrency 24 \
  --sfz-max-replicas 2 \
  --include-melody
```

Batch 使用同一個協調器並維護 SQLite/WAL 狀態；輸出目錄保留來源資料夾相對結構。`--active-songs` 控制同時進入規劃/執行的歌曲數，不等於 CPU Core 數。

### 續跑、失敗重試、重新渲染

```bash
# 原指令重跑：已完成的 DONE 歌曲會略過
midi-render batch ./midis --state-db ./renders/render-state.sqlite3

# 重新嘗試之前 FAILED 的歌曲
midi-render batch ./midis --state-db ./renders/render-state.sqlite3 --retry-failed

# 忽略 DONE / FAILED 記錄，重新規劃全部輸入
midi-render batch ./midis --state-db ./renders/render-state.sqlite3 --force

# 重新規劃且強制重做 RAW 合成
midi-render batch ./midis --state-db ./renders/render-state.sqlite3 --force --rebuild-raw
```

續跑時請保持 `--output-dir`、`--work-root` 與 `--state-db` 一致（或有意地切換到另一個實驗），否則容易誤判結果是否已完成。調整配置後的 Cache/State Identity 可能改變，這是為了避免混用不同實驗結果。

## 5. 參數分組（render / batch 共用）

### 常用控制

| 參數 | 預設 | 用途 |
| --- | --- | --- |
| `--concurrency N` | `5` | 全域同時執行的工作數；最常調 |
| `--sfz-max-replicas N` | `1` | 同一 SFZ 音色最多幾個 Worker Replica |
| `--blocksize N` | `1024` | 合成區塊大小 |
| `--samplerate N` | `48000` | 輸出取樣率 |
| `--quality N` | `2` | sfizz 合成品質參數 |
| `--polyphony N` | `256` | 最大同時發聲數 |
| `--include-melody` | 關閉 | 也處理 Melody 角色的音軌 |
| `--skip-unconfigured` | 關閉 | 無可用 SFZ/GM 路線的軌道直接略過 |
| `--keep-work` | 關閉 | 保留中間工作檔（便於排查） |
| `--rebuild-raw` | 關閉 | 不使用已存在的 RAW 快取 |

### 進階效能控制

| 參數 | 預設 | 用途 |
| --- | --- | --- |
| `--sfz-concurrency N` | 自動 | 同時執行的 SFZ 任務上限 |
| `--gm-concurrency N` | 自動 | 同時執行的 FluidSynth 任務上限 |
| `--fx-concurrency N` | 自動 | 同時執行的 FX 任務上限 |
| `--mix-concurrency N` | 自動 | MIX 任務上限 |
| `--sfz-memory-budget SIZE` | 自動 | 常駐 SFZ Working Set 預算；如 `8GiB` |
| `--max-fx-backlog N` | 自動 | FX 堆積達門檻後暫停派送新 RAW |

全域 `--concurrency` 仍是總 Ceiling，調高單個 Backend 上限不會額外創造全域工作槽。預設記憶體預算依系統可用 RAM 自動估算，並非嚴格的程序 RSS 上限。

### 記錄與觀察

```bash
midi-render render song.mid \
  --verbose \
  --log-file renders/run.log \
  --json-log renders/run.jsonl

midi-render batch ./midis \
  --debug --color never --heartbeat 5
```

`--verbose` 看規劃與任務詳情；`--debug` 顯示擷取的底層診斷；`--log-file` 寫純文字；`--json-log` 寫 JSONL；`--heartbeat` 只有 Batch 使用，預設 5 秒。`--color` 可選 `auto|always|never`。

### Batch 專用

| 參數 | 預設 | 用途 |
| --- | --- | --- |
| `--output-dir PATH` | `renders/final-batch` | 成品資料夾 |
| `--work-root PATH` | `renders/work-batch` | RAW 與中間工作根目錄 |
| `--state-db PATH` | `renders/render-state.sqlite3` | SQLite 續跑狀態 |
| `--active-songs N` | `32` | 同時駐留規劃歌曲上限 |
| `--retry-failed` | 關閉 | 重試 FAILED |
| `--force` | 關閉 | 忽略既有 DONE/FAILED 狀態 |

舊版參數仍有兼容別名：單曲 `--jobs`、Batch `--workers` 都指向 `--concurrency`；`--*-workers` 舊 Backend 參數也仍支援。新腳本請一律使用 `--concurrency` / `--*-concurrency`，以免概念混亂。

## 6. 配置檔的 CLI 組織方式

推薦為不同音色/FX 實驗各準備一份完整 TOML，並為每次批次實驗分開輸出、工作區及狀態 DB：

```bash
midi-render --config config/patches.local.toml batch ./midis \
  --output-dir renders/experiments/bass-v2/output \
  --work-root renders/experiments/bass-v2/work \
  --state-db renders/experiments/bass-v2/state.sqlite3 \
  --concurrency 12 --sfz-max-replicas 2
```

目前 CLI **沒有** `config init`、`config validate`、`presets`、`render --channel` 之類子命令；文檔不應把它們當成已實作功能。若未來增加，可規劃放進 `midi-render config ...`，但它不屬於 0.5.0 的使用手冊。
