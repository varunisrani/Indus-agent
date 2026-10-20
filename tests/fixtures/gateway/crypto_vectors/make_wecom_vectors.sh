#!/bin/sh
# Known-answer vectors for the WeCom callback crypto (P16-18), made from the vendor's description
# with independent tools only (openssl, coreutils printf/od/base64/sha1sum, sed) -- never with the code under test.
#
# Scheme (WeCom "callback crypto" page): AESKey = base64(EncodingAESKey + "="), 32 bytes;
# IV = first 16 bytes of AESKey; AES-256-CBC over
#   16 random bytes + 4-byte big-endian length of msg + msg + receive id,
# PKCS#7-padded to a multiple of 32 bytes; the result is base64. Signature = lower-case hex SHA1 of
# token, timestamp, nonce and the base64 text, sorted as strings and joined.
#
# Usage: sh make_wecom_vectors.sh > wecom_callback.json   (output is deterministic)
set -eu

TOKEN="indusvectortoken"
ENCODING_AES_KEY="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFE"
RECEIVE_ID="wwvectorcorp0001"
TIMESTAMP="1767258000"
NONCE="n0nce4vectors"
RANDOM_HEX="000102030405060708090a0b0c0d0e0f"

KEY_HEX=$(printf '%s=' "$ENCODING_AES_KEY" | base64 -d | od -An -v -tx1 | tr -d ' \n')
IV_HEX=$(printf '%s' "$KEY_HEX" | cut -c1-32)

hex() { od -An -v -tx1 | tr -d ' \n'; }
unhex() { env printf "$(printf '%s' "$1" | sed 's/../\\x&/g')"; }

# $1 = message, $2 = receive id, $3 = "good" or "badpad": the hex of the padded plaintext.
padded_hex() {
  msg_hex=$(printf '%s' "$1" | hex)
  rid_hex=$(printf '%s' "$2" | hex)
  len=$(printf '%s' "$1" | wc -c | tr -d ' ')
  body="${RANDOM_HEX}$(printf '%08x' "$len")${msg_hex}${rid_hex}"
  size=$(( ${#body} / 2 ))
  pad=$(( 32 - size % 32 ))
  if [ "$3" = "badpad" ]; then
    # last byte claims 33 bytes of padding: never valid for a 32-byte block
    pad_hex=$(i=1; while [ $i -lt $pad ]; do printf '00'; i=$((i+1)); done; printf '21')
  else
    pad_hex=$(i=0; while [ $i -lt $pad ]; do printf '%02x' "$pad"; i=$((i+1)); done)
  fi
  printf '%s%s' "$body" "$pad_hex"
}

encrypt() {
  unhex "$(padded_hex "$1" "$2" "$3")" \
    | openssl enc -aes-256-cbc -nopad -K "$KEY_HEX" -iv "$IV_HEX" | base64 -w0
}

sign() {
  printf '%s\n%s\n%s\n%s\n' "$TOKEN" "$TIMESTAMP" "$NONCE" "$1" | LC_ALL=C sort | tr -d '\n' \
    | sha1sum | cut -d' ' -f1
}

ECHO_PLAIN="1234567890123456789"
MSG_PLAIN='<xml><ToUserName><![CDATA[wwvectorcorp0001]]></ToUserName><FromUserName><![CDATA[ana]]></FromUserName><CreateTime>1767258000</CreateTime><MsgType><![CDATA[text]]></MsgType><Content><![CDATA[hello from the vector]]></Content><MsgId>7000000000000000001</MsgId><AgentID>1000002</AgentID></xml>'

ECHO_ENC=$(encrypt "$ECHO_PLAIN" "$RECEIVE_ID" good)
MSG_ENC=$(encrypt "$MSG_PLAIN" "$RECEIVE_ID" good)
OTHER_ENC=$(encrypt "$MSG_PLAIN" "wwsomeoneelse999" good)
BADPAD_ENC=$(encrypt "$MSG_PLAIN" "$RECEIVE_ID" badpad)

json_string() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

cat <<JSON
{
  "_comment": "Made by make_wecom_vectors.sh with openssl/coreutils from the vendor spec; do not edit by hand.",
  "token": "$TOKEN",
  "encoding_aes_key": "$ENCODING_AES_KEY",
  "receive_id": "$RECEIVE_ID",
  "timestamp": "$TIMESTAMP",
  "nonce": "$NONCE",
  "aes_key_hex": "$KEY_HEX",
  "iv_hex": "$IV_HEX",
  "echostr": {"plain": "$ECHO_PLAIN", "encrypted": "$ECHO_ENC", "signature": "$(sign "$ECHO_ENC")"},
  "message": {"plain": "$(json_string "$MSG_PLAIN")", "encrypted": "$MSG_ENC", "signature": "$(sign "$MSG_ENC")"},
  "wrong_receive_id": {"encrypted": "$OTHER_ENC", "signature": "$(sign "$OTHER_ENC")"},
  "bad_padding": {"encrypted": "$BADPAD_ENC", "signature": "$(sign "$BADPAD_ENC")"}
}
JSON
