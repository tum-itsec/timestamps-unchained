# Accompanying code for paper "Timestamps Unchained"

## Description of files / folders

- `esp_side`: Flashable ESP32-C3 applications and supporting libraries.
  - `libopenrtt`: Timestamping library. Main result of our work. Name isn't final and might change in the future.
    - `include/libopenrtt.h`: API to timestamping library.
    - `include/libringbuffer.h`: Helper for passing data between `libopenrtt` callbacks and regular application code.
  - `libserial`: Helper library for more reliability when transmitting large amounts of data over emulated serial port. Only an improvement; serial communication still isn't 100% reliable. Needs `host_primitives` on host side.
  - `generic-eval`: Application meant for performing experiments. Allows for exchanging and timestamping WiFi frames in configurable circumstances, without needing to reflash the application.
  - `n-party-presence`: Implementation of proposed "Authenticated Party Presence" protocol from paper. Name might change in the future. ESP side only contains data collection and cryptography - evaluation of data and computation of final timestamps happens on host side currently, to allow for faster development. Implementation allows for more than 2 parties to participate, but that's not yet tested for accuracy.
- `host_side`: Python scripts interacting with `esp_side` apps. These scripts run on the host system, not ESPs. Currently very chaotic.
  - `data_eval`: Various helper scripts; not meant to be called directly.
  - `host_primitives.py`: Wrappers around `pyserial` to interact with ESPs over serial port. Also includes counterpart to ESP-side `libserial`.
  - `generic_data_capture.py`: Host-side counterpart for ESP-side `generic-eval`. Does one measurement burst.
  - `livedemo.py`: Live demo showing (unfiltered) measured DS-TWR distances based on our timestamps, optionally in comparison with off-the-shelf FTM. Based on `generic-eval`.
  - `random_sampling.py`, `test.py`, `twr.py`, `tuna.py`: Various scripts based on `generic-eval`. Intention of these is to help in finding optimal combination of parameters such as the size of transceived frames.
  - `n-party-presence.py`: Very chaotic script; subject to be reorganized completely. Counterpart to ESP-side `n-party-presence`. Two modes of invoking: Either give it 1 or more UARTS of live ESPs, plus logfile name suffix; then it'll interactively plot various information depending on chosen configuration constants at beginning of file, as well as log all received data to logfiles for future evaluation. Or give it exactly one logfile (suffix is ignored in that case); then it'll do some statistical evaluation and put results into a `.eval` file for consumption by `n-party-presence-ugly-plotter.py`.
  - `n-party-presence-ugly-plotter.py`: Another chaotic script; subject to be reorganized. Give it 1 or more eval files from `n-party-presence.py`; then it'll plot some results and statistical evaluation according to configuration at beginning of script, and optionally also produce CSV files for external plotting (LaTeX for example).
- `openocd.sh`: Helper script for starting OpenOCD with correct arguments so that breakpoints / watchpoints work out-of-the-box. Only needed for ESP debugging.
