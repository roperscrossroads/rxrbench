# fixture.widgets

A plugin collection for the tests, and the smallest example of a plugin. Four families:

- `widget`: a board that answers `widget state` with a state line, compared by class and
  features (the generic identity core's flashers and plays use), with two config regions
  and a UF2 bootloader requirement; a module of its own on core's code (`widget_facts`), step
  tasks (`step_widget.yml`) for one with no console, and a recogniser for its images
  (`RECOGNISERS`, `WIDG` at the start of a region). Its boards have USB ids of their own
  (`USB_KINDS`: a UF2 bootloader and a shipped app, the chip id as serial), and the survey
  tags and describes what its probe finds (`survey_tags`, `survey_describe`), and `just
  diag` asks it two commands by default (`diag_commands`).
- `gadget`: an ESP32 release written whole, with no console: its config regions are the data
  partitions its own partition table names (as a stock release's are). The survey tags it by
  the model and version its probe reads, and enroll keeps them in the board's registry entry
  (`enroll_note`).
- `beacon`: a board whose LoRa radio this tool sets (`radio: managed`). Its layout declares
  the radio as `beacon.radio` (`rf_declared`), and core judges it against the site's RF rules;
  its image is built for one board type only. The tests register the band themselves, as a
  radio plugin does (`rfrules.register_radio`).
- `probechip`: a chip with no console and no partition table, written through a debug probe
  by this plugin's own flasher (`probeflash`), from images built elsewhere. Its flash is a
  file standing in for the chip (`probechip_flash`). It brings everything core has no word
  for: its flasher and the bearer it writes over (`FLASHERS`, `FLASHER_BEARERS`), an update
  step (`update_probe.yml`, through the `probe_chip` module and core's `drive()`), the chip
  verbs (`chip_backup_probe.yml`, `chip_restore_probe.yml`, `erase_probe.yml`), device tests
  through step tasks, a survey tool run through the probe (`host/bin/probe_probechip.py`), and
  its flash's ranges (`flash_regions`: a bootloader, the app, a config range), which core's write rule
  reads through the module (`read_range`, `write_ranges`).
  `tests/unit/test_probechip_end_to_end.py` runs it end to end on a site of its own.

`host/dialects.d/widget-line.json` is a console line dialect as a plugin ships one to the
broker: one-line answers (`= <text>`) and state lines, a secret-bearing command (`passwd `)
and log rules that keep a tunnel's frames (`~W...`) out of the console log.
`tests/support/fake_widget_console.py` is a console that speaks it.
