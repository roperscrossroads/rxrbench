"""Where a run's records go, for the callbacks that write them (state.py, timing.py).

A run is named by `bench_run`, given as an extra variable (`just run`, and the `just` recipes
of the plays that keep records). Its directory is `<bench_runs_dir>/<bench_run>`, the default
`<bench_repo>/runs/<bench_run>`, where the plays put their own results.
"""
import os
import subprocess


def run_dir(play):
    """(run, its directory, the first host's vars) for a play, or None when no bench_run was
    given as an extra variable: then nothing is recorded."""
    vm = play.get_variable_manager()
    run = vm.extra_vars.get("bench_run")
    hosts = vm._inventory.get_hosts(play.hosts) if play else []  # pylint: disable=protected-access
    if not run or not hosts:
        return None
    first = vm.get_vars(play=play, host=hosts[0])
    repo = first.get("bench_repo") or os.getcwd()
    runs = first.get("bench_runs_dir") or os.path.join(repo, "runs")
    return str(run), os.path.join(str(runs), str(run)), first


def host_vars(play, name):
    vm = play.get_variable_manager()
    return vm.get_vars(play=play, host=vm._inventory.get_host(name))  # pylint: disable=protected-access


def git(path):
    """{commit, dirty} of the git tree at path, or None when it is not one."""
    try:
        head = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", path, "status", "--porcelain"], capture_output=True,
                               text=True, check=True, timeout=10).stdout.strip() != ""
    except (OSError, subprocess.SubprocessError):
        return None
    return {"commit": head, "dirty": dirty}
