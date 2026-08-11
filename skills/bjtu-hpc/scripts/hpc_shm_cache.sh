# BJTU HPC /dev/shm packed-dataset cache helpers.
#
# Source this file from an sbatch script before training:
#
#   source /data/home/<account>/projects/<project_slug>/code/hpc_shm_cache.sh
#   export SHM_SHARED_ACL_USERS="<cluster_account_a>,<cluster_account_b>"
#   bjtu_stage_packed_to_shm "$PACKED_DATA_ROOT" "$DATASET_NAME" PACKED_DATA_ROOT
#
# The helpers are best-effort: ACL or staging failures log a warning and fall
# back to the account-local packed root instead of failing the training job.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "hpc_shm_cache.sh is a shell library; source it from an sbatch script." >&2
  exit 2
fi

bjtu_shm_log() {
  local event="$1"
  shift || true
  printf 'shm_stage=%s' "$event"
  local item
  for item in "$@"; do
    printf ' %s' "$item"
  done
  printf '\n'
}

bjtu_shm_acl_user_words() {
  printf '%s\n' "${SHM_SHARED_ACL_USERS:-}" | tr ',:' '  '
}

bjtu_shm_cache_key() {
  local raw="${1:-dataset}"
  raw="${raw#/}"
  raw="${raw//\//_}"
  raw="${raw// /_}"
  raw="${raw//[^A-Za-z0-9._-]/_}"
  printf '%s\n' "${raw:-dataset}"
}

bjtu_shm_node_name() {
  hostname -s 2>/dev/null || hostname 2>/dev/null || printf 'unknown\n'
}

bjtu_shm_export_data_path() {
  local path="$1"
  local var_name="${2:-PACKED_DATA_ROOT}"
  printf -v "$var_name" '%s' "$path"
  export "$var_name"
  export DATA_ROOT="$path"
  export DATA_PACKED_ROOT="$path"
}

bjtu_shm_apply_shared_acl() {
  local path="$1"
  local mode="${2:-recursive}"
  local user acl_spec="" default_acl_spec=""

  [[ -n "${SHM_SHARED_ACL_USERS:-}" && -d "$path" ]] || return 0
  if ! command -v setfacl >/dev/null 2>&1; then
    bjtu_shm_log warning "reason=setfacl_missing" "path=$path"
    return 0
  fi

  for user in $(bjtu_shm_acl_user_words); do
    if [[ "$user" =~ ^[A-Za-z0-9._-]+$ ]]; then
      acl_spec="${acl_spec:+$acl_spec,}u:${user}:rwx"
      default_acl_spec="${default_acl_spec:+$default_acl_spec,}d:u:${user}:rwx"
    else
      bjtu_shm_log warning "reason=invalid_acl_user" "user=$user"
    fi
  done
  [[ -n "$acl_spec" ]] || return 0

  if [[ "$mode" == "recursive" ]]; then
    setfacl -R -m "${acl_spec},m:rwx" "$path" 2>/dev/null || \
      bjtu_shm_log warning "reason=setfacl_apply_failed" "path=$path"
    find "$path" -type d -exec setfacl -m "${default_acl_spec},d:m:rwx" {} + 2>/dev/null || \
      bjtu_shm_log warning "reason=setfacl_default_failed" "path=$path"
  else
    setfacl -m "${acl_spec},m:rwx,${default_acl_spec},d:m:rwx" "$path" 2>/dev/null || \
      bjtu_shm_log warning "reason=setfacl_apply_failed" "path=$path"
  fi
}

