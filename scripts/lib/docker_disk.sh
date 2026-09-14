#!/usr/bin/env bash
# Docker's disk is a VM allocation, not the host's free space. Sourced by k8s_up.sh and
# run_all_tests.sh.
#
# WHY THIS EXISTS, AND WHY THE OBVIOUS CHECK IS THE WRONG ONE. A run filled the Docker disk and
# broke the kind cluster with "No space left on device" while the host reported 650 GB free. The
# host was never the constraint: this machine runs Colima, whose VM disk is a FIXED allocation
# (~/.colima/default/colima.yaml: `disk: 30`), and /dev/vdb1 inside the VM is what fills. Docker
# Desktop behaves the same way for the same reason. So `df` on the host answers a question nobody
# asked, and answering it confidently is worse than not checking at all -- it reads as an
# all-clear.
#
# The failure it prevents is badly disguised: the disk fills while a kind node is starting
# Postgres, and what surfaces is an initdb crash inside the node. Nothing in that message mentions
# disk, so the next hour goes into the database.

# Docker's own view of its storage, in GB, or "" when it cannot be determined.
# Deliberately `docker system df` and not a host `df`: it is the daemon's own accounting, so it is
# right for Docker Desktop, Colima and a plain Linux daemon alike.
docker_disk_used_gb() {
  docker system df --format '{{.Size}}' 2>/dev/null | head -1 | sed 's/[^0-9.]//g'
}

docker_disk_reclaimable_gb() {
  docker system df --format '{{.Reclaimable}}' 2>/dev/null | head -1 | sed 's/ .*//; s/[^0-9.]//g'
}

# Free GB on the filesystem backing Docker's data root. Reads it from INSIDE the VM when one is in
# use, which is the only place the number is real.
docker_disk_free_gb() {
  if command -v colima >/dev/null 2>&1 && colima status >/dev/null 2>&1; then
    colima ssh -- df -BG /var/lib/docker 2>/dev/null \
      | awk 'NR==2 {gsub(/G/,"",$4); print $4}'
    return
  fi
  # Docker Desktop and native Linux: ask the daemon where its data lives, then measure that.
  local root
  root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
  [ -n "$root" ] && [ -d "$root" ] || return 0
  df -g "$root" 2>/dev/null | awk 'NR==2 {print $4}'
}

# Refuse to start when there is not enough room, and name the REAL cause. Same stance
# smoke_compose.sh takes on a port conflict: a run that cannot succeed should say so in one line
# rather than fail deep inside a container ten minutes later.
#   require_docker_disk <needed_gb> <what_for>
require_docker_disk() {
  local needed="$1" what="${2:-this run}" free
  free="$(docker_disk_free_gb)"
  if [ -z "$free" ]; then
    printf '  \033[33mnote\033[0m  could not read Docker'\''s free disk; continuing unchecked\n'
    return 0
  fi
  if [ "$free" -lt "$needed" ]; then
    printf '  \033[31mfail\033[0m  Docker has %sGB free, %s needs ~%sGB\n' "$free" "$what" "$needed"
    printf '        This is the DOCKER VM'\''s disk, not the host'\''s -- host free space is not\n'
    printf '        the number that matters here. Reclaimable now: %sGB of %sGB in images.\n' \
      "$(docker_disk_reclaimable_gb)" "$(docker_disk_used_gb)"
    printf '        Free it with:  docker image prune -f   (dangling layers only)\n'
    printf '                       docker system prune -f  (also stopped containers/networks)\n'
    if command -v colima >/dev/null 2>&1 && colima status >/dev/null 2>&1; then
      printf '        Or grow the VM: colima stop && colima start --disk 60\n'
    fi
    return 1
  fi
  printf '  \033[32mok\033[0m    Docker disk: %sGB free\n' "$free"
  return 0
}

# Drop dangling layers. CALL ONLY AFTER A SUCCESSFUL RUN.
#
# After a FAILURE those layers are the evidence -- the half-built image is what you inspect to
# find out why the build broke, and pruning it turns a diagnosable failure into a mystery. That is
# the whole reason this is a separate function rather than a trap.
prune_dangling_images() {
  local before after
  before="$(docker_disk_used_gb)"
  docker image prune -f >/dev/null 2>&1 || return 0
  after="$(docker_disk_used_gb)"
  printf '  \033[32mok\033[0m    pruned dangling layers (images %sGB -> %sGB)\n' "$before" "$after"
}
