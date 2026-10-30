/*
 * linkloop.c — VZ virtiofs hardlink reproducer.
 *
 * This is the reproducer from the patch comment in
 * https://github.com/podman-container-tools/podman/discussions/23886
 * (Docker Desktop's private fs/fuse patch, "[PATCH 9/9] hardlinks:
 * drop the cache of the existing entry", Feb 2024).  The body is
 * verbatim; the original's missing #include lines are restored
 * and the two signatures modernized (`static int one(void)`,
 * `int main(void)`) so modern C compilers accept it.  Build and
 * run with the working directory on the virtiofs mount under
 * test:
 *
 *   gcc -O2 -o linkloop linkloop.c
 *   mkdir -p work && cd work && ../linkloop
 *
 * It prints one dot per clean iteration and stops at the first
 * failure it detects — `link()` returning -1, `a` disappearing,
 * or `b/b` reporting ENOENT — naming which check failed.  A
 * transient failure prints its evidence and keeps going after
 * the in-source retry loop recovers; a persistent one spins in
 * that loop forever, so an outer `timeout` kill is the expected
 * end of a buggy run — read stderr, not the exit code.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/sendfile.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static int one(void)
{
    int ret;
    struct stat st;
    ret = stat("b/b", &st);
    if (ret == 0) {
        unlink("b/b");
    }
    if (!stat("b/b", &st)){
        fprintf(stderr, "b/b exists\n");
        return 0;
    }
    if (stat("a", &st)){
        fprintf(stderr, "a does not exist\n");
        return 0;
    }
    struct timespec begin, end;
    clock_gettime(CLOCK_MONOTONIC_RAW, &begin);
    ret = link("a", "b/b");
    if(ret == -1){
        perror("link");
        if (stat("a", &st)){
            fprintf(stderr, "a does not exist\n");
        } else {
            fprintf(stderr, "a exists\n");
        }
        if (stat("b", &st)){
            fprintf(stderr, "b does not exist\n");
        } else {
            fprintf(stderr, "b exists\n");
        }
        if (stat("b/b", &st)){
            fprintf(stderr, "b/b does not exist\n");
        } else {
            fprintf(stderr, "b/b exists\n");
        }
        int retries = 0;
        do {
            ret = link("a", "b/b");
            if (ret == 0) {
                break;
            }
            retries++;
        } while (1);
        clock_gettime(CLOCK_MONOTONIC_RAW, &end);
        double time_spent = (end.tv_nsec - begin.tv_nsec) / 1000000000.0 + (end.tv_sec  - begin.tv_sec);
        fprintf(stderr, "link succeeded after %d retries in %f seconds\n", retries, time_spent);
        return 0;
    }
    int fd = open("b/b", O_RDONLY);
    if(fd == -1){
        perror("open");
        return 0;
    }
    sendfile(1, fd, NULL, 16777216);
    close(fd);

    ret = unlink("b/b");
    if(ret == -1){
        perror("unlink");
        return 0;
    }
    return 1;
}

int main(void)
{
    for (int i = 0;; i++){
        if (!one()){
            fprintf(stdout, "\nFailed on iteration %d\n", i);
            break;
        };
        fprintf(stdout, ".");
        fflush(stdout);
    }
    return 1;
}
