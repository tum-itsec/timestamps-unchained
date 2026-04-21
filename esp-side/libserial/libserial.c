#include <stddef.h>
// for printf. TODO do we want to use a more low-level primitive for sending our stuff?
#include <stdio.h>

#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"

#include "libserial.h"

#define DEFAULT_BUFSIZE_RX 1024
#define DEFAULT_BUFSIZE_TX DEFAULT_BUFSIZE_RX

// Markers shared with host_primitives.py.
// We just assume / hope the user never sends these.
#define FLOWCONTROL_MARKER_START "FLOWCONTROLSTART "
// End deliberately just newline so that if system message intervenes and prints some garbage,
// parsing isn't stuck forever.
#define FLOWCONTROL_MARKER_END "\n"
// Specific messages. Messages with parameters contain their trailing space here.
#define MSG_READY   "READY "
#define MSG_WARNING "WARNING "
#define MSG_ACK     "ACK "

#define FC_MSG(msg, ...) printf(FLOWCONTROL_MARKER_START msg FLOWCONTROL_MARKER_END, ## __VA_ARGS__)

size_t serial_received_bytes;

void serial_setup(size_t bufsize_rx, size_t bufsize_tx) {
	serial_received_bytes = 0;

	if(bufsize_rx < 65)
		bufsize_rx = bufsize_rx == 0 ? DEFAULT_BUFSIZE_RX : 65;
	if(bufsize_tx < 65)
		bufsize_tx = bufsize_tx == 0 ? DEFAULT_BUFSIZE_TX : 65;

	// setup serial driver
	usb_serial_jtag_driver_config_t usb_serial_jtag_config = {
		.rx_buffer_size = bufsize_rx,
		.tx_buffer_size = bufsize_tx,
	};
	ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb_serial_jtag_config));

	// don't do weird converting between '\n' and '\r'
	usb_serial_jtag_vfs_set_rx_line_endings(ESP_LINE_ENDINGS_LF);
	usb_serial_jtag_vfs_set_tx_line_endings(ESP_LINE_ENDINGS_LF);

	// regular printf and related functions don't sidestep serial driver
	usb_serial_jtag_vfs_use_driver();

	FC_MSG(MSG_READY "%d", bufsize_rx);
}

/*
 * If necessary, send flow control ACK message to host.
 */
void serial_send_fc_ack() {
	// For now, just send ACK message if any number of un-acked bytes exist,
	// even if it's just 1.
	// We could think about only sending ACKs for 32 bytes or more.
	if(serial_received_bytes) {
		FC_MSG(MSG_ACK "%d", serial_received_bytes);
		serial_received_bytes = 0;
	}
}

/*
 * Mostly same as serial_read except for timeout handling.
 * fc_ack == 0: update tracked received bytes count, but don't send flow control ACK message to host
 * fc_ack == 1: update tracked received bytes count and also send flow control ACK message to host
 */
int serial_read_fc_timeout(char *buf, size_t size, char fc_ack, char allow_unflushed, TickType_t timeout) {
	// Should never happend; sanity check to avoid deadlock bugs.
	if(!allow_unflushed && serial_received_bytes != 0)
		FC_MSG(MSG_WARNING "serial_read called while fc_ack doesn't seem flushed");

	// Judging from experiments, it seems that this function has exactly the right timeout behaviour:
	// read as many as currently available, and block until there's at least 1.
	// There doesn't seem to be a way to specify infinite timeout.
	int ret = usb_serial_jtag_read_bytes(buf, size, timeout);
	if(ret > 0) {
		serial_received_bytes += ret;
		if(fc_ack)
			serial_send_fc_ack();
	}
	return ret;
}

int serial_read_fc(char *buf, size_t size, char fc_ack) {
	for(;;) {
		//TODO after verifying that nothing bad happens if this timeout happens, increase this timeout.
		int ret = serial_read_fc_timeout(buf, size, fc_ack, 0, 20 / portTICK_PERIOD_MS);
		if(ret != 0)
			return ret;
	}
}

int serial_read(char *buf, size_t size) {
	return serial_read_fc(buf, size, 1);
}

int serial_readline(char *buf, size_t size) {
	if(size == 0)
		return -4;
	if(size == 1) {
		buf[0] = 0;
		return -1;
	}

	size_t i = 0;
	char try_fast_path = 1;
	for(;;) {
		int ret;
		if(try_fast_path)
			ret = serial_read_fc_timeout(&buf[i], 1, 0, 1, 0);
		else
			ret = serial_read_fc(&buf[i], 1, 0);
		if(ret == 0) {
			// Timeout. Ack, then fall back to slow path.
			serial_send_fc_ack();
			try_fast_path = 0;
			continue;
		}
		if(ret != 1) {
			// either error, or neither 0 nor 1 bytes read - something is wrong.
			buf[i] = 0;
			serial_send_fc_ack();
			return -3;
		}

		// If we get here, we have read exactly one byte.

		if(buf[i] == 0) {
			serial_send_fc_ack();
			return -2;
		}
		if(buf[i] == '\n') {
			buf[i] = 0;
			serial_send_fc_ack();
			return i;
		}

		// If we get here, a byte was read which doesn't terminate the line.
		// Try fast path again next, to avoid having to ack individual bytes.
		i++;
		try_fast_path = 1;
	}
}
