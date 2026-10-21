/*
 * h3track: the software part of multi-color marker tracking in H3 Vision
 * (Ishii et al., IROS 2009, Sec. IV), run as the cyclictest-job job.
 *
 * In H3 the O(N^2) part (color extraction, labeling of 16 blocks in parallel)
 * runs on the FPGA and hands the PC the zeroth and first moments (m0, mx, my)
 * of every block label. The PC does the O(M) part, implemented here:
 *   1-c merging: a marker across a block boundary has one label per block;
 *       labels near the boundary closer than a threshold are merged;
 *   2   inter-frame correspondence: the label numbers of the previous frame
 *       are embedded at their centroids in a label map, and a square window
 *       around each current centroid is scanned for the nearest one.
 *
 * Input: the moments the FPGA would produce for the paper's experiment,
 * 60 markers on a disc rotating at 26 rps, seen at 1000 fps. The paper does
 * not give the disc geometry; here 5 rings of 12 markers, radii 40-136 px,
 * marker radius 4 px, blocks = 16 horizontal strips of 64 rows. 1000 frames
 * are 26 whole turns, so the input loops without a jump. Generated without
 * libm (positions quantized to 1/16 px) so the input is the same on every
 * platform; h3_input_hash() lets runs compare it.
 */
#include <stdint.h>
#include <string.h>

#define H3_SIZE		1024		/* image side, px */
#define H3_BLOCK_ROWS	64		/* 16 labeling blocks */
#define H3_FPS		1000
#define H3_RPS		26
#define H3_FRAMES	1000		/* 26 turns: frame 1000 == frame 0 */
#define H3_RINGS	5
#define H3_PER_RING	12
#define H3_MARKERS	(H3_RINGS * H3_PER_RING)
#define H3_R0		40		/* inner ring radius, px */
#define H3_DR		24		/* ring spacing, px */
#define H3_BLOB		4		/* marker radius, px */
#define H3_NEAR		(H3_BLOB + 1)	/* centroid of a split half: closer than this to the boundary */
#define H3_MERGE	(2 * H3_BLOB + 1) /* max distance between the two halves of a marker */
/* Max displacement per frame 2*pi*136*26/1000 = 22.2 px, plus centroid rounding */
#define H3_HALF		25		/* window = (2*H3_HALF+1)^2 px */
#define H3_LABELS_MAX	(2 * H3_MARKERS * H3_FRAMES)	/* a marker spans at most 2 blocks */

struct h3_label {		/* one block label, as handed over by the FPGA */
	uint32_t m0, mx, my;
	uint16_t block;
	uint16_t marker;	/* ground truth, read only by h3_check() */
};

struct h3_target {		/* one merged marker of the current frame */
	uint32_t m0, mx, my;
	uint32_t first;		/* index in the frame of its first label */
	uint32_t pos;		/* centroid pixel in the label map */
	uint16_t id;		/* tracking label number */
};

struct h3 {
	struct h3_label *labels;	/* H3_LABELS_MAX, all frames back to back */
	uint32_t *offset;		/* H3_FRAMES + 1: first label of each frame */
	uint16_t *map;			/* H3_SIZE * H3_SIZE label map */
	struct h3_target *target;	/* 2 * H3_MARKERS */
	uint32_t *prev;			/* 2 * H3_MARKERS: map pixels written last frame */
	uint32_t nprev, frame, ntarget;
	uint16_t next_id;
	uint64_t matched, created, merged, sink;
};

/* sin and cos of 2*pi*t without libm (Taylor series on [-pi, pi]) */
static void h3_sincos(double t, double *s, double *c)
{
	double x, x2, ts, tc;
	int k;

	t -= (double)(long)t;
	if (t > 0.5)
		t -= 1.0;
	x = 2 * 3.14159265358979323846 * t;
	x2 = x * x;
	*s = ts = x;
	*c = tc = 1.0;
	for (k = 1; k < 16; k++) {
		ts *= -x2 / ((2 * k) * (2 * k + 1));
		tc *= -x2 / ((2 * k - 1) * (2 * k));
		*s += ts;
		*c += tc;
	}
}

