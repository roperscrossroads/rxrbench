# The CI image

`ghcr.io/roperscrossroads/rxrbench-ci` is the environment the unit job runs in: a Wolfi base
assembled by apko (`rxrbench-ci.apko.yaml`) with the tool's Python environment on top
(`Dockerfile`), so a job does not install it every time.

- **Tags**: `req-<key>`, where the key (`requirements-key`) hashes every file the image is built
  from; also `latest` and a date. The unit job computes the key of its own checkout and uses
  `req-<key>` when it exists, so a pull request that changes the requirements is tested against
  them, from a fresh install, until the image for them is built.
- **Built** by `.github/workflows/ci-image.yml` when those files change on a push, weekly (new
  Wolfi packages), and by hand.
- **Only CI uses it.** The bench hosts install the tool as `docs/GETTING-STARTED.md` says; the
  hardware CI runs on its bench host directly, since it needs the host's USB devices.
