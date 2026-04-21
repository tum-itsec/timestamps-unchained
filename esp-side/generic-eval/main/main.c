/* BSD Socket API Example

   This example code is in the Public Domain (or CC0 licensed, at your option.)

   Unless required by applicable law or agreed to in writing, this
   software is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
   CONDITIONS OF ANY KIND, either express or implied.
*/
#include <stdarg.h>
#include <sys/param.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_event.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_wifi_types.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_wifi.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "inttypes.h"
#include <lwip/netdb.h>
#include "esp_task_wdt.h"

#include "libopenrtt.h"
#include "libringbuffer.h"
#include "libserial.h"

// Commands which take arguments should include space and are compared with strncmp.
// Commands without args should not include newline and are compared with strcmp.
#define RESET_COMMAND "reset"
#define SET_SSID_COMMAND "set ssid "
#define SET_IF_COMMAND "set if "
#define SET_CHANNEL_COMMAND "set channel "
#define SET_ENCODING_SPEED_COMMAND "set encoding_speed "
#define SET_BANDWIDTH_COMMAND "set bandwidth "
#define START_WIFI_COMMAND "start_wifi"
#define START_AP_COMMAND "start_ap"
#define CONNECT_COMMAND "connect"
#define SET_BURST_ID_COMMAND "set burst_id "
#define SET_BURST_PEER_COMMAND "set burst_peer "
#define SET_BURST_SCHEDULING_MODE_COMMAND "set burst_scheduling_mode "
#define SET_BURST_PERIOD_COMMAND "set burst_period "
#define SET_PACKET_LENGTH_COMMAND "set packet_length "
#define SYNC_COMMAND "sync "
#define BURST_COMMAND "burst "
#define FTM_COMMAND "ftm"
#define SYSTEM_INFO_COMMAND "systeminfo"
#define WAIT_RB_EMPTY_COMMAND "wait_rb_empty"

#define ESP_MSG_MARKER "ESP_MSG: "
#define msg(fmt, ...) printf(ESP_MSG_MARKER fmt "\n", ##__VA_ARGS__)

uint8_t const frame_header[] = {
// 0xd0: Frame type "Management", subtype "Action" [IEEE 802.11-2020 9.2.4.1.3, p.758]
// See ieee80211_raw_frame_sanity_check - we can only send:
// - Non-QoS Data frames (type == 0b10, subtype == 0b0xxx): first byte 0x[0-7]8
// - Management frames (type == 0b00):
//   - Probe Request (subtype == 0b0100): first byte 0x40
//   - Probe Response (subtype == 0b0101): first byte 0x50
//   - Beacon (subtype == 0b1000): first byte 0x80
//   - Action (subtype == 0b1101): first byte 0xd0
// It seems that it doesn't make any difference what we choose here;
// neither how fast we can send frames, nor how precise the timestamps are
0xd0,
// 0x00: flags
0x00,
// "Duration". Wireshark always sees 0 here no matter what we set; probably overridden by phy
0x00, 0x00,
// dest MAC - broadcast so no ACK is expected
//0xe4, 0xb3, 0x23, 0xc5, 0xc9, 0xc0,
0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
// src MAC - will be overwritten by send function
0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
// BSS ID, according to Wireshark. We put our burst and frame IDs here.
0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
// Sequence number. Phy overrides this.
0x00, 0x00,
// 0x7f: Category code "Vendor-specific" [IEEE 802.11-2020 Table 9-51, p.880]
0x7f,
// Organization Identifier comes next, we don't care [IEEE 802.11-2020 9.6.5 p.1457; IEEE 802.11-2020 9.4.1.31 p.897]

// Following: example FTM frame. Insert at offset 24, so immediately after sequence number.
// 0x04: Category code "Public" [IEEE 802.11-2020 Table 9-51, p.880]
//0x7f,
// 0x21: Public action "FTM" [IEEE 802.11-2020 Table 9-364, p.1463]
//0x21,
// FTM body
//0x0b, 0x0a, 0xf7, 0xfd, 0xb8, 0xcb,
//0x9c, 0xae, 0xaf, 0x84, 0xb9, 0xd2, 0x9c, 0xae,
//0x00, 0x00, 0x00, 0x00, 0xce, 0x09, 0x01, 0x93
};

