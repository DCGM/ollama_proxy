#$ -V
#$ -S /bin/bash
######!/bin/bash
set -euo pipefail

echo Running SGE job with TASK_ID=${SGE_TASK_ID:-0} on host $(hostname) at $(date) >&2

export LC_ALL=C
export LANG=C

### ---------------------------
### Configuration
### ---------------------------

MIN_PORT=24000
PORT_RANGE=150

REMOTE_USER="ihradis"
REMOTE_HOST="pchradis2"

SMALL_MODEL="all-minilm:22m" 
MODEL="gemma4:26b"
JOB_TIMEOUT="15m"
PRELOAD_KEEP_ALIVE="15m"

# Optional: set your model cache
export OLLAMA_MODELS="/mnt/matylda0/ihradis/ollama_cache"

# How many seconds to wait for Ollama HTTP API to come up
OLLAMA_START_TIMEOUT=60

# Lock directory only your jobs use
LOCK_DIR="/tmp/${USER}/ollama-gpu-locks"

mkdir -p "$LOCK_DIR"

sleep $(( RANDOM % 15 ))  # Stagger job startups to reduce contention

### ---------------------------
### Derived values
### ---------------------------

TASK_ID="${SGE_TASK_ID:-0}"
PORT=$((MIN_PORT + TASK_ID % PORT_RANGE))

export OLLAMA_HOST="127.0.0.1:${PORT}"

SSH_PID=""
OLLAMA_PID=""
LOCK_FD=""
CUDA_DEV=""

### ---------------------------
### Cleanup
### ---------------------------

cleanup() {
  set +e

  if [[ -n "${OLLAMA_PID}" ]]; then
    kill "${OLLAMA_PID}" 2>/dev/null || true
    wait "${OLLAMA_PID}" 2>/dev/null || true
  fi

  if [[ -n "${SSH_PID}" ]]; then
    kill "${SSH_PID}" 2>/dev/null || true
    wait "${SSH_PID}" 2>/dev/null || true
  fi

  if [[ -n "${LOCK_FD}" ]]; then
    eval "exec ${LOCK_FD}>&-"
  fi
}
trap cleanup EXIT INT TERM

### ---------------------------
### Open reverse tunnel
### ---------------------------
#
# Keep ssh in foreground but backgrounded by shell, so we have its PID.
# This makes cleanup easier than using ssh -f.

ssh -N \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=10 \
  -o ServerAliveCountMax=3 \
  -R "127.0.0.1:${PORT}:127.0.0.1:${PORT}" \
  "${REMOTE_USER}@${REMOTE_HOST}" &
SSH_PID=$!

sleep 1
if ! kill -0 "${SSH_PID}" 2>/dev/null; then
  echo "ERROR: reverse tunnel failed to start" >&2
  exit 1
fi
echo "Opened reverse tunnel on port ${PORT}" >&2


### ---------------------------
### Pick a GPU
### ---------------------------
#
# Strategy:
# - iterate over GPU indices
# - skip GPUs that currently show compute processes
# - try to acquire a per-GPU flock
# - use first GPU satisfying both
#
# This is only "good enough" coordination.
# It does NOT protect against other users who ignore these locks.

choose_gpu() {
  local line idx uuid lockfile fd

  NVIDIA_PROCESS_FILE="/tmp/${USER}/nvidia_processes.csv"
  nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader | tee "$NVIDIA_PROCESS_FILE" >&2 

  while IFS=',' read -r idx uuid; do
    idx="$(echo "$idx" | xargs)"
    uuid="$(echo "$uuid" | xargs)"

    echo "Checking GPU index=${idx} uuid=${uuid}" >&2
    # Skip GPU if nvidia-smi reports an active compute process on it
    if grep -q "^${uuid}" "$NVIDIA_PROCESS_FILE"; then
      continue
    fi

    lockfile="${LOCK_DIR}/gpu-${uuid}.lock"

    exec {fd}>"${lockfile}" || continue
    if flock -n "${fd}"; then
      CUDA_DEV="${idx}"
      LOCK_FD="${fd}"
      echo "Selected GPU index=${idx} uuid=${uuid}" >&2
      return 0
    else
      eval "exec ${fd}>&-"
    fi
  done < <(nvidia-smi --query-gpu=index,gpu_uuid --format=csv,noheader)

  return 1
}

if ! choose_gpu; then
  echo "ERROR: no apparently free GPU found" >&2
  exit 1
fi

export CUDA_VISIBLE_DEVICES="${CUDA_DEV}"
nvidia-smi >&2


### ---------------------------
### Start Ollama
### ---------------------------

ollama serve &
OLLAMA_PID=$!

sleep 0.5
if ! kill -0 "${OLLAMA_PID}" 2>/dev/null; then
  echo "ERROR: ollama serve exited immediately" >&2
  exit 1
fi

### ---------------------------
### Wait for Ollama API
### ---------------------------

deadline=$((SECONDS + OLLAMA_START_TIMEOUT))
until curl -sf "http://127.0.0.1:${PORT}/api/tags" >/dev/null; do
  if ! kill -0 "${OLLAMA_PID}" 2>/dev/null; then
    echo "ERROR: ollama serve died before becoming ready" >&2
    exit 1
  fi

  if (( SECONDS >= deadline )); then
    echo "ERROR: timeout waiting for Ollama API on port ${PORT}" >&2
    exit 1
  fi

  sleep 0.1
done

echo "Ollama API ready on ${OLLAMA_HOST}" >&2

### ---------------------------
### Preload model
### ---------------------------
#
# If this fails, exit nonzero.
# That prevents advertising a backend that cannot actually run.

#if ! curl -sf "http://127.0.0.1:${PORT}/api/embed" \
#  -H 'Content-Type: application/json' \
#  -d "{
#    \"model\": \"${SMALL_MODEL}\",
#    \"input\": [ \"First sentence\" ]
#  }" >/dev/null; then
#  echo "ERROR: model preload failed for ${SMALL_MODEL}" >&2
#  exit 1
#fi
#echo "Small model ${SMALL_MODEL} preloaded successfully" >&2


if ! curl -sf "http://127.0.0.1:${PORT}/api/generate" \
  -H 'Content-Type: application/json' \
  -d "{
    \"model\": \"${MODEL}\",
    \"prompt\": \"\",
    \"stream\": false,
    \"keep_alive\": \"${PRELOAD_KEEP_ALIVE}\"
  }" >/dev/null; then
  echo "ERROR: model preload failed for ${MODEL}" >&2
  exit 1
fi
echo "Model ${MODEL} preloaded successfully at $(date)" >&2


### ---------------------------
### Run until timeout or exit
### ---------------------------

timeout "${JOB_TIMEOUT}" bash -c '
  while kill -0 "'"${OLLAMA_PID}"'" 2>/dev/null && kill -0 "'"${SSH_PID}"'" 2>/dev/null; do
    sleep 5
  done
'

# If timeout exits, cleanup trap handles shutdown
exit 0