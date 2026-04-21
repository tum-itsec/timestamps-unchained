/* Example usage:

struct datapoint {
	int a;
	size_t b;
};
DEFINE_RINGBUF(struct datapoint, my_rb, 1024);

// called by writer thread
void write_data_point_now(int a, size_t b) {
	struct datapoint newdata = {
		.a = a,
		.b = b,
	};
	// equivalent to: ringbuf_put_or_overflow(my_rb, newdata)
	my_rb_put_or_overflow(newdata);
}

// called by reader thread
void print_accumulated_datapoints_so_far() {
	// equivalent to: ringbuf_consume_overflow(my_rb)
	if(my_rb_consume_overflow())
		printf("Ringbuffer has overflown! Some data points were lost.\n");
	// equivalent to: ringbuf_has_element(my_rb)
	while(my_rb_has_element()) {
		// equivalent to: ringbuf_take(my_rb)
		struct datapoint newdata = my_rb_take();
		printf("datapoint a=%d, b=%zu\n", newdata.a, newdata.b);
	}
}

*/ // end example usage

/**
 * Declares and initializes all symbols necessary for a new ringbuffer.
 * For documentation about how to interact with the ringbuffer, see the ringbuf_xyz macros defined below,
 * but note that their functionality is accessible via the symbols <name>_xyz too.
 *
 * You can't name a ringbuffer "ringbuf" - doing so will lead to incomprehensible errors!
 *
 * The ringbuffer will be full once it contains (capacity-1) elements -
 * in other words, one element slot needs to stay free the entire time.
 *
 * No two readers may work on the same ringbuffer at a time,
 * and the same is true for two writers.
 * However, one (single) reader thread and one (single) writer thread are allowed to work in parallel on the same ringbuffer.
 *
 * If you want to use the same ringbuffer from multiple translation units,
 * use DEFINE_RINGBUF in exactly one translation unit,
 * and DECLARE_RINGBUF in the others.
 * This usually means to put DEFINE_RINGBUF into one .c file and DECLARE_RINGBUF into the corresponding .h file.
 */
#define DEFINE_RINGBUF(elemtype, name, capacity) \
	DECLARE_RINGBUF(elemtype, name) \
	elemtype name##_data[(capacity)]; \
	_Atomic(size_t) name##_start = 0; \
	_Atomic(size_t) name##_end = 0; \
	_Atomic(char) name##_overflow = 0; \
	/* Using sizeof here to avoid evaluating capacity twice */ \
	const size_t name##_capacity = sizeof(name##_data) / sizeof(elemtype); \
	/* We define all ringbuf operations here, inside DEFINE_RINGBUF, for three reasons: */ \
	/* 1. take's implementation depends on elemtype */ \
	/* 2. this way we don't need a separate C file */ \
	/* 3. the user can use these as abbreviations for ringbuf_xyz */ \
	/* Most of these could be macros, but that doesn't work for the nice aliases. */ \
	inline size_t name##_len(void) { \
		return (name##_end - name##_start + name##_capacity) % name##_capacity; \
	} \
	inline char name##_has_space(void) { \
		return (name##_end + 1) % name##_capacity != name##_start; \
	} \
	inline char name##_is_full(void) { \
		return !name##_has_space(); \
	} \
	inline char name##_has_element(void) { \
		return name##_start != name##_end; \
	} \
	inline char name##_is_empty(void) { \
		return !name##_has_element(); \
	} \
	inline void name##_set_overflow(void) { \
		name##_overflow = 1; \
	} \
	inline char name##_consume_overflow(void) { \
		char overflow = name##_overflow; \
		name##_overflow = 0; \
		return overflow; \
	} \
	inline void name##_put(elemtype elem) { \
		/* Same as in put_or_overflow. */ \
	        name##_data[name##_end] = elem; \
        	name##_end = (name##_end + 1) % name##_capacity; \
	} \
	inline char name##_put_or_overflow(elemtype elem) { \
		size_t name##_end_next = (name##_end + 1) % name##_capacity; \
		if(name##_end_next == name##_start) { \
			name##_set_overflow(); \
			return 0; \
		} \
		else { \
			/* It's important that we first copy the entire struct into the ringbuffer before incrementing end. */ \
			/* Otherwise, the reader could racily read our half-written element. */ \
			/* TODO is the memory model strong enough to guarantee no reordering shenanigans between reading / writing elements */ \
			/* and incrementing start and end offsets? */ \
			name##_data[name##_end] = elem; \
			name##_end = name##_end_next; \
			return 1; \
		} \
	} \
	inline elemtype name##_take(void) { \
		/* Same as in put_or_overflow. */ \
		elemtype elem = name##_data[name##_start]; \
		name##_start = (name##_start + 1) % name##_capacity; \
		return elem; \
	}

