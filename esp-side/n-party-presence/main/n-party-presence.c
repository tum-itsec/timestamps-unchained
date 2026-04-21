#include <stdio.h>
#include <string.h>

#include "libopenrtt.h"
#include "libringbuffer.h"

#include "esp_mac.h"
#include "nvs_flash.h"
#include "esp_wifi.h"
#include "esp_random.h"
#include "aes/esp_aes_gcm.h"
// just for MBEDTLS_GCM_{DE,EN}CRYPT - I think that's an oversight in the ESP library.
#include "mbedtls/gcm.h"

// 1: use data frames
// 0: use action frames
#define USE_DATA_FRAMES 1

// Length of authentication tag in bytes. Security parameter.
// Valid values: 12, 13, 14, 15, 16.
#define TAG_LENGTH 16

// How often each timestamp should be transmitted.
// A timestamp for a received frame is only useful if at least one of the next TS_TRANSMIT_REPEAT frames is received too.
// Has to fit in a char (or give matched_tss_remaining_tx different type)
#define TS_TRANSMIT_REPEAT 3
// How many timestamped tx frames we can queue at most before the TX callback of the first one is called.
// (If somehow we go over this limit, nothing very bad happens; just TX timestamp gets lost)
#define TX_TS_DELAY_MAX 2
// How often the TX callback can be called before results are polled.
#define TX_RB_SIZE TX_TS_DELAY_MAX
// How often the RX callback can be called before results are polled.
#define RX_RB_SIZE 64

// Length of one timestamp in bytes. Not configurable.
#define TIMESTAMP_LENGTH 8
typedef uint64_t ts_t;

// TODO does this work?
typedef _Atomic char atomic_char_t;

#define abort_if(cond) do { if(cond) abort(); } while(0)

// TODO check endianness
typedef struct __attribute__((packed)) {
	uint32_t lower;
	uint16_t upper;
} msg_id_t;

typedef struct __attribute__((packed)) {
	ts_t ts;
	msg_id_t msg_id;
	uint8_t party_id;
} matched_ts_t;

typedef unsigned char tag_t[TAG_LENGTH];

typedef struct __attribute__((packed)) {
	tag_t tag;
	msg_id_t msg_id;
	uint8_t count;
	// In GCM, ciphertext and plaintext have same length - there's no padding.
	// Length: count * sizeof(matched_ts_t)
	unsigned char data[];
} enc_blob_t;

// IV should be 12 byte for GCM.
// Randomness / unpredictability is not important, but uniqueness very important,
// so we use deterministic counters here.
typedef struct __attribute__((packed)) {
	// 6 byte message id
	msg_id_t msg_id;
	// 1 byte party id
	uint8_t party_id;
	// 5 byte unused - padding to 12 byte
	uint8_t padding[5];
} iv_t;

struct npp_state {
	esp_gcm_context ctx;
	iv_t iv;

	// For matching transmitted tags to msg_ids in tx callback,
	// to verify that transmitted tag observed by tx callback actually is fresh.
	struct {
		tag_t tag;
		msg_id_t msg_id;
	} tx_tags[TX_TS_DELAY_MAX];
	// No further sync is necessary if we take care that:
	// - tx callback always checks ==1, then reads tag, then sets to 0 (and never sets to 1)
	// - fill_tx always checks ==0, then writes tag, then sets to 1 (and never sets to 0)
	atomic_char_t tx_tags_valid[TX_TS_DELAY_MAX];

	// Storing these will be redundant in most circumstances.
	// However, this greatly simplifies logic.
	size_t matched_tss_size;
	matched_ts_t *matched_tss;
	// How many (re-)transmissions each of the matched_tss has left. 0 means free slot.
	unsigned char *matched_tss_remaining_tx;
};

struct npp_state *npp_state;

struct tx_ts {
	ts_t ts;
	msg_id_t msg_id;
};
DEFINE_RINGBUF(struct tx_ts, tx_ts_rb, TX_RB_SIZE+1)

struct rx {
	ts_t ts;
	unsigned char src_mac[6];
	uint16_t payload_len;
	uint8_t *payload;
};
DEFINE_RINGBUF(struct rx, rx_rb, RX_RB_SIZE+1)