// ieee80211_raw_frame_sanity_check requires at least 24 bytes per frame.
// This is after sequence number.
#define FRAME_LEN_MIN 24
// burst ID: first 4 bytes of BSS ID
#define BURST_ID_POSITION 16
#define BURST_ID_LENGTH 4
// frame ID: last two bytes of BSS ID. MSBit of frame ID is mode: 1 == sync, 0 == measurement.
#define FRAME_ID_POSITION 20
#define FRAME_ID_LENGTH 2
#define FRAME_ID_SYNCBIT 0x8000
typedef uint16_t frame_id_t;

struct timestamp_record {
	// For non-sync frames, timestamp is in picoseconds and obtained using read_tN()
	// For sync frames, timestamp is result of esp_timer_get_time()
	uint64_t timestamp;
	int frame_id;
	bool is_tx;
};
DEFINE_RINGBUF(struct timestamp_record, my_rb, 2048);
volatile char notify_rb_empty;

// TODO make configurable
char ssid[32] = "830e7259aa7a267d";
const uint8_t our_mac[] = {0x11, 0x22, 0x33, 0x44, 0x55, 0x66};
wifi_interface_t wifi_if = WIFI_IF_STA;
unsigned long wifi_channel = 3;
unsigned long wifi_bandwidth = 3;
unsigned long wifi_encoding_speed = WIFI_PHY_RATE_6M;

enum {
	SCHEDULING_MODE_ASAP,
	SCHEDULING_MODE_PINGPONG,
	SCHEDULING_MODE_BUSYWAIT,
	SCHEDULING_MODE_TIMER,
	SCHEDULING_MODE_TIMER_ISR,
	SCHEDULING_MODE_RTOS,
} burst_scheduling_mode;
uint64_t burst_period;
uint64_t burst_start_time;
uint8_t burst_id_own[BURST_ID_LENGTH];
uint8_t burst_id_peer[BURST_ID_LENGTH];
size_t packet_length;

enum {
	EVT_AP_STARTED = 0x01,
	EVT_STA_CONNECTED = 0x02,
} eventbits;
EventGroupHandle_t eventgroup;
uint8_t ap_bssid[6];

frame_id_t burst_next_frame_id;
int burst_remaining_frames;

void output_results()
{
	if(my_rb_consume_overflow())
		msg("{\"type\": \"error\", \"message\": \"Ringbuffer has overflown! Some data points were lost.\"}");
	while(my_rb_has_element())
	{
		struct timestamp_record record = my_rb_take();
		const char *msgtype = (record.frame_id & FRAME_ID_SYNCBIT) ? "sync" : "timestamp";
		msg("{\"type\": \"%s\", \"direction\": \"%s\", \"frame_id\": %d, \"timestamp\": %llu}",
			msgtype, record.is_tx ? "tx" : "rx", record.frame_id & ~FRAME_ID_SYNCBIT, record.timestamp);
	}
	if(notify_rb_empty) {
		notify_rb_empty = 0;
		msg("{\"type\": \"rbempty\"}");
	}
}

void handle_txrx_event(uint8_t *frame, uint16_t len, char is_tx, uint8_t *burst_id, uint64_t ts)
{
	if(len < BURST_ID_POSITION + BURST_ID_LENGTH || len < FRAME_ID_POSITION + FRAME_ID_LENGTH)
	{
		// too short to be one of our frames, so definitely unrelated; ignore it
		//msg("{\"type\": \"error\", \"message\": \"Too short frame received; that's suspicious: len %d.\"}", len);
		return;
	}
	if(memcmp(frame + BURST_ID_POSITION, burst_id, BURST_ID_LENGTH))
		// unrelated frame; ignore it
		return;

	// this is one of our frames!

	union {
		frame_id_t i;
		uint8_t b[FRAME_ID_LENGTH];
	} frame_id_u;
	for(size_t i = 0; i < FRAME_ID_LENGTH; i ++)
		frame_id_u.b[FRAME_ID_LENGTH - i - 1] = frame[FRAME_ID_POSITION + i];

	struct timestamp_record record;
	record.is_tx = is_tx;
	record.frame_id = frame_id_u.i;
	if(record.frame_id & FRAME_ID_SYNCBIT)
		record.timestamp = esp_timer_get_time();
	else
		record.timestamp = ts;
	my_rb_put_or_overflow(record);
}

