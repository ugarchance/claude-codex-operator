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
 * The runner is no more powerful than a grant given to the agent CLIs themselves:
 * - PROGRAM must be an absolute path. It is started suspended, and it runs only if the code of that very process is
 *   signed with one of RUNNER_AGENTS ("signing-identifier:team-identifier" pairs; default Claude Code and the Codex
 *   CLI). Otherwise the process is killed before it runs and the runner exits 126.
 * - Environment variables that make a runtime load foreign code (DYLD_*, NODE_OPTIONS, BUN_*, ...) are removed.
 *
 * Build: cc -O2 -Wall -o agent-bridge-runner runner.c -framework Security -framework CoreFoundation
 *        && codesign --force --sign - --options runtime agent-bridge-runner   (hardened: dyld ignores DYLD_* for it)
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

#ifndef RUNNER_AGENTS
#define RUNNER_AGENTS "com.anthropic.claude-code:Q6L2SF6YDW codex:2DC432GLL2" /* Anthropic PBC, OpenAI OpCo */
#endif

extern char **environ;
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

#ifndef RUNNER_ALLOW_ANY
/* Whether code (a file on disk, or a running process when `running`) satisfies one of RUNNER_AGENTS. */
static int satisfies_agents(SecStaticCodeRef code, int running) {
    int ok = 0;
    char agents[] = RUNNER_AGENTS;
    char *save = NULL;
    for (char *pair = strtok_r(agents, " ", &save); pair && !ok; pair = strtok_r(NULL, " ", &save)) {
        char *team = strchr(pair, ':');
        if (!team)
            continue;
        *team++ = '\0';
        CFStringRef text = CFStringCreateWithFormat(
            kCFAllocatorDefault, NULL,
            CFSTR("identifier \"%s\" and anchor apple generic and certificate leaf[subject.OU] = \"%s\""), pair, team);
        SecRequirementRef requirement = NULL;
        if (text && SecRequirementCreateWithString(text, kSecCSDefaultFlags, &requirement) == errSecSuccess) {
            OSStatus status = running ? SecCodeCheckValidity((SecCodeRef)code, kSecCSDefaultFlags, requirement)
                                      : SecStaticCodeCheckValidity(code, kSecCSCheckAllArchitectures, requirement);
            ok = status == errSecSuccess;
            CFRelease(requirement);
        }
        if (text)
            CFRelease(text);
    }
    return ok;
}
#endif

static int file_allowed(const char *path) { /* early, friendly refusal; the process check below decides */
#ifdef RUNNER_ALLOW_ANY
    (void)path;
    return 1;
#else
    int ok = 0;
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(kCFAllocatorDefault, (const UInt8 *)path, (CFIndex)strlen(path), false);
    SecStaticCodeRef code = NULL;
    if (url && SecStaticCodeCreateWithPath(url, kSecCSDefaultFlags, &code) == errSecSuccess) {
        ok = satisfies_agents(code, 0);
        CFRelease(code);
    }
    if (url)
        CFRelease(url);
    return ok;
#endif
}

static int process_allowed(pid_t pid) { /* the code of the suspended child itself: what is checked is what runs */
#ifdef RUNNER_ALLOW_ANY
    (void)pid;
    return 1;
#else
    int ok = 0, value = pid;
    CFNumberRef number = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &value);
    const void *keys[] = {kSecGuestAttributePid};
    const void *values[] = {number};
    CFDictionaryRef attributes = number ? CFDictionaryCreate(kCFAllocatorDefault, keys, values, 1, &kCFTypeDictionaryKeyCallBacks,
                                                             &kCFTypeDictionaryValueCallBacks)
                                        : NULL;
    SecCodeRef code = NULL;
    if (attributes && SecCodeCopyGuestWithAttributes(NULL, attributes, kSecCSDefaultFlags, &code) == errSecSuccess) {
        ok = satisfies_agents((SecStaticCodeRef)code, 1);
        CFRelease(code);
    }
    if (attributes)
        CFRelease(attributes);
    if (number)
        CFRelease(number);
    return ok;
#endif
}

static int loads_foreign_code(const char *entry) {
    static const char *prefixes[] = {"DYLD_", "LD_", "ELECTRON_", "BUN_", "PYTHON", "PERL5", "NODE_OPTIONS=", "NODE_PATH=",
                                     "RUBYOPT="};
    for (size_t i = 0; i < sizeof prefixes / sizeof prefixes[0]; i++)
        if (strncmp(entry, prefixes[i], strlen(prefixes[i])) == 0)
            return 1;
    return 0;
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
    if (!file_allowed(program)) {
        fprintf(stderr, "agent-bridge-runner: %s: not an allowed agent CLI (signing identity)\n", program);
        return 126;
    }
    size_t count = 0;
    while (environ[count])
        count++;
    char **env = calloc(count + 1, sizeof *env);
    if (!env)
        return 1;
    for (size_t i = 0, n = 0; i < count; i++)
        if (!loads_foreign_code(environ[i]))
            env[n++] = environ[i];

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
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_START_SUSPENDED);
    pid_t pid;
    int rc = posix_spawn(&pid, program, NULL, &attr, argv + 1, env);
    posix_spawnattr_destroy(&attr);
    free(env);
    if (rc != 0) {
        fprintf(stderr, "agent-bridge-runner: %s: %s\n", program, strerror(rc));
        return 127;
    }
    int status;
    if (!process_allowed(pid)) {
        kill(pid, SIGKILL);
        waitpid(pid, &status, 0);
        fprintf(stderr, "agent-bridge-runner: %s: the started code is not an allowed agent CLI\n", program);
        return 126;
    }
    child = pid;
    kill(pid, SIGCONT);
    sigprocmask(SIG_SETMASK, &old, NULL);
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