#if USE_DATA_FRAME
uint8_t const npp_frame_header[] = {
	// data frame, flags, duration (overwritten by PHY)
	0x08, 0x42, 0x00, 0x00,
	// dest MAC - broadcast so no ACK is expected
	0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
	// src MAC - overwritten by send function
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	// BSS ID. For future use. Our protocol doesn't care.
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	// Sequence number (overwritten by PHY)
	0x00, 0x00,
	// just for funz (and wireshark readability)
	0xde, 0xad, 0xbe, 0xef
#else
uint8_t const npp_frame_header[] = {
	// Action frame, flags, duration (overwritten by PHY)
	0xd0, 0x00, 0x00, 0x00,
	// dest MAC - broadcast so no ACK is expected
	0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
	// src MAC - overwritten by send function
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	// BSS ID. For future use. Our protocol doesn't care.
	0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
	// Sequence number (overwritten by PHY)
	0x00, 0x00,
	// category code "vendor-specific"
	0x7f
};
#endif

uint8_t mac_to_party_id(uint8_t *mac);

void npp_tx_cb(uint64_t tx_ts, uint8_t *payload, uint16_t payload_len) {
	// payload_len seems to always be miscounting by 20 bytes
	// TODO why!?
	payload_len += 20;

	// check if payload is long enough to contain at least fixed-length part of enc_blob_t
	if(payload_len < sizeof(npp_frame_header) + sizeof(enc_blob_t))
		return;

	// check if frame header matches our npp_frame_header
	if(payload[0] != 0xd0 || payload[1] != 0x00 || payload[24] != 0x7f)
		return;

	enc_blob_t *enc_blob = (enc_blob_t *) (payload + sizeof(npp_frame_header));
	for(size_t i = 0; i < TX_TS_DELAY_MAX; i ++) {
		if(!npp_state->tx_tags_valid[i])
			continue;
		if(memcmp(&enc_blob->msg_id, &npp_state->tx_tags[i].msg_id, sizeof(msg_id_t)))
			continue;
		if(memcmp(&enc_blob->tag   , &npp_state->tx_tags[i].tag   , sizeof(tag_t)))
			continue;

		npp_state->tx_tags_valid[i] = 0;

		struct tx_ts tx_ts_struct;
		tx_ts_struct.ts = tx_ts;
		memcpy(&tx_ts_struct.msg_id, &enc_blob->msg_id, sizeof(msg_id_t));
		tx_ts_rb_put_or_overflow(tx_ts_struct);
		return;
	}
}

void npp_rx_cb(uint64_t rx_ts, wifi_promiscuous_pkt_t *pkt) {
	uint16_t payload_len = pkt->rx_ctrl.sig_len;
	uint8_t *payload = pkt->payload;

	// payload_len seems to always be miscounting by 4 bytes
	// TODO why!? maybe some kind of checksum?
	payload_len -= 4;

	// check if payload is long enough to contain at least fixed-length part of enc_blob_t
	if(payload_len < sizeof(npp_frame_header) + sizeof(enc_blob_t))
		return;

	// check if frame header matches our npp_frame_header
	if(payload[0] != 0xd0 || payload[1] != 0x00 || payload[24] != 0x7f)
		return;

	// We have a frame that's likely to be a NPP frame!
	// Timestamp it and copy over data.
	// We shouldn't do crypto here - that might take too long for low-level callback.

	if(!rx_rb_has_space())
		return;

	uint16_t payload_len_noheader = payload_len - sizeof(npp_frame_header);

	uint8_t *payload_copy = malloc(payload_len_noheader);
	if(!payload_copy)
		return;

	memcpy(payload_copy, payload + sizeof(npp_frame_header), payload_len_noheader);

	// We already checked has_space
	struct rx rx = {
		.ts = rx_ts,
		.payload_len = payload_len_noheader,
		.payload = payload_copy,
	};
	memcpy(rx.src_mac, payload + 10, 6);
	rx_rb_put(rx);
}

/**
 * Initializes state.
 * peer_count: how many peers to support at most
 * party_id: Has to be unique for each participant! Else, the protocol is insecure.
 * keybits as documented in esp_aes_gcm_setkey: one of 128, 192, 256.
 *
 * Only call this after wifi has been initialized!
 */
void npp_init(size_t peer_count, uint8_t party_id, const unsigned char *key, unsigned int keybits) {
	assert(TIMESTAMP_LENGTH == sizeof(ts_t));
	assert(sizeof(iv_t) == 12);

	npp_state = malloc(sizeof(struct npp_state));

	esp_aes_gcm_init(&npp_state->ctx);
	abort_if(esp_aes_gcm_setkey(&npp_state->ctx, MBEDTLS_CIPHER_ID_AES, key, keybits));
	npp_state->iv.msg_id.lower = 0;
	npp_state->iv.msg_id.upper = 0;
	npp_state->iv.party_id = party_id;
	memset(npp_state->iv.padding, 0, sizeof(npp_state->iv.padding));
	memset(npp_state->tx_tags_valid, 0, sizeof(npp_state->tx_tags_valid));
	// Reasonable estimate how many timestamps we need to keep simultaneously
	npp_state->matched_tss_size = TX_TS_DELAY_MAX + (1 + peer_count) * TS_TRANSMIT_REPEAT;
	npp_state->matched_tss = calloc(npp_state->matched_tss_size, sizeof(matched_ts_t));
	npp_state->matched_tss_remaining_tx = calloc(npp_state->matched_tss_size, sizeof(unsigned char));
	abort_if(!npp_state->matched_tss || !npp_state->matched_tss_remaining_tx);

	set_tx_cb(npp_tx_cb);
	set_rx_cb(npp_rx_cb);
}

size_t npp_find_matched_ts_slot() {
	// find best slot to put this ts.
	// It should be rare that we don't find a free slot -
	// still, in that case pick the least bad slot.
	size_t matched_ts_slot = 0;
	unsigned char *matched_ts_slot_badnesses = npp_state->matched_tss_remaining_tx;
	size_t slot_count = npp_state->matched_tss_size;
	unsigned char matched_ts_slot_badness = matched_ts_slot_badnesses[0];
	// we can early-out once we reach badness 0, which means free slot.
	for(size_t i = 1; matched_ts_slot_badness != 0 && i < slot_count; i ++) {
		if(matched_ts_slot_badnesses[i] < matched_ts_slot_badness) {
			matched_ts_slot_badness = matched_ts_slot_badnesses[i];
			matched_ts_slot = i;
		}
	}
	return matched_ts_slot;
}

void npp_handle_rx(ts_t ts, uint8_t party_id, uint8_t *payload, uint16_t payload_len) {
	enc_blob_t *enc_blob = (enc_blob_t *) payload;

	size_t length = enc_blob->count != 0 ? enc_blob->count * sizeof(matched_ts_t) : 1;
	size_t enc_length = sizeof(enc_blob_t) + length;
	if(payload_len < enc_length) {
		printf("rx too short - skipping\n");
		return;
	} else if(payload_len > enc_length) {
		printf("rx trailing data (%u bytes) - ignoring\n", payload_len - enc_length);
	}

	// Try to decrypt!
	iv_t iv;
	memset(iv.padding, 0, sizeof(iv.padding));
	iv.msg_id = enc_blob->msg_id;
	iv.party_id = party_id;

	matched_ts_t *plaintext = malloc(length);
	if(!plaintext) {
		printf("rx plaintext oom - skipping\n");
		return;
	}

	int ret = esp_aes_gcm_auth_decrypt(
		&npp_state->ctx, length,
		(unsigned char *) &iv, sizeof(iv_t),
		NULL, 0,
		enc_blob->tag, sizeof(tag_t),
		enc_blob->data, (unsigned char *) plaintext
	);

	if(ret == MBEDTLS_ERR_GCM_AUTH_FAILED) {
		printf("rx AUTH FAILED - skipping\n");
		free(plaintext);
		return;
	}
	abort_if(ret);

	// from here on, plaintext and thus msg_id is proven valid!
	// TODO verify that msg_id is strictly increasing

	for(size_t i = 0; i < enc_blob->count; i ++) {
		matched_ts_t *contained_ts = plaintext + i;
		printf("[TS] %u %u %llu %llu\n", contained_ts->party_id, party_id,
			(uint64_t) contained_ts->msg_id.upper << 32 | contained_ts->msg_id.lower, contained_ts->ts);
	}

	printf("[TS] %u %u %llu %llu\n", party_id, npp_state->iv.party_id,
		(uint64_t) enc_blob->msg_id.upper << 32 | enc_blob->msg_id.lower, ts);

	// Notify others of our RX timestamp.
	// We explicitly don't want to retransmit the TX/RX timestamps from inside the enc_blob!
	size_t matched_ts_slot = npp_find_matched_ts_slot();
	matched_ts_t *rx_ts = &npp_state->matched_tss[matched_ts_slot];
	rx_ts->ts = ts;
	// msg_id is not encrypted itself, BUT attacker can't predict correct tag for faked msg_id,
	// thus msg_id is guaranteed to match tag.
	rx_ts->msg_id = enc_blob->msg_id;
	rx_ts->party_id = party_id;
	npp_state->matched_tss_remaining_tx[matched_ts_slot] = TS_TRANSMIT_REPEAT;

	free(plaintext);
}

/**
 * Polls accumulated RX and TX events.
 * Call this regularily; at least once per sent frame.
 * This function changes state.
 */
void npp_poll_rxtx() {
	if(tx_ts_rb_consume_overflow())
		printf("TX ringbuffer overflow!\n");
	while(tx_ts_rb_has_element()) {
		struct tx_ts tx_ts = tx_ts_rb_take();

		printf("[TS] %u %u %llu %llu\n",
			npp_state->iv.party_id, npp_state->iv.party_id,
			(uint64_t) tx_ts.msg_id.upper << 32 | tx_ts.msg_id.lower, tx_ts.ts);

		// Find free matched_ts slot
		size_t matched_ts_slot = npp_find_matched_ts_slot();

		// Copy over data.
		matched_ts_t *matched_ts = &npp_state->matched_tss[matched_ts_slot];
		matched_ts->ts = tx_ts.ts;
		matched_ts->msg_id = tx_ts.msg_id;
		matched_ts->party_id = npp_state->iv.party_id;

		// We can now mark this matched_ts as valid.
		npp_state->matched_tss_remaining_tx[matched_ts_slot] = TS_TRANSMIT_REPEAT;
	}

	if(rx_rb_consume_overflow())
		printf("RX ringbuffer overflow!\n");
	while(rx_rb_has_element()) {
		struct rx rx = rx_rb_take();
		uint8_t party_id = mac_to_party_id(rx.src_mac);
		npp_handle_rx(rx.ts, party_id, rx.payload, rx.payload_len);
		free(rx.payload);
	}
}

/*
 * How big next fill_tx buffer has to be.
 * May change after poll_rxtx and fill_tx.
 * This function does not change state.
 */
size_t npp_compute_tx_size() {
	size_t matched_ts_count = 0;
	for(size_t i = 0; i < npp_state->matched_tss_size; i ++)
		if(npp_state->matched_tss_remaining_tx[i] > 0)
			matched_ts_count ++;
	// The AES-GCM lib refuses to handle empty input.
	// So, we send a nullbyte if plaintext would be empty instead.
	return sizeof(enc_blob_t) + (matched_ts_count != 0 ? matched_ts_count * sizeof(matched_ts_t) : 1);
}

/**
 * Pass a buffer at least as big as returned by compute_tx_size(); this function will put data in there.
 * Include that data in some transmitted WiFi frame and ensure tx_cb and rx_cb can find it.
 * Careful: only ever send each buffer once!
 * This function changes state.
 */
void npp_fill_tx(unsigned char *buf) {
	enc_blob_t *enc_blob = (enc_blob_t *) buf;

	// Count how many matched_ts we'll transmit and store in enc_blob
	size_t matched_ts_count = 0;
	for(size_t i = 0; i < npp_state->matched_tss_size; i ++)
		if(npp_state->matched_tss_remaining_tx[i] > 0)
			matched_ts_count ++;
	// Size of count field in enc_blob_t is only 1 byte.
	// TODO something more sane.
	abort_if(matched_ts_count > 255);
	enc_blob->count = matched_ts_count;
	size_t length = matched_ts_count != 0 ? matched_ts_count * sizeof(matched_ts_t) : 1;

	// Compute msg_id to use and copy to enc_blob.
	// Increment before usage to be absolutely sure this isn't reused,
	// including paranoid check for overflow.
	// Remember that unsigned int overflow is not UB in C.
	if(++ npp_state->iv.msg_id.lower == 0)
		if(++ npp_state->iv.msg_id.upper == 0)
			abort();
	memcpy(&enc_blob->msg_id, &npp_state->iv.msg_id, sizeof(msg_id_t));

	// Concat all chosen matched_tss to form plaintext buffer.
	// Allocated on heap because stack doesn't have that much space.
	unsigned char *plaintext = malloc(length);
	abort_if(!plaintext);
	// Nullbyte in case plaintext length would be 0 - see comment in npp_compute_tx_size.
	// It's probably faster to unconditionally set this to 0 than to branch.
	plaintext[0] = '\0';
	size_t off = 0;
	for(size_t i = 0; i < npp_state->matched_tss_size; i ++)
		if(npp_state->matched_tss_remaining_tx[i] > 0) {
			npp_state->matched_tss_remaining_tx[i] --;
			memcpy(plaintext + off, &npp_state->matched_tss[i], sizeof(matched_ts_t));
			off += sizeof(matched_ts_t);
		}

	// Do the crypto now.
	abort_if(esp_aes_gcm_starts(&npp_state->ctx, MBEDTLS_GCM_ENCRYPT, (unsigned char*) &npp_state->iv, sizeof(iv_t)));
	size_t output_length_1;
	abort_if(esp_aes_gcm_update(&npp_state->ctx, plaintext, length, enc_blob->data, length, &output_length_1));
	// Explicitly initialize to 0: esp_aes_gcm_finish has a bug and doesn't write output_length unless mbedtls fallback is used.
	size_t output_length_2 = 0;
	abort_if(esp_aes_gcm_finish(&npp_state->ctx, enc_blob->data + output_length_1, length - output_length_1,
		&output_length_2, enc_blob->tag, sizeof(enc_blob->tag)));
	abort_if(output_length_1 + output_length_2 != length);

	// Plaintext not needed anymore from here
	free(plaintext);
	plaintext = NULL;


	// Finally, notify tx callack about msg_id / tag combo

	// Identify some free / invalid / not in use slot.
	// Fall back to first slot if all randoms are valid / in use.
	// A slot being valid / in use here should be very unlikely anyway.
	size_t tx_tag_slot = 0;
	for(size_t i = 0; i < TX_TS_DELAY_MAX; i ++)
		if(!npp_state->tx_tags_valid[i]) {
			tx_tag_slot = i;
			break;
		}

	// Copy over msg_id and tag_id, then mark valid. Order is important.
	memcpy(&npp_state->tx_tags[tx_tag_slot].tag, &enc_blob->tag, sizeof(tag_t));
	npp_state->tx_tags[tx_tag_slot].msg_id = npp_state->iv.msg_id;
	npp_state->tx_tags_valid[tx_tag_slot] = 1;
}

/**
 * Transmits one NPP frame
 */
void npp_tx(uint8_t *src_mac) {
	size_t txsize = npp_compute_tx_size();
	size_t framesize = sizeof(npp_frame_header) + txsize;

	uint8_t *frame = malloc(framesize);
	abort_if(!frame);

	memcpy(frame, npp_frame_header, sizeof(npp_frame_header));
	memcpy(frame + 10, src_mac, 6);
	npp_fill_tx(frame + sizeof(npp_frame_header));

	//printf("triggering tx, len %d\n", framesize);
	// retry at most 16 times. If we don't have mem by then, drop TX:
	// We can't busyloop or we would starve the TX logic.
	int err = ESP_ERR_NO_MEM;
	for(size_t i = 0; err == ESP_ERR_NO_MEM && i < 16; i++)
		err = esp_wifi_80211_tx(WIFI_IF_STA, frame, framesize, true);
	if(err != ESP_ERR_NO_MEM)
		abort_if(err);
	else
		printf("dropped tx because NO_MEM\n");

	free(frame);
}

void npp_deinit() {
	set_tx_cb(NULL);
	set_rx_cb(NULL);
	// TODO Technically we should wait to ensure tx / rx callbacks aren't currently running before we free npp_state.

	esp_aes_gcm_free(&npp_state->ctx);
	free(npp_state->matched_tss);
	free(npp_state->matched_tss_remaining_tx);
	free(npp_state);
	npp_state = NULL;
}

uint8_t mac_to_party_id(uint8_t *mac) {
	// TODO big case distinction over hardcoded MAC addresses is more sane than assuming lowest byte is distinct for each used ESP
	return mac[5];
}

void event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data) {
	switch(event_id) {
	case WIFI_EVENT_STA_CONNECTED:
		printf("sta connect\n");
		break;
	case WIFI_EVENT_STA_DISCONNECTED:
		printf("sta disconnect\n");
		break;
	}
}