void handle_rx_event(uint64_t rx_ts, wifi_promiscuous_pkt_t *pkt)
{
	// TODO Frostie says sig_len isn't exactly the length of what's in the buffer,
	// but rather the hw cuts away some things.
	// Finding out how long the actual buffer is is supposedly more complex than just reading sig_len.
	// Also, once we enable CSI,
	// that will be prepended / inserted between rx_ctrl header and frame data.
	handle_txrx_event(pkt->payload, pkt->rx_ctrl.sig_len, false, burst_id_peer, rx_ts);
}

void handle_tx_event(uint64_t tx_ts, uint8_t *payload, uint16_t payload_len)
{
	// payload_len seems to always be miscounting by 20 bytes
	//TODO why!?
	payload_len += 20;
	handle_txrx_event(payload, payload_len, true, burst_id_own, tx_ts);
}

void event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data) {
	switch(event_id) {
	case WIFI_EVENT_STA_CONNECTED:
		xEventGroupSetBits(eventgroup, EVT_STA_CONNECTED);
		wifi_event_sta_connected_t *sta_event = (wifi_event_sta_connected_t *) event_data;
		memcpy(ap_bssid, sta_event->bssid, sizeof(ap_bssid));
		break;
	case WIFI_EVENT_STA_DISCONNECTED: xEventGroupClearBits(eventgroup, EVT_STA_CONNECTED); break;
	case WIFI_EVENT_AP_START: xEventGroupSetBits  (eventgroup, EVT_AP_STARTED); break;
	case WIFI_EVENT_AP_STOP:  xEventGroupClearBits(eventgroup, EVT_AP_STARTED); break;
	case WIFI_EVENT_FTM_REPORT:
		wifi_event_ftm_report_t *ftm_event = (wifi_event_ftm_report_t *) event_data;
		if(ftm_event->status != FTM_STATUS_SUCCESS) {
			msg("{\"type\": \"errror\", \"message\": \"ftm failed\"}");
			break;
		}
		msg("{\"type\": \"ftmreport\", \"rtt\": %lu, \"dist\": %lu}",
			ftm_event->rtt_est, ftm_event->dist_est);
		free(ftm_event->ftm_report_data);
	default:
		break;
	};
}

/* Enable Wifi (also action frames) on channel provided */
void enable_wifi()
{
	esp_log_level_set("wifi", ESP_LOG_INFO);
	wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
	ESP_ERROR_CHECK(esp_wifi_init(&cfg));
	ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL, NULL));
	ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

	wifi_mode_t wifi_mode;
	wifi_config_t config;
	switch(wifi_if) {
		case WIFI_IF_STA:
			wifi_mode = WIFI_MODE_STA;
			config.sta = (wifi_sta_config_t) {
				.password = "\0",
				.scan_method = WIFI_FAST_SCAN,
				.channel = wifi_channel,
			};
			memcpy(config.sta.ssid, ssid, sizeof(config.sta.ssid));
			break;
		case WIFI_IF_AP:
			wifi_mode = WIFI_MODE_AP;
			config.ap = (wifi_ap_config_t) {
				.password = "\0",
				.ssid_len = 0,
				.channel = wifi_channel,
				.authmode = WIFI_AUTH_OPEN,
				.ssid_hidden = 1,
				.max_connection = 1,
				.ftm_responder = 1,
			};
			memcpy(config.ap.ssid, ssid, sizeof(config.ap.ssid));
			break;
		default:
			msg("{\"type\": \"error\", \"mesage\": \"if neither STA nor AP\"}");
			return;
	};

	ESP_ERROR_CHECK(esp_wifi_set_mode(wifi_mode));
	ESP_ERROR_CHECK(esp_wifi_start());
	ESP_ERROR_CHECK(esp_wifi_set_config(wifi_if, &config));

	// TODO how can BW40 work if we don't set a second channel?
	ESP_ERROR_CHECK(esp_wifi_set_channel(wifi_channel, WIFI_SECOND_CHAN_NONE));
}

