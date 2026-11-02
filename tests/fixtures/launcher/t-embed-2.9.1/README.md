# Launcher 2.9.1 on a LilyGo T-Embed CC1101 Plus, as its serial console printed it

Read through the board's broker on 2026-10-26 (the first Launcher board the family read).
`version.txt`, `partitions.txt` as printed; `settings_get.txt` with the WebUI password
replaced by `example-only` and the board's MAC (which keys one entry) by an example MAC.
What the printouts show: a subtype is named only for `ota_N`, `fat`, `spiffs`, `littlefs`
and printed as hex otherwise (Launcher's own partition is `0x20`); sizes are `20KB`, `3MB`;
`dev` is the developer-mode flag; a release has no `bootTimer` or `DDLB`; this build answers
`version`. `help` and `whoami` print nothing on it.
