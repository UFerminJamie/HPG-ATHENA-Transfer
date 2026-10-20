# HPG-ATHENA-Transfer

Attach JSON annotations generated on HiPerGator to the matching whole-slide image items in [Athena](https://athena.rc.ufl.edu/). The tool calls Athena's Girder/DSA annotation API, so annotations appear in each slide's **Annotations** panel. It never uploads JSON as an ordinary folder file.

## Install and authenticate

Run on a machine that can read the HiPerGator source directory and reach `https://athena.rc.ufl.edu` over HTTPS. The script reads JSON directly from the shared filesystem; there is no need to copy files to a laptop.

```bash
cd HPG-ATHENA-Transfer
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
hpg-athena-transfer --help
```

Set `ATHENA_API_KEY` in the process environment, or place it in a local `.env` file in the directory where you run the command. `.env` is ignored by Git. Keep the key out of shell history, command arguments, manifests, and shared logs. The program exchanges the API key for a Girder token and never prints either value.

## HiPerGator workflow

The default source directory is:

`/orange/pinaki.sarder/j.fermin/cort_med_wsi/trident_processed_final/KPMP/inference_jsons/svc_2_rbf_1.0/`

Use `--source` to choose another directory. Top-level discovery is the default; `--recursive` includes subdirectories. Set `--destination` to a collection or folder URL/ID. A folder URL copied from Athena's browser UI is accepted, including a `#user/.../folder/<id>` route.

```bash
hpg-athena-transfer --check-connection
hpg-athena-transfer --destination 'https://athena.rc.ufl.edu/#collection/0123456789abcdef01234567' --dry-run
hpg-athena-transfer --destination 'https://athena.rc.ufl.edu/#collection/0123456789abcdef01234567' --apply
```

The ID above is synthetic. Use your actual authorized collection or folder. `--check-connection --destination URL` also checks access to that destination. `--api-url` defaults to `https://athena.rc.ufl.edu/api/v1`. A bare ID is probed as a folder and collection; if it cannot be resolved uniquely, use a URL. `--manifest PATH` changes the report location.

**Dry-run is always the default.** Only `--apply` creates annotations. Review the dry-run manifest and any unmatched or ambiguous rows before applying.

## Matching and annotation format

Only these exact source suffixes are recognized:

| Suffix | Annotation type |
| --- | --- |
| `_heatmap.json` | Heatmap |
| `_refined_heatmap.json` | Refined Heatmap |
| `_mask_gt.json` | Ground Truth Mask |
| `_mask_prediction.json` | Prediction Mask |

The sample name is the filename with exactly one recognized suffix removed. It must equal the DSA item's name, or a file name inside the item, after removing a WSI extension (extension case is ignored; sample-name case is preserved). `.svs`, `.tif`, `.tiff`, and common other WSI extensions are recognized. For an item with `largeImage` metadata, the item's image filename can use any extension. The search includes the selected folder or collection and all descendant folders, using paginated Girder calls. An item must have a recognized WSI filename or large-image metadata to count as a slide. Duplicate source files with the same sample and type, multiple slides with the same basename, ambiguous matches, and unmatched files are reported and skipped. No fuzzy matching is used.

Input may be one native DSA annotation object, a single `{"annotation": {...}}` wrapper, or a list of these. The current KPMP JSON files each contain **two** native annotation documents (cortical and medullary); each document becomes a separate entry in the slide's Annotations panel. This preserves their separate names, attributes, elements, holes, colors, and display settings. A source annotation's valid name is kept; the suffix-derived type appears in provenance and the manifest. Empty element lists are valid for a named native annotation. Malformed or unsupported structures are skipped.

The tool adds `annotation.attributes.hpg_athena_transfer` with a source filename, SHA-256 hash, document index, and annotation type. It checks that marker and hash on subsequent runs, skipping matching documents. It does not replace or delete annotations. After each create request it queries the item's annotation list and confirms the returned ID and name. A failed create can be retried; if a timeout happened after Athena committed it, the next run recognizes the saved hash.

## Reports and failure recovery

`transfer-manifest.json` is written after each processed annotation and ignored by Git. It records source path, sample, slide name/ID, type, SHA-256, document index, annotation ID/name, status, and error. Statuses are `planned`, `uploaded`, `skipped-identical`, `unmatched`, `ambiguous`, `invalid`, or `failed`. Console and manifest totals report both files and annotation records; a JSON containing two native documents contributes two records. Unmatched, ambiguous, and invalid files contribute one row because no annotation is created. A file with mixed record outcomes is counted under its most serious status.

If a run stops, rerun the same `--apply` command. Already uploaded documents are skipped by provenance and hash. Inspect `invalid` rows for unsupported or malformed DSA documents and `failed` rows for API/network issues. A 403 means the account lacks access to the destination or annotation write permission; 401 suggests an invalid API key. A network failure means the machine may not reach Athena over HTTPS. Check the [Athena REST API](https://athena.rc.ufl.edu/api/v1) and [large_image annotation schema](https://girder.github.io/large_image/annotations.html) for accepted annotation fields.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests use synthetic annotation documents and mocked Athena API calls. They never read the real JSON directory or require an Athena account.

## Submit a HiPerGator job

The included [`run_transfer.sh`](run_transfer.sh) requests the `sarder-hubmap` account on `hpg-default` with **one CPU, 4 GB RAM, and eight hours**. It uses the project's source directory without installing a package and reads the API key from the environment or the ignored `.env` in this directory. Submit from the project directory so Slurm logs land here.

If you use `.env`, create it interactively so the key is not typed into a saved command:

```bash
cd /blue/pinaki.sarder/j.fermin/HPG-ATHENA-Transfer
umask 077
read -rsp 'Athena API key: ' ATHENA_API_KEY; printf '\n'
printf 'ATHENA_API_KEY=%s\n' "$ATHENA_API_KEY" > .env
unset ATHENA_API_KEY
```

Use the real Athena collection or folder URL in place of the synthetic ID below. Keep the URL in single quotes because Athena browser URLs contain `#`.

```bash
sbatch run_transfer.sh 'https://athena.rc.ufl.edu/#collection/0123456789abcdef01234567'
squeue -u "$USER"
```

The first job is a dry-run. Read `slurm-JOBID.out` and `transfer-manifest-JOBID.json`. Once the matches look correct, submit the same destination with `--apply`:

```bash
sbatch run_transfer.sh 'https://athena.rc.ufl.edu/#collection/0123456789abcdef01234567' --apply
```

Each job uses a separate manifest named with its Slurm job ID. Logs and manifests are ignored by Git. You may pass any other CLI option after the destination, such as `--recursive` or `--source DIR`. If the job reports a connection failure, confirm the compute node can reach Athena over HTTPS.
