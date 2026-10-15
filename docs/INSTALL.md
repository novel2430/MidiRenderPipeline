# 安裝與編譯（Linux）

[返回導航](README.md) · [配置指南](CONFIGURATION.md) · [CLI 指南](CLI.md)

這個流程以 `MidiRenderPipeline 0.5.0` 為準。**Python 套件、兩個 MRP 原生 Helper、修改版 libsfizz、FluidSynth、LV2 外掛、音色資源是不同的安裝步驟。** `pip install -e .` 不會替你取得或編譯全部資源。

## 1. 安裝系統依賴

推薦先在 Linux x86-64 操作；下面僅列常見套件名稱。套件版本及可用性以實際發行版為準。

**Debian / Ubuntu 系（範例）**：

```bash
sudo apt update
sudo apt install -y \
  git build-essential cmake ninja-build pkg-config \
  python3 python3-venv python3-pip \
  liblilv-dev libsndfile1-dev lv2-dev libfluidsynth-dev unzip
```

**Void Linux（範例）**：

```bash
sudo xbps-install -S \
  git base-devel cmake ninja pkg-config python3 python3-pip \
  lilv-devel libsndfile-devel lv2-devel fluidsynth fluidsynth-devel unzip
```

用途區分：

| 依賴 | 用於 |
| --- | --- |
| Python 3.11+、`mido`、`numpy`、`soundfile` | MIDI 分析、排程、音訊讀寫 |
| C/C++ 編譯器、Make、CMake、Ninja | 編譯本地 Worker 與 sfizz fork |
| Lilv、LV2 開發標頭、libsndfile | 編譯 `mrp-lv2-chain` |
| `libfluidsynth` | GM / SF2 備援；MRP 不需要 `fluidsynth` CLI |
| LV2 外掛 Bundle | 真正執行 EQ、Amp、Reverb 等效果 |
| SFZ 和 SF2 音色資料 | 提供發聲素材，**不包含在本程式碼 ZIP 中** |

## 2. 安裝 Python 專案

在 MRP 專案根目錄：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
midi-render --help
```

需要執行測試時，額外安裝 `pytest`（它不是 `pyproject.toml` 的正式依賴）：

```bash
python -m pip install pytest
python -m pytest -q
```

## 3. 編譯「修改版 sfizz」共享函式庫

**這一節不能用系統標準 sfizz 直接取代。** MRP 需要基於 sfizz **1.2.3** 的 MRP fork，持久離線 API 至少 **v3**，MRP Worker 協定為 **5**。原生 `sfizz_render` CLI 不是它的替代品。

### 3.1 先確保原始碼完整

MRP 使用的修改版 sfizz 原始碼位於 [novel2430/sfizz](https://github.com/novel2430/sfizz)，而不是官方 upstream。請使用 Git 取得原始碼與所有 Submodules：

```bash
git clone --recurse-submodules https://github.com/novel2430/sfizz.git ~/src/sfizz
cd ~/src/sfizz
git submodule update --init --recursive
```

如果你已經 Clone 過儲存庫，只要進入現有目錄執行 `git submodule update --init --recursive` 即可。請使用與 MRP 相容的 fork 版本；編譯後透過 `midi-render doctor` 驗證離線 API。

本次提供的 `sfizz-mrp-offline-poc-v1(1).zip` **不足以直接編譯**：`external/abseil-cpp`、`external/st_audiofile/thirdparty/libaiff` 和 `wavpack` 等內容缺失；也不包含 `.git` / `.gitmodules`，無法在 ZIP 解壓目錄直接執行 `git submodule update` 補全。需要從對應 Git 原始碼取得缺失的依賴，或另行提供完整 source archive。

**簡單檢查**：

```bash
test -f external/abseil-cpp/CMakeLists.txt && echo 'Abseil present'
test -f external/st_audiofile/thirdparty/libaiff/CMakeLists.txt && echo 'libaiff present'
test -f external/st_audiofile/thirdparty/wavpack/CMakeLists.txt && echo 'wavpack present'
```

### 3.2 Configure、Build、（可選）Install

在**完整的 MRP fork 原始碼**中執行：

```bash
cmake -S . -B build-mrp -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DSFIZZ_SHARED=ON \
  -DSFIZZ_JACK=OFF \
  -DSFIZZ_RENDER=OFF \
  -DSFIZZ_TESTS=OFF \
  -DSFIZZ_BENCHMARKS=OFF \
  -DSFIZZ_DEMOS=OFF \
  -DSFIZZ_DEVTOOLS=OFF

cmake --build build-mrp --target sfizz_shared -j "$(nproc)"

# 預期共享函式庫出現在：
ls -lah build-mrp/library/lib/libsfizz.so*

