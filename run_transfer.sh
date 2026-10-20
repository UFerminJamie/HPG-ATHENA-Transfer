#!/usr/bin/env bash
#SBATCH --job-name=hpg-athena-transfer
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=08:00:00
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err

set -euo pipefail

if [[ $# -lt 1 || $1 == --help || $1 == -h ]]; then
    echo "Usage: sbatch run_transfer.sh ATHENA_COLLECTION_OR_FOLDER_URL_OR_ID --source JSON_DIR [--apply] [other CLI options]" >&2
    echo "Default: dry-run. Add --apply only after reviewing its manifest." >&2
    exit 2
fi

destination=$1
shift

project_dir=$(cd -- "${SLURM_SUBMIT_DIR:-$(dirname -- "${BASH_SOURCE[0]}")}" && pwd)
if [[ ! -f $project_dir/pyproject.toml ]]; then
    echo "Submit this job from the transfer project directory" >&2
    exit 2
fi
cd -- "$project_dir"
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is unavailable on this node" >&2
    exit 2
fi

# The CLI reads ATHENA_API_KEY from the environment or this project's ignored
# .env file. Neither the key nor the Girder token is passed as an argument.
manifest="$project_dir/transfer-manifest-${SLURM_JOB_ID:-manual}.json"
exec python3 -m hpg_athena_transfer --destination "$destination" --manifest "$manifest" "$@"
