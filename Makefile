CC ?= cc
PKG_CONFIG ?= pkg-config
NATIVE_LV2_CFLAGS ?= -O3 -DNDEBUG -std=c11 -Wall -Wextra -Wpedantic
NATIVE_LV2_SRC := tools/mrp_lv2_chain.c
NATIVE_LV2_BIN := resources/tools/mrp-lv2-chain
NATIVE_LV2_PKGS := lilv-0 sndfile

.PHONY: native-lv2 clean-native-lv2 test

native-lv2: $(NATIVE_LV2_BIN)

$(NATIVE_LV2_BIN): $(NATIVE_LV2_SRC)
	@$(PKG_CONFIG) --exists $(NATIVE_LV2_PKGS) || { \
		echo "Missing native LV2 build dependencies: $(NATIVE_LV2_PKGS)" >&2; \
		echo "Void: sudo xbps-install -S base-devel pkg-config lilv-devel libsndfile-devel" >&2; \
		exit 1; \
	}
	@mkdir -p $(dir $@)
	$(CC) $(NATIVE_LV2_CFLAGS) $< -o $@ \
		$$($(PKG_CONFIG) --cflags --libs $(NATIVE_LV2_PKGS)) -lm

clean-native-lv2:
	rm -f $(NATIVE_LV2_BIN)

test:
	python -m pytest -q
