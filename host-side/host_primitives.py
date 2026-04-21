import serial
from time import sleep
from threading import Thread, Event, Semaphore
from queue import Queue, Empty

# Markers shared with serial.c.
# Only meant for internal use.
FLOWCONTROL_MARKER_START = b"FLOWCONTROLSTART "
FLOWCONTROL_MARKER_END = b"\n"
# Specific messages. Messages with parameters don't contain the trailing space here,
# independently of C file.
MSG_READY   = b"READY"
MSG_WARNING = b"WARNING"
MSG_ACK     = b"ACK"

# Much serial / hardware weirdness ahead:
# RTS / DTR / EN / IO0 are active-LOW, so pyserial's True is logical 0 and False is 1.
# Mostly, RTS is mapped to EN and DTR to IO0.
# However, according to https://dl.espressif.com/dl/schematics/esp32_devkitc_v4-sch-20180607a.pdf,
# RTS=0, DTR=0 is special-cased to map to EN=1, IO0=1.
# (That schematic is for a different board and different ESP,
# but behaviour seems identical for the Seeed board we use.)
# pyserial always sets RTS and DTS to True / logical 0 on connect,
# so we land in exactly that case.
# By first setting RTS to 1, then DTR to 1, we could go to EN=1, IO0=1 in the not-special-cased state
# at the cost of toggling IO0 briefly,
# but that would maybe create problems once pyserial connects the next time.
# So, we don't do that, and instead just use RTS and DTR like the following:
# setting DTR to logical 1 will set EN  to logical 0, and
# setting RTS to logical 1 will set IO0 to logical 0.
# Because of active-LOW, the values we give to pyserial get inverted again,
# so we arrive at:
# setDTR(0) sets EN  to logical 0, and
# setRTS(0) sets IO0 to logical 0.
# We still need to take care to not try to pull both to logical 0 at the same time.
# TODO what is IO0 from ESP point of view!?

# For use with applications not using serial.h
class ESPNoFlowControl:
	def __init__(self, port, do_reset=True, name=None):
		self.serial = serial.Serial(port, baudrate=115200)
		self.name = name
		if do_reset:
			self.reset()

	def debugprint(self, msg):
		# TODO make configurable
		print(f"{'' if self.name is None else f'[{self.name}] '}{msg}")
		pass

	def reset(self):
		# Seems like the ESP resets on 1->0 edge on EN,
		# instead of being kept disabled while EN is held at 0.
		# So, we need to clear buffers _before_ the edge triggers.
		sleep(.1)
		self.serial.reset_input_buffer()
		self.serial.reset_output_buffer()
		self.serial.setDTR(0)
		sleep(.1)
		self.serial.setDTR(1)

	# Briefly pulls IO0 to logical 0, then back to logical 1 again.
	# There's deliberately no functionality to keep IO0 at logical 0 for extended periods
	# as that would conflict with reset functionality - see long comment at beginning of file for details.
	def pulse_IO0(self, duration=.1):
		self.serial.setRTS(0)
		sleep(duration)
		self.serial.setRTS(1)

	# Reads exactly n bytes; waits indefinitely until exactly that many bytes are available.
	def read_n(self, n=1):
		return self.serial.read(n)

	# Reads up to n bytes.
	# If asap is True (default) and at any point bytes are available,
	#  returns immediately without waiting for more bytes,
	#  even if timeout hasn't fully elapsed yet and buffer isn't full yet.
	# If asap is False, waits until timeout elapses (if not None) or buffer is full.
	# So, for example, if asap is False and timeout is None, behaves exactly like read_n.
	def read(self, n=4096, timeout=None, asap=True):
		if asap:
			# override n with 1 so that lib returns as soon as we have even only one byte
			# not sure how closures behave in Python
			n_copy = n
			n = 1
			def asap_wrapper(buf):
				to_read = min(self.serial.in_waiting, n_copy - len(buf))
				return (buf + self.serial.read(to_read)) if to_read > 0 else buf
		else:
			asap_wrapper = lambda buf: buf

		if timeout is None:
			return asap_wrapper(self.serial.read(n))
		self.serial.timeout = timeout
		try:
			return asap_wrapper(self.serial.read(n))
		finally:
			self.serial.timeout = None

	# Write data; waits indefinitely until all bytes have been enqueued at the least.
	def write(self, data):
		return self.serial.write(data)

