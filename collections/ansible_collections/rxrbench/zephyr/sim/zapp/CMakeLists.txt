# zapp: the example application of the rxrbench.zephyr plugin (docs/SIMULATED.md. A
# Zephyr application that keeps its configuration in the settings subsystem and takes the
# settings shell, as any Zephyr application can; built for native_sim by `just sim-zapp-build`.
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(zapp)
target_sources(app PRIVATE src/main.c src/digest.c)
