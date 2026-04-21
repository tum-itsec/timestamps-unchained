import subprocess
import os
import time
import select

TIMEOUT_TO_SYNC = 10
# TODO don't hardcode number of messages here
TIMEOUT_AFTER_SYNC_FUNC = lambda t: (t * 100 + 10)
BASE_COMMAND = os.path.join(os.path.dirname(__file__), "generic_data_capture.py")

def init_search_state(needle, func):
	# can't use a tuple since those are immutable
	return [b"", needle, func]

def update_search_state(state, buf):
	prev, needle, func = state
	buf = prev + buf
	if needle in buf:
		func()
		return True
	state[0] = buf[len(needle) - 1:]

def copy_ready_streams(non_eof_streams, timeout):
	while non_eof_streams:
		read_ready, _, _ = select.select(non_eof_streams.keys(), [], [], timeout)
		if not read_ready:
			break
		for fd in read_ready:
			i, o, search_states = non_eof_streams[fd]
			buf = i.read1()
			if not buf:
				non_eof_streams.pop(fd)
			done_search_states = []
			for s in search_states:
				if update_search_state(s, buf):
					done_search_states.append(s)
			for s in done_search_states:
				search_states.remove(s)
			o.write(buf)
		# for future iterations, don't use timeout - just see which streams are available immediately
		timeout = 0

def msmt(esp_a_path, esp_b_path, data_dir, R, D, C, E, B, S, T, L, msmt_name = lambda: time.strftime("%y-%m-%d_%H-%M-%S")):
	if callable(msmt_name):
		msmt_name = msmt_name()
	while True:
		datafile = lambda ext: os.path.join(data_dir, msmt_name + "." + ext)
		if not os.path.exists(datafile("csv")):
			break
		print("Too fast! Waiting to not overwrite existing data...")
		time.sleep(1)
	with open(datafile("out"), "wb") as out, open(datafile("err"), "wb") as err:
		print(f"Doing measurement {msmt_name}: E={E}, B={B}, S={S}, T={T}, L={L}...")
		# Using Popen instead of just importing and calling functions:
		# firstly because we would need to restructure that script,
		# secondly because this way it's easier to enforce a timeout,
		# and thirdly because we can be sure that no threads / fds / similar things are still running / open / active.
		env = dict(os.environ)
		env["PYTHONUNBUFFERED"] = "True"
		output_filepath = datafile("csv")
		p = subprocess.Popen([BASE_COMMAND,
				esp_a_path, esp_b_path,
				"-o", output_filepath,
				"-R", str(R), "-D", str(D), "-C", str(C),
				"-E", str(E), "-B", str(B), "-S", str(S), "-T", str(T), "-L", str(L),
			],
			stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
		end_time = [time.monotonic() + TIMEOUT_TO_SYNC]
		stdout_search_states = []
		def found_sync():
			end_time[0] = time.monotonic() + TIMEOUT_AFTER_SYNC_FUNC(T)
		stdout_search_states.append(init_search_state(b"Sync successful!", found_sync))
		non_eof_streams = {p.stdout.fileno(): (p.stdout, out, stdout_search_states), p.stderr.fileno(): (p.stderr, err, [])}
		running = True
		while running:
			remaining = end_time[0] - time.monotonic()
			if remaining < 0:
				print("Timed out! Killing measurement process...")
				p.terminate()
				p.wait(1)
				p.kill()
				p.wait()
			exitcode = p.poll()
			running = exitcode is None
			# If the process is not running anymore, do a final "grace" timeout of 1 second for output to appear.
			# That timeout will very likely not matter at all since stdout and stderr should be closed by now
			# as long as things haven't gone horribly wrong.
			# TODO just directly let stdout and stderr point to these files...
			copy_ready_streams(non_eof_streams, remaining if running else 1)
		if exitcode == 0:
			print("Measurement finished successfully")
			return output_filepath
		else:
			print(f"Measurement FAILED: {exitcode}")
			return None
