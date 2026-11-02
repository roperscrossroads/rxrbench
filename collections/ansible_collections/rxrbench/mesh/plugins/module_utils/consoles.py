"""What the two mesh consoles carry that an error message must not quote.

The phone API tunnel (`meshtastic api`) carries a node's configuration, its private key
included; MeshCore's CLI has commands that set a secret. Registered with the broker client
when any of this plugin's code is imported, so an error names such a command by its words.
"""
from ansible_collections.rxrbench.core.plugins.module_utils import broker_client

TUNNEL = r"^(meshtastic api (?:\+ )?)(\S{8,})(.*)$"
SECRET_WORDS = ("password ", "set prv.key ", "set guest.password ", "set bridge.secret ")

broker_client.secret_shapes(payloads=[TUNNEL], prefixes=SECRET_WORDS)
