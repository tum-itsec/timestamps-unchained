#!/bin/bash

idf.py openocd --openocd-commands "-c \"gdb_memory_map disable\" -c \"gdb_breakpoint_override hard\" -f board/esp32c3-builtin.cfg $1"