bjtu_shm_init_root() {
  local root="${1:-/dev/shm/bjtu_dataset_cache}"
  local lock_dir="$root/.locks"

  case "$root" in
    /dev/shm/*) ;;
    *)
      bjtu_shm_log warning "reason=invalid_cache_root" "root=$root"
      return 1
      ;;
  esac

  if ! mkdir -p "$root" "$lock_dir"; then
    bjtu_shm_log warning "reason=mkdir_failed" "root=$root"
    return 1
  fi
  chmod 1777 "$root" "$lock_dir" 2>/dev/null || \
    bjtu_shm_log warning "reason=chmod_1777_failed" "root=$root"
  bjtu_shm_apply_shared_acl "$root" shallow
  bjtu_shm_apply_shared_acl "$lock_dir" shallow
}

bjtu_shm_cache_ready() {
  local dest="$1"
  [[ -f "$dest/.ready" ]] || return 1
  [[ -n "$(find "$dest" -mindepth 1 ! -name .ready ! -name .source_manifest.sha256 ! -name '.cache_*' -print -quit 2>/dev/null)" ]] || return 1
  [[ -f "$dest/manifest.json" ]] || bjtu_shm_log warning "reason=missing_manifest" "staged=$dest"
  [[ -f "$dest/validation_report.json" ]] || bjtu_shm_log warning "reason=missing_validation_report" "staged=$dest"
}

bjtu_stage_packed_to_shm() {
  local src="$1"
  local key
  key="$(bjtu_shm_cache_key "${2:-$(basename "$src")}")"
  local var_name="${3:-PACKED_DATA_ROOT}"
  local root="${SHM_CACHE_ROOT:-/dev/shm/bjtu_dataset_cache}"
  local dest="$root/$key"
  local lock_dir="$root/.locks"
  local lock="$lock_dir/$key.lock"
  local min_free="${MIN_SHM_FREE_BYTES:-21474836480}"
  local max_pct="${MAX_SHM_STAGE_PCT:-70}"
  local strict="${SHM_STRICT_CACHE_CHECK:-0}"
  local node_name="${SHM_NODE_NAME:-$(bjtu_shm_node_name)}"
  local src_bytes shm_total shm_avail shm_limit tmp lock_fd
  local src_manifest_sha staged_manifest_sha

  [[ -e "$src" ]] || {
    bjtu_shm_log skipped "node=$node_name" "dataset_key=$key" "reason=missing_source" "src=$src"
    return 0
  }

  umask 0002
  if ! bjtu_shm_init_root "$root"; then
    bjtu_shm_export_data_path "$src" "$var_name"
    return 0
  fi

  if bjtu_shm_cache_ready "$dest"; then
    if [[ -f "$src/manifest.json" ]]; then
      src_manifest_sha="$(sha256sum "$src/manifest.json" | awk '{print $1}')"
      staged_manifest_sha="$(cat "$dest/.source_manifest.sha256" 2>/dev/null || true)"
      if [[ "$src_manifest_sha" != "$staged_manifest_sha" ]]; then
        bjtu_shm_log warning "reason=manifest_sha_mismatch" "staged=$dest"
        [[ "$strict" != "1" ]] || {
          bjtu_shm_export_data_path "$src" "$var_name"
          return 0
        }
      fi
    fi
    bjtu_shm_apply_shared_acl "$dest" recursive
    bjtu_shm_export_data_path "$dest" "$var_name"
    bjtu_shm_log reused "node=$node_name" "dataset_key=$key" "staged=$dest"
    return 0
  fi

  if ! exec {lock_fd}>"$lock"; then
    bjtu_shm_log skipped "node=$node_name" "dataset_key=$key" "reason=lock_open_failed" "lock=$lock"
    bjtu_shm_export_data_path "$src" "$var_name"
    return 0
  fi
  if ! flock "$lock_fd"; then
    bjtu_shm_log skipped "node=$node_name" "dataset_key=$key" "reason=lock_failed" "lock=$lock"
    exec {lock_fd}>&-
    bjtu_shm_export_data_path "$src" "$var_name"
    return 0
  fi

  if bjtu_shm_cache_ready "$dest"; then
    bjtu_shm_apply_shared_acl "$dest" recursive
    bjtu_shm_export_data_path "$dest" "$var_name"
    bjtu_shm_log reused "node=$node_name" "dataset_key=$key" "staged=$dest"
    exec {lock_fd}>&-
    return 0
  fi

  if ! src_bytes="$(du -sb "$src" | awk '{print $1}')"; then
    bjtu_shm_log skipped "node=$node_name" "dataset_key=$key" "reason=du_failed" "src=$src"
    exec {lock_fd}>&-
    bjtu_shm_export_data_path "$src" "$var_name"
    return 0
  fi
  read -r shm_total shm_avail < <(df -B1 --output=size,avail /dev/shm | awk 'NR==2{print $1, $2}')
  shm_limit=$(( shm_total * max_pct / 100 ))
  if (( src_bytes > shm_limit || shm_avail <= src_bytes + min_free )); then
    bjtu_shm_log skipped "node=$node_name" "dataset_key=$key" "reason=capacity" "src_bytes=$src_bytes" "shm_avail=$shm_avail" "shm_limit=$shm_limit"
    exec {lock_fd}>&-
    bjtu_shm_export_data_path "$src" "$var_name"
    return 0
  fi

  tmp="${dest}.copying.${SLURM_JOB_ID:-manual}.${SLURM_ARRAY_TASK_ID:-0}.$$"
  rm -rf "$tmp" 2>/dev/null || true
  if ! mkdir -p "$tmp"; then
    bjtu_shm_log skipped "node=$node_name" "dataset_key=$key" "reason=tmp_mkdir_failed" "tmp=$tmp"
    exec {lock_fd}>&-
    bjtu_shm_export_data_path "$src" "$var_name"
    return 0
  fi
  bjtu_shm_apply_shared_acl "$tmp" shallow

  if [[ -d "$src" ]]; then
    if ! cp -a "$src"/. "$tmp"/; then
      bjtu_shm_log skipped "node=$node_name" "dataset_key=$key" "reason=copy_failed" "src=$src" "tmp=$tmp"
      rm -rf "$tmp" 2>/dev/null || true
      exec {lock_fd}>&-
      bjtu_shm_export_data_path "$src" "$var_name"
      return 0
    fi
  else
    if ! cp -a "$src" "$tmp"/; then
      bjtu_shm_log skipped "node=$node_name" "dataset_key=$key" "reason=copy_failed" "src=$src" "tmp=$tmp"
      rm -rf "$tmp" 2>/dev/null || true
      exec {lock_fd}>&-
      bjtu_shm_export_data_path "$src" "$var_name"
      return 0
    fi
  fi

  if [[ -f "$src/manifest.json" ]]; then
    sha256sum "$src/manifest.json" | awk '{print $1}' > "$tmp/.source_manifest.sha256" || true
  fi
  printf '%s\n' "$node_name" > "$tmp/.cache_node" || true
  printf '%s\n' "$key" > "$tmp/.cache_key" || true
  date -Is > "$tmp/.cache_created_at" 2>/dev/null || true
  bjtu_shm_apply_shared_acl "$tmp" recursive
  touch "$tmp/.ready" || true
  bjtu_shm_apply_shared_acl "$tmp" recursive

  if [[ -e "$dest" ]] && ! rm -rf "$dest"; then
    bjtu_shm_log skipped "node=$node_name" "dataset_key=$key" "reason=replace_failed" "staged=$dest"
    rm -rf "$tmp" 2>/dev/null || true
    exec {lock_fd}>&-
    bjtu_shm_export_data_path "$src" "$var_name"
    return 0
  fi
  if ! mv "$tmp" "$dest"; then
    bjtu_shm_log skipped "node=$node_name" "dataset_key=$key" "reason=rename_failed" "tmp=$tmp" "staged=$dest"
    rm -rf "$tmp" 2>/dev/null || true
    exec {lock_fd}>&-
    bjtu_shm_export_data_path "$src" "$var_name"
    return 0
  fi
  bjtu_shm_apply_shared_acl "$dest" recursive
  bjtu_shm_export_data_path "$dest" "$var_name"
  bjtu_shm_log enabled "node=$node_name" "dataset_key=$key" "src_bytes=$src_bytes" "shm_avail_before=$shm_avail" "staged=$dest"
  exec {lock_fd}>&-
}

# Strict external-HDF5 API.  This is additive: legacy packed callers above keep
# their historical behavior until the project explicitly enables external mode.
bjtu_shm_sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    LC_ALL=C LANG=C shasum -a 256 "$path" | awk '{print $1}'
  else
    openssl dgst -sha256 "$path" | awk '{print $NF}'
  fi
}

bjtu_shm_validate_external_source() {
  local src="$1"
  local artifact_hash="$2"
  local consumer_hash="$3"
  local ready_path="$4"
  python3 - "$src" "$artifact_hash" "$consumer_hash" "$ready_path" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
artifact_hash, consumer_hash = sys.argv[2:4]
ready_path = pathlib.Path(sys.argv[4]).resolve()

def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SystemExit(9)
        result[key] = value
    return result

def load(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=no_duplicates)

def canonical(value):
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

def canonical_sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

manifest_path = root / "DATA_ARTIFACT_MANIFEST.json"
complete_path = root / "ARTIFACT_COMPLETE.json"
if not manifest_path.is_file() or not complete_path.is_file() or not ready_path.is_file():
    raise SystemExit(10)
if root not in ready_path.parents:
    raise SystemExit(10)
manifest, complete, ready = load(manifest_path), load(complete_path), load(ready_path)
identity = {
    "schema": manifest.get("schema"),
    "artifact_schema": manifest.get("artifact_schema"),
    "dataset_semantics_sha256": manifest.get("dataset_semantics_sha256"),
    "source_inventory_sha256": manifest.get("source_inventory_sha256"),
    "artifact_build_contract_sha256": manifest.get("artifact_build_contract_sha256"),
    "converter_bundle_sha256": manifest.get("converter_bundle_sha256"),
    "shards": manifest.get("shards"),
}
if manifest.get("schema") != "autoreskill.data_artifact_manifest.v1":
    raise SystemExit(11)
if manifest.get("artifact_schema") != "image_bytes_indexed_v1":
    raise SystemExit(11)
if manifest.get("artifact_content_sha256") != artifact_hash or canonical_sha(identity) != artifact_hash:
    raise SystemExit(11)
if complete.get("schema") != "autoreskill.artifact_complete.v1" or complete.get("artifact_content_sha256") != artifact_hash:
    raise SystemExit(12)
if complete.get("manifest_sha256") != sha(manifest_path):
    raise SystemExit(13)
if ready.get("schema") != "autoreskill.data_validation_attestation.v1" or ready.get("artifact_content_sha256") != artifact_hash:
    raise SystemExit(14)
if ready.get("consumer_contract_sha256") != consumer_hash:
    raise SystemExit(15)
hash_fields = (
    artifact_hash,
    consumer_hash,
    manifest.get("dataset_semantics_sha256"),
    manifest.get("source_inventory_sha256"),
    manifest.get("artifact_build_contract_sha256"),
    manifest.get("converter_bundle_sha256"),
    ready.get("validator_bundle_sha256"),
    ready.get("validation_report_sha256"),
    ready.get("validation_attestation_id"),
)
for value in hash_fields:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise SystemExit(16)
attestation_identity = {
    "schema": ready.get("schema"),
    "artifact_content_sha256": ready.get("artifact_content_sha256"),
    "consumer_contract_sha256": ready.get("consumer_contract_sha256"),
    "validator_bundle_sha256": ready.get("validator_bundle_sha256"),
    "validation_report_sha256": ready.get("validation_report_sha256"),
}
if canonical_sha(attestation_identity) != ready.get("validation_attestation_id"):
    raise SystemExit(17)
report_path = ready_path.parent / "VALIDATION_REPORT.json"
if not report_path.is_file():
    raise SystemExit(18)
report = load(report_path)
if canonical_sha(report) != ready.get("validation_report_sha256"):
    raise SystemExit(19)
if report.get("schema") != "autoreskill.data_validation_report.v1" or report.get("valid") is not True or report.get("errors") != []:
    raise SystemExit(20)
for field, expected in (
    ("artifact_schema", manifest.get("artifact_schema")),
    ("artifact_content_sha256", artifact_hash),
    ("consumer_contract_sha256", consumer_hash),
    ("validator_bundle_sha256", ready.get("validator_bundle_sha256")),
    ("source_inventory_sha256", manifest.get("source_inventory_sha256")),
    ("converter_bundle_sha256", manifest.get("converter_bundle_sha256")),
):
    if report.get(field) != expected:
        raise SystemExit(21)
shards = manifest.get("shards")
if not isinstance(shards, list) or not shards:
    raise SystemExit(22)
seen = set()
total = 0
for shard in shards:
    relative = shard.get("path") if isinstance(shard, dict) else None
    if not isinstance(relative, str) or not relative or relative in seen:
        raise SystemExit(22)
    seen.add(relative)
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise SystemExit(23)
    size = shard.get("size_bytes")
    if not isinstance(size, int) or size < 0 or path.stat().st_size != size:
        raise SystemExit(24)
    if sha(path) != shard.get("sha256"):
        raise SystemExit(25)
    total += size
if total != manifest.get("total_size_bytes"):
    raise SystemExit(26)
for path in root.rglob("*"):
    if (
        path.is_file()
        and path.suffix.lower() not in {".h5", ".json", ".sha256"}
        and path.name not in {".ready", ".cache_node"}
    ):
        raise SystemExit(27)
print(f"{sha(ready_path)}\t{ready_path.relative_to(root).as_posix()}")
PY
}

bjtu_shm_copy_external_members() {
  local src="$1"
  local dest="$2"
  local ready_rel="$3"
  python3 - "$src" "$dest" "$ready_rel" <<'PY'
import json
import pathlib
import re
import shutil
import sys

source = pathlib.Path(sys.argv[1]).resolve()
destination = pathlib.Path(sys.argv[2]).resolve()
ready_rel = pathlib.PurePosixPath(sys.argv[3])
if ready_rel.is_absolute() or ".." in ready_rel.parts:
    raise SystemExit(30)
ready = (source / pathlib.Path(*ready_rel.parts)).resolve()
if source not in ready.parents or ready.name != "READY.json":
    raise SystemExit(30)
report = ready.parent / "VALIDATION_REPORT.json"
with (source / "DATA_ARTIFACT_MANIFEST.json").open("r", encoding="utf-8") as handle:
    manifest = json.load(handle)
members = [source / "DATA_ARTIFACT_MANIFEST.json", source / "ARTIFACT_COMPLETE.json", ready, report]
for shard in manifest.get("shards", []):
    relative = shard.get("path") if isinstance(shard, dict) else None
    if not isinstance(relative, str) or not re.fullmatch(r"shards/part-[0-9]{5}\.h5", relative):
        raise SystemExit(31)
    candidate = (source / relative).resolve()
    if source not in candidate.parents:
        raise SystemExit(31)
    members.append(candidate)
for member in members:
    if not member.is_file():
        raise SystemExit(32)
    relative = member.relative_to(source)
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(member, target)
PY
}

bjtu_shm_external_cache_ready() {
  local dest="$1"
  local artifact_hash="$2"
  local consumer_hash="$3"
  local ready_sha="$4"
  local ready_rel="$5"
  local observed observed_sha
  [[ -f "$dest/.ready" ]] || return 1
  [[ "$(cat "$dest/.artifact_content.sha256" 2>/dev/null)" == "$artifact_hash" ]] || return 1
  [[ "$(cat "$dest/.consumer_contract.sha256" 2>/dev/null)" == "$consumer_hash" ]] || return 1
  [[ "$(cat "$dest/.attestation_ready.sha256" 2>/dev/null)" == "$ready_sha" ]] || return 1
  [[ -f "$dest/DATA_ARTIFACT_MANIFEST.json" && -f "$dest/ARTIFACT_COMPLETE.json" ]] || return 1
  observed="$(bjtu_shm_validate_external_source "$dest" "$artifact_hash" "$consumer_hash" "$dest/$ready_rel")" || return 1
  observed_sha="${observed%%$'\t'*}"
  [[ "$observed_sha" == "$ready_sha" ]] || return 1
}

bjtu_shm_export_artifact_path() {
  local path="$1"
  local var_name="${2:-DATA_ARTIFACT_ROOT}"
  printf -v "$var_name" '%s' "$path"
  export "$var_name"
  export DATA_ARTIFACT_ROOT="$path"
  export DATA_ROOT="$path"
}

bjtu_stage_hdf5_artifact_to_shm() {
  local src="$1"
  local artifact_hash="$2"
  local consumer_hash="$3"
  local ready_path="$4"
  local var_name="${5:-DATA_ARTIFACT_ROOT}"
  local root="${SHM_CACHE_ROOT:-/dev/shm/bjtu_data_artifacts}"
  local lock_dir="$root/.locks"
  local dest lock
  local node_name="${SHM_NODE_NAME:-$(bjtu_shm_node_name)}"
  local min_free="${MIN_SHM_FREE_BYTES:-21474836480}"
  local max_pct="${MAX_SHM_STAGE_PCT:-70}"
  local src_bytes shm_total shm_avail shm_limit tmp ready_sha ready_rel validation_output copied_output copied_sha

  [[ "$artifact_hash" =~ ^[0-9a-f]{64}$ ]] || {
    bjtu_shm_log failed "reason=invalid_artifact_hash"
    return 2
  }
  [[ "$consumer_hash" =~ ^[0-9a-f]{64}$ ]] || {
    bjtu_shm_log failed "reason=invalid_consumer_hash"
    return 2
  }
  [[ -d "$src" ]] || {
    bjtu_shm_log failed "reason=missing_verified_source" "src=$src"
    return 2
  }
  validation_output="$(bjtu_shm_validate_external_source "$src" "$artifact_hash" "$consumer_hash" "$ready_path")" || {
    bjtu_shm_log failed "reason=source_identity_invalid" "src=$src" "artifact=$artifact_hash"
    return 2
  }
  ready_sha="${validation_output%%$'\t'*}"
  ready_rel="${validation_output#*$'\t'}"
  [[ -n "$ready_sha" && "$ready_rel" != "$validation_output" ]] || {
    bjtu_shm_log failed "reason=source_validation_output_invalid" "src=$src"
    return 2
  }
  dest="$root/$artifact_hash/$ready_sha"
  lock="$lock_dir/$artifact_hash.$ready_sha.lock"

  case "$root" in
    /dev/shm/*) ;;
    *)
      [[ "${BJTU_SHM_TEST_MODE:-0}" == "1" ]] || {
        bjtu_shm_log failed "reason=invalid_cache_root" "root=$root"
        return 2
      }
      ;;
  esac
  umask 0002
  mkdir -p "$root/$artifact_hash" "$lock_dir" || {
    bjtu_shm_log fallback "reason=cache_root_unavailable" "source=$src"
    bjtu_shm_export_artifact_path "$src" "$var_name"
    return 0
  }

  if bjtu_shm_external_cache_ready "$dest" "$artifact_hash" "$consumer_hash" "$ready_sha" "$ready_rel"; then
    bjtu_shm_export_artifact_path "$dest" "$var_name"
    bjtu_shm_log reused "node=$node_name" "artifact=$artifact_hash" "staged=$dest"
    return 0
  fi
  exec 9>"$lock" || {
    bjtu_shm_log fallback "reason=lock_open_failed" "source=$src"
    bjtu_shm_export_artifact_path "$src" "$var_name"
    return 0
  }
  flock 9 || {
    exec 9>&-
    bjtu_shm_log fallback "reason=lock_failed" "source=$src"
    bjtu_shm_export_artifact_path "$src" "$var_name"
    return 0
  }
  if bjtu_shm_external_cache_ready "$dest" "$artifact_hash" "$consumer_hash" "$ready_sha" "$ready_rel"; then
    bjtu_shm_export_artifact_path "$dest" "$var_name"
    exec 9>&-
    return 0
  fi
  if [[ -e "$dest" ]]; then
    bjtu_shm_log fallback "reason=existing_unverified_cache_not_overwritten" "staged=$dest" "source=$src"
    bjtu_shm_export_artifact_path "$src" "$var_name"
    exec 9>&-
    return 0
  fi

  src_bytes="$(du -sk "$src" | awk '{print $1 * 1024}')" || src_bytes=0
  if [[ -n "${BJTU_SHM_TEST_TOTAL_BYTES:-}" && -n "${BJTU_SHM_TEST_AVAILABLE_BYTES:-}" ]]; then
    shm_total="$BJTU_SHM_TEST_TOTAL_BYTES"
    shm_avail="$BJTU_SHM_TEST_AVAILABLE_BYTES"
  else
    read -r shm_total shm_avail < <(df -B1 --output=size,avail /dev/shm | awk 'NR==2{print $1, $2}')
  fi
  shm_limit=$(( shm_total * max_pct / 100 ))
  if (( src_bytes > shm_limit || shm_avail <= src_bytes + min_free )); then
    bjtu_shm_log fallback "reason=capacity" "artifact=$artifact_hash" "source=$src"
    bjtu_shm_export_artifact_path "$src" "$var_name"
    exec 9>&-
    return 0
  fi

  tmp="${dest}.copying.${SLURM_JOB_ID:-manual}.${SLURM_ARRAY_TASK_ID:-0}.$$"
  mkdir -p "$tmp" || {
    bjtu_shm_export_artifact_path "$src" "$var_name"
    exec 9>&-
    return 0
  }
  if ! bjtu_shm_copy_external_members "$src" "$tmp" "$ready_rel"; then
    rm -rf "$tmp"
    bjtu_shm_log fallback "reason=copy_or_hash_failed" "artifact=$artifact_hash" "source=$src"
    bjtu_shm_export_artifact_path "$src" "$var_name"
    exec 9>&-
    return 0
  fi
  copied_output="$(bjtu_shm_validate_external_source "$tmp" "$artifact_hash" "$consumer_hash" "$tmp/$ready_rel")" || {
    rm -rf "$tmp"
    bjtu_shm_log fallback "reason=copy_or_hash_failed" "artifact=$artifact_hash" "source=$src"
    bjtu_shm_export_artifact_path "$src" "$var_name"
    exec 9>&-
    return 0
  }
  copied_sha="${copied_output%%$'\t'*}"
  if [[ "$copied_sha" != "$ready_sha" ]]; then
    rm -rf "$tmp"
    bjtu_shm_log fallback "reason=copy_attestation_mismatch" "artifact=$artifact_hash" "source=$src"
    bjtu_shm_export_artifact_path "$src" "$var_name"
    exec 9>&-
    return 0
  fi
  printf '%s\n' "$artifact_hash" > "$tmp/.artifact_content.sha256"
  printf '%s\n' "$consumer_hash" > "$tmp/.consumer_contract.sha256"
  printf '%s\n' "$ready_sha" > "$tmp/.attestation_ready.sha256"
  printf '%s\n' "$node_name" > "$tmp/.cache_node"
  touch "$tmp/.ready"
  if ! mv "$tmp" "$dest"; then
    rm -rf "$tmp"
    bjtu_shm_log fallback "reason=atomic_publish_failed" "source=$src"
    bjtu_shm_export_artifact_path "$src" "$var_name"
    exec 9>&-
    return 0
  fi
  bjtu_shm_export_artifact_path "$dest" "$var_name"
  bjtu_shm_log enabled "node=$node_name" "artifact=$artifact_hash" "src_bytes=$src_bytes" "staged=$dest"
  exec 9>&-
}
