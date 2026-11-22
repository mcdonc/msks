/*
 * virtiofsd-as — the owner-exec helper behind msks host shares
 * (#1087, the helper half of #1080 phase 2a).
 *
 * Contract: virtiofsd-as <uid> --fd <n> -- <argv...>
 *
 * The daemon owns every policy decision — which owner a share
 * runs as, which share it serves. This binary is only the
 * privilege boundary: it validates the invocation, drops every
 * privilege but the target uid, and execs one fixed virtiofsd
 * path with one inherited listening-socket fd. A malformed
 * invocation refuses and exits nonzero, and nothing about the
 * caller's environment crosses the exec. The fixed path is
 * compile-time (-DVIRTIOFSD_PATH=...), so a caller cannot point
 * the exec at a binary of its choosing; the default is the
 * location the distro packages install virtiofsd at.
 *
 * Installed setuid-root (root:msksd, mode 4750; the NixOS
 * wrapper name is msks-virtiofsd-as). The rejected designs and
 * their reasons are recorded in #1080: a setuid setpriv copy
 * runs arbitrary argv as any user, a sudoers entry validates by
 * wildcard and drags PAM onto a hot path, and systemd transient
 * units grant polkit power equal to the dumb copy.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <linux/capability.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <syslog.h>
#include <unistd.h>

#ifndef VIRTIOFSD_PATH
#define VIRTIOFSD_PATH "/usr/libexec/virtiofsd"
#endif

/* The environment every exec lands with: one fixed PATH, and
 * nothing inherited — not HOME, not LD_*, nothing the caller
 * set. */
static char *const environment[] = {
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    NULL,
};

static __attribute__((noreturn)) void refuse(const char *message)
{
    fprintf(stderr, "virtiofsd-as: %s\n", message);
    exit(2);
}

static __attribute__((noreturn)) void fail(const char *what)
{
    fprintf(stderr, "virtiofsd-as: %s: %s\n", what, strerror(errno));
    exit(1);
}

/* Decimal digits only — no sign, no whitespace, no empty string.
 * The bound is uid_t: unsigned 32-bit on every Linux the release
 * builds for. The multiply cannot wrap: value stays <= 2^32-1 at
 * each step and unsigned long is 64-bit on those platforms. */
static unsigned long parse_uid(const char *text)
{
    unsigned long value = 0;
    if (*text == '\0')
        refuse("uid: expected a number");
    for (const char *p = text; *p != '\0'; p++) {
        if (*p < '0' || *p > '9')
            refuse("uid: expected a number");
        value = value * 10 + (unsigned long)(*p - '0');
        if (value > 4294967295UL)
            refuse("uid: out of range");
    }
    return value;
}

static int parse_fd(const char *text)
{
    long value = 0;
    if (*text == '\0')
        refuse("fd: expected a number");
    for (const char *p = text; *p != '\0'; p++) {
        if (*p < '0' || *p > '9')
            refuse("fd: expected a number");
        value = value * 10 + (long)(*p - '0');
        if (value > 2147483647L)
            refuse("fd: out of range");
    }
    return (int)value;
}

/* An explicit empty capability set. The kernel already clears
 * permitted/effective/inheritable when the uids leave zero; the
 * explicit capset covers the uid-0 target, where they would
 * otherwise survive the switch. */
static void drop_capabilities(void)
{
    struct __user_cap_header_struct header;
    struct __user_cap_data_struct data[2];
    memset(&header, 0, sizeof(header));
    memset(data, 0, sizeof(data));
    header.version = _LINUX_CAPABILITY_VERSION_3;
    if (syscall(SYS_capset, &header, data) != 0)
        fail("cannot drop capabilities");
}

/* One audit line per exec — to the journal and, mirrored, to
 * stderr (the daemon's spawn log). The passed argv carries the
 * share facts: --shared-dir and --tag ride in it verbatim. */
static void audit(unsigned long uid, int fd, int argc, char **argv)
{
    char line[4096];
    int used = snprintf(line, sizeof(line), "exec uid=%lu fd=%d argv=",
                        uid, fd);
    for (int i = 5; i < argc && used < (int)sizeof(line) - 1; i++)
        used += snprintf(line + used, sizeof(line) - (size_t)used, "%s%s",
                         i > 5 ? " " : "", argv[i]);
    openlog("virtiofsd-as", LOG_PID, LOG_AUTHPRIV);
    syslog(LOG_INFO, "%s", line);
    closelog();
    fprintf(stderr, "virtiofsd-as: audit: %s\n", line);
}

int main(int argc, char **argv)
{
    unsigned long uid;
    int fd;
    struct stat st;
    char **child;

    if (argc < 6 || strcmp(argv[2], "--fd") != 0 ||
        strcmp(argv[4], "--") != 0)
        refuse("usage: virtiofsd-as <uid> --fd <n> -- <argv...>");
    uid = parse_uid(argv[1]);
    fd = parse_fd(argv[3]);
    if (fstat(fd, &st) != 0)
        fail("the listening fd is not open");
    if (!S_ISSOCK(st.st_mode))
        refuse("the fd is not a socket");
    /* The fd must survive the exec even if the caller left the
     * close-on-exec bit set on it. */
    if (fcntl(fd, F_SETFD, 0) != 0)
        fail("cannot clear close-on-exec on the fd");
    if (geteuid() != 0)
        refuse("not running setuid-root "
               "(install root-owned, mode 4750)");

    /* Order is load-bearing: clearing groups needs CAP_SETGID,
     * the bounding-set drop needs CAP_SETPCAP, and both leave
     * with the setresuid — the last privilege the euid-0 exec
     * holds. The gid takes the uid's value: the humans this
     * serves hold user-private groups (uid == gid), and with
     * supplementary groups cleared there is nothing else to
     * restore. */
    if (setgroups(0, NULL) != 0)
        fail("cannot clear supplementary groups");
    if (setresgid((gid_t)uid, (gid_t)uid, (gid_t)uid) != 0)
        fail("cannot set the owner gid");
    for (int cap = 0;; cap++) {
        if (prctl(PR_CAPBSET_DROP, (unsigned long)cap, 0UL, 0UL, 0UL) == 0)
            continue;
        if (errno == EINVAL)
            break; /* past the kernel's last capability */
        fail("cannot drop the capability bounding set");
    }
    if (setresuid((uid_t)uid, (uid_t)uid, (uid_t)uid) != 0)
        fail("cannot become the owner uid");
    drop_capabilities();
    audit(uid, fd, argc, argv);

    /* The exec: the fixed path as argv[0], the passed argv
     * verbatim behind it. */
    child = calloc((size_t)(argc - 4) + 1, sizeof(*child));
    if (child == NULL)
        fail("cannot build the exec argv");
    child[0] = VIRTIOFSD_PATH;
    for (int i = 5; i < argc; i++)
        child[i - 4] = argv[i];
    child[argc - 4] = NULL;
    execve(VIRTIOFSD_PATH, child, environment);
    fail("cannot exec " VIRTIOFSD_PATH);
}