/* Moments of every block label, frame by frame, in block order (as the FPGA scans) */
static void h3_generate(struct h3 *t)
{
	struct h3_label tmp[2 * H3_MARKERS];
	uint32_t f, n = 0;
	int k, b, i, px, py, ntmp;

	for (f = 0; f < H3_FRAMES; f++) {
		t->offset[f] = n;
		ntmp = 0;
		for (k = 0; k < H3_MARKERS; k++) {
			int ring = k / H3_PER_RING, idx = k % H3_PER_RING;
			/* Angle in turns as an exact fraction: marker slot, ring stagger, rotation */
			long num = ((long)idx * 5 + ring) * H3_FPS + (long)H3_RPS * f * H3_PER_RING * 5;
			double s, c, r = H3_R0 + H3_DR * ring;
			int qx, qy, top = -1;

			h3_sincos((double)(num % (H3_PER_RING * 5 * H3_FPS)) / (H3_PER_RING * 5 * H3_FPS), &s, &c);
			qx = (int)((H3_SIZE / 2 + r * c) * 16 + 0.5);
			qy = (int)((H3_SIZE / 2 + r * s) * 16 + 0.5);
			for (py = (qy >> 4) - H3_BLOB - 1; py <= (qy >> 4) + H3_BLOB + 1; py++)
				for (px = (qx >> 4) - H3_BLOB - 1; px <= (qx >> 4) + H3_BLOB + 1; px++) {
					int dx = 16 * px + 8 - qx, dy = 16 * py + 8 - qy;
					struct h3_label *l;

					if (dx * dx + dy * dy > 256 * H3_BLOB * H3_BLOB)
						continue;
					b = py / H3_BLOCK_ROWS;
					if (b != top) {		/* rows go down: a new block opens a new label */
						top = b;
						tmp[ntmp++] = (struct h3_label){ 0, 0, 0, b, k };
					}
					l = &tmp[ntmp - 1];
					l->m0++;
					l->mx += px;
					l->my += py;
				}
		}
		for (b = 0; b < H3_SIZE / H3_BLOCK_ROWS; b++)
			for (i = 0; i < ntmp; i++)
				if (tmp[i].block == b)
					t->labels[n++] = tmp[i];
	}
	t->offset[H3_FRAMES] = n;
}

static void h3_reset(struct h3 *t)
{
	memset(t->map, 0, H3_SIZE * H3_SIZE * sizeof(*t->map));
	t->nprev = t->frame = t->ntarget = 0;
	t->next_id = 1;
	t->matched = t->created = t->merged = t->sink = 0;
}