void init_system() {
	ESP_ERROR_CHECK(nvs_flash_init());
	ESP_ERROR_CHECK(esp_netif_init());
	ESP_ERROR_CHECK(esp_event_loop_create_default());

	wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
	ESP_ERROR_CHECK(esp_wifi_init(&cfg));
	ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL, NULL));
	ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

	// The fact that the ESPs are connected to an AP isn't relevant to the protocol.
	// This is just to demonstrate that our protocol doesn't inhibit the ESP's regular WiFi operations.
	// The only thing that matters is that all parties use the same channel.
	wifi_config_t config;
	config.sta = (wifi_sta_config_t) {
		.ssid = "npp",
		.password = "5615203e4f8c",
		.scan_method = WIFI_FAST_SCAN,
	};
	ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
	ESP_ERROR_CHECK(esp_wifi_start());
	ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));

	// Redundant if the AP configured above exists and all parties can connect to it.
	ESP_ERROR_CHECK(esp_wifi_set_channel(3, WIFI_SECOND_CHAN_NONE));

	ESP_ERROR_CHECK(esp_wifi_connect());

	ESP_ERROR_CHECK(esp_wifi_config_80211_tx_rate(WIFI_IF_STA, WIFI_PHY_RATE_6M));

	ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
	wifi_promiscuous_filter_t filter = {
		.filter_mask = WIFI_PROMIS_FILTER_MASK_DATA|WIFI_PROMIS_FILTER_MASK_CTRL
	};
	ESP_ERROR_CHECK(esp_wifi_set_promiscuous_filter(&filter));
	wifi_promiscuous_filter_t ctrl_filter = {
		.filter_mask = WIFI_PROMIS_CTRL_FILTER_MASK_ACK
	};
	ESP_ERROR_CHECK(esp_wifi_set_promiscuous_ctrl_filter(&ctrl_filter));
}

void app_main(void)
{
	unsigned char key[] = {
		0x7d, 0x21, 0x32, 0x00, 0x87, 0xf2, 0xd5, 0x26, 0x0c, 0x83, 0x3c, 0x12, 0x62, 0x7a, 0x2e, 0xa6,
		0x39, 0xdd, 0x63, 0x37, 0x69, 0xec, 0x5d, 0x3e, 0x43, 0x03, 0x28, 0xf0, 0x4f, 0xed, 0x8a, 0x4c};
	uint8_t mac[6];
	ESP_ERROR_CHECK(esp_efuse_mac_get_default(mac));
	uint8_t own_party_id = mac_to_party_id(mac);
	printf("[PARTYID] %d\n", own_party_id);

	init_system();

	npp_init(10, own_party_id, key, sizeof(key) * 8);

	TickType_t last_tx_time = xTaskGetTickCount();
	for(char i = 0;; i++) {
		vTaskDelayUntil(&last_tx_time, pdMS_TO_TICKS(100));
		npp_poll_rxtx();
		if(i >= 1 /* 20 */) {
			i = 0;
			npp_tx(mac);
		}
	}

	npp_deinit();
}
