#pragma once

#include "esp_wifi_types.h" // for wifi_promiscuous_pkt_t

// Unit of rx_ts and tx_ts is picoseconds since system boot.
// Overflows may occur if the system runs for extended periods of time.
typedef void (*rx_cb_t)(uint64_t rx_ts, wifi_promiscuous_pkt_t *pkt);
typedef void (*tx_cb_t)(uint64_t tx_ts, uint8_t *frame_data, uint16_t data_len);

void set_rx_cb(rx_cb_t rx_cb);
void set_tx_cb(tx_cb_t tx_cb);