/* The job: software part of H3 tracking for one frame */
static void h3_track(struct h3 *t)
{
	const struct h3_label *in = t->labels + t->offset[t->frame];
	uint32_t n = t->offset[t->frame + 1] - t->offset[t->frame];
	uint32_t i, j, m = 0, cur = 0, prev_lo = 0, prev_hi = 0, block = UINT32_MAX;

	if (++t->frame == H3_FRAMES)
		t->frame = 0;

	/* 1-c Merging */
	for (i = 0; i < n; i++) {
		const struct h3_label *l = &in[i];
		uint32_t edge;

		if (l->block != block) {
			/* Candidates are the targets of the block just above, if adjacent */
			prev_lo = l->block == block + 1 ? cur : m;
			prev_hi = m;
			cur = m;
			block = l->block;
		}
		edge = block * H3_BLOCK_ROWS;
		/* Centroid near the top edge: maybe the lower half of a split marker */
		if (block && l->my < (edge + H3_NEAR) * l->m0) {
			for (j = prev_lo; j < prev_hi; j++) {
				struct h3_target *p = &t->target[j];
				float dx, dy;

				if (p->my < (edge - H3_NEAR) * p->m0)
					continue;	/* not near the bottom edge */
				dx = (float)l->mx / l->m0 - (float)p->mx / p->m0;
				dy = (float)l->my / l->m0 - (float)p->my / p->m0;
				if (dx * dx + dy * dy <= H3_MERGE * H3_MERGE) {
					p->m0 += l->m0;
					p->mx += l->mx;
					p->my += l->my;
					t->merged++;
					break;
				}
			}
			if (j < prev_hi)
				continue;
		}
		t->target[m++] = (struct h3_target){ l->m0, l->mx, l->my, i, 0, 0 };
	}

	/* 2 Inter-frame correspondence: nearest previous label in the window */
	for (j = 0; j < m; j++) {
		struct h3_target *p = &t->target[j];
		int x = (p->mx + p->m0 / 2) / p->m0, y = (p->my + p->m0 / 2) / p->m0;
		int x0 = x > H3_HALF ? x - H3_HALF : 0, x1 = x < H3_SIZE - 1 - H3_HALF ? x + H3_HALF : H3_SIZE - 1;
		int y0 = y > H3_HALF ? y - H3_HALF : 0, y1 = y < H3_SIZE - 1 - H3_HALF ? y + H3_HALF : H3_SIZE - 1;
		int xx, yy, best = 0, bestd = 1 << 30;

		for (yy = y0; yy <= y1; yy++) {
			const uint16_t *row = t->map + (uint32_t)yy * H3_SIZE;

			for (xx = x0; xx <= x1; xx++)
				if (row[xx]) {
					int d = (xx - x) * (xx - x) + (yy - y) * (yy - y);

					if (d < bestd) {
						bestd = d;
						best = row[xx];
					}
				}
		}
		if (best) {
			t->matched++;
		} else {
			best = t->next_id++;
			if (!t->next_id)
				t->next_id = 1;
			t->created++;
		}
		p->id = best;
		p->pos = (uint32_t)y * H3_SIZE + x;
	}

	/* Label map for the next frame: only the current label numbers */
	for (j = 0; j < t->nprev; j++)
		t->map[t->prev[j]] = 0;
	for (j = 0; j < m; j++) {
		t->map[t->target[j].pos] = t->target[j].id;
		t->prev[j] = t->target[j].pos;
		t->sink += t->target[j].id;
	}
	t->nprev = t->ntarget = m;
}

/*
 * Runs `rounds` passes over the input and counts tracking errors against the
 * ground truth: a marker whose label number changes, or two markers sharing one.
 * Leaves the tracker reset. Not part of the timed job.
 */
static unsigned long h3_check(struct h3 *t, int rounds)
{
	uint16_t id_of[H3_MARKERS] = { 0 };
	unsigned long errors = 0;
	uint32_t f, j, k;

	h3_reset(t);
	static uint16_t owner[1 << 16];

	for (f = 0; f < (uint32_t)rounds * H3_FRAMES; f++) {
		const struct h3_label *in = t->labels + t->offset[t->frame];

		h3_track(t);
		if (t->ntarget != H3_MARKERS)
			errors++;			/* a marker not merged, or two merged */
		for (j = 0; j < t->ntarget; j++) {
			k = in[t->target[j].first].marker;
			if (id_of[k] && id_of[k] != t->target[j].id)
				errors++;
			id_of[k] = t->target[j].id;
			owner[t->target[j].id] = 0;
		}
		for (j = 0; j < t->ntarget; j++) {
			if (owner[t->target[j].id]++)
				errors++;
		}
	}
	h3_reset(t);
	return errors;
}

/* FNV-1a over the generated input, to compare it across platforms */
static uint64_t h3_input_hash(const struct h3 *t)
{
	const unsigned char *p = (const unsigned char *)t->labels;
	uint64_t h = 14695981039346656037ULL;
	size_t i, size = t->offset[H3_FRAMES] * sizeof(*t->labels);

	for (i = 0; i < size; i++)
		h = (h ^ p[i]) * 1099511628211ULL;
	return h;
}
