#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPECTED="$ROOT/MRP-resources.tree"
RESOURCES="$ROOT/resources"

if [[ ! -f "$EXPECTED" ]]; then
    echo "FAIL: reference tree not found: $EXPECTED" >&2
    exit 2
fi

if [[ ! -d "$RESOURCES/instruments" ]]; then
    echo "FAIL: missing resources/instruments" >&2
    exit 2
fi

if [[ ! -d "$RESOURCES/fx" ]]; then
    echo "FAIL: missing resources/fx" >&2
    exit 2
fi

CURRENT="$(mktemp)"
trap 'rm -f "$CURRENT"' EXIT

(
    cd "$RESOURCES"
    find instruments fx -printf '%y %p -> %l\n' | LC_ALL=C sort
) > "$CURRENT"

if diff -u "$EXPECTED" "$CURRENT"; then
    echo "OK: resource tree matches reference."
else
    echo
    echo "FAIL: resource tree differs from reference." >&2
    exit 1
fi
