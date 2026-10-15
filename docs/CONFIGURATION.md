# 自訂 `patches.toml`：從音色到效果鏈

[返回導航](README.md) · [安裝指南](INSTALL.md) · [CLI 指南](CLI.md)

MRP 0.5.0 的配置是一份 **TOML Patch Registry**。你可以換音色、關閉某個 SFZ、調節每軌 Gain、添加 LV2、指定 GM 備援、調整 Velocity 與 Master，而**不需要修改 Python**。

## 1. 最安全的開始方式

建議第一次不要從空檔案開始，先複製現有配置，並放在相同 `config/` 目錄：

```bash
cp config/patches.toml config/patches.local.toml
# 編輯 config/patches.local.toml
midi-render --config config/patches.local.toml doctor
midi-render --config config/patches.local.toml render song.mid --track 6
```

**重要規則：**

- `--config` 必須在子命令之前。
- `--config` **整份替換**預設 Registry，不會和 `config/patches.toml` 合併。
- `[paths]` 中的相對目錄以**當前 TOML 檔案所在目錄**為基準，不是執行命令的 CWD。
- `[libraries.*].root` 相對 `[paths].instruments`；`[patches.*].sfz` 相對對應 Library Root。
- LV2 `bundle` 相對 `[paths].lv2`，`[drum_kick_layer].sfz` 相對 `[paths].instruments`。
- Library Root 及 `sfizz_library` 也可用絕對路徑，便於把資源放在其他磁碟。
- 不必安裝所有 Patch；缺失或停用的 SFZ 無法命中時，若 GM SoundFont 已配置，會嘗試 GM fallback。

## 2. 完整渲染路由

```text
MIDI Program / Track Name / Channel
  → 標準化樂器名稱（例如 electric_bass）
  → [patches.electric_bass] 已啟用、SFZ 檔存在？
      ├─ 有：sfizz → patch.effects → post_effects → mix
      └─ 無：family_fallbacks（若有）
                ├─ 有：sfizz → FX → mix
                └─ 無：FluidSynth + GM SoundFont → post_effects* → mix
```

`*` **特殊限制**：真正多 Program Change 的 GM 動態音軌，目前會跳過 `StemPlan.effects`，避免同一條音軌上混合不同音色時套用固定效果策略。

若同一 MIDI Channel 真正使用多個 Program，SFZ 路徑會選一個代表性音色並鎖定，**不是隨 Program Change 動態切換 SFZ**；GM 備援可保留原始動態 Program Change。

## 3. 目錄與共享函式庫 `[paths]`

```toml
[paths]
instruments = "../resources/instruments"
lv2 = "../resources/fx/lv2"
tools = "../resources/tools"
# sfizz_library = "/absolute/path/to/libsfizz.so"
```

`[paths].fx` 是可選的舊式 FX Root，主要保留相容性；目前 native LV2 Plugin Bundle 使用 `[paths].lv2`。

若把配置放到 `/home/user/mrp-configs/my.toml`，仍保留 `../resources/...`，通常會找錯目錄。請改為絕對路徑或重算相對路徑。

## 4. 宣告 SFZ 音源 `[libraries]` 和 `[patches]`

```toml
[libraries.my_bass]
root = "MyBassSamples"       # 實際路徑：resources/instruments/MyBassSamples/

[patches.electric_bass]
library = "my_bass"          # 必須對應 libraries.my_bass
sfz = "bass.sfz"             # 相對 MyBassSamples/
enabled = true
gain_db = -3.0
effects = ["my_amp"]       # 可為 []；效果要在 [effects.*] 定義
```

`electric_bass` 是**既有解析器識別的樂器 ID**，不是可以任意新增且自動匹配的名稱；例如改成 `cool_bass` 並不會讓 MIDI 自動選中它。配置新增 Patch，只是新增一個 Registry Entry；若要引入全新辨識類別，仍須改 Instrument Resolver 的程式碼。

當某個 `patches.*` 不存在、`enabled = false`，或指向的 SFZ 不存在時，才會使用 Family / GM 備援。僅僅列出一個不存在的 `effects` 名稱，不能產生音訊效果。

### 4.1 共用音色 Family Fallback

```toml
[family_fallbacks]
electric_guitar_muted = "electric_guitar_clean"
muted_trumpet = "trumpet"
```

左邊是被解析出的樂器；右邊**必須是已宣告的 Patch 名稱**，且該 Patch 的 SFZ 實際存在、已啟用才會命中。

## 5. FluidSynth / GM 備援

```toml
[general_midi_fallback]
enabled = true
soundfont = "MuseScore_General_Full.sf2"  # 相對 instruments Root
synth_gain = 0.2
gain_db = 6.0

[general_midi_fallback.program_for_instrument]
acoustic_piano = 0
acoustic_guitar = 25
electric_bass = 33
synth_lead = 80
```

GM Program 編號在這份 TOML 中使用 **0–127（從零開始）**。有可信原始 Program 時會優先保留；映射表主要用於無法信任原始 Program 的軌道。`synth_gain` 為 FluidSynth 合成設定；`gain_db` 為輸出音軌的後處理增益。

