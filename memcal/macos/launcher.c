/* Keep the app-bundle process alive while scheduled work runs beneath it.
 * Usage: memcal <absolute-program> [args...]
 *
 * Do not simplify this to execv in place. macOS attributes Apple Events and
 * Calendar access to the responsible parent bundle process, so the launcher must
 * remain alive as the parent (fork + wait): exec would hand attribution back to
 * the interpreter or terminal that started it.
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>
#include <errno.h>

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "memcal: need a program to run\n");
        return 64; /* EX_USAGE */
    }

    /* Tell everything beneath us it is already running under the bundle, so a memcal
     * that would otherwise re-exec itself through the app to gain this identity knows
     * it already has it and does not loop. Inherited across the fork below. */
    setenv("MEMCAL_APP", "1", 1);

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
