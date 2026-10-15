# MRP 使用者文檔

> 適用版本：MidiRenderPipeline **0.5.0**；以 `MidiRenderPipeline-main(4)` 的程式行為為準。以下以 **Linux** 為主要操作環境。

MRP 用來將單曲或整批 MIDI 離線渲染成 WAV。它可優先使用自訂 SFZ 音色；缺少對應音色時，使用 FluidSynth + GM SoundFont；也支援 LV2 效果器。音源、效果器與選音規則由 TOML 配置控制。

## 從這裡開始

| 你的目標 | 閱讀 |
| --- | --- |
| 第一次安裝：Python、修改版 sfizz、MRP Helpers、LV2、Resources 下載與解壓 | [安裝指南](INSTALL.md) |
| 使用既有配置、建立自己的 `patches.toml`、換 SFZ、調 EQ / Reverb | [配置指南](CONFIGURATION.md) |
| 檢查 MIDI、單曲渲染、指定 Track、批次與續跑 | [CLI 指南](CLI.md) |
| `doctor` 顯示 MISSING、sfizz 找不到、LV2 失敗、音色不正確 | [故障排查](TROUBLESHOOTING.md) |
| 想了解內部排程、快取與架構 | [渲染系統設計](../RENDERING_SYSTEM.md) |

**Resources 下載連結目前保留在 [安裝指南](INSTALL.md)，待提供 `MidiRenderPipeline.zip` 正式網址。** 資源包解壓後，請把內層 `MidiRenderPipeline/resources/` 的內容合併至本專案 `resources/`。

## 安裝完成之後，最短使用路徑

在**專案根目錄**執行（假設 `.venv` 已啟用，音源已安裝）：

```bash
# 1. 確認設定、音源與執行工具
midi-render doctor

# 2. 看 MIDI 內有哪些軌與樂器
midi-render inspect /path/to/song.mid

# 3. 渲染一首歌
midi-render render /path/to/song.mid --include-melody

# 4. 使用自己的完整配置
cp config/patches.toml config/patches.local.toml
midi-render --config config/patches.local.toml doctor
midi-render --config config/patches.local.toml render /path/to/song.mid
```

**注意：** `--config` 要放在 `render`、`batch`、`doctor` 等子命令**之前**。目前沒有 `config merge`、`config set` 或 TOML 局部覆寫功能：`--config` 指向一份**完整獨立**的配置檔。

## 理解五個名詞

| 名詞 | 用途 |
| --- | --- |
| **SFZ** | 描述取樣音色的檔案；需保留其 Sample 相對路徑 |
| **修改版 libsfizz** | [novel2430/sfizz](https://github.com/novel2430/sfizz)；MRP 的長駐 SFZ 渲染核心，與一般系統 sfizz 不同 |
| **mrp-sfizz-worker** | MRP 自己編譯的 Worker，負責呼叫修改版 libsfizz |
| **SoundFont（SF2）/ FluidSynth** | 沒有 SFZ 覆蓋時的 GM 備援 |
| **LV2 Bundle / mrp-lv2-chain** | LV2 外掛資源，以及執行 FX Chain 的 MRP Host |

這份文檔是**使用指南**，不取代既有 `README.md` 和 `RENDERING_SYSTEM.md` 的內部技術說明。
