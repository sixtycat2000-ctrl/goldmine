This is a separate 60-question absent-answer fragment for the new expansion
bundle. It stays DRAFT until reviewed against the complete merged source-view
inventory and independently audited. It contains no source files and adds no
synthetic text to the indexed corpus.

The questions were individually written by an AI curator: 20 near-topic
requests ask for specific measurements, runtime records, project configuration
or historical receipts that the actual topic documents do not supply;
20 unrelated requests concern records outside the corpus scope; 20 conflicting
identifiers mutate genuine corpus APIs, constants or versions and request
unsupported details about those changed identities. A nearby genuine API is
not labeled as evidence for the changed identifier.

Every query has empty `expect`, `required_facts` and `needs`, with a distinct
`noise:<id>` group. Source-first reasons and literal discriminators are retained
in `absence-notes.json`, outside all indexed assets. All questions and reasons
are literal authoring entries in `author_noise.py`; no template generates the
question wording. The fragment has not been scored or run against a candidate.

`audit_absence.py --manifest <complete-aggregate-manifest.json>` records the
exact inventory and searches every registered source view. `--shards` can
inspect an interim set but explicitly reports PARTIAL_INVENTORY_ONLY. A lack
of exact discriminator hits does not prove unanswerability: each source-first
scope reason must also be checked, and the inventory receipt must match the
eventual aggregate source set. Source scope, version or label changes require
another absence review before freeze. No human review or FROZEN state is
claimed by this fragment.
