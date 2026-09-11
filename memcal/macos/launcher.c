/*
 * memcal.app launcher — the whole reason the bundle exists.
 *
 * launchd runs this binary (inside memcal.app) as the top of the job, so macOS
 * treats memcal.app as the *responsible process* for everything below it. The
 * Calendar work runs through `osascript`, and TCC attributes that Apple Event to
 * the responsible process — which is why the permission prompt and the
 * Privacy > Automation / Calendars lists now read "memcal" instead of the bare
 * `python3.14` / `osascript` this used to surface. The grant is keyed to this
 * binary's code signature, so `brew upgrade python` renaming the interpreter no
 * longer invalidates it.
 *
 * It must stay alive as the parent of the real work: fork the target, wait for
 * it, and exit with its status. If it exec'd the target in place instead, the
 * bundled process would be gone and responsibility could fall back to the shell
 * or interpreter that replaced it.
 *
 * Usage: memcal <program> [args...]  — runs <program> with the given args.
 * <program> is always an absolute path (a pinned python, or the nightly script);
 * this launcher does no PATH lookup on purpose.
 */
#include <stdio.h>
#include <unistd.h>
#include <sys/wait.h>
#include <errno.h>

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "memcal: need a program to run\n");
        return 64; /* EX_USAGE */
    }

    pid_t pid = fork();
    if (pid < 0) {
        perror("memcal: fork");
        return 71; /* EX_OSERR */
    }
    if (pid == 0) {
        execv(argv[1], &argv[1]);
        perror("memcal: exec");
        _exit(127);
    }

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("memcal: waitpid");
            return 1;
        }
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}