# For use with applications using serial.h
class ESP:
	def __init__(self, port, do_reset=True, name=None):
		self.quit_event = Event()
		self.ready = Event()
		self.tx_available = Semaphore(0)
		self.rx_exception = None
		self.rx_queue = Queue()
		self.esp_nofc = ESPNoFlowControl(port, do_reset, name)
		self.read_thread = Thread(target=self._read_action_catching)
		self.read_thread.start()

	def debugprint(self, msg):
		self.esp_nofc.debugprint(msg)

	# Internal use only
	def _read_action_catching(self):
		try:
			self._read_action()
		except Exception as x:
			self.rx_exception = x

	# Internal use only
	def _read_action(self):
		buf = b""
		while not self.quit_event.is_set():
			chunk = self.esp_nofc.read(timeout=1)
			# fast path retry to avoid heavy parsing logic if nothing changed
			if not chunk:
				continue
			buf += chunk
			# We could receive multiple messages in one read,
			# so try to parse messages until there are none left.
			while FLOWCONTROL_MARKER_START in buf:
				# split into user data part and message part
				i = buf.index(FLOWCONTROL_MARKER_START)
				self._handle_userdata(buf[:i])
				msg = buf[i+len(FLOWCONTROL_MARKER_START):]
				# read rest of message
				while not FLOWCONTROL_MARKER_END in msg and not self.quit_event.is_set():
					msg += self.esp_nofc.read(timeout=1)
				if self.quit_event.is_set():
					break
				# split into msg and potential trailing data after end; put that back into buf.
				# Careful: don't give to user (yet)! Might be / contain another flowcontrol message.
				i = msg.index(FLOWCONTROL_MARKER_END)
				buf = msg[i+len(FLOWCONTROL_MARKER_END):]
				msg = msg[:i]
				# Handle msg!
				self._handle_flowcontrol_msg(msg)
			# Check for the longest suffix of buf that's a prefix of FLOWCONTROL_MARKER_START,
			# then give all bytes before that to the user.
			# We can start with one byte less than entire marker because the while above would've catched that.
			# Extreme case where no prefix matches, which is equivalent to matching prefix of length 0,
			# is handled correctly too if we don't use negative indices,
			# but rather subtract from len(buf) manually.
			for i in range(len(FLOWCONTROL_MARKER_START)-1, -1, -1):
				if buf.endswith(FLOWCONTROL_MARKER_START[:i]):
					# This is the longest match between prefix of marker / suffix of buf.
					# All bytes before this can go to user,
					# but we need to keep this prefix of marker / suffix of buf of length i.
					self._handle_userdata(buf[:len(buf)-i])
					buf = buf[len(buf)-i:]
					# Don't check for shorter prefixes!
					break

	# Internal use only
	def _handle_userdata(self, data):
		if not data:
			return
		if not self.ready.is_set():
			self.debugprint(f"Ignoring early data: {data}")
			return
		# we need to put bytes individually into queue
		# since a read call might only want one byte at a time
		for b in data:
			self.rx_queue.put(b)

	# Internal use only
	def _handle_flowcontrol_msg(self, msg):
		try:
			split = msg.split(b" ", 1)
			if split[0] == MSG_READY:
				if self.ready.is_set():
					self.debugprint(f"Warning: ESP already sent READY. Flushing tx_available.")
					while self.tx_available.acquire(blocking=False):
						pass
				self.ready.set()
				self.tx_available.release(int(split[1]))
			elif split[0] == MSG_WARNING:
				self.debugprint(f"Warning from ESP: {split[1]}")
			elif split[0] == MSG_ACK:
				self.tx_available.release(int(split[1]))
			else:
				self.debugprint(f"Unparseable flowcontrol msg; probably stream corrupted: {msg}")
		except Exception as x:
			self.debugprint(f"Exception while parsing flowcontrol message; probably stream corrupted: {msg}: {x}")

	# Resets the ESP in some application-defined way, without messing up flow control.
	# Expects func to do the actual reset - that is, at the least serial.h's serial_setup should be called again.
	# Note that func mustn't depend on writing to ESP with flowcontrol -
	# if you need to write, use esp_nofc.write() instead.
	# If wait=True, does wait_until_ready too.
	def custom_reset(self, func, wait=True, wait_timeout=None):
		while self.tx_available.acquire(blocking=False):
			pass
		self.ready.clear()
		self.debugprint("custom reset start")
		func()
		self.debugprint("custom reset done")
		if wait:
			self.wait_until_ready(wait_timeout)

	# Hard resets the ESP. Note: when using this, bootid and uptime can't be determined reliably.
	# If wait=True, does wait_until_ready too.
	def reset(self, wait=True, wait_timeout=None):
		self.custom_reset(self.esp.reset, wait, wait_timeout)

	# Waits until the ESP has initialized the serial port using serial_setup, optionally with timeout.
	# Returns True except if timeout happens before serial port is initilized.
	def wait_until_ready(self, timeout=None):
		return self.ready.wait(timeout)

	# Waits at most timeout seconds until at least one byte is available,
	# then returns as many bytes as are available then.
	def read(self, n=4096, timeout=None):
		if n <= 0:
			return b""
		buf = bytearray()
		try:
			buf.append(self.rx_queue.get(timeout=timeout))
		except Empty:
			pass
		try:
			while len(buf) < n:
				buf.append(self.rx_queue.get(block=False))
		except Empty:
			pass
		return bytes(buf)

	# Reads until needle is found in output. Result will contain needle (unless timed out).
	# If timeout is not None, will abort if for timeout seconds not a single byte has been read;
	#  in that case, will return what has been accumulated so far.
	# If timeout is None (default), waits indefinitely until needle is found.
	# Timeout vs. no timeout can be distinguished by checking if return value ends with needle.
	def read_until(self, needle, timeout=None):
		if type(needle) != bytes:
			raise Exception("read_until's needle must be a bytestring")
		buf = b""
		while True:
			r = self.read(1, timeout)
			if not r:
				return buf
			buf += r
			if buf[-len(needle):] == needle:
				return buf

	# Reads one line (defined as ending in b"\n").
	# For precise meaning of timeout, see read_until.
	# Result will contain the final \n (unless timed out).
	def readline(self, timeout=None):
		return self.read_until(b"\n", timeout)

	# Write data; waits indefinitely until all bytes have been enqueued at the least.
	def write(self, data):
		while data:
			# acquire at least 1 with infinite timeout
			self.tx_available.acquire()
			i = 1
			# now try to acquire as many more as possible.
			# We can't specify a counter to acquire!?
			while len(data) > i:
				if not self.tx_available.acquire(blocking=False):
					break
				i += 1
			# we now have the right to send i bytes.
			# Do that, then go to next loop iteration.
			self.esp_nofc.write(data[:i])
			data = data[i:]

	def quit(self):
		self.quit_event.set()
		self.read_thread.join()