# 選用：安裝到自訂前綴，而不是覆寫系統版本
cmake --install build-mrp --prefix "$HOME/.local/opt/sfizz-mrp"
```

建議**保留獨立編譯產物，且明確指定完整路徑**，避免意外載入系統已安裝的 upstream libsfizz：

```bash
export MRP_LIBSFIZZ="$(realpath build-mrp/library/lib/libsfizz.so)"
```

`MRP_LIBSFIZZ` 需在**同一個 Shell 環境**中對後續 `midi-render` 生效。也可寫入你的自訂 TOML：

```toml
[paths]
sfizz_library = "/absolute/path/to/sfizz-mrp/build-mrp/library/lib/libsfizz.so"
```

`LIBSFIZZ` 是相容用的環境變數。配置內的 `sfizz_library` 優先於環境變數。

## 4. 下載並放置 Resources（下載連結待補）

> **Resources 下載：** **[MidiRenderPipeline.zip（連結待補）](https://cloud.gskdha.top/f/xQRcR/MidiRenderPipeline.zip)**  
> 發布文檔前，請把 `RESOURCE_DOWNLOAD_URL_HERE` 換成正式下載網址。**目前這只是佔位連結，還不能下載。**

下載的檔案預計叫作 `MidiRenderPipeline.zip`，解壓後的結構如下：

```text
MidiRenderPipeline.zip
└── MidiRenderPipeline/
    └── resources/
        ├── instruments/       # SFZ / SF2 音色
        ├── fx/                # LV2 Bundle
        └── ...
```

**要放入的是 ZIP 內層 `MidiRenderPipeline/resources/` 的內容**，而不是把整個 `MidiRenderPipeline/` 複製進你的 Git 專案，也不是建立 `resources/resources/`。

假設 ZIP 已下載至 `~/Downloads/MidiRenderPipeline.zip`，請在 **MRP 專案根目錄** 執行：

```bash
# 把 ZIP 解壓到暫存資料夾
mkdir -p ~/Downloads/mrp-resource-pack
unzip -q ~/Downloads/MidiRenderPipeline.zip -d ~/Downloads/mrp-resource-pack

# 將 resources 內容合併進當前專案的 resources/
mkdir -p resources
cp -a ~/Downloads/mrp-resource-pack/MidiRenderPipeline/resources/. resources/

# 檢查目錄
ls resources/instruments
ls resources/fx
```

如果希望用圖形檔案管理器操作，則打開解壓後的 `MidiRenderPipeline/resources/`，將**裡面的所有內容**複製到目前 MRP 專案的 `resources/` 目錄，合併同名資料夾即可。

完成後目錄大致如下（實際音色、效果器名稱以資源包為準）：

```text
你的 MRP 專案/
├── config/patches.toml
└── resources/
    ├── instruments/
    │   ├── MuseScore_General_Full.sf2
    │   └── ...
    ├── fx/
    │   └── ...
    └── tools/                 # 下一步會編譯的 MRP Helper
```

資源包不等於 Python 或 sfizz 的安裝，仍需完成其他步驟。請確認所取得資源的使用與再散布符合各自授權。若你的音色包或路徑不同，可參考 [配置指南](CONFIGURATION.md) 自訂 TOML，不需要強制使用預設資源。

## 5. 編譯 MRP 的兩個 Helper

**切回 MRP 專案根目錄**（非 sfizz 原始碼），且建議在複製 Resources **之後**執行，以確保使用目前原始碼編譯出的 Helper：

```bash
make native

# 等價於
# make native-sfizz-worker
# make native-lv2

ls -l resources/tools/mrp-sfizz-worker resources/tools/mrp-lv2-chain
```

`make native` **不會編譯 libsfizz**。其中：

- `mrp-sfizz-worker` 用 C++17 和 `-ldl` 編譯，執行時再動態載入指定的修改版 libsfizz。
- `mrp-lv2-chain` 用 Lilv、libsndfile、LV2 headers 編譯，是 MRP 自己的離線 LV2 Host。

只有更新了對應 C/C++ 原始碼，才需要重新編譯 Helper。若要確保覆蓋舊的 Helper：

```bash
make clean-native && make native
```

## 6. 首次檢查

```bash
# 回到 MRP 根目錄；若使用環境變數，確保已 export MRP_LIBSFIZZ
midi-render doctor
midi-render inspect /path/to/song.mid
midi-render render /path/to/song.mid --include-melody --verbose
```

`doctor` 會列出 Patch/Library/Effect 的 `OK`、`MISSING`、`DISABLED`，並探測實際 sfizz Worker/API。**不要只看指令退出碼**：目前有些 `MISSING` / `FAIL` 會顯示在輸出中，而不一定導致 `doctor` 返回非零碼。

測試建議先使用**沒有太多音軌的一首 MIDI**，確認 `render` 真正輸出 WAV，再跑 Batch。
