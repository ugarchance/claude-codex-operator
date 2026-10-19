/* agent-bridge-runner: a stable macOS privacy identity for the agent CLIs.
 *
 * macOS ties a Full Disk Access grant for a command-line tool to the tool's path. Claude Code installs every update
 * under a new versioned path, so a grant given to `claude` lapses with the next update. launchd starts this runner as
 * the job's own (responsible) process and the agent runs as its child, so the agent is judged by the runner's grant,
 * which stays valid as long as this binary is not rebuilt.
 *
 *   agent-bridge-runner PROGRAM [ARGS...]   run PROGRAM, forward SIGTERM/SIGINT/SIGHUP, exit the way it exited
 *   agent-bridge-runner --check             exit 0 when this process holds Full Disk Access, 1 when it does not
 *
 * The runner is no more powerful than a grant given to the agent CLIs themselves: PROGRAM must be an absolute path to
 * a binary signed by one of RUNNER_TEAMS (Developer ID team identifiers; default Anthropic and OpenAI), checked on
 * the resolved file before it is started. Exit 126 when it is not.
 *
 * Build: cc -O2 -Wall -o agent-bridge-runner runner.c -framework Security -framework CoreFoundation
 *        && codesign --force --sign - agent-bridge-runner
 * Tests build with -DRUNNER_ALLOW_ANY (a separate binary that never holds a grant).
 */
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef RUNNER_TEAMS
#define RUNNER_TEAMS "Q6L2SF6YDW 2DC432GLL2" /* Anthropic PBC, OpenAI OpCo */
#endif

extern char **environ;

static int signed_by_allowed_team(const char *path) {
#ifdef RUNNER_ALLOW_ANY
    (void)path;
    return 1;
#else
    int ok = 0;
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(kCFAllocatorDefault, (const UInt8 *)path, (CFIndex)strlen(path), false);
    SecStaticCodeRef code = NULL;
    if (url && SecStaticCodeCreateWithPath(url, kSecCSDefaultFlags, &code) == errSecSuccess) {
        char teams[] = RUNNER_TEAMS;
        char *save = NULL;
        for (char *team = strtok_r(teams, " ", &save); team && !ok; team = strtok_r(NULL, " ", &save)) {
            CFStringRef text = CFStringCreateWithFormat(kCFAllocatorDefault, NULL,
                                                        CFSTR("anchor apple generic and certificate leaf[subject.OU] = \"%s\""), team);
            SecRequirementRef requirement = NULL;
            if (text && SecRequirementCreateWithString(text, kSecCSDefaultFlags, &requirement) == errSecSuccess) {
                ok = SecStaticCodeCheckValidity(code, kSecCSCheckAllArchitectures, requirement) == errSecSuccess;
                CFRelease(requirement);
            }
            if (text)
                CFRelease(text);
        }
        CFRelease(code);
    }
    if (url)
        CFRelease(url);
    return ok;
#endif
}
static volatile pid_t child = 0;

static void forward(int sig) {
    if (child > 0)
        kill(child, sig);
}

static int check(void) {
    /* readable only with Full Disk Access; without it open() fails at once with EPERM, no prompt is shown */
    const char *home = getenv("HOME");
    char paths[3][1024];
    snprintf(paths[0], sizeof paths[0], "/Library/Application Support/com.apple.TCC/TCC.db");
    snprintf(paths[1], sizeof paths[1], "%s/Library/Safari", home ? home : "/var/empty");
    snprintf(paths[2], sizeof paths[2], "%s/Library/Mail", home ? home : "/var/empty");
    int denied = 0;
    for (int i = 0; i < 3; i++) {
        int fd = open(paths[i], O_RDONLY);
        if (fd >= 0) {
            close(fd);
            return 0;
        }
        if (errno == EPERM || errno == EACCES)
            denied = 1;
    }
    fprintf(stderr, "agent-bridge-runner: %s\n", denied ? "no Full Disk Access" : "cannot tell (no protected file found)");
    return denied ? 1 : 2;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--check") == 0)
        return check();
    if (argc < 2) {
        fprintf(stderr, "usage: agent-bridge-runner PROGRAM [ARGS...] | --check\n");
        return 64;
    }
    char program[PATH_MAX];
    if (argv[1][0] != '/' || !realpath(argv[1], program)) {
        fprintf(stderr, "agent-bridge-runner: %s: needs the absolute path of an existing program\n", argv[1]);
        return 127;
    }
    if (!signed_by_allowed_team(program)) {
        fprintf(stderr, "agent-bridge-runner: %s: not signed by an allowed developer team\n", program);
        return 126;
    }
    const int sigs[] = {SIGTERM, SIGINT, SIGHUP};
    sigset_t block, old, defaults;
    sigemptyset(&block);
    sigemptyset(&defaults);
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward;
    sigemptyset(&sa.sa_mask);
    for (size_t i = 0; i < sizeof sigs / sizeof sigs[0]; i++) {
        sigaddset(&block, sigs[i]);
        sigaddset(&defaults, sigs[i]);
        sigaction(sigs[i], &sa, NULL);
    }
    /* a signal that arrives before the child's pid is known is delivered once it is */
    sigprocmask(SIG_BLOCK, &block, &old);
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    posix_spawnattr_setsigmask(&attr, &old);
    posix_spawnattr_setsigdefault(&attr, &defaults);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);
    pid_t pid;
    int rc = posix_spawn(&pid, program, NULL, &attr, argv + 1, environ);
    posix_spawnattr_destroy(&attr);
    if (rc != 0) {
        fprintf(stderr, "agent-bridge-runner: %s: %s\n", program, strerror(rc));
        return 127;
    }
    child = pid;
    sigprocmask(SIG_SETMASK, &old, NULL);
    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("agent-bridge-runner: waitpid");
            return 1;
        }
    }
    if (WIFEXITED(status))
        return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) { /* end by the same signal, so launchd reports what ended the agent */
        int sig = WTERMSIG(status);
        signal(sig, SIG_DFL);
        sigset_t only;
        sigemptyset(&only);
        sigaddset(&only, sig);
        sigprocmask(SIG_UNBLOCK, &only, NULL);
        kill(getpid(), sig);
    }
    return 1;
}
