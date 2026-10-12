# TerminalBench evaluator-owned dependency images

These images preserve the pinned official TerminalBench base image and move
network-dependent `apt` installation out of scored trials. The official task
assets, build commands after dependency installation, tests, fixtures and
reward parser remain unchanged.

Build once on an evaluator host, inspect `/opt/agentswe-system-packages.txt`,
then lock the resulting immutable image ID in `terminalbench_controller.py`:

```bash
docker build -f write-compressor.Dockerfile \
  -t agentswe/tbench-write-compressor-deps:20260812 .
docker build -f reverse-engineering.Dockerfile \
  -t agentswe/tbench-reverse-engineering-deps:20260812 .
docker build -f accelerate-maximal-square.Dockerfile \
  -t agentswe/tbench-accelerate-maximal-square-deps:20260813 .
docker build -f csv-to-parquet.Dockerfile \
  -t agentswe/tbench-csv-to-parquet-deps:20260813 .
```

A mirror only transports public Ubuntu packages during image preparation.
Formal evaluation verifies the local image ID and performs no online package
installation for these system dependencies.

The formal controller also locks evaluator-owned compressed `docker save`
archives under `$AGENTSWE_HOME/assets/terminalbench/images`. If a
derived tag is missing or points at a different image ID, the controller first
checks the archive byte size and SHA-256, loads it, and then checks the restored
image ID. A missing, changed, or incorrectly restored archive is infrastructure
failure; it is never accepted as an agent score.

For upstream task Dockerfiles that still install or download their own fixed
dependencies, the migrated temporary build context is transport-stabilized:

- Ubuntu/Debian apt sources use the configured public mirror (the paper used a public mirror) over signed apt metadata;
- pip uses the configured simple index with the upstream version constraints;
- curl/wget receive bounded retry and connection timeouts;
- `train-fasttext` receives the two upstream Yelp parquet files from
  `$AGENTSWE_HOME/assets/terminalbench/task-assets/train-fasttext`
  only after controller-enforced byte-size and SHA-256 checks.

These changes do not modify the evaluator-owned native task tree, official test
payload, package versions, task instruction, fixtures, or reward parser. The
paper's formal prebuild evidence was generated using the
same migration and stabilization functions as scored trials.

## Release notes

- Setup fetches the locked archive, the task build assets and the verifier wheels by SHA-256 into
  `$AGENTSWE_HOME/assets/terminalbench` (task.json `runner_config`); the official base images are pulled
  from ghcr.io by index digest. Only `write-compressor` is used by the current 10/40 split; the other
  three dependency images belong to the superseded 3/5 split and are not fetched.
- Image identity is checked by content: the controller compares `docker image inspect .RootFS.Layers`
  with the layer digests of the locked image configs (`PINNED_IMAGE_LAYERS`), so the same image verifies
  on the classic and the containerd image store. The protocol image IDs are still accepted.
- The apt/pip build-transport mirrors default to the paper's and can be pointed at another mirror of the
  same archives (`AGENTSWE_TERMINALBENCH_APT_MIRROR`, `AGENTSWE_TERMINALBENCH_PIP_INDEX`).
