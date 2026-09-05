/*
 * Narrow privileged capability for the Symphony shared ext4 project-quota
 * pool.  This is deliberately not a command broker: argv contains only one
 * host-derived project/T-N identity and (for admission) the two reviewed
 * policy limits.  The installer gives this exact binary setuid root and
 * restricts its group to the trusted Pilot operator account.
 *
 * The helper uses FS_IOC_FS{GET,SET}XATTR for the directory project ID and
 * inheritance flag, and the generic Q_{GET,SET}QUOTA/PRJQUOTA interface with
 * struct dqblk for kernel limits. It temporarily tightens one limit at a
 * time and performs a bounded write/create probe, restoring the reviewed
 * limits before emitting evidence. A failed restore is fatal.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/fs.h>
#include <linux/quota.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/quota.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>

#define POOL_ROOT "/home/duck-lint/symphony-workspaces"
#define MAX_PROJECT 64
#define MAX_IDENTIFIER 8
#define QUOTA_BLOCK 1024ULL
#define POOL_VERIFY_ID 0U

/* Keep the Linux ext4 project-inheritance xflag explicit and reviewable. */
#ifndef FS_XFLAG_PROJINHERIT
#define FS_XFLAG_PROJINHERIT 0x20000000
#endif

static int parse_identity(const char *project, const char *identifier,
                          uint32_t *project_id) {
    size_t n;
    char *end = NULL;
    unsigned long value;
    if (!project || !identifier ||
        (n = strlen(project)) == 0 || n > MAX_PROJECT ||
        !((project[0] >= 'a' && project[0] <= 'z') ||
          (project[0] >= '0' && project[0] <= '9')) ||
        strlen(identifier) != MAX_IDENTIFIER || identifier[0] != 'T' ||
        identifier[1] != '-') return 0;
    for (size_t i = 0; i < n; ++i)
        if (!((project[i] >= 'a' && project[i] <= 'z') ||
              (project[i] >= '0' && project[i] <= '9') || project[i] == '-')) return 0;
    for (size_t i = 2; i < MAX_IDENTIFIER; ++i)
        if (identifier[i] < '0' || identifier[i] > '9') return 0;
    value = strtoul(identifier + 2, &end, 10);
    if (!end || *end || value > 999999UL) return 0;
    *project_id = (uint32_t)(1000000UL + value);
    return *project_id != 0;
}

static int exact_path(char *out, size_t size, const char *project,
                      const char *identifier) {
    int written = snprintf(out, size, "%s/%s/%s", POOL_ROOT, project, identifier);
    return written > 0 && (size_t)written < size;
}

