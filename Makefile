CC ?= cc
CXX ?= c++
PKG_CONFIG ?= pkg-config
NATIVE_LV2_CFLAGS ?= -O3 -DNDEBUG -std=c11 -Wall -Wextra -Wpedantic
NATIVE_SFIZZ_CXXFLAGS ?= -O3 -DNDEBUG -std=c++17 -Wall -Wextra -Wpedantic
NATIVE_LV2_SRC := tools/mrp_lv2_chain.c
NATIVE_LV2_BIN := resources/tools/mrp-lv2-chain
NATIVE_LV2_PKGS := lilv-0 sndfile lv2
NATIVE_SFIZZ_DIR := tools/mrp_sfizz_worker
NATIVE_SFIZZ_SRC := \
	$(NATIVE_SFIZZ_DIR)/worker.cpp \
	$(NATIVE_SFIZZ_DIR)/worker_engine.cpp \
	$(NATIVE_SFIZZ_DIR)/sfizz_dyn.cpp \
	$(NATIVE_SFIZZ_DIR)/events.cpp \
	$(NATIVE_SFIZZ_DIR)/wav_writer.cpp
NATIVE_SFIZZ_BIN := resources/tools/mrp-sfizz-worker

.PHONY: native native-lv2 native-sfizz-worker clean-native clean-native-lv2 clean-native-sfizz-worker test

native: native-lv2 native-sfizz-worker

native-lv2: $(NATIVE_LV2_BIN)

$(NATIVE_LV2_BIN): $(NATIVE_LV2_SRC)
	@$(PKG_CONFIG) --exists $(NATIVE_LV2_PKGS) || { \
		echo "Missing native LV2 build dependencies: $(NATIVE_LV2_PKGS)" >&2; \
		echo "Void: sudo xbps-install -S base-devel pkg-config lilv-devel libsndfile-devel lv2-devel" >&2; \
		exit 1; \
	}
	@mkdir -p $(dir $@)
	$(CC) $(NATIVE_LV2_CFLAGS) $< -o $@ \
		$$($(PKG_CONFIG) --cflags --libs $(NATIVE_LV2_PKGS)) -lm

native-sfizz-worker: $(NATIVE_SFIZZ_BIN)

$(NATIVE_SFIZZ_BIN): $(NATIVE_SFIZZ_SRC) $(wildcard $(NATIVE_SFIZZ_DIR)/*.hpp) $(NATIVE_SFIZZ_DIR)/sfizz_abi_min.h
	@mkdir -p $(dir $@)
	$(CXX) $(NATIVE_SFIZZ_CXXFLAGS) -I$(NATIVE_SFIZZ_DIR) $(NATIVE_SFIZZ_SRC) -o $@ -ldl

clean-native: clean-native-lv2 clean-native-sfizz-worker

clean-native-lv2:
	rm -f $(NATIVE_LV2_BIN)

clean-native-sfizz-worker:
	rm -f $(NATIVE_SFIZZ_BIN)

test:
	python -m pytest -q
