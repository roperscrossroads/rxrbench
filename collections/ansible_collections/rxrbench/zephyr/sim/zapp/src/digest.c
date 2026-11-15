/*
 * `zapp digest <name>`: the SHA-256 and length of a stored setting, so a bench can check a value
 * the settings shell cannot read back whole (it shows 256 bytes at most), and a secret without
 * the secret crossing the console. Prints one line:
 *
 *     digest <name> <length> <sha256 hex>     or     digest <name> none
 *
 * SHA-256 is written out here (FIPS 180-4) so the app needs no crypto library: the same code
 * builds on every Zephyr the bench runs.
 */
#include <stdint.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/settings/settings.h>
#include <zephyr/shell/shell.h>

#define DIGEST_MAX_VALUE 2048   /* a value as long as one shell command line can write */

struct sha256 {
	uint32_t h[8];
	uint64_t bits;
	uint8_t block[64];
	size_t used;
};

static const uint32_t K[64] = {
	0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
	0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
	0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
	0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
	0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
	0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
	0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
	0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
	0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
	0xc67178f2,
};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha256_block(struct sha256 *s, const uint8_t *p)
{
	uint32_t w[64], a, b, c, d, e, f, g, h;

	for (int i = 0; i < 16; i++) {
		w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 |
		       (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
	}
	for (int i = 16; i < 64; i++) {
		uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
		uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);

		w[i] = w[i - 16] + s0 + w[i - 7] + s1;
	}
	a = s->h[0]; b = s->h[1]; c = s->h[2]; d = s->h[3];
	e = s->h[4]; f = s->h[5]; g = s->h[6]; h = s->h[7];
	for (int i = 0; i < 64; i++) {
		uint32_t t1 = h + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
		uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));

		h = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
	}
	s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d;
	s->h[4] += e; s->h[5] += f; s->h[6] += g; s->h[7] += h;
}

static void sha256(const uint8_t *data, size_t len, uint8_t out[32])
{
	static const uint32_t H0[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
				       0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
	struct sha256 s = {.bits = (uint64_t)len * 8};

	memcpy(s.h, H0, sizeof(H0));
	for (; len >= 64; len -= 64, data += 64) {
		sha256_block(&s, data);
	}
	memcpy(s.block, data, len);
	s.block[len++] = 0x80;
	if (len > 56) {
		memset(&s.block[len], 0, 64 - len);
		sha256_block(&s, s.block);
		len = 0;
	}
	memset(&s.block[len], 0, 56 - len);
	for (int i = 0; i < 8; i++) {
		s.block[56 + i] = (uint8_t)(s.bits >> (56 - 8 * i));
	}
	sha256_block(&s, s.block);
	for (int i = 0; i < 8; i++) {
		out[4 * i] = s.h[i] >> 24; out[4 * i + 1] = s.h[i] >> 16;
		out[4 * i + 2] = s.h[i] >> 8; out[4 * i + 3] = s.h[i];
	}
}

struct digest_read {
	bool found;
	ssize_t len;
	uint8_t value[DIGEST_MAX_VALUE];
};

static struct digest_read found_value;

static int digest_cb(const char *key, size_t len, settings_read_cb read_cb, void *cb_arg,
		     void *param)
{
	struct digest_read *r = param;

	/* the exact name only, as the settings shell's own read */
	if (settings_name_next(key, NULL) != 0) {
		return 0;
	}
	r->found = true;
	r->len = len > sizeof(r->value) ? -EFBIG : read_cb(cb_arg, r->value, len);
	return 0;
}

static int cmd_digest(const struct shell *sh, size_t argc, char **argv)
{
	uint8_t d[32];
	char hex[65];

	found_value.found = false;
	found_value.len = 0;
	int rc = settings_load_subtree_direct(argv[1], digest_cb, &found_value);

	if (rc) {
		shell_error(sh, "digest %s: error %d", argv[1], rc);
		return rc;
	}
	if (!found_value.found) {
		shell_print(sh, "digest %s none", argv[1]);
		return 0;
	}
	if (found_value.len < 0) {
		shell_error(sh, "digest %s: error %d", argv[1], (int)found_value.len);
		return (int)found_value.len;
	}
	sha256(found_value.value, (size_t)found_value.len, d);
	for (int i = 0; i < 32; i++) {
		snprintk(&hex[2 * i], 3, "%02x", d[i]);
	}
	shell_print(sh, "digest %s %d %s", argv[1], (int)found_value.len, hex);
	memset(found_value.value, 0, sizeof(found_value.value));   /* a secret does not linger */
	return 0;
}

SHELL_SUBCMD_ADD((zapp), digest, NULL, "<name>: the SHA-256 and length of a stored setting",
		 cmd_digest, 2, 0);
