#!/bin/sh
# Known-answer vectors for Weixin (iLink) media crypto (P16-19), made from the vendor's description
# with independent tools only (openssl, coreutils od/base64/printf) -- never with the code under test.
#
# Scheme: AES-128-ECB with PKCS#7 padding over the whole file, one random 16-byte key per file
# (ECB is what the vendor protocol uses). Keys appear as base64 of the 16 raw bytes, or as base64
# of their 32 hex characters (the form outbound items carry).
#
# Usage: sh make_weixin_vectors.sh > weixin_media.json   (output is deterministic)
set -eu

KEY_HEX="5e1f0a9b37c4d2e8806f13a5c9b0e47d"

hex() { od -An -v -tx1 | tr -d ' \n'; }
unhex() { env printf "$(printf '%s' "$1" | sed 's/../\\x&/g')"; }

KEY_B64_RAW=$(unhex "$KEY_HEX" | base64 -w0)
KEY_B64_HEX=$(printf '%s' "$KEY_HEX" | base64 -w0)

case_json() {
  plain_hex=$(printf '%s' "$1" | hex)
  cipher_hex=$(printf '%s' "$1" | openssl enc -aes-128-ecb -K "$KEY_HEX" -nosalt | hex)
  printf '    {"name": "%s", "plain_hex": "%s", "cipher_hex": "%s"}' "$2" "$plain_hex" "$cipher_hex"
}

printf '{\n'
printf '  "key_hex": "%s",\n' "$KEY_HEX"
printf '  "key_b64_raw": "%s",\n' "$KEY_B64_RAW"
printf '  "key_b64_hex": "%s",\n' "$KEY_B64_HEX"
printf '  "cases": [\n'
case_json "" "empty"; printf ',\n'
case_json "hello weixin" "short"; printf ',\n'
case_json "exactly16bytes!!" "one block"; printf ',\n'
case_json "a photo of the whiteboard, page 2 of 3" "three blocks"; printf '\n'
printf '  ]\n'
printf '}\n'
