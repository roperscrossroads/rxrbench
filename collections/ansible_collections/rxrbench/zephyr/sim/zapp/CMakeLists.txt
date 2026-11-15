# zapp: the example application of the rxrbench.zephyr plugin (docs/SIMULATED.md). A Zephyr
# application that keeps its configuration in the settings subsystem and takes the settings
# shell, as any Zephyr application can; built for native_sim by `just sim-zapp-build`, and for
# real boards by `just zapp-build` (hw/README.md).
cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(zapp)
target_sources(app PRIVATE src/main.c src/shell.c src/features.c)

# each feature its own file; the list goes beside the image for the catalog
set(features "")
foreach(f DIGEST STATE LOCATE SELFTEST BOOTSEL WIFI)
  if(CONFIG_ZAPP_${f})
    string(TOLOWER ${f} name)
    target_sources(app PRIVATE src/${name}.c)
    list(APPEND features ${name})
  endif()
endforeach()
if(CONFIG_REBOOT)
  list(APPEND features reboot)
endif()
list(SORT features)
string(REPLACE ";" " " features_text "${features}")
file(WRITE ${CMAKE_BINARY_DIR}/zapp-features.txt "${features_text}\n")
target_compile_definitions(app PRIVATE ZAPP_FEATURES="${features_text}")