static int open_task(int pool_fd, const char *project, const char *identifier,
                     uint32_t project_id, int create, char *path, size_t path_size) {
    int project_fd = -1, task_fd = -1;
    struct stat st;
    struct fsxattr attrs;
    if (!exact_path(path, path_size, project, identifier)) return -1;
    if (fstat(pool_fd, &st) || !S_ISDIR(st.st_mode)) goto fail;
    project_fd = openat(pool_fd, project, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (project_fd < 0 && create && errno == ENOENT) {
        if (mkdirat(pool_fd, project, 0700) && errno != EEXIST) goto fail;
        project_fd = openat(pool_fd, project, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    }
    if (project_fd < 0) goto fail;
    if (fstat(project_fd, &st) || !S_ISDIR(st.st_mode)) goto fail;
    task_fd = openat(project_fd, identifier,
                     O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (task_fd < 0 && create && errno == ENOENT) {
        if (mkdirat(project_fd, identifier, 0700) && errno != EEXIST) goto fail;
        task_fd = openat(project_fd, identifier,
                         O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    }
    if (task_fd < 0 || fstat(task_fd, &st) || !S_ISDIR(st.st_mode)) goto fail;
    if (ioctl(task_fd, FS_IOC_FSGETXATTR, &attrs)) goto fail;
    if (attrs.fsx_projid != 0 && attrs.fsx_projid != project_id) {
        errno = EEXIST; goto fail;
    }
    if (attrs.fsx_projid == 0) {
        attrs.fsx_projid = project_id;
    }
    if (!(attrs.fsx_xflags & FS_XFLAG_PROJINHERIT)) {
        /* Preserve unrelated flags while enabling subtree project charging. */
        attrs.fsx_xflags |= FS_XFLAG_PROJINHERIT;
    }
    if (attrs.fsx_projid != project_id ||
        !(attrs.fsx_xflags & FS_XFLAG_PROJINHERIT)) {
        if (ioctl(task_fd, FS_IOC_FSSETXATTR, &attrs)) goto fail;
    }
    if (ioctl(task_fd, FS_IOC_FSGETXATTR, &attrs) ||
        attrs.fsx_projid != project_id ||
        !(attrs.fsx_xflags & FS_XFLAG_PROJINHERIT)) goto fail;
    close(project_fd);
    return task_fd;
fail:
    if (task_fd >= 0) close(task_fd);
    if (project_fd >= 0) close(project_fd);
    return -1;
}

static int quota_control(int operation, int pool_fd, uint32_t id,
                         struct dqblk *quota) {
    return (int)syscall(SYS_quotactl_fd, pool_fd,
                         QCMD(operation, PRJQUOTA), (qid_t)id,
                         (char *)quota);
}

static int quota_get(int pool_fd, uint32_t id, struct dqblk *quota) {
    memset(quota, 0, sizeof(*quota));
    return quota_control(Q_GETQUOTA, pool_fd, id, quota);
}

static int quota_set(int pool_fd, uint32_t id, uint64_t byte_limit,
                     uint64_t inode_limit) {
    struct dqblk quota;
    memset(&quota, 0, sizeof(quota));
    quota.dqb_bhardlimit = (uint64_t)((byte_limit + QUOTA_BLOCK - 1) / QUOTA_BLOCK);
    quota.dqb_ihardlimit = inode_limit;
    quota.dqb_valid = QIF_BLIMITS | QIF_ILIMITS;
    return quota_control(Q_SETQUOTA, pool_fd, id, &quota);
}

static int restore_limits(int pool_fd, uint32_t id, uint64_t bytes,
                          uint64_t inodes) {
    return quota_set(pool_fd, id, bytes, inodes);
}

static int byte_probe(int pool_fd, int task_fd, uint32_t id,
                      struct dqblk *before) {
    char name[] = ".symphony-byte-probe";
    int fd = -1, ok = 0;
    uint64_t temporary = before->dqb_curspace / QUOTA_BLOCK;
    if (temporary == 0) temporary = 1;
    if (quota_set(pool_fd, id, temporary * QUOTA_BLOCK,
                  before->dqb_curinodes + 1)) return 0;
    fd = openat(task_fd, name, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd >= 0) {
        char block[4096] = {0};
        ssize_t written = write(fd, block, sizeof(block));
        ok = written < 0 && errno == EDQUOT;
        close(fd);
    } else ok = errno == EDQUOT;
    unlinkat(task_fd, name, 0);
    if (restore_limits(pool_fd, id, before->dqb_bhardlimit * QUOTA_BLOCK,
                       before->dqb_ihardlimit)) return 0;
    return ok;
}

static int inode_probe(int pool_fd, int task_fd, uint32_t id,
                       struct dqblk *before) {
    char name[] = ".symphony-inode-probe";
    int fd = -1, ok = 0;
    uint64_t temporary = before->dqb_curinodes;
    if (temporary == 0) temporary = 1;
    if (quota_set(pool_fd, id,
                  (before->dqb_curspace / QUOTA_BLOCK + 1) * QUOTA_BLOCK,
                  temporary)) return 0;
    fd = openat(task_fd, name, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0600);
    ok = fd < 0 && errno == EDQUOT;
    if (fd >= 0) { close(fd); unlinkat(task_fd, name, 0); }
    if (restore_limits(pool_fd, id, before->dqb_bhardlimit * QUOTA_BLOCK,
                       before->dqb_ihardlimit)) return 0;
    return ok;
}

static int inheritance_probe(int task_fd, uint32_t project_id) {
    const char name[] = ".symphony-inheritance-probe";
    int child_fd = -1;
    struct fsxattr attrs;
    if (mkdirat(task_fd, name, 0700)) return 0;
    child_fd = openat(task_fd, name,
                      O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (child_fd < 0 || ioctl(child_fd, FS_IOC_FSGETXATTR, &attrs) ||
        attrs.fsx_projid != project_id) {
        if (child_fd >= 0) close(child_fd);
        unlinkat(task_fd, name, AT_REMOVEDIR);
        return 0;
    }
    close(child_fd);
    return unlinkat(task_fd, name, AT_REMOVEDIR) == 0;
}

static int verify_pool(int pool_fd) {
    struct dqblk quota;
    if (geteuid() != 0 || quota_get(pool_fd, POOL_VERIFY_ID, &quota) ||
        !(quota.dqb_valid & (QIF_LIMITS | QIF_USAGE)) ||
        quota_control(Q_SETQUOTA, pool_fd, POOL_VERIFY_ID, &quota) ||
        quota_get(pool_fd, POOL_VERIFY_ID, &quota)) return 3;
    puts("{\"schema\":\"symphony-pilot-pool-quota-proof/v1\",\"quota_type\":\"PRJQUOTA\",\"get_set\":\"verified\"}");
    return 0;
}

static int admit(const char *project, const char *identifier,
                 uint64_t byte_limit, uint64_t inode_limit) {
    uint32_t id;
    char path[256];
    struct dqblk quota;
    int pool_fd = -1, task_fd;
    if (geteuid() != 0 || !parse_identity(project, identifier, &id) ||
        byte_limit != 8ULL * 1024ULL * 1024ULL * 1024ULL || inode_limit != 250000ULL)
        return 2;
    pool_fd = open(POOL_ROOT, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (pool_fd < 0) return 3;
    task_fd = open_task(pool_fd, project, identifier, id, 1, path, sizeof(path));
    if (task_fd < 0 || quota_set(pool_fd, id, byte_limit, inode_limit) ||
        quota_get(pool_fd, id, &quota)) { close(pool_fd); return 3; }
    if (quota.dqb_bhardlimit != (byte_limit / QUOTA_BLOCK) ||
        quota.dqb_ihardlimit != inode_limit) { close(task_fd); close(pool_fd); return 4; }
    struct fsxattr attrs;
    if (ioctl(task_fd, FS_IOC_FSGETXATTR, &attrs) || attrs.fsx_projid != id ||
        !(attrs.fsx_xflags & FS_XFLAG_PROJINHERIT) ||
        !inheritance_probe(task_fd, id) ||
        !byte_probe(pool_fd, task_fd, id, &quota) || quota_get(pool_fd, id, &quota) ||
        !inode_probe(pool_fd, task_fd, id, &quota) || quota_get(pool_fd, id, &quota) ||
        !(quota.dqb_valid & (QIF_LIMITS | QIF_USAGE))) {
        close(task_fd); close(pool_fd); return 5;
    }
    printf("{\"schema\":\"symphony-pilot-task-quota-proof/v1\",\"identifier\":\"%s\",\"workspace_path\":\"%s\",\"project_id\":%u,\"workspace_project_id\":%u,\"workspace_project_inherit\":true,\"inheritance_probe\":{\"attempted\":true,\"result\":\"project-id\"},\"byte_hard_limit\":%llu,\"inode_hard_limit\":%llu,\"usage\":{\"bytes\":%llu,\"inodes\":%llu},\"byte_probe\":{\"attempted\":true,\"result\":\"EDQUOT\"},\"inode_probe\":{\"attempted\":true,\"result\":\"EDQUOT\"}}\n",
           identifier, path, id, id, (unsigned long long)byte_limit,
           (unsigned long long)inode_limit, (unsigned long long)quota.dqb_curspace,
           (unsigned long long)quota.dqb_curinodes);
    close(task_fd); close(pool_fd);
    return 0;
}

static int release_task(const char *project, const char *identifier) {
    uint32_t id;
    char path[256];
    struct stat st;
    struct dqblk quota;
    int root = -1, project_fd = -1;
    if (geteuid() != 0 || !parse_identity(project, identifier, &id) ||
        !exact_path(path, sizeof(path), project, identifier)) return 2;
    root = open(POOL_ROOT, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (root < 0) goto fail;
    project_fd = openat(root, project, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (project_fd < 0 || !fstatat(project_fd, identifier, &st, AT_SYMLINK_NOFOLLOW)) goto fail;
    if (errno != ENOENT || quota_get(root, id, &quota) || quota.dqb_curspace || quota.dqb_curinodes ||
        quota_set(root, id, 0, 0) || quota_get(root, id, &quota) || quota.dqb_curspace || quota.dqb_curinodes) goto fail;
    printf("{\"schema\":\"symphony-pilot-task-quota-release/v1\",\"project\":\"%s\",\"identifier\":\"%s\",\"workspace_path\":\"%s\",\"project_id\":%u,\"workspace_state\":\"destroyed\",\"quota_state\":\"removed\",\"growth_possible\":false,\"remaining_bytes\":0,\"remaining_inodes\":0}\n", project, identifier, path, id);
    close(project_fd); close(root); return 0;
fail:
    if (project_fd >= 0) close(project_fd);
    if (root >= 0) close(root);
    return 3;
}

int main(int argc, char **argv) {
    const char *operation = NULL, *project = NULL, *identifier = NULL;
    uint64_t bytes = 0, inodes = 0;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--operation") && ++i < argc) operation = argv[i];
        else if (!strcmp(argv[i], "--project") && ++i < argc) project = argv[i];
        else if (!strcmp(argv[i], "--identifier") && ++i < argc) identifier = argv[i];
        else if (!strcmp(argv[i], "--byte-limit") && ++i < argc) bytes = strtoull(argv[i], NULL, 10);
        else if (!strcmp(argv[i], "--inode-limit") && ++i < argc) inodes = strtoull(argv[i], NULL, 10);
        else return 2;
    }
    if (!operation) return 2;
    if (!strcmp(operation, "verify-pool") && !project && !identifier &&
        bytes == 0 && inodes == 0) {
        int pool_fd = open(POOL_ROOT, O_PATH | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (pool_fd < 0) return 3;
        int result = verify_pool(pool_fd);
        close(pool_fd);
        return result;
    }
    if (!project || !identifier) return 2;
    if (!strcmp(operation, "admit")) return admit(project, identifier, bytes, inodes);
    if (!strcmp(operation, "release") && bytes == 0 && inodes == 0)
        return release_task(project, identifier);
    return 2;
}
