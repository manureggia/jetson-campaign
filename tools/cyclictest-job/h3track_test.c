/*
 * Standalone check of h3track.h, the cyclictest-job h3track job, on any platform.
 * Build: cc -O2 -Wall -o h3track_test h3track_test.c
 * Prints the input hash (must match the "input_hash" in the cyclictest-job JSON),
 * the tracking errors against the ground truth (must be 0) and the time per frame.
 */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#include "h3track.h"

int main(void)
{
	struct h3 t = { 0 };
	struct timespec a, b;
	unsigned long errors, labels, frames = 10 * H3_FRAMES;
	uint32_t f, max = 0;
	double ns;

	t.labels = calloc(H3_LABELS_MAX, sizeof(*t.labels));
	t.offset = calloc(H3_FRAMES + 1, sizeof(*t.offset));
	t.map = calloc(H3_SIZE * H3_SIZE, sizeof(*t.map));
	t.target = calloc(2 * H3_MARKERS, sizeof(*t.target));
	t.prev = calloc(2 * H3_MARKERS, sizeof(*t.prev));
	if (!t.labels || !t.offset || !t.map || !t.target || !t.prev)
		return 1;

	h3_generate(&t);
	labels = t.offset[H3_FRAMES];
	for (f = 0; f < H3_FRAMES; f++)
		if (t.offset[f + 1] - t.offset[f] > max)
			max = t.offset[f + 1] - t.offset[f];
	errors = h3_check(&t, 2);

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (f = 0; f < frames; f++)
		h3_track(&t);
	clock_gettime(CLOCK_MONOTONIC, &b);
	ns = (b.tv_sec - a.tv_sec) * 1e9 + (b.tv_nsec - a.tv_nsec);

	printf("input_hash %016llx\n", (unsigned long long)h3_input_hash(&t));
	printf("labels %lu (%.1f per frame, max %u), input %lu B\n", labels, (double)labels / H3_FRAMES, max,
	       labels * (unsigned long)sizeof(*t.labels));
	printf("check errors %lu\n", errors);
	printf("frames %lu: matched %llu created %llu merged %llu\n", frames, (unsigned long long)t.matched,
	       (unsigned long long)t.created, (unsigned long long)t.merged);
	printf("time per frame %.1f us (hot caches, this machine)\n", ns / frames / 1000);
	return errors || t.created != H3_MARKERS;
}