/* Enable Promiscous Mode (also Action Frames) */
void enable_promiscous_mode()
{
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

void send_faked_ftm_retrying(uint8_t *burst_id, frame_id_t frame_id, size_t frame_len)
{
	// swap endianness for easier readability in Wireshark dump / similar introspection tools
	union {
		frame_id_t i;
		uint8_t b[FRAME_ID_LENGTH];
	} frame_id_u;
	frame_id_u.i = frame_id;

	// assemble frame
	uint8_t frame[frame_len];
	memset(frame, 0, frame_len);
	memcpy(frame, frame_header, sizeof(frame_header) < frame_len ? sizeof(frame_header) : frame_len);
	memcpy(frame + 10, our_mac, 6);
	memcpy(frame + BURST_ID_POSITION, burst_id, BURST_ID_LENGTH);
	for(size_t i = 0; i < FRAME_ID_LENGTH; i ++)
		frame[FRAME_ID_POSITION + i] = frame_id_u.b[FRAME_ID_LENGTH - i - 1];
	// for easier debuggability in Wireshark: add one byte of BURST_ID to source MAC
	frame[10 + 4] = burst_id[0];

	int err;
	do
		err = esp_wifi_80211_tx(wifi_if, frame, sizeof(frame), true);
	while(err == ESP_ERR_NO_MEM);
	if(err != 0)
		// let's hope esp_err_to_name never contains "
		msg("{\"type\": \"error\", \"message\": \"Transmission Error: %s\"}", esp_err_to_name(err));
}

/* To be called when wifi is fully configured by remote side */
void init_wireless()
{
	enable_wifi();
	sleep(1);

	/* Magic Setting to Force OFDM */
	ESP_ERROR_CHECK(esp_wifi_config_80211_tx_rate(wifi_if, wifi_encoding_speed));
	/* also 40MHz bandwidth */
	esp_wifi_set_bandwidth(wifi_if, wifi_bandwidth);
	enable_promiscous_mode();
	/* Enable RX Timestamping */
	set_rx_cb(handle_rx_event);
	/* Enable TX Timestamping */
	set_tx_cb(handle_tx_event);
}

void output_results_task(void *) {
	for(;;) {
		output_results();
		taskYIELD();
	}
}

void send_burst_frame_retrying() {
	send_faked_ftm_retrying(burst_id_own, burst_next_frame_id, packet_length);
	burst_next_frame_id ++;
	burst_remaining_frames --;
}

// Only used for ASAP, BUSYWAIT, RTOS modes
void burst_task(void *) {
	switch(burst_scheduling_mode) {
	case SCHEDULING_MODE_ASAP:
		// for ASAP, don't even yield
		while(esp_timer_get_time() < burst_start_time);
		while(burst_remaining_frames)
			send_burst_frame_retrying();
		break;
	case SCHEDULING_MODE_BUSYWAIT:
		uint64_t next_burst_time = burst_start_time;
		while(burst_remaining_frames) {
			while(esp_timer_get_time() < next_burst_time)
				taskYIELD();
			send_burst_frame_retrying();
			next_burst_time += burst_period;
		}
		break;
	case SCHEDULING_MODE_RTOS:
		//TODO take Python-side chosen start time into account
		TickType_t last_burst_time = xTaskGetTickCount();
		while(burst_remaining_frames) {
			BaseType_t was_delayed = xTaskDelayUntil(&last_burst_time, pdMS_TO_TICKS(burst_period / 1000));
			if(!was_delayed)
				msg("{\"type\": \"error\", \"message\": \"burst took too long, continuing anyway\"}");
			send_burst_frame_retrying();
		}
		break;
	default:
		msg("{\"type\": \"error\", \"message\": \"invalid state - burst_task active in a scheduling_mode that isn't based on burst_task\"}");
		vTaskDelete(NULL);
	}

	msg("{\"type\": \"burstevent\", \"event\": \"done\"}");
	// we're done now, but we mustn't return or FreeRTOS feels personally offended
	vTaskDelete(NULL);
}

void app_main(void)
{
	int ret;
	char linebuf[128];

	/* Set Main Task priority low, otherwise the watchdog starts complaining about the idle task not getting scheduled */
	vTaskPrioritySet(xTaskGetCurrentTaskHandle(), tskIDLE_PRIORITY);

	eventgroup = xEventGroupCreate();
	if(!eventgroup) {
		msg("{\"type\": \"error\", \"message\": \"event group creation failed\"}");
		return;
	}

	ESP_ERROR_CHECK(nvs_flash_init());
	ESP_ERROR_CHECK(esp_netif_init());
	ESP_ERROR_CHECK(esp_event_loop_create_default());
	// We sometimes deliberately hog the CPU for extended periods of time,
	// so just disable watchdog entirely.
	ESP_ERROR_CHECK(esp_task_wdt_deinit());

	/* Create task responsible for printing outputs */
	TaskHandle_t output_results_task_handle;
	//TODO that task takes more than 1KB of stack space... why!?
	// Maybe switch printf to some more low-level write function
	xTaskCreate(output_results_task, "output_results", 2000, NULL, tskIDLE_PRIORITY, &output_results_task_handle);
	configASSERT(output_results_task_handle);

	esp_reset_reason_t rr = esp_reset_reason();
	char *rr_str;
	switch(rr) {
		#define RRCASE(c) case c: rr_str = #c; break;
		RRCASE(ESP_RST_UNKNOWN)
		RRCASE(ESP_RST_POWERON)
		RRCASE(ESP_RST_EXT)
		RRCASE(ESP_RST_SW)
		RRCASE(ESP_RST_PANIC)
		RRCASE(ESP_RST_INT_WDT)
		RRCASE(ESP_RST_TASK_WDT)
		RRCASE(ESP_RST_WDT)
		RRCASE(ESP_RST_DEEPSLEEP)
		RRCASE(ESP_RST_BROWNOUT)
		RRCASE(ESP_RST_SDIO)
		RRCASE(ESP_RST_USB)
		RRCASE(ESP_RST_JTAG)
		RRCASE(ESP_RST_EFUSE)
		RRCASE(ESP_RST_PWR_GLITCH)
		RRCASE(ESP_RST_CPU_LOCKUP)
		#undef RRCASE
		default: rr_str = "<unknown enum value>"; break;
	};
	printf("reset reason: %d / %s\n", rr, rr_str);
	uint8_t mac[6];
	ESP_ERROR_CHECK(esp_efuse_mac_get_default(mac));
	printf("system id (hopefully): %02x:%02x:%02x:%02x:%02x:%02x\n", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
	printf("time: %lld\n", time(NULL));
	printf("tick period in ms: %ld\n", portTICK_PERIOD_MS);

	// From this point on, every line we send out on serial port is expected to be valid JSON by host side
	serial_setup(0, 0);

	char *end;
	while(1)
	{
		/* Get and Process commands */
		ret = serial_readline(linebuf, sizeof(linebuf));
		if(ret < 0)
		{
			msg("{\"type\": \"error\", \"message\": \"error reading line: %d\"}", ret);
			continue;
		}

		if(!strcmp(linebuf, RESET_COMMAND)) {
			msg("{\"type\": \"reset\"}");
			esp_restart();
		}
		else if(!strcmp(linebuf, SYSTEM_INFO_COMMAND)) {
			msg("{\"type\": \"warning\", \"message\": \"reset reason %d / %s\"}", rr, rr_str);
			msg("{\"type\": \"sysid\", \"sysid\": \"%02x:%02x:%02x:%02x:%02x:%02x\"}", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
		}
		else if(!strncmp(linebuf, SYNC_COMMAND, strlen(SYNC_COMMAND)))
		{
			char *arg = linebuf + strlen(SYNC_COMMAND);
			/* Parsing frame id */
			unsigned long frame_id = strtoul(arg, &end, 0);
			if(arg[0] == 0 || *end != 0 || frame_id > 0xff)
				// breaks if frame_id contains quotation mark or newline,
				// but at that point things are horribly broken anyway
				msg("{\"type\": \"error\", \"message\": \"invalid frame_id: %s\"}", arg);
			else
				send_faked_ftm_retrying(burst_id_own, frame_id | FRAME_ID_SYNCBIT, packet_length);
		}
		else if(!strncmp(linebuf, SET_CHANNEL_COMMAND, strlen(SET_CHANNEL_COMMAND)))
		{
			char *arg = linebuf + strlen(SET_CHANNEL_COMMAND);
			wifi_channel = strtoul(arg, &end, 0);
			if(arg[0] == 0 || *end != 0)
			{
				msg("{\"type\": \"error\", \"message\": \"invalid packet_width: %s\"}", arg);
				continue;
			}
			msg("{\"type\": \"done\"}");
		}
		else if(!strncmp(linebuf, SET_PACKET_LENGTH_COMMAND, strlen(SET_PACKET_LENGTH_COMMAND)))
		{
			char *arg = linebuf + strlen(SET_PACKET_LENGTH_COMMAND);
			unsigned long packet_length_local = strtoul(arg, &end, 0);
			if(arg[0] == 0 || *end != 0)
			{
				msg("{\"type\": \"error\", \"message\": \"invalid packet_length: %s\"}", arg);
				continue;
			}
			else if(packet_length_local < 24)
			{
				msg("{\"type\": \"error\", \"message\": \"too small packet_length: %lu\"}", packet_length_local);
				continue;
			}
			packet_length = packet_length_local;
			msg("{\"type\": \"done\"}");
		}
		else if(!strncmp(linebuf, SET_SSID_COMMAND, strlen(SET_SSID_COMMAND)))
		{
			char *arg = linebuf + strlen(SET_SSID_COMMAND);
			if(strlen(arg) >= sizeof(ssid))
			{
				msg("{\"type\": \"error\", \"message\": \"invalid ssid - too long\"}");
				continue;
			}
			strcpy(ssid, arg);
			msg("{\"type\": \"done\"}");
		}
		else if(!strncmp(linebuf, SET_IF_COMMAND, strlen(SET_IF_COMMAND)))
		{
			char *arg = linebuf + strlen(SET_IF_COMMAND);
			#define ECASE(e) else if(strcmp(arg, #e) == 0) wifi_if = e;
			if(0);
			ECASE(WIFI_IF_STA)
			ECASE(WIFI_IF_AP)
			#undef ECASE
			else
			{
				msg("{\"type\": \"error\", \"message\": \"invalid interfac string: %s\"}", arg);
				continue;
			}
			msg("{\"type\": \"done\"}");
		}
		else if(!strncmp(linebuf, SET_ENCODING_SPEED_COMMAND, strlen(SET_ENCODING_SPEED_COMMAND)))
		{
			char *arg = linebuf + strlen(SET_ENCODING_SPEED_COMMAND);
			#define ECASE(e) else if(strcmp(arg, #e) == 0) wifi_encoding_speed = e;
			if(0);
			ECASE(WIFI_PHY_RATE_1M_L)
			ECASE(WIFI_PHY_RATE_2M_L)
			ECASE(WIFI_PHY_RATE_5M_L)
			ECASE(WIFI_PHY_RATE_11M_L)
			ECASE(WIFI_PHY_RATE_2M_S)
			ECASE(WIFI_PHY_RATE_5M_S)
			ECASE(WIFI_PHY_RATE_11M_S)
			ECASE(WIFI_PHY_RATE_48M)
			ECASE(WIFI_PHY_RATE_24M)
			ECASE(WIFI_PHY_RATE_12M)
			ECASE(WIFI_PHY_RATE_6M)
			ECASE(WIFI_PHY_RATE_54M)
			ECASE(WIFI_PHY_RATE_36M)
			ECASE(WIFI_PHY_RATE_18M)
			ECASE(WIFI_PHY_RATE_9M)
			ECASE(WIFI_PHY_RATE_MCS0_LGI)
			ECASE(WIFI_PHY_RATE_MCS1_LGI)
			ECASE(WIFI_PHY_RATE_MCS2_LGI)
			ECASE(WIFI_PHY_RATE_MCS3_LGI)
			ECASE(WIFI_PHY_RATE_MCS4_LGI)
			ECASE(WIFI_PHY_RATE_MCS5_LGI)
			ECASE(WIFI_PHY_RATE_MCS6_LGI)
			ECASE(WIFI_PHY_RATE_MCS7_LGI)
			// ECASE(WIFI_PHY_RATE_MCS8_LGI)
			// ECASE(WIFI_PHY_RATE_MCS9_LGI)
			ECASE(WIFI_PHY_RATE_MCS0_SGI)
			ECASE(WIFI_PHY_RATE_MCS1_SGI)
			ECASE(WIFI_PHY_RATE_MCS2_SGI)
			ECASE(WIFI_PHY_RATE_MCS3_SGI)
			ECASE(WIFI_PHY_RATE_MCS4_SGI)
			ECASE(WIFI_PHY_RATE_MCS5_SGI)
			ECASE(WIFI_PHY_RATE_MCS6_SGI)
			ECASE(WIFI_PHY_RATE_MCS7_SGI)
			// ECASE(WIFI_PHY_RATE_MCS8_SGI)
			// ECASE(WIFI_PHY_RATE_MCS9_SGI)
			ECASE(WIFI_PHY_RATE_LORA_250K)
			ECASE(WIFI_PHY_RATE_LORA_500K)
			ECASE(WIFI_PHY_RATE_MAX)
			#undef ECASE
			else
			{
				msg("{\"type\": \"error\", \"message\": \"invalid endcoding speed string: %s\"}", arg);
				continue;
			}
			msg("{\"type\": \"done\"}");
		}
		else if(!strncmp(linebuf, SET_BANDWIDTH_COMMAND, strlen(SET_BANDWIDTH_COMMAND)))
		{
			char *arg = linebuf + strlen(SET_BANDWIDTH_COMMAND);
			unsigned int bandwidth = strtoul(arg, &end, 0);
			if(arg[0] == 0 || *end != 0)
			{
				msg("{\"type\": \"error\", \"message\": \"invalid packet_width: %s\"}", arg);
				continue;
			}
			if(bandwidth != 20 && bandwidth != 40)
			{
				msg("{\"type\": \"error\", \"message\": \"invalid bandwidth: %u\"}", bandwidth);
				continue;
			}
			wifi_bandwidth = bandwidth == 20 ? WIFI_BW_HT20 : WIFI_BW_HT40;
			msg("{\"type\": \"done\"}");
		}
		else if(!strcmp(linebuf, START_WIFI_COMMAND))
		{
			init_wireless();
			msg("{\"type\": \"done\"}");
		}
		else if(!strcmp(linebuf, START_AP_COMMAND))
		{
			// init_wireless will already start the ap for us,
			// so there's nothing for us to do except await the start event.
			EventBits_t result = xEventGroupWaitBits(eventgroup,
				EVT_AP_STARTED, pdFALSE, pdFALSE, pdMS_TO_TICKS(5000));
			if(result & EVT_AP_STARTED)
				msg("{\"type\": \"done\"}");
			else
				msg("{\"type\": \"error\", \"message\": \"timeout while waiting for ap start\"}");
		}
		else if(!strcmp(linebuf, CONNECT_COMMAND))
		{
			ESP_ERROR_CHECK(esp_wifi_connect());
			EventBits_t result = xEventGroupWaitBits(eventgroup,
				EVT_STA_CONNECTED, pdFALSE, pdFALSE, pdMS_TO_TICKS(5000));
			if(result & EVT_STA_CONNECTED)
				msg("{\"type\": \"done\"}");
			else
				msg("{\"type\": \"error\", \"message\": \"timeout while waiting for sta connection\"}");
		}
		else if(!strncmp(linebuf, SET_BURST_ID_COMMAND, strlen(SET_BURST_ID_COMMAND)))
		{
			char *arg = linebuf + strlen(SET_BURST_ID_COMMAND);
			uint8_t burst_id[BURST_ID_LENGTH];
			char doit = 1;
			for(size_t i = 0; i < BURST_ID_LENGTH; i ++)
				if(sscanf(arg + 2*i, "%02hhx", burst_id + i) != 1)
				{
					msg("{\"type\": \"error\", \"message\": \"invalid burst_id: %s\"}", arg);
					doit = 0;
					break;
				}
			if(!doit)
				continue;
			memcpy(burst_id_own, burst_id, BURST_ID_LENGTH);
			msg("{\"type\": \"done\"}");
		}
		else if(!strncmp(linebuf, SET_BURST_PEER_COMMAND, strlen(SET_BURST_PEER_COMMAND)))
		{
			char *arg = linebuf + strlen(SET_BURST_PEER_COMMAND);
			uint8_t burst_id[BURST_ID_LENGTH];
			char doit = 1;
			for(size_t i = 0; i < BURST_ID_LENGTH; i ++)
				if(sscanf(arg + 2*i, "%02hhx", burst_id + i) != 1)
				{
					msg("{\"type\": \"error\", \"message\": \"invalid burst_id: %s\"}", arg);
					doit = 0;
					break;
				}
			if(!doit)
				continue;
			memcpy(burst_id_peer, burst_id, BURST_ID_LENGTH);
			msg("{\"type\": \"done\"}");
		}
		else if(!strncmp(linebuf, SET_BURST_SCHEDULING_MODE_COMMAND, strlen(SET_BURST_SCHEDULING_MODE_COMMAND)))
		{
			char *arg = linebuf + strlen(SET_BURST_SCHEDULING_MODE_COMMAND);
			if(0);
			else if(!strcmp(arg, "asap"))     burst_scheduling_mode = SCHEDULING_MODE_ASAP;
			else if(!strcmp(arg, "pingpong")) burst_scheduling_mode = SCHEDULING_MODE_PINGPONG;
			else if(!strcmp(arg, "timer"))    burst_scheduling_mode = SCHEDULING_MODE_TIMER;
			else if(!strcmp(arg, "timerisr")) burst_scheduling_mode = SCHEDULING_MODE_TIMER_ISR;
			else if(!strcmp(arg, "busywait")) burst_scheduling_mode = SCHEDULING_MODE_BUSYWAIT;
			else if(!strcmp(arg, "rtos"))     burst_scheduling_mode = SCHEDULING_MODE_RTOS;
			else {
				msg("{\"type\": \"error\", \"message\": \"invalid burst scheduling mode: %s\"}", arg);
				continue;
			}
			msg("{\"type\": \"done\"}");
		}
		else if(!strncmp(linebuf, SET_BURST_PERIOD_COMMAND, strlen(SET_BURST_PERIOD_COMMAND)))
		{
			char *arg = linebuf + strlen(SET_BURST_PERIOD_COMMAND);
			burst_period = strtoull(arg, &end, 0);
			if(arg[0] == 0 || *end != 0) {
				// breaks if input contains quotation mark or newline,
				// but at that point things are horribly broken anyway
				msg("{\"type\": \"error\", \"message\": \"invalid burst param: %s\"}", arg);
				continue;
			}
			msg("{\"type\": \"done\"}");
		}
		else if(!strncmp(linebuf, BURST_COMMAND, strlen(BURST_COMMAND)))
		{
			char *arg = linebuf + strlen(BURST_COMMAND);
			/* Parsing start */
			burst_start_time = strtoull(arg, &end, 0);
			if(arg[0] == 0 || *end != 0) {
				// breaks if input contains quotation mark or newline,
				// but at that point things are horribly broken anyway
				msg("{\"type\": \"error\", \"message\": \"invalid burst start: %s\"}", arg);
				continue;
			}
			burst_next_frame_id = 0;
			//TODO make configurable
			burst_remaining_frames = 100;
			switch(burst_scheduling_mode) {
			case SCHEDULING_MODE_ASAP:
			case SCHEDULING_MODE_BUSYWAIT:
			case SCHEDULING_MODE_RTOS:
				// we have to print this before actually starting the burst task:
				// it has higher priority than us, so if it doesn't use a FreeRTOS-based delay method,
				// it'll preempt us until it's done, and specifically also has printed the done message
				msg("{\"type\": \"burstevent\", \"event\": \"start\"}");
				TaskHandle_t burst_task_handle;
				xTaskCreate(burst_task, "burst", 3000, NULL, tskIDLE_PRIORITY + 1, &burst_task_handle);
				configASSERT(burst_task_handle);
				break;
			default:
				msg("{\"type\": \"error\", \"message\": \"Unimplemented / unknown scheduling mode\"}");
				break;
			}
		} else if(!strcmp(linebuf, WAIT_RB_EMPTY_COMMAND)) {
			notify_rb_empty = 1;
			// No need to send done message or similar - output_task will send a response soon anyway
		} else if(!strcmp(linebuf, FTM_COMMAND)) {
			//TODO
			wifi_ftm_initiator_cfg_t cfg = {
				.channel = wifi_channel,
			};
			memcpy(cfg.resp_mac, ap_bssid, sizeof(cfg.resp_mac));
			ESP_ERROR_CHECK(esp_wifi_ftm_initiate_session(&cfg));
		} else {
			// breaks if command contains quotation mark or newline,
			// but at that point things are horribly broken anyway
			msg("{\"type\": \"error\", \"message\": \"invalid command: %s\"}", linebuf);
		}
	}
}
