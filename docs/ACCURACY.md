# Offline accuracy evaluation

The evaluator uses the G2 YuNet detector, SFace embedder, quality checks, and
cosine matcher. It loads the dataset and model files in memory and does not
write to the application database.

## Collecting a dataset

Get **written consent** from every participant before collecting or evaluating
their images. Explain how the images and resulting face templates will be used,
who can access them, and how to request deletion. Store the evaluation dataset
locally and do not commit it.

For a useful evaluation, collect photos from around **20–30 people**. Include
varied lighting, slight changes in face angle, glasses or a cap where
applicable, different camera distances, and different camera heights. Use
several separate photos per person rather than copies or near-identical frames.
Reserve photos not used in enrollment/gallery as probes.

Arrange user-supplied photos as:

```text
data/eval/
  PERSON-001/
    01.jpg
    02.jpg
    03.jpg
    04.jpg
  PERSON-002/
    01.jpg
    02.jpg
    03.jpg
    04.jpg
  _unknown/
    01.jpg
```

Each person's images are sorted by filename; the first `K` form that person's
gallery and the remaining images are known probes. `_unknown` images are
impostor probes and must depict people who are **not enrolled** in the other
folders. Use at least one image beyond `K` for each enrolled identity to get a
known-probe rate. Unknown images are needed to measure false accepts.

Download model files once if needed, then run:

```text
python -m tools.download_models
python -m tools.eval_accuracy --k 3 --margin 0.05
```

The report is written to `data/eval/report.html` and `data/eval/report.csv`.
For a video, extract usable face frames before sorting them into a person's
folder:

```text
python -m tools.extract_frames input.mp4 data/eval/PERSON-001 --every 5
```

## Reading the report

- **True-accept rate (TAR):** known probes accepted as the correct person,
  divided by usable known probes.
- **False-reject rate (FRR):** known probes rejected as unknown, divided by
  usable known probes.
- **Wrong-person rate:** known probes accepted as a different enrolled person,
  divided by usable known probes. This is separate from false rejects.
- **False-accept rate (FAR):** unknown probes accepted as any enrolled person,
  divided by usable unknown probes.
- **Top-1 accuracy:** correct known-person matches divided by usable known
  probes at the default matcher threshold (`0.363`).
- **Dropped images:** images rejected before matching, counted by reason such
  as no face, multiple faces, blur, or face size.

The threshold table and inline curve show the tradeoff as the cosine threshold
changes. The command also prints the threshold with the highest TAR among
thresholds with FAR at or below 0.1%; if the dataset contains no unknown probes,
FAR cannot be measured and no threshold is reported as meeting that target.

Rates are only estimates for this dataset, not guarantees of real-world
performance. Small or unrepresentative datasets, especially those lacking
unknown people or varied capture conditions, can make the rates misleading.
