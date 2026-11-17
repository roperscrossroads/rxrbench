"""The hawkBit plugin: an Eclipse hawkBit update server as a route to a device.

It brings no family. A family says what a device runs and how that is read; this plugin says
one more way an image can get there. A device with a `hawkbit` entry under its registry
`reach` takes its image from the server when its layout pins `routes: {flash: hawkbit}`: the
bench host tells the server which image the device should have, the device fetches and
installs it on its own schedule, and the family's flasher reads the device before and after,
as it does for every other route.

core's plugin_utils/families says what the exports are; `ROUTE_DELIVERIES` is the one this
plugin is for.
"""

# No family: nothing here says what a device runs.
FAMILIES = {}

# The route. `tool` is deployed to a bench host from host/bin/; `formats` are the images it
# carries (a family takes the route when it lists one of them in `takes_delivery`); `settings`
# names the layout variable that says where the server is and how to log in to it.
ROUTE_DELIVERIES = {
    "hawkbit": {"tool": "hawkbit_route.py", "formats": ("raucb",), "settings": "bench_hawkbit"},
}
