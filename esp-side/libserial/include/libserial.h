// Problem: Communication in host -> ESP direction doesn't have built-in flow control.
// Solution: Host knows receive buffer size of ESP and will only send that many bytes;
// ESP actively acks number of received bytes, which allows the host to send more bytes.

// Warning: None of the functions defined here are threadsafe!
// At the least, you must not do several read calls simulatenously.
// You should also avoid printing output while a read call is on the way.
// TODO figure out how threadsafe printf is.

/*
 * Pass 0 as bufsize_rx and bufsize_tx to use sensible default.
 *
 * Both bufsize_rx and bufsize_tx will internally be made at least 65 bytes large,
 * as required by ESP32 SDK / library.
 * Handles in-band flow control initialization.
 */
void serial_setup(size_t bufsize_rx, size_t bufsize_tx);

/*
 * Reads up to size bytes into given buffer.
 * Blocks indefinitely until at least one byte has been read.
 * Returns how many bytes have been read, or errors as usb_serial_jtag_read_bytes.
 * Handles in-band flow control notification.
 */
int serial_read(char *buf, size_t size);

/*
 * Reads until '\n' or nullbyte is encountered, or buffer is full, or error occurs.
 * Blocks indefinitely until one of these conditions is met.
 * Returns:
 *  - -4 if size is 0
 *  - -3 if underlying serial port reports error. Already received bytes will be in the buffer; buffer will be nullterminated.
 *  - -2 if nullbyte is received. Already received bytes will be in the buffer; buffer will include the received nullbyte.
 *  - -1 if buffer becomes full before '\n' was received. Buffer still gets properly nullterminated in this case.
 *  - some positive integer if complete line was received. Buffer gets nullterminated, and terminating '\n' is not included. strlen of returned line (without '\n' or nullbyte) is returned.
 * Handles in-band flow control notification.
 */
int serial_readline(char *buf, size_t size);

// Deliberately no methods for sending exist - use regular printf / puts / write on stdout.
// Your output must not contain FLOWCONTROL_MARKER_START as defined in sniffer.c!
// Also, if at any point a suffix of your output so far is a prefix of FLOWCONTROL_MARKER_START,
// this suffix won't be made accessible to the host side until you send more data.
// You can circumvent this by doing line-based communication and always sending entire lines -
// then, your output will always end in '\n', which is not a prefix of FLOWCONTROL_MARKER_START.
// (Unless you sent FLOWCONTROL_MARKER_START itself, which you should not have done anyway.)

// We don't provide non-standard methods for writing
// since we need to handle the lib / rom printing weird stuff we can't parse anyway.