只使用 FluidSynth 的示例配置：[`config/examples/gm-only.toml`](../config/examples/gm-only.toml)。其 `[paths]` 已對應範例檔所在的 `config/examples/` 位置。

## 6. 設定 LV2 效果器

先定義**效果本身**，再將它放入一條 FX Chain。

```toml
[effect_renderer]
backend = "native-lv2"
tool = "mrp-lv2-chain"
block_size = 1024

[effects.my_amp]
bundle = "gx_ampegsvt.lv2"
plugin_uri = "http://guitarix.sourceforge.net/plugins/gx_ampegsvt_#_ampegsvt_"
input_channels = 1

[effects.my_amp.params]
BYPASS = 1.0
BASS = 0.60
MIDDLE = 0.30
TREBLE = 0.40
VOLUME = 0.20
```

- `bundle`：LV2 Bundle 目錄名；可填絕對路徑。
- `plugin_uri`：外掛在 `.ttl` 中真正宣告的 URI；不是 Bundle 名稱。
- `input_channels`：根據 Plugin 需要選 1（mono）或 2（stereo）等，不是「讓它變成 2 聲道」的音色參數。
- `[effects.NAME.params]`：Control Port 的 **symbol 和實際取值**，必須對上 Plugin 規格；不同外掛並沒有通用的 `0–1` 範圍。
- `backend = "lv2apply"` 可作單一效果的相容性指定，但**同一條 Chain 不可混用 native-lv2 和 lv2apply**。
- Reverb / Delay 可設定 `tail_seconds`，例如 `tail_seconds = 2.5`；僅支援 `native-lv2`，Chain 中的 Tail 會加總。

### 6.1 Patch-local FX 與 Post FX 的差別

```toml
[patches.electric_bass]
library = "my_bass"
sfz = "bass.sfz"
effects = ["my_amp"]         # 音色本身的專屬處理：只套在此 Patch

[post_effects]
electric_bass = ["my_eq", "my_reverb"]  # 音軌類型：SFZ 與固定 Program GM 均可追加
```

實際執行順序為：

```text
RAW → my_amp → my_eq → my_reverb → MIX
```

要新增 `my_eq`、`my_reverb`，還要自行宣告對應 `[effects.my_eq]` 和 `[effects.my_reverb]`；上面只是路由示範。也可用現有 `lead_eq` 與 `dragonfly_plate`：

```toml
[post_effects]
synth_lead = ["lead_eq", "dragonfly_plate"]
melody = ["lead_eq", "dragonfly_plate"]
```

請注意 TOML **同名 Table 不能重複宣告**；修改時請編輯原有 `[post_effects]`，不要在檔案後方再加第二個。

## 7. Melody、演奏強度與 Master

```toml
[melody]
mode = "gm"            # auto | gm | instrument
gm_program = 71        # gm 模式可選；0..127

[performance]
enabled = true
constant_spread_max = 4.0
low_percentile = 0.10
high_percentile = 0.90

[performance.instruments.electric_bass]
velocity_min = 50
velocity_nominal = 60
velocity_max = 65

[master]
normalize_peak_db = -1.0
gain_db = -10.0
```

- Melody 預設是否渲染由 CLI 的 `--include-melody` 控制；`melody.mode` 控制**渲染時如何選音色**。
- `mode="gm"` 強制使用 GM；`mode="instrument"` 還需 `instrument="acoustic_piano"` 等有效樂器名稱；`auto` 則由 MIDI 決定。
- Performance Profile 只對**明確宣告的樂器**有效，不會自動改寫所有 MIDI Velocity。
- Master：先進行 Peak Normalize，再加總輸出 Gain；`render --track` 的單軌調音刻意不套用整曲 Master。

## 8. 鼓組補強 Layer

```toml
[drum_kick_layer]
enabled = true
sfz = "MuldjordKit SFZ+FLAC-20201018/MuldjordKit 20201018 GM.sfz"
notes = [35, 36]
gain_db = -4.0
```

這個 Layer 針對鼓的特定 MIDI Note 額外合成一條 Stem。`sfz` 相對 `[paths].instruments`，和一般 `[patches]` 的 SFZ 路徑規則不同。要停用，設 `enabled = false` 或刪掉整段。

## 9. 自訂配置的檢查順序

1. 以 `midi-render --config config/patches.local.toml doctor` 查路徑和工具。
2. 以 `midi-render inspect song.mid` 看 MIDI 真正解析成哪些樂器。
3. 以 `midi-render --config ... render song.mid --track N --verbose` 調單軌的音色與 FX。
4. 再做整首歌或 Batch。
5. 換 FX 或 Master 時 RAW 合成結果可重用；需要強制重合成才加 `--rebuild-raw`。

`doctor` 顯示 Patch 為 `MISSING` 不一定是致命錯誤；如果 GM 備援可用且不是你預期使用的 SFZ，就可繼續。但**你預期的 SFZ 若顯示 MISSING，代表實際可能改走 GM，音色就會不同**。
