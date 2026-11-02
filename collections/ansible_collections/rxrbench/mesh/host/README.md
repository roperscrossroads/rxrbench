# What this plugin puts on a bench host

`dialects.d/` holds the console rules the serial broker loads (`roles/serial_broker` copies
them beside it; `host/broker/broker.py` `load_dialects` says what a file holds):

- `meshcore.json`: the MeshCore console. Each typed character is echoed, the line ends at CR,
  and the answer is one line: `  -> <text>`, a bare state line `~S{json}*crc16`, or a key
  export `~K<base64>*crc16`, which the log never keeps. A command that carries a secret is
  echoed only up to its command word, so the broker paces the rest instead of waiting for an
  echo.
- `meshtastic.json`: log rules only. The phone API tunnel (`meshtastic api`) carries a node's
  configuration as base64, its private key included, in both directions: in the commands'
  echo and in `~P` frames. A client gets every byte; the log gets only that a frame passed.
