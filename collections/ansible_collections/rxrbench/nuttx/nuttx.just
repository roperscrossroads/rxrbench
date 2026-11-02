# The rxrbench.nuttx plugin's recipes, imported by the repository's justfile.

# NuttX's NSH on its sim board, at two builds (nsh, nsh-b with another prompt), from a
# directory holding nuttx/ and apps/ (RXRBENCH_NUTTX_WORKSPACE); no NuttX is brought
[group("simulated benches")]
[doc("NuttX's NSH on its sim board at two builds, from a directory holding `nuttx/` and `apps/` (`RXRBENCH_NUTTX_WORKSPACE`)")]
sim-nuttx-build *args:
    ./venv/bin/python collections/ansible_collections/rxrbench/nuttx/tools/nuttx_sim.py build --name nsh {{args}}
    ./venv/bin/python collections/ansible_collections/rxrbench/nuttx/tools/nuttx_sim.py build --name nsh-b --set 'CONFIG_NSH_PROMPT_STRING="nsh-b> "' {{args}}
