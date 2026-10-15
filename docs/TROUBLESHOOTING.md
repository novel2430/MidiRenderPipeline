# 故障排查

[返回導航](README.md) · [安裝指南](INSTALL.md) · [配置指南](CONFIGURATION.md)

## `sfizz` 的 CMake Configure 失敗：缺少 `external/abseil-cpp/CMakeLists.txt`

**原因**：sfizz fork 的 Source ZIP 可能缺少 Git submodules。此次隨專案提供的 `sfizz-mrp-offline-poc-v1(1).zip` 確實缺少 Abseil、libaiff、wavpack 等內容。

**處理**：使用 [MRP 的 sfizz fork](https://github.com/novel2430/sfizz) 取得完整 Git Checkout：

```bash
git clone --recurse-submodules https://github.com/novel2430/sfizz.git ~/src/sfizz
# 已 Clone 的話，改在現有儲存庫內執行：
cd ~/src/sfizz
git submodule update --init --recursive
```

解壓 ZIP 若沒有 `.git`，不能直接對它使用 `git submodule update`。不要為了解決缺模組而直接以官方 upstream sfizz 取代 MRP fork。

## `doctor` 找到 Worker，但 sfizz API Probe 失敗

**原因**：`mrp-sfizz-worker` 和 `libsfizz.so` 不是符合規格的一對；或設定載入了系統 upstream libsfizz。

**處理**：

```bash
# 在 MRP 根目錄
make clean-native-sfizz-worker && make native-sfizz-worker
export MRP_LIBSFIZZ=/absolute/path/to/your/patched/libsfizz.so
midi-render doctor
```

檢查輸出中的 `sfizz OK offline API v...`，至少 v3。MRP 0.5.0 的 Worker Protocol 是 5。

## `doctor` 出現 `patch MISSING` / SFZ 變成 GM 音色

檢查 `libraries.*.root`、`patches.*.sfz` 是否指向真實檔案，大小寫是否吻合。不同音色包有不同的內層目錄。缺失 SFZ 可能**不報錯，直接走 FluidSynth**，所以「有 WAV」不等於「用了預期 SFZ」。

```bash
midi-render --config config/patches.local.toml doctor
midi-render --config config/patches.local.toml render song.mid --verbose
```

## `lv2` Bundle MISSING / 外掛無法載入

檢查 `resources/fx/lv2/<bundle>.lv2` 是否包含 `.ttl`、DSP `.so` 和依賴；`plugin_uri` 必須是外掛宣告的 URI，而不是檔名。也需編譯 `mrp-lv2-chain`：

```bash
pkg-config --modversion lilv-0 sndfile lv2
make native-lv2
```

FX Chain 不能在同一 Stem 混合 `lv2apply` 和 `native-lv2`。使用 `tail_seconds` 必須全部使用支援 Tail 的 native Backend。

## `config` 路徑找不到，明明檔案存在

MRP 的 TOML 相對路徑以**TOML 自己的目錄**為基準；例如從 `config/` 搬到 `config/examples/`，原來的 `../resources` 應改為 `../../resources`。建議先把自訂版放在 `config/patches.local.toml`，確認成功再搬遷。

## `midi-render render song.mid --config ...` 報參數錯誤

`--config` 是全域參數。正確寫法：

```bash
midi-render --config config/patches.local.toml render song.mid
```

## 調了 Reverb / Gain 卻感覺沒有變

先確認效果有放在該軌的 `patch.effects` 或 `[post_effects]`，且實際解析出來的樂器 ID 正確。`--track` 模式不做整曲 Master，適合 A/B 音色；動態 GM Program Change 的軌道目前會跳過音軌 FX。`--verbose` 可顯示 Planning/FX 執行路徑。

## 改了 Patch 還看到舊成果

- Batch 有 `DONE` 狀態：用 `--force` 忽略。
- 希望重做 SFZ / GM RAW：再加 `--rebuild-raw`。
- 換的只是 FX 參數：RAW Cache 本來就可以重用，不需要重做合成。
- 實驗不同配置：建議使用不同的 `--output-dir`、`--work-root`、`--state-db`。

## 渲染完成後找不到某些音軌

用 `inspect --all-tracks` 確認該 MIDI 的 Channel/Program、Note 數量；用 `render --verbose` 看是否 `melody` 被預設略過（加 `--include-melody`）、是否使用 `--skip-unconfigured`、或 SFZ/GM 資源不足。如果是同一物理 Track 含多個 Channel，注意當前 `--track` 只選第一個 Logical Stem，請改用整首渲染驗證。

## 如何回報可重現的錯誤

提供 MRP 版本、作業系統、`--config` 使用的檔案（移除私人路徑與敏感內容）、完整執行命令、`doctor` 關鍵輸出、`--log-file` / `--json-log`，以及可以公開的最小 MIDI 範例。若是崩潰問題，再補上 `--debug` 輸出。