/**
 * See DEFINE_RINGBUF.
 * Summary: For each ringbuffer, use DEFINE_RINGBUF in exactly one .c file, and DECLARE_RINGBUF in arbitrarily many .h files.
 */
#define DECLARE_RINGBUF(elemtype, name) \
	/* Internals */ \
	extern elemtype name##_data[]; \
	extern _Atomic(size_t) name##_start; \
	extern _Atomic(size_t) name##_end; \
	extern _Atomic(char) name##_overflow; \
	/* Publicly accessible */ \
	extern const size_t name##_capacity; \
	extern inline size_t name##_len(void); \
	extern inline char name##_has_space(void); \
	extern inline char name##_is_full(void); \
	extern inline char name##_has_element(void); \
	extern inline char name##_is_empty(void); \
	extern inline void name##_set_overflow(void); \
	extern inline char name##_consume_overflow(void); \
	extern inline void name##_put(elemtype elem); \
	extern inline char name##_put_or_overflow(elemtype elem); \
	extern inline elemtype name##_take(void);


/*
 * All "ringbuf_xyz" macros defined below have name-specific aliases;
 * they exist maninly for documentation purposes.
 * For example, instead of "ringbuf_is_full(my_rb)", you can write "my_rb_is_full()".
 */

/**
 * Returns this ringbuffer's capacity, in elements. Is constant.
 * Note that the ringbuffer will be full once it contains 1 less elements than this -
 * in other words, one slot always has to stay free.
 *
 * Note: Unlike the other symbols, <name>_capacity is not a function, so don't put () after it.
 */
#define ringbuf_capacity(name) name##_capacity

/**
 * Returns the current number of elements in the ringbuffer.
 * Might decrease if reader is active,
 * and might increase if writer is active.
 * Will always be between 0 (inclusive) and capacity (exclusive).
 */
#define ringbuf_len(name) name##_len()

/**
 * Returns true / non-zero if the ringbuffer currently has space for at least one more element,
 * and false / zero if it does not have space.
 * Note: Might become true if reader is active,
 * and might become false if writer is active.
 */
#define ringbuf_has_space(name) name##_has_space()

/**
 * Inverse of ringbuf_has_space.
 */
#define ringbuf_is_full(name) name##_is_full()

/**
 * Returns true / non-zero if the ringbuffer currently contains at least one element,
 * and false / zero if it does not.
 * Note: Might become false if reader is active,
 * and might become true if writer is active.
 */
#define ringbuf_has_element(name) name##_has_element()

/**
 * Inverse of ringbuf_has_element.
 */
#define ringbuf_is_empty(name) name##_is_empty()

/**
 * For use by writer only. Sets the overflow bit to true.
 * Not needed when using ringbuf_put_or_overflow.
 */
#define ringbuf_set_overflow(name) name##_set_overflow()

/**
 * For use by reader only. Clears overflow bit to false, and returns previous value.
 */
#define ringbuf_consume_overflow(name) name##_consume_overflow()

/**
 * For use by writer only. Writer must check beforehand that ringbuf_has_space is true!
 */
#define ringbuf_put(name, elem) name##_put(elem)

/**
 * For use by writer only. No manual ringbuf_has_space check necessary.
 * If the ringbuf is not full, the element gets inserted, and this function returns 1.
 * If the ringbuf is full, its overflow bit becomes set, and this function returns 0.
 */
#define ringbuf_put_or_overflow(name, elem) name##_put_or_overflow(elem)

/**
 * For use by reader only. Reader must check beforehand that ringbuf_has_element is true!
 */
#define ringbuf_take(name) name##_take()
