# WeCom

Indus Agent supports two kinds of WeCom (WeChat Work) integration.

| | Smart robot | Callback app |
|---|---|---|
| Platform name | `wecom` | `wecom_callback` |
| Connection | one WebSocket opened by the gateway | WeCom calls the gateway's endpoint |
| Credentials | `WECOM_BOT_ID`, `WECOM_SECRET` | `WECOM_CALLBACK_CORP_ID`, `WECOM_CALLBACK_CORP_SECRET`, `WECOM_CALLBACK_AGENT_ID`, `WECOM_CALLBACK_TOKEN`, `WECOM_CALLBACK_ENCODING_AES_KEY` |

## Smart robot

Create an intelligent robot in the WeCom admin console with the long connection API and copy its
bot id and secret. Then `indus gateway setup wecom`, or set the two variables. WeCom allows one
connection per robot: if a second gateway uses the same robot, the first one stops and says so.

## Callback app

Create a self-built application in the WeCom admin console, set its message receiving URL to the
gateway's callback endpoint (published through your HTTPS reverse proxy), and copy the corp id,
the app's secret and agent id, and the token and encoding AES key you set there. Then
`indus gateway setup wecom_callback`. Messages are verified and decrypted with those keys.
